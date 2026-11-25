"""Saved episodes as one self-contained HTML page, for the clinician who reads every episode (eprbench view).

The page has an index of episodes (task, interface, harness and model, tier, terminal reason, truncated and
contaminated), filterable, and for each episode:

- a timeline in the order things happened: the model's text and reasoning as recorded; each action (a tool call,
  an API call, a browser action, or a person's page load) with its arguments, the operations it ran with what they
  returned (audit.jsonl, by action id), the simulated time, the patient whose record it touched (with a banner) and
  for browser runs the screenshots; scripted staff responses and other scenario events, results reported and notes
  filed by others, placed where the simulated clock passed them; every submission and the final answer;
- the outcome: tier, pathway (requirements met and missing), harms and near misses (observed or projected),
  scope violations, waste, bonuses, safety findings and every check;
- a review panel: agree or disagree with the automatic grade, the clinician's own tier, a comment, and a disagree
  toggle per check, kept in the browser (localStorage) and exported to, or imported from, one JSON file.

Read-only steps are collapsed, writes are highlighted, and actions on another patient's record are marked: a write
to a patient the task does not concern (patients named in its checks or jobs), or an attempt the record refused
as the wrong patient.

`blind` replaces the harness, provider, model and agent with a stable code (the key goes to a separate file) and
blanks vendor and model names wherever they appear in the text. `public` (implied by `blind`) leaves out what a
published page must not carry: the checks' definitions (SQL, patterns, accepted answers), and anything of the
scenario but what happened (rules that did not fire, regimens and hidden results never appear in any mode). The
page needs no network: CSS and JavaScript are inline, and screenshots are linked relative to the page, copied
beside it (blind or public) or embedded (`embed`).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
from importlib import resources
from pathlib import Path

from eprbench.episode import redact_output
from eprbench.identifiers import format_nhs_number
from eprbench.report import episode_id, load_reviews
from eprbench.world import World, iso, parse_iso

TEXT_LIMIT = 20_000  # characters of one result shown in the page; the evidence keeps all of it
VENDOR = re.compile(r"\b(?:claude[\w.-]*|anthropic|openai|chatgpt|gpt[\w.-]*|codex|gemini[\w.-]*|google|opus|sonnet|haiku|fable)\b", re.I)
WHO = ("harness", "provider", "model", "agent", "effort")


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _lines(path: Path) -> list:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _clip(value):
    """A result for the page: as recorded, or as text cut at TEXT_LIMIT characters when it is longer."""
    text = json.dumps(value, default=str)
    return value if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + f" ... [{len(text) - TEXT_LIMIT} more characters in the evidence]"


def find(dirs) -> list[Path]:
    """Every saved episode (a directory with result.json and episode.json) under the given directories."""
    found = set()
    for root in map(Path, dirs):
        for result in [root / "result.json", *root.rglob("result.json")]:
            if result.is_file() and (result.parent / "episode.json").is_file():
                found.add(result.parent)
    return sorted(found)


# --- what the model said -----------------------------------------------------------------------------------------


def model_says(body) -> tuple[list[str], list[str]]:
    """(text, reasoning) in one recorded model response, whichever provider's shape it has."""
    texts, thoughts = [], []
    if not isinstance(body, dict):
        return texts, thoughts
    for b in body.get("content") or []:  # Anthropic
        if isinstance(b, dict):
            if b.get("type") == "text" and b.get("text"):
                texts.append(b["text"])
            elif b.get("type") == "thinking" and b.get("thinking"):
                thoughts.append(b["thinking"])
            elif b.get("type") == "redacted_thinking":
                thoughts.append("[reasoning withheld by the provider]")
    for item in body.get("output") or []:  # OpenAI Responses
        if isinstance(item, dict) and item.get("type") == "message":
            texts += [c.get("text") or c.get("refusal") or "" for c in item.get("content") or [] if isinstance(c, dict)]
        elif isinstance(item, dict) and item.get("type") == "reasoning":
            thoughts += [s.get("text", "") for s in item.get("summary") or [] if isinstance(s, dict)]
    for step in body.get("steps") or []:  # Gemini Interactions
        if isinstance(step, dict) and step.get("type") == "thought":
            thoughts += [s.get("text", "") for s in step.get("summary") or [] if isinstance(s, dict)] or ["[reasoning]"]
        elif isinstance(step, dict) and step.get("type") == "text" and step.get("text"):
            texts.append(step["text"])
    if not texts and body.get("output_text") and not body.get("content"):
        texts.append(body["output_text"])
    return [t for t in texts if t], [t for t in thoughts if t]


