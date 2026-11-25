"""The final answer: how every surface offers the answer form, takes a submission, checks its shape and records it.

A task with an `answer_schema` has an answer form (Task.answer_form(): names, kinds, prompts, options and unit names,
never accepted answers or unit factors). The same form reaches the agent on every surface:

    tool calls   the `submit` tool's input schema: the free-text `answer` plus `fields`, one property per field
    JSON API     POST /api/ops/submit with the same schema, listed with the operations at GET /api/ops
    web UI       the "Handover / reply" page, one control per field (radio buttons, a number box with unit radio
                 buttons, yes/no radio buttons, text areas), linked from the header of every page

Whatever the surface, an accepted submission becomes the same answer object, {"text": ..., "fields": {...}}, which is
graded as it is (tasks.grade) and recorded in the evidence (result.json, the transcript's submission and end events).

One rule on every route (SUBMISSION_RULE, kept by a Desk per episode): every field but a text field is required. A
submission that is malformed (not an object, a field the form does not have, a value the field cannot take: an
option it does not offer, a unit it does not list, something that is not a number) or incomplete is refused with
what is wrong, and the agent may submit again; MAX_ATTEMPTS attempts in all, the last of which is taken as given
(a field still wrong or missing is then graded as such). Submitting is not an action: no limit on actions stops it,
and when a limit ends the episode the final chance can still submit the form (tool harnesses offer only the submit
tool; the API keeps its submit endpoint open while refusing operations; a browser is taken to the reply page and
may act only there, for at most FINAL_CHANCE_ACTIONS actions). In the web UI submitting takes two deliberate steps:
the form shows what was entered and anything missing or wrong, and only a separate confirmation submits it, so
pressing Enter in a box never ends the task. A task without a form keeps its plain-text answer on every surface.
"""

from eprbench.assessment import read_fields

ANSWER = "Your reply to the colleague who asked: the answer, or a brief account of what you did."
SUBMIT = "Finish the task. Call this once, when everything you intend to do is done."
LEGACY_TOOL = {
    "name": "submit",
    "description": SUBMIT,
    "input_schema": {"type": "object", "properties": {"answer": {"type": "string", "description": ANSWER}}, "required": ["answer"], "additionalProperties": False},
}
INVALID = "invalid_answer"
MAX_ATTEMPTS = 3  # submissions per episode, on every route; the last is taken as given
FINAL_CHANCE_ACTIONS = 40  # a browser's mouse and keyboard actions on the reply page after a limit, uncharged and recorded
FINAL_CHANCE_SCREENSHOTS = 40  # and its screenshots and zooms there, counted apart as they are outside the final chance
SUBMISSION_RULE = (
    f"every field but a text field is required; a malformed or incomplete submission is refused with what is wrong, up to {MAX_ATTEMPTS} "
    "attempts on every route, the last taken as given; submitting is not an action and stays possible in the final chance after a limit "
    "(tools: the submit tool alone; API: the submit endpoint stays open; browser: the reply page only, as it stood (a reply being "
    f"filled in is kept), at most {FINAL_CHANCE_ACTIONS} mouse and keyboard actions and {FINAL_CHANCE_SCREENSHOTS} screenshots); the web UI "
    "needs a review step and a separate confirmation"
)


def has_form(task) -> bool:
    return task.answer_schema is not None


def field_schema(f: dict) -> dict:
    """The JSON schema of one public form field (Task.answer_form())."""
    description = f["prompt"] or f["name"].replace("_", " ")
    if f["kind"] == "choice":
        return {"type": "string", "enum": list(f["options"]), "description": description}
    if f["kind"] == "boolean":
        return {"type": "boolean", "description": description}
    if f["kind"] == "text":
        return {"type": "string", "description": description}
    units = f.get("units") or []
    return {
        "type": "object", "description": description, "additionalProperties": False, "required": ["value", *(["unit"] if units else [])],
        "properties": {"value": {"type": "number"}, **({"unit": {"type": "string", "enum": units}} if units else {})},
    }  # fmt: skip


def submit_tool(task) -> dict:
    """The submit tool (name, description, input_schema): the legacy one for a task without a form."""
    form = task.answer_form()
    if form is None:
        return LEGACY_TOOL
    return {
        "name": "submit",
        "description": SUBMIT + " This task has an answer form: fill in its fields in `fields` as well as your reply in `answer`. "
        "A field you leave out is marked as not answered.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["answer"],
            "properties": {"answer": {"type": "string", "description": ANSWER},
                           "fields": {"type": "object", "description": "The answer form.", "additionalProperties": False,
                                      "properties": {f["name"]: field_schema(f) for f in form}}},
        },
    }  # fmt: skip


