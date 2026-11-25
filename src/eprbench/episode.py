"""One episode's lifecycle, limits and evidence record, shared by every harness.

Every harness counts model requests, actions (environment operations, or mouse and keyboard actions in the
browser), tokens and wall time through an `Episode`. It enforces separate limits on requests and on actions,
plus a wall-clock timeout, and ends with exactly one terminal reason from `TERMINAL_REASONS`. A run that reaches
a limit is truncated: it is reported apart from completed runs and never counted as a clinical failure.

Given an output directory, the episode writes its evidence as it happens, so a crash leaves everything up to that
point:

    episode.json      task, repeat, harness, provider, model, settings, limits and immutable identifiers
    task.json         the task exactly as run
    transcript.jsonl  every request (as sent) and response (as received), action and result, in order; every attempt
                      to submit an answer to a task with an answer form (submission events, accepted or not)
    screenshots/      every screenshot as a PNG, named in the transcript with its SHA-256
    http.jsonl, http/ every HTTP exchange with the EPR server (API and UI runs), with response bodies; each entry's
                      after_seq is the last transcript event before it, which places it in the transcript, and for
                      an agent CLI output_mark is how many lines of the agent's output had been read when the
                      request arrived, which places it in the agent's own record
    agent.stdout/err  an agent CLI's own output (CLI runs)
    final.sqlite      the world as the agent left it (World.snapshot), so the episode can be regraded offline
    audit.jsonl       the full audit log, each operation with its result where the surface returns one
    reads.jsonl       what each agent action showed the agent of the record (read events: agent_read in assessment.py)
    manifest.json     written last, atomically: every other file's SHA-256 and the episode's ledger range; without a
                      valid one the directory is not complete evidence (a plan re-runs it)
    report.json       the grading report
    result.json       terminal reason, final answer, usage, counts, actions_summary, timestamps and any error with traceback

The final answer is recorded as given: text, or for a task with an answer form the submitted object
{"text": ..., "fields": {...}} (eprbench.answers), and regrading grades that same answer. actions_summary counts distinct agent
actions (audit action ids) by the task's actor, per interface, split into those that changed the record, those
that only read it, and the rest (failed writes and waits).

Wall-clock times live only here, never in the world database. Credentials are redacted by value (see
REDACTION_POLICY); episode.json says what was redacted in which file. episode.json, result.json and report.json carry
the benchmark canary, since the record holds ground truth.

Before the final snapshot, rules that have fallen due are fired (as grading would fire them), so final.sqlite is
byte for byte the world the checks ran on; its SHA-256 is recorded as graded_world_sha256. A task with a horizon is
also assessed on a copy of that world advanced to its horizon (tasks.grade); the copy is not saved, being rebuilt
exactly from final.sqlite, and its SHA-256 is recorded as horizon_world_sha256.
"""

import ast
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time
import traceback
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata, resources
from pathlib import Path

from eprbench import scenario
from eprbench.answers import SUBMISSION_RULE
from eprbench.loader import bundle_digests, file_sha256
from eprbench.tasks import CANARY, Task, conclude, grade, load_task, task_file
from eprbench.world import SCHEMA_VERSION, World, parse_iso, saved_schema

TERMINAL_REASONS = {
    "submitted": "the model gave its final answer in the harness's normal way",
    "stopped_without_submit": "the model ended its turn without calling submit; its closing text is the answer",
    "refusal": "the model refused",
    "model_request_limit": "the limit on model requests was reached",
    "action_limit": "the limit on actions was reached",
    "timeout": "the wall-clock limit was reached",
    "context_limit": "the model's context window was full",
    "spend_limit": "the next request's worst-case cost would have crossed a spend ceiling (eprbench.spend)",
    "harness_error": "the harness, the agent program or the model API failed",
    "agent_usage_limit": "the agent CLI's subscription reached a usage or rate limit: an infrastructure failure, never a clinical outcome; run it again",
}
ERRORS = frozenset({"harness_error", "agent_usage_limit"})  # infrastructure failures: reported apart, never a clinical outcome, always run again
TRUNCATED = {"model_request_limit", "action_limit", "timeout", "context_limit", "spend_limit"}  # reported apart: neither a clinical outcome nor a fault

_WHY = {
    "model_request_limit": "You have reached the limit on model turns for this task.",
    "action_limit": "You have reached the limit on actions for this task.",
    "timeout": "You have reached the time limit for this task.",
    "context_limit": "The conversation is too long to continue.",
    "spend_limit": "You have reached the spending limit for this task.",
}

# Signs, in a provider's error or status text, that the model's context window is full.
_CONTEXT_FULL = re.compile(r"context_length_exceeded|context[ _-]window|prompt is too long|maximum context length|input token count exceeds|too many (?:input )?tokens", re.I)


def context_full(text: str) -> bool:
    return bool(_CONTEXT_FULL.search(text or ""))


def _failure(e: Exception) -> str:
    """How a failed request failed: "context" (the context window is full), "timeout", "transient" (worth retrying)
    or "fatal"."""
    if context_full(str(e)):
        return "context"
    name = type(e).__name__
    if "Timeout" in name or isinstance(e, TimeoutError):
        return "timeout"
    status = getattr(e, "status_code", None)
    if "Connection" in name or status in (408, 409, 429) or (isinstance(status, int) and status >= 500):
        return "transient"
    return "fatal"


REDACTION_POLICY = (
    "values: the values of credential-named environment variables, and of credential arguments and headers seen in this run, are replaced "
    "wherever they appear, except in final.sqlite (kept byte-exact for regrading); patterns: common key formats and Basic/Bearer "
    "credentials are also redacted in agent.stdout and agent.stderr only. Hashes recorded for saved files are of the bytes as written."
)

MAX_ATTEMPTS = 3  # attempts per request, each counted as a request, all within the episode's deadline
FINAL_CHANCE = "{why} No further actions will be carried out. Reply now, in plain text, with your final answer to your colleague: the answer, or a brief account of what you did."
FINAL_CHANCE_FORM = {
    "tool": "{why} No further actions will be carried out, but you can still submit your answer: call submit now with your reply and the answer form.",
    "api": "{why} No further operations will be carried out, but you can still submit your answer: POST it to /api/ops/submit now, with your reply and the answer form.",
    "ui": "{why} No further actions will be carried out elsewhere, but you can still submit your answer: you are on the Handover / reply page; fill in the form and your reply, submit it and confirm.",
}


