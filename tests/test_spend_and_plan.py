"""Pricing usage, the reserve for final chances, cost in the report, and the counterbalanced run plan (eprbench.runner):
pinned settings and dependencies, completion manifests, and the model version the provider reports. Scripted models
and fake prices only: no request here costs anything."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from eprbench import report, runner, spend
from eprbench.agents import claude_tools
from eprbench.env import EPREnv
from eprbench.episode import Limits, manifest_problems
from tests.test_agents import ScriptedClient, ScriptedOpenAI, oa_response, reply, text, tool_use
from tests.test_spend import MODEL, OUT, PRICES, budget, costly, prices


def test_the_example_prices_are_placeholders_and_a_model_without_prices_is_refused():
    example = spend.Prices(Path(__file__).parent.parent / "docs" / "prices.example.json")
    assert "FAKE" in json.loads(example.path.read_text())["_comment"] and list(example.models) == ["REPLACE-WITH-EXACT-MODEL-ID"]
    with pytest.raises(spend.SpendError, match="no prices for model 'claude-haiku-5-5'"):
        example.model("claude-haiku-5-5")
    example.model("REPLACE-WITH-EXACT-MODEL-ID")  # the example itself is complete


def test_usage_is_priced_by_token_class():
    claude = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000, "output_tokens": 200}
    assert spend.token_classes(claude) == {"input": 10, "cache_write": 1000, "cache_read": 5000, "output": 200}
    openai = {"input_tokens": 6000, "cached_input_tokens": 5000, "output_tokens": 200}  # OpenAI counts cached tokens within input
    assert spend.token_classes(openai) == {"input": 1000, "cache_write": 0, "cache_read": 5000, "output": 200}
    from eprbench.agents.openai_tools import openai_usage

    usage = NS(input_tokens=6000, output_tokens=200, input_tokens_details=NS(cached_tokens=5000))
    assert openai_usage(NS(usage=usage)) == openai


def test_the_reserve_is_left_for_the_final_chance_and_a_per_run_ceiling_holds(tmp_path):
    b = budget(tmp_path, ceiling=0.02, reserve_gbp=0.005)
    client = ScriptedClient(costly(tool_use("list_results", {"patient_id": 1})), costly(text("Hb 109 g/L."), stop_reason="end_turn"))
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000, spend=b, out_dir=tmp_path / "ev")
    # first request: bound just over 0.01, within 0.015 (the ceiling less the reserve); spent 0.006. The second could
    # reach 0.016: not sent. The final chance may use the reserve: within 0.02, so it is sent and answers.
    assert r["terminal_reason"] == "spend_limit" and r["truncated"] and r["final_chance"]["answer"] == "Hb 109 g/L." and len(client.requests) == 2
    assert r["cost"]["gbp"] == pytest.approx(0.012, abs=1e-5) and r["cost"]["refused"]["crossed"] == ["ledger_ceiling_less_reserve"]
    events = [json.loads(x) for x in next((tmp_path / "ev").iterdir()).joinpath("transcript.jsonl").read_text().splitlines()]
    assert [e["cost"]["outcome"] for e in events if e["kind"] == "response"] == ["confirmed"] * 2
    assert [e["kind"] for e in events].count("spend_refused") == 1
    each = budget(tmp_path, ceiling=1.0, ledger="each.jsonl", per_run_gbp=0.009)
    client = ScriptedClient(costly(text("Hb 109 g/L."), stop_reason="end_turn"))
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, MODEL, max_tokens=1000, spend=each)
    assert r["terminal_reason"] == "spend_limit" and client.requests == [] and r["cost"]["gbp"] == 0 and r["final_chance"]["answer"] is None


def test_cost_is_reported_by_interface():
    rows = [{"task_id": "t", "model": "m", "interface": i, "cost": {"gbp": g, "refused": None}} for i, g in (("tool", 0.01), ("tool", 0.03), ("ui", 0.2))]
    table = report.cost_table(rows + [{"task_id": "t", "agent": "codex", "interface": "api"}])
    assert "| m (tool) | 2 | 0.0400 | 0.0200 | 0.0300 | 0 |" in table and "| m (ui) | 1 | 0.2000 |" in table and "codex" not in table
    assert "## Cost" in report.render(rows)


# --- the plan --------------------------------------------------------------------------------------------------------

MODELS = [("anthropic", "claude-haiku-5-5"), ("openai", "gpt-5.4-mini")]


def plan(seed=7, repeats=4, tasks=("find-result-001", "order-ctpa-001")):
    return runner.make_plan(list(tasks), ["tool", "ui"], MODELS, repeats, seed, effort="high", limits=Limits(max_actions=40), max_output_tokens=4000,
                            allow_alias=True)  # fmt: skip


def test_the_plan_is_seeded_counterbalanced_and_pins_every_setting():
    a, b = plan(), plan()
    assert a == b and plan(seed=8)["episodes"] != a["episodes"]
    eps = a["episodes"]
    assert len(eps) == 2 * 2 * 4 * 2 and [e["n"] for e in eps] == list(range(1, len(eps) + 1))
    for pair in a["pairs"]:  # each pair's two episodes run one after the other, one per interface
        mine = [e for e in eps if e["pair"] == pair["pair"]]
        assert [e["interface"] for e in mine] == pair["order"] and mine[1]["n"] == mine[0]["n"] + 1
        assert {(e["task_id"], e["provider"], e["model"], e["repeat"]) for e in mine} == {(pair["task_id"], pair["provider"], pair["model"], pair["repeat"])}
    for task_id in ("find-result-001", "order-ctpa-001"):
        for provider, model in MODELS:
            firsts = [p["order"][0] for p in sorted(a["pairs"], key=lambda p: p["repeat"]) if (p["task_id"], p["model"]) == (task_id, model)]
            assert firsts in (["tool", "ui"] * 2, ["ui", "tool"] * 2)  # alternated within each task and model
    assert all(e["limits"]["max_actions"] == 40 and e["max_output_tokens"] == 4000 and e["effort"] == "high" for e in eps)
    assert {e["reasoning_parameter"] for e in eps} == {"output_config.effort", "reasoning.effort"}
    assert set(a["pins"]) == {"tasks", "bundles", "grader_sha256", "prices_sha256"} and set(a["pins"]["tasks"]) == {"find-result-001", "order-ctpa-001"}
    assert "catalogues/dmd.json" in json.dumps(a["pins"]["bundles"]) and "tasks.py" in a["pins"]["grader_sha256"]
    gemini = runner.make_plan(["find-result-001"], ["tool"], [("gemini", "g-20260101")], 1, 1, effort="high", limits=Limits(), max_output_tokens=1)
    assert gemini["episodes"][0]["effort"] is None  # its harness sends no effort, and the plan says so
    with pytest.raises(ValueError, match="gemini does not support the ui"):
        runner.make_plan(["find-result-001"], ["tool", "ui"], [("gemini", "g-20260101")], 1, 1, effort="medium", limits=Limits(), max_output_tokens=1)


def test_review_6_the_openai_harness_sends_the_effort_it_is_given():
    client = ScriptedOpenAI(oa_response("r1", text="Hb 109 g/L"))
    r = runner.run_one("find-result-001", "openai", "gpt-5.4-mini", "tool", client=client, effort="high", repeat=0, limits=Limits(), unmetered=True,
                       max_output_tokens=500)  # fmt: skip
    assert client.requests[0]["reasoning"] == {"effort": "high"} and client.requests[0]["max_output_tokens"] == 500 and r["effort"] == "high"


def run_cli(monkeypatch, *argv):
    from eprbench.__main__ import main

    monkeypatch.setattr(sys, "argv", ["eprbench", *argv])
    main()


class Answering:
    """Every request answered at once, by the model version it is set to report."""

    def __init__(self, seen, version="fake-model-2026-01-01"):
        self.seen, self.version = seen, version
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.seen.append(kw)
        return NS(content=[text("Hb 109 g/L.")], stop_reason="end_turn", usage=NS(input_tokens=10, output_tokens=5), model=self.version)


def made_plan(monkeypatch, capsys, tmp_path, repeats=2):
    path = tmp_path / "plan.json"
    spend.Ledger.init(tmp_path / "ledger.jsonl", 1.0)
    run_cli(monkeypatch, "plan", "find-result-001", "--models", f"anthropic:{MODEL}", "--repeats", str(repeats), "--seed", "3", "--interfaces", "tool",
            "--out", str(path), "--max-actions", "30", "--max-output-tokens", "1000", "--prices", str(prices(tmp_path).path), "--ledger", str(tmp_path / "ledger.jsonl"))  # fmt: skip
    assert f"{repeats} episodes in {repeats} pairs" in capsys.readouterr().out
    return path, ["--ledger", str(tmp_path / "ledger.jsonl"), "--prices", str(tmp_path / "prices.json")]


def test_review_7_a_plan_resumes_only_past_episodes_with_a_valid_completion_manifest(monkeypatch, capsys, tmp_path):
    path, money = made_plan(monkeypatch, capsys, tmp_path, repeats=3)
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen))
    out = tmp_path / "ev"
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    first = capsys.readouterr()
    assert len(first.out.splitlines()) == 3 and json.loads(first.err.splitlines()[-1])["plan"]["ran"] == 3
    assert all(kw["model"] == MODEL and kw["max_tokens"] == 1000 and kw["output_config"] == {"effort": "medium"} for kw in seen)
    sha = runner.load_plan(path)[1]
    dirs = sorted(out.iterdir())
    for d in dirs:
        assert manifest_problems(d) == []
        m = json.loads((d / "manifest.json").read_text())
        assert m["plan"]["sha256"] == sha and m["ledger"]["first_seq"] < m["ledger"]["last_seq"] and "final.sqlite" in m["files"]
        assert json.loads((d / "result.json").read_text())["reported_models"] == ["fake-model-2026-01-01"]
    (dirs[0] / "report.json").write_text("")  # an empty report, as after a cut-off write
    (dirs[1] / "manifest.json").unlink()  # never finished
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    again = json.loads(capsys.readouterr().err.splitlines()[-1])["plan"]
    assert again == {"plan_sha256": sha, "episodes": 3, "already_complete": 1, "ran": 2, "completed": 2, "errored": {}, "left": 0, "stopped": None}
    assert manifest_problems(dirs[0]) == ["report.json does not match its hash"]
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["plan"]["ran"] == 0 and len(seen) == 5


def test_review_6_a_plan_refuses_changed_dependencies_or_prices_and_stops_on_a_new_model_version(monkeypatch, capsys, tmp_path):
    path, money = made_plan(monkeypatch, capsys, tmp_path)
    seen = []
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen, "fake-model-v1"))
    out = tmp_path / "ev"
    other = prices(tmp_path, {**PRICES, "models": {MODEL: {**PRICES["models"][MODEL], "max_image_tokens": 2001}}}, name="other.json")
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), "--ledger", money[1], "--prices", str(other.path))
    assert e.value.code == 2 and "prices file is not the one" in capsys.readouterr().err
    changed = json.loads(path.read_text())
    changed["pins"]["tasks"]["find-result-001"]["file_sha256"] = "0" * 64  # as if the task file had been edited since
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(changed))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(edited), "--out-dir", str(out), *money)
    assert e.value.code == 2 and "task find-result-001 changed" in capsys.readouterr().err and seen == []
    # the first episode runs on v1; the provider then answers the same model id with v2: the plan stops
    versions = iter(["fake-model-v1", "fake-model-v2"])
    monkeypatch.setattr("anthropic.Anthropic", lambda: NS(messages=NS(create=lambda **kw: Answering(seen, next(versions)).create(**kw))))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    err = capsys.readouterr().err
    assert e.value.code == 3 and "model version changed" in err and "fake-model-v2" in err
    monkeypatch.setattr("anthropic.Anthropic", lambda: Answering(seen, "fake-model-v2"))
    with pytest.raises(SystemExit):  # and on resuming, the drifted episode runs again and stops at its first response again
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(out), *money)
    assert "model version changed" in capsys.readouterr().err and len(seen) == 3
