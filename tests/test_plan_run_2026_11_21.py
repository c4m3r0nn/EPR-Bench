"""Problems found by the first 40-episode agent plan (results/agents-preview, 21 November 2026).

1. The UI's Wait control let a slip skip hours: Codex clicked the prefilled minutes box ("15"), pressed ctrl+a to select
   it (on macOS Chromium ctrl+a moves the caret to the start instead) and typed "5": the box read 515 and the clock
   jumped 8 h 35 min (and 215 in another run). The control is now fixed buttons, the wait operation has the same
   maximum per call on every route, prefilled boxes select their value on focus, and ctrl means the platform's
   shortcut key in the browser executor.
2. A subscription's usage limit ended episodes as harness errors and the plan ploughed on: it is now its own terminal
   reason and stops the plan.
3. The plan summary said everything had run: it now reports what completed, what errored and what is left.
"""

import json
import sys
from pathlib import Path

import pytest

from eprbench import ops
from eprbench.agents import claude_browser, cli_agent, cli_browser
from eprbench.episode import COMPLETE_ENDINGS, TERMINAL_REASONS, TRUNCATED, Limits
from eprbench.tasks import build_world, load_task
from tests.test_cli_browser import API_STAND_IN, run_cli, stand_in

sync_api = pytest.importorskip("playwright.sync_api")


# --- 1. the Wait control ---------------------------------------------------------------------------------------------


def test_the_wait_operation_takes_at_most_an_hour_a_call_on_every_route():
    world = build_world(load_task("find-result-001"))
    assert ops.MAX_WAIT_SECONDS == 3600
    with pytest.raises(ops.OpError):
        ops.call(world, "wait", {"seconds": 3601}, ops.agent_ctx("Dr A. Gent (FY1)", "tool"))
    before = world.now
    ops.call(world, "wait", {"seconds": 3600}, ops.agent_ctx("Dr A. Gent (FY1)", "tool"))
    assert world.now == before + 3600
    schema = next(t for t in ops.tool_schemas() if t["name"] == "wait")["input_schema"]["properties"]["seconds"]
    assert schema["maximum"] == 3600 and "60 minutes" in schema["description"]


@pytest.fixture
def page():
    task = load_task("preview-stemi-judgement")
    with claude_browser.serve_ui(task) as (url, app), sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport=claude_browser.VIEWPORT)
        pg.goto(url + "/ice/patients/3?tab=results")
        yield pg, app, claude_browser.Screen(pg)
        browser.close()


def centre(pg, selector):
    box = pg.locator(selector).first.bounding_box()
    return [round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2)]


def test_the_wait_control_is_fixed_buttons_and_cannot_skip_hours(page):
    pg, app, screen = page
    labels = pg.locator("#wait-form button").all_inner_texts()
    assert labels == ["1", "5", "15", "30", "60"] and "Wait (min)" in pg.inner_text("#wait-form") and pg.locator("#wait-form input:not([type=hidden])").count() == 0
    before = app.state.world.now
    screen.run("left_click", {"coordinate": centre(pg, "#wait-form button[value='5']")})
    assert app.state.world.now - before == 300 and pg.url.endswith("/ice/patients/3?tab=results")  # back where it was
    # a forged larger value is refused by the operation, and the clock does not move
    response = pg.request.post(pg.url.split("/ice")[0] + "/clock/wait", form={"minutes": "515", "next": "/"})
    assert response.ok and app.state.world.now - before == 300
    # every control fits in the header at the viewport's width, on screen without scrolling
    assert all(0 <= centre(pg, f"#wait-form button[value='{m}']")[0] < claude_browser.VIEWPORT["width"] for m in (1, 5, 15, 30, 60))


def test_what_codex_did_now_replaces_the_value(page):
    """The plan's run: click a box that holds a value, ctrl+a, type. On macOS Chromium ctrl+a moved the caret to the start
    and "5" joined "15" as 515; ctrl now means the platform's shortcut key, so ctrl+a selects all as on Linux and Windows."""
    pg, app, screen = page
    pg.goto(pg.url.split("/ice")[0] + "/?q=Whitcombe")  # the search box shows the last query again
    assert pg.input_value("#patient-search") == "Whitcombe"
    screen.run("left_click", {"coordinate": centre(pg, "#patient-search")})
    screen.run("key", {"text": "ctrl+a"})
    screen.run("type", {"text": "H5120986"})
    assert pg.input_value("#patient-search") == "H5120986"
    assert claude_browser.to_playwright_key("shift+Tab") == "Shift+Tab"  # what ctrl+a becomes on each platform: tests/test_keys.py