# Facts about the invocation an episode belongs to, set by whoever runs it (eprbench.runner: its place in a plan, with
# the plan's SHA-256), recorded in episode.json and result.json.
RUN_CONTEXT: ContextVar[dict] = ContextVar("eprbench_run_context", default={})


class ModelDrift(Exception):
    """A response named a different model version from the one the episode (or its plan) was answered by."""


class LimitReached(Exception):
    """Raised by `Episode.send` when no further request may be made; ends the episode with the limit as its reason."""


@dataclass(frozen=True)
class Limits:
    """Generous ceilings by default: they bound cost while budgets are measured, and are not part of any result."""

    max_requests: int = 200
    max_actions: int = 500  # operations, or mouse and keyboard actions in the browser: never a screenshot or zoom requested
    max_screenshots: int = 500  # screenshots and zooms a browser model asks for: not actions, capped apart so neither harness gains
    timeout_seconds: float = 3600.0
    final_chance: bool = True  # on reaching a limit, ask once more for the answer, with no further actions carried out
    final_chance_seconds: float = 120.0  # how long that last request may take; an answer arriving later is not accepted


# What a browser model may ask for that only looks at the screen. Not charged against max_actions (the OpenAI harness's
# model is shown the screen after every call anyway), but counted and capped apart (max_screenshots).
SCREENSHOT_ACTIONS = {"screenshot", "zoom", "cursor_position"}
ACCOUNTING = {
    "actions": "charged against max_actions: operations (tool, API) or mouse and keyboard actions (browser)",
    "screenshot_requests": "screenshots, zooms and cursor-position queries the model asked for: not actions, capped by max_screenshots",
    "automatic_screenshots": "screenshots the harness sent without being asked (after every computer call, or with the final chance)",
    "model_requests": "model requests, every attempt and the final chance included (None where an agent program's are not visible)",
}


def count_units(harness: str, interface: str) -> dict:
    """What each count in an episode's record counts, in that episode's own terms. Two of them are easily confused: the
    agent actions the limit is charged in (`accounting.actions`, which is also `actions`/`steps`) and the EPR requests
    the record served (`actions_summary.<interface>.actions`). In the browser a click that loads no page is an agent
    action but no EPR request, so the second is usually smaller; through tools and the API the two are the same calls."""
    charged = {"tool": "tool calls of the record's operations", "api": "API operation calls"}.get(interface, "mouse and keyboard actions")
    if harness == "human":
        charged = "HTTP requests (page loads and form posts)"
    return {
        "accounting.actions": f"agent actions charged against max_actions: here, {charged}; checked before each is carried out",
        "accounting.screenshot_requests": "screenshots, zooms and cursor-position queries the agent asked for: never actions, capped by max_screenshots",
        "actions_summary.<interface>.actions": "EPR requests that ran the record's operations (distinct audit action ids by the task's actor): a tool "
                                               "or API call, or a page load or form post in the UI; a measure of effort, not charged against any limit",
        "actions_summary.<interface>.operations": "the record's operations those requests ran (a UI page runs several)",
    }  # fmt: skip

# --- credentials ------------------------------------------------------------------
#
# Evidence is redacted by VALUE only: the values of credential-named environment variables, and of credential
# arguments and headers seen in this run, are replaced wherever they appear. Model and EPR content is never redacted
# by pattern, so the record replays exactly; only an agent CLI's own stdout and stderr, which can echo credentials the
# harness never saw, are also redacted by pattern (`redact_output`).

_SECRET_NAME = re.compile(r"api[_-]?key|_key\b|key$|token(?!s)|secret|passw|auth(?!or)|cookie|credential", re.I)
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key", "x-harness-token", "x-goog-api-key"}
_NOT_SECRET = {"SSH_AUTH_SOCK"}
_OUTPUT_PATTERNS = re.compile(
    r"(?i)(?P<pre>(?:bearer|basic)\s+|(?:authorization|x-api-key|api[_-]key|cookie)[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic)\s+)?)"
    r"(?P<val>[A-Za-z0-9._~+/=\-]{8,})"
    r"|(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{30,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,})"
)


def environment_secrets() -> list[tuple[str, str]]:
    """(source, value) for every environment variable named like a credential."""
    return [(f"env:{k}", v) for k, v in os.environ.items() if _SECRET_NAME.search(k) and k not in _NOT_SECRET and len(v) >= 8]


def argv_secrets(argv: list[str]) -> list[tuple[str, str]]:
    """(source, value) for the values of credential-looking options in a command line (--api-key X, --token=X)."""
    found, flag = [], None
    for arg in argv:
        if flag is not None:
            found.append((f"argv:{flag}", arg))
            flag = None
            continue
        name, eq, value = arg.partition("=")
        if arg.startswith("-") and _SECRET_NAME.search(name):
            if eq:
                found.append((f"argv:{name}", value))
            else:
                flag = name
    return [(k, v) for k, v in found if len(v) >= 4]


def redact_output(text: str) -> str:
    """Pattern-based redaction, for an agent CLI's stdout and stderr only."""
    return _OUTPUT_PATTERNS.sub(lambda m: (m["pre"] or "") + "[REDACTED]", text)


def _is_image(d: dict) -> bool:
    kind = str(d.get("mimeType") or d.get("media_type") or "")
    return d.get("type") in ("image", "base64", "input_image") or kind.startswith("image/")


def _redact_fields(obj):
    """Pattern redaction of a parsed event's text fields; an image's base64 payload (MCP's {"type": "image", "data"},
    Anthropic's {"source": {"type": "base64", "data"}}, a data: URL) is left as it is."""
    if isinstance(obj, dict):
        return {k: v if k in ("data", "image_url", "url") and isinstance(v, str) and (_is_image(obj) or v.startswith("data:image/")) else _redact_fields(v)
                for k, v in obj.items()}  # fmt: skip
    if isinstance(obj, list):
        return [_redact_fields(v) for v in obj]
    return redact_output(obj) if isinstance(obj, str) else obj


