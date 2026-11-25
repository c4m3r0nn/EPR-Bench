"""The evidence record, offline regrading and the run command's checks."""

import json
import sys

import pytest

from eprbench.agents import claude_tools
from eprbench.env import EPREnv
from eprbench.episode import Episode, Limits, episode_dirs, regrade
from eprbench.tasks import build_world, load_task
from eprbench.world import World
from tests.test_agents import CTPA, ScriptedClient, reply, text, tool_use


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def ctpa_client():
    return ScriptedClient(
        reply(text("Finding her."), tool_use("search_patients", {"query": "Ellison"})),
        reply(tool_use("place_order", CTPA, "tu_2"), tool_use("get_patient", {"patient_id": 404}, "tu_3")),
        reply(tool_use("submit", {"answer": "CTPA requested."}, "tu_4")),
    )


def test_evidence_record_is_complete(tmp_path):
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), ctpa_client(), effort="high", out_dir=tmp_path, repeat=2)
    out = tmp_path / report["evidence_dir"].split("/")[-1]
    assert report["evidence_dir"] == str(out) and report["repeat"] == 2

    meta = json.loads((out / "episode.json").read_text())
    assert (meta["task_id"], meta["repeat"], meta["harness"], meta["provider"], meta["model"], meta["effort"]) == ("order-ctpa-001", 2, "claude_tools", "anthropic", "claude-opus-5-5", "high")
    assert meta["limits"] == {"max_requests": 200, "max_actions": 500, "max_screenshots": 500, "timeout_seconds": 3600.0, "final_chance": True, "final_chance_seconds": 120.0} and meta["settings"]["max_tokens"] == 16000
    p = meta["provenance"]
    assert len(p["git_commit"]) == 40 and isinstance(p["git_dirty"], bool) and p["task_matches_packaged_file"]
    assert set(p["data_sha256"]) == {"bundles/demo.json", "catalogues/dmd.json", "catalogues/imaging.json", "catalogues/forms.json"}
    assert set(p["grader_sha256"]) >= {"tasks.py", "safety.py", "scenario.py", "ops.py", "schema.sql"} and p["versions"]["anthropic"] and len(p["code_sha256"]) == 64
    assert load_task("order-ctpa-001") == load_task("order-ctpa-001").model_validate_json((out / "task.json").read_text())

    events = lines(out / "transcript.jsonl")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    kinds = [e["kind"] for e in events]
    assert kinds == ["request", "response", "action", "request", "response", "action", "action", "request", "response", "end"]
    first, second = events[0]["params"], events[3]["params"]
    assert first["system"].startswith("You are a doctor") and len(first["tools"]) > 10 and first["messages_from"] == 0
    assert set(second["unchanged"]) >= {"tools", "system", "model"} and second["messages_from"] == 1  # only what is new is repeated
    # usage as the response reported it: the scripted response names no cache counts
    assert events[1]["usage"] == {"input_tokens": 100, "cache_creation_input_tokens": None, "cache_read_input_tokens": None, "output_tokens": 10}
    assert events[1]["body"]["content"][1]["name"] == "search_patients"
    order = events[5]
    assert order["name"] == "place_order" and order["result"]["order_id"] and order["audit_id"]
    assert events[-1] == {**events[-1], "terminal_reason": "submitted", "answer": "CTPA requested."}

    audit = lines(out / "audit.jsonl")
    placed = next(a for a in audit if a["op"] == "place_order")
    assert placed["actor"] == "Dr A. Gent (FY1)" and placed["patient_id"] == 1 and placed["at"] and placed["result"] == order["result"]
    assert next(a for a in audit if a["op"] == "get_patient")["result"]["error"] == "not_found"

    world = World.restore((out / "final.sqlite").read_bytes())
    assert world.one("SELECT count(*) AS n FROM \"order\" WHERE orderable_code = 'CTPA'")["n"] == 1
    assert json.loads((out / "report.json").read_text())["score"] == report["score"] == 1.0
    result = json.loads((out / "result.json").read_text())
    assert result["terminal_reason"] == "submitted" and result["answer"] == "CTPA requested." and result["usage"]["input_tokens"] == 300
    assert result["requests"] == 3 and result["actions"] == 3 and result["started_at"] <= result["ended_at"] and result["error"] is None
    # nothing from the wall clock reached the world
    assert world.now == build_world(load_task("order-ctpa-001")).now


def test_a_crash_leaves_what_happened_so_far(tmp_path):
    client = ScriptedClient(reply(tool_use("place_order", CTPA)))  # the second request fails
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client, out_dir=tmp_path)
    out = tmp_path / report["evidence_dir"].split("/")[-1]
    events = lines(out / "transcript.jsonl")
    assert [e["kind"] for e in events] == ["request", "response", "action", "request", "attempt_failed", "error", "end"]
    assert "Traceback" in events[-2]["traceback"] and "IndexError" in events[-2]["traceback"]
    result = json.loads((out / "result.json").read_text())
    assert result["terminal_reason"] == "harness_error" and "IndexError" in result["error"] and (out / "final.sqlite").is_file()