def agent_log(stdout: str) -> list[dict]:
    """An agent CLI's own record, in order: its messages, reasoning and commands (Claude Code's stream-json, Codex's
    --json events, or plain text), each with `line`, the number of output lines up to and including the one it came
    from, to set against the output_mark each API call recorded when it arrived."""
    items, commands = [], {}
    for n, line in enumerate(stdout.splitlines(), start=1):
        try:
            e = json.loads(line) if line.startswith("{") else None
        except ValueError:
            e = None
        if not isinstance(e, dict):
            if line.strip():
                if items and items[-1].get("plain") and items[-1]["line"] == n - 1:
                    items[-1]["text"][0] += "\n" + line
                    items[-1]["line"] = n
                else:
                    items.append({"type": "model", "text": [line], "reasoning": [], "line": n, "plain": True})
            continue
        kind, item = e.get("type"), e.get("item") or {}
        if kind == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text"):
                    items.append({"type": "model", "text": [b["text"]], "reasoning": [], "line": n})
                elif b.get("type") == "thinking" and b.get("thinking"):
                    items.append({"type": "model", "text": [], "reasoning": [b["thinking"]], "line": n})
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    commands[b.get("id")] = c = {"type": "command", "command": inp.get("command") or json.dumps(inp), "output": None, "line": n}
                    items.append(c)
        elif kind == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in commands:
                    content = b.get("content")
                    commands[b["tool_use_id"]]["output"] = content if isinstance(content, str) else json.dumps(content)
        elif kind == "item.started" and item.get("type") == "command_execution":  # Codex: placed where it started
            commands[item.get("id")] = c = {"type": "command", "command": item.get("command", ""), "output": None, "line": n}
            items.append(c)
        elif kind == "item.completed":
            if item.get("type") == "agent_message" and item.get("text"):
                items.append({"type": "model", "text": [item["text"]], "reasoning": [], "line": n})
            elif item.get("type") == "reasoning" and item.get("text"):
                items.append({"type": "model", "text": [], "reasoning": [item["text"]], "line": n})
            elif item.get("type") == "command_execution":
                if item.get("id") in commands:
                    commands[item["id"]]["output"] = item.get("aggregated_output")
                else:
                    items.append({"type": "command", "command": item.get("command", ""), "output": item.get("aggregated_output"), "line": n})
    for item in items:
        item.pop("plain", None)
        if item["type"] == "command" and item["output"] is not None:
            item["output"] = _clip(item["output"])
    return items


# --- the world ---------------------------------------------------------------------------------------------------


def _effects(effects: dict) -> list[str]:
    """What a scenario rule did that anyone could see; what tests would show and monitor trajectories stay hidden."""
    out = [f"note filed: {n.get('title')} ({n.get('author')})" for n in effects.get("notes", [])]
    out += [f"observations charted ({len(effects['observations'])} set{'s' if len(effects['observations']) > 1 else ''})"] if effects.get("observations") else []
    if u := effects.get("referral_update"):
        out.append(f"referral {u.get('status')} by {u.get('by')}: {u.get('text')}")
    out += [f"{m.get('drug')} {m.get('dose')} {m.get('route')} {m.get('frequency')} prescribed by {m.get('prescriber')}" for m in effects.get("medications", [])]
    out += ["the prescription is given (recorded below)"] if effects.get("administer") else []
    if any(effects.get(k) for k in ("vitals", "result_windows", "latent_results")):
        out.append("the patient's course changed")
    return out


def _q(world: World, sql: str, params=()) -> list[dict]:
    """A query over a saved world, or nothing where the world predates the tables it reads."""
    try:
        return world.q(sql, params)
    except sqlite3.OperationalError:
        return []


