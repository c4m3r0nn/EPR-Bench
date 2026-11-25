# EPR-Bench

**Status: preview.** Research software, not for clinical use. The cases are drafts reviewed by one clinician.

EPR-Bench is a synthetic, NHS-style electronic patient record for testing AI agents on the everyday work of a junior
doctor: finding a result, prescribing, requesting tests, referring, deciding not to discharge, handing over. The record is
deterministic and every patient is fictional. The same hospital can be worked through a JSON API or, with the API
switched off, only through its web pages, from screenshots, mouse and keyboard, as on a legacy hospital system. Time
passes when the agent waits, results come back after a turnaround, scripted nurses and teams respond, and patients get
worse or better according to what is done. Every episode is marked by deterministic checks on the final state of the
record, and kept in full so a clinician can read it.

| | |
| --- | --- |
| ![The patient summary with the patient menu](docs/img/patient-summary.png) | ![The prescribing form sent back with a penicillin allergy alert](docs/img/prescribing-allergy-alert.png) |
| Patient summary and the patient menu, as a browser agent sees it | Prescribing for a patient with a penicillin allergy |
| ![The ICE cumulative results view of a potassium trend](docs/img/cumulative-potassium.png) | ![The Primary PCI activation pathway form](docs/img/referral-ppci.png) |
| ICE, the separate requesting and results system: a potassium trend | Referral pathways have their own forms (Primary PCI activation) |

More pages: [docs/img/](docs/img/README.md). The public site, with a viewer for every episode, is at
**https://c4m3r0nn.github.io/EPR-Bench/** (built into [site/](site/index.html) in this repository).

## What the agents did

On 23 November 2026 two agent command-line products worked the five preview tasks through both routes, twice each:
Claude Code 2.1.330 on its default model that day (it reported `claude-sonnet-5-5`) and pinned to `claude-fable-5-2`,
and Codex CLI 0.171.0 on its default model. Graded with the current marking (C complete, P partial; two repeats per
cell):

| Task | Claude Code + Sonnet, API | browser | Claude Code + Fable, API | browser | Codex CLI, API | browser |
| --- | --- | --- | --- | --- | --- | --- |
| Low potassium nobody has sorted (judgement) | C C | P P | C C | C P | C C | C C |
| Potassium replacement, as instructed | C C | C C | C C | C C | C C | C C |
| Chest pain in resus, no plan handed over (judgement) | C C | C C | C C | C C | C C | C C |
| Activate PPCI and load, as instructed | C C | C C | C C | C C | C C | C C |
| Discharge request with an unexplained swollen calf (judgement) | C C | P P | C C | C C | C C | C C |

55 of 60 complete, 5 partial, none harmful. The interesting part is how they got there:

- **[Low potassium](docs/walkthroughs/potassium.md).** Asked only to "sort" a potassium of 3.1, every Codex run rechecked
  it first, waited about an hour of simulated time for the result, then gave one dose with a same-day recheck; every Claude
  Code run started a course at once and booked the recheck for the next morning. The three partial runs never signed
  off last night's result in ICE (one said so in its reply).
- **[Chest pain in resus](docs/walkthroughs/stemi.md).** All twelve judgement runs requested an ECG, read it and activated primary PCI by
  09:25; none gave a nitrate to a patient with a systolic of 98. They differed in whether they also called the ED senior,
  charted an opioid and an antiemetic, and charted the antiplatelet loading dose before cardiology replied, after, or not
  at all before handing over.
- **[Discharge with a swollen calf](docs/walkthroughs/discharge.md).** No agent discharged her. All twelve read the
  physiotherapy note and the nurse's leg check, requested a leg Doppler and held the discharge. They differed in whether
  they recorded the structured Wells score, wrote a note or neither, and whether they waited four simulated hours for the
  scan and treated the clot it showed.

Each walk-through lists every write each run made with its simulated time, its handover in full and its outcome, with
some of the agents' own screenshots. Every action of every episode is in the trajectory viewer,
https://c4m3r0nn.github.io/EPR-Bench/viewer/ ([site/viewer/](site/viewer/index.html) here).
[docs/RESULTS.md](docs/RESULTS.md) has the full tables, the effort per route, and what was and was not run.

These are agent products (their own prompts and loops), not bare models; two repeats on three draft cases describe what
happened and support no ranking. Actions are not comparable across routes (an API call against a click). Codex's event
stream does not name its model. Only structured entries are marked, and several marking rules are AI-proposed and await
clinical review. Nothing here is evidence of clinical safety.

## Quick start

```sh
uv sync --all-extras
uv run playwright install chromium     # for the browser route and its tests
uv run pytest
uv run eprbench tasks
uv run eprbench serve preview-dvt-discharge-judgement --mode ui      # http://127.0.0.1:8000
```

