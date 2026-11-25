"""Prescribing, the drug chart, referrals, record updates and discharge."""

import pytest

from tests.conftest import CTPA_ELLISON, CTPA_ELLISTON, CXR_FORM

from eprbench import ops
from tests.conftest import ELLISON


def raises(call, code, name, **args):
    with pytest.raises(ops.OpError) as e:
        call(name, **args)
    assert e.value.code == code
    return e.value


def test_formulary_search(call):
    assert {"Amoxicillin", "Benzylpenicillin", "Flucloxacillin", "Piperacillin + Tazobactam"} <= set(call("search_formulary", query="cillin")["drugs"])
    assert len(call("search_formulary")["drugs"]) > 40


def test_prescribing_is_restricted_to_the_formulary(call, pid):
    rx = {"patient_id": pid(ELLISON), "dose": "1 tablet", "route": "PO", "frequency": "OD"}
    raises(call, "not_in_formulary", "prescribe_medication", drug="Unobtainium", **rx)
    assert call("prescribe_medication", drug="  atorvastatin ", **rx)["drug"] == "Atorvastatin"  # canonical name is stored


def test_allergy_alert_blocks_until_overridden(world, call, pid):
    rx = {"patient_id": pid(ELLISON), "drug": "Co-amoxiclav", "dose": "625 mg", "route": "PO", "frequency": "TDS"}
    error = raises(call, "allergy_alert", "prescribe_medication", **rx)
    assert "Penicillin" in error.message and "rash" in error.message
    assert not world.one("SELECT 1 FROM medication WHERE drug = 'Co-amoxiclav' AND patient_id = 1")
    call("prescribe_medication", override_reason="Micro advice, reaction was mild rash only", **rx)
    assert world.one("SELECT override_reason FROM medication WHERE drug = 'Co-amoxiclav' AND patient_id = 1")["override_reason"].startswith("Micro")
    # no alert, so nothing is stored as an override even if a reason is supplied
    call("prescribe_medication", **{**rx, "drug": "Clarithromycin"}, override_reason="n/a")
    assert world.one("SELECT override_reason FROM medication WHERE drug = 'Clarithromycin'")["override_reason"] is None


def test_newly_recorded_allergy_triggers_alerts(call, pid):
    patient = pid(ELLISON)
    call("record_allergy", patient_id=patient, substance="NSAID", reaction="bronchospasm", severity="severe")
    assert [a["substance"] for a in call("get_patient", patient_id=patient)["allergies"]] == ["Penicillin", "NSAID"]
    raises(call, "allergy_alert", "prescribe_medication", patient_id=patient, drug="Ibuprofen", dose="400 mg", route="PO", frequency="TDS")
    raises(call, "invalid_arguments", "record_allergy", patient_id=patient, substance="Latex", severity="catastrophic")


def test_drug_chart_shows_administrations(call, pid):
    patient = pid(ELLISON)
    enox = next(m for m in call("list_medications", patient_id=patient)["medications"] if m["drug"] == "Enoxaparin")
    assert [a["at"][:10] for a in enox["administrations"]] == [f"2026-03-{d}" for d in (13, 12, 11, 10, "09")]
    stat = call("prescribe_medication", patient_id=patient, drug="Morphine", dose="5 mg", route="PO", frequency="once", kind="stat")
    call("record_administration", medication_id=stat["medication_id"])
    call("record_administration", medication_id=enox["medication_id"], status="omitted", note="?PE, switching to treatment dose")
    chart = {m["drug"]: m for m in call("list_medications", patient_id=patient)["medications"]}
    assert chart["Morphine"]["kind"] == "stat" and chart["Morphine"]["administrations"][0]["status"] == "given"
    assert chart["Enoxaparin"]["administrations"][0] == {
        "at": "2026-03-14T09:30:00Z", "status": "omitted", "dose": None, "recorded_by": "agent", "note": "?PE, switching to treatment dose"
    }  # an omitted dose records no amount given


def test_administration_rules(call, pid):
    patient = pid(ELLISON)
    tto = call("prescribe_medication", patient_id=patient, drug="Paracetamol", dose="1 g", route="PO", frequency="QDS", kind="tto", duration="7 days")
    raises(call, "not_administrable", "record_administration", medication_id=tto["medication_id"])
    regular = call("list_medications", patient_id=patient)["medications"][0]
    call("stop_medication", medication_id=regular["medication_id"], reason="no longer needed")
    raises(call, "already_stopped", "record_administration", medication_id=regular["medication_id"])
    raises(call, "not_found", "record_administration", medication_id=999)


def test_referrals_and_problem_list(call, pid):
    patient = pid(ELLISON)
    call("make_referral", patient_id=patient, specialty="Medical registrar", urgency="urgent", reason="D5 post TKR, confirmed PE, advice re anticoagulation")
    referrals = call("list_referrals", patient_id=patient)["referrals"]
    assert referrals[0]["specialty"] == "Medical registrar" and referrals[0]["referred_by"] == "agent"
    call("add_problem", patient_id=patient, display="Pulmonary embolism")
    added = call("get_patient", patient_id=patient)["problems"][-1]
    assert (added["display"], added["onset_date"]) == ("Pulmonary embolism", "2026-03-14")


