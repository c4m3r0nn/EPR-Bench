"""Run an agent CLI (Claude Code, Codex) on a task through the web UI, from screenshots alone.

The browser route for agent CLIs, the counterpart of cli_agent's JSON API route: the same logged-in subscription CLIs,
no API key, and so a paired API-versus-UI comparison at no API cost. Like cli_agent it measures the agent PRODUCT as
shipped (its own system prompt, loop and context handling), not the bare model the SDK browser harnesses measure;
the two must be reported separately.

How it is put together:

- The EPR is served in UI-only mode (no JSON endpoint exists) on a local port that serves only a client carrying a
  per-episode secret cookie, which only the harness's own Chromium page holds. Anything else, the agent included, is
  refused with 403 before reaching the application, uncharged, and recorded (`isolation.server_requests_refused`).
- The harness opens the record in headless Chromium at the SDK browser harnesses' viewport and drives it with the same
  executor (claude_browser.Screen); what each screenshot shows is recorded as read the same way (ScreenReads).
- The agent CLI's only tools come from one MCP server (stdio), named "computer", whose tools are exactly Screen's
  actions (TOOLS): screenshot, zoom, the clicks, move, drag, scroll, type, key, wait. A screenshot is returned as MCP
  image content. The server is a relay of a few lines of standard-library Python (RELAY), written to the episode's
  private scratch directory and run outside the package, that passes each JSON-RPC message between the CLI and the
  harness over a Unix socket; the harness answers every message itself, in the thread that owns the browser. So the
  tools are the same for every CLI, the browser stays in the harness's process, every MCP message and result is in
  the transcript, and nothing in the page (DOM, text, script, network) is ever given to the agent: only pixels.
- Each CLI is restricted to those tools as far as it can be (PRESETS; `limits_enforced` and `restriction` in the
  record say what each cannot be held to). Everything the agent's own output shows is scanned as on the API route,
  and the agent runs in the same sandbox (eprbench.isolation).

Lifecycle, as on every route: each mouse or keyboard action is charged against max_actions before it runs;
screenshots and zooms are not actions but are counted and capped (max_screenshots). After any limit nothing more is
carried out; with a final chance the next tool result says so and asks for the answer, and for a task with an answer
form the browser stays on (or is put on) the "Handover / reply" page, keeping a reply being filled in, and can load
nothing else, with a screenshot of it; up to answers.FINAL_CHANCE_ACTIONS mouse and keyboard actions and, apart,
answers.FINAL_CHANCE_SCREENSHOTS screenshots are carried out there, within final_chance_seconds. Arguments that repeat
or last are bounded (claude_browser.BOUNDS), and a watchdog stops the agent at the deadline whatever the browser loop
is doing. Both routes of a CLI share one controlled configuration (cli_agent.CLAUDE_COMMON, CODEX_COMMON,
CONTROLLED_ENV), and each episode records the tools and instruction sources the CLI reported. Submitting the reply
page (two steps: check, then confirm) ends the episode. A task without a form takes the CLI's final message as the
answer, exactly as on the API route. A screenshot's reads count once its tool result has been written to the agent
program (the relay confirms it); none count after the answer has been submitted.
"""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from eprbench import answers, isolation
from eprbench.agents import SYSTEM_PROMPT
from eprbench.agents.claude_browser import BOUNDS, UI_COMPARABILITY, VIEWPORT, FinalChance, Screen, ScreenReads, reply_page_only, ui_note
from eprbench.agents.cli_agent import (CLAUDE_COMMON, CODEX_COMMON, EFFORT_FLAGS, MODEL_FLAGS, Stream, _pump, _stop, agent_config, agent_environment,
                                       cli_executable, codex_preflight, inventory, inventory_problems, refuse_instruction_files, refuse_preflight)
from eprbench.episode import SCREENSHOT_ACTIONS, Episode, Limits, argv_secrets
from eprbench.server.local import serve_task
from eprbench.tasks import Task, load_task

SERVER = "computer"  # the MCP server's name, as the agent sees it (Claude Code: mcp__computer__left_click)
COOKIE = "epr_session"
SUBMIT_GRACE_SECONDS = 30.0  # how long an agent may take to exit after its answer has been accepted (never past the deadline)
WATCHDOG_GRACE_SECONDS = 5.0  # past the deadline and final chance, before the watchdog stops the agent whatever the harness is doing

_POINT = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2, "description": "[x, y] in screen pixels"}
_HOLD = {"type": "string", "description": 'modifier keys held during the action, e.g. "shift" or "ctrl+shift"'}
_CLICK = {"properties": {"coordinate": _POINT | {"description": "[x, y] in screen pixels (default: where the pointer is)"}, "text": _HOLD}}


