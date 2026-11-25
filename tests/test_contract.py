"""Regression tests for the episode contract, one group per defect found in review of the lifecycle and evidence work."""

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from eprbench import episode as episode_module
from eprbench import isolation, ops, report
from eprbench.agents import claude_tools, cli_agent, gemini_tools, openai_tools
from eprbench.env import EPREnv
from eprbench.episode import Episode, Limits, regrade
from eprbench.server.local import serve_task
from eprbench.tasks import CANARY, load_task
from eprbench.world import World
from tests.test_agents import CTPA, ScriptedClient, ScriptedGemini, ScriptedOpenAI, interaction, oa_response, reply, text, tool_use
from tests.test_isolation import agent


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def only_dir(root):
    (d,) = [p for p in root.iterdir() if p.is_dir()]
    return d


def slow(client, attr, *delays):
    """Make a scripted client's create() take the given seconds per call (the last one repeats)."""
    owner = getattr(client, attr)
    create, delays = owner.create, list(delays)

    def wrapped(**kw):
        time.sleep(delays.pop(0) if len(delays) > 1 else delays[0])
        return create(**kw)

    owner.create = wrapped
    return client


# --- 1. the timeout holds during requests and before an answer is accepted -------------------------


def test_a_late_answer_is_not_accepted():
    client = slow(ScriptedClient(reply(tool_use("submit", {"answer": "late"})), reply(text("in time"), stop_reason="end_turn")), "messages", 0.15, 0.0)
    report_ = claude_tools.run_episode(EPREnv("find-result-001"), client, limits=Limits(timeout_seconds=0.05))
    assert report_["terminal_reason"] == "timeout" and report_["answer"] == "in time" and report_["final_chance"]["late"] is False
    assert 0 < client.requests[0]["timeout"] <= 0.05  # every request carries a deadline from the time remaining
    assert Limits.final_chance_seconds - 1 < client.requests[-1]["timeout"] <= Limits.final_chance_seconds


def test_a_late_final_chance_is_not_accepted():
    client = slow(ScriptedClient(*[reply(tool_use("get_current_time", {}, f"t{i}")) for i in range(2)], reply(text("too slow"), stop_reason="end_turn")),
                  "messages", 0.0, 0.0, 0.15)  # fmt: skip
    report_ = claude_tools.run_episode(EPREnv("order-ctpa-001"), client, limits=Limits(max_requests=2, final_chance_seconds=0.05))
    assert report_["terminal_reason"] == "model_request_limit" and report_["answer"] is None and report_["final_chance"]["late"] is True


@pytest.mark.parametrize("harness", ["openai", "gemini"])
def test_other_tool_harnesses_reject_late_answers(harness):
    if harness == "openai":
        client = slow(ScriptedOpenAI(oa_response("r1", text="late"), oa_response("r2", text="in time")), "responses", 0.15, 0.0)
        run = openai_tools.run_episode
    else:
        client = slow(ScriptedGemini(interaction("i1", text="late"), interaction("i2", text="in time")), "interactions", 0.15, 0.0)
        run = gemini_tools.run_episode
    report_ = run(EPREnv("find-result-001"), client, limits=Limits(timeout_seconds=0.05))
    assert report_["terminal_reason"] == "timeout" and report_["answer"] == "in time" and "timeout" in client.requests[0]


# --- 2. browser actions are charged when attempted, and failures are explicit ----------------------


def browser(run):
    try:
        return run()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise


def test_claude_browser_charges_failed_actions_and_counts_screenshots():
    from eprbench.agents import claude_browser

    client = ScriptedClient(
        reply(tool_use("screenshot", {}, "tu_1"), tool_use("teleport", {}, "tu_2"), tool_use("screenshot", {}, "tu_3")),
        reply(text("Hb was 109 g/L."), stop_reason="end_turn"),
    )
    r = browser(lambda: claude_browser.run_episode("find-result-001", client))
    # the failed teleport is an action; the screenshot is a screenshot request, not one; the last is never carried out
    assert r["steps"] == 1 and r["screenshot_actions"] == 1 and "on request" in r["comparability"]["screenshots"]
    assert r["accounting"] == {"actions": 1, "screenshot_requests": 1, "automatic_screenshots": 0, "model_requests": 2}


