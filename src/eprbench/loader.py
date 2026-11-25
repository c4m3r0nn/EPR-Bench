"""Load a patient bundle (JSON) into a world.

The bundle format is the contract between data generation and the EPR: any
generator, hand-written or LLM-driven, only has to emit a document that
validates against `Bundle`. Timestamps are ISO 8601 strings.
"""

import hashlib
import json
import re
from typing import Literal
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eprbench.identifiers import is_valid_nhs_number, normalise_nhs_number
from eprbench.ops import OpError, doses_per_day, given_dose, insert_result, prescription_facts
from eprbench.world import World, parse_iso


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")



class Location(Model):
    code: str
    name: str
    kind: str
    live_monitoring: bool = False


class Allergy(Model):
    substance: str
    reaction: str | None = None
    severity: str | None = None


class Problem(Model):
    display: str
    code_system: str | None = None
    code: str | None = None
    onset_date: str | None = None
    resolved_date: str | None = None
    encounter: str | None = None


class Encounter(Model):
    ref: str  # bundle-local name other records use to point at this encounter
    kind: str
    location: str | None = None
    bed: str | None = None
    specialty: str | None = None
    consultant: str | None = None
    presenting_complaint: str | None = None
    admitted_at: str
    discharged_at: str | None = None


class Note(Model):
    encounter: str | None = None
    kind: str
    title: str
    author: str
    author_role: str | None = None
    written_at: str
    body: str
    status: Literal["draft", "final"] = "final"


class EnteredInError(Model):
    at: str
    by: str
    reason: str


class ObservationSet(Model):
    encounter: str | None = None
    taken_at: str
    recorded_by: str | None = None
    values: dict[str, float | str]
    entered_in_error: EnteredInError | None = None  # kept on the chart, struck through, left out of NEWS2


class ResultItem(Model):
    analyte: str
    value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    ref_low: float | None = None
    ref_high: float | None = None


class ResultPayload(Model):
    report_text: str | None = None
    reported_by: str | None = None
    items: list[ResultItem] = []


class Orderable(Model):
    code: str
    name: str
    category: str
    cost_pence: int
    tat_seconds: int
    contrast: bool = False
    modality: str | None = None
    region: str | None = None
    lateral: bool = False
    point_of_care: bool = False  # analysed at the bedside, e.g. a blood gas: its results are marked point of care
    normal: ResultPayload | None = None  # the catalogue normal
    # Outside every result window for a patient: "last_value" reports each analyte at the patient's latest value
    # (the catalogue normal where there is none); "normal" reports the catalogue normal.
    fallback: Literal["last_value", "normal"] = "last_value"
    fallback_max_age_days: float = Field(90, gt=0)  # last_value carries a value forward at most this long (then the normal)
    min_repeat_seconds: int | None = Field(None, gt=0)  # requests closer together than this are duplicates (safety)


ResultSource = Literal["hospital_lab", "point_of_care", "gp", "other_hospital"]


class Result(ResultPayload):
    encounter: str | None = None
    orderable_code: str
    performed_at: str  # when the sample was taken
    reported_at: str
    source: ResultSource = "hospital_lab"  # provenance: GP and other-hospital results usually have no encounter


class ResultBranch(ResultPayload):
    """What the window shows for a request whose structured fields match: each field (a request-form field such as
    laterality, or priority) has the value given, or one of a list of values."""

    request: dict[str, str | list[str]] = Field(min_length=1)


class Branching(ResultPayload):
    """A window's payload, optionally depending on what was asked for. The first branch whose request matches gives
    the result; a request no branch matches gets the window's own payload when it has one (report_text or items),
    otherwise the test's fallback as if there were no window (for imaging, the catalogue normal for that study)."""

    branches: list[ResultBranch] = []

    def payload(self) -> dict:
        out = self.model_dump(include={"report_text", "reported_by", "items"})
        return out | ({"branches": [b.model_dump() for b in self.branches]} if self.branches else {})


class LatentResult(Branching):
    """What a test shows for any sample taken in [valid_from, valid_to); unbounded where not given. Every request
    in the window gets the same values; where windows overlap the one that starts latest wins."""

    orderable_code: str
    valid_from: str | None = None
    valid_to: str | None = None


class RuleWindow(Branching):
    """A result window a rule opens, relative to when its effects take place; open-ended without a duration."""

    orderable_code: str
    offset_s: int = 0
    duration_s: int | None = None


