"""The agent-CLI browser route (eprbench.agents.cli_browser), with a stand-in for the CLI and a real Chromium page.

The stand-in is a small Python program run with -S, as the agent CLI would be: it reads its MCP configuration from the
command line (as Claude Code's --mcp-config), starts the MCP server it names (the harness's relay), and plays a scripted
list of tool calls through it, printing its final message. The script is planned on a twin of the episode's world
(Twin): the same task served and driven by the same executor, so every click lands where it would on the episode's page.
No real agent CLI is run here.
"""

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

from eprbench import isolation
from eprbench.agents import cli_browser
from eprbench.agents.claude_browser import VIEWPORT, Screen, serve_ui
from eprbench.episode import Limits, manifest_problems
from eprbench.tasks import CANARY, load_task, task_file

sync_api = pytest.importorskip("playwright.sync_api")

SANDBOX, WHY_NOT = isolation.sandbox(isolation.protected_paths())
needs_sandbox = pytest.mark.skipif(not SANDBOX, reason=f"no sandbox here: {WHY_NOT}")

# The stand-in CLI: argv is {prompt} {mcp_config} SCRIPT [OUT]. SCRIPT is a JSON file: {"calls": [[name, args], ...],
# "final": text, "attack": {...}}; OUT (optional) receives every tool result as JSON lines.
STAND_IN = r'''
import json, os, subprocess, sys, urllib.error, urllib.request
prompt, config, plan = sys.argv[1], json.loads(sys.argv[2]), json.load(open(sys.argv[3]))
log = open(sys.argv[4], "w") if len(sys.argv) > 4 else None
(name, server), = config["mcpServers"].items()
mcp = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
n = 0
def rpc(method, params=None, notify=False):
    global n
    msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
    if not notify:
        n += 1
        msg["id"] = n
    mcp.stdin.write((json.dumps(msg) + "\n").encode())
    mcp.stdin.flush()
    if notify:
        return None
    line = mcp.stdout.readline()
    if not line:  # the harness has gone
        sys.exit(3)
    reply = json.loads(line)
    assert reply["id"] == n, reply
    return reply
for event in plan.get("events_first", []):  # what a CLI prints before it works (Claude Code's init event)
    print(json.dumps(event), flush=True)
init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "stand-in", "version": "0"}})
rpc("notifications/initialized", notify=True)
tools = rpc("tools/list")["result"]["tools"]
if log:
    log.write(json.dumps({"init": init["result"], "tools": [t["name"] for t in tools], "prompt": prompt}) + "\n")
attack = plan.get("attack") or {}
for path in attack.get("read", []):
    try:
        print("read:", open(path).read()[:4000], file=sys.stderr)
    except OSError as e:
        print("read blocked:", type(e).__name__, file=sys.stderr)
if attack.get("url_file"):
    url = open(attack["url_file"]).read().strip()
    for path in ("/", "/reply", "/patients/1"):
        try:
            body = urllib.request.urlopen(url + path).read().decode()
            print("server:", path, "200", body[:200], file=sys.stderr)
        except urllib.error.HTTPError as e:
            print("server:", path, e.code, file=sys.stderr)
for i, (call, args) in enumerate(plan["calls"]):
    __import__("time").sleep(plan.get("pause_before", {}).get(str(i), 0))  # e.g. until a limit has passed
    reply = rpc("tools/call", {"name": call, "arguments": args})
    if log:
        result = reply.get("result") or {}
        log.write(json.dumps({"call": call, "isError": result.get("isError"), "content": [c if c["type"] == "text" else {"type": "image", "sha256": __import__("hashlib").sha256(__import__("base64").b64decode(c["data"])).hexdigest()} for c in result.get("content", [])]}) + "\n")
        log.flush()
print(plan.get("final", ""), end="", flush=True)
for event in plan.get("events_last", []):  # Claude Code's result event, which carries its final message
    print(json.dumps(event), flush=True)
__import__("time").sleep(plan.get("sleep_after", 0))
'''


def stand_in(tmp_path, calls, final="", attack=None, log=True, **more):
    script, plan = tmp_path / "standin.py", tmp_path / "plan.json"
    script.write_text(STAND_IN)
    plan.write_text(json.dumps({"calls": calls, "final": final, "attack": attack or {}, **more}))
    return [sys.executable, "-S", str(script), "{prompt}", "{mcp_config}", str(plan), *([str(tmp_path / "calls.jsonl")] if log else [])]