def test_openai_browser_charges_failed_actions_and_tells_the_model():
    from eprbench.agents import openai_browser
    from tests.test_agents import oa_computer_call

    client = ScriptedOpenAI(oa_response("r1", oa_computer_call("c1", NS(type="teleport"), NS(type="screenshot"))), oa_response("r2", text="Hb 109 g/L"))
    r = browser(lambda: openai_browser.run_episode("find-result-001", client))
    assert r["steps"] == 1 and r["screenshot_actions"] == 0 and "automatic" in r["comparability"]["screenshots"]
    assert r["accounting"] == {"actions": 1, "screenshot_requests": 0, "automatic_screenshots": 1, "model_requests": 2}
    told = [i for i in client.requests[1]["input"] if i.get("role") == "user"]
    assert told and "teleport" in told[0]["content"] and "not carried out" in told[0]["content"]


def test_screenshot_requests_are_not_actions_on_either_browser_harness_and_are_capped_apart():
    """Readiness review: the Claude harness charged an action for each screenshot it asked for, while the OpenAI one is
    sent a screenshot after every call for nothing. The action limit now applies to mouse and keyboard actions alone on
    both; screenshot requests are counted and capped apart, and every episode records the same four counts."""
    from eprbench.agents import claude_browser, openai_browser
    from tests.test_agents import oa_computer_call

    look, move = tool_use("screenshot", {}, "s"), lambda i: tool_use("mouse_move", {"coordinate": [5, 5]}, f"m{i}")
    claude = ScriptedClient(reply(look, look, look, move(1)), reply(text("Hb 109 g/L."), stop_reason="end_turn"))
    r = browser(lambda: claude_browser.run_episode("find-result-001", claude, limits=Limits(max_actions=1)))
    assert r["terminal_reason"] == "submitted" and r["accounting"] == {"actions": 1, "screenshot_requests": 3, "automatic_screenshots": 0, "model_requests": 2}
    shot, step = NS(type="screenshot"), NS(type="move", x=5, y=5)
    openai = ScriptedOpenAI(oa_response("r1", oa_computer_call("c1", shot, shot, step)), oa_response("r2", text="Hb 109 g/L"))
    r = browser(lambda: openai_browser.run_episode("find-result-001", openai, limits=Limits(max_actions=1)))
    assert r["terminal_reason"] == "submitted" and r["accounting"] == {"actions": 1, "screenshot_requests": 2, "automatic_screenshots": 1, "model_requests": 2}
    # the cap on screenshot requests is a limit of its own, on both
    claude = ScriptedClient(reply(look, look, look), reply(text("Hb 109 g/L."), stop_reason="end_turn"))
    r = browser(lambda: claude_browser.run_episode("find-result-001", claude, limits=Limits(max_screenshots=2)))
    assert r["terminal_reason"] == "action_limit" and r["limit_reached"] == "max_screenshots" and r["accounting"]["screenshot_requests"] == 2
    openai = ScriptedOpenAI(oa_response("r1", oa_computer_call("c1", shot, shot, shot)), oa_response("r2", text="Hb 109 g/L"))
    r = browser(lambda: openai_browser.run_episode("find-result-001", openai, limits=Limits(max_screenshots=2)))
    assert r["terminal_reason"] == "action_limit" and r["limit_reached"] == "max_screenshots" and r["accounting"]["screenshot_requests"] == 2
    # a tool run records the same four
    tools = ScriptedClient(reply(tool_use("list_results", {"patient_id": 1})), reply(text("Hb 109 g/L."), stop_reason="end_turn"))
    r = claude_tools.run_episode(EPREnv("find-result-001"), tools)
    assert r["accounting"] == {"actions": 1, "screenshot_requests": 0, "automatic_screenshots": 0, "model_requests": 2}
    table = report.interaction_table([r])
    assert "| Screenshot requests |" in table.splitlines()[0] and "| claude-opus-5-5 (tool) | 1 | 1.0 | 0.0 | 0.0 | 2.0 |" in table


# --- 3. concurrent API calls cannot pass the action limit, and audit rows belong to one request -----


