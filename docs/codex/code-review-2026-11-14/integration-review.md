Part 1 — verification against `master...HEAD`

| Item | Status | Evidence |
|---|---|---|
| Overall deadline; retries counted | **FIXED** | Each attempt receives the remaining allowance and increments requests; SDK retries are disabled. [episode.py:368](src/eprbench/episode.py:368), [agents/__init__.py:31](src/eprbench/agents/__init__.py:31). |
| CLI final chance | **FIXED within documented capabilities** | Separate grace allowance; late answers rejected. Claude’s own turn limit ends without another chance; Codex requests remain uncounted. [cli_agent.py:202](src/eprbench/agents/cli_agent.py:202). |
| Saved world equals graded world | **FIXED** | Due rules fire before snapshot; grading restores those exact saved bytes. [episode.py:562](src/eprbench/episode.py:562). |
| Provider status precedes calls | **FIXED** | Failed responses terminate before dispatch, including responses containing `submit`. [openai_tools.py:94](src/eprbench/agents/openai_tools.py:94), [gemini_tools.py:75](src/eprbench/agents/gemini_tools.py:75), [claude_tools.py:105](src/eprbench/agents/claude_tools.py:105). |
| Cancelled requests recorded | **FIXED** | `BaseException` capture and unconditional recording cover cancellation. [local.py:105](src/eprbench/server/local.py:105). |
| Value-only redaction | **FIXED** | Evidence uses known-value replacement; pattern matching is confined to CLI output, as documented. [episode.py:295](src/eprbench/episode.py:295), [cli_agent.py:135](src/eprbench/agents/cli_agent.py:135). |
| Server lock scope | **FIXED** | Body reception precedes the application lock; response transmission follows release. [local.py:63](src/eprbench/server/local.py:63). |
| `context_limit` | **PARTIAL across all harnesses** | SDK paths classify it correctly. CLI termination recognizes `error_max_turns`, then falls back to exit status; explicit context exhaustion is not mapped. [cli_agent.py:243](src/eprbench/agents/cli_agent.py:243). |
| `regrade --saved-task` | **FIXED** | Preserves the saved definition, including retired tasks. [episode.py:623](src/eprbench/episode.py:623). |
| EPREnv content key | **FIXED** | Serialized task plus cache generation determines reuse. [env.py:36](src/eprbench/env.py:36). |
| Hash once; explicit invalidation | **FIXED** | Cached bytes and hashes stay together; invalidation clears sources/snapshots and advances generation. [loader.py:481](src/eprbench/loader.py:481), [tasks.py:96](src/eprbench/tasks.py:96). |
| Per-episode comparability | **PARTIAL** | Tool/UI harnesses populate surface-specific fields. CLI retains the generic block, including misleading “first model request” timing text, despite starting at process launch. [episode.py:254](src/eprbench/episode.py:254), [cli_agent.py:195](src/eprbench/agents/cli_agent.py:195). |

Eight targeted harness checks passed, including retry accounting, provider-status precedence, context classification, task-change invalidation and malformed-call accounting.

Part 2 — merged interactions

**No additional merge-created production defect was reproduced.** The known stale server-error test is a confirmed merge interaction: `[1]` now fails argument validation with 422, so it no longer exercises the 500 recorder path. Repair it by injecting an actual internal exception, preserving both validation and error-recording coverage. [test_contract.py:319](tests/test_contract.py:319).

The remaining integration concerns, ranked by impact, are:

1. **P2 — UI operation results are missing from `audit.jsonl`.** Tool runs attach operation results; API runs attach the JSON response envelope. UI requests never receive the API action number that gates result attachment, and the UI wrapper discards write results before redirecting. A successful acknowledgement therefore records its resolved patient and success, but omits its returned result. I reproduced this for successful and wrong-patient acknowledgements; API evidence contained results, UI evidence did not. [local.py:115](src/eprbench/server/local.py:115), [ui.py:67](src/eprbench/server/ui.py:67), [episode.py:581](src/eprbench/episode.py:581). **This already exists on the harness parent; it is an unresolved integration gap, not a newly caused merge regression.**

2. **P2 — equivalent ownership-check capability does not mean identical automatic protection.** All three tool schema adapters preserve optional `patient_id`, and dispatch passes it unchanged. API does likewise. Patient-scoped UI routes supply the displayed patient automatically. Consequently, `acknowledge_result(result_id=1, patient_id=4)` is rejected everywhere, but omitting `patient_id` allows the tool/API call. The ward-wide UI job-completion route deliberately omits it. [ops.py:98](src/eprbench/ops.py:98), [ui.py:310](src/eprbench/server/ui.py:310), [ui.py:227](src/eprbench/server/ui.py:227). **No harness drops the new field; this is an existing interface-policy asymmetry.**

Validation covered **19 oracles plus all 260 adversarial trajectories**, using actual `Episode` serialization/closure with filesystem writes redirected to memory because temporary files were prohibited. Both current-task and saved-task regrading reproduced every complete report: **558 regrades, zero mismatches**, excluding only the evidence-only canary key. All oracles passed without safety findings. Separate API/UI evidence probes also regraded identically.

Task checks and findings are intentionally different outputs: namesake prescribing followed by stopping remains `unsafe=true` with no generic finding; allergy exposure followed by stopping produces both an unsafe check failure and `allergy_override`. These are reproducible policy distinctions, not evidence drift. [tasks.py:168](src/eprbench/tasks.py:168), [safety.py:1](src/eprbench/safety.py:1).

Grading reads simulated database time, task content and answer—not wall-clock or evidence files. Frequency recognition remains duplicated between Python normalization and task SQL, but the exercised trajectories showed no conflicting result. `DOSE_UNITS` has one definition consumed by UI; the canary likewise has one shared definition. [tasks.py:138](src/eprbench/tasks.py:138), [ops.py:1197](src/eprbench/ops.py:1197), [medrec-withhold-001.json:17](src/eprbench/data/tasks/medrec-withhold-001.json:17).

Part 3 — readiness

**Before (a), subscription CLI dry runs:** no production-code must-fix identified. Run from an environment permitting the existing isolation check and evidence writes, with subscription authentication configured. Measure API actions and elapsed time; Codex model-request counts cannot be inferred from the current record. Keep these runs explicitly unscored.

**Before (b), a paid paired preview:**

- Capture UI operation results against their audit IDs.
- Specify and validate the intended-patient protection policy for the paired surfaces.
- Obtain clinician approval for the three cases and resolve relevant clinical/scoring xfails.
- Finish the known test repair and validate evidence/regrading through the selected tool and browser harnesses.
- Freeze model/settings, surface-specific budgets, and truncation policy.

CLI context classification and comparability metadata should be completed before treating CLI runs as scored comparative evidence; neither blocks exploratory unscored runs.

No files were edited; no `.env*`, remotes, paid APIs or real agent task runs were used.

**READY FOR DRY RUNS — must-fix list before (a): none.**