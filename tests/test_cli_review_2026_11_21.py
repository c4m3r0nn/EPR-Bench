"""The review of the agent-CLI routes of 2026-11-21 (docs/codex/cli-browser-2026-11-21/codex-review.md): each finding
reproduced, then held fixed. Finding 1 (the test guard) is tests/test_agent_cli_guard.py; finding 4 (the potassium
window) is in tests/test_preview_cases.py."""

import base64
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from eprbench import runner
from eprbench.agents import claude_browser, cli_agent, cli_browser
from eprbench.agents.claude_browser import BOUNDS, Screen, Stopped
from eprbench.episode import Limits, redact_output, redact_stream_line
from tests.test_cli_browser import API_STAND_IN, DX, STEMI, Twin, hb_calls, results, run_cli, stand_in  # noqa: F401  (hb_calls is a fixture)

sync_api = pytest.importorskip("playwright.sync_api")


# --- 2. one call cannot outrun the limits ----------------------------------------------------------------------------


class FakeKeyboard:
    def __init__(self):
        self.pressed, self.typed = [], []

    def press(self, key):
        self.pressed.append(key)

    def type(self, text):
        self.typed.append(text)

    def down(self, key):
        pass

    def up(self, key):
        pass


class FakePage:
    def __init__(self):
        self.keyboard = FakeKeyboard()
        self.mouse = type("Mouse", (), {"move": lambda *a: None, "wheel": lambda *a: None})()

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self):
        pass


@pytest.mark.parametrize("name, args", [
    ("key", {"text": "Tab", "repeat": BOUNDS["repeat"] + 1}),
    ("key", {"text": "Tab", "repeat": 100000}),
    ("scroll", {"coordinate": [1, 1], "scroll_direction": "down", "scroll_amount": -1}),  # more than the most is cut to it (the audit of the paired run)
    ("type", {"text": "x" * (BOUNDS["type_chars"] + 1)}),
    ("hold_key", {"text": "shift", "duration": 30}),
    ("wait", {"duration": 60}),
])  # fmt: skip
def test_every_repeat_and_duration_is_bounded_on_every_browser_route(name, args):
    page = FakePage()
    with pytest.raises(ValueError):
        Screen(page).run(name, args)
    assert page.keyboard.pressed == [] and page.keyboard.typed == []


def test_a_repeated_action_stops_at_the_deadline_or_the_answer():
    page, presses = FakePage(), []
    screen = Screen(page, should_stop=lambda: len(page.keyboard.pressed) >= 3)
    with pytest.raises(Stopped):
        screen.run("key", {"text": "Tab", "repeat": 10})
    assert len(page.keyboard.pressed) == 3
    typing = Screen(FakePage(), should_stop=lambda: True)
    with pytest.raises(Stopped):
        typing.run("type", {"text": "a" * 500})
    assert typing.page.keyboard.typed == [] and presses == []


def hb_reply_calls():
    """find-result-001 answered on its reply page (a task without a form takes an answer there too)."""
    twin = Twin("find-result-001")
    try:
        twin.click("#reply-link").type("#reply-text", "Hb 109 g/L").click("#reply-form button").click("#reply-confirm button")
        assert twin.app.state.submission is not None
        return twin.calls
    finally:
        twin.close()


def test_after_the_answer_the_agent_is_still_stopped_at_the_deadline(tmp_path):
    """The submission no longer suspends the clock: an agent that lingers is stopped at the deadline, not after the grace."""
    started = time.monotonic()
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_reply_calls(), final="done", sleep_after=60), isolate=False,
                                limits=Limits(timeout_seconds=4))  # fmt: skip
    assert r["terminal_reason"] == "submitted" and r["ended_by"] == "reply page" and "Hb 109 g/L" in json.dumps(r["answer"])
    assert time.monotonic() - started < 25 < cli_browser.SUBMIT_GRACE_SECONDS + 4