class Administration(Model):
    at: str
    status: str = "given"
    recorded_by: str
    note: str | None = None
    dose: str | None = None  # the amount given; defaults to the prescribed dose


class Medication(Model):
    encounter: str | None = None
    drug: str
    dose: str
    route: str
    frequency: str
    indication: str | None = None
    prescriber: str
    started_at: str
    stopped_at: str | None = None
    stop_reason: str | None = None
    kind: str = "regular"
    duration: str | None = None
    administrations: list[Administration] = []


class Product(Model):
    name: str
    form: str | None = None
    routes: list[str] = []
    unit_dose_form: str | None = None
    controlled_drug: str | None = None
    vpid: str | None = None
    strengths: list[dict] = []
    flags: list[str] = []


class ProductFlag(Model):
    """Safety flags for some of a drug's products, e.g. strong potassium or high-strength midazolam."""

    pattern: str  # regex matched against the product name (case-insensitive)
    min_mg_per_ml: float | None = None  # and, if set, only products at least this concentrated
    flags: list[str]


def mg_per_ml(strengths: list[dict]) -> float | None:
    """Concentration of a single-ingredient liquid in mg/mL, from dm+d strength fields."""
    if len(strengths) != 1:
        return None
    s = strengths[0]
    scale = {"gram": 1000, "mg": 1, "microgram": 0.001}.get(s.get("unit"))
    per = {"ml": 1, "litre": 1000}.get(s.get("per_unit"))
    if scale is None or per is None or not s.get("value") or not s.get("per_value"):
        return None
    return s["value"] * scale / (s["per_value"] * per)


class Drug(Model):
    name: str
    products: list[Product] = []  # forms and strengths; when present, prescriptions must name one
    product_flags: list[ProductFlag] = []  # safety flags applied to matching products, including catalogue ones
    classes: list[str] = []
    routes: list[str] = []  # permitted routes; empty means any
    flags: list[str] = []  # insulin | weekly | critical_care_only | critical
    max_dose_mg: float | None = None
    max_daily_mg: float | None = None
    max_dose_basis: str | None = None  # what the maxima are doses of when not the drug itself, e.g. "Codeine phosphate"


class PrescribingRule(Model):
    """An overridable alert: fires when a targeted drug is prescribed and the condition holds."""

    name: str
    kind: Literal["contraindication", "interaction"]
    drug_regex: str | None = None
    drug_class: str | None = None
    problem_regex: str | None = None
    analyte: str | None = None
    below: float | None = None
    above: float | None = None
    with_class: str | None = None
    message: str


class RuleNote(Model):
    offset_s: int = 0
    kind: str
    title: str
    author: str
    author_role: str | None = None
    body: str


class RuleObservations(Model):
    offset_s: int = 0
    recorded_by: str | None = None
    values: dict[str, float | str]


class ReferralUpdate(Model):
    """The reply to the referral that triggered the rule, shown on it from when the rule takes effect."""

    status: Literal["accepted", "advice", "declined"]
    by: str
    role: str | None = None
    text: str


class Administer(Model):
    """A nurse gives the prescription that triggered the rule (unless stopped, a TTO, or already given)."""

    recorded_by: str
    note: str | None = None


class RuleMedication(Model):
    """A prescription written by a responding member of staff."""

    drug: str
    dose: str
    route: str
    frequency: str  # canonical, as the prescribing picklist writes it: OD, BD, 'every 4 hours', ...
    kind: Literal["regular", "prn", "stat"] = "regular"
    product: str | None = None  # the product, whose stated strength says what a mass is of (e.g. Enoxaparin sodium)
    indication: str | None = None
    prescriber: str


class RuleEffects(Model):
    vitals: dict[str, list[tuple[int, float]]] = {}  # signal -> [[seconds after taking effect, value], ...]
    result_windows: list[RuleWindow] = []  # replace what the named tests show from now on
    latent_results: list[LatentResult] = []  # older form of result_windows: open-ended from now
    notes: list[RuleNote] = []
    observations: list[RuleObservations] = []
    referral_update: ReferralUpdate | None = None
    administer: Administer | None = None
    medications: list[RuleMedication] = []


class Range(Model):
    """A numeric condition on an event field, e.g. {"min": 2} on an assessment's score (bounds included)."""

    min: float | None = None
    max: float | None = None