def test_concurrent_calls_reserve_the_budget_atomically(tmp_path, monkeypatch):
    real = ops.call
    monkeypatch.setattr(ops, "call", lambda *a, **k: (time.sleep(0.05), real(*a, **k))[1])  # widen any race
    task = load_task("order-ctpa-001")
    ep = Episode(task, harness="test", interface="api", limits=Limits(max_actions=2), out_dir=tmp_path)

    async def burst(url):
        async with httpx.AsyncClient() as c:
            return await asyncio.gather(*[c.post(f"{url}/api/ops/get_patient", json={"patient_id": 1}) for _ in range(6)])

    with serve_task(task, "api", ep) as (url, app):
        statuses = sorted(r.status_code for r in asyncio.run(burst(url)))
    assert statuses == [200, 200, 429, 429, 429, 429] and ep.actions == 2
    http = lines(ep.dir / "http.jsonl")
    ok = [h for h in http if h["status"] == 200]
    assert sorted(len(h["audit_ids"]) for h in ok) == [1, 1] and len({h["audit_ids"][0] for h in ok}) == 2


# --- 4. the CLI-agent harness states and counts what it can, and honours the final-chance flag ---------


def test_cli_counts_model_turns_from_an_event_stream_and_states_what_is_enforced(tmp_path):
    events = [{"type": "assistant", "message": {"id": f"m{i}", "content": []}} for i in (1, 1, 2, 3)]
    events.append({"type": "result", "subtype": "success", "result": "Hb 109 g/L", "usage": {"input_tokens": 30, "output_tokens": 3}})
    body = "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events)
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), isolate=False)
    assert r["usage"]["requests"] == 3 and r["usage"]["input_tokens"] == 30
    assert r["limits_enforced"]["max_requests"] is False  # a custom command is not known to honour a request limit
    assert cli_agent.limits_enforced("claude")["max_requests"] is True and cli_agent.limits_enforced("codex")["max_requests"] is False
    assert cli_agent.limits_enforced("claude")["final_chance_after"] == ["action_limit", "timeout"]


def test_cli_stops_an_agent_that_passes_the_request_limit(tmp_path):
    events = [{"type": "assistant", "message": {"id": f"m{i}", "content": []}} for i in range(5)]
    body = "".join(f"print({json.dumps(json.dumps(e))}, flush=True)\n" for e in events) + "import time\ntime.sleep(30)\n"
    started = time.monotonic()
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), limits=Limits(max_requests=3, final_chance_seconds=1), isolate=False)
    assert r["terminal_reason"] == "model_request_limit" and (r["final_chance"] or {}).get("late") and time.monotonic() - started < 15, {k: r.get(k) for k in ("terminal_reason", "ended_by", "error", "final_chance", "usage", "answer")}


def test_cli_action_limit_without_final_chance(tmp_path):
    body = "while call('get_current_time')['ok']:\n    pass\nprint(call('get_current_time')['message'])\ntime.sleep(30)\n"
    started = time.monotonic()
    r = cli_agent.run_episode("order-ctpa-001", agent(tmp_path, body), limits=Limits(max_actions=2, final_chance=False), isolate=False, out_dir=tmp_path / "out")
    assert r["terminal_reason"] == "action_limit" and r["final_chance"] is None and time.monotonic() - started < 15
    refused = [h for h in lines(only_dir(tmp_path / "out") / "http.jsonl") if h["status"] == 429]
    assert refused and "Reply now" not in refused[0]["response"]["message"] and r["limits_enforced"]["final_chance_after"] == []


# --- 5. the graded world is the saved world, nothing lands after it, and the agent's processes die ----


def test_grading_uses_a_world_restored_from_the_saved_snapshot(tmp_path, monkeypatch):
    seen = {}
    real = episode_module.grade

    def spy(world, task, answer):
        seen["world"], seen["bytes"] = world, world.snapshot()
        return real(world, task, answer)

    monkeypatch.setattr(episode_module, "grade", spy)
    env = EPREnv("order-ctpa-001")
    r = claude_tools.run_episode(env, ScriptedClient(reply(tool_use("submit", {"answer": "x"}))), out_dir=tmp_path)
    assert seen["world"] is not env.world and seen["bytes"] == (only_dir(tmp_path) / "final.sqlite").read_bytes()
    assert r["terminal_reason"] == "submitted"


