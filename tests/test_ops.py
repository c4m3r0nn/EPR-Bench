import json

import pytest

from eprbench import ops
from tests.conftest import CTPA_ELLISON, CXR_FORM, ELLISON, ELLISTON, WHITCOMBE

HIDDEN = ("latent_result", "vitals_track", "pulmonary emboli", "ST elevation")


def test_search_by_name_mrn_and_nhs_number(call):
    assert {p["mrn"] for p in call("search_patients", query="Margaret")["patients"]} == {ELLISON, ELLISTON}
    assert [p["mrn"] for p in call("search_patients", query=ELLISON)["patients"]] == [ELLISON]
    assert [p["mrn"] for p in call("search_patients", query="999 000 0034")["patients"]] == [WHITCOMBE]
    assert call("search_patients", query="Margaret Ellison")["patients"][0]["bed"] == "12"


def test_patient_summary(call, pid):
    p = call("get_patient", patient_id=pid(ELLISON))
    assert p["name"] == "ELLISON, Margaret" and p["nhs_number"] == "999 000 0018"
    assert p["allergies"][0]["substance"] == "Penicillin"
    assert p["current_encounter"]["location_code"] == "W7B"
    assert len(call("list_encounters", patient_id=pid(ELLISON))["encounters"]) == 3


def test_ward_list(call):
    beds = [p["bed"] for p in call("list_location_patients", location_code="W7B")["patients"]]
    assert beds == ["12", "14"]


def test_notes(call, pid):
    notes = call("list_notes", patient_id=pid(ELLISON))["notes"]
    assert notes[0]["title"] == "Nursing entry"  # newest first
    assert "body" not in notes[0]
    assert "SOB" in call("get_note", note_id=notes[0]["note_id"])["body"]
    new = call("write_note", patient_id=pid(ELLISON), title="FY1 review", body="Reviewed. ?PE. CTPA requested.")
    written = call("get_note", note_id=new["note_id"])
    assert written["author"] == "agent" and written["written_at"] == "2026-03-14T09:30:00Z"


def test_observations_carry_news2(call, pid):
    sets = call("list_observations", patient_id=pid(ELLISON))["observation_sets"]
    assert sets[0]["taken_at"] == "2026-03-14T08:50:00Z" and sets[0]["news2"] == 9
    assert sets[-1]["values"] == {"WEIGHT": 82.4} and sets[-1]["news2"] is None
    new = call("record_observations", patient_id=pid(ELLISON), values={"HR": 110, "SBP": 112, "ACVPU": "a"})
    assert new["observation_set"]["values"] == {"HR": 110.0, "SBP": 112.0, "ACVPU": "A"}


def test_order_result_appears_only_after_turnaround(call, pid):
    patient = pid(ELLISON)
    before = len(call("list_results", patient_id=patient)["results"])
    order = call("place_order", patient_id=patient, orderable_code="DDIMER")
    assert call("list_orders", patient_id=patient)["orders"][0]["status"] == "in_progress"
    call("wait", seconds=5399)
    assert len(call("list_results", patient_id=patient)["results"]) == before
    call("wait", seconds=1)
    listed = call("list_orders", patient_id=patient)["orders"][0]
    assert listed["order_id"] == order["order_id"] and listed["status"] == "resulted"
    result = call("get_result", result_id=listed["result_id"])
    assert result["items"][0]["value"] == 3850 and result["items"][0]["flag"] == "H"


def test_unreported_result_cannot_be_read_by_guessing_its_id(world, call, pid):
    call("place_order", patient_id=pid(ELLISON), orderable_code="CTPA", clinical_details="D5 post TKR", request_details=CTPA_ELLISON)
    hidden_id = world.one("SELECT max(id) AS id FROM result")["id"]
    with pytest.raises(ops.OpError) as e:
        call("get_result", result_id=hidden_id)
    assert e.value.code == "not_found"


def test_a_result_window_applies_to_every_sample_taken_inside_it(world, call, pid):
    patient = pid(ELLISON)
    for _ in range(2):
        call("place_order", patient_id=patient, orderable_code="DDIMER")
    call("wait", seconds=5400)
    results = [call("get_result", result_id=r["result_id"]) for r in call("list_results", patient_id=patient)["results"][:2]]
    assert [r["items"][0]["value"] for r in results] == [3850, 3850]