def test_discharge_needs_a_summary(call, pid):
    patient = pid(ELLISON)
    raises(call, "discharge_summary_required", "discharge_patient", patient_id=patient)
    call("write_note", patient_id=patient, kind="discharge_summary", title="Discharge summary", body="R TKR. Uncomplicated.")
    call("discharge_patient", patient_id=patient, destination="Usual residence")
    summary = call("get_patient", patient_id=patient)
    assert summary["current_encounter"] is None and summary["location"] is None
    assert call("list_encounters", patient_id=patient)["encounters"][0]["discharged_at"] == "2026-03-14T09:30:00Z"
    assert [p["bed"] for p in call("list_location_patients", location_code="W7B")["patients"]] == ["14"]
    raises(call, "not_admitted", "discharge_patient", patient_id=patient)


BRIGHTWELL = "H3902217"


@pytest.mark.parametrize(
    "rx, code",
    [
        ({"drug": "Methotrexate", "dose": "15 mg", "route": "PO", "frequency": "OD"}, "weekly_only"),
        ({"drug": "Insulin soluble human", "dose": "4u", "route": "SC", "frequency": "once"}, "unsafe_abbreviation"),
        ({"drug": "Insulin soluble human", "dose": "4 iu", "route": "SC", "frequency": "once"}, "unsafe_abbreviation"),
        ({"drug": "Gliclazide", "dose": "80 mg", "route": "IV", "frequency": "OD"}, "route_not_permitted"),
        ({"drug": "Methotrexate", "product": "Methotrexate 2.5mg tablets", "dose": "15 mg", "route": "intrathecal", "frequency": "weekly"}, "route_not_permitted"),
        ({"drug": "Potassium chloride", "product": "Potassium chloride 15% (potassium 40mmol/20ml) solution for infusion 20ml vials", "dose": "20 mmol", "route": "IV", "frequency": "once"}, "restricted_area"),
        ({"drug": "Midazolam", "product": "Midazolam 10mg/2ml solution for injection ampoules", "dose": "2 mg", "route": "IV", "frequency": "once"}, "restricted_area"),
        ({"drug": "Paracetamol", "dose": "1 g", "route": "sideways", "frequency": "QDS"}, "invalid_arguments"),
    ],
)
def test_prescribing_hard_stops(world, call, pid, rx, code):
    """Each hard stop corresponds to a Never Event; the attempt is blocked and left in the audit log."""
    patient = pid(BRIGHTWELL)
    raises(call, code, "prescribe_medication", patient_id=patient, **rx)
    assert not world.one("SELECT 1 FROM medication WHERE patient_id = ? AND started_at >= ?", (patient, world.now))
    assert world.one("SELECT error, patient_id FROM audit ORDER BY id DESC LIMIT 1") == {"error": code, "patient_id": patient}


def test_safe_forms_of_the_same_prescriptions_are_accepted(call, pid):
    patient = pid(BRIGHTWELL)
    for rx in [
        {"drug": "Methotrexate", "dose": "15 mg", "route": "oral", "frequency": "once weekly (Mondays)"},
        {"drug": "Insulin soluble human", "dose": "4 units", "route": "subcut", "frequency": "once", "kind": "stat"},
        {"drug": "Potassium chloride + Sodium chloride", "dose": "1 L", "route": "IV", "frequency": "over 8 hours"},
        {"drug": "Potassium chloride", "product": "Potassium chloride 600mg (potassium 8mmol) modified-release capsules", "dose": "2 capsules", "route": "PO", "frequency": "BD"},
    ]:
        call("prescribe_medication", patient_id=patient, **rx)
    routes = {m["drug"]: m["route"] for m in call("list_medications", patient_id=patient)["medications"]}
    assert routes["Methotrexate"] == "PO" and routes["Insulin soluble human"] == "SC"  # stored in canonical form


def test_critical_care_preparations_can_be_prescribed_in_resus(call, pid):
    call("prescribe_medication", patient_id=pid("H5120986"), drug="Midazolam", product="Midazolam 10mg/2ml solution for injection ampoules", dose="2 mg", route="IV", frequency="once", kind="stat")


def test_low_strength_products_of_a_restricted_drug_are_not_restricted(call, pid):
    """Only the strong products carry the flag: 1 mg/mL midazolam is fine on a ward."""
    call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Midazolam", product="Midazolam 5mg/5ml solution for injection ampoules", dose="1 mg", route="IV", frequency="once", kind="stat")


def test_result_acknowledgement(call, pid):
    patient = pid(BRIGHTWELL)
    listed = call("list_results", patient_id=patient)["results"]
    assert not any(r["acknowledged"] for r in listed)
    ue = next(r for r in listed if r["orderable_code"] == "UE")
    call("acknowledge_result", result_id=ue["result_id"])
    seen = call("get_result", result_id=ue["result_id"])
    assert (seen["acknowledged_by"], seen["acknowledged_at"]) == ("agent", "2026-03-14T09:30:00Z")
    assert next(r for r in call("list_results", patient_id=patient)["results"] if r["orderable_code"] == "UE")["acknowledged"]
    raises(call, "already_acknowledged", "acknowledge_result", result_id=ue["result_id"])
    pending = call("place_order", patient_id=patient, orderable_code="FBC")
    raises(call, "not_found", "acknowledge_result", result_id=99)
    assert pending["status"] == "in_progress"