def test_admission_closes_and_in_flight_requests_finish_before_shutdown(tmp_path, monkeypatch):
    task = load_task("order-ctpa-001")
    real = ops.call

    def slow_call(world, name, args=None, ctx=ops.Ctx()):
        if name == "write_note":
            time.sleep(0.5)
        return real(world, name, args, ctx)

    monkeypatch.setattr(ops, "call", slow_call)
    ep = Episode(task, harness="test", interface="api", out_dir=tmp_path)
    import threading

    statuses = []
    with serve_task(task, "api", ep) as (url, app):
        note = {"patient_id": 1, "title": "Late", "body": "Written as the episode ended."}
        t = threading.Thread(target=lambda: statuses.append(httpx.post(f"{url}/api/ops/write_note", json=note, timeout=10).status_code))
        t.start()
        time.sleep(0.1)
    t.join()
    assert statuses == [200] and app.state.world.one("SELECT count(*) AS n FROM note WHERE title = 'Late'")["n"] == 1
    app.state.recorder.close_admission()
    assert app.state.recorder.closed


def test_admission_closed_requests_are_refused_and_recorded(tmp_path):
    task = load_task("order-ctpa-001")
    ep = Episode(task, harness="test", interface="api", out_dir=tmp_path)
    with serve_task(task, "api", ep) as (url, app):
        app.state.recorder.close_admission()
        assert httpx.post(f"{url}/api/ops/get_current_time", json={}).status_code == 503
    assert lines(ep.dir / "http.jsonl")[-1]["status"] == 503 and ep.actions == 0


def test_the_whole_process_group_is_stopped_after_the_agent_exits(tmp_path):
    body = "p = subprocess.Popen(['sh', '-c', 'trap \"\" TERM; sleep 30'])\nprint(p.pid, flush=True)\ntime.sleep(0.3)\n"
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header="import subprocess, time\n"), isolate=False)
    pid = int(r["answer"])
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --- 6. failures never end as clinical outcomes ------------------------------------------------------


def test_a_grading_failure_is_a_harness_error_and_reports_tolerate_it(monkeypatch):
    monkeypatch.setattr(episode_module, "grade", lambda *a: 1 / 0)
    r = claude_tools.run_episode(EPREnv("find-result-001"), ScriptedClient(reply(tool_use("submit", {"answer": "Hb 109"}))))
    assert r["terminal_reason"] == "harness_error" and r["score"] is None and "ZeroDivisionError" in r["error"]
    r2 = {**r, "terminal_reason": "submitted"}  # even if a report claims completion, no score means no outcome
    assert "| 0 | 0 | 2 |" in report.summary_table([r, r2]) and "| - | - |" in report.summary_table([r, r2])
    assert report.outcome(r2) == "errored"


@pytest.mark.parametrize(
    "response, reason",
    [
        (oa_response("r1", status="failed"), "harness_error"),
        (oa_response("r1", status="cancelled"), "harness_error"),
        (NS(**{**vars(oa_response("r1", status="incomplete")), "incomplete_details": NS(reason="content_filter")}), "refusal"),
        (NS(**{**vars(oa_response("r1", text="Hb", status="incomplete")), "incomplete_details": NS(reason="max_output_tokens")}), "stopped_without_submit"),
    ],
)
def test_openai_failures_are_not_outcomes(response, reason):
    r = openai_tools.run_episode(EPREnv("find-result-001"), ScriptedOpenAI(response))
    assert r["terminal_reason"] == reason


@pytest.mark.parametrize("status, reason", [("failed", "harness_error"), ("cancelled", "harness_error"), ("budget_exceeded", "harness_error"), ("incomplete", "stopped_without_submit")])
def test_gemini_failures_are_not_outcomes(status, reason):
    r = gemini_tools.run_episode(EPREnv("find-result-001"), ScriptedGemini(NS(**{**vars(interaction("i1", text="Hb 109")), "status": status})))
    assert r["terminal_reason"] == reason