def _tool(name: str, description: str, properties: dict | None = None, required=()) -> dict:
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required)}}


SIZE = f"{VIEWPORT['width']}x{VIEWPORT['height']}"
# Exactly the actions claude_browser.Screen carries out, under its names and with its arguments.
TOOLS = [
    _tool("screenshot", f"Take a screenshot of the screen ({SIZE} pixels). This is the only way to see the screen."),
    _tool("zoom", "A screenshot of one region of the screen at its own size, to read small text.",
          {"region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4, "description": "[x0, y0, x1, y1] in screen pixels"}}, ["region"]),
    _tool("left_click", "Click the left mouse button.", **_CLICK),
    _tool("right_click", "Click the right mouse button.", **_CLICK),
    _tool("middle_click", "Click the middle mouse button.", **_CLICK),
    _tool("double_click", "Double-click the left mouse button.", **_CLICK),
    _tool("triple_click", "Triple-click the left mouse button.", **_CLICK),
    _tool("mouse_move", "Move the pointer.", {"coordinate": _POINT}, ["coordinate"]),
    _tool("left_mouse_down", "Press the left mouse button and keep it pressed."),
    _tool("left_mouse_up", "Release the left mouse button."),
    _tool("left_click_drag", "Press the left mouse button at start_coordinate, move to coordinate and release it.",
          {"start_coordinate": _POINT, "coordinate": _POINT, "text": _HOLD}, ["start_coordinate", "coordinate"]),
    _tool("scroll", f"Turn the mouse wheel, with the pointer at coordinate (at most {BOUNDS['scroll_amount']} clicks a call; more is cut to that).",
          {"coordinate": _POINT, "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
           "scroll_amount": {"type": "integer", "minimum": 1, "maximum": BOUNDS["scroll_amount"],
                             "description": f"wheel clicks, 100 pixels each (default 3, at most {BOUNDS['scroll_amount']})"}, "text": _HOLD}, ["scroll_direction"]),
    _tool("type", f"Type text with the keyboard (at most {BOUNDS['type_chars']} characters a call).",
          {"text": {"type": "string", "maxLength": BOUNDS["type_chars"]}}, ["text"]),
    _tool("key", f'Press a key or key combination, e.g. "Return", "Tab", "ctrl+a", "Page_Down" (at most {BOUNDS["repeat"]} times a call).',
          {"text": {"type": "string"}, "repeat": {"type": "integer", "minimum": 1, "maximum": BOUNDS["repeat"],
                                                  "description": f"times to press it (default 1, at most {BOUNDS['repeat']})"}}, ["text"]),
    _tool("hold_key", f"Hold a key down for a few seconds (at most {BOUNDS['hold_seconds']:g}).",
          {"text": {"type": "string"}, "duration": {"type": "number", "minimum": 0, "maximum": BOUNDS["hold_seconds"], "description": "seconds"}},
          ["text", "duration"]),
    _tool("wait", f"Pause for up to {BOUNDS['wait_seconds']:g} second of real time. Time in the record does not pass: the record has its own Wait control for that.",
          {"duration": {"type": "number", "minimum": 0, "maximum": BOUNDS["wait_seconds"], "description": f"seconds (at most {BOUNDS['wait_seconds']:g})"}}),
    _tool("cursor_position", "Report where the pointer is."),
]  # fmt: skip
TOOL_NAMES = [t["name"] for t in TOOLS]

# The MCP server the CLI launches: a relay of JSON-RPC lines between its stdin/stdout and the harness's Unix socket.
# After writing each reply to the CLI it tells the harness so ("delivered"): that is when a screenshot reached the agent.
RELAY = '''import socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
r, w, out = s.makefile("rb"), s.makefile("wb"), sys.stdout.buffer
for line in sys.stdin.buffer:
    if not line.strip():
        continue
    w.write(line.rstrip(b"\\r\\n") + b"\\n")
    w.flush()
    reply = r.readline()
    if not reply:
        break
    if reply.strip():
        out.write(reply)
        out.flush()
    w.write(b"delivered\\n")
    w.flush()
'''

UI_CLI_NOTE = """

You can ONLY interact with the record through the computer tools you have been given: screenshots of the screen \
({size} pixels) and its mouse and keyboard, with coordinates in screen pixels. Do not read local files or use \
anything else."""

# argv templates, as in cli_agent, and under the SAME controlled configuration as cli_agent's API presets (its
# CLAUDE_COMMON, CODEX_COMMON and CONTROLLED_ENV): only the tool, MCP and sandbox options differ. {mcp_config} is Claude
# Code's MCP configuration (JSON); {mcp_overrides} Codex's -c options defining the same server.
PRESETS = {
    "claude": ["claude", *CLAUDE_COMMON, "--tools", "", "--mcp-config", "{mcp_config}", "--allowedTools", ",".join(f"mcp__{SERVER}__{n}" for n in TOOL_NAMES)],
    "codex": ["codex", *CODEX_COMMON[: CODEX_COMMON.index("-c")], "--disable", "shell_tool", "--disable", "unified_exec", *CODEX_COMMON[CODEX_COMMON.index("-c"):],
              "{mcp_overrides}", "--sandbox", "read-only", "-o", "{answer_file}", "{prompt}"],
}  # fmt: skip

# What each CLI can still do besides the computer tools, as verified against its help and the tools it actually starts
# with (Claude Code's init event; Codex's `codex debug prompt-input`), with no model request made: docs/ARCHITECTURE.md,
# "Agent CLIs through the UI". Every episode records what the CLI itself reported (agent_inventory).
RESTRICTION = {
    "claude": ("enforced by the CLI: --tools '' (no built-in tool at all), --strict-mcp-config with only the computer server, --allowedTools "
               "naming its tools and --permission-prompts none (anything else that would ask is denied). Its init event must list exactly "
               "the computer tools, no other MCP server, no skills and no memory paths, or the run is stopped as a harness error. The "
               "configuration is the API route's (cli_agent.CLAUDE_COMMON, CONTROLLED_ENV); its built-in plugins load on both routes."),
    "codex": ("shell and unified exec are switched off here, and everything cli_agent.CODEX_COMMON switches off on both routes. Codex cannot be "
              "held to the computer tools alone: it keeps its JavaScript 'exec' tool (a V8 isolate with no file system or network, which "
              "can only call enabled tools), apply_patch, MCP resource listing, a clock, asking the user (nobody answers in exec mode) and "
              "its collaboration tools (sub-agents reach the same computer server). None of these can read the record: a patch writes only "
              "where the sandbox allows (never the repository), and the EPR server refuses any client without the harness's cookie "
              "(recorded as a bypass attempt). Each episode records the tools Codex rendered for these flags before the run."),
}  # fmt: skip


def preset_argv(agent: str, sandboxed: bool, model: str | None, effort: str | None) -> list[str]:
    """A preset with the model and effort pinned where given. Under our sandbox Codex cannot start its own (macOS
    sandboxes do not nest; it would also sandbox the MCP server), so it is told not to: with no shell it has nothing to run."""
    argv = list(PRESETS[agent])
    argv[0] = cli_executable(agent, argv[0])
    if sandboxed and agent == "codex":
        argv[argv.index("--sandbox") + 1] = "danger-full-access"
    extra = (MODEL_FLAGS[agent](model) if model else []) + (EFFORT_FLAGS[agent](effort) if effort else [])
    at = argv.index("{prompt}") if agent == "codex" else len(argv)
    return argv[:at] + extra + argv[at:]


def relay_command(scratch: Path, socket_path: str) -> list[str]:
    """How the CLI starts the MCP server: the base interpreter (not the project's virtualenv, which the sandbox closes),
    isolated from site-packages and the environment, running RELAY from the episode's scratch directory."""
    script = scratch / "relay.py"
    script.write_text(RELAY)
    return [os.path.realpath(sys.executable), "-I", "-S", str(script), socket_path]


def mcp_settings(command: list[str]) -> dict:
    """The placeholders that give each CLI the one MCP server."""
    config = {"mcpServers": {SERVER: {"type": "stdio", "command": command[0], "args": command[1:]}}}
    overrides = ["-c", f"mcp_servers.{SERVER}.command={json.dumps(command[0])}", "-c", f"mcp_servers.{SERVER}.args={json.dumps(command[1:])}",
                 "-c", f"mcp_servers.{SERVER}.required=true", "-c", f'mcp_servers.{SERVER}.default_tools_approval_mode="approve"']  # fmt: skip
    return {"{mcp_config}": json.dumps(config), "{mcp_overrides}": overrides}


FINAL_CHANCE_TEXT = ("after any limit nothing more is carried out; the next tool result says so and asks for the answer. For a task with an answer "
                     "form the browser stays on (or is put on) the reply page, keeping a reply being filled in, with up to "
                     f"{answers.FINAL_CHANCE_ACTIONS} mouse and keyboard actions and {answers.FINAL_CHANCE_SCREENSHOTS} screenshots there; the API "
                     "route's equal is its submit endpoint, which stays open (up to answers.MAX_ATTEMPTS submissions, never charged) while every "
                     "other operation is refused. Either way the agent has final_chance_seconds.")  # fmt: skip


def limits_enforced(agent, final_chance: bool = True) -> dict:
    requests = {
        "claude": "Claude Code's --max-turns, checked against the turns counted from its stream-json events; it ends without a final chance",
        "codex": "not enforced or counted: codex exec has no turn limit and its --json events do not show each model request",
    }.get(agent if isinstance(agent, str) else "", "not enforced: counted, and stopped when over, only if the command prints Claude Code's stream-json events")
    return {"max_requests": agent == "claude", "max_actions": True, "max_screenshots": True, "timeout": True,
            "final_chance_after": ["action_limit", "timeout"] if final_chance else [],
            "notes": {"max_requests": requests, "max_actions": "charged by the harness before each mouse or keyboard action; repeats, scroll amounts, "
                      "durations and typed text are bounded (claude_browser.BOUNDS) and cut short at the deadline",
                      "max_screenshots": "screenshots and zooms, counted and capped by the harness",
                      "timeout": "measured from when the agent starts; a watchdog stops the agent's processes at the deadline (and its final chance) "
                      "even while a browser action is running", "final_chance": FINAL_CHANCE_TEXT},
            "restriction": RESTRICTION.get(agent if isinstance(agent, str) else "", "a custom command: whatever it allows itself")}  # fmt: skip


class Bridge:
    """The harness's end of the relay: a Unix socket in a private directory. Each message from the agent's MCP client
    is handed to the thread that owns the browser (`inbox`: ("message", line, box)), which puts (reply, content) in the
    box; when the relay says the reply has been written to the agent, ("delivered", content, None) follows.

    Every connection and every box still waiting is tracked: `close` (or a connection's peer going away) releases each
    waiting worker, shuts the sockets down and joins the workers, so none is left blocked. A box holds one reply and is
    only ever filled without blocking (`answer`): whoever comes second (the harness, or close() releasing it, or close()
    again for the same message still in the inbox) finds it full and leaves it, so close() cannot block, in any order."""

    WAIT = 0.2  # how often a waiting worker looks whether the bridge has closed

    def __init__(self, path: str):
        self.path, self.inbox, self.closed = path, queue.Queue(), False
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(4)
        self.connections: set = set()
        self.waiting: set = set()
        self.workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            with self._lock:
                if self.closed:
                    conn.close()
                    return
                self.connections.add(conn)
                self.workers.append(worker)
            worker.start()

    def _answer(self, box: queue.Queue):
        """The reply put in `box`, or (None, None) once the bridge has closed."""
        while True:
            try:
                return box.get(timeout=self.WAIT)
            except queue.Empty:
                if self.closed:
                    return None, None

    def _serve(self, conn):
        try:
            with conn.makefile("rb") as r, conn.makefile("wb") as w:
                for line in r:
                    box = queue.Queue(1)
                    with self._lock:
                        if self.closed:
                            return
                        self.waiting.add(box)
                    self.inbox.put(("message", line, box))
                    reply, content = self._answer(box)
                    with self._lock:
                        self.waiting.discard(box)
                    if reply is None or self.closed:
                        return
                    w.write(reply + b"\n")
                    w.flush()
                    if r.readline().strip() == b"delivered":
                        self.inbox.put(("delivered", content, None))
        except (OSError, ValueError):
            return
        finally:
            with self._lock:
                self.connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def answer(box: queue.Queue, reply) -> bool:
        """Put `reply` in a worker's box, never blocking: False if the box already holds a reply (the first one stands)."""
        try:
            box.put_nowait(reply)
            return True
        except queue.Full:
            return False

    def close(self, join: float = 5.0):
        """Release every waiting worker, shut every connection down and join the workers (within `join` seconds). Never
        blocks on a box, whatever the harness and the workers are doing meanwhile."""
        with self._lock:
            self.closed = True
            waiting, connections = list(self.waiting), list(self.connections)
        for box in waiting:
            self.answer(box, (None, None))
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.server.close()
        except OSError:
            pass
        while True:  # messages nobody took: nobody will answer them
            try:
                kind, _, box = self.inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "message":
                self.answer(box, (None, None))
        deadline = time.monotonic() + join
        for worker in [self._acceptor, *self.workers]:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def alive(self) -> list[threading.Thread]:
        return [w for w in self.workers if w.is_alive()]


def static_bridge(path: str) -> Bridge:
    """A bridge answering only initialize and tools/list (for Codex's preflight rendering, which lists the tools)."""
    bridge = Bridge(path)

    def serve():
        while not bridge.closed:
            try:
                kind, payload, box = bridge.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind != "message":
                continue
            try:
                msg = json.loads(payload)
            except ValueError:
                bridge.answer(box, (b"", None))
                continue
            rid, method = msg.get("id"), msg.get("method")
            if rid is None:
                bridge.answer(box, (b"", None))
                continue
            if method == "initialize":
                result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": SERVER, "version": "1.0"}}  # fmt: skip
                body = {"result": result}
            elif method == "tools/list":
                body = {"result": {"tools": TOOLS}}
            else:
                body = {"error": {"code": -32601, "message": "not available before the run"}}
            bridge.answer(box, (json.dumps({"jsonrpc": "2.0", "id": rid, **body}).encode(), None))

    threading.Thread(target=serve, daemon=True).start()
    return bridge


def mcp_content(blocks: list[dict]) -> list[dict]:
    """Screen's tool_result content as MCP content."""
    out = []
    for b in blocks:
        if b.get("type") == "image":
            out.append({"type": "image", "data": b["source"]["data"], "mimeType": b["source"]["media_type"]})
        else:
            out.append({"type": "text", "text": b["text"]})
    return out


class Computer:
    """Answers the agent's MCP messages against the harness's browser page, under the episode's limits."""

    def __init__(self, ep: Episode, task: Task, app, page, base_url: str, screen: Screen, reads: ScreenReads):
        self.ep, self.task, self.app, self.page, self.base_url, self.screen, self.reads = ep, task, app, page, base_url, screen, reads
        self.form = answers.has_form(task)
        self.limit_at: float | None = None  # when the first limit was reached
        self.noted = False  # the final chance's request has been delivered in a tool result
        self.allowance = FinalChance()  # the final chance's own allowances, as on the SDK browser harnesses
        self.blocked: list = []
        self.calls = 0
        self.stack = ExitStack()

    def window_open(self) -> bool:
        return self.limit_at is not None and self.ep.limits.final_chance and time.monotonic() - self.limit_at < self.ep.limits.final_chance_seconds

    def limit_reached(self) -> None:
        """Called once a limit is reached: the final chance starts, on the reply page for a task with an answer form."""
        if self.limit_at is None:
            self.limit_at = time.monotonic()
            self.ep.open_final_chance()  # actions on the reply page now run to the final chance's own end, not the deadline
            if self.ep.limits.final_chance and self.form:
                self.blocked = self.stack.enter_context(reply_page_only(self.page, self.base_url))

    def handle(self, raw: bytes) -> tuple[dict | None, list | None]:
        """One JSON-RPC message: (the reply, or None for a notification; Screen-shaped content of a tools/call)."""
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("not a single JSON-RPC message")
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            self.ep.event("mcp", request=raw.decode(errors="replace"), reply=reply)
            return reply, None
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        content, result, error = None, None, None
        if method == "tools/call" and rid is not None:
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            content, is_error = self.call(str(params.get("name")), args)
            result = {"content": mcp_content(content), "isError": is_error}
        elif method == "initialize":
            version = params.get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER, "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "ping":
            result = {}
        elif rid is not None:
            error = {"code": -32601, "message": f"method not found: {method}"}
        reply = None if rid is None else {"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result})}
        logged = None if reply is None else {**reply, "result": {**result, "content": content}} if content is not None else reply
        self.ep.event("mcp", request=msg, reply=logged)  # every message, its images saved once by content (Episode.jsonable)
        return reply, content

    def _run(self, name: str, args: dict) -> list[dict]:
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown computer action: {name}")
        return self.screen.run(name, args)

    def call(self, name: str, args: dict) -> tuple[list[dict], bool]:
        """One tool call: charged before it is carried out; refused, or the final chance, after a limit."""
        ep = self.ep
        self.calls += 1
        if self.app.state.submission is not None:
            return [{"type": "text", "text": "Not executed: your answer has been submitted and the task is over. Stop now."}], True
        n = ep.try_charge(name) if ep.stop is None else 0
        if n:
            try:
                content = self._run(name, args)
                ep.action(n, name, args, content, interface_call="mcp", output_mark=ep.output_mark)
                return content, False
            except Exception as e:  # reported to the agent, as on the SDK harnesses
                error = f"{type(e).__name__}: {e}"
                ep.action(n, name, args, error=error, interface_call="mcp", output_mark=ep.output_mark)
                return [{"type": "text", "text": error}], True
        self.limit_reached()
        if not self.window_open():
            return [{"type": "text", "text": "Not executed: a limit was reached." + (" The final chance is over." if ep.limits.final_chance else "")}], True
        if not self.noted:  # the first refused call carries the request for the answer (and the reply page as it now is)
            self.noted = True
            content = [{"type": "text", "text": f"Not executed. {ep.note(form=self.form)}"}]
            if self.form:
                try:
                    content += self.screen._image()
                    ep.automatic_screenshot()
                except Exception as e:  # the request still goes out, without the picture
                    ep.event("error", error=f"the final chance's screenshot failed: {type(e).__name__}: {e}")
            ep.event("final_chance_requested", name=name, args=args, result=content)
            return content, False
        if not self.form or not self.allowance.take(name):
            return [{"type": "text", "text": "Not executed: the final chance is over." if self.form else f"Not executed. {ep.note()}"}], True
        try:  # not charged against the limits: counted and recorded apart
            content = self._run(name, args)
            ep.event("action", action=None, final_chance=True, name=name, args=args, result=content)
            return content, False
        except Exception as e:
            ep.event("action", action=None, final_chance=True, name=name, args=args, error=f"{type(e).__name__}: {e}")
            return [{"type": "text", "text": f"{type(e).__name__}: {e}"}], True

    def delivered(self, content: list | None) -> None:
        """A tool result has been written to the agent: what its screenshots showed is read (not after the answer)."""
        if content and self.app.state.submission is None and any(b.get("type") == "image" for b in content):
            n = self.reads.delivered({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "mcp", "content": content}]}]})
            if n:
                self.ep.event("screenshot_reads", request=None, entries=n)


