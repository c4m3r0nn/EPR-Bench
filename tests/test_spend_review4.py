"""The final spend-safety check (docs/codex/readiness-fixes-2026-11-18/codex-review-4.md), each finding reproduced: the
estimate's upper bound grows with the conversation and evidence without usable usage is never priced as zero; and two
copies of one ledger on one machine cannot both advance, even interleaved. Fake prices only."""

import json
import multiprocessing
import shutil
from pathlib import Path

import pytest

from eprbench import runner, spend
from eprbench.episode import Limits
from tests.test_spend import MODEL, PRICES, budget, prices


def growing_plan(max_requests=2):
    return runner.make_plan(["find-result-001"], ["tool"], [("anthropic", MODEL)], 1, 1, effort="medium", limits=Limits(max_requests=max_requests, final_chance=False),
                            max_output_tokens=16000)  # fmt: skip


def test_review4_1_the_upper_bound_grows_with_the_conversation_and_states_its_assumptions(tmp_path):
    dear = prices(tmp_path, {**PRICES, "models": {MODEL: {**PRICES["models"][MODEL], "per_million_tokens": {"input": 3, "cache_write": 3.75, "cache_read": 0.3, "output": 15}}}})
    (row,) = runner.estimate(growing_plan(2), dear, 10.0)["models"]
    first, growth = row["first_request_reservation_gbp"], row["assumptions"]["growth_bytes_per_request"]
    second = first + growth * 3.75 / 1e6  # the second request carries the first and its answer, at the dearest input rate
    assert row["upper_bound_if_every_reservation_kept"]["episode_gbp"] == pytest.approx(first + second, abs=2e-6)  # each figure is rounded to 6 places
    assert row["upper_bound_if_every_reservation_kept"]["episode_gbp"] > 2 * first
    assert row["assumptions"]["requests_per_episode"] == 2 and row["assumptions"]["per_request_size_limit_bytes"] == spend.MAX_REQUEST_BYTES
    assert not [k for k in json.dumps(row) .split('"') if k in ("worst", "episodes_admitted_worst", "requests_admitted_worst")]
    # with many requests each is capped at the largest request that may be sent
    (big,) = runner.estimate(growing_plan(5000), dear, 10.0)["models"]
    assert big["upper_bound_if_every_reservation_kept"]["episode_gbp"] <= 5000 * big["largest_request_reservation_gbp"] + 1e-9
    assert big["upper_bound_if_every_reservation_kept"]["episodes_within_ceiling"] == 0


def test_review4_1_evidence_without_usable_usage_is_counted_apart_never_priced_as_zero(tmp_path):
    ev = tmp_path / "ev"
    for name, usage in (("a", {"input_tokens": 10_000, "output_tokens": 500, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}),
                        ("b", {"input_tokens": None, "output_tokens": 500, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})):  # fmt: skip
        d = ev / name
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"provider": "anthropic", "model": MODEL, "interface": "tool"}))
        (d / "transcript.jsonl").write_text(json.dumps({"kind": "response", "usage": usage}) + "\n")
    (row,) = runner.estimate(growing_plan(2), prices(tmp_path), 10.0, ev)["models"]
    exp = row["expected_from_evidence"]
    assert exp["episodes_measured"] == 1 and exp["episodes_without_usable_usage"] == 1
    assert exp["mean_episode_cost_gbp"] == pytest.approx(10_000 * PRICES["models"][MODEL]["per_million_tokens"]["input"] / 1e6 + 500 * 10 / 1e6)


def _reserve_slowly(args):
    """In a process of its own: reserve on one copy of the ledger, slowly enough that the other copy's process is
    between reading its ledger and checking the anchor."""
    path, prices_path, state = args
    import os
    import time

    os.environ["EPRBENCH_STATE_DIR"] = state
    real = spend.replay

    def slow(lines):
        out = real(lines)
        time.sleep(0.5)
        return out

    spend.replay = slow
    b = spend.Budget(spend.Prices(prices_path), spend.Ledger(path))
    try:
        rid, why = b.meter({"model": MODEL, "provider": "anthropic", "interface": "tool"}, "t", 0).reserve({"max_tokens": 1000, "messages": "x"}, False)
    except spend.LedgerError as e:
        return f"refused: {e}"
    return "reserved" if rid else f"refused: {why}"


def test_review4_2_two_copies_of_a_ledger_on_one_machine_cannot_both_advance_even_interleaved(tmp_path):
    budget(tmp_path, ceiling=0.015)  # room for one reservation of 0.01, not two
    one, two = tmp_path / "ledger.jsonl", tmp_path / "copy" / "ledger.jsonl"
    two.parent.mkdir()
    shutil.copy(one, two)
    state = spend.Ledger(one).read()
    spend._write_json(spend.Ledger._path_record(two), {"path": str(two.resolve()), "ledger_id": state.ledger_id})  # both copies anchored, as the review had them
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        outcomes = pool.map(_reserve_slowly, [(one, tmp_path / "prices.json", str(spend.state_dir())), (two, tmp_path / "prices.json", str(spend.state_dir()))])
    assert sorted(o.split(":")[0] for o in outcomes) == ["refused", "reserved"], outcomes
    assert sum(spend.replay(Path(p).read_text().splitlines()).requests() for p in (one, two)) == 1
