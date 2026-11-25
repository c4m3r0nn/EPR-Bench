"""How the simulated patient and hospital respond over time and to what is done.

Most scripted change needs no machinery at all: bundles may contain notes,
observations and results dated in the future, and they simply come into view
as the clock passes them. Rules cover the rest, where the future depends on
the agent: "once anticoagulation has been given, the observations settle",
"if nobody has referred by 10:00, the nurse escalates".

Two kinds of rule. A clinical outcome fires once: when a matching write
succeeds, or when the clock reaches its time. A staff response (`repeat`)
responds to every matching event: a nurse gives each newly charted treatment
dose. Effects take place `delay_s` after the event, which is how staff respond:
cardiology accept a referral 5 minutes after it is made, a nurse gives a new
prescription 30 minutes after it is charted. Each pending response belongs to
its event and is cancelled if that prescription is stopped first. An order is
not treatment and a referral is not senior review, so a patient improves
through a rule on the response or on an administration, never on the order or
the referral alone. Rules match structurally (a regimen of drug, dose,
frequency and weight; a hospital service named in a referral), with regular
expressions kept for older bundles.

Reading and navigating cost no simulated time: the clock moves only by explicit
waits, and these declared delays and investigation turnaround times say what
has happened by then.

Rules are evaluated lazily inside ops.call, so there is no scheduler. A rule
that came due while the clock jumped forward is applied as of its own time,
which is indistinguishable to the agent because it could not have looked in
between. Samples scheduled for later (a request for tomorrow's phlebotomy
round) are taken the same way, in time order with the rules, so what a sample
shows is evaluated at the time it is taken.

A rule is gated so that what it scripts stays coherent: it is active only while an encounter is open for the patient
(`encounter`: "current", the default, any open encounter; an encounter's ref; or null, always), an on_op rule only
for events in [not_before, not_after], and no rule fires once any rule named in `unless` (a name or a list) has taken
effect. A delayed effect is gated again when it falls due: suppressed if an `unless` rule has taken effect by then
or the patient has left the encounter (unless the rule says after_discharge "deliver"). Conditions key on structured
fields, never on free text: `pathway` (the referral pathway chosen; "" for a general referral), `fields` (the request
form, pathway form or assessment answers recorded), `record` (a reported result of a test is, or is not, on the
record), `service`, `regimen`, any event field's value or values (e.g. urgency, status, kind, collection) and numeric
ranges ({"score": {"min": 2}}). A rule's `variants` choose its effects when it takes effect, on the same conditions:
a team says it reviewed the ECG only if an ECG result is on the record. Every application of a rule, each one of a
repeating staff response included, is recorded in scenario_application, applied or suppressed (and why).

For task authors: v2 checks grade structured entries only. Free text (a referral's reason, a note's body, a job's
description) is for the clinician reading the episode; "Do not request U&E" in a job is not a request for U&E. What a
check can use without hand-rolling it (schema.sql):

  order_status(order_id, patient_id, encounter_id, orderable_code, priority, collection, ordered_by, ordered_at,
               collect_at, collected_at, due_at, status, cancelled_at, cancelled_by, cancel_reason, result_id,
               reported_at, origin, origin_action_id)
      every request made by now. collection is now | phlebotomy_round | scheduled; status is scheduled (not yet
      collected) | in_progress | resulted | cancelled ('Not collected: ...' when the patient had left by then).
      "Bloods arranged for tomorrow's round": collection = 'phlebotomy_round' AND status != 'cancelled'.
  order_field(order_id, patient_id, orderable_code, field, value)
      one row per completed request-form field: a left leg Doppler is field = 'laterality' AND value = 'left'.
  referral.pathway, referral.pathway_details (JSON), and referral_field(referral_id, patient_id, pathway, field, value)
      a pathway chosen from the picklist and its form: PPCI activation is pathway = 'ppci', not a reason mentioning
      PCI. referral.urgency is the pathway's own where it sets one (e.g. emergency).
  note.status ('draft' | 'final'), note.finalised_at, note.finalised_by; note_revision(note_id, revision, at, by,
      status, title, body, origin, origin_action_id), every version an operation saved. "Summary complete":
      kind = 'discharge_summary' AND status = 'final'; a draft does not allow discharge either.
  assessment.kind, .score, .category (a form's band value, e.g. dvt_likely), and assessment_answer(assessment_id,
      patient_id, field, value, points) for every field of a form assessment, an unticked box as value 'no'.
  job.status ('open' | 'done' | 'cancelled'), job.task_type (ops.JOB_TYPES), job.order_id (the request it concerns),
      and job_event(job_id, patient_id, at, status, by, note, origin, origin_action_id), its history. Completed is
      status = 'done'; cancelling never sets done_at.
  bmi(patient_id, observation_id, taken_at, weight_kg, height_cm, bmi, weight_origin, weight_origin_action_id,
      height_observation_id, height_taken_at, height_origin, height_origin_action_id)
      each weight on the chart as it stands, with the latest height (cm) charted by then and who charted each.
  collection_event(order_id, patient_id, at, status, by, role, origin, origin_action_id)
      taking each request's sample: collected or not_collected; origin agent when taken with the request, staff
      (the collector's role) when taken later, with the requesting action as origin_action_id. Results on a request
      are origin staff with that action; results loaded with the bundle are origin record.
  scenario_application(rule_id, patient_id, event, applied_at, origin_action_id, variant, suppressed)  HIDDEN
      every time a scripted response fell due, e.g. to check what the simulation did in an episode.

  Writing checks on these (from the review of the first cases):
  - A request counts only as it stands: not cancelled, collected in the window the case needs (collect_at,
    collected_at) and, where the case needs a result, reported (order_status.status = 'resulted'). A request booked 90
    days ahead is not a timely investigation.
  - A job proves nothing by itself: join job.order_id to order_status for the request's current status, and treat
    job.status = 'done' as a claim, not as the work.
  - Check a form's answers (assessment_answer) against the case facts, not only the computed category: a Wells
    'DVT unlikely' reached by leaving the true criteria unticked is not a correct assessment.
  - note.status = 'final' proves the document was signed, not that it is complete; what it must contain needs a
    structured entry elsewhere, or the clinician's reading.
  - A reply meant to come before admission or in outpatients needs encounter: null on its rule, or it never fires;
    one meant to follow the patient home needs after_discharge: "deliver".
  - Every value of a field a result branches on needs a result (lint checks it), and a side-specific finding needs
    an explicit bilateral branch.

Also:

  regimen_match(medication_id, patient_id, regimen, kind, doses_per_day, started_at, stopped_at)
      prescriptions that meet a named regimen as written (meets_regimen). medication also has dose_low,
      dose_high, dose_unit, dose_basis, doses_per_day and weight_kg (frozen when prescribed).
  administration_regimen_match(administration_id, medication_id, patient_id, regimen, at)
      doses recorded as given that meet it, by the amount actually given (given_in_regimen).
  observation_chart       every reading charted by now, with retracted_at/_by/_reason/_origin if it has
                          since been marked entered in error. "Ever charted": a harm check on what was seen
                          uses this, so retracting a reading (anyone's) does not hide it.
  observation_current     the chart as it stands: what a requirement such as "charted the obs" should use,
                          so a set the agent charted and then retracted does not count.
  as at time :t           observation_chart WHERE taken_at <= :t AND (retracted_at IS NULL OR retracted_at > :t)

  origin, origin_action_id on note, observation, medication, administration, referral, order, assessment, job,
  job_event and note_revision:
      agent   written by the agent's own operation; origin_action_id is that action (audit.action_id)
      staff   written by a scripted response; origin_action_id is the agent action that set it off (a nurse
              giving what the agent prescribed), or NULL when no agent action did (a timed deterioration)
      record  loaded with the bundle
  So "the agent caused it" is origin_action_id IS NOT NULL; "the agent did it" is origin = 'agent'.
"""