def test_the_watchdog_stops_the_agent_while_a_browser_action_hangs(tmp_path, monkeypatch, hb_calls):
    real = Screen.run

    def hangs(self, name, args):
        if name == "left_click":
            time.sleep(8)  # a browser action that will not return in time
        return real(self, name, args)

    monkeypatch.setattr(Screen, "run", hangs)
    monkeypatch.setattr(cli_browser, "WATCHDOG_GRACE_SECONDS", 0.5)
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_calls, final="Hb 109 g/L"), isolate=False,
                                limits=Limits(timeout_seconds=1, final_chance_seconds=0.5), out_dir=tmp_path / "out")  # fmt: skip
    events = [json.loads(x) for x in (Path(r["evidence_dir"]) / "transcript.jsonl").read_text().splitlines()]
    (fired,) = [e for e in events if e["kind"] == "watchdog"]
    exited = next(e for e in events if e["kind"] == "agent_exited")
    assert fired["seconds"] < 4 and fired["t"] < exited["t"] and exited["returncode"] != 0
    assert r["terminal_reason"] == "timeout" and r["final_chance"]["watchdog"] and r["final_chance"]["answer"] is None


# --- 3. both routes of a CLI under one controlled configuration ------------------------------------------------------


def test_both_routes_of_each_cli_share_every_flag_but_the_tool_ones_and_the_environment():
    for agent in ("claude", "codex"):
        api, ui = cli_agent.PRESETS[agent], cli_browser.PRESETS[agent]
        assert cli_agent.common_flags(api) == cli_agent.common_flags(ui), agent
        removed = [a for a in api + ui if a not in cli_agent.common_flags(api)]
        assert set(removed) <= {"--tools", "Bash", "", "--allowedTools", "Bash(curl:*)", ui[ui.index("--allowedTools") + 1] if agent == "claude" else "",
                                "--mcp-config", "{mcp_config}", "--sandbox", "workspace-write", "read-only", "-c", "sandbox_workspace_write.network_access=true",
                                "--disable", "shell_tool", "unified_exec", "{mcp_overrides}"}, (agent, removed)  # fmt: skip
    assert "--safe-mode" not in cli_agent.PRESETS["claude"] + cli_browser.PRESETS["claude"]  # it would drop the UI route's MCP server
    assert "skills.include_instructions=false" in cli_agent.CODEX_COMMON
    env, _ = cli_agent.agent_environment(False, "claude")
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "CLAUDE_CODE_DISABLE_CLAUDE_MDS" not in cli_agent.agent_environment(False, ["custom"])[0] or "CLAUDE_CODE_DISABLE_CLAUDE_MDS" in os.environ


