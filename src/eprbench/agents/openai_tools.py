"""Run an OpenAI model on a task through tool calls (Responses API).

Request and response field names are checked against the installed openai
SDK's type definitions.
"""

import json

from eprbench import answers
from eprbench.agents import check_metering, NOT_EXECUTED_AFTER_FAILURE, ONLY_SUBMIT, SYSTEM_PROMPT, final_submissions, not_executed, submission, submitted_answer, without_retries
from eprbench.agents.claude_tools import TOOL_COMPARABILITY, step
from eprbench.env import EPREnv
from eprbench.episode import Episode, Limits, context_full


def _functions(tools: list[dict]) -> list[dict]:
    # strict is off: strict mode requires every property to be listed as required, which our optional arguments are not
    return [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"], "strict": False} for t in tools]


def openai_usage(response) -> dict | None:
    """The usage as reported: None without any, a field None or left out where it is absent (eprbench.spend.check_usage
    decides what can be trusted; a missing cached count keeps the reservation). cached_input_tokens is included in
    input_tokens, and billed lower."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", None)
    return {"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None)} | (
        {"cached_input_tokens": cached} if cached is not None else {})


def refused(response) -> bool:
    """Whether the response is a refusal: a message whose content is a refusal, or output stopped by the content filter."""
    details = getattr(response, "incomplete_details", None)
    if response.status == "incomplete" and getattr(details, "reason", None) == "content_filter":
        return True
    return any(getattr(c, "type", None) == "refusal" for item in response.output if item.type == "message" for c in getattr(item, "content", None) or [])


def failed(response) -> tuple[str, str] | None:
    """Terminal reason and detail if the response's status ends the episode whatever it contains, else None."""
    if response.status in ("completed", None):
        return None
    if refused(response):
        return "refusal", "content_filter"
    error = getattr(response, "error", None)
    cause = f"{getattr(error, 'code', '')} {getattr(error, 'message', '')} {getattr(getattr(response, 'incomplete_details', None), 'reason', '')}"
    if context_full(cause):
        return "context_limit", cause.strip()
    if response.status == "incomplete":  # out of output tokens: the model's own doing
        return "stopped_without_submit", f"incomplete: {cause.strip()}"
    return "harness_error", f"{response.status}: {cause.strip()}"  # failed, cancelled, or anything unexpected


def closing(response, answered: str) -> tuple[str, str]:
    """Terminal reason and detail for a response with no calls in it (see claude_tools.closing for `answered`)."""
    if refused(response):
        return "refusal", "refusal"
    return failed(response) or (answered, "end_turn")


def _submitted(response, task):
    """The answer in a final-chance response: its submit call's, or else its text."""
    calls = [i for i in response.output if i.type == "function_call" and i.name == "submit"]
    try:
        return submitted_answer(task, json.loads(calls[0].arguments or "{}"), response.output_text or "") if calls else response.output_text or ""
    except json.JSONDecodeError:
        return response.output_text or ""


def as_items(input) -> list:
    return [{"role": "user", "content": input}] if isinstance(input, str) else list(input)


def run_episode(env: EPREnv, client, model: str = "gpt-5.4-mini", limits: Limits | None = None, out_dir=None, repeat: int = 0, spend=None,
                max_output_tokens: int | None = None, effort: str | None = None, unmetered: bool = False) -> dict:  # fmt: skip
    """Play one episode and return the grading report plus token usage, answer and terminal reason. `client` is an openai.OpenAI().
    `max_output_tokens`, if given, bounds each response (a spend ceiling needs it); `effort`, if given, is sent as the
    reasoning effort; `spend` is an eprbench.spend.Budget.

    A call whose arguments are not valid JSON is charged as an action (as a failed UI action is) and answered with an
    error; like any failed call, it stops the rest of the turn's calls.
    """
    bound = ({"max_output_tokens": max_output_tokens} if max_output_tokens else {}) | ({"reasoning": {"effort": effort}} if effort else {})
    metering = check_metering(client, spend, unmetered)  # before anything else: no unmetered client by default
    ep = Episode(env.task, harness="openai_tools", interface="tool", provider="openai", model=model, effort=effort, limits=limits,
                 settings={"instructions": SYSTEM_PROMPT, **bound}, out_dir=out_dir, repeat=repeat, spend=spend)  # fmt: skip
    ep.extra["metering"] = metering
    ep.extra["comparability"] |= TOOL_COMPARABILITY
    obs, _ = env.reset()
    env.max_steps = 0  # the episode's limits apply instead of the task's own allowance
    tools = _functions(env.tools + [answers.submit_tool(env.task)])
    create = without_retries(client).responses.create
    sent = {}  # the last request's input and previous id, to resend with the final-chance note if it got no response

    def send(input, previous=None, final_chance=False, offered=None):
        sent.update(input=input, previous=previous)
        extra = {"previous_response_id": previous} if previous else {}
        return ep.send(create, openai_usage, final_chance=final_chance, model=model, instructions=SYSTEM_PROMPT, input=input, tools=offered or tools, **bound, **extra)

    def submits(response):
        out = []
        for i in response.output:
            if i.type == "function_call" and i.name == "submit":
                try:
                    out.append(json.loads(i.arguments or "{}"))
                except json.JSONDecodeError:
                    out.append(i.arguments)
        return out

    with ep:
        response = send(obs["instruction"])
        while True:
            outputs, previous = as_items(sent["input"]), sent["previous"]
            if response is not None:
                stop = failed(response)
                calls = [item for item in response.output if item.type == "function_call"]
                if not ep.late and (stop or not calls):
                    # The model stopped without calling submit: its closing text is its answer.
                    reason, detail = stop or closing(response, "stopped_without_submit")
                    ep.end(reason, response.output_text or "", detail=detail)
                    break
                outputs, previous, answer, failing = [], response.id, None, False
                for call in calls:
                    try:
                        arguments = json.loads(call.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = None
                    if call.name == "submit" and arguments is not None and not (ep.late or failing):
                        answer, body = submission(ep, env.task, arguments)
                        if body is None:
                            break
                        failing = True  # a malformed submission: the model is told why and may submit again
                    elif failing or ep.late or call.name == "submit":
                        body = not_executed(ep) if ep.late else {"error": "not_executed", "message": NOT_EXECUTED_AFTER_FAILURE}
                    elif arguments is None:
                        n = ep.try_charge()
                        body = {"error": "invalid_json", "message": "arguments were not valid JSON"} if n else not_executed(ep)
                        if n:
                            ep.action(n, call.name, call.arguments, error="invalid_json")
                        failing = True
                    else:
                        body, ok = step(ep, env, call.name, arguments)
                        failing = not ok
                    outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(body)})
                if answer is not None:
                    ep.end(ep.stop or "submitted", answer, detail="submit")
                    break
                if ep.allow_request():
                    response = send(outputs, previous)
                    continue
            answer = None
            if ep.limits.final_chance and answers.has_form(env.task):
                state = {"input": outputs + [{"role": "user", "content": ep.note(form=True)}], "previous": previous}

                def ask(fed):
                    if fed is not None:
                        state.update(input=fed[1], previous=fed[0])
                    return send(state["input"], state["previous"], final_chance=True, offered=_functions([answers.submit_tool(env.task)]))

                def feedback(r, body):
                    return r.id, [{"type": "function_call_output", "call_id": i.call_id, "output": json.dumps(body if i.name == "submit" else ONLY_SUBMIT)}
                                  for i in r.output if i.type == "function_call"]  # fmt: skip

                answer = final_submissions(ep, env.task, ask, submits, lambda r: r.output_text or "", feedback)
            elif ep.limits.final_chance:
                last = send(outputs + [{"role": "user", "content": ep.note()}], previous, final_chance=True)
                answer = ep.final_answer(last, lambda r: _submitted(r, env.task))
            ep.end(ep.stop, answer)
            break

    return ep.close(env.world)
