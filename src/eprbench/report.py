"""Turn run reports (one JSON object per line) into readable tables.

A run that reached a limit (truncated), failed in the harness or has no score (errored), showed the agent had seen
the ground truth (contaminated), or ran an agent CLI without its sandbox (unsandboxed) is not a clinical outcome:
each is shown in its own column and left out of the solved and mean-score figures, whose denominator is the completed
runs. Runs only suspected of contamination still count, and are shown. Unsafe runs and safety findings are counted
over every run, since harm done before a limit is still harm.

Reports carrying an outcome (tasks.grade) are also tabulated by tier, with proportions and Wilson intervals, and
their harms, near misses, scope violations and waste are listed. Harm is only ever counted, never averaged.

Effort is counted as distinct agent actions per interface (reads and writes apart). Reviews a clinician exported
from the viewer (eprbench view) give an agreement table: the automatic tier against the clinician's, with counts.
"""

import json
from collections import defaultdict
from math import sqrt
from pathlib import Path
from statistics import mean


def load_reports(paths: list[str]) -> list[dict]:
    reports = []
    for path in paths:
        for line in Path(path).read_text().splitlines():
            if line.strip().startswith("{"):
                reports.append(json.loads(line))
    return reports


TRUNCATED = {"model_request_limit", "action_limit", "timeout", "context_limit", "spend_limit"}
ERRORS = {"harness_error", "agent_usage_limit"}  # as episode.ERRORS: infrastructure failures, never a clinical outcome


def _who(r: dict) -> str:
    return f"{r.get('agent') or r.get('model', '?')} ({r.get('interface', '?')})"


def outcome(r: dict) -> str:
    """"completed", "truncated", "errored", "contaminated" or "unsandboxed". Reports from before terminal reasons
    existed count as completed."""
    reason = r.get("terminal_reason")
    if r.get("contaminated"):
        return "contaminated"
    if r.get("sandboxed") is False:
        return "unsandboxed"
    if reason in ERRORS:  # the harness, or the agent's subscription, failed
        return "errored"
    if reason in TRUNCATED:
        return "truncated"
    return "errored" if r.get("score") is None else "completed"


