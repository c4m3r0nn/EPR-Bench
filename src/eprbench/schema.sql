-- All timestamps are INTEGER unix seconds (UTC) in *simulated* time.
-- Tables marked HIDDEN hold ground truth and generator parameters; no
-- operation in ops.py may return their contents directly.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE patient (
    id          INTEGER PRIMARY KEY,
    mrn         TEXT NOT NULL UNIQUE,          -- hospital number
    nhs_number  TEXT NOT NULL UNIQUE,          -- 10 digits, no spaces
    family_name TEXT NOT NULL,
    given_name  TEXT NOT NULL,
    dob         TEXT NOT NULL,                 -- YYYY-MM-DD
    sex         TEXT NOT NULL,
    address     TEXT,
    gp_practice TEXT,
    spo2_scale  INTEGER NOT NULL DEFAULT 1     -- NEWS2 SpO2 scale (2 = hypercapnic target 88-92%)
);

CREATE TABLE allergy (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patient(id),
    substance   TEXT NOT NULL,
    reaction    TEXT,
    severity    TEXT,
    recorded_by TEXT,
    recorded_at INTEGER
);

CREATE TABLE location (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,             -- ED_RESUS | ED_MAJORS | WARD | ICU | OUTPATIENT
    live_monitoring INTEGER NOT NULL DEFAULT 0 -- 1 = continuous bedside monitor feed available
);

CREATE TABLE encounter (
    id                   INTEGER PRIMARY KEY,
    patient_id           INTEGER NOT NULL REFERENCES patient(id),
    kind                 TEXT NOT NULL,        -- inpatient | emergency | outpatient
    location_id          INTEGER REFERENCES location(id),
    bed                  TEXT,
    specialty            TEXT,
    consultant           TEXT,
    presenting_complaint TEXT,
    admitted_at          INTEGER NOT NULL,
    discharged_at        INTEGER,
    discharge_destination TEXT,
    ref                  TEXT                  -- the bundle's name for it, which scenario rules may name
);
CREATE INDEX encounter_patient ON encounter(patient_id, admitted_at);

CREATE TABLE problem (
    id            INTEGER PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patient(id),
    encounter_id  INTEGER REFERENCES encounter(id),
    code_system   TEXT,                        -- e.g. SNOMED-CT, ICD-10
    code          TEXT,
    display       TEXT NOT NULL,
    onset_date    TEXT,
    resolved_date TEXT
);

CREATE TABLE note (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    kind         TEXT NOT NULL,                -- clerking | ward_round | nursing | discharge_summary | ...
    title        TEXT NOT NULL,
    author       TEXT NOT NULL,
    author_role  TEXT,
    written_at   INTEGER NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'final', -- draft (visibly a draft; its author may edit or finalise it) | final
    updated_at   INTEGER,                      -- when a draft was last edited, if ever
    finalised_at INTEGER,                      -- when a draft was made final, and by whom
    finalised_by TEXT,
    origin       TEXT NOT NULL DEFAULT 'record', -- who made the row: agent | staff (scripted response) | record (the bundle)
    origin_action_id INTEGER                   -- the agent action it came from, directly or as a scripted consequence
);
CREATE INDEX note_patient ON note(patient_id, written_at);

-- Every version of a note an operation saved: as written, each edit of a draft, and finalising it.
CREATE TABLE note_revision (
    id           INTEGER PRIMARY KEY,
    note_id      INTEGER NOT NULL REFERENCES note(id),
    patient_id   INTEGER NOT NULL,
    revision     INTEGER NOT NULL,             -- 1 = as first written
    at           INTEGER NOT NULL,
    by           TEXT NOT NULL,
    status       TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT 'agent',
    origin_action_id INTEGER
);
CREATE INDEX note_revision_note ON note_revision(note_id, revision);

CREATE TABLE observation (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    taken_at     INTEGER NOT NULL,
    kind         TEXT NOT NULL,                -- HR | SBP | DBP | RR | SPO2 | TEMP | ON_O2 | ACVPU | WEIGHT (kg) | HEIGHT (cm) | ...
    value        REAL,
    value_text   TEXT,
    recorded_by  TEXT,
    error_at     INTEGER,                      -- the set was marked entered in error: kept and shown struck through,
    error_by     TEXT,                         --   but left out of NEWS2, trends and weight-based dosing
    error_reason TEXT,
    error_origin TEXT,                         -- who marked it: agent | staff | record
    origin       TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);
