"""Regression tests for the second review of the episode contract: one group per item."""

import asyncio
import hashlib
import json
import socket
import threading
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from eprbench import episode as episode_module
from eprbench import loader, report, tasks
from eprbench.agents import claude_tools, cli_agent, gemini_tools, openai_tools
from eprbench.env import EPREnv
from eprbench.episode import Episode, Limits, regrade
from eprbench.server.local import serve_task
from eprbench.tasks import build_world, load_task
from eprbench.world import World
from tests.test_agents import CTPA, ScriptedClient, ScriptedGemini, ScriptedOpenAI, interaction, oa_call, oa_response, reply, text, tool_use
from tests.test_isolation import agent


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def only_dir(root):
    (d,) = [p for p in root.iterdir() if p.is_dir()]
    return d


@pytest.fixture
def black_hole():
    """A local port that accepts connections and never answers: every request to it times out."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    held, stop = [], threading.Event()

    def accept():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join()
    for c in held:
        c.close()
    server.close()


# --- 1. the deadline covers every attempt; a timeout goes to the final chance ---------------------------


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_a_real_sdk_client_cannot_overrun_the_deadline(black_hole, provider):
    if provider == "anthropic":
        import anthropic

        client, run = anthropic.Anthropic(api_key="test", base_url=black_hole), claude_tools.run_episode
    else:
        import openai

        client, run = openai.OpenAI(api_key="test", base_url=black_hole), openai_tools.run_episode
    started = time.monotonic()
    # a real SDK client, but pointed at a local server that never answers: nothing is billed, so unmetered on purpose
    r = run(EPREnv("find-result-001"), client, limits=Limits(timeout_seconds=0.3, final_chance_seconds=0.3), unmetered=True)
    assert time.monotonic() - started < 2.0
    assert r["terminal_reason"] == "timeout" and r["usage"]["requests"] == 2  # the attempt that timed out, and the final chance
    assert r["final_chance"] == {"reason": "timeout", "request": 2, "answer": None, "late": True}


def test_sdk_retries_are_switched_off():
    import anthropic
    import google.genai as genai
    import openai

    from eprbench.agents import without_retries

    assert without_retries(anthropic.Anthropic(api_key="x")).max_retries == 0
    assert without_retries(openai.OpenAI(api_key="x")).max_retries == 0
    assert without_retries(genai.Client(api_key="x")).interactions.sdk_configuration.retry_config.strategy == "none"


class Transient(Exception):
    status_code = 503


def test_a_transient_failure_is_retried_within_the_deadline_and_counted():
    client = ScriptedClient(reply(tool_use("submit", {"answer": "Hb 109"})))
    create, calls = client.messages.create, []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise Transient("overloaded")
        return create(**kw)

    client.messages.create = flaky
    r = claude_tools.run_episode(EPREnv("find-result-001"), client)
    assert r["terminal_reason"] == "submitted" and r["usage"]["requests"] == 2


# --- 4. CLI final chance: its own allowance, and a late answer is not accepted ---------------------------


def test_cli_final_chance_has_its_own_allowance(tmp_path):
    body = "import time, sys\ntime.sleep(1.5)\nprint('Hb 109 g/L')\n"
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), limits=Limits(timeout_seconds=1, final_chance_seconds=3), isolate=False)
    assert r["terminal_reason"] == "timeout" and r["final_chance"]["late"] is False and r["answer"] == "Hb 109 g/L"


def test_a_late_cli_answer_is_not_accepted(tmp_path):
    body = "import time, sys\nprint('partial', flush=True)\ntime.sleep(30)\nprint('Hb 109 g/L')\n"
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), limits=Limits(timeout_seconds=0.5, final_chance_seconds=0.5),
                              isolate=False, out_dir=tmp_path / "out")  # fmt: skip
    assert r["terminal_reason"] == "timeout" and r["final_chance"]["late"] is True and r["answer"] is None
    assert (only_dir(tmp_path / "out") / "agent.stdout").read_text() == "partial\n"  # kept as evidence, not as an answer


# --- 5. the saved world is the graded world, even with a rule falling due --------------------------------


def test_due_rules_fire_before_the_snapshot(tmp_path):
    six_hours = [tool_use("wait", {"seconds": 3600}, f"w{k}") for k in range(6)]  # an hour a call (ops.MAX_WAIT_SECONDS)
    client = ScriptedClient(reply(*six_hours), reply(tool_use("submit", {"answer": "waited"}, "t2")))
    r = claude_tools.run_episode(EPREnv("mgmt-sob-001"), client, out_dir=tmp_path)
    saved = (only_dir(tmp_path) / "final.sqlite").read_bytes()
    world = World.restore(saved)
    assert world.one("SELECT fired_at FROM scenario_rule WHERE name = 'untreated-deterioration'")["fired_at"] is not None
    from eprbench import scenario

    scenario.fire_due(world)
    assert world.snapshot() == saved and r["graded_world_sha256"] == hashlib.sha256(saved).hexdigest()


# --- 6. a provider's failure status wins over any calls in the response ----------------------------------


def test_openai_failed_response_with_a_submit_call_is_an_error():
    r = openai_tools.run_episode(EPREnv("find-result-001"), ScriptedOpenAI(oa_response("r1", oa_call("submit", {"answer": "Hb 109"}), status="failed")))
    assert r["terminal_reason"] == "harness_error" and r["answer"] != "Hb 109"


def test_gemini_failed_interaction_with_a_submit_call_is_an_error():
    failed = NS(**{**vars(interaction("i1", NS(type="function_call", name="submit", arguments={"answer": "Hb 109"}, id="f1"))), "status": "failed"})
    r = gemini_tools.run_episode(EPREnv("find-result-001"), ScriptedGemini(failed))
    assert r["terminal_reason"] == "harness_error"


# --- 10. a request cancelled while it waits is recorded ---------------------------------------------------


def test_a_cancelled_request_is_recorded(tmp_path, monkeypatch):
    from eprbench.server.app import create_app
    from eprbench.server.local import Recorder

    task = load_task("order-ctpa-001")
    ep = Episode(task, harness="test", interface="api", out_dir=tmp_path)
    app = create_app(lambda: build_world(task), mode="api", task=task)

    class Slow:  # holds the lock for a while without blocking the event loop
        state = app.state

        async def __call__(self, scope, receive, send):
            await asyncio.sleep(0.4)
            await app(scope, receive, send)

    recorder = Recorder(Slow(), ep, count_actions=True)

    def scope():
        return {"type": "http", "method": "POST", "path": "/api/ops/get_current_time", "query_string": b"", "headers": [(b"content-type", b"application/json")]}

    async def go():
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            pass

        first = asyncio.create_task(recorder(scope(), receive, send))
        await asyncio.sleep(0.1)
        second = asyncio.create_task(recorder(scope(), receive, send))
        await asyncio.sleep(0.1)
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(go())
    http = lines(ep.dir / "http.jsonl")
    assert len(http) == 2 and any("Cancelled" in (h["error"] or "") for h in http), [(h["status"], h["error"]) for h in http]


# --- 12 and B. redaction: known values everywhere, patterns only in agent output ------------------------


def test_basic_authorisation_in_agent_output_and_headers_is_redacted(tmp_path):
    basic = "dXNlcjpwbGFudGVkLWJhc2ljLXNlY3JldA=="
    body = (
        f"print(json.dumps({{'headers': {{'Authorization': 'Basic {basic}'}}}}))\n"
        f"req = urllib.request.Request(url + '/api/ops/get_current_time', b'{{}}', {{'content-type': 'application/json', 'authorization': 'Basic {basic}'}})\n"
        "print(urllib.request.urlopen(req).status)\n"
    )
    cli_agent.run_episode("find-result-001", agent(tmp_path, body), out_dir=tmp_path / "out", isolate=False)
    for p in only_dir(tmp_path / "out").rglob("*"):
        if p.is_file():
            assert basic.encode() not in p.read_bytes(), p.name


def test_clinical_text_that_looks_like_a_key_is_kept(tmp_path):
    note = "Ref sk-clinical-reference-12345678 and Bearer tokens-are-not-here-123"
    client = ScriptedClient(reply(text(note), stop_reason="end_turn"))
    r = claude_tools.run_episode(EPREnv("find-result-001"), client, out_dir=tmp_path)
    d = only_dir(tmp_path)
    assert r["answer"] == note and json.loads((d / "result.json").read_text())["answer"] == note
    assert note in (d / "transcript.jsonl").read_text()
    meta = json.loads((d / "episode.json").read_text())
    assert "values" in meta["redaction"]["policy"] and meta["redaction"]["counts"] == {}


def test_recorded_page_hashes_match_the_files(tmp_path, monkeypatch):
    task = load_task("order-ctpa-001")
    with serve_task(task, "ui") as (url, app):
        page = httpx.get(url + "/").text
    monkeypatch.setenv("FAKE_PAGE_API_KEY", page[:40])  # a "secret" that appears in the page, so the saved page is redacted
    ep = Episode(task, harness="test", interface="ui", out_dir=tmp_path)
    with serve_task(task, "ui", ep) as (url, app):
        httpx.get(url + "/")
    for h in lines(ep.dir / "http.jsonl"):
        if "response_file" in h:
            assert hashlib.sha256((ep.dir / h["response_file"]).read_bytes()).hexdigest() == h["response_sha256"] and h["redacted"]


# --- A. a stalled upload does not stall the server ------------------------------------------------------


def test_a_stalled_upload_does_not_block_other_requests(tmp_path, monkeypatch):
    from eprbench.server import local

    monkeypatch.setattr(local, "BODY_SECONDS", 1.0)
    task = load_task("order-ctpa-001")
    ep = Episode(task, harness="test", interface="ui", out_dir=tmp_path)
    started = time.monotonic()
    with serve_task(task, "ui", ep) as (url, app):
        host, port = url.removeprefix("http://").split(":")
        stalled = socket.create_connection((host, int(port)))
        stalled.sendall(b"POST /patients/1/notes HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 100\r\n\r\ntitle=x")
        time.sleep(0.2)
        t = time.monotonic()
        assert httpx.get(url + "/", timeout=5).status_code == 200 and time.monotonic() - t < 0.8
        time.sleep(1.2)
    stalled.close()
    assert time.monotonic() - started < 10
    assert any(h["status"] == 408 and h["method"] == "POST" for h in lines(ep.dir / "http.jsonl"))


# --- C. running out of context is a truncation, not an error ---------------------------------------------


def test_context_exhaustion_is_a_truncation():
    r = claude_tools.run_episode(EPREnv("find-result-001"), ScriptedClient(reply(text("..."), stop_reason="model_context_window_exceeded")))
    assert r["terminal_reason"] == "context_limit" and r["truncated"]
    assert report.outcome(r) == "truncated"


class ContextError(Exception):
    status_code = 400

    def __init__(self):
        super().__init__("Error code: 400 - {'error': {'code': 'context_length_exceeded', 'message': 'Your input exceeds the context window'}}")


def test_a_context_length_error_is_a_truncation():
    client = ScriptedOpenAI()
    client.responses.create = lambda **kw: (_ for _ in ()).throw(ContextError())
    r = openai_tools.run_episode(EPREnv("find-result-001"), client)
    assert r["terminal_reason"] == "context_limit"
    failed = NS(**{**vars(oa_response("r1", status="failed")), "error": NS(code="context_length_exceeded", message="too long")})
    assert openai_tools.run_episode(EPREnv("find-result-001"), ScriptedOpenAI(failed))["terminal_reason"] == "context_limit"


@pytest.mark.parametrize(
    "status, errors, reason",
    [
        ("failed", [NS(code="INVALID_ARGUMENT", message="The input token count exceeds the maximum number of tokens allowed")], "context_limit"),
        ("failed", [NS(code="SAFETY", message="Response blocked for safety reasons")], "refusal"),
        ("failed", [NS(code="INTERNAL", message="backend error")], "harness_error"),
        ("incomplete", [NS(code="MAX_TOKENS", message="")], "stopped_without_submit"),
    ],
)
def test_gemini_causes(status, errors, reason):
    i = NS(**{**vars(interaction("i1", text="Hb 109")), "status": status, "errors": errors})
    assert gemini_tools.run_episode(EPREnv("find-result-001"), ScriptedGemini(i))["terminal_reason"] == reason


# --- D. a saved task need not still be packaged ---------------------------------------------------------


def test_regrade_with_the_saved_task_of_a_task_no_longer_packaged(tmp_path):
    claude_tools.run_episode(EPREnv("find-result-001"), ScriptedClient(reply(tool_use("submit", {"answer": "Hb 109"}))), out_dir=tmp_path)
    d = only_dir(tmp_path)
    for name in ("task.json", "result.json"):
        data = json.loads((d / name).read_text())
        data["id" if name == "task.json" else "task_id"] = "retired-task-001"
        (d / name).write_text(json.dumps(data))
    r = regrade(d, saved_task=True)
    assert r["new_score"] == r["old_score"] == 1.0 and r["task_changed"] is None
    with pytest.raises(FileNotFoundError, match="--saved-task"):
        regrade(d)


# --- E and F. caches: content keys, explicit invalidation, and speed -------------------------------------


def test_env_rebuilds_when_its_task_changes():
    env = EPREnv("order-ctpa-001")
    env.reset()
    env.task = env.task.model_copy(update={"start_time": "2026-03-14T11:00:00Z"})
    env.reset()
    assert env.world.now == tasks.parse_iso("2026-03-14T11:00:00Z")


def test_files_are_read_once_per_process_until_caches_are_invalidated(tmp_path, monkeypatch):
    original = loader.data_file
    (tmp_path / "bundles").mkdir()
    path = tmp_path / "bundles" / "demo.json"
    path.write_bytes(original("bundles", "demo").read_bytes())
    monkeypatch.setattr(loader, "data_file", lambda kind, name: path if kind == "bundles" else original(kind, name))
    tasks.invalidate_caches()
    task = load_task("order-ctpa-001")
    name = lambda w: w.one("SELECT family_name FROM patient WHERE id = 1")["family_name"]  # noqa: E731
    env = EPREnv(task)
    env.reset()
    assert name(build_world(task)) == name(env.world) == "Ellison"
    path.write_text(path.read_text().replace('"Ellison"', '"Ellisom"', 1))
    world = build_world(task)
    assert name(world) == "Ellison"  # not picked up: the bytes already hashed are the ones used
    assert world.source_sha256["bundles/demo.json"] == episode_module.provenance(task)["data_sha256"]["bundles/demo.json"]
    assert world.source_sha256["bundles/demo.json"] != hashlib.sha256(path.read_bytes()).hexdigest()
    tasks.invalidate_caches()
    world = build_world(task)
    env.reset()
    assert name(world) == name(env.world) == "Ellisom"
    assert world.source_sha256["bundles/demo.json"] == hashlib.sha256(path.read_bytes()).hexdigest() == episode_module.provenance(task)["data_sha256"]["bundles/demo.json"]
    tasks.invalidate_caches()


def test_build_world_is_fast_when_warm():
    task = load_task("order-ctpa-001")
    build_world(task)
    t = time.perf_counter()
    for _ in range(100):
        build_world(task)
    assert (time.perf_counter() - t) / 100 < 0.0015  # target 0.6 ms; generous here so a busy machine does not fail it


# --- G. comparability between tool and UI runs ------------------------------------------------------------


def test_the_timer_starts_at_the_first_model_request():
    task = load_task("find-result-001")
    ep = Episode(task, harness="test", interface="tool", limits=Limits(timeout_seconds=0.2))
    time.sleep(0.3)  # set-up: a server or browser starting
    with ep:
        ep.send(lambda **kw: NS(usage=NS(input_tokens=1, output_tokens=1)), lambda r: {})
    assert ep.stop is None and ep.extra["comparability"]["setup_seconds"] >= 0.3


def test_malformed_calls_are_charged_and_a_failed_call_stops_the_batch():
    client = ScriptedOpenAI(oa_response("r1", oa_call("get_patient", "{not json", "c1")), oa_response("r2", text="done"))
    assert openai_tools.run_episode(EPREnv("order-ctpa-001"), client)["steps"] == 1
    client = ScriptedClient(reply(tool_use("get_patient", {"patient_id": 404}), tool_use("place_order", CTPA, "t2")), reply(text("x"), stop_reason="end_turn"))
    r = claude_tools.run_episode(EPREnv("order-ctpa-001"), client)
    assert r["steps"] == 1 and not any(s["op"] == "place_order" for s in r["trajectory"])
    assert "earlier" in client.requests[1]["messages"][-1]["content"][1]["content"]
    assert set(r["comparability"]) >= {"timer_starts", "setup_seconds", "action_unit", "batch_stops_on", "malformed_calls", "screenshots"}
