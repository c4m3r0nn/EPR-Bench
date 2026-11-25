"""The second spend-safety review (docs/codex/readiness-fixes-2026-11-18/codex-review-2.md), each finding reproduced:
usage that is missing or implausible never releases money; only real images in the providers' image fields earn the
image allowance; a ledger cannot be rolled back, replaced or swapped for a fresh one unseen; a spend refusal stops a
plan; screenshots count as read only once delivered; plans refuse mutable model aliases and stop on a new model
version at once. Scripted models and fake prices only: nothing here costs anything."""

import base64
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from eprbench import runner, spend
from eprbench.agents import claude_tools, openai_tools
from eprbench.env import EPREnv
from tests.test_agents import ScriptedOpenAI, oa_response, text, tool_use, unmetered_test_client
from tests.test_spend import MODEL, PRICES, budget, costly, entries, prices

WHO = {"model": MODEL, "provider": "anthropic", "interface": "tool"}


def png(width: int, height: int) -> str:
    """A real PNG of the given size, base64."""
    raw = b"".join(b"\x00" + b"\x00" * width * 3 for _ in range(height))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    body = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(body).decode()


# --- 1. usage that is missing, partial or implausible keeps the whole reservation ------------------------------------


def test_review2_1_an_openai_response_without_usage_keeps_its_reservation(tmp_path):
    b = budget(tmp_path)
    nousage = NS(id="r1", output=[], output_text="Hb 109 g/L", status="completed", usage=None)
    r = openai_tools.run_episode(EPREnv("find-result-001"), ScriptedOpenAI(nousage), MODEL, spend=b, max_output_tokens=1000)
    (rec,) = [e for e in entries(tmp_path / "ledger.jsonl") if e["kind"] == "reconcile"]
    (res,) = [e for e in entries(tmp_path / "ledger.jsonl") if e["kind"] == "reserve"]
    assert rec["outcome"] == "kept" and rec["gbp"] == res["gbp"] and "usage" in rec["reason"] and r["cost"]["kept"] == 1


@pytest.mark.parametrize("usage, why", [
    (NS(input_tokens=100, output_tokens=10), "cache_creation_input_tokens"),  # a token class missing
    (NS(input_tokens=None, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0), "input_tokens"),
    (NS(input_tokens=0, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0), "no input"),  # nothing read from a 30 KB request
    (NS(input_tokens=100.5, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0), "input_tokens"),
    (NS(input_tokens=100, output_tokens=-1, cache_creation_input_tokens=0, cache_read_input_tokens=0), "output_tokens"),
])  # fmt: skip
def test_review2_1_partial_or_implausible_usage_keeps_the_reservation_and_says_why(tmp_path, usage, why):
    b = budget(tmp_path)
    client = NS(messages=NS(create=lambda **kw: NS(content=[text("Hb 109 g/L.")], stop_reason="end_turn", usage=usage)))
    client.with_options = lambda **k: client
    claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000, spend=b)
    (rec,) = [e for e in entries(tmp_path / "ledger.jsonl") if e["kind"] == "reconcile"]
    assert rec["outcome"] == "kept" and why in rec["reason"]


def test_review2_1_a_continued_request_is_never_reconciled_below_the_context_it_carries(tmp_path):
    b = budget(tmp_path)
    m = b.meter({"model": MODEL, "provider": "openai", "interface": "tool"}, "t", 0)
    rid, _ = m.reserve({"input": "x", "max_output_tokens": 100}, False)
    m.settle(rid, {"input_tokens": 50_000, "cached_input_tokens": 0, "output_tokens": 10})
    params = {"input": "y", "previous_response_id": "r1", "max_output_tokens": 100}
    rid, _ = m.reserve(params, False)
    out = m.settle(rid, {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 5})  # less than the 50,000 it continues
    assert out["outcome"] == "kept" and "context" in out["reason"]


# --- 2. only real images in the image fields earn the image allowance -------------------------------------------------

FAKE = "data:image/png;base64," + "A" * 300_000  # text that looks like an image
FAKE_B64 = "A" * 300_000


