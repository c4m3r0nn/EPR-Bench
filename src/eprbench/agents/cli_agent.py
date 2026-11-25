"""Run an agent command-line tool (Claude Code, Codex, or any other) on a task.

The EPR is served in API-only mode on a local port and the agent is told to
work it with curl. This needs no API key, only a logged-in CLI, and works for
any vendor's agent. It measures the agent product as shipped (its own system
prompt, tools and loop), which is a different thing from the bare model that
the SDK harnesses measure; report the two separately.

The agent runs in an empty temporary directory and, on macOS, under a sandbox
that denies it the repository, the installed package, package caches and the
evidence directory, which hold every task's ground truth (see eprbench.isolation).
Without a sandbox it is not run, unless that is explicitly allowed. Its output is
also scanned for signs that it saw the ground truth anyway. Its server has no
harness token, so it cannot reset or grade the world.

Limits (each episode records which of them were enforced, in `limits_enforced`):
each API operation call is an action, charged by the server before it runs; past
the action limit calls are refused, with a request for the answer and a grace
period to give it when the episode allows a final chance. Model requests are
counted from the agent's event stream where it prints one (Claude Code's
stream-json) and limited where the CLI can be told to (Claude Code's --max-turns,
which ends without a final chance); Codex can be neither limited nor counted. The
timeout stops the agent and everything it started, keeping its output so far.

The answer: for a task with an answer form the agent is told to submit it through POST /api/ops/submit (listed
with the operations), and the accepted submission is the answer; the agent is then given SUBMIT_GRACE_SECONDS to
exit before it is stopped, and its closing message is kept as `closing_message`. If it exits without submitting,
it has stopped without submitting and its final message is the answer's text. For a task without a form, the
final message is the answer, as before. After a limit, a submission is still accepted within the final chance.
"""

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from eprbench import answers, isolation
from eprbench.agents import SYSTEM_PROMPT
from eprbench.episode import Episode, Limits, argv_secrets, context_full, redact_stream_line
from eprbench.server.local import serve_task
from eprbench.tasks import Task, load_task

API_NOTE = """

You can ONLY interact with the record through its HTTP JSON API, using curl:
  GET  {url}/api/ops           lists every operation with its input schema, grouped by section
  POST {url}/api/ops/<name>    calls one, with a JSON object as the body
Do not read local files or use any other endpoint. When everything you intend to do is done, your \
final message must be only your reply to the colleague: the answer, or a brief account of what you did.

Task: {instruction}"""
# For a task with an answer form, the final answer is given through the submit endpoint instead.
API_NOTE_FORM = API_NOTE.replace(
    """When everything you intend to do is done, your \
final message must be only your reply to the colleague: the answer, or a brief account of what you did.""",
    """This task has an answer form. \
When everything you intend to do is done, give your final answer by calling POST {url}/api/ops/submit (listed with \
the operations, with its schema) once, with your reply to the colleague in "answer" (the answer, or a brief account \
of what you did) and the form in "fields". Once it is accepted the task is over: stop.""",
)
SUBMIT_GRACE_SECONDS = 30.0  # how long an agent may take to exit after its answer has been accepted

# argv templates. {prompt} is the full prompt; {answer_file} is where the final message should go if the tool can
# write it to a file (otherwise standard output is taken as the answer); {max_requests} is the request limit; {debug_file}
# is where Claude Code writes its debug log, from which the instruction sources it loaded are recorded.
#
# Both routes of a CLI (this API route and cli_browser's UI route) run under the SAME controlled configuration: the
# common flags below and CONTROLLED_ENV, differing only in the tool and MCP options (TOOL_OPTIONS says which those are).
# Verified with no model request (a model id that does not exist), from each CLI's own report: Claude Code's init event
# and debug log, Codex's `codex debug prompt-input` (docs/ARCHITECTURE.md, "Agent CLIs through the UI"):
# - Claude Code: user and local settings (so hooks, enabled plugins, output styles and permissions), skills and slash
#   commands, every MCP server but those given, session persistence, CLAUDE.md files (CLAUDE_CODE_DISABLE_CLAUDE_MDS:
#   its debug log says "project memory is off") and auto memory (CLAUDE_CODE_DISABLE_AUTO_MEMORY: its init event has
#   no memory_paths) are shut out. --safe-mode is not used on either route: it also removes --mcp-config servers. Its
#   built-in plugins (agents-md, telemetry, privacy, plugin-authoring) load on both routes alike.
# - Codex: user config.toml and rules ignored; plugins, apps, hooks, skill search, browser and computer use, sub-agents,
#   tool suggestions, sleep, image viewing and generation and web search off; and the skills list left out of its
#   instructions (skills.include_instructions=false).
# A user-level instruction file the CLI would read (instruction_files) stops the run before the agent starts.
CLAUDE_COMMON = [
    "-p", "{prompt}", "--strict-mcp-config", "--permission-prompts", "none", "--disable-slash-commands", "--setting-sources", "project",
    "--no-session-persistence", "--max-turns", "{max_requests}", "--output-format", "stream-json", "--verbose", "--debug-file", "{debug_file}",
]
CODEX_DISABLED = ("plugins", "apps", "hooks", "skill_search", "browser_use", "browser_use_external", "in_app_browser", "computer_use", "multi_agent",
                  "tool_suggest", "sleep_tool", "image_generation", "view_image")
