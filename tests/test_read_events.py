"""One definition of "the agent read X" on every surface (agent_read, ops.SHOWS).

An entry counts as read when its content reached the agent: returned by a tool or API call, or, in the browser, inside
a screenshot sent to the model. The same clinical behaviour through tools and through a real Chromium session that
scrolls through each page gives the same read events and the same grade; a page that was rendered but never seen, or
an entry never scrolled into view, is not read.
"""

import json
import sqlite3

import pytest

from eprbench import ops
from eprbench.agents import claude_browser
from eprbench.env import EPREnv
from eprbench.server.app import create_app
from eprbench.tasks import build_world, grade, load_task
from eprbench.world import World
from tests.test_preview_cases import DVT, E, B, HYPOK_J, LEG_CHECK, PHYSIO_NOTE, SOURCES, WELLS_FORM, WELLS_TRUE, DOPPLER, full, play, tool
from tests.test_server import client

sync_api = pytest.importorskip("playwright.sync_api")

ANSWER = {"text": "Not discharged: ?DVT, Wells recorded, Doppler requested.", "fields": {}}
UE = build_world(load_task(DVT)).one("SELECT id FROM result WHERE patient_id = ? AND orderable_code = 'UE' ORDER BY performed_at DESC LIMIT 1", (E,))["id"]
INFORMATION = ("note", "problems", "result", "result_value", "observation_set")  # what the cases' reading checks are about
WEIGHT_SET = build_world(load_task(DVT)).one("SELECT min(s.id) AS id FROM observation w JOIN observation s ON s.patient_id = w.patient_id AND s.taken_at = w.taken_at "
                                              "WHERE w.patient_id = ? AND w.kind = 'WEIGHT'", (E,))["id"]  # fmt: skip


def reads(world, kinds=INFORMATION, via=("returned", "screenshot", "person")) -> set:
    """What agent_read sees: never what was only rendered."""
    marks, ways = ", ".join("?" * len(kinds)), ", ".join("?" * len(via))
    rows = world.q(f"SELECT kind, object_id, patient_id FROM read_event WHERE kind IN ({marks}) AND via IN ({ways})", (*kinds, *via))
    return {(r["kind"], r["object_id"], r["patient_id"]) for r in rows}


def by_tools(actions):
    env = EPREnv(DVT, max_steps=0)
    env.reset()
    for a in actions:
        obs, *_ = env.step(a)
        assert obs["ok"], (a, obs)
    return env.world


def by_pages(steps, reads_mode="screenshots"):
    """Page requests with no browser and nobody looking: GET a path, or POST a form (path, fields)."""
    if reads_mode == "person":
        from fastapi.testclient import TestClient

        task = load_task(DVT)
        app = create_app(lambda: build_world(task), mode="ui", actor=task.actor, task=task, reads="person")
        c = TestClient(app, follow_redirects=True)
    else:
        c, app = client("ui", DVT)
    for step in steps:
        r = c.post(step[0], data=step[1]) if isinstance(step, tuple) else c.get(step)
        assert r.status_code == 200 and 'role="alert"' not in r.text, (step, r.text[-600:])
    return app.state.world