CREATE INDEX observation_patient ON observation(patient_id, taken_at);

-- For task SQL (see scenario.py). observation_chart: every reading ever charted by now, with its retraction if it
-- has happened by now (who, why, and whether the agent did it). observation_current: the chart as it stands now.
-- The chart as it stood at time :t is observation_chart WHERE taken_at <= :t AND (retracted_at IS NULL OR retracted_at > :t).
CREATE VIEW observation_chart AS
SELECT o.id, o.patient_id, o.encounter_id, o.taken_at, o.kind, o.value, o.value_text, o.recorded_by, o.origin, o.origin_action_id,
       CASE WHEN o.error_at <= n.now THEN o.error_at END AS retracted_at,
       CASE WHEN o.error_at <= n.now THEN o.error_by END AS retracted_by,
       CASE WHEN o.error_at <= n.now THEN o.error_reason END AS retracted_reason,
       CASE WHEN o.error_at <= n.now THEN o.error_origin END AS retracted_origin
FROM observation o, (SELECT CAST(value AS INTEGER) AS now FROM meta WHERE key = 'now') n
WHERE o.taken_at <= n.now;
CREATE VIEW observation_current AS SELECT * FROM observation_chart WHERE retracted_at IS NULL;
-- Each weight on the chart as it stands, with the latest height (cm) charted by then and the BMI they give (kg/m2, 1 dp).
-- Who charted each: *_origin and *_origin_action_id, so a case can require measurements the case itself supports.
CREATE VIEW bmi AS
SELECT w.patient_id, w.id AS observation_id, w.taken_at, w.value AS weight_kg, h.value AS height_cm,
       round(w.value / ((h.value / 100.0) * (h.value / 100.0)), 1) AS bmi,
       w.origin AS weight_origin, w.origin_action_id AS weight_origin_action_id,
       h.id AS height_observation_id, h.taken_at AS height_taken_at, h.origin AS height_origin, h.origin_action_id AS height_origin_action_id
FROM observation_current w JOIN observation_current h ON h.id = (
    SELECT x.id FROM observation_current x WHERE x.patient_id = w.patient_id AND x.kind = 'HEIGHT' AND x.value > 0 AND x.taken_at <= w.taken_at
    ORDER BY x.taken_at DESC, x.id DESC LIMIT 1)
WHERE w.kind = 'WEIGHT' AND w.value > 0;

