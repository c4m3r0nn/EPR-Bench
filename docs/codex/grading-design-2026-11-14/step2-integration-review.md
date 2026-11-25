Read-only review completed using the specified interpreter, `PYTHONPATH=src`, and `PYTHONDONTWRITEBYTECODE=1`. **81 targeted tests passed**, including 19 oracles and 348 frozen legacy grades. I did not rerun the full suite. No files edited, `.env*` read, remotes accessed, paid APIs called, or agent CLIs run.

**Part 1 — nine runtime findings**

| Finding | Status | Evidence and probe |
|---|---|---|
| Dose ranges and rounding | **PARTIAL** | Whole-range matching and explicit administration doses work. However, nurse formatting uses `:g`: an accepted `5.0000001–10 mg` prescription becomes `5 mg`, causing both waiting and horizon assessment to raise `OpError`. [scenario.py:267](src/eprbench/scenario.py:267), [schema.sql:307](src/eprbench/schema.sql:307). |
| Cancelled prescription/TTO consuming nurse response | **FIXED** | Stopping cancels the pending response; replacement prescriptions receive their own response. Default regimens exclude TTOs. Both replacement scenarios passed. [scenario.py:104](src/eprbench/scenario.py:104), [schema.sql:298](src/eprbench/schema.sql:298). |
| Treatment after discharge | **PARTIAL** | Scripted nursing checks the encounter, but direct `record_administration` does not. Prescribe → discharge → administer still succeeds and triggers treatment recognition. [scenario.py:263](src/eprbench/scenario.py:263), [ops.py:1723](src/eprbench/ops.py:1723). |
| Double-firing once-only rules | **FIXED** | Conditional claims prevent an outer event firing a rule already claimed by a nested staff prescription. Regression passed. [scenario.py:91](src/eprbench/scenario.py:91). |
| Observation corrections rewriting treatment recognition | **FIXED** | Prescriptions retain the weight used when written. Correcting that observation leaves existing recognition unchanged; subsequent prescriptions use the corrected record. [ops.py:1563](src/eprbench/ops.py:1563). |
| Differing patient requirements by interface | **FIXED** | Tool, API and UI use `agent_ctx`, requiring patient context for object operations. Omission probes were rejected. [ops.py:50](src/eprbench/ops.py:50), [ops.py:163](src/eprbench/ops.py:163). |
| Dose basis and frequency equivalents | **FIXED** | Salt/base identity is preserved; BD and equivalent twelve-hour frequencies match identically. [ops.py:1527](src/eprbench/ops.py:1527), [schema.sql:307](src/eprbench/schema.sql:307). |
| Unit changes manufacturing normals; fallback age | **PARTIAL** | Supported conversions and configurable age limits work. Unsupported units still manufacture normals: Hb `5.7 mmol/L` produced `138 g/L`. Lint warns but execution proceeds. [ops.py:993](src/eprbench/ops.py:993). |
| Future correction leak | **FIXED** | Future correction metadata stays hidden; an earlier agent correction succeeds without revealing the scripted correction. [ops.py:612](src/eprbench/ops.py:612), [ops.py:690](src/eprbench/ops.py:690). |

**Part 2 — grading/runtime interactions**

- **Staff attribution: helpers work; universal protection does not.** `agent_op`/`agent_write` exclude scripted nurse administrations, registrar prescriptions and referral updates. Probes showed only the originating agent operations. However, raw-table checks bypass that protection: injecting an unrelated timed staff co-amoxiclav prescription into the v2 antibiotic example generated `penicillin_prescribed` despite zero agent writes. Case SQL must distinguish independent staff actions from consequences of agent decisions. [assessment.py:308](src/eprbench/assessment.py:308), [test_assessment_v2.py:74](tests/test_assessment_v2.py:74).

- **Horizon versus waiting: equivalent in the exercised scenarios.** Projection and stepped waits produced identical prescriptions, administrations, referrals, pending rules, observations, trajectories and result windows. Pre-response samples remained unchanged; subsequent samples agreed. The submitted world stayed untouched. The range-formatting defect above breaks both paths, including grading itself. [assessment.py:348](src/eprbench/assessment.py:348).

- **Both regimen views execute inside task SQL at submission and horizon.** An Apixaban prescription matched immediately; its administration matched only after the projected nurse response. These are structural views: authors must supply appropriate time, active-status and attribution conditions. [schema.sql:304](src/eprbench/schema.sql:304), [schema.sql:318](src/eprbench/schema.sql:318).

- **Compatibility reproduced:** all 19 packaged oracles succeeded with mandatory patient context, and all **348 frozen legacy grades** matched. This establishes compatibility, including existing scoring weaknesses. [test_assessment_v2.py:421](tests/test_assessment_v2.py:421).

- **Stored result/action ID did not change grading.** Replacing both preserved checks, score, success, unsafe status, findings and tier; the horizon snapshot hash changed appropriately. Helpers expose neither field. Custom SQL explicitly querying `audit` could nevertheless depend on them. [assessment.py:309](src/eprbench/assessment.py:309).

- **Corrections can defeat poorly specified grading.** A custom harm check excluding observations marked in error changed **harmful → complete** after the agent retracted an abnormal staff observation. Conversely, packaged `chart-obs-001` still passed after its required observation set was retracted. Historical harm needs preserved evidence; current-chart requirements need correction-aware predicates. [ops.py:684](src/eprbench/ops.py:684), [chart-obs-001.json:13](src/eprbench/data/tasks/chart-obs-001.json:13).

- **Explicit administration doses resisted the tested forgery.** Against `0–10 mg`, giving `0` or `2.5 mg` did not satisfy treatment recognition; `5 mg` did; `11 mg` was rejected. Dose-dependent checks must use actual administration quantities. The discharge bypass remains a separate way to obtain invalid treatment credit. [ops.py:1707](src/eprbench/ops.py:1707).

**Part 3 — must-fixes before validated cases and paid preview**

Before accepting the three clinician-validated v2 cases:

- Remove nurse dose rounding and enforce encounter eligibility on direct administrations.
- Prevent unsupported-unit fallback from silently returning a normal result.
- Specify and test correction handling and causal staff attribution in each case’s grading.
- Obtain clinician approval for regimens, response delays, horizons, permitted escalation, refusal criteria and harm thresholds; resolve relevant strict xfails.
- Add complete, partial, inactive, harmful, delayed, corrected and cancelled golden trajectories for the explicit-instruction, emergency-judgement and should-refuse cases.

Before the paid paired tool-versus-UI preview:

- Exercise those trajectories through both surfaces and verify equivalent clinical outcomes, complete evidence and reproducible regrading.
- Freeze starting snapshots, case/grader versions, model settings, budgets and truncation handling.
- Arrange clinician review of every preview episode.

**Verdict: FIXES NEEDED — nurse dose serialization, post-discharge administration, unsupported-unit fallback, and correction/provenance-aware case grading.**