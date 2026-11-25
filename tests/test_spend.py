"""Money safety (eprbench.spend, docs/SPEND_SAFETY.md), each finding of the readiness-fixes review reproduced first:
reservations before every attempt, kept unless a response is priced; one locked, validated, hash-chained ledger; a
bound from bytes, strict prices and backstops; and no paid SDK run without a budget. Scripted models and fake prices
only: nothing here costs anything."""

import json
import math
import multiprocessing
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from eprbench import runner, spend
from eprbench.agents import claude_tools
from eprbench.env import EPREnv
from tests.test_agents import ScriptedClient, reply, text, tool_use

MODEL = "fake-model-20260101"  # dated, as a pinned model id is
TINY = 0.0001  # per million tokens: input so cheap that the bounded output decides the bound
PRICES = {"currency": "GBP", "gbp_per_unit": 1,
          "models": {MODEL: {"per_million_tokens": {"input": TINY, "cache_write": TINY, "cache_read": TINY, "output": 10},
                             "max_image_tokens": 2000, "request_overhead_tokens": 0}}}  # fmt: skip
OUT = 10 / 1e6  # pounds per output token


def prices(tmp_path, data=None, name="prices.json") -> spend.Prices:
    path = tmp_path / name
    path.write_text(json.dumps(data or PRICES))
    return spend.Prices(path)


def budget(tmp_path, ceiling=1.0, ledger="ledger.jsonl", **kw) -> spend.Budget:
    path = tmp_path / ledger
    led = spend.Ledger(path) if path.exists() else spend.Ledger.init(path, ceiling)
    return spend.Budget(prices(tmp_path), led, **kw)


def costly(*blocks, stop_reason="tool_use", output=600):
    return NS(content=list(blocks), stop_reason=stop_reason, usage=NS(input_tokens=10_000, output_tokens=output, cache_creation_input_tokens=0, cache_read_input_tokens=0))


def entries(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


# --- 1. every attempt is reserved first, and kept unless it is priced --------------------------------------------------


class ServerError(Exception):
    status_code = 500  # transient: retried, and the provider may still have billed it


def test_review_1_every_attempt_is_reserved_before_it_is_sent_and_a_failed_one_stays_spent(tmp_path, monkeypatch):
    b = budget(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    synced, attempts = [], []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))

    def create(**kw):
        reserved = [e for e in entries(ledger) if e["kind"] == "reserve"]
        attempts.append((len(reserved), len(synced)))  # the reservation for this attempt is already on disk, fsynced
        if len(attempts) < 3:
            raise ServerError("server error")
        return costly(text("Hb 109 g/L."), stop_reason="end_turn")

    client = NS(messages=NS(create=create), with_options=lambda **k: client)
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000, spend=b)
    assert [n for n, _ in attempts] == [1, 2, 3] and all(s > 0 for _, s in attempts)
    kinds = [(e["kind"], e.get("outcome")) for e in entries(ledger)]
    assert kinds == [("init", None), ("reserve", None), ("reconcile", "kept"), ("reserve", None), ("reconcile", "kept"), ("reserve", None),
                     ("reconcile", "confirmed"), ("episode", None)]  # fmt: skip
    reserved = [e["gbp"] for e in entries(ledger) if e["kind"] == "reserve"]
    state = spend.Ledger(ledger).read()
    # two whole reservations kept (each at least the 1,000 output tokens it allowed) and the third's actual cost
    assert state.spent() == pytest.approx(reserved[0] + reserved[1] + 600 * OUT + 10_000 * TINY / 1e6)
    assert reserved[0] >= 1000 * OUT and r["cost"]["gbp"] == pytest.approx(state.spent(), abs=1e-6) and r["cost"]["kept"] == 2 and r["cost"]["unreconciled"] == 0