def results(tmp_path) -> list[dict]:
    return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]


class Twin:
    """Plans a scripted agent's tool calls on a twin of the episode's world: the same task, served and driven by the
    same executor, at the same viewport. Each step is carried out on the twin and recorded as the call to replay."""

    def __init__(self, task_id):
        self.task = load_task(task_id)
        self._serve = serve_ui(self.task)
        self.url, self.app = self._serve.__enter__()
        self._pcm = sync_api.sync_playwright()
        self._p = self._pcm.__enter__()
        try:
            self.browser = self._p.chromium.launch()
        except Exception as e:  # browser binary not installed
            self.close()
            pytest.skip(f"chromium unavailable: {e}")
        self.page = self.browser.new_page(viewport=VIEWPORT)
        self.page.goto(self.url)
        self.screen, self.calls = Screen(self.page), []

    def do(self, name, args=None):
        self.screen.run(name, args or {})
        self.calls.append([name, args or {}])
        return self

    def click(self, selector):
        target = self.page.locator(selector).first
        for _ in range(40):  # scroll it on screen with the wheel, as an agent would
            box = target.bounding_box()
            if box["y"] < 0:
                self.do("scroll", {"coordinate": [640, 400], "scroll_direction": "up", "scroll_amount": 2})
            elif box["y"] + box["height"] > VIEWPORT["height"]:
                self.do("scroll", {"coordinate": [640, 400], "scroll_direction": "down", "scroll_amount": 2})
            else:
                break
        box = target.bounding_box()
        return self.do("left_click", {"coordinate": [round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2)]})

    def type(self, selector, text):
        return self.click(selector).do("type", {"text": text})

    def shot(self):
        return self.do("screenshot")

    def close(self):
        if getattr(self, "browser", None):
            self.browser.close()
        self._pcm.__exit__(None, None, None)
        self._serve.__exit__(None, None, None)


def find_hb(twin):
    """find-result-001: the FBC in ICE, looked at."""
    twin.shot().click('a.applink[href="/ice"]').type("#ice-search", "H4417302").do("key", {"text": "Return"}).click("#search-results a")
    return twin.click("nav.tabs a:text-is('Results')").click("#results a:text-is('Full blood count')").shot()


@pytest.fixture
def hb_calls():
    twin = Twin("find-result-001")
    try:
        return find_hb(twin).calls
    finally:
        twin.close()  # before the episode starts its own browser


def only_dir(root):
    (d,) = [p for p in Path(root).iterdir() if p.is_dir()]
    return d


def test_a_legacy_task_through_the_mcp_tools_with_its_plain_text_answer(tmp_path, hb_calls):
    argv = stand_in(tmp_path, hb_calls, final="Hb 109 g/L on the latest FBC.")
    r = cli_browser.run_episode("find-result-001", argv, isolate=False, out_dir=tmp_path / "out")
    assert r["terminal_reason"] == "submitted" and r["success"] and r["answer"] == "Hb 109 g/L on the latest FBC." and r["interface"] == "ui"
    assert r["harness"] == "cli_browser" and r["tool_calls"] == len(hb_calls) and r["screenshot_actions"] == 2 and r["steps"] == len(hb_calls) - 2
    assert not r["contaminated"] and not r["bypass_attempted"] and r["isolation"]["server_requests_refused"] == []
    first = results(tmp_path)[0]
    assert first["tools"] == cli_browser.TOOL_NAMES and first["init"]["serverInfo"]["name"] == "computer"
    # the prompt: the same core and task as the API route; the note says only how to interact
    assert first["prompt"].startswith(cli_browser.SYSTEM_PROMPT) and first["prompt"].endswith("Task: " + load_task("find-result-001").instruction)
    assert "curl" not in first["prompt"] and "http" not in first["prompt"]


