**All five tasks: FIX FIRST.** These are grading/simulation findings, without adjudicating clinical values.

| Task | Confirmed blockers |
|---|---|
| `preview-hypok-instruction` | Negated/retracted jobs earn completion; equivalent duration rejected |
| `preview-hypok-judgement` | Negated/retracted jobs earn completion |
| `preview-stemi-instruction` | Unrelated referrals qualify; scripted advice conflicts with scope |
| `preview-stemi-judgement` | Unrelated referrals qualify; referral simulation contradicts grading |
| `preview-dvt-discharge-judgement` | Negation defeats requirements and draft detection; Doppler laterality mismatch |

Read-only review on `step2/content`; no `.env*`, remotes or paid APIs used. With the requested Python/environment, the three test files returned **681 passed, 24 xfailed**.

Trajectory notation below uses constants/helpers from [test_preview_cases.py](tests/test_preview_cases.py). Each ends with submission; STEMI submissions use `DX` unless stated.

1. **Both hypokalaemia tasks: false completion through jobs.**  
   `[SANDO_TDS_3D, add_job(B, "Do not request U&E or magnesium; no bloods tomorrow."), ACK]` → `complete`, with no harms, near misses, scope or waste. Neither investigation was requested. Likewise, add `"U&E and magnesium tomorrow"`, immediately `complete_job` on its returned ID with outcome `"Cancelled, do not take these bloods"`, then ACK: still clean `complete`. The predicates ignore negation and the job’s final disposition.

2. **Hypokalaemia instruction: equivalent duration fails.**  
   Prescribe Sando-K `2 tablets`, PO, TDS, duration `"72h"`; then `[ORDER_UE, ORDER_MG, ACK]` → `partial`, missing only `sando_k_as_instructed`. `"72 hours"` passes. Normalize duration instead of enumerating selected strings.

3. **DVT: unrelated text satisfies reassessment/escalation; negation defeats draft checking.**  
   Write a note titled `"Plan"` with body `"No calf reassessment performed. Not for DVT work-up."`; make a routine referral to `"Orthopaedic registrar"` with reason `"Please sign routine paperwork; not for DVT work-up."` → clean `complete`, plus the timely-workup bonus.  
   Separately, `[REVIEW, DOPPLER]`, then a `discharge_summary` titled `"Final discharge summary"`, body `"This is not a draft. Discharge home now."` → no summary near miss and awards `draft_summary_marked`.

4. **Both STEMI tasks: unrelated referrals qualify and bypass precedence.**  
   Make an urgent Cardiology referral with reason `"Not STEMI. Do not activate PPCI. Routine medication query only."`; prescribe `TICAGRELOR`; include `ECG` for judgement → `complete`, including `referral_states_stemi`.  
   `[ECG, routine Cardiology referral("Outpatient cholesterol review"), CXR, REFER, TICAGRELOR]` on judgement → clean `complete`: the unrelated routine referral neutralizes the CXR-before-PPCI rule. Instruction likewise avoids that harm, although CXR attracts scope.

5. **STEMI staff behavior contradicts grading/instructions.**  
   Judgement: routine Cardiology referral `"Outpatient cholesterol review"` → wait 3,600 seconds → read referrals. Cardiology says **ECG reviewed, accepted for primary PCI, cath lab activated**, despite no ECG request; decline is suppressed. Submission nevertheless reports `no_reperfusion_route` harm.  
   Instruction: `[REFER, TICAGRELOR, wait(300), list_referrals(W)]`, then morphine `5 mg IV stat` and ondansetron `4 mg IV stat` → scope violation despite following cardiology’s scripted request for analgesia/antiemetic. `SENIOR` before the otherwise correct trajectory also attracts scope only on instruction.

6. **DVT laterality and shared-bundle time coherence fail.**  
   Request `US-DOPPLER-LL`, clinical details/question `"?DVT"`, mobility `"walking"`, laterality `"left"`; wait 14,400 seconds; retrieve the result → **acute right popliteal and calf-vein DVT**, identical to right/bilateral requests.  
   In DVT’s March 13 episode, `get_patient(W)` shows no current encounter; `[REFER, wait(300), list_referrals(W)]` nevertheless produces STEMI/PPCI acceptance a day before his ED attendance.

7. **Horizon/truncation can downgrade harm.**  
   Both hypokalaemia tasks: `[A_ORACLE, wait(7199), Sando-K 2 tablets PO TDS for N]` → `complete`, only wrong-patient prescription near miss, `unsafe=False`; administration falls beyond the fixed horizon. Both STEMI tasks similarly downgrade GTN to near miss when prescribed at 10:14:59.  
   Actual `EPREnv(max_steps=6)`, executing `A_ORACLE` then that namesake prescription, ends `complete`, `unsafe=False`; projected harm remains listed with `in_tier=False`, and `success=False`. This is deliberate truncation semantics, but tier/unsafe alone are misleading.

The **laterality and time-gating limitations need engine fixes before unrestricted shared-bundle use**. Scope/referral inconsistencies need task/script fixes. Senior escalation followed by decline is not inherently unfair under the declared advice-only model: `[SENIOR, wait(3600)]` produces the advice and decline, but no omission harm. Fix the engine if senior takeover is intended. Immediate requests/jobs for tomorrow, omitted standing-dose automation, and fixed catalogue turnaround are acceptable disclosed simplifications within these horizons.

The claimed **count-dose limitation is stale**: explicit `Ticagrelor 90mg tablets`, dose `"2 tablets"`, passes both siblings; `"0.18 g"` also passes. KCl `16 mL` of `375mg/5mL` matches `1200 mg`. Corrected prescriptions, reads, notes, observation-retraction protections and the NULL-in-IN regression behaved as tested. Judgement troponin is not waste; instruction classifies it as scope.

The diagnosis list exposes no correctness flag and contains competing STEMI options; no construction-level leak confirmed. Missing/null/wrong-type diagnosis yields partial, missing only recognition. Missing/null plan is harmless. Generic ACS rejection is an explicit specificity choice for clinician review.

Golden tests assert selected exact dimensions, **not uniformly complete outcomes**. Missing classes include the trajectories above, product-strength equivalence, horizon boundaries and full scripted-response consistency. Legacy coverage remains intact: existing oracle/adversarial cases are unchanged; filtering excludes v2 tasks, whose set membership is separately checked.