import json
import re

from eprbench import vitals
from eprbench.world import World

EVENT_IDS = ("referral_id", "medication_id", "administration_id", "order_id", "note_id", "assessment_id", "job_id", "action_id")
# Operation fields that are free text for the reader: no rule keys on them, and a pending event does not keep them.
FREE_TEXT = ("reason", "clinical_details", "body", "title", "description", "outcome", "details", "indication", "note")


def fire_due(world: World) -> None:
    """Apply, in time order, everything that has fallen due: pending responses to earlier events, timed rules
    (skipped if a rule named in `unless` has taken effect by then, or the patient is not in the encounter the rule
    needs), and samples scheduled for collection. At the same moment a response comes first, then a timed rule, then
    the sample, so a sample is taken after whatever happened at that moment."""
    while True:
        due = world.conn.execute(
            """SELECT min(t) FROM (SELECT min(at) AS t FROM scenario_rule WHERE on_op IS NULL AND applied_at IS NULL AND skipped = 0
               UNION ALL SELECT min(due_at) FROM scenario_pending WHERE applied_at IS NULL AND cancelled_at IS NULL
               UNION ALL SELECT min(collect_at) FROM "order" WHERE status = 'active' AND collected_at IS NULL)"""
        ).fetchone()[0]
        if due is None or due > world.now:
            return  # the usual case: nothing has fallen due
        timed = world.one(
            "SELECT * FROM scenario_rule WHERE on_op IS NULL AND applied_at IS NULL AND skipped = 0 AND at <= ? ORDER BY at, id LIMIT 1", (world.now,)
        )
        pending = world.one(
            "SELECT * FROM scenario_pending WHERE applied_at IS NULL AND cancelled_at IS NULL AND due_at <= ? ORDER BY due_at, id LIMIT 1", (world.now,)
        )
        sample = world.one(
            """SELECT * FROM "order" WHERE status = 'active' AND collected_at IS NULL AND collect_at <= ? ORDER BY collect_at, id LIMIT 1""", (world.now,)
        )
        first = min((t, rank) for t, rank in ((pending and pending["due_at"], 0), (timed and timed["at"], 1), (sample and sample["collect_at"], 2)) if t is not None)
        if first[1] == 0:
            rule, t, event = world.one("SELECT * FROM scenario_rule WHERE id = ?", (pending["rule_id"],)), pending["due_at"], json.loads(pending["event"])
            why = suppression(world, rule, t, after=True)
            if why:
                world.conn.execute("UPDATE scenario_pending SET cancelled_at = due_at WHERE id = ?", (pending["id"],))
                _record(world, rule, t, event, suppressed=why)
            elif world.conn.execute("UPDATE scenario_pending SET applied_at = due_at WHERE id = ? AND applied_at IS NULL", (pending["id"],)).rowcount:
                _apply(world, rule, t, event)
        elif first[1] == 1:
            why = suppression(world, timed, timed["at"])
            if why:
                world.conn.execute("UPDATE scenario_rule SET skipped = 1 WHERE id = ?", (timed["id"],))
                _record(world, timed, timed["at"], {}, suppressed=why)
            else:
                _apply(world, timed, timed["at"], {})
        else:
            from eprbench import ops  # ops imports this module

            ops.collect_sample(world, sample, sample["collect_at"])


