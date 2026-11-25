"""The audit of the paired agent-CLI run (docs/codex/paired-run-2026-11-21/codex-review.md): each finding reproduced,
then held fixed. The test guard (finding 1) is tests/test_agent_cli_guard.py; keys (finding 4) tests/test_keys.py;
discoverability (the measurement finding) tests/test_navigation.py."""

import json
import random
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from eprbench.agents import claude_browser, cli_browser, openai_browser
from eprbench.episode import Limits
from eprbench.tasks import load_task
from tests.test_agents import ScriptedClient, ScriptedOpenAI, oa_computer_call, oa_response, reply, text, tool_use
from tests.test_cli_browser import STEMI, results, stand_in

sync_api = pytest.importorskip("playwright.sync_api")

# --- 2. the final chance after a timeout has its own deadline for actions on the reply page ---------------------------


def reply_text_point(task_id=STEMI) -> list[int]:
    """Where the reply page's free-text box is, at the harnesses' viewport."""
    with claude_browser.serve_ui(load_task(task_id)) as (url, _app), sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=claude_browser.VIEWPORT)
        page.goto(url + "/reply")
        box = page.locator("#reply-text").bounding_box()
        browser.close()
    assert box["y"] + box["height"] < claude_browser.VIEWPORT["height"], "the reply box is below the fold; scroll first"
    return [round(box["x"] + 20), round(box["y"] + box["height"] / 2)]


def final_chance_actions(evidence_dir) -> list[dict]:
    events = [json.loads(x) for x in (Path(evidence_dir) / "transcript.jsonl").read_text().splitlines()]
    return [e for e in events if e["kind"] == "action" and e.get("final_chance")]


def test_cli_browser_final_chance_after_a_timeout_can_type_on_the_reply_page(tmp_path):
    at = reply_text_point()
    calls = [["screenshot", {}], ["left_click", {"coordinate": at}], ["type", {"text": "Inferior STEMI"}], ["key", {"text": "Tab"}]]
    r = cli_browser.run_episode(STEMI, stand_in(tmp_path, calls, final="", pause_before={"0": 2.5}), isolate=False,
                                limits=Limits(timeout_seconds=2, final_chance_seconds=30), out_dir=tmp_path / "out")  # fmt: skip
    assert r["terminal_reason"] == "timeout"
    told = results(tmp_path)[1:]
    assert [c["isError"] for c in told] == [False, False, False, False], told
    actions = final_chance_actions(r["evidence_dir"])
    assert [a["name"] for a in actions] == ["left_click", "type", "key"] and not any(a.get("error") for a in actions), actions


class Slow:
    """A scripted client whose request number `at` (from 0) takes `seconds`, so the episode's time runs out meanwhile."""

    def __init__(self, at: int, seconds: float):
        self.at, self.seconds = at, seconds

    def _create(self, **kw):
        if len(self.requests) == self.at:
            time.sleep(self.seconds)
        return super()._create(**kw)


class SlowClaude(Slow, ScriptedClient):
    def __init__(self, *responses, at=1, seconds=1.5):
        Slow.__init__(self, at, seconds)
        ScriptedClient.__init__(self, *responses)


class SlowOpenAI(Slow, ScriptedOpenAI):
    def __init__(self, *responses, at=1, seconds=1.5):
        Slow.__init__(self, at, seconds)
        ScriptedOpenAI.__init__(self, *responses)


def test_claude_browser_final_chance_after_a_timeout_can_type_on_the_reply_page(tmp_path):
    at = reply_text_point()
    client = SlowClaude(
        reply(tool_use("screenshot", {}, "a")),
        reply(tool_use("screenshot", {}, "b")),  # arrives after the deadline
        reply(tool_use("left_click", {"coordinate": at}, "c"), tool_use("type", {"text": "Inferior STEMI"}, "d"), tool_use("key", {"text": "Tab"}, "e")),
        reply(text("done"), stop_reason="end_turn"),
    )
    r = claude_browser.run_episode(STEMI, client, limits=Limits(timeout_seconds=1, final_chance_seconds=30), out_dir=tmp_path)
    assert r["terminal_reason"] == "timeout"
    actions = final_chance_actions(r["evidence_dir"])
    assert [a["name"] for a in actions] == ["left_click", "type", "key"] and not any(a.get("error") for a in actions), actions