def redact_stream_line(line: str) -> str:
    """One line of an agent CLI's output, pattern-redacted: a JSON event field by field, so a credential-like run of
    characters inside a screenshot's base64 is never replaced (that would corrupt the image); any other line whole. A
    JSON line with nothing to redact is kept byte for byte."""
    body = line.rstrip("\n")
    if body.startswith("{"):
        try:
            event = json.loads(body)
        except ValueError:
            return redact_output(line)
        redacted = _redact_fields(event)
        return line if redacted == event else json.dumps(redacted, ensure_ascii=False) + line[len(body):]
    return redact_output(line)


def _sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def grader_files(pkg: Path) -> list[str]:
    """tasks.py and every package module it imports, directly or not, plus the schema: everything grading reads through."""
    seen, todo = set(), ["tasks"]
    while todo:
        name = todo.pop()
        if name in seen or not (pkg / f"{name}.py").is_file():
            continue
        seen.add(name)
        for node in ast.walk(ast.parse((pkg / f"{name}.py").read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "eprbench":
                todo += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("eprbench."):
                todo.append(node.module.split(".")[1])
            elif isinstance(node, ast.Import):
                todo += [a.name.split(".")[1] for a in node.names if a.name.startswith("eprbench.")]
    return sorted(f"{n}.py" for n in seen) + ["schema.sql"]


def provenance(task: Task) -> dict:
    """Immutable identifiers of the code, data and grader an episode ran against."""
    pkg = Path(str(resources.files("eprbench")))
    commit = _git(pkg, "rev-parse", "HEAD")
    status = _git(pkg, "status", "--porcelain")
    try:
        packaged = load_task(task.id)
    except (OSError, ValueError):
        packaged = None
    code = sorted(p for p in pkg.rglob("*") if p.is_file() and p.suffix in (".py", ".sql", ".html", ".css") and "__pycache__" not in p.parts)
    versions = {"python": platform.python_version()}
    for name in ("eprbench", "anthropic", "openai", "google-genai", "playwright", "fastapi", "pydantic", "uvicorn"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "git_commit": commit.strip() if commit else None,
        "git_dirty": bool(status.strip()) if status is not None else None,
        "task_sha256": _sha256(task.model_dump_json()),
        "task_file_sha256": file_sha256(task_file(task.id)) if packaged is not None else None,
        "task_matches_packaged_file": packaged == task,
        "data_sha256": bundle_digests(task.bundle),
        "grader_sha256": {name: file_sha256(pkg / name) for name in grader_files(pkg)},
        "code_sha256": _sha256("".join(f"{p.relative_to(pkg)}:{file_sha256(p)}\n" for p in code)),
        "versions": versions,
    }


def _wall(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


class Episode:
    """Counts, limits and records one episode. Use as a context manager around the harness loop, then `close`.

    An exception inside the `with` block ends the episode as a harness_error (or as the limit already reached), with
    its traceback recorded, and does not propagate (an interrupt does, after being recorded), so a batch carries on.

    Harness contract: charge each action with `try_charge` before carrying it out and record it with `action` whether
    it succeeded or failed; make every model request through `send`; accept nothing from a response that arrived
    after the time limit (`late`); end with `end`.
    """

    def __init__(self, task: Task, *, harness: str, interface: str, model: str | None = None, provider: str | None = None,
                 agent: str | None = None, effort: str | None = None, limits: Limits | None = None, settings: dict | None = None,
                 out_dir: str | Path | None = None, repeat: int = 0, secrets: list[tuple[str, str]] = (), spend=None):  # fmt: skip
        self.task, self.limits, self.repeat = task, limits or Limits(), repeat
        self.who = {"harness": harness, "interface": interface, "provider": provider, "model": model, "agent": agent, "effort": effort}
        self.spend = spend.meter(self.who, task.id, repeat) if spend is not None else None  # eprbench.spend: a ceiling in money
        self.run_context = dict(RUN_CONTEXT.get())
        self.reported_models: set[str] = set()  # the model versions the provider says answered
        self.unverified_responses = 0  # responses that named no model version
        self.spend_refused = False  # any request refused for spend: the episode ends at spend_limit, whatever else stopped it
        self.expected_model_version = self.run_context.get("expected_model_versions", {}).get(model)  # a plan's, from its earlier episodes
        self.on_delivered = None  # a browser harness's: called with each request that came back, returns the reads it delivered
        self.requests = self.actions = self.screenshot_actions = self.automatic_screenshots = 0
        self.requests_known = True  # False when an agent program makes requests the harness cannot see
        self.usage: dict[str, int] = {}
        self.stop: str | None = None  # the first limit reached; sticky
        self.late = False  # the last response arrived after its deadline, so nothing in it is accepted
        self.reason = self.detail = self.answer = self.error = None
        self.final_chance: dict | None = None
        self._final_until: float | None = None  # when the final chance, all its requests together, runs out
        self.results: dict[int, object] = {}  # audit id -> what the operation returned, where the surface returns it
        self.meta_updates: dict = {}  # facts known only at the end that belong in episode.json (e.g. the model an agent reported)
        self.output_mark: int | None = None  # an agent CLI's output lines read so far (see http)
        self.desk = None  # the episode's submissions, for a harness that takes them itself (eprbench.answers.Desk)
        final_after = sorted(TRUNCATED) if self.limits.final_chance else []
        # harness-specific fields for the summary; a harness that cannot enforce a limit must say so here
        self.extra: dict = {"limits_enforced": {"max_requests": True, "max_actions": True, "timeout": True, "final_chance_after": final_after}}
        self.extra["count_units"] = count_units(harness, interface)
        # what differs between surfaces and harnesses, so that tool and UI runs can be compared fairly
        self.extra["comparability"] = {
            "timer_starts": "at the first model request; set-up before it (environment, server, browser) is setup_seconds and not counted",
            "setup_seconds": None, "malformed_calls": "charged as actions", "submission": SUBMISSION_RULE,
        }  # fmt: skip
        # Wall time: `started` is when the episode was created; the limits run from the first model request (`_t0`).
        self.started, self._created, self._t0 = time.time(), time.monotonic(), None
        self._lock = threading.RLock()
        self._seq, self._http = 0, 0
        self._shots: dict[str, str] = {}
        self._sent: dict = {}
        self._secrets: list[tuple[str, str]] = []
        self.redactions: dict[str, dict[str, int]] = {}
        self.add_secrets(environment_secrets() + list(secrets))
        self.dir = self._transcript = None
        if out_dir is not None:
            self.dir = self._new_dir(Path(out_dir))
            (self.dir / "task.json").write_text(task.model_dump_json(indent=2))
            meta = {"task_id": task.id, "repeat": repeat, **self.who, "limits": asdict(self.limits), "settings": settings or {},
                    **({"spend_ceiling": spend.describe()} if spend is not None else {}), **self.run_context,
                    "started_at": _wall(self.started), "provenance": provenance(task), "redaction": {"policy": REDACTION_POLICY, "counts": {}}}  # fmt: skip
            self._write("episode.json", meta)
            self._transcript = open(self.dir / "transcript.jsonl", "a")

    def _new_dir(self, root: Path) -> Path:
        who = self.who["agent"] or "-".join(filter(None, (self.who["provider"], self.who["model"], self.who["interface"])))
        stem = f"{datetime.fromtimestamp(self.started, UTC):%Y%m%dT%H%M%S}-{self.task.id}-{who}-r{self.repeat}".replace("/", "_")
        for n in range(1000):
            path = root / (stem if n == 0 else f"{stem}-{n}")
            try:
                path.mkdir(parents=True)
                return path
            except FileExistsError:
                continue
        raise RuntimeError(f"cannot create an evidence directory under {root}")

    def add_secrets(self, found: list[tuple[str, str]]) -> None:
        """Values to redact wherever they appear in the evidence, with where each came from."""
        with self._lock:
            known = {v for _, v in self._secrets}
            self._secrets += [(src, v) for src, v in found if v and v not in known]
            self._secrets.sort(key=lambda sv: -len(sv[1]))

    def redact(self, obj, where: str = "summary"):
        """`obj` with every known secret value replaced, counting replacements by file and source."""
        if isinstance(obj, str):
            for src, value in self._secrets:
                if value in obj:
                    counts = self.redactions.setdefault(where, {})
                    counts[src] = counts.get(src, 0) + obj.count(value)
                    obj = obj.replace(value, f"[REDACTED:{src}]")
            return obj
        if isinstance(obj, dict):
            return {k: self.redact(v, where) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v, where) for v in obj]
        return obj

    def _write(self, name: str, data) -> None:
        if self.dir is not None:
            if isinstance(data, dict) and name in ("episode.json", "result.json", "report.json"):
                data = {**data, "canary": CANARY}
            (self.dir / name).write_text(json.dumps(self.redact(json.loads(json.dumps(data, default=str)), name), indent=2) + "\n")

    # --- accounting and limits ---------------------------------------------

    def start_clock(self) -> None:
        """Start the limits' clock (at the first model request, or when an agent program is launched)."""
        if self._t0 is None:
            self._t0 = time.monotonic()
            self.extra["comparability"]["setup_seconds"] = round(self._t0 - self._created, 3)

    def elapsed(self) -> float:
        """Seconds since the clock started (zero before it has)."""
        return 0.0 if self._t0 is None else time.monotonic() - self._t0

    def open_final_chance(self) -> None:
        """Start the final chance's window now (once; only when the episode allows one). A harness whose final chance
        begins without a model request (the agent-CLI browser route) calls this when the limit is reached; the SDK
        harnesses' first final-chance request opens it."""
        if self.limits.final_chance and self._final_until is None:
            self._final_until = time.monotonic() + self.limits.final_chance_seconds

    def acting_too_late(self) -> bool:
        """Whether an action under way must stop now: past the episode's deadline or, once the final chance has begun,
        past the final chance's own end (which may lie after the deadline: that is what it is for)."""
        if self._final_until is not None:
            return time.monotonic() >= self._final_until
        return self.elapsed() >= self.limits.timeout_seconds

    def _timed_out(self) -> bool:
        if self.stop is None and self.elapsed() >= self.limits.timeout_seconds:
            self.stop = "timeout"
        return self.stop is not None

    def allow_request(self) -> bool:
        """Whether another ordinary model request may be made. Once False, the next request can only be the final chance."""
        if not self._timed_out() and self.requests >= self.limits.max_requests:
            self.stop = "model_request_limit"
        return self.stop is None

    def try_charge(self, name: str | None = None) -> int:
        """Reserve one action against the limit, before it is carried out. Returns its number, or 0 if it is refused.

        A browser action that only looks at the screen (`name` in SCREENSHOT_ACTIONS) is a screenshot request instead:
        not an action, numbered and capped apart (max_screenshots). Reaching either cap is an action_limit."""
        look = name in SCREENSHOT_ACTIONS
        with self._lock:
            used, cap = (self.screenshot_actions, self.limits.max_screenshots) if look else (self.actions, self.limits.max_actions)
            if not self._timed_out() and used >= cap:
                self.stop = "action_limit"
                self.extra["limit_reached"] = "max_screenshots" if look else "max_actions"
            if self.stop is not None:
                return 0
            if look:
                self.screenshot_actions += 1
                return self.screenshot_actions
            self.actions += 1
            return self.actions

    def automatic_screenshot(self, n: int = 1) -> None:
        """Count screenshots the harness sends without the model asking for them."""
        with self._lock:
            self.automatic_screenshots += n

    def model_version(self) -> str | None:
        """The model version every response named; "unverified" when any named none; None before any response."""
        if self.unverified_responses:
            return "unverified"
        return sorted(self.reported_models)[0] if len(self.reported_models) == 1 else ("mixed" if self.reported_models else None)

    def accounting(self) -> dict:
        """The episode's interaction counts (ACCOUNTING), the same four for every harness."""
        return {"actions": self.actions, "screenshot_requests": self.screenshot_actions, "automatic_screenshots": self.automatic_screenshots,
                "model_requests": self.requests if self.requests_known else None}  # fmt: skip

    def note(self, form: bool = False) -> str:
        """What the model is told when it is given its final chance; for a task with an answer form, how to submit it."""
        text = FINAL_CHANCE_FORM.get(self.who["interface"], FINAL_CHANCE) if form else FINAL_CHANCE
        return text.format(why=_WHY[self.stop])

    def send(self, create, usage_of, *, final_chance: bool = False, **params):
        """Make one model request through `create(**params, timeout=...)`, counting and recording it and its response.

        The deadline is the time left (the final chance has its own `final_chance_seconds`), and it covers every
        attempt: SDK retries must be off (agents.without_retries) and transient failures are retried here, while time
        remains, up to MAX_ATTEMPTS; every attempt counts as a request. Returns the response, or None when no usable
        response came in time or within the limits: the limit is then in `stop` and `late` is set, so the harness goes
        on to the final chance (or, for a final chance, records no answer). A full context window raises LimitReached,
        since a final chance could not fit either; any other failure raises."""
        if not final_chance and not self.allow_request():
            raise LimitReached(self.stop)
        self.start_clock()
        began = time.monotonic()
        if final_chance and self._final_until is None:  # the final chance is one window, however many requests it takes
            self._final_until = began + self.limits.final_chance_seconds
        logged = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Money first: every attempt is reserved, durably, before it is sent (eprbench.spend), and the reservation
            # stays spent unless a response with its usage comes back.
            rid = None
            if self.spend is not None:
                rid, why = self.spend.reserve(params, final_chance)
                if why:
                    return self._unaffordable(why, final_chance)
            if not logged:
                self.event("request", request=self.requests + 1, final_chance=final_chance, params=self._delta(params))
                logged = True
            budget = (self._final_until - time.monotonic()) if final_chance else self.limits.timeout_seconds - self.elapsed()
            started = time.monotonic()
            try:
                response = create(**params, timeout=max(budget, 0.001))
            except Exception as e:
                if rid is not None:
                    self.event("spend_kept", reservation=rid, **self.spend.settle(rid, None, f"the attempt failed: {type(e).__name__}"))
                self.requests += 1
                kind = _failure(e)
                self.event("attempt_failed", request=self.requests, attempt=attempt, failure=kind, seconds=round(time.monotonic() - started, 3),
                           error=f"{type(e).__name__}: {e}", traceback="".join(traceback.format_exception(e)))  # fmt: skip
                if kind == "context":
                    self.stop = self.stop or "context_limit"
                    raise LimitReached(self.stop) from e
                remaining = (self._final_until - time.monotonic()) if final_chance else self.limits.timeout_seconds - self.elapsed()
                out_of_time = kind == "timeout" or remaining <= 0
                out_of_requests = not final_chance and self.requests >= self.limits.max_requests
                if kind == "transient" and not (out_of_time or out_of_requests) and attempt < MAX_ATTEMPTS and remaining > 0.5:
                    time.sleep(min(0.5 * 2 ** (attempt - 1), remaining / 2))
                    continue
                if final_chance:  # the episode has already ended at its limit: just no answer
                    self.late = True
                    return None
                if out_of_time or out_of_requests:  # a limit, handled like any other: on to the final chance
                    self.stop = self.stop or ("timeout" if out_of_time else "model_request_limit")
                    self.late = True
                    return None
                raise
            took = time.monotonic() - started
            self.requests += 1
            try:
                usage, unreadable = usage_of(response), None
            except Exception as e:  # the response came back, but what it cost cannot be read
                usage, unreadable = None, f"usage could not be read: {type(e).__name__}: {e}"
            counted = {k: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0 for k, v in (usage or {}).items()}  # for the record
            with self._lock:
                for k, v in counted.items():
                    self.usage[k] = self.usage.get(k, 0) + v
            self.late = took > budget if final_chance else self._timed_out() and self.stop == "timeout"
            # money: reconciled to the reported usage only if it is complete and plausible; otherwise kept, with why
            cost = {"cost": self.spend.settle(rid, usage, unreadable)} if rid is not None else {}
            self.event("response", request=self.requests, final_chance=final_chance, seconds=round(took, 3), late=self.late, usage=usage, **cost, body=response)
            if self.on_delivered is not None and (n := self.on_delivered(params)):
                self.event("screenshot_reads", request=self.requests, reservation=rid, entries=n)  # what the delivered images showed
            self._check_model(getattr(response, "model", None))
            return response

    def _check_model(self, reported) -> None:
        """The model version the provider says answered: every response of an episode, and every episode of a plan, must
        name the same one (the first response is checked like any other). A response naming none leaves the episode's
        version unverified."""
        if not reported:
            self.unverified_responses += 1
            return
        reported = str(reported)
        self.reported_models.add(reported)
        expected = self.expected_model_version or reported
        self.expected_model_version = expected
        if reported != expected:
            self.extra["model_drift"] = {"model": self.who["model"], "expected": expected, "reported": reported, "request": self.requests}
            raise ModelDrift(f"{self.who['model']} was answered by {reported}, not {expected}")

    def _unaffordable(self, why: dict, final_chance: bool):
        """A request not made because its worst-case cost would cross a spend ceiling: an ordinary one ends the
        episode at spend_limit (on to the final chance, if that fits); a final chance gets no answer."""
        self.event("spend_refused", request=self.requests + 1, **why)
        self.spend_refused = True
        if not final_chance:
            self.stop = self.stop or "spend_limit"
        self.late = True
        return None

    def _delta(self, params: dict) -> dict:
        """The request as sent. A conversation's `messages` already logged, and any parameter unchanged since the
        previous request, are named rather than repeated (and one no longer sent is named as omitted): request n is
        all earlier deltas applied in order."""
        out, unchanged = {}, []
        omitted = [k for k in self._sent if k not in params and self._sent[k] is not None]
        for key in omitted:
            self._sent[key] = None
        for key, value in params.items():
            if key == "messages" and isinstance(value, list):
                sent = self._sent.get("messages") or 0
                start = sent if len(value) >= sent else 0
                out["messages_from"], out["messages"] = start, value[start:]
                self._sent["messages"] = len(value)
                continue
            encoded = json.dumps(self.jsonable(value), sort_keys=True, default=str)
            if self._sent.get(key) == encoded:
                unchanged.append(key)
            else:
                out[key] = value
                self._sent[key] = encoded
        return out | ({"unchanged": unchanged} if unchanged else {}) | ({"omitted": omitted} if omitted else {})

    def action(self, n: int, name: str, args, result=None, *, audit_id: int | None = None, error: str | None = None, **data) -> None:
        """Record action number `n` (from `try_charge`; a screenshot request's own number) and what it returned, or the
        error it failed with."""
        if audit_id is not None:
            self.results[audit_id] = result
        number = {"action": None, "screenshot_request": n} if name in SCREENSHOT_ACTIONS else {"action": n}
        self.event("action", **number, name=name, args=args, result=result, error=error, audit_id=audit_id, **data)

    def end(self, reason: str, answer: str | dict | None, detail: str | None = None) -> None:
        """Set the terminal reason and final answer (text, or the submitted answer object). The first call wins."""
        assert reason in TERMINAL_REASONS, reason
        if self.spend_refused and reason != "spend_limit":  # any request refused for spend, a final chance's included, ends it there
            reason, detail = "spend_limit", f"a request was refused for spend; the run would otherwise have ended: {detail or reason}"
        if self.reason is None:
            self.reason, self.answer, self.detail = reason, answer, detail or reason
            self.event("end", terminal_reason=reason, detail=self.detail, answer=answer)

    def final_answer(self, response, extract) -> str | None:
        """Record the final chance and return the answer in its response, unless the response is missing or late."""
        answer = None if response is None or self.late else extract(response)
        self.final_chance = {"reason": self.stop, "request": self.requests, "answer": answer, "late": response is None or self.late}
        return answer

    # --- the record ----------------------------------------------------------

    def screenshot(self, png: bytes) -> dict:
        """Save a PNG once (by content) and return how the transcript refers to it."""
        sha = _sha256(png)
        with self._lock:
            if sha not in self._shots:
                self._shots[sha] = f"screenshots/{len(self._shots) + 1:04d}.png"
                if self.dir is not None:
                    (self.dir / "screenshots").mkdir(exist_ok=True)
                    (self.dir / self._shots[sha]).write_bytes(png)
        return {"screenshot": self._shots[sha], "sha256": sha}

    def jsonable(self, obj):
        """Plain JSON for any SDK object, with every base64 PNG replaced by a reference to its saved file."""
        if isinstance(obj, dict):
            if obj.get("type") == "base64" and str(obj.get("media_type", "")).startswith("image/") and isinstance(obj.get("data"), str):
                return {k: v for k, v in obj.items() if k != "data"} | self.screenshot(base64.b64decode(obj["data"]))
            return {str(k): self.jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.jsonable(v) for v in obj]
        if isinstance(obj, str):
            if obj.startswith("data:image/png;base64,"):
                return self.screenshot(base64.b64decode(obj.split(",", 1)[1]))
            return obj
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, bytes):
            return self.screenshot(obj)
        if hasattr(obj, "model_dump"):
            return self.jsonable(obj.model_dump(mode="json", exclude_none=True))
        if hasattr(obj, "__dict__"):
            return self.jsonable({k: v for k, v in vars(obj).items() if not k.startswith("_")})
        return repr(obj)

    def _since_created(self) -> float:
        return round(time.monotonic() - self._created, 3)

    def event(self, kind: str, **data) -> None:
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "wall": _wall(time.time()), "t": self._since_created(), "kind": kind, **self.redact(self.jsonable(data), "transcript.jsonl")}
            if self._transcript is not None and not self._transcript.closed:
                self._transcript.write(json.dumps(record, default=str) + "\n")
                self._transcript.flush()

    def http(self, method: str, path: str, query: str, headers: dict, request_body: bytes, status: int, content_type: str, body: bytes,
             audit_ids: list[int], error: str | None = None, output_mark: int | None = None) -> None:  # fmt: skip
        """Record one exchange with the EPR server (called from the server's thread), including one that failed.
        Credential headers are kept by name only, and their values are redacted wherever else they appear."""
        with self._lock:
            self.add_secrets([(f"header:{k}", v.split(" ", 1)[-1] if k.lower() in ("authorization", "proxy-authorization") else v)
                              for k, v in headers.items() if k.lower() in SECRET_HEADERS])  # fmt: skip
            self._http += 1
            entry = {"n": self._http, "after_seq": self._seq, "wall": _wall(time.time()), "t": self._since_created(), "method": method, "path": path, "query": query,
                     "headers": headers, "request_body": request_body.decode(errors="replace"), "status": status, "audit_ids": audit_ids, "error": error}  # fmt: skip
            if output_mark is not None:
                entry["output_mark"] = output_mark
            if "json" in content_type:
                try:
                    entry["response"] = json.loads(body)
                except ValueError:
                    entry["response"] = body.decode(errors="replace")
            elif self.dir is not None and body:
                (self.dir / "http").mkdir(exist_ok=True)
                name = f"http/{self._http:05d}.{'html' if 'html' in content_type else 'bin'}"
                written = self.redact(body.decode(errors="replace"), name).encode() if "html" in content_type else body
                (self.dir / name).write_bytes(written)
                entry |= {"response_file": name, "response_sha256": _sha256(written), "redacted": written != body}
            elif body:
                entry["response"] = body.decode(errors="replace")
            if self.dir is not None:
                with open(self.dir / "http.jsonl", "a") as f:
                    f.write(json.dumps(self.redact(entry, "http.jsonl"), default=str) + "\n")

    def save_file(self, name: str, data: bytes | str) -> None:
        if self.dir is not None:
            (self.dir / name).write_bytes(data.encode() if isinstance(data, str) else data)

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if exc is None:
            return False
        if isinstance(exc, LimitReached):
            self.end(self.stop, self.answer)
            return True
        self._timed_out()  # a request that failed because its deadline passed is a timeout
        self.error = "".join(traceback.format_exception(kind, exc, tb))
        self.event("error", error=f"{kind.__name__}: {exc}", traceback=self.error)
        self.end(self.stop or "harness_error", self.answer, detail=f"{kind.__name__}: {exc}")  # a failure after a limit is still that limit
        if not isinstance(exc, Exception):  # an interrupt: keep what we have, then stop
            self._write("result.json", self._result(None))
            return False
        return True

    def _result(self, report: dict | None) -> dict:
        return {
            "task_id": self.task.id, "repeat": self.repeat, **self.who, "terminal_reason": self.reason, "truncated": self.reason in TRUNCATED,
            "detail": self.detail, "answer": self.answer, "final_chance": self.final_chance, "requests": self.requests if self.requests_known else None,
            "actions": self.actions, "screenshot_actions": self.screenshot_actions, "accounting": self.accounting(), "usage": dict(self.usage), "limits": asdict(self.limits),
            "cost": self.spend.summary() if self.spend is not None else None, "reported_models": sorted(self.reported_models),
            "model_version": self.model_version(), **self.run_context,
            "started_at": _wall(self.started), "ended_at": _wall(time.time()), "wall_seconds": round(self.elapsed(), 1), "error": self.error, **self.extra,
            "score": report and report.get("score"), "success": report and report.get("success"), "unsafe": report and report.get("unsafe"),
        }  # fmt: skip

    def close(self, world: World | None) -> dict:
        """Snapshot the final world under its lock, grade a copy restored from that snapshot (so what was graded is
        exactly what was saved), finish the record and return the one-line summary, with credentials redacted."""
        if self.reason is None:
            self.end("harness_error", self.answer, detail="the harness ended without a terminal reason")
        report = None
        if world is not None:
            # Rules that have fallen due fire first, as grading would fire them, so the saved world is exactly the
            # world the checks run on; grading the restored copy fires nothing further.
            with world.lock:
                world.conn.execute("BEGIN")
                scenario.fire_due(world)
                world.conn.execute("COMMIT")
                snapshot = world.snapshot()
            self.save_file("final.sqlite", snapshot)
            self.extra["graded_world_sha256"] = _sha256(snapshot)
            self.extra["actions_summary"] = actions_summary(World.restore(snapshot), self.task, self.who["interface"])
            if hasattr(world, "source_sha256"):
                self.extra["data_sha256_loaded"] = world.source_sha256
            try:
                report = conclude(grade(World.restore(snapshot), self.task, self.answer), self.task, self.reason)
                if report["outcome"]["horizon"]:  # the copy assessed at the task's horizon, never saved: regrade rebuilds it
                    self.extra["horizon_world_sha256"] = report["outcome"]["horizon"]["world_sha256"]
            except Exception as e:
                self.error = (self.error or "") + "".join(traceback.format_exception(e))
                self.event("error", error=f"grading failed: {type(e).__name__}: {e}", traceback=self.error)
                self.reason, self.detail = "harness_error", f"grading failed: {type(e).__name__}: {e}"  # never a clinical outcome
            if self.dir is not None:
                # Each operation's result as the world stored it, the same for every surface (a UI page's operations
                # included); what a harness recorded is the fallback for worlds saved before results were stored.
                final = World.restore(snapshot)
                with open(self.dir / "audit.jsonl", "w") as f:
                    for row in final.q("SELECT * FROM audit ORDER BY id"):
                        row["args"] = json.loads(row["args"])
                        if row["result"] is not None:
                            row["result"] = json.loads(row["result"])
                        elif row["id"] in self.results:
                            row["result"] = self.jsonable(self.results[row["id"]])
                        f.write(json.dumps(self.redact(row, "audit.jsonl"), default=str) + "\n")
                with open(self.dir / "reads.jsonl", "w") as f:  # what each action showed the agent (agent_read)
                    for row in final.q("SELECT * FROM read_event ORDER BY id"):
                        f.write(json.dumps(row) + "\n")
        if report is None:
            report = {"task_id": self.task.id, "success": None, "score": None, "unsafe": None, "checks": [], "safety_findings": [], "trajectory": [],
                      "investigation_cost_gbp": 0, "investigations_ordered": 0, "operations": 0, "elapsed_seconds": 0, "outcome": None}  # fmt: skip
        self.extra["comparability"]["setup_seconds"] = self.extra["comparability"]["setup_seconds"] or round(time.monotonic() - self._created, 3)
        self._write("report.json", report)
        if self.spend is not None:  # every reservation reconciled, then the episode's line: its range goes in the manifest
            self.extra["ledger"] = self.spend.close(str(self.dir) if self.dir else None, self.reason)
        result = self._result(report)
        self._write("result.json", result)
        if self._transcript is not None:
            self._transcript.close()
        if self.dir is not None:  # what was redacted, where, and from which source (written last, so it is complete)
            meta = json.loads((self.dir / "episode.json").read_text())
            meta["redaction"]["counts"] = self.redactions
            meta.update(self.redact(self.meta_updates, "episode.json"))
            (self.dir / "episode.json").write_text(json.dumps(meta, indent=2) + "\n")
            if world is not None:
                write_manifest(self.dir, self.extra.get("ledger"), self.run_context.get("plan"), self.reason)
        usage = {**self.usage, "requests": self.requests if self.requests_known else None}
        return self.redact({
            **report, **{k: v for k, v in self.who.items() if v is not None}, "repeat": self.repeat, "answer": self.answer,
            "terminal_reason": self.reason, "truncated": self.reason in TRUNCATED, "ended_by": self.detail, "final_chance": self.final_chance,
            "steps": self.actions, "screenshot_actions": self.screenshot_actions, "accounting": self.accounting(), "usage": usage, "wall_seconds": result["wall_seconds"],
            "cost": result["cost"], "reported_models": result["reported_models"], "model_version": result["model_version"], **self.run_context,
            "limits": asdict(self.limits), **self.extra, **({"error": self.error.strip().splitlines()[-1]} if self.error else {}),
            **({"evidence_dir": str(self.dir)} if self.dir else {}),
        })  # fmt: skip


