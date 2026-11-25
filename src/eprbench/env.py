"""In-process episode loop with the Gymnasium reset/step shape.

No HTTP and no subprocess: an episode is a restored SQLite image plus Python
function calls, so thousands of environments fit in one machine and the only
real cost is the policy's own inference.
"""

from dataclasses import replace

from eprbench import answers, ops
from eprbench.tasks import GENERATION, Task, grade, initial_snapshot, load_task, restore
from eprbench.world import World, iso


class EPREnv:
    """Actions are {"tool": name, "args": {...}} or {"submit": answer}.

    The answer is text, or for a task with an answer form (reset's observation then carries it as `answer_form`)
    {"text": ..., "fields": {...}}. A malformed or incomplete answer to a form is refused with a message and the
    episode goes on, so the agent may submit again, under the rule every route shares (answers.Desk: the last of
    answers.MAX_ATTEMPTS attempts is taken as given). A refused attempt does not count as a step.

    The episode is truncated after `max_steps` operations (default: the task's own allowance; 0 for no limit). The
    reference harnesses set it to 0 and enforce their own limits through eprbench.episode, so that a model that
    reaches a limit can still be asked for its answer.

    Each step is one agent action. An operation on an object by its id must name the patient too, as on the API and
    in the UI. `store_read_results` keeps what read operations return in the audit log, as evidence; turn it off for
    training, where only writes and errors need keeping.

    `seconds_per_action` is a fixed simulated cost per step, off by default: clinical time moves by explicit waits
    and declared scenario delays, not by reading or navigating.
    """

    def __init__(self, task: Task | str, seconds_per_action: int = 0, max_steps: int | None = None, store_read_results: bool = True):
        self.task = load_task(task) if isinstance(task, str) else task
        self.seconds_per_action = seconds_per_action
        self.max_steps = self.task.max_steps if max_steps is None else max_steps
        self.ctx = ops.agent_ctx(self.task.actor, "tool", store_reads=store_read_results)
        self.world: World | None = None
        self._initial, self._initial_key = None, None  # the starting snapshot, by task content and cache generation
        self.steps = 0
        self.done = True

    @property
    def tools(self) -> list[dict]:
        return ops.tool_schemas()

    def reset(self) -> tuple[dict, dict]:
        key = (self.task.model_dump_json(), GENERATION[0])
        if key != self._initial_key:
            self._initial, self._initial_key = initial_snapshot(self.task), key
        self.world = restore(*self._initial)
        self.steps = 0
        self.done = False
        self.desk = answers.Desk(self.task)
        obs = {"instruction": self.task.instruction, "now": iso(self.world.now)}
        if answers.has_form(self.task):
            obs["answer_form"] = self.task.answer_form()
        return obs, {"task_id": self.task.id}

    def step(self, action: dict) -> tuple[dict, float, bool, bool, dict]:
        if self.done:
            raise RuntimeError("episode is over; call reset()")
        if "submit" in action:
            answer = action["submit"]
            if answers.has_form(self.task):
                found = answers.problems(self.task, answer)
                record = self.desk.take(answer, found, "tool", iso(self.world.now))
                if not record["accepted"]:
                    return {"ok": False, "error": answers.INVALID, "message": answers.message(found, record["left"])}, 0.0, False, False, {}
            self.steps += 1
            return self._finish({"submitted": True}, answer, truncated=False)
        self.steps += 1
        ctx = replace(self.ctx, action_id=ops.next_action_id(self.world))
        try:
            obs = {"ok": True, "result": ops.call(self.world, action.get("tool", ""), action.get("args"), ctx)}
        except ops.OpError as e:
            obs = {"ok": False, "error": e.code, "message": e.message}
        if self.seconds_per_action:
            self.world.advance(self.seconds_per_action)
        info = {"audit_id": self.world.one("SELECT max(id) AS id FROM audit")["id"]}
        if self.max_steps and self.steps >= self.max_steps:
            return self._finish(obs, None, truncated=True)
        return obs, 0.0, False, False, info

    def _finish(self, obs: dict, answer: str | dict | None, truncated: bool):
        self.done = True
        reason = "action_limit" if truncated else "submitted"
        report = grade(self.world, self.task, answer, terminal_reason=reason)
        report["terminal_reason"] = reason
        return obs, report["score"], not truncated, truncated, report