def world_events(world: World, start: int, actor: str) -> list[dict]:
    """What happened in the record that the agent did not do, between the start and the end of the episode, by
    simulated time: scenario responses that took effect, results reported on requests made in the episode, and notes
    and medication administrations by other people."""
    end, out = world.now, []
    for r in _q(
        world,
        """SELECT a.applied_at AS at, a.variant, r.name, r.repeat, r.effects, r.variants, a.patient_id FROM scenario_application a
             JOIN scenario_rule r ON r.id = a.rule_id
           WHERE a.applied_at BETWEEN ? AND ? AND a.suppressed IS NULL ORDER BY a.applied_at, a.id""",
        (start, end),
    ):  # every application, each of a repeating staff response included, with the variant that took effect
        effects = json.loads(r["variants"])[r["variant"]]["effects"] if r["variant"] is not None else json.loads(r["effects"])
        out.append({"type": "event", "at": r["at"], "kind": "staff response" if r["repeat"] else "scenario", "name": r["name"], "patient": r["patient_id"],
                    "what": _effects(effects)})  # fmt: skip
    seen = {(e["at"], w) for e in out for w in e["what"]}
    for n in _q(world, "SELECT written_at AS at, patient_id, title, author FROM note WHERE written_at > ? AND written_at <= ? AND author IS NOT ? ORDER BY written_at, id", (start, end, actor)):
        what = f"note filed: {n['title']} ({n['author']})"
        if (n["at"], what) not in seen:
            out.append({"type": "event", "at": n["at"], "kind": "record", "name": None, "patient": n["patient_id"], "what": [what]})
    for a in _q(world, "SELECT at, patient_id, recorded_by, status, medication_id FROM administration WHERE at > ? AND at <= ? AND recorded_by IS NOT ? ORDER BY at, id",
                     (start, end, actor)):  # fmt: skip
        drug = world.one("SELECT drug, dose FROM medication WHERE id = ?", (a["medication_id"],)) or {}
        out.append({"type": "event", "at": a["at"], "kind": "record", "name": None, "patient": a["patient_id"],
                    "what": [f"{drug.get('drug')} {drug.get('dose')} {a['status']} by {a['recorded_by']}"]})  # fmt: skip
    for r in _q(
        world,
        """SELECT r.reported_at AS at, r.patient_id, o.orderable_code, i.name FROM result r JOIN "order" o ON o.id = r.order_id
           JOIN orderable i ON i.code = o.orderable_code WHERE o.ordered_at >= ? AND r.reported_at > ? AND r.reported_at <= ? ORDER BY r.reported_at, r.id""",
        (start, start, end),
    ):
        out.append({"type": "event", "at": r["at"], "kind": "result", "name": None, "patient": r["patient_id"], "what": [f"result reported: {r['name']}"]})
    out.sort(key=lambda e: e["at"])
    return out


def banners(world: World | None, ids) -> dict:
    out = {}
    for pid in sorted(i for i in ids if i is not None):
        p = world.one("SELECT * FROM patient WHERE id = ?", (pid,)) if world is not None else None
        if p:
            out[str(pid)] = {"name": f"{p['family_name'].upper()}, {p['given_name']}", "mrn": p["mrn"], "nhs": format_nhs_number(p["nhs_number"]),
                             "dob": p["dob"], "sex": p["sex"]}  # fmt: skip
    return out


def task_patients(world: World | None, task: dict) -> list[int]:
    """The patients a task concerns: those whose hospital numbers its checks or jobs name."""
    if world is None:
        return []
    mrns = set(re.findall(r"'(H\d{7})'", json.dumps(task.get("checks", [])))) | {j["mrn"] for j in task.get("jobs", [])}
    return sorted(r["id"] for r in world.q(f"SELECT id FROM patient WHERE mrn IN ({', '.join('?' * len(mrns))})", sorted(mrns))) if mrns else []


# --- one episode -----------------------------------------------------------------------------------------------


def _shots(value) -> list[str]:
    """Screenshot files referred to anywhere in a recorded value."""
    if isinstance(value, dict):
        return ([value["screenshot"]] if isinstance(value.get("screenshot"), str) else []) + [s for v in value.values() for s in _shots(v)]
    if isinstance(value, list):
        return [s for v in value for s in _shots(v)]
    return []