Wanted = str | list[str]
Conditions = dict[str, Wanted | Range | dict[str, Wanted]]


def _typed_conditions(when):
    """"fields" and "record" hold named conditions; any other mapping is a numeric range."""
    if not isinstance(when, dict):
        return when
    out = {}
    for key, want in when.items():
        if isinstance(want, dict):
            if key in ("fields", "record"):
                if not all(isinstance(v, str | list) for v in want.values()):
                    raise ValueError(f"when.{key}: each takes a value or a list of values")
                if key == "record" and set(want) - {"result", "no_result"}:
                    raise ValueError("when.record: the predicates are result and no_result (orderable codes)")
            else:
                want = Range.model_validate(want)
        out[key] = want
    return out


class RuleVariant(Model):
    """One version of a rule's effects, chosen when the rule takes effect: the first variant whose conditions hold
    then (on the triggering event's structured fields and on the record), else the rule's own effects."""

    when: Conditions = Field(min_length=1)
    effects: RuleEffects

    @field_validator("when", mode="before")
    @classmethod
    def _conditions(cls, when):
        return _typed_conditions(when)


class Rule(Model):
    """A clinical outcome fires once: on a matching successful write (on_op), or when the clock reaches `at`. A staff
    response (`repeat`) responds to every matching write, e.g. a nurse giving each new prescription. Effects take
    place `delay_s` after the event, e.g. a team's response time to a referral.

    Gating: a rule is active only while an encounter is open for the patient, at the event's time or at `at`:
    `encounter` "current" (the default: any), an encounter's ref, or null (always, e.g. a GP letter). An on_op rule
    responds only to events in [not_before, not_after]. A rule does not fire once any rule named in `unless` has
    taken effect (a timed rule is then skipped for good). A delayed effect is gated again when it falls due: suppressed
    (and recorded as suppressed) if an `unless` rule has taken effect by then, or if the patient has left the
    encounter, unless `after_discharge` is "deliver" (a reply that may arrive after discharge, such as a letter).

    `variants` let the effects depend on what was sent and on the record when the rule takes effect, e.g. a team
    that says it reviewed the ECG only when an ECG result is on the record ({"record": {"result": "ECG"}})."""

    name: str
    repeat: bool = False
    on_op: str | None = None
    match: dict[str, str] = {}  # field of the operation's arguments or result -> regex (case-insensitive); older bundles
    # Structured conditions on the event: {"regimen": name} (the prescription meets a regimen), {"service": name} (the
    # referral reached a service), {"pathway": name} (the referral pathway chosen; "" for a general referral),
    # {"fields": {field: value}} (the request form, pathway form or assessment form fields), {"record": {"result":
    # code}} or {"no_result": code} (a reported result of that test is, or is not, on the patient's record), any event
    # field's value or list of values (e.g. {"urgency": ["urgent", "emergency"]}), or a numeric range ({"score": {"min": 2}}).
    when: Conditions = {}
    not_before: str | None = None
    not_after: str | None = None
    at: str | None = None
    unless: str | list[str] | None = None
    encounter: str | None = "current"
    after_discharge: Literal["suppress", "deliver"] = "suppress"
    delay_s: int = Field(0, ge=0)
    placeholder: bool = False  # its timing or content awaits clinical review
    effects: RuleEffects
    variants: list[RuleVariant] = []

    @field_validator("when", mode="before")
    @classmethod
    def _conditions(cls, when):
        return _typed_conditions(when)

    @model_validator(mode="after")
    def _one_trigger(self):
        if (self.on_op is None) == (self.at is None):
            raise ValueError("a rule needs exactly one of on_op or at")
        if self.at is not None and (self.when or self.match or self.delay_s or self.repeat or self.not_before):
            raise ValueError("a timed rule takes no match, when, delay_s, repeat or not_before")
        return self

    @property
    def unless_names(self) -> list[str]:
        return [self.unless] if isinstance(self.unless, str) else list(self.unless or [])


class Service(Model):
    """A service a referral can reach: its name, or any alias, as whole words in the referral's destination."""

    name: str
    aliases: list[str] = []
    placeholder: bool = False