CODEX_COMMON = [
    "exec", "--skip-git-repo-check", "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules", *[a for f in CODEX_DISABLED for a in ("--disable", f)],
    "-c", 'web_search="disabled"', "-c", 'approval_policy="never"', "-c", "skills.include_instructions=false",
]
CONTROLLED_ENV = {"claude": {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}, "codex": {}}
# The options that may differ between a CLI's two routes: the tools, their permissions, MCP and the sandbox mode.
TOOL_OPTIONS = {"--tools", "--allowedTools", "--mcp-config", "--sandbox", "{mcp_overrides}"}
PRESETS = {
    "claude": ["claude", *CLAUDE_COMMON, "--tools", "Bash", "--allowedTools", "Bash(curl:*)"],
    "codex": ["codex", *CODEX_COMMON, "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true", "-o", "{answer_file}", "{prompt}"],
}  # fmt: skip
# How each preset is told the model and reasoning effort; not passed when not asked for, so the CLI's default applies.
MODEL_FLAGS = {"claude": lambda m: ["--model", m], "codex": lambda m: ["-m", m]}
EFFORT_FLAGS = {"claude": lambda e: ["--effort", e], "codex": lambda e: ["-c", f'model_reasoning_effort="{e}"']}
# Where a preset's CLI is, when it is not simply the one found on PATH. When the variable is set it is the only place
# looked: there is no PATH search behind it (the test suite points both at stubs, so no test can reach a real CLI).
CLI_BIN_ENV = {"claude": "EPRBENCH_CLAUDE_BIN", "codex": "EPRBENCH_CODEX_BIN"}


def cli_executable(agent: str, program: str | None = None) -> str:
    """The executable a preset runs: EPRBENCH_<NAME>_BIN when that is set (used as given, never searched for), otherwise
    the CLI's name, found on PATH when it is started. `program` is the preset's own first word: only the bare CLI name
    is located this way (a preset replaced by another program keeps it)."""
    var = CLI_BIN_ENV.get(agent)
    if program is not None and program != agent:
        return program
    return (os.environ.get(var) or agent) if var else agent


def common_flags(argv: list[str]) -> list[str]:
    """A preset without its tool and MCP options (and their values): what must be the same on a CLI's two routes."""
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in TOOL_OPTIONS:
            skip = a != "{mcp_overrides}"
            continue
        if a == "-c" and i + 1 < len(argv) and argv[i + 1].startswith(("sandbox_workspace_write.", "mcp_servers.")):
            skip = True
            continue
        if a == "--disable" and i + 1 < len(argv) and argv[i + 1] in ("shell_tool", "unified_exec"):
            skip = True
            continue
        out.append(a)
    return out


def instruction_files(agent: str, cwd: str | Path) -> list[str]:
    """Instruction files the CLI would read for a run in `cwd`: the user's own (Claude Code's ~/.claude, Codex's
    CODEX_HOME) and any in `cwd` or its ancestors. A run is refused while any exists."""
    home = Path.home()
    if agent == "claude":
        user = [home / ".claude" / n for n in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")] + sorted((home / ".claude" / "rules").glob("*.md"))
        names = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "AGENTS.md", ".claude/AGENTS.md")
    elif agent == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        user = [codex_home / "AGENTS.md", codex_home / "AGENTS.override.md"]
        names = ("AGENTS.md", "AGENTS.override.md")
    else:
        return []
    found = [str(f) for f in user if f.is_file()]
    for d in [Path(cwd).resolve(), *Path(cwd).resolve().parents]:
        found += [str(d / n) for n in names if (d / n).is_file()]
    return found


