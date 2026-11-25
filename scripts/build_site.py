"""Build the public site (site/): a landing page and the public trajectory viewer, for GitHub Pages.

    uv run --with pillow==13.0.0 python scripts/build_site.py EVIDENCE_DIR [EVIDENCE_DIR ...]

- site/viewer/<task>.html: one page per task, built by `eprbench view --public --regrade` (model and agent names shown,
  check definitions and ground truth left out, local paths and credential-like strings removed, the outcome graded with
  the current marking). One page per task keeps every file to a few MB; screenshots are separate files beside each page
  (<task>_files/), metadata stripped and, with Pillow, reduced to a 256-colour palette (the evidence keeps the
  originals). The viewer's key file, which names the evidence directories, goes to a temporary directory, never site/.
- site/viewer/index.html: the list of those pages.
- site/index.html: the landing page, self-contained (inline CSS, no script, no external asset, no tracker), with the
  results table read from docs/results/episodes.csv (written by scripts/make_walkthroughs.py) and screenshots copied
  from docs/img/ to site/img/.

Run scripts/make_screenshots.py and scripts/make_walkthroughs.py first. GitHub Pages serves site/ through a Pages
workflow (a branch can only be served from / or /docs); nothing here configures it.
"""

import argparse
import csv
import html
import io
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

REPO = "https://github.com/c4m3r0nn/EPR-Bench"
TASKS = [
    ("preview-hypok-judgement", "Low potassium nobody has sorted (judgement)", "potassium"),
    ("preview-hypok-instruction", "Potassium replacement, as instructed", "potassium"),
    ("preview-stemi-judgement", "Chest pain in resus, no plan handed over (judgement)", "stemi"),
    ("preview-stemi-instruction", "Activate PPCI and load, as instructed", "stemi"),
    ("preview-dvt-discharge-judgement", "Discharge request with an unexplained swollen calf (judgement)", "discharge"),
]
AGENTS = ["Claude Code + Sonnet", "Claude Code + Fable", "Codex CLI"]
ROUTES = [("api", "API"), ("ui", "browser")]
SHOTS = [
    ("patient-summary.png", "The patient summary and the patient menu: what a browser agent sees first"),
    ("prescribing-allergy-alert.png", "Prescribing for a patient with a penicillin allergy: the alert needs a reason to override"),
    ("assessment-wells.png", "A structured assessment form (Wells DVT score)"),
    ("cumulative-potassium.png", "ICE cumulative results: a potassium trend across GP and hospital"),
]


def optimise(path: Path) -> None:
    from PIL import Image

    out = io.BytesIO()
    Image.open(path).convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(out, "PNG", optimize=True)
    path.write_bytes(out.getvalue())


def build_viewer(dirs: list[Path], out: Path, quantise: bool) -> dict[str, int]:
    from eprbench.viewer import build

    found = sorted(d for root in dirs for d in root.iterdir() if (d / "result.json").is_file())
    counts = {}
    with tempfile.TemporaryDirectory() as tmp:
        for task, _, _ in TASKS:
            mine = [d for d in found if f"-{task}-" in d.name]
            built = build(mine, out / f"{task}.html", public=True, regrade=True, key=str(Path(tmp) / "key.json"))
            counts[task] = built["episodes"]
    if quantise:
        for png in sorted(out.rglob("*.png")):
            optimise(png)
    return counts


def rows() -> list[dict]:
    with open(ROOT / "docs" / "results" / "episodes.csv") as fh:
        return list(csv.DictReader(fh))


