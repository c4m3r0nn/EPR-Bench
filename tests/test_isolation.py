"""The agent-CLI harness: its limits, its evidence, and keeping the agent away from the ground truth.

The "agents" here are small Python scripts run with -S (no site-packages), standing in for Claude Code or Codex.
"""

import json
import sys

import pytest

from eprbench import isolation
from eprbench.agents import cli_agent
from eprbench.episode import Limits
from eprbench.tasks import CANARY, list_tasks, load_task, task_file

HEADER = (
    "import json, os, re, subprocess, sys, time, urllib.error, urllib.request\n"
    "prompt = sys.argv[1]\n"
    "url = re.search(r'POST (http://\\S+)/api/ops', prompt).group(1)\n"
    "def call(name, **args):\n"
    "    req = urllib.request.Request(f'{url}/api/ops/{name}', json.dumps(args).encode(), {'content-type': 'application/json'})\n"
    "    try:\n"
    "        return json.load(urllib.request.urlopen(req))\n"
    "    except urllib.error.HTTPError as e:\n"
    "        return json.load(e)\n"
)


def agent(tmp_path, body, *args, header=HEADER):
    script = tmp_path / "agent.py"
    script.write_text(header + body)
    return [sys.executable, "-S", str(script), "{prompt}", *args]


SANDBOX, WHY_NOT = isolation.sandbox(isolation.protected_paths())
needs_sandbox = pytest.mark.skipif(not SANDBOX, reason=f"no sandbox here: {WHY_NOT}")


def test_action_limit_refuses_further_calls_and_asks_for_the_answer(tmp_path):
    body = (
        "while True:\n"
        "    out = call('get_current_time')\n"
        "    if not out['ok']:\n"
        "        break\n"
        "print('Stopped:', out['error'], '-', out['message'])\n"
    )
    report = cli_agent.run_episode("order-ctpa-001", agent(tmp_path, body), limits=Limits(max_actions=3), out_dir=tmp_path / "out", allow_unsandboxed=True)
    assert report["terminal_reason"] == "action_limit" and report["truncated"] and report["steps"] == 3
    assert report["answer"].startswith("Stopped: action_limit - You have reached the limit on actions") and report["final_chance"]["reason"] == "action_limit"
    out = tmp_path / "out" / next((tmp_path / "out").iterdir()).name
    http = [json.loads(line) for line in (out / "http.jsonl").read_text().splitlines()]
    assert [h["status"] for h in http] == [200, 200, 200, 429] and http[0]["response"]["ok"] and http[-1]["audit_ids"] == []
    audit = [json.loads(line) for line in (out / "audit.jsonl").read_text().splitlines()]
    assert len(audit) == 3 and all(a["interface"] == "api" and a["ok"] and "now" in a["result"] for a in audit)  # the operation's own result
    assert (out / "agent.stdout").read_text().startswith("Stopped:")


def test_timeout_stops_the_agent_and_keeps_its_output(tmp_path):
    body = "print('looking things up', flush=True)\nprint('oops', file=sys.stderr, flush=True)\nsubprocess.Popen(['sleep', '60'])\ntime.sleep(60)\n"
    report = cli_agent.run_episode("order-ctpa-001", agent(tmp_path, body), limits=Limits(timeout_seconds=1.5, final_chance_seconds=0.5),
                                   out_dir=tmp_path / "out", allow_unsandboxed=True)  # fmt: skip
    assert report["terminal_reason"] == "timeout" and report["truncated"] and report["wall_seconds"] < 15
    out = tmp_path / "out" / next((tmp_path / "out").iterdir()).name
    assert (out / "agent.stdout").read_text() == "looking things up\n" and (out / "agent.stderr").read_text() == "oops\n"
    assert report["answer"] is None and report["final_chance"]["late"]  # what it printed is evidence, not an answer


def test_an_event_stream_answer_and_the_request_limit(tmp_path):
    """Claude Code's preset prints a JSON event stream; the answer is in its result event, and --max-turns is the request limit."""
    events = [{"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success", "result": "Hb 109 g/L", "num_turns": 2}]
    body = "import sys\nassert sys.argv[sys.argv.index('--max-turns') + 1] == '7'\n" + "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events)
    argv = agent(tmp_path, body, header="")[:3] + cli_agent.PRESETS["claude"][1:]
    report = cli_agent.run_episode("find-result-001", argv, limits=Limits(max_requests=7), isolate=False)
    assert report["terminal_reason"] == "submitted" and report["answer"] == "Hb 109 g/L" and report["success"]
    assert report["agent_result"]["num_turns"] == 2

    events[-1] = {"type": "result", "subtype": "error_max_turns", "num_turns": 7}
    body = "import sys\n" + "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events) + "sys.exit(1)\n"
    report = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), isolate=False)
    assert report["terminal_reason"] == "model_request_limit" and report["truncated"]


def test_presets_restrict_the_agent():
    claude = cli_agent.PRESETS["claude"]
    assert claude[claude.index("--tools") + 1] == "Bash" and claude[claude.index("--allowedTools") + 1] == "Bash(curl:*)"
    assert "--strict-mcp-config" in claude and claude[claude.index("--setting-sources") + 1] == "project"
    # under our sandbox Codex must not try to start its own, which macOS would refuse
    assert "workspace-write" in cli_agent.PRESETS["codex"] and "danger-full-access" in cli_agent.preset_argv("codex", True, None, None)
    assert "workspace-write" in cli_agent.preset_argv("codex", False, None, None)


