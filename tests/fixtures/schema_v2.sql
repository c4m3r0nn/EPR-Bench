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
    discharge_destination TEXT
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
    origin       TEXT NOT NULL DEFAULT 'record', -- who made the row: agent | staff (scripted response) | record (the bundle)
    origin_action_id INTEGER                   -- the agent action it came from, directly or as a scripted consequence
);
CREATE INDEX note_patient ON note(patient_id, written_at);

CREATE TABLE observation (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    taken_at     INTEGER NOT NULL,
    kind         TEXT NOT NULL,                -- HR | SBP | DBP | RR | SPO2 | TEMP | ON_O2 | ACVPU | WEIGHT | ...
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

CREATE TABLE orderable (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,                 -- lab | imaging | cardiology | ...
    modality    TEXT,                          -- imaging: XR | US | CT | MRI | Nuclear medicine | Fluoroscopy | Interventional
    region      TEXT,                          -- imaging: body region, e.g. Chest, Lower limb
    lateral     INTEGER NOT NULL DEFAULT 0,    -- the request must state a side
    cost_pence  INTEGER NOT NULL,
    contrast    INTEGER NOT NULL DEFAULT 0,    -- uses iodinated contrast: alert if renal function is poor
    tat_seconds INTEGER NOT NULL,              -- sample-to-report turnaround (the sample is taken when requested)
    point_of_care INTEGER NOT NULL DEFAULT 0,  -- analysed at the bedside (e.g. blood gas), not in the laboratory
    fallback    TEXT NOT NULL DEFAULT 'last_value', -- outside every result window: last_value (each analyte's latest
                                               --   value for the patient, else the normal) | normal (the catalogue normal)
    fallback_max_age_days REAL NOT NULL DEFAULT 90, -- last_value carries a value forward at most this long
    normal      TEXT                           -- JSON result payload: the catalogue normal
);

CREATE TABLE "order" (
    id               INTEGER PRIMARY KEY,
    patient_id       INTEGER NOT NULL REFERENCES patient(id),
    encounter_id     INTEGER REFERENCES encounter(id),
    orderable_code   TEXT NOT NULL REFERENCES orderable(code),
    priority         TEXT NOT NULL DEFAULT 'routine',
    clinical_details TEXT,
    status           TEXT NOT NULL DEFAULT 'active',   -- active | cancelled
    ordered_by       TEXT NOT NULL,
    ordered_at       INTEGER NOT NULL,
    due_at           INTEGER NOT NULL,
    override_reason  TEXT,                     -- reason given for requesting despite an alert
    request_details  TEXT,                     -- JSON: the completed request form
    egfr_on_record   REAL                      -- HIDDEN: latest reported eGFR when requested, to check what was typed
);
CREATE INDEX order_patient ON "order"(patient_id, ordered_at);

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
    source         TEXT NOT NULL DEFAULT 'hospital_lab'  -- hospital_lab | point_of_care | gp | other_hospital
);
CREATE INDEX result_patient ON result(patient_id, reported_at);

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
    kind         TEXT NOT NULL,                -- vte | bleeding_risk | capacity | falls | ...
    outcome      TEXT NOT NULL,
    details      TEXT,
    assessed_by  TEXT NOT NULL,
    assessed_at  INTEGER NOT NULL
);
CREATE INDEX assessment_patient ON assessment(patient_id, assessed_at);

CREATE TABLE referral (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patient(id),
    encounter_id INTEGER REFERENCES encounter(id),
    specialty    TEXT NOT NULL,
    urgency      TEXT NOT NULL DEFAULT 'routine',
    reason       TEXT NOT NULL,
    referred_by  TEXT NOT NULL,
    referred_at  INTEGER NOT NULL,
    service      TEXT,                         -- HIDDEN: the hospital service the destination resolved to, if any
    status       TEXT NOT NULL DEFAULT 'sent', -- sent, or the response's status (accepted | advice | declined) once responded_at passes
    responded_at INTEGER,
    responded_by TEXT,
    responder_role TEXT,
    response     TEXT,
    origin       TEXT NOT NULL DEFAULT 'record',
    origin_action_id INTEGER
);
CREATE INDEX referral_patient ON referral(patient_id, referred_at);

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

-- The ward jobs list handed between shifts.
CREATE TABLE job (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patient(id),
    description TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'routine',
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    done_by     TEXT,
    done_at     INTEGER,
    outcome     TEXT
);

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