def _ops(audit: dict, by_action: dict, e: dict, http: list, prev_seq: int, prev_t: float) -> list[dict]:
    """The audit rows (operations) one recorded action caused."""
    result = e.get("result")
    if isinstance(result, dict) and isinstance(result.get("audit_ids"), list):  # a person's request
        ids = result["audit_ids"]
    elif e.get("audit_id") in audit:
        row = audit[e["audit_id"]]
        ids = [r["id"] for r in by_action.get(row.get("action_id"), [])] if row.get("action_id") is not None else [row["id"]]
    else:  # a browser action: the requests it made, between the previous action and this one
        ids = [i for h in http if (prev_seq <= h["after_seq"] < e["seq"] if "after_seq" in h else prev_t < h.get("t", 0) <= e.get("t", 0)) for i in h["audit_ids"]]
    return [audit[i] for i in ids if i in audit]


def _action(n, name, args, result, error, ops: list[dict], clock: int, mine: set) -> dict:
    """One action as the timeline shows it: what was asked, the operations it ran, whose record, and its marks."""
    wrote = [r for r in ops if r["ok"] and r["writes"] and r["op"] != "wait"]
    patients = sorted({r["patient_id"] for r in ops if r.get("patient_id") is not None})
    wrong = any(r.get("error") == "wrong_patient" for r in ops) or bool(mine and any(r["patient_id"] not in mine for r in wrote if r.get("patient_id") is not None))
    return {
        "type": "action", "n": n, "name": name, "args": _clip(args), "result": _clip(result), "error": error, "at": iso(clock), "patients": patients,
        "writes": bool(wrote), "wrong_patient": wrong, "other_patient": bool(mine and any(p not in mine for p in patients)),
        "failed": any(not r["ok"] for r in ops) or bool(error), "screenshots": [],
        "ops": [{"id": r["id"], "at": iso(r["at"]), "op": r["op"], "args": r.get("args"), "ok": bool(r["ok"]), "error": r.get("error"),
                 "writes": bool(r["writes"]), "patient": r.get("patient_id"), "result": _clip(r.get("result"))} for r in ops],
    }  # fmt: skip