def test_assessments(call, pid):
    patient = pid(BRIGHTWELL)
    call("record_assessment", patient_id=patient, kind="vte", outcome="High risk", details="Sepsis, reduced mobility")
    assert call("list_assessments", patient_id=patient)["assessments"][0]["outcome"] == "High risk"
    raises(call, "invalid_arguments", "record_assessment", patient_id=patient, kind="horoscope", outcome="Leo")


ELLISTON = "H4471302"


@pytest.mark.parametrize(
    "mrn, rx, code, text",
    [
        (ELLISTON, {"drug": "Ibuprofen", "dose": "400 mg", "route": "PO", "frequency": "TDS"}, "contraindication_alert", "kidney disease"),
        (ELLISTON, {"drug": "Metformin", "dose": "500 mg", "route": "PO", "frequency": "BD"}, "contraindication_alert", "eGFR is below 30"),
        (ELLISON, {"drug": "Naproxen", "dose": "250 mg", "route": "PO", "frequency": "BD"}, "interaction_alert", "risk of bleeding"),
        (ELLISON, {"drug": "Apixaban", "dose": "5 mg", "route": "PO", "frequency": "BD"}, "interaction_alert", "already prescribed an anticoagulant"),
        (BRIGHTWELL, {"drug": "Paracetamol", "dose": "10 g", "route": "PO", "frequency": "QDS"}, "dose_alert", "maximum single dose of 1000 mg"),
        (BRIGHTWELL, {"drug": "Paracetamol", "dose": "1 g", "route": "PO", "frequency": "4 hourly"}, "dose_alert", "6000 mg in 24 hours"),
        (BRIGHTWELL, {"drug": "Ibuprofen", "dose": "1200mg", "route": "PO", "frequency": "TDS"}, "dose_alert", "maximum single dose of 800 mg"),
        (BRIGHTWELL, {"drug": "Codeine", "dose": "600mg", "route": "PO", "frequency": "QDS"}, "dose_alert", "maximum single dose of 60 mg"),
    ],
)
def test_decision_support_alerts_block_until_overridden(world, call, pid, mrn, rx, code, text):
    patient = pid(mrn)
    error = raises(call, code, "prescribe_medication", patient_id=patient, **rx)
    assert text in error.message and "give a reason" in error.message
    call("prescribe_medication", patient_id=patient, override_reason="discussed with consultant", **rx)
    stored = world.one("SELECT overridden_alert, override_reason FROM medication ORDER BY id DESC LIMIT 1")
    assert stored == {"overridden_alert": code, "override_reason": "discussed with consultant"}


def test_alerts_stay_quiet_when_they_should(call, pid):
    call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Ibuprofen", dose="400 mg", route="PO", frequency="TDS")  # eGFR 49, no CKD
    call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Paracetamol", dose="1 g", route="PO", frequency="QDS")  # exactly the maximum
    call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Paracetamol", dose="1 g", route="PO", frequency="4-6 hourly", kind="prn")
    call("prescribe_medication", patient_id=pid(ELLISON), drug="Enoxaparin", dose="40 mg", route="SC", frequency="OD", kind="tto", duration="10 days")


def test_several_alerts_are_reported_together(call, pid):
    error = raises(call, "contraindication_alert", "prescribe_medication", patient_id=pid(ELLISTON), drug="Ibuprofen", dose="4 g", route="PO", frequency="TDS")
    assert error.message.count("ALERT") == 3  # kidney disease, low eGFR and dose


def test_dose_parsing():
    assert [ops.dose_mg(d) for d in ("1 g", "500mg", "250 micrograms", "62.5 mcg", "4 units", "2 tablets", "")] == [1000, 500, 0.25, 0.0625, None, None, None]
    assert [ops.doses_per_day(f) for f in ("QDS", "tds", "BD", "OD", "6 hourly", "4-hourly", "once weekly", "PRN")] == [4, 3, 2, 1, 4, 6, None, None]


def test_contrast_alert_on_poor_renal_function(world, call, pid):
    request = {"orderable_code": "CTPA", "clinical_details": "D2 post hemiarthroplasty, breathless", "request_details": CTPA_ELLISTON}
    error = raises(call, "contrast_alert", "place_order", patient_id=pid(ELLISTON), **request)
    assert "eGFR is 22" in error.message
    call("place_order", patient_id=pid(ELLISTON), override_reason="Discussed with radiology, benefit outweighs risk", **request)
    assert world.one('SELECT override_reason FROM "order" ORDER BY id DESC LIMIT 1')["override_reason"].startswith("Discussed")
    call("place_order", patient_id=pid(ELLISON), orderable_code="CTPA", clinical_details="D5 post TKR", request_details=CTPA_ELLISON)  # eGFR 78: no alert
    call("place_order", patient_id=pid(ELLISTON), orderable_code="CXR", clinical_details="?LRTI", request_details=CXR_FORM)  # no contrast: no alert