CREATE TABLE orderable (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,                 -- lab | imaging | cardiology | ...
    modality    TEXT,                          -- imaging: XR | US | CT | MRI | Nuclear medicine | Fluoroscopy | Interventional
    region      TEXT,                          -- imaging: body region, e.g. Chest, Lower limb
    lateral     INTEGER NOT NULL DEFAULT 0,    -- the request must state a side
    cost_pence  INTEGER NOT NULL,
    contrast    INTEGER NOT NULL DEFAULT 0,    -- uses iodinated contrast: alert if renal function is poor
    tat_seconds INTEGER NOT NULL,              -- sample-to-report turnaround (from collection); a bundle may override it
    point_of_care INTEGER NOT NULL DEFAULT 0,  -- analysed at the bedside (e.g. blood gas), not in the laboratory
    fallback    TEXT NOT NULL DEFAULT 'last_value', -- outside every result window: last_value (each analyte's latest
                                               --   value for the patient, else the normal) | normal (the catalogue normal)
    fallback_max_age_days REAL NOT NULL DEFAULT 90, -- last_value carries a value forward at most this long
    normal      TEXT,                          -- JSON result payload: the catalogue normal
    min_repeat_seconds INTEGER                 -- requests closer together than this are duplicates (NULL: safety's default)
);

CREATE TABLE "order" (
    id               INTEGER PRIMARY KEY,
    patient_id       INTEGER NOT NULL REFERENCES patient(id),
    encounter_id     INTEGER REFERENCES encounter(id),
    orderable_code   TEXT NOT NULL REFERENCES orderable(code),
    priority         TEXT NOT NULL DEFAULT 'routine',
    clinical_details TEXT,
    status           TEXT NOT NULL DEFAULT 'active',   -- active | cancelled (by a person, or not collected)
    ordered_by       TEXT NOT NULL,
    ordered_at       INTEGER NOT NULL,
    due_at           INTEGER NOT NULL,         -- when it is reported: collect_at plus the turnaround
    override_reason  TEXT,                     -- reason given for requesting despite an alert
    request_details  TEXT,                     -- JSON: the completed request form
    egfr_on_record   REAL,                     -- HIDDEN: latest reported eGFR when requested, to check what was typed
    collection       TEXT NOT NULL DEFAULT 'now', -- now | phlebotomy_round | scheduled: when the sample is to be taken
    collect_at       INTEGER,                  -- when it is to be taken (ordered_at for now)
    collected_at     INTEGER,                  -- when it was taken (NULL while scheduled); results are evaluated then
    cancelled_at     INTEGER,
    cancelled_by     TEXT,
    cancel_reason    TEXT,
    origin           TEXT NOT NULL DEFAULT 'agent',
    origin_action_id INTEGER
);
CREATE INDEX order_patient ON "order"(patient_id, ordered_at);
CREATE INDEX order_to_collect ON "order"(collect_at) WHERE status = 'active' AND collected_at IS NULL;

-- For task SQL. order_status: every request made by now with its status now: scheduled (not yet collected),
-- in_progress (collected, not reported), resulted, or cancelled (cancel_reason says why, 'Not collected: ...' when
-- the patient had left by the collection time). order_field: one row per completed request-form field.
CREATE VIEW order_status AS
SELECT o.id AS order_id, o.patient_id, o.encounter_id, o.orderable_code, o.priority, o.collection, o.ordered_by, o.ordered_at,
       o.collect_at, o.collected_at, o.due_at,
       CASE WHEN o.status = 'cancelled' THEN 'cancelled' WHEN o.collected_at IS NULL OR o.collected_at > n.now THEN 'scheduled'
            WHEN r.reported_at <= n.now THEN 'resulted' ELSE 'in_progress' END AS status,
       o.cancelled_at, o.cancelled_by, o.cancel_reason, r.id AS result_id, r.reported_at, o.origin, o.origin_action_id
FROM "order" o JOIN (SELECT CAST(value AS INTEGER) AS now FROM meta WHERE key = 'now') n LEFT JOIN result r ON r.order_id = o.id
WHERE o.ordered_at <= n.now;
CREATE VIEW order_field AS
SELECT o.id AS order_id, o.patient_id, o.orderable_code, f.key AS field, f.value
FROM "order" o, json_each(coalesce(o.request_details, '{}')) f;

-- A result exists from the moment the test is performed but is only visible
-- to operations once the simulated clock reaches reported_at.
CREATE TABLE result (
    id             INTEGER PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES patient(id),
    encounter_id   INTEGER REFERENCES encounter(id),
    order_id       INTEGER REFERENCES "order"(id),
    orderable_code TEXT NOT NULL REFERENCES orderable(code),
    performed_at   INTEGER NOT NULL,
    reported_at    INTEGER NOT NULL,
    reported_by    TEXT,
    report_text    TEXT,
    acknowledged_by TEXT,
    acknowledged_at INTEGER,
    source         TEXT NOT NULL DEFAULT 'hospital_lab',  -- hospital_lab | point_of_care | gp | other_hospital
    origin         TEXT NOT NULL DEFAULT 'record',  -- record (the bundle), or staff: reported on a request, with
    origin_action_id INTEGER                         --   origin_action_id the agent action that made the request
);
CREATE INDEX result_patient ON result(patient_id, reported_at);

-- Taking a request's sample (or doing the test): collected, or not collected (the patient had left). Taken now, it is
-- the requester's (origin agent, their action); taken later, by staff (origin staff, collector's role, and the
-- requesting action as origin_action_id). The request itself keeps the requester's attribution.
CREATE TABLE collection_event (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES "order"(id),
    patient_id INTEGER NOT NULL,
    at         INTEGER NOT NULL,
    status     TEXT NOT NULL,                  -- collected | not_collected
    by         TEXT NOT NULL,
    role       TEXT,
    origin     TEXT NOT NULL,
    origin_action_id INTEGER
);
CREATE INDEX collection_event_order ON collection_event(order_id);

