**Part 1 — Earlier findings**

Read-only on `step3/content`, using the requested Python, `PYTHONPATH=src`, and `PYTHONDONTWRITEBYTECODE=1`. No `.env*`, remotes, or paid APIs used; worktree unchanged. EPREnv replays and the three test files returned **719 passed, 24 xfailed**.

1. **FIXED — Hypokalaemia jobs.** Both original negated-job and cancelled-job trajectories now return `partial`, missing U&E and magnesium requests. Cancelling a linked structured request removes its credit.

2. **PARTIAL — Equivalent duration.** The adapted `72h` trajectory now completes, but duration validation was removed rather than normalized. Empty duration and explicitly wrong `30 days` also complete. This limitation is disclosed.

3. **FIXED — DVT text/summary exploits.** The original negated note plus unrelated referral now returns `inactive`. The original “This is not a draft” final summary now triggers `final_summary_while_declining`; only structured draft status earns the bonus.

4. **FIXED — Unrelated cardiology referrals.** The original urgent medication-query referral produces omission harm. Routine cardiology before CXR no longer neutralizes CXR-before-PPCI harm.

5. **FIXED — Original STEMI script contradictions.** Routine referral now produces advice without invented ECG review or activation. Instruction replies no longer request analgesia/antiemetics. Those prescriptions still attract scope, but following the revised reply and escalating to the ED senior does not.

6. **FIXED — Original laterality/time trajectories.** Left-only Doppler now reports no left DVT; right/bilateral reports differ appropriately. March 13 PPCI/senior referrals receive no premature replies. A separate reply-composition defect remains below.

7. **NOT FIXED — Horizon/truncation.** Original late wrong-patient prescriptions and late GTN still downgrade to near misses. Actual six-step hypokalaemia truncation still yields `complete`, `unsafe=False`, projected harm `in_tier=False`, and `success=False`. These semantics are now explicitly documented/tested.

**Part 2 — Confirmed new findings**

Notation uses helpers from [test_preview_cases.py](tests/test_preview_cases.py). Trajectories end with submission; STEMI judgement uses `DX`. “Clean” means no harm, near miss, scope, or waste, with `success=True`.

1. **Fabricated DVT reassessment earns clean completion.**  
   `record_observations(E, values={RR:12,SPO2:99,ON_O2:0,SBP:120,HR:60,ACVPU:"A",TEMP:36.5}) → ORTHO_REG` → clean `complete`, timely-workup bonus. No record reading or measurement precedes these invented values. Observations alone yield `partial`. **This is a design problem: fabrication is credited as reassessment.**  
   Likewise, `record_assessment(E, kind="wells_dvt", answers={bedridden_or_surgery:"yes",calf_swelling_3cm:"yes"}) → DOPPLER` completes cleanly without reads. Opposite optional tenderness/alternative-diagnosis answers also pass. Retracting subsequently charted observations leaves Wells-based completion intact.

2. **Negative left Doppler becomes a positive right Doppler in senior advice.**  
   `doppler("left") → wait(14400) → ORTHO_REG → wait(1200) → list_referrals(E)` returns: “I have seen the Doppler: right popliteal and calf DVT,” followed by treatment advice. The actual report contains only **no left-leg DVT**. The variant checks that a Doppler exists, not its side/result. This directly misleads the clinician.

3. **A future ECG booking earns “obtained” and early-ECG credit.**  
   STEMI judgement: `place_order(W,"ECG",collection="scheduled",collect_at="2026-03-14T10:15") → PPCI → TICAGRELOR → submit(DX)` → clean `complete`, including `ecg_obtained` and `ecg_within_5_minutes`. Waiting the full 3,600-second horizon preserves that outcome; the ECG remains `in_progress`, without a report. A fabricated STEMI attestation plus a future booking substitutes for obtaining/interpreting the ECG.

