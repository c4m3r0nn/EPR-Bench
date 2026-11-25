**MERGE AFTER FIXES — address findings 1–8 below.** Reviewed all three commits read-only; no `.env*`, remotes, or external APIs accessed.

1. **P1 — Bilateral imaging can silently miss known disease.** [ops.py:1283](src/eprbench/ops.py:1283), [lint.py:73](src/eprbench/lint.py:73).  
   Probe: a right-only branch reporting acute right popliteal DVT passes lint; requesting bilateral returns **“Normal study.”** Conversely, an explicit bilateral branch reporting right-sided DVT fails lint. Fix coverage validation: require explicit bilateral handling/defaults for lateral studies; permit unilateral findings within bilateral examinations. Authors must cover each supported request, including separate contrast/noncontrast orderables. Do not automatically combine arbitrary report text.

2. **P1 — Pending effects bypass execution-time gates.** [scenario.py:125](src/eprbench/scenario.py:125).  
   Probe: trigger a delayed rule, apply its `unless` suppressor, discharge, then wait. Its staff note still appears, with `encounter_id=NULL`. Pending execution calls `_apply` without rechecking either gate. Revalidate gates at effect time for encounter-bound clinical effects; provide an explicit policy for replies legitimately arriving after discharge. Authors cannot currently rely on `unless` to cancel an already queued effect.

3. **P1 — PPCI still manufactures unsupported acceptance.** [demo.json:216](src/eprbench/data/bundles/demo.json:216).  
   Probe: no visible ECG, `pathway="ppci"`, **`ecg_stemi="no"`** → “ECG reviewed … cath lab activated.” The rule checks service/urgency, ignoring the new pathway fields. Key acceptance on the intended pathway and answers. A real system may accept a clinician’s assertion without an electronically stored ECG; it must not invent having reviewed one. Authors should separately grade ECG evidence/timing where required; scripts should distinguish reported findings from verified record evidence.

4. **P2 — Empty final summaries permit discharge.** [ops.py:735](src/eprbench/ops.py:735).  
   Probe: whitespace title/body → draft → final → successful discharge. Reject whitespace-only documents on creation/finalisation. More fundamentally, `status='final'` proves signing, not clinical completeness: arbitrary nonempty text also qualifies. Authors need structured discharge requirements, not a “summary complete” predicate based solely on status. Grade current state, not superseded draft revisions; final documents correctly resist subsequent edits.

5. **P2 — HEIGHT admits non-finite values that corrupt BMI.** [ops.py:868](src/eprbench/ops.py:868).  
   Probe: `HEIGHT="inf", WEIGHT=64` produces **BMI 0**; height 1 produces BMI 640000. Reject non-finite numbers and validate configured measurement bounds. Authors must use trusted/case-supported measurements and provenance for BMI-dependent dosing; merely checking the latest agent-entered BMI permits manipulation. Plausible but false measurements require grading against case evidence, not an omniscient engine prohibition.

6. **P2 — Confirmed duplicate-request false positive.** [safety.py:63](src/eprbench/safety.py:63).  
   U&E now plus tomorrow’s round yields `duplicate_request: UE x2`. Compare noncancelled requests for the same patient, investigation and relevant structured parameters within the **same intended sampling episode/window**, rather than grouping every request since task start. Use configurable intervals for clinically meaningful repeat-testing rules; distinct daily monitoring should not automatically count as duplication.

7. **P2 — Scheduled collection lacks separate provenance.** [ops.py:1252](src/eprbench/ops.py:1252).  
   Collection updates `collected_at` on an agent-origin order and creates a result without collector/origin columns or a collection event. The order’s attribution is correct, but authors cannot independently establish who performed collection. Add collection-event provenance (`staff`, collector, timestamp, causal order/action), retaining the original requesting attribution. Do not create a fictitious agent action at sampling time.

8. **P2 — Assessment readback differs between UI and API.** [patient.html:78](src/eprbench/server/templates/patient.html:78).  
   An explicit `yes_no` answer of “no” survives in the API but disappears from the UI as “nothing ticked.” Render every answered criterion with its value; distinguish negative answers from omitted optional fields.

Case-author requirements beyond those fixes:

- Requests: check patient/encounter, cancellation, collection deadline/window and, when actual testing is required, `collected_at`/reported result. A permitted 90-day booking is not timely investigation. Join linked jobs to current order status; I could cancel an order and still mark its linked bloods job done. Preserve legitimate cancellation/history rather than banning it.
- Wells: validate criterion answers against case facts, not just the computed category. Arbitrary ticks remain possible, appropriately for a documentation interface.
- Pre-admission/outpatient replies needing no open encounter require `encounter=null`. Death has no dedicated state here; collection protection depends on closing the encounter.

Verification: **161 tests passed, one skipped**, including 19 oracles and 348 frozen grades. Nineteen actual baseline-code oracle snapshots migrated and reproduced their grades; repeated migration did not duplicate job history. Horizon copies, sample-time evaluation, UTC midnight/past-round handling, and repeating applications were deterministic in probes. Patient-ID enforcement and wrong-patient links passed. No new hidden-rule/branch/application/lint-data leak found; visible scoring criteria are appropriate. The claimed reset **+13% was not reproduced** in my limited timing run; it measured approximately 478→366 μs.