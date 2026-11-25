"""The patient and the hospital respond to time and to what is done."""

import pytest
from pydantic import ValidationError

from eprbench import ops
from eprbench.loader import Rule
from eprbench.tasks import Check, build_world, grade, load_task
from tests.conftest import ELLISON, WHITCOMBE, calling

APIXABAN = {"drug": "Apixaban", "product": "Apixaban 5mg tablets", "dose": "10 mg", "route": "PO", "frequency": "BD", "override_reason": "replacing prophylactic enoxaparin"}


def latest_obs(call, patient):
    return call("list_observations", patient_id=patient, limit=1)["observation_sets"][0]


def titles(call, patient):
    return [n["title"] for n in call("list_notes", patient_id=patient)["notes"]]


def test_untreated_patient_deteriorates_on_schedule(call, pid):
    patient = pid(ELLISON)
    call("wait", seconds=4 * 3600 - 1)  # 13:29:59
    assert latest_obs(call, patient)["taken_at"] == "2026-03-14T08:50:00Z"
    call("wait", seconds=1)
    worse = latest_obs(call, patient)
    assert worse["taken_at"] == "2026-03-14T13:30:00Z" and worse["values"]["SPO2"] == 87 and worse["news2"] >= 11
    assert titles(call, patient)[0] == "Nursing entry - escalation"


def test_treatment_prevents_deterioration_and_brings_improvement(call, pid):
    patient = pid(ELLISON)
    call("prescribe_medication", patient_id=patient, **APIXABAN)
    assert latest_obs(call, patient)["taken_at"] == "2026-03-14T08:50:00Z"  # nothing visible yet
    call("wait", seconds=5 * 3600)
    better = latest_obs(call, patient)
    assert better["taken_at"] == "2026-03-14T13:30:00Z" and better["values"]["SPO2"] == 95
    assert "Nursing entry - escalation" not in titles(call, patient)


def test_treatment_after_deterioration_does_not_undo_it(call, pid):
    patient = pid(ELLISON)
    call("wait", seconds=5 * 3600)
    call("prescribe_medication", patient_id=patient, **APIXABAN)
    call("wait", seconds=5 * 3600)
    assert "Nursing entry - escalation" in titles(call, patient)
    assert latest_obs(call, patient)["values"]["SPO2"] == 95


def test_a_rule_fires_once_and_only_for_a_match(world, call, pid):
    patient = pid(ELLISON)
    call("prescribe_medication", patient_id=patient, drug="Paracetamol", dose="1 g", route="IV", frequency="once", kind="stat")
    assert world.one("SELECT fired_at FROM scenario_rule WHERE name = 'anticoagulated'")["fired_at"] is None
    call("prescribe_medication", patient_id=patient, **APIXABAN)
    call("prescribe_medication", patient_id=patient, drug="Rivaroxaban", dose="15 mg", route="PO", frequency="BD", override_reason="test")
    call("wait", seconds=5 * 3600)
    assert world.one("SELECT count(*) AS n FROM note WHERE body LIKE 'Pt more comfortable%'")["n"] == 1


def test_a_failed_operation_does_not_fire_rules(world, call, pid):
    with pytest.raises(ops.OpError):
        call("prescribe_medication", patient_id=pid(ELLISON), drug="Apixaban", dose="", route="PO", frequency="BD")
    assert world.one("SELECT fired_at FROM scenario_rule WHERE name = 'anticoagulated'")["fired_at"] is None


def mean_sbp(call, patient):
    trend = call("get_live_vitals", patient_id=patient, history_seconds=300, interval_seconds=10)["trend"]
    return sum(s["SBP"] for s in trend) / len(trend)


def test_monitor_trajectory_bends_when_the_patient_is_referred(pid):
    task = load_task("dx-chestpain-001")
    outcomes = {}
    for referred in (True, False):
        world = build_world(task)
        call = lambda name, **args: calling(world, name, args)  # noqa: E731
        patient = pid(WHITCOMBE)
        if referred:
            call("make_referral", patient_id=patient, specialty="Cardiology (PPCI)", urgency="urgent", reason="Inferior STEMI")
        call("wait", seconds=75 * 60)  # 10:30
        outcomes[referred] = (mean_sbp(call, patient), titles(call, patient)[0])
    assert outcomes[True][0] > 100 and outcomes[True][1] == "Cardiology SpR - telephone advice"
    assert outcomes[False][0] < 85 and outcomes[False][1] == "Nursing entry - escalation"


def test_bending_a_trajectory_is_continuous(world, pid):
    task = load_task("dx-chestpain-001")
    world = build_world(task)
    patient = pid(WHITCOMBE)
    before = ops.call(world, "get_live_vitals", {"patient_id": patient})["current"]["SBP"]
    ops.call(world, "make_referral", {"patient_id": patient, "specialty": "Cardiology", "reason": "STEMI"})
    after = ops.call(world, "get_live_vitals", {"patient_id": patient})["current"]["SBP"]
    assert before == after  # same instant, same reading: only the future changed


def test_grading_sees_rules_that_came_due_during_the_final_wait():
    task = load_task("order-ctpa-001")
    world = build_world(task)
    world.advance(6 * 3600)  # the clock moved but no operation has run since
    probe = task.model_copy(update={"checks": [*task.checks, Check(id="deteriorated", sql="SELECT 1 FROM scenario_rule WHERE name = 'untreated-deterioration' AND fired_at IS NOT NULL")]})
    report = grade(world, probe)
    assert next(c for c in report["checks"] if c["id"] == "deteriorated")["passed"]
    # grading fires them on its own copy: the world it was given is unchanged
    assert world.one("SELECT fired_at FROM scenario_rule WHERE name = 'untreated-deterioration'")["fired_at"] is None


def test_rule_needs_exactly_one_trigger():
    effects = {"notes": []}
    with pytest.raises(ValidationError):
        Rule(name="x", effects=effects)
    with pytest.raises(ValidationError):
        Rule(name="x", on_op="wait", at="2026-01-01T00:00:00Z", effects=effects)


def test_rules_are_hidden_and_deterministic(pid):
    task = load_task("order-ctpa-001")

    def run():
        world = build_world(task)
        ops.call(world, "prescribe_medication", {"patient_id": 1, **APIXABAN})
        calling(world, "wait", {"seconds": 20000})
        ops.call(world, "list_notes", {"patient_id": 1})
        return world.snapshot()

    assert run() == run()
    assert not any("rule" in name or "scenario" in name for name in ops.REGISTRY)