def test_the_evidence_record_is_complete(tmp_path, hb_calls):
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_calls, final="Hb 109 g/L"), isolate=False, out_dir=tmp_path / "out")
    d = Path(r["evidence_dir"])
    assert manifest_problems(d) == [] and {"agent.stdout", "agent.stderr", "http.jsonl", "reads.jsonl", "audit.jsonl", "final.sqlite"} <= {p.name for p in d.iterdir()}
    events = [json.loads(x) for x in (d / "transcript.jsonl").read_text().splitlines()]
    mcp = [e for e in events if e["kind"] == "mcp"]
    assert [e["request"]["method"] for e in mcp[:3]] == ["initialize", "notifications/initialized", "tools/list"]
    calls = [e for e in mcp if e["request"].get("method") == "tools/call"]
    assert len(calls) == len(hb_calls)
    # every screenshot returned to the agent is saved once, under its hash, and is the image the agent received
    sent = [c for c in calls if c["request"]["params"]["name"] == "screenshot"]
    got = [c for c in results(tmp_path)[1:] if c["call"] == "screenshot"]
    for e, g in zip(sent, got):
        image = e["reply"]["result"]["content"][0]["source"]
        assert hashlib.sha256((d / image["screenshot"]).read_bytes()).hexdigest() == image["sha256"] == g["content"][0]["sha256"]
    actions = [e for e in events if e["kind"] == "action"]
    assert len(actions) == len(hb_calls) and all(e["interface_call"] == "mcp" for e in actions)
    # the harness's own browser loaded every page, carrying the cookie, which the record keeps by name only
    http = [json.loads(x) for x in (d / "http.jsonl").read_text().splitlines()]
    assert http and all(h["status"] in (200, 303) for h in http) and all("[REDACTED" in h["headers"]["cookie"] for h in http)
    meta = json.loads((d / "episode.json").read_text())
    assert meta["harness"] == "cli_browser" and meta["interface"] == "ui"
    assert "computer" in r["agent_config"]["tools"] and r["limits_enforced"]["max_screenshots"] and r["comparability"]["action_unit"].startswith("one mouse")


def test_reads_count_only_from_screenshots_delivered_to_the_agent(tmp_path, hb_calls):
    from tests.test_typed_answers import RESULT, TASK  # the same FBC result in the same world

    assert TASK.bundle == load_task("find-result-001").bundle
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_calls, final="Hb 109 g/L"), isolate=False, out_dir=tmp_path / "a")
    rows = [json.loads(x) for x in (Path(r["evidence_dir"]) / "reads.jsonl").read_text().splitlines()]
    assert ("result", RESULT) in {(x["kind"], x["object_id"]) for x in rows if x["via"] == "screenshot"} and not [x for x in rows if x["via"] == "returned"]
    # the same pages opened without the last screenshot: the result was rendered, never seen
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_calls[:-1], final="Hb 109 g/L"), isolate=False, out_dir=tmp_path / "b")
    rows = [json.loads(x) for x in (Path(r["evidence_dir"]) / "reads.jsonl").read_text().splitlines()]
    assert ("result", RESULT) not in {(x["kind"], x["object_id"]) for x in rows if x["via"] == "screenshot"}
    assert ("result", RESULT) in {(x["kind"], x["object_id"]) for x in rows if x["via"] == "rendered"}


def test_the_action_limit_and_the_final_chance_without_a_form(tmp_path, hb_calls):
    clicks = [c for c in hb_calls if c[0] != "screenshot"]
    argv = stand_in(tmp_path, clicks[:4] + [["screenshot", {}], ["left_click", {"coordinate": [10, 10]}], ["left_click", {"coordinate": [10, 10]}]],
                    final="Hb 109 g/L")  # fmt: skip
    r = cli_browser.run_episode("find-result-001", argv, isolate=False, limits=Limits(max_actions=3))
    assert r["terminal_reason"] == "action_limit" and r["truncated"] and r["steps"] == 3 and r["final_chance"]["answer"] == "Hb 109 g/L"
    out = results(tmp_path)[1:]
    assert [o["isError"] for o in out[:3]] == [False] * 3
    assert "limit on actions" in out[3]["content"][0]["text"] and out[3]["content"][0]["text"].startswith("Not executed")  # the request for the answer
    assert all(o["isError"] and o["content"][0]["text"].startswith("Not executed") for o in out[4:])
    assert r["final_chance"]["requested"] and not r["final_chance"]["late"]


