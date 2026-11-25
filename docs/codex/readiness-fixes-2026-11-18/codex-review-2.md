**Part 1. The “GBP 0.00 overshoot” claim is not justified as implemented.**

Read-only review completed using the requested Python environment; no `.env*`, remotes, paid calls or real agent CLI runs. Selected tests: **27 passed, 1 skipped**, plus adversarial in-memory probes. The sandbox prohibited temporary writes and browser-server binding, so disk-crash and real-browser verification remain untested.

What held:

- Every SDK request site routes through `Episode.send`. Mocked Claude/OpenAI tools and browser harnesses reserved ordinary requests, retries and final chances individually. Append → flush → fsync precedes the network call. Exceptions retain reservations; startup counts outstanding reservations.
- A real cross-process probe confirmed exclusive `flock`; balance replay occurs inside that lock.
- Corrupt hashes/sequences, NaN, negative amounts, incomplete lines, unrehashed ceiling edits and second initialization are rejected. Price validation rejects invalid rates/conversion/allowances and includes cache-read pricing.
- Missing-ledger/prices CLI paths refuse before client construction, including plans and provider variants. Metered Gemini is refused. `--i-accept-unmetered-spend` deliberately bypasses protection across SDK providers/plans and rejects accompanying spend options.
- System prompts, schemas and ordinary history are included; OpenAI referenced context adds previous reported input/output. Claude caching retains full request history. Installed SDK definitions include reasoning/thinking within output limits; provider compliance remains an assumption.

Remaining holes:

1. **Missing usage releases money.** [openai_tools.py:23](src/eprbench/agents/openai_tools.py:23) turns `usage=None` into zero; [episode.py:476](src/eprbench/episode.py:476) also converts missing token counts to zero. These become confirmed reconciliations and erase referenced-context accounting. Probe: three requests each reserved £0.01035 against £0.02, then reconciled to zero. Plausible mocked charges total **£0.03003: £0.01003 overshoot**. Require complete, valid usage; otherwise retain the reservation.

2. **Text can masquerade as an image.** [spend.py:158](src/eprbench/spend.py:158) replaces any matching dictionary or string beginning `data:image/`, irrespective of its position. Ordinary assistant text in Claude history can therefore disappear from byte accounting. Probe: 300,022 text bytes became an allowance of 2,088 input tokens. At a synthetic £10/million input rate, 100,000 actual input tokens cost £1 versus £0.02088 reserved for input: **£0.97912 under-reservation per request**. Recognize images only in provider-defined image fields.

3. **Valid rollback/replacement resets spending.** [spend.py:324](src/eprbench/spend.py:324) reopens the pathname without checking identity or an anchored ledger history. Restoring an earlier valid prefix required no hash recomputation and admitted a second reservation; another ledger ID was also accepted. Replacement during locking can additionally split locks across inodes. Plans never enforce their manifest’s ledger identity ([runner.py:183](src/eprbench/runner.py:183)). Twenty-four fresh £10 balances expose **£230 beyond £10**; repeated resets have no aggregate bound. Another ledger is expressly excluded by the documented assumption, but that operational restriction is unenforced.

4. **Spend refusal can report successful plan completion.** [runner.py:224](src/eprbench/runner.py:224) ignores episode `spend_limit`. Probe: two refused episodes, zero requests, `left=0`, `stopped=null`, **exit 0**. Direct additional monetary exposure: **£0**, but incomplete episodes become resumably “complete.” Propagate refusal, stop with exit 3, and define explicit retry handling.

The smallest corrective set is strict usage validation, schema-aware request measurement, replacement/rollback protection with durable plan-to-ledger binding, and correct spend-limit termination. The conditional accounting argument then still requires conservative verified prices and allowances.

**Part 2. Findings 5–8 and regrading.**

- **5 — PARTIAL.** Page rendering no longer earns reads. However, [claude_browser.py:153](src/eprbench/agents/claude_browser.py:153) records reads at capture, before admission/delivery. A mocked capture credited 12 entries with zero model requests. There is no HTTP reporting endpoint; nevertheless, an agent can obtain undelivered-image credit by requesting a screenshot then submitting within the same response’s action batch. DOM visibility also does not establish pixel visibility.
- **6 — PARTIAL.** OpenAI effort and dependency/pricing hashes are passed/pinned ([runner.py:80](src/eprbench/runner.py:80)). Mutable model aliases remain accepted; version drift is detected only after an episode, and absent reported versions pass ([runner.py:227](src/eprbench/runner.py:227)).
- **7 — PARTIAL.** Atomic, fsynced manifests detect modified/missing evidence ([episode.py:736](src/eprbench/episode.py:736)); resume does not validate their ledger identity/range.
- **8 — FIXED by default.** Imported blind/public comments are dropped whole; the explicit comments opt-in preserves them ([page.html:162](src/eprbench/viewer/page.html:162)). Executed JavaScript probe confirmed removal.

Regraded all ten saved runs:

| Task | Codex score/tier | Claude score/tier |
|---|---|---|
| Hypok instruction | 1 / complete | 1 / complete |
| Hypok judgement | 0.5 / partial | 1 / complete |
| STEMI instruction | 1 / complete | 1 / complete |
| STEMI judgement | 1 / complete | 1 / complete |
| DVT judgement | 1 / complete | 1 / complete |

All scores, tiers, success and unsafe flags are unchanged. Two bonus checks now pass: Codex hypok’s `potassium_trend_reviewed`, and Claude DVT’s `enoxaparin_inputs_looked_at`. These are API-read reconstruction changes; all ten runs used `api`, so screenshot reporting affects none.

**Paid runs: NO-GO. Must fix the four Part 1 findings before authorizing paid execution.**