def load(d: Path, regrade: bool = False) -> dict:
    """One episode as the page shows it, with everything (load is not where hiding happens; see prepare).

    With `regrade` the outcome is the saved world graded again by the current grader and the current packaged task's
    checks (eprbench regrade), whose descriptions the page then shows; the tier recorded when the episode ran is kept
    beside it and stated in the timeline's last note when it differs."""
    meta, result, report, task = (_json(d / n, {}) for n in ("episode.json", "result.json", "report.json", "task.json"))
    regraded = None
    if regrade:
        from eprbench.episode import ASSESSMENT_FIELDS
        from eprbench.episode import regrade as regrade_episode
        from eprbench.tasks import load_task

        try:
            again = regrade_episode(d)
            task = task | json.loads(load_task(task["id"]).model_dump_json(include=set(ASSESSMENT_FIELDS)))
        except FileNotFoundError:  # a task that is not packaged (any more) is graded on its saved definition
            again = regrade_episode(d, saved_task=True)
        regraded = {"tier_when_run": again["old_tier"], "tier": again["new_tier"]}
        report = again["report"]
    events = _lines(d / "transcript.jsonl")
    audit_rows = _lines(d / "audit.jsonl")
    http = [h for h in _lines(d / "http.jsonl") if h.get("audit_ids")]
    world = World.restore((d / "final.sqlite").read_bytes()) if (d / "final.sqlite").is_file() else None
    start = parse_iso(task["start_time"]) if task.get("start_time") else 0
    actor = task.get("actor")
    agent_rows = [r for r in audit_rows if r.get("actor") == actor and r.get("interface") in ("tool", "api", "ui")]
    audit = {r["id"]: r for r in agent_rows}
    by_action = {}
    for r in agent_rows:
        by_action.setdefault(r.get("action_id"), []).append(r)
    mine = set(task_patients(world, task))

    # prev_seq starts at 1: a request recorded before any transcript event (a browser's first page) precedes every action
    timeline, prev_seq, prev_t, clock, used = [], 1, 0.0, start, set()
    pending = world_events(world, start, actor) if world is not None else []

    def flush(upto: int):
        while pending and pending[0]["at"] <= upto:
            ev = pending.pop(0)
            timeline.append({**ev, "at": iso(ev["at"])})

    flush(start)
    for e in events:
        kind = e.get("kind")
        if kind == "response":
            texts, thoughts = model_says(e.get("body"))
            if texts or thoughts:
                timeline.append({"type": "model", "text": texts, "reasoning": thoughts, "seq": e["seq"]})
        elif kind == "request" and e.get("final_chance"):
            timeline.append({"type": "note", "text": "A limit was reached: the model was asked once more for its answer, with nothing further carried out."})
        elif kind == "action":
            ops = _ops(audit, by_action, e, http, prev_seq, prev_t)
            prev_seq, prev_t = e["seq"], e.get("t", prev_t)
            waited = bool(ops) and all(r["op"] == "wait" for r in ops)  # a wait is shown from when it began
            clock = clock if waited else min((r["at"] for r in ops), default=clock)
            timeline.append(_action(e.get("action"), e.get("name"), e.get("args"), None if ops else e.get("result"), e.get("error"), ops, clock, mine)
                            | {"screenshots": _shots(e.get("result"))} | ({"until": iso(max(r["at"] for r in ops))} if waited else {})
                            | ({"output_mark": e["output_mark"]} if "output_mark" in e else {}) | ({"final_chance": True} if e.get("final_chance") else {}))  # fmt: skip
            clock = max([clock] + [r["at"] for r in ops])
            used.update(r["id"] for r in ops)
            flush(clock)
        elif kind == "observation":
            timeline.append({"type": "screen", "screenshots": _shots(e)})
        elif kind == "submission":
            timeline.append({"type": "submission", "accepted": e.get("accepted"), "answer": e.get("answer"), "problems": e.get("problems") or [],
                             "interface": e.get("interface"), "at": e.get("at")})  # fmt: skip
        elif kind in ("error", "attempt_failed"):
            timeline.append({"type": "note", "text": f"{'Harness error' if kind == 'error' else 'A model request failed'}: {e.get('error')}"})
    if world is not None:
        flush(world.now)
    # Operations no recorded action accounts for (a browser's first page, loaded before the model acted): shown where
    # they fall among the actions, by audit order.
    for action_id, rows in by_action.items():
        rows = [r for r in rows if r["id"] not in used]
        if rows:
            first = min(r["id"] for r in rows)
            before = not used or first < min(used)
            at = next((k for k, item in enumerate(timeline) if item["type"] == "action" and (before or (item["ops"] and item["ops"][0]["id"] > first))), len(timeline))
            name = "page loaded before the first action" if before else "operations of no recorded action"
            timeline.insert(at, _action(None, name, None, None, None, rows, rows[0]["at"], mine))
    if (d / "agent.stdout").is_file():  # an agent CLI: its own messages and commands, with the API calls they made
        timeline = _interleave(agent_log((d / "agent.stdout").read_text(errors="replace")), timeline)
    if regraded is not None:
        changed = regraded["tier_when_run"] != regraded["tier"]
        timeline.append({"type": "note", "text": "Graded again for this page with the current marking rules"
                         + (f": the tier was {regraded['tier_when_run']} when the episode ran, and is {regraded['tier']} now." if changed
                            else f" (the tier, {regraded['tier']}, is the same as when the episode ran).")})  # fmt: skip
    timeline.append({"type": "end", "terminal_reason": result.get("terminal_reason"), "detail": result.get("detail"), "answer": result.get("answer"),
                     "closing_message": result.get("closing_message"), "final_chance": result.get("final_chance")})  # fmt: skip

    ids = {p for item in timeline for p in item.get("patients", [])} | {item.get("patient") for item in timeline} | mine
    checks = {c["id"]: c for c in task.get("checks", [])}
    outcome = report.get("outcome") or {}
    return {
        "id": episode_id(d.name), "dir": str(d), "task": {
            "id": task.get("id"), "title": task.get("title"), "category": task.get("category"), "instruction": task.get("instruction"),
            "actor": actor, "start_time": task.get("start_time"), "horizon_seconds": task.get("horizon_seconds"), "track": task.get("track"),
            "answer_form": [_public_field(f) for f in task["answer_schema"]] if task.get("answer_schema") else None,
            "pathways": task.get("pathways"),
            "checks": [{"id": c["id"], "description": c.get("description", ""), "role": c.get("role") or _legacy_role(c), "event": c.get("event"),
                        "predicate": {k: c[k] for k in ("sql", "answer_regex", "answer_field", "expect", "at", "by", "supersedes", "state_only") if c.get(k) not in (None, [], False)}}
                       for c in checks.values()],
        },  # fmt: skip
        "who": {k: meta.get(k) if meta.get(k) is not None else result.get(k) for k in WHO} | {"interface": meta.get("interface") or result.get("interface")},
        "observed_model": ((meta.get("agent_config") or {}).get("model") or {}).get("observed") or (result.get("agent_inventory") or {}).get("model"),
        "result": {k: result.get(k) for k in ("terminal_reason", "truncated", "detail", "answer", "final_chance", "requests", "actions", "screenshot_actions", "accounting",
                                               "usage", "wall_seconds", "started_at", "contaminated", "suspected", "sandboxed", "closing_message", "actions_summary", "repeat")}
                  | {"error": ((result.get("error") or "").strip().splitlines() or [None])[-1]},  # fmt: skip
        "outcome": outcome, "score": report.get("score"), "success": report.get("success"), "unsafe": report.get("unsafe"), "regraded": regraded,
        "checks": [{"id": c["id"], "passed": c["passed"], "role": c.get("role")} for c in report.get("checks", [])],
        "patients": banners(world, ids), "task_patients": sorted(mine), "timeline": timeline,
    }  # fmt: skip