def test_no_box_comes_prefilled_with_a_default(page):
    """A default shown as a value is what a typed digit joins ("2" after "1" read 12): defaults are placeholders now, and
    the server applies them when the box is left empty. Boxes still show what the agent itself typed (a search, a
    refused form), which it can see in the screenshot."""
    pg, app, screen = page
    root = pg.url.split("/ice")[0]
    pg.goto(root + "/ice/patients/3?tab=blood")
    assert pg.input_value("#blood-units") == "" and pg.get_attribute("#blood-units", "placeholder") == "1"
    screen.run("left_click", {"coordinate": centre(pg, "#blood-units")})
    screen.run("type", {"text": "2"})
    assert pg.input_value("#blood-units") == "2"
    pg.goto(root + "/patients/3?tab=discharge")
    assert pg.input_value("#discharge-destination") == "" and pg.get_attribute("#discharge-destination", "placeholder") == "Usual residence"
    for template in Path(claude_browser.__file__).parent.parent.joinpath("server", "templates").glob("*.html"):
        text = template.read_text()  # no input carries a literal default: only placeholders, or what was entered ({{ ... }})
        import re

        literal = [m for m in re.findall(r'<input[^>]*type="(?:text|number|search)"[^>]*>', text) if re.search(r'value="[^{"]', m)]
        assert literal == [], (template.name, literal)


def test_an_empty_box_takes_its_default_when_submitted():
    from fastapi.testclient import TestClient

    from eprbench.server.app import create_app

    task = load_task("preview-stemi-judgement")
    app = create_app(lambda: build_world(task), mode="ui", actor=task.actor, task=task)
    c = TestClient(app)
    c.post("/patients/3/discharge", data={"destination": ""})
    assert app.state.world.one("SELECT args FROM audit WHERE op = 'discharge_patient'")["args"].count("Usual residence") == 1


# --- 2. a subscription's usage limit --------------------------------------------------------------------------------

LIMITED = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "api_error": "usage_limit_reached",
           "result": "You've hit your limit · resets 7pm (Europe/London)"}  # fmt: skip


def test_a_usage_limit_is_its_own_ending_never_a_clinical_outcome():
    assert "agent_usage_limit" in TERMINAL_REASONS and "agent_usage_limit" not in COMPLETE_ENDINGS and "agent_usage_limit" not in TRUNCATED
    stream = cli_agent.Stream()
    stream.feed(json.dumps(LIMITED) + "\n")
    assert stream.usage_limit == {"source": "Claude Code result event", "status": 429, "error": "usage_limit_reached", "message": LIMITED["result"]}
    codex = cli_agent.Stream()
    codex.feed(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again at 7:05 PM."}}) + "\n")
    assert codex.usage_limit["source"] == "Codex event" and "7:05 PM" in codex.usage_limit["message"]
    # stderr is no longer read for limits: only the terminal event counts (tests/test_usage_limits.py)
    assert cli_agent.classify_ending({"type": "error", "message": "ERROR: rate_limit_exceeded: try again in 20m"})["kind"] == "usage_limit"
    assert cli_agent.classify_ending({"type": "result", "is_error": False, "result": "Hb was 109 g/L, within limits"}) is None


def limited_api_agent(tmp_path):
    script = tmp_path / "limited.py"
    script.write_text(f"import json, sys\nprint(json.dumps({LIMITED!r}))\nsys.exit(1)\n")
    return [sys.executable, "-S", str(script), "{prompt}"]


@pytest.mark.parametrize("route", ["api", "ui"])
def test_an_episode_stopped_by_a_usage_limit_ends_as_such(tmp_path, route):
    if route == "api":
        r = cli_agent.run_episode("find-result-001", limited_api_agent(tmp_path), isolate=False, out_dir=tmp_path / "out")
    else:
        r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, [], events_last=[LIMITED], log=False), isolate=False, out_dir=tmp_path / "out")
    assert r["terminal_reason"] == "agent_usage_limit" and r["usage_limit"]["error"] == "usage_limit_reached" and "resets 7pm" in r["ended_by"]
    manifest = json.loads((Path(r["evidence_dir"]) / "manifest.json").read_text())
    assert manifest["complete"] is False and r["outcome"]["tier"] is not None  # graded for the record, never counted