def test_costs_and_turnaround_are_never_shown(call, pid):
    catalogue = call("list_orderables", category="imaging")["orderables"]
    assert {"CXR", "CTPA", "CTHEAD", "MR-KNEE", "US-DOPPLER-LL"} <= {o["code"] for o in catalogue} and len(catalogue) > 90
    assert set(catalogue[0]) == {"code", "name", "category", "modality", "region", "contrast", "lateral"}
    placed = call("place_order", patient_id=pid(ELLISON), orderable_code="FBC")
    assert set(placed) == {"order_id", "patient_id", "name", "status", "collection", "collect_at"}
    assert placed["collect_at"] == call("get_current_time")["now"]  # when it is taken, not when it will be reported
    assert "due_at" not in json.dumps(call("list_orders", patient_id=pid(ELLISON)))


def test_imaging_needs_a_complete_request_form(call, pid):
    with pytest.raises(ops.OpError) as e:
        call("place_order", patient_id=pid(ELLISON), orderable_code="CTPA", clinical_details="  ", request_details=CTPA_ELLISON)
    assert e.value.code == "incomplete_request" and "Clinical summary" in e.value.message


def test_cancel_order(call, pid):
    patient = pid(ELLISON)
    order = call("place_order", patient_id=patient, orderable_code="CXR", clinical_details="SOB", request_details=CXR_FORM)
    call("cancel_order", order_id=order["order_id"])
    assert call("list_orders", patient_id=patient)["orders"][0]["status"] == "cancelled"
    call("wait", seconds=86400)
    assert all(r["orderable_code"] != "CXR" or r["reported_at"] < "2026" for r in call("list_results", patient_id=patient)["results"])
    with pytest.raises(ops.OpError):
        call("cancel_order", order_id=order["order_id"])

    reported = call("place_order", patient_id=patient, orderable_code="ECG")
    call("wait", seconds=600)
    with pytest.raises(ops.OpError) as e:
        call("cancel_order", order_id=reported["order_id"])
    assert e.value.code == "already_resulted"


def test_medications(call, pid):
    patient = pid(ELLISON)
    assert len(call("list_medications", patient_id=patient)["medications"]) == 4
    enox = next(m for m in call("list_medications", patient_id=patient)["medications"] if m["drug"] == "Enoxaparin")
    call("stop_medication", medication_id=enox["medication_id"], reason="Switched to treatment dose DOAC")
    new = call("prescribe_medication", patient_id=patient, drug="Apixaban", dose="10 mg", route="PO", frequency="BD")
    active = {m["drug"] for m in call("list_medications", patient_id=patient)["medications"]}
    assert "Apixaban" in active and "Enoxaparin" not in active and new["medication_id"]
    assert len(call("list_medications", patient_id=patient, include_stopped=True)["medications"]) == 5


def test_live_vitals_only_where_monitored(world, call, pid):
    live = call("get_live_vitals", patient_id=pid(WHITCOMBE), history_seconds=300)
    assert set(live["current"]) == {"HR", "SBP", "DBP", "SPO2", "RR"}
    assert len(live["trend"]) == 5 and 90 < live["current"]["HR"] < 120
    assert live == call("get_live_vitals", patient_id=pid(WHITCOMBE), history_seconds=300)
    with pytest.raises(ops.OpError) as e:
        call("get_live_vitals", patient_id=pid(ELLISON))
    assert e.value.code == "not_monitored"


def test_errors_and_validation(call):
    for name, args, code in [
        ("nope", {}, "unknown_operation"),
        ("get_patient", {"patient_id": 999}, "not_found"),
        ("get_patient", {"patient_id": 1, "extra": 1}, "invalid_arguments"),
        ("wait", {"seconds": -5}, "invalid_arguments"),
        ("place_order", {"patient_id": 1, "orderable_code": "MRI-UNICORN"}, "not_found"),
    ]:
        with pytest.raises(ops.OpError) as e:
            call(name, **args)
        assert e.value.code == code