def test_openai_browser_final_chance_after_a_timeout_can_type_on_the_reply_page(tmp_path):
    x, y = reply_text_point()
    client = SlowOpenAI(
        oa_response("r1", oa_computer_call("c1", NS(type="screenshot"))),
        oa_response("r2", oa_computer_call("c2", NS(type="screenshot"))),  # arrives after the deadline
        oa_response("r3", oa_computer_call("c3", NS(type="click", button="left", x=x, y=y, keys=None), NS(type="type", text="Inferior STEMI"),
                                           NS(type="keypress", keys=["Tab"]))),  # fmt: skip
        oa_response("r4", text="done"),
    )
    r = openai_browser.run_episode(STEMI, client, limits=Limits(timeout_seconds=1, final_chance_seconds=30), out_dir=tmp_path)
    assert r["terminal_reason"] == "timeout"
    actions = final_chance_actions(r["evidence_dir"])
    assert [a["name"] for a in actions] == ["click", "type", "keypress"] and not any(a.get("error") for a in actions), actions


def test_without_a_final_chance_an_action_still_stops_at_the_deadline():
    from eprbench.episode import Episode


    e = Episode(load_task("find-result-001"), harness="t", interface="ui", limits=Limits(timeout_seconds=0.01, final_chance=False))
    e.start_clock()
    time.sleep(0.02)
    assert e.acting_too_late()
    e.open_final_chance()  # no final chance allowed: nothing opens
    assert e.acting_too_late()
    e2 = Episode(load_task("find-result-001"), harness="t", interface="ui", limits=Limits(timeout_seconds=0.01, final_chance_seconds=30))
    e2.start_clock()
    time.sleep(0.02)
    assert e2.acting_too_late()
    e2.open_final_chance()
    assert not e2.acting_too_late()


# --- 3. the bridge shuts down without blocking, under any interleaving ------------------------------------------------


def closes_within(bridge, seconds: float = 5.0) -> bool:
    done = threading.Thread(target=bridge.close, kwargs={"join": 2.0}, daemon=True)
    done.start()
    done.join(seconds)
    return not done.is_alive()


def test_close_does_not_block_on_a_box_it_has_already_filled():
    """The reproduction: a box both waiting and queued in the inbox. close() filled it once, then blocked filling it
    again while draining the inbox."""
    bridge = cli_browser.Bridge(cli_browser._private_dir() + "/s")
    box = cli_browser.queue.Queue(1)
    bridge.waiting.add(box)
    bridge.inbox.put(("message", b"{}", box))
    assert closes_within(bridge)


def test_close_does_not_block_when_the_harness_answered_first():
    bridge = cli_browser.Bridge(cli_browser._private_dir() + "/s")
    box = cli_browser.queue.Queue(1)
    bridge.answer(box, (b"{}", None))  # the harness's answer, not yet taken by the worker
    bridge.inbox.put(("message", b"{}", box))
    bridge.waiting.add(box)
    assert closes_within(bridge)
    bridge.answer(box, (b"{}", None))  # answering after close never blocks either


def _client(path, n, stop):
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(path)
        r = s.makefile("rb")
        for i in range(n):
            if stop.is_set():
                break
            s.sendall(b'{"jsonrpc": "2.0", "id": %d, "method": "ping"}\n' % i)
            if random.random() < 0.5 and r.readline():
                s.sendall(b"delivered\n")
        if random.random() < 0.5:
            s.close()
    except OSError:
        pass


def _harness(bridge, stop):
    """Answers some messages, drops others, as a harness that fails part-way would."""
    while not stop.is_set():
        try:
            kind, _, box = bridge.inbox.get(timeout=0.01)
        except cli_browser.queue.Empty:
            continue
        if kind == "message" and random.random() < 0.7:
            time.sleep(random.random() * 0.005)
            bridge.answer(box, (b'{"jsonrpc": "2.0", "id": 0, "result": {}}', None))


