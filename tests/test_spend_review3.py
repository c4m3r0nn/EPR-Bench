"""The third spend-safety review (docs/codex/readiness-fixes-2026-11-18/codex-review-3.md), each finding reproduced:
missing anchors fail closed; a final-chance refusal ends the episode at spend_limit; uncertain usage does not poison
later requests; an override covers only the replacement it approved; harnesses refuse a real client without a meter;
direct runs need --allow-alias for an undated model id; only a structurally complete PNG is an image; and the
feasibility estimate. Scripted models and fake prices only: nothing here costs anything."""

import json
import os
import shutil
import stat
import sys
from types import SimpleNamespace as NS

import pytest

from eprbench import spend
from eprbench.agents import claude_tools, openai_tools
from eprbench.env import EPREnv
from eprbench.episode import Limits
from tests.test_agents import ScriptedClient, reply, text, tool_use, unmetered_test_client
from tests.test_spend import MODEL, PRICES, budget, costly, entries, prices
from tests.test_spend_review2 import WHO, Answering, plan_with_ledger, png, reserve_one, run_cli


# --- A. anchors fail closed --------------------------------------------------------------------------------------------


def test_review3_a_a_ledger_with_entries_but_no_anchor_is_refused_unless_adopted_on_record(tmp_path):
    b = budget(tmp_path)
    path = tmp_path / "ledger.jsonl"
    shutil.copy(path, tmp_path / "early.jsonl")
    assert reserve_one(b)
    shutil.copy(tmp_path / "early.jsonl", path)  # an earlier prefix...
    shutil.rmtree(spend.state_dir())  # ...with the anchors gone too
    with pytest.raises(spend.LedgerError, match="no anchor"):
        spend.Ledger(path)
    spend.Ledger.adopt(path, "moved the ledger to this machine; checked it against the evidence")
    assert entries(path)[-1]["kind"] == "override" and spend.Ledger(path).read().ledger_id


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="running as root, which can write to a read-only directory")
def test_review3_a_anchors_are_private_and_an_unwritable_state_directory_stops_before_the_ledger_changes(tmp_path):
    b = budget(tmp_path)
    for f in spend.state_dir().rglob("*.json"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    path = tmp_path / "ledger.jsonl"
    before = path.read_bytes()
    anchors = spend.state_dir() / "ledgers"
    os.chmod(anchors, 0o500)
    try:
        with pytest.raises((spend.LedgerError, OSError)):
            reserve_one(b)
    finally:
        os.chmod(anchors, 0o700)
    assert path.read_bytes() == before  # nothing was appended


# --- B. a final-chance refusal ends the episode at spend_limit -------------------------------------------------------


def test_review3_b_a_refused_final_chance_ends_the_episode_at_spend_limit_and_exits_3(monkeypatch, capsys, tmp_path):
    b = budget(tmp_path, ceiling=0.0105)  # room for the first request (0.01), not the final chance after it
    client = unmetered_test_client(ScriptedClient(costly(tool_use("list_results", {"patient_id": 1})), costly(text("Hb."), stop_reason="end_turn")))
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000, spend=b, limits=Limits(max_requests=1), out_dir=tmp_path / "ev")
    assert r["terminal_reason"] == "spend_limit" and len(client.requests) == 1
    (d,) = (tmp_path / "ev").iterdir()
    assert json.loads((d / "manifest.json").read_text())["complete"] is False
    p = prices(tmp_path).path
    spend.Ledger.init(tmp_path / "l2.jsonl", 0.0105)
    monkeypatch.setattr("anthropic.Anthropic", lambda: ScriptedClient(costly(tool_use("list_results", {"patient_id": 1})), costly(text("Hb."), stop_reason="end_turn")))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "find-result-001", "--model", MODEL, "--max-turns", "1", "--max-output-tokens", "1000", "--ledger", str(tmp_path / "l2.jsonl"),
                "--prices", str(p))  # fmt: skip
    assert e.value.code == 3 and json.loads(capsys.readouterr().out)["terminal_reason"] == "spend_limit"


# --- C. uncertain usage does not poison what follows ---------------------------------------------------------------


def test_review3_c_after_uncertain_usage_later_confirmed_counts_are_accepted(tmp_path):
    b = budget(tmp_path)
    m = b.meter({"model": MODEL, "provider": "openai", "interface": "tool"}, "t", 0)
    first = {"input": "x" * 8000, "max_output_tokens": 100}
    m.settle(m.reserve(first, False)[0], {"input_tokens": 3000, "cached_input_tokens": 0, "output_tokens": 50})
    unknown = {"input": "y", "previous_response_id": "r1", "max_output_tokens": 100}
    assert m.settle(m.reserve(unknown, False)[0], None)["outcome"] == "kept"  # no usage: kept, and its upper bound is not a floor
    later = {"input": "z", "previous_response_id": "r2", "max_output_tokens": 100}
    assert m.settle(m.reserve(later, False)[0], {"input_tokens": 3100, "cached_input_tokens": 3000, "output_tokens": 20})["outcome"] == "confirmed"
    below = {"input": "w", "previous_response_id": "r3", "max_output_tokens": 100}
    assert m.settle(m.reserve(below, False)[0], {"input_tokens": 2000, "cached_input_tokens": 0, "output_tokens": 5})["outcome"] == "kept"  # below a confirmed one