def _expected(f: dict) -> str:
    if f["kind"] == "choice":
        return "one of " + ", ".join(f'"{o}"' for o in f["options"])
    if f["kind"] == "boolean":
        return "true or false"
    if f["kind"] == "text":
        return "text"
    units = f.get("units") or []
    return '{"value": <a number>' + (', "unit": one of ' + ", ".join(f'"{u}"' for u in units) if units else "") + "}"


def problems(task, answer) -> list[str]:
    """What is wrong with an answer to this task's form: its shape, and every required field (all but text) left out.
    Empty when it may be accepted. Plain text leaves out every field."""
    if isinstance(answer, str):
        answer = {"text": answer}
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object"]
    out = [f"unknown key {k!r}" for k in sorted(set(answer) - {"text", "fields"})]
    if not isinstance(answer.get("text", ""), str):
        out.append("answer: must be text")
    fields = answer.get("fields", {})
    if not isinstance(fields, dict):
        return out + ["fields: must be an object with one entry per field of the form"]
    form = {f["name"]: f for f in task.answer_form() or []}
    read = read_fields(task.answer_schema or [], fields)
    out += [f"fields: {name!r} is not a field of the form (its fields are {', '.join(form) or 'none'})" for name in read["unknown"]]
    out += [f"fields.{name}: expected {_expected(form[name])}" for name, entry in read["fields"].items() if entry["status"] == "invalid"]
    out += [f"fields.{name}: required, {_expected(form[name])}" for name, entry in read["fields"].items()
            if entry["status"] == "missing" and form[name]["kind"] != "text"]  # fmt: skip
    return out


def from_submit(task, args) -> tuple:
    """(answer, problems) from the submit tool's or endpoint's arguments. A task without a form takes the answer as
    text, exactly as before; a task with one takes {"text": answer, "fields": fields}, passed on as submitted."""
    if not has_form(task):
        return str((args or {}).get("answer", "")) if isinstance(args, dict) else str(args), []
    if not isinstance(args, dict):
        return None, ["the submission must be a JSON object with answer and fields"]
    answer = {"text": args.get("answer", ""), "fields": args.get("fields", {})}
    extra = sorted(set(args) - {"answer", "fields"})
    hint = [f"unknown argument {k!r}: the form's fields go inside fields" for k in extra]
    return answer, hint + problems(task, answer)


# --- the web form ---------------------------------------------------------------------------------------------


def control(name: str) -> str:
    return f"field_{name}"


def unit_control(name: str) -> str:
    return f"unit_{name}"


TEXT_CONTROL = "text"


def from_form(task, form: dict[str, str]) -> dict:
    """The answer from the reply page's posted controls: for a task without a form, the reply as text, as on every
    other surface; otherwise the answer object, where a blank control is a field left out and a number is kept as
    typed (text), which the grader reads exactly."""
    if not has_form(task):
        return form.get(TEXT_CONTROL, "")
    fields = {}
    for f in task.answer_form() or []:
        raw = form.get(control(f["name"]), "")
        if raw == "":
            continue
        if f["kind"] == "boolean":
            fields[f["name"]] = {"yes": True, "no": False}.get(raw, raw)
        elif f["kind"] == "number":
            fields[f["name"]] = {"value": raw} | ({"unit": form.get(unit_control(f["name"]), "")} if f.get("units") else {})
        else:
            fields[f["name"]] = raw
    return {"text": form.get(TEXT_CONTROL, ""), "fields": fields}


def message(found: list[str], left: int | None = None) -> str:
    tail = "" if left is None else f" You have {left} attempt{'s' if left != 1 else ''} left; the last is taken as it stands."
    return "The answer was not accepted: " + "; ".join(found) + ". Correct it and submit again." + tail


class Desk:
    """Takes one episode's submissions under SUBMISSION_RULE, whichever route they come by: refuses a malformed or
    incomplete one while attempts remain, takes the last as given, and accepts one answer only."""

    def __init__(self, task):
        self.task, self.refused, self.accepted = task, 0, None

    def last_attempt(self) -> bool:
        return self.refused >= MAX_ATTEMPTS - 1

    def left(self) -> int:
        return MAX_ATTEMPTS - self.refused - 1

    def take(self, answer, found: list[str], interface: str, at: str | None = None) -> dict:
        """The record of one attempt: accepted, or refused with its problems (and `left`, attempts after this one)."""
        if self.accepted is not None:
            return {"accepted": False, "already": True, "problems": ["an answer has already been submitted"]}
        record = {"interface": interface, "at": at, "answer": answer, "problems": found, "attempt": self.refused + 1}
        if found and not self.last_attempt():
            self.refused += 1
            return record | {"accepted": False, "left": self.left() + 1}
        self.accepted = record | {"accepted": True, "as_given": bool(found)}
        return self.accepted