@pytest.mark.parametrize("params", [
    {"system": FAKE, "messages": [], "max_tokens": 1},
    {"messages": [{"role": "user", "content": FAKE}], "max_tokens": 1},
    {"messages": [{"role": "user", "content": [{"type": "text", "text": FAKE}]}], "max_tokens": 1},
    {"messages": [{"role": "assistant", "content": [{"type": "text", "text": FAKE}]}], "max_tokens": 1},
    {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": FAKE_B64}}}]}], "max_tokens": 1},
    {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": FAKE}]}], "max_tokens": 1},
    {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": FAKE}]}]}], "max_tokens": 1},
    {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": FAKE_B64}}]}], "max_tokens": 1},
    {"input": FAKE, "max_output_tokens": 1},
    {"input": [{"role": "user", "content": FAKE}], "max_output_tokens": 1},
    {"input": [{"role": "user", "content": [{"type": "input_text", "text": FAKE}]}], "max_output_tokens": 1},
    {"input": [{"type": "function_call_output", "call_id": "c", "output": FAKE}], "max_output_tokens": 1},
    {"input": [{"type": "computer_call_output", "call_id": "c", "output": {"type": "computer_screenshot", "image_url": FAKE}}], "max_output_tokens": 1},
    {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": FAKE}]}], "max_output_tokens": 1},
    {"instructions": FAKE, "input": "x", "max_output_tokens": 1},
])  # fmt: skip
def test_review2_2_text_that_looks_like_an_image_is_counted_by_its_bytes(params):
    size = spend.measure(params)
    assert size["images"] == 0 and size["text_bytes"] >= 300_000, size


def test_review2_2_a_real_screenshot_in_an_image_field_earns_the_image_allowance():
    shot = png(1280, 800)
    for params in ({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                                               "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": shot}}]}]}], "max_tokens": 1},
                   {"input": [{"type": "computer_call_output", "call_id": "c", "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64," + shot}},
                              {"role": "user", "content": [{"type": "input_text", "text": "note"}, {"type": "input_image", "image_url": "data:image/png;base64," + shot}]}],
                    "max_output_tokens": 1}):  # fmt: skip
        size = spend.measure(params)
        assert size["images"] in (1, 2) and size["text_bytes"] < 2000 and size["serialised_bytes"] > len(shot), size
    wide = png(4000, 10)  # a real image, but larger than the viewport max_image_tokens is priced for: counted by its bytes
    size = spend.measure({"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": wide}}]}],
                          "max_tokens": 1})  # fmt: skip
    assert size["images"] == 0 and size["text_bytes"] >= len(wide)


# --- 3. a ledger cannot be rolled back, replaced or swapped unseen --------------------------------------------------


def reserve_one(b, n=0):
    return b.meter(WHO, "t", n).reserve({"max_tokens": 100, "messages": "x"}, False)[0]


def test_review2_3_restoring_an_earlier_copy_of_the_ledger_is_refused(tmp_path):
    b = budget(tmp_path)
    path = tmp_path / "ledger.jsonl"
    shutil.copy(path, tmp_path / "early.jsonl")
    assert reserve_one(b)
    shutil.copy(tmp_path / "early.jsonl", path)  # a valid earlier prefix: every hash in it is right
    with pytest.raises(spend.LedgerError, match="behind its anchor"):
        spend.Ledger(path)


def test_review2_3_a_different_ledger_at_the_same_path_is_refused(tmp_path):
    budget(tmp_path)
    path = tmp_path / "ledger.jsonl"
    spend.Ledger.init(tmp_path / "other.jsonl", 1.0)
    shutil.move(tmp_path / "other.jsonl", path)
    with pytest.raises(spend.LedgerError, match="not the ledger"):
        spend.Ledger(path)
    path.unlink()
    with pytest.raises(spend.LedgerError, match="already had a ledger"):
        spend.Ledger.init(path, 1.0)  # a fresh one where one was is refused, unless said so
    spend.Ledger.init(path, 1.0, replace_reason="owner discarded the test ledger of 18 November")
    assert entries(path)[0]["replaces"]["reason"].startswith("owner discarded")


def test_review2_3_a_ledger_replaced_while_waiting_for_its_lock_is_refused(tmp_path, monkeypatch):
    b = budget(tmp_path)
    path = tmp_path / "ledger.jsonl"
    real = spend.fcntl.flock
    swapped = []

    def flock(fd, op):
        real(fd, op)
        if op == spend.fcntl.LOCK_EX and not swapped:
            swapped.append(1)
            shutil.copy(path, tmp_path / "copy.jsonl")
            shutil.move(tmp_path / "copy.jsonl", path)  # same content, a new file: the lock no longer guards it

    monkeypatch.setattr(spend.fcntl, "flock", flock)
    with pytest.raises(spend.LedgerError, match="replaced"):
        reserve_one(b)