CREATE TABLE result_item (
    id         INTEGER PRIMARY KEY,
    result_id  INTEGER NOT NULL REFERENCES result(id) ON DELETE CASCADE,
    analyte    TEXT NOT NULL,
    value      REAL,
    value_text TEXT,
    unit       TEXT,
    ref_low    REAL,
    ref_high   REAL,
    flag       TEXT                            -- H | L | NULL
);
CREATE INDEX result_item_result ON result_item(result_id);
CREATE INDEX result_item_analyte ON result_item(analyte);

-- The formulary. `classes` is a JSON list used to match allergies (e.g. amoxicillin -> penicillin).
CREATE TABLE drug (
    name    TEXT PRIMARY KEY COLLATE NOCASE,
    classes TEXT NOT NULL DEFAULT '[]',
    routes  TEXT NOT NULL DEFAULT '[]',        -- JSON list of permitted routes; empty = any
    flags   TEXT NOT NULL DEFAULT '[]',        -- JSON list: insulin | weekly | critical_care_only | critical
    max_dose_mg  REAL,                         -- usual maximum single dose
    max_daily_mg REAL,                         -- usual maximum total in 24 hours
    max_dose_basis TEXT                        -- what the maxima are doses of (a salt, e.g. Codeine phosphate); NULL = the drug itself
);

-- A drug in a specific form and strength (a dm+d virtual medicinal product), e.g. "Amoxicillin 500mg capsules".
-- When a drug has products, prescriptions must name one, and its routes replace the drug's.
CREATE TABLE product (
    name            TEXT PRIMARY KEY COLLATE NOCASE,
    drug            TEXT NOT NULL REFERENCES drug(name),
    form            TEXT,
    routes          TEXT NOT NULL DEFAULT '[]',
    unit_dose_form  TEXT,
    controlled_drug TEXT,
    vpid            TEXT,                      -- dm+d identifier, when imported
    strengths       TEXT NOT NULL DEFAULT '[]',  -- JSON: [{ingredient, value, unit, per_value, per_unit}]
    flags           TEXT NOT NULL DEFAULT '[]'   -- product-level safety flags, e.g. critical_care_only
);
CREATE INDEX product_drug ON product(drug);

-- Decision support: when a matching drug is prescribed and the condition holds for the patient,
-- an alert blocks the prescription unless a reason for overriding it is given.
CREATE TABLE prescribing_rule (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,               -- contraindication | interaction
    drug_regex    TEXT,                        -- matches the drug name, or
    drug_class    TEXT,                        -- matches one of its classes
    problem_regex TEXT,                        -- condition: an unresolved problem matches, or
    analyte       TEXT,                        -- condition: the latest reported value of this analyte is
    below         REAL,                        --   below this, or
    above         REAL,                        --   above this, or
    with_class    TEXT,                        -- condition: another active medicine has this class
    message       TEXT NOT NULL
);

CREATE TABLE medication (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    kind         TEXT NOT NULL DEFAULT 'regular',  -- regular | prn | stat | tto (to take out, on discharge)
    drug         TEXT NOT NULL,
    product      TEXT,
    dose         TEXT NOT NULL,
    route        TEXT NOT NULL,
    frequency    TEXT NOT NULL,
    indication   TEXT,
    prescriber   TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    stopped_at   INTEGER,
    stop_reason  TEXT,
    duration     TEXT,
    overridden_alert TEXT,                     -- comma-separated alert codes that were overridden
    override_reason  TEXT,                     -- reason given for prescribing despite the alert
    -- Structured as prescribed (ops.structured_dose), for regimens and grading:
    dose_low     REAL,                         -- the dose, or the bottom and top of a range, in dose_unit: a mass in mg
    dose_high    REAL,                         --   (through the product's strength where it can be), units, mL, or as written
    dose_unit    TEXT,
    dose_basis   TEXT,                         -- what a mass or units are of: the product's stated ingredient (a salt,
                                               --   e.g. Codeine phosphate), else the drug's declared basis or name
    doses_per_day REAL,                        -- from the frequency (BD = every 12 hours = 2); NULL for once, weekly, infusions
    weight_kg    REAL,                         -- the latest weight charted when prescribed, as the prescriber saw it
    origin       TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);
