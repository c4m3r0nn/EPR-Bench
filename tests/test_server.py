import re

import pytest
from fastapi.testclient import TestClient

from eprbench import ops, sections
from eprbench.server.app import create_app
from eprbench.tasks import build_world, load_task
from tests.conftest import CTPA_ELLISON, CTPA_ELLISTON, ELLISON

TOKEN = "s3cret"


def client(mode="both", task_id="order-ctpa-001"):
    task = load_task(task_id)
    app = create_app(lambda: build_world(task), mode=mode, actor=task.actor, task=task, harness_token=TOKEN)
    return TestClient(app, follow_redirects=True), app


def test_api_exposes_every_operation():
    c, _ = client("api")
    assert {s["name"] for s in sections.operations(c.get("/api/ops").json())} == set(ops.REGISTRY)
    r = c.post("/api/ops/search_patients", json={"query": "Whitcombe"})
    assert r.status_code == 200 and r.json()["result"]["patients"][0]["mrn"] == "H5120986"
    assert c.post("/api/ops/get_current_time").json()["result"]["now"] == "2026-03-14T09:30:00Z"


def test_api_errors():
    c, _ = client("api")
    assert c.post("/api/ops/get_patient", json={"patient_id": 999}).status_code == 404
    r = c.post("/api/ops/get_patient", json={"patient": 1})
    assert r.status_code == 422 and r.json()["error"] == "invalid_arguments"
    assert c.post("/api/ops/get_patient", content="{not json").status_code == 400
    assert c.post("/api/ops/nope").status_code == 404


def test_ui_mode_has_no_api_and_api_mode_has_no_ui():
    ui, _ = client("ui")
    assert ui.get("/api/ops").status_code == 404
    assert ui.post("/api/ops/get_current_time").status_code == 404
    assert ui.get("/openapi.json").status_code == 404
    assert ui.get("/").status_code == 200
    api, _ = client("api")
    assert api.get("/").status_code == 404
    assert api.get("/patients/1").status_code == 404


def test_ui_pages_contain_no_script_or_api_reference():
    c, _ = client("ui")
    urls = ["/", "/?q=Margaret", "/ice", "/ice?q=Margaret"]
    urls += [f"/patients/1?tab={t}" for t in ("summary", "notes", "observations", "medications", "monitor")]
    urls += [f"/ice/patients/1?tab={t}" for t in ("request", "requests", "results")]
    for url in urls:
        r = c.get(url)
        assert r.status_code == 200 and "<script" not in r.text and "/api/" not in r.text, url
        assert "£" not in r.text and "turnaround" not in r.text.lower(), url
        # native dropdown popups are invisible in page screenshots, so every select must be an in-page list box
        assert "<select" not in r.text.replace("<select name=\"orderable_code\" id=\"order-item\" size=", "").replace("<select name=\"drug\" id=\"med-drug\" size=", ""), url
        assert "<datalist" not in r.text, url


def test_ui_search_and_navigation():
    c, _ = client("ui")
    html = c.get("/?q=Margaret").text
    assert "ELLISON, Margaret" in html and "ELLISTON, Margaret" in html
    html = c.get("/patients/1").text
    assert "999 000 0018" in html and "Penicillin" in html and "Ward 7B" in html
    assert "NEWS 9" in c.get("/patients/1?tab=notes&note_id=5").text
    assert c.get("/patients/999").status_code == 404


def test_requesting_is_a_separate_system_without_patient_context():
    c, _ = client("ui")
    record = c.get("/patients/1").text
    assert 'href="/ice"' in record and "/ice/patients" not in record  # no deep link: the patient must be found again
    assert "Haemoglobin" not in c.get("/patients/1?tab=results").text
    ice = c.get("/ice?q=H4417302").text
    assert "ICE Requesting and Reporting" in ice and 'href="/ice/patients/1"' in ice
    assert "Haemoglobin" in c.get("/ice/patients/1?tab=results&result_id=2").text
    assert c.get("/ice/patients/999").status_code == 404