DEBUG_SOURCES = re.compile(r"CLAUDE\.md|AGENTS\.md|project memory|[Mm]emory|Loading skills|Loaded \d+ unique skills|installed plugins|plugin\.register|output styles")


def claude_sources(debug_file: Path) -> list[str]:
    """What Claude Code's debug log says about where its instructions came from (CLAUDE.md and AGENTS.md files, memory,
    skills, plugins), without timestamps; the rest of the log is not kept."""
    try:
        lines = debug_file.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return list(dict.fromkeys(re.sub(r"^\S+ \[\w+\] ", "", x)[:300] for x in lines if DEBUG_SOURCES.search(x)))


INIT_FIELDS = ("model", "tools", "mcp_servers", "plugins", "skills", "slash_commands", "agents", "output_style", "memory_paths", "apiKeySource",
               "permissionMode", "claude_code_version")


def inventory(agent, stream, debug_file: Path | None, preflight: dict | None = None) -> dict:
    """The effective tool inventory and instruction sources, as the CLI itself reported them: Claude Code's init event
    and debug log; for Codex, its rendering of the same flags before the run (codex_preflight)."""
    if stream.init is not None:
        return {"reported_by": "Claude Code's init event and debug log", **{k: stream.init.get(k) for k in INIT_FIELDS},
                "instruction_sources": claude_sources(debug_file) if debug_file is not None else []}  # fmt: skip
    if preflight is not None:
        return {"reported_by": "codex debug prompt-input with the run's flags (no model request)", **preflight}
    return {"reported_by": None, "note": "this command reports no inventory"}


def inventory_problems(init: dict, tools: set[str], servers: set[str]) -> list[str]:
    """How Claude Code's init event differs from the controlled configuration: other tools, other MCP servers, any
    memory path, any skill. Nothing: as intended."""
    found = []
    if set(init.get("tools") or []) != tools:
        found.append(f"tools {sorted(init.get('tools') or [])}, expected {sorted(tools)}")
    if names := {s.get("name") for s in init.get("mcp_servers") or []} - servers:
        found.append(f"MCP servers {sorted(names)}")
    if init.get("memory_paths"):
        found.append(f"memory paths {init['memory_paths']}")
    if init.get("skills"):
        found.append(f"skills {init['skills']}")
    return found


EXEC_ONLY = {"exec", "--skip-git-repo-check", "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules"}


def codex_preflight(argv: list[str], cwd, env: dict, timeout: float = 120) -> dict:
    """Codex's own rendering of what its model would be given under these flags (`codex debug prompt-input`, which
    makes no model request): its tools, the nested tools of its exec tool, deferred namespaces, and the heading of every
    instruction message. Run with an empty CODEX_HOME, which is what --ignore-user-config (an exec-only flag) amounts to
    for configuration; instruction files under the real CODEX_HOME are refused before the run (instruction_files)."""
    flags, skip = [], False
    for i, a in enumerate(argv[1:-1], 1):  # the last argument is the prompt
        if skip:
            skip = False
            continue
        if a in EXEC_ONLY:
            continue
        if a in ("-o", "--output-last-message"):
            skip = True
        elif a in ("-m", "--model"):
            flags += ["-c", f"model={json.dumps(argv[i + 1])}"]
            skip = True
        elif a in ("--sandbox", "-s"):
            flags += ["-c", f"sandbox_mode={json.dumps(argv[i + 1])}"]
            skip = True
        else:
            flags.append(a)
    with tempfile.TemporaryDirectory() as home:
        try:
            done = subprocess.run([argv[0], "debug", "prompt-input", *flags, "."], cwd=cwd, env={**env, "CODEX_HOME": home}, capture_output=True,
                                  text=True, timeout=timeout, stdin=subprocess.DEVNULL)  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            return {"error": f"{type(e).__name__}: {e}"}
    try:
        items = json.loads(done.stdout)
    except ValueError:
        return {"error": f"exit {done.returncode}: {done.stderr[-500:]}"}
    tools, nested, messages, deferred, agents_md = [], [], [], [], False

    def walk(entries, prefix=""):
        for x in entries:
            if x.get("type") == "namespace":
                walk(x.get("tools") or [], prefix + x["name"] + ".")
            else:
                tools.append(prefix + str(x.get("name") or x.get("type")))
                if x.get("name") == "exec":
                    nested.extend(re.findall(r"^### `([^`]+)`", x.get("description") or "", re.M))

    for item in items if isinstance(items, list) else []:
        if item.get("type") == "additional_tools":
            walk(item.get("tools") or [])
        elif item.get("type") == "message":
            for c in item.get("content") or []:
                text = c.get("text") or ""
                if text and text != ".":
                    messages.append(f"{item.get('role')}: {text.strip().splitlines()[0][:120]}")
                    if "Deferred tool namespaces" in text:  # "- mcp__computer" or "- name: what it is", one a line
                        deferred += re.findall(r"^- ([^\s:]+)", text[text.find("Deferred tool namespaces") :], re.M)
                    agents_md = agents_md or "AGENTS.md instructions for" in text  # the block Codex adds for an AGENTS.md it read
    return {"tools": tools, "exec_nested_tools": nested, "instruction_messages": messages, "deferred": deferred, "agents_md_loaded": agents_md}