def test_review_1_a_crash_or_an_unreadable_response_leaves_the_reservation_spent(tmp_path):
    b = budget(tmp_path)

    def interrupted(**kw):
        raise KeyboardInterrupt  # the process stops with the request sent and no response

    client = NS(messages=NS(create=interrupted), with_options=lambda **k: client)
    with pytest.raises(KeyboardInterrupt):
        claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000, spend=b)
    later = spend.Ledger(tmp_path / "ledger.jsonl").read()  # what the next process sees
    (open_rid,) = later.outstanding()
    assert later.spent() == pytest.approx(later.reservations[open_rid]["gbp"]) and later.spent() >= 1000 * OUT

    unreadable = ScriptedClient(NS(content=[text("x")], stop_reason="end_turn", usage=NS(input_tokens="lots", output_tokens=None)))
    b2 = budget(tmp_path, ledger="second.jsonl")
    r = claude_tools.run_episode(EPREnv("find-result-001"), unreadable, MODEL, max_tokens=1000, spend=b2)
    assert r["terminal_reason"] == "stopped_without_submit" and r["cost"]["kept"] == 1 and r["cost"]["gbp"] >= 1000 * OUT  # answered, but unpriced


# --- 2. one ledger, locked, validated, created on purpose -------------------------------------------------------------


def test_review_2_a_ledger_is_created_once_on_purpose_and_its_ceiling_raised_only_by_an_entry(tmp_path):
    path = tmp_path / "l.jsonl"
    with pytest.raises(spend.LedgerError, match="eprbench ledger init"):
        spend.Ledger(path)  # a missing ledger is never zero spent
    spend.Ledger.init(path, 10)
    with pytest.raises(spend.LedgerError, match="already"):
        spend.Ledger.init(path, 1000)
    led = spend.Ledger(path)
    for bad in (5, 10, math.nan):
        with pytest.raises(spend.SpendError):
            led.raise_ceiling(bad, "more runs")
    with pytest.raises(spend.SpendError, match="reason"):
        led.raise_ceiling(12, " ")
    led.raise_ceiling(12, "owner approved two more pairs")
    assert led.read().ceiling == 12 and [e["kind"] for e in entries(path)] == ["init", "raise"]


def corrupt(path: Path, n: int, change) -> None:
    lines = path.read_text().splitlines()
    e = json.loads(lines[n])
    change(e)
    lines[n] = json.dumps(e)
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("name, change, message", [
    ("nan", lambda e: e.update(gbp=float("nan")), "not valid JSON"),  # a NaN cannot be written by the ledger, nor read
    ("negative", lambda e: e.update(gbp=-5.0, hash=spend._hash({**e, "gbp": -5.0})), "not negative"),
    ("edited", lambda e: e.update(gbp=0.0), "hash does not match"),
    ("rehashed", lambda e: (e.update(gbp=0.0), e.update(hash=spend._hash(e))), "hash chain broken"),
    ("seq", lambda e: (e.update(seq=7), e.update(hash=spend._hash(e))), "sequence number"),
    ("kind", lambda e: (e.update(kind="credit"), e.update(hash=spend._hash(e))), "unknown kind"),
])  # fmt: skip
def test_review_2_a_ledger_that_does_not_validate_stops_everything(tmp_path, name, change, message):
    b = budget(tmp_path)
    m = b.meter({"model": MODEL, "provider": "anthropic", "interface": "tool"}, "t", 0)
    rid, _ = m.reserve({"max_tokens": 1000, "messages": "x"}, False)
    m.settle(rid, {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})  # a line after the one altered, which chains to it
    path = tmp_path / "ledger.jsonl"
    if name == "nan":
        path.write_text(path.read_text().replace('"gbp":' + json.dumps(entries(path)[1]["gbp"]), '"gbp":NaN'))
    else:
        corrupt(path, 1, change)
    with pytest.raises(spend.LedgerError, match=message):
        spend.Ledger(path)
    with pytest.raises(spend.LedgerError):
        b.meter({"model": MODEL, "provider": "anthropic", "interface": "tool"}, "t", 1).reserve({"max_tokens": 10, "messages": "x"}, False)
    path.write_text(path.read_text()[:-5])  # a write cut off part-way
    with pytest.raises(spend.LedgerError):
        spend.Ledger(path)


def _reserve(args):
    path, prices_path, rid_out = args
    b = spend.Budget(spend.Prices(prices_path), spend.Ledger(path))
    rid, why = b.meter({"model": MODEL, "provider": "anthropic", "interface": "tool"}, "t", 0).reserve({"max_tokens": 1000, "messages": "x"}, False)
    return rid is not None