def took_effect(world: World, patient_id: int, names: str | list | None, by: int) -> str | None:
    """The first of the named rules (a JSON list, a list, or one name) to have taken effect by `by`, if any."""
    if not names:
        return None
    names = json.loads(names) if isinstance(names, str) and names.startswith("[") else [names] if isinstance(names, str) else names
    marks = ", ".join("?" * len(names))
    row = world.one(f"SELECT name FROM scenario_rule WHERE patient_id = ? AND name IN ({marks}) AND applied_at <= ? ORDER BY applied_at, id LIMIT 1",
                    (patient_id, *names, by))  # fmt: skip
    return row["name"] if row else None


def suppression(world: World, rule: dict, t: int, after: bool = False) -> str | None:
    """Why a rule's effects must not take place at `t`, or None: a rule named in `unless` has taken effect, or the
    patient is not in the encounter the rule needs. A delayed effect (`after`) whose rule says after_discharge
    'deliver' still arrives once the patient has left (a letter, a reply that follows the patient). A world saved before
    effects were gated again ('legacy') keeps behaving as it did, so regrading it reproduces it."""
    if after and rule["after_discharge"] == "legacy":
        return None
    if name := took_effect(world, rule["patient_id"], rule["unless"], t):
        return f"unless: {name} had taken effect"
    if not in_encounter(world, rule, t) and not (after and rule["after_discharge"] == "deliver"):
        return "the patient had left the encounter" if after else "the patient was not in the encounter the rule needs"
    return None


