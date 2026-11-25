"""Run a Claude model on a task through the web UI, from screenshots alone.

The EPR is served in UI-only mode on a local port and opened in a headless
Chromium page with no address bar, so the model has pixels, a mouse and a
keyboard and nothing else. Requires the `browser` extra (Playwright) and
`playwright install chromium`.

For a task with an answer form the model is told to give its answer on the "Handover / reply" page; the episode
ends as soon as that form is accepted, with the submitted form as the answer. A model that replies in plain text
instead has stopped without submitting. For a task without a form, the closing plain-text reply is the answer, as
before (the reply page is there too, and an answer given on it is taken in the same way).
"""

import base64
import hashlib
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from eprbench import answers, spend
from eprbench.agents import check_metering, NOT_EXECUTED_AFTER_FAILURE, SYSTEM_PROMPT, without_retries
from eprbench.agents.claude_tools import _text, add_user, anthropic_usage, closing
from eprbench.episode import SCREENSHOT_ACTIONS, Episode, Limits
from eprbench.server.local import serve_task
from eprbench.tasks import Task, load_task

# 1280x800 is 46x29 = 1334 visual tokens, well inside the model's screenshot limits, so
# screenshots are sent unscaled and the model's coordinates are page coordinates.
VIEWPORT = {"width": 1280, "height": 800}

UI_NOTE = """

You are at a hospital computer showing the patient record in a web browser. Use the screen, mouse \
and keyboard. Requests and results are in a separate system (ICE), linked from the top of the page. \
The "Wait" control in the header lets time pass, for example until a result is reported. When \
everything you intend to do is done, stop and reply to your colleague in plain text: the answer, or \
a brief account of what you did."""
# For a task with an answer form, the same note ends by sending the model to the reply page instead.
UI_NOTE_FORM = UI_NOTE.removesuffix(
    """When \
everything you intend to do is done, stop and reply to your colleague in plain text: the answer, or \
a brief account of what you did."""
) + """When everything you intend to do is \
done, open "Handover / reply" from the header, fill in the answer form and your reply to your colleague (the \
answer, or a brief account of what you did), and submit it. You can submit once; submitting ends the task."""


def ui_note(task: Task) -> str:
    return UI_NOTE_FORM if answers.has_form(task) else UI_NOTE


@contextmanager
def reply_page_only(page, base_url: str):
    """The final chance of a task with an answer form: the browser is on the reply page and can load nothing else, so
    the model can still give its typed answer (as a tool or API agent can after a limit) and can do nothing more.
    Yields the list of requests refused meanwhile."""
    blocked = []
    already = urlsplit(page.url).path == "/reply"  # a reply being filled in stays as it is: reloading would lose it

    def guard(route):
        path = urlsplit(route.request.url).path
        if path == "/reply" or path.startswith("/reply/"):
            route.continue_()
        else:
            blocked.append(f"{route.request.method} {path}")
            route.fulfill(status=204, body="")  # no content: the browser stays on the reply page

    page.route("**/*", guard)
    try:
        if not already:
            page.goto(base_url + "/reply")
        yield blocked
    finally:
        page.unroute("**/*", guard)


def final_chance_record(ep: Episode, answer, late: bool, actions: int, blocked: list, screenshots: int = 0) -> None:
    ep.final_chance = {"reason": ep.stop, "request": ep.requests, "answer": None if late else answer, "late": late, "actions": actions,
                       "action_allowance": answers.FINAL_CHANCE_ACTIONS, "screenshots": screenshots, "screenshot_allowance": answers.FINAL_CHANCE_SCREENSHOTS,
                       "blocked_requests": blocked, "offered": "the reply page only"}  # fmt: skip


class FinalChance:
    """The final chance's own allowances on the reply page, the same on every browser route: mouse and keyboard actions
    up to answers.FINAL_CHANCE_ACTIONS, and screenshots and zooms apart, up to answers.FINAL_CHANCE_SCREENSHOTS (as
    outside it, a screenshot is never charged as an action). `take(name)` is False once that kind's allowance is used."""

    def __init__(self):
        self.actions = self.screenshots = 0

    def take(self, name: str) -> bool:
        if name in SCREENSHOT_ACTIONS:
            if self.screenshots >= answers.FINAL_CHANCE_SCREENSHOTS:
                return False
            self.screenshots += 1
            return True
        if self.actions >= answers.FINAL_CHANCE_ACTIONS:
            return False
        self.actions += 1
        return True


