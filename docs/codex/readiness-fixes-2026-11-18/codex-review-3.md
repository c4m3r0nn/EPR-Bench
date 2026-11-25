**NO-GO for paid runs.** Reviewed `9586eb6` on `step4/integration`. No files edited, `.env*` read, remotes accessed, paid calls, or agent CLIs run. Probes used the requested Python/environment with `EPRBENCH_STATE_DIR=/tmp/eprbench-review3-state`. Sandbox restrictions prevented temporary writes: findings below combine source inspection and in-memory probes, not a full disk-backed test rerun.

Paths below are relative to `src/eprbench/`.

1. **Missing usage — FIXED.** `spend.py:157,636`: absent usage/cache counts retain reservations. Probe confirmed both rejection reasons; real `Meter.settle` retained the whole reservation.

2. **Text priced as images — FIXED.** `spend.py:220,256`: all **16 image regression cases passed**, invoked directly. Exact Anthropic `tool_result` with `toolset_name="computer"`, OpenAI `computer_call_output`, and final-chance `input_image` each counted one image. Ordinary image-looking text remained bytes. Caveat: `real_image`, line 214, validates only the PNG header/dimensions; a truncated 24-byte header passes.

3. **Rollback/replacement — PARTIAL.** `spend.py:479`: anchored rollback was refused. After deleting only the simulated anchors, the same earlier ledger prefix was silently accepted; a surviving same-ID plan reported no problem. No hash forgery or override was needed. An interrupted episode need not have a completed manifest witnessing its spend.

4. **Spend refusal reported as success — PARTIAL.** `episode.py:520`, `__main__.py:348`: ordinary refusal produced exit **3**. Final-chance refusal preserved `model_request_limit`; the CLI probe returned **0**, and that reason belongs to `COMPLETE_ENDINGS` (`episode.py:764`). Thus refused work can still become resumably complete.

5. **Screenshot reads at capture — FIXED.** `claude_browser.py:144,151`, `episode.py:500`: mocked capture credited **0** reads; delivering its exact image credited **1**.

6. **Mutable aliases — PARTIAL.** `runner.py:127`, `episode.py:505`: plan aliases were rejected; changed reported versions raised `ModelDrift`. However, absent versions merely mark “unverified” and continue; the first response establishes the expected version. Direct runs still accept aliases without the plan’s opt-in.

7. **Resume ledger identity — PARTIAL.** `runner.py:200`: a mismatched manifest hash was refused. But `__main__.py:307` permanently skips **all** ledger/evidence checks for that plan hash after a recorded override, including future mismatches—not merely the originally approved replacement.

The screenshot allowance matches the 1280×800 viewport. Although harness zoom arguments are unclamped, installed Playwright clips non-full-page captures to the viewport (`coreBundle.js:21740`); zoom does not upscale them.

The 64-byte floor is not established as a universal tokenizer bound: serialized metadata and highly compressible whitespace can invalidate that inference. A simulated 100,034-byte/800-token request was rejected; provider tokenization was not verified offline. **Recognized screenshot base64 is excluded from this floor**, so normal screenshots do not automatically trigger it.

A stronger reproduced denial occurs at `spend.py:644`: missing usage assigns the **upper bound** to `previous_input`. Subsequent legitimate lower counts then fail indefinitely. With illustrative GBP/million rates **3 input, 3.75 cache-write, 0.3 cache-read, 15 output**, 8KB text, one 1,600-token screenshot, 1,000 overhead and 16,000 maximum output, a £10 ledger admitted **12 requests**, retaining £9.993. A 24-episode preview is **not feasible under that failure pattern**.

Anchors use `EPRBENCH_STATE_DIR`, then `XDG_STATE_HOME/eprbench`, then `~/.local/state/eprbench` (`spend.py:369`). Missing directories are recreated; another machine loses rollback protection. Read-only state fails before sending, potentially after ledger append. Anchor permissions follow umask, unlike the ledger’s explicit 0600. Two checkouts share a ceiling only by sharing the physical ledger; copied same-ID ledgers have separate locks. Tests’ autouse fixture redirects anchors away from home.

All inspected SDK call sites use `Episode.send`, but direct harness calls default to `spend=None`: a mocked Claude request proceeded without reservation or explicit acceptance (`claude_tools.py:67`).

The guarantee overstates enforcement: **“A ledger that went back, was replaced or was swapped for a fresh one is refused.”** (`docs/SPEND_SAFETY.md:139`).

**Must fix:** fail closed on missing anchors; coordinate copied-ledger identity; propagate final-chance spend refusal; recover safely from uncertain usage; narrow persistent overrides; enforce metering at harness entry; correct the guarantee and validate preview affordability.