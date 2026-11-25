"""Runtime v2: results that persist, longitudinal history, staff responses, structured treatment recognition,
interface-neutral effort and evidence, and equal wrong-patient protection on every surface."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eprbench import ops, scenario, sections
from eprbench.env import EPREnv
from eprbench.lint import lint_bundle
from eprbench.loader import Bundle, load_bundle, read_bundle
from eprbench.server.app import create_app
from eprbench.tasks import build_world, load_task
from eprbench.world import World, parse_iso
from tests.conftest import ELLISON, WHITCOMBE, calling, with_product

FIXTURE = Path(__file__).parent / "fixtures" / "history.json"
H = 1  # the fixture's only patient


def history_world(now: str = "2026-05-02T09:00:00Z", raw: dict | None = None) -> World:
    world = World.create(now=parse_iso(now))
    load_bundle(world, Bundle.model_validate(raw) if raw is not None else Bundle.model_validate_json(FIXTURE.read_text()))
    return world


def fixture_raw() -> dict:
    return json.loads(FIXTURE.read_text())


def call(world, name, **args):
    return calling(world, name, args)


def latest_result(world, patient, code):
    rows = [r for r in call(world, "list_results", patient_id=patient)["results"] if r["orderable_code"] == code]
    return call(world, "get_result", result_id=rows[0]["result_id"], patient_id=patient)


def values(result):
    return {i["analyte"]: i["value"] for i in result["items"]}


# --- 1. repeat results persist -----------------------------------------------------------------------------------


def test_repeat_requests_inside_one_window_agree(world, pid):
    """The live-run defect: a second FBC came back with the catalogue normal (Hb 138) after a post-op Hb of 109."""
    patient = pid(ELLISON)
    for _ in range(3):
        call(world, "place_order", patient_id=patient, orderable_code="FBC")
        call(world, "wait", seconds=3600)
    fbcs = [call(world, "get_result", result_id=r["result_id"]) for r in call(world, "list_results", patient_id=patient)["results"] if r["orderable_code"] == "FBC"]
    assert [values(r)["Haemoglobin"] for r in fbcs] == [104, 104, 104, 109]


def test_windows_change_what_a_test_shows_over_time():
    world = history_world()  # 09:00: before the first window opens at 12:00
    call(world, "place_order", patient_id=H, orderable_code="FBC")
    call(world, "wait", seconds=4 * 3600)  # 13:00, inside the first window
    assert values(latest_result(world, H, "FBC")) == {"Haemoglobin": 92, "Platelets": 210}  # each analyte's latest value
    call(world, "place_order", patient_id=H, orderable_code="FBC")
    call(world, "wait", seconds=12 * 3600)  # 01:00 next day, inside the open-ended window
    assert values(latest_result(world, H, "FBC"))["Haemoglobin"] == 90
    call(world, "place_order", patient_id=H, orderable_code="FBC")
    call(world, "wait", seconds=3600)
    assert values(latest_result(world, H, "FBC"))["Haemoglobin"] == 88


def test_outside_every_window_a_test_falls_back_to_the_last_value_unless_the_bundle_says_normal():
    world = history_world()
    call(world, "place_order", patient_id=H, orderable_code="CRP")  # CRP declares fallback "normal"
    call(world, "wait", seconds=3600)
    assert values(latest_result(world, H, "CRP")) == {"CRP": 3}


def test_a_scenario_response_replaces_the_window_from_when_it_takes_effect():
    world = history_world("2026-05-02T13:00:00Z")
    call(world, "request_blood_product", patient_id=H, product="red cells", units=1, indication="test")
    call(world, "place_order", patient_id=H, orderable_code="FBC")  # sampled before the transfusion
    call(world, "wait", seconds=2 * 3600)  # the response (transfusion) takes effect at 15:00
    call(world, "place_order", patient_id=H, orderable_code="FBC")
    call(world, "wait", seconds=24 * 3600)  # well into the window that the response closed
    call(world, "place_order", patient_id=H, orderable_code="FBC")
    call(world, "wait", seconds=3600)
    fbcs = [values(call(world, "get_result", result_id=r["result_id"]))["Haemoglobin"]
            for r in call(world, "list_results", patient_id=H)["results"] if r["orderable_code"] == "FBC"]  # fmt: skip
    assert fbcs[:3] == [104, 104, 90]


def test_the_sample_is_taken_when_requested_and_reported_a_turnaround_later(world, pid):
    patient = pid(ELLISON)
    order = call(world, "place_order", patient_id=patient, orderable_code="FBC")
    row = world.one("SELECT performed_at, reported_at FROM result WHERE order_id = ?", (order["order_id"],))
    assert row["performed_at"] == world.now and row["reported_at"] == world.now + 3600
    assert order["order_id"] not in [o["order_id"] for o in call(world, "list_orders", patient_id=patient)["orders"] if o["status"] == "resulted"]


def test_lint_flags_abnormal_history_without_an_open_ended_window():
    raw = json.loads(FIXTURE.read_text())
    problems = "\n".join(lint_bundle(Bundle.model_validate(raw)))
    assert "CRP was abnormal in the past but has no open-ended result window, so a repeat would come back normal" in problems
    assert "FBC was abnormal" not in problems and "UE was abnormal" not in problems
    raw["patients"][0]["result_windows"] = [w for w in raw["patients"][0]["result_windows"] if w.get("valid_to") or w["orderable_code"] != "FBC"]
    problems = "\n".join(lint_bundle(Bundle.model_validate(raw)))
    assert "FBC was abnormal in the past but has no open-ended result window, so a repeat would repeat the last value (until it is too old" in problems


def test_older_bundles_load_unchanged():
    """latent_results is still accepted, as an open-ended window."""
    demo = read_bundle("demo")
    assert demo.patients[0].latent_results and not demo.patients[0].result_windows


# --- 2. longitudinal history -------------------------------------------------------------------------------------


def test_cumulative_results_span_admissions_attendances_and_the_gp():
    world = history_world()
    out = call(world, "get_analyte_history", patient_id=H, analyte="creatinine")
    assert out["analyte"] == "Creatinine"
    series = [(s["sampled_at"][:10], s["value"], s["flag"], s["source"], s["setting"]) for s in out["series"]]
    assert series == [
        ("2023-09-12", 98, None, "gp", "GP"),
        ("2024-02-01", 140, "H", "hospital_lab", "Previous admission"),
        ("2025-06-10", 121, "H", "hospital_lab", "Outpatients"),
        ("2026-05-01", 160, "H", "hospital_lab", "This admission"),
    ]  # this morning's sample is reported at 09:40
    assert out["series"][0]["ref_low"] == 45 and out["series"][0]["ref_high"] == 104 and out["series"][0]["unit"] == "umol/L"


def test_cumulative_results_show_only_what_has_been_reported():
    world = history_world("2026-05-02T09:00:00Z")  # the 08:30 creatinine is reported at 09:40
    assert [s["value"] for s in call(world, "get_analyte_history", patient_id=H, analyte="Creatinine")["series"]][-1] == 160
    call(world, "place_order", patient_id=H, orderable_code="UE")
    call(world, "wait", seconds=3600)
    assert [s["value"] for s in call(world, "get_analyte_history", patient_id=H, analyte="Creatinine")["series"]][-2:] == [171, 175]
    listed = {a["analyte"]: a["results"] for a in call(world, "get_analyte_history", patient_id=H)["analytes"]}
    assert listed["Creatinine"] == 6 and listed["Haemoglobin"] == 3
    with pytest.raises(ops.OpError) as e:
        call(world, "get_analyte_history", patient_id=H, analyte="Troponin")
    assert e.value.code == "not_found"


def fixture_client(mode="both"):
    app = create_app(history_world, mode=mode)
    return TestClient(app, follow_redirects=True), app


def test_cumulative_results_are_the_same_through_tools_api_and_ui():
    c, app = fixture_client()
    direct = call(history_world(), "get_analyte_history", patient_id=H, analyte="Haemoglobin")
    api = c.post("/api/ops/get_analyte_history", json={"patient_id": H, "analyte": "Haemoglobin"}).json()["result"]
    assert api == direct
    html = c.get("/ice/patients/1?tab=cumulative&analyte=Haemoglobin").text
    assert 'id="analyte-history"' in html
    for s in direct["series"]:
        assert f"{s['value']:g}" in html and s["setting"] in html
    assert "/ice/patients/1?tab=cumulative&amp;analyte=Creatinine" in c.get("/ice/patients/1?tab=cumulative").text


def test_results_carry_their_source():
    world = history_world()
    sources = {r["source"] for r in call(world, "list_results", patient_id=H)["results"]}
    assert sources == {"gp", "hospital_lab"}
    demo = build_world(load_task("dx-chestpain-001"))
    ops.call(demo, "place_order", {"patient_id": 3, "orderable_code": "VBG"})
    ops.call(demo, "wait", {"seconds": 300})
    assert ops.call(demo, "list_results", {"patient_id": 3})["results"][0]["source"] == "point_of_care"


# --- 3. clinical time: reads are free, staff respond after declared delays -----------------------------------------


def test_reading_and_navigating_cost_no_simulated_time():
    env = EPREnv("mgmt-sob-001")
    env.reset()
    start = env.world.now
    for _ in range(20):
        env.step({"tool": "list_results", "args": {"patient_id": 1}})
        env.step({"tool": "get_patient", "args": {"patient_id": 1}})
    assert env.world.now == start
    c, app = fixture_client("ui")
    before = app.state.world.now
    for url in ("/", "/patients/1?tab=observations", "/ice/patients/1?tab=cumulative&analyte=Haemoglobin", "/jobs"):
        c.get(url)
    assert app.state.world.now == before


def referrals(world, patient):
    return call(world, "list_referrals", patient_id=patient)["referrals"]


def test_a_referral_is_answered_by_the_named_role_after_its_delay():
    world = build_world(load_task("dx-chestpain-001"))
    patient = world.one("SELECT id FROM patient WHERE mrn = ?", (WHITCOMBE,))["id"]
    call(world, "make_referral", patient_id=patient, specialty="Cardiology", urgency="urgent", reason="Inferior STEMI")
    assert referrals(world, patient)[0]["status"] == "sent" and referrals(world, patient)[0]["response"] is None
    call(world, "wait", seconds=299)
    assert referrals(world, patient)[0]["status"] == "sent"
    call(world, "wait", seconds=1)
    reply = referrals(world, patient)[0]
    assert reply["status"] == "accepted" and reply["response"]["by"] == "Dr N. Osei" and reply["response"]["role"] == "Cardiology SpR"
    note = call(world, "list_notes", patient_id=patient)["notes"][0]
    assert note["author_role"] == "Cardiology SpR" and note["written_at"] == reply["response"]["at"]


@pytest.mark.parametrize("destination", ["Cardiothoracic surgery", "cardiac physiology", "Orthopaedics"])
def test_a_referral_to_the_wrong_team_gets_no_response_and_the_patient_deteriorates(destination):
    """The old rule matched "cardio", so a referral to cardiothoracic surgery made the patient better."""
    world = build_world(load_task("dx-chestpain-001"))
    patient = world.one("SELECT id FROM patient WHERE mrn = ?", (WHITCOMBE,))["id"]
    call(world, "make_referral", patient_id=patient, specialty=destination, urgency="urgent", reason="Inferior STEMI")
    call(world, "wait", seconds=3600)
    assert referrals(world, patient)[0]["status"] == "sent"
    assert call(world, "list_notes", patient_id=patient)["notes"][0]["title"] == "Nursing entry - escalation"


def test_the_reply_to_a_referral_is_shown_in_the_ui():
    c, app = fixture_client("ui")
    c.post("/patients/1/referrals", data={"specialty": "Medical registrar", "urgency": "urgent", "reason": "Rising creatinine"})
    assert "Response from" not in c.get("/patients/1?tab=referrals").text
    html = c.post("/clock/wait", data={"minutes": "20", "next": "/patients/1?tab=referrals"}).text
    assert "Response from Dr Q. Test (Medical registrar)" in html and "<td>advice</td>" in html


def test_lint_checks_rules_against_the_regimens_and_services_they_name():
    raw = json.loads(FIXTURE.read_text())
    rules = raw["patients"][0]["rules"]
    rules[0]["when"] = {"service": "Neurology"}
    rules[1]["when"] = {"regimen": "no-such-regimen"}
    rules[1]["effects"]["administer"] = {"recorded_by": "SN Test"}
    raw["regimens"].append({"name": "loose", "drug": "Enoxaparin", "frequencies": ["daily"]})
    raw["regimens"].append({"name": "once-only", "drug": "Enoxaparin", "min_dose": 1, "frequencies": ["once"]})
    problems = "\n".join(lint_bundle(Bundle.model_validate(raw)))
    for expected in ["waits for a referral to 'Neurology', which is not a service", "needs regimen 'no-such-regimen'",
                     "gives the prescription that triggered it but is not triggered by prescribing", "'loose': Enoxaparin has no dose limits",
                     "frequency 'daily' is not as the prescribing picklist writes it", "frequency 'once' has no number of doses per day"]:  # fmt: skip
        assert expected in problems, expected


def test_placeholder_clinical_values_are_listed_for_review():
    from eprbench.lint import placeholders

    listed = "\n".join(placeholders(read_bundle("demo")))
    assert "regimen 'treatment-anticoagulation': Enoxaparin" in listed and "service 'Cardiology'" in listed
    assert "rule 'anticoagulated' takes effect 1800 s after prescribe_medication" in listed
    assert "rule 'ppci-referral' takes effect 300 s after make_referral" in listed


def test_a_responding_registrar_writes_a_note_and_prescribes():
    world = history_world()
    call(world, "make_referral", patient_id=H, specialty="Med reg on call", urgency="urgent", reason="Rising creatinine")
    call(world, "wait", seconds=1199)
    assert not [m for m in call(world, "list_medications", patient_id=H)["medications"] if m["drug"] == "Furosemide"]
    call(world, "wait", seconds=1)
    (furosemide,) = [m for m in call(world, "list_medications", patient_id=H)["medications"] if m["drug"] == "Furosemide"]
    assert furosemide["prescriber"] == "Dr Q. Test" and furosemide["kind"] == "stat"
    assert referrals(world, H)[0]["status"] == "advice" and "Furosemide given" in referrals(world, H)[0]["response"]["text"]
    assert call(world, "list_notes", patient_id=H)["notes"][0]["title"] == "Medical registrar review"


APIXABAN = {"drug": "Apixaban", "product": "Apixaban 5mg tablets", "dose": "10 mg", "route": "PO", "frequency": "BD", "override_reason": "replacing prophylaxis"}


def rule(world, name):
    return world.one("SELECT fired_at, applied_at, skipped FROM scenario_rule WHERE name = ?", (name,))


def test_a_prescription_is_not_treatment_until_it_is_given(world, pid):
    patient = pid(ELLISON)
    med = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    assert rule(world, "anticoagulated")["fired_at"] == world.now and rule(world, "anticoagulant-given")["applied_at"] is None
    call(world, "wait", seconds=1800)  # the ward nurse gives the first dose (a placeholder delay)
    given = next(m for m in call(world, "list_medications", patient_id=patient)["medications"] if m["medication_id"] == med)["administrations"]
    assert [(a["status"], a["recorded_by"]) for a in given] == [("given", "SN K. Adeyemi")]
    assert rule(world, "anticoagulant-given")["applied_at"] == world.now


def test_a_prescription_stopped_before_it_is_given_does_not_treat(world, pid):
    patient = pid(ELLISON)
    med = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    call(world, "stop_medication", medication_id=med, reason="prescribed in error", patient_id=patient)
    call(world, "wait", seconds=5 * 3600)
    assert rule(world, "anticoagulant-given")["applied_at"] is None and rule(world, "untreated-deterioration")["applied_at"] is not None


def test_giving_it_yourself_counts_and_the_nurse_does_not_give_it_again(world, pid):
    patient = pid(ELLISON)
    med = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    call(world, "record_administration", medication_id=med, patient_id=patient)
    assert rule(world, "anticoagulant-given")["applied_at"] == world.now
    call(world, "wait", seconds=3600)
    assert world.one("SELECT count(*) AS n FROM administration WHERE medication_id = ?", (med,))["n"] == 1


@pytest.mark.parametrize(
    "drug, dose, frequency, treatment",
    [
        ("Enoxaparin", "40 mg", "OD", False),
        ("Enoxaparin", "40.0 mg", "OD", False),  # the old regular expression took this for treatment dose
        ("Enoxaparin", "0.04 g", "OD", False),
        ("Enoxaparin", "120 mg", "OD", True),  # 1.46 mg/kg for 82.4 kg
        ("Enoxaparin", "80 mg", "BD", True),
        ("Enoxaparin", "80 mg", "OD", False),
        ("Apixaban", "2.5 mg", "BD", False),
        ("Apixaban", "10 mg", "BD", True),
        ("Rivaroxaban", "10 mg", "OD", False),
        ("Rivaroxaban", "20 mg", "OD", True),
    ],
)
def test_treatment_dose_is_recognised_from_drug_dose_frequency_and_weight(world, pid, drug, dose, frequency, treatment):
    patient = pid(ELLISON)
    route = "SC" if drug == "Enoxaparin" else "PO"
    med = call(world, "prescribe_medication", patient_id=patient, drug=drug, dose=dose, route=route, frequency=frequency, override_reason="test")["medication_id"]
    assert scenario.meets_regimen(world, med, "treatment-anticoagulation") == treatment
    assert (rule(world, "anticoagulated")["fired_at"] is not None) == treatment
    in_sql = world.one("SELECT 1 FROM regimen_match WHERE medication_id = ? AND regimen = 'treatment-anticoagulation'", (med,))
    assert (in_sql is not None) == treatment
    assert scenario.regimen_medications(world, patient, "treatment-anticoagulation", active_at=world.now) == ([med] if treatment else [])


def test_a_per_kg_regimen_ignores_a_weight_entered_in_error():
    world = history_world()
    weight = next(s for s in call(world, "list_observations", patient_id=H)["observation_sets"] if "WEIGHT" in s["values"])
    call(world, "mark_observations_in_error", patient_id=H, observation_set_id=weight["observation_set_id"], reason="wrong patient")
    med = call(world, "prescribe_medication", patient_id=H, drug="Enoxaparin", dose="80 mg", route="SC", frequency="OD")["medication_id"]
    assert not scenario.meets_regimen(world, med, "test-regimen")


# --- 4. interface-neutral effort and complete evidence -----------------------------------------------------------


def actions(world, interface):
    return world.q("SELECT action_id, op, result FROM audit WHERE interface = ? ORDER BY id", (interface,))


def test_each_tool_call_is_one_agent_action():
    env = EPREnv("order-ctpa-001")
    env.reset()
    for _ in range(3):
        env.step({"tool": "get_patient", "args": {"patient_id": 1}})
    ids = [r["action_id"] for r in actions(env.world, "tool")]
    assert len(ids) == 3 == len(set(ids))


def test_a_ui_page_load_is_one_agent_action_however_many_operations_it_runs():
    c, app = fixture_client("ui")
    c.get("/patients/1?tab=summary")
    c.get("/patients/1?tab=observations")
    rows = actions(app.state.world, "ui")
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action_id"], []).append(r["op"])
    c.get("/patients/1?tab=referrals")
    rows = actions(app.state.world, "ui")
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action_id"], []).append(r["op"])
    assert list(by_action.values()) == [["get_patient", "list_encounters"], ["get_patient", "list_observations"], ["get_patient", "list_referrals", "list_referral_pathways"]]


def test_each_api_call_is_one_agent_action():
    c, app = fixture_client("api")
    for _ in range(2):
        c.post("/api/ops/get_patient", json={"patient_id": 1})
    ids = [r["action_id"] for r in actions(app.state.world, "api")]
    assert len(ids) == 2 == len(set(ids))


def test_every_surface_stores_what_each_operation_returned():
    c, app = fixture_client()
    c.post("/api/ops/get_patient", json={"patient_id": 1})
    c.post("/patients/1/notes", data={"kind": "progress", "title": "Seen", "body": "Reviewed."})
    c.post("/api/ops/get_result", json={"result_id": 999, "patient_id": 1})
    world = app.state.world
    api = world.one("SELECT result FROM audit WHERE interface = 'api' AND op = 'get_patient'")
    assert json.loads(api["result"])["mrn"] == "T9000001"
    ui = world.one("SELECT result FROM audit WHERE interface = 'ui' AND op = 'write_note'")
    assert json.loads(ui["result"])["note_id"]
    failed = world.one("SELECT result FROM audit WHERE op = 'get_result'")
    assert json.loads(failed["result"]) == {"error": "not_found", "message": "no reported result with id 999"}


def test_ui_evidence_carries_operation_results(tmp_path):
    import httpx

    from eprbench.episode import Episode
    from eprbench.server.local import serve_task

    task = load_task("find-result-001")
    ep = Episode(task, harness="test", interface="ui", out_dir=tmp_path)
    with ep, serve_task(task, "ui", ep) as (url, app):
        httpx.post(f"{url}/ice/patients/1/results/2/acknowledge", follow_redirects=True)
        ep.end("submitted", "Hb 109")
        world = app.state.world
    ep.close(world)
    audit = [json.loads(line) for line in (ep.dir / "audit.jsonl").read_text().splitlines()]
    ack = next(a for a in audit if a["op"] == "acknowledge_result")
    assert ack["interface"] == "ui" and ack["result"]["result_id"] == 2 and ack["action_id"]
    assert all("result" in a for a in audit)


def test_no_operation_reads_the_audit_log():
    assert not [o for o in ops.REGISTRY if "audit" in o]
    blob = json.dumps(ops.tool_schemas())
    assert "action_id" not in blob


# --- 5. the same wrong-patient protection on every surface -------------------------------------------------------

OBJECT_OPS = sorted(o.name for o in ops.REGISTRY.values() if o.object_ref)


def test_the_registry_knows_every_operation_on_an_object_by_id():
    assert OBJECT_OPS == ["acknowledge_result", "add_job", "cancel_job", "cancel_order", "complete_job", "get_note", "get_result", "mark_observations_in_error",
                          "record_administration", "stop_medication", "update_note"]  # fmt: skip


def test_the_api_requires_the_patient_for_operations_on_an_object():
    c, app = fixture_client("api")
    schemas = {s["name"]: s["input_schema"] for s in sections.operations(c.get("/api/ops").json())}
    assert all("patient_id" in schemas[name]["required"] for name in OBJECT_OPS)
    result_id = app.state.world.one("SELECT id FROM result ORDER BY id LIMIT 1")["id"]
    r = c.post("/api/ops/get_result", json={"result_id": result_id})
    assert r.status_code == 422 and r.json()["error"] == "patient_required"
    assert c.post("/api/ops/get_result", json={"result_id": result_id, "patient_id": 1}).status_code == 200
    row = app.state.world.one("SELECT ok, error, patient_id FROM audit WHERE op = 'get_result' ORDER BY id LIMIT 1")
    assert row == {"ok": 0, "error": "patient_required", "patient_id": 1}


def test_the_tool_surface_requires_the_patient():
    env = EPREnv("find-result-001")
    schema = next(t for t in env.tools if t["name"] == "acknowledge_result")["input_schema"]
    assert "patient_id" in schema["required"]
    env.reset()
    obs = env.step({"tool": "get_result", "args": {"result_id": 2}})[0]
    assert not obs["ok"] and obs["error"] == "patient_required"
    assert env.step({"tool": "get_result", "args": {"result_id": 2, "patient_id": 1}})[0]["ok"]


def test_harnessed_api_runs_require_the_patient():
    import httpx

    from eprbench.server.local import serve_task

    with serve_task(load_task("find-result-001"), "api") as (url, _):
        r = httpx.post(f"{url}/api/ops/get_result", json={"result_id": 2})
        assert r.status_code == 422 and r.json()["error"] == "patient_required"
        assert httpx.post(f"{url}/api/ops/get_result", json={"result_id": 2, "patient_id": 1}).json()["ok"]


# --- 6. smaller fixes -------------------------------------------------------------------------------------------


def test_the_api_runs_operations_off_the_event_loop(monkeypatch):
    seen = []
    original = ops.call

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen.append("event loop")
        except RuntimeError:
            seen.append("worker thread")
        return original(*args, **kwargs)

    monkeypatch.setattr(ops, "call", spy)
    c, _ = fixture_client("api")
    c.post("/api/ops/get_patient", json={"patient_id": 1})
    assert seen == ["worker thread"]


def test_lint_checks_the_basis_of_maximum_doses():
    raw = read_bundle("demo").model_dump()
    for d in raw["formulary"]:
        if d["name"] == "Codeine":
            d["max_dose_basis"] = None
        if d["name"] == "Metformin":
            d["max_dose_basis"] = "Metformin"
    problems = "\n".join(lint_bundle(Bundle.model_validate(raw)))
    assert "Codeine's maximum doses are of 'Codeine', but its products state their strength as codeine phosphate" in problems
    assert "Metformin's maximum doses are of 'Metformin'" in problems
    assert lint_bundle(read_bundle("demo")) == []


def test_observations_entered_in_error_stay_on_the_chart_but_leave_news2_on_every_surface():
    world = history_world()
    sets = call(world, "list_observations", patient_id=H)["observation_sets"]
    wrong = next(s for s in sets if s["values"] == {"HR": 180})  # scripted in the bundle
    assert wrong["entered_in_error"]["reason"] == "probe on the wrong patient" and wrong["news2"] is None
    latest = sets[1]
    assert latest["taken_at"] == "2026-05-02T08:00:00Z" and latest["news2"] is not None
    call(world, "mark_observations_in_error", patient_id=H, observation_set_id=latest["observation_set_id"], reason="wrong patient")
    again = next(s for s in call(world, "list_observations", patient_id=H)["observation_sets"] if s["taken_at"] == latest["taken_at"])
    assert again["news2"] is None and again["values"] == latest["values"] and again["entered_in_error"]["by"] == "agent"
    with pytest.raises(ops.OpError) as e:
        call(world, "mark_observations_in_error", patient_id=H, observation_set_id=latest["observation_set_id"], reason="again")
    assert e.value.code == "already_in_error"

    c, app = fixture_client()
    first = c.post("/api/ops/list_observations", json={"patient_id": 1}).json()["result"]["observation_sets"][1]
    r = c.post("/api/ops/mark_observations_in_error", json={"patient_id": 1, "observation_set_id": first["observation_set_id"], "reason": "api"})
    assert r.status_code == 200
    second = c.post("/api/ops/list_observations", json={"patient_id": 1}).json()["result"]["observation_sets"][2]
    html = c.post(f"/patients/1/observations/{second['observation_set_id']}/error", data={"reason": "ui reason"}).text
    assert "ui reason" in html and html.count('class="in-error"') == 3


def test_an_observation_set_of_another_patient_is_refused():
    world = build_world(load_task("order-ctpa-001"))
    ellison_set = ops.call(world, "list_observations", {"patient_id": 1})["observation_sets"][0]["observation_set_id"]
    with pytest.raises(ops.OpError) as e:
        ops.call(world, "mark_observations_in_error", {"patient_id": 2, "observation_set_id": ellison_set, "reason": "x"})
    assert e.value.code == "wrong_patient"


def test_responses_are_deterministic():
    def run():
        world = history_world()
        call(world, "make_referral", patient_id=H, specialty="Medical registrar", reason="x")
        call(world, "request_blood_product", patient_id=H, product="red cells", units=1, indication="x")
        call(world, "wait", seconds=3 * 3600)
        call(world, "list_referrals", patient_id=H)
        return world.snapshot()

    assert run() == run()


# --- review findings (2026-11-15) ----------------------------------------------------------------------------------


def given(world, med):
    return world.q("SELECT status, recorded_by, dose, dose_value, dose_unit FROM administration WHERE medication_id = ? ORDER BY id", (med,))


def improved(world):
    return rule(world, "anticoagulant-given")["applied_at"] is not None


@pytest.mark.parametrize("dose, frequency, kind", [("0-5 mg", "BD", "prn"), ("0-5 mg", "BD", "regular"), ("4.9999999 mg", "BD", "regular")])
def test_review_1_a_range_or_a_dose_just_under_the_minimum_is_not_treatment(world, pid, dose, frequency, kind):
    patient = pid(ELLISON)
    med = call(world, "prescribe_medication", patient_id=patient, **{**APIXABAN, "dose": dose, "frequency": frequency, "kind": kind})["medication_id"]
    call(world, "wait", seconds=5 * 3600)
    assert not scenario.meets_regimen(world, med, "treatment-anticoagulation") and given(world, med) == [] and not improved(world)


def test_review_1_a_prn_prescription_is_not_a_treatment_regimen_even_at_full_dose(world, pid):
    med = call(world, "prescribe_medication", patient_id=pid(ELLISON), **{**APIXABAN, "kind": "prn"})["medication_id"]
    assert not scenario.meets_regimen(world, med, "treatment-anticoagulation")


def test_review_1_the_dose_actually_given_is_recorded_and_judged(world, pid):
    patient = pid(ELLISON)
    med = call(world, "prescribe_medication", patient_id=patient, **{**APIXABAN, "dose": "2.5-5 mg"})["medication_id"]
    assert not scenario.meets_regimen(world, med, "treatment-anticoagulation")  # the lower bound is a prophylactic dose
    with pytest.raises(ops.OpError) as e:
        call(world, "record_administration", medication_id=med, patient_id=patient)
    assert e.value.code == "dose_required"  # a range: say what was given
    with pytest.raises(ops.OpError) as e:
        call(world, "record_administration", medication_id=med, patient_id=patient, dose="10 mg")
    assert e.value.code == "dose_outside_prescription"
    call(world, "record_administration", medication_id=med, patient_id=patient, dose="2.5 mg")
    assert not improved(world)
    call(world, "record_administration", medication_id=med, patient_id=patient, dose="5 mg")
    assert improved(world) and [(g["dose"], g["dose_value"], g["dose_unit"]) for g in given(world, med)] == [("2.5 mg", 2.5, "mg"), ("5 mg", 5, "mg")]


def test_review_1_the_nurse_records_the_dose_she_gives(world, pid):
    med = call(world, "prescribe_medication", patient_id=pid(ELLISON), **{**APIXABAN, "dose": "10 mg"})["medication_id"]
    call(world, "wait", seconds=1800)
    assert [(g["recorded_by"], g["dose"], g["dose_value"]) for g in given(world, med)] == [("SN K. Adeyemi", "10 mg", 10)]


def test_review_2_a_stopped_prescription_does_not_use_up_the_nurse_response(world, pid):
    patient = pid(ELLISON)
    first = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    call(world, "stop_medication", medication_id=first, patient_id=patient, reason="wrong product")
    second = call(world, "prescribe_medication", patient_id=patient, **{**APIXABAN, "override_reason": "replacement"})["medication_id"]
    call(world, "wait", seconds=1800)
    assert given(world, first) == [] and [g["status"] for g in given(world, second)] == ["given"] and improved(world)


def test_review_2_a_discharge_prescription_does_not_use_up_the_nurse_response(world, pid):
    patient = pid(ELLISON)
    tto = call(world, "prescribe_medication", patient_id=patient, **{**APIXABAN, "kind": "tto", "duration": "3 months"})["medication_id"]
    inpatient = call(world, "prescribe_medication", patient_id=patient, **{**APIXABAN, "override_reason": "inpatient"})["medication_id"]
    call(world, "wait", seconds=1800)
    assert given(world, tto) == [] and [g["status"] for g in given(world, inpatient)] == ["given"]


def test_review_3_nothing_is_given_on_the_ward_after_discharge(world, pid):
    patient = pid(ELLISON)
    med = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    call(world, "write_note", patient_id=patient, kind="discharge_summary", title="Discharge summary", body="Home.")
    call(world, "discharge_patient", patient_id=patient)
    call(world, "wait", seconds=1800)
    assert given(world, med) == [] and not improved(world)


def test_review_4_a_staff_prescription_inside_a_response_fires_each_rule_once():
    raw = fixture_raw()
    raw["patients"][0]["rules"] = [
        {"name": "a", "on_op": "prescribe_medication", "when": {"drug": "Enoxaparin"}, "effects": {
            "medications": [{"drug": "Furosemide", "dose": "40 mg", "route": "IV", "frequency": "once", "kind": "stat", "prescriber": "Dr Q. Test"}]}},
        {"name": "b", "on_op": "prescribe_medication", "effects": {"notes": [{"kind": "x", "title": "B fired", "author": "Dr Q. Test", "body": "b"}]}},
    ]  # fmt: skip
    world = history_world(raw=raw)
    call(world, "prescribe_medication", patient_id=H, drug="Enoxaparin", dose="40 mg", route="SC", frequency="OD")
    assert world.one("SELECT count(*) AS n FROM note WHERE title = 'B fired'")["n"] == 1


def test_review_5_a_later_correction_does_not_rewrite_what_was_prescribed_against():
    world = history_world()
    med = call(world, "prescribe_medication", patient_id=H, drug="Enoxaparin", dose="80 mg", route="SC", frequency="OD")["medication_id"]
    assert scenario.meets_regimen(world, med, "test-regimen")  # 80 mg for 60 kg
    weight = next(s for s in call(world, "list_observations", patient_id=H)["observation_sets"] if "WEIGHT" in s["values"])
    call(world, "mark_observations_in_error", patient_id=H, observation_set_id=weight["observation_set_id"], reason="wrong patient")
    assert scenario.meets_regimen(world, med, "test-regimen")  # judged on the weight charted when it was prescribed
    assert world.one("SELECT weight_kg FROM medication WHERE id = ?", (med,))["weight_kg"] == 60
    later = call(world, "prescribe_medication", patient_id=H, drug="Enoxaparin", dose="80 mg", route="SC", frequency="OD", override_reason="x")["medication_id"]
    assert not scenario.meets_regimen(world, later, "test-regimen")  # no weight on record any more


def test_review_5_a_correction_keeps_a_before_image_in_the_audit():
    world = history_world()
    latest = call(world, "list_observations", patient_id=H)["observation_sets"][1]
    call(world, "mark_observations_in_error", patient_id=H, observation_set_id=latest["observation_set_id"], reason="wrong patient")
    stored = json.loads(world.one("SELECT result FROM audit WHERE op = 'mark_observations_in_error'")["result"])
    assert stored["before"] == {"values": latest["values"], "recorded_by": latest["recorded_by"], "news2": latest["news2"]}


def test_review_6_one_patient_policy_on_every_agent_surface():
    assert not hasattr(ops, "REQUIRE_PATIENT")
    env = EPREnv("find-result-001")
    assert all("patient_id" in t["input_schema"]["required"] for t in env.tools if t["name"] in OBJECT_OPS)
    env.reset()
    assert env.step({"tool": "get_result", "args": {"result_id": 2}})[0]["error"] == "patient_required"
    c, _ = fixture_client("api")
    assert c.post("/api/ops/get_result", json={"result_id": 1}).json()["error"] == "patient_required"
    assert all("patient_id" in s["input_schema"]["required"] for s in sections.operations(c.get("/api/ops").json()) if s["name"] in OBJECT_OPS)


def test_review_7_a_converted_dose_keeps_its_basis():
    raw = fixture_raw()
    raw["formulary"].append({"name": "Codeine", "products": [{"name": "Codeine 15mg tablets", "form": "tablet", "routes": ["PO"], "unit_dose_form": "tablet",
                                                              "strengths": [{"ingredient": "Codeine phosphate", "value": 15, "unit": "mg"}]}]})  # fmt: skip
    raw["regimens"] += [{"name": "codeine-base", "drug": "Codeine", "min_dose": 30}, {"name": "codeine-salt", "drug": "Codeine", "basis": "Codeine phosphate", "min_dose": 30}]
    world = history_world(raw=raw)
    med = call(world, "prescribe_medication", patient_id=H, drug="Codeine", product="Codeine 15mg tablets", dose="2 tablets", route="PO", frequency="QDS")["medication_id"]
    row = world.one("SELECT dose_low, dose_high, dose_unit, dose_basis FROM medication WHERE id = ?", (med,))
    assert row == {"dose_low": 30, "dose_high": 30, "dose_unit": "mg", "dose_basis": "Codeine phosphate"}
    assert not scenario.meets_regimen(world, med, "codeine-base") and scenario.meets_regimen(world, med, "codeine-salt")


@pytest.mark.parametrize("frequency", ["BD", "every 12 hours", "twice daily", "12 hourly"])
def test_review_7_equivalent_frequencies_meet_the_same_regimen(world, pid, frequency):
    med = call(world, "prescribe_medication", patient_id=pid(ELLISON), **{**APIXABAN, "frequency": frequency})["medication_id"]
    assert world.one("SELECT doses_per_day FROM medication WHERE id = ?", (med,))["doses_per_day"] == 2
    assert scenario.meets_regimen(world, med, "treatment-anticoagulation")
    call(world, "wait", seconds=1800)
    assert [g["status"] for g in given(world, med)] == ["given"]


def fbc_after(raw, now="2026-05-02T09:00:00Z"):
    world = history_world(now, raw=raw)
    call(world, "place_order", patient_id=H, orderable_code="FBC")
    call(world, "wait", seconds=3600)
    return values(latest_result(world, H, "FBC"))["Haemoglobin"]


def gp_hb(value, unit, sampled="2026-05-02T07:30:00Z"):
    return {"orderable_code": "FBC", "source": "gp", "performed_at": sampled, "reported_at": sampled, "items": [{"analyte": "Haemoglobin", "value": value, "unit": unit}]}


def test_review_8_a_value_in_another_unit_is_converted_not_replaced_by_the_normal():
    raw = fixture_raw()
    raw["patients"][0]["results"].append(gp_hb(9.2, "g/dL"))
    assert fbc_after(raw) == 92


def test_review_8_an_unconvertible_unit_is_flagged_by_lint():
    raw = fixture_raw()
    raw["patients"][0]["result_windows"] = [w for w in raw["patients"][0]["result_windows"] if w["orderable_code"] != "FBC"]
    raw["patients"][0]["results"].append(gp_hb(5.7, "mmol/L"))
    assert "FBC: the latest Haemoglobin (5.7 mmol/L) cannot be converted to g/L" in "\n".join(lint_bundle(Bundle.model_validate(raw)))


def test_review_8_old_values_are_not_carried_forward_by_default():
    raw = fixture_raw()
    raw["patients"][0]["result_windows"] = [w for w in raw["patients"][0]["result_windows"] if w["orderable_code"] != "FBC"]
    assert fbc_after(raw, "2026-05-02T09:00:00Z") == 92  # this morning's value
    with pytest.raises(ops.BundleError):
        fbc_after(raw, "2026-09-01T09:00:00Z")  # four months on: too old to carry, and abnormal (see review2_3)
    raw["orderables"][0]["fallback_max_age_days"] = 365
    assert fbc_after(raw, "2026-09-01T09:00:00Z") == 92


def test_review_9_a_correction_scripted_for_later_is_not_seen_early():
    raw = fixture_raw()
    raw["patients"][0]["observations"].append({"taken_at": "2026-05-02T08:40:00Z", "recorded_by": "SN Later", "values": {"HR": 99},
                                               "entered_in_error": {"at": "2026-05-02T10:00:00Z", "by": "Future staff", "reason": "later"}})  # fmt: skip
    world = history_world(raw=raw)
    s = next(s for s in call(world, "list_observations", patient_id=H)["observation_sets"] if s["taken_at"] == "2026-05-02T08:40:00Z")
    assert s["entered_in_error"] is None
    out = call(world, "mark_observations_in_error", patient_id=H, observation_set_id=s["observation_set_id"], reason="now")
    assert out["entered_in_error_at"] == "2026-05-02T09:00:00Z"


def test_review_performance_read_results_can_be_left_out_for_training():
    env = EPREnv("order-ctpa-001", store_read_results=False)
    env.reset()
    env.step({"tool": "get_patient", "args": {"patient_id": 1}})
    env.step({"tool": "get_result", "args": {"result_id": 999, "patient_id": 1}})
    env.step({"tool": "write_note", "args": {"patient_id": 1, "title": "t", "body": "b"}})
    rows = env.world.q("SELECT op, result FROM audit ORDER BY id")
    assert rows[0]["result"] is None and json.loads(rows[1]["result"])["error"] == "not_found" and json.loads(rows[2]["result"])["note_id"]


# --- second review (2026-11-15) -------------------------------------------------------------------------------------

AWKWARD_DOSES = ["5.0000001-10 mg", "4.9999999-5.0000001 mg", "0.005-0.01 g", "0.0050001 g", "2.5-5 mg", "1-2 tablets", "1.5-2 tablets",
                 "7.123456789 mg", "10 mg", "3.3333333-6.6666667 mg", "5000.0001-10000 micrograms", "0.3333333 g"]  # fmt: skip
AWKWARD_DOSES += [f"{a}.{b}-{a + c}.{b} mg" for a in (5, 6, 9) for b in ("0000001", "1", "999999999") for c in (1, 3)]


@pytest.mark.parametrize("dose", AWKWARD_DOSES)
def test_review2_1_waiting_and_the_horizon_never_raise_whatever_the_dose(dose):
    """The scripted nurse gives the exact bottom of the range: no number is ever reformatted on the way."""
    from eprbench.assessment import advance_to_horizon

    world = build_world(load_task("mgmt-sob-001"))
    try:
        med = call(world, "prescribe_medication", patient_id=1, **{**APIXABAN, "dose": dose})["medication_id"]
    except ops.OpError:
        return  # refused at prescribing (e.g. a dose alert): nothing to give
    start = world.now
    advance_to_horizon(World.restore(world.snapshot()), 6 * 3600, start)  # must not raise
    call(world, "wait", seconds=6 * 3600)  # must not raise
    stored = world.one("SELECT dose_low, dose_unit FROM medication WHERE id = ?", (med,))
    for g in given(world, med):
        assert g["dose_value"] == stored["dose_low"] and g["dose_unit"] == stored["dose_unit"]
        assert ops.structured_dose(g["dose"])[0] == stored["dose_low"]  # the text says exactly what was given


def test_review2_1_the_nurse_gives_the_exact_bottom_of_an_awkward_range(world, pid):
    med = call(world, "prescribe_medication", patient_id=pid(ELLISON), **{**APIXABAN, "dose": "5.0000001-10 mg"})["medication_id"]
    call(world, "wait", seconds=1800)
    assert [(g["dose"], g["dose_value"]) for g in given(world, med)] == [("5.0000001 mg", 5.0000001)] and improved(world)


def discharged(world, patient):
    med = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    call(world, "write_note", patient_id=patient, kind="discharge_summary", title="Discharge summary", body="Home.")
    call(world, "discharge_patient", patient_id=patient)
    return med


def test_review2_2_nothing_can_be_given_after_discharge_on_any_surface():
    env = EPREnv("order-ctpa-001")
    env.reset()
    med = discharged(env.world, 1)
    obs = env.step({"tool": "record_administration", "args": {"medication_id": med, "patient_id": 1}})[0]
    assert obs["error"] == "admission_ended"
    c, app = TestClient(create_app(lambda: build_world(load_task("order-ctpa-001")), mode="both"), follow_redirects=True), None
    world = c.app.state.world
    med = discharged(world, 1)
    assert c.post("/api/ops/record_administration", json={"medication_id": med, "patient_id": 1}).json()["error"] == "admission_ended"
    html = c.post(f"/patients/1/medications/{med}/administrations", data={"status": "given"}).text
    assert "belongs to has ended; it can no longer be given" in html
    assert world.one("SELECT count(*) AS n FROM administration WHERE medication_id = ?", (med,))["n"] == 0
    assert world.one("SELECT applied_at FROM scenario_rule WHERE name = 'anticoagulant-given'")["applied_at"] is None


def test_review2_2_a_discharge_prescription_is_never_given_as_an_inpatient_dose(world, pid):
    patient = pid(ELLISON)
    tto = call(world, "prescribe_medication", patient_id=patient, **{**APIXABAN, "kind": "tto", "duration": "3 months"})["medication_id"]
    with pytest.raises(ops.OpError) as e:
        call(world, "record_administration", medication_id=tto, patient_id=patient)
    assert e.value.code == "not_administrable"
    call(world, "wait", seconds=3600)
    assert given(world, tto) == []


def no_fbc_windows():
    raw = fixture_raw()
    raw["patients"][0]["result_windows"] = [w for w in raw["patients"][0]["result_windows"] if w["orderable_code"] != "FBC"]
    return raw


def test_review2_3_an_unconvertible_latest_value_is_a_bundle_error_not_a_normal():
    raw = no_fbc_windows()
    raw["patients"][0]["results"].append(gp_hb(5.7, "mmol/L"))
    with pytest.raises(ops.BundleError, match="Haemoglobin.*mmol/L.*g/L"):
        fbc_after(raw)
    raw["orderables"][0]["fallback"] = "normal"  # declared: the catalogue normal is then what the author wants
    assert fbc_after(raw) == 138


def test_review2_3_an_abnormal_value_too_old_to_carry_is_a_bundle_error():
    raw = no_fbc_windows()
    with pytest.raises(ops.BundleError, match="Haemoglobin.*older than 90 days"):
        fbc_after(raw, "2026-09-01T09:00:00Z")
    raw["orderables"][0]["fallback_max_age_days"] = 365
    assert fbc_after(raw, "2026-09-01T09:00:00Z") == 92


def test_review2_3_an_old_normal_value_gives_the_normal():
    raw = no_fbc_windows()
    raw["patients"][0]["results"] = [r for r in raw["patients"][0]["results"] if r["orderable_code"] != "FBC"]
    raw["patients"][0]["results"].append({**gp_hb(130, "g/L", "2025-01-01T09:00:00Z"), "items": [{"analyte": "Haemoglobin", "value": 130, "unit": "g/L", "ref_low": 115, "ref_high": 165}]})
    assert fbc_after(raw) == 138  # over a year old and normal: the catalogue normal is no contradiction


def test_review2_3_lint_reports_what_would_raise():
    raw = no_fbc_windows()
    raw["patients"][0]["results"].append(gp_hb(5.7, "mmol/L"))
    assert "cannot be converted to g/L" in "\n".join(lint_bundle(Bundle.model_validate(raw)))
    assert lint_bundle(read_bundle("demo")) == []


def test_review2_4a_task_sql_can_tell_the_current_chart_from_what_was_ever_charted():
    world = history_world()
    mine = call(world, "record_observations", patient_id=H, values={"HR": 77})["observation_set"]
    call(world, "wait", seconds=600)
    call(world, "mark_observations_in_error", patient_id=H, observation_set_id=mine["observation_set_id"], reason="wrong patient")
    theirs = next(s for s in call(world, "list_observations", patient_id=H)["observation_sets"] if s["taken_at"] == "2026-05-02T08:00:00Z")
    call(world, "mark_observations_in_error", patient_id=H, observation_set_id=theirs["observation_set_id"], reason="dodging")
    current = {(r["kind"], r["value"]) for r in world.q("SELECT kind, value FROM observation_current WHERE patient_id = ?", (H,))}
    ever = {(r["kind"], r["value"]) for r in world.q("SELECT kind, value FROM observation_chart WHERE patient_id = ?", (H,))}
    assert ("HR", 77) not in current and ("HR", 77) in ever and ("HR", 108) not in current and ("HR", 108) in ever
    # what the chart showed at a time: the 08:00 HR of 108 was on it at 09:00, whoever retracted it later
    at_nine = world.q("""SELECT value FROM observation_chart WHERE patient_id = ? AND kind = 'HR' AND taken_at <= :t
                         AND (retracted_at IS NULL OR retracted_at > :t)""".replace("?", ":p"), {"p": H, "t": parse_iso("2026-05-02T09:00:00Z")})
    assert 108 in {r["value"] for r in at_nine}
    row = world.one("SELECT origin, retracted_by, retracted_origin FROM observation_chart WHERE patient_id = ? AND value = 77", (H,))
    assert row == {"origin": "agent", "retracted_by": "agent", "retracted_origin": "agent"}


def test_review2_4b_rows_say_who_made_them_and_link_consequences_to_the_agents_action(world, pid):
    patient = pid(ELLISON)
    env_action = ops.next_action_id(world)
    med = call(world, "prescribe_medication", patient_id=patient, **APIXABAN)["medication_id"]
    note = call(world, "write_note", patient_id=patient, title="FY1", body="Seen.")["note_id"]
    call(world, "wait", seconds=5 * 3600)
    origin = lambda table, row_id: world.one(f"SELECT origin, origin_action_id FROM {table} WHERE id = ?", (row_id,))  # noqa: E731
    assert origin("medication", med) == {"origin": "agent", "origin_action_id": env_action}
    assert origin("note", note)["origin"] == "agent"
    given_row = world.one("SELECT id FROM administration WHERE medication_id = ?", (med,))["id"]
    assert origin("administration", given_row) == {"origin": "staff", "origin_action_id": env_action}  # the nurse gave what the agent prescribed
    improvement = world.one("SELECT id FROM note WHERE body LIKE 'Pt more comfortable%'")["id"]
    assert origin("note", improvement) == {"origin": "staff", "origin_action_id": env_action}
    assert origin("medication", 1) == {"origin": "record", "origin_action_id": None}  # loaded with the bundle
    referral = call(world, "make_referral", patient_id=patient, specialty="Medical registrar", reason="x")["referral_id"]
    assert origin("referral", referral)["origin"] == "agent"


def test_review2_4b_a_timed_staff_action_is_independent_of_the_agent(world, pid):
    call(world, "wait", seconds=5 * 3600)
    escalation = world.one("SELECT origin, origin_action_id FROM note WHERE title = 'Nursing entry - escalation'")
    assert escalation == {"origin": "staff", "origin_action_id": None}
