"""The audit log names the patient every operation concerned, however the arguments were written."""

import pytest

from eprbench import ops
from eprbench.tasks import build_world, grade, load_task
from tests.conftest import CTPA_ELLISON, ELLISON, ELLISTON, with_product


def last_audit(world):
    return world.one("SELECT op, ok, error, patient_id FROM audit ORDER BY id DESC LIMIT 1")


def attempt(world, name, args):
    try:
        return ops.call(world, name, args)
    except ops.OpError as e:
        return e


def test_a_patient_id_written_as_a_string_is_still_audited_against_that_patient(world, pid):
    patient = pid(ELLISTON)
    ops.call(world, "write_note", {"patient_id": str(patient), "title": "Review", "body": "Seen."})
    assert last_audit(world) == {"op": "write_note", "ok": 1, "error": None, "patient_id": patient}
    attempt(world, "write_note", {"patient_id": str(patient), "title": "", "body": "Seen."})  # fails validation
    assert last_audit(world)["patient_id"] == patient


def test_a_write_to_another_patient_by_string_id_fails_the_wrong_patient_check():
    """Reproduces review item A1: this run used to score 1.0."""
    task = load_task("order-ue-001")
    world = build_world(task)
    elliston = world.one("SELECT id FROM patient WHERE mrn = ?", (ELLISTON,))["id"]
    ellison = world.one("SELECT id FROM patient WHERE mrn = ?", (ELLISON,))["id"]
    ops.call(world, "place_order", {"patient_id": elliston, "orderable_code": "UE"})
    assert grade(world, task)["score"] == 1.0
    ops.call(world, "write_note", {"patient_id": str(ellison), "title": "Oops", "body": "Wrong patient."})
    report = grade(world, task)
    assert report["score"] == 0 and not report["success"]


def setup_objects(world, pid):
    """One of each object an operation can refer to by id, all belonging to Ellison."""
    patient = pid(ELLISON)
    note = ops.call(world, "write_note", {"patient_id": patient, "title": "t", "body": "b", "status": "draft"})["note_id"]
    order = ops.call(world, "place_order", {"patient_id": patient, "orderable_code": "CTPA", "clinical_details": "?PE", "request_details": CTPA_ELLISON})["order_id"]
    result = ops.call(world, "list_results", {"patient_id": patient})["results"][0]["result_id"]
    med = ops.call(world, "prescribe_medication", with_product({"patient_id": patient, "drug": "Paracetamol", "dose": "1 g", "route": "PO", "frequency": "QDS"}))["medication_id"]
    job = ops.call(world, "add_job", {"patient_id": patient, "description": "Chase CTPA"})["job_id"]
    return patient, {"note_id": note, "order_id": order, "result_id": result, "medication_id": med, "job_id": job}


def record(world):
    return [world.q(f"SELECT * FROM {t} ORDER BY id") for t in ("note", '"order"', "result", "medication", "administration", "job")]


REFERENCING = [
    ("get_note", "note_id", {}),
    ("get_result", "result_id", {}),
    ("acknowledge_result", "result_id", {}),
    ("cancel_order", "order_id", {}),
    ("stop_medication", "medication_id", {"reason": "not needed"}),
    ("record_administration", "medication_id", {"status": "given"}),
    ("complete_job", "job_id", {"outcome": "done"}),
    ("cancel_job", "job_id", {"reason": "not needed"}),
    ("update_note", "note_id", {"body": "edited", "status": "final"}),
    ("add_job", "order_id", {"description": "Chase CTPA"}),
]


@pytest.mark.parametrize("name, field, extra", REFERENCING)
def test_operations_on_a_referenced_object_are_audited_against_its_owner(world, pid, name, field, extra):
    patient, ids = setup_objects(world, pid)
    attempt(world, name, {field: str(ids[field]), **extra})  # a string id, as some clients send
    first = last_audit(world)
    assert first["patient_id"] == patient, first
    attempt(world, name, {field: ids[field], **extra})  # repeated: many of these now fail (already done)
    assert last_audit(world)["patient_id"] == patient


@pytest.mark.parametrize("name, field, extra", REFERENCING)
def test_an_object_of_another_patient_is_refused_when_the_patient_is_named(world, pid, name, field, extra):
    patient, ids = setup_objects(world, pid)
    before = record(world)
    error = attempt(world, name, {field: ids[field], "patient_id": pid(ELLISTON), **extra})
    assert isinstance(error, ops.OpError) and error.code == "wrong_patient", error
    assert last_audit(world) == {"op": name, "ok": 0, "error": "wrong_patient", "patient_id": patient}
    assert record(world) == before  # nothing changed
    assert not isinstance(attempt(world, name, {field: ids[field], "patient_id": patient, **extra}), ops.OpError)


def test_every_operation_is_either_patient_specific_or_declared_not_to_be():
    """A write that changes a patient's record must name the patient, directly or through an object it refers to."""
    for o in ops.REGISTRY.values():
        fields = set(o.args_model.model_fields)
        identifies = "patient_id" in fields or bool(fields & set(ops.OWNED_BY))
        assert o.patient_specific == identifies, o.name
        if o.writes and not identifies:
            assert o.name in ops.UNATTRIBUTED_WRITES, o.name


def test_a_successful_patient_write_cannot_go_unattributed(world, pid, monkeypatch):
    """Belt and braces: if an operation ever succeeded without its patient being known, it is rolled back."""
    patient = pid(ELLISON)
    monkeypatch.setattr(ops, "_owner", lambda world, values, check=True: None)
    notes = world.one("SELECT count(*) AS n FROM note")["n"]
    with pytest.raises(ops.OpError) as e:
        ops.call(world, "write_note", {"patient_id": patient, "title": "t", "body": "b"})
    assert e.value.code == "unattributed_write"
    assert world.one("SELECT count(*) AS n FROM note")["n"] == notes


def test_successful_writes_are_audited_against_the_patient_in_their_response(world, pid):
    """Whatever an operation reports about its patient agrees with the audit."""
    _, ids = setup_objects(world, pid)
    for name, field, extra in REFERENCING:
        out = attempt(world, name, {field: ids[field], **extra})
        if isinstance(out, dict) and "patient_id" in out:
            assert out["patient_id"] == last_audit(world)["patient_id"], name


def test_a_failed_call_is_attributed_only_through_identifiers_its_operation_declares(world, pid):
    """Review: an undeclared note_id on write_note made a failed call count against note 5's owner."""
    elliston, ellison = pid(ELLISTON), pid(ELLISON)
    assert world.one("SELECT patient_id FROM note WHERE id = 5")["patient_id"] == ellison
    attempt(world, "write_note", {"patient_id": elliston, "title": "t", "body": "b", "note_id": 5})
    assert last_audit(world) == {"op": "write_note", "ok": 0, "error": "invalid_arguments", "patient_id": elliston}
    attempt(world, "search_patients", {"query": "x", "patient_id": ellison})  # not a patient operation at all
    assert last_audit(world)["patient_id"] is None
    attempt(world, "no_such_operation", {"patient_id": ellison})
    assert last_audit(world)["patient_id"] is None