def _record(world: World, rule: dict, t: int, event: dict, variant: int | None = None, suppressed: str | None = None) -> None:
    world.insert("scenario_application", rule_id=rule["id"], patient_id=rule["patient_id"], event=json.dumps(event), applied_at=t,
                 origin_action_id=event.get("action_id"), variant=variant, suppressed=suppressed)  # fmt: skip


def in_encounter(world: World, rule: dict, t: int) -> bool:
    """Whether the rule is active at `t` for its patient: an encounter is open (encounter 'current'), the encounter it
    names is open, or it needs none (NULL)."""
    if rule["encounter"] is None:
        return True
    named = "" if rule["encounter"] == "current" else "AND ref = ?"
    params = (rule["patient_id"], t, t) + (() if rule["encounter"] == "current" else (rule["encounter"],))
    return world.one(f"SELECT 1 FROM encounter WHERE patient_id = ? AND admitted_at <= ? AND (discharged_at IS NULL OR discharged_at > ?) {named}", params) is not None


def fire_matching(world: World, op_name: str, event: dict, t: int | None = None) -> None:
    """Fire rules triggered by a successful write at time `t` (default now). `event` is the operation's arguments
    and result, with the patient. A rule with no delay takes effect at once; otherwise its effects wait in
    scenario_pending. A once-only rule is claimed atomically, so a rule fired by a nested event (a responding
    doctor's prescription) is not fired again by the outer one. Stopping a prescription cancels its pending responses."""
    t = world.now if t is None else t
    if op_name == "stop_medication" and event.get("medication_id") is not None:
        cancel_pending(world, event["medication_id"], t)
    rules = world.q(
        """SELECT * FROM scenario_rule
           WHERE (repeat = 1 OR fired_at IS NULL) AND skipped = 0 AND on_op = ? AND patient_id = ?
             AND (not_after IS NULL OR not_after >= ?) AND (not_before IS NULL OR not_before <= ?) ORDER BY id""",
        (op_name, event.get("patient_id"), t, t),
    )
    for rule in rules:
        if not matches(world, rule, event, t) or suppression(world, rule, t):
            continue
        claim = "UPDATE scenario_rule SET fired_at = coalesce(fired_at, ?) WHERE id = ?" + ("" if rule["repeat"] else " AND fired_at IS NULL")
        if not world.conn.execute(claim, (t, rule["id"])).rowcount:
            continue  # a once-only rule already fired by a nested event
        # what the response is to: the event's identifiers and structured values (variants are chosen on them later),
        # and the encounter it belongs to
        ids = {k: v for k, v in event.items() if v is not None and (k in EVENT_IDS or (k not in FREE_TEXT and isinstance(v, str | int | float)))}
        ids["_encounter"] = encounter_at(world, rule["patient_id"], t)
        if rule["delay_s"] == 0:
            _apply(world, rule, t, ids)
        else:
            world.insert(
                "scenario_pending", rule_id=rule["id"], patient_id=rule["patient_id"], event=json.dumps(ids), medication_id=ids.get("medication_id"),
                fired_at=t, due_at=t + rule["delay_s"],
            )  # fmt: skip


