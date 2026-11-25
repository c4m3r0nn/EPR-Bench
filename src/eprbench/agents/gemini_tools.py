"""Run a Gemini model on a task through tool calls (Interactions API).

Written from Google's function-calling documentation, with request and
response field names checked against the installed google-genai SDK's type
definitions. Tested only with a scripted stand-in: it has not yet called the
live API, so whether Gemini accepts every operation's JSON schema as-is is
still unknown.
"""

import json

from eprbench import answers
from eprbench.agents import check_metering, NOT_EXECUTED_AFTER_FAILURE, ONLY_SUBMIT, SYSTEM_PROMPT, final_submissions, not_executed, submission, submitted_answer, without_retries
from eprbench.agents.claude_tools import TOOL_COMPARABILITY, step
from eprbench.env import EPREnv
from eprbench.episode import Episode, Limits, context_full


def _declarations(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in tools]


def gemini_usage(interaction) -> dict:
    usage = getattr(interaction, "usage", None)
    return {"input_tokens": getattr(usage, "total_input_tokens", 0) or 0, "output_tokens": getattr(usage, "total_output_tokens", 0) or 0}


_BLOCKED = ("SAFETY", "BLOCK", "PROHIBITED_CONTENT", "RECITATION", "SPII", "BLOCKLIST")


def failed(interaction) -> tuple[str, str] | None:
    """Terminal reason and detail if the interaction's status ends the episode whatever it contains, else None.
    The Interactions API reports the cause in `errors` (code and message), used here where it is given; it has no
    refusal status, so a safety block is recognised by its error code."""
    status = getattr(interaction, "status", None) or "completed"
    if status in ("completed", "requires_action"):
        return None
    cause = " ".join(f"{getattr(e, 'code', '') or ''} {getattr(e, 'message', '') or ''}" for e in getattr(interaction, "errors", None) or []).strip()
    detail = f"{status}: {cause}" if cause else status
    if context_full(cause):
        return "context_limit", detail
    if any(b in cause.upper() for b in _BLOCKED):
        return "refusal", detail
    if status == "incomplete":  # out of output tokens (MAX_TOKENS) or stopped early: the model's own doing
        return "stopped_without_submit", detail
    return "harness_error", detail  # failed, cancelled, budget_exceeded (a quota), or anything unexpected


def _submitted(interaction, task):
    """The answer in a final-chance interaction: its submit call's, or else its text."""
    calls = [s for s in interaction.steps or [] if s.type == "function_call" and s.name == "submit"]
    return submitted_answer(task, dict(calls[0].arguments or {}), interaction.output_text or "") if calls else interaction.output_text or ""


def run_episode(env: EPREnv, client, model: str = "gemini-3.8-flash", limits: Limits | None = None, out_dir=None, repeat: int = 0,
                unmetered: bool = False) -> dict:  # fmt: skip
    """Play one episode and return the grading report plus token usage, answer and terminal reason. `client` is a genai.Client()."""
    metering = check_metering(client, None, unmetered)  # before anything else: no unmetered client by default
    ep = Episode(env.task, harness="gemini_tools", interface="tool", provider="gemini", model=model, limits=limits,
                 settings={"system_instruction": SYSTEM_PROMPT}, out_dir=out_dir, repeat=repeat)  # fmt: skip
    ep.extra["metering"] = metering
    ep.extra["comparability"] |= TOOL_COMPARABILITY
    obs, _ = env.reset()
    env.max_steps = 0  # the episode's limits apply instead of the task's own allowance
    tools = _declarations(env.tools + [answers.submit_tool(env.task)])
    create = without_retries(client).interactions.create
    sent = {}

    def send(input, previous=None, final_chance=False, offered=None):
        sent.update(input=input, previous=previous)
        extra = {"previous_interaction_id": previous} if previous else {}
        return ep.send(create, gemini_usage, final_chance=final_chance, model=model, input=input, tools=offered or tools, system_instruction=SYSTEM_PROMPT, **extra)

    with ep:
        interaction = send(obs["instruction"])
        while True:
            results = [{"type": "text", "text": sent["input"]}] if isinstance(sent["input"], str) else list(sent["input"])
            previous = sent["previous"]
            if interaction is not None:
                stop = failed(interaction)
                calls = [s for s in interaction.steps or [] if s.type == "function_call"]
                if not ep.late and (stop or not calls):
                    # The model stopped without calling submit: its closing text is its answer.
                    reason, detail = stop or ("stopped_without_submit", "end_turn")
                    ep.end(reason, interaction.output_text or "", detail=detail)
                    break
                results, previous, answer, failing = [], interaction.id, None, False
                for call in calls:
                    if call.name == "submit" and not (ep.late or failing):
                        answer, body = submission(ep, env.task, dict(call.arguments or {}))
                        if body is None:
                            break
                        failing = True  # a malformed submission: the model is told why and may submit again
                    elif failing or ep.late or call.name == "submit":
                        body = not_executed(ep) if ep.late else {"error": "not_executed", "message": NOT_EXECUTED_AFTER_FAILURE}
                    else:
                        body, ok = step(ep, env, call.name, dict(call.arguments or {}))
                        failing = not ok
                    results.append({"type": "function_result", "name": call.name, "call_id": call.id, "result": [{"type": "text", "text": json.dumps(body)}]})
                if answer is not None:
                    ep.end(ep.stop or "submitted", answer, detail="submit")
                    break
                if ep.allow_request():
                    interaction = send(results, previous)
                    continue
            answer = None
            if ep.limits.final_chance and answers.has_form(env.task):
                state = {"input": results + [{"type": "text", "text": ep.note(form=True)}], "previous": previous}

                def ask(fed):
                    if fed is not None:
                        state.update(input=fed[1], previous=fed[0])
                    return send(state["input"], state["previous"], final_chance=True, offered=_declarations([answers.submit_tool(env.task)]))

                def feedback(r, body):
                    return r.id, [{"type": "function_result", "name": s.name, "call_id": s.id, "result": [{"type": "text", "text": json.dumps(body if s.name == "submit" else ONLY_SUBMIT)}]}
                                  for s in r.steps or [] if s.type == "function_call"]  # fmt: skip

                submits = lambda r: [dict(s.arguments or {}) for s in r.steps or [] if s.type == "function_call" and s.name == "submit"]  # noqa: E731
                answer = final_submissions(ep, env.task, ask, submits, lambda r: r.output_text or "", feedback)
            elif ep.limits.final_chance:
                last = send(results + [{"type": "text", "text": ep.note()}], previous, final_chance=True)
                answer = ep.final_answer(last, lambda r: _submitted(r, env.task))
            ep.end(ep.stop, answer)
            break

    return ep.close(env.world)
