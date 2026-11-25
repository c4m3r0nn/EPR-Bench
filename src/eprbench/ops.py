"""Every way of reading or changing the record.

The REST API, the tool-call surface and the web UI are all thin adapters over
this registry, so the three interfaces expose exactly the same information and
capabilities. Operations only ever reveal what a clinician at the simulated
time could see: results not yet reported and the hidden generator tables stay
out of reach.
"""

import json
import math
import re
from collections import defaultdict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from eprbench import scenario, vitals
from eprbench.identifiers import format_nhs_number, normalise_nhs_number
from eprbench.news2 import news2
from eprbench.world import World, iso, parse_iso


class OpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Ctx:
    """Who is acting, through which surface, and as part of which agent action.

    `action_id` groups the operations one agent action causes (one tool call, one API call, one browser request);
    when neither it nor ACTION is set, each call is an action of its own. With `require_patient`, an operation on an
    object by its id must also name the patient whose record the caller is working in: every agent surface sets it
    (AGENT_CTX); only privileged in-process callers (tests, set-up scripts) leave it off. `store_reads` keeps what
    read operations return in the audit log, for evidence; writes and errors are always kept. `shows`: what a read
    returns reaches the caller whole, so its read events (SHOWS) are recorded with it; a UI page sets it off and records
    what it renders instead (record_reads)."""

    actor: str = "agent"
    interface: str = "tool"
    action_id: int | None = None
    require_patient: bool = False
    store_reads: bool = True
    shows: bool = True


def by_agent(ctx: Ctx) -> dict:
    """Provenance columns for a row an operation writes: the agent, and the action that wrote it."""
    return {"origin": "agent", "origin_action_id": ctx.action_id}


def agent_ctx(actor: str, interface: str, store_reads: bool = True) -> Ctx:
    """The context of an agent surface (tool, api, ui): one patient policy for all of them. A UI page records what it
    renders, not what its operations return (see record_reads)."""
    return Ctx(actor=actor, interface=interface, require_patient=True, store_reads=store_reads, shows=interface != "ui")


# The agent action a surface is handling, for surfaces that run several operations per action (a UI page load).
ACTION: ContextVar[int | None] = ContextVar("eprbench_action", default=None)


def next_action_id(world: World) -> int:
    """A new agent action number: one more than any recorded in the audit log or the read events (read once per world,
    then kept up to date by call), so it is the same after a snapshot is restored."""
    if world.last_action is None:
        world.last_action = world.one(
            "SELECT max(coalesce((SELECT max(action_id) FROM audit), 0), coalesce((SELECT max(action_id) FROM read_event), 0)) AS n")["n"]
    return world.last_action + 1


def claim_action_id(world: World) -> int:
    """A new agent action number, taken at once, for an action that may write no audit row (a screenshot delivered)."""
    with world.lock:
        action = next_action_id(world)
        world.last_action = action
        return action


_RESULT_JSON = json.JSONEncoder(default=str, check_circular=False, separators=(",", ":"))


@dataclass(frozen=True)
class Op:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[World, Any, Ctx], dict]
    writes: bool
    patient_specific: bool = False  # the arguments say whose record it concerns
    object_ref: str | None = None  # the argument naming an object on a patient's record by its id, if any


REGISTRY: dict[str, Op] = {}

# Arguments that identify a patient through an object on their record: the table, the column saying
# when the object becomes visible, and what to call it in a message.
OWNED_BY = {
    "note_id": ("note", "written_at", "note"),
    "order_id": ('"order"', "ordered_at", "request"),
    "result_id": ("result", "reported_at", "result"),
    "medication_id": ("medication", "started_at", "medication"),
    "job_id": ("job", "created_at", "job"),
    "observation_set_id": ("observation", "taken_at", "observation set"),
}
# Writes that change no patient's record. Every other write must identify its patient, so the audit
# log (and every wrong-patient check built on it) knows whose record was changed.
UNATTRIBUTED_WRITES = {"wait": "moves the simulated clock"}
_ID = TypeAdapter(int)


def op(description: str, writes: bool = False):
    def register(fn):
        args_model = get_type_hints(fn)["args"]
        fields = set(args_model.model_fields)
        patient_specific = "patient_id" in fields or bool(fields & set(OWNED_BY))
        if writes and not patient_specific and fn.__name__ not in UNATTRIBUTED_WRITES:
            raise TypeError(f"{fn.__name__} writes but its arguments do not identify a patient")
        ref = next((f for f in OWNED_BY if f in fields), None)
        REGISTRY[fn.__name__] = Op(fn.__name__, description, args_model, fn, writes, patient_specific, ref)
        return fn

    return register


def _as_id(value) -> int | None:
    """An identifier as validation would read it ("2" is 2), or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return _ID.validate_python(value)
    except ValidationError:
        return None


def _owner(world: World, values: dict, check: bool = True) -> int | None:
    """Whose record the arguments concern: the owner of an object they refer to, else the patient they name.

    With `check`, naming one patient and referring to a visible object on another's record is refused."""
    named = _as_id(values.get("patient_id"))
    for field, (table, visible_from, label) in OWNED_BY.items():
        ref = _as_id(values.get(field))
        row = world.one(f"SELECT patient_id, {visible_from} <= ? AS visible FROM {table} WHERE id = ?", (world.now, ref)) if ref is not None else None
        if row is None:
            continue
        if check and named is not None and row["visible"] and row["patient_id"] != named:
            raise OpError("wrong_patient", f"{label} {ref} is not on this patient's record")
        return row["patient_id"]
    return named


def call(world: World, name: str, args: dict | None = None, ctx: Ctx = Ctx()) -> dict:
    """Validate, run and audit one operation. Writes are atomic.

    The audit row names the patient the operation concerned, worked out from the arguments and the
    objects they refer to, never from what the operation returns."""
    args = args or {}
    with world.lock:
        error, out, patient_id = None, None, None
        action_id = ctx.action_id or ACTION.get() or next_action_id(world)
        try:
            if name not in REGISTRY:
                raise OpError("unknown_operation", f"no such operation: {name}")
            spec = REGISTRY[name]
            try:
                parsed = spec.args_model.model_validate(args)
            except ValidationError as e:
                # only through identifiers this operation declares: anything else in a rejected call means nothing
                if spec.patient_specific and isinstance(args, dict):
                    patient_id = _owner(world, {k: v for k, v in args.items() if k in spec.args_model.model_fields}, check=False)
                detail = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
                raise OpError("invalid_arguments", detail) from None
            values = parsed.model_dump()
            patient_id = _owner(world, values, check=False) if spec.patient_specific else None
            if ctx.require_patient and spec.object_ref and values.get("patient_id") is None:
                label = OWNED_BY[spec.object_ref][2]
                raise OpError("patient_required", f"patient_id is required: give the patient whose record you are working in, so that a {label} on another patient's record is refused")
            world.conn.execute("BEGIN")
            try:
                scenario.fire_due(world)
                if values.get("patient_id") is not None:
                    _owner(world, values)  # refuses another patient's object
                # writes stamp rows with the action that wrote them (by_agent); reads need not pay for the copy
                out = spec.fn(world, parsed, replace(ctx, action_id=action_id) if spec.writes and ctx.action_id != action_id else ctx)
                if spec.writes and spec.patient_specific and patient_id is None:
                    raise OpError("unattributed_write", f"{name} changed a record without identifying the patient")
                if spec.writes:
                    scenario.fire_matching(world, name, {**values, **out, "patient_id": patient_id, "action_id": action_id})
                    scenario.fire_due(world)  # what fell due while it ran, e.g. during a wait, as of its own time
                world.conn.execute("COMMIT")
                return out
            except BaseException:
                world.conn.execute("ROLLBACK")
                # the clock lives in the database; resync the cached value after a rollback
                world._now = int(world.one("SELECT value FROM meta WHERE key = 'now'")["value"])
                raise
        except OpError as e:
            error, out = e.code, {"error": e.code, "message": e.message}
            raise
        except BaseException:
            error, out = "internal_error", {"error": "internal_error"}
            raise
        finally:
            audit_id = world.insert(
                "audit",
                at=world.now,
                actor=ctx.actor,
                interface=ctx.interface,
                op=name,
                args=json.dumps(args, sort_keys=True, default=str),
                ok=int(error is None),
                error=error,
                writes=int(name in REGISTRY and REGISTRY[name].writes),
                patient_id=patient_id,
                action_id=action_id,
                result=_RESULT_JSON.encode(out) if error or ctx.store_reads or (name in REGISTRY and REGISTRY[name].writes) else None,
            )
            if error is None and ctx.shows and name in SHOWS:
                record_reads(world, ctx.actor, ctx.interface, action_id, patient_id, name, out, audit_id=audit_id)
            if world.last_action is not None and action_id > world.last_action:
                world.last_action = action_id


# --- what an agent was shown: read events ----------------------------------------------------------------------
#
# "The agent read X" means the same on every surface: an entry of the record counts as read when its CONTENT reached
# the agent: returned by a tool or API call, or, in the browser, inside the viewport of a screenshot the harness sent
# the model. Bodies, not titles in a list; results' values, not their existence. Each read operation says which entries
# its output carries (SHOWS): an entry by id (kind, object_id), or a part of the record shown whole (kind, None): the
# allergies, the problem list, the demographics, the list of notes or results (titles), the list of analytes, the fluid
# balance chart or the monitor. Grading sees them as agent_read (assessment.py). A UI page tags each part it renders
# with the entries it carries, by the same derivation (server/ui.py); rendering records only a `rendered` event, and a
# browser harness reports the tags visible in each screenshot it sends (agents/claude_browser.screen_reads).

READ_KINDS = {
    "note": "a note's body", "result": "a result in full (report text and every value)", "result_value": "one reported value (result_item id)",
    "observation_set": "one charted set of observations (its observation_set_id)", "assessment": "an assessment with its answers and score",
    "medication": "a drug chart row with its recent administrations", "referral": "a referral and any reply", "order": "a request and its status",
    "encounter": "an admission or attendance", "job": "a job on the jobs list", "blood_request": "a blood product request",
    "allergies": "the allergy list", "problems": "the problem list", "demographics": "address and GP", "note_list": "the list of notes (titles)",
    "result_list": "the list of results (titles, abnormal counts)", "analyte_list": "the analytes on record (cumulative view)",
    "fluid_balance": "the fluid balance chart", "monitor": "the bedside monitor",
}  # fmt: skip


def _values_of(world: World, out: dict) -> list:
    """The result_item ids of a result read in full."""
    return [("result_value", r["id"], None) for r in world.q("SELECT id FROM result_item WHERE result_id = ? ORDER BY id", (out["result_id"],))]


def _history(world: World, out: dict) -> list:
    if "analytes" in out:
        return [("analyte_list", None, None)]
    ids = sorted({s["result_id"] for s in out["series"]})
    marks = ", ".join("?" * len(ids))
    rows = world.q(f"SELECT id FROM result_item WHERE result_id IN ({marks}) AND analyte = ? COLLATE NOCASE ORDER BY id", (*ids, out["analyte"]))
    return [("result_value", r["id"], None) for r in rows]


def _each(key: str, kind: str, field: str):
    return lambda world, out: [(kind, x[field], x.get("patient_id")) for x in out[key]]


# per read operation: (kind, object_id, patient_id or None for the patient the call concerned) for each entry it returns
SHOWS: dict[str, Callable[[World, dict], list[tuple[str, Any, int | None]]]] = {
    "get_patient": lambda world, out: [("allergies", None, None), ("problems", None, None), ("demographics", None, None)]
    + ([("encounter", out["current_encounter"]["encounter_id"], None)] if out.get("current_encounter") else []),
    "list_encounters": _each("encounters", "encounter", "encounter_id"),
    "list_referrals": _each("referrals", "referral", "referral_id"),
    "list_assessments": _each("assessments", "assessment", "assessment_id"),
    "list_notes": lambda world, out: [("note_list", None, None)],
    "get_note": lambda world, out: [("note", out["note_id"], out["patient_id"])],
    "list_observations": _each("observation_sets", "observation_set", "observation_set_id"),
    "get_fluid_balance": lambda world, out: [("fluid_balance", None, None)],
    "get_live_vitals": lambda world, out: [("monitor", None, None)],
    "list_orders": _each("orders", "order", "order_id"),
    "list_results": lambda world, out: [("result_list", None, None)],
    "get_result": lambda world, out: [("result", out["result_id"], out["patient_id"]), *_values_of(world, out)],
    "get_analyte_history": _history,
    "list_medications": _each("medications", "medication", "medication_id"),
    "list_jobs": _each("jobs", "job", "job_id"),
    "list_blood_requests": _each("blood_requests", "blood_request", "blood_request_id"),
}


# How an entry reached the agent: returned by a tool or API call; seen in a screenshot a browser harness delivered to
# the model; shown on a page a person had open. A page rendered is not a page read (`rendered`: kept, never credited).
VIA = ("returned", "screenshot", "person", "rendered")


def shown_entries(world: World, name: str, out: dict, patient_id: int | None, kinds: tuple[str, ...] = ()) -> list[tuple[str, Any, int | None]]:
    """The entries operation `name`'s output `out` carries (only those of `kinds`, if given), each with its patient."""
    return [(kind, oid, patient_id if pid is None else pid) for kind, oid, pid in SHOWS[name](world, out) if not kinds or kind in kinds]