MANIFEST_FORMAT = "eprbench-manifest/1"
MANIFEST_REQUIRED = ("episode.json", "task.json", "transcript.jsonl", "final.sqlite", "audit.jsonl", "reads.jsonl", "report.json", "result.json")


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


COMPLETE_ENDINGS = frozenset(TERMINAL_REASONS) - ERRORS - {"spend_limit"}  # a clinical outcome, or a truncation that is one's equal


def write_manifest(d: Path, ledger: dict | None, plan: dict | None, terminal_reason: str | None = None) -> None:
    """The manifest, written last and atomically: the SHA-256 of every evidence file (each fsynced first), the ledger
    range of the episode's reservations with the hash of its last entry (so the evidence witnesses how far the ledger
    had got), and whether the episode is complete: it ran to a clinical outcome or a truncation other than a spend
    refusal (a refused or failed episode keeps its evidence, and is run again)."""
    files = {}
    for p in sorted(x for x in d.rglob("*") if x.is_file() and x.name not in ("manifest.json", "manifest.json.tmp")):
        _fsync(p)
        files[p.relative_to(d).as_posix()] = file_sha256(p)
    manifest = {"format": MANIFEST_FORMAT, "files": files, "ledger": ledger, "plan": plan, "terminal_reason": terminal_reason,
                "complete": terminal_reason in COMPLETE_ENDINGS, "written_at": _wall(time.time())}  # fmt: skip
    tmp = d / "manifest.json.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, d / "manifest.json")
    _fsync(d)