class Regimen(Model):
    """One way of meeting a named regimen: limits per dose, in `unit` (mg for any mass) of `basis` (the drug unless a
    salt is named, as products state it, e.g. Enoxaparin sodium), optionally per kg of the weight charted when
    prescribed. A range must lie wholly within the limits. `frequencies` are matched by doses per day (BD also covers
    every 12 hours); empty means any, as for routes. `kinds` defaults to regular and stat: never PRN or TTO."""

    name: str
    drug: str
    basis: str | None = None
    unit: str = "mg"
    min_dose: float | None = None
    max_dose: float | None = None
    min_per_kg: float | None = None
    max_per_kg: float | None = None
    frequencies: list[str] = []
    routes: list[str] = []
    kinds: list[Literal["regular", "stat", "prn", "tto"]] = ["regular", "stat"]
    placeholder: bool = False  # the limits await clinical review


class FormOption(Model):
    value: str
    label: str
    points: float = 0


class FormField(Model):
    """A field of a pathway or assessment form. tick: a tick box, 'yes' when ticked and 'no' otherwise, worth
    `points` when ticked; yes_no and choice: one of the options (yes_no's are Yes and No, 'yes' worth `points`);
    number, text, date (YYYY-MM-DD) and datetime (YYYY-MM-DDTHH:MM, UTC). Every field but a tick is required unless
    it says otherwise."""

    name: str
    label: str
    kind: Literal["tick", "yes_no", "choice", "number", "text", "date", "datetime"]
    options: list[FormOption] = []
    points: float = 0
    required: bool = True


class Pathway(Model):
    """A named referral pathway, chosen from a picklist rather than inferred from text: e.g. Primary PCI activation
    as against a general referral to Cardiology. It reaches `service` (a declared service), may set its own urgency
    (e.g. emergency, 2WW), and has its own structured form."""

    name: str
    label: str
    service: str | None = None
    urgency: str | None = None
    description: str | None = None
    fields: list[FormField] = []
    placeholder: bool = False  # its fields or wording await clinical review


class ScoreBand(Model):
    """A category for a total in [min, max] (bounds included; either may be open)."""

    value: str
    label: str
    min: float | None = None
    max: float | None = None


class AssessmentForm(Model):
    """A structured assessment: typed fields whose points add up to a score, and bands that name its category."""

    name: str
    label: str
    description: str | None = None
    fields: list[FormField] = Field(min_length=1)
    bands: list[ScoreBand] = []
    placeholder: bool = False  # its criteria, points or bands await clinical review


class FluidEntry(Model):
    at: str
    direction: Literal["in", "out"]
    kind: str
    volume_ml: int
    recorded_by: str | None = None


class Job(Model):
    description: str
    priority: Literal["routine", "urgent"] = "routine"
    created_by: str
    created_at: str
    task_type: str | None = None  # one of ops.JOB_TYPES


class VitalsTrack(Model):
    signal: str
    seed: int
    sd: float
    lo: float | None = None
    hi: float | None = None
    decimals: int = 0
    keyframes: list[tuple[str, float]]


class Patient(Model):
    mrn: str
    nhs_number: str
    family_name: str
    given_name: str
    dob: str
    sex: str
    address: str | None = None
    gp_practice: str | None = None
    spo2_scale: int = 1
    allergies: list[Allergy] = []
    problems: list[Problem] = []
    encounters: list[Encounter] = []
    notes: list[Note] = []
    observations: list[ObservationSet] = []
    results: list[Result] = []
    medications: list[Medication] = []
    result_windows: list[LatentResult] = []  # what tests requested from now on would show
    latent_results: list[LatentResult] = []  # older name for result_windows, loaded the same way
    vitals_tracks: list[VitalsTrack] = []
    rules: list[Rule] = []
    fluids: list[FluidEntry] = []
    jobs: list[Job] = []

    @field_validator("nhs_number")
    @classmethod
    def _valid_nhs_number(cls, v: str) -> str:
        if not is_valid_nhs_number(v):
            raise ValueError(f"{v!r} is not a valid NHS number")
        return normalise_nhs_number(v)