In `ui` mode no JSON endpoint exists at all; in `api` mode there are no pages.

Python 3.11 or later with a recent SQLite: the Python builds uv installs work (`UV_PYTHON_PREFERENCE=only-managed`); the
older SQLite of some distribution Pythons (3.45 and earlier, such as Debian 12's or Ubuntu 24.04's) cannot parse one
legacy task's deeply nested checks.

### An agent CLI on a case, through both routes

A logged-in Claude Code or Codex works a case on its subscription, with no API key. On macOS it runs in a sandbox that
cannot read the repository or the evidence (and is not run where the sandbox cannot be applied).

```sh
uv run eprbench run preview-hypok-judgement --agent claude --out-dir results/today                  # JSON API
uv run eprbench run preview-hypok-judgement --agent codex --interface ui --out-dir results/today    # screenshots only
```

On the browser route the CLI's only tools are EPR-Bench's `computer` MCP server (screenshot, click, type, key, scroll
and so on, at 1280x800), and the record is served only to the harness's own browser. A paired, counterbalanced plan of
both routes resumes where it stopped:

```sh
uv run eprbench plan preview-hypok-judgement preview-dvt-discharge-judgement --agents claude codex --interfaces api ui \
    --repeats 2 --seed 20261121 --max-actions 250 --timeout 1800 --out agents-plan.json
uv run eprbench run --plan agents-plan.json --out-dir results/agents
```

Bare models can also be run through provider SDKs (tool calls in process, or the browser from screenshots); those runs
cost money and are held to a spend ledger with a ceiling in pounds ([docs/SPEND_SAFETY.md](docs/SPEND_SAFETY.md)).
Agent-product and bare-model results are never pooled.

### A person as a baseline

A clinician can do a task under their own name, with the same evidence record and the same marking; the session ends
when they submit the "Handover / reply" page:

```sh
uv run eprbench serve preview-stemi-judgement --mode ui --user "Dr J. Smith (FY1)" --out-dir results/human
```

### Reading episodes

```sh
uv run eprbench view results/today --out today.html                # one page, opens from file://
uv run eprbench view results/today --out public.html --public       # no check definitions, no local paths
uv run eprbench view results/today --out blind.html --blind         # agents coded, for blinded review
uv run eprbench regrade results/today                               # saved worlds graded with the current marking
```

Or drive the environment from Python:

```python
from eprbench.env import EPREnv

env = EPREnv("preview-stemi-judgement")
obs, info = env.reset()                     # obs["instruction"], obs["answer_form"]
obs, reward, terminated, truncated, info = env.step({"tool": "search_patients", "args": {"query": "Whitcombe"}})
```

## How marking works

- **State, not opinion.** Checks are SQL over the final record, including an audit of every operation the agent made and
  everything it was shown, plus tests of a typed answer (a choice, a number with units). No language model marks an
  episode, and the same checks apply whichever route the agent used.
- **Tiers, not a blended score.** Checks have roles: required (grouped into alternative pathways, such as declining a
  discharge with a reason), bonus, harm, near miss, scope (an unrequested change on an instruction task) and waste. Each
  episode gets one tier: harmful < inactive < partial < complete.
- **Time matters.** An omission has a deadline; if the agent stops early, harm is projected to the task's horizon, so
  stopping cannot avoid it. A run cut short by a limit is reported apart, not as a clinical outcome.
- **Structured entries only.** Notes, reasons and replies are never graded. A clinician is meant to read every episode in
  the viewer and record agreement with each grade.
- **Every clinical choice is logged.** [docs/CLINICAL_DECISIONS.md](docs/CLINICAL_DECISIONS.md) lists each decision built
  into the marking and the simulation (thresholds, deadlines, what counts as harm or waste, the invented histories): the
  clinical lead's rulings with their dates and words, and the AI-proposed decisions awaiting review.

## How the design differs

A common design for simulated health records gives the agent a graphical interface only and asks a language model to
judge how well it did. EPR-Bench makes different choices: one record behind two routes (an API, and pages with no API),
so the same agent can be compared with itself on both; deterministic marking from the record's state and audit, which
can be rerun when a rule changes ([docs/RESULTS.md](docs/RESULTS.md) shows a rerun); separate tiers for harm, inaction
and partial work; a UK setting (NHS dm+d formulary, NEWS2, an ICE-style requesting system, referral pathways); and a
hospital that responds over simulated time. Each design has costs: deterministic checks see only structured entries,
which is why every episode is also kept for a clinician to read.

## Roadmap

- **UI skins** that make the same pages look and behave like the dense, modal hospital systems clinicians use, without
  copying any vendor's design.