def manifest_problems(d: Path) -> list[str]:
    """Why an episode directory is not complete evidence (nothing if it is): no manifest, a required file missing from
    it, or any listed file missing or not matching its hash."""
    try:
        m = json.loads((Path(d) / "manifest.json").read_text())
    except (OSError, ValueError):
        return ["no readable manifest.json"]
    if m.get("format") != MANIFEST_FORMAT or not isinstance(m.get("files"), dict):
        return ["manifest.json is not an eprbench manifest"]
    problems = [f"{name} is not in the manifest" for name in MANIFEST_REQUIRED if name not in m["files"]]
    for name, sha in m["files"].items():
        p = Path(d) / name
        if not p.is_file():
            problems.append(f"{name} is missing")
        elif file_sha256(p) != sha:
            problems.append(f"{name} does not match its hash")
    return problems


ZERO_ACTIONS = {"actions": 0, "read_actions": 0, "write_actions": 0, "other_actions": 0, "operations": 0}


def actions_summary(world: World, task: Task, interface: str | None = None) -> dict:
    """Distinct agent actions by the task's actor since the start, per interface (tool, api, ui): those that changed
    the record (any successful write but a wait), those that only read it, and the rest (failed writes and waits).
    The episode's own interface is always present, with zeros when the agent did nothing (a measured zero)."""
    rows = world.q(
        """SELECT interface, count(DISTINCT action_id) AS actions, count(*) AS operations,
                  count(DISTINCT CASE WHEN ok = 1 AND writes = 1 AND op != 'wait' THEN action_id END) AS writing,
                  count(DISTINCT CASE WHEN writes = 1 THEN action_id END) AS any_write
           FROM audit WHERE actor = ? AND interface IN ('tool', 'api', 'ui') AND at >= ? GROUP BY interface ORDER BY interface""",
        (task.actor, parse_iso(task.start_time)),
    )
    out = {i: dict(ZERO_ACTIONS) for i in ([interface] if interface in ("tool", "api", "ui") else ["ui"] if interface == "both" else [])}
    return out | {r["interface"]: {"actions": r["actions"], "read_actions": r["actions"] - r["any_write"], "write_actions": r["writing"],
                                   "other_actions": r["any_write"] - r["writing"], "operations": r["operations"]} for r in rows}  # fmt: skip