ATTACK = (
    "attempts = {}\n"
    "for label, path in (('task', sys.argv[2]), ('canary', sys.argv[3])):\n"
    "    try:\n"
    "        attempts[label + ' open'] = open(path).read()\n"
    "    except OSError as e:\n"
    "        attempts[label + ' open'] = 'BLOCKED ' + type(e).__name__\n"
    "    print('$ curl -s file://' + path, file=sys.stderr)  # an agent CLI's event stream shows the commands it ran\n"
    "    done = subprocess.run(['curl', '-s', 'file://' + path], capture_output=True, text=True)\n"
    "    attempts[label + ' curl'] = done.stdout or 'BLOCKED curl exit %d' % done.returncode\n"
    "listing = subprocess.run(['ls', os.path.dirname(sys.argv[2])], capture_output=True, text=True)\n"
    "attempts['listing'] = listing.stdout or 'BLOCKED'\n"
    "print(json.dumps(attempts))\n"
)


@pytest.fixture
def canary(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "grader.txt").write_text(f"{CANARY}\nexpected answer: 109 g/L\n")
    return secret


@needs_sandbox
def test_the_sandbox_prevents_reading_the_ground_truth(tmp_path, canary):
    argv = agent(tmp_path, ATTACK, str(task_file("find-result-001")), str(canary / "grader.txt"))
    report = cli_agent.run_episode("find-result-001", argv, protect=[canary])
    attempts = json.loads(report["answer"])
    assert all(v.startswith("BLOCKED") for v in attempts.values()), attempts
    iso = report["isolation"]
    assert iso["sandbox"] and str(canary.resolve()) in iso["protected"] and iso["ground_truth_seen"] == []
    # the attempts were blocked, so they are recorded but the episode is not contaminated
    assert not report["contaminated"] and "file://" in iso["protected_paths_mentioned"]


@needs_sandbox
def test_the_sandboxed_agent_can_still_do_the_task(tmp_path):
    body = "out = call('search_patients', query='H4417302')\nprint('found', out['result']['patients'][0]['mrn'])\n"
    report = cli_agent.run_episode("find-result-001", agent(tmp_path, body))
    assert report["isolation"]["sandbox"] and report["terminal_reason"] == "submitted" and report["answer"] == "found H4417302"


def test_without_the_sandbox_reading_the_ground_truth_is_detected(tmp_path, canary):
    argv = agent(tmp_path, ATTACK, str(task_file("find-result-001")), str(canary / "grader.txt"))
    report = cli_agent.run_episode("find-result-001", argv, isolate=False, protect=[canary])
    iso = report["isolation"]
    assert not iso["sandbox"] and iso["why_not"] == "isolation switched off" and report["contaminated"]
    seen = " ".join(iso["ground_truth_seen"])
    assert "canary" in seen and "find-result-001 check id: haemoglobin_value" in iso["suspected"]  # the task file was printed


@pytest.mark.parametrize("task_id", ["order-ue-001", "find-result-001", "rx-allergy-trap-001"])
def test_an_agent_that_reads_a_legacy_task_file_is_caught_by_its_canary(tmp_path, task_id):
    """Release checklist: every task file carries the canary, the legacy ones too, so reading any of them is seen."""
    body = "print(open(sys.argv[2]).read()[:4000])\n"
    report = cli_agent.run_episode(task_id, agent(tmp_path, body, str(task_file(task_id))), isolate=False)
    assert report["contaminated"] and any("canary" in s for s in report["isolation"]["ground_truth_seen"]), report["isolation"]


def test_every_task_and_bundle_file_carries_the_canary():
    from eprbench.loader import data_file

    for task_id in list_tasks():
        sql = next(c.sql for c in load_task(task_id).checks if c.sql)
        assert sql.startswith(f"/* {CANARY} */ "), task_id  # in the first check with SQL, as a comment that changes nothing
    for bundle in ("demo", "preview"):
        assert json.loads(data_file("bundles", bundle).read_text())["source"].startswith(CANARY), bundle


def test_scan_finds_fingerprints_in_an_event_stream():
    leaked = json.dumps({"type": "tool_result", "content": task_file("order-ctpa-001").read_text()})
    found = isolation.scan(leaked, ["/nowhere"])
    assert any(s.startswith("order-ctpa-001 check") for s in found["content"])
    assert isolation.scan("I requested the CTPA via curl http://127.0.0.1:8000/api/ops/place_order", ["/nowhere"]) == {"content": [], "suspected": [], "paths": []}


def test_the_agent_cannot_reach_the_harness_control_plane(tmp_path):
    body = (
        "for path in ('/harness/reset', '/harness/grade'):\n"
        "    try:\n"
        "        urllib.request.urlopen(urllib.request.Request(url + path, b'{}', {'x-harness-token': 'guess'}))\n"
        "        print(path, 'reachable')\n"
        "    except urllib.error.HTTPError as e:\n"
        "        print(path, e.code)\n"
    )
    report = cli_agent.run_episode("find-result-001", agent(tmp_path, body), allow_unsandboxed=True)  # sandboxed wherever it can be
    assert report["answer"] == "/harness/reset 404\n/harness/grade 404"


def test_an_event_stream_without_a_result_is_not_an_answer():
    stream = '{"type": "item.completed", "item": {"text": "Hb 109 g/L on 13 March"}}\n'
    assert cli_agent._answer(stream) == ("", None)
    assert cli_agent._answer("Hb 109 g/L\n") == ("Hb 109 g/L\n", None)