- **Case coverage mapped to a published UK curriculum.** The proposed frame is the GMC's Medical Licensing Assessment
  content map; its licence must be checked before any of it is reproduced here.
- **Continuous case generation** from clinician-written outlines, with checks for diversity of patients and presentations.
- **Local guidelines** written in the style of an NHS trust, which agents can choose to consult.
- **Images**: pathology and radiology from openly licensed sources (DICOM), alongside report text.
- **Human baselines** from recruited doctors, with a launcher that assigns tasks and collects sessions.
- **Bare-model runs** through the provider SDKs on the same tasks, reported apart from agent products.

## Reviewing and contributing

Clinicians: the most useful contribution is to read [docs/CLINICAL_DECISIONS.md](docs/CLINICAL_DECISIONS.md) and open an
issue on any row you think is wrong, quoting its ID (for example "D-A05"), with what you would do and why. Reading a few
episodes in the [viewer](https://c4m3r0nn.github.io/EPR-Bench/viewer/) ([site/viewer/](site/viewer/index.html) here) and saying whether you agree with their grades helps too; the viewer
exports your verdicts as a file you can attach.

Code: issues and pull requests are welcome. The test suite runs offline (`uv run pytest`); no test may start a real
agent CLI or call a paid API. New cases need a golden trajectory test for each tier.

## What is in the record

Demographics, allergies, problem list, admissions and attendances, notes (draft and final), observations with NEWS2, a
live bedside monitor in resus, a drug chart with administrations, the full NHS dm+d formulary (17,112 products) with
allergy, dose and interaction alerts and prescribing hard stops, a separate ICE-style system for requests (now, on the
phlebotomy round or at a set time), results with sign-off and cumulative views across GP and hospital, structured
assessments (VTE, Wells DVT), fluid balance, a ward jobs list, blood product requests, referrals and named pathways with
their own forms, discharge with TTOs, and scripted staff who give what is charted and answer referrals.

## Documentation

- [docs/RESULTS.md](docs/RESULTS.md): the agent-CLI runs, with their caveats
- [docs/walkthroughs/](docs/walkthroughs/): how the agents worked each case
- [docs/CLINICAL_DECISIONS.md](docs/CLINICAL_DECISIONS.md): every clinical decision, for review
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design decisions, limitations and roadmap
- [docs/QUESTIONS.md](docs/QUESTIONS.md): the clinical questions as first asked, and the answers given
- [docs/SAFETY.md](docs/SAFETY.md): the unsafe-action catalogue
- [docs/SPEND_SAFETY.md](docs/SPEND_SAFETY.md): how paid SDK runs are capped
- [docs/codex/](docs/codex/): the adversarial reviews the code and cases went through
- [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md): what is checked before publication

## Layout

```
src/eprbench/
  schema.sql, world.py   the record (SQLite in memory) and its simulated clock
  ops.py                 every read and write; the single source for tools, API and UI
  sections.py            the capability map both routes share
  scenario.py, vitals.py how patients and staff respond over time
  tasks.py, assessment.py, answers.py   task format, deterministic marking, the answer form
  episode.py, runner.py  limits, the evidence record, regrading, run plans
  agents/                reference harnesses: SDK tool calls and browser, agent CLIs over the API or the UI
  server/                FastAPI app: JSON API, server-rendered UI, a person's session
  viewer/                eprbench view
  data/                  bundles (fictional patients), tasks, catalogues (dm+d)
scripts/                 screenshots, walk-throughs, the site, the release scan and the public export
site/                    the public site (landing page and trajectory viewer)
```

## Fictional data

Every patient, clinician, staff member and record in this repository is fictional, and any resemblance to a real person
is coincidental. NHS numbers are in the 999 test range, which is never issued to real patients. The formulary is built
from NHS dm+d, which describes medicines, not patients.

## Licence

- Code: Apache License 2.0 (`LICENSE`).
- Cases, synthetic patients, bundles, task definitions, documentation and recorded evidence: Creative Commons Attribution
  4.0 International (CC BY 4.0); `LICENSE-CONTENT.md` lists exactly which paths.
- The formulary derived from NHS dm+d (`src/eprbench/data/catalogues/dmd.json`) and the importer's test release
  (`tests/fixtures/dmd/`) remain under the Open Government Licence v3.0. Contains public sector information licensed
  under the Open Government Licence v3.0 (NHS Business Services Authority, NHS Dictionary of Medicines and Devices).

Attributions are in `NOTICE`; `CITATION.cff` gives the preferred citation.

## Author

Dr Cameron Dent (GitHub: [c4m3r0nn](https://github.com/c4m3r0nn)). Questions, corrections and clinical comments are
welcome as [GitHub issues](https://github.com/c4m3r0nn/EPR-Bench/issues).