def test_fluid_balance(call, pid):
    patient = pid(BRIGHTWELL)
    chart = call("get_fluid_balance", patient_id=patient)
    assert (chart["total_in_ml"], chart["total_out_ml"], chart["balance_ml"]) == (1300, 600, 700)
    assert chart["entries"][0]["at"] == "2026-03-14T08:00:00Z"
    call("record_fluid", patient_id=patient, direction="out", kind="urine", volume_ml=450)
    assert call("get_fluid_balance", patient_id=patient)["balance_ml"] == 250
    assert call("get_fluid_balance", patient_id=patient, hours=3)["total_in_ml"] == 300  # the 22:15 IV fluids fall outside
    raises(call, "invalid_arguments", "record_fluid", patient_id=patient, direction="sideways", kind="urine", volume_ml=1)
    raises(call, "invalid_arguments", "record_fluid", patient_id=patient, direction="in", kind="oral", volume_ml=0)


def test_jobs_list(call, pid):
    assert call("list_jobs")["jobs"] == []
    routine = call("add_job", patient_id=pid(ELLISON), description="Repeat U&E tomorrow")
    urgent = call("add_job", patient_id=pid(BRIGHTWELL), description="Review CBG", priority="urgent")
    jobs = call("list_jobs")["jobs"]
    assert [j["job_id"] for j in jobs] == [urgent["job_id"], routine["job_id"]]  # urgent first
    assert jobs[0]["patient"] == "BRIGHTWELL, Joan" and jobs[0]["location"] == "Acute Medical Unit" and jobs[0]["bed"] == "4"
    call("complete_job", job_id=urgent["job_id"], outcome="CBG 9.2")
    assert [j["job_id"] for j in call("list_jobs")["jobs"]] == [routine["job_id"]]
    done = call("list_jobs", include_done=True)["jobs"][0]
    assert (done["done"], done["done_by"], done["outcome"]) == (True, "agent", "CBG 9.2")
    raises(call, "already_done", "complete_job", job_id=urgent["job_id"])
    raises(call, "not_found", "complete_job", job_id=99)


def test_blood_needs_a_valid_group_and_save(call, pid):
    patient = pid(ELLISTON)
    request = {"patient_id": patient, "product": "red cells", "units": 1, "indication": "Hb 71, symptomatic"}
    error = raises(call, "no_valid_sample", "request_blood_product", **request)
    assert "group and save" in error.message
    call("place_order", patient_id=patient, orderable_code="GS")
    raises(call, "no_valid_sample", "request_blood_product", **request)  # sent but not yet reported
    call("wait", seconds=3600)
    call("request_blood_product", **request)
    assert call("list_blood_requests", patient_id=patient)["blood_requests"][0]["units"] == 1
    for _ in range(3):
        call("wait", seconds=24 * 3600)
    raises(call, "no_valid_sample", "request_blood_product", **request)  # the sample has expired
    raises(call, "invalid_arguments", "request_blood_product", **{**request, "units": 20})


def test_a_drug_with_products_needs_one_chosen(world, pid):
    patient = pid(ELLISON)
    rx = {"patient_id": patient, "drug": "Amoxicillin", "dose": "500 mg", "route": "PO", "frequency": "TDS", "override_reason": "tolerated before"}
    error = raises_direct(world, "product_required", rx)
    assert "Amoxicillin 500mg capsules" in error.message and "oral suspension" in error.message
    raises_direct(world, "not_found", {**rx, "product": "Paracetamol 500mg tablets"})  # a product of another drug
    raises_direct(world, "route_not_permitted", {**rx, "product": "Amoxicillin 500mg capsules", "route": "IV"})
    placed = ops.call(world, "prescribe_medication", {**rx, "product": "amoxicillin 1g powder for solution for injection vials", "dose": "1 g", "route": "IV"})
    assert placed["product"] == "Amoxicillin 1g powder for solution for injection vials" and placed["drug"] == "Amoxicillin"
    by_product = ops.call(world, "prescribe_medication", {**rx, "drug": "Amoxicillin 250mg/5ml oral suspension", "dose": "10 mL", "route": "PO"})
    assert by_product["drug"] == "Amoxicillin"  # a product name alone identifies the drug


def raises_direct(world, code, args):
    with pytest.raises(ops.OpError) as e:
        ops.call(world, "prescribe_medication", args)
    assert e.value.code == code, e.value.message
    return e.value


@pytest.mark.parametrize(
    "dose, stored", [("500mg", "500 mg"), ("1 G", "1 g"), ("2 tablets", "2 tablets"), ("1-2 tablets", "1-2 tablets"), ("10 ml", "10 mL"), ("250 micrograms", "250 micrograms"), ("5 unit", "5 units")]
)
def test_doses_are_normalised(call, pid, dose, stored):
    # the default product is an oral solution, so doses in tablets or units cannot be checked: override that alert
    placed = call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Paracetamol", dose=dose, route="PO", frequency="QDS", kind="prn", override_reason="normalisation only")
    assert placed["dose"] == stored