def test_stress_close_under_random_interleavings():
    for seed in range(40):
        random.seed(seed)
        bridge = cli_browser.Bridge(cli_browser._private_dir() + "/s")
        stop = threading.Event()
        threads = [threading.Thread(target=_client, args=(bridge.path, random.randint(1, 8), stop), daemon=True) for _ in range(random.randint(1, 6))]
        threads.append(threading.Thread(target=_harness, args=(bridge, stop), daemon=True))
        for t in threads:
            t.start()
        time.sleep(random.random() * 0.05)
        assert closes_within(bridge), f"seed {seed}: close() blocked"
        stop.set()
        for t in threads:
            t.join(2)
        assert bridge.alive() == [], f"seed {seed}: workers left: {bridge.alive()}"


# --- 5. every bound is advertised; a scroll beyond it is clamped, a repeat or text beyond it refused --------------------


class Wheel:
    def __init__(self):
        self.turned, self.moved = [], []
        self.keyboard = NS(press=lambda k: None, type=lambda t: None, down=lambda k: None, up=lambda k: None)
        self.mouse = NS(move=lambda *a: self.moved.append(a), wheel=lambda dx, dy: self.turned.append((dx, dy)))

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self):
        pass


def test_every_bound_is_in_the_advertised_mcp_schemas():
    from eprbench.agents.claude_browser import BOUNDS

    props = {t["name"]: t["inputSchema"]["properties"] for t in cli_browser.TOOLS}
    assert props["scroll"]["scroll_amount"]["maximum"] == BOUNDS["scroll_amount"]
    assert props["key"]["repeat"]["maximum"] == BOUNDS["repeat"]
    assert props["type"]["text"]["maxLength"] == BOUNDS["type_chars"]
    assert props["hold_key"]["duration"]["maximum"] == BOUNDS["hold_seconds"]
    assert props["wait"]["duration"]["maximum"] == BOUNDS["wait_seconds"]
    for name, p in props.items():  # every number an agent gives has its range stated
        for field, schema in p.items():
            if schema.get("type") in ("integer", "number"):
                assert "maximum" in schema and "minimum" in schema, (name, field)
    assert str(BOUNDS["scroll_amount"]) in props["scroll"]["scroll_amount"]["description"]


def test_a_scroll_beyond_the_maximum_is_clamped_and_says_so():
    from eprbench.agents.claude_browser import BOUNDS, Screen

    page = Wheel()
    out = Screen(page).run("scroll", {"coordinate": [5, 5], "scroll_direction": "down", "scroll_amount": 15})  # the refused call of the run
    assert page.turned == [(0, 100 * BOUNDS["scroll_amount"])]
    assert f"scrolled {BOUNDS['scroll_amount']}" in out[0]["text"] and "15" in out[0]["text"]
    assert Screen(page).run("scroll", {"coordinate": [5, 5], "scroll_direction": "up", "scroll_amount": 3}) == [{"type": "text", "text": "OK"}]


@pytest.mark.parametrize("name, args, limit", [
    ("key", {"text": "Tab", "repeat": 11}, "10"),
    ("type", {"text": "x" * 2001}, "2000"),
    ("hold_key", {"text": "shift", "duration": 3}, "2"),
    ("wait", {"duration": 2}, "1"),
])  # fmt: skip
def test_a_repeat_or_text_beyond_its_bound_is_refused_with_the_limit(name, args, limit):
    from eprbench.agents.claude_browser import Screen

    with pytest.raises(ValueError, match=rf"at most {limit}\b"):
        Screen(Wheel()).run(name, args)


def test_the_openai_browser_scroll_goes_through_the_shared_bound():
    from eprbench.agents.claude_browser import BOUNDS, Screen

    page = Wheel()
    openai_browser.perform(Screen(page), NS(type="scroll", x=10, y=20, scroll_x=-50, scroll_y=99999))
    assert page.turned == [(-50, 100 * BOUNDS["scroll_amount"])] and page.moved[-1] == (10, 20)