def test_screenshots_are_capped_apart_from_actions(tmp_path):
    argv = stand_in(tmp_path, [["screenshot", {}], ["zoom", {"region": [0, 0, 200, 100]}], ["screenshot", {}]], final="Hb 109 g/L")
    r = cli_browser.run_episode("find-result-001", argv, isolate=False, limits=Limits(max_screenshots=2))
    assert r["terminal_reason"] == "action_limit" and r["limit_reached"] == "max_screenshots" and r["steps"] == 0 and r["screenshot_actions"] == 2


STEMI = "preview-stemi-judgement"
DX = {"text": "Inferior STEMI: PPCI activated, ticagrelor loaded.", "fields": {"diagnosis": "Inferior ST-elevation myocardial infarction", "plan": "PPCI"}}


def stemi_calls(answer=True):
    """preview-stemi-judgement through the UI: ECG requested in ICE, five minutes waited, the ECG read, PPCI activated, ticagrelor
    loaded, and (with `answer`) the typed answer given on the reply page in its two steps."""
    twin = Twin(STEMI)
    try:
        twin.shot().click('a.applink[href="/ice"]').type("#ice-search", "H5120986").do("key", {"text": "Return"}).click("#search-results a")
        twin.click("#request-categories a:text-is('Cardiology')").click("#request-exams a:text-is('12-lead ECG')")
        twin.click("#order-priority input[value=urgent]").type("#order-clinical_details", "Chest pain, ?ACS").click("#order-form button")
        twin.click("#wait-form button[value='5']")  # five minutes, from the header
        twin.click("nav.tabs a:text-is('Results')").click("#results a").shot()
        twin.click('a.applink[href="/"]').type("#patient-search", "H5120986").do("key", {"text": "Return"}).click("#search-results a")
        twin.click("#patient-menu a:text-is('Referrals')").click("#referral-pathways a:text-is('Primary PCI activation')").click("#pathway-ecg_stemi input[value=yes]")
        twin.type("#pathway-symptom_onset", "2026-03-14 08:25").type("#pathway-reason", "Inferior STEMI, II III aVF").click("#referral-pathway-form button")
        twin.click("#patient-menu a:text-is('Medications')").type("#rx-search", "ticagrelor").do("key", {"text": "Return"}).click("#rx-drugs a:text-is('Ticagrelor')")
        twin.click("#rx-products a:has-text('90mg')").click("#med-route input[value=PO]").type("#med-dose", "180").click("#med-unit input[value=mg]")
        twin.click("#med-frequency input[value=once]").click("#med-kind input[value=stat]").click("#prescribe-form button").shot()
        if answer:
            twin.click("#reply-link").click(f'input[name="field_diagnosis"][value="{DX["fields"]["diagnosis"]}"]').type("#answer-plan-text", "PPCI")
            twin.type("#reply-text", DX["text"]).click("#reply-form button").shot().click("#reply-confirm button")
            assert twin.app.state.submission is not None
        return twin.calls
    finally:
        twin.close()


def centre(page, selector):
    box = page.locator(selector).first.bounding_box()
    return [round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2)]


def test_a_preview_task_with_a_typed_answer_through_the_reply_page(tmp_path):
    from tests.test_preview_cases import BJ_BONUS, expect, full

    calls = stemi_calls()
    argv = stand_in(tmp_path, calls + [["screenshot", {}]], final="Done.")
    r = cli_browser.run_episode(STEMI, argv, isolate=False, out_dir=tmp_path / "out")
    assert r["terminal_reason"] == "submitted" and r["ended_by"] == "reply page" and r["answer"] == DX and r["closing_message"] == "Done."
    assert full(r) == expect(STEMI, "complete", bonuses=BJ_BONUS)
    # the call after the answer is refused: the task is over
    assert results(tmp_path)[-1]["isError"] and "submitted" in results(tmp_path)[-1]["content"][0]["text"]
    events = [json.loads(x) for x in (Path(r["evidence_dir"]) / "transcript.jsonl").read_text().splitlines()]
    (sub,) = [e for e in events if e["kind"] == "submission"]
    assert sub["accepted"] and sub["interface"] == "ui"