def answered(ep: Episode, app) -> bool:
    """End the episode with the answer given on the reply page, if one has been accepted."""
    if app is None or app.state.submission is None:
        return False
    ep.end("submitted", app.state.submission["answer"], detail="reply page")
    return True

_KEYS = {
    "return": "Enter", "enter": "Enter", "kp_enter": "Enter", "backspace": "Backspace", "delete": "Delete",
    "tab": "Tab", "escape": "Escape", "esc": "Escape", "space": "Space", "home": "Home", "end": "End",
    "page_up": "PageUp", "pageup": "PageUp", "prior": "PageUp", "page_down": "PageDown", "pagedown": "PageDown", "next": "PageDown",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "pgup": "PageUp", "pgdn": "PageDown",
    "bracketleft": "[", "bracketright": "]", "comma": ",", "period": ".", "minus": "-", "equal": "=", "plus": "+", "slash": "/",
}  # fmt: skip
# Modifiers, by every name an agent uses, as Playwright names them. Never translated: a held Ctrl is Control.
_MODIFIERS = {"ctrl": "Control", "control": "Control", "alt": "Alt", "option": "Alt", "shift": "Shift", "super": "Meta", "cmd": "Meta",
              "command": "Meta", "meta": "Meta", "win": "Meta"}  # fmt: skip
_ORDER = ("Control", "Alt", "Meta", "Shift")

# Key combinations, as a Linux (or Windows) user expects them in a browser. The agent is told nothing of the platform, so
# on every platform a combination must do what it does in Chromium on Linux:
#
# - Translated, on macOS only (elsewhere every combination is pressed as given). Chromium on macOS binds Ctrl to Emacs
#   editing commands (Ctrl+K deletes to the end of the line, Ctrl+A goes to the line's start) and the Linux editing
#   shortcuts to Cmd and Option, so: Ctrl+Left/Right (a word) and Ctrl+Up/Down (a paragraph), Ctrl+Backspace/Delete (a
#   word) become Option+ the same key; Ctrl+Home/End (the document's start, end) become Cmd+Up/Down; Home and End (the
#   line's start, end; on macOS they only scroll) become Cmd+Left/Right; Ctrl+Y (redo) becomes Cmd+Shift+Z; and every
#   other Ctrl+key becomes Cmd+key: select all, copy, cut, paste, undo, Ctrl+Shift+Z redo, and the Ctrl+letters Linux
#   leaves alone (which Cmd also leaves alone). Shift is kept throughout, so the same keys select instead of moving.
# - Not translated, anywhere: modifiers held during a click, drag, scroll or hold_key (Ctrl+click stays Ctrl+click), and
#   combinations already using Cmd/Super or Alt (they are pressed as given).
# - Refused, everywhere (BROWSER_SHORTCUTS): browser shortcuts that would leave the page, reload it, open another tab,
#   window or dialog, or open developer tools. The call fails with a message saying so, and nothing is pressed.
_FUNCTION_REFUSED = {"F1", "F5", "F6", "F7", "F10", "F11", "F12"}
_PRIMARY_REFUSED = set("lntwqrohjupsdke") | {"Tab", "PageUp", "PageDown", "[", "]", ",", *"123456789"}  # with Ctrl (or Cmd)
_PRIMARY_SHIFT_REFUSED = set("ntwqbojicm") | {"Delete"}  # with Ctrl (or Cmd) and Shift
_ALT_REFUSED = {"ArrowLeft", "ArrowRight", "Home", "F4", "d", "e", "f"}  # alone with Alt: back, forward, home page, close, menus
BROWSER_SHORTCUTS = ("F1, F5, F6, F7, F10, F11, F12; Ctrl or Cmd with L N T W Q R O H J U P S D K E, Tab, Page Up/Down, [ ] , or a digit; "
                     "Ctrl or Cmd with Shift and N T W Q B O J I C M or Delete; Cmd+Alt+I/J/C/U; Alt with Left, Right, Home, F4, D, E or F")


