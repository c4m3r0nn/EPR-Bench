"""Engine v3: the structured record a v2 case grades, so that no check has to read free text.

Dated requests, request-dependent results, referral pathways, document status, structured assessment forms, rule
gating, jobs with a structured link and a status history, turnaround overrides, and height with BMI. Each is tested
through the operations and, where a person would use it, the UI; the parity crawl (test_ui_parity) covers every
field of every new operation."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eprbench import ops
from eprbench.lint import lint_bundle, placeholders
from eprbench.loader import Bundle, load_bundle, read_bundle
from eprbench.server.app import create_app
from eprbench.tasks import build_world, load_task
from eprbench.world import SCHEMA_VERSION, World, parse_iso
from tests.conftest import WHITCOMBE, calling

FIXTURE = Path(__file__).parent / "fixtures" / "history.json"
H = 1

DOPPLER = {"code": "USDVT", "name": "US Doppler leg veins", "category": "imaging", "modality": "US", "region": "Lower limb", "lateral": True,
           "cost_pence": 5000, "tat_seconds": 7200, "fallback": "normal", "normal": {"reported_by": "Radiology", "report_text": "Normal study."}}  # fmt: skip
PPCI = {"name": "ppci", "label": "Primary PCI activation", "service": "Cardiology", "urgency": "emergency", "fields": [
    {"name": "ecg_stemi", "label": "ECG meets STEMI criteria", "kind": "yes_no"},
    {"name": "symptom_onset", "label": "Time of symptom onset", "kind": "datetime"},
]}  # fmt: skip
WELLS = {"name": "wells_test", "label": "Test score", "fields": [
    {"name": "a", "label": "Criterion A", "kind": "tick", "points": 1},
    {"name": "b", "label": "Criterion B", "kind": "tick", "points": 1},
    {"name": "alt", "label": "Alternative as likely", "kind": "tick", "points": -2},
], "bands": [{"max": 1, "value": "unlikely", "label": "Unlikely"}, {"min": 2, "value": "likely", "label": "Likely"}]}  # fmt: skip
LEFT = "Acute left popliteal DVT."
BOTH = "Bilateral calf DVT: acute right popliteal and left calf vein thrombosis."  # a bilateral report names each side
RIGHT = "No deep vein thrombosis in the right leg."


def raw(**changes) -> dict:
    """The longitudinal fixture with what these tests need: a lateral examination with side-dependent findings, a PPCI
    pathway with a rule keyed on it, a structured form, a phlebotomy round and a turnaround override."""
    r = json.loads(FIXTURE.read_text())
    r["orderables"].append(DOPPLER)
    r["services"][1]["aliases"] = []  # "PPCI" in a general referral's destination must not reach the pathway's reply
    r["pathways"] = [PPCI]
    r["assessment_forms"] = [WELLS]
    r["phlebotomy_rounds"] = ["07:30"]
    r["turnaround_overrides"] = {"UE": 5400}
    p = r["patients"][0]
    p["result_windows"].append({"orderable_code": "USDVT", "valid_from": "2026-05-02T00:00:00Z", "reported_by": "Radiology", "branches": [
        {"request": {"laterality": "left"}, "report_text": LEFT}, {"request": {"laterality": "bilateral"}, "report_text": BOTH},
        {"request": {"laterality": "right"}, "report_text": RIGHT}]})  # fmt: skip
    p["rules"].append({"name": "ppci-accept", "on_op": "make_referral", "when": {"pathway": "ppci", "fields": {"ecg_stemi": "yes"}}, "delay_s": 300,
                       "effects": {"referral_update": {"status": "accepted", "by": "Dr C. Test", "role": "Cardiology SpR", "text": "Accepted for PCI."}}})  # fmt: skip
    for key, value in changes.items():
        r[key] = value
    return r


def world_of(r: dict | None = None, now: str = "2026-05-02T09:00:00Z") -> World:
    world = World.create(now=parse_iso(now))
    load_bundle(world, Bundle.model_validate(r if r is not None else raw()))
    return world


def call(world, name, **args):
    return calling(world, name, args)  # a long wait as repeated waits of at most an hour


def refused(world, name, **args) -> str:
    with pytest.raises(ops.OpError) as e:
        call(world, name, **args)
    return e.value.code


def client(world_factory=None, mode="both"):
    app = create_app(world_factory or world_of, mode=mode)
    return TestClient(app, follow_redirects=True), app


def order(world, order_id):
    return next(o for o in call(world, "list_orders", patient_id=H)["orders"] if o["order_id"] == order_id)


def hb(world, order_id):
    result = call(world, "get_result", result_id=order(world, order_id)["result_id"], patient_id=H)
    return next(i["value"] for i in result["items"] if i["analyte"] == "Haemoglobin")


# --- 1. dated requests ---------------------------------------------------------------------------------------------


def test_a_blood_test_for_the_next_phlebotomy_round_is_scheduled_then_collected_then_reported():
    world = world_of()
    form = call(world, "get_request_form", patient_id=H, orderable_code="FBC")
    assert {"value": "phlebotomy_round", "label": "Next phlebotomy round", "at": "2026-05-03T07:30:00Z"} in form["collection"]
    o = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round")
    assert o["status"] == "scheduled" and o["collect_at"] == "2026-05-03T07:30:00Z"
    assert order(world, o["order_id"])["status"] == "scheduled" and world.one("SELECT 1 FROM result WHERE order_id = ?", (o["order_id"],)) is None
    call(world, "wait", seconds=parse_iso("2026-05-03T07:30:00Z") - world.now)
    row = order(world, o["order_id"])
    assert row["status"] == "in_progress" and row["collected_at"] == "2026-05-03T07:30:00Z"
    call(world, "wait", seconds=3600)
    assert order(world, o["order_id"])["status"] == "resulted" and hb(world, o["order_id"]) == 88  # the window covering 07:30
    result = call(world, "get_result", result_id=order(world, o["order_id"])["result_id"], patient_id=H)
    assert result["performed_at"] == "2026-05-03T07:30:00Z" and result["reported_at"] == "2026-05-03T08:30:00Z"


def test_a_result_is_evaluated_at_the_sample_time_including_what_happened_in_between():
    world = world_of(now="2026-05-02T13:00:00Z")
    now = call(world, "place_order", patient_id=H, orderable_code="FBC")["order_id"]
    later = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="scheduled", collect_at="2026-05-02T16:00")["order_id"]
    call(world, "request_blood_product", patient_id=H, product="red cells", units=1, indication="test")  # transfused at 15:00
    call(world, "wait", seconds=5 * 3600)
    assert hb(world, now) == 90 and hb(world, later) == 104


def test_a_scheduled_request_can_be_cancelled_and_is_never_collected():
    world = world_of()
    o = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="scheduled", collect_at="2026-05-02T18:00:00Z")["order_id"]
    assert call(world, "cancel_order", order_id=o, patient_id=H)["status"] == "cancelled"
    call(world, "wait", seconds=12 * 3600)
    assert order(world, o)["status"] == "cancelled" and world.one("SELECT 1 FROM result WHERE order_id = ?", (o,)) is None
    row = world.one("SELECT status, cancelled_by, collected_at FROM order_status WHERE order_id = ?", (o,))
    assert row == {"status": "cancelled", "cancelled_by": "agent", "collected_at": None}


def test_a_patient_who_has_left_by_the_collection_time_is_not_bled():
    world = world_of()
    o = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round")["order_id"]
    call(world, "write_note", patient_id=H, kind="discharge_summary", title="Discharge summary", body="Home.")
    call(world, "discharge_patient", patient_id=H)
    call(world, "wait", seconds=86400)
    row = world.one("SELECT status, cancel_reason FROM order_status WHERE order_id = ?", (o,))
    assert row["status"] == "cancelled" and row["cancel_reason"].startswith("Not collected")


@pytest.mark.parametrize(
    "args, code",
    [
        ({"collection": "scheduled"}, "invalid_arguments"),  # no time
        ({"collection": "scheduled", "collect_at": "2026-05-02T08:00"}, "invalid_arguments"),  # in the past
        ({"collection": "scheduled", "collect_at": "tomorrow"}, "invalid_arguments"),
        ({"collection": "now", "collect_at": "2026-05-02T18:00"}, "invalid_arguments"),  # a time only with scheduled
        ({"collection": "phlebotomy_round", "orderable_code": "USDVT"}, "invalid_arguments"),  # phlebotomists take blood
    ],
)
def test_a_collection_time_is_checked(args, code):
    world = world_of()
    base = {"patient_id": H, "orderable_code": "FBC"}
    if args.get("orderable_code") == "USDVT":
        base |= {"clinical_details": "?DVT", "request_details": {"clinical_question": "?DVT", "laterality": "left", "mobility": "walking"}}
    assert refused(world, "place_order", **{**base, **args}) == code


def test_without_a_round_in_the_bundle_none_is_offered():
    world = world_of(raw(phlebotomy_rounds=[]))
    assert [c["value"] for c in call(world, "get_request_form", patient_id=H, orderable_code="FBC")["collection"]] == ["now", "scheduled"]
    assert refused(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round") == "invalid_arguments"


def test_a_bundle_can_override_a_turnaround():
    world = world_of()
    o = call(world, "place_order", patient_id=H, orderable_code="UE")["order_id"]
    assert world.one("SELECT reported_at - performed_at AS tat FROM result WHERE order_id = ?", (o,))["tat"] == 5400
    o = call(world, "place_order", patient_id=H, orderable_code="FBC")["order_id"]
    assert world.one("SELECT reported_at - performed_at AS tat FROM result WHERE order_id = ?", (o,))["tat"] == 3600


def test_lint_checks_rounds_and_overrides():
    problems = "\n".join(lint_bundle(Bundle.model_validate(raw(phlebotomy_rounds=["7.30"], turnaround_overrides={"NOPE": 60, "FBC": 0}))))
    assert "phlebotomy round '7.30' is not a time as HH:MM" in problems
    assert "turnaround override for 'NOPE', which is not in the catalogue" in problems and "turnaround override for 'FBC' must be positive" in problems


def test_scheduled_requests_through_the_api_and_the_ui():
    c, app = client()
    r = c.post("/api/ops/place_order", json={"patient_id": H, "orderable_code": "FBC", "collection": "scheduled", "collect_at": "2026-05-02T18:00"}).json()
    assert r["ok"] and r["result"]["status"] == "scheduled"
    html = c.get("/ice/patients/1?tab=request&category=lab&code=FBC").text
    assert 'value="phlebotomy_round"' in html and "2026-05-03 07:30" in html and 'name="collect_at"' in html
    html = c.post("/ice/patients/1/requests", data={"orderable_code": "CRP", "collection": "phlebotomy_round"}).text
    assert "scheduled" in html and "2026-05-03 07:30" in html
    rows = app.state.world.q("SELECT orderable_code, collection, status FROM order_status ORDER BY order_id")
    assert rows == [{"orderable_code": "FBC", "collection": "scheduled", "status": "scheduled"}, {"orderable_code": "CRP", "collection": "phlebotomy_round", "status": "scheduled"}]


# --- 2. request-dependent results ------------------------------------------------------------------------------------


def doppler(world, side):
    o = call(world, "place_order", patient_id=H, orderable_code="USDVT", clinical_details="?DVT",
             request_details={"clinical_question": "?DVT", "laterality": side, "mobility": "walking"})["order_id"]  # fmt: skip
    call(world, "wait", seconds=7200)
    return call(world, "get_result", result_id=order(world, o)["result_id"], patient_id=H)["report_text"]


@pytest.mark.parametrize("side, report", [("left", LEFT), ("bilateral", BOTH), ("right", RIGHT)])
def test_a_report_matches_the_side_requested(side, report):
    assert doppler(world_of(), side) == report


def test_an_unmatched_request_gets_the_windows_own_default_when_it_declares_one_else_the_catalogue_normal():
    r = raw()
    r["patients"][0]["result_windows"][-1]["branches"].pop()  # no right branch
    assert doppler(world_of(r), "right") == "Normal study."
    r["patients"][0]["result_windows"][-1]["report_text"] = "No DVT in the right leg."
    assert doppler(world_of(r), "right") == "No DVT in the right leg." and doppler(world_of(r), "left") == LEFT


def test_a_scenario_window_can_branch_on_the_request_too():
    r = raw()
    r["patients"][0]["rules"].append({"name": "clot-extends", "at": "2026-05-02T10:00:00Z", "effects": {"result_windows": [
        {"orderable_code": "USDVT", "branches": [{"request": {"laterality": ["left", "bilateral"]}, "report_text": "Extension into the left femoral vein."}]}]}})  # fmt: skip
    world = world_of(r)
    call(world, "wait", seconds=3600)
    assert doppler(world, "bilateral") == "Extension into the left femoral vein." and doppler(world, "right") == "Normal study."


def test_lint_flags_side_dependent_imaging_without_branches_on_the_side():
    r = raw()
    window = r["patients"][0]["result_windows"][-1]
    window.pop("branches")
    window["report_text"] = "Acute right popliteal DVT."
    assert "USDVT is requested for a side, but a result window reports a right-sided finding whatever side is asked for" in "\n".join(lint_bundle(Bundle.model_validate(r)))
    r = raw()
    r["patients"][0]["result_windows"][-1]["branches"][0]["report_text"] = "Acute right popliteal DVT."
    assert "for a request with laterality left reports a right-sided finding" in "\n".join(lint_bundle(Bundle.model_validate(r)))
    r = raw()
    r["patients"][0]["result_windows"][-1]["branches"][0]["request"] = {"laterality": "sinister", "colour": "red"}
    problems = "\n".join(lint_bundle(Bundle.model_validate(r)))
    assert "branches on laterality 'sinister', which the request form does not offer" in problems and "branches on 'colour'" in problems
    assert not [p for p in lint_bundle(Bundle.model_validate(raw())) if "USDVT" in p]


# --- 3. referral pathways ------------------------------------------------------------------------------------------


def referral(world, referral_id):
    return next(r for r in call(world, "list_referrals", patient_id=H)["referrals"] if r["referral_id"] == referral_id)


def test_pathways_are_a_picklist_with_their_own_forms():
    world = world_of()
    (ppci,) = call(world, "list_referral_pathways")["pathways"]
    assert ppci["name"] == "ppci" and ppci["label"] == "Primary PCI activation" and ppci["urgency"] == "emergency"
    assert [f["name"] for f in ppci["fields"]] == ["ecg_stemi", "symptom_onset"]
    assert ppci["fields"][0]["options"] == [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]
    rid = call(world, "make_referral", patient_id=H, pathway="ppci", pathway_details={"ecg_stemi": "yes", "symptom_onset": "2026-05-02T08:10"},
               reason="Not STEMI. Do not activate.")["referral_id"]  # fmt: skip
    r = referral(world, rid)
    assert r["pathway"] == "ppci" and r["specialty"] == "Primary PCI activation" and r["urgency"] == "emergency"
    assert r["pathway_details"] == {"ecg_stemi": "yes", "symptom_onset": "2026-05-02T08:10:00Z"}
    assert world.q("SELECT field, value FROM referral_field WHERE referral_id = ? ORDER BY field", (rid,)) == [
        {"field": "ecg_stemi", "value": "yes"}, {"field": "symptom_onset", "value": "2026-05-02T08:10:00Z"}]  # fmt: skip
    assert world.one("SELECT service FROM referral WHERE id = ?", (rid,))["service"] == "Cardiology"


@pytest.mark.parametrize(
    "args",
    [
        {"pathway": "nope"},
        {"pathway": "ppci", "pathway_details": {"ecg_stemi": "yes"}},  # incomplete
        {"pathway": "ppci", "pathway_details": {"ecg_stemi": "maybe", "symptom_onset": "2026-05-02T08:10"}},
        {"pathway": "ppci", "pathway_details": {"ecg_stemi": "yes", "symptom_onset": "this morning"}},
        {"pathway": "ppci", "pathway_details": {"ecg_stemi": "yes", "symptom_onset": "2026-05-02T08:10", "extra": "x"}},
        {"pathway": "ppci", "specialty": "Cardiology", "pathway_details": {"ecg_stemi": "yes", "symptom_onset": "2026-05-02T08:10"}},
        {},  # neither a pathway nor a destination
        {"specialty": "Cardiology", "pathway_details": {"ecg_stemi": "yes"}},  # pathway fields without a pathway
    ],
)
def test_a_pathway_form_is_checked(args):
    assert refused(world_of(), "make_referral", patient_id=H, reason="x", **args) in ("invalid_arguments", "not_found", "incomplete_request")


def test_a_scripted_reply_keys_on_the_pathway_and_its_fields_never_on_the_reason():
    world = world_of()
    general = call(world, "make_referral", patient_id=H, specialty="Cardiology", urgency="urgent", reason="STEMI, ECG yes: activate primary PCI")["referral_id"]
    no_ecg = call(world, "make_referral", patient_id=H, pathway="ppci", pathway_details={"ecg_stemi": "no", "symptom_onset": "2026-05-02T08:10"}, reason="x")["referral_id"]
    yes = call(world, "make_referral", patient_id=H, pathway="ppci", pathway_details={"ecg_stemi": "yes", "symptom_onset": "2026-05-02T08:10"}, reason="Not STEMI")["referral_id"]
    call(world, "wait", seconds=300)
    assert [referral(world, r)["status"] for r in (general, no_ecg, yes)] == ["sent", "sent", "accepted"]


def test_lint_checks_pathways_and_the_rules_that_name_them():
    r = raw()
    r["pathways"].append({**PPCI, "service": "Neurosurgery", "fields": [{"name": "x", "label": "X", "kind": "choice"}]})
    r["patients"][0]["rules"][-1]["when"] = {"pathway": "2ww", "fields": {"ecg": "yes"}}
    r["patients"][0]["rules"].append({"name": "by-reason", "on_op": "make_referral", "match": {"reason": "STEMI"}, "effects": {}})
    problems = "\n".join(lint_bundle(Bundle.model_validate(r)))
    for expected in ["duplicate pathway 'ppci'", "pathway 'ppci' reaches 'Neurosurgery', which is not a service", "field 'x' is a choice with no options",
                     "rule 'ppci-accept' waits for pathway '2ww', which the bundle does not define", "rule 'by-reason' matches free text (reason)"]:  # fmt: skip
        assert expected in problems, expected


def test_a_pathway_referral_through_the_ui():
    c, app = client(mode="ui")
    html = c.get("/patients/1?tab=referrals").text
    assert "/patients/1?tab=referrals&amp;referral_pathway=ppci" in html
    html = c.get("/patients/1?tab=referrals&referral_pathway=ppci").text
    assert 'name="rp_ecg_stemi"' in html and 'name="rp_symptom_onset"' in html and 'name="pathway" value="ppci"' in html
    html = c.post("/patients/1/referrals", data={"pathway": "ppci", "rp_ecg_stemi": "yes", "rp_symptom_onset": "2026-05-02 08:10", "reason": "Inferior STEMI"}).text
    assert "Primary PCI activation" in html and 'role="alert"' not in html
    assert app.state.world.one("SELECT pathway, urgency FROM referral") == {"pathway": "ppci", "urgency": "emergency"}
    r = raw()
    r["pathways"].append({"name": "outreach", "label": "Critical care outreach"})  # sets no urgency: the referrer chooses
    c, app = client(lambda: world_of(r), mode="ui")
    assert 'id="pathway-urgency"' in c.get("/patients/1?tab=referrals&referral_pathway=outreach").text
    c.post("/patients/1/referrals", data={"pathway": "outreach", "urgency": "urgent", "reason": "NEWS 7"})
    assert app.state.world.one("SELECT pathway, urgency, pathway_details FROM referral") == {"pathway": "outreach", "urgency": "urgent", "pathway_details": "{}"}


# --- 4. document status --------------------------------------------------------------------------------------------


def test_a_draft_discharge_summary_does_not_allow_discharge_until_it_is_finalised():
    world = world_of()
    note = call(world, "write_note", patient_id=H, kind="discharge_summary", title="Discharge summary", body="Final, not a draft.", status="draft")
    assert note["status"] == "draft"
    assert refused(world, "discharge_patient", patient_id=H) == "discharge_summary_draft"
    listed = call(world, "list_notes", patient_id=H)["notes"][0]
    assert listed["status"] == "draft" and call(world, "get_note", note_id=note["note_id"], patient_id=H)["status"] == "draft"
    call(world, "wait", seconds=60)
    out = call(world, "update_note", note_id=note["note_id"], patient_id=H, body="Home today. Enoxaparin stopped.", status="final")
    assert out["status"] == "final"
    got = call(world, "get_note", note_id=note["note_id"], patient_id=H)
    assert got["body"] == "Home today. Enoxaparin stopped." and got["finalised_by"] == "agent" and got["finalised_at"] == "2026-05-02T09:01:00Z"
    call(world, "discharge_patient", patient_id=H)
    revisions = world.q("SELECT revision, status, body FROM note_revision WHERE note_id = ? ORDER BY revision", (note["note_id"],))
    assert revisions == [{"revision": 1, "status": "draft", "body": "Final, not a draft."}, {"revision": 2, "status": "final", "body": "Home today. Enoxaparin stopped."}]


def test_only_the_author_may_edit_a_draft_and_a_final_note_is_never_edited():
    world = world_of()
    mine = call(world, "write_note", patient_id=H, title="Plan", body="Draft.", status="draft")["note_id"]
    other = ops.Ctx(actor="Dr Other")
    with pytest.raises(ops.OpError) as e:
        ops.call(world, "update_note", {"note_id": mine, "patient_id": H, "body": "Mine now."}, other)
    assert e.value.code == "not_author"
    call(world, "update_note", note_id=mine, patient_id=H, title="Plan (edited)")
    assert call(world, "get_note", note_id=mine, patient_id=H)["title"] == "Plan (edited)"
    final = call(world, "write_note", patient_id=H, title="Plan", body="Final.")["note_id"]
    assert refused(world, "update_note", note_id=final, patient_id=H, body="Changed.") == "note_final"
    assert refused(world, "update_note", note_id=mine, patient_id=2, body="x") in ("wrong_patient", "not_found")


def test_a_draft_is_visibly_a_draft_and_editable_by_its_author_in_the_ui():
    c, app = client(mode="ui")
    c.post("/patients/1/notes", data={"kind": "discharge_summary", "title": "Discharge summary", "body": "Draft text", "status": "draft"})
    note = app.state.world.one("SELECT id, status FROM note ORDER BY id DESC LIMIT 1")
    assert note["status"] == "draft"
    html = c.get(f"/patients/1?tab=notes&note_id={note['id']}").text
    assert "DRAFT" in html and f'action="/patients/1/notes/{note["id"]}"' in html
    assert "the discharge summary is still a draft" in c.post("/patients/1/discharge", data={"destination": "Home"}).text
    c.post(f"/patients/1/notes/{note['id']}", data={"title": "Discharge summary", "body": "Final text", "status": "final"})
    assert app.state.world.one("SELECT status, body FROM note WHERE id = ?", (note["id"],)) == {"status": "final", "body": "Final text"}
    assert f'action="/patients/1/notes/{note["id"]}"' not in c.get(f"/patients/1?tab=notes&note_id={note['id']}").text


# --- 5. structured assessments --------------------------------------------------------------------------------------


def test_a_bundle_form_is_listed_recorded_scored_and_listed_back():
    world = world_of()
    forms = {f["name"]: f for f in call(world, "list_assessment_forms")["forms"]}
    assert [f["name"] for f in forms["wells_test"]["fields"]] == ["a", "b", "alt"] and forms["wells_test"]["fields"][2]["points"] == -2
    out = call(world, "record_assessment", patient_id=H, kind="wells_test", answers={"a": "yes", "b": "yes"}, details="calf 3 cm bigger")
    assert (out["score"], out["category"], out["outcome"]) == (2, "likely", "Likely")
    (listed,) = call(world, "list_assessments", patient_id=H)["assessments"]
    assert listed["score"] == 2 and listed["category"] == "likely" and listed["outcome"] == "Likely"
    assert [(a["field"], a["value"], a["points"]) for a in listed["answers"]] == [("a", "yes", 1), ("b", "yes", 1), ("alt", "no", 0)]
    rows = world.q("SELECT field, value, points FROM assessment_answer WHERE assessment_id = ? ORDER BY id", (out["assessment_id"],))
    assert rows == [{"field": "a", "value": "yes", "points": 1}, {"field": "b", "value": "yes", "points": 1}, {"field": "alt", "value": "no", "points": 0}]
    out = call(world, "record_assessment", patient_id=H, kind="wells_test", answers={"a": "yes", "b": "yes", "alt": "yes"})
    assert (out["score"], out["category"]) == (0, "unlikely")


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "wells_test", "answers": {"zzz": "yes"}},
        {"kind": "wells_test", "answers": {"a": "probably"}},
        {"kind": "vte", "outcome": "Low risk", "answers": {"a": "yes"}},  # a free-text kind has no fields
        {"kind": "vte"},  # and needs its outcome
        {"kind": "nope", "outcome": "x"},
    ],
)
def test_an_assessment_is_checked_against_its_kind(args):
    assert refused(world_of(), "record_assessment", patient_id=H, **args) == "invalid_arguments"


def test_the_packaged_wells_form_is_data_marked_for_the_clinician():
    demo = build_world(load_task("order-ctpa-001"))
    (wells,) = [f for f in ops.call(demo, "list_assessment_forms", {})["forms"] if f["name"] == "wells_dvt"]
    assert len(wells["fields"]) == 10 and [b["value"] for b in wells["bands"]] == ["dvt_unlikely", "dvt_likely"]
    assert any("assessment form 'wells_dvt'" in p for p in placeholders(read_bundle("demo")))


def test_a_form_assessment_through_the_ui():
    c, app = client(mode="ui")
    assert "/patients/1?tab=assessments&amp;assessment_form=wells_test" in c.get("/patients/1?tab=assessments").text
    html = c.get("/patients/1?tab=assessments&assessment_form=wells_test").text
    assert 'type="checkbox" name="af_a"' in html and 'name="kind" value="wells_test"' in html
    html = c.post("/patients/1/assessments", data={"kind": "wells_test", "af_a": "yes", "af_b": "yes", "details": "x"}).text
    assert "Likely" in html and app.state.world.one("SELECT score, category FROM assessment") == {"score": 2, "category": "likely"}


# --- 6. rule gating ------------------------------------------------------------------------------------------------


def note_rule(name, **fields):
    return {"name": name, "effects": {"notes": [{"kind": "x", "title": name, "author": "Staff", "body": "."}]}, **fields}


def fired(world, name):
    return world.one("SELECT count(*) AS n FROM note WHERE title = ?", (name,))["n"]


def test_on_op_rules_respect_not_before_and_not_after():
    r = raw()
    r["patients"][0]["rules"] += [note_rule("early", on_op="write_note", not_after="2026-05-02T09:30:00Z", repeat=True),
                                  note_rule("late", on_op="write_note", not_before="2026-05-02T10:00:00Z", repeat=True)]  # fmt: skip
    world = world_of(r)
    call(world, "write_note", patient_id=H, title="t", body="b")
    call(world, "wait", seconds=3600)
    call(world, "write_note", patient_id=H, title="t", body="b")
    assert (fired(world, "early"), fired(world, "late")) == (1, 1)


def test_a_rule_fires_only_while_its_encounter_is_open_unless_the_bundle_says_otherwise():
    r = raw()
    r["patients"][0]["rules"] += [note_rule("default", on_op="write_note", repeat=True), note_rule("anytime", on_op="write_note", repeat=True, encounter=None),
                                  note_rule("old-admission", on_op="write_note", repeat=True, encounter="adm-2024"),
                                  note_rule("this-admission", on_op="write_note", repeat=True, encounter="adm-2026")]  # fmt: skip
    world = world_of(r, now="2026-04-30T09:00:00Z")  # before the admission
    call(world, "write_note", patient_id=H, title="t", body="b")
    assert [fired(world, n) for n in ("default", "anytime", "old-admission", "this-admission")] == [0, 1, 0, 0]
    for _ in range(2):
        call(world, "wait", seconds=86400)
    call(world, "write_note", patient_id=H, title="t", body="b")
    assert [fired(world, n) for n in ("default", "anytime", "old-admission", "this-admission")] == [1, 2, 0, 1]


def test_a_timed_rule_is_skipped_when_the_patient_has_gone():
    r = raw()
    r["patients"][0]["rules"].append(note_rule("evening", at="2026-05-02T18:00:00Z"))
    world = world_of(r)
    call(world, "write_note", patient_id=H, kind="discharge_summary", title="Discharge summary", body="Home.")
    call(world, "discharge_patient", patient_id=H)
    call(world, "wait", seconds=12 * 3600)
    assert fired(world, "evening") == 0 and world.one("SELECT skipped FROM scenario_rule WHERE name = 'evening'")["skipped"] == 1


def test_unless_takes_a_list_for_timed_and_triggered_rules():
    r = raw()
    r["patients"][0]["rules"] += [note_rule("a", on_op="record_fluid"), note_rule("b", on_op="add_problem"),
                                  note_rule("timed", at="2026-05-02T12:00:00Z", unless=["a", "b"]),
                                  note_rule("triggered", on_op="write_note", unless=["a", "b"], repeat=True)]  # fmt: skip
    world = world_of(r)
    call(world, "write_note", patient_id=H, title="t", body="b")
    call(world, "add_problem", patient_id=H, display="x")
    call(world, "write_note", patient_id=H, title="t", body="b")
    call(world, "wait", seconds=4 * 3600)
    assert (fired(world, "triggered"), fired(world, "timed")) == (1, 0)


def test_rules_can_key_on_structured_fields_of_every_kind():
    r = raw()
    r["patients"][0]["rules"] += [
        note_rule("left-doppler", on_op="place_order", when={"orderable_code": "USDVT", "fields": {"laterality": "left"}}),
        note_rule("likely", on_op="record_assessment", when={"kind": "wells_test", "score": {"min": 2}}),
        note_rule("final-summary", on_op="update_note", when={"kind": "discharge_summary", "status": "final"}),
        note_rule("scheduled", on_op="place_order", when={"collection": "phlebotomy_round"}),
    ]  # fmt: skip
    world = world_of(r)
    for side in ("right", "left"):
        call(world, "place_order", patient_id=H, orderable_code="USDVT", clinical_details="?DVT", request_details={"clinical_question": "?DVT", "laterality": side, "mobility": "walking"})
    call(world, "record_assessment", patient_id=H, kind="wells_test", answers={"a": "yes"})
    assert (fired(world, "left-doppler"), fired(world, "likely")) == (1, 0)
    call(world, "record_assessment", patient_id=H, kind="wells_test", answers={"a": "yes", "b": "yes"})
    note = call(world, "write_note", patient_id=H, kind="discharge_summary", title="DS", body="x", status="draft")["note_id"]
    call(world, "update_note", note_id=note, patient_id=H, body="y")
    assert (fired(world, "likely"), fired(world, "final-summary")) == (1, 0)
    call(world, "update_note", note_id=note, patient_id=H, status="final")
    call(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round")
    assert (fired(world, "final-summary"), fired(world, "scheduled")) == (1, 1)


def test_every_application_of_a_repeating_response_is_recorded():
    r = raw()
    r["patients"][0]["rules"].append(note_rule("each", on_op="write_note", repeat=True))
    world = world_of(r)
    for _ in range(3):
        call(world, "write_note", patient_id=H, title="t", body="b")
    rows = world.q("SELECT a.applied_at, a.origin_action_id FROM scenario_application a JOIN scenario_rule r ON r.id = a.rule_id WHERE r.name = 'each'")
    assert len(rows) == 3 and len({r["origin_action_id"] for r in rows}) == 3


def test_lint_checks_rule_gating():
    r = raw()
    r["patients"][0]["rules"] += [note_rule("x", on_op="write_note", encounter="adm-1999", unless=["nope"]),
                                  note_rule("y", on_op="write_note", not_before="2026-05-03T00:00:00Z", not_after="2026-05-02T00:00:00Z")]  # fmt: skip
    problems = "\n".join(lint_bundle(Bundle.model_validate(r)))
    assert "rule 'x' is active only during encounter 'adm-1999', which the patient does not have" in problems
    assert "rule 'x' depends on unknown rule 'nope'" in problems and "rule 'y' can never fire: not_before is after not_after" in problems


def whitcombe_reply(task_id, **referral):
    world = build_world(load_task(task_id))
    patient = world.one("SELECT id FROM patient WHERE mrn = ?", (WHITCOMBE,))["id"]
    ops.call(world, "make_referral", {"patient_id": patient, "reason": "x", **referral})
    ops.call(world, "wait", {"seconds": 600})
    return ops.call(world, "list_referrals", {"patient_id": patient})["referrals"][0]["status"]


def test_the_demo_ppci_reply_needs_an_urgent_referral_while_he_is_in_the_department():
    assert whitcombe_reply("find-result-001", specialty="Cardiology", urgency="urgent") == "sent"  # 13 March: he has not attended yet
    assert whitcombe_reply("dx-chestpain-001", specialty="Cardiology", urgency="routine") == "sent"  # a routine referral is not a PPCI call
    assert whitcombe_reply("dx-chestpain-001", specialty="Cardiology", urgency="urgent") == "accepted"
    details = {"ecg_stemi": "yes", "symptom_onset": "2026-03-14T08:30"}
    assert whitcombe_reply("dx-chestpain-001", pathway="ppci", pathway_details=details) == "accepted"


# --- 7. jobs -------------------------------------------------------------------------------------------------------


def test_a_job_links_to_its_request_and_type_and_keeps_its_history():
    world = world_of()
    o = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round")["order_id"]
    a = call(world, "add_job", patient_id=H, description="Do not take these bloods", task_type="bloods", order_id=o)["job_id"]
    b = call(world, "add_job", patient_id=H, description="Chase FBC", task_type="chase_result", order_id=o)["job_id"]
    call(world, "wait", seconds=60)
    call(world, "cancel_job", job_id=a, patient_id=H, reason="Not needed")
    call(world, "complete_job", job_id=b, patient_id=H, outcome="Hb 88")
    assert refused(world, "complete_job", job_id=a, patient_id=H) == "already_closed" and refused(world, "cancel_job", job_id=b, patient_id=H, reason="x") == "already_closed"
    jobs = {j["job_id"]: j for j in call(world, "list_jobs", include_done=True)["jobs"]}
    assert (jobs[a]["status"], jobs[a]["task_type"], jobs[a]["order_id"], jobs[a]["done"]) == ("cancelled", "bloods", o, False)
    assert jobs[a]["cancel_reason"] == "Not needed" and (jobs[b]["status"], jobs[b]["done"]) == ("done", True)
    assert not call(world, "list_jobs")["jobs"]
    history = world.q("SELECT job_id, status, by, note FROM job_event ORDER BY id")
    assert history == [{"job_id": a, "status": "open", "by": "agent", "note": None}, {"job_id": b, "status": "open", "by": "agent", "note": None},
                       {"job_id": a, "status": "cancelled", "by": "agent", "note": "Not needed"}, {"job_id": b, "status": "done", "by": "agent", "note": "Hb 88"}]  # fmt: skip
    assert world.one("SELECT done_at FROM job WHERE id = ?", (a,))["done_at"] is None  # cancelling is not doing


def test_a_job_cannot_link_to_another_patients_request():
    world = build_world(load_task("order-ctpa-001"))
    o = ops.call(world, "place_order", {"patient_id": 1, "orderable_code": "FBC"})["order_id"]
    with pytest.raises(ops.OpError) as e:
        ops.call(world, "add_job", {"patient_id": 2, "description": "x", "order_id": o})
    assert e.value.code == "wrong_patient"
    assert ops.call(world, "add_job", {"patient_id": 1, "description": "x", "order_id": o})["job_id"]


def test_jobs_through_the_ui():
    c, app = client(mode="ui")
    c.post("/ice/patients/1/requests", data={"orderable_code": "FBC", "collection": "phlebotomy_round"})
    html = c.get("/patients/1?tab=jobs").text
    assert 'name="task_type" value="bloods"' in html and 'name="order_id" value="1"' in html
    c.post("/patients/1/jobs", data={"description": "Bloods tomorrow", "task_type": "bloods", "order_id": "1"})
    assert app.state.world.one("SELECT task_type, order_id FROM job") == {"task_type": "bloods", "order_id": 1}
    html = c.post("/patients/1/jobs/1/cancel", data={"reason": "Not needed"}).text
    assert "Cancelled by" in html and app.state.world.one("SELECT status FROM job")["status"] == "cancelled"


# --- 8. height and BMI ---------------------------------------------------------------------------------------------


def test_height_is_charted_and_bmi_is_computed_where_weight_and_height_exist():
    world = world_of()
    assert all(s.get("bmi") is None for s in call(world, "list_observations", patient_id=H)["observation_sets"])
    call(world, "record_observations", patient_id=H, values={"HEIGHT": 160})
    call(world, "wait", seconds=60)
    s = call(world, "record_observations", patient_id=H, values={"WEIGHT": 64})["observation_set"]
    assert s["bmi"] == 25.0
    assert world.q("SELECT weight_kg, height_cm, bmi FROM bmi WHERE patient_id = ? ORDER BY taken_at", (H,)) == [{"weight_kg": 64, "height_cm": 160, "bmi": 25.0}]
    c, _ = client(lambda: copy_world(world), mode="ui")
    html = c.get("/patients/1?tab=observations").text
    assert 'name="HEIGHT"' in html and "<td>25</td>" in html


def copy_world(world):
    return World.restore(world.snapshot())


# --- legacy and the packaged bundle --------------------------------------------------------------------------------


def test_the_packaged_bundle_lints_clean_and_old_bundles_still_load():
    assert lint_bundle(read_bundle("demo")) == []
    legacy = json.loads(FIXTURE.read_text())
    legacy["patients"][0]["rules"][0]["unless"] = "transfused"  # a single name, as older bundles write it
    world_of(legacy)


def test_a_world_saved_by_the_previous_version_is_migrated_when_restored():
    """Saved evidence (final.sqlite) from step 2 can still be regraded: its samples were taken when requested, its done
    jobs were done, and its rules ran ungated with unless as a single name."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript((Path(__file__).parent / "fixtures" / "schema_v2.sql").read_text())
    t = parse_iso("2026-05-02T09:00:00Z")
    conn.executescript(f"""
        INSERT INTO meta VALUES ('now', '{t}');
        INSERT INTO patient (id, mrn, nhs_number, family_name, given_name, dob, sex) VALUES (1, 'T1', '9990000050', 'Old', 'World', '1950-01-01', 'F');
        INSERT INTO encounter (id, patient_id, kind, admitted_at) VALUES (1, 1, 'inpatient', {t - 86400});
        INSERT INTO orderable (code, name, category, cost_pence, tat_seconds) VALUES ('FBC', 'Full blood count', 'lab', 300, 3600);
        INSERT INTO "order" (id, patient_id, encounter_id, orderable_code, ordered_by, ordered_at, due_at) VALUES (1, 1, 1, 'FBC', 'agent', {t - 7200}, {t - 3600});
        INSERT INTO result (patient_id, order_id, orderable_code, performed_at, reported_at) VALUES (1, 1, 'FBC', {t - 7200}, {t - 3600});
        INSERT INTO job (id, patient_id, description, created_by, created_at, done_by, done_at, outcome) VALUES (1, 1, 'Chase FBC', 'x', {t - 7000}, 'agent', {t - 3000}, 'Hb 90');
        INSERT INTO scenario_rule (patient_id, name, at, unless, effects) VALUES (1, 'later', {t + 600}, 'never', '{{"notes": [{{"offset_s": 0, "kind": "x", "title": "Later", "author": "Staff", "body": "."}}]}}');""")
    world = World.restore(conn.serialize())

    def shape(w):
        objects = w.q("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
        return objects, {o["name"]: [c["name"] for c in w.q(f'PRAGMA table_info("{o["name"]}")')] for o in objects if o["type"] == "table"}

    new_objects, new_columns = shape(World.create())
    old_objects, old_columns = shape(world)
    assert old_objects == new_objects and {t: sorted(c) for t, c in old_columns.items()} == {t: sorted(c) for t, c in new_columns.items()}
    assert world.one("SELECT status, collected_at FROM order_status") == {"status": "resulted", "collected_at": t - 7200}
    (job,) = call(world, "list_jobs", include_done=True)["jobs"]
    assert job["status"] == "done" and world.q("SELECT status FROM job_event ORDER BY id") == [{"status": "open"}, {"status": "done"}]
    call(world, "wait", seconds=600)
    assert world.one("SELECT title FROM note")["title"] == "Later"
    assert World.restore(world.snapshot()).one("SELECT value FROM meta WHERE key = 'schema'")["value"] == SCHEMA_VERSION
    assert world.q("SELECT status, origin FROM collection_event") == [{"status": "collected", "origin": "agent"}]
    assert world.one("SELECT origin FROM result WHERE order_id = 1")["origin"] == "staff"
    assert [a["applied_at"] for a in world.q("SELECT applied_at FROM scenario_application")] == [t + 600]  # read by the viewer


def test_grader_views_carry_provenance():
    world = world_of()
    action = ops.next_action_id(world)
    o = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round")["order_id"]
    row = world.one("SELECT origin, origin_action_id FROM order_status WHERE order_id = ?", (o,))
    assert row == {"origin": "agent", "origin_action_id": action}
    a = call(world, "record_assessment", patient_id=H, kind="wells_test", answers={"a": "yes"})["assessment_id"]
    j = call(world, "add_job", patient_id=H, description="x")["job_id"]
    for table, row_id in (("assessment", a), ("job", j)):
        assert world.one(f"SELECT origin FROM {table} WHERE id = ?", (row_id,))["origin"] == "agent"
    assert world.one("SELECT origin FROM job_event WHERE job_id = ?", (j,))["origin"] == "agent"


def test_nothing_hidden_leaks_through_the_new_operations():
    world = world_of()
    blob = json.dumps([call(world, "list_referral_pathways"), call(world, "list_assessment_forms"), call(world, "get_request_form", patient_id=H, orderable_code="USDVT")])
    assert LEFT not in blob and "Cardiology" not in blob  # no result window, and not the service a pathway resolves to


# --- independent review of step 3 (2026-11-17) ------------------------------------------------------------------------


def usdvt_problems(branches, default=None) -> list[str]:
    r = raw()
    window = r["patients"][0]["result_windows"][-1]
    window["branches"] = branches
    if default is not None:
        window["report_text"] = default
    return [p for p in lint_bundle(Bundle.model_validate(r)) if "USDVT" in p]


def test_review_1_every_side_needs_its_own_result_or_an_explicit_default():
    """A right-only branch reporting a DVT passed lint, and a bilateral request then came back normal."""
    right_only = "\n".join(usdvt_problems([{"request": {"laterality": "right"}, "report_text": "Acute right popliteal DVT."}]))
    assert "no result for a request with laterality left" in right_only and "no result for a request with laterality bilateral" in right_only
    # an explicit default covers the other side, but one text cannot also be the report of both legs
    with_default = usdvt_problems([{"request": {"laterality": "right"}, "report_text": "Acute right popliteal DVT."}], default="No DVT in the left leg.")
    assert len(with_default) == 1 and "none for a bilateral request" in with_default[0]
    assert usdvt_problems([{"request": {"laterality": ["right", "bilateral"]}, "report_text": "Acute right popliteal DVT."}], default="No DVT in the left leg.") == []
    # a bilateral report names each side: a unilateral finding inside it is not a contradiction
    assert usdvt_problems([{"request": {"laterality": "bilateral"}, "report_text": "Acute right popliteal DVT. Left leg normal."},
                           {"request": {"laterality": ["left", "right"]}, "report_text": "Normal study."}]) == []  # fmt: skip
    # the same for any field a result branches on
    urgent_only = "\n".join(usdvt_problems([{"request": {"laterality": ["left", "right", "bilateral"], "priority": "urgent"}, "report_text": "DVT."}]))
    assert "no result for a request with laterality left and priority routine" in urgent_only


def delayed_note(name, **fields):
    return {"name": name, "on_op": "write_note", "delay_s": 600, "effects": {"notes": [{"kind": "x", "title": name, "author": "Staff", "body": "."}]}, **fields}


def applications(world, name):
    return world.q("SELECT a.suppressed FROM scenario_application a JOIN scenario_rule r ON r.id = a.rule_id WHERE r.name = ? ORDER BY a.id", (name,))


def test_review_2_a_pending_effect_is_gated_again_when_it_falls_due():
    r = raw()
    r["patients"][0]["rules"] += [note_rule("stop", on_op="add_problem"), delayed_note("late-reply", unless="stop"),
                                  delayed_note("ward-reply"), delayed_note("letter", after_discharge="deliver")]  # fmt: skip
    world = world_of(r)
    call(world, "write_note", patient_id=H, title="t", body="b")
    call(world, "add_problem", patient_id=H, display="x")  # the suppressor takes effect before the reply is due
    call(world, "write_note", patient_id=H, kind="discharge_summary", title="Discharge summary", body="Home.")
    call(world, "discharge_patient", patient_id=H)
    call(world, "wait", seconds=3600)
    assert (fired(world, "late-reply"), fired(world, "ward-reply"), fired(world, "letter")) == (0, 0, 1)
    assert applications(world, "late-reply") == [{"suppressed": "unless: stop had taken effect"}]
    assert applications(world, "ward-reply") == [{"suppressed": "the patient had left the encounter"}]
    assert world.one("SELECT encounter_id FROM note WHERE title = 'letter'")["encounter_id"] is not None  # filed against the admission it answers


def whitcombe_referral(*setup, **referral):
    world = build_world(load_task("dx-chestpain-001"))
    patient = world.one("SELECT id FROM patient WHERE mrn = ?", (WHITCOMBE,))["id"]
    for name, args in setup:
        ops.call(world, name, {"patient_id": patient, **args} if name != "wait" else args)
    ops.call(world, "make_referral", {"patient_id": patient, "reason": "x", **referral})
    ops.call(world, "wait", {"seconds": 3600})
    reply = ops.call(world, "list_referrals", {"patient_id": patient})["referrals"][0]
    declined = world.one("SELECT 1 FROM note WHERE title = 'Nursing entry - escalation' AND patient_id = ?", (patient,)) is not None
    return reply["status"], (reply["response"] or {}).get("text", ""), declined


ONSET = "2026-03-14T08:30"


def test_review_3_the_demo_ppci_reply_acts_on_what_was_sent_and_what_is_on_the_record():
    status, text, declined = whitcombe_referral(pathway="ppci", pathway_details={"ecg_stemi": "no", "symptom_onset": ONSET})
    assert status == "advice" and "cath lab activated" not in text.lower() and "ECG" in text and declined  # no activation, no improvement
    status, text, declined = whitcombe_referral(pathway="ppci", pathway_details={"ecg_stemi": "yes", "symptom_onset": ONSET})
    assert status == "accepted" and "on your description of the ECG" in text and "ECG reviewed" not in text and not declined
    ecg = ("place_order", {"orderable_code": "ECG"}), ("wait", {"seconds": 600})
    status, text, _ = whitcombe_referral(*ecg, pathway="ppci", pathway_details={"ecg_stemi": "yes", "symptom_onset": ONSET})
    assert status == "accepted" and "ECG reviewed" in text
    status, text, declined = whitcombe_referral(specialty="Cardiology", urgency="urgent")  # the legacy general referral
    assert status == "accepted" and "ECG reviewed" not in text and not declined


@pytest.mark.parametrize("title, body", [("   ", "Seen."), ("Plan", " \n\t ")])
def test_review_4_a_document_needs_words(title, body):
    world = world_of()
    assert refused(world, "write_note", patient_id=H, kind="discharge_summary", title=title, body=body) == "invalid_arguments"
    note = call(world, "write_note", patient_id=H, title="Plan", body="Draft.", status="draft")["note_id"]
    world.conn.execute("UPDATE note SET body = ' ' WHERE id = ?", (note,))  # as a bundle could load it
    assert refused(world, "update_note", note_id=note, patient_id=H, status="final") == "invalid_arguments"


@pytest.mark.parametrize("values", [{"HEIGHT": "inf"}, {"HEIGHT": "nan"}, {"HEIGHT": 1}, {"HEIGHT": 400}, {"WEIGHT": "-inf"}, {"HR": 1000}, {"TEMP": "Infinity"}])
def test_review_5_absurd_or_non_finite_observations_are_refused(values):
    assert refused(world_of(), "record_observations", patient_id=H, values=values) == "invalid_arguments"
    c, _ = client(mode="ui")
    assert 'role="alert"' in c.post("/patients/1/observations", data={k: str(v) for k, v in values.items()}).text


def test_review_5_bounds_are_configurable_and_lint_checks_the_bundle():
    world = world_of(raw(observation_bounds={"HEIGHT": [40, 120]}))  # a children's ward
    call(world, "record_observations", patient_id=H, values={"HEIGHT": 95})
    assert refused(world, "record_observations", patient_id=H, values={"HEIGHT": 160}) == "invalid_arguments"
    r = raw()
    r["patients"][0]["observations"].append({"taken_at": "2026-05-01T10:00:00Z", "values": {"HEIGHT": 1600}})
    assert "HEIGHT 1600 is outside 30 to 250" in "\n".join(lint_bundle(Bundle.model_validate(r)))


def test_review_5_bmi_says_where_its_measurements_came_from():
    world = world_of()
    action = ops.next_action_id(world)
    call(world, "record_observations", patient_id=H, values={"HEIGHT": 160})
    call(world, "wait", seconds=60)
    call(world, "record_observations", patient_id=H, values={"WEIGHT": 64})
    row = world.one("SELECT weight_kg, weight_origin, height_origin, height_origin_action_id FROM bmi WHERE patient_id = ? ORDER BY taken_at DESC", (H,))
    assert row == {"weight_kg": 64, "weight_origin": "agent", "height_origin": "agent", "height_origin_action_id": action}


def duplicates(*orders):
    from eprbench.safety import safety_findings

    task = load_task("order-ue-001")
    world = build_world(task)
    for o in orders:
        ops.call(world, "place_order", {"patient_id": 2, "orderable_code": "UE", **o})
    return [f for f in safety_findings(world, parse_iso(task.start_time)) if f["code"] == "duplicate_request"]


def test_review_6_a_repeat_on_a_later_round_is_not_a_duplicate():
    assert duplicates({}, {"collection": "phlebotomy_round"}) == []
    assert len(duplicates({}, {})) == 1 and len(duplicates({}, {"collection": "scheduled", "collect_at": "2026-03-14T11:00"})) == 1
    assert duplicates({"collection": "phlebotomy_round"}, {"collection": "phlebotomy_round"})  # the same round twice


def test_review_7_a_scheduled_sample_is_collected_by_staff_not_by_the_agent():
    world = world_of()
    action = ops.next_action_id(world)
    later = call(world, "place_order", patient_id=H, orderable_code="FBC", collection="phlebotomy_round")["order_id"]
    now = call(world, "place_order", patient_id=H, orderable_code="CRP")["order_id"]
    audit = world.one("SELECT count(*) AS n FROM audit")["n"]
    call(world, "wait", seconds=86400)
    assert world.one("SELECT count(*) AS n FROM audit")["n"] == audit + 24  # the waits themselves, an hour each: no action at sampling time
    events = {e["order_id"]: e for e in world.q("SELECT * FROM collection_event")}
    assert (events[later]["origin"], events[later]["role"], events[later]["origin_action_id"], events[later]["status"]) == ("staff", "Phlebotomist", action, "collected")
    assert events[later]["at"] == parse_iso("2026-05-03T07:30:00Z") and events[now]["origin"] == "agent"
    assert world.one("SELECT origin, origin_action_id FROM result WHERE order_id = ?", (later,)) == {"origin": "staff", "origin_action_id": action}
    assert world.one('SELECT ordered_by, origin FROM "order" WHERE id = ?', (later,)) == {"ordered_by": "agent", "origin": "agent"}
    assert world.one("SELECT origin FROM result WHERE order_id IS NULL LIMIT 1")["origin"] == "record"


def test_review_8_every_answer_is_shown_in_the_ui_and_no_is_not_unanswered():
    r = raw()
    r["assessment_forms"] = [{"name": "f", "label": "Form", "fields": [{"name": "q", "label": "Question", "kind": "yes_no", "points": 1},
                                                                        {"name": "box", "label": "Box", "kind": "tick", "points": 1},
                                                                        {"name": "comment", "label": "Free comment", "kind": "text", "required": False}]}]  # fmt: skip
    c, app = client(lambda: world_of(r), mode="ui")
    c.post("/patients/1/assessments", data={"kind": "f", "af_q": "no"})
    html = c.get("/patients/1?tab=assessments").text
    assert "Question: No" in html and "Box: No" in html and "Free comment: not answered" in html and "nothing ticked" not in html
    (a,) = ops.call(app.state.world, "list_assessments", {"patient_id": 1})["assessments"]
    assert [(x["label"], x["value"]) for x in a["answers"]] == [("Question", "no"), ("Box", "no"), ("Free comment", None)]


def test_review_viewer_shows_every_application_of_a_repeating_response():
    from eprbench.viewer import world_events

    r = raw()
    r["patients"][0]["rules"].append(note_rule("each", on_op="write_note", repeat=True))
    world = world_of(r)
    start = world.now
    for _ in range(3):
        call(world, "wait", seconds=60)
        call(world, "write_note", patient_id=H, title="t", body="b")
    assert len([e for e in world_events(world, start, "agent") if e["name"] == "each"]) == 3