def test_after_a_limit_the_typed_answer_can_still_be_given_on_the_reply_page(tmp_path):
    calls = stemi_calls(answer=False)
    n = sum(1 for c in calls if c[0] not in ("screenshot", "zoom"))
    # the reply page, as the final chance shows it: the agent fills it in there
    twin = Twin(STEMI)
    try:
        for name, args in calls:
            twin.do(name, args)
        twin.calls = []
        twin.page.goto(twin.url + "/reply")
        twin.shot().click(f'input[name="field_diagnosis"][value="{DX["fields"]["diagnosis"]}"]').type("#reply-text", DX["text"]).shot()
        twin.click("#reply-form button").shot().do("zoom", {"region": [0, 0, 400, 300]}).click("#reply-confirm button")
        reply_calls = twin.calls
    finally:
        twin.close()
    argv = stand_in(tmp_path, calls + [["left_click", {"coordinate": [5, 5]}]] + reply_calls, final="Answer given.")
    r = cli_browser.run_episode(STEMI, argv, isolate=False, limits=Limits(max_actions=n))
    assert r["terminal_reason"] == "action_limit" and r["final_chance"]["answer"]["fields"]["diagnosis"] == DX["fields"]["diagnosis"]
    # review 2026-11-21 (6): screenshots and zooms in the final chance are counted apart, not against its 40 actions
    shots = sum(1 for c in reply_calls if c[0] in ("screenshot", "zoom"))
    assert shots == 4 and r["final_chance"]["screenshots"] == shots and r["final_chance"]["actions"] == len(reply_calls) - shots
    assert r["final_chance"]["offered"] == "the reply page only" and not r["final_chance"]["late"]
    note = results(tmp_path)[len(calls) + 1]
    assert "Handover / reply page" in note["content"][0]["text"] and note["content"][1]["type"] == "image"


def test_the_agent_cannot_reach_the_epr_server_itself(tmp_path, monkeypatch, hb_calls):
    """An agent that has found the server's address (here handed it by the test) is refused: only the harness's browser holds the cookie."""
    url_file = tmp_path / "url"
    real = cli_browser.serve_task

    def serve(*a, **kw):
        cm = real(*a, **kw)

        class Wrap:
            def __enter__(self):
                url, app = cm.__enter__()
                url_file.write_text(url)
                return url, app

            def __exit__(self, *e):
                return cm.__exit__(*e)

        return Wrap()

    monkeypatch.setattr(cli_browser, "serve_task", serve)
    argv = stand_in(tmp_path, hb_calls, final="Hb 109 g/L", attack={"url_file": str(url_file)})
    r = cli_browser.run_episode("find-result-001", argv, isolate=False, out_dir=tmp_path / "out")
    assert r["bypass_attempted"] and r["isolation"]["server_requests_refused"] == ["GET /", "GET /reply", "GET /patients/1"]
    stderr = (Path(r["evidence_dir"]) / "agent.stderr").read_text()
    assert stderr.count(" 403") == 3 and "200" not in stderr
    assert r["success"] and r["steps"] == len(hb_calls) - 2  # refused requests are not actions, and the agent's own work still counts


@needs_sandbox
def test_the_sandboxed_agent_cannot_read_the_ground_truth_and_still_works(tmp_path, hb_calls):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "grader.txt").write_text(f"{CANARY}\n")
    argv = stand_in(tmp_path, hb_calls, final="Hb 109 g/L", attack={"read": [str(task_file("find-result-001")), str(secret / "grader.txt")]})
    r = cli_browser.run_episode("find-result-001", argv, protect=[secret], out_dir=tmp_path / "out")
    assert r["sandboxed"] and r["isolation"]["sandbox"] and not r["contaminated"] and r["success"]
    stderr = (Path(r["evidence_dir"]) / "agent.stderr").read_text()
    assert stderr.count("read blocked: PermissionError") == 2 and CANARY not in stderr


def test_without_the_sandbox_reading_the_ground_truth_is_detected(tmp_path, hb_calls):
    argv = stand_in(tmp_path, hb_calls, final="Hb 109 g/L", attack={"read": [str(task_file("find-result-001"))]})
    r = cli_browser.run_episode("find-result-001", argv, isolate=False)
    assert r["contaminated"] and any("canary" in s for s in r["isolation"]["ground_truth_seen"])