def test_review3_c_a_compressible_request_with_few_tokens_is_not_rejected(tmp_path):
    b = budget(tmp_path)
    m = b.meter(WHO, "t", 0)
    params = {"max_tokens": 100, "messages": [{"role": "user", "content": " " * 100_000}]}  # 100 KB of spaces: a few hundred tokens at most
    out = m.settle(m.reserve(params, False)[0], {"input_tokens": 800, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
    assert out["outcome"] == "confirmed"
    zero = m.settle(m.reserve(params, False)[0], {"input_tokens": 0, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
    assert zero["outcome"] == "kept" and "no input" in zero["reason"]


# --- D. an override covers only the replacement it approved ---------------------------------------------------------


def test_review3_d_an_override_approves_one_replacement_and_later_mismatches_are_refused_again(monkeypatch, capsys, tmp_path):
    path, money = plan_with_ledger(monkeypatch, capsys, tmp_path)
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen))
    out = tmp_path / "ev"
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    capsys.readouterr()
    (tmp_path / "ledger.jsonl").unlink()
    spend.Ledger.init(tmp_path / "ledger.jsonl", 1.0, replace_reason="the first ledger was lost")
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money, "--new-ledger-for-existing-run", "the first ledger was lost")
    capsys.readouterr()
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)  # the approved replacement stays approved
    capsys.readouterr()
    (tmp_path / "ledger.jsonl").unlink()  # and a second replacement, unapproved
    spend.Ledger.init(tmp_path / "ledger.jsonl", 1.0, replace_reason="lost again")
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert e.value.code == 2 and "not the ledger" in capsys.readouterr().err


# --- E. harnesses refuse a real client without a meter --------------------------------------------------------------


def test_review3_e_harnesses_refuse_a_client_without_a_meter_unless_unmetered_is_explicit(tmp_path):
    from eprbench.agents import UnmeteredRefused

    real_looking = NS(messages=NS(create=lambda **kw: pytest.fail("a request was sent unmetered")))
    with pytest.raises(UnmeteredRefused):
        claude_tools.run_episode(EPREnv("find-result-001"), real_looking, MODEL)
    with pytest.raises(UnmeteredRefused):
        openai_tools.run_episode(EPREnv("find-result-001"), NS(responses=NS(create=lambda **kw: pytest.fail("sent"))), "gpt-x-20260101")
    client = ScriptedClient(reply(text("Hb 109 g/L."), stop_reason="end_turn"))  # the test helper marks scripted clients
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, out_dir=tmp_path)
    assert r["metering"] == "unmetered: scripted test client"
    plain = NS(messages=NS(create=lambda **kw: reply(text("Hb 109 g/L."), stop_reason="end_turn")))
    r = claude_tools.run_episode(EPREnv("find-result-001"), plain, MODEL, unmetered=True)
    assert r["metering"] == "unmetered: accepted by the caller"


# --- F. direct runs and undated model ids ---------------------------------------------------------------------------


def test_review3_f_a_direct_run_with_an_undated_model_id_needs_allow_alias_and_then_checks_the_version(monkeypatch, capsys, tmp_path):
    p = prices(tmp_path, {**PRICES, "models": {"claude-haiku-5-5": PRICES["models"][MODEL]}}, name="alias.json").path
    spend.Ledger.init(tmp_path / "l.jsonl", 1.0)
    money = ["--ledger", str(tmp_path / "l.jsonl"), "--prices", str(p)]
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "find-result-001", "--model", "claude-haiku-5-5", *money)
    assert e.value.code == 2 and "--allow-alias" in capsys.readouterr().err
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen, ["claude-haiku-5-5-20260101", "claude-haiku-5-5-20260301"]))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "find-result-001", "--model", "claude-haiku-5-5", "--repeats", "3", "--allow-alias", *money)
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert e.value.code == 3 and out[0]["model_version"] == "claude-haiku-5-5-20260101" and out[0]["model_alias_allowed"] is True
    assert out[1]["model_drift"]["reported"] == "claude-haiku-5-5-20260301" and len(seen) == 2


# --- G. only a structurally complete PNG is an image ----------------------------------------------------------------


def test_review3_g_a_truncated_or_corrupt_png_is_counted_by_its_bytes():
    import base64

    good = base64.b64decode(png(64, 64))
    assert spend.real_image(base64.b64encode(good).decode())
    for bad in (good[:24], good[:-12], good[:23] + bytes([good[23] ^ 1]) + good[24:], good.replace(b"IEND", b"IENX")):
        assert not spend.real_image(base64.b64encode(bad).decode())


# --- H. the feasibility estimate ------------------------------------------------------------------------------------


def test_review3_h_the_estimate_says_what_the_ceiling_admits_in_the_worst_and_the_expected_case(monkeypatch, capsys, tmp_path):
    path, money = plan_with_ledger(monkeypatch, capsys, tmp_path)
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen))
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(tmp_path / "ev"), *money)
    capsys.readouterr()
    run_cli(monkeypatch, "ledger", "estimate", "--prices", money[3], "--plan", str(path), "--ledger", money[1], "--from-evidence", str(tmp_path / "ev"))
    est = json.loads(capsys.readouterr().out)
    (row,) = est["models"]
    upper = row["upper_bound_if_every_reservation_kept"]
    assert row["first_request_reservation_gbp"] > 0.01 and upper["episode_gbp"] >= row["first_request_reservation_gbp"]
    assert upper["requests_of_one_episode_within_ceiling"] >= 1 and row["assumptions"]["requests_per_episode"] == 203  # 200 requests and the final chance
    exp = row["expected_from_evidence"]
    assert exp["episodes_measured"] == 2 and exp["episodes_without_usable_usage"] == 0 and exp["mean_episode_cost_gbp"] > 0
    assert exp["episodes_within_ceiling_at_that_cost"] >= 1 and est["episodes_planned"] == 2 and "provider-side" in est["note"]