def _legacy_role(c: dict) -> str:
    return "harm" if c.get("critical") else "required" if c.get("expect", "some") == "some" else "scope"


def _public_field(f: dict) -> dict:
    return {"name": f["name"], "kind": f["kind"], "prompt": f.get("prompt", "")} | ({"options": f["options"]} if f.get("options") else {}) | (
        {"units": list(f["units"])} if f.get("units") else {})


SERVER_ORDER = "API calls (order from server log)"


def _interleave(log: list[dict], timeline: list[dict]) -> list[dict]:
    """An agent CLI's own record with the API calls it made. Each call recorded how much of the agent's output had
    been read when it arrived (output_mark), so it goes after the last thing the agent had printed by then. The
    pairing with that command is asserted only when the command names the operation; otherwise the calls are shown,
    in the order the server received them, in a group saying so. The record's other events follow the calls they
    came after. Evidence from before output marks were recorded puts every call in such groups."""
    placed, at, sure = [], -1, False
    for item in timeline:
        if item["type"] == "action":
            mark = item.get("output_mark")
            if mark is None:
                sure = False
            else:
                at = max((i for i, x in enumerate(log) if x["line"] <= mark), default=-1)
                command = next((x for x in reversed(log[: at + 1]) if x["type"] == "command"), None)
                sure = command is not None and f"/api/ops/{item['name']}" in (command["command"] or "")
        placed.append((at, sure))
    out = []

    def emit(position):
        group = None
        for (p, certain), item in zip(placed, timeline):
            if p != position or item["type"] == "end":
                continue
            if item["type"] == "action" and not certain:
                if group is None:
                    group = {"type": "group", "label": SERVER_ORDER, "items": []}
                    out.append(group)
                group["items"].append(item)
            elif group is not None and item["type"] != "action":
                group["items"].append(item)
            else:
                group = None
                out.append(item)

    emit(-1)
    for i, x in enumerate(log):
        out.append(x)
        emit(i)
    return out + [t for t in timeline if t["type"] == "end"]


# --- hiding, coding and writing ------------------------------------------------------------------------------------


PATHS = re.compile(r"(?:/Users|/home|/private|/var/folders|[A-Za-z]:\\Users)[/\\][^\s'\"<>]*")
PEOPLE = "screenshots of a person's session are never taken, so none is published"


def _scrub(value, names: list[tuple[str, str]], vendors: bool):
    """`value` with each name replaced by its code, local paths and credential-like strings removed and, with
    `vendors`, vendor and model names blanked."""
    if isinstance(value, str):
        for old, new in names:
            value = value.replace(old, new)
        value = redact_output(PATHS.sub("[path]", value))
        return VENDOR.sub("[model]", value) if vendors else value
    if isinstance(value, dict):
        return {k: _scrub(v, names, vendors) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, names, vendors) for v in value]
    return value


def identity(ep: dict) -> str:
    return json.dumps([ep["who"].get(k) for k in ("harness", "provider", "model", "agent")])


def person(ep: dict) -> str | None:
    """The real person whose session this was, if it was one: their name is the task's actor."""
    return ep["task"]["actor"] if ep["who"].get("harness") == "human" and ep["task"].get("actor") else None