def _parse(combo: str) -> tuple[set[str], str]:
    """A combination's modifiers (Playwright names) and its key (Playwright name; a letter in lower case)."""
    parts = [p for p in str(combo).replace(" ", "").split("+") if p] or [str(combo)]
    if str(combo).endswith("++"):  # "ctrl++"
        parts = parts + ["+"]
    mods, keys = set(), []
    for part in parts:
        if part.lower() in _MODIFIERS:
            mods.add(_MODIFIERS[part.lower()])
        else:
            keys.append(_KEYS.get(part.lower(), part))
    if len(keys) > 1:
        raise ValueError(f"one key at a time, with modifiers: {combo!r}")
    key = keys[0] if keys else ""
    if len(key) == 1 and key.isalpha():
        key = key.lower()
    elif len(key) > 1 and key.upper().startswith("F") and key[1:].isdigit():
        key = key.upper()
    return mods, key


def refused(combo: str) -> bool:
    """Whether `combo` is a browser shortcut this harness refuses (BROWSER_SHORTCUTS)."""
    mods, key = _parse(combo)
    primary = bool(mods & {"Control", "Meta"})
    if key in _FUNCTION_REFUSED or (key == "F4" and "Alt" in mods):
        return True
    if primary and "Shift" in mods and key in _PRIMARY_SHIFT_REFUSED:
        return True
    if primary and "Shift" not in mods and "Alt" not in mods and key in _PRIMARY_REFUSED:
        return True
    if primary and "Shift" in mods and key in ("Tab", "r"):
        return True
    if "Meta" in mods and "Alt" in mods and key in ("i", "j", "c", "u"):
        return True
    return mods == {"Alt"} and key in _ALT_REFUSED


def key_combo(combo: str, platform: str | None = None) -> str:
    """The Playwright key combination to press for an agent's `combo` ("ctrl+a", "Return", "shift+Page_Down") on
    `platform` (this one by default), translated so that it does what it does on Linux. Raises ValueError for a refused
    browser shortcut."""
    if refused(combo):
        raise ValueError(f"{combo} is a browser shortcut (it would leave the page, open another window or dialog, or open browser tools): "
                         "not available here")  # fmt: skip
    mods, key = _parse(combo)
    if (platform or sys.platform) == "darwin":
        shift = {"Shift"} & mods
        if mods - shift == {"Control"}:
            if key in ("ArrowLeft", "ArrowRight", "Backspace", "Delete"):
                mods = {"Alt"} | shift
            elif key in ("ArrowUp", "ArrowDown"):  # in Chromium on Linux, Ctrl+Up/Down moves a line, as Up/Down do
                mods = shift
            elif key in ("Home", "End"):
                mods, key = {"Meta"} | shift, {"Home": "ArrowUp", "End": "ArrowDown"}[key]
            elif key == "y" and not shift:
                mods, key = {"Meta", "Shift"}, "z"
            else:
                mods = {"Meta"} | shift
        elif mods <= {"Shift"} and key in ("Home", "End"):
            mods, key = {"Meta"} | shift, {"Home": "ArrowLeft", "End": "ArrowRight"}[key]
    return "+".join([m for m in _ORDER if m in mods] + ([key] if key else []))


def held_keys(text: str | None) -> list[str]:
    """The keys to hold down for a click, drag, scroll or hold_key: as named, never translated (Ctrl is Control)."""
    if not text:
        return []
    return [_MODIFIERS.get(p.lower()) or _KEYS.get(p.lower(), p) for p in str(text).replace(" ", "").split("+") if p]