@pytest.mark.parametrize("route", ["api", "ui"])
def test_a_user_instruction_file_stops_the_run_before_the_agent_starts(tmp_path, monkeypatch, route):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("Always discharge everyone.\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    started = tmp_path / "started"
    module = cli_agent if route == "api" else cli_browser
    monkeypatch.setitem(module.PRESETS, "claude", [sys.executable, "-S", "-c", f"open({str(started)!r}, 'w')"])
    r = module.run_episode("find-result-001", "claude", isolate=False)
    assert r["terminal_reason"] == "harness_error" and "instruction files" in r["ended_by"] and not started.exists()
    assert r["instruction_files_found"] == [str(home / ".claude" / "CLAUDE.md")]


INIT_UI = {"type": "system", "subtype": "init", "model": "claude-test-1", "tools": [f"mcp__computer__{n}" for n in cli_browser.TOOL_NAMES],
           "mcp_servers": [{"name": "computer", "status": "connected"}], "plugins": [{"name": "cc-plugin-agents-md"}], "skills": [], "memory_paths": None}


def test_the_ui_route_records_the_inventory_and_stops_when_it_is_not_the_controlled_one(tmp_path, monkeypatch, hb_calls):
    done = [{"type": "result", "subtype": "success", "result": "Hb 109 g/L"}]
    monkeypatch.setitem(cli_browser.PRESETS, "claude", stand_in(tmp_path, hb_calls, events_first=[INIT_UI], events_last=done))
    r = cli_browser.run_episode("find-result-001", "claude", isolate=False)
    assert r["success"] and r["agent_inventory"]["tools"] == INIT_UI["tools"] and r["agent_inventory"]["plugins"] == INIT_UI["plugins"]
    assert r["agent_inventory"]["reported_by"].startswith("Claude Code's init event") and r["agent_config"]["model"]["observed"] == "claude-test-1"
    extra = {**INIT_UI, "tools": INIT_UI["tools"] + ["Bash"], "memory_paths": {"auto": "/x"}}
    monkeypatch.setitem(cli_browser.PRESETS, "claude", stand_in(tmp_path, hb_calls, final="Hb 109 g/L", events_first=[extra], sleep_after=30))
    started = time.monotonic()
    r = cli_browser.run_episode("find-result-001", "claude", isolate=False)
    assert r["terminal_reason"] == "harness_error" and "controlled configuration" in r["ended_by"] and "Bash" in r["ended_by"] and "memory" in r["ended_by"]
    assert time.monotonic() - started < 20


def api_stand_in(tmp_path, events):
    script = tmp_path / "api_agent.py"
    head = "".join(f"print({json.dumps(json.dumps(e))}, flush=True)\n" for e in events)
    script.write_text(head + API_STAND_IN.replace("print('Hb 109 g/L', end='')\n", "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'Hb 109 g/L'}))\n"))
    return [sys.executable, "-S", str(script), "{prompt}"]


def test_the_api_route_checks_the_same_inventory(tmp_path, monkeypatch):
    init = {"type": "system", "subtype": "init", "model": "claude-test-1", "tools": ["Bash"], "mcp_servers": [], "skills": [], "memory_paths": None}
    monkeypatch.setitem(cli_agent.PRESETS, "claude", api_stand_in(tmp_path, [init]))
    r = cli_agent.run_episode("find-result-001", "claude", isolate=False)
    assert r["success"] and r["agent_inventory"]["tools"] == ["Bash"] and r["agent_config"]["controlled"]["environment"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    monkeypatch.setitem(cli_agent.PRESETS, "claude", api_stand_in(tmp_path, [{**init, "tools": ["Bash", "Read"]}]))
    r = cli_agent.run_episode("find-result-001", "claude", isolate=False)
    assert r["terminal_reason"] == "harness_error" and "Read" in r["ended_by"]


def test_codex_inventory_is_its_own_rendering_of_the_run_flags(tmp_path):
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir()
    rendered = [{"type": "additional_tools", "tools": [{"type": "namespace", "name": "functions", "tools": [
                    {"name": "exec", "description": "### `apply_patch`\n### `mcp__computer__screenshot`\n"}, {"name": "request_user_input"}]}]},
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Codex.\nmore"}]},
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<multi_agent_mode>Do not spawn sub-agents unless applicable AGENTS.md say so"}]},
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<tools>\nDeferred tool namespaces:\n- mcp__computer\n- mcp__x: what x is\n</tools>"}]}]  # fmt: skip

    def render(items):
        fake.write_text(f"#!{sys.executable}\nimport json, os, sys\njson.dump({{'argv': sys.argv[1:], 'home': os.listdir(os.environ['CODEX_HOME'])}}, open({str(tmp_path / 'seen.json')!r}, 'w'))\n"
                        f"print({json.dumps(json.dumps(items))})\n")  # fmt: skip
        fake.chmod(0o755)
        argv = [str(fake), *cli_agent.CODEX_COMMON, "-m", "gpt-test", "--sandbox", "read-only", "-o", "/tmp/answer", "PROMPT"]
        return cli_agent.codex_preflight(argv, tmp_path, dict(os.environ))

    out = render(rendered)
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["argv"][:2] == ["debug", "prompt-input"] and seen["home"] == [] and "PROMPT" not in seen["argv"] and "exec" not in seen["argv"]
    assert 'model="gpt-test"' in seen["argv"] and 'sandbox_mode="read-only"' in seen["argv"] and "--ignore-user-config" not in seen["argv"]
    assert out["tools"] == ["functions.exec", "functions.request_user_input"] and out["exec_nested_tools"] == ["apply_patch", "mcp__computer__screenshot"]
    assert out["instruction_messages"][0] == "developer: You are Codex." and out["deferred"] == ["mcp__computer", "mcp__x"] and not out["agents_md_loaded"]
    # an AGENTS.md Codex would read shows in its rendering, and stops the run
    agents_md = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /tmp\n\n<INSTRUCTIONS>Be brief"}]}
    out = render([*rendered, agents_md])
    assert out["agents_md_loaded"]
    from eprbench.episode import Episode
    from eprbench.tasks import load_task

    with pytest.raises(RuntimeError, match="AGENTS.md"):
        cli_agent.refuse_preflight(Episode(load_task("find-result-001"), harness="test", interface="api"), out)


# --- 5. a plan stops when the agent product drifts -------------------------------------------------------------------


def test_an_agent_plan_stops_on_drift_and_goes_on_only_with_a_recorded_reason(tmp_path, monkeypatch, capsys):
    counter = tmp_path / "count"
    init = {"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": [], "skills": [], "memory_paths": None}
    script = tmp_path / "api_agent.py"
    script.write_text(  # the model it reports changes after the first episode, as after an upgrade behind a default
        "import json, os, sys\n"
        f"n = int(open({str(counter)!r}).read()) if os.path.exists({str(counter)!r}) else 0\n"
        f"open({str(counter)!r}, 'w').write(str(n + 1))\n"
        f"print(json.dumps({{**{init!r}, 'model': 'm1' if n == 0 else 'm2'}}), flush=True)\n"
        + API_STAND_IN.replace("import json, re, sys, urllib.request\n", "import re, urllib.request\n")
        .replace("print('Hb 109 g/L', end='')\n", "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'Hb 109 g/L'}))\n")
    )  # fmt: skip
    monkeypatch.setitem(cli_agent.PRESETS, "claude", [sys.executable, "-S", str(script), "{prompt}"])
    plan = tmp_path / "agents.json"
    run_cli(monkeypatch, "plan", "find-result-001", "--agents", "claude", "--interfaces", "api", "--repeats", "3", "--seed", "1", "--out", str(plan))
    capsys.readouterr()
    out = str(tmp_path / "ev")
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(plan), "--out-dir", out, "--allow-unsandboxed")
    err = capsys.readouterr().err
    assert e.value.code == 3 and "more than one model" in err and "--accept-agent-drift" in err
    assert json.loads(err.splitlines()[-2])["plan"]["ran"] == 2
    with pytest.raises(SystemExit) as e:  # resuming meets the same drift in the evidence and runs nothing
        run_cli(monkeypatch, "run", "--plan", str(plan), "--out-dir", out, "--allow-unsandboxed")
    assert e.value.code == 3 and json.loads(capsys.readouterr().err.splitlines()[-2])["plan"]["ran"] == 0
    run_cli(monkeypatch, "run", "--plan", str(plan), "--out-dir", out, "--allow-unsandboxed", "--accept-agent-drift", "CLI updated its default model")
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1 and lines[0]["agent_drift_accepted"]["reason"] == "CLI updated its default model" and lines[0]["agent_drift_accepted"]["drift"]
    # a CLI whose version is no longer the plan's stops it before anything runs
    monkeypatch.setattr(cli_agent, "cli_version", lambda exe: "claude 99.0")
    assert any("the claude CLI is now 'claude 99.0'" in d for d in runner.agent_drift(json.loads(plan.read_text()), []))


# --- 6. the final chance keeps a reply being written, and counts screenshots apart ------------------------------------


def test_the_final_chance_keeps_a_reply_being_filled_in():
    from eprbench.tasks import load_task

    with claude_browser.serve_ui(load_task("find-result-001")) as (url, _app), sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=claude_browser.VIEWPORT)
        page.goto(url + "/reply")
        page.fill("#reply-text", "Hb 109 g/L, half written")
        with claude_browser.reply_page_only(page, url):
            assert page.input_value("#reply-text") == "Hb 109 g/L, half written"
        page.goto(url + "/")
        with claude_browser.reply_page_only(page, url):  # from anywhere else, it goes to the reply page
            assert page.url.endswith("/reply")
        browser.close()


def test_the_sdk_browser_harness_counts_final_chance_screenshots_apart():
    from tests.test_agents import ScriptedClient, reply, text, tool_use

    client = ScriptedClient(
        reply(tool_use("left_click", {"coordinate": [5, 5]}, "a")),
        reply(tool_use("left_click", {"coordinate": [5, 5]}, "b")),  # over the limit: the final chance follows
        reply(tool_use("screenshot", {}, "c"), tool_use("zoom", {"region": [0, 0, 300, 200]}, "d"), tool_use("screenshot", {}, "e")),
        reply(text("Inferior STEMI."), stop_reason="end_turn"),
    )
    r = claude_browser.run_episode(STEMI, client, limits=Limits(max_actions=1))
    assert r["terminal_reason"] == "action_limit" and r["final_chance"]["screenshots"] == 3 and r["final_chance"]["actions"] == 0


# --- 7. the bridge never leaves a worker waiting ----------------------------------------------------------------------


def connected(tmp_path):
    path = cli_browser._private_dir() + "/s"
    bridge = cli_browser.Bridge(path)
    client = socket.socket(socket.AF_UNIX)
    client.connect(path)
    return bridge, client


def test_closing_the_bridge_releases_a_worker_whose_message_was_taken_but_never_answered(tmp_path):
    bridge, client = connected(tmp_path)
    client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    kind, _, box = bridge.inbox.get(timeout=5)  # the harness took it, then failed before answering
    assert kind == "message" and len(bridge.alive()) == 1
    bridge.close(join=3)
    assert bridge.alive() == [] and not bridge.waiting
    client.close()


def test_a_peer_that_goes_away_leaves_no_worker_behind(tmp_path):
    bridge, client = connected(tmp_path)
    client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    _, _, box = bridge.inbox.get(timeout=5)
    client.close()
    box.put((b'{"jsonrpc": "2.0", "id": 1, "result": {}}', None))
    deadline = time.monotonic() + 5
    while bridge.alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bridge.alive() == []
    bridge.close(join=1)


def test_a_failing_handler_ends_the_episode_as_a_harness_error_with_its_evidence(tmp_path, monkeypatch, hb_calls):
    real = cli_browser.Computer.handle

    def fails(self, raw):
        if b"tools/call" in raw:
            raise RuntimeError("boom in the handler")
        return real(self, raw)

    monkeypatch.setattr(cli_browser.Computer, "handle", fails)
    started = time.monotonic()
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_calls, final="Hb 109 g/L", sleep_after=60), isolate=False, out_dir=tmp_path / "out")
    assert r["terminal_reason"] == "harness_error" and "boom in the handler" in r["error"] and time.monotonic() - started < 20
    events = [json.loads(x) for x in (Path(r["evidence_dir"]) / "transcript.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "error" and "boom" in e["error"] for e in events)


# --- 8. redaction never touches an image ---------------------------------------------------------------------------


KEYLIKE = "AIza" + "A" * 35  # a credential pattern that can occur by chance inside base64
FAKE_KEY = "sk" + "-" + "abcdefghij" * 2  # built here, so the release scan does not take the test for a leaked key


@pytest.mark.parametrize("event", [
    {"type": "item.completed", "item": {"type": "mcp_tool_call", "result": {"content": [{"type": "image", "data": "iVBOR+" + KEYLIKE + "==", "mimeType": "image/png"}]}}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR/" + KEYLIKE}}]}]}},
])  # fmt: skip
def test_a_screenshot_in_an_event_stream_is_never_redacted(event):
    line = json.dumps(event) + "\n"
    assert redact_output(line) != line  # what the old whole-line redaction did to it
    assert redact_stream_line(line) == line  # byte for byte


def test_text_in_an_event_stream_is_still_redacted():
    line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "my key is " + FAKE_KEY}]},
                       "image": {"type": "image", "data": KEYLIKE}}) + "\n"  # fmt: skip
    out = json.loads(redact_stream_line(line))
    assert FAKE_KEY not in out["message"]["content"][0]["text"] and out["image"]["data"] == KEYLIKE
    assert redact_stream_line(f"plain {FAKE_KEY}\n") == "plain [REDACTED]\n"