def preset_argv(agent: str, sandboxed: bool, model: str | None, effort: str | None) -> list[str]:
    """A preset's argv template with the model and effort pinned where given. Under our sandbox Codex cannot start its
    own (macOS sandboxes do not nest), so it runs its commands unsandboxed inside ours, which allows the network and
    everything except the protected paths."""
    argv = list(PRESETS[agent])
    argv[0] = cli_executable(agent, argv[0])
    if sandboxed and agent == "codex":
        i = argv.index("--sandbox")
        argv[i : i + 4] = ["--sandbox", "danger-full-access"]
    extra = (MODEL_FLAGS[agent](model) if model else []) + (EFFORT_FLAGS[agent](effort) if effort else [])
    at = argv.index("{prompt}") if agent == "codex" else len(argv)
    return argv[:at] + extra + argv[at:]


# Credentials that would make an agent CLI bill an API account instead of using the logged-in subscription (or send
# its requests elsewhere). They are withheld from the agent unless the caller opts in.
_CREDENTIAL = re.compile(
    r"^(ANTHROPIC|CLAUDE|OPENAI|AZURE_OPENAI|CODEX|GEMINI|GOOGLE|VERTEX|XAI|MISTRAL|COHERE)\w*(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|_TOKEN|_KEY)$"
    r"|^(ANTHROPIC_BASE_URL|OPENAI_BASE_URL|CLAUDE_CODE_USE_BEDROCK|CLAUDE_CODE_USE_VERTEX|GOOGLE_APPLICATION_CREDENTIALS|AWS_BEARER_TOKEN_BEDROCK)$"
)


def agent_environment(pass_credentials: bool, agent=None) -> tuple[dict, dict]:
    """The environment to launch the agent with (a preset's CONTROLLED_ENV included), and how it is to authenticate
    (names only, never values)."""
    env = dict(os.environ) | (CONTROLLED_ENV.get(agent, {}) if isinstance(agent, str) else {})
    found = sorted(k for k in env if _CREDENTIAL.match(k))
    if pass_credentials:
        return env, {"mode": "api_key" if found else "subscription", "withheld": [], "passed": found}
    for k in found:
        del env[k]
    return env, {"mode": "subscription", "withheld": found, "passed": []}


def cli_version(executable: str) -> str | None:
    try:
        done = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or done.stderr).strip() or None


def limits_enforced(agent: str | list[str], final_chance: bool = True) -> dict:
    """Which limits this agent can be held to, stated in the episode record rather than implied."""
    requests = {
        "claude": "Claude Code's --max-turns, checked against the turns counted from its stream-json events; it ends without a final chance",
        "codex": "not enforced or counted: codex exec has no turn limit and its --json events do not show each model request",
    }.get(agent if isinstance(agent, str) else "", "not enforced: counted, and stopped when over, only if the command prints Claude Code's stream-json events")
    after = ["action_limit", "timeout"]  # a request limit ends the agent itself (Claude Code's --max-turns) or is not enforced
    return {"max_requests": agent == "claude", "max_actions": True, "timeout": True, "final_chance_after": after if final_chance else [],
            "notes": {"max_requests": requests, "max_actions": "charged by the EPR server before each operation call",
                      "timeout": "measured from when the agent starts", "final_chance": "after any limit, further API calls are refused with a request "
            "for the answer and the agent has final_chance_seconds to finish; it is not otherwise told, and may make more model requests meanwhile "
            "(counted where its event stream shows them)"}}  # fmt: skip