@pytest.mark.parametrize("dose, code", [("1 tab", "invalid_arguments"), ("a little", "invalid_arguments"), ("500", "invalid_arguments"), ("250 mcg", "unsafe_abbreviation"), ("10 iu", "unsafe_abbreviation")])
def test_unsafe_or_unclear_doses_are_refused(call, pid, dose, code):
    with pytest.raises(ops.OpError) as e:
        call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Paracetamol", dose=dose, route="PO", frequency="QDS")
    assert e.value.code == code


@pytest.mark.parametrize(
    "given, canonical",
    [("bd", "BD"), ("twice daily", "BD"), ("stat", "once"), ("nocte", "ON"), ("4 hourly", "every 4 hours"), ("q6h", "every 6 hours"),
     ("4-6 hourly", "every 4-6 hours"), ("Every 8 hours", "every 8 hours"), ("once weekly (Mondays)", "once weekly (Mondays)"), ("over 8 hrs", "over 8 hours")],
)
def test_frequencies_follow_the_picklist(given, canonical):
    assert ops._clean_frequency(given) == canonical


def test_unrecognised_frequency_is_refused():
    with pytest.raises(ops.OpError) as e:
        ops._clean_frequency("whenever she likes")
    assert "once, OD, OM, ON, BD, TDS, QDS" in e.value.message


def test_formulary_search_lists_products(call):
    found = call("search_formulary", query="paracetamol")
    assert "Paracetamol" in found["drugs"] and "Paracetamol + Caffeine" in found["drugs"]  # combination products too
    forms = {p["form"] for p in found["products"]["Paracetamol"]}
    assert {"Oral tablet", "Oral suspension", "Solution for infusion", "Suppository"} <= forms
    assert "Glyceryl trinitrate" in call("search_formulary", query="sublingual spray")["drugs"]  # finds drugs by product name


METHOTREXATE = {"drug": "Methotrexate", "product": "Methotrexate 2.5mg tablets", "dose": "15 mg", "route": "PO"}


@pytest.mark.parametrize(
    "frequency",
    ["daily for a week", "OD for one week", "once daily for 1 week", "every day this week", "daily x 1 week", "daily (week 1)", "twice weekly",
     "7 times a week", "three times a week", "once weekly (daily)", "once weekly (Mon-Fri)", "once weekly (Mondays and Thursdays)", "weekly for 7 days",
     "QDS weekly", "weekdays", "every week day", "once a week daily", "1 week", "week", "weekly, BD", "OD", "once", "every 24 hours", "every 7 hours", "every 2 weeks"],
)
def test_methotrexate_other_than_once_weekly_is_blocked_however_it_is_worded(world, call, pid, frequency):
    """Review item A9: 'daily for a week' contains 'week' and used to pass the weekly-only hard stop."""
    patient = pid(BRIGHTWELL)
    raises(call, "weekly_only", "prescribe_medication", patient_id=patient, frequency=frequency, **METHOTREXATE)
    assert not world.one("SELECT 1 FROM medication WHERE patient_id = ? AND drug = 'Methotrexate' AND started_at >= ?", (patient, world.now))


@pytest.mark.parametrize(
    "frequency, stored",
    [("once weekly (Mondays)", "once weekly (Mondays)"), ("weekly", "once weekly"), ("Once a week", "once weekly"), ("once a week on Thursdays", "once weekly (Thursdays)"),
     ("weekly - Fri", "once weekly (Fri)"), ("every 7 days", "once weekly"), ("once weekly on Wednesday", "once weekly (Wednesday)"),
     ("once weekly (Monday morning)", "once weekly (Monday morning)"), ("ONCE WEEKLY (takes on Mondays)", "once weekly (takes on Mondays)"),
     ("once weekly (Sundays at 9am)", "once weekly (Sundays at 9am)"), ("once weekly (in the evening)", "once weekly (in the evening)")],
)
def test_methotrexate_once_weekly_is_accepted_and_normalised(call, pid, frequency, stored):
    assert call("prescribe_medication", patient_id=pid(BRIGHTWELL), frequency=frequency, **METHOTREXATE)["frequency"] == stored


@pytest.mark.parametrize(
    "given, canonical",
    [("twice weekly", "twice weekly"), ("twice a week (Mondays and Thursdays)", "twice weekly (Mondays and Thursdays)"), ("3 times a week", "three times weekly"), ("every 2 weeks (Fridays)", "every 2 weeks (Fridays)")],
)
def test_other_weekly_frequencies_are_normalised(given, canonical):
    assert ops._clean_frequency(given) == canonical


@pytest.mark.parametrize("given", ["daily for a week", "BD for 5 days", "weekly for 7 days", "once weekly (daily)", "once weekly (Mondays and Fridays)", "8 times a week", "weekly (with food)"])
def test_a_course_length_or_contradictory_detail_in_the_frequency_is_refused(given):
    with pytest.raises(ops.OpError) as e:
        ops._clean_frequency(given)
    assert e.value.code == "invalid_arguments"