def test_review_2_processes_sharing_a_ledger_cannot_both_spend_the_same_balance(tmp_path):
    b = budget(tmp_path, ceiling=0.015)  # room for one reservation of 1,000 output tokens (0.01), not two
    path = tmp_path / "ledger.jsonl"
    other = spend.Budget(b.prices, spend.Ledger(path))  # a second process's budget, opened before either reserves
    who = {"model": MODEL, "provider": "anthropic", "interface": "tool"}
    assert b.meter(who, "t", 0).reserve({"max_tokens": 1000, "messages": "x"}, False)[0]
    rid, why = other.meter(who, "t", 1).reserve({"max_tokens": 1000, "messages": "x"}, False)
    assert rid is None and "ledger_ceiling_less_reserve" in why["crossed"]  # re-read under the lock: the first is counted
    budget(tmp_path, ceiling=0.015, ledger="race.jsonl")
    with multiprocessing.get_context("spawn").Pool(6) as pool:
        won = pool.map(_reserve, [(tmp_path / "race.jsonl", b.prices.path, None)] * 6)
    assert sum(won) == 1 and spend.Ledger(tmp_path / "race.jsonl").read().requests() == 1


# --- 3. a bound that holds, prices that make sense, backstops ---------------------------------------------------------


def model_entry(**kw):
    return {"per_million_tokens": {"input": 1, "cache_write": 1, "cache_read": 1, "output": 1}, "max_image_tokens": 1, "request_overhead_tokens": 0} | kw


@pytest.mark.parametrize("data, message", [
    ({"currency": "GBP", "gbp_per_unit": 0.01, "models": {MODEL: model_entry()}}, "exactly 1"),
    ({"currency": "GBP", "models": {MODEL: model_entry()}}, "exactly 1"),
    ({"currency": "GBP", "gbp_per_unit": 1, "fx_safety_margin": 0.1, "models": {MODEL: model_entry()}}, "no exchange margin"),
    ({"currency": "USD", "gbp_per_unit": 0.8, "models": {MODEL: model_entry()}}, "fx_safety_margin"),
    ({"currency": "USD", "gbp_per_unit": 0.008, "fx_safety_margin": 0.1, "models": {MODEL: model_entry()}}, "between 0.1 and 10"),
    ({"currency": "usd", "gbp_per_unit": 0.8, "fx_safety_margin": 0.1, "models": {MODEL: model_entry()}}, "three-letter"),
    ({"currency": "GBP", "gbp_per_unit": True, "models": {MODEL: model_entry()}}, "exactly 1"),
])  # fmt: skip
def test_review_3_nonsense_currencies_and_conversions_are_refused(tmp_path, data, message):
    with pytest.raises(spend.SpendError, match=message):
        prices(tmp_path, data).model(MODEL)


@pytest.mark.parametrize("entry, message", [
    (model_entry(per_million_tokens={"input": 0, "cache_write": 1, "cache_read": 1, "output": 1}), "above zero"),
    (model_entry(per_million_tokens={"input": 1, "cache_write": 1, "cache_read": 1}), "above zero"),
    (model_entry(per_million_tokens={"input": 1, "cache_write": 1, "cache_read": 1, "output": 1, "audio": 1}), "unknown: audio"),
    ({k: v for k, v in model_entry().items() if k != "max_image_tokens"}, "max_image_tokens"),
    (model_entry(max_image_tokens=0), "max_image_tokens"),
    ({k: v for k, v in model_entry().items() if k != "request_overhead_tokens"}, "request_overhead_tokens"),
    (model_entry(request_overhead_tokens=-1), "request_overhead_tokens"),
])  # fmt: skip
def test_review_3_every_price_and_allowance_must_be_given_and_positive(tmp_path, entry, message):
    with pytest.raises(spend.SpendError, match=message):
        prices(tmp_path, {"currency": "GBP", "gbp_per_unit": 1, "models": {MODEL: entry}}).model(MODEL)