def test_an_interrupt_is_recorded_and_not_swallowed(tmp_path):
    task = load_task("order-ctpa-001")
    with pytest.raises(KeyboardInterrupt):
        with Episode(task, harness="test", interface="tool", out_dir=tmp_path) as ep:
            ep.event("note", text="working")
            raise KeyboardInterrupt
    result = json.loads((ep.dir / "result.json").read_text())
    assert result["terminal_reason"] == "harness_error" and "KeyboardInterrupt" in result["error"]


def test_regrade_round_trip(tmp_path, capsys, monkeypatch):
    claude_tools.run_episode(EPREnv("order-ctpa-001"), ctpa_client(), out_dir=tmp_path)
    find = ScriptedClient(reply(tool_use("submit", {"answer": "Hb 109 g/L"})))
    claude_tools.run_episode(EPREnv("find-result-001"), find, out_dir=tmp_path)
    dirs = episode_dirs(tmp_path)
    assert len(dirs) == 2
    for d in dirs:
        r = regrade(d)
        assert r["old_score"] == r["new_score"] == 1.0 and not r["grader_changed"] and not r["task_changed"]
        assert r["report"]["checks"] == json.loads((d / "report.json").read_text())["checks"]

    # a changed task definition is applied to the saved world and answer
    find_dir = next(d for d in dirs if "find-result" in d.name)
    saved = json.loads((find_dir / "task.json").read_text())
    saved["checks"][0]["answer_regex"] = r"\b110\b"
    (find_dir / "task.json").write_text(json.dumps(saved))
    r = regrade(find_dir, saved_task=True)
    assert (r["old_score"], r["new_score"], r["task_changed"]) == (1.0, 0.0, True)

    from eprbench.__main__ import main

    monkeypatch.setattr(sys, "argv", ["eprbench", "regrade", str(tmp_path)])
    main()
    out = capsys.readouterr().out
    # the find-result episode ran with checks that now differ from the packaged ones (edited above); it is graded with the current ones
    assert "| Old tier | New tier |" in out and "| Graded world reproduced | Horizon world reproduced |" in out
    assert out.count("| 1.00 | 1.00 | complete | complete | no | no | no | yes | - |") == 1
    assert out.count("| 1.00 | 1.00 | complete | complete | no | no | yes | yes | - |") == 1


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--provider", "gemini", "--interface", "ui"], "does not support --interface ui"),
        (["--agent", "claude", "--interface", "tool"], "the web UI (--interface ui)"),
        (["--interface", "api"], "does not support --interface api"),
        (["--allow-unsandboxed"], "agent CLI runs only"),
    ],
)
def test_unsupported_run_combinations_are_rejected(monkeypatch, capsys, argv, message):
    from eprbench.__main__ import main

    monkeypatch.setattr(sys, "argv", ["eprbench", "run", "find-result-001", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2 and message in capsys.readouterr().err


def test_run_passes_limits_and_out_dir_to_every_harness(monkeypatch, capsys, tmp_path):
    from eprbench.__main__ import main
    from eprbench.agents import claude_browser

    seen = {}

    def fake(task_id, client, model, effort, **kw):
        seen.update(kw, model=model, effort=effort)
        return {"task_id": task_id, "terminal_reason": "submitted", "answer": "x"}

    monkeypatch.setattr(claude_browser, "run_episode", fake)
    monkeypatch.setattr("anthropic.Anthropic", lambda: None)
    monkeypatch.setattr(sys, "argv", ["eprbench", "run", "find-result-001", "--interface", "ui", "--max-turns", "7", "--max-actions", "9",
                                      "--timeout", "30", "--no-final-chance", "--out-dir", str(tmp_path), "--i-accept-unmetered-spend",
                                      "--allow-alias"])  # fmt: skip
    main()
    assert seen["limits"] == Limits(max_requests=7, max_actions=9, timeout_seconds=30, final_chance=False) and seen["out_dir"] == str(tmp_path)
    assert seen["limits"].final_chance_seconds == Limits.final_chance_seconds
    assert json.loads(capsys.readouterr().out)["answer"] == "x"


def test_ui_forms_work_through_the_recorder_and_are_recorded(tmp_path):
    import httpx

    from eprbench.server.local import serve_task

    task = load_task("order-ctpa-001")
    ep = Episode(task, harness="test", interface="ui", out_dir=tmp_path)
    with serve_task(task, "ui", ep) as (url, app):
        r = httpx.post(f"{url}/patients/1/notes", data={"kind": "progress", "title": "Review", "body": "Seen on the ward round."}, follow_redirects=True)
    assert r.status_code == 200 and app.state.world.one("SELECT count(*) AS n FROM note WHERE title = 'Review'")["n"] == 1
    http = lines(ep.dir / "http.jsonl")
    post = next(h for h in http if h["method"] == "POST")
    note = app.state.world.one("SELECT id FROM audit WHERE op = 'write_note'")["id"]
    assert "Seen+on+the+ward+round" in post["request_body"] and note in post["audit_ids"] and ep.actions == 0  # UI actions are counted by the browser harness


def test_audit_evidence_carries_the_patient_resolved_by_the_operation(tmp_path):
    """Operations addressed by object id (here a result) are attributed to the patient ops.call resolves."""
    task = load_task("find-result-001")
    result_id = build_world(task).one("SELECT id FROM result WHERE patient_id = 1 AND reported_at IS NOT NULL ORDER BY id LIMIT 1")["id"]
    client = ScriptedClient(reply(tool_use("get_result", {"result_id": result_id, "patient_id": 1})), reply(tool_use("submit", {"answer": "Hb 109 g/L"}, "t2")))
    claude_tools.run_episode(EPREnv(task), client, out_dir=tmp_path)
    out = next(tmp_path.iterdir())
    (row,) = [a for a in lines(out / "audit.jsonl") if a["op"] == "get_result"]
    assert row["args"] == {"result_id": result_id, "patient_id": 1} and row["patient_id"] == 1 and row["result"]["result_id"] == result_id
    schema = next(t for t in EPREnv(task).tools if t["name"] == "get_result")["input_schema"]
    assert "patient_id" in schema["required"]  # one policy on every agent surface: an object by id is opened only with its patient named


def test_a_persons_session_is_credited_to_them_and_leaves_the_same_evidence(tmp_path):
    """v2 checks credit only the task's actor, so a person serving under their own name must be the actor."""
    import httpx

    from eprbench.server.local import human_session
    from eprbench.tasks import grade
    from tests.test_typed_answers import EXPECTED, FORM, RESULT, TASK, verdict

    person = TASK.model_copy(update={"actor": "Dr J. Human (FY1)"})

    def work(url, app):
        assert httpx.get(f"{url}/ice/patients/1", params={"tab": "results", "result_id": RESULT}).status_code == 200
        assert httpx.post(f"{url}/reply", data=FORM).status_code == 200  # checked, not yet submitted
        assert httpx.post(f"{url}/reply/confirm", data=FORM, follow_redirects=True).status_code == 200

    report = human_session(person, "ui", tmp_path, port=0, on_ready=work, poll=0.01)
    assert report["terminal_reason"] == "submitted" and report["harness"] == "human" and report["agent"] == "Dr J. Human (FY1)"
    assert verdict(report) == EXPECTED and report["actions_summary"]["ui"]["actions"] == 1  # the result page; the reply runs no operation
    out = next(tmp_path.iterdir())
    actions = [e for e in lines(out / "transcript.jsonl") if e["kind"] == "action"]
    assert [a["name"] for a in actions] == ["GET /ice/patients/1", "POST /reply", "POST /reply/confirm", "GET /reply"] and actions[0]["args"]["result_id"] == str(RESULT)
    assert {a["actor"] for a in lines(out / "audit.jsonl")} == {"Dr J. Human (FY1)"}
    assert json.loads((out / "task.json").read_text())["actor"] == "Dr J. Human (FY1)"
    # graded as the task's own actor instead, the person's work would earn nothing: the defect this fixes
    world = World.restore((out / "final.sqlite").read_bytes())
    assert verdict(grade(world, TASK, report["answer"]))["checks"]["looked_up"] is False
    assert regrade(out, saved_task=True)["new_tier"] == "complete"


def test_serve_makes_the_user_the_actor(monkeypatch, tmp_path):
    import uvicorn

    from eprbench.__main__ import main
    from eprbench.server import local

    seen = {}
    monkeypatch.setattr(local, "human_session", lambda task, mode, out_dir, *a, **kw: seen.update(task=task, mode=mode, out_dir=out_dir) or {"ok": 1})
    monkeypatch.setattr(sys, "argv", ["eprbench", "serve", "find-result-001", "--user", "Dr J. Human", "--out-dir", str(tmp_path), "--mode", "ui"])
    main()
    assert seen["task"].actor == "Dr J. Human" and seen["mode"] == "ui" and seen["out_dir"] == str(tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app))
    monkeypatch.setattr(sys, "argv", ["eprbench", "serve", "find-result-001", "--user", "Dr J. Human"])
    main()
    assert seen["app"].state.task.actor == "Dr J. Human"