def cancel_pending(world: World, medication_id: int, t: int) -> None:
    """Cancel the responses still waiting on a prescription that has been stopped. A once-only rule they belonged to
    can then be triggered again by another event."""
    for p in world.q(
        """SELECT p.id, p.rule_id, r.repeat FROM scenario_pending p JOIN scenario_rule r ON r.id = p.rule_id
           WHERE p.medication_id = ? AND p.applied_at IS NULL AND p.cancelled_at IS NULL""",
        (medication_id,),
    ):
        world.conn.execute("UPDATE scenario_pending SET cancelled_at = ? WHERE id = ?", (t, p["id"]))
        if not p["repeat"]:
            world.conn.execute("UPDATE scenario_rule SET fired_at = NULL WHERE id = ? AND applied_at IS NULL", (p["rule_id"],))


def matches(world: World, rule: dict, event: dict, t: int) -> bool:
    """Does an event meet a rule's conditions at `t`? Regular expressions over fields (`match`, older bundles), and
    structured conditions (`when`, see conditions_hold)."""
    patterns = json.loads(rule["match"] or "{}")
    if not all(re.search(rx, str(event.get(field, "")), re.IGNORECASE) for field, rx in patterns.items()):
        return False
    return conditions_hold(world, json.loads(rule["condition"] or "{}"), event, rule["patient_id"], t)


def conditions_hold(world: World, conditions: dict, event: dict, patient_id: int, t: int) -> bool:
    """Structured conditions on an event and the record at `t`: a regimen the event meets (for an administration, the
    dose actually given; for a prescription, the dose as written), the hospital service its referral reached, the
    referral pathway chosen ("" for none), the structured fields of what it recorded (`fields`), what the record holds
    (`record`: a reported result of a test, or none), a field's exact value (or one of a list of values), or a
    number's range ({"min", "max"})."""
    from eprbench.ops import request_matches  # ops imports this module

    for key, want in conditions.items():
        if key == "record":
            for predicate, codes in want.items():
                codes = codes if isinstance(codes, list) else [codes]
                marks = ", ".join("?" * len(codes))
                found = world.one(f"SELECT 1 FROM result WHERE patient_id = ? AND orderable_code IN ({marks}) AND reported_at <= ?", (patient_id, *codes, t))
                if (found is None) == (predicate == "result"):
                    return False
            continue
        if key == "regimen":
            if event.get("administration_id") is not None:
                if not given_in_regimen(world, event["administration_id"], want):
                    return False
            elif event.get("medication_id") is None or not meets_regimen(world, event["medication_id"], want):
                return False
        elif key in ("service", "pathway"):
            ref = world.one(f"SELECT {key} FROM referral WHERE id = ?", (event.get("referral_id"),))
            if ref is None or not request_matches({key: want}, {key: ref[key] or ""}):
                return False
        elif key == "fields":
            if not request_matches(want, event_fields(world, event)):
                return False
        elif isinstance(want, dict):
            try:
                value = float(event.get(key))
            except (TypeError, ValueError):
                return False
            if (want.get("min") is not None and value < want["min"]) or (want.get("max") is not None and value > want["max"]):
                return False
        elif not request_matches({key: want}, event):
            return False
    return True


def event_fields(world: World, event: dict) -> dict:
    """The structured fields of what an event recorded: a request's form, a referral pathway's form, or a form
    assessment's answers."""
    if event.get("assessment_id") is not None:
        return {a["field"]: a["value"] for a in world.q("SELECT field, value FROM assessment_answer WHERE assessment_id = ?", (event["assessment_id"],))}
    for key, sql in (("referral_id", "SELECT pathway_details AS f FROM referral WHERE id = ?"), ("order_id", 'SELECT request_details AS f FROM "order" WHERE id = ?')):
        if event.get(key) is not None:
            row = world.one(sql, (event[key],))
            return json.loads(row["f"] or "{}") if row else {}
    return {}