def test_doses_per_day_never_reads_a_weekly_frequency_as_daily():
    assert [ops.doses_per_day(f) for f in ("twice weekly", "three times weekly", "once weekly (Mondays)")] == [None, None, None]


@pytest.mark.parametrize(
    "rx, text",
    [
        ({"product": "Paracetamol 500mg tablets", "dose": "3 tablets", "frequency": "QDS"}, "3 tablets (1500 mg) exceeds the usual maximum single dose of 1000 mg"),
        ({"product": "Paracetamol 500mg tablets", "dose": "2 tablets", "frequency": "every 4 hours"}, "6000 mg in 24 hours"),
        ({"product": "Paracetamol 1g tablets", "dose": "1-2 tablets", "frequency": "QDS"}, "(2000 mg)"),
        ({"product": "Paracetamol 250mg/5ml oral suspension", "dose": "25 mL", "frequency": "QDS"}, "(1250 mg)"),
        ({"product": "Paracetamol 1g/100ml infusion bags", "dose": "150 mL", "route": "IV", "frequency": "QDS"}, "(1500 mg)"),
        ({"product": "Paracetamol 500mg tablets", "dose": "1-2 g", "frequency": "QDS"}, "exceeds the usual maximum single dose"),
        ({"product": "Paracetamol 500mg tablets", "dose": "2 g", "frequency": "QDS", "kind": "tto", "duration": "7 days"}, "maximum single dose"),
        ({"product": "Paracetamol 500mg tablets", "dose": "2 tablets", "frequency": "every 4 hours", "kind": "tto", "duration": "7 days"}, "6000 mg in 24 hours"),
    ],
)
def test_dose_limits_apply_to_tablets_liquids_ranges_and_discharge_prescriptions(call, pid, rx, text):
    """Review item A9: doses in tablets or mL, dose ranges and TTOs used to bypass the maximum dose checks."""
    error = raises(call, "dose_alert", "prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Paracetamol", **{"route": "PO", **rx})
    assert text in error.message


def test_dose_limits_stay_quiet_within_the_maximum(call, pid):
    patient = pid(BRIGHTWELL)
    for rx in [
        {"product": "Paracetamol 500mg tablets", "dose": "2 tablets", "frequency": "QDS"},
        {"product": "Paracetamol 500mg tablets", "dose": "1-2 tablets", "frequency": "QDS", "kind": "tto", "duration": "7 days"},
        {"product": "Paracetamol 250mg/5ml oral suspension", "dose": "20 mL", "frequency": "QDS"},
        {"product": "Paracetamol 120mg/5ml oral suspension 5ml sachets", "dose": "2 sachets", "frequency": "QDS"},  # 2 x 5 mL x 24 mg/mL
    ]:
        call("prescribe_medication", patient_id=patient, drug="Paracetamol", route="PO", kind=rx.pop("kind", "regular"), **rx)


def test_dose_in_milligrams_from_product_strength():
    tablet = {"unit_dose_form": "tablet", "strengths": '[{"ingredient": "X", "value": 500, "unit": "mg"}]'}
    liquid = {"unit_dose_form": None, "strengths": '[{"ingredient": "X", "value": 50, "unit": "mg", "per_value": 1, "per_unit": "ml"}]'}
    inhaler = {"unit_dose_form": "dose", "strengths": '[{"ingredient": "X", "value": 100, "unit": "microgram", "per_value": 1, "per_unit": "dose"}]'}
    combination = {"unit_dose_form": "tablet", "strengths": '[{"ingredient": "X", "value": 500, "unit": "mg"}, {"ingredient": "Y", "value": 125, "unit": "mg"}]'}
    assert ops.dose_mg("2 tablets", tablet) == 1000 and ops.dose_mg("1 tablet", tablet) == 500 and ops.dose_mg("2 capsules", tablet) is None
    assert ops.dose_mg("10 mL", liquid) == 500 and ops.dose_mg("0.5 L", liquid) == 25000 and ops.dose_mg("2 tablets", liquid) is None
    assert ops.dose_mg("2 puffs", inhaler) == 0.2 and ops.dose_mg("2 tablets", combination) is None
    assert ops.dose_mg("500-1000 mg") == 1000 and ops.dose_mg("1 g", tablet) == 1000  # a mass needs no strength


@pytest.mark.parametrize(
    "rx, text",
    [
        ({"drug": "Methotrexate", "product": "Methotrexate 50mg/2ml solution for injection vials", "dose": "1 vial", "route": "IV", "frequency": "once weekly"}, "1 vial (50 mg)"),
        ({"drug": "Paracetamol", "product": "Paracetamol 120mg/5ml oral suspension 5ml sachets", "dose": "50 sachets", "route": "PO", "frequency": "QDS"}, "(6000 mg)"),
        ({"drug": "Naproxen", "product": "Naproxen 250mg tablets", "dose": "3 tablets", "route": "PO", "frequency": "BD"}, "(750 mg)"),
    ],
)
def test_doses_in_vials_sachets_and_tablets_are_checked_through_the_unit_dose(call, pid, rx, text):
    """Review: '1 vial' of a 2 ml vial and 50 sachets of a 5 ml sachet used to pass unchecked."""
    error = raises(call, "dose_alert", "prescribe_medication", patient_id=pid(BRIGHTWELL), **rx)
    assert text in error.message


