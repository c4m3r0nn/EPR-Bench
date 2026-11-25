**NO-GO for paid runs: the £10 ceiling is not reliable.** Reviewed the eight non-merge commits. No files edited, `.env*` read, remote access, paid calls or real agent CLI runs. Using the requested Python environment, **130 tests passed**; additional probes used mocks and in-memory data. Browser JavaScript was exercised in Node; Chromium was not launched.

Ranked findings:

1. **P1 — Failed requests and crashes can leave spending unaccounted for.** [episode.py:439](src/eprbench/episode.py:439), [spend.py:210](src/eprbench/spend.py:210). Charging happens only after receiving and parsing usage. A provider can complete a request whose response is lost; retries repeat it against unchanged funds. Probe: three attempts, only one recorded, permitting a potential £0.03006 charge against £0.02. A crash before ledger append likewise loses the charge; there is no `fsync`. **Fix:** durably reserve the conservative maximum before each attempt, retain reservations for uncertain outcomes, then reconcile confirmed usage.

2. **P1 — Shared and resumed ledgers do not enforce an aggregate cap.** [spend.py:122](src/eprbench/spend.py:122), [spend.py:149](src/eprbench/spend.py:149). Each process reads the balance once, without locking or atomic reservations. Two budgets both approved spending against the same balance. Missing ledgers silently mean zero; omitted ledgers reset spending on resume. A ledger containing `{"kind":"request","gbp":NaN}` made a £1,000 request pass a £10 check; negative amounts also inflate availability. **Fix:** transactional shared reservations, mandatory ledger identity on resume, explicit first-time creation, and strict finite/nonnegative validation.

3. **P1 — “Worst case” is an estimate, and unsafe prices pass validation.** [spend.py:175](src/eprbench/spend.py:175), [spend.py:51](src/eprbench/spend.py:51). Characters/3 cannot bound arbitrary Unicode or tool-result tokens. Images use a configurable allowance, defaulting to 1,600 regardless of provider; provider-injected computer/tool overhead has no allowance. Zero token prices and zero image tokens are accepted. `currency="GBP", gbp_per_unit=0.01` is accepted, understating costs 100-fold. The maximum input rate excludes `cache_read`. **Fix:** validated provider/model-specific upper bounds, overhead allowances, strictly positive applicable rates/image bounds, GBP conversion fixed at 1, and conservative foreign-currency conversion. Reject unsupported pricing configurations.

4. **P1 — Spending protection is optional, including for plans.** [__main__.py:205](src/eprbench/__main__.py:205). Mocked ordinary and `run --plan` invocations both reached execution with `spend=None`. Gemini is rejected only when a budget is supplied; otherwise it runs unmetered. **Fix:** refuse paid SDK execution by default without explicit authorization, total ceiling, validated prices and a durable ledger. Apply this before client construction.

5. **P2 — UI rendering over-credits information never delivered in a screenshot.** [ui.py:65](src/eprbench/server/ui.py:65), [patient.html:180](src/eprbench/server/templates/patient.html:180). Template rendering marks every observation set read, including old weight rows below the viewport. Two HTTP page requests, with no screenshot, earned the DVT renal/weight bonus. Claude can navigate away without ever requesting that screen. **Fix:** record visible entries when screenshots are delivered, or explicitly grade page availability rather than claimed reading.

6. **P2 — Plans do not fully pin effective execution.** [runner.py:62](src/eprbench/runner.py:62), [runner.py:152](src/eprbench/runner.py:152). A plan’s OpenAI `effort="high"` disappears before the harness call. Model aliases remain mutable, and resuming after task/grader changes retains the same plan hash. **Fix:** pass and record effective reasoning settings; freeze and verify model versions, task/data/grader hashes and pricing configuration.

7. **P2 — Resume checks existence, not complete evidence.** [runner.py:133](src/eprbench/runner.py:133). A valid metadata/result pair causes an episode to be skipped even with empty or corrupt report/database files; the mocked probe confirmed neither is inspected. **Fix:** atomically publish a completion manifest after validating and durably writing evidence and ledger reconciliation.

8. **P2 — Browser-imported comments still disclose human identities.** [page.html:151](src/eprbench/viewer/page.html:151). A synthetic clinician name survived blind-review import while vendor/path text was scrubbed. Browser sanitising lacks Python’s identity replacements. **Fix:** drop comments by default in shared views, or sanitise through the private identity-aware build process.

Other checks: bounded Claude/OpenAI request sites, retries and final chances pass through `Episode.send`; the reply-page screenshot fix is present. Screenshot/zoom caps symmetrically produce `action_limit`; ordinary screenshot loops also encounter request/time limits. Automatic screenshots remain uncapped separately. Reasoning beyond OpenAI’s output bound was **not** substantiated by installed SDK definitions.

Read lists distinguish titles/counts from bodies; summaries do not expose note bodies. Reads advance simulated time by zero. Empty magnesium lookup provides absence information but records no read. Migration reconstructs tool/API reads, not old UI reads; `horizon_world_reproduced=None` correctly means unverified after migration. The 24-episode counterbalance probe produced six pairs starting with each interface.

**Overshoot:** no defensible finite monetary bound exists with these failures and unspecified real prices. A realistic restart pattern alone permits **£240 total—£230 over budget** across 24 episodes, each using a fresh £10 balance. This is an exposure example, not an upper bound.

**Must-fix before ANY paid request:**

- Findings 1–4; enforce one durable £10 aggregate reservation budget with verified prices and conversion.
- Obtain explicit owner authorization; none is established here.

**Should-fix:**

- Findings 5–8; resolve A19/B25/B26 before freezing preview outcomes.
- Harden [release_scan.py:72](scripts/release_scan.py:72): `.envproduction` is missed, as are unquoted generic secrets in probes. Scan history/artifacts separately. The filtered tree scan returned 141 findings, not the checklist’s 135.