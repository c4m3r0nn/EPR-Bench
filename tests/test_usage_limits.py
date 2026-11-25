"""How an agent CLI's run ended, read from its terminal event only (cli_agent.Stream.ending): a subscription's usage or
rate limit, a full context window, or neither. Built against the terminal events of every saved agent-CLI episode
(tests/fixtures/cli_terminal_events.json: 99 episodes, the 19 that hit Claude's five-hour limit among them) and the
shapes Codex prints. The audit of the paired run found the old reading flagged recovered warnings and context-limit
text, missed a bare 429, insufficient_quota and string-valued errors, and could override a successful submission."""

import json
import re
import sys
from pathlib import Path

import pytest

from eprbench.agents import cli_agent, cli_browser
from tests.test_cli_browser import DX, STEMI, stand_in
from tests.test_cli_review_2026_11_21 import hb_reply_calls

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cli_terminal_events.json").read_text())["episodes"]
LIMITED = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "api_error_type": "rate_limit_error",
           "api_error": "usage_limit_reached", "result": "You've hit your 5-hour limit · resets 4pm (Europe/London)"}  # fmt: skip


def ending(events):
    stream = cli_agent.Stream()
    for e in events:
        stream.feed((json.dumps(e) if isinstance(e, dict) else e) + "\n")
    return stream.ending()


def kind(events):
    found = ending(events)
    return found and found["kind"]


@pytest.mark.parametrize("episode", FIXTURE, ids=[e["episode"] for e in FIXTURE])
def test_every_saved_episode_is_classified_from_its_terminal_event(episode):
    limited = any(e.get("type") == "result" and e.get("api_error_status") == 429 for e in episode["events"])
    assert kind(episode["events"]) == ("usage_limit" if limited else None)


def test_the_fixture_holds_the_limited_and_the_recovered_episodes():
    limited = [e for e in FIXTURE if kind(e["events"]) == "usage_limit"]
    assert len(limited) == 19 and sum(1 for e in limited if e["episode"].startswith("agents-preview/")) == 18
    warned = [e for e in FIXTURE if any((x.get("rate_limit_info") or {}).get("status") == "allowed_warning" for x in e["events"])]
    assert len(warned) == 21 and all(kind(e["events"]) is None for e in warned if e not in limited)  # warnings, then success


def warning(status):
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "rateLimitType": "five_hour", "utilization": 0.97}}


SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "Done; no rate limit or quota troubled me."}
CASES = [
    # Claude Code
    ("claude: warnings then success", [warning("allowed_warning"), warning("allowed_warning"), SUCCESS], None),
    ("claude: rejected, retried, then success", [warning("rejected"), warning("allowed"), SUCCESS], None),
    ("claude: the five-hour limit", [warning("rejected"), LIMITED], "usage_limit"),
    ("claude: a bare 429", [{"type": "result", "is_error": True, "api_error_status": 429, "result": "API Error"}], "usage_limit"),
    ("claude: context full", [{"type": "result", "is_error": True, "result": "Prompt is too long"}], "context_limit"),
    ("claude: context limit worded as a limit", [{"type": "result", "is_error": True, "result": "context window limit exceeded"}], "context_limit"),
    ("claude: other error", [{"type": "result", "is_error": True, "api_error_status": 500, "result": "Internal server error"}], None),
    ("claude: success text naming a limit", [SUCCESS | {"result": "You've hit your 5-hour limit? No: Hb within limits."}], None),
    ("claude: success after a limited result", [LIMITED, SUCCESS], None),  # only the last result counts
    # Codex (codex exec --json)
    ("codex: recovered 429 then completed", [{"type": "error", "message": "Reconnecting... 2/5 (unexpected status 429 Too Many Requests)"},
                                            {"type": "turn.completed"}], None),  # fmt: skip
    ("codex: usage limit message", [{"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again at 7:05 PM."}}],
     "usage_limit"),
    ("codex: bare 429 status", [{"type": "turn.failed", "error": {"status": 429}}], "usage_limit"),
    ("codex: 429 in the message", [{"type": "turn.failed", "error": {"message": "unexpected status 429 Too Many Requests: "}}], "usage_limit"),
    ("codex: insufficient_quota code", [{"type": "turn.failed", "error": {"message": "You exceeded your current quota.", "code": "insufficient_quota"}}],
     "usage_limit"),
    ("codex: string-valued error", [{"type": "turn.failed", "error": "insufficient_quota: billing details"}], "usage_limit"),
    ("codex: error event last", [{"type": "turn.started"}, {"type": "error", "message": "stream error: rate_limit_exceeded"}], "usage_limit"),
    ("codex: context window", [{"type": "turn.failed", "error": {"message": "Your input exceeds the context window of this model."}}], "context_limit"),
    ("codex: context_length_exceeded", [{"type": "turn.failed", "error": {"code": "context_length_exceeded", "message": "limit exceeded"}}],
     "context_limit"),
    ("codex: other failure", [{"type": "turn.failed", "error": {"message": "stream disconnected before completion"}}], None),
    ("codex: limit text only on an item", [{"type": "item.completed", "item": {"type": "agent_message", "text": "usage limit reached"}},
                                          {"type": "turn.completed"}], None),  # fmt: skip
    ("no events at all", ["plain output: rate limit exceeded"], None),
]  # fmt: skip


@pytest.mark.parametrize("name, events, expected", CASES, ids=[c[0] for c in CASES])
def test_the_ending_is_read_from_the_terminal_event_only(name, events, expected):
    assert kind(events) == expected


def test_what_a_usage_limit_records():
    found = ending([LIMITED])
    assert found == {"kind": "usage_limit", "source": "Claude Code result event", "status": 429, "error": "usage_limit_reached", "message": LIMITED["result"]}
    stream = cli_agent.Stream()
    stream.feed(json.dumps(LIMITED) + "\n")
    assert stream.usage_limit == {k: v for k, v in found.items() if k != "kind"} and not stream.context_full


def api_agent_that_submits_then_is_limited(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(f"""import json, re, sys, urllib.request
url = re.search(r"GET  (http://\\S+)/api/ops", sys.argv[1]).group(1)
req = urllib.request.Request(url + "/api/ops/submit", data=json.dumps({{"answer": {DX["text"]!r}, "fields": {DX["fields"]!r}}}).encode(),
                             headers={{"Content-Type": "application/json"}})
print(urllib.request.urlopen(req).read().decode(), file=sys.stderr)
print(json.dumps({LIMITED!r}))
sys.exit(1)
""")
    return [sys.executable, "-S", str(script), "{prompt}"]


@pytest.mark.parametrize("route", ["api", "ui"])
def test_a_run_that_submitted_is_never_reclassified(tmp_path, route):
    if route == "api":
        r = cli_agent.run_episode(STEMI, api_agent_that_submits_then_is_limited(tmp_path), isolate=False)
    else:
        r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, hb_reply_calls(), events_last=[LIMITED], log=False), isolate=False)
    assert r["terminal_reason"] == "submitted" and "usage_limit" not in r, (r["terminal_reason"], r.get("ended_by"))


def test_stderr_naming_a_limit_no_longer_ends_a_run_as_limited(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text("import sys\nprint('warning: rate_limit_exceeded, retrying', file=sys.stderr)\nprint('Hb 109 g/L')\n")
    r = cli_agent.run_episode("find-result-001", [sys.executable, "-S", str(script), "{prompt}"], isolate=False)
    assert r["terminal_reason"] == "submitted" and re.search("109", json.dumps(r["answer"]))