def test_the_presets_give_each_cli_only_the_computer_tools():
    claude = cli_browser.preset_argv("claude", sandboxed=True, model="claude-opus-5-5", effort="high")
    assert claude[claude.index("--tools") + 1] == "" and claude[claude.index("--allowedTools") + 1].split(",") == [f"mcp__computer__{n}" for n in cli_browser.TOOL_NAMES]
    assert {"--strict-mcp-config", "--mcp-config", "--disable-slash-commands"} <= set(claude) and "Bash" not in " ".join(claude)
    assert claude[claude.index("--permission-prompts") + 1] == "none" and claude[claude.index("--model") + 1] == "claude-opus-5-5"
    codex = cli_browser.preset_argv("codex", sandboxed=True, model="gpt-6", effort="high")
    for feature in ("shell_tool", "unified_exec", "view_image", "browser_use", "computer_use", "in_app_browser", "plugins", "apps", "multi_agent", "sleep_tool"):
        assert codex[codex.index(feature) - 1] == "--disable"
    assert 'web_search="disabled"' in codex and "{mcp_overrides}" in codex and codex[codex.index("--sandbox") + 1] == "danger-full-access"
    unsandboxed = cli_browser.preset_argv("codex", sandboxed=False, model=None, effort=None)
    assert unsandboxed[unsandboxed.index("--sandbox") + 1] == "read-only"
    settings = cli_browser.mcp_settings(["/usr/bin/python3", "-I", "-S", "/tmp/x/relay.py", "/tmp/x/s"])
    assert json.loads(settings["{mcp_config}"]) == {"mcpServers": {"computer": {"type": "stdio", "command": "/usr/bin/python3", "args": ["-I", "-S", "/tmp/x/relay.py", "/tmp/x/s"]}}}
    assert 'mcp_servers.computer.args=["-I", "-S", "/tmp/x/relay.py", "/tmp/x/s"]' in settings["{mcp_overrides}"]


def test_the_tools_are_exactly_the_browser_executors_actions():
    import inspect

    from eprbench.agents import claude_browser

    source = inspect.getsource(claude_browser.Screen.run)
    assert set(cli_browser.TOOL_NAMES) == {n for n in cli_browser.TOOL_NAMES if f'case "{n}"' in source}
    assert source.count('case "') == len(cli_browser.TOOL_NAMES)


def test_no_paid_client_on_this_route(monkeypatch, tmp_path, hb_calls):
    import anthropic
    import openai

    def refuse(*a, **kw):
        raise AssertionError("a paid SDK client was constructed")

    monkeypatch.setattr(anthropic, "Anthropic", refuse)
    monkeypatch.setattr(openai, "OpenAI", refuse)
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_calls, final="Hb 109 g/L"), isolate=False)
    assert r["success"] and r["cost"] is None


# --- paired plans of agent CLIs: api and ui, no money --------------------------------------------------------------


def run_cli(monkeypatch, *argv):
    from eprbench.__main__ import main

    monkeypatch.setattr(sys, "argv", ["eprbench", *argv])
    main()


def test_an_agent_plan_is_counterbalanced_and_names_no_prices_or_ledger():
    from eprbench import runner

    plan = runner.make_plan(["find-result-001", "order-ctpa-001"], ["api", "ui"], [], 2, 11, effort="high", limits=Limits(max_actions=60),
                            max_output_tokens=None, agents=[("claude", None), ("codex", "gpt-6")])  # fmt: skip
    eps = plan["episodes"]
    assert len(eps) == 2 * 2 * 2 * 2 and {e["provider"] for e in eps} == {"agent"} and {e["agent"] for e in eps} == {"claude", "codex"}
    assert {e["model"] for e in eps if e["agent"] == "codex"} == {"gpt-6"} and {e["model"] for e in eps if e["agent"] == "claude"} == {None}
    for pair in plan["pairs"]:
        mine = [e for e in eps if e["pair"] == pair["pair"]]
        assert [e["interface"] for e in mine] == pair["order"] and sorted(pair["order"]) == ["api", "ui"]
    assert "ledger" not in plan and plan["pins"]["prices_sha256"] is None and "max_output_tokens" not in eps[0] and eps[0]["effort"] == "high"
    assert set(plan["cli_versions"]) == {"claude", "codex"} and plan["metering"].startswith("none")
    with pytest.raises(ValueError, match="api or ui interface, not tool"):
        runner.make_plan(["find-result-001"], ["tool", "ui"], [], 1, 1, effort=None, limits=Limits(), max_output_tokens=None, agents=[("claude", None)])
    with pytest.raises(ValueError, match="plan them separately"):
        runner.make_plan(["find-result-001"], ["ui"], [("anthropic", "claude-haiku-5-5-20260101")], 1, 1, effort=None, limits=Limits(), max_output_tokens=None,
                         agents=[("claude", None)])  # fmt: skip
    with pytest.raises(ValueError, match="nothing to estimate"):
        runner.estimate(plan, None, 1.0)


