"""Capability discovery is the same on both routes (the audit of the paired run, docs/codex/paired-run-2026-11-21): every
API run of the discharge case recorded the structured Wells assessment and no browser run did, while the API listing
named the assessment operations and the UI's entry to them sat below the fold of the Summary page. Now one map
(eprbench.sections) gives the API listing its sections and the UI its patient menu: the same names, the same one-line
descriptions, every operation reachable from the menu, and the whole menu on the first screen of every patient page.
Nothing in the map may point at what a task needs."""

import re
from urllib.parse import urlencode, urlsplit

import pytest

from eprbench import ops, sections
from eprbench.agents.claude_browser import VIEWPORT
from eprbench.server import ui
from tests.test_server import client
from tests.test_ui_parity import Page, fill

# Words that would hint at a particular task or tell the agent when to act: none may appear in the map.
HINTS = re.compile(r"wells|dvt|stemi|ppci|potassium|doppler|vte|warfarin|should|must|always|when |before |remember|important|required", re.I)
CLICKS = 8  # the most clicks or form submissions from a patient page to any operation (ICE's imaging menus are the deepest)
PATIENT_CLICKS = 5  # and to any operation of a patient section: its menu entry, then at most the prescribing steps (search, drug, product) and the form


def test_every_operation_is_in_exactly_one_section_and_the_map_is_neutral():
    names = [op for s in (*sections.SECTIONS, sections.REPLY) for op in s.operations]
    assert sorted(names) == sorted([*ops.REGISTRY, "submit"]) and len(names) == len(set(names))
    for s in (*sections.SECTIONS, sections.REPLY):
        assert "\n" not in s.description and len(s.description) < 160, s.name
        assert not HINTS.search(s.name + " " + s.description), (s.name, HINTS.search(s.name + " " + s.description)[0])
    assert ui.TABS == sections.TABS


@pytest.mark.parametrize("task_id", ["order-ctpa-001", "preview-dvt-discharge-judgement", "preview-stemi-judgement"])
def test_the_api_listing_groups_operations_under_the_ui_section_names(task_id):
    from eprbench import answers
    from eprbench.tasks import load_task

    form = answers.has_form(load_task(task_id))
    c, _ = client("api", task_id)
    listing = c.get("/api/ops").json()
    assert [s["section"] for s in listing["sections"]] == [s.name for s in sections.SECTIONS] + (["Handover / reply"] if form else [])
    if form:
        assert [o["name"] for o in listing["sections"][-1]["operations"]] == ["submit"]
    for listed, s in zip(listing["sections"], sections.SECTIONS):  # each as the UI names and describes it
        assert listed["description"] == s.description and [o["name"] for o in listed["operations"]] == list(s.operations)
        assert all(set(o) == {"name", "description", "input_schema"} for o in listed["operations"])