class Watchdog:
    """Stops the agent's whole process group at the episode's last moment (the deadline, plus the final chance when
    there is one, plus `grace`), from its own thread, whatever the browser loop is doing then."""

    def __init__(self, ep: Episode, proc, grace: float | None = None):
        self.ep, self.proc, self.fired = ep, proc, False
        limits = ep.limits
        self.at = limits.timeout_seconds + (limits.final_chance_seconds if limits.final_chance else 0) + (WATCHDOG_GRACE_SECONDS if grace is None else grace)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self):
        while not self.done.wait(0.1):
            if self.ep.elapsed() >= self.at and self.proc.poll() is None:
                self.fired = True
                self.ep.event("watchdog", seconds=round(self.ep.elapsed(), 1), detail="the agent was stopped at the episode's deadline")
                _stop(self.proc)
                return

    def cancel(self):
        self.done.set()


def _private_dir() -> str:
    """A fresh private directory with a short path (a Unix socket's path is limited to about 100 bytes)."""
    return tempfile.mkdtemp(prefix="epr", dir="/tmp" if os.path.isdir("/tmp") else None)


def run_episode(task: Task | str, agent: str | list[str], limits: Limits | None = None, timeout: float | None = None, out_dir=None,
                repeat: int = 0, isolate: bool = True, protect=(), allow_unsandboxed: bool = False, model: str | None = None,
                effort: str | None = None, pass_credentials: bool = False, headless: bool = True) -> dict:  # fmt: skip
    """Play one episode through the web UI. `agent` is a preset name ("claude", "codex") or an argv template, which may
    use {prompt}, {answer_file}, {max_requests}, {debug_file}, {mcp_config} (Claude Code's --mcp-config JSON),
    {mcp_overrides} (Codex's -c options) and {mcp_command} (the relay's command line, as several arguments). The rest is
    as in cli_agent.run_episode."""
    from playwright.sync_api import sync_playwright

    task = load_task(task) if isinstance(task, str) else task
    limits = limits or Limits()
    if timeout is not None:
        limits = Limits(**{**limits.__dict__, "timeout_seconds": timeout})
    name = agent if isinstance(agent, str) else Path(agent[0]).name
    env, auth = agent_environment(pass_credentials, agent)
    config = agent_config(agent, PRESETS, model, effort) | {
        "tools": f"only the MCP server '{SERVER}': " + ", ".join(TOOL_NAMES),
        "restriction": RESTRICTION.get(agent if isinstance(agent, str) else "", "a custom command: whatever it allows itself"),
    }  # fmt: skip
    cookie = (COOKIE, secrets.token_urlsafe(24))
    settings = {"agent": agent, "isolate": isolate, "allow_unsandboxed": allow_unsandboxed, "auth": auth, "agent_config": config, "viewport": VIEWPORT,
                "headless": headless}  # fmt: skip
    ep = Episode(task, harness="cli_browser", interface="ui", agent=name, effort=effort, limits=limits, settings=settings, out_dir=out_dir, repeat=repeat,
                 secrets=argv_secrets(agent if isinstance(agent, list) else PRESETS[agent]) + [("cookie", cookie[1])])  # fmt: skip
    ep.who["model"] = model
    ep.extra["auth"], ep.extra["agent_config"] = auth, config
    ep.extra["limits_enforced"] = limits_enforced(agent, limits.final_chance)
    ep.extra["comparability"] |= UI_COMPARABILITY | {
        "timer_starts": "at agent process launch: the agent's own model requests are not visible to the harness; set-up before launch is setup_seconds",
        "screenshots": "on request, as on the Claude browser harness: the agent sees the screen only when it calls screenshot or zoom (and once, "
                       "unasked, with the final chance of a task with an answer form); a screenshot's reads count once its tool result is written "
                       "to the agent program",
        "tools": f"one MCP server ('{SERVER}', stdio) whose tools are the browser harnesses' computer actions; the same for every agent CLI",
        "model_requests": "counted from Claude Code's stream-json events; not visible for Codex",
        "final_chance": FINAL_CHANCE_TEXT,
        "configuration": "the same controlled configuration as the API route of the same CLI (cli_agent.CLAUDE_COMMON / CODEX_COMMON, "
                         "CONTROLLED_ENV); only the tool, MCP and sandbox options differ",
    }  # fmt: skip
    ep.extra["sandboxed"], ep.extra["bypass_attempted"] = False, False
    protected = isolation.protected_paths([*protect, *([out_dir] if out_dir is not None else [])])
    app, stream, proc, computer, watchdog = None, Stream(), None, None, None

    with ExitStack() as stack:
        stack.enter_context(ep)
        url, app = stack.enter_context(serve_task(task, "ui", ep, cookie=cookie))
        workdir = stack.enter_context(tempfile.TemporaryDirectory())
        private = _private_dir()
        stack.callback(lambda: __import__("shutil").rmtree(private, ignore_errors=True))
        refuse_instruction_files(ep, agent, workdir)
        p = stack.enter_context(sync_playwright())
        browser = p.chromium.launch(headless=headless)
        stack.callback(browser.close)
        context = browser.new_context(viewport=VIEWPORT)
        context.add_cookies([{"name": cookie[0], "value": cookie[1], "url": url}])
        page = context.new_page()
        page.goto(url)
        reads = ScreenReads(page, app)
        screen = Screen(page, on_shot=reads.captured, should_stop=lambda: app.state.submission is not None or ep.acting_too_late())
        computer = Computer(ep, task, app, page, url, screen, reads)
        stack.callback(computer.stack.close)
        bridge = Bridge(os.path.join(private, "s"))
        stack.callback(bridge.close)

        prefix, why_not = isolation.sandbox(protected) if isolate else ([], "isolation switched off")
        ep.extra["sandboxed"] = sandboxed = bool(prefix)
        ep.extra["isolation"] = {"sandbox": sandboxed, "why_not": why_not, "protected": protected}
        if isolate and not sandboxed and not allow_unsandboxed:
            raise RuntimeError(f"the agent cannot be sandboxed here ({why_not}); pass --allow-unsandboxed to run it anyway")
        template = preset_argv(agent, sandboxed, model, effort) if isinstance(agent, str) else agent
        answer_file, debug_file = Path(workdir) / "answer.txt", Path(private) / "claude-debug.log"
        command = relay_command(Path(private), bridge.path)
        prompt = SYSTEM_PROMPT + UI_CLI_NOTE.format(size=f"{VIEWPORT['width']}x{VIEWPORT['height']}") + ui_note(task) + f"\n\nTask: {task.instruction}"

        def filled(relay: list[str]) -> list[str]:
            fill = {"{prompt}": prompt, "{answer_file}": str(answer_file), "{max_requests}": str(limits.max_requests), "{debug_file}": str(debug_file),
                    "{mcp_command}": relay, **mcp_settings(relay)}  # fmt: skip
            out = []
            for part in template:
                value = fill.get(part, part)
                out += value if isinstance(value, list) else [value]
            return out

        preflight = None
        if agent == "codex":  # what Codex would give its model under these flags, rendered with no model request
            probe = static_bridge(os.path.join(private, "p"))
            try:
                preflight = codex_preflight(filled([*command[:-1], probe.path]), workdir, env)
            finally:
                probe.close()
            refuse_preflight(ep, preflight)
        argv = list(prefix) + filled(command)
        ep.event("agent_started", argv=[a if a != prompt else "{prompt}" for a in argv], prompt=prompt, tools=TOOLS, preflight=preflight)
        expected = ({f"mcp__{SERVER}__{n}" for n in TOOL_NAMES}, {SERVER}) if agent == "claude" else None

        logs = Path(private) if ep.dir is None else ep.dir
        ep.start_clock()
        proc = subprocess.Popen(argv, cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True)
        stack.callback(lambda: _stop(proc))
        watchdog = Watchdog(ep, proc)
        stack.callback(watchdog.cancel)
        errors: list[str] = []
        mismatch: list[str] = []
        ep.output_mark = 0

        def read(line: str) -> None:
            first = stream.init is None
            stream.feed(line)
            ep.output_mark += 1
            if first and stream.init is not None and expected is not None:
                mismatch.extend(inventory_problems(stream.init, *expected))

        pumps = [_pump(proc.stdout, logs / "agent.stdout", ep, read), _pump(proc.stderr, logs / "agent.stderr", ep, errors.append)]
        turns_at_limit, in_time, submitted_at = None, True, None
        while proc.poll() is None and not mismatch:
            if ep.stop is None and app.state.submission is None and len(stream.turns) > limits.max_requests:
                ep.stop = "model_request_limit"
            if ep.stop is None and app.state.submission is None and ep.elapsed() >= limits.timeout_seconds:
                ep.stop = "timeout"
            if app.state.submission is not None:  # the answer is in: the agent has a short while to finish, never past the deadline
                submitted_at = submitted_at or time.monotonic()
                if time.monotonic() - submitted_at >= SUBMIT_GRACE_SECONDS or ep.elapsed() >= limits.timeout_seconds:
                    break
            elif ep.stop:
                computer.limit_reached()
                turns_at_limit = len(stream.turns) if turns_at_limit is None else turns_at_limit
                if not computer.window_open():
                    in_time = False
                    break
            try:
                kind, payload, box = bridge.inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "delivered":
                computer.delivered(payload)
                continue
            try:
                reply, content = computer.handle(payload)
            except BaseException:  # the agent is told nothing more; the episode ends as a harness error, with the traceback
                bridge.answer(box, (None, None))
                raise
            bridge.answer(box, (b"" if reply is None else json.dumps(reply).encode(), content))
        while True:  # tool results written before the agent stopped
            try:
                kind, payload, box = bridge.inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "delivered":
                computer.delivered(payload)
            else:
                bridge.answer(box, (None, None))
        watchdog.cancel()
        _stop(proc)
        for pump in pumps:
            pump.join(timeout=10)
        form = app.state.submission["answer"] if app.state.submission is not None else None
        if ep.stop is None and form is None and (ep.elapsed() >= limits.timeout_seconds or watchdog.fired):
            ep.stop = "timeout"  # it ended between two checks of the clock, or the watchdog stopped it
            computer.limit_reached()
            in_time, turns_at_limit = limits.final_chance and not watchdog.fired, len(stream.turns)
        if watchdog.fired:
            in_time = False
        stderr = "".join(errors)
        ep.requests, ep.requests_known = len(stream.turns), bool(stream.turns)
        ep.usage.update(stream.usage)
        config["model"]["observed"] = stream.model
        ep.extra["agent_inventory"] = config["inventory"] = inventory(agent, stream, debug_file, preflight)
        ep.meta_updates["agent_config"] = config
        ep.event("agent_exited", returncode=proc.returncode, model_turns=len(stream.turns) if stream.turns else None, tool_calls=computer.calls)
        answer = (answer_file.read_text() if answer_file.exists() else stream.answer()).strip()
        if form is not None:
            ep.extra["closing_message"] = answer

        refused = list(app.state.recorder.refused)
        found = isolation.scan("".join(stream.text) + "\n" + stderr + "\n" + answer + "\n" + json.dumps(form), protected)
        contaminated = bool(found["content"]) or (bool(found["paths"]) and not sandboxed)
        ep.extra["isolation"] |= {"contaminated": contaminated, "ground_truth_seen": found["content"], "suspected": found["suspected"],
                                  "protected_paths_mentioned": found["paths"], "server_requests_refused": refused}  # fmt: skip
        ep.extra["contaminated"], ep.extra["suspected"], ep.extra["bypass_attempted"] = contaminated, found["suspected"], bool(refused)
        ep.extra["tool_calls"] = computer.calls
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
            accepted = None
            if in_time and limits.final_chance:
                accepted = form if form is not None else (None if computer.form else answer or None)
            if limits.final_chance:
                ep.final_chance = {"reason": ep.stop, "request": None, "answer": accepted, "late": not in_time, "actions": computer.allowance.actions,
                                   "screenshots": computer.allowance.screenshots,
                                   "action_allowance": answers.FINAL_CHANCE_ACTIONS if computer.form else 0,
                                   "screenshot_allowance": answers.FINAL_CHANCE_SCREENSHOTS if computer.form else 0, "blocked_requests": computer.blocked,
                                   "offered": "the reply page only" if computer.form else "a request for the answer in the next tool result",
                                   "requested": computer.noted, "watchdog": watchdog.fired,
                                   "model_turns_after_limit": len(stream.turns) - turns_at_limit if stream.turns and turns_at_limit is not None else None}  # fmt: skip
            ep.end(ep.stop, accepted, detail=ep.stop)
        elif form is not None:
            ep.end("submitted", form, detail="reply page")
        elif stream.context_full:
            ep.end("context_limit", answer or None, detail="the agent reported its context window full")
        elif stream.result and stream.result.get("subtype") == "error_max_turns":
            ep.end("model_request_limit", answer or None, detail="error_max_turns")
        elif proc.returncode == 0:
            ep.end("stopped_without_submit" if computer.form else "submitted", answer, detail="exit")
        else:
            ep.end("harness_error", answer, detail=f"exit_{proc.returncode}")

    if proc is not None and proc.poll() is None:
        _stop(proc)
    return ep.close(app.state.world if app is not None else None)