def test_ctpa_task_can_be_completed_through_the_ui_alone():
    c, app = client("ui")
    form = {"orderable_code": "CTPA", "priority": "urgent", "clinical_details": "D5 post R TKR, pleuritic chest pain, hypoxia ?PE", **{f"rf_{k}": v for k, v in CTPA_ELLISON.items()}}
    html = c.post("/ice/patients/1/requests", data=form).text
    assert "CT Pulmonary angiogram" in html and "in progress" in html
    report = c.post("/harness/grade", headers={"X-Harness-Token": TOKEN}).json()
    assert report["success"] and report["score"] == 1.0
    audit = app.state.world.one("SELECT * FROM audit WHERE op = 'place_order'")
    assert audit["interface"] == "ui"


def test_ui_shows_operation_errors():
    c, _ = client("ui")
    html = c.post("/ice/patients/1/requests", data={"orderable_code": "CTPA", "clinical_details": "", "rf_egfr": "78"}).text
    assert 'role="alert"' in html and "request form is incomplete" in html and "Clinical summary" in html
    assert 'name="rf_egfr" id="order-egfr" value="78"' in html  # what was entered is kept


def test_ui_write_flows():
    c, _ = client("ui")
    assert "FY1 review" in c.post("/patients/1/notes", data={"kind": "progress", "title": "FY1 review", "body": "Seen."}).text
    html = c.post("/patients/1/observations", data={"HR": "112", "SBP": "110", "RR": "", "ACVPU": "A", "ON_O2": "1"}).text
    assert re.search(r"<td>112</td>", html)
    html = c.post("/patients/1/medications/1/stop", data={"reason": "treatment dose started"}).text
    assert "is not in the formulary" in c.post("/patients/1/medications", data={"drug": "Unobtainium", "dose": "1", "route": "PO", "frequency": "OD"}).text
    assert "treatment dose started" in html
    rx = {"drug": "Apixaban", "product": "Apixaban 5mg tablets", "dose_value": "10", "dose_unit": "mg", "route": "PO", "frequency": "BD", "indication": "PE"}
    html = c.post("/patients/1/medications", data=rx).text
    assert "<td>Apixaban</td><td>10 mg</td>" in html and 'role="alert"' not in html