# The two browser harnesses see the screen differently: this one's model asks for each screenshot, the OpenAI one's
# is sent one after every call. So a screenshot or zoom requested is not an action on either: the action limit applies
# to mouse and keyboard actions alone, and screenshot requests are counted and capped apart (episode.ACCOUNTING).
SCREENSHOT_POLICY = ("on request: the model sees the screen only when it asks (and once, unasked, with the final chance); a screenshot or zoom "
                     "requested is a screenshot request, not an action")  # fmt: skip
UI_COMPARABILITY = {
    "action_unit": "one mouse or keyboard action; screenshots and zooms requested are not actions (counted and capped apart: max_screenshots)",
    "accounting": "actions, screenshot_requests, automatic_screenshots and model_requests (episode.ACCOUNTING), the same four on both browser harnesses",
    "batch_stops_on": "the first action that cannot be performed; an EPR error shown on the page does not stop the turn",
}


def to_playwright_key(combo: str) -> str:
    """Translate xdotool-style names ("ctrl+a", "Return", "Page_Down") to the Playwright combination to press here."""
    return key_combo(combo)


# Which tagged parts of the page (data-read, server/ui.py) a screenshot shows: inside the viewport (or a zoom's region),
# not hidden, and at least half of the part, or of the region, on screen. Run at the moment the screenshot is taken.
SEEN_JS = """(clip) => {
  const R = clip ? {x0: clip.x, y0: clip.y, x1: clip.x + clip.width, y1: clip.y + clip.height} : {x0: 0, y0: 0, x1: innerWidth, y1: innerHeight};
  const region = (R.x1 - R.x0) * (R.y1 - R.y0), seen = new Set();
  for (const el of document.querySelectorAll("[data-read]")) {
    const s = getComputedStyle(el);
    if (s.visibility === "hidden" || s.display === "none" || Number(s.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const w = Math.max(0, Math.min(r.right, R.x1) - Math.max(r.left, R.x0)), h = Math.max(0, Math.min(r.bottom, R.y1) - Math.max(r.top, R.y0));
    if (w * h > 0 && (w * h >= 0.5 * r.width * r.height || w * h >= 0.5 * region)) seen.add(el.dataset.read);
  }
  return {render: document.body.dataset.render || null, tags: [...seen]};
}"""


class ScreenReads:
    """What each screenshot shows, counted as read only once the screenshot reaches the model.

    At capture (`captured`, Screen's on_shot) the tagged parts of the page in view are noted against the image; when a
    request carrying that image comes back from the model (`delivered`, the episode's on_delivered, given the request's
    parameters), they are reported to the EPR server, in this process (nothing in the browser can reach it), and
    recorded as read with an action of their own. A screenshot taken in the same turn as the actions that end the
    episode, or never sent, earns nothing."""

    def __init__(self, page, app):
        self.page, self.app = page, app
        self.pending: dict[str, dict] = {}

    def captured(self, png: bytes, clip=None) -> None:
        try:
            found = self.page.evaluate(SEEN_JS, clip)
        except Exception:  # a page between loads: it showed nothing that can be named
            return
        self.pending[hashlib.sha256(png).hexdigest()] = found

    def delivered(self, params: dict) -> int:
        record = getattr(getattr(self.app, "state", None), "record_seen", None)
        if record is None:
            return 0
        n = 0
        for data in spend.images_in(params):
            found = self.pending.pop(hashlib.sha256(base64.b64decode(data)).hexdigest(), None)
            if found:
                n += len(record(found.get("render"), found.get("tags") or []))
        return n


# Every argument that repeats or lasts is bounded, so no single call (one action, charged once) can run on, on every
# browser route (the OpenAI harness's pixel scrolls included: Screen.wheel). A scroll beyond its bound is cut to it and
# the agent told (scrolling less than asked is harmless and shows in the next screenshot); a larger repeat, longer text
# or longer hold or wait is refused as malformed (charged like any malformed call) with a message stating the limit.
# The agent-CLI route advertises every bound in its MCP tool schemas (cli_browser.TOOLS); the SDK harnesses use their
# providers' own computer tools, whose formats take no argument schema, so there the limit is learnt from that message.
# Repeats are checked against `should_stop` (the deadline, or the answer having been given) between presses and chunks.
BOUNDS = {"repeat": 10, "scroll_amount": 10, "type_chars": 2000, "hold_seconds": 2.0, "wait_seconds": 1.0}
TYPE_CHUNK = 100