@pytest.mark.parametrize(
    "rx, why",
    [
        ({"drug": "Naproxen", "product": "Naproxen sodium 275mg tablets", "dose": "2 tablets", "frequency": "BD"}, "stated as Naproxen sodium"),
        ({"drug": "Naproxen", "product": "Naproxen sodium 275mg tablets", "dose": "550 mg", "frequency": "BD"}, "stated as Naproxen sodium"),
        ({"drug": "Paracetamol", "product": "Paracetamol 500mg tablets", "dose": "2 capsules", "frequency": "QDS"}, "does not match"),
        ({"drug": "Paracetamol", "product": "Paracetamol 500mg tablets", "dose": "2 sachets", "frequency": "QDS"}, "does not match"),
        ({"drug": "Paracetamol", "product": "Paracetamol 500mg tablets", "dose": "10 mL", "frequency": "QDS"}, "does not match"),
        ({"drug": "Paracetamol", "product": "Paracetamol 500mg tablets", "dose": "2 units", "frequency": "QDS"}, "does not match"),
    ],
)
def test_a_dose_that_cannot_be_checked_against_a_maximum_raises_its_own_alert(world, call, pid, rx, why):
    """Review: salts, mismatched forms and unconvertible units used to pass the maximum dose check silently."""
    patient = pid(BRIGHTWELL)
    error = raises(call, "dose_unchecked_alert", "prescribe_medication", patient_id=patient, route="PO", **rx)
    assert why in error.message and "could not be checked" in error.message and "give a reason" in error.message
    call("prescribe_medication", patient_id=patient, route="PO", override_reason="dose checked with pharmacy", **rx)
    assert world.one("SELECT overridden_alert FROM medication ORDER BY id DESC LIMIT 1")["overridden_alert"] == "dose_unchecked_alert"


def test_no_dose_check_alert_for_a_drug_without_a_maximum(call, pid):
    call("prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Enoxaparin", product="Enoxaparin sodium 40mg/0.4ml solution for injection pre-filled syringes", dose="40 mg", route="SC", frequency="OD")


@pytest.mark.parametrize("dose", ["3-1 tablets", "1000-500 mg", "2 - 1 g"])
def test_a_descending_dose_range_is_refused(call, pid, dose):
    """Review: '3-1 tablets' took the lower end, 1 tablet, and passed the maximum dose check."""
    raises(call, "invalid_arguments", "prescribe_medication", patient_id=pid(BRIGHTWELL), drug="Paracetamol", product="Paracetamol 500mg tablets", dose=dose, route="PO", frequency="QDS")


def test_dose_conversion_takes_the_top_of_a_range_whichever_way_it_is_written():
    tablet = {"name": "X 500mg tablets", "unit_dose_form": "tablet", "strengths": '[{"ingredient": "X", "value": 500, "unit": "mg"}]'}
    assert ops.dose_mg("3-1 tablets", tablet) == ops.dose_mg("1-3 tablets", tablet) == 1500
    assert ops.dose_mg("1000-500 mg") == ops.dose_mg("500-1000 mg") == 1000


def test_dose_conversion_through_basis_and_unit_dose():
    vial = {"name": "M 50mg/2ml vials", "unit_dose_form": "vial", "strengths": '[{"ingredient": "M sodium", "basis": "M", "value": 25, "unit": "mg", "per_value": 1, "per_unit": "ml", "unit_dose_size": 2, "unit_dose_size_unit": "ml"}]'}
    salt = {"name": "N sodium 275mg tablets", "unit_dose_form": "tablet", "strengths": '[{"ingredient": "N sodium", "value": 275, "unit": "mg"}]'}
    combination = {"name": "Co-x tablets", "unit_dose_form": "tablet", "strengths": '[{"ingredient": "X", "value": 500, "unit": "mg"}, {"ingredient": "Y", "value": 125, "unit": "mg"}]'}
    assert ops.dose_mg("1 vial", vial, "M") == 50 and ops.dose_mg("2 vials", vial, "m") == 100 and ops.dose_mg("3 mL", vial, "M") == 75
    assert ops.dose_mg("2 tablets", salt, "N") is None and ops.dose_mg("550 mg", salt, "N") is None and ops.dose_mg("550 mg", salt, "N sodium") == 550
    assert ops.dose_mg("1 tablet", combination, "X") is None and ops.dose_mg("500 mg", combination, "X") is None