def look(screen, reads, scroll: bool) -> None:
    """A screenshot of the page as it opens and, with `scroll`, one after each scroll down to its end; each is delivered
    to the model in a request of its own, as a harness sends it (a tool result carrying the image)."""
    def shot():
        content = screen.run("screenshot", {})
        reads.delivered({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s", "content": content}]}]})

    shot()
    for _ in range(40):
        if not scroll or screen.page.evaluate("scrollY + innerHeight >= document.documentElement.scrollHeight - 1"):
            break
        screen.run("scroll", {"coordinate": [640, 400], "scroll_direction": "down", "scroll_amount": 5})
        shot()


def by_browser(steps, scroll=True):
    """A real Chromium session, as a browser harness runs one: each page opened and looked at in screenshots, each
    delivered and reported (claude_browser.ScreenReads); forms are posted without anyone looking at the page they lead to."""
    with claude_browser.serve_ui(load_task(DVT)) as (url, app), sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=claude_browser.VIEWPORT)
        reads = claude_browser.ScreenReads(page, app)
        screen = claude_browser.Screen(page, on_shot=reads.captured)
        for step in steps:
            if isinstance(step, tuple):
                assert page.request.post(url + step[0], form=step[1]).ok, step
            else:
                page.goto(url + step)
                look(screen, reads, scroll)
        browser.close()
    return app.state.world


WELLS_PAGE = (f"/patients/{E}/assessments", {"kind": "wells_dvt", "details": "R calf 3.5 cm larger", **{f"af_{k}": v for k, v in WELLS_TRUE.items()}})
DOPPLER_PAGE = (f"/ice/patients/{E}/requests", {"orderable_code": "US-DOPPLER-LL", "priority": "urgent", "clinical_details": "D4 post R TKR, new R calf swelling",
                                                "collection": "now", "rf_clinical_question": "?DVT", "rf_mobility": "walking", "rf_laterality": "right"})  # fmt: skip
SUMMARY, PHYSIO, LEG = f"/patients/{E}?tab=summary", f"/patients/{E}?tab=notes&note_id={PHYSIO_NOTE}", f"/patients/{E}?tab=notes&note_id={LEG_CHECK}"
UE_PAGE, OBS_PAGE = f"/ice/patients/{E}?tab=results&result_id={UE}", f"/patients/{E}?tab=observations"
LOOK_UP = [tool("get_result", result_id=UE, patient_id=E), tool("list_observations", patient_id=E, limit=200)]
STEPS = [SUMMARY, PHYSIO, LEG, UE_PAGE, OBS_PAGE, WELLS_PAGE, DOPPLER_PAGE]


def test_the_same_reading_through_tools_and_a_scrolling_browser_gives_the_same_read_events_and_grade():
    tools = by_tools([*SOURCES, *LOOK_UP, WELLS_FORM, DOPPLER])
    browser = by_browser(STEPS)
    assert reads(tools) == reads(browser) and {k for k, *_ in reads(tools)} == set(INFORMATION)
    assert browser.q("SELECT DISTINCT via FROM read_event WHERE via != 'rendered'") == [{"via": "screenshot"}]
    task = load_task(DVT)
    graded = [full(grade(w, task, ANSWER, terminal_reason="submitted")) for w in (tools, browser)]
    assert graded[0] == graded[1]
    assert graded[0]["tier"] == "complete" and "enoxaparin_inputs_looked_at" in graded[0]["bonuses"]
    # each screenshot's reads are an action of their own, after the page that showed them and before the next form
    seen = browser.q("SELECT DISTINCT action_id FROM read_event WHERE via = 'screenshot'")
    pages = {r["action_id"] for r in browser.q("SELECT action_id FROM audit WHERE interface = 'ui'")}
    assert seen and not {r["action_id"] for r in seen} & pages


def test_review_5_an_entry_never_scrolled_into_view_is_not_read():
    world = by_browser(STEPS, scroll=False)
    shown = reads(world, ("observation_set",))
    assert shown and ("observation_set", WEIGHT_SET, E) not in shown  # the oldest set, her weight, is below the first screen
    assert ("observation_set", WEIGHT_SET, E) in reads(world, ("observation_set",), via=("rendered",))  # though the page carried it
    assert "enoxaparin_inputs_looked_at" not in full(grade(world, load_task(DVT), ANSWER, terminal_reason="submitted"))["bonuses"]


def test_review_5_pages_requested_but_never_seen_record_only_what_was_rendered():
    # the review's probe: page requests with no screenshot earned the reading bonus
    world = by_pages(STEPS)
    assert reads(world) == set() and reads(world, via=("rendered",)) >= {("problems", None, E), ("note", PHYSIO_NOTE, E), ("observation_set", WEIGHT_SET, E)}
    outcome = full(grade(world, load_task(DVT), ANSWER, terminal_reason="submitted"))
    assert outcome["missing"] == ["reassessed"] and "enoxaparin_inputs_looked_at" not in outcome["bonuses"]
    # a page that fetches the summary to draw its banner renders the allergies and nothing else of it
    world = by_pages([f"/patients/{E}?tab=notes", f"/patients/{E}?tab=medications", f"/ice/patients/{E}?tab=results"])
    assert world.q("SELECT DISTINCT op FROM audit WHERE op = 'get_patient' AND interface = 'ui'") == [{"op": "get_patient"}]
    rendered = {k for k, *_ in reads(world, tuple(ops.READ_KINDS), via=("rendered",))}
    assert "problems" not in rendered and {"allergies", "note_list", "medication", "result_list"} <= rendered


def test_a_persons_session_counts_what_its_pages_showed_since_nobody_reports_the_screen():
    world = by_pages(STEPS, reads_mode="person")
    assert reads(world, via=("person",)) == reads(by_tools([*SOURCES, *LOOK_UP, WELLS_FORM, DOPPLER]))
    assert full(grade(world, load_task(DVT), ANSWER, terminal_reason="submitted"))["tier"] == "complete"


def test_a_ui_agent_who_never_opens_the_entries_gets_no_reading_credit():
    # both notes opened, and every page showed her banner, but the problem list was never on screen
    world = by_browser([PHYSIO, LEG, f"/ice/patients/{E}?tab=results", f"/ice/patients/{E}?tab=cumulative", WELLS_PAGE, DOPPLER_PAGE])
    outcome = full(grade(world, load_task(DVT), ANSWER, terminal_reason="submitted"))
    assert outcome["tier"] == "partial" and outcome["missing"] == ["reassessed"] and "enoxaparin_inputs_looked_at" not in outcome["bonuses"]
    assert {k for k, *_ in reads(world, ("allergies", "problems"))} == {"allergies"}
    # the notes list (titles) is not a note
    world = by_browser([SUMMARY, f"/patients/{E}?tab=notes", WELLS_PAGE, DOPPLER_PAGE])
    assert full(grade(world, load_task(DVT), ANSWER, terminal_reason="submitted"))["missing"] == ["reassessed"]
    # opening the notes and the summary after the form is too late: the score was recorded unread
    late = by_browser([WELLS_PAGE, SUMMARY, PHYSIO, LEG, DOPPLER_PAGE])
    assert full(grade(late, load_task(DVT), ANSWER, terminal_reason="submitted"))["missing"] == ["reassessed"]


def test_both_browser_harnesses_report_what_their_screenshots_showed(tmp_path):
    from types import SimpleNamespace as NS

    from eprbench.agents import openai_browser
    from tests.test_agents import ScriptedClient, ScriptedOpenAI, oa_computer_call, oa_response, reply, text, tool_use
    from tests.test_typed_answers import RESULT, TASK, spots

    at = spots()
    click = lambda name, id: tool_use("left_click", {"coordinate": at[name]}, id)  # noqa: E731
    claude = ScriptedClient(
        reply(click("ice", "a"), click("search", "b"), tool_use("type", {"text": "H4417302"}, "c"), tool_use("key", {"text": "Return"}, "d")),
        reply(click("patient", "e"), click("results", "f"), click("fbc", "g"), tool_use("screenshot", {}, "h")),
        reply(text("Hb 109 g/L."), stop_reason="end_turn"),
    )  # fmt: skip
    claude_browser.run_episode(TASK, claude, out_dir=tmp_path / "claude")
    go = lambda name: NS(type="click", button="left", x=at[name][0], y=at[name][1], keys=None)  # noqa: E731
    openai = ScriptedOpenAI(oa_response("r1", oa_computer_call("c1", go("ice"), go("search"), NS(type="type", text="H4417302"), NS(type="keypress", keys=["ENTER"]))),
                            oa_response("r2", oa_computer_call("c2", go("patient"), go("results"), go("fbc"))), oa_response("r3", text="Hb 109 g/L."))  # fmt: skip
    openai_browser.run_episode(TASK, openai, out_dir=tmp_path / "openai")
    for harness in ("claude", "openai"):
        rows = [json.loads(x) for x in next((tmp_path / harness).iterdir()).joinpath("reads.jsonl").read_text().splitlines()]
        assert ("result", RESULT) in {(r["kind"], r["object_id"]) for r in rows if r["via"] == "screenshot"}, harness
        assert not [r for r in rows if r["via"] == "returned"]


def test_every_route_to_the_same_information_earns_the_same_credit():
    # renal function from one result, the cumulative eGFR or the cumulative creatinine; her weight from the chart
    for renal in (tool("get_result", result_id=UE, patient_id=E), tool("get_analyte_history", patient_id=E, analyte="eGFR"),
                  tool("get_analyte_history", patient_id=E, analyte="creatinine")):  # fmt: skip
        assert "enoxaparin_inputs_looked_at" in full(play(DVT, [renal, tool("list_observations", patient_id=E), *SOURCES, WELLS_FORM, DOPPLER]))["bonuses"]
    # the list of results (titles) shows no value, and charted observations without her weight show no weight
    assert "enoxaparin_inputs_looked_at" not in full(play(DVT, [tool("list_results", patient_id=E), tool("list_observations", patient_id=E, limit=1)]))["bonuses"]
    # potassium over time: the cumulative view, or two potassium results read one by one; one result is only the last value
    ue = [r["id"] for r in build_world(load_task(HYPOK_J)).q("SELECT id FROM result WHERE patient_id = ? AND orderable_code = 'UE' ORDER BY performed_at DESC", (B,))]
    trend = "potassium_trend_reviewed"
    assert trend in full(play(HYPOK_J, [tool("get_analyte_history", patient_id=B, analyte="potassium")]))["bonuses"]
    assert trend in full(play(HYPOK_J, [tool("get_result", result_id=i, patient_id=B) for i in ue[:2]]))["bonuses"]
    assert trend not in full(play(HYPOK_J, [tool("get_result", result_id=ue[0], patient_id=B)]))["bonuses"]


def test_the_ui_tags_every_kind_of_entry_a_tool_can_return():
    from tests.test_ui_parity import ui_session

    c, app = client("ui")
    ui_session((c, app))
    c.get("/ice/patients/1?tab=blood")  # the crawl requested blood after it had seen that tab
    shown = {r["kind"] for r in app.state.world.q("SELECT DISTINCT kind FROM read_event WHERE interface = 'ui' AND via = 'rendered'")}
    assert shown == set(ops.READ_KINDS)


def test_tool_and_api_reads_record_what_was_returned_and_failures_record_nothing():
    world = by_tools([tool("get_note", note_id=PHYSIO_NOTE, patient_id=E), tool("list_notes", patient_id=E)])
    assert reads(world, ("note", "note_list")) == {("note", PHYSIO_NOTE, E), ("note_list", None, E)} == reads(world, ("note", "note_list"), via=("returned",))
    env = EPREnv(DVT, max_steps=0)
    env.reset()
    env.step(tool("get_note", note_id=PHYSIO_NOTE, patient_id=E + 1))  # refused: another patient's note
    env.step(tool("get_analyte_history", patient_id=E, analyte="magnesium"))  # none on record
    assert env.world.q("SELECT * FROM read_event") == []
    c, app = client("api", DVT)
    assert c.post("/api/ops/get_note", json={"note_id": PHYSIO_NOTE, "patient_id": E}).status_code == 200
    (row,) = app.state.world.q("SELECT interface, kind, object_id, audit_id IS NOT NULL AS linked FROM read_event")
    assert row == {"interface": "api", "kind": "note", "object_id": PHYSIO_NOTE, "linked": 1}


def test_a_world_saved_before_read_events_rebuilds_them_from_the_audit_log():
    world = by_tools([*SOURCES, *LOOK_UP])
    before = world.q("SELECT at, actor, interface, action_id, audit_id, patient_id, kind, object_id FROM read_event ORDER BY id")
    conn = sqlite3.connect(":memory:")
    conn.deserialize(world.snapshot())
    conn.executescript("DROP TABLE read_event; UPDATE meta SET value = '4' WHERE key = 'schema';")
    old = World.restore(conn.serialize())
    assert old.q("SELECT at, actor, interface, action_id, audit_id, patient_id, kind, object_id FROM read_event ORDER BY id") == before


def test_a_ui_world_saved_when_pages_counted_as_read_is_migrated_to_rendered():
    world = by_pages([SUMMARY, PHYSIO])
    conn = sqlite3.connect(":memory:")
    conn.deserialize(world.snapshot())
    conn.executescript("DROP INDEX read_event_kind; ALTER TABLE read_event DROP COLUMN via; CREATE INDEX read_event_kind ON read_event(kind, object_id);"
                       "UPDATE meta SET value = '5' WHERE key = 'schema';")  # fmt: skip
    old = World.restore(conn.serialize())
    assert old.q("SELECT DISTINCT via FROM read_event") == [{"via": "rendered"}] and reads(old) == set()


def test_the_evidence_keeps_the_read_events_and_regrading_an_older_world_says_it_was_migrated(tmp_path):
    from eprbench.episode import Episode, regrade

    env = EPREnv(DVT, max_steps=0)
    env.reset()
    for a in [*SOURCES, WELLS_FORM, DOPPLER]:
        env.step(a)
    ep = Episode(env.task, harness="test", interface="tool", out_dir=tmp_path)
    ep.end("submitted", ANSWER)
    line = ep.close(env.world)
    kinds = [json.loads(x)["kind"] for x in (ep.dir / "reads.jsonl").read_text().splitlines()]
    assert {"note", "problems", "allergies"} <= set(kinds)
    assert regrade(ep.dir)["world_migrated_from"] is None and regrade(ep.dir)["horizon_world_reproduced"]
    conn = sqlite3.connect(":memory:")  # the same episode as an earlier version saved it: no read events, schema 4
    conn.deserialize((ep.dir / "final.sqlite").read_bytes())
    conn.executescript("DROP TABLE read_event; UPDATE meta SET value = '4' WHERE key = 'schema';")
    (ep.dir / "final.sqlite").write_bytes(conn.serialize())
    r = regrade(ep.dir)
    assert r["world_migrated_from"] == "4" and r["horizon_world_reproduced"] is None and r["graded_world_reproduced"] is False
    assert full(r["report"]) == full(line) and full(line)["tier"] == "complete"  # its reads were rebuilt from the audit log