# How a run ended, as the CLI's terminal event says (Stream.ending): only the last of Claude Code's `result` events or of
# Codex's turn.completed / turn.failed / error events counts. A rate-limit warning, a request retried after a 429, or an
# answer that mentions a limit is not an ending. Built against the saved episodes' events (tests/test_usage_limits.py).
TERMINAL_EVENTS = ("result", "turn.completed", "turn.failed", "error")
_LIMIT_CODES = {"usage_limit_reached", "usage_limit_exceeded", "rate_limit_error", "rate_limit_exceeded", "insufficient_quota", "quota_exceeded",
                "too_many_requests"}  # fmt: skip
_LIMIT_TEXT = re.compile(r"usage[_ ]limit|rate[_ ]limit|hit your (?:[\w-]+ )?limit|insufficient_quota|exceeded your (?:current )?quota|"
                         r"quota exceeded|too many requests|\b429\b", re.I)  # fmt: skip


def classify_ending(event: dict | None) -> dict | None:
    """What a CLI's terminal event says about how its run ended: {"kind": "usage_limit" | "context_limit", "source",
    "status", "error", "message"}, or None (it succeeded, or failed for another reason). A full context window is
    `context_limit`, never a usage limit, whatever words it uses."""
    if not event:
        return None
    kind = event.get("type")
    if kind == "turn.completed" or (kind == "result" and not event.get("is_error")):
        return None
    error = event.get("error")
    detail = error if isinstance(error, dict) else {}
    if kind == "result":
        source, message = "Claude Code result event", str(event.get("result") or "")
        status, code = event.get("api_error_status"), event.get("api_error")
        codes = [event.get("api_error"), event.get("api_error_type")]
    else:
        source = "Codex event"
        message = str(detail.get("message") or "") if detail else str(error or event.get("message") or "")
        status, code = detail.get("status") or detail.get("http_status") or event.get("status"), detail.get("code") or detail.get("type")
        codes = [detail.get("code"), detail.get("type")]
    words = " ".join(str(c) for c in codes if c) + " " + message
    found = {"source": source, "status": status, "error": code, "message": message[:500]}
    if context_full(words):
        return {"kind": "context_limit", **found}
    if str(status) == "429" or any(str(c).lower() in _LIMIT_CODES for c in codes if c) or _LIMIT_TEXT.search(words):
        return {"kind": "usage_limit", **found}
    return None


class Stream:
    """What an agent's standard output says, read line by line as it is written: its answer (the last `result` event
    of an event stream), the model turns it made (distinct assistant message ids), its token usage, the model it
    reports, and how it ended (`ending`, from its terminal event only: a subscription's usage or rate limit, with the
    message that usually says when it resets; a full context window; or neither)."""

    def __init__(self):
        self.text, self.result, self.turns, self.usage, self.events = [], None, set(), {}, False
        self.model, self.init, self.terminal = None, None, None

    def feed(self, line: str) -> None:
        self.text.append(line)
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or "type" not in event:
            return
        self.events = True
        kind = event["type"]
        if kind == "assistant" and (event.get("message") or {}).get("id"):
            self.turns.add(event["message"]["id"])
        elif kind == "result":
            self.result = event
            self.usage = {k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)}
        elif kind == "turn.completed":  # Codex
            for k, v in (event.get("usage") or {}).items():
                if isinstance(v, int):
                    self.usage[k] = self.usage.get(k, 0) + v
        if kind in TERMINAL_EVENTS:
            self.terminal = event
        if kind == "system" and event.get("subtype") == "init":  # Claude Code's init event: its model, tools and instruction sources
            self.init, self.model = event, event.get("model") or self.model

    def ending(self) -> dict | None:
        return classify_ending(self.terminal)

    @property
    def usage_limit(self) -> dict | None:
        """The usage or rate limit the run ended on: {"source", "status", "error", "message"}; None if it did not."""
        found = self.ending()
        return {k: v for k, v in found.items() if k != "kind"} if found and found["kind"] == "usage_limit" else None

    @property
    def context_full(self) -> bool:
        found = self.ending()
        return bool(found and found["kind"] == "context_limit")

    def answer(self) -> str:
        """The result event's text; nothing for an event stream without one (the operations' results in it are not an
        answer); otherwise everything printed."""
        if self.result:
            return str(self.result.get("result") or "")
        return "" if self.events else "".join(self.text)