def names(ep: dict, key: dict | None, blind: bool) -> list[tuple[str, str]]:
    """What a public or blind page replaces in this episode, longest first: a person's name by their code and, when
    blind, the harness, provider, model and agent by the agent's code."""
    out = {}
    if (who := person(ep)) and key is not None:
        out[who] = key["people"][who]
    if blind:
        code = key["codes"][identity(ep)]
        out |= {str(v): code for v in [*json.loads(identity(ep)), ep.get("observed_model")] if v and len(str(v)) >= 3 and str(v) not in out}
    return sorted(out.items(), key=lambda kv: -len(kv[0]))


def prepare(ep: dict, *, hide: bool, blind: bool, key: dict | None) -> dict:
    """The episode as this page may show it."""
    ep = json.loads(json.dumps(ep, default=str))
    if not hide:
        return ep
    for c in ep["task"]["checks"]:
        c.pop("predicate", None)
    ep.pop("dir", None)
    replace = names(ep, key, blind)
    if blind:
        ep["who"] = {"code": key["codes"][identity(ep)], "interface": ep["who"]["interface"]}
        ep.pop("observed_model", None)
    return _scrub(ep, replace, blind)


def _code(salt: str, ident: str, taken: set, prefix: str = "Agent") -> str:
    digest = hmac.new(salt.encode(), ident.encode(), hashlib.sha256).hexdigest().upper()
    for n in range(4, len(digest)):
        code = f"{prefix} {digest[:n]}"
        if code not in taken:
            return code
    raise RuntimeError("no free code")


PNG_KEPT = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}  # the image itself; text, time, colour profiles and the rest are dropped


def clean_png(data: bytes) -> bytes:
    """A PNG with only the chunks that make up the image, so no metadata (text chunks, times, profiles) goes with it."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    out, i = [data[:8]], 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        kind, end = data[i + 4 : i + 8], i + 12 + length
        if kind in PNG_KEPT:
            out.append(data[i:end])
        i = end
        if kind == b"IEND":
            break
    return b"".join(out)


def _assets(ep: dict, source: Path, out: Path, mode: str, clean: bool) -> None:
    """Point each screenshot at where the page can load it: relative to the page, copied beside it (as 001.png,
    002.png, ... in the order first shown), or embedded; `clean` strips every chunk but the image's own."""
    names: dict[str, str] = {}

    def place(path: str) -> str:
        src = source / path
        if mode == "link":
            return Path(os.path.relpath(src, out.parent)).as_posix()
        if not src.is_file():
            return ""
        data = clean_png(src.read_bytes()) if clean else src.read_bytes()
        if mode == "embed":
            return "data:image/png;base64," + base64.b64encode(data).decode()
        if path not in names:
            names[path] = f"{len(names) + 1:03d}.png"
        target = out.parent / f"{out.stem}_files" / ep["id"] / names[path]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.relative_to(out.parent).as_posix()

    for item in ep["timeline"]:
        for inner in item.get("items", []) + [item]:
            if inner.get("screenshots"):
                inner["screenshots"] = [x for x in (place(p) for p in inner["screenshots"]) if x]


def row(ep: dict) -> dict:
    """The episode's line in the index (also what the agreement table reads)."""
    o, r = ep.get("outcome") or {}, ep["result"]
    who = ep["who"]
    return {"id": ep["id"], "task_id": ep["task"]["id"], "title": ep["task"]["title"], "interface": who.get("interface"),
            "who": who.get("code") or (f"{who['agent']} ({who.get('model') or ep.get('observed_model')})" if who.get("agent") and (who.get("model") or ep.get("observed_model")) else None)
            or who.get("agent") or "/".join(filter(None, (who.get("provider"), who.get("model")))) or who.get("harness"),
            "harness": who.get("code") or who.get("harness"), "tier": o.get("tier"), "terminal_reason": r.get("terminal_reason"),
            "truncated": bool(r.get("truncated")), "contaminated": bool(r.get("contaminated")), "suspected": bool(r.get("suspected")),
            "unsandboxed": r.get("sandboxed") is False, "score": ep.get("score")}  # fmt: skip


REVIEW_FIELDS = ("task_id", "automatic_tier", "verdict", "tier", "comment", "checks", "updated_at")