def summary_table(reports: list[dict]) -> str:
    """One row per agent and interface."""
    groups = defaultdict(list)
    for r in reports:
        groups[_who(r)].append(r)
    lines = [
        "| Agent | Runs | Completed | Truncated | Errored | Contaminated | Unsandboxed | Suspected | Solved (of completed) | Mean score (completed) "
        "| Unsafe | Runs with safety findings | Mean operations | Mean investigation cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for who, rs in sorted(groups.items()):
        done = [r for r in rs if outcome(r) == "completed"]
        solved = sum(bool(r["success"]) for r in done)
        flagged = sum(bool(r.get("safety_findings")) for r in rs)
        rate = f"{solved} of {len(done)} ({solved / len(done):.0%})" if done else "-"
        score = f"{mean(r['score'] for r in done):.2f}" if done else "-"
        lines.append(
            f"| {who} | {len(rs)} | {len(done)} | "
            + " | ".join(str(sum(outcome(r) == kind for r in rs)) for kind in ("truncated", "errored", "contaminated", "unsandboxed"))
            + f" | {sum(bool(r.get('suspected')) for r in rs)} | {rate} | {score} "
            f"| {sum(bool(r.get('unsafe')) for r in rs)} | {flagged} | {mean(r.get('operations') or 0 for r in rs):.0f} "
            f"| £{mean(r.get('investigation_cost_gbp') or 0 for r in rs):.2f} |"
        )
    return "\n".join(lines)


def task_table(reports: list[dict]) -> str:
    """One row per task, one column per agent: mean score over completed repeats, with ! where any run was unsafe and
    the number of truncated (T), errored (E), contaminated (C) or unsandboxed (U) runs, which the mean leaves out."""
    agents = sorted({_who(r) for r in reports})
    cells = defaultdict(list)
    for r in reports:
        cells[r["task_id"], _who(r)].append(r)
    lines = ["| Task | " + " | ".join(agents) + " |", "| --- |" + " --- |" * len(agents)]
    for task_id in sorted({r["task_id"] for r in reports}):
        row = []
        for agent in agents:
            rs = cells.get((task_id, agent)) or []
            done = [r for r in rs if outcome(r) == "completed"]
            cell = (f"{mean(r['score'] for r in done):.2f}" if done else "-" if rs else "") + ("!" if any(r.get("unsafe") for r in rs) else "")
            for kind, mark in (("truncated", "T"), ("errored", "E"), ("contaminated", "C"), ("unsandboxed", "U")):
                if n := sum(outcome(r) == kind for r in rs):
                    cell += f" ({n}{mark})"
            row.append(cell)
        lines.append(f"| {task_id} | " + " | ".join(row) + " |")
    return "\n".join(lines)


def failures(reports: list[dict]) -> str:
    """Every failed check and safety finding, for reading rather than counting."""
    lines = []
    for r in sorted(reports, key=lambda r: (r["task_id"], _who(r))):
        failed = [c["id"] for c in r.get("checks", []) if not c["passed"] and c.get("role") != "bonus"]  # a missed bonus is no failure
        found = [f"{f['code']} ({f['detail']})" for f in r.get("safety_findings", [])]
        status = [] if outcome(r) == "completed" else [f"{outcome(r)} ({r.get('terminal_reason')})"]
        if failed or found or status:
            parts = status + ([f"failed {', '.join(failed)}"] if failed else []) + ([f"findings: {', '.join(found)}"] if found else [])
            lines.append(f"- **{r['task_id']}**, {_who(r)}: " + "; ".join(parts))
    return "\n".join(lines) or "None."


TIERS = ("harmful", "inactive", "partial", "complete")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for k of n (95% by default)."""
    if not n:
        return 0.0, 1.0
    p = k / n
    centre, half = p + z * z / (2 * n), z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - half) / (1 + z * z / n)), min(1.0, (centre + half) / (1 + z * z / n))


def _share(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k} ({k / n:.0%}, {lo:.0%}-{hi:.0%})" if n else "-"


def _tier(r: dict) -> str | None:
    return (r.get("outcome") or {}).get("tier")


def tier_table(reports: list[dict]) -> str:
    """One row per agent and interface: completed runs by tier, each a count with its proportion and 95% Wilson
    interval. Harm, observed near misses, scope and waste are counted over every run with an outcome, as harm done
    before a limit is still harm; truncated runs whose projected harm their tier leaves out (by policy) are counted
    apart. Completion is the best pathway's, harmful runs included, so benefit is kept; nothing is averaged with harm."""
    groups = defaultdict(list)
    for r in reports:
        groups[_who(r)].append(r)
    lines = [
        "| Agent | Completed with an outcome | " + " | ".join(t.capitalize() for t in TIERS) + " | Harmful (any run) | Truncated with projected harm "
        "| Runs with observed near misses | Runs with scope violations | Wasteful orders (any run) | Mean completion (completed) |",
        "| --- |" + " --- |" * 11,
    ]
    for who, rs in sorted(groups.items()):
        assessed = [r for r in rs if _tier(r)]
        done = [r for r in assessed if outcome(r) == "completed"]
        o = [r["outcome"] for r in assessed]
        projected = sum(outcome(r) == "truncated" and bool(_left_out(r["outcome"])) for r in assessed)
        paths = [r["outcome"]["pathway"]["completion"] for r in done if r["outcome"]["pathway"]]
        waste = sum(x["waste"]["count"] for x in o)
        lines.append(
            f"| {who} | {len(done)} | " + " | ".join(_share(sum(_tier(r) == t for r in done), len(done)) for t in TIERS)
            + f" | {sum(_tier(r) == 'harmful' for r in assessed)} of {len(assessed)} | {projected} | {sum(any(not m['projected'] for m in x['near_misses']) for x in o)} "
            f"| {sum(bool(x['scope_violations']) for x in o)} | {waste} (£{sum(x['waste']['gbp'] for x in o):.2f}) "
            f"| {f'{mean(paths):.2f}' if paths else '-'} |"
        )
    return "\n".join(lines)


def task_tier_table(reports: list[dict]) -> str:
    """One row per task, one column per agent: completed runs by tier (H harmful, I inactive, P partial, C complete),
    with runs left out as in the score table (T, E, C for contaminated, U)."""
    agents = sorted({_who(r) for r in reports})
    cells = defaultdict(list)
    for r in reports:
        cells[r["task_id"], _who(r)].append(r)
    lines = ["| Task | " + " | ".join(agents) + " |", "| --- |" + " --- |" * len(agents)]
    for task_id in sorted({r["task_id"] for r in reports}):
        row = []
        for agent in agents:
            rs = cells.get((task_id, agent)) or []
            done = [r for r in rs if outcome(r) == "completed" and _tier(r)]
            cell = " ".join(f"{n}{t[0].upper()}" for t in TIERS if (n := sum(_tier(r) == t for r in done))) or ("-" if rs else "")
            for kind, mark in (("truncated", "T"), ("errored", "E"), ("contaminated", "C"), ("unsandboxed", "U")):
                if n := sum(outcome(r) == kind for r in rs):
                    cell += f" ({n}{mark})"
            row.append(cell)
        lines.append(f"| {task_id} | " + " | ".join(row) + " |")
    return "\n".join(lines)


def outcome_details(reports: list[dict]) -> str:
    """Every harm, near miss, scope violation and wasteful order, for reading rather than counting."""
    lines = []
    for r in sorted((r for r in reports if _tier(r)), key=lambda r: (r["task_id"], _who(r))):
        o = r["outcome"]
        parts = [f"harm {h['id']}" + (f" ({h['event']})" if h["event"] else "") + _projected(h) for h in o["harms"]]
        parts += [f"near miss {m['id']}" + (f" ({m['event']})" if m["event"] else "") + _projected(m) for m in o["near_misses"]]
        parts += [f"scope {s['id']}" for s in o["scope_violations"]]
        parts += [f"waste {', '.join(w['orderable_code'] for w in o['waste']['orders'])} (£{o['waste']['gbp']:.2f})"] if o["waste"]["count"] else []
        if parts:
            status = "" if outcome(r) == "completed" else f" [{outcome(r)}]"
            lines.append(f"- **{r['task_id']}**, {_who(r)}, {o['tier']}{status}: " + "; ".join(parts))
    return "\n".join(lines) or "None."


def _left_out(o: dict) -> list[dict]:
    """The harm a tier leaves out: projected harm on a truncated run."""
    return [h for h in o["harms"] if not h.get("in_tier", True)]


def _projected(e: dict) -> str:
    return "" if not e["projected"] else " [projected]" if e.get("in_tier", True) else " [projected, not in the tier]"


def truncated_runs(reports: list[dict]) -> str:
    """Every truncated run with its harm, observed and projected: its tier is not a clinical outcome and leaves projected
    harm out, so the harm is shown here and cannot be hidden by spending the budget on reads."""
    lines = []
    for r in sorted((r for r in reports if outcome(r) == "truncated"), key=lambda r: (r["task_id"], _who(r))):
        o = r.get("outcome") or {"harms": [], "near_misses": []}
        parts = [f"{'projected' if h['projected'] else 'observed'} harm {h['id']}" + (f" ({h['event']})" if h["event"] else "") for h in o["harms"]]
        parts += [f"{'projected' if m['projected'] else 'observed'} near miss {m['id']}" for m in o["near_misses"]]
        lines.append(f"- **{r['task_id']}**, {_who(r)} ({r.get('terminal_reason')}): " + ("; ".join(parts) or "no harm observed or projected"))
    return "\n".join(lines) or "None."


def trajectory(report: dict) -> str:
    """The operations of one run in order, as recorded in the audit log."""
    score = "-" if report.get("score") is None else f"{report['score']:.2f}"
    lines = [f"{report['task_id']} - {_who(report)} - score {score}" + (f" - {report['terminal_reason']}" if report.get("terminal_reason") else "")]
    for step in report.get("trajectory", []):
        mark = "ok " if step["ok"] else f"ERR {step['error']}"
        lines.append(f"  {step['at']}  {step['op']}({json.dumps(step['args'], separators=(',', ':'))})  {mark}")
    return "\n".join(lines)


KEYS = ("actions", "read_actions", "write_actions", "other_actions", "operations")


def effort_table(reports: list[dict]) -> str:
    """Effort as distinct agent actions (audit action ids: one tool call, one API call, or one page load or form in
    the UI), per agent and per interface the actions came through, as means over the runs where it was measured
    (`actions_summary`; a run that did nothing counts as zero): actions that changed the record, actions that only
    read it, and the rest (failed writes and waits). Runs from before it was measured are counted as not measured.
    (A report's `effort` is the reasoning effort asked of the model, not this.)"""
    measured, unmeasured = defaultdict(list), defaultdict(int)
    for r in reports:
        summary = r.get("actions_summary")
        if not isinstance(summary, dict):
            unmeasured[_who(r)] += 1
            continue
        measured[_who(r)].append(summary)
    if not measured:
        return f"Effort was not measured in these runs ({sum(unmeasured.values())} not measured)."
    lines = ["| Agent | Interface | Runs measured | Runs not measured | Mean EPR requests | Mean reading requests | Mean writing requests "
             "| Mean other requests | Mean operations |", "| --- |" + " --- |" * 8]  # fmt: skip
    for who in sorted(measured):
        runs = measured[who]
        for interface in sorted({i for s in runs for i in s}):
            rows = [s.get(interface) or dict.fromkeys(KEYS, 0) for s in runs]
            means = " | ".join(f"{mean((e.get(k) or 0) for e in rows):.1f}" for k in KEYS)
            lines.append(f"| {who} | {interface} | {len(runs)} | {unmeasured[who]} | {means} |")
    if left := sorted(set(unmeasured) - set(measured)):
        lines.append("\nNot measured (runs from before effort was recorded): " + ", ".join(f"{w} ({unmeasured[w]})" for w in left) + ".")
    return "\n".join(lines)


ACCOUNTING = ("actions", "screenshot_requests", "automatic_screenshots", "model_requests")


def interaction_table(reports: list[dict]) -> str:
    """Per agent and interface, the means of the four counts every episode records (episode.ACCOUNTING): actions charged
    against the action limit (operations, or mouse and keyboard actions), screenshots the model asked for (not actions),
    screenshots sent without asking, and model requests. Browser harnesses differ in how they show the screen, so the
    counts are given apart rather than as one number of steps."""
    groups = defaultdict(list)
    for r in reports:
        if isinstance(r.get("accounting"), dict):
            groups[_who(r)].append(r["accounting"])
    if not groups:
        return "Not recorded in these runs."
    lines = ["| Agent | Runs | Agent actions (charged) | Screenshot requests | Automatic screenshots | Model requests |", "| --- |" + " --- |" * 5]
    for who, rows in sorted(groups.items()):
        means = [[a[k] for a in rows if a.get(k) is not None] for k in ACCOUNTING]
        lines.append(f"| {who} | {len(rows)} | " + " | ".join(f"{mean(v):.1f}" if v else "-" for v in means) + " |")
    return "\n".join(lines)


def cost_table(reports: list[dict]) -> str:
    """Spend per agent and interface, from runs made under a spend ceiling (eprbench.spend: priced from recorded
    usage with the prices file named in each run). Agent CLI runs use a subscription and are not priced."""
    groups = defaultdict(list)
    for r in reports:
        if isinstance(r.get("cost"), dict):
            groups[_who(r)].append(r["cost"])
    if not groups:
        return "No run was priced (only runs under --max-spend are)."
    lines = ["| Agent | Priced runs | Total (£) | Mean per run (£) | Most in one run (£) | Runs stopped by the ceiling |", "| --- |" + " --- |" * 5]
    for who, costs in sorted(groups.items()):
        gbp = [c["gbp"] for c in costs]
        lines.append(f"| {who} | {len(costs)} | {sum(gbp):.4f} | {mean(gbp):.4f} | {max(gbp):.4f} | {sum(bool(c.get('refused')) for c in costs)} |")
    return "\n".join(lines)


# --- clinician review ------------------------------------------------------------------------------------------


def episode_id(name: str) -> str:
    """The stable id of a saved episode, from its directory's name; it does not show the model (see eprbench.viewer)."""
    import hashlib

    return hashlib.sha256(Path(name).name.encode()).hexdigest()[:12]


def load_reviews(path: str) -> dict:
    """The reviews in a file exported from the viewer, by episode id."""
    data = json.loads(Path(path).read_text())
    if data.get("format") != "eprbench-reviews/1":
        raise ValueError(f"{path} is not a reviews file exported from eprbench view")
    return data["reviews"]


def agreement_table(reviews: dict, rows: list[dict]) -> str:
    """The automatic tier against the clinician's, with counts. `rows` are run reports (matched to reviews through
    their evidence directory) or the viewer's episode rows (`id`, `tier`). The automatic tier is the current one where
    the episode is known, else the one the reviewer saw. The clinician's tier is the one they gave, or the automatic
    tier they agreed with."""
    current = {}
    for r in rows:
        key = r.get("id") or (episode_id(r["evidence_dir"]) if r.get("evidence_dir") else None)
        if key:
            current[key] = r.get("tier", _tier(r))
    columns = [*TIERS, None]
    counts = defaultdict(int)
    verdicts, checks = defaultdict(int), defaultdict(int)
    for key, rv in reviews.items():
        auto = current.get(key, rv.get("automatic_tier"))
        mine = rv.get("tier") or (rv.get("automatic_tier") if rv.get("verdict") == "agree" else None)
        counts[auto, mine] += 1
        verdicts[rv.get("verdict") or "none"] += 1
        for check, mark in (rv.get("checks") or {}).items():
            checks[check] += mark == "disagree"
    if not reviews:
        return "No reviews."
    head = "| Automatic \\ clinician | " + " | ".join(t.capitalize() if t else "No tier given" for t in columns) + " | Total |"
    lines = [head, "| --- |" + " --- |" * (len(columns) + 1)]
    for auto in [*TIERS, None]:
        n = [counts[auto, c] for c in columns]
        if sum(n):
            lines.append(f"| {auto.capitalize() if auto else 'Not graded'} | " + " | ".join(map(str, n)) + f" | {sum(n)} |")
    rated = sum(v for (a, c), v in counts.items() if c is not None and a is not None)
    same = sum(v for (a, c), v in counts.items() if c is not None and a == c)
    lines += ["", f"Reviewed: {len(reviews)}. Verdicts: " + ", ".join(f"{k} {verdicts[k]}" for k in ("agree", "disagree", "none") if verdicts[k]) + ". "
              + (f"Same tier in {same} of {rated} ({same / rated:.0%})." if rated else "No tier to compare.")]  # fmt: skip
    if any(checks.values()):
        lines.append("Checks the clinician disagreed with: " + ", ".join(f"{k} ({v})" for k, v in sorted(checks.items()) if v) + ".")
    return "\n".join(lines)


def render(reports: list[dict]) -> str:
    return (
        f"## Summary\n\nTruncated, errored, contaminated and unsandboxed runs are not clinical outcomes: they are excluded from solved and mean score.\n\n"
        f"{summary_table(reports)}\n\n## Outcome by tier\n\nTiers are counted over completed runs, each with a 95% Wilson interval; harm is "
        f"also counted over every run and never averaged into a mean.\n\n{tier_table(reports)}\n\n## Tier by task\n\n{task_tier_table(reports)}\n\n"
        f"## Score by task\n\n{task_table(reports)}\n\n## Harm, near misses, scope and waste\n\n{outcome_details(reports)}\n\n"
        f"## Truncated runs\n\nA truncated run's tier is not a clinical outcome, and by policy leaves out projected harm, which is listed here.\n\n"
        f"{truncated_runs(reports)}\n\n"
        f"## Effort\n\nDistinct agent actions per run (one tool call, one API call, or one page load or form in the UI), by the interface they "
        f"came through.\n\n{effort_table(reports)}\n\n"
        f"## Interaction counts\n\nActions are charged against the action limit; screenshots a browser model asks for are not actions "
        f"(capped apart), and screenshots sent without asking are counted on their own.\n\n{interaction_table(reports)}\n\n"
        f"## Cost\n\nPriced from recorded token usage with the run's prices file, by the interface the runs came through.\n\n{cost_table(reports)}\n\n"
        f"## Failed checks and safety findings\n\n{failures(reports)}\n"
    )
