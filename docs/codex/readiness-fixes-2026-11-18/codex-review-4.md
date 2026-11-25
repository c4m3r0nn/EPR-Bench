**GO for a £10 preview, conditional on a genuine provider-enforced £10 hard cap covering all preview charges.** Prepaid credit alone must not be assumed to provide that guarantee.

Read-only review of `cd4163b` on `step4/integration`. Probes used the requested Python, `PYTHONPATH=src`, `PYTHONDONTWRITEBYTECODE=1`, and `/tmp/eprbench-final-readonly-state`. Filesystem operations were simulated in memory; the disk-backed suite was not rerun. No files edited, `.env*` read, remotes contacted, paid calls, or agent CLIs run.

Paths below are under `src/eprbench/`.

1. **Missing anchors — FIXED.** [spend.py:537](src/eprbench/spend.py:537). Probe refused absent anchors. Explicit adoption allowed recovery; surviving rollback anchors still blocked adoption.
2. **Copied-ledger identity — PARTIAL.** [spend.py:489](src/eprbench/spend.py:489). Locks remain per file. Simulated interleaving of two previously anchored copies admitted £6 each against £10. Sequential stale copies are refused.
3. **Final-chance refusal — FIXED.** [episode.py:565](src/eprbench/episode.py:565). Scripted harness/CLI probe sent one request, returned `spend_limit`, exit **3**, and excluded the ending from completion.
4. **Uncertain-usage recovery — FIXED.** [spend.py:689](src/eprbench/spend.py:689). Probe: confirmed 3,000 input → missing usage retained → confirmed 3,100 accepted.
5. **Narrow overrides — FIXED.** [runner.py:220](src/eprbench/runner.py:220). Approved replacement passed; another old ID, another plan, and same-ledger hash mismatch remained refused.
6. **Harness metering — FIXED.** [agents/__init__.py:25](src/eprbench/agents/__init__.py:25). All five harnesses rejected unmetered defaults.
7. **Correct guarantee — PARTIAL.** Documentation now states assumptions, but retains overstatements below.
8. **Preview affordability — PARTIAL.** [runner.py:317](src/eprbench/runner.py:317). New estimator labelled £0.227 “worst” where two growing requests reserved £3.977 using illustrative prices. Missing evidence usage becomes zero at line 336.

**New-hole checks:** `unmetered=True` is deliberately reachable in production and recorded; CLI access requires explicit acceptance. Adoption requires operator trust, not proof of freshness. Alias probe required acknowledgement and stopped on changed versions. Constructed Playwright-compatible 1280×800 RGB/RGBA PNGs, including multiple IDAT chunks, counted as images in all three request formats; delivery credited reads. No screenshot-byte exhaustion regression reproduced.

**Documentation still overstates enforcement:** [SPEND_SAFETY.md:174](docs/SPEND_SAFETY.md:174), “two copies at different paths cannot both go on,” overlooks concurrency. Line 31’s “whole episodes the ceiling admits” overstates the estimator. Line 84’s “None of these can reject usage a provider really reported” is not established by these checks.

Operator order:

1. Set and verify the actual provider hard cap; disable reload.
2. Use one machine, fixed state directory, one physical ledger and one runner.
3. Validate prices/allowances; initialise £10; create a small paired plan.
4. Review the estimate cautiously; run metered, without overrides or unmetered options.
5. Inspect usage, versions, refusals and evidence before expanding.

Residual risks: early exhaustion, incomplete pairs, optimistic estimates, and unverifiable aliases. No additional silent result-corruption blocker was reproduced.