def record_entries(world: World, actor: str, interface: str, action_id: int | None, entries: list, via: str, audit_id: int | None = None) -> None:
    rows = [(world.now, actor, interface, action_id, audit_id, pid, kind, oid, via) for kind, oid, pid in entries]
    with world.lock:
        world.conn.executemany(
            "INSERT INTO read_event (at, actor, interface, action_id, audit_id, patient_id, kind, object_id, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )


def record_reads(world: World, actor: str, interface: str, action_id: int | None, patient_id: int | None, name: str, out: dict,
                 kinds: tuple[str, ...] = (), audit_id: int | None = None, via: str = "returned") -> None:  # fmt: skip
    """Record the entries operation `name`'s output `out` showed the agent (only those of `kinds`, if given)."""
    record_entries(world, actor, interface, action_id, shown_entries(world, name, out, patient_id, kinds), via, audit_id)


def tool_schemas(require_patient: bool = True) -> list[dict]:
    """Tool definitions in the name/description/input_schema shape used by LLM tool-calling APIs.

    patient_id is required by every operation on an object by id, as every agent surface enforces it."""
    schemas = []
    for o in REGISTRY.values():
        schema = o.args_model.model_json_schema()
        if require_patient and o.object_ref and "patient_id" not in schema.get("required", []):
            label = OWNED_BY[o.object_ref][2]
            schema["properties"]["patient_id"] = {"type": "integer", "title": "Patient Id",
                                                  "description": f"The patient whose record you are working in. Required: the operation is refused when the {label} is on another patient's record."}  # fmt: skip
            schema["required"] = [*schema.get("required", []), "patient_id"]
        schemas.append({"name": o.name, "description": o.description, "input_schema": schema})
    return schemas


# --- shared helpers ---------------------------------------------------------


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


class PatientArgs(Args):
    patient_id: int


def _for_patient(what: str):
    return Field(None, description=f"The patient whose record you are working in. If given, the operation is refused when the {what} is on another patient's record.")


def _patient(world: World, patient_id: int) -> dict:
    row = world.one("SELECT * FROM patient WHERE id = ?", (patient_id,))
    if row is None:
        raise OpError("not_found", f"no patient with id {patient_id}")
    return row


def _current_encounter(world: World, patient_id: int) -> dict | None:
    return world.one(
        """SELECT e.*, l.code AS location_code, l.name AS location_name, l.kind AS location_kind,
                  l.live_monitoring
           FROM encounter e LEFT JOIN location l ON l.id = e.location_id
           WHERE e.patient_id = ? AND e.admitted_at <= ? AND (e.discharged_at IS NULL OR e.discharged_at > ?)
           ORDER BY e.admitted_at DESC LIMIT 1""",
        (patient_id, world.now, world.now),
    )


def _encounter_view(e: dict) -> dict:
    return {
        "encounter_id": e["id"],
        "kind": e["kind"],
        "location": e.get("location_name"),
        "location_code": e.get("location_code"),
        "bed": e["bed"],
        "specialty": e["specialty"],
        "consultant": e["consultant"],
        "presenting_complaint": e["presenting_complaint"],
        "admitted_at": iso(e["admitted_at"]),
        "discharged_at": iso(e["discharged_at"]),
    }


def _patient_header(world: World, p: dict) -> dict:
    enc = _current_encounter(world, p["id"])
    return {
        "patient_id": p["id"],
        "mrn": p["mrn"],
        "nhs_number": format_nhs_number(p["nhs_number"]),
        "name": f"{p['family_name'].upper()}, {p['given_name']}",
        "dob": p["dob"],
        "sex": p["sex"],
        "location": enc["location_name"] if enc else None,
        "bed": enc["bed"] if enc else None,
    }


_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?Z?")


def parse_datetime(text: str) -> int | None:
    """A date and time typed as YYYY-MM-DDTHH:MM (a space for the T, seconds and a Z optional; UTC), or None."""
    m = _DATETIME.fullmatch(text.strip())
    if not m:
        return None
    try:
        return parse_iso(f"{m[1]}T{m[2]}{m[3] or ':00'}Z")
    except ValueError:
        return None


def form_fields(stored: str) -> list[dict]:
    """A pathway's or assessment form's fields (loader.FormField, as stored), with Yes and No as the options of a
    yes/no question; a tick box takes yes or no too."""
    out = []
    for f in json.loads(stored):
        if f["kind"] in ("yes_no", "tick"):
            f = {**f, "options": [{"value": "yes", "label": "Yes", "points": f["points"]}, {"value": "no", "label": "No", "points": 0}]}
        out.append(f)
    return out


def fields_view(fields: list[dict], points: bool) -> list[dict]:
    """Fields as an operation shows them: points only where they make a score; a tick box's options are implied."""
    out = []
    for f in fields:
        v = {"name": f["name"], "label": f["label"], "kind": f["kind"], "required": f["required"] and f["kind"] != "tick"}
        if f["kind"] == "tick":
            v |= {"points": f["points"]} if points else {}
        elif f["options"]:
            v["options"] = [{"value": o["value"], "label": o["label"], **({"points": o["points"]} if points else {})} for o in f["options"]]
        out.append(v)
    return out


def check_form(fields: list[dict], values: dict[str, str], where: str, incomplete: str) -> dict[str, str]:
    """Validate a completed pathway or assessment form: every value typed as its field says, nothing the form lacks,
    every required field given. Returns the values in the form's order, an unticked box as 'no' and a date and time
    as ISO 8601."""
    unknown = sorted(set(values) - {f["name"] for f in fields})
    if unknown:
        raise OpError("invalid_arguments", f"{where}: fields not on this form: {', '.join(unknown)}")
    clean, missing = {}, []
    for f in fields:
        value = str(values.get(f["name"], "")).strip()
        if not value:
            if f["kind"] == "tick":
                clean[f["name"]] = "no"
            elif f["required"]:
                missing.append(f["label"])
            continue
        if f["options"]:
            option = next((o["value"] for o in f["options"] if o["value"].lower() == value.lower()), None)
            if option is None:
                raise OpError("invalid_arguments", f"{where}.{f['name']}: must be one of {', '.join(o['value'] for o in f['options'])}")
            value = option
        elif f["kind"] == "number":
            try:
                float(value)
            except ValueError:
                raise OpError("invalid_arguments", f"{where}.{f['name']}: must be a number") from None
        elif f["kind"] == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise OpError("invalid_arguments", f"{where}.{f['name']}: must be a date as YYYY-MM-DD")
        elif f["kind"] == "datetime":
            t = parse_datetime(value)
            if t is None:
                raise OpError("invalid_arguments", f"{where}.{f['name']}: must be a date and time as YYYY-MM-DDTHH:MM")
            value = iso(t)
        clean[f["name"]] = value
    if missing:
        raise OpError(incomplete, f"the form is incomplete: {'; '.join(missing)}")
    return clean


def _points(field: dict, value: str | None) -> float:
    return next((o["points"] for o in field["options"] if o["value"] == value), 0) if field["options"] else 0


def _num(x: float | None) -> float | int | None:
    """A whole number as an int, so a score of 2 reads 2."""
    return int(x) if x is not None and x == int(x) else x


# --- clock ------------------------------------------------------------------


@op("Get the current date and time.")
def get_current_time(world: World, args: NoArgs, ctx: Ctx) -> dict:
    return {"now": iso(world.now)}


MAX_WAIT_SECONDS = 3600  # one call moves the clock at most an hour, on every route (the UI's largest Wait button is 60 minutes)


class WaitArgs(Args):
    seconds: int = Field(gt=0, le=MAX_WAIT_SECONDS, description="How long to wait, in seconds (at most 60 minutes a call; wait again for longer).")


@op("Wait for time to pass, for example until a pending result is reported.", writes=True)
def wait(world: World, args: WaitArgs, ctx: Ctx) -> dict:
    world.advance(args.seconds)
    return {"now": iso(world.now)}


# --- patients ---------------------------------------------------------------


class SearchArgs(Args):
    query: str = Field(min_length=2, description="Name, hospital number (MRN) or NHS number.")


@op("Search for patients by name, hospital number (MRN) or NHS number.")
def search_patients(world: World, args: SearchArgs, ctx: Ctx) -> dict:
    q = args.query.strip()
    like = f"%{q}%"
    rows = world.q(
        """SELECT * FROM patient
           WHERE mrn = ? OR nhs_number = ? OR family_name LIKE ? OR given_name LIKE ?
              OR given_name || ' ' || family_name LIKE ?
           ORDER BY family_name, given_name LIMIT 50""",
        (q, normalise_nhs_number(q), like, like, like),
    )
    return {"patients": [_patient_header(world, p) for p in rows]}


@op("List locations (wards and departments) in the hospital.")
def list_locations(world: World, args: NoArgs, ctx: Ctx) -> dict:
    rows = world.q("SELECT code, name, kind, live_monitoring FROM location ORDER BY name")
    return {"locations": [{**r, "live_monitoring": bool(r["live_monitoring"])} for r in rows]}


class LocationArgs(Args):
    location_code: str


@op("List the patients currently in a ward or department.")
def list_location_patients(world: World, args: LocationArgs, ctx: Ctx) -> dict:
    if not world.one("SELECT 1 FROM location WHERE code = ?", (args.location_code,)):
        raise OpError("not_found", f"no location with code {args.location_code}")
    rows = world.q(
        """SELECT p.* FROM patient p JOIN encounter e ON e.patient_id = p.id
           JOIN location l ON l.id = e.location_id
           WHERE l.code = ? AND e.admitted_at <= ? AND (e.discharged_at IS NULL OR e.discharged_at > ?)
           ORDER BY e.bed""",
        (args.location_code, world.now, world.now),
    )
    return {"patients": [_patient_header(world, p) for p in rows]}


@op("Get a patient's summary: demographics, allergies, problem list and current admission.")
def get_patient(world: World, args: PatientArgs, ctx: Ctx) -> dict:
    p = _patient(world, args.patient_id)
    enc = _current_encounter(world, p["id"])
    return {
        **_patient_header(world, p),
        "address": p["address"],
        "gp_practice": p["gp_practice"],
        "allergies": world.q("SELECT substance, reaction, severity FROM allergy WHERE patient_id = ? ORDER BY id", (p["id"],)),
        "problems": world.q(
            """SELECT display, code_system, code, onset_date, resolved_date FROM problem
               WHERE patient_id = ? ORDER BY onset_date""",
            (p["id"],),
        ),
        "current_encounter": _encounter_view(enc) if enc else None,
    }


@op("List a patient's admissions, emergency attendances and outpatient visits, newest first.")
def list_encounters(world: World, args: PatientArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT e.*, l.code AS location_code, l.name AS location_name FROM encounter e
           LEFT JOIN location l ON l.id = e.location_id
           WHERE e.patient_id = ? AND e.admitted_at <= ? ORDER BY e.admitted_at DESC""",
        (args.patient_id, world.now),
    )
    return {"encounters": [_encounter_view(e) for e in rows]}


class AllergyArgs(PatientArgs):
    substance: str = Field(min_length=1, description="Drug, drug class, food or other substance.")
    reaction: str = Field("", description="What happened, e.g. rash, anaphylaxis.")
    severity: Literal["mild", "moderate", "severe"] | None = None


@op("Record an allergy or adverse drug reaction on the patient's record.", writes=True)
def record_allergy(world: World, args: AllergyArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    allergy_id = world.insert(
        "allergy",
        patient_id=args.patient_id,
        substance=args.substance,
        reaction=args.reaction or None,
        severity=args.severity,
        recorded_by=ctx.actor,
        recorded_at=world.now,
    )
    return {"allergy_id": allergy_id}


class ProblemArgs(PatientArgs):
    display: str = Field(min_length=1, description="The diagnosis or problem, e.g. 'Pulmonary embolism'.")
    onset_date: str | None = Field(None, description="YYYY-MM-DD. Defaults to today.")


@op("Add a diagnosis or problem to the patient's problem list.", writes=True)
def add_problem(world: World, args: ProblemArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    enc = _current_encounter(world, args.patient_id)
    problem_id = world.insert(
        "problem",
        patient_id=args.patient_id,
        encounter_id=enc["id"] if enc else None,
        display=args.display,
        onset_date=args.onset_date or iso(world.now)[:10],
    )
    return {"problem_id": problem_id}


class ReferralArgs(PatientArgs):
    specialty: str = Field("", description="For a general referral: the team being asked, e.g. Cardiology, Medical registrar, Physiotherapy. Leave empty when choosing a pathway.")
    urgency: Literal["routine", "urgent"] = "routine"
    reason: str = Field(min_length=1, description="The question being asked and the relevant background.")
    pathway: str = Field("", description="A named pathway from list_referral_pathways (for example an emergency activation), instead of a general referral. It sets the destination and may set the urgency.")
    pathway_details: dict[str, str] = Field({}, description="The chosen pathway's form, as field name to value (list_referral_pathways gives the fields and their options).")


def resolve_service(world: World, destination: str) -> str | None:
    """The hospital service a referral's destination names: its name or an alias as whole words, in declared order.

    "Cardiology (PPCI)" reaches Cardiology; "Cardiothoracic surgery" does not."""
    for s in world.q("SELECT name, aliases FROM service ORDER BY rowid"):
        for alias in [s["name"], *json.loads(s["aliases"])]:
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", destination, re.IGNORECASE):
                return s["name"]
    return None


@op(
    "Refer the patient: either a general referral to a specialty or team, or a named pathway (list_referral_pathways) "
    "with its form completed.",
    writes=True,
)
def make_referral(world: World, args: ReferralArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    enc = _current_encounter(world, args.patient_id)
    specialty, urgency, details = args.specialty.strip(), args.urgency, None
    if args.pathway:
        pathway = world.one("SELECT * FROM pathway WHERE name = ?", (args.pathway,))
        if pathway is None:
            raise OpError("not_found", f"no referral pathway {args.pathway!r}; list_referral_pathways gives them")
        if specialty:
            raise OpError("invalid_arguments", "give either a pathway or a team to refer to (specialty), not both")
        details = check_form(form_fields(pathway["fields"]), args.pathway_details, "pathway_details", "incomplete_request")
        specialty, urgency, service = pathway["label"], pathway["urgency"] or args.urgency, pathway["service"] or resolve_service(world, pathway["label"])
    elif not specialty:
        raise OpError("invalid_arguments", "specialty: give the team being asked, or choose a pathway")
    elif args.pathway_details:
        raise OpError("invalid_arguments", "pathway_details: only a pathway has a form; choose the pathway too")
    else:
        service = resolve_service(world, specialty)
    referral_id = world.insert(
        "referral",
        **by_agent(ctx),
        patient_id=args.patient_id,
        encounter_id=enc["id"] if enc else None,
        specialty=specialty,
        urgency=urgency,
        reason=args.reason,
        referred_by=ctx.actor,
        referred_at=world.now,
        pathway=args.pathway or None,
        pathway_details=json.dumps(details) if details is not None else None,
        service=service,
    )
    return {"referral_id": referral_id, "specialty": specialty, "urgency": urgency, "pathway": args.pathway or None}


@op("List the named referral pathways (such as emergency activations) that can be chosen instead of a general referral, each with its form.")
def list_referral_pathways(world: World, args: NoArgs, ctx: Ctx) -> dict:
    rows = world.q("SELECT name, label, urgency, description, fields FROM pathway ORDER BY rowid")
    return {"pathways": [{**r, "fields": fields_view(form_fields(r["fields"]), points=False)} for r in rows]}


@op("List the referrals made for a patient, newest first, with the response from the team once they have replied.")
def list_referrals(world: World, args: PatientArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT * FROM referral WHERE patient_id = ? AND referred_at <= ? ORDER BY referred_at DESC, id DESC""",
        (args.patient_id, world.now),
    )
    out = []
    for r in rows:
        replied = r["responded_at"] is not None and r["responded_at"] <= world.now
        out.append({
            "referral_id": r["id"], "specialty": r["specialty"], "urgency": r["urgency"], "reason": r["reason"],
            "pathway": r["pathway"], "pathway_details": json.loads(r["pathway_details"]) if r["pathway_details"] else None,
            "referred_by": r["referred_by"], "referred_at": iso(r["referred_at"]), "status": r["status"] if replied else "sent",
            "response": {"at": iso(r["responded_at"]), "by": r["responded_by"], "role": r["responder_role"], "text": r["response"]} if replied else None,
        })  # fmt: skip
    return {"referrals": out}


ASSESSMENT_KINDS = ("vte", "bleeding_risk", "capacity", "falls", "frailty")  # recorded as an outcome in words


class AssessmentArgs(PatientArgs):
    kind: str = Field(min_length=1, description="vte, bleeding_risk, capacity, falls or frailty (give the outcome), or a structured form from list_assessment_forms (give its answers).")
    outcome: str = Field("", description="The conclusion, e.g. 'High risk of VTE, low bleeding risk'. For a form, its category unless you write one.")
    details: str = ""
    answers: dict[str, str] = Field({}, description="A form's fields, as field name to value: 'yes' for a ticked box (a box left out is unticked), an option's value, a number or text.")


@op(
    "Record a clinical assessment: the VTE risk assessment required on admission and others in words, or a structured "
    "form (list_assessment_forms) whose score and category are computed from its answers.",
    writes=True,
)
def record_assessment(world: World, args: AssessmentArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    enc = _current_encounter(world, args.patient_id)
    form = world.one("SELECT * FROM assessment_form WHERE name = ?", (args.kind,))
    score = category = None
    if form is None:
        if args.kind not in ASSESSMENT_KINDS:
            forms = [r["name"] for r in world.q("SELECT name FROM assessment_form ORDER BY rowid")]
            raise OpError("invalid_arguments", f"kind: {args.kind!r} is not an assessment; choose one of {', '.join([*ASSESSMENT_KINDS, *forms])}")
        if args.answers:
            raise OpError("invalid_arguments", f"answers: a {args.kind} assessment has no form; write its outcome")
        if not args.outcome.strip():
            raise OpError("invalid_arguments", "outcome: write the conclusion")
        outcome, answers = args.outcome.strip(), []
    else:
        fields = form_fields(form["fields"])
        clean = check_form(fields, args.answers, "answers", "invalid_arguments")
        answers = [(f["name"], clean.get(f["name"]), _points(f, clean.get(f["name"]))) for f in fields]
        score = sum(points for _, _, points in answers)
        band = next((b for b in json.loads(form["bands"]) if b.get("min", score) <= score <= b.get("max", score)), None)
        category = band["value"] if band else None
        outcome = args.outcome.strip() or (band["label"] if band else f"Score {_num(score)}")
    assessment_id = world.insert(
        "assessment",
        **by_agent(ctx),
        patient_id=args.patient_id,
        encounter_id=enc["id"] if enc else None,
        kind=args.kind,
        outcome=outcome,
        details=args.details or None,
        assessed_by=ctx.actor,
        assessed_at=world.now,
        score=score,
        category=category,
    )
    for name, value, points in answers:
        world.insert("assessment_answer", assessment_id=assessment_id, patient_id=args.patient_id, field=name, value=value, points=points)
    return {"assessment_id": assessment_id, "patient_id": args.patient_id, "kind": args.kind, "outcome": outcome, "score": _num(score), "category": category}


@op("List the structured assessment forms (scores such as a Wells score) that can be recorded, with their fields, points and categories.")
def list_assessment_forms(world: World, args: NoArgs, ctx: Ctx) -> dict:
    rows = world.q("SELECT name, label, description, fields, bands FROM assessment_form ORDER BY rowid")
    return {"forms": [{**r, "fields": fields_view(form_fields(r["fields"]), points=True), "bands": json.loads(r["bands"])} for r in rows]}


@op("List the structured assessments recorded for a patient, newest first, with a form's answers, score and category.")
def list_assessments(world: World, args: PatientArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT id AS assessment_id, kind, outcome, details, assessed_by, assessed_at, score, category FROM assessment
           WHERE patient_id = ? AND assessed_at <= ? ORDER BY assessed_at DESC, id DESC""",
        (args.patient_id, world.now),
    )
    labels = {(f["name"], x["name"]): x["label"] for f in world.q("SELECT name, fields FROM assessment_form") for x in json.loads(f["fields"])}
    answers = defaultdict(list)
    for a in world.q(
        "SELECT x.assessment_id, a.kind, x.field, x.value, x.points FROM assessment_answer x JOIN assessment a ON a.id = x.assessment_id WHERE x.patient_id = ? ORDER BY x.id",
        (args.patient_id,),
    ):
        # every field of the form, with its answer: value None is a field left unanswered, 'no' an answer of no
        answers[a["assessment_id"]].append({"field": a["field"], "label": labels.get((a["kind"], a["field"]), a["field"]), "value": a["value"], "points": _num(a["points"])})
    return {"assessments": [{**r, "assessed_at": iso(r["assessed_at"]), "score": _num(r["score"]), "answers": answers.get(r["assessment_id"], [])} for r in rows]}


class DischargeArgs(PatientArgs):
    destination: str = Field("Usual residence", description="Where the patient is going, e.g. Usual residence, Care home.")


@op(
    "Discharge the patient from their current admission. A final (not draft) discharge summary must already have been "
    "written during this admission.",
    writes=True,
)
def discharge_patient(world: World, args: DischargeArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    enc = _current_encounter(world, args.patient_id)
    if enc is None or enc["kind"] == "outpatient":
        raise OpError("not_admitted", "this patient is not currently admitted")
    summaries = {r["status"] for r in world.q("SELECT status FROM note WHERE encounter_id = ? AND kind = 'discharge_summary' AND written_at <= ?", (enc["id"], world.now))}
    if not summaries:
        raise OpError("discharge_summary_required", "write a discharge summary before discharging the patient")
    if "final" not in summaries:
        raise OpError("discharge_summary_draft", "the discharge summary is still a draft: finalise it before discharging the patient")
    world.conn.execute(
        "UPDATE encounter SET discharged_at = ?, discharge_destination = ? WHERE id = ?",
        (world.now, args.destination, enc["id"]),
    )
    return {"encounter_id": enc["id"], "discharged_at": iso(world.now)}


# --- notes ------------------------------------------------------------------


class ListNotesArgs(PatientArgs):
    encounter_id: int | None = Field(None, description="Restrict to one admission or visit.")


@op("List a patient's clinical notes (titles only), newest first.")
def list_notes(world: World, args: ListNotesArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT id, encounter_id, kind, title, author, author_role, written_at, status FROM note
           WHERE patient_id = ? AND written_at <= ? AND (? IS NULL OR encounter_id = ?)
           ORDER BY written_at DESC, id DESC""",
        (args.patient_id, world.now, args.encounter_id, args.encounter_id),
    )
    return {"notes": [{"note_id": r.pop("id"), **r, "written_at": iso(r["written_at"])} for r in rows]}


class NoteArgs(Args):
    note_id: int
    patient_id: int | None = _for_patient("note")


@op("Read the full text of a clinical note.")
def get_note(world: World, args: NoteArgs, ctx: Ctx) -> dict:
    r = world.one("SELECT * FROM note WHERE id = ? AND written_at <= ?", (args.note_id, world.now))
    if r is None:
        raise OpError("not_found", f"no note with id {args.note_id}")
    return {"note_id": r.pop("id"), **r, **{k: iso(r[k]) for k in ("written_at", "updated_at", "finalised_at")}}


class WriteNoteArgs(PatientArgs):
    kind: str = Field("progress", description="For example: clerking, ward_round, progress, discharge_summary.")
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    status: Literal["final", "draft"] = Field("final", description="draft: saved, visibly a draft, for you to edit and finalise later (update_note). A draft discharge summary does not allow discharge.")


def _has_words(title: str, body: str) -> None:
    """A document has a title and a text that are more than white space, whatever its kind."""
    for name, text in (("title", title), ("body", body)):
        if not text.strip():
            raise OpError("invalid_arguments", f"{name}: is empty; a note needs a title and its text")


def _revise(world: World, note: dict, ctx: Ctx, first: bool = False) -> None:
    """Keep the note as it now stands as its next revision."""
    n = 1 if first else world.one("SELECT coalesce(max(revision), 0) + 1 AS n FROM note_revision WHERE note_id = ?", (note["id"],))["n"]
    world.insert("note_revision", note_id=note["id"], patient_id=note["patient_id"], revision=n, at=world.now, by=ctx.actor, status=note["status"],
                 title=note["title"], body=note["body"], **by_agent(ctx))  # fmt: skip


@op("Write a clinical note in the patient's record, final (the default) or as a draft.", writes=True)
def write_note(world: World, args: WriteNoteArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    _has_words(args.title, args.body)
    enc = _current_encounter(world, args.patient_id)
    note_id = world.insert(
        "note",
        **by_agent(ctx),
        patient_id=args.patient_id,
        encounter_id=enc["id"] if enc else None,
        kind=args.kind,
        title=args.title,
        author=ctx.actor,
        written_at=world.now,
        body=args.body,
        status=args.status,
    )
    _revise(world, {"id": note_id, "patient_id": args.patient_id, "status": args.status, "title": args.title, "body": args.body}, ctx, first=True)
    return {"note_id": note_id, "status": args.status}


class UpdateNoteArgs(Args):
    note_id: int
    title: str = Field("", description="The new title; leave empty to keep it.")
    body: str = Field("", description="The new text, in full; leave empty to keep it.")
    status: Literal["draft", "final"] = Field("draft", description="final finalises the note: it can no longer be edited.")
    patient_id: int | None = _for_patient("note")


@op("Edit or finalise a draft note you wrote. A final note cannot be changed; write a further note instead.", writes=True)
def update_note(world: World, args: UpdateNoteArgs, ctx: Ctx) -> dict:
    n = world.one("SELECT * FROM note WHERE id = ? AND written_at <= ?", (args.note_id, world.now))
    if n is None:
        raise OpError("not_found", f"no note with id {args.note_id}")
    if n["status"] == "final":
        raise OpError("note_final", "this note is final and cannot be edited; write a further note")
    if n["author"] != ctx.actor:
        raise OpError("not_author", f"only its author ({n['author']}) can edit or finalise this draft")
    if not (args.title.strip() or args.body.strip() or args.status == "final"):
        raise OpError("invalid_arguments", "give a new title or text, or status final to finalise it")
    final = args.status == "final"
    title, body = args.title if args.title.strip() else n["title"], args.body if args.body.strip() else n["body"]
    _has_words(title, body)
    world.conn.execute(
        "UPDATE note SET title = ?, body = ?, status = ?, updated_at = ?, finalised_at = ?, finalised_by = ? WHERE id = ?",
        (title, body, args.status, world.now, world.now if final else None, ctx.actor if final else None, n["id"]),
    )
    _revise(world, world.one("SELECT * FROM note WHERE id = ?", (n["id"],)), ctx)
    return {"note_id": n["id"], "patient_id": n["patient_id"], "kind": n["kind"], "status": args.status}


# --- observations -----------------------------------------------------------


class ListObsArgs(PatientArgs):
    limit: int = Field(20, ge=1, le=200, description="Maximum number of observation sets, newest first.")


def _obs_sets(world: World, patient: dict, where: str, params: tuple, limit: int) -> list[dict]:
    """The newest `limit` observation sets (all readings charted at one time), in one query."""
    rows = world.conn.execute(
        f"""SELECT taken_at, id, kind, value, value_text, recorded_by, error_at, error_by, error_reason FROM observation
            WHERE patient_id = ? AND taken_at IN (SELECT DISTINCT taken_at FROM observation WHERE patient_id = ? AND taken_at <= ? {where}
                                                  ORDER BY taken_at DESC LIMIT ?)
            ORDER BY taken_at DESC, id""",
        (patient["id"], patient["id"], world.now, *params, limit),
    ).fetchall()
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)
    sets = []
    for taken_at, group in grouped.items():
        first = group[0]
        values = {r[2]: r[4] if r[3] is None else r[3] for r in group}
        error = first if first[6] is not None and first[6] <= world.now else None
        score = None if error else news2(values, patient["spo2_scale"])  # an entry in error is left out of NEWS2 and trends
        weight = next((r[1] for r in group if r[2] == "WEIGHT"), None) if not error else None
        bmi = world.one("SELECT bmi FROM bmi WHERE observation_id = ?", (weight,)) if weight is not None else None
        sets.append(
            {
                "observation_set_id": first[1],
                "taken_at": iso(taken_at),
                "recorded_by": first[5],
                "values": values,
                "news2": score["total"] if score else None,
                "bmi": bmi["bmi"] if bmi else None,  # from this weight and the latest height charted by then
                "entered_in_error": {"at": iso(error[6]), "by": error[7], "reason": error[8]} if error else None,
            }
        )
    return sets


@op(
    "List a patient's charted observations (heart rate, blood pressure, respiratory rate, SpO2, "
    "temperature, weight, height and so on) as timestamped sets with the NEWS2 score, and the BMI where a weight "
    "and a height have been charted, newest first."
)
def list_observations(world: World, args: ListObsArgs, ctx: Ctx) -> dict:
    p = _patient(world, args.patient_id)
    return {"observation_sets": _obs_sets(world, p, "", (), args.limit)}


ObsKind = Literal["HR", "SBP", "DBP", "RR", "SPO2", "TEMP", "ON_O2", "ACVPU", "WEIGHT", "HEIGHT", "GLUCOSE"]
# PLACEHOLDER for clinical review: the widest values a chart accepts, to catch a slipped decimal or a wrong unit, not
# clinical thresholds. A bundle replaces any of them (observation_bounds), e.g. for a children's ward.
OBSERVATION_BOUNDS = {"HR": (0, 300), "SBP": (0, 300), "DBP": (0, 250), "RR": (0, 80), "SPO2": (0, 100), "TEMP": (25, 45), "ON_O2": (0, 1),
                      "WEIGHT": (0.3, 400), "HEIGHT": (30, 250), "GLUCOSE": (0, 100)}  # fmt: skip
UNITS = {"TEMP": "degrees C", "WEIGHT": "kg", "HEIGHT": "cm", "GLUCOSE": "mmol/L", "SPO2": "%"}


def observation_bounds(world: World) -> dict[str, tuple[float, float]]:
    row = world.one("SELECT value FROM meta WHERE key = 'observation_bounds'")
    return OBSERVATION_BOUNDS | {k: tuple(v) for k, v in json.loads(row["value"] if row else "{}").items()}


class RecordObsArgs(PatientArgs):
    values: dict[ObsKind, float | str] = Field(
        min_length=1,
        description="Observation kind to value, e.g. {\"HR\": 88, \"SBP\": 124, \"DBP\": 76, \"ACVPU\": \"A\"}. "
        "ON_O2 is 1 on supplemental oxygen, 0 on air. WEIGHT is in kg and HEIGHT in cm.",
    )


@op("Chart a set of observations for a patient at the current time.", writes=True)
def record_observations(world: World, args: RecordObsArgs, ctx: Ctx) -> dict:
    p = _patient(world, args.patient_id)
    enc = _current_encounter(world, p["id"])
    bounds = observation_bounds(world)
    for kind, value in args.values.items():
        if kind == "ACVPU":
            if str(value).upper() not in ("A", "C", "V", "P", "U"):
                raise OpError("invalid_arguments", "ACVPU must be one of A, C, V, P, U")
            num, text = None, str(value).upper()
        else:
            try:
                num, text = float(value), None
            except ValueError:
                raise OpError("invalid_arguments", f"{kind} must be a number") from None
            low, high = bounds.get(kind, (-math.inf, math.inf))
            if not math.isfinite(num) or not low <= num <= high:
                unit = f" {UNITS[kind]}" if kind in UNITS else ""
                raise OpError("invalid_arguments", f"{kind} {value} is not a value this chart accepts ({low:g} to {high:g}{unit}); check the number and its unit")
        world.insert(
            "observation",
            **by_agent(ctx),
            patient_id=p["id"],
            encounter_id=enc["id"] if enc else None,
            taken_at=world.now,
            kind=kind,
            value=num,
            value_text=text,
            recorded_by=ctx.actor,
        )
    return {"observation_set": _obs_sets(world, p, "AND taken_at = ?", (world.now,), 1)[0]}


class ObsErrorArgs(PatientArgs):
    observation_set_id: int = Field(description="The observation set, from list_observations.")
    reason: str = Field(min_length=1, description="Why it is wrong, e.g. 'charted on the wrong patient'.")


@op(
    "Mark a charted observation set as entered in error. It stays on the chart, struck through, and is left out "
    "of the NEWS2 score and trends.",
    writes=True,
)
def mark_observations_in_error(world: World, args: ObsErrorArgs, ctx: Ctx) -> dict:
    """Open to any user, as the set's author may not be the one who finds the mistake; the reason, who marked it and
    the set as it was (`before`) are kept, and the correction applies from now on only."""
    row = world.one("SELECT * FROM observation WHERE id = ? AND taken_at <= ?", (args.observation_set_id, world.now))
    if row is None:
        raise OpError("not_found", f"no observation set with id {args.observation_set_id}")
    if row["error_at"] is not None and row["error_at"] <= world.now:
        raise OpError("already_in_error", f"this observation set was already marked entered in error by {row['error_by']}")
    before = _obs_sets(world, _patient(world, row["patient_id"]), "AND taken_at = ?", (row["taken_at"],), 1)[0]
    world.conn.execute(
        "UPDATE observation SET error_at = ?, error_by = ?, error_reason = ?, error_origin = 'agent' WHERE patient_id = ? AND taken_at = ?",
        (world.now, ctx.actor, args.reason, row["patient_id"], row["taken_at"]),
    )
    return {"observation_set_id": row["id"], "patient_id": row["patient_id"], "taken_at": iso(row["taken_at"]), "entered_in_error_at": iso(world.now),
            "before": {"values": before["values"], "recorded_by": before["recorded_by"], "news2": before["news2"]}}  # fmt: skip


class LiveVitalsArgs(PatientArgs):
    history_seconds: int = Field(0, ge=0, le=3600, description="Also return the trend over this many seconds.")
    interval_seconds: int = Field(60, ge=1, le=600, description="Spacing of trend samples.")


@op(
    "Read the patient's bedside monitor (continuous heart rate, blood pressure, SpO2 and so on). "
    "Only available where the patient is attached to a monitor, such as ED resus or critical care."
)
def get_live_vitals(world: World, args: LiveVitalsArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    enc = _current_encounter(world, args.patient_id)
    tracks = world.q("SELECT * FROM vitals_track WHERE patient_id = ? ORDER BY signal", (args.patient_id,))
    if not enc or not enc["live_monitoring"] or not tracks:
        raise OpError("not_monitored", "this patient is not attached to a bedside monitor")
    steps = min(args.history_seconds // args.interval_seconds, 120)
    times = [world.now - i * args.interval_seconds for i in range(steps, 0, -1)]
    times = [t for t in times if t >= enc["admitted_at"]]
    return {
        "at": iso(world.now),
        "current": {t["signal"]: vitals.value_at(t, world.now) for t in tracks},
        "trend": [{"at": iso(ts), **{t["signal"]: vitals.value_at(t, ts) for t in tracks}} for ts in times],
    }


# --- orders and results -----------------------------------------------------


class ListOrderablesArgs(Args):
    query: str | None = Field(None, description="Filter by name or code.")
    category: str | None = Field(None, description="For example: lab, imaging, cardiology.")


@op("Search the catalogue of investigations that can be requested.")
def list_orderables(world: World, args: ListOrderablesArgs, ctx: Ctx) -> dict:
    like = f"%{args.query or ''}%"
    rows = world.q(
        """SELECT code, name, category, modality, region, contrast, lateral FROM orderable
           WHERE (name LIKE ? OR code LIKE ?) AND (? IS NULL OR category = ?) ORDER BY category, name""",
        (like, like, args.category, args.category),
    )
    # Cost and turnaround are backend facts used for grading; clinicians are not shown them.
    return {"orderables": [{**r, "contrast": bool(r["contrast"]), "lateral": bool(r["lateral"])} for r in rows]}


class BrowseImagingArgs(Args):
    modality: str | None = Field(None, description="Omit to list modalities.")
    region: str | None = Field(None, description="With a modality: list the examinations for this body region.")


@op(
    "Browse the imaging catalogue as the request menus do: with no arguments, list modalities; with a "
    "modality, list its body regions; with a modality and a region, list the examinations."
)
def browse_imaging_catalogue(world: World, args: BrowseImagingArgs, ctx: Ctx) -> dict:
    if args.modality is None:
        rows = world.q("SELECT modality, count(*) AS n FROM orderable WHERE category = 'imaging' GROUP BY modality ORDER BY modality")
        return {"modalities": [{"modality": r["modality"], "examinations": r["n"]} for r in rows]}
    if args.region is None:
        rows = world.q(
            "SELECT region, count(*) AS n FROM orderable WHERE category = 'imaging' AND modality = ? GROUP BY region ORDER BY region",
            (args.modality,),
        )
        if not rows:
            raise OpError("not_found", f"no imaging modality {args.modality!r}")
        return {"modality": args.modality, "regions": [{"region": r["region"], "examinations": r["n"]} for r in rows]}
    rows = world.q(
        "SELECT code, name, contrast, lateral FROM orderable WHERE category = 'imaging' AND modality = ? AND region = ? ORDER BY name",
        (args.modality, args.region),
    )
    if not rows:
        raise OpError("not_found", f"no {args.modality} examinations for region {args.region!r}")
    return {"modality": args.modality, "region": args.region, "examinations": [{**r, "contrast": bool(r["contrast"]), "lateral": bool(r["lateral"])} for r in rows]}


IONISING = {"XR", "CT", "Nuclear medicine", "Fluoroscopy", "Interventional"}
RENAL_FUNCTION_MAX_AGE_DAYS = 90


def _age(world: World, dob: str) -> int:
    born, today = dob, iso(world.now)[:10]
    return int(today[:4]) - int(born[:4]) - ((today[5:], born[5:])[0] < born[5:])


def request_form(world: World | None, patient: dict | None, item: dict) -> list[dict]:
    """The fields this patient's request for this item must complete, in order. With no patient, every field the
    examination's form can have (for checking a bundle)."""
    yes_no = [("yes", "Yes"), ("no", "No")]
    fields = [{"name": "clinical_details", "label": "Clinical summary", "kind": "text", "required": item["category"] == "imaging"}]
    if item["category"] != "imaging":
        return fields
    fields.append({"name": "clinical_question", "label": "Clinical question to be answered", "kind": "text", "required": True})
    if item["lateral"]:
        fields.append({"name": "laterality", "label": "Side", "kind": "choice", "options": [("left", "Left"), ("right", "Right"), ("bilateral", "Both")], "required": True})
    if item["modality"] in IONISING and (patient is None or (patient["sex"] == "F" and 12 <= _age(world, patient["dob"]) <= 55)):
        fields.append({"name": "pregnancy", "label": "Pregnancy status", "kind": "choice", "options": [("not_pregnant", "Not pregnant"), ("possibly_pregnant", "Could be pregnant"), ("pregnant", "Pregnant")], "required": True})
    if item["contrast"]:
        fields += [
            {"name": "egfr", "label": "Latest eGFR (mL/min/1.73m2)", "kind": "number", "required": True},
            {"name": "egfr_date", "label": "Date of that eGFR (YYYY-MM-DD)", "kind": "date", "required": True},
            {"name": "contrast_reaction", "label": "Previous reaction to contrast", "kind": "choice", "options": yes_no, "required": True},
        ]
    if item["modality"] == "MRI":
        fields += [
            {"name": "mri_device", "label": "Pacemaker, defibrillator or other implanted device", "kind": "choice", "options": yes_no, "required": True},
            {"name": "mri_metal", "label": "Metal implants, fragments or surgery with metalwork", "kind": "choice", "options": yes_no, "required": True},
        ]
    fields.append({"name": "mobility", "label": "Mobility", "kind": "choice", "options": [("walking", "Walking"), ("chair", "Wheelchair"), ("bed", "Bed")], "required": True})
    return fields


def request_choices(item: dict) -> dict[str, set[str]]:
    """The structured fields a result can depend on for this item, with their values: its request form's choices,
    the priority and the collection."""
    out = {f["name"]: {v for v, _ in f["options"]} for f in request_form(None, None, item) if f["kind"] == "choice"}
    return out | {"priority": {"routine", "urgent"}, "collection": {"now", "phlebotomy_round", "scheduled"}}


def _form_view(fields: list[dict]) -> list[dict]:
    return [{**f, "options": [{"value": v, "label": label} for v, label in f["options"]]} if "options" in f else f for f in fields]


class RequestFormArgs(PatientArgs):
    orderable_code: str


MAX_SCHEDULE_DAYS = 90  # how far ahead a request can be dated (a system limit, not a clinical one)


def next_phlebotomy_round(world: World, after: int) -> int | None:
    """The first routine phlebotomy round after `after` (the bundle's phlebotomy_rounds, HH:MM UTC, every day)."""
    rounds = json.loads((world.one("SELECT value FROM meta WHERE key = 'phlebotomy_rounds'") or {"value": "[]"})["value"])
    midnight = after - after % 86400
    times = [midnight + day * 86400 + int(r[:2]) * 3600 + int(r[3:5]) * 60 for day in (0, 1) for r in rounds]
    return min((t for t in times if t > after), default=None)


def collection_choices(world: World, item: dict) -> list[dict]:
    """When a request's sample can be taken (or the test done): now, on the next phlebotomy round for a laboratory
    blood test where the hospital has rounds, or at a date and time given."""
    choices = [{"value": "now", "label": "Now"}]
    round_at = next_phlebotomy_round(world, world.now) if item["category"] == "lab" and not item["point_of_care"] else None
    if round_at is not None:
        choices.append({"value": "phlebotomy_round", "label": "Next phlebotomy round", "at": iso(round_at)})
    return choices + [{"value": "scheduled", "label": "At a set date and time"}]


def _collect_at(world: World, item: dict, collection: str, collect_at: str) -> int:
    """When the sample for a request is to be taken."""
    if collect_at.strip() and collection != "scheduled":
        raise OpError("invalid_arguments", "collect_at: a time is given only with collection 'scheduled'")
    if collection == "now":
        return world.now
    if collection == "phlebotomy_round":
        choice = next((c for c in collection_choices(world, item) if c["value"] == "phlebotomy_round"), None)
        if choice is None:
            raise OpError("invalid_arguments", f"collection: there is no phlebotomy round for {item['name']}; take it now or schedule a time")
        return parse_iso(choice["at"])
    t = parse_datetime(collect_at)
    if t is None:
        raise OpError("invalid_arguments", "collect_at: give the date and time to take it as YYYY-MM-DDTHH:MM")
    if t <= world.now:
        raise OpError("invalid_arguments", "collect_at: must be later than now; choose collection 'now' to take it now")
    if t > world.now + MAX_SCHEDULE_DAYS * 86400:
        raise OpError("invalid_arguments", f"collect_at: a request can be dated at most {MAX_SCHEDULE_DAYS} days ahead")
    return t


@op(
    "Get the request form for an investigation: the fields this patient's request must complete, with allowed values, "
    "and when it can be collected (now, the next phlebotomy round, or a set time)."
)
def get_request_form(world: World, args: RequestFormArgs, ctx: Ctx) -> dict:
    p = _patient(world, args.patient_id)
    item = world.one("SELECT * FROM orderable WHERE code = ?", (args.orderable_code,))
    if item is None:
        raise OpError("not_found", f"no orderable with code {args.orderable_code}")
    return {"orderable_code": item["code"], "name": item["name"], "fields": _form_view(request_form(world, p, item)), "collection": collection_choices(world, item)}


def _check_request(world: World, patient: dict, item: dict, details: dict[str, str]) -> dict[str, str]:
    """Validate a completed request form. Returns the cleaned fields."""
    fields = request_form(world, patient, item)
    allowed = {f["name"] for f in fields}
    unknown = sorted(set(details) - allowed)
    if unknown:
        raise OpError("invalid_arguments", f"request_details: fields not on this form: {', '.join(unknown)}")
    clean = {k: str(v).strip() for k, v in details.items() if str(v).strip()}
    missing = [f["label"] for f in fields if f["required"] and f["name"] not in clean]
    if missing:
        raise OpError("incomplete_request", f"the request form is incomplete: {'; '.join(missing)}")
    for f in fields:
        value = clean.get(f["name"])
        if value is None:
            continue
        if f["kind"] == "choice" and value not in {v for v, _ in f["options"]}:
            raise OpError("invalid_arguments", f"request_details.{f['name']}: must be one of {', '.join(v for v, _ in f['options'])}")
        if f["kind"] == "number":
            try:
                float(value)
            except ValueError:
                raise OpError("invalid_arguments", f"request_details.{f['name']}: must be a number") from None
        if f["kind"] == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise OpError("invalid_arguments", f"request_details.{f['name']}: must be a date as YYYY-MM-DD")
            if value > iso(world.now)[:10]:
                raise OpError("invalid_arguments", f"request_details.{f['name']}: cannot be in the future")
    if item["contrast"]:
        cutoff = iso(world.now - RENAL_FUNCTION_MAX_AGE_DAYS * 86400)[:10]
        if clean["egfr_date"] < cutoff:
            raise OpError("renal_function_out_of_date", f"contrast examinations need an eGFR from the last {RENAL_FUNCTION_MAX_AGE_DAYS} days")
    return clean


CONTRAST_EGFR_THRESHOLD = 30


def _latest_value(world: World, patient_id: int, analyte: str) -> float | None:
    """The most recent reported value of an analyte, as a clinician would find it in the results."""
    row = world.one(
        """SELECT i.value FROM result_item i JOIN result r ON r.id = i.result_id
           WHERE r.patient_id = ? AND r.reported_at <= ? AND i.analyte = ? AND i.value IS NOT NULL
           ORDER BY r.performed_at DESC, r.id DESC LIMIT 1""",
        (patient_id, world.now, analyte),
    )
    return row["value"] if row else None


class PlaceOrderArgs(PatientArgs):
    orderable_code: str = Field(description="Code from list_orderables.")
    priority: Literal["routine", "urgent"] = "routine"
    clinical_details: str = Field("", description="Clinical summary: relevant history and findings. Required for imaging.")
    request_details: dict[str, str] = Field(
        {}, description="The rest of the request form, as field name to value. Call get_request_form to see the fields this request needs."
    )
    override_reason: str = Field("", description="Only if an alert has been raised and the request is still intended.")
    collection: Literal["now", "phlebotomy_round", "scheduled"] = Field(
        "now", description="When the sample is taken (or the test done): now; on the next routine phlebotomy round (laboratory blood tests); or scheduled at collect_at. Until then the request is scheduled and can be cancelled."
    )
    collect_at: str = Field("", description="Only for collection 'scheduled': the date and time, YYYY-MM-DDTHH:MM (UTC), later than now.")


def _flag(item: dict) -> str | None:
    v = item.get("value")
    if v is None:
        return None
    if item.get("ref_low") is not None and v < item["ref_low"]:
        return "L"
    if item.get("ref_high") is not None and v > item["ref_high"]:
        return "H"
    return None


def insert_result(world: World, *, patient_id, encounter_id, order_id, code, performed_at, reported_at, payload, source="hospital_lab", origin="record", origin_action_id=None):
    result_id = world.insert(
        "result",
        patient_id=patient_id,
        encounter_id=encounter_id,
        order_id=order_id,
        orderable_code=code,
        performed_at=performed_at,
        reported_at=reported_at,
        reported_by=payload.get("reported_by"),
        report_text=payload.get("report_text"),
        source=source,
        origin=origin,
        origin_action_id=origin_action_id,
    )
    for item in payload.get("items", []):
        world.insert(
            "result_item",
            result_id=result_id,
            analyte=item["analyte"],
            value=item.get("value"),
            value_text=item.get("value_text"),
            unit=item.get("unit"),
            ref_low=item.get("ref_low"),
            ref_high=item.get("ref_high"),
            flag=_flag(item),
        )
    return result_id


@op(
    "Order an investigation (blood test, imaging, ECG and so on) for a patient, to be collected now, on the next "
    "phlebotomy round or at a set time.",
    writes=True,
)
def place_order(world: World, args: PlaceOrderArgs, ctx: Ctx) -> dict:
    p = _patient(world, args.patient_id)
    item = world.one("SELECT * FROM orderable WHERE code = ?", (args.orderable_code,))
    if item is None:
        raise OpError("not_found", f"no orderable with code {args.orderable_code}")
    form = _check_request(world, p, item, {**args.request_details, "clinical_details": args.clinical_details})
    collect_at = _collect_at(world, item, args.collection, args.collect_at)
    details = form.get("clinical_details", "")
    override = args.override_reason.strip()
    egfr = _latest_value(world, p["id"], "eGFR")
    renal_alert = bool(item["contrast"]) and egfr is not None and egfr < CONTRAST_EGFR_THRESHOLD
    if renal_alert and not override:
        raise OpError(
            "contrast_alert",
            f"CONTRAST ALERT: latest eGFR is {egfr:g}. {item['name']} uses iodinated contrast. "
            "Not requested. To request anyway, give a reason for overriding the alert.",
        )
    enc = _current_encounter(world, p["id"])
    order = {
        "patient_id": p["id"], "encounter_id": enc["id"] if enc else None, "orderable_code": item["code"], "priority": args.priority,
        "ordered_at": world.now, "due_at": collect_at + item["tat_seconds"], "request_details": json.dumps(form, sort_keys=True),
        "collection": args.collection, "ordered_by": ctx.actor, **by_agent(ctx),
    }  # fmt: skip
    order_id = world.insert(
        "order",
        **order,
        clinical_details=details,
        override_reason=override if renal_alert else None,
        egfr_on_record=egfr if item["contrast"] else None,
        collect_at=collect_at,
    )
    if collect_at == world.now:
        collect_sample(world, {**order, "id": order_id}, world.now, item)
    status = "in_progress" if collect_at == world.now else "scheduled"
    return {"order_id": order_id, "patient_id": p["id"], "name": item["name"], "status": status, "collection": args.collection, "collect_at": iso(collect_at)}


def collector(item: dict) -> str:
    """Who takes a sample (or does the test) at a later time: the role, which also signs the collection."""
    return "Phlebotomist" if item["category"] == "lab" and not item["point_of_care"] else "Radiographer" if item["category"] == "imaging" else "Ward nurse"


def collect_sample(world: World, order: dict, t: int, item: dict | None = None) -> None:
    """Take a request's sample (or do the test) at `t`. What it shows is evaluated now, at the sample time, for what
    was asked for (sample_payload); it is reported at the order's due time, a turnaround later. A scheduled request
    whose admission has ended by then is not collected, and is cancelled saying so.

    Taken with the request, the collection is the requester's own (their action); taken later, staff collect it, and
    the requesting action is recorded as its cause: no agent action is made up at sampling time. The result is the
    laboratory's (origin staff), caused by the request."""
    later = t > order["ordered_at"]
    item = item or world.one("SELECT * FROM orderable WHERE code = ?", (order["orderable_code"],))
    role = collector(item)
    by = {"by": role, "role": role, "origin": "staff"} if later else {"by": order["ordered_by"], "role": None, "origin": "agent"}
    cause = order["origin_action_id"]
    if later and order["encounter_id"] is not None and not admission_active(world, order, t):
        world.conn.execute(
            """UPDATE "order" SET status = 'cancelled', cancelled_at = ?, cancelled_by = ?, cancel_reason = ? WHERE id = ?""",
            (t, role, "Not collected: the patient had left by the collection time", order["id"]),
        )
        world.insert("collection_event", order_id=order["id"], patient_id=order["patient_id"], at=t, status="not_collected", origin_action_id=cause, **by)
        return
    request = {**json.loads(order["request_details"] or "{}"), "priority": order["priority"], "collection": order["collection"]}
    world.conn.execute('UPDATE "order" SET collected_at = ? WHERE id = ?', (t, order["id"]))
    world.insert("collection_event", order_id=order["id"], patient_id=order["patient_id"], at=t, status="collected", origin_action_id=cause, **by)
    payload = sample_payload(world, order["patient_id"], item, t, request)
    if payload:
        enc = scenario.encounter_at(world, order["patient_id"], t) if later else order["encounter_id"]
        insert_result(world, patient_id=order["patient_id"], encounter_id=enc if enc is not None else order["encounter_id"], order_id=order["id"],
                      code=item["code"], performed_at=t, reported_at=order["due_at"], payload=payload,
                      source="point_of_care" if item["point_of_care"] else "hospital_lab", origin="staff", origin_action_id=cause)  # fmt: skip


def request_matches(wanted: dict, request: dict) -> bool:
    """Does a request's structured fields meet a branch's or rule's conditions: each field has the value given, or one
    of a list of values (any case)?"""
    return all(str(request.get(f, "")).lower() in [str(v).lower() for v in (w if isinstance(w, list) else [w])] for f, w in wanted.items())


def sample_payload(world: World, patient_id: int, item: dict, t: int, request: dict | None = None) -> dict | None:
    """What a sample for this test taken at `t` shows: the result window covering `t` (the latest-starting, then the
    latest declared), or else the orderable's fallback. A window may branch on the request's structured fields (a
    side, priority): the first matching branch gives the result; a request no branch matches gets the window's own
    payload where it has one, otherwise the fallback as if there were no window. The default fallback keeps the
    catalogue normal's panel but reports each analyte at the patient's latest value from a sample taken before `t`,
    in the same unit, so a repeat never contradicts the history; `normal` reports the catalogue normal unchanged."""
    window = world.one(
        """SELECT payload FROM latent_result WHERE patient_id = ? AND orderable_code = ?
             AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to > ?)
           ORDER BY valid_from IS NULL, valid_from DESC, id DESC LIMIT 1""",
        (patient_id, item["code"], t, t),
    )
    if window:
        payload = json.loads(window["payload"])
        branches = payload.pop("branches", None)
        hit = next((b for b in branches or [] if request_matches(b["request"], request or {})), None)
        if hit is not None:
            return {"report_text": hit.get("report_text"), "reported_by": hit.get("reported_by") or payload.get("reported_by"), "items": hit.get("items") or []}
        if not branches or payload.get("report_text") or payload.get("items"):
            return payload
    normal = json.loads(item["normal"]) if item["normal"] else None
    if normal is None or item["fallback"] == "normal" or not normal.get("items"):
        return normal
    items = []
    max_age = item["fallback_max_age_days"]
    for i in normal["items"]:
        last = last_value(world, patient_id, i["analyte"], t)
        recent = last is not None and last["performed_at"] >= t - max_age * 86400
        value = convert_unit(last["value"], last["unit"], i.get("unit")) if last is not None else None
        if last is None:
            items.append(i)
        elif recent and value is not None:
            items.append({**i, "value": value, "value_text": None})
        elif recent and last["value"] is None and _unit_key(last["unit"]) == _unit_key(i.get("unit")):
            items.append({**i, "value": None, "value_text": last["value_text"]})
        elif last["value"] is not None and value is None:
            raise BundleError(f"{item['code']} for patient {patient_id}: the latest {i['analyte']} ({last['value']:g} {last['unit']}) cannot be converted "
                              f"to {i.get('unit')}, so a repeat has no value to report; give the test a result window or declare fallback normal")  # fmt: skip
        elif last["flag"]:
            raise BundleError(f"{item['code']} for patient {patient_id}: the latest {i['analyte']} was abnormal but is older than {max_age:g} days, "
                              "so a repeat has no value to report; give the test a result window, a longer fallback_max_age_days or fallback normal")  # fmt: skip
        else:  # an old normal value: the catalogue normal contradicts nothing
            items.append(i)
    return {**normal, "items": items}


class BundleError(Exception):
    """The bundle does not say what the world should do here, and inventing an answer could contradict the record.
    Not an operation error the agent caused: it means the bundle needs fixing (lint reports these)."""


def last_value(world: World, patient_id: int, analyte: str, t: int) -> dict | None:
    """The latest value of an analyte from a sample taken before `t`."""
    return world.one(
        """SELECT x.value, x.value_text, x.unit, x.flag, r.performed_at FROM result_item x JOIN result r ON r.id = x.result_id
           WHERE r.patient_id = ? AND x.analyte = ? AND r.performed_at < ?
             AND (x.value IS NOT NULL OR x.value_text IS NOT NULL) ORDER BY r.performed_at DESC, r.id DESC LIMIT 1""",
        (patient_id, analyte, t),
    )


# Conversions between units that need no knowledge of the analyte (molar masses are not attempted).
_UNIT_FACTORS = {("g/dl", "g/l"): Decimal(10), ("mg/dl", "mg/l"): Decimal(10), ("mmol/l", "umol/l"): Decimal(1000), ("g/l", "mg/l"): Decimal(1000),
                 ("mmol/l", "nmol/l"): Decimal(1000000), ("umol/l", "nmol/l"): Decimal(1000), ("x10^9/l", "10^9/l"): Decimal(1)}  # fmt: skip


def _unit_key(unit: str | None) -> str:
    return (unit or "").strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")


def convert_unit(value: float | None, from_unit: str | None, to_unit: str | None) -> float | None:
    """`value` in `to_unit`, or None if it cannot be converted (or there is no value)."""
    if value is None:
        return None
    a, b = _unit_key(from_unit), _unit_key(to_unit)
    if a == b:
        return value
    if (a, b) in _UNIT_FACTORS:
        return float(Decimal(repr(value)) * _UNIT_FACTORS[a, b])
    if (b, a) in _UNIT_FACTORS:
        return float(Decimal(repr(value)) / _UNIT_FACTORS[b, a])
    return None


class OrderArgs(Args):
    order_id: int
    patient_id: int | None = _for_patient("request")


@op("Cancel an investigation that has not yet been reported, including one scheduled for later.", writes=True)
def cancel_order(world: World, args: OrderArgs, ctx: Ctx) -> dict:
    o = world.one('SELECT * FROM "order" WHERE id = ?', (args.order_id,))
    if o is None:
        raise OpError("not_found", f"no order with id {args.order_id}")
    if o["status"] == "cancelled":
        raise OpError("already_cancelled", "this order has already been cancelled")
    if world.one("SELECT 1 FROM result WHERE order_id = ? AND reported_at <= ?", (o["id"], world.now)):
        raise OpError("already_resulted", "this order has already been reported and cannot be cancelled")
    world.conn.execute("DELETE FROM result WHERE order_id = ?", (o["id"],))
    world.conn.execute("""UPDATE "order" SET status = 'cancelled', cancelled_at = ?, cancelled_by = ? WHERE id = ?""", (world.now, ctx.actor, o["id"]))
    return {"order_id": o["id"], "patient_id": o["patient_id"], "status": "cancelled"}


@op(
    "List the investigations ordered for a patient, newest first, with their status: scheduled (to be collected at "
    "collect_at), in_progress, resulted or cancelled."
)
def list_orders(world: World, args: PatientArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT o.*, i.name, i.category,
                  (SELECT r.id FROM result r WHERE r.order_id = o.id AND r.reported_at <= ?) AS result_id
           FROM "order" o JOIN orderable i ON i.code = o.orderable_code
           WHERE o.patient_id = ? AND o.ordered_at <= ? ORDER BY o.ordered_at DESC, o.id DESC""",
        (world.now, args.patient_id, world.now),
    )
    return {
        "orders": [
            {
                "order_id": r["id"],
                "orderable_code": r["orderable_code"],
                "name": r["name"],
                "category": r["category"],
                "priority": r["priority"],
                "clinical_details": r["clinical_details"],
                "request_details": json.loads(r["request_details"] or "{}"),
                "ordered_by": r["ordered_by"],
                "ordered_at": iso(r["ordered_at"]),
                "collection": r["collection"],
                "collect_at": iso(r["collect_at"]),
                "collected_at": iso(r["collected_at"]),
                "status": "cancelled" if r["status"] == "cancelled" else "scheduled" if r["collected_at"] is None else "resulted" if r["result_id"] else "in_progress",
                "cancel_reason": r["cancel_reason"],
                "result_id": r["result_id"],
            }
            for r in rows
        ]
    }


class ListResultsArgs(PatientArgs):
    category: str | None = Field(None, description="For example: lab, imaging, cardiology.")
    limit: int = Field(50, ge=1, le=200)


@op("List a patient's reported investigation results (titles only), newest first.")
def list_results(world: World, args: ListResultsArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT r.id, r.orderable_code, i.name, i.category, r.performed_at, r.reported_at, r.acknowledged_at, r.source,
                  (SELECT count(*) FROM result_item x WHERE x.result_id = r.id AND x.flag IS NOT NULL) AS abnormal
           FROM result r JOIN orderable i ON i.code = r.orderable_code
           WHERE r.patient_id = ? AND r.reported_at <= ? AND (? IS NULL OR i.category = ?)
           ORDER BY r.reported_at DESC, r.id DESC LIMIT ?""",
        (args.patient_id, world.now, args.category, args.category, args.limit),
    )
    return {
        "results": [
            {
                "result_id": r["id"],
                "orderable_code": r["orderable_code"],
                "name": r["name"],
                "category": r["category"],
                "performed_at": iso(r["performed_at"]),
                "reported_at": iso(r["reported_at"]),
                "source": r["source"],
                "abnormal_values": r["abnormal"],
                "acknowledged": r["acknowledged_at"] is not None and r["acknowledged_at"] <= world.now,
            }
            for r in rows
        ]
    }


class ResultArgs(Args):
    result_id: int
    patient_id: int | None = _for_patient("result")


@op("Read a reported investigation result in full: values with reference ranges, or the report text.")
def get_result(world: World, args: ResultArgs, ctx: Ctx) -> dict:
    r = world.one(
        """SELECT r.*, i.name, i.category FROM result r JOIN orderable i ON i.code = r.orderable_code
           WHERE r.id = ? AND r.reported_at <= ?""",
        (args.result_id, world.now),
    )
    if r is None:
        raise OpError("not_found", f"no reported result with id {args.result_id}")
    items = world.q(
        "SELECT analyte, value, value_text, unit, ref_low, ref_high, flag FROM result_item WHERE result_id = ? ORDER BY id",
        (r["id"],),
    )
    return {
        "result_id": r["id"],
        "patient_id": r["patient_id"],
        "orderable_code": r["orderable_code"],
        "name": r["name"],
        "category": r["category"],
        "performed_at": iso(r["performed_at"]),
        "reported_at": iso(r["reported_at"]),
        "reported_by": r["reported_by"],
        "source": r["source"],
        "report_text": r["report_text"],
        "items": items,
        "acknowledged_by": r["acknowledged_by"],
        "acknowledged_at": iso(r["acknowledged_at"]),
    }


class AnalyteHistoryArgs(PatientArgs):
    analyte: str = Field("", description="An analyte, e.g. Haemoglobin, Creatinine, eGFR. Leave empty to list the analytes on record.")


SOURCE_LABELS = {"gp": "GP", "other_hospital": "Other hospital"}
ENCOUNTER_LABELS = {"inpatient": "Previous admission", "emergency": "Emergency attendance", "outpatient": "Outpatients"}


@op(
    "Cumulative results: every reported value of one analyte for a patient, oldest first, across this admission, "
    "earlier admissions and attendances and results from the GP, with reference ranges and flags. With no analyte, "
    "list the analytes on record."
)
def get_analyte_history(world: World, args: AnalyteHistoryArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    if not args.analyte.strip():
        rows = world.q(
            """SELECT x.analyte, count(*) AS results, max(r.performed_at) AS latest FROM result_item x JOIN result r ON r.id = x.result_id
               WHERE r.patient_id = ? AND r.reported_at <= ? GROUP BY x.analyte COLLATE NOCASE ORDER BY x.analyte COLLATE NOCASE""",
            (args.patient_id, world.now),
        )
        return {"analytes": [{"analyte": r["analyte"], "results": r["results"], "latest_sample_at": iso(r["latest"])} for r in rows]}
    rows = world.q(
        """SELECT r.id AS result_id, r.orderable_code, r.performed_at, r.reported_at, r.source, r.encounter_id, e.kind AS encounter_kind,
                  x.analyte, x.value, x.value_text, x.unit, x.ref_low, x.ref_high, x.flag
           FROM result_item x JOIN result r ON r.id = x.result_id LEFT JOIN encounter e ON e.id = r.encounter_id
           WHERE r.patient_id = ? AND r.reported_at <= ? AND x.analyte = ? COLLATE NOCASE ORDER BY r.performed_at, r.id""",
        (args.patient_id, world.now, args.analyte.strip()),
    )
    if not rows:
        raise OpError("not_found", f"no reported {args.analyte.strip()} results for this patient; call with no analyte to list those on record")
    enc = _current_encounter(world, args.patient_id)
    series = []
    for r in rows:
        setting = SOURCE_LABELS.get(r["source"]) or (
            ("This admission" if enc["kind"] == "inpatient" else "This attendance") if enc and r["encounter_id"] == enc["id"]
            else ENCOUNTER_LABELS.get(r["encounter_kind"], "Hospital"))
        series.append({
            "result_id": r["result_id"], "orderable_code": r["orderable_code"], "sampled_at": iso(r["performed_at"]), "reported_at": iso(r["reported_at"]),
            "value": r["value"], "value_text": r["value_text"], "unit": r["unit"], "ref_low": r["ref_low"], "ref_high": r["ref_high"], "flag": r["flag"],
            "source": r["source"], "encounter_id": r["encounter_id"], "setting": setting,
        })  # fmt: skip
    return {"analyte": rows[0]["analyte"], "series": series}


@op("Acknowledge (sign off) a reported result to record that it has been seen and acted on.", writes=True)
def acknowledge_result(world: World, args: ResultArgs, ctx: Ctx) -> dict:
    r = world.one("SELECT * FROM result WHERE id = ? AND reported_at <= ?", (args.result_id, world.now))
    if r is None:
        raise OpError("not_found", f"no reported result with id {args.result_id}")
    if r["acknowledged_at"] is not None:
        raise OpError("already_acknowledged", f"this result was already acknowledged by {r['acknowledged_by']}")
    world.conn.execute(
        "UPDATE result SET acknowledged_by = ?, acknowledged_at = ? WHERE id = ?", (ctx.actor, world.now, r["id"])
    )
    return {"result_id": r["id"], "patient_id": r["patient_id"], "acknowledged_at": iso(world.now)}


# --- medications ------------------------------------------------------------


class ListMedsArgs(PatientArgs):
    include_stopped: bool = False


@op("List a patient's prescribed medications.")
def list_medications(world: World, args: ListMedsArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT * FROM medication WHERE patient_id = ? AND started_at <= ?
           AND (? OR stopped_at IS NULL OR stopped_at > ?) ORDER BY drug""",
        (args.patient_id, world.now, args.include_stopped, world.now),
    )
    given = world.q(
        """SELECT medication_id, at, status, recorded_by, note, dose FROM administration
           WHERE patient_id = ? AND at <= ? ORDER BY at DESC, id DESC""",
        (args.patient_id, world.now),
    )
    return {
        "medications": [
            {
                "medication_id": r["id"],
                "kind": r["kind"],
                "drug": r["drug"],
                "product": r["product"],
                "dose": r["dose"],
                "route": r["route"],
                "frequency": r["frequency"],
                "indication": r["indication"],
                "prescriber": r["prescriber"],
                "started_at": iso(r["started_at"]),
                "stopped_at": iso(r["stopped_at"]),
                "stop_reason": r["stop_reason"],
                "duration": r["duration"],
                "administrations": [
                    {"at": iso(a["at"]), "status": a["status"], "dose": a["dose"], "recorded_by": a["recorded_by"], "note": a["note"]}
                    for a in given
                    if a["medication_id"] == r["id"]
                ][:10],
            }
            for r in rows
        ]
    }


class PrescribeArgs(PatientArgs):
    drug: str = Field(min_length=1, description="Drug name, e.g. Amoxicillin (or a product name, which also identifies the drug).")
    product: str = Field("", description="The product: form and strength, e.g. 'Amoxicillin 500mg capsules'. Required when the drug has products; search_formulary lists them.")
    dose: str = Field(min_length=1, description="A number and a unit, e.g. '1 g', '40 mg', '2 tablets', '10 mL', '4 units'.")
    route: str = Field(min_length=1, description="One of: PO, IV, SC, IM, INH, NEB, TOP, PR, SL, NG, IT, EPIDURAL.")
    frequency: str = Field(min_length=1, description="once, OD, OM, ON, BD, TDS, QDS, 'every 4 hours', 'once weekly (Mondays)' or 'over 8 hours' (infusions).")
    kind: Literal["regular", "prn", "stat", "tto"] = Field(
        "regular", description="regular, prn (as required), stat (single dose now) or tto (to take out on discharge)."
    )
    duration: str = Field("", description="Course length or supply, e.g. 5 days, 14 days, ongoing.")
    indication: str = ""
    override_reason: str = Field("", description="Only if an alert has been raised and prescribing is still intended.")


class FormularyArgs(Args):
    query: str = Field("", description="Part of the drug name. Leave empty to list the whole formulary.")


@op("Search the hospital formulary by drug or product name. Returns matching drugs with their products (form, strength and routes).")
def search_formulary(world: World, args: FormularyArgs, ctx: Ctx) -> dict:
    like = f"%{args.query.strip()}%"
    names = [
        r["name"]
        for r in world.q(
            "SELECT name FROM drug WHERE name LIKE ? OR name IN (SELECT drug FROM product WHERE name LIKE ?) ORDER BY name LIMIT 500",
            (like, like),
        )
    ]
    products = defaultdict(list)
    if names:
        marks = ",".join("?" * len(names))
        for p in world.q(f"SELECT * FROM product WHERE drug IN ({marks}) ORDER BY name", names):
            products[p["drug"]].append({"name": p["name"], "form": p["form"], "routes": json.loads(p["routes"]), "controlled_drug": p["controlled_drug"]})
    return {"drugs": names, "products": {n: products[n] for n in names if products[n]}}


ROUTES = {
    "PO": ("PO", "ORAL", "BY MOUTH", "O"),
    "IV": ("IV", "INTRAVENOUS"),
    "SC": ("SC", "S/C", "SUBCUT", "SUBCUTANEOUS"),
    "IM": ("IM", "INTRAMUSCULAR"),
    "INH": ("INH", "INHALED"),
    "NEB": ("NEB", "NEBULISED"),
    "TOP": ("TOP", "TOPICAL", "CUTANEOUS"),
    "PR": ("PR", "RECTAL"),
    "SL": ("SL", "SUBLINGUAL"),
    "NG": ("NG", "NASOGASTRIC", "GASTROENTERAL"),
    "IT": ("IT", "INTRATHECAL"),
    "EPIDURAL": ("EPIDURAL",),
    "PV": ("PV", "VAGINAL"),
    "BUCC": ("BUCC", "BUCCAL", "OROMUCOSAL"),
    "TD": ("TD", "TRANSDERMAL"),
    "EYE": ("EYE", "OCULAR"),
    "EAR": ("EAR", "AURICULAR"),
    "NASAL": ("NASAL", "INTRANASAL"),
    "IO": ("IO", "INTRAOSSEOUS"),
    "ID": ("ID", "INTRADERMAL"),
}
_ROUTE_ALIASES = {alias: route for route, aliases in ROUTES.items() for alias in aliases}


def _prescribing_safeguards(world: World, patient_id: int, drug: dict, route: str, args, product: dict | None = None) -> None:
    """Hard stops of the kind an electronic prescribing system enforces. Each maps to a Never Event."""
    flags = set(json.loads(drug["flags"])) | set(json.loads(product["flags"]) if product else [])
    name = product["name"] if product else drug["name"]
    permitted = json.loads(drug["routes"])
    if permitted and route not in permitted:
        raise OpError("route_not_permitted", f"{drug['name']} cannot be prescribed by the {route} route (permitted: {', '.join(permitted)})")
    if "insulin" in flags and not re.fullmatch(r"\d+(\.\d+)?\s*units?", args.dose.strip(), re.IGNORECASE):
        raise OpError("unsafe_abbreviation", "insulin doses must be written as a number followed by the word 'units' in full")
    if "weekly" in flags and not _is_once_weekly(args.frequency):
        raise OpError("weekly_only", f"{drug['name']} is prescribed once WEEKLY for non-cancer treatment; choose once weekly and give the day")
    if "critical_care_only" in flags:
        enc = _current_encounter(world, patient_id)
        if not enc or enc["location_kind"] not in ("ICU", "ED_RESUS"):
            raise OpError("restricted_area", f"{name} is restricted to critical care areas and cannot be prescribed here")


def _resolve_drug(world: World, name: str, product_name: str) -> tuple[dict, dict | None]:
    """The drug and, when it has products, the chosen product. A product name alone identifies both."""
    drug = world.one("SELECT * FROM drug WHERE name = ?", (name,))
    if drug is None:
        as_product = world.one("SELECT * FROM product WHERE name = ?", (name,))
        if as_product is None:
            raise OpError("not_in_formulary", f"{name!r} is not in the formulary; search the formulary for the drug name")
        drug, product_name = world.one("SELECT * FROM drug WHERE name = ?", (as_product["drug"],)), product_name or as_product["name"]
    products = world.q("SELECT * FROM product WHERE drug = ? ORDER BY name", (drug["name"],))
    if not products:
        return drug, None
    if not product_name:
        listed = "; ".join(p["name"] for p in products[:15])
        raise OpError("product_required", f"choose a product (form and strength) for {drug['name']}: {listed}")
    product = next((p for p in products if p["name"].lower() == product_name.lower()), None)
    if product is None:
        raise OpError("not_found", f"{product_name!r} is not a product of {drug['name']}; search the formulary for its products")
    return drug, product


def _allergy_conflict(world: World, patient_id: int, drug: dict) -> dict | None:
    names = {drug["name"].lower(), *(c.lower() for c in json.loads(drug["classes"]))}
    for a in world.q("SELECT substance, reaction FROM allergy WHERE patient_id = ? ORDER BY id", (patient_id,)):
        if a["substance"].lower() in names:
            return a
    return None


DOSE_UNITS = (
    "mg", "g", "micrograms", "nanograms", "mL", "L", "units", "mmol", "tablet", "tablets", "capsule", "capsules",
    "puff", "puffs", "drop", "drops", "sachet", "sachets", "patch", "patches", "application", "applications",
    "suppository", "suppositories", "spray", "sprays", "vial", "vials", "ampoule", "ampoules", "dose", "doses",
    "pessary", "pessaries",
)  # fmt: skip
_DOSE_FORMAT = re.compile(r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*([A-Za-z]+)")
_UNIT_LOOKUP = {u.lower(): u for u in DOSE_UNITS} | {"microgram": "micrograms", "nanogram": "nanograms", "ml": "mL", "unit": "units"}


def _clean_dose(dose: str) -> str:
    """A dose is a number (or range) and a unit, written so it cannot be misread."""
    m = _DOSE_FORMAT.fullmatch(dose.strip())
    if m and m.group(1).lower() in ("mcg", "ug", "iu", "u"):
        raise OpError("unsafe_abbreviation", f"write the unit in full rather than {m.group(1)!r} (micrograms, units)")
    if not m or m.group(1).lower() not in _UNIT_LOOKUP:
        raise OpError("invalid_arguments", f"dose: {dose!r} must be a number and a unit, e.g. '500 mg' or '2 tablets' (units: {', '.join(DOSE_UNITS[:12])}, ...)")
    low, high = _DOSE.fullmatch(dose).group(1, 2)
    if high is not None and float(high) < float(low):
        raise OpError("invalid_arguments", f"dose: {dose!r} is a range written from high to low; write it from the lower to the higher amount, e.g. '1-2 tablets'")
    number = dose.strip()[: m.start(1)].strip().replace(" ", "")
    return f"{number} {_UNIT_LOOKUP[m.group(1).lower()]}"


FREQUENCIES = ("once", "OD", "OM", "ON", "BD", "TDS", "QDS", "every N hours", "once weekly", "twice weekly", "three times weekly", "every N weeks", "over N hours")
_FREQUENCY_ALIASES = [
    (r"(stat|once|once only|single dose)", "once"),
    (r"(od|daily|once daily|once a day)", "OD"),
    (r"(om|mane|morning|in the morning)", "OM"),
    (r"(on|nocte|night|at night)", "ON"),
    (r"(bd|bid|twice daily|twice a day)", "BD"),
    (r"(tds|tid|three times daily|three times a day)", "TDS"),
    (r"(qds|qid|four times daily|four times a day)", "QDS"),
]


def _clean_frequency(frequency: str) -> str:
    """Normalise to the picklist an electronic prescribing system offers."""
    f = " ".join(frequency.strip().split())
    low = f.lower()
    for pattern, canonical in _FREQUENCY_ALIASES:
        if re.fullmatch(pattern, low):
            return canonical
    m = re.fullmatch(r"(?:every\s+)?(\d+(?:\s*-\s*\d+)?)\s*-?\s*(?:hourly|hours?|hrly)|q(\d+)h", low)
    if m:
        hours = (m.group(1) or m.group(2)).replace(" ", "")
        return f"every {hours} hours"
    weekly = _weekly(f)
    if weekly:
        return weekly
    m = re.fullmatch(r"over\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)", low)
    if m:
        unit = "hours" if m.group(2).startswith("h") else "minutes"
        return f"over {m.group(1)} {unit}"
    hint = " A course length goes in duration, not frequency." if re.search(r"\b(for|x)\s*\w+\s+(days?|weeks?)\b|\bweek\b|this week", low) else ""
    raise OpError("invalid_arguments", f"frequency: {frequency!r} is not recognised; use one of {', '.join(FREQUENCIES)}, with the day(s) for a weekly medicine.{hint}")


_DAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)days?|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)"
_DAYS = rf"{_DAY}(?:\s*(?:,|&|/|\band\b)\s*{_DAY})*"
_CLOCK = r"(?:(?:0?[1-9]|1[0-2])(?:[:.][0-5]\d)?\s*(?:am|pm|a\.m\.|p\.m\.)|(?:[01]?\d|2[0-3])[:.][0-5]\d)"  # 9am, 9:30 pm, 21.00
_TIME = (
    r"(?:(?:in\s+the\s+)?(?:morning|afternoon|evening)s?|(?:at\s+)?(?:night|bedtime|lunchtime|teatime|midday|(?:12\s+)?noon)"
    rf"|mane|nocte|am|pm|(?:at\s+)?{_CLOCK})"
)
# a note of when: the day(s), a time of day, or both, e.g. 'Mondays', 'takes on Mondays', 'Monday morning'
_WHEN = rf"(?:(?:(?:takes?|taken|given|give|to\s+be\s+(?:taken|given))\s+)?(?:on\s+)?(?:every\s+)?{_DAYS}(?:\s*,?\s*{_TIME})?|{_TIME})"
_TIMES_A_WEEK = {"once": 1, "1": 1, "twice": 2, "2": 2, "three": 3, "3": 3}
_WEEKLY = re.compile(
    rf"(?:(?:(?P<times>once|twice|three|[123])(?:\s*times)?\s+(?:weekly|a\s+week|per\s+week|every\s+week|each\s+week))|weekly|every\s+week|every\s+(?P<seven>7|seven)\s+days|every\s+(?P<weeks>[2-9]|1[0-2])\s+weeks)"
    rf"(?:\s*\(\s*(?P<a>{_WHEN})\s*\)|\s+(?:on\s+)?(?P<b>{_WHEN})|\s*[-,:]\s*(?P<c>{_WHEN}))?",
    re.IGNORECASE,
)


def _weekly(frequency: str) -> str | None:
    """'once weekly', 'twice weekly', 'three times weekly' or 'every N weeks', with when it is taken; None otherwise.

    A note of when may give the day for each dose and a time of day ('Monday morning', 'takes on Mondays').
    Anything else, or a number of days other than the doses a week, makes it not a weekly frequency, so
    'once weekly (daily)', 'once weekly (Mon-Fri)' and 'daily for a week' are refused."""
    m = _WEEKLY.fullmatch(frequency)
    if not m:
        return None
    times = 1 if m["seven"] or m["weeks"] or not m["times"] else _TIMES_A_WEEK[m["times"].lower()]
    when = m["a"] or m["b"] or m["c"]
    if when and len(re.findall(rf"\b{_DAY}\b", when, re.IGNORECASE)) not in (0, times):
        return None
    canonical = f"every {m['weeks']} weeks" if m["weeks"] else {1: "once weekly", 2: "twice weekly", 3: "three times weekly"}[times]
    return f"{canonical} ({when})" if when else canonical


def _is_once_weekly(frequency: str) -> bool:
    return (_weekly(" ".join(frequency.strip().split())) or "").startswith("once weekly")


_DOSE = re.compile(r"\s*(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*([A-Za-z]+)\s*")
_MG = {"mg": 1, "g": 1000, "gram": 1000, "grams": 1000, "microgram": 0.001, "micrograms": 0.001, "mcg": 0.001, "nanogram": 1e-6, "nanograms": 1e-6}
_ML = {"ml": 1, "l": 1000, "litre": 1000, "microlitre": 0.001}
_METERED = {"puff", "spray", "dose"}  # a measured dose from an inhaler or spray: dm+d's 'dose' or 'actuation'
_PER_DAY = [(r"\bqds\b|four times", 4), (r"\btds\b|three times", 3), (r"\bbd\b|twice", 2), (r"^(od|on|om)$|\b(od|on|om)\b|once daily|daily|mane|nocte", 1)]


def _singular(unit: str) -> str:
    return unit[:-3] + "y" if unit.endswith("ies") else unit[:-2] if unit.endswith(("ches", "shes")) else unit[:-1] if unit.endswith("s") else unit


def _dose_in_mg(dose: str, product: dict | None = None, drug: str | None = None) -> tuple[float | None, str]:
    """A dose in mg of `drug` (the top of a range), or None and the reason it cannot be worked out.

    `drug` is what the formulary's maximum is a dose of: the drug itself, or the salt it declares.

    A mass converts directly. Tablets, vials, sachets, puffs and other unit doses, and volumes of a liquid,
    convert through the product's dm+d strength and unit dose size. With a product, the strength must be of a
    single active ingredient stated as `drug`: a salt (naproxen sodium) is never converted to its base."""
    m = _DOSE.fullmatch(dose)
    if not m:
        return None, "it is not a number and a unit"
    amount, unit = max(float(m[1]), float(m[2] or m[1])), m[3].lower()
    if product is None:
        return (amount * _MG[unit], "") if unit in _MG else (None, f"a dose in {unit} needs a product with a stated strength")
    name = product.get("name", "the product")
    strengths = json.loads(product["strengths"]) if product.get("strengths") else []
    if len(strengths) != 1:
        return None, f"{name} has {'no stated strength' if not strengths else 'more than one active ingredient'}"
    s = strengths[0]
    basis = s.get("basis") or s.get("ingredient") or ""
    if drug is not None and basis.lower() != drug.lower():
        return None, f"the strength of {name} is stated as {basis}, not {drug}"
    if unit in _MG:
        return amount * _MG[unit], ""
    if s.get("unit") not in _MG or not s.get("value"):
        return None, f"the strength of {name} is not a mass"
    per_unit, per_value = s.get("per_unit"), s.get("per_value") or 1
    mg_per = s["value"] * _MG[s["unit"]] / per_value  # mg in one per_unit (one tablet, 1 ml, one dose)
    counted, form = _singular(unit), product.get("unit_dose_form")
    size, size_unit = s.get("unit_dose_size"), s.get("unit_dose_size_unit")
    if unit in _ML and per_unit in _ML:
        return amount * _ML[unit] / _ML[per_unit] * mg_per, ""
    if counted == form or (counted in _METERED and form in ("dose", "actuation")):  # a number of unit doses
        if per_unit is None or per_unit in ("dose", "actuation"):
            return amount * mg_per, ""
        if size and size_unit in _ML and per_unit in _ML:
            return amount * size * _ML[size_unit] / _ML[per_unit] * mg_per, ""
        if size and size_unit == per_unit:
            return amount * size * mg_per, ""
        return None, f"dm+d does not give how much one {form} of {name} holds"
    if counted in _METERED and per_unit in ("dose", "actuation"):
        return amount * mg_per, ""
    return None, f"a dose in {unit} does not match the form of {name} ({form or product.get('form') or 'unknown'})"


_PLURAL = {u: p for u in DOSE_UNITS for p in (f"{u}s", f"{u}es", f"{u[:-1]}ies") if p in DOSE_UNITS}


def _product_basis(product: dict | None) -> str | None:
    """What a single-ingredient product's strength is stated as (e.g. Codeine phosphate); None otherwise."""
    strengths = json.loads(product["strengths"]) if product and product.get("strengths") else []
    return (strengths[0].get("basis") or strengths[0].get("ingredient")) if len(strengths) == 1 else None


def dose_decimals(dose: str, product: dict | None = None, drug: dict | None = None) -> tuple[Decimal, Decimal, str, str | None] | None:
    """A dose as (low, high, unit, basis), the bottom and top of a range (equal for a single amount) as exact
    decimals, for comparing with a regimen. A mass is in mg and units in units, of the product's stated ingredient,
    else of the drug's declared basis or its name; a count of tablets, vials and so on is converted to mg of the
    product's stated ingredient through its strength where it can be; a volume is in mL with no basis; anything else
    stays as written ("tablets") with no basis. None if the dose is not a number and a unit."""
    m = _DOSE.fullmatch(dose or "")
    if not m:
        return None
    low, high, unit = Decimal(m[1]), Decimal(m[2] or m[1]), m[3].lower()
    stated = _product_basis(product)
    default = stated or (drug and (drug.get("max_dose_basis") or drug.get("name"))) or None
    if unit in _MG or unit in ("unit", "units"):
        factor = Decimal(str(_MG[unit])) if unit in _MG else Decimal(1)
        return low * factor, high * factor, "mg" if unit in _MG else "units", default
    mg = _dose_in_mg(f"1 {m[3]}", product, None)[0] if product and stated else None
    if mg is not None:
        factor = Decimal(repr(mg))
        return low * factor, high * factor, "mg", stated
    if unit in _ML:
        factor = Decimal(str(_ML[unit]))
        return low * factor, high * factor, "mL", None
    canonical = _UNIT_LOOKUP.get(unit, unit)
    return low, high, _PLURAL.get(canonical, canonical), None


def structured_dose(dose: str, product: dict | None = None, drug: dict | None = None) -> tuple[float, float, str, str | None] | None:
    """dose_decimals as stored: (low, high) as floats converted from the exact decimals, never rounded."""
    d = dose_decimals(dose, product, drug)
    return None if d is None else (float(d[0]), float(d[1]), d[2], d[3])


def plain_decimal(d: Decimal) -> str:
    """A decimal written out in full with no exponent and no trailing zeros: Decimal('5.0000001') -> '5.0000001'."""
    return format(d.normalize(), "f")


def latest_weight(world: World, patient_id: int, t: int) -> float | None:
    """The latest weight in kg charted by time `t` and not marked entered in error by then."""
    row = world.one(
        """SELECT value FROM observation WHERE patient_id = ? AND kind = 'WEIGHT' AND value > 0 AND taken_at <= ?
             AND (error_at IS NULL OR error_at > ?) ORDER BY taken_at DESC, id DESC LIMIT 1""",
        (patient_id, t, t),
    )
    return row["value"] if row else None


def prescription_facts(world: World, patient_id: int, dose: str, frequency: str, kind: str, product: dict | None, drug: dict | None, t: int) -> dict:
    """The structured columns of a prescription written at `t`: its dose, doses per day, and the weight then charted,
    frozen with it so that a later correction cannot change what it was judged against."""
    low, high, unit, basis = structured_dose(dose, product, drug) or (None, None, None, None)
    per_day = doses_per_day(frequency) if kind in ("regular", "tto") else None
    return {"dose_low": low, "dose_high": high, "dose_unit": unit, "dose_basis": basis, "doses_per_day": per_day, "weight_kg": latest_weight(world, patient_id, t)}


def dose_mg(dose: str, product: dict | None = None, drug: str | None = None) -> float | None:
    """A dose in milligrams (the top of a range), or None if it cannot be worked out; see _dose_in_mg."""
    return _dose_in_mg(dose, product, drug)[0]


def doses_per_day(frequency: str) -> float | None:
    if re.search(r"week", frequency, re.IGNORECASE):
        return None  # at most one dose in any 24 hours; the single-dose maximum applies
    hourly = re.search(r"every (\d+)", frequency) or re.search(r"(\d+)\s*-?\s*hourly", frequency, re.IGNORECASE)
    if hourly and int(hourly.group(1)):
        return 24 / int(hourly.group(1))
    for pattern, n in _PER_DAY:
        if re.search(pattern, frequency, re.IGNORECASE):
            return n
    return None


def _prescribing_alerts(world: World, patient_id: int, drug: dict, args, product: dict | None = None) -> list[tuple[str, str]]:
    """Overridable decision-support alerts as (code, message), most serious first."""
    alerts = []
    conflict = _allergy_conflict(world, patient_id, drug)
    if conflict:
        reaction = f" ({conflict['reaction']})" if conflict["reaction"] else ""
        alerts.append(("allergy_alert", f"ALLERGY ALERT: patient has a recorded allergy to {conflict['substance']}{reaction}."))

    classes = json.loads(drug["classes"])
    active = world.q(
        # discharge medicines are checked against each other, inpatient medicines against each other:
        # a TTO that continues an inpatient drug is not a duplicate
        """SELECT d.classes FROM medication m JOIN drug d ON d.name = m.drug
           WHERE m.patient_id = ? AND (m.kind = 'tto') = ? AND m.started_at <= ? AND (m.stopped_at IS NULL OR m.stopped_at > ?)""",
        (patient_id, args.kind == "tto", world.now, world.now),
    )
    active_classes = {c for row in active for c in json.loads(row["classes"])}
    problems = world.q("SELECT display FROM problem WHERE patient_id = ? AND resolved_date IS NULL", (patient_id,))
    for rule in world.q("SELECT * FROM prescribing_rule ORDER BY id"):
        targets = (rule["drug_regex"] and re.search(rule["drug_regex"], drug["name"], re.IGNORECASE)) or rule["drug_class"] in classes
        if not targets:
            continue
        value = _latest_value(world, patient_id, rule["analyte"]) if rule["analyte"] else None
        holds = (
            (rule["problem_regex"] and any(re.search(rule["problem_regex"], p["display"], re.IGNORECASE) for p in problems))
            or (value is not None and rule["below"] is not None and value < rule["below"])
            or (value is not None and rule["above"] is not None and value > rule["above"])
            or (rule["with_class"] and rule["with_class"] in active_classes)
        )
        if holds:
            alerts.append((f"{rule['kind']}_alert", f"{rule['kind'].upper()} ALERT: {rule['message']}"))

    has_maximum = drug["max_dose_mg"] is not None or drug["max_daily_mg"] is not None
    mg, why = _dose_in_mg(args.dose, product, drug["max_dose_basis"] or drug["name"]) if has_maximum else (None, "")
    if has_maximum and mg is None:
        alerts.append(("dose_unchecked_alert", f"DOSE CHECK ALERT: {args.dose} could not be checked against the usual maximum for {drug['name']}: {why}. Check the dose yourself."))
    if mg is not None:
        # a discharge prescription is taken as written; only PRN doses have no daily total to check
        per_day = doses_per_day(args.frequency) if args.kind in ("regular", "tto") else None
        shown = args.dose if dose_mg(args.dose) is not None and "-" not in args.dose else f"{args.dose} ({mg:g} mg)"
        if drug["max_dose_mg"] is not None and mg > drug["max_dose_mg"]:
            alerts.append(("dose_alert", f"DOSE ALERT: {shown} exceeds the usual maximum single dose of {drug['max_dose_mg']:g} mg."))
        elif drug["max_daily_mg"] is not None and per_day and mg * per_day > drug["max_daily_mg"]:
            alerts.append(("dose_alert", f"DOSE ALERT: {mg * per_day:g} mg in 24 hours exceeds the usual maximum of {drug['max_daily_mg']:g} mg."))
    return alerts


@op(
    "Prescribe a medication from the formulary for a patient. An allergy, dose, contraindication or "
    "interaction alert blocks the prescription unless a reason for overriding it is given.",
    writes=True,
)
def prescribe_medication(world: World, args: PrescribeArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    drug, product = _resolve_drug(world, args.drug.strip(), args.product.strip())
    # dm+d leaves routes blank for some products; fall back to the drug's, and to any route if neither says
    permitted = (json.loads(product["routes"]) if product else []) or json.loads(drug["routes"])
    route = _ROUTE_ALIASES.get(args.route.strip().upper()) or next((r for r in permitted if r.lower() == args.route.strip().lower()), None)
    if route is None:
        raise OpError("invalid_arguments", f"route: {args.route!r} is not a recognised route ({', '.join(ROUTES)})")
    if product and permitted and route not in permitted:
        raise OpError("route_not_permitted", f"{product['name']} cannot be given by the {route} route (permitted: {', '.join(permitted)})")
    _prescribing_safeguards(world, args.patient_id, drug, route, args, product)
    args.dose = _clean_dose(args.dose)
    args.frequency = _clean_frequency(args.frequency)
    alerts = _prescribing_alerts(world, args.patient_id, drug, args, product)
    override = args.override_reason.strip()
    if alerts and not override:
        text = " ".join(message for _, message in alerts)
        raise OpError(alerts[0][0], f"{text} Not prescribed. To prescribe anyway, give a reason for overriding the alert.")
    enc = _current_encounter(world, args.patient_id)
    med_id = world.insert(
        "medication",
        **by_agent(ctx),
        **prescription_facts(world, args.patient_id, args.dose, args.frequency, args.kind, product, drug, world.now),
        patient_id=args.patient_id,
        encounter_id=enc["id"] if enc else None,
        kind=args.kind,
        duration=args.duration or None,
        overridden_alert=",".join(dict.fromkeys(code for code, _ in alerts)) or None,
        override_reason=override if alerts else None,
        drug=drug["name"],
        product=product["name"] if product else None,
        dose=args.dose,
        route=route,
        frequency=args.frequency,
        indication=args.indication,
        prescriber=ctx.actor,
        started_at=world.now,
    )
    return {"medication_id": med_id, "patient_id": args.patient_id, "drug": drug["name"], "product": product["name"] if product else None, "dose": args.dose, "frequency": args.frequency}


class StopMedArgs(Args):
    medication_id: int
    reason: str = Field(min_length=1)
    patient_id: int | None = _for_patient("medication")


@op("Stop a prescribed medication.", writes=True)
def stop_medication(world: World, args: StopMedArgs, ctx: Ctx) -> dict:
    m = world.one("SELECT * FROM medication WHERE id = ?", (args.medication_id,))
    if m is None:
        raise OpError("not_found", f"no medication with id {args.medication_id}")
    if m["stopped_at"] is not None and m["stopped_at"] <= world.now:
        raise OpError("already_stopped", "this medication has already been stopped")
    world.conn.execute(
        "UPDATE medication SET stopped_at = ?, stop_reason = ? WHERE id = ?", (world.now, args.reason, m["id"])
    )
    return {"medication_id": m["id"], "patient_id": m["patient_id"], "drug": m["drug"], "stopped_at": iso(world.now)}


class AdministerArgs(Args):
    medication_id: int
    status: Literal["given", "omitted", "refused"] = "given"
    dose: str = Field("", description="The amount given, e.g. '5 mg' or '2 tablets'. Defaults to the prescribed dose; needed when that is a range.")
    note: str = ""
    patient_id: int | None = _for_patient("medication")


def admission_active(world: World, med: dict, t: int) -> bool:
    """Whether a prescription can be given at `t`: its admission (or, without one, any admission) is still open."""
    if med["encounter_id"] is not None:
        sql, ref = "SELECT 1 FROM encounter WHERE id = ? AND admitted_at <= ? AND (discharged_at IS NULL OR discharged_at > ?)", med["encounter_id"]
    else:
        sql, ref = "SELECT 1 FROM encounter WHERE patient_id = ? AND admitted_at <= ? AND (discharged_at IS NULL OR discharged_at > ?)", med["patient_id"]
    return world.one(sql, (ref, t, t)) is not None


def _med_product_and_drug(world: World, med: dict) -> tuple[dict | None, dict | None]:
    product = world.one("SELECT * FROM product WHERE name = ?", (med["product"],)) if med["product"] else None
    return product, world.one("SELECT * FROM drug WHERE name = ?", (med["drug"],))


def bottom_of_range(world: World, med: dict) -> str:
    """The bottom of a prescribed range, written exactly in the prescription's structured unit (e.g. '5.0000001 mg'),
    for a dose given by scripted staff; '' when the prescription is a single amount (give it as written)."""
    d = dose_decimals(med["dose"], *_med_product_and_drug(world, med))
    return "" if d is None or d[0] == d[1] else f"{plain_decimal(d[0])} {d[2]}"


def given_dose(world: World, med: dict, dose: str) -> dict:
    """The administration columns for a dose of `med` given: what was given, which must be a single amount within the
    prescription (the prescribed dose itself unless it is a range). Every number is parsed, never reformatted."""
    if not dose.strip() and med["dose_low"] is not None and med["dose_low"] != med["dose_high"]:
        raise OpError("dose_required", f"{med['dose']} is a range: give the dose that was given")
    text = _clean_dose(dose) if dose.strip() else med["dose"]
    low, high, unit, basis = structured_dose(text, *_med_product_and_drug(world, med)) or (None, None, None, None)
    if low != high:
        raise OpError("invalid_arguments", f"dose: {text!r} is a range; give the single amount that was given")
    if med["dose_low"] is not None and (unit != med["dose_unit"] or not med["dose_low"] <= low <= med["dose_high"]):
        raise OpError("dose_outside_prescription", f"{text} is not the prescribed dose ({med['dose']})")
    return {"dose": text, "dose_value": low, "dose_unit": unit, "dose_basis": basis}


@op("Record on the drug chart that a dose of a prescribed medication was given (or omitted, or refused) now.", writes=True)
def record_administration(world: World, args: AdministerArgs, ctx: Ctx) -> dict:
    m = world.one("SELECT * FROM medication WHERE id = ?", (args.medication_id,))
    if m is None:
        raise OpError("not_found", f"no medication with id {args.medication_id}")
    if m["stopped_at"] is not None and m["stopped_at"] <= world.now:
        raise OpError("already_stopped", "this medication has been stopped")
    if m["kind"] == "tto":
        raise OpError("not_administrable", "discharge (TTO) medications are dispensed, not given on the ward")
    if not admission_active(world, m, world.now):
        raise OpError("admission_ended", "the admission this prescription belongs to has ended; it can no longer be given")
    admin_id = world.insert(
        "administration",
        **by_agent(ctx),
        medication_id=m["id"],
        patient_id=m["patient_id"],
        at=world.now,
        status=args.status,
        recorded_by=ctx.actor,
        note=args.note or None,
        **(given_dose(world, m, args.dose) if args.status == "given" else {}),
    )
    return {"administration_id": admin_id, "patient_id": m["patient_id"], "drug": m["drug"], "status": args.status}


# --- fluid balance ----------------------------------------------------------


class FluidArgs(PatientArgs):
    direction: Literal["in", "out"]
    kind: str = Field(min_length=1, description="For example: oral, IV fluids, urine, drain, vomit.")
    volume_ml: int = Field(gt=0, le=10000)


@op("Record fluid intake or output on the patient's fluid balance chart.", writes=True)
def record_fluid(world: World, args: FluidArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    entry_id = world.insert(
        "fluid_entry",
        patient_id=args.patient_id,
        at=world.now,
        direction=args.direction,
        kind=args.kind,
        volume_ml=args.volume_ml,
        recorded_by=ctx.actor,
    )
    return {"fluid_entry_id": entry_id}


class FluidBalanceArgs(PatientArgs):
    hours: int = Field(24, ge=1, le=72, description="How far back to total.")


@op("Read the patient's fluid balance chart: each entry, and total intake, output and balance over a period.")
def get_fluid_balance(world: World, args: FluidBalanceArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT at, direction, kind, volume_ml, recorded_by FROM fluid_entry
           WHERE patient_id = ? AND at <= ? AND at > ? ORDER BY at DESC, id DESC""",
        (args.patient_id, world.now, world.now - args.hours * 3600),
    )
    total_in = sum(r["volume_ml"] for r in rows if r["direction"] == "in")
    total_out = sum(r["volume_ml"] for r in rows if r["direction"] == "out")
    return {
        "hours": args.hours,
        "total_in_ml": total_in,
        "total_out_ml": total_out,
        "balance_ml": total_in - total_out,
        "entries": [{**r, "at": iso(r["at"])} for r in rows],
    }


# --- jobs list --------------------------------------------------------------


JOB_TYPES = {"bloods": "Bloods", "chase_result": "Chase a result", "review": "Review the patient", "prescribing": "Prescribing", "fluids": "Fluids",
             "cannula": "Cannula", "imaging": "Imaging", "referral": "Referral", "discharge": "Discharge paperwork", "other": "Other"}  # fmt: skip


class ListJobsArgs(Args):
    include_done: bool = Field(False, description="Also list jobs that are closed: done or cancelled.")


@op("Read the ward jobs list handed over between shifts: open jobs, or with include_done every job with its status (open, done or cancelled).")
def list_jobs(world: World, args: ListJobsArgs, ctx: Ctx) -> dict:
    rows = world.q(
        """SELECT j.*, p.mrn, p.family_name, p.given_name, i.name AS order_name FROM job j JOIN patient p ON p.id = j.patient_id
             LEFT JOIN "order" o ON o.id = j.order_id LEFT JOIN orderable i ON i.code = o.orderable_code
           WHERE j.created_at <= ? AND (? OR ((j.done_at IS NULL OR j.done_at > ?) AND (j.cancelled_at IS NULL OR j.cancelled_at > ?)))
           ORDER BY (j.priority = 'urgent') DESC, j.created_at, j.id""",
        (world.now, args.include_done, world.now, world.now),
    )
    jobs = []
    for r in rows:
        enc = _current_encounter(world, r["patient_id"])
        done = r["done_at"] is not None and r["done_at"] <= world.now
        cancelled = r["cancelled_at"] is not None and r["cancelled_at"] <= world.now
        jobs.append(
            {
                "job_id": r["id"],
                "patient_id": r["patient_id"],
                "patient": f"{r['family_name'].upper()}, {r['given_name']}",
                "mrn": r["mrn"],
                "location": enc["location_name"] if enc else None,
                "bed": enc["bed"] if enc else None,
                "description": r["description"],
                "priority": r["priority"],
                "task_type": r["task_type"],
                "order_id": r["order_id"],
                "order_name": r["order_name"],
                "created_by": r["created_by"],
                "created_at": iso(r["created_at"]),
                "status": "done" if done else "cancelled" if cancelled else "open",
                "done": done,
                "done_by": r["done_by"] if done else None,
                "outcome": r["outcome"] if done else None,
                "cancelled_by": r["cancelled_by"] if cancelled else None,
                "cancel_reason": r["cancel_reason"] if cancelled else None,
            }
        )
    return {"jobs": jobs}


class AddJobArgs(PatientArgs):
    description: str = Field(min_length=1)
    priority: Literal["routine", "urgent"] = "routine"
    task_type: Literal[tuple(JOB_TYPES)] | None = Field(None, description=f"What kind of job it is: {', '.join(JOB_TYPES)}.")
    order_id: int | None = Field(None, description="The patient's request (from list_orders) the job concerns, if any: bloods to take, a result to chase.")


@op("Add a job for a patient to the ward jobs list, for yourself or the next shift, optionally saying its type and the request it concerns.", writes=True)
def add_job(world: World, args: AddJobArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    if args.order_id is not None:
        o = world.one('SELECT patient_id FROM "order" WHERE id = ? AND ordered_at <= ?', (args.order_id, world.now))
        if o is None:
            raise OpError("not_found", f"no request with id {args.order_id}")
        if o["patient_id"] != args.patient_id:
            raise OpError("wrong_patient", f"request {args.order_id} is not on this patient's record")
    job_id = world.insert(
        "job", **by_agent(ctx), patient_id=args.patient_id, description=args.description, priority=args.priority, created_by=ctx.actor,
        created_at=world.now, task_type=args.task_type, order_id=args.order_id,
    )  # fmt: skip
    return {"job_id": job_id}


class CompleteJobArgs(Args):
    job_id: int
    outcome: str = Field("", description="What was done, for whoever reads the list next.")
    patient_id: int | None = _for_patient("job")


def _close_job(world: World, j: dict, status: str, note: str, ctx: Ctx) -> None:
    if j["status"] != "open":
        raise OpError("already_closed", f"this job has already been {'marked as done' if j['status'] == 'done' else 'cancelled'}")
    world.insert("job_event", job_id=j["id"], patient_id=j["patient_id"], at=world.now, status=status, by=ctx.actor, note=note or None, **by_agent(ctx))


@op("Mark a job on the jobs list as done.", writes=True)
def complete_job(world: World, args: CompleteJobArgs, ctx: Ctx) -> dict:
    j = world.one("SELECT * FROM job WHERE id = ? AND created_at <= ?", (args.job_id, world.now))
    if j is None:
        raise OpError("not_found", f"no job with id {args.job_id}")
    if j["status"] == "done":
        raise OpError("already_done", "this job has already been marked as done")
    _close_job(world, j, "done", args.outcome, ctx)
    world.conn.execute(
        "UPDATE job SET status = 'done', done_by = ?, done_at = ?, outcome = ? WHERE id = ?", (ctx.actor, world.now, args.outcome or None, j["id"])
    )
    return {"job_id": j["id"], "patient_id": j["patient_id"], "status": "done"}


class CancelJobArgs(Args):
    job_id: int
    reason: str = Field(min_length=1, description="Why it is no longer to be done, for whoever reads the list next.")
    patient_id: int | None = _for_patient("job")


@op("Cancel a job on the jobs list that is no longer to be done. It stays on the list as cancelled, with the reason.", writes=True)
def cancel_job(world: World, args: CancelJobArgs, ctx: Ctx) -> dict:
    j = world.one("SELECT * FROM job WHERE id = ? AND created_at <= ?", (args.job_id, world.now))
    if j is None:
        raise OpError("not_found", f"no job with id {args.job_id}")
    _close_job(world, j, "cancelled", args.reason, ctx)
    world.conn.execute(
        "UPDATE job SET status = 'cancelled', cancelled_by = ?, cancelled_at = ?, cancel_reason = ? WHERE id = ?", (ctx.actor, world.now, args.reason, j["id"])
    )
    return {"job_id": j["id"], "patient_id": j["patient_id"], "status": "cancelled"}


# --- blood products ---------------------------------------------------------

SAMPLE_VALIDITY_HOURS = 72


class BloodRequestArgs(PatientArgs):
    product: Literal["red cells", "platelets", "fresh frozen plasma"]
    units: int = Field(ge=1, le=6)
    urgency: Literal["routine", "urgent"] = "routine"
    indication: str = Field(min_length=1, description="Why the transfusion is needed, e.g. 'Hb 68, symptomatic'.")


@op(
    "Request blood products from the transfusion laboratory. The patient must have a reported group and save "
    "sample from the last 72 hours.",
    writes=True,
)
def request_blood_product(world: World, args: BloodRequestArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    valid = world.one(
        "SELECT 1 FROM result WHERE patient_id = ? AND orderable_code = 'GS' AND reported_at <= ? AND performed_at > ?",
        (args.patient_id, world.now, world.now - SAMPLE_VALIDITY_HOURS * 3600),
    )
    if not valid:
        raise OpError(
            "no_valid_sample",
            f"no reported group and save sample in the last {SAMPLE_VALIDITY_HOURS} hours; request a group and save and wait for the result",
        )
    request_id = world.insert(
        "blood_request",
        patient_id=args.patient_id,
        product=args.product,
        units=args.units,
        urgency=args.urgency,
        indication=args.indication,
        requested_by=ctx.actor,
        requested_at=world.now,
    )
    return {"blood_request_id": request_id}


@op("List the blood product requests made for a patient, newest first.")
def list_blood_requests(world: World, args: PatientArgs, ctx: Ctx) -> dict:
    _patient(world, args.patient_id)
    rows = world.q(
        """SELECT id AS blood_request_id, product, units, urgency, indication, requested_by, requested_at FROM blood_request
           WHERE patient_id = ? AND requested_at <= ? ORDER BY requested_at DESC, id DESC""",
        (args.patient_id, world.now),
    )
    return {"blood_requests": [{**r, "requested_at": iso(r["requested_at"])} for r in rows]}
