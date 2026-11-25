"""HTTP front end: JSON API, web UI and a token-protected harness control plane.

`mode` decides what the agent under test can reach:
  "api"  - JSON API only
  "ui"   - web UI only; no JSON endpoint exists, so the record can only be
           worked through pages and forms, as on a legacy hospital system
  "both" - everything (for development)

The final answer (see eprbench.answers) is given on the web UI's "Handover / reply" page whenever the server has a
task, and through POST /api/ops/submit, listed with the operations, when the task has an answer form. One answer is
accepted per session: after it, operations and pages are refused, so nothing changes once the answer is given.
"""

import asyncio
import json
import secrets
import time
from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from eprbench import answers, ops, sections
from eprbench.server.ui import build_ui_router
from eprbench.tasks import Task, grade
from eprbench.world import World, iso

OPEN_AFTER_SUBMISSION = ("/reply", "/harness/")  # what still answers once the answer has been given


class OneAtATime:
    """ASGI middleware: handles one HTTP request at a time, so the world changes in one deterministic order, and
    makes each request one agent action (a UI page load runs several operations under one action id). Operations
    themselves run in worker threads (see the handlers), so the event loop stays free while they do."""

    def __init__(self, app, owner: FastAPI):
        self.app, self.owner = app, owner
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        lock = self._locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
        async with lock:
            if self.owner.state.submission is not None and not scope["path"].startswith(OPEN_AFTER_SUBMISSION):
                return await submitted(scope, send)
            world = self.owner.state.world
            action = ops.claim_action_id(world)  # taken at once, so nothing else (a screenshot's reads) can share it
            token = ops.ACTION.set(action)
            try:
                await self.app(scope, receive, send)
            finally:
                ops.ACTION.reset(token)


async def submitted(scope, send) -> None:
    """The reply to anything but the reply page once the answer has been given."""
    if scope["path"].startswith("/api/"):
        body, kind = json.dumps({"ok": False, "error": "submitted", "message": "Your answer has been submitted: the task is over."}).encode(), b"application/json"
    else:
        body = (b'<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><title>Session ended</title></head><body>'
                b'<p role="alert">Your reply has been submitted: the session has ended.</p><p><a href="/reply">See what was submitted</a></p></body></html>')  # fmt: skip
        kind = b"text/html; charset=utf-8"
    await send({"type": "http.response.start", "status": 409, "headers": [(b"content-type", kind)]})
    await send({"type": "http.response.body", "body": body})


def create_app(
    world_factory: Callable[[], World],
    mode: Literal["api", "ui", "both"] = "both",
    actor: str = "Dr A. Gent (FY1)",
    task: Task | None = None,
    harness_token: str | None = None,
    event_log: str | None = None,
    reads: str = "screenshots",
) -> FastAPI:
    app = FastAPI(title="EPR-Bench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.world = world_factory()
    app.state.task = task
    app.state.reads = reads  # "screenshots": a harness reports what the model saw (ui.py); "person": a page shown is read
    app.state.submission = None  # the accepted answer: {"interface", "at", "answer", "accepted", "problems", ...}
    app.state.on_submission = None  # called with every submission attempt, accepted or not (the episode's record)
    app.state.desk = answers.Desk(task) if task is not None else None  # one submission rule for every route

    def submit(answer, found: list[str], interface: str) -> dict:
        """Take one submission attempt under the episode's rule (answers.Desk): record it; accept it, or refuse it
        with what is wrong and how many attempts are left."""
        if app.state.submission is not None:
            return {"ok": False, "error": "already_submitted", "message": "An answer has already been submitted."}
        record = app.state.desk.take(answer, found, interface, iso(app.state.world.now))
        if app.state.on_submission is not None:
            app.state.on_submission(record)
        if not record["accepted"]:
            return {"ok": False, "error": answers.INVALID, "message": answers.message(found, record["left"]), "problems": found, "left": record["left"]}
        app.state.submission = record
        return {"ok": True, "result": {"submitted": True, "message": "Your answer has been submitted. The task is over: stop now."}}

    app.state.submit = submit

    if event_log:
        # Real elapsed time per request, for timing people (or agents) at the keyboard. It is written
        # to a file rather than the database because wall-clock time must never enter the simulation.
        @app.middleware("http")
        async def log_request(request: Request, call_next):
            started = time.time()
            response = await call_next(request)
            entry = {"wall_time": started, "ms": round((time.time() - started) * 1000, 1), "user": actor, "method": request.method,
                     "path": request.url.path, "query": request.url.query, "status": response.status_code, "sim_time": app.state.world.now}
            with open(event_log, "a") as f:
                f.write(json.dumps(entry) + "\n")
            return response

    if mode in ("api", "both"):
        ctx = ops.agent_ctx(actor, "api")

        form = task is not None and answers.has_form(task)

        @app.get("/api/ops")
        def api_list_ops():
            """Every operation with its input schema, grouped under the sections the web UI's menus name (eprbench.sections)."""
            return sections.grouped(ops.tool_schemas() + ([answers.submit_tool(task)] if form else []))

        if form:

            @app.post("/api/ops/submit")
            async def api_submit(request: Request):
                body = await request.body()
                try:
                    args = json.loads(body) if body else {}
                except ValueError:
                    return JSONResponse({"ok": False, "error": "invalid_json", "message": "body must be a JSON object"}, 400)
                out = submit(*answers.from_submit(task, args), "api")
                return out if out["ok"] else JSONResponse(out, 409 if out["error"] == "already_submitted" else 422)

        @app.post("/api/ops/{name}")
        async def api_call_op(name: str, request: Request):
            body = await request.body()
            try:
                args = await request.json() if body else {}
                return {"ok": True, "result": await run_in_threadpool(ops.call, app.state.world, name, args, ctx)}
            except ValueError:
                return JSONResponse({"ok": False, "error": "invalid_json", "message": "body must be a JSON object"}, 400)
            except ops.OpError as e:
                status = 404 if e.code in ("not_found", "unknown_operation") else 422
                return JSONResponse({"ok": False, "error": e.code, "message": e.message}, status)

    if mode in ("ui", "both"):
        app.include_router(build_ui_router(app, actor))
    app.add_middleware(OneAtATime, owner=app)

    if harness_token:

        def authorise(token: str | None):
            if token is None or not secrets.compare_digest(token, harness_token):
                raise HTTPException(404)

        @app.post("/harness/reset")
        def harness_reset(x_harness_token: str | None = Header(None)):
            authorise(x_harness_token)
            app.state.world = world_factory()
            app.state.submission, app.state.desk = None, answers.Desk(task) if task is not None else None
            return {"ok": True}

        @app.post("/harness/grade")
        async def harness_grade(request: Request, x_harness_token: str | None = Header(None)):
            authorise(x_harness_token)
            if task is None:
                raise HTTPException(409, "server was started without a task")
            body = await request.body()
            answer = (await request.json()).get("answer") if body else None
            if answer is None and app.state.submission is not None:  # the answer given in the session
                answer = app.state.submission["answer"]
            return grade(app.state.world, task, answer)

    return app