# --- 3, 4, 6, 7 through plans ------------------------------------------------------------------------------------


def run_cli(monkeypatch, *argv):
    from eprbench.__main__ import main

    monkeypatch.setattr(sys, "argv", ["eprbench", *argv])
    main()


class Answering:
    def __init__(self, seen, versions=("fake-model-20260101",)):
        self.seen, self.versions = seen, list(versions)
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.seen.append(kw)
        version = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        return NS(content=[text("Hb 109 g/L.")], stop_reason="end_turn", model=version,
                  usage=NS(input_tokens=30_000, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0))


def plan_with_ledger(monkeypatch, capsys, tmp_path, repeats=2, ceiling=1.0, price_file=None, extra=()):
    price_file = price_file or prices(tmp_path).path
    spend.Ledger.init(tmp_path / "ledger.jsonl", ceiling)
    path = tmp_path / "plan.json"
    run_cli(monkeypatch, "plan", "find-result-001", "--models", f"anthropic:{MODEL}", "--repeats", str(repeats), "--seed", "3", "--interfaces", "tool",
            "--out", str(path), "--max-output-tokens", "1000", "--prices", str(price_file), "--ledger", str(tmp_path / "ledger.jsonl"), *extra)  # fmt: skip
    capsys.readouterr()
    return path, ["--ledger", str(tmp_path / "ledger.jsonl"), "--prices", str(price_file)]


def test_review2_3_a_plan_is_bound_to_its_ledger_and_its_evidence_to_the_ledger_it_spent(monkeypatch, capsys, tmp_path):
    path, money = plan_with_ledger(monkeypatch, capsys, tmp_path)
    plan = json.loads(path.read_text())
    assert plan["ledger"]["ledger_id"] == spend.Ledger(tmp_path / "ledger.jsonl").read().ledger_id
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen))
    out = tmp_path / "ev"
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    capsys.readouterr()
    led = entries(tmp_path / "ledger.jsonl")
    assert [e for e in led if e["kind"] == "bind"][0]["plan_sha256"] == runner.load_plan(path)[1]
    for d in out.iterdir():
        m = json.loads((d / "manifest.json").read_text())["ledger"]
        assert m["ledger_id"] == plan["ledger"]["ledger_id"] and led[m["last_seq"] - 1]["hash"] == m["last_hash"]
    # both the ledger and its anchors deleted, a fresh ledger made in their place: the plan and the evidence refuse it
    (tmp_path / "ledger.jsonl").unlink()
    shutil.rmtree(spend.state_dir())
    spend.Ledger.init(tmp_path / "ledger.jsonl", 1.0)
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert e.value.code == 2 and "ledger" in capsys.readouterr().err
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money, "--new-ledger-for-existing-run", "the owner lost the ledger and accepts the risk")
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["plan"]["ran"] == 0  # nothing left to run; the override is on record
    assert [e for e in entries(tmp_path / "ledger.jsonl") if e["kind"] == "override"][0]["reason"].startswith("the owner lost")


def test_review2_7_resume_refuses_evidence_from_a_rolled_back_or_different_ledger(monkeypatch, capsys, tmp_path):
    path, money = plan_with_ledger(monkeypatch, capsys, tmp_path)
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen))
    out = tmp_path / "ev"
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    capsys.readouterr()
    d = sorted(out.iterdir())[0]
    m = json.loads((d / "manifest.json").read_text())
    m["ledger"]["last_hash"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert e.value.code == 2 and "does not match the ledger" in capsys.readouterr().err


def test_review2_4_a_spend_refusal_stops_the_plan_and_is_not_complete_until_the_ceiling_is_raised(monkeypatch, capsys, tmp_path):
    dear = prices(tmp_path, {**PRICES, "models": {MODEL: {**PRICES["models"][MODEL], "per_million_tokens": {"input": 1, "cache_write": 1, "cache_read": 1, "output": 10}}}},
                  name="dear.json")  # fmt: skip
    path, money = plan_with_ledger(monkeypatch, capsys, tmp_path, ceiling=0.02, price_file=dear.path)  # 30 KB of input at 1/M and 1,000 output: over 0.02
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen))
    out = tmp_path / "ev"
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    err = capsys.readouterr().err
    assert e.value.code == 3 and "spend" in err and seen == [] and len(list(out.iterdir())) == 1  # stopped at the first refusal
    (d,) = out.iterdir()
    assert json.loads((d / "manifest.json").read_text())["complete"] is False and not runner.completed(out, runner.load_plan(path)[1])
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert e.value.code == 3 and "eprbench ledger raise" in capsys.readouterr().err and len(list(out.iterdir())) == 1  # not retried unchanged
    run_cli(monkeypatch, "ledger", "raise", str(tmp_path / "ledger.jsonl"), "--ceiling", "1", "--reason", "owner approved")
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["plan"]["ran"] == 2 and len(seen) == 2