# --- 3. the plan stops at a usage limit, and its summary tells the truth ---------------------------------------------


def test_a_plan_stops_at_a_usage_limit_says_when_it_resets_and_reruns_the_episode(tmp_path, monkeypatch, capsys):
    counter = tmp_path / "count"
    script = tmp_path / "agent.py"
    script.write_text(  # the second episode meets the subscription's limit; after the reset it works again
        "import json, os, sys\n"
        f"n = int(open({str(counter)!r}).read()) if os.path.exists({str(counter)!r}) else 0\n"
        f"open({str(counter)!r}, 'w').write(str(n + 1))\n"
        f"if n == 1:\n    print(json.dumps({LIMITED!r}))\n    sys.exit(1)\n"
        + API_STAND_IN
    )  # fmt: skip
    monkeypatch.setitem(cli_agent.PRESETS, "claude", [sys.executable, "-S", str(script), "{prompt}"])
    plan = tmp_path / "plan.json"
    run_cli(monkeypatch, "plan", "find-result-001", "--agents", "claude", "--interfaces", "api", "--repeats", "3", "--seed", "1", "--out", str(plan))
    capsys.readouterr()
    out = str(tmp_path / "ev")
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(plan), "--out-dir", out, "--allow-unsandboxed")
    err = capsys.readouterr().err
    summary = json.loads(next(x for x in err.splitlines() if x.startswith('{"plan"')))["plan"]
    assert e.value.code == 3 and "usage limit" in err and "resets 7pm" in err
    assert (summary["ran"], summary["completed"], summary["errored"], summary["left"]) == (2, 1, {"agent_usage_limit": 1}, 2)
    run_cli(monkeypatch, "run", "--plan", str(plan), "--out-dir", out, "--allow-unsandboxed")  # after the reset: the limited episode runs again
    summary = json.loads(next(x for x in capsys.readouterr().err.splitlines() if x.startswith('{"plan"')))["plan"]
    assert (summary["already_complete"], summary["ran"], summary["completed"], summary["errored"], summary["left"]) == (1, 2, 2, {}, 0)


def test_a_plan_with_episodes_that_errored_says_so_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    script = tmp_path / "broken.py"
    script.write_text("import sys\nsys.exit(2)\n")
    monkeypatch.setitem(cli_agent.PRESETS, "claude", [sys.executable, "-S", str(script), "{prompt}"])
    plan = tmp_path / "plan.json"
    run_cli(monkeypatch, "plan", "find-result-001", "--agents", "claude", "--interfaces", "api", "--repeats", "2", "--seed", "1", "--out", str(plan))
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "run", "--plan", str(plan), "--out-dir", str(tmp_path / "ev"), "--allow-unsandboxed")
    err = capsys.readouterr().err
    summary = json.loads(next(x for x in err.splitlines() if x.startswith('{"plan"')))["plan"]
    assert e.value.code == 3 and (summary["ran"], summary["completed"], summary["errored"], summary["left"]) == (2, 0, {"harness_error": 2}, 2)
    assert "2 episodes left" in err


# --- the two counts of actions ---------------------------------------------------------------------------------------


def test_the_two_action_counts_are_named_for_what_they_count(tmp_path):
    from tests.test_cli_browser import Twin, find_hb

    twin = Twin("find-result-001")
    try:
        calls = find_hb(twin).calls
    finally:
        twin.close()
    r = cli_browser.run_episode("find-result-001", stand_in(tmp_path, calls, final="Hb 109 g/L"), isolate=False, limits=Limits(max_actions=4))
    acc = r["accounting"]
    assert r["terminal_reason"] == "action_limit" and acc["actions"] == 4  # the limit's unit: mouse and keyboard actions, charged before they run
    units = r["count_units"]
    assert units["accounting.actions"].startswith("agent actions charged against max_actions: here, mouse and keyboard actions")
    assert units["actions_summary.<interface>.actions"].startswith("EPR requests")  # page loads and form posts the record served
    assert r["actions_summary"]["ui"]["actions"] < acc["actions"]  # clicks that load no page are actions but no EPR request