class Bundle(Model):
    source: str | None = None  # attribution, e.g. for catalogues built from dm+d
    catalogues: list[str] = []  # packaged catalogues to include, e.g. "imaging" (data/catalogues/imaging.json)
    orderables: list[Orderable] = []
    locations: list[Location] = []
    formulary: list[Drug] = []
    prescribing_rules: list[PrescribingRule] = []
    services: list[Service] = []
    regimens: list[Regimen] = []
    pathways: list[Pathway] = []
    assessment_forms: list[AssessmentForm] = []  # catalogues may carry them too (e.g. "forms")
    phlebotomy_rounds: list[str] = []  # times of the routine phlebotomy round, HH:MM (UTC); none: no round to book onto
    turnaround_overrides: dict[str, int] = {}  # orderable code -> sample-to-report seconds, replacing the catalogue's
    repeat_interval_overrides: dict[str, int] = {}  # orderable code -> minimum seconds between requests before a repeat is a duplicate
    observation_bounds: dict[str, tuple[float, float]] = {}  # kind -> (lowest, highest) accepted, replacing ops.OBSERVATION_BOUNDS
    patients: list[Patient] = []


def _t(s: str | None) -> int | None:
    return parse_iso(s) if s else None


def load_bundle(world: World, bundle: Bundle) -> None:
    w = world
    w.conn.execute("BEGIN")
    for o in catalogue_orderables(bundle):
        normal = o.normal.model_dump_json() if o.normal else None
        tat = bundle.turnaround_overrides.get(o.code, o.tat_seconds)
        repeat = bundle.repeat_interval_overrides.get(o.code, o.min_repeat_seconds)
        w.insert("orderable", **o.model_dump(exclude={"normal", "tat_seconds", "min_repeat_seconds"}), tat_seconds=tat, min_repeat_seconds=repeat, normal=normal)
    w.insert("meta", key="phlebotomy_rounds", value=json.dumps(sorted(bundle.phlebotomy_rounds)))
    w.insert("meta", key="observation_bounds", value=json.dumps(bundle.observation_bounds, sort_keys=True))
    for pw in bundle.pathways:
        w.insert("pathway", name=pw.name, label=pw.label, service=pw.service, urgency=pw.urgency, description=pw.description,
                 fields=json.dumps([f.model_dump() for f in pw.fields]))  # fmt: skip
    for af in catalogue_forms(bundle):
        w.insert("assessment_form", name=af.name, label=af.label, description=af.description,
                 fields=json.dumps([f.model_dump() for f in af.fields]),
                 bands=json.dumps([b.model_dump(exclude_none=True) for b in af.bands]))  # fmt: skip
    for loc in bundle.locations:
        w.insert("location", **loc.model_dump())
    for d in catalogue_formulary(bundle):
        w.conn.execute(
            """INSERT INTO drug (name, classes, routes, flags, max_dose_mg, max_daily_mg, max_dose_basis) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET classes = excluded.classes, flags = excluded.flags,
                 routes = CASE WHEN excluded.routes = '[]' THEN drug.routes ELSE excluded.routes END,
                 max_dose_mg = excluded.max_dose_mg, max_daily_mg = excluded.max_daily_mg, max_dose_basis = excluded.max_dose_basis""",
            (d.name, json.dumps(d.classes), json.dumps(d.routes), json.dumps(d.flags), d.max_dose_mg, d.max_daily_mg, d.max_dose_basis),
        )
        w.conn.executemany(
            """INSERT OR IGNORE INTO product (name, drug, form, routes, unit_dose_form, controlled_drug, vpid, strengths, flags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (p.name, d.name, p.form, json.dumps(p.routes), p.unit_dose_form, p.controlled_drug, p.vpid, json.dumps(p.strengths), json.dumps(p.flags))
                for p in d.products
            ],
        )
        for rule in d.product_flags:
            pattern = re.compile(rule.pattern, re.IGNORECASE)
            for prod in w.q("SELECT name, strengths, flags FROM product WHERE drug = ?", (d.name,)):
                if not pattern.search(prod["name"]):
                    continue
                if rule.min_mg_per_ml is not None and (mg_per_ml(json.loads(prod["strengths"])) or 0) < rule.min_mg_per_ml:
                    continue
                flags = sorted(set(json.loads(prod["flags"])) | set(rule.flags))
                w.conn.execute("UPDATE product SET flags = ? WHERE name = ?", (json.dumps(flags), prod["name"]))
    for r in bundle.prescribing_rules:
        w.insert("prescribing_rule", **r.model_dump())
    for s in bundle.services:
        w.insert("service", name=s.name, aliases=json.dumps(s.aliases))
    for g in bundle.regimens:
        per_day = sorted({doses_per_day(f) for f in g.frequencies} - {None})
        w.insert(
            "regimen",
            **g.model_dump(exclude={"frequencies", "routes", "kinds", "placeholder", "basis"}), basis=g.basis or g.drug,
            per_day=json.dumps(per_day), routes=json.dumps(g.routes), kinds=json.dumps(g.kinds),
        )  # fmt: skip
    locations = {r["code"]: r["id"] for r in w.q("SELECT id, code FROM location")}

    for p in bundle.patients:
        pid = w.insert(
            "patient",
            **p.model_dump(include={"mrn", "nhs_number", "family_name", "given_name", "dob", "sex", "address", "gp_practice", "spo2_scale"}),
        )
        enc_ids: dict[str | None, int | None] = {None: None}
        for e in p.encounters:
            enc_ids[e.ref] = w.insert(
                "encounter",
                patient_id=pid,
                kind=e.kind,
                location_id=locations[e.location] if e.location else None,
                bed=e.bed,
                specialty=e.specialty,
                consultant=e.consultant,
                presenting_complaint=e.presenting_complaint,
                admitted_at=_t(e.admitted_at),
                discharged_at=_t(e.discharged_at),
                ref=e.ref,
            )
        for a in p.allergies:
            w.insert("allergy", patient_id=pid, **a.model_dump())
        for pr in p.problems:
            w.insert("problem", patient_id=pid, encounter_id=enc_ids[pr.encounter], **pr.model_dump(exclude={"encounter"}))
        for n in p.notes:
            w.insert(
                "note",
                patient_id=pid,
                encounter_id=enc_ids[n.encounter],
                written_at=_t(n.written_at),
                **n.model_dump(exclude={"encounter", "written_at"}),
            )
        for s in p.observations:
            error = s.entered_in_error
            for kind, value in s.values.items():
                numeric = not isinstance(value, str)
                w.insert(
                    "observation",
                    patient_id=pid,
                    encounter_id=enc_ids[s.encounter],
                    taken_at=_t(s.taken_at),
                    kind=kind,
                    value=value if numeric else None,
                    value_text=None if numeric else value,
                    recorded_by=s.recorded_by,
                    error_at=_t(error.at) if error else None,
                    error_by=error.by if error else None,
                    error_reason=error.reason if error else None,
                    error_origin="record" if error else None,
                )
        for r in p.results:
            insert_result(
                w,
                patient_id=pid,
                encounter_id=enc_ids[r.encounter],
                order_id=None,
                code=r.orderable_code,
                performed_at=_t(r.performed_at),
                reported_at=_t(r.reported_at),
                payload=r.model_dump(include={"report_text", "reported_by", "items"}),
                source=r.source,
            )
        for m in p.medications:
            drug = w.one("SELECT * FROM drug WHERE name = ?", (m.drug,))
            med_id = w.insert(
                "medication",
                patient_id=pid,
                encounter_id=enc_ids[m.encounter],
                started_at=_t(m.started_at),
                stopped_at=_t(m.stopped_at),
                **prescription_facts(w, pid, m.dose, m.frequency, m.kind, None, drug, _t(m.started_at)),
                **m.model_dump(exclude={"encounter", "started_at", "stopped_at", "administrations"}),
            )
            med = w.one("SELECT * FROM medication WHERE id = ?", (med_id,))
            for a in m.administrations:
                try:
                    dose = given_dose(w, med, a.dose or "") if a.status == "given" else {"dose": a.dose}
                except OpError:  # a range charted without the amount given: kept as written
                    dose = {"dose": a.dose}
                w.insert("administration", medication_id=med_id, patient_id=pid, at=_t(a.at), **a.model_dump(exclude={"at", "dose"}), **dose)
        for lr in p.result_windows + p.latent_results:
            w.insert(
                "latent_result",
                patient_id=pid,
                orderable_code=lr.orderable_code,
                valid_from=_t(lr.valid_from),
                valid_to=_t(lr.valid_to),
                payload=json.dumps(lr.payload()),
            )
        for v in p.vitals_tracks:
            w.insert(
                "vitals_track",
                patient_id=pid,
                keyframes=json.dumps(sorted([parse_iso(t), value] for t, value in v.keyframes)),
                **v.model_dump(exclude={"keyframes"}),
            )
        for f in p.fluids:
            w.insert("fluid_entry", patient_id=pid, at=_t(f.at), **f.model_dump(exclude={"at"}))
        for j in p.jobs:
            w.insert("job", patient_id=pid, created_at=_t(j.created_at), **j.model_dump(exclude={"created_at"}))
        for r in p.rules:
            w.insert(
                "scenario_rule",
                patient_id=pid,
                name=r.name,
                on_op=r.on_op,
                match=json.dumps(r.match),
                condition=json.dumps(r.model_dump(mode="json", include={"when"}, exclude_none=True)["when"]),
                not_before=_t(r.not_before),
                not_after=_t(r.not_after),
                at=_t(r.at),
                unless=json.dumps(r.unless_names) if r.unless_names else None,
                encounter=r.encounter,
                after_discharge=r.after_discharge,
                variants=json.dumps([v.model_dump(mode="json", exclude_none=True) for v in r.variants]) if r.variants else None,
                delay_s=r.delay_s,
                repeat=int(r.repeat),
                effects=r.effects.model_dump_json(),
            )
    w.conn.execute("COMMIT")


def catalogue_orderables(bundle: Bundle) -> list[Orderable]:
    """Every orderable a bundle makes available: its packaged catalogues, then its own."""
    items = []
    for name in bundle.catalogues:
        items += _catalogue(name).orderables
    return items + bundle.orderables


def file_sha256(path) -> str:
    """SHA-256 of a file's bytes, read now."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def data_file(kind: str, name: str):
    """A packaged data file: kind is "bundles", "catalogues" or "tasks"."""
    return resources.files("eprbench").joinpath("data", kind, f"{name}.json")


# Source files are read and hashed once per process: the bytes and their SHA-256 are kept together, and every world
# is built from those very bytes (never from a re-read), so the hashes recorded as provenance are always the hashes of
# what was used. An edited file is therefore not picked up until `invalidate()` (tasks.invalidate_caches()).
_SOURCES: dict[tuple[str, str], tuple[bytes, str]] = {}
_PARSED: dict[str, "Bundle"] = {}  # catalogues parsed, by the SHA-256 of their bytes
_CATALOGUE_NAMES: dict[str, tuple[str, ...]] = {}  # the catalogues a bundle includes, by the bundle's SHA-256


def invalidate() -> None:
    """Forget every source file read so far, so the next build reads them again."""
    _SOURCES.clear()
    _PARSED.clear()
    _CATALOGUE_NAMES.clear()


def source(kind: str, name: str) -> tuple[bytes, str]:
    """A packaged data file's bytes and their SHA-256, read once per process."""
    if (kind, name) not in _SOURCES:
        data = data_file(kind, name).read_bytes()
        _SOURCES[kind, name] = (data, hashlib.sha256(data).hexdigest())
    return _SOURCES[kind, name]


def _catalogue(name: str) -> "Bundle":
    data, sha = source("catalogues", name)
    if sha not in _PARSED:
        _PARSED[sha] = Bundle.model_validate_json(data)
    return _PARSED[sha]


def bundle_sources(name: str) -> dict[str, tuple[bytes, str]]:
    """The bytes, and their SHA-256, of a packaged bundle and of every catalogue it includes."""
    data, sha = source("bundles", name)
    if sha not in _CATALOGUE_NAMES:
        _CATALOGUE_NAMES[sha] = tuple(Bundle.model_validate_json(data).catalogues)
    return {f"bundles/{name}.json": (data, sha)} | {f"catalogues/{c}.json": source("catalogues", c) for c in _CATALOGUE_NAMES[sha]}


def bundle_digests(name: str) -> dict[str, str]:
    """SHA-256 of a packaged bundle and of every catalogue it includes: the content a world built from it depends on."""
    return {path: sha for path, (_, sha) in bundle_sources(name).items()}


def catalogue_forms(bundle: Bundle) -> list[AssessmentForm]:
    """Assessment forms from the packaged catalogues, then the bundle's own (a bundle's form replaces one of the same name)."""
    forms = {}
    for f in [f for name in bundle.catalogues for f in _catalogue(name).assessment_forms] + bundle.assessment_forms:
        forms[f.name] = f
    return list(forms.values())


def catalogue_formulary(bundle: Bundle) -> list[Drug]:
    """Packaged catalogue formularies first, then the bundle's own entries, which take precedence for safety metadata."""
    drugs = []
    for name in bundle.catalogues:
        drugs += _catalogue(name).formulary
    return drugs + bundle.formulary


def read_bundle(name: str) -> Bundle:
    """Read a packaged bundle from eprbench/data/bundles by name."""
    text = data_file("bundles", name).read_text()
    return Bundle.model_validate_json(text)