def results_table(data: list[dict]) -> str:
    cells = defaultdict(list)
    for r in data:
        cells[(r["task"], r["agent"], r["route"])].append(r)
    head = "".join(f"<th>{a}<br><span class=muted>{rl}</span></th>" for a in AGENTS for _, rl in ROUTES)
    body = []
    for task, title, _ in TASKS:
        tds = []
        for a in AGENTS:
            for route, _ in ROUTES:
                eps = sorted(cells[(task, a, route)], key=lambda r: r["repeat"])
                tds.append("<td>" + " ".join(f'<span class="tier {r["tier_now"]}" title="repeat {r["repeat"]}">{r["tier_now"][0].upper()}</span>' for r in eps) + "</td>")
        body.append(f"<tr><th scope=row>{html.escape(title)}</th>{''.join(tds)}</tr>")
    n = Counter(r["tier_now"] for r in data)
    then = Counter(r["tier_when_run"] for r in data)
    caption = (f"{len(data)} episodes: {n['complete']} complete, {n['partial']} partial, {n['inactive']} inactive, {n['harmful']} harmful "
               f"(when run: {then['complete']} complete, {then['partial']} partial). Each cell is two repeats; C complete, P partial.")  # fmt: skip
    return f'<div class="table-wrap"><table class="results"><caption>{caption}</caption><thead><tr><th>Task</th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


CSS = """
:root { --bg:#f5f7f8; --card:#fff; --ink:#18242c; --muted:#56656f; --line:#d8dfe3; --accent:#005eb8; --t-complete:#0b6e4f; --t-partial:#a35f00; --t-inactive:#6b7280; --t-harmful:#b42318; --code:#eef1f3; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#101417; --card:#182026; --ink:#e4e9ed; --muted:#9aa7b1; --line:#2b343b; --accent:#6aaaf0; --t-complete:#4fc79a; --t-partial:#e9b54a; --t-inactive:#9aa7b1; --t-harmful:#ff8a80; --code:#1f272d; } }
:root[data-theme="dark"] { --bg:#101417; --card:#182026; --ink:#e4e9ed; --muted:#9aa7b1; --line:#2b343b; --accent:#6aaaf0; --t-complete:#4fc79a; --t-partial:#e9b54a; --t-inactive:#9aa7b1; --t-harmful:#ff8a80; --code:#1f272d; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; }
a { color: var(--accent); }
h1 { font-size: 30px; line-height: 1.2; margin: 8px 0 4px; } h2 { font-size: 21px; margin: 36px 0 10px; } h3 { font-size: 17px; margin: 18px 0 6px; }
p, li { max-width: 72ch; }
.lede { font-size: 18px; color: var(--ink); }
.muted { color: var(--muted); }
.status { display: inline-block; font-size: 13px; font-weight: 600; padding: 2px 10px; border-radius: 999px; background: var(--code); color: var(--muted); }
.nav { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 14px 0 0; padding: 0; list-style: none; }
.shots { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
figure { margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
figure img { display: block; width: 100%; height: auto; border-bottom: 1px solid var(--line); }
figcaption { padding: 8px 12px; font-size: 14px; color: var(--muted); }
.table-wrap { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
caption { caption-side: bottom; text-align: left; padding: 8px 12px; color: var(--muted); font-size: 13px; }
th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
thead th { font-size: 13px; }
tbody th { font-weight: 500; min-width: 210px; }
.tier { display: inline-block; min-width: 22px; text-align: center; padding: 0 6px; margin: 1px; border-radius: 999px; font-weight: 700; font-size: 12px; color: var(--card); }
.tier.complete { background: var(--t-complete); } .tier.partial { background: var(--t-partial); } .tier.inactive { background: var(--t-inactive); } .tier.harmful { background: var(--t-harmful); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; }
.card h3 { margin-top: 0; }
pre { background: var(--code); border-radius: 8px; padding: 12px 14px; overflow-x: auto; font-size: 13.5px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 14px; color: var(--muted); }
"""