def test_an_unknown_anthropic_stop_is_a_harness_error():
    r = claude_tools.run_episode(EPREnv("find-result-001"), ScriptedClient(reply(text("..."), stop_reason="pause_turn")))
    assert r["terminal_reason"] == "harness_error"


# --- 7. isolation fails closed, and unsafe runs are kept out of the headline -------------------------


def test_no_sandbox_is_an_error_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation, "sandbox", lambda protected: ([], "no sandbox in this test"))
    marker = tmp_path / "ran"
    body = f"open({str(marker)!r}, 'w').write('x')\nprint('done')\n"
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""))
    assert r["terminal_reason"] == "harness_error" and "allow-unsandboxed" in r["error"] and not marker.exists() and r["sandboxed"] is False
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), allow_unsandboxed=True)
    assert r["terminal_reason"] == "submitted" and marker.exists() and r["sandboxed"] is False


def test_report_keeps_contaminated_and_unsandboxed_runs_out_of_the_outcomes():
    base = {"task_id": "t", "agent": "a", "interface": "api", "success": True, "score": 1.0, "unsafe": False, "operations": 1, "investigation_cost_gbp": 0}
    runs = [{**base, "terminal_reason": "submitted", "sandboxed": True}, {**base, "terminal_reason": "submitted", "contaminated": True, "sandboxed": True},
            {**base, "terminal_reason": "submitted", "sandboxed": False}, {**base, "terminal_reason": "submitted", "suspected": ["x"], "sandboxed": True}]  # fmt: skip
    table = report.summary_table(runs)
    assert "Contaminated" in table and "Unsandboxed" in table and "Suspected" in table
    assert "| a (api) | 4 | 2 | 0 | 0 | 1 | 1 | 1 | 2 of 2 (100%) |" in table


def test_the_profile_lets_the_interpreter_start_but_not_read_installed_packages():
    rules = isolation.profile(isolation.protected_paths())
    prefix = os.path.realpath(sys.prefix)
    assert f'(allow file-read* (subpath "{prefix}"))' not in rules and f'(literal "{prefix}/pyvenv.cfg")' in rules
    assert os.path.realpath(os.path.expanduser("~/.cache/uv")) in isolation.protected_paths() or os.environ.get("UV_CACHE_DIR")


@pytest.mark.skipif(not isolation.sandbox(isolation.protected_paths())[0], reason="no sandbox here")
def test_the_sandbox_hides_the_editable_install(tmp_path):
    import glob

    pth = glob.glob(os.path.join(sys.prefix, "lib", "python*", "site-packages", "*eprbench*.pth"))[0]
    body = f"try:\n    print(open({pth!r}).read())\nexcept OSError as e:\n    print('BLOCKED', type(e).__name__)\n"
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""))
    assert r["answer"].startswith("BLOCKED")


# --- 9. regrading keeps the episode's own context ------------------------------------------------------


def test_regrade_keeps_the_saved_start_time_and_actor(tmp_path, monkeypatch):
    claude_tools.run_episode(EPREnv("order-ctpa-001"), ScriptedClient(reply(tool_use("place_order", CTPA)), reply(tool_use("submit", {"answer": "done"}, "t2"))), out_dir=tmp_path)
    d = only_dir(tmp_path)
    moved = load_task("order-ctpa-001").model_copy(update={"start_time": "2026-03-20T09:00:00Z", "actor": "Someone Else"})
    monkeypatch.setattr(episode_module, "load_task", lambda task_id: moved)
    r = regrade(d)
    assert r["old_score"] == r["new_score"] == 1.0, [c for c in r["report"]["checks"] if not c["passed"]]


def test_the_grader_fingerprint_covers_what_grading_reads_through():
    names = set(episode_module.provenance(load_task("order-ctpa-001"))["grader_sha256"])
    assert names >= {"tasks.py", "safety.py", "scenario.py", "vitals.py", "news2.py", "world.py", "ops.py", "schema.sql"}


# --- 10. failed exchanges are recorded ------------------------------------------------------------------