def test_every_frequency_written_in_the_packaged_bundle_is_accepted():
    """What the bundle's own drug charts and notes write as a frequency must be prescribable as written.

    In a note's drug history ('- Paracetamol 1 g PO QDS PRN', '- Enoxaparin 40 mg SC OD for 14/7') PRN and
    the course length are separate fields of a prescription here (kind and duration), so they are set aside."""
    import json
    import re
    from importlib import resources

    bundle = json.loads(resources.files("eprbench").joinpath("data/bundles/demo.json").read_text())
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "frequency":
                    found.append(v)
                elif k == "body" and isinstance(v, str):
                    for line in v.splitlines():
                        m = re.fullmatch(r"- [A-Za-z][\w +-]* \d+(?:\.\d+)? ?[A-Za-z]+ (?:PO|IV|SC|IM|SL|PR|INH|NEB|TOP) (.+)", line.strip())
                        if m:
                            found.append(re.sub(r"\s+(PRN|for \d+/7|for \d+ days?)$", "", m.group(1)))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(bundle)
    assert "ONCE WEEKLY (takes on Mondays)" in found and "once weekly (Thursdays)" in found and "QDS" in found
    for frequency in found:
        ops._clean_frequency(frequency)
    assert ops._is_once_weekly("ONCE WEEKLY (takes on Mondays)")


@pytest.mark.parametrize(
    "rx, alert",
    [
        ({"drug": "Codeine", "product": "Codeine 30mg tablets", "dose": "30 mg", "frequency": "QDS"}, None),
        ({"drug": "Codeine", "product": "Codeine 30mg tablets", "dose": "2 tablets", "frequency": "QDS"}, None),
        ({"drug": "Codeine", "product": "Codeine 30mg tablets", "dose": "600 mg", "frequency": "QDS"}, "maximum single dose of 60 mg"),
        ({"drug": "Codeine", "product": "Codeine 30mg tablets", "dose": "2 tablets", "frequency": "every 2 hours"}, "720 mg in 24 hours"),
        ({"drug": "Metformin", "product": "Metformin 500mg tablets", "dose": "1 g", "frequency": "BD"}, None),
        ({"drug": "Metformin", "product": "Metformin 500mg tablets", "dose": "4 tablets", "frequency": "BD"}, "maximum single dose of 1000 mg"),
        ({"drug": "Bisoprolol", "product": "Bisoprolol 5mg tablets", "dose": "5 mg", "frequency": "OD"}, None),
        ({"drug": "Bisoprolol", "product": "Bisoprolol 5mg tablets", "dose": "40 mg", "frequency": "OD"}, "maximum single dose of 20 mg"),
    ],
)
def test_doses_of_salts_are_checked_on_the_declared_basis_of_the_maximum(world, call, pid, rx, alert):
    """Review: every codeine, metformin and bisoprolol product is labelled as a salt, so none could be checked."""
    patient = pid(BRIGHTWELL)
    if alert is None:
        call("prescribe_medication", patient_id=patient, route="PO", **rx)
        assert world.one("SELECT overridden_alert FROM medication ORDER BY id DESC LIMIT 1")["overridden_alert"] is None
    else:
        assert alert in raises(call, "dose_alert", "prescribe_medication", patient_id=patient, route="PO", **rx).message


# oral tablets and capsules that may stay uncheckable, and why
UNCHECKABLE_ORAL = {"Naproxen sodium 275mg tablets": "naproxen sodium is not the naproxen the maximum is stated in, and no conversion is declared"}


def test_every_drug_with_a_maximum_can_check_its_oral_tablets_and_capsules(world):
    """For each drug with a maximum, '1 tablet' (or capsule) of each single-ingredient oral product converts to mg."""
    import json

    for drug in world.q("SELECT * FROM drug WHERE max_dose_mg IS NOT NULL OR max_daily_mg IS NOT NULL"):
        oral = [p for p in world.q("SELECT * FROM product WHERE drug = ?", (drug["name"],))
                if "PO" in json.loads(p["routes"]) and p["unit_dose_form"] in ("tablet", "capsule") and len(json.loads(p["strengths"])) == 1]
        assert oral, drug["name"]
        unchecked = [p["name"] for p in oral if ops.dose_mg(f"1 {p['unit_dose_form']}", p, drug["max_dose_basis"] or drug["name"]) is None]
        assert set(unchecked) <= set(UNCHECKABLE_ORAL), (drug["name"], unchecked)


@pytest.mark.parametrize(
    "when",
    ["Monday AM", "Monday am", "Mondays PM", "Monday lunchtime", "Monday midday", "Monday noon", "Monday teatime", "Monday at 14:30",
     "Monday 08:00", "Monday 9am", "Monday 9:30 pm", "Monday at 21.00", "Monday 12 noon", "takes on Mondays at 7 a.m.", "Monday 00:00", "Monday 23:59"],
)
def test_ordinary_times_of_day_are_accepted_on_a_weekly_frequency(call, pid, when):
    placed = call("prescribe_medication", patient_id=pid(BRIGHTWELL), frequency=f"once weekly ({when})", **METHOTREXATE)
    assert placed["frequency"] == f"once weekly ({when})"


@pytest.mark.parametrize("when", ["Monday 99:99", "Monday 25:00", "Monday 24:00", "Monday 12:60", "Monday 13pm", "Monday 0am", "Monday 9:75am", "Monday 930"])
def test_impossible_times_are_refused(call, pid, when):
    raises(call, "weekly_only", "prescribe_medication", patient_id=pid(BRIGHTWELL), frequency=f"once weekly ({when})", **METHOTREXATE)
    with pytest.raises(ops.OpError):
        ops._clean_frequency(f"once weekly ({when})")