def landing(data: list[dict]) -> str:
    shots = "".join(f'<figure><img src="img/{n}" alt="{html.escape(c)}" width="1280" height="800" loading="lazy"><figcaption>{html.escape(c)}</figcaption></figure>' for n, c in SHOTS)
    walk = lambda name: f"{REPO}/blob/main/docs/walkthroughs/{name}.md"  # noqa: E731
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EPR-Bench</title>
<meta name="description" content="A deterministic synthetic NHS-style electronic patient record for testing AI agents, through an API or through the screen alone.">
<style>{CSS}</style>
</head>
<body>
<main>
<span class="status">Preview: research software, not for clinical use</span>
<h1>EPR-Bench</h1>
<p class="lede">A synthetic, NHS-style electronic patient record for testing AI agents on the jobs junior doctors do: finding results,
prescribing, requesting, referring and handing over. The same record can be worked through a JSON API or, with no API at
all, through its web pages from screenshots, mouse and keyboard. Every patient is fictional.</p>
<ul class="nav">
<li><a href="#results">What the agents did</a></li>
<li><a href="viewer/index.html">Trajectory viewer</a></li>
<li><a href="{REPO}">Code and documentation</a></li>
<li><a href="#run">Run it</a></li>
<li><a href="#limits">Limits</a></li>
</ul>

<h2>What it is</h2>
<p>A simulated hospital record with demographics, notes, observations with NEWS2, a drug chart with a full NHS dm+d
formulary and prescribing alerts, a separate ICE-style requesting and results system, referral pathways with their own
forms, structured assessments, a ward jobs list and discharge. Time passes only when the agent waits; results come back
after a turnaround, scripted nurses give what is charted, teams answer referrals, and patients get worse or better
according to what is done.</p>
<p>Each episode is marked by deterministic checks on the final state of the record, including an audit of what the agent
did and was shown: no language model judges it. Every episode gets a tier (harmful, inactive, partial or complete), never
a blended score, and a clinician is meant to read every one.</p>
<div class="shots">{shots}</div>

<h2 id="results">What the agents did</h2>
<p>On 23 November 2026 two agent command-line products worked the five preview tasks on both routes, twice each: Claude
Code (on its default model that day, which it reported as <code>claude-sonnet-5-5</code>, and pinned to
<code>claude-fable-5-2</code>) and Codex CLI 0.171.0 on its default model. That is 60 episodes, marked again here with the
current rules.</p>
{results_table(data)}
<div class="cards" style="margin-top:14px">
<div class="card"><h3><a href="{walk('potassium')}">Low potassium</a></h3><p>Asked only to "sort" a potassium of 3.1, every Codex run rechecked it first, then gave one dose with a same-day recheck; every Claude Code run started a course at once and rechecked the next morning. The three partial runs never signed off last night's result.</p></div>
<div class="card"><h3><a href="{walk('stemi')}">Chest pain in resus</a></h3><p>All 24 runs activated primary PCI in time and none gave a nitrate to a hypotensive patient. They differed in whether they also called the ED senior, charted analgesia, and gave the antiplatelet loading dose before or after cardiology replied.</p></div>
<div class="card"><h3><a href="{walk('discharge')}">Discharge with a swollen calf</a></h3><p>No agent discharged her. All twelve requested a leg Doppler and held the discharge; they differed in how they wrote down the reassessment, and in whether they waited four simulated hours for the scan and treated the clot it showed.</p></div>
</div>
<p>Full tables, denominators and the effort per route are in <a href="{REPO}/blob/main/docs/RESULTS.md">RESULTS.md</a>.
Every episode can be read action by action, with the agent's own screenshots, in the <a href="viewer/index.html">trajectory viewer</a>.</p>

<h2 id="limits">Read these results with their limits</h2>
<ul>
<li>These are agent products (their own prompts, loops and tools), not bare models. Codex's event stream does not name its model.</li>
<li>Two repeats per cell on three draft cases: exploratory description, not a comparison or a ranking.</li>
<li>Actions are not comparable across routes: an API call against a click or keystroke.</li>
<li>Staff replies are scripted, and each agent chose how long to wait, so they saw different things.</li>
<li>Only structured entries are marked: a Wells score written in prose is not a Wells form. A screenshot delivered to an agent does not show that the model attended to it.</li>
<li>The cases were written by the developer and reviewed by one clinician; several marking rules are AI-proposed and await review
(<a href="{REPO}/blob/main/docs/CLINICAL_DECISIONS.md">CLINICAL_DECISIONS.md</a>). Nothing here says anything about clinical safety.</li>
</ul>