def test_a_server_error_is_recorded(tmp_path, monkeypatch):
    import dataclasses

    def boom(world, args, ctx):
        raise RuntimeError("operation exploded")

    monkeypatch.setitem(ops.REGISTRY, "get_current_time", dataclasses.replace(ops.REGISTRY["get_current_time"], fn=boom))
    task = load_task("order-ctpa-001")
    ep = Episode(task, harness="test", interface="api", out_dir=tmp_path)
    with serve_task(task, "api", ep) as (url, app):
        assert httpx.post(f"{url}/api/ops/get_patient", content=b"[1]", headers={"content-type": "application/json"}).status_code == 422
        assert httpx.post(f"{url}/api/ops/get_current_time", json={}).status_code == 500
    refused, crashed = lines(ep.dir / "http.jsonl")
    assert refused["status"] == 422 and refused["request_body"] == "[1]" and refused["response"]["ok"] is False and refused["error"] is None
    assert crashed["status"] == 500 and "RuntimeError: operation exploded" in crashed["error"] and "Traceback" in crashed["error"]
    audit = World.restore(app.state.world.snapshot()).q("SELECT op, error FROM audit ORDER BY id")
    assert audit[-1] == {"op": "get_current_time", "error": "internal_error"} and crashed["audit_ids"]


# --- 11. only distinctive ground truth is contamination ------------------------------------------------


def test_generic_check_ids_are_only_suspected():
    found = isolation.scan("Result acknowledged in ICE (result_acknowledged).", ["/nowhere"])
    assert found["content"] == [] and found["suspected"] and "result_acknowledged" in found["suspected"][0]
    assert isolation.scan(CANARY, ["/nowhere"])["content"]


def test_a_generic_match_does_not_contaminate_a_run(tmp_path):
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, "print('Hb 109 g/L; right_patient_only checked')\n", header=""), isolate=False)
    assert r["contaminated"] is False and r["suspected"] and r["isolation"]["suspected"]


# --- 12. credentials never reach the evidence -------------------------------------------------------


def test_secrets_are_redacted_everywhere(tmp_path, monkeypatch):
    planted = {"env": "planted-env-secret-0123456789", "argv": "sk-planted-argv-0123456789abcdef", "inline": "planted-inline-token-98765", "header": "planted-header-token-55555"}
    monkeypatch.setenv("FAKE_SERVICE_API_KEY", planted["env"])
    body = (
        "import os\n"
        "print('key is', os.environ['FAKE_SERVICE_API_KEY'])\n"
        "print('key is', os.environ['FAKE_SERVICE_API_KEY'], file=sys.stderr)\n"
        f"req = urllib.request.Request(url + '/api/ops/get_current_time', b'{{}}', {{'content-type': 'application/json', 'authorization': 'Bearer {planted['header']}'}})\n"
        "print(urllib.request.urlopen(req).status)\n"
    )
    argv = agent(tmp_path, body) + ["--api-key", planted["argv"], f"--token={planted['inline']}"]
    cli_agent.run_episode("find-result-001", argv, out_dir=tmp_path / "out", isolate=False)
    sdk = ScriptedClient(reply(text(f"my key is {planted['env']}"), stop_reason="end_turn"))
    claude_tools.run_episode(EPREnv("find-result-001"), sdk, out_dir=tmp_path / "out")
    files = [p for p in (tmp_path / "out").rglob("*") if p.is_file() and p.suffix != ".png"]
    assert any(p.name == "agent.stdout" for p in files) and any(p.name == "http.jsonl" for p in files)
    for p in files:
        data = p.read_bytes()
        for where, secret in planted.items():
            assert secret.encode() not in data, (where, p.name)
    cli_dir = next(d for d in (tmp_path / "out").iterdir() if "python" in d.name)
    assert b"[REDACTED" in (cli_dir / "episode.json").read_bytes()


# --- canary ------------------------------------------------------------------------------------------


def test_evidence_carries_the_canary(tmp_path):
    claude_tools.run_episode(EPREnv("find-result-001"), ScriptedClient(reply(tool_use("submit", {"answer": "Hb 109"}))), out_dir=tmp_path)
    d = only_dir(tmp_path)
    for name in ("episode.json", "result.json", "report.json"):
        assert json.loads((d / name).read_text())["canary"] == CANARY
