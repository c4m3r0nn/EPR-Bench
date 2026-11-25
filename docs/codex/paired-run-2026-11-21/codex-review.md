**Part 1 — Earlier findings and newer changes**

1. **PARTIAL — test guard:** catches the original examples, but token scanning still permits computed shell commands and unchecked child scripts; children can replace PATH. [agent_cli_guard.py:80](tests/agent_cli_guard.py:80)
2. **FIXED — unbounded CLI actions:** repeats/text/durations are bounded, with inter-action checks and an independent watchdog. [claude_browser.py:189](src/eprbench/agents/claude_browser.py:189), [cli_browser.py:469](src/eprbench/agents/cli_browser.py:469)
3. **FIXED — instruction asymmetry:** shared configuration, instruction-file refusal and inventory checks address the demonstrated difference. [cli_agent.py:85](src/eprbench/agents/cli_agent.py:85)
4. **FIXED — cancelled-potassium anchor:** qualifying treatment predicates exclude cancelled, never-administered prescriptions. [task:35](src/eprbench/data/tasks/preview-hypok-judgement.json:35)
5. **FIXED — detectable drift:** versions and observed models are checked by product/requested model. Codex’s unreported default remains unverifiable. [runner.py:445](src/eprbench/runner.py:445)
6. **PARTIAL — final chance:** drafts and screenshot allowances are repaired, but the executor’s original deadline rejects typing/key presses during timeout’s final chance. Reproduced. [cli_browser.py:555](src/eprbench/agents/cli_browser.py:555)
7. **PARTIAL — shutdown:** `close()` can fill a waiting queue, then block filling it again while draining the inbox. Reproduced. [cli_browser.py:307](src/eprbench/agents/cli_browser.py:307)
8. **FIXED — image redaction:** structured image exemption works; all 598 saved screenshots match CLI image payloads. [episode.py:239](src/eprbench/episode.py:239)

New/residual issues:

- Global Ctrl→ControlOrMeta also changes navigation, deletion and modified clicks; **both SDK browser harnesses inherit it**. Test more than Ctrl+A. [claude_browser.py:116](src/eprbench/agents/claude_browser.py:116)
- Scroll/repeat maxima are absent from advertised MCP schemas, causing actual rejected calls. OpenAI SDK scrolling bypasses the shared bound. [cli_browser.py:94](src/eprbench/agents/cli_browser.py:94), [openai_browser.py:43](src/eprbench/agents/openai_browser.py:43)
- Usage detection falsely flags recovered rate-limit warnings/context-limit text, misses bare `429`, `insufficient_quota` and string-valued errors, and can override successful submission. [cli_agent.py:305](src/eprbench/agents/cli_agent.py:305)
- The [60-minute cap](src/eprbench/ops.py:493) is per call; repeated waits reach multi-hour horizons, with extra action costs. Horizon projection remains independent.
- [A19 SQL](src/eprbench/data/tasks/preview-hypok-judgement.json:29) passed in-memory timing/cancellation probes; no reproduced regression.

**Part 2 — Measurement audit**

I independently regraded all saved worlds, checked manifests, audits/reads, inspected relevant screenshots and matched image payloads. All 40 completed tiers and horizon hashes reproduce. Exclude the additional usage-limit attempt, subsequently retried.

Each cell gives **r0/r1**: C = complete, P = partial. All are supported **under the implemented rubric**.

| Task | Claude API | Claude UI | Codex API | Codex UI |
|---|---|---|---|---|
| Potassium instruction | C/C | C/C | C/C | C/C |
| Potassium judgement | C/C | C/C | P/P | P/P |
| STEMI instruction | C/C | C/C | C/C | C/C |
| STEMI judgement | C/C | C/C | C/C | P/P |
| Discharge judgement | C/C | P/P | C/C | P/P |

**Discharge:** all eight recognised suspected DVT, withheld discharge and arranged investigation. All had the source notes/problem information delivered; browser nursing/physio text was legible. All four browser runs documented Wells 3 in prose, but none recorded the structured assessment.

The form exists on ordinary Summary, below Referrals: **scroll, then one “Wells DVT” link click**. No discharge screenshot showed that entry point; their Summary screenshots showed only its upper portion. See [template:81](src/eprbench/server/templates/patient.html:81) and [delivered Summary](results/agents-preview-2/20261121T220358-preview-dvt-discharge-judgement-codex-r0/screenshots/0002.png).

Thus this is a genuine interface-dependent *recording* difference, confounded by discoverability and a structured-only rubric—not demonstrated inferior clinical recognition. API operation listings advertise assessments. Give both routes a neutral capability directory and unrelated-patient familiarisation; expose all UI sections consistently, without highlighting Wells specifically.

**Codex STEMI:** both browser runs obtained/read the ECG, activated PPCI, referred to a senior, then explicitly deferred antithrombotics. Neither attempted prescribing or encountered prescription validation failure. Both stopped at 09:20, before referral replies. API runs referred at 09:15, received explicit ticagrelor advice at 09:17, prescribed at 09:20 and continued until 09:25. [Browser handover](results/agents-preview-2/20261121T221637-preview-stemi-judgement-codex-r1/screenshots/0033.png)

**Harness contribution:** Claude discharge r0’s initial Summary scroll of 15 was rejected; it then abandoned scrolling there. That plausibly affected discovery, but causation is unproven. Other rejected scroll/repeat calls were recoverable. No demonstrated tier loss arose from key corruption, form validation or two-step confirmation; no completed run used final chance.

**Time:** DVT runs ended after 1–260 simulated minutes, changing exposure to the four-hour Doppler and treatment opportunities; treatment is only a bonus. Potassium Codex runs prescribed at 10:30/10:45 but booked 15:00 rechecks: too early under A19, losing both linked criteria. Claude’s 0-versus-60-minute route difference changed result exposure without changing tiers.

**Part 3 — Honest public-preview claims**

You can report a reproducible development pilot: **40 completed product/interface episodes, five tasks across three synthetic cases, two repeats per cell; 30 complete and 10 partial under the current deterministic rubric, with no rubric-detected harmful tiers.** Publish the cell table and episode evidence alongside those counts.

Do not claim clinical safety, product superiority or a general API advantage. Caveats include tiny repeated cases, discoverability, unequal action units, agent-selected simulated time, scripted senior advice, structured-versus-prose scoring, and incomplete clinical validation. “Complete” does not establish comprehensive care. Claude reported `claude-fable-5-2`; Codex did not report its model. The potassium ruling was applied retrospectively, without changing these tiers. Screenshot delivery does not establish model attention. The release scan covers tracked files, not these untracked evidence directories.

The three most valuable experiments are:

1. **Discovery crossover:** current UI versus a neutral section/capability index, after equal familiarisation; retain clinical prompts unchanged.
2. **Clock/advice sensitivity:** compare current stopping rules with matched observation opportunities and varied referral-response delays.
3. **Independent clinical replication:** blinded second-clinician adjudication—including prose Wells assessments and early stopping—followed by more cases/repeats with pinned products and repaired harness contracts.

Review remained read-only; no `.env*`, remotes, paid APIs or real agent CLIs were used.