CREATE INDEX medication_patient ON medication(patient_id);

CREATE TABLE administration (
    id            INTEGER PRIMARY KEY,
    medication_id INTEGER NOT NULL REFERENCES medication(id),
    patient_id    INTEGER NOT NULL REFERENCES patient(id),
    at            INTEGER NOT NULL,
    status        TEXT NOT NULL,               -- given | omitted | refused
    recorded_by   TEXT NOT NULL,
    note          TEXT,
    dose          TEXT,                        -- what was given, as written (a single amount within the prescription)
    dose_value    REAL,                        -- structured like the medication's dose
    dose_unit     TEXT,
    dose_basis    TEXT,
    origin        TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);
CREATE INDEX administration_medication ON administration(medication_id, at);

CREATE TABLE assessment (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    kind         TEXT NOT NULL,                -- vte | bleeding_risk | capacity | falls | frailty, or a bundle form's name
    outcome      TEXT NOT NULL,                -- for a form: its band's label unless the assessor wrote an outcome
    details      TEXT,
    assessed_by  TEXT NOT NULL,
    assessed_at  INTEGER NOT NULL,
    score        REAL,                         -- a form's computed total
    category     TEXT,                         -- the value of the form's band the total falls in
    origin       TEXT NOT NULL DEFAULT 'agent',
    origin_action_id INTEGER
);
CREATE INDEX assessment_patient ON assessment(patient_id, assessed_at);

-- Structured assessment forms a bundle defines (e.g. a two-level Wells score): typed fields with points, and bands
-- that turn the total into a category. fields and bands are JSON (loader.AssessmentForm).
CREATE TABLE assessment_form (
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT,
    fields      TEXT NOT NULL,
    bands       TEXT NOT NULL DEFAULT '[]'
);

-- Every field of every form assessment as recorded, an unticked box included (value 'no'), with its points.
CREATE TABLE assessment_answer (
    id            INTEGER PRIMARY KEY,
    assessment_id INTEGER NOT NULL REFERENCES assessment(id),
    patient_id    INTEGER NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    points        REAL NOT NULL DEFAULT 0
);
CREATE INDEX assessment_answer_assessment ON assessment_answer(assessment_id);

CREATE TABLE referral (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    specialty    TEXT NOT NULL,
    urgency      TEXT NOT NULL DEFAULT 'routine',
    reason       TEXT NOT NULL,                -- free text for the reader; no rule or check should key on it
    referred_by  TEXT NOT NULL,
    referred_at  INTEGER NOT NULL,
    pathway      TEXT,                         -- the named pathway chosen from the picklist (e.g. ppci); NULL: a general referral
    pathway_details TEXT,                      -- JSON: the pathway form's structured fields
    service      TEXT,                        -- HIDDEN: the hospital service the destination resolved to, if any
    status       TEXT NOT NULL DEFAULT 'sent', -- sent, or the response's status (accepted | advice | declined) once responded_at passes
    responded_at INTEGER,
    responded_by TEXT,
    responder_role TEXT,
    response     TEXT,
    origin       TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);
CREATE INDEX referral_patient ON referral(patient_id, referred_at);
CREATE VIEW referral_field AS
SELECT r.id AS referral_id, r.patient_id, r.pathway, f.key AS field, f.value
FROM referral r, json_each(coalesce(r.pathway_details, '{}')) f;

-- Named referral pathways a bundle defines (e.g. Primary PCI activation, 2-week wait), each with its own form.
CREATE TABLE pathway (
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    service     TEXT,                          -- the service it reaches
    urgency     TEXT,                          -- set by the pathway, if it says
    description TEXT,
    fields      TEXT NOT NULL DEFAULT '[]'
);

-- The services a referral can reach, and the names it may use for them (matched as whole words, any case).
CREATE TABLE service (
    name    TEXT PRIMARY KEY COLLATE NOCASE,
    aliases TEXT NOT NULL DEFAULT '[]'
);