def episode_dirs(root: str | Path) -> list[Path]:
    """Every saved episode under `root` (or `root` itself), in name order."""
    root = Path(root)
    return sorted({p.parent for p in root.rglob("final.sqlite") if (p.parent / "result.json").is_file()})


ASSESSMENT_FIELDS = ("checks", "pathways", "answer_schema", "horizon_seconds")  # how a task is assessed, as against what was run


def regrade(path: str | Path, saved_task: bool = False) -> dict:
    """Grade a saved episode again from its final world with the current grader.

    The episode's own context (start time, actor, bundle, everything else in the task as run) is kept; only how it
    is assessed (ASSESSMENT_FIELDS) comes from the current packaged task, unless `saved_task` keeps that too. Says
    whether final.sqlite is still the world graded at the time and, for a task with a horizon, whether the horizon
    world was rebuilt to the same SHA-256 (None where the episode recorded no hash, or where the saved world was written
    at an earlier schema version and migrated, `world_migrated_from`, since the rebuilt copy then has the new schema)."""
    path = Path(path)
    result = json.loads((path / "result.json").read_text())
    old = json.loads((path / "report.json").read_text()) if (path / "report.json").is_file() else {}
    meta = json.loads((path / "episode.json").read_text()) if (path / "episode.json").is_file() else {}
    ran = Task.model_validate_json((path / "task.json").read_text())
    try:
        packaged = load_task(ran.id)
        current = {f: getattr(packaged, f) for f in ASSESSMENT_FIELDS}
    except FileNotFoundError:
        if not saved_task:
            raise FileNotFoundError(f"task {ran.id} is no longer packaged; regrade with --saved-task to use the saved definition") from None
        current = None
    task = ran if saved_task else Task.model_validate({**ran.model_dump(), **current})
    snapshot = (path / "final.sqlite").read_bytes()
    report = grade(World.restore(snapshot), task, result.get("answer"), terminal_reason=result.get("terminal_reason"))
    then = meta.get("provenance", {})
    now = provenance(task)
    horizon = (report["outcome"]["horizon"] or {}).get("world_sha256")
    recorded = {k: result.get(k) for k in ("graded_world_sha256", "horizon_world_sha256")}
    saved = saved_schema(snapshot)
    migrated = saved != SCHEMA_VERSION  # a migrated world's horizon copy has the new schema: its hash cannot match
    return {
        "episode": path.name, "task_id": task.id, "terminal_reason": result.get("terminal_reason"),
        "old_score": old.get("score"), "new_score": report["score"], "old_success": old.get("success"), "new_success": report["success"],
        "old_unsafe": old.get("unsafe"), "new_unsafe": report["unsafe"], "old_tier": (old.get("outcome") or {}).get("tier"),
        "new_tier": report["outcome"]["tier"], "grader_changed": then.get("grader_sha256") != now["grader_sha256"],
        "task_changed": None if current is None else any(current[f] != getattr(ran, f) for f in ASSESSMENT_FIELDS),
        "graded_world_reproduced": None if recorded["graded_world_sha256"] is None else recorded["graded_world_sha256"] == _sha256(snapshot),
        "horizon_world_sha256": horizon,
        "horizon_world_reproduced": None if recorded["horizon_world_sha256"] is None or migrated else recorded["horizon_world_sha256"] == horizon,
        "world_migrated_from": (saved or "unversioned") if migrated else None,
        "report": report,
    }  # fmt: skip