def meets_regimen(world: World, medication_id: int, regimen: str) -> bool:
    """Whether a prescription as written meets a named regimen: drug and dose basis, the whole of any dose range within
    the limits, doses per day (BD = every 12 hours), route, an eligible kind (regular or stat unless the regimen says
    otherwise; never PRN or TTO by default) and, for per-kg limits, the weight charted when it was prescribed. The same
    predicate as the regimen_match view, which grading can query directly."""
    return world.one("SELECT 1 FROM regimen_match WHERE medication_id = ? AND regimen = ?", (medication_id, regimen)) is not None


def given_in_regimen(world: World, administration_id: int, regimen: str) -> bool:
    """Whether a dose recorded as given meets a regimen: the amount actually given, with the prescription's frequency,
    route, kind and weight (the administration_regimen_match view)."""
    return world.one("SELECT 1 FROM administration_regimen_match WHERE administration_id = ? AND regimen = ?", (administration_id, regimen)) is not None


def regimen_medications(world: World, patient_id: int, regimen: str, active_at: int | None = None) -> list[int]:
    """The patient's prescriptions that meet a regimen, oldest first; with `active_at`, only those started by then
    and not stopped."""
    return [
        r["medication_id"]
        for r in world.q(
            """SELECT medication_id FROM regimen_match WHERE patient_id = ? AND regimen = ?
                 AND (? IS NULL OR (started_at <= ? AND (stopped_at IS NULL OR stopped_at > ?))) ORDER BY started_at, medication_id""",
            (patient_id, regimen, active_at, active_at, active_at),
        )
    ]


def encounter_at(world: World, pid: int, t: int) -> int | None:
    enc = world.one(
        """SELECT id FROM encounter WHERE patient_id = ? AND admitted_at <= ?
           AND (discharged_at IS NULL OR discharged_at > ?) ORDER BY admitted_at DESC LIMIT 1""",
        (pid, t, t),
    )
    return enc["id"] if enc else None


