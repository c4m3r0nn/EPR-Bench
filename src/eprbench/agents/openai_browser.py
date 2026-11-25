"""Run an OpenAI model on a task through the web UI, from screenshots alone.

Uses the Responses API computer tool, with field names checked against the
installed openai SDK. The page, server and action executor are shared with
the Claude browser harness, so both vendors see identical screens.
"""

import base64

from eprbench import answers
from eprbench.agents import check_metering, SYSTEM_PROMPT, without_retries
from eprbench.agents.claude_browser import UI_COMPARABILITY, VIEWPORT, FinalChance, Screen, answered, final_chance_record, reply_page_only, ScreenReads, serve_ui, ui_note
from eprbench.agents.openai_tools import as_items, closing, failed, openai_usage
from eprbench.episode import Episode, Limits
from eprbench.tasks import Task, load_task

# See claude_browser.SCREENSHOT_POLICY: this harness's model is shown the screen after every call without asking.
SCREENSHOT_POLICY = ("automatic: a screenshot is returned after every computer call (automatic_screenshots); an explicit screenshot action is a "
                     "screenshot request, not an action, as on the Claude harness")  # fmt: skip

_BUTTONS = {"left": "left_click", "right": "right_click", "wheel": "middle_click", "middle": "middle_click"}


def _keys(keys) -> str | None:
    return "+".join(keys) if keys else None


def perform(screen: Screen, action) -> None:
    """Translate one OpenAI computer action onto the shared executor."""
    kind = action.type
    at = [getattr(action, "x", 0), getattr(action, "y", 0)]
    modifiers = _keys(getattr(action, "keys", None))
    if kind == "click":
        screen.run(_BUTTONS.get(action.button, "left_click"), {"coordinate": at, "text": modifiers})
    elif kind == "double_click":
        screen.run("double_click", {"coordinate": at, "text": modifiers})
    elif kind == "move":
        screen.run("mouse_move", {"coordinate": at})
    elif kind == "drag":
        path = [[p.x, p.y] for p in action.path]
        screen.run("left_click_drag", {"start_coordinate": path[0], "coordinate": path[-1], "text": modifiers})
    elif kind == "scroll":  # in pixels, through the same bound as every other route's scroll (cut, not refused)
        screen.wheel(at, action.scroll_x or 0, action.scroll_y or 0)
        screen.page.wait_for_timeout(150)
    elif kind == "type":
        screen.run("type", {"text": action.text})
    elif kind == "keypress":
        screen.run("key", {"text": "+".join(action.keys)})
    elif kind == "wait":
        screen.run("wait", {"duration": 1})
    elif kind != "screenshot":
        raise ValueError(f"unknown computer action: {kind}")


def _screenshot(screen: Screen) -> str:
    return "data:image/png;base64," + base64.standard_b64encode(screen.shot()).decode()


