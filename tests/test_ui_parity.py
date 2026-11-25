"""The web UI can do what the API can, so API and UI runs of a task are comparable."""

import json
import re

import pytest
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from eprbench import ops
from eprbench.server import ui
from tests.conftest import CTPA_ELLISON
from tests.test_server import TOKEN, client


class Page(HTMLParser):
    """The links and forms of a rendered page, as a browser would see them."""

    def __init__(self, html: str):
        super().__init__()
        self.links, self.forms, self._form, self._textarea = [], [], None, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href", "").startswith("/"):
            self.links.append(a["href"])
        elif tag == "form":
            self._form = {"method": a.get("method", "get").lower(), "action": a["action"], "controls": [], "buttons": []}
            self.forms.append(self._form)
        elif self._form is not None and tag == "input" and a.get("name"):
            self._form["controls"].append({"name": a["name"], "type": a.get("type", "text"), "value": a.get("value", ""), "checked": "checked" in a})
        elif self._form is not None and tag == "textarea":
            self._textarea = {"name": a["name"], "type": "textarea", "value": "", "checked": False}
            self._form["controls"].append(self._textarea)
        elif self._form is not None and tag == "button" and a.get("name"):
            self._form["buttons"].append((a["name"], a.get("value", "")))

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea["value"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "textarea":
            self._textarea = None


# What the session types into each free-text or number control, by name. Every control the UI renders must be
# listed here, and every control listed must be rendered somewhere, so a field that appears or disappears fails.
# An empty value leaves an alternative control blank (it would replace the choice made with the radio buttons).
TYPED = {
    "q": "Ellison", "rx_q": "paracetamol", "fluid_hours": "12", "minutes": "1",
    "title": "title-parity", "body": "body-parity", "kind_other": "handover-parity",
    "RR": "18", "SPO2": "96", "SBP": "121", "DBP": "71", "HR": "81", "TEMP": "37.1", "WEIGHT": "70.5", "HEIGHT": "172", "GLUCOSE": "6.2",
    "collect_at": "2026-03-15T07:45", "rp_symptom_onset": "2026-03-14T08:15",
    "kind": "oral-parity", "volume_ml": "210",
    "specialty": "specialty-parity", "reason": "reason-parity", "display": "problem-parity", "onset_date": "2026-03-13",
    "substance": "substance-parity", "reaction": "reaction-parity", "description": "job-parity", "outcome": "outcome-parity",
    "details": "details-parity", "destination": "destination-parity", "note": "note-parity", "dose": "40 mg",
    "dose_value": "2", "frequency_other": "", "frequency_detail": "", "duration": "5 days", "indication": "indication-parity",
    "override_reason": "override-parity", "route": "PO",
    "clinical_details": "clinical-parity", "rf_clinical_question": "question-parity", "rf_egfr": "78", "rf_egfr_date": "2026-03-10",
    "units": "2", "text": "reply-parity",
}  # fmt: skip
# Which option to choose in a group of radio buttons or submit buttons, by name; otherwise the checked or first one.
CHOSEN = {"kind": ("other", "regular", "vte"), "frequency": ("QDS",), "dose_unit": ("tablets",), "status": ("omitted", "draft"), "urgency": ("urgent",),
          "priority": ("urgent",), "severity": ("mild",), "ACVPU": ("A",), "ON_O2": ("1",), "direction": ("in",), "collection": ("scheduled",),
          "task_type": ("bloods",)}  # fmt: skip
NOT_ARGUMENTS = {"next": "where the wait form returns to", "tab": "which tab a search returns to"}
TRANSFORMED = {"minutes": lambda v: str(int(v) * 60), "monitor_minutes": lambda v: str(int(v) * 60)}  # the UI works in minutes, the operations in seconds


def fill(form: dict, seen: set) -> dict:
    data, radios = {}, {}
    for ctl in form["controls"]:
        seen.add(ctl["name"])
        if ctl["type"] in ("hidden", "checkbox"):  # every tick box is ticked
            data[ctl["name"]] = ctl["value"]
        elif ctl["type"] == "radio":
            radios.setdefault(ctl["name"], []).append(ctl)
        else:
            assert ctl["name"] in TYPED, f"{form['action']} has a control {ctl['name']!r} this test does not know: add it to TYPED"
            data[ctl["name"]] = TYPED[ctl["name"]]
    for name, group in list(radios.items()) + [(n, [{"value": v, "checked": False}]) for n, v in form["buttons"]]:
        seen.add(name)
        values = [c["value"] for c in group] if name not in data else []
        pick = next((v for v in CHOSEN.get(name, ()) if v in values), None) or next((c["value"] for c in group if c["checked"]), None) or (values or [None])[0]
        if pick is not None:
            data[name] = pick
    if form["buttons"]:
        name = form["buttons"][0][0]
        data[name] = next((v for v in CHOSEN.get(name, ()) if (name, v) in form["buttons"]), form["buttons"][0][1])
    return data


def signature(url: str) -> tuple:
    """Pages that differ only in the values of their query are the same page, except for the tab, menu
    category or examination they show (each examination has its own request form); patients are told apart by their path."""
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    return parts.path, tuple(sorted((k, query[k][0] if k in ("tab", "category", "code") else "") for k in query))


def ui_session(app_client=None):
    """Crawl the UI from its front pages, following every link and submitting every form a person could.

    Writes are submitted once per kind of form, on Margaret Ellison's record (patient 1), the jobs list and the
    clock; each must succeed. Every successful operation is kept with what the request carried that a person
    chose: the path and query of a link, or the action and the controls of a form."""
    c, app = app_client or client("ui")
    world = app.state.world
    # what some forms need before they can succeed
    ops.call(world, "write_note", {"patient_id": 1, "kind": "discharge_summary", "title": "Discharge summary", "body": "R TKR."})
    ops.call(world, "place_order", {"patient_id": 1, "orderable_code": "GS"})
    ops.call(world, "wait", {"seconds": 3600})
    ops.call(world, "place_order", {"patient_id": 1, "orderable_code": "FBC"})
    for job in ("Chase FBC", "Repeat FBC tomorrow"):  # one to complete and one to cancel
        ops.call(world, "add_job", {"patient_id": 1, "description": job})
    ops.call(world, "write_note", {"patient_id": 1, "title": "Draft plan", "body": "To finish.", "status": "draft"}, ops.Ctx(actor=app.state.task.actor))

    calls, written, seen, closed = [], [], set(), set()

    def request(method: str, url: str, data: dict | None = None):
        before = world.one("SELECT max(id) AS id FROM audit")["id"]
        response = c.request(method, url, data=data, follow_redirects=False)
        assert response.status_code in (200, 303), (method, url, response.status_code)
        parts = urlsplit(url)
        chosen = [(None, v) for v in parts.path.split("/")] + [(k, v) for k, vs in parse_qs(parts.query).items() for v in vs] + list((data or {}).items())
        rows = world.q("SELECT op, args, ok, writes FROM audit WHERE id > ? AND interface = 'ui' ORDER BY id", (before,))
        chosen = [(k, v) for k, v in chosen if v != ""] + [(k, TRANSFORMED[k](v)) for k, v in chosen if k in TRANSFORMED and v != ""]
        calls.extend((r["op"], json.loads(r["args"]), chosen) for r in rows if r["ok"])
        return response, rows

    def submit(form, data):
        _, rows = request("POST", form["action"], data)
        rows = [r for r in rows if r["writes"]]
        assert len(rows) == 1 and rows[0]["ok"], f"{form['action']} with {data}: {rows}"
        written.append((form["action"], data, rows[0]))

    queue, visited, submitted, last = ["/", "/ice", "/jobs"], set(), set(), []
    while queue:
        url = queue.pop(0)
        if signature(url) in visited:
            continue
        visited.add(signature(url))
        response, _ = request("GET", url)
        page = Page(response.text)
        queue += page.links
        for form in page.forms:
            data = fill(form, seen)
            if form["method"] == "get":
                queue.append(f"{form['action']}?{urlencode(data)}")
                continue
            kind = re.sub(r"\d+", "N", form["action"]), tuple(sorted(data))
            mine = form["action"].startswith(("/patients/1/", "/ice/patients/1/", "/jobs/", "/clock/"))
            job = re.fullmatch(r"(/patients/\d+/jobs/\d+)/(complete|cancel)", form["action"])  # each closes the job: one per job
            if not mine or kind in submitted or (job and job[1] in closed):
                continue
            submitted.add(kind)
            closed |= {job[1]} if job else set()
            if form["action"].endswith("/discharge"):
                last.append((form, data))  # after discharge nothing more can be given, so discharge comes last
            else:
                submit(form, data)
    for form, data in last:
        submit(form, data)
    return calls, seen, written


def _matches(value: str, field) -> bool:
    """Did a value a person chose become this argument? Exactly for numbers and flags, as a word within text."""
    if isinstance(field, dict):
        return any(_matches(value, v) for v in field.values())
    if isinstance(field, bool):
        return value.lower() in (("1", "true", "yes") if field else ("0", "false", "no"))
    if isinstance(field, int | float):
        try:
            return float(value) == float(field)
        except ValueError:
            return False
    return isinstance(field, str) and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", field) is not None


def driven(calls) -> set:
    """The (operation, field) pairs some successful UI call took from something a person chose, not a default."""
    out = set()
    for op, args, chosen in calls:
        validated = ops.REGISTRY[op].args_model.model_validate(args).model_dump()
        # a number in the path (/patients/1/...) can only be an identifier
        out |= {(op, field) for field, value in validated.items()
                if value is not None and any(_matches(v, value) for k, v in chosen if k is not None or field.endswith("_id"))}
    return out


def gaps(calls) -> list:
    """Operation fields no person's choice in the UI reaches, less those exempted in ui.NOT_IN_UI."""
    used, covered = {op for op, _, _ in calls}, driven(calls)
    missing = []
    for name, o in ops.REGISTRY.items():
        if name not in used and (name, None) not in ui.NOT_IN_UI:
            missing.append((name, None))
        missing += [(name, f) for f in o.args_model.model_fields if name in used and (name, f) not in covered and (name, f) not in ui.NOT_IN_UI]
    return missing


def test_what_the_ui_submits_reaches_the_operation():
    _, seen, written = ui_session()
    for action, data, row in written:
        validated = json.dumps(ops.REGISTRY[row["op"]].args_model.model_validate(json.loads(row["args"])).model_dump())
        for name, value in data.items():
            if value == "" or name in NOT_ARGUMENTS or (name, value) == ("kind", "other"):
                continue
            assert TRANSFORMED.get(name, str)(value) in validated, f"{action}: {name}={value!r} did not reach {row['op']}: {validated}"
    assert not set(TYPED) - seen and not set(CHOSEN) - seen, f"controls no longer rendered: {(set(TYPED) | set(CHOSEN)) - seen}"


def test_every_operation_field_is_set_by_a_ui_control_or_exempt_with_a_reason():
    """Each operation field takes its value, in some successful call, from a link or control a person chose; a
    handler's default does not count. Otherwise it must be listed in ui.NOT_IN_UI, saying whether it is a
    difference in capability."""
    for (name, field), reason in ui.NOT_IN_UI.items():
        assert name in ops.REGISTRY and (field is None or field in ops.REGISTRY[name].args_model.model_fields), (name, field)
        assert reason.startswith(("Capability difference: ", "No capability difference: ")), (name, field)
    missing = gaps(ui_session()[0])
    assert not missing, f"no UI control sets these operation fields; add a control or list them in ui.NOT_IN_UI: {missing}"


@pytest.mark.parametrize(
    "template, control, field",
    [
        ("ice_patient.html", "{{ radios('urgency', [('routine', 'Routine'), ('urgent', 'Urgent')], 'routine', 'blood-urgency') }}", ("request_blood_product", "urgency")),
        ("patient.html", "{{ radios('urgency', [('routine', 'Routine'), ('urgent', 'Urgent')], 'routine', 'referral-urgency') }}", ("make_referral", "urgency")),
    ],
)
def test_the_parity_check_notices_a_missing_selector(monkeypatch, template, control, field):
    """Mutation test: with the selector removed from its form, the handler's default fills the field and the
    check must still report it."""
    from jinja2 import FunctionLoader

    original = ui.templates.env.loader

    def without_control(name):
        source, filename, _ = original.get_source(ui.templates.env, name)
        if name == template:
            assert control in source, "the selector this test removes has changed; update the test"
            source = source.replace(control, "")
        return source, filename, lambda: False

    monkeypatch.setattr(ui.templates.env, "loader", FunctionLoader(without_control))
    ui.templates.env.cache.clear()
    try:
        assert field in gaps(ui_session()[0])
    finally:
        ui.templates.env.cache.clear()


def test_every_dose_unit_the_api_accepts_is_offered_in_the_ui():
    offered = set(ui.UNIT_CHOICES)
    for unit in ops.DOSE_UNITS:
        assert unit in offered or f"{unit}s" in offered or f"{unit}es" in offered or f"{unit[:-1]}ies" in offered, unit
    c, app = client("ui")
    product = "Potassium chloride 0.15% (potassium 20mmol/1litre) / Sodium chloride 0.9% infusion 1litre bags"
    html = c.get("/patients/2", params={"tab": "medications", "rx_drug": "Potassium chloride + Sodium chloride", "rx_product": product}).text
    assert 'name="dose_unit" value="L"' in html
    rx = {"drug": "Potassium chloride + Sodium chloride", "product": product, "dose_value": "1", "dose_unit": "L", "route": "IV", "frequency_other": "over 8 hours", "kind": "regular"}
    html = c.post("/patients/2/medications", data=rx).text
    assert 'role="alert"' not in html and "<td>1 L</td>" in html


def test_a_reason_given_for_an_omitted_dose_in_the_ui_is_recorded():
    """Review item A8: the UI dropped the note, so a justified omission of a critical medicine became a finding."""
    c, app = client("ui", "oncall-jobs-001")
    html = c.post("/patients/1/medications/1/administrations", data={"status": "omitted", "note": "CTPA pending, ?switch"}).text
    assert "omitted (CTPA pending, ?switch)" in html
    assert app.state.world.one("SELECT note FROM administration ORDER BY id DESC LIMIT 1")["note"] == "CTPA pending, ?switch"
    findings = c.post("/harness/grade", headers={"X-Harness-Token": TOKEN}).json()["safety_findings"]
    assert not [f for f in findings if f["code"] == "critical_dose_omitted"]


def test_a_result_or_note_of_another_patient_is_never_shown_under_this_patients_banner():
    c, _ = client("ui")
    assert "Haemoglobin" in c.get("/ice/patients/1?tab=results&result_id=2").text
    html = c.get("/ice/patients/2?tab=results&result_id=2").text  # result 2 is Ellison's; patient 2 is Elliston
    assert "ELLISTON, Margaret" in html and 'id="result-view"' not in html and "not on this patient" in html
    assert "NEWS 9" in c.get("/patients/1?tab=notes&note_id=5").text
    html = c.get("/patients/2?tab=notes&note_id=5").text
    assert 'id="note-view"' not in html and "NEWS 9" not in html and "not on this patient" in html


def test_ui_actions_on_another_patients_objects_are_refused():
    c, app = client("ui")
    w = app.state.world
    assert w.one("SELECT patient_id FROM medication WHERE id = 1")["patient_id"] == 1
    before = w.one("SELECT count(*) AS n FROM administration")["n"]
    html = c.post("/patients/2/medications/1/administrations", data={"status": "given"}).text
    assert "not on this patient" in html and w.one("SELECT count(*) AS n FROM administration")["n"] == before
    assert "not on this patient" in c.post("/patients/2/medications/1/stop", data={"reason": "x"}).text
    assert w.one("SELECT stopped_at FROM medication WHERE id = 1")["stopped_at"] is None
    assert "not on this patient" in c.post("/ice/patients/2/results/2/acknowledge").text
    assert w.one("SELECT acknowledged_at FROM result WHERE id = 2")["acknowledged_at"] is None
    c.post("/ice/patients/1/requests", data={"orderable_code": "FBC"})
    order = w.one('SELECT max(id) AS id FROM "order"')["id"]
    assert "not on this patient" in c.post(f"/ice/patients/2/requests/{order}/cancel").text
    assert w.one('SELECT status FROM "order" WHERE id = ?', (order,))["status"] == "active"


def test_ui_can_record_problem_onset_and_other_note_types():
    c, app = client("ui")
    c.post("/patients/1/problems", data={"display": "Pulmonary embolism", "onset_date": "2026-03-13"})
    assert app.state.world.one("SELECT onset_date FROM problem WHERE display = 'Pulmonary embolism'")["onset_date"] == "2026-03-13"
    c.post("/patients/1/notes", data={"kind": "other", "kind_other": "handover", "title": "Weekend handover", "body": "Plan."})
    assert app.state.world.one("SELECT kind FROM note WHERE title = 'Weekend handover'")["kind"] == "handover"


def test_ui_fluid_balance_and_monitor_can_look_further_back():
    c, _ = client("ui", "jobs-list-001")
    assert "Balance <strong>+700 mL</strong>" in c.get("/patients/4?tab=fluids").text
    assert re.search(r"In 300 mL", c.get("/patients/4?tab=fluids&fluid_hours=3").text)
    c, _ = client("ui")
    rows = lambda html: len(re.findall(r"<tr><td>2026-", html.split("Last ")[1]))  # noqa: E731
    assert rows(c.get("/patients/3?tab=monitor&monitor_minutes=30").text) > rows(c.get("/patients/3?tab=monitor").text)


def test_the_reply_page_offers_exactly_what_the_submit_tool_and_endpoint_take():
    """The answer form is a surface too: each field of the submit schema has its control, with the same options and
    units, and the page has no control the schema lacks."""
    from fastapi.testclient import TestClient

    from eprbench import answers
    from eprbench.server.app import create_app
    from eprbench.tasks import build_world
    from tests.test_typed_answers import TASK

    c = TestClient(create_app(lambda: build_world(TASK), mode="ui", actor=TASK.actor, task=TASK))
    (form,) = [f for f in Page(c.get("/reply").text).forms if f["action"] == "/reply"]
    offered = {}
    for ctl in form["controls"]:
        offered.setdefault(ctl["name"], []).append(ctl["value"] if ctl["type"] == "radio" else ctl["type"])
    schema = answers.submit_tool(TASK)["input_schema"]["properties"]["fields"]["properties"]
    expected = {answers.TEXT_CONTROL: ["textarea"]}
    for name, s in schema.items():
        if s["type"] == "object":
            expected[answers.control(name)] = ["text"]
            expected[answers.unit_control(name)] = s["properties"]["unit"]["enum"]
        else:
            expected[answers.control(name)] = s.get("enum") or (["yes", "no"] if s["type"] == "boolean" else ["textarea"])
    assert offered == expected


def test_the_header_stays_on_one_line_at_the_screenshot_width():
    """Every screenshot agent sees the header: adding the reply link must not wrap it at 1280 px, on any page."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from fastapi.testclient import TestClient

    from eprbench.agents.claude_browser import VIEWPORT
    from eprbench.server.app import create_app
    from eprbench.tasks import build_world, load_task

    task = load_task("order-ctpa-001")
    with_link = TestClient(create_app(lambda: build_world(task), mode="ui", actor=task.actor, task=task))
    without = TestClient(create_app(lambda: build_world(task), mode="ui", actor=task.actor))
    paths = ["/", "/ice", "/jobs", "/patients/1", "/patients/1?tab=medications", "/ice/patients/1", "/ice/patients/1?tab=results"]
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        page = chromium.new_page(viewport=VIEWPORT)

        def header(html):
            page.set_content(html)
            return page.evaluate("""() => { const h = document.querySelector('header.app');
                const tops = [...h.children].map(e => { const r = e.getBoundingClientRect(); return r.top + r.height / 2; });
                return [h.getBoundingClientRect().height, Math.max(...tops) - Math.min(...tops)]; }""")

        for path in paths:
            (height, spread), (before, _) = header(with_link.get(path).text), header(without.get(path).text)
            assert 'href="/reply"' in with_link.get(path).text and height == before and spread < 12, (path, height, before, spread)
        height, spread = header(with_link.get("/reply").text)
        assert spread < 12 and height < 60
        # with the Wait buttons (1, 5, 15, 30, 60 minutes) and a longer user name, too
        stemi = load_task("preview-stemi-judgement")
        longer = TestClient(create_app(lambda: build_world(stemi), mode="ui", actor=stemi.actor, task=stemi))
        for path in ["/", "/ice", "/jobs", "/patients/3", "/ice/patients/3?tab=results", "/reply"]:
            assert header(longer.get(path).text)[1] < 12, path
        chromium.close()