def page_reviews(raw: dict, shown: list[dict], replace: dict, *, hide: bool, blind: bool, comments: bool) -> dict:
    """Reviews to load into the page: only for episodes on it, only the fields a review has, with the page's own
    agent label, and on a public or blind page sanitised as the episodes are (names coded, paths and credentials
    removed, vendors blanked when blind). A public page carries no reviewer's comment unless `comments`. Episode ids
    are hashes of the evidence directories' names, the same on every kind of page, so they need no mapping."""
    rows = {r["id"]: r for r in map(row, shown)}
    out = {}
    for key, rv in (raw or {}).items():
        if key not in rows or not isinstance(rv, dict):
            continue
        clean = {k: rv.get(k) for k in REVIEW_FIELDS}
        clean["checks"] = {str(c): "disagree" for c, v in (rv.get("checks") or {}).items() if v == "disagree"} if isinstance(rv.get("checks"), dict) else {}
        clean["comment"] = str(clean["comment"] or "")
        if hide and not blind and not comments:
            clean["comment"] = ""
        if hide:
            clean = _scrub(clean, replace.get(key, []), blind)
        out[key] = clean | {"who": rows[key]["who"], "interface": rows[key]["interface"]}
    return out


def build(dirs, out, *, blind: bool = False, public: bool = False, embed: bool = False, reviews: str | None = None, key: str | None = None,
          include_review_comments: bool = False, regrade: bool = False) -> dict:  # fmt: skip
    """Write the page for every episode under `dirs` to `out`. Returns {"out", "episodes", "rows", "key"}.

    A public or blind page codes real people (a person's session) and, when blind, agents; the codes and what they
    stand for go to the key file (default: beside the page, NAME.key.json), reused on the next build. With `regrade`
    each outcome is the saved world graded again with the current marking (see load)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    hide = blind or public
    found = find(dirs)
    episodes = [load(d, regrade) for d in found]
    keyfile, keydata = None, None
    if hide:
        keyfile = Path(key) if key else out.with_name(out.stem + ".key.json")
        keydata = {"salt": secrets.token_hex(16), "codes": {}, "people": {}, "episodes": {}} | (_json(keyfile, None) or {})
        keydata.setdefault("people", {})
        for ep in episodes:
            if (who := person(ep)) and who not in keydata["people"]:
                keydata["people"][who] = _code(keydata["salt"], "person:" + who, set(keydata["people"].values()), "Clinician")
            if blind and identity(ep) not in keydata["codes"]:
                keydata["codes"][identity(ep)] = _code(keydata["salt"], identity(ep), set(keydata["codes"].values()))
            keydata["episodes"][ep["id"]] = {"dir": ep["dir"], "who": ep["who"]}
        keyfile.write_text(json.dumps(keydata, indent=2) + "\n")
    shown, replace = [], {}
    for d, ep in zip(found, episodes):
        replace[ep["id"]] = names(ep, keydata, blind) if hide else []
        ep = prepare(ep, hide=hide, blind=blind, key=keydata)
        _assets(ep, d, out, "embed" if embed else "copy" if hide else "link", clean=hide)
        shown.append(ep)
    notes = [PEOPLE] if any(person(e) for e in episodes) else []
    review_data = page_reviews(load_reviews(reviews), shown, replace, hide=hide, blind=blind, comments=include_review_comments) if reviews else {}
    keep_comments = not hide or blind or include_review_comments  # as page_reviews keeps them
    mode = "blind" if blind else "public" if public else "private"
    build_id = hashlib.sha256(json.dumps([mode, keep_comments, include_review_comments, sorted(e["id"] for e in shown)]).encode()).hexdigest()[:16]
    data = {"episodes": shown, "rows": [row(e) for e in shown], "reviews": review_data, "blind": blind, "public": public, "notes": notes,
            "build_id": build_id, "keep_comments": keep_comments, "review_comments": not hide or include_review_comments}  # fmt: skip
    page = resources.files("eprbench.viewer").joinpath("page.html").read_text()
    # Inside a script element only "<" can end the element or open a comment; U+2028 and U+2029 are escaped as well
    payload = json.dumps(data, default=str, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out.write_text(page.replace("/*__DATA__*/{}", payload))
    return {"out": str(out), "episodes": len(shown), "rows": data["rows"], "key": str(keyfile) if keyfile else None}