def _apply(world: World, rule: dict, t: int, event: dict) -> None:
    from eprbench import ops  # ops imports this module

    pid = rule["patient_id"]
    world.conn.execute("UPDATE scenario_rule SET fired_at = coalesce(fired_at, ?), applied_at = coalesce(applied_at, ?) WHERE id = ?", (t, t, rule["id"]))
    variants = json.loads(rule["variants"] or "[]")
    variant = next((i for i, v in enumerate(variants) if conditions_hold(world, v["when"], event, pid, t)), None)
    _record(world, rule, t, event, variant)
    effects = variants[variant]["effects"] if variant is not None else json.loads(rule["effects"])
    # a reply delivered after the patient has left is filed against the encounter it answers
    enc_id = encounter_at(world, pid, t) or event.get("_encounter")

    # Bend each signal's trajectory from where it is now, discarding what was going to happen.
    for signal, frames in effects.get("vitals", {}).items():
        track = world.one("SELECT * FROM vitals_track WHERE patient_id = ? AND signal = ?", (pid, signal))
        if track is None:
            continue
        kept = [f for f in json.loads(track["keyframes"]) if f[0] < t]
        new = kept + [[t, vitals.trend_at(track["keyframes"], t)]] + [[t + off, v] for off, v in frames if off > 0]
        world.conn.execute("UPDATE vitals_track SET keyframes = ? WHERE id = ?", (json.dumps(new), track["id"]))

    # What the named tests show from now on: earlier windows close here, and later ones no longer apply.
    windows = effects.get("result_windows", []) + effects.get("latent_results", [])
    for code in dict.fromkeys(w["orderable_code"] for w in windows):
        world.conn.execute(
            "DELETE FROM latent_result WHERE patient_id = ? AND orderable_code = ? AND valid_from >= ?", (pid, code, t)
        )
        world.conn.execute(
            "UPDATE latent_result SET valid_to = ? WHERE patient_id = ? AND orderable_code = ? AND (valid_to IS NULL OR valid_to > ?)",
            (t, pid, code, t),
        )
    for w in windows:
        start = t + (w.get("offset_s") or 0)
        end = start + w["duration_s"] if w.get("duration_s") is not None else None
        payload = {k: w.get(k) for k in ("report_text", "reported_by", "items")} | ({"branches": w["branches"]} if w.get("branches") else {})
        world.insert("latent_result", patient_id=pid, orderable_code=w["orderable_code"], valid_from=start, valid_to=end, payload=json.dumps(payload))

    update = effects.get("referral_update")
    if update and event.get("referral_id") is not None:
        world.conn.execute(
            "UPDATE referral SET status = ?, responded_at = ?, responded_by = ?, responder_role = ?, response = ? WHERE id = ?",
            (update["status"], t, update["by"], update.get("role"), update["text"], event["referral_id"]),
        )

    # Rows written by staff in response, linked to the agent action that set them off (none for a timed rule).
    staff = {"origin": "staff", "origin_action_id": event.get("action_id")}
    for n in effects.get("notes", []):
        world.insert(
            "note",
            **staff,
            patient_id=pid,
            encounter_id=enc_id,
            kind=n["kind"],
            title=n["title"],
            author=n["author"],
            author_role=n.get("author_role"),
            written_at=t + n["offset_s"],
            body=n["body"],
        )

    for s in effects.get("observations", []):
        for kind, value in s["values"].items():
            numeric = not isinstance(value, str)
            world.insert(
                "observation",
                **staff,
                patient_id=pid,
                encounter_id=enc_id,
                taken_at=t + s["offset_s"],
                kind=kind,
                value=value if numeric else None,
                value_text=None if numeric else value,
                recorded_by=s.get("recorded_by"),
            )

    # A member of staff prescribes, as a responding doctor might; rules on prescribing then see it.
    for m in effects.get("medications", []):
        kind = m.get("kind", "regular")
        drug = world.one("SELECT * FROM drug WHERE name = ?", (m["drug"],))
        product = world.one("SELECT * FROM product WHERE name = ?", (m["product"],)) if m.get("product") else None
        med_id = world.insert(
            "medication", patient_id=pid, encounter_id=enc_id, kind=kind, drug=m["drug"], product=m.get("product"), dose=m["dose"], route=m["route"],
            frequency=m["frequency"], indication=m.get("indication"), prescriber=m["prescriber"], started_at=t,
            **ops.prescription_facts(world, pid, m["dose"], m["frequency"], kind, product, drug, t), **staff,
        )  # fmt: skip
        fire_matching(world, "prescribe_medication", {**m, "patient_id": pid, "medication_id": med_id, "action_id": event.get("action_id")}, t)

    # A nurse gives the prescription that triggered the rule (the bottom of a range), unless by now it has been
    # stopped, is a discharge prescription, has already been given, or belongs to an admission that has ended;
    # rules on administration then see it.
    give = effects.get("administer")
    if give and event.get("medication_id") is not None:
        med = world.one("SELECT * FROM medication WHERE id = ?", (event["medication_id"],))
        given = world.one("SELECT 1 FROM administration WHERE medication_id = ? AND status = 'given' AND at >= ?", (med["id"], med["started_at"]))
        if med["kind"] != "tto" and (med["stopped_at"] is None or med["stopped_at"] > t) and not given and ops.admission_active(world, med, t):
            dose = ops.bottom_of_range(world, med)
            admin_id = world.insert(
                "administration", medication_id=med["id"], patient_id=pid, at=t, status="given", recorded_by=give["recorded_by"], note=give.get("note"),
                **ops.given_dose(world, med, dose), **staff,
            )  # fmt: skip
            fire_matching(world, "record_administration", {"medication_id": med["id"], "status": "given", "drug": med["drug"], "patient_id": pid,
                                                            "administration_id": admin_id, "action_id": event.get("action_id")}, t)  # fmt: skip
