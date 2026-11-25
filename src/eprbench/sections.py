"""One map of the record's capabilities, the same on every surface: which section each operation belongs to, under the
name the web UI gives that section and a one-line description of it.

The JSON API lists its operations grouped under these sections (GET /api/ops); the web UI's patient menu, shown at the
top of every patient page, has an entry for each patient section with the same name, and shows the same description
on that section's page. The descriptions say what a section holds and lets you do, never when to use it: nothing here
may point at what any task needs (tests/test_navigation.py holds both surfaces to this map).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    name: str  # as the UI labels it
    description: str  # one line, shown on the section's page and in the API listing
    operations: tuple[str, ...]
    tab: str | None = None  # the patient menu's tab (`?tab=`), for a section of a patient's record
    href: str | None = None  # otherwise where the UI has it (a header link or control)


SECTIONS = (
    Section("Patients", "Find a patient by name, hospital number or NHS number, or from a ward or department's list.",
            ("search_patients", "list_locations", "list_location_patients"), href="/"),
    Section("Clock", "The current date and time, and letting time pass.", ("get_current_time", "wait"), href="#wait-form"),
    Section("Summary", "Current admission, problem list, allergies, admissions and attendances, and demographics.",
            ("get_patient", "list_encounters", "add_problem", "record_allergy"), tab="summary"),
    Section("Notes", "Clinical notes: read them, write one, or edit and finalise a draft of your own.",
            ("list_notes", "get_note", "write_note", "update_note"), tab="notes"),
    Section("Observations", "Charted observations with NEWS2: chart a set, or mark one as entered in error.",
            ("list_observations", "record_observations", "mark_observations_in_error"), tab="observations"),
    Section("Fluid balance", "Fluid intake and output, with totals over a period: read the chart or add to it.",
            ("get_fluid_balance", "record_fluid"), tab="fluids"),
    Section("Medications", "The drug chart: prescribe from the formulary, record doses given, omitted or refused, and stop a medicine.",
            ("list_medications", "search_formulary", "prescribe_medication", "record_administration", "stop_medication"), tab="medications"),
    Section("Monitor", "The bedside monitor: current values and their recent trend.", ("get_live_vitals",), tab="monitor"),
    Section("Referrals", "Referrals made and the teams' replies; refer to a team, or through a named pathway with its form.",
            ("list_referrals", "list_referral_pathways", "make_referral"), tab="referrals"),
    Section("Assessments", "Assessments recorded, and the assessment forms available: a structured form with its score, or an assessment in words.",
            ("list_assessments", "list_assessment_forms", "record_assessment"), tab="assessments"),
    Section("Jobs", "This patient's jobs on the ward jobs list: add one, or mark one done or cancelled.",
            ("list_jobs", "add_job", "complete_job", "cancel_job"), tab="jobs"),
    Section("Discharge", "Discharge from the current admission.", ("discharge_patient",), tab="discharge"),
    Section("Requests and results (ICE)",
            "The requesting and results system: request investigations and blood products, follow and cancel requests, read and acknowledge "
            "results, and cumulative results.",
            ("list_orderables", "browse_imaging_catalogue", "get_request_form", "place_order", "list_orders", "cancel_order", "list_results",
             "get_result", "get_analyte_history", "acknowledge_result", "request_blood_product", "list_blood_requests"), href="/ice"),
)  # fmt: skip
REPLY = Section("Handover / reply", "Your reply to your colleague, with the task's answer form: checked, then submitted once; submitting ends the task.",
                ("submit",), href="/reply")  # fmt: skip

PATIENT_MENU = [s for s in SECTIONS if s.tab or s.href == "/ice"]  # the patient menu, in order: its tabs, then ICE
TABS = [s.tab for s in SECTIONS if s.tab]
BY_TAB = {s.tab: s for s in SECTIONS if s.tab}
SECTION_OF = {op: s.name for s in (*SECTIONS, REPLY) for op in s.operations}


def grouped(schemas: list[dict]) -> dict:
    """Operation schemas (name, description, input_schema) grouped under the sections, in order: the API's listing."""
    by_name = {s["name"]: s for s in schemas}
    out = []
    for s in (*SECTIONS, REPLY):
        listed = [by_name[n] for n in s.operations if n in by_name]
        if listed:
            out.append({"section": s.name, "description": s.description, "operations": listed})
    missing = set(by_name) - {n for s in out for n in (o["name"] for o in s["operations"])}
    if missing:
        raise ValueError(f"operations in no section: {sorted(missing)}")
    return {"sections": out}


def operations(listing: dict) -> list[dict]:
    """Every operation of a grouped listing, in order."""
    return [o for s in listing["sections"] for o in s["operations"]]