API_STAND_IN = (
    "import json, re, sys, urllib.request\n"
    "url = re.search(r'POST (http://\\S+)/api/ops', sys.argv[1]).group(1)\n"
    "def call(name, **args):\n"
    "    req = urllib.request.Request(f'{url}/api/ops/{name}', json.dumps(args).encode(), {'content-type': 'application/json'})\n"
    "    return json.load(urllib.request.urlopen(req))['result']\n"
    "pid = call('search_patients', query='H4417302')['patients'][0]['patient_id']\n"
    "rid = [r for r in call('list_results', patient_id=pid)['results'] if r['name'] == 'Full blood count'][0]['result_id']\n"
    "call('get_result', result_id=rid, patient_id=pid)\n"
    "print('Hb 109 g/L', end='')\n"
)


def test_a_paired_agent_plan_is_written_run_and_resumed_without_any_money(monkeypatch, capsys, tmp_path, hb_calls):
    import anthropic
    import openai

    from eprbench.agents import cli_agent

    def refuse(*a, **kw):
        raise AssertionError("a paid SDK client was constructed")

    monkeypatch.setattr(anthropic, "Anthropic", refuse)
    monkeypatch.setattr(openai, "OpenAI", refuse)
    api = tmp_path / "api_agent.py"
    api.write_text(API_STAND_IN)
    # the claude preset of each route, played by its stand-in
    monkeypatch.setitem(cli_agent.PRESETS, "claude", [sys.executable, "-S", str(api), "{prompt}"])
    monkeypatch.setitem(cli_browser.PRESETS, "claude", stand_in(tmp_path, hb_calls, final="Hb 109 g/L", log=False))
    path = tmp_path / "agents-plan.json"
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "plan", "find-result-001", "--agents", "claude", "--seed", "4", "--out", str(path), "--prices", "p.json")
    assert "not metered" in capsys.readouterr().err
    run_cli(monkeypatch, "plan", "find-result-001", "--agents", "claude", "--repeats", "1", "--seed", "4", "--max-actions", "60", "--out", str(path))
    assert "2 episodes in 1 pairs" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(tmp_path / "ev"), "--ledger", "x.jsonl")
    assert "not metered" in capsys.readouterr().err
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(tmp_path / "ev"), "--allow-unsandboxed")
    out = capsys.readouterr()
    lines = [json.loads(x) for x in out.out.splitlines()]
    assert sorted(r["interface"] for r in lines) == ["api", "ui"] and all(r["success"] and r["cost"] is None for r in lines), \
        [(r["interface"], r["terminal_reason"], r["ended_by"], r["answer"]) for r in lines]
    assert {r["harness"] for r in lines} == {"cli_agent", "cli_browser"} and all(r["plan"]["sha256"] for r in lines)
    assert all(r["limits"]["max_actions"] == 60 for r in lines)
    assert json.loads(out.err.splitlines()[-1])["plan"]["ran"] == 2
    run_cli(monkeypatch, "run", "--plan", str(path), "--out-dir", str(tmp_path / "ev"), "--allow-unsandboxed")
    done = json.loads(capsys.readouterr().err.splitlines()[-1])["plan"]
    assert (done["already_complete"], done["ran"], done["left"], done["stopped"]) == (2, 0, 0, None)


def test_run_sends_an_agent_with_interface_ui_to_the_browser_route(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli_browser, "run_episode", lambda task_id, agent, **kw: seen.update(kw, agent=agent) or {"task_id": task_id})
    run_cli(monkeypatch, "run", "find-result-001", "--agent", "codex", "--interface", "ui", "--agent-model", "gpt-6", "--agent-effort", "high", "--timeout", "900")
    assert seen["agent"] == "codex" and seen["model"] == "gpt-6" and seen["effort"] == "high" and seen["limits"].timeout_seconds == 900
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "run", "find-result-001", "--agent", "codex", "--interface", "tool")
    assert "--interface api" in capsys.readouterr().err