-- Named regimens as structured dose criteria (e.g. treatment-dose anticoagulation), declared by the bundle. A
-- medication meets a regimen if it meets any one row of it. The simulation and grading share them through the
-- regimen_match view.
CREATE TABLE regimen (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    drug        TEXT NOT NULL COLLATE NOCASE,
    basis       TEXT NOT NULL COLLATE NOCASE,  -- what the limits are amounts of (a salt where the products state one)
    unit        TEXT NOT NULL DEFAULT 'mg',    -- what the limits are in: mg (any mass), units, mL, tablets, ...
    min_dose    REAL,                          -- per dose: the bottom of a prescribed range must reach it
    max_dose    REAL,                          --   and the top must not exceed this
    min_per_kg  REAL,                          -- per dose, per kg of the weight charted when prescribed
    max_per_kg  REAL,
    per_day     TEXT NOT NULL DEFAULT '[]',    -- JSON list of doses per day (from the bundle's frequencies; BD = 2); empty = any
    routes      TEXT NOT NULL DEFAULT '[]',    -- JSON list of routes; empty = any
    kinds       TEXT NOT NULL DEFAULT '["regular","stat"]'  -- JSON list of kinds that count; never prn or tto unless listed
);
CREATE INDEX regimen_drug ON regimen(drug);

-- Every (medication, regimen) pair where the prescription as written meets the regimen: the drug and basis, the
-- whole of any range within the limits, the doses per day, route and kind, and the weight charted when prescribed.
CREATE VIEW regimen_match AS
SELECT DISTINCT m.id AS medication_id, m.patient_id, r.name AS regimen, m.kind, m.doses_per_day, m.started_at, m.stopped_at
FROM medication m JOIN regimen r ON r.drug = m.drug
WHERE m.dose_low IS NOT NULL AND m.dose_unit = r.unit AND m.dose_basis = r.basis
  AND (r.min_dose IS NULL OR m.dose_low >= r.min_dose)
  AND (r.max_dose IS NULL OR m.dose_high <= r.max_dose)
  AND (r.min_per_kg IS NULL OR m.dose_low >= r.min_per_kg * m.weight_kg)
  AND (r.max_per_kg IS NULL OR m.dose_high <= r.max_per_kg * m.weight_kg)
  AND (r.per_day = '[]' OR m.doses_per_day IN (SELECT value FROM json_each(r.per_day)))
  AND (r.routes = '[]' OR m.route IN (SELECT value FROM json_each(r.routes)))
  AND m.kind IN (SELECT value FROM json_each(r.kinds));

-- Every (administration, regimen) pair where a dose recorded as given meets the regimen: the amount actually given,
-- with the prescription's frequency, route, kind and weight.
CREATE VIEW administration_regimen_match AS
SELECT DISTINCT a.id AS administration_id, a.medication_id, a.patient_id, r.name AS regimen, a.at
FROM administration a JOIN medication m ON m.id = a.medication_id JOIN regimen r ON r.drug = m.drug
WHERE a.status = 'given' AND a.dose_value IS NOT NULL AND a.dose_unit = r.unit AND a.dose_basis = r.basis
  AND (r.min_dose IS NULL OR a.dose_value >= r.min_dose)
  AND (r.max_dose IS NULL OR a.dose_value <= r.max_dose)
  AND (r.min_per_kg IS NULL OR a.dose_value >= r.min_per_kg * m.weight_kg)
  AND (r.max_per_kg IS NULL OR a.dose_value <= r.max_per_kg * m.weight_kg)
  AND (r.per_day = '[]' OR m.doses_per_day IN (SELECT value FROM json_each(r.per_day)))
  AND (r.routes = '[]' OR m.route IN (SELECT value FROM json_each(r.routes)))
  AND m.kind IN (SELECT value FROM json_each(r.kinds));

-- HIDDEN: what a test shows for any sample taken inside [valid_from, valid_to) (NULL = unbounded), however many
-- times it is requested. Where windows overlap, the one that started latest wins, then the later row. Outside every
-- window the orderable's fallback applies. Scenario effects add windows and close the ones they replace.
CREATE TABLE latent_result (
    id             INTEGER PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES patient(id),
    orderable_code TEXT NOT NULL REFERENCES orderable(code),
    valid_from     INTEGER,
    valid_to       INTEGER,
    payload        TEXT NOT NULL               -- JSON: {report_text, reported_by, items[]}
);
CREATE INDEX latent_result_patient ON latent_result(patient_id, orderable_code);

-- HIDDEN: parameters of the deterministic bedside-monitor signal generator.
CREATE TABLE vitals_track (
    id         INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patient(id),
    signal     TEXT NOT NULL,
    seed       INTEGER NOT NULL,
    sd         REAL NOT NULL,
    lo         REAL,
    hi         REAL,
    decimals   INTEGER NOT NULL DEFAULT 0,
    keyframes  TEXT NOT NULL,                  -- JSON [[t, value], ...] sorted by t
    UNIQUE (patient_id, signal)
);

-- HIDDEN: how the patient and the hospital respond. A clinical outcome rule fires at most once, either when a
-- matching operation succeeds (on_op with match and when, optionally only before not_after) or when the clock
-- reaches `at` (unless the rule named in `unless` has already taken effect by then). A staff response (`repeat`)
-- responds to every matching event, e.g. a nurse giving each new prescription. Effects take place delay_s after the
-- event, through scenario_pending. See scenario.py.
CREATE TABLE scenario_rule (
    id         INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patient(id),
    name       TEXT NOT NULL,
    on_op      TEXT,
    match      TEXT,                           -- JSON {field: regex}
    condition  TEXT,                           -- JSON structured conditions on the event (the bundle's `when`)
    not_after  INTEGER,
    at         INTEGER,
    unless     TEXT,
    delay_s    INTEGER NOT NULL DEFAULT 0,
    effects    TEXT NOT NULL,                  -- JSON
    repeat     INTEGER NOT NULL DEFAULT 0,     -- 1: a staff response to every matching event; 0: a once-only outcome
    fired_at   INTEGER,                        -- when it was (first) triggered
    applied_at INTEGER,                        -- when its effects (first) took place
    skipped    INTEGER NOT NULL DEFAULT 0,
    not_before INTEGER,                        -- an on_op rule responds only to events from this time
    encounter  TEXT DEFAULT 'current',         -- active only while an encounter is open: 'current' (any), one by its ref,
                                               --   or NULL (always). `unless` is a JSON list of rule names
    after_discharge TEXT DEFAULT 'suppress',   -- a delayed effect falling due after the patient has left: suppress | deliver
                                               --   (legacy: a world saved before effects were gated again)
    variants   TEXT,                           -- JSON [{when, effects}]: the first whose conditions hold when it takes effect
    UNIQUE (patient_id, name)
);
CREATE INDEX scenario_rule_at ON scenario_rule(at) WHERE on_op IS NULL AND applied_at IS NULL AND skipped = 0;

-- HIDDEN: effects waiting for their time: each triggered rule's response to one event. A response to a
-- prescription is cancelled when that prescription is stopped.
CREATE TABLE scenario_pending (
    id            INTEGER PRIMARY KEY,
    rule_id       INTEGER NOT NULL REFERENCES scenario_rule(id),
    patient_id    INTEGER NOT NULL,
    event         TEXT NOT NULL,               -- JSON: identifiers of the triggering event (referral_id, medication_id, ...)
    medication_id INTEGER,
    fired_at      INTEGER NOT NULL,
    due_at        INTEGER NOT NULL,
    applied_at    INTEGER,
    cancelled_at  INTEGER
);
CREATE INDEX scenario_pending_due ON scenario_pending(due_at) WHERE applied_at IS NULL AND cancelled_at IS NULL;

-- HIDDEN: every time a rule's effects fell due, each application of a repeating staff response included: applied, or
-- suppressed (why) when its gates no longer held then.
CREATE TABLE scenario_application (
    id         INTEGER PRIMARY KEY,
    rule_id    INTEGER NOT NULL REFERENCES scenario_rule(id),
    patient_id INTEGER NOT NULL,
    event      TEXT NOT NULL,                  -- JSON: the triggering event's identifiers and structured fields; {} if timed
    applied_at INTEGER NOT NULL,
    origin_action_id INTEGER,                  -- the agent action that set it off, if any
    variant    INTEGER,                        -- which of the rule's variants took effect (NULL: its own effects)
    suppressed TEXT                            -- NULL: it took effect; otherwise why it did not
);

-- Every operation, read or write, successful or not. Doubles as the trajectory log.
CREATE TABLE audit (
    id        INTEGER PRIMARY KEY,
    at        INTEGER NOT NULL,
    actor     TEXT NOT NULL,
    interface TEXT NOT NULL,                   -- tool | api | ui | loader
    op        TEXT NOT NULL,
    args      TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    error     TEXT,
    writes    INTEGER NOT NULL DEFAULT 0,      -- 1 if the operation changes the record
    patient_id INTEGER,                        -- whose record was read or changed, when there is one
    action_id INTEGER,                         -- the agent action that caused it: one tool call, one API call, or one
                                               --   browser request (a page load runs several operations)
    result    TEXT                             -- HIDDEN from the agent: JSON of what the operation returned, or
                                               --   {error, message} when it failed (evidence, equal for every surface)
);

-- HIDDEN, for grading (agent_read in assessment.py): what each agent action showed the agent of the record. An entry
-- whose content an operation returned (tool, api), or that was inside a screenshot sent to the model (ui); a page
-- rendered is recorded too, as `rendered`, and never counts as read. See ops.SHOWS.
CREATE TABLE read_event (
    id         INTEGER PRIMARY KEY,
    at         INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    interface  TEXT NOT NULL,                  -- tool | api | ui
    action_id  INTEGER,                        -- the agent action that showed it
    audit_id   INTEGER,                        -- the operation that returned it (tool, api); NULL on a UI page
    patient_id INTEGER,                        -- whose record the entry is on
    kind       TEXT NOT NULL,                  -- ops.READ_KINDS
    object_id  INTEGER,                        -- the entry's id; NULL for a part of the record shown whole
    via        TEXT NOT NULL DEFAULT 'returned' -- ops.VIA: returned | screenshot | person | rendered (never a read)
);
CREATE INDEX read_event_kind ON read_event(kind, object_id);

CREATE TABLE fluid_entry (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patient(id),
    at          INTEGER NOT NULL,
    direction   TEXT NOT NULL,                 -- in | out
    kind        TEXT NOT NULL,                 -- oral, IV fluids, urine, drain, vomit, ...
    volume_ml   INTEGER NOT NULL,
    recorded_by TEXT
);
CREATE INDEX fluid_entry_patient ON fluid_entry(patient_id, at);

-- The ward jobs list handed between shifts. The description is free text for the reader; what a job is for is its
-- task_type and, optionally, the request it concerns. status is open | done | cancelled, and job_event its history.
CREATE TABLE job (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patient(id),
    description TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'routine',
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    done_by     TEXT,                          -- completed: by whom, when, and what was done (never set by cancelling)
    done_at     INTEGER,
    outcome     TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    task_type   TEXT,                          -- one of ops.JOB_TYPES, e.g. bloods, chase_result
    order_id    INTEGER REFERENCES "order"(id),
    cancelled_by TEXT,
    cancelled_at INTEGER,
    cancel_reason TEXT,
    origin      TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);

CREATE TABLE job_event (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES job(id),
    patient_id  INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    status      TEXT NOT NULL,                 -- open (added) | done | cancelled
    by          TEXT NOT NULL,
    note        TEXT,                          -- the outcome, or why it was cancelled
    origin      TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);
CREATE INDEX job_event_job ON job_event(job_id, at);
CREATE TRIGGER job_added AFTER INSERT ON job BEGIN
    INSERT INTO job_event (job_id, patient_id, at, status, by, origin, origin_action_id)
    VALUES (new.id, new.patient_id, new.created_at, 'open', new.created_by, new.origin, new.origin_action_id);
END;

CREATE TABLE blood_request (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    product      TEXT NOT NULL,
    units        INTEGER NOT NULL,
    urgency      TEXT NOT NULL DEFAULT 'routine',
    indication   TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at INTEGER NOT NULL
);