def _answer(stdout: str) -> tuple[str, dict | None]:
    stream = Stream()
    for line in stdout.splitlines(keepends=True):
        stream.feed(line)
    return stream.answer(), stream.result


def _pump(pipe, path: Path, ep: Episode, sink) -> threading.Thread:
    """Copy one of the agent's output streams to the evidence as it arrives, with credentials redacted, and pass
    each line to `sink`."""

    def run():
        with open(path, "w") as out:
            for raw in iter(pipe.readline, b""):
                line = raw.decode(errors="replace")
                out.write(redact_stream_line(ep.redact(line, path.name)))
                out.flush()
                sink(line)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _stop(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Stop the agent and every process it started, whether or not the agent itself has already exited.

    macOS can answer EPERM rather than ESRCH for a group whose members are still exiting, so that is treated as
    "still there" and never raised."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            proc.poll()  # reap the leader, so only live processes keep the group in existence
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            time.sleep(0.02)


def agent_config(agent, presets: dict, model, effort) -> dict:
    """What the episode records about the agent's configuration: model and effort asked for, CLI version, and the
    controlled configuration both routes share (the flags but the tool ones, and the environment)."""
    inherited = "inherited: not passed, so the CLI's own default applies (user configuration is shut out where the CLI allows)"
    if not isinstance(agent, str):
        inherited = "inherited: not passed to a custom command, which decides its own"
    preset = isinstance(agent, str)
    return {
        "model": {"requested": model} | ({} if model and preset else {"note": inherited}),
        "effort": {"requested": effort} | ({} if effort and preset else {"note": inherited}),
        "cli_version": cli_version(cli_executable(agent, presets[agent][0]) if preset else agent[0]),
        "controlled": {"common_flags": common_flags(presets[agent]), "environment": CONTROLLED_ENV.get(agent, {}),
                       "same_on_both_routes": "every flag but the tool, MCP and sandbox options, and the environment"} if preset
                      else "a custom command: its own configuration",
    }  # fmt: skip


def refuse_instruction_files(ep: Episode, agent, cwd) -> None:
    """Stop before the agent starts if a user-level or ancestor instruction file would be read."""
    if isinstance(agent, str) and (files := instruction_files(agent, cwd)):
        ep.extra["instruction_files_found"] = files
        raise RuntimeError(f"instruction files the agent would read: {', '.join(files)}; move them away to run it under the controlled configuration")


def refuse_preflight(ep: Episode, preflight: dict | None) -> None:
    """Stop before the agent starts if Codex's own rendering shows an AGENTS.md it would read, or could not be made."""
    if preflight is None:
        return
    ep.extra["agent_inventory_preflight"] = preflight
    if preflight.get("agents_md_loaded"):
        raise RuntimeError("Codex would read an AGENTS.md (its rendering of the run's flags shows one); move it away to run under the controlled configuration")
    if preflight.get("error"):
        raise RuntimeError(f"Codex could not show what it would run with: {preflight['error']}")


def run_episode(task: Task | str, agent: str | list[str], limits: Limits | None = None, timeout: float | None = None, out_dir=None,
                repeat: int = 0, isolate: bool = True, protect=(), allow_unsandboxed: bool = False, model: str | None = None,
                effort: str | None = None, pass_credentials: bool = False) -> dict:  # fmt: skip
    """Play one episode. `agent` is a preset name or an argv template. `timeout` overrides the limits' wall-clock limit.

    `isolate` runs the agent under the sandbox; if the sandbox cannot be applied the episode ends as a harness error
    before the agent starts, unless `allow_unsandboxed`. `protect` adds paths the agent must not read. `model` and
    `effort` are passed to a preset CLI (a custom command gets them only if it says so itself). Provider credentials in
    the environment are withheld from the agent, so it uses its logged-in subscription, unless `pass_credentials`.
    """
    task = load_task(task) if isinstance(task, str) else task
    limits = limits or Limits()
    if timeout is not None:
        limits = Limits(**{**limits.__dict__, "timeout_seconds": timeout})
    name = agent if isinstance(agent, str) else Path(agent[0]).name
    env, auth = agent_environment(pass_credentials, agent)
    config = agent_config(agent, PRESETS, model, effort)
    settings = {"agent": agent, "isolate": isolate, "allow_unsandboxed": allow_unsandboxed, "auth": auth, "agent_config": config}
    ep = Episode(task, harness="cli_agent", interface="api", agent=name, effort=effort, limits=limits, settings=settings, out_dir=out_dir,
                 repeat=repeat, secrets=argv_secrets(agent if isinstance(agent, list) else PRESETS[agent]))  # fmt: skip
    ep.who["model"] = model
    ep.extra["auth"], ep.extra["agent_config"] = auth, config
    ep.extra["limits_enforced"] = limits_enforced(agent, limits.final_chance)
    ep.extra["comparability"] |= {
        "timer_starts": "at agent process launch: the agent's own model requests are not visible to the harness; set-up before launch is setup_seconds",
        "action_unit": "one API operation call", "screenshots": "none: operations return data",
        "batch_stops_on": "not applicable: the agent makes its calls one at a time",
        "malformed_calls": "a call the server cannot parse is charged as an action and answered with an error",
    }  # fmt: skip
    ep.extra["sandboxed"] = False
    protected = isolation.protected_paths([*protect, *([out_dir] if out_dir is not None else [])])
    app, stream, proc = None, Stream(), None

    with ep, serve_task(task, "api", ep) as (url, app), tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as scratch:
        prefix, why_not = isolation.sandbox(protected) if isolate else ([], "isolation switched off")
        ep.extra["sandboxed"] = sandboxed = bool(prefix)
        ep.extra["isolation"] = {"sandbox": sandboxed, "why_not": why_not, "protected": protected}
        if isolate and not sandboxed and not allow_unsandboxed:
            raise RuntimeError(f"the agent cannot be sandboxed here ({why_not}); pass --allow-unsandboxed to run it anyway")
        refuse_instruction_files(ep, agent, workdir)
        template = preset_argv(agent, sandboxed, model, effort) if isinstance(agent, str) else agent
        answer_file, debug_file = Path(workdir) / "answer.txt", Path(scratch) / "claude-debug.log"
        prompt = SYSTEM_PROMPT + (API_NOTE_FORM if answers.has_form(task) else API_NOTE).format(url=url, instruction=task.instruction)
        fill = {"{prompt}": prompt, "{answer_file}": str(answer_file), "{max_requests}": str(limits.max_requests), "{debug_file}": str(debug_file)}
        command = [fill.get(part, part) for part in template]
        argv = prefix + command
        preflight = codex_preflight(command, workdir, env) if agent == "codex" else None
        refuse_preflight(ep, preflight)
        ep.event("agent_started", argv=[a if a != prompt else "{prompt}" for a in argv], prompt=prompt, preflight=preflight)
        expected = ({"Bash"}, set()) if agent == "claude" else None

        logs = Path(scratch) if ep.dir is None else ep.dir
        ep.start_clock()  # no model request is visible from here, so the clock starts when the agent does
        proc = subprocess.Popen(argv, cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True)
        errors: list[str] = []
        ep.output_mark = 0

        mismatch: list[str] = []

        def read(line: str) -> None:  # counted as read, so each API call records how much of the agent's output preceded it
            first = stream.init is None
            stream.feed(line)
            ep.output_mark += 1
            if first and stream.init is not None and expected is not None:
                mismatch.extend(inventory_problems(stream.init, *expected))

        pumps = [_pump(proc.stdout, logs / "agent.stdout", ep, read), _pump(proc.stderr, logs / "agent.stderr", ep, errors.append)]
        # As in the SDK harnesses: once a limit is reached nothing further is carried out (the server refuses calls,
        # asking for the answer) and, with a final chance, the agent has final_chance_seconds from that moment to
        # finish; an answer given after that is not accepted.
        limit_at, turns_at_limit, in_time, submitted_at = None, None, True, None
        while proc.poll() is None and not mismatch:
            if app.state.submission is not None:  # the answer is in: the agent has a short while to finish
                submitted_at = submitted_at or time.monotonic()
                if time.monotonic() - submitted_at >= SUBMIT_GRACE_SECONDS or ep.elapsed() >= limits.timeout_seconds:
                    break
                time.sleep(0.05)
                continue
            if ep.stop is None and len(stream.turns) > limits.max_requests:
                ep.stop = "model_request_limit"
            if ep.stop is None and ep.elapsed() >= limits.timeout_seconds:
                ep.stop = "timeout"
            if ep.stop:
                if limit_at is None:
                    limit_at, turns_at_limit = time.monotonic(), len(stream.turns)
                if not limits.final_chance or time.monotonic() - limit_at >= limits.final_chance_seconds:
                    in_time = False
                    break
            time.sleep(0.05)
        _stop(proc)  # the whole process group, including anything left running after the agent exited
        for pump in pumps:
            pump.join(timeout=10)
        form = app.state.submission["answer"] if app.state.submission is not None else None
        if ep.stop is None and form is None and ep.elapsed() >= limits.timeout_seconds:
            ep.stop, limit_at, turns_at_limit = "timeout", time.monotonic(), len(stream.turns)  # it ended between two checks of the clock
            in_time = limits.final_chance
        stderr = "".join(errors)
        ep.requests, ep.requests_known = len(stream.turns), bool(stream.turns)
        ep.usage.update(stream.usage)
        config["model"]["observed"] = stream.model  # what the agent says it ran, where its event stream says
        ep.extra["agent_inventory"] = config["inventory"] = inventory(agent, stream, debug_file, preflight)
        ep.meta_updates["agent_config"] = config
        ep.event("agent_exited", returncode=proc.returncode, model_turns=len(stream.turns) if stream.turns else None)
        answer = answer_file.read_text() if answer_file.exists() else stream.answer()
        answer = answer.strip()
        if form is not None:
            ep.extra["closing_message"] = answer

        found = isolation.scan("".join(stream.text) + "\n" + stderr + "\n" + answer + "\n" + json.dumps(form), protected)
        contaminated = bool(found["content"]) or (bool(found["paths"]) and not sandboxed)
        ep.extra["isolation"] |= {"contaminated": contaminated, "ground_truth_seen": found["content"], "suspected": found["suspected"],
                                  "protected_paths_mentioned": found["paths"]}  # fmt: skip
        ep.extra["contaminated"], ep.extra["suspected"] = contaminated, found["suspected"]
        if stream.result:
            ep.extra["agent_result"] = {k: stream.result.get(k) for k in ("subtype", "num_turns", "is_error", "total_cost_usd")}

        limited = stream.usage_limit if form is None else None  # from the terminal event only; a run that submitted is never reclassified
        if limited:
            ep.extra["usage_limit"] = limited
        if mismatch:
            ep.end("harness_error", None, detail="the CLI did not start under the controlled configuration: " + "; ".join(mismatch))
        elif limited:
            ep.end("agent_usage_limit", None, detail=f"the agent's subscription reached a usage limit: {limited['message']}")
        elif ep.stop:
            accepted = (form if form is not None else answer) if in_time and limits.final_chance else None
            if limits.final_chance:
                ep.final_chance = {"reason": ep.stop, "request": None, "answer": accepted or None, "late": not in_time,
                                   "via": "further API calls are refused with a request for the answer; the agent is not otherwise told",
                                   "model_turns_after_limit": len(stream.turns) - turns_at_limit if stream.turns else None}  # fmt: skip
            ep.end(ep.stop, accepted or None, detail=ep.stop)
        elif form is not None:
            ep.end("submitted", form, detail="submit")
        elif stream.context_full:
            ep.end("context_limit", answer or None, detail="the agent reported its context window full")
        elif stream.result and stream.result.get("subtype") == "error_max_turns":
            ep.end("model_request_limit", answer or None, detail="error_max_turns")
        elif proc.returncode == 0:
            ep.end("stopped_without_submit" if answers.has_form(task) else "submitted", answer, detail="exit")
        else:
            ep.end("harness_error", answer, detail=f"exit_{proc.returncode}")

    if proc is not None and proc.poll() is None:
        _stop(proc)
    return ep.close(app.state.world if app is not None else None)