def test_review_3_the_bound_counts_bytes_images_overhead_and_the_dearest_input_rate(tmp_path):
    data = {"currency": "EUR", "gbp_per_unit": 0.9, "fx_safety_margin": 0.2,
            "models": {MODEL: model_entry(per_million_tokens={"input": 1, "cache_write": 2, "cache_read": 3, "output": 5}, max_image_tokens=1500,
                                          request_overhead_tokens=700)}}  # fmt: skip
    p = prices(tmp_path, data)
    b = spend.Budget(p, spend.Ledger.init(tmp_path / "l.jsonl", 10))
    m = b.meter({"model": MODEL, "provider": "openai", "interface": "ui"}, "t", 0)
    words = "Ångström ✓ 💉 " * 100  # multi-byte UTF-8: every character's bytes are counted, never fewer tokens than bytes
    from tests.test_spend_review2 import png

    image = "data:image/png;base64," + png(1280, 800)  # a real screenshot, in an image field
    bound = m.bound({"input": [{"role": "user", "content": [{"type": "input_text", "text": words}, {"type": "input_image", "image_url": image}]}],
                     "max_output_tokens": 2000})  # fmt: skip
    assert bound["text_bytes"] >= len(words.encode()) and bound["images"] == 1 and bound["serialised_bytes"] >= bound["text_bytes"] + len(image) - 100
    assert bound["input_tokens_bound"] == bound["text_bytes"] + 1500 + 700
    rate = 0.9 * 1.2 / 1e6  # the conservative rate, raised by its margin
    assert bound["gbp"] == pytest.approx((bound["input_tokens_bound"] * 3 + 2000 * 5) * rate)  # input at the dearest class: cache read here
    m.settle(m.reserve({"input": "x", "max_output_tokens": 10}, False)[0], {"input_tokens": 40_000, "cached_input_tokens": 0, "output_tokens": 9})
    assert m.bound({"input": "x", "previous_response_id": "r1", "max_output_tokens": 1})["input_tokens_bound"] >= 40_009 + 700
    assert m.bound({"input": "x"})["gbp"] == math.inf and "unbounded_output" in m.reserve({"input": "x"}, False)[1]["crossed"]