def run_episode(task: Task | str, client, model: str = "gpt-5.4-mini", limits: Limits | None = None, headless: bool = True,
                out_dir=None, repeat: int = 0, spend=None, max_output_tokens: int | None = None, effort: str | None = None,
                unmetered: bool = False) -> dict:  # fmt: skip
    """Play one episode through the browser. `client` is an openai.OpenAI(). Safety checks raised by the
    API are acknowledged automatically (this is a sandbox) and counted in the report. Each computer action is one action."""
    from playwright.sync_api import sync_playwright

    task = load_task(task) if isinstance(task, str) else task
    instructions = SYSTEM_PROMPT + ui_note(task)
    bound = ({"max_output_tokens": max_output_tokens} if max_output_tokens else {}) | ({"reasoning": {"effort": effort}} if effort else {})
    settings = {"instructions": instructions, "viewport": VIEWPORT, "headless": headless, **bound}
    metering = check_metering(client, spend, unmetered)  # before anything else: no unmetered client by default
    ep = Episode(task, harness="openai_browser", interface="ui", provider="openai", model=model, effort=effort, limits=limits, settings=settings,
                 out_dir=out_dir, repeat=repeat, spend=spend)  # fmt: skip
    ep.extra["metering"] = metering
    ep.extra["api_safety_checks"] = 0
    ep.extra["comparability"] |= UI_COMPARABILITY | {"screenshots": SCREENSHOT_POLICY}
    app = None
    create = without_retries(client).responses.create
    sent = {}

    def send(input, previous=None, final_chance=False):
        sent.update(input=input, previous=previous)
        extra = {"previous_response_id": previous} if previous else {}
        return ep.send(create, openai_usage, final_chance=final_chance, model=model, instructions=instructions,
                       tools=[{"type": "computer"}], input=input, **bound, **extra)  # fmt: skip

    with ep, serve_ui(task, ep) as (base_url, app), sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(base_url)
        reads = ScreenReads(page, app)
        screen = Screen(page, on_shot=reads.captured, should_stop=lambda: app.state.submission is not None or ep.acting_too_late())
        ep.on_delivered = reads.delivered
        response = send(task.instruction)
        while True:
            outputs, previous = as_items(sent["input"]), sent["previous"]
            if response is not None:  # None: no usable response in time or within the limits; on to the final chance
                stop = failed(response)
                calls = [item for item in response.output if item.type == "computer_call"]
                if not ep.late and (stop or not calls):
                    # Replying in plain text is how this harness's model gives its answer, unless the task has a form.
                    reason, detail = stop or closing(response, "stopped_without_submit" if answers.has_form(task) else "submitted")
                    ep.end(reason, response.output_text or "", detail=detail)
                    break
                outputs, previous, problems = [], response.id, []
                for call in calls:
                    # every call gets an output; once an action has failed or been refused, later ones are not carried out
                    for action in call.actions or ([call.action] if call.action else []):
                        n = 0 if problems or ep.late else ep.try_charge(action.type)  # charged before it runs
                        if problems or not n:
                            problems.append(f"{action.type}: not carried out" + ("" if problems else " (a limit was reached)"))
                            continue
                        try:
                            perform(screen, action)
                            ep.action(n, action.type, action)
                        except Exception as e:  # the model is told, and sees the screen as it now is
                            problems.append(f"{action.type} failed: {type(e).__name__}: {e}")
                            ep.action(n, action.type, action, error=problems[-1])
                        if app.state.submission is not None:
                            break
                    if app.state.submission is not None:
                        break
                    output = {"type": "computer_call_output", "call_id": call.call_id, "output": {"type": "computer_screenshot", "image_url": _screenshot(screen)}}
                    ep.automatic_screenshot()
                    if call.pending_safety_checks:
                        ep.extra["api_safety_checks"] += len(call.pending_safety_checks)
                        output["acknowledged_safety_checks"] = [{"id": c.id, "code": c.code, "message": c.message} for c in call.pending_safety_checks]
                    ep.event("observation", call_id=call.call_id, screenshot=output["output"]["image_url"])
                    outputs.append(output)
                if answered(ep, app):  # the reply page was submitted: the task is over
                    break
                if problems:
                    outputs.append({"role": "user", "content": "Some actions were not carried out as asked. " + "; ".join(problems) + ". Later actions were not carried out."})
                if ep.allow_request():
                    response = send(outputs, previous)
                    continue
            answer = None
            if ep.limits.final_chance and answers.has_form(task):
                with reply_page_only(page, base_url) as blocked:
                    allowance = FinalChance()
                    # the reply page as it now is, with the note, as the Claude harness sends it: not only the screens
                    # the pending calls left, which show the page the model was on before the limit
                    note = [{"type": "input_text", "text": ep.note(form=True)}, {"type": "input_image", "image_url": _screenshot(screen)}]
                    ep.automatic_screenshot()
                    response = send(outputs + [{"role": "user", "content": note}], previous, final_chance=True)
                    while response is not None and not ep.late:
                        calls = [item for item in response.output if item.type == "computer_call"]
                        if not calls:
                            answer = response.output_text or None
                            break
                        outs = []
                        for call in calls:
                            for action in call.actions or ([call.action] if call.action else []):
                                if app.state.submission is not None or not allowance.take(action.type):
                                    break
                                # not charged against the action limit: counted and recorded apart
                                try:
                                    perform(screen, action)
                                    ep.event("action", action=None, final_chance=True, name=action.type, args=action)
                                except Exception as e:
                                    ep.event("action", action=None, final_chance=True, name=action.type, args=action, error=f"{type(e).__name__}: {e}")
                            outs.append({"type": "computer_call_output", "call_id": call.call_id, "output": {"type": "computer_screenshot", "image_url": _screenshot(screen)}})
                            ep.automatic_screenshot()
                        if app.state.submission is not None:
                            answer = app.state.submission["answer"]
                            break
                        if allowance.actions >= answers.FINAL_CHANCE_ACTIONS and allowance.screenshots >= answers.FINAL_CHANCE_SCREENSHOTS:
                            break
                        response = send(outs, response.id, final_chance=True)
                    late = response is None or ep.late
                    final_chance_record(ep, answer, late, allowance.actions, blocked, allowance.screenshots)
                    answer = None if late else answer
            elif ep.limits.final_chance:
                last = send(outputs + [{"role": "user", "content": ep.note()}], previous, final_chance=True)
                answer = ep.final_answer(last, lambda r: r.output_text or "")
            ep.end(ep.stop, answer)
            break
        browser.close()

    return ep.close(app.state.world if app is not None else None)