<h2 id="run">Run it</h2>
<pre><code>uv sync --all-extras
uv run playwright install chromium
uv run eprbench tasks
uv run eprbench serve preview-dvt-discharge-judgement --mode ui     # http://127.0.0.1:8000

# a logged-in agent CLI on a case, through each route (subscription, no API key)
uv run eprbench run preview-hypok-judgement --agent claude --interface api --out-dir results/today
uv run eprbench run preview-hypok-judgement --agent codex --interface ui --out-dir results/today

# a person as a baseline, and the viewer
uv run eprbench serve preview-stemi-judgement --mode ui --user "Dr J. Smith (FY1)" --out-dir results/human
uv run eprbench view results/today --out today.html</code></pre>

<h2>Review the cases</h2>
<p>If you are a clinician, the most useful thing you can do is read <a href="{REPO}/blob/main/docs/CLINICAL_DECISIONS.md">the clinical decisions log</a>
and open an issue on any row you think is wrong, quoting its ID.</p>

<footer>
<p>By Dr Cameron Dent (<a href="https://github.com/c4m3r0nn">c4m3r0nn</a>). Code under the Apache License 2.0; cases, documentation and recorded
evidence under CC BY 4.0; the formulary contains public sector information licensed under the Open Government Licence v3.0
(NHS Business Services Authority, NHS Dictionary of Medicines and Devices). All patients, staff and records are fictional;
NHS numbers are in the 999 test range. Not for clinical use. This page loads nothing from anywhere else.</p>
</footer>
</main>
</body>
</html>
"""


def viewer_index(counts: dict[str, int]) -> str:
    items = "".join(f'<li><a href="{t}.html">{html.escape(title)}</a> <span class="muted">({counts.get(t, 0)} episodes)</span></li>' for t, title, _ in TASKS)
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EPR-Bench trajectories</title>
<style>{CSS}</style>
</head>
<body>
<main>
<p><a href="../index.html">EPR-Bench</a></p>
<h1>Trajectory viewer</h1>
<p>Every episode of the runs of 23 November 2026, one page per task: each action in order with the simulated time, what the
record returned, the agent's own screenshots on the browser route, its reply and the automatic outcome, marked again with
the current rules (the page says when that changed a tier). Check definitions are left out. Screenshots are reduced to a
256-colour palette to keep the pages small; the agents saw the originals.</p>
<ul>{items}</ul>
<p class="muted">Each page has a review panel that keeps a reader's verdicts in their own browser, to export as a file;
nothing is sent anywhere. All patients and records are fictional. Not for clinical use.</p>
</main>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dirs", nargs="+", help="evidence directories")
    parser.add_argument("--out", default=str(ROOT / "site"))
    parser.add_argument("--no-optimise", action="store_true", help="keep the screenshots as they are (no Pillow needed)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if (out / "viewer").exists():
        shutil.rmtree(out / "viewer")
    counts = build_viewer([Path(d) for d in args.dirs], out / "viewer", quantise=not args.no_optimise)
    (out / "viewer" / "index.html").write_text(viewer_index(counts))
    (out / "img").mkdir(parents=True, exist_ok=True)
    for name, _ in SHOTS:
        shutil.copyfile(ROOT / "docs" / "img" / name, out / "img" / name)
    (out / "index.html").write_text(landing(rows()))
    (out / ".nojekyll").write_text("")
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    biggest = max((f for f in out.rglob("*") if f.is_file()), key=lambda f: f.stat().st_size)
    print(f"site/ built: {sum(counts.values())} episodes on {len(counts)} viewer pages; {total / 1e6:.1f} MB; largest file {biggest.relative_to(out)} "
          f"({biggest.stat().st_size / 1e6:.1f} MB)")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
