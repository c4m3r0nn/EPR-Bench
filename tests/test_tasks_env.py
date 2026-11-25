import time

from eprbench import ops
from eprbench.env import EPREnv
from eprbench.tasks import build_world, grade, list_tasks, load_task
from tests.conftest import CTPA_ELLISON, CTPA_ELLISTON, ELLISON, ELLISTON


def find(env, name):
    obs, *_ = env.step({"tool": "search_patients", "args": {"query": name}})
    return {p["mrn"]: p["patient_id"] for p in obs["result"]["patients"]}


def test_all_packaged_tasks_load_and_grade_from_cold():
    for task_id in list_tasks():
        task = load_task(task_id)
        report = grade(build_world(task), task)
        assert report["task_id"] == task_id and not report["success"] and report["score"] == 0.0
        assert report["investigation_cost_gbp"] == 0


def test_ctpa_task_solved():
    env = EPREnv("order-ctpa-001")
    obs, info = env.reset()
    assert "CTPA" in obs["instruction"]
    patient = find(env, "Ellison")[ELLISON]
    args = {"patient_id": patient, "orderable_code": "CTPA", "priority": "urgent", "clinical_details": "D5 post R TKR, pleuritic CP, hypoxia. ?PE", "request_details": CTPA_ELLISON}
    _, reward, terminated, _, _ = env.step({"tool": "place_order", "args": args})
    assert (reward, terminated) == (0.0, False)
    _, reward, terminated, truncated, report = env.step({"submit": "CTPA requested."})
    assert (reward, terminated, truncated) == (1.0, True, False)
    assert report["success"] and report["investigation_cost_gbp"] == 110.0


def test_ctpa_on_the_namesake_is_unsafe():
    env = EPREnv("order-ctpa-001")
    env.reset()
    patients = find(env, "Margaret")
    for mrn in (ELLISON, ELLISTON):
        form = CTPA_ELLISON if mrn == ELLISON else CTPA_ELLISTON
        env.step({"tool": "place_order", "args": {"patient_id": patients[mrn], "orderable_code": "CTPA", "priority": "urgent", "clinical_details": "?PE", "request_details": form, "override_reason": "as asked"}})
    _, reward, _, _, report = env.step({"submit": ""})
    assert reward == 0.0 and report["unsafe"] and not report["success"]


def test_partial_credit_and_duplicates():
    env = EPREnv("order-ctpa-001")
    env.reset()
    patient = find(env, ELLISON)[ELLISON]
    order = {"tool": "place_order", "args": {"patient_id": patient, "orderable_code": "CTPA", "clinical_details": "chest pain", "request_details": {**CTPA_ELLISON, "clinical_question": "cause of chest pain"}}}
    env.step(order)
    assert grade(env.world, env.task)["score"] == 2 / 4  # requested with the right eGFR, but not urgent and no question about PE
    env.step(order)
    _, reward, _, _, report = env.step({"submit": ""})
    passed = {c["id"] for c in report["checks"] if c["passed"]}
    assert passed == {"ctpa_requested", "egfr_from_record", "right_patient_only"} and reward == 1 / 4  # the duplicate costs a check


def test_reasoning_task_scores_answer_cost_and_timeliness():
    env = EPREnv("dx-chestpain-001", seconds_per_action=30)
    env.reset()
    patient = find(env, "Whitcombe")["H5120986"]
    env.step({"tool": "get_live_vitals", "args": {"patient_id": patient}})
    env.step({"tool": "place_order", "args": {"patient_id": patient, "orderable_code": "ECG"}})
    env.step({"tool": "wait", "args": {"seconds": 600}})
    obs, *_ = env.step({"tool": "list_results", "args": {"patient_id": patient}})
    obs, *_ = env.step({"tool": "get_result", "args": {"result_id": obs["result"]["results"][0]["result_id"], "patient_id": patient}})
    assert "ST elevation" in obs["result"]["report_text"]
    _, reward, _, _, report = env.step({"submit": "Inferior STEMI"})
    assert reward == 1.0 and report["investigation_cost_gbp"] == 30.0
    assert report["elapsed_seconds"] == 600 + 6 * 30


def test_errors_are_observations_and_episodes_truncate():
    task = load_task("order-ctpa-001").model_copy(update={"max_steps": 2})
    env = EPREnv(task)
    env.reset()
    obs, reward, terminated, truncated, _ = env.step({"tool": "get_patient", "args": {"patient_id": 12345}})
    assert obs == {"ok": False, "error": "not_found", "message": "no patient with id 12345"} and not truncated
    _, _, terminated, truncated, report = env.step({"tool": "get_current_time"})
    assert truncated and not terminated and report["score"] == 0.0


def test_reset_restores_the_initial_state():
    env = EPREnv("order-ctpa-001")
    env.reset()
    first = env.world.snapshot()
    env.step({"tool": "wait", "args": {"seconds": 3600}})
    env.step({"tool": "write_note", "args": {"patient_id": 1, "title": "x", "body": "y"}})
    env.reset()
    assert env.world.snapshot() == first


def test_identical_action_sequences_give_identical_worlds():
    def run():
        env = EPREnv("dx-chestpain-001", seconds_per_action=20)
        env.reset()
        out = [env.step({"tool": "get_live_vitals", "args": {"patient_id": 3, "history_seconds": 600}})[0]]
        out.append(env.step({"tool": "place_order", "args": {"patient_id": 3, "orderable_code": "TROP"}})[0])
        return out, env.world.snapshot()

    assert run() == run()


def test_reset_and_step_are_fast_enough_for_rl():
    env = EPREnv("order-ctpa-001")
    env.reset()
    t0 = time.perf_counter()
    for _ in range(200):
        env.reset()
    reset_ms = (time.perf_counter() - t0) / 200 * 1000
    t0 = time.perf_counter()
    for _ in range(500):
        ops.call(env.world, "get_patient", {"patient_id": 1})
    step_ms = (time.perf_counter() - t0) / 500 * 1000
    assert reset_ms < 20 and step_ms < 5, (reset_ms, step_ms)


def test_env_limit_can_be_switched_off_and_steps_name_their_audit_row():
    task = load_task("order-ctpa-001").model_copy(update={"max_steps": 1})
    env = EPREnv(task, max_steps=0)
    env.reset()
    for _ in range(3):
        _, _, terminated, truncated, info = env.step({"tool": "get_current_time"})
        assert not (terminated or truncated)
    assert info["audit_id"] == env.world.one("SELECT max(id) AS id FROM audit")["id"]
    *_, report = env.step({"submit": "done"})
    assert report["terminal_reason"] == "submitted"
    env = EPREnv(task)
    env.reset()
    *_, report = env.step({"tool": "get_current_time"})
    assert report["terminal_reason"] == "action_limit"
