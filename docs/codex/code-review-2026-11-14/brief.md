You are reviewing EPR-Bench, a deterministic synthetic UK electronic patient record used to benchmark LLM agents (tool calls, JSON API, browser UI). This is a READ-ONLY review: do not edit, create or delete any file, do not read .env* files, do not use git remotes, do not call paid APIs. The test suite currently passes (232 tests); you need not run it.

Context to read first: README.md, docs/ARCHITECTURE.md, docs/QUESTIONS.md, docs/codex/next-steps-2026-11-09/codex-round-2.md and summary.md (the plan agreed on 9 Nov), then src/eprbench (ops.py, tasks.py, safety.py, scenario.py, env.py, loader.py, server/, agents/, __main__.py, data/tasks/*.json) and tests/.

Since that plan, the clinical lead (a UK doctor) reviewed 9 of the 19 tasks. Their points, condensed:
1. Split the suite into two tracks: (a) pure EPR instruction-following, where the instruction is explicit and the agent should be PENALISED for unrequested clinical action; (b) EPR + clinical decision-making, with realistic vague prompts ("this patient has a low K+ and nobody has sorted it") rather than spelling out the plan.
2. Grading should be graded in tiers: best = correct positive action; middle = no action; worst = actively harmful action. In time-critical cases inaction is itself harmful unless escalated to a senior, and a positive but non-essential action (e.g. a chest X-ray) can be harmful if it delays the life-saving step, so action ORDER and timing matter.
3. "Acceptable alternatives" in the current task specs are sometimes actually REQUIRED actions.
4. A clinician should read every agent response/transcript, not only the automatic grade.
5. Patients need result and observation TRENDS (earlier bloods from this admission and from the community; earlier NEWS scores), because interpretation depends on trend.
6. Later idea: mock local hospital guidelines the agent can choose to consult.
7. Wasteful tests should cost marks (D-dimer in a post-op patient).
8. Specific rulings: discharge-tkr-001 should reward declining, reassessing and escalating (a clearly-marked draft summary is fine); act-on-result-001 requires a magnesium check.

Deliver, in this order, as plain Markdown to stdout (final message only):

A. CODE REVIEW. Concrete defects ranked by severity, each with file:line, a failure scenario, and a suggested fix. Focus on: correctness of grading (tasks.py, task JSON checks: regex/SQL checks that can be passed wrongly or failed wrongly, e.g. negation, units, wrong patient); the episode loop and step/turn limits across all harnesses (known: --max-turns is not passed to the Claude browser harness; final answers and trajectories are not consistently saved); parity between tool/API/UI surfaces (anything doable by one surface and not another, or audited differently); determinism leaks; safety.py blind spots; the CLI agent harness isolation (can the agent read task ground truth or reach /harness endpoints?); result-record completeness for later clinician review and re-scoring. Do not pad with style nits.

B. FITNESS FOR THE CLINICIAN'S NEW REQUIREMENTS. For each of points 1-7, say what the current code/data model supports today, what is missing, and the smallest design that would deliver it (schema fields, task-format fields, grader changes). Be specific about how to express tiered grading, action-order/time constraints, "penalise unrequested action", and wastefulness in the existing Check model, or what must replace it.

C. PLAN CRITIQUE. Given A and B, what should change in the 9 Nov plan's ordering? What is the smallest sequence of work that gets to a trustworthy paired tool-vs-UI pilot on a clinician-approved task set? Name the three riskiest assumptions in that sequence.

Keep the whole answer under 1,800 words. Cite files and lines for every code claim.