def test_review_3_backstops_on_requests_output_tokens_and_request_size_hold_whatever_the_estimate(tmp_path):
    data = {"currency": "GBP", "gbp_per_unit": 1,
            "models": {MODEL: model_entry(per_million_tokens={"input": 1, "cache_write": 1, "cache_read": 1, "output": 10}, request_overhead_tokens=200)}}
    p = prices(tmp_path, data)
    who = {"model": MODEL, "provider": "anthropic", "interface": "tool"}
    # output: a ceiling of 0.05 buys at most 5,000 output tokens; small actual costs do not free the tokens reserved
    m = spend.Budget(p, spend.Ledger.init(tmp_path / "o.jsonl", 0.05)).meter(who, "t", 0)
    for _ in range(5):
        m.settle(m.reserve({"max_tokens": 1000, "messages": "x"}, False)[0], {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
    rid, why = m.reserve({"max_tokens": 1000, "messages": "x"}, False)
    assert rid is None and why["crossed"] == ["backstop_output_tokens"] and spend.Ledger(tmp_path / "o.jsonl").read().spent() < 0.001
    # requests: at least 200 tokens each at 1 per million, so 0.002 allows ten
    m = spend.Budget(p, spend.Ledger.init(tmp_path / "r.jsonl", 0.002)).meter(who, "t", 0)
    assert p.model(MODEL).max_requests(0.002) == 10
    # size: a request larger than the limit is never sent, however cheap
    m = spend.Budget(p, spend.Ledger.init(tmp_path / "s.jsonl", 1.0), max_request_bytes=1000).meter(who, "t", 0)
    rid, why = m.reserve({"max_tokens": 1, "messages": "x" * 2000}, False)
    assert rid is None and why["crossed"] == ["backstop_request_bytes"]


# --- 4. no paid run without a budget ----------------------------------------------------------------------------------


def run_cli(monkeypatch, *argv):
    from eprbench.__main__ import main

    monkeypatch.setattr(sys, "argv", ["eprbench", *argv])
    main()


def never(*a, **k):
    raise AssertionError("a client was constructed")


def test_review_4_no_paid_sdk_run_reaches_a_client_without_a_ledger_and_prices(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("anthropic.Anthropic", never)
    monkeypatch.setattr("openai.OpenAI", never)
    p = prices(tmp_path).path
    spend.Ledger.init(tmp_path / "l.jsonl", 10)
    plan = tmp_path / "plan.json"
    run_cli(monkeypatch, "plan", "find-result-001", "--models", f"anthropic:{MODEL}", "--interfaces", "tool", "--repeats", "1", "--seed", "1",
            "--prices", str(p), "--out", str(plan), "--ledger", str(tmp_path / "l.jsonl"))  # fmt: skip
    capsys.readouterr()
    for argv, message in [
        (["run", "find-result-001"], "needs --ledger"),
        (["run", "find-result-001", "--provider", "openai", "--interface", "ui"], "needs --ledger"),
        (["run", "find-result-001", "--provider", "gemini"], "needs --ledger"),
        (["run", "--plan", str(plan), "--out-dir", str(tmp_path / "ev")], "needs --ledger"),
        (["run", "find-result-001", "--prices", str(p)], "needs --ledger"),
        (["run", "find-result-001", "--ledger", str(tmp_path / "l.jsonl")], "needs --ledger"),
        (["run", "find-result-001", "--ledger", str(tmp_path / "none.jsonl"), "--prices", str(p), "--model", MODEL], "ledger init"),
        (["run", "find-result-001", "--ledger", str(tmp_path / "l.jsonl"), "--prices", str(p)], "--allow-alias"),
        (["run", "find-result-001", "--ledger", str(tmp_path / "l.jsonl"), "--prices", str(p), "--allow-alias"], "no prices for model 'claude-opus-5-5'"),
        (["run", "find-result-001", "--ledger", str(tmp_path / "l.jsonl"), "--prices", str(p), "--provider", "gemini", "--model", MODEL], "Gemini"),
        (["run", "find-result-001", "--agent", "claude", "--interface", "api", "--ledger", str(tmp_path / "l.jsonl")], "not metered"),
        (["run", "find-result-001", "--i-accept-unmetered-spend", "--prices", str(p)], "no spend options"),
    ]:  # fmt: skip
        with pytest.raises(SystemExit) as e:
            run_cli(monkeypatch, *argv)
        assert e.value.code == 2 and message in capsys.readouterr().err, argv
    with pytest.raises(runner.UnmeteredRefused):
        runner.run_one("find-result-001", "anthropic", MODEL, "tool", client=None, effort="medium", repeat=0, limits=None)


def test_review_4_unmetered_spend_must_be_accepted_explicitly_and_is_recorded(monkeypatch, capsys, tmp_path):
    client = ScriptedClient(reply(text("Hb 109 g/L."), stop_reason="end_turn"))
    monkeypatch.setattr("anthropic.Anthropic", lambda: client)
    run_cli(monkeypatch, "run", "find-result-001", "--model", MODEL, "--i-accept-unmetered-spend", "--out-dir", str(tmp_path))
    out = capsys.readouterr()
    assert "WARNING" in out.err and "nothing limits what they cost" in out.err and json.loads(out.out)["unmetered_spend_accepted"] is True
    (d,) = tmp_path.iterdir()
    assert json.loads((d / "episode.json").read_text())["unmetered_spend_accepted"] is True


def test_a_metered_cli_run_reserves_every_request_in_the_ledger(monkeypatch, capsys, tmp_path):
    client = ScriptedClient(*[costly(text("Hb 109 g/L."), stop_reason="end_turn", output=800)] * 3)
    monkeypatch.setattr("anthropic.Anthropic", lambda: client)
    p = prices(tmp_path).path
    spend.Ledger.init(tmp_path / "l.jsonl", 0.025)
    with pytest.raises(SystemExit) as e:  # 0.008 an episode, each reserving 0.01 first: two run, the third could cross 0.025
        run_cli(monkeypatch, "run", "find-result-001", "--repeats", "3", "--model", MODEL, "--max-output-tokens", "1000", "--ledger", str(tmp_path / "l.jsonl"),
                "--prices", str(p))  # fmt: skip
    out = capsys.readouterr()
    assert e.value.code == 3 and "spend budget" in out.err and len(out.out.splitlines()) == 2 and len(client.requests) == 2
    assert client.requests[0]["max_tokens"] == 1000 and spend.Ledger(tmp_path / "l.jsonl").read().spent() == pytest.approx(0.016, rel=1e-3)
    run_cli(monkeypatch, "ledger", "show", str(tmp_path / "l.jsonl"))
    shown = json.loads(capsys.readouterr().out)
    assert shown["ceiling_gbp"] == 0.025 and shown["reservations"] == 2 and shown["unreconciled"] == 0