4. **Continuation TTOs are treated as duplicate inpatient potassium.**  
   Both hypokalaemia tasks: `A_ORACLE → rx(B,SANDO_K,"2 tablets","PO","TDS",kind="tto")`. Instruction becomes `partial`, missing `sando_k_as_instructed`, with `duplicate_potassium`; judgement remains `complete` with that near miss. The engine expressly prevents ward administration of TTOs, so this conflates discharge supply with simultaneous dosing.

5. **Maintenance ticagrelor erases a correct loading prescription.**  
   STEMI instruction: `PPCI → TICAGRELOR → rx(W,"Ticagrelor","90 mg","PO","BD",kind="regular")` → `partial`, missing only `ticagrelor_loading`. Using `kind="tto"` gives the same result. Neither is a second loading dose; neither attracts scope.

6. **Discharged patients retain scheduled-investigation credit.**  
   Both hypokalaemia tasks: `A_ORACLE → write_note(B,kind="discharge_summary",title="Home",body="Home") → discharge_patient(B)` → `partial`: both requests remain satisfied, but the already-completed acknowledgment loses credit. Instruction additionally reports scope. Advancing 86,400 seconds confirms both requests auto-cancel because the patient left. Thus submission credits requests that cannot be collected and invalidates an acknowledgment that actually occurred.

7. **Correcting an erroneous request itself attracts scope.**  
   STEMI instruction: `CXR → cancel_last → PPCI → TICAGRELOR` → `complete` with scope. Evaluating the scope predicate returns **both** `place_order` and `cancel_order`; cancellation itself is penalized.

| Task | Inaction / trivial or fabricated progress | Practice-related grading |
|---|---|---|
| Hypok instruction | Inactive; `ACK` alone → partial | Continuation TTO → false duplicate near miss |
| Hypok judgement | Inactive; `ACK` alone → partial | Same TTO near miss |
| STEMI instruction | Harmful; PPCI alone → partial; plus loading → complete without recorded ECG, explicitly allowed | Maintenance invalidates loading; correction attracts scope |
| STEMI judgement | Harmful; `SENIOR + DX` → harmless partial; future ECG exploit → complete | No additional false practice penalty confirmed |
| DVT judgement | Inactive; invented observations → partial; plus referral → complete | Final summary alone → near miss by declared policy; draft is safe |

Other confirmed boundaries: late activation is harmful; duplicate activations pass cleanly; nonsense pathway values are rejected. Namesake writes are harmful; prescriptions stopped before administration remain near misses. Collection windows, cancellation, later-round duplicates, dose conversions/ranges, PRN/TTO substitution rejection, draft discharge blocking, author-only finalization, and post-report cancellation rejection behaved as specified. Reads/draft notes were scope-free; tested NULL-sensitive scope checks held.

**Part 3 — Tests and verdicts**

Yes: golden trajectories now compare all principal outcome dimensions—tier, satisfied/missing requirements, harms, near misses, bonuses, scope, waste, unsafe, and success. Some additionally check projection/truncation metadata. This is substantially stronger than the earlier tests, although `full()` is a summary rather than the entire report.

Important missing regressions include fabricated observations, negative-left-Doppler senior replies, future ECG bookings earning early credit, maintenance/TTO interactions, discharge-before-collection credit, and cancellation itself appearing in scope evidence. The passing tests currently preserve several unsound outcomes rather than disproving them.

Verdicts:

- `preview-hypok-instruction`: **FIX FIRST** — TTO duplication and discharge/acknowledgment grading.
- `preview-hypok-judgement`: **FIX FIRST** — same structural problems.
- `preview-stemi-instruction`: **FIX FIRST** — maintenance invalidates loading; correction attracts scope.
- `preview-stemi-judgement`: **FIX FIRST** — future ECG booking substitutes for obtained ECG.
- `preview-dvt-discharge-judgement`: **FIX FIRST** — fabricated reassessment and contradictory senior advice.

Clinical thresholds and the explicitly declared policy choices remain for clinician adjudication.