def test_ui_wait_advances_clock_and_reveals_results():
    c, _ = client("ui")
    c.post("/ice/patients/3/requests", data={"orderable_code": "ECG"})
    assert "12-lead ECG" not in c.get("/ice/patients/3?tab=results").text
    html = c.post("/clock/wait", data={"minutes": "10", "next": "/ice/patients/3?tab=requests"}).text
    assert "2026-03-14 09:40" in html and "resulted" in html
    result_id = re.search(r"result_id=(\d+)", html).group(1)
    assert "ST elevation" in c.get(f"/ice/patients/3?tab=results&result_id={result_id}").text
    r = c.post("/clock/wait", data={"minutes": "5", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_monitor_tab_depends_on_location():
    c, _ = client("ui")
    assert 'id="live-HR"' in c.get("/patients/3?tab=monitor").text
    assert "not attached to a bedside monitor" in c.get("/patients/1?tab=monitor").text


def test_ui_and_api_see_the_same_record():
    c, _ = client("both")
    api = c.post("/api/ops/list_medications", json={"patient_id": 1}).json()["result"]["medications"]
    html = c.get("/patients/1?tab=medications").text
    assert api and all(m["drug"] in html and m["dose"] in html for m in api)


@pytest.mark.parametrize("headers", [{}, {"X-Harness-Token": "wrong"}])
def test_harness_requires_token(headers):
    c, _ = client("both")
    assert c.post("/harness/grade", headers=headers).status_code == 404
    assert c.post("/harness/reset", headers=headers).status_code == 404


def test_harness_reset_and_answer_grading():
    c, _ = client("api", "dx-chestpain-001")
    auth = {"X-Harness-Token": TOKEN}
    c.post("/api/ops/place_order", json={"patient_id": 3, "orderable_code": "ECG"})
    report = c.post("/harness/grade", headers=auth, json={"answer": "inferior STEMI"}).json()
    assert report["success"]
    assert c.post("/harness/reset", headers=auth).json() == {"ok": True}
    assert not c.post("/harness/grade", headers=auth, json={"answer": "inferior STEMI"}).json()["success"]


def test_ui_allergy_alert_keeps_the_draft_and_can_be_overridden():
    c, app = client("ui")
    rx = {"drug": "Amoxicillin", "product": "Amoxicillin 500mg capsules", "dose_value": "500", "dose_unit": "mg", "route": "PO", "frequency": "TDS",
          "kind": "regular", "duration": "5 days", "indication": "LRTI"}
    html = c.post("/patients/1/medications", data=rx).text
    assert "ALLERGY ALERT" in html and "<h3>Amoxicillin 500mg capsules</h3>" in html and 'value="5 days"' in html and 'value="TDS" checked' in html
    assert not app.state.world.one("SELECT 1 FROM medication WHERE drug = 'Amoxicillin'")
    html = c.post("/patients/1/medications", data={**rx, "override_reason": "previous tolerance documented"}).text
    assert "ALLERGY ALERT" not in html and app.state.world.one("SELECT 1 FROM medication WHERE drug = 'Amoxicillin'")


def test_ui_drug_chart_referral_and_discharge_flows():
    c, app = client("ui")
    assert "2026-03-13 18:05 given" in c.get("/patients/1?tab=medications").text
    html = c.post("/patients/1/medications/1/administrations", data={"status": "omitted"}).text
    assert "2026-03-14 09:30 omitted" in html
    html = c.post("/patients/1/referrals", data={"specialty": "Medical registrar", "urgency": "urgent", "reason": "Confirmed PE"}).text
    assert "Medical registrar" in html and "Confirmed PE" in html
    assert "Pulmonary embolism" in c.post("/patients/1/problems", data={"display": "Pulmonary embolism"}).text
    assert "Latex (contact dermatitis)" in c.post("/patients/1/allergies", data={"substance": "Latex", "reaction": "contact dermatitis", "severity": ""}).text
    assert "write a discharge summary" in c.post("/patients/1/discharge", data={"destination": "Usual residence"}).text
    c.post("/patients/1/notes", data={"kind": "discharge_summary", "title": "Discharge summary", "body": "R TKR."})
    html = c.post("/patients/1/discharge", data={"destination": "Usual residence"}).text
    assert "Not currently admitted" in html and 'id="discharge-form"' not in html


def test_ui_acknowledge_result_and_record_assessment():
    c, _ = client("ui", "act-on-result-001")
    listing = c.get("/ice/patients/4?tab=results").text
    assert "<th>Acknowledged</th>" in listing and ">No<" in listing
    html = c.post("/ice/patients/4/results/6/acknowledge").text
    assert 'id="result-acknowledged"' in html and "Acknowledge result</button>" not in html
    assert "already acknowledged" in c.post("/ice/patients/4/results/6/acknowledge").text
    html = c.post("/patients/4/assessments", data={"kind": "vte", "outcome": "High risk", "details": "Sepsis"}).text
    assert "High risk" in html and "Sepsis" in html


def test_ui_shows_prescribing_hard_stops():
    c, _ = client("ui", "medrec-withhold-001")
    rx = {"drug": "Methotrexate", "product": "Methotrexate 2.5mg tablets", "dose_value": "15", "dose_unit": "mg", "route": "PO", "frequency": "OD", "kind": "regular"}
    html = c.post("/patients/4/medications", data=rx).text
    assert "once WEEKLY" in html and 'value="PO" checked' in html  # the draft, including the route, is kept


def test_event_log_records_wall_clock_timing_outside_the_world(tmp_path):
    import json

    task = load_task("order-ctpa-001")
    log = tmp_path / "events.jsonl"
    app = create_app(lambda: build_world(task), mode="ui", actor="Dr Human", task=task, event_log=str(log))
    c = TestClient(app)
    before = app.state.world.snapshot()
    c.get("/patients/1?tab=notes")
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert entries[0]["path"] == "/patients/1" and entries[0]["user"] == "Dr Human" and entries[0]["wall_time"] > 1e9
    assert "Dr Human" in c.get("/").text
    # only audit rows were added: no wall-clock value entered the database
    assert "wall" not in " ".join(r["sql"] or "" for r in app.state.world.q("SELECT sql FROM sqlite_master"))
    assert before != app.state.world.snapshot()


def test_ui_contrast_alert_keeps_the_request_draft():
    c, app = client("ui", "contrast-renal-001")
    form = {"orderable_code": "CTPA", "priority": "urgent", "clinical_details": "D2 post hemiarthroplasty, breathless ?PE", **{f"rf_{k}": v for k, v in CTPA_ELLISTON.items()}}
    html = c.post("/ice/patients/2/requests", data=form).text
    assert "CONTRAST ALERT" in html and 'value="CTPA"' in html and "breathless ?PE</textarea>" in html and 'value="urgent" checked' in html
    assert not app.state.world.one('SELECT 1 FROM "order"')
    html = c.post("/ice/patients/2/requests", data={**form, "override_reason": "Discussed with radiology"}).text
    assert "CT Pulmonary angiogram" in html and "CONTRAST ALERT" not in html


def test_ui_fluids_jobs_and_blood():
    c, app = client("ui", "jobs-list-001")
    html = c.get("/patients/4?tab=fluids").text
    assert "Balance <strong>+700 mL</strong>" in html
    html = c.post("/patients/4/fluids", data={"direction": "out", "kind": "urine", "volume_ml": "450"}).text
    assert "Balance <strong>+250 mL</strong>" in html

    jobs = c.get("/jobs").text
    assert "Chase this morning" in jobs and jobs.index("ELLISTON") < jobs.index("BRIGHTWELL")  # urgent first
    done = c.post("/patients/2/jobs/2/complete", data={"outcome": "Hb 71"}).text
    assert "Done by" in done and "Hb 71" in done
    assert "already been marked as done" in c.post("/patients/2/jobs/2/complete", data={"outcome": "again"}).text
    assert app.state.world.one("SELECT patient_id FROM job WHERE id = 1")["patient_id"] == 4
    assert "not on this patient" in c.post("/patients/2/jobs/1/complete", data={"outcome": "wrong patient"}).text
    assert app.state.world.one("SELECT done_at FROM job WHERE id = 1")["done_at"] is None

    assert "Check wound" in c.post("/patients/2/jobs", data={"description": "Check wound", "priority": "routine"}).text
    assert 'href="/jobs"' in c.get("/").text and 'href="/jobs"' not in c.get("/ice").text

    blood = {"product": "red cells", "units": "1", "urgency": "routine", "indication": "Hb 71"}
    assert "no reported group and save" in c.post("/ice/patients/2/blood", data=blood).text
    c.post("/ice/patients/2/requests", data={"orderable_code": "GS"})
    c.post("/clock/wait", data={"minutes": "60", "next": "/ice"})
    html = c.post("/ice/patients/2/blood", data=blood).text
    assert "<td>red cells</td><td>1</td>" in html


def test_imaging_is_requested_through_menus():
    """Category, then modality, then body region, then the examination's own form, as in a real requesting system."""
    import re

    c, _ = client("ui")
    links = lambda html: re.findall(r'<li><a href="([^"]+)">([^<]+)</a>', html)  # noqa: E731
    top = c.get("/ice/patients/1?tab=request").text
    assert [t for _, t in links(top)] == ["Laboratory tests", "Imaging (radiology)", "Cardiology"]
    modalities = dict((t, h) for h, t in links(c.get(dict((t, h) for h, t in links(top))["Imaging (radiology)"].replace("&amp;", "&")).text))
    assert {"XR", "US", "CT", "MRI", "Nuclear medicine", "Fluoroscopy", "Interventional"} <= set(modalities)
    regions = dict((t, h) for h, t in links(c.get(modalities["CT"].replace("&amp;", "&")).text))
    assert {"Head and neck", "Chest", "Abdomen and pelvis", "Vascular"} <= set(regions)
    exams_html = c.get(regions["Chest"].replace("&amp;", "&")).text
    exams = dict((t, h) for h, t in links(exams_html))
    assert "CT Pulmonary angiogram" in exams and "CT Head (non-contrast)" not in exams
    form = c.get(exams["CT Pulmonary angiogram"].replace("&amp;", "&")).text
    for field in ("Clinical summary", "Clinical question", "Latest eGFR", "Date of that eGFR", "Previous reaction to contrast", "Mobility"):
        assert field in form, field
    assert "Pregnancy status" not in form and "Side" not in form  # a 67-year-old woman; not a limb
    assert "&rsaquo; <a" in form and "CT Pulmonary angiogram" in form  # breadcrumb back up the menus


def test_request_forms_depend_on_the_examination_and_the_patient(world, call, pid):
    form = lambda mrn, code: [f["name"] for f in call("get_request_form", patient_id=pid(mrn), orderable_code=code)["fields"]]  # noqa: E731
    assert "laterality" in form(ELLISON, "US-DOPPLER-LL") and "egfr" not in form(ELLISON, "US-DOPPLER-LL")
    assert {"mri_device", "mri_metal"} <= set(form(ELLISON, "MR-KNEE")) and "pregnancy" not in form(ELLISON, "MR-KNEE")
    assert form(ELLISON, "FBC") == ["clinical_details"]
    young = world.insert("patient", mrn="T1", nhs_number="9990000050", family_name="Test", given_name="Ann", dob="1996-01-01", sex="F")
    assert "pregnancy" in [f["name"] for f in call("get_request_form", patient_id=young, orderable_code="CTPA")["fields"]]
    assert "pregnancy" not in [f["name"] for f in call("get_request_form", patient_id=young, orderable_code="US-ABDO")["fields"]]  # no ionising radiation


def test_prescribing_is_done_through_search_then_drug_then_product():
    import re

    c, app = client("ui", "rx-insulin-stat-001")
    links = lambda html: dict((t, h.replace("&amp;", "&")) for h, t in re.findall(r'<li><a href="([^"]+)">([^<]+)</a>', html))  # noqa: E731
    start = c.get("/patients/4?tab=medications").text
    assert 'id="rx-search"' in start and 'id="prescribe-form"' not in start
    drugs = links(c.get("/patients/4?tab=medications&rx_q=insulin").text)
    assert {"Insulin glargine", "Insulin soluble human"} <= set(drugs) and len(drugs) > 10  # the real formulary
    products = links(c.get(drugs["Insulin soluble human"]).text)
    assert "Insulin soluble human 100units/ml solution for injection 10ml vials" in products
    form = c.get(products["Insulin soluble human 100units/ml solution for injection 10ml vials"]).text
    assert 'name="route" value="SC"' in form and 'name="route" value="IV"' in form and 'name="route" value="PO"' not in form
    rx = {"drug": "Insulin soluble human", "product": "Insulin soluble human 100units/ml solution for injection 10ml vials", "route": "SC",
          "dose_value": "4", "dose_unit": "units", "frequency": "once", "kind": "stat", "indication": "CBG 17.2"}
    html = c.post("/patients/4/medications", data=rx).text
    assert 'role="alert"' not in html
    stored = app.state.world.one("SELECT drug, product, dose, route, frequency, kind FROM medication WHERE patient_id = 4 AND drug LIKE 'Insulin%'")
    assert stored == {"drug": "Insulin soluble human", "product": rx["product"], "dose": "4 units", "route": "SC", "frequency": "once", "kind": "stat"}
    weekly = {"drug": "Folic acid", "product": "Folic acid 5mg tablets", "route": "PO", "dose_value": "5", "dose_unit": "mg", "frequency": "once weekly", "frequency_detail": "Thursdays"}
    c.post("/patients/4/medications", data=weekly)
    assert app.state.world.one("SELECT frequency FROM medication WHERE drug = 'Folic acid'")["frequency"] == "once weekly (Thursdays)"


def test_ui_shows_the_dose_check_alert_and_takes_an_override():
    c, app = client("ui", "medrec-withhold-001")
    rx = {"drug": "Naproxen", "product": "Naproxen sodium 275mg tablets", "dose_value": "2", "dose_unit": "tablets", "route": "PO", "frequency": "BD", "kind": "regular"}
    assert "DOSE CHECK ALERT" in c.post("/patients/4/medications", data=rx).text
    assert not app.state.world.one("SELECT 1 FROM medication WHERE drug = 'Naproxen'")
    c.post("/patients/4/medications", data={**rx, "override_reason": "checked in the BNF"})
    assert app.state.world.one("SELECT overridden_alert FROM medication WHERE drug = 'Naproxen'")["overridden_alert"] == "dose_unchecked_alert"