def menu(html: str) -> list[tuple[str, str]]:
    nav = re.search(r'<nav class="tabs" aria-label="Patient menu"[^>]*>(.*?)</nav>', html, re.S)
    assert nav, "no patient menu"
    return [(re.sub(r"<[^>]+>", "", label).strip(), href.replace("&amp;", "&")) for href, label in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', nav[1])]


def test_every_patient_page_has_the_menu_and_its_sections_description():
    c, _ = client("ui")
    for tab in sections.TABS:
        html = c.get(f"/patients/1?tab={tab}").text
        assert [label for label, _ in menu(html)] == [s.name for s in sections.PATIENT_MENU], tab
        assert dict(menu(html))["Assessments"] == "/patients/1?tab=assessments" and dict(menu(html))["Requests and results (ICE)"] == "/ice"
        description = re.search(r'<p class="muted" id="section-description">(.*?)</p>', html)[1]
        assert description == sections.BY_TAB[tab].description.replace("'", "&#39;"), tab


# Forms that change the record, by action, as ui.py routes them.
POSTS = [
    (r"/patients/\d+/notes", "write_note"), (r"/patients/\d+/notes/\d+", "update_note"), (r"/patients/\d+/observations", "record_observations"),
    (r"/patients/\d+/observations/\d+/error", "mark_observations_in_error"), (r"/patients/\d+/medications", "prescribe_medication"),
    (r"/patients/\d+/medications/\d+/administrations", "record_administration"), (r"/patients/\d+/medications/\d+/stop", "stop_medication"),
    (r"/patients/\d+/referrals", "make_referral"), (r"/patients/\d+/problems", "add_problem"), (r"/patients/\d+/allergies", "record_allergy"),
    (r"/patients/\d+/fluids", "record_fluid"), (r"/patients/\d+/jobs", "add_job"), (r"/patients/\d+/jobs/\d+/complete", "complete_job"),
    (r"/patients/\d+/jobs/\d+/cancel", "cancel_job"), (r"/patients/\d+/assessments", "record_assessment"), (r"/patients/\d+/discharge", "discharge_patient"),
    (r"/ice/patients/\d+/requests", "place_order"), (r"/ice/patients/\d+/results/\d+/acknowledge", "acknowledge_result"),
    (r"/ice/patients/\d+/blood", "request_blood_product"), (r"/ice/patients/\d+/requests/\d+/cancel", "cancel_order"), (r"/clock/wait", "wait"),
]  # fmt: skip


def reachable(start: str) -> dict[str, int]:
    """The fewest clicks (a link, or a form submitted) from `start` to each operation: a reading operation counts once
    a page that runs it has loaded; a writing one once a page offers its form, plus the submission."""
    c, app = client("ui")
    world = app.state.world
    ops.call(world, "write_note", {"patient_id": 1, "title": "Draft plan", "body": "To finish.", "status": "draft"}, ops.Ctx(actor=app.state.task.actor))
    ops.call(world, "place_order", {"patient_id": 1, "orderable_code": "FBC"})
    ops.call(world, "add_job", {"patient_id": 1, "description": "Chase FBC"})
    depth, frontier, seen = {}, [(start, 0)], set()
    while frontier:
        url, d = frontier.pop(0)
        parts = urlsplit(url)
        key = (parts.path, tuple(sorted(k for k in parts.query.split("&") if k.split("=")[0] in ("tab", "category", "modality", "region", "code", "note_id", "result_id", "analyte", "assessment_form", "referral_pathway", "q", "rx_q", "rx_drug", "rx_product"))))
        if key in seen or d > CLICKS:
            continue
        seen.add(key)
        before = world.one("SELECT max(id) AS id FROM audit")["id"]
        page = c.get(url)
        assert page.status_code == 200, url
        for row in world.q("SELECT DISTINCT op FROM audit WHERE id > ? AND ok", (before,)):
            depth[row["op"]] = min(depth.get(row["op"], 99), d)
        parsed = Page(page.text)
        frontier += [(link, d + 1) for link in parsed.links if not link.startswith(("/reply", "/clock"))]
        for form in parsed.forms:
            if form["method"] == "get":
                frontier.append((f"{form['action']}?{urlencode(fill(form, set()))}", d + 1))
                continue
            name = next((op for pattern, op in POSTS if re.fullmatch(pattern, form["action"])), None)
            assert name, f"a form this test does not know: {form['action']}"
            depth[name] = min(depth.get(name, 99), d + 1)
    return depth


def test_every_operation_is_reachable_from_the_patient_page_within_a_few_clicks():
    depth = reachable("/patients/1")
    for s in sections.SECTIONS:
        limit = PATIENT_CLICKS if s.tab else CLICKS
        for name in s.operations:
            if (name, None) in ui.NOT_IN_UI:  # no page needs it (the clock is on every page)
                continue
            assert depth.get(name, 99) <= limit, (s.name, name, depth.get(name))


chromium_tasks = ["preview-dvt-discharge-judgement", "preview-stemi-judgement", "preview-hypok-judgement", "order-ctpa-001"]


@pytest.mark.parametrize("task_id", chromium_tasks)
def test_the_whole_patient_menu_is_on_the_first_screen_of_every_patient_page(task_id, tmp_path):
    """Measured in Chromium at the screenshot size: every menu entry inside the viewport before any scroll, on every tab
    of every patient of the task, and the header on one line. A screenshot of the DVT patient's page is kept."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from fastapi.testclient import TestClient

    from eprbench.server.app import create_app
    from eprbench.tasks import build_world, load_task

    task = load_task(task_id)
    c = TestClient(create_app(lambda: build_world(task), mode="ui", actor=task.actor, task=task))
    patients = [m.group(1) for m in re.finditer(r'href="/patients/(\d+)"', c.get("/").text)]
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        for pid in sorted(set(patients), key=int):
            for tab in sections.TABS:
                page.set_content(c.get(f"/patients/{pid}?tab={tab}").text)
                boxes = page.evaluate("""() => [...document.querySelectorAll('nav[aria-label="Patient menu"] a')].map(a => {
                    const r = a.getBoundingClientRect(); return [a.textContent.trim(), r.top, r.bottom, r.left, r.right]; })""")
                assert [b[0] for b in boxes] == [s.name for s in sections.PATIENT_MENU]
                for label, top, bottom, left, right in boxes:
                    assert 0 <= top and bottom <= VIEWPORT["height"] and 0 <= left and right <= VIEWPORT["width"], (task_id, pid, tab, label, top, bottom)
                spread = page.evaluate("""() => { const tops = [...document.querySelector('header.app').children].map(e => {
                    const r = e.getBoundingClientRect(); return r.top + r.height / 2; }); return Math.max(...tops) - Math.min(...tops); }""")
                assert spread < 12, (task_id, pid, tab)
        browser.close()
