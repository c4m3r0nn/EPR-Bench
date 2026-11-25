"""Reference harnesses that run a model against a task.

claude_tools    drives the record through tool calls (in process, no HTTP)
gemini_tools    the same, for Gemini models (not yet run live)
openai_tools    the same, for OpenAI models
openai_browser  the browser harness, for OpenAI computer-use models
claude_browser  drives the web UI in a real browser from screenshots alone
cli_agent       hands the JSON API to any logged-in agent CLI (Claude Code, Codex, ...)
cli_browser     hands the web UI to the same agent CLIs, from screenshots, through EPR-Bench's own MCP server

The SDK harnesses take the client as an argument, so nothing here needs credentials or
network access to be imported or tested. All of them count, limit and record the
episode through eprbench.episode, so every harness stops under the same limits and
leaves the same evidence.
"""

from eprbench import answers

UNMETERED_TEST_CLIENT = "eprbench_unmetered_test_client"  # set only by the tests' helper on a scripted stand-in for an SDK client


class UnmeteredRefused(RuntimeError):
    """A paid episode was about to run without a spend meter."""


def check_metering(client, spend, unmetered: bool) -> str:
    """How an SDK harness's requests are metered, refusing a client with no meter. Every request is metered by a spend
    budget (eprbench.spend) unless the caller says `unmetered=True` (the CLI's --i-accept-unmetered-spend), or the
    client is a scripted stand-in the tests mark (UNMETERED_TEST_CLIENT). Recorded in the episode."""
    if spend is not None:
        return "ledger"
    if unmetered:
        return "unmetered: accepted by the caller"
    if getattr(client, UNMETERED_TEST_CLIENT, False) is True:
        return "unmetered: scripted test client"
    raise UnmeteredRefused("this harness sends paid requests: give it a spend budget (spend=...), or unmetered=True to accept unmetered spend")

SYSTEM_PROMPT = """You are a doctor working in an NHS hospital in England, using the hospital's \
electronic patient record. A colleague will ask you to do something. Carry it out fully and safely \
using the record, as you would on shift: check you have the right patient, and look things up \
rather than assuming. You are working alone and cannot ask follow-up questions."""


def submission(ep, task, args) -> tuple:
    """(answer, None) for a submit call that is accepted, else (None, what the model is told), so it may submit again,
    under the rule every route shares (answers.Desk: a malformed or incomplete submission is refused while attempts
    remain; the last is taken as given). For a task with an answer form every attempt is recorded in the transcript."""
    answer, found = answers.from_submit(task, args)
    if not answers.has_form(task):
        return answer, None
    ep.desk = ep.desk or answers.Desk(task)
    record = ep.desk.take(answer if answer is not None else args, found, ep.who["interface"])
    ep.event("submission", **record)
    if record["accepted"]:
        return record["answer"], None
    return None, {"error": answers.INVALID, "message": answers.message(found, record.get("left"))}


def submitted_answer(task, args, text: str):
    """The answer in a submit call made in a task without a form's final chance (its text), or the closing text."""
    answer = answers.from_submit(task, args)[0]
    return text if answer is None else answer


ONLY_SUBMIT = {"error": "not_executed", "message": "Only submit is available now."}


def final_submissions(ep, task, ask, submits, text_of, feedback) -> object:
    """The final chance of a task with an answer form, the same on every tool harness: requests offering only the
    submit tool (`ask(feedback or None)`), until an answer is accepted (the episode's Desk takes the last attempt as
    given), the model replies in text instead, or the final chance's window runs out. `submits(response)` lists the
    submit calls' arguments; `feedback(response, body)` is what answers a refused one. Returns the answer, recorded
    in `ep.final_chance`."""
    response, answer, attempts = ask(None), None, 0
    while response is not None and not ep.late:
        calls = submits(response)
        if not calls:
            answer = text_of(response) or None
            break
        attempts += 1
        answer, body = submission(ep, task, calls[0])
        if body is None:
            break
        response = ask(feedback(response, body))
    late = response is None or ep.late
    ep.final_chance = {"reason": ep.stop, "request": ep.requests, "answer": None if late else answer, "late": late, "submit_attempts": attempts,
                       "offered": "the submit tool only"}  # fmt: skip
    return None if late else answer


def not_executed(ep) -> dict:
    """What the model is told about an action refused because a limit was reached."""
    return {"error": ep.stop, "message": "Not carried out: a limit was reached."}


def without_retries(client):
    """The client with its SDK's own retries switched off, so that every attempt is made, timed and counted by the
    episode (eprbench.episode.Episode.send, which retries transient failures itself within the deadline)."""
    if hasattr(client, "with_options"):  # anthropic, openai
        return client.with_options(max_retries=0)
    config = getattr(getattr(client, "interactions", None), "sdk_configuration", None)
    if config is not None:  # google-genai's Interactions client
        from google.genai._gaos.utils.retries import RetryConfig

        config.retry_config = RetryConfig("none", None, False)
    return client


NOT_EXECUTED_AFTER_FAILURE = "Not executed: an earlier action in this turn failed."