def test_failed_write_leaves_no_trace_but_is_audited(world, call, pid):
    orders = world.one('SELECT count(*) AS n FROM "order"')["n"]
    with pytest.raises(ops.OpError):
        call("place_order", patient_id=pid(ELLISON), orderable_code="CTPA")
    assert world.one('SELECT count(*) AS n FROM "order"')["n"] == orders
    last = world.one("SELECT * FROM audit ORDER BY id DESC LIMIT 1")
    assert (last["op"], last["ok"], last["error"]) == ("place_order", 0, "incomplete_request")


def test_no_operation_leaks_hidden_state(world, call, pid):
    """Read everything a fresh agent can read; ground truth must not appear."""
    seen = [json.dumps(ops.tool_schemas())]
    for mrn in (ELLISON, ELLISTON, WHITCOMBE):
        patient = pid(mrn)
        for name in ("get_patient", "list_encounters", "list_observations", "list_orders", "list_medications"):
            seen.append(json.dumps(call(name, patient_id=patient)))
        for n in call("list_notes", patient_id=patient)["notes"]:
            seen.append(json.dumps(call("get_note", note_id=n["note_id"])))
        for r in call("list_results", patient_id=patient)["results"]:
            seen.append(json.dumps(call("get_result", result_id=r["result_id"])))
    blob = "\n".join(seen)
    assert not [h for h in HIDDEN if h in blob]


def test_every_operation_has_a_usable_tool_schema():
    schemas = ops.tool_schemas()
    assert len(schemas) == len(ops.REGISTRY) >= 20
    for s in schemas:
        assert s["description"] and s["input_schema"]["type"] == "object"
        assert s["input_schema"].get("additionalProperties") is False


def test_request_form_validation(world, call, pid):
    patient = pid(ELLISON)

    def order(code, **form):
        return call("place_order", patient_id=patient, orderable_code=code, clinical_details="D5 post TKR, breathless", request_details=form)

    for form, code in [
        ({**CTPA_ELLISON, "egfr_date": "2025-11-01"}, "renal_function_out_of_date"),
        ({**CTPA_ELLISON, "egfr_date": "2026-04-01"}, "invalid_arguments"),  # in the future
        ({**CTPA_ELLISON, "egfr_date": "10/03/2026"}, "invalid_arguments"),
        ({**CTPA_ELLISON, "egfr": "normal"}, "invalid_arguments"),
        ({**CTPA_ELLISON, "mobility": "flying"}, "invalid_arguments"),
        ({**CTPA_ELLISON, "laterality": "left"}, "invalid_arguments"),  # not a field on a CTPA form
        ({k: v for k, v in CTPA_ELLISON.items() if k != "contrast_reaction"}, "incomplete_request"),
    ]:
        with pytest.raises(ops.OpError) as e:
            order("CTPA", **form)
        assert e.value.code == code, (form, e.value.code)
    with pytest.raises(ops.OpError) as e:
        order("US-DOPPLER-LL", clinical_question="?DVT", mobility="walking")
    assert e.value.code == "incomplete_request" and "Side" in e.value.message
    placed = order("US-DOPPLER-LL", clinical_question="?DVT", mobility="walking", laterality="right")
    stored = call("list_orders", patient_id=patient)["orders"][0]
    assert stored["order_id"] == placed["order_id"] and stored["request_details"]["laterality"] == "right"


def test_browsing_the_imaging_catalogue(call):
    modalities = {m["modality"] for m in call("browse_imaging_catalogue")["modalities"]}
    assert modalities == {"XR", "US", "CT", "MRI", "Nuclear medicine", "Fluoroscopy", "Interventional"}
    regions = {r["region"] for r in call("browse_imaging_catalogue", modality="MRI")["regions"]}
    assert {"Head and neck", "Spine", "Lower limb"} <= regions
    knees = call("browse_imaging_catalogue", modality="MRI", region="Lower limb")["examinations"]
    assert {"code": "MR-KNEE", "name": "MRI Knee", "contrast": False, "lateral": True} in knees
    with pytest.raises(ops.OpError):
        call("browse_imaging_catalogue", modality="Telepathy")