class Stopped(RuntimeError):
    """An action cut short because the episode's deadline passed or its answer was given."""


class Screen:
    """Executes computer-toolset member calls against a Playwright page. `on_shot(png, clip)` is called after every
    screenshot taken to be sent to the model (ScreenReads.captured, in the harnesses); `should_stop()`, if given, is
    asked between the parts of a repeated action (BOUNDS)."""

    def __init__(self, page, on_shot=None, should_stop=None):
        self.page = page
        self.cursor = (0, 0)
        self.on_shot = on_shot
        self.should_stop = should_stop

    def _check(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise Stopped("not finished: the episode's time is up or its answer has been given")

    @staticmethod
    def _bounded(args: dict, key: str, default, limit, cast=int):
        value = cast(args.get(key, default) if args.get(key) is not None else default)
        if not 0 <= value <= limit:
            raise ValueError(f"{key} must be at most {limit:g} (and not negative), not {value:g}")
        return value

    def wheel(self, at, dx: float, dy: float) -> str | None:
        """Turn the wheel by (dx, dy) pixels with the pointer at `at`, each direction cut to the most one call may
        scroll (BOUNDS: scroll_amount clicks of 100 pixels). Scrolling less than asked is harmless and shows in the next
        screenshot, so it is not refused. Returns what was cut, to tell the agent, or None."""
        most = 100 * BOUNDS["scroll_amount"]
        if at is not None:
            self.cursor = tuple(at)
            self.page.mouse.move(*self.cursor)
        cut = [max(-most, min(most, v)) for v in (dx, dy)]
        self.page.mouse.wheel(*cut)
        return None if cut == [dx, dy] else f"{most} pixels is the most one scroll turns: scrolled ({cut[0]:g}, {cut[1]:g}), not ({dx:g}, {dy:g})"

    def shot(self, clip=None) -> bytes:
        png = self.page.screenshot(clip=clip)
        if self.on_shot is not None:
            self.on_shot(png, clip)
        return png

    def _image(self, clip=None) -> list[dict]:
        png = self.shot(clip)
        data = base64.standard_b64encode(png).decode()
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]

    @contextmanager
    def _holding(self, modifiers: str | None):
        keys = held_keys(modifiers)
        for k in keys:
            self.page.keyboard.down(k)
        try:
            yield
        finally:
            for k in reversed(keys):
                self.page.keyboard.up(k)

    def _click(self, args: dict, button: str = "left", count: int = 1):
        if args.get("coordinate") is not None:
            self.cursor = tuple(args["coordinate"])
        with self._holding(args.get("text")):
            self.page.mouse.click(*self.cursor, button=button, click_count=count)

    def run(self, name: str, args: dict) -> list[dict]:
        """Perform one action and return tool_result content."""
        page = self.page
        match name:
            case "screenshot":
                return self._image()
            case "zoom":
                x0, y0, x1, y1 = args["region"]
                return self._image({"x": x0, "y": y0, "width": max(1, x1 - x0), "height": max(1, y1 - y0)})
            case "left_click":
                self._click(args)
            case "right_click":
                self._click(args, button="right")
            case "middle_click":
                self._click(args, button="middle")
            case "double_click":
                self._click(args, count=2)
            case "triple_click":
                self._click(args, count=3)
            case "mouse_move":
                self.cursor = tuple(args["coordinate"])
                page.mouse.move(*self.cursor)
            case "left_mouse_down":
                page.mouse.down()
            case "left_mouse_up":
                page.mouse.up()
            case "left_click_drag":
                with self._holding(args.get("text")):
                    page.mouse.move(*args["start_coordinate"])
                    page.mouse.down()
                    page.mouse.move(*args["coordinate"])
                    page.mouse.up()
                self.cursor = tuple(args["coordinate"])
            case "cursor_position":
                return [{"type": "text", "text": f"[{self.cursor[0]}, {self.cursor[1]}]"}]
            case "scroll":
                asked = int(args.get("scroll_amount") if args.get("scroll_amount") is not None else 3)
                if asked < 0:
                    raise ValueError(f"scroll_amount must not be negative, not {asked}")
                amount = min(asked, BOUNDS["scroll_amount"])  # more is cut to the most, never refused
                step = 100 * amount
                dx, dy = {"up": (0, -step), "down": (0, step), "left": (-step, 0), "right": (step, 0)}[args["scroll_direction"]]
                with self._holding(args.get("text")):
                    self.wheel(args.get("coordinate") or None, dx, dy)
                if amount < asked:
                    page.wait_for_timeout(150)
                    page.wait_for_load_state()
                    return [{"type": "text", "text": f"OK: scrolled {amount}, the most one scroll turns (scroll_amount {asked} was asked)"}]
            case "type":
                text = str(args["text"])
                if len(text) > BOUNDS["type_chars"]:
                    raise ValueError(f"text may be at most {BOUNDS['type_chars']} characters, not {len(text)}")
                for i in range(0, len(text), TYPE_CHUNK):
                    self._check()
                    page.keyboard.type(text[i : i + TYPE_CHUNK])
            case "key":
                key = to_playwright_key(args["text"])
                for _ in range(self._bounded(args, "repeat", 1, BOUNDS["repeat"])):
                    self._check()
                    page.keyboard.press(key)
            case "hold_key":
                seconds = self._bounded(args, "duration", 0, BOUNDS["hold_seconds"], float)
                with self._holding(args["text"]):
                    time.sleep(seconds)
            case "wait":
                # Real seconds mean nothing here: simulated time only moves through the UI's Wait control.
                time.sleep(self._bounded(args, "duration", 0, BOUNDS["wait_seconds"], float))
            case _:
                raise ValueError(f"unknown computer action: {name}")
        # A click or keypress may start a navigation slightly after it returns; give it a moment
        # to begin, then wait for it, so the next screenshot never shows the previous page.
        page.wait_for_timeout(150)
        page.wait_for_load_state()
        return [{"type": "text", "text": "OK"}]