def test_review2_6_plans_refuse_mutable_aliases_unless_allowed_on_record(monkeypatch, capsys, tmp_path):
    spend.Ledger.init(tmp_path / "ledger.jsonl", 1.0)
    alias = {**PRICES, "models": {"fake-model-latest": PRICES["models"][MODEL]}}
    p = prices(tmp_path, alias, name="alias.json")
    argv = ["plan", "find-result-001", "--models", "anthropic:fake-model-latest", "--repeats", "1", "--seed", "1", "--interfaces", "tool",
            "--out", str(tmp_path / "plan.json"), "--prices", str(p.path), "--ledger", str(tmp_path / "ledger.jsonl")]  # fmt: skip
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, *argv)
    assert e.value.code == 2 and "pinned" in capsys.readouterr().err
    run_cli(monkeypatch, *argv, "--allow-alias")
    assert json.loads((tmp_path / "plan.json").read_text())["allow_alias"] is True


def test_review2_6_a_new_model_version_stops_the_plan_at_its_first_response_and_a_missing_one_is_unverified(monkeypatch, capsys, tmp_path):
    path, money = plan_with_ledger(monkeypatch, capsys, tmp_path, repeats=3)
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen, ["fake-model-20260101", "fake-model-20260202"]))
    out = tmp_path / "ev"
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert e.value.code == 3 and "fake-model-20260202" in capsys.readouterr().err and len(seen) == 2  # the second episode's first response
    drifted = [json.loads((d / "result.json").read_text()) for d in out.iterdir()]
    assert sorted(r["terminal_reason"] for r in drifted) == ["harness_error", "stopped_without_submit"]
    client = unmetered_test_client(NS(messages=NS(create=lambda **kw: costly(text("Hb 109 g/L."), stop_reason="end_turn"))))  # its responses name no model
    client.with_options = lambda **k: client
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000)
    assert r["model_version"] == "unverified"


# --- 5. a screenshot counts as read once it is delivered -------------------------------------------------------------


def test_review2_5_a_captured_screenshot_is_not_read_until_a_request_delivers_it(tmp_path):
    from tests.test_agents import ScriptedClient, reply
    from tests.test_typed_answers import RESULT, TASK, spots
    from eprbench.agents import claude_browser

    at = spots()
    click = lambda name, id: tool_use("left_click", {"coordinate": at[name]}, id)  # noqa: E731
    walk = [click("ice", "a"), click("search", "b"), tool_use("type", {"text": "H4417302"}, "c"), tool_use("key", {"text": "Return"}, "d"),
            click("patient", "e"), click("results", "f"), click("fbc", "g"), tool_use("screenshot", {}, "h")]  # fmt: skip
    # the screenshot is taken, but no request follows (the request limit, no final chance): the image never reaches the model
    from eprbench.episode import Limits

    claude_browser.run_episode(TASK, ScriptedClient(reply(*walk)), limits=Limits(max_requests=1, final_chance=False), out_dir=tmp_path / "none")
    claude_browser.run_episode(TASK, ScriptedClient(reply(*walk), reply(text("Hb 109 g/L."), stop_reason="end_turn")), out_dir=tmp_path / "sent")
    rows = lambda where: [json.loads(x) for x in next((tmp_path / where).iterdir()).joinpath("reads.jsonl").read_text().splitlines()]  # noqa: E731
    assert not [r for r in rows("none") if r["via"] == "screenshot"]
    sent = [r for r in rows("sent") if r["via"] == "screenshot"]
    assert ("result", RESULT) in {(r["kind"], r["object_id"]) for r in sent}
    events = [json.loads(x) for x in next((tmp_path / "sent").iterdir()).joinpath("transcript.jsonl").read_text().splitlines()]
    (delivery,) = [e for e in events if e["kind"] == "screenshot_reads"]
    assert delivery["request"] == 2 and delivery["entries"] >= 1  # recorded with the request that carried the image
