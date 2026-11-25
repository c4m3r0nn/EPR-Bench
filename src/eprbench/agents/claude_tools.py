"""Run a Claude model on a task through tool calls."""

import json

from eprbench import answers
from eprbench.agents import check_metering, NOT_EXECUTED_AFTER_FAILURE, ONLY_SUBMIT, SYSTEM_PROMPT, final_submissions, not_executed, submission, submitted_answer, without_retries
from eprbench.env import EPREnv
from eprbench.episode import Episode, Limits

SUBMIT_TOOL = answers.LEGACY_TOOL  # a task with an answer form gets answers.submit_tool(task), which adds its fields


def anthropic_usage(response) -> dict | None:
    """The usage as reported, None where a field is absent (eprbench.spend.check_usage decides what can be trusted).
    input_tokens excludes cached input, which is billed separately (writes above, reads well below the base rate)."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    return {k: getattr(u, k, None) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text")


def closing(stop_reason: str, answered: str) -> str:
    """Terminal reason for a response with no tool calls. `answered` is the reason when the model simply ended its
    turn: stopped_without_submit for a harness with a submit tool, submitted for one where replying is answering."""
    if stop_reason in ("end_turn", "stop_sequence"):
        return answered
    reasons = {"refusal": "refusal", "max_tokens": "stopped_without_submit", "model_context_window_exceeded": "context_limit"}
    return reasons.get(stop_reason, "harness_error")


def add_user(messages: list, blocks: list) -> None:
    """Append content blocks as the user's turn, joining the last message if it is already the user's."""
    if messages and messages[-1]["role"] == "user":
        last = messages[-1]["content"]
        messages[-1] = {"role": "user", "content": ([{"type": "text", "text": last}] if isinstance(last, str) else list(last)) + blocks}
    else:
        messages.append({"role": "user", "content": blocks})


TOOL_COMPARABILITY = {
    "action_unit": "one operation call", "screenshots": "none: operations return data",
    "batch_stops_on": "the first call in a turn that returns an error (including invalid arguments); the rest are not executed",
}  # fmt: skip


def _submitted(response, task):
    """The answer in a final-chance response: its submit call's, or else its text."""
    calls = [b for b in response.content if b.type == "tool_use" and b.name == "submit"]
    return submitted_answer(task, calls[0].input, _text(response)) if calls else _text(response)


def step(ep: Episode, env: EPREnv, name: str, args) -> tuple[dict, bool]:
    """Charge, carry out and record one operation. Returns what the model is shown, and whether it succeeded."""
    n = ep.try_charge()
    if not n:
        return not_executed(ep), False
    obs, *_, info = env.step({"tool": name, "args": args})
    body = obs["result"] if obs["ok"] else {"error": obs["error"], "message": obs["message"]}
    ep.action(n, name, args, body, audit_id=info.get("audit_id"), ok=obs["ok"])
    return body, obs["ok"]


def run_episode(env: EPREnv, client, model: str = "claude-opus-5-5", effort: str = "medium", max_tokens: int = 16000,
                limits: Limits | None = None, out_dir=None, repeat: int = 0, spend=None, unmetered: bool = False) -> dict:  # fmt: skip
    """Play one episode and return the grading report plus token usage, answer and terminal reason.

    `client` is an anthropic.Anthropic(). Server-side refusal fallbacks are
    deliberately not enabled: a benchmark must report the named model's own
    behaviour, so a refusal ends the episode and is recorded as such. `spend` is an eprbench.spend.Budget, if any.
    """
    metering = check_metering(client, spend, unmetered)  # before anything else: no unmetered client by default
    ep = Episode(env.task, harness="claude_tools", interface="tool", provider="anthropic", model=model, effort=effort, limits=limits,
                 settings={"max_tokens": max_tokens, "system": SYSTEM_PROMPT}, out_dir=out_dir, repeat=repeat, spend=spend)  # fmt: skip
    ep.extra["metering"] = metering
    ep.extra["comparability"] |= TOOL_COMPARABILITY
    obs, _ = env.reset()
    env.max_steps = 0  # the episode's limits apply instead of the task's own allowance
    tools = env.tools + [answers.submit_tool(env.task)]
    messages = [{"role": "user", "content": obs["instruction"]}]
    create = without_retries(client).messages.create

    def send(final_chance=False, offered=None):
        return ep.send(
            create, anthropic_usage, final_chance=final_chance, model=model, max_tokens=max_tokens, system=SYSTEM_PROMPT,
            tools=offered or tools, messages=messages, output_config={"effort": effort}, cache_control={"type": "ephemeral"},
        )  # fmt: skip

    def ask(feedback):  # the final chance of a task with a form: only the submit tool is offered
        if feedback is not None:
            add_user(messages, feedback)
        return send(final_chance=True, offered=[answers.submit_tool(env.task)])

    def feedback(response, body):
        messages.append({"role": "assistant", "content": response.content})
        return [{"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(body if b.name == "submit" else ONLY_SUBMIT), "is_error": True}
                for b in response.content if b.type == "tool_use"]  # fmt: skip

    with ep:
        response = send()
        while True:
            results = []
            if response is not None:  # None: no usable response in time or within the limits; on to the final chance
                calls = [b for b in response.content if b.type == "tool_use"]
                if not ep.late and (response.stop_reason != "tool_use" or not calls):
                    # The model stopped without calling submit: its closing text is its answer.
                    ep.end(closing(response.stop_reason, "stopped_without_submit"), _text(response), detail=response.stop_reason)
                    break
                if response.content:
                    messages.append({"role": "assistant", "content": response.content})
                answer, failed = None, False
                for call in calls:
                    if call.name == "submit" and not (ep.late or failed):
                        answer, body = submission(ep, env.task, call.input)
                        if body is None:
                            break
                        ok, failed = False, True  # a malformed submission: the model is told why and may submit again
                    elif failed or call.name == "submit":
                        body, ok = (not_executed(ep) if ep.late else {"error": "not_executed", "message": NOT_EXECUTED_AFTER_FAILURE}), False
                    else:
                        body, ok = step(ep, env, call.name, call.input)
                        failed = not ok
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(body), "is_error": not ok})
                if answer is not None:
                    ep.end(ep.stop or "submitted", answer, detail="submit")
                    break
                if ep.allow_request():
                    add_user(messages, results)
                    response = send()
                    continue
            # A limit was reached (or the response came too late): one last request, with nothing further carried out.
            answer = None
            if ep.limits.final_chance and answers.has_form(env.task):
                add_user(messages, results + [{"type": "text", "text": ep.note(form=True)}])
                submits = lambda r: [b.input for b in r.content if b.type == "tool_use" and b.name == "submit"]  # noqa: E731
                answer = final_submissions(ep, env.task, ask, submits, _text, feedback)
            elif ep.limits.final_chance:
                add_user(messages, results + [{"type": "text", "text": ep.note()}])
                answer = ep.final_answer(send(final_chance=True), lambda r: _submitted(r, env.task))
            ep.end(ep.stop, answer)
            break

    return ep.close(env.world)