def serve_ui(task: Task, episode=None):
    """Serve the task's world in UI-only mode on a free local port. Yields (base_url, app)."""
    return serve_task(task, "ui", episode)


def run_episode(task: Task | str, client, model: str = "claude-opus-5-5", effort: str = "medium", limits: Limits | None = None,
                headless: bool = True, out_dir=None, repeat: int = 0, max_tokens: int = 16000, spend=None, unmetered: bool = False) -> dict:  # fmt: skip
    """Play one episode through the browser and return the grading report plus token usage, answer and terminal reason.

    `client` is an anthropic.Anthropic(). As in the tool harness, refusal
    fallbacks are deliberately off so results reflect the named model only.
    Each computer action (a click, a keypress, a screenshot) is one action, charged when it is attempted.
    """
    from playwright.sync_api import sync_playwright

    task = load_task(task) if isinstance(task, str) else task
    system = SYSTEM_PROMPT + ui_note(task)
    settings = {"max_tokens": max_tokens, "system": system, "viewport": VIEWPORT, "headless": headless}
    metering = check_metering(client, spend, unmetered)  # before anything else: no unmetered client by default
    ep = Episode(task, harness="claude_browser", interface="ui", provider="anthropic", model=model, effort=effort, limits=limits,
                 settings=settings, out_dir=out_dir, repeat=repeat, spend=spend)  # fmt: skip
    ep.extra["metering"] = metering
    ep.extra["comparability"] |= UI_COMPARABILITY | {"screenshots": SCREENSHOT_POLICY}
    app = None
    messages = [{"role": "user", "content": task.instruction}]
    create = without_retries(client).messages.create

    def send(final_chance=False):
        return ep.send(
            create, anthropic_usage, final_chance=final_chance, model=model, max_tokens=max_tokens, system=system,
            tools=[{"type": "computer_toolset_20260801"}], messages=messages, output_config={"effort": effort}, cache_control={"type": "ephemeral"},
        )  # fmt: skip

    with ep, serve_ui(task, ep) as (base_url, app), sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(base_url)
        reads = ScreenReads(page, app)
        screen = Screen(page, on_shot=reads.captured, should_stop=lambda: app.state.submission is not None or ep.acting_too_late())
        ep.on_delivered = reads.delivered
        response = send()
        while True:
            results = []
            if response is not None:  # None: no usable response in time or within the limits; on to the final chance
                calls = [b for b in response.content if b.type == "tool_use"]
                if not ep.late and (response.stop_reason != "tool_use" or not calls):
                    # Replying in plain text is how this harness's model gives its answer, unless the task has a form.
                    ep.end(closing(response.stop_reason, "stopped_without_submit" if answers.has_form(task) else "submitted"), _text(response),
                           detail=response.stop_reason)  # fmt: skip
                    break
                if response.content:
                    messages.append({"role": "assistant", "content": response.content})
                failed = False
                for call in calls:
                    result = {"type": "tool_result", "tool_use_id": call.id, "toolset_name": "computer"}
                    n = 0 if failed or ep.late else ep.try_charge(call.name)  # charged before it runs: an action that fails part-way may still have acted
                    if failed:
                        result |= {"is_error": True, "content": NOT_EXECUTED_AFTER_FAILURE}
                    elif not n:
                        result |= {"is_error": True, "content": "Not executed: a limit was reached."}
                    else:
                        try:
                            result["content"] = screen.run(call.name, call.input)
                            ep.action(n, call.name, call.input, result["content"])
                        except Exception as e:  # report the failure to the model rather than ending the episode
                            failed = True
                            result |= {"is_error": True, "content": f"{type(e).__name__}: {e}"}
                            ep.action(n, call.name, call.input, error=result["content"])
                    results.append(result)
                    if app.state.submission is not None:
                        break
                if answered(ep, app):  # the reply page was submitted: the task is over
                    break
                if ep.allow_request():
                    add_user(messages, results)
                    response = send()
                    continue
            answer = None
            if ep.limits.final_chance and answers.has_form(task):
                with reply_page_only(page, base_url) as blocked:
                    add_user(messages, results + [{"type": "text", "text": ep.note(form=True)}] + screen._image())
                    ep.automatic_screenshot()
                    allowance, response = FinalChance(), send(final_chance=True)
                    while response is not None and not ep.late:
                        calls = [b for b in response.content if b.type == "tool_use"]
                        if not calls:
                            answer = _text(response) or None
                            break
                        messages.append({"role": "assistant", "content": response.content})
                        outs = []
                        for call in calls:
                            out = {"type": "tool_result", "tool_use_id": call.id, "toolset_name": "computer"}
                            if app.state.submission is not None or not allowance.take(call.name):
                                outs.append(out | {"is_error": True, "content": "Not executed: the final chance is over."})
                                continue
                            # not charged against the action limit: counted and recorded apart
                            try:
                                out["content"] = screen.run(call.name, call.input)
                                ep.event("action", action=None, final_chance=True, name=call.name, args=call.input, result=out["content"])
                            except Exception as e:
                                out |= {"is_error": True, "content": f"{type(e).__name__}: {e}"}
                                ep.event("action", action=None, final_chance=True, name=call.name, args=call.input, error=out["content"])
                            outs.append(out)
                        if app.state.submission is not None:
                            answer = app.state.submission["answer"]
                            break
                        if allowance.actions >= answers.FINAL_CHANCE_ACTIONS and allowance.screenshots >= answers.FINAL_CHANCE_SCREENSHOTS:
                            break
                        add_user(messages, outs)
                        response = send(final_chance=True)
                    late = response is None or ep.late
                    final_chance_record(ep, answer, late, allowance.actions, blocked, allowance.screenshots)
                    answer = None if late else answer
            elif ep.limits.final_chance:
                add_user(messages, results + [{"type": "text", "text": ep.note()}])
                answer = ep.final_answer(send(final_chance=True), _text)
            ep.end(ep.stop, answer)
            break
        browser.close()

    return ep.close(app.state.world if app is not None else None)
