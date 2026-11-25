"""Serve one task's world on a local port for the duration of an episode: an agent's, or a person's (human_session)."""

import asyncio
import json
import secrets
import socket
import threading
import time
import traceback
from contextlib import contextmanager
from urllib.parse import parse_qsl

import uvicorn

from eprbench import answers
from eprbench.server.app import create_app
from eprbench.tasks import Task, build_world

DRAIN_SECONDS = 30  # how long requests already admitted may take to finish when the episode ends
BODY_SECONDS = 10  # how long a client may take to send a request body


class Recorder:
    """ASGI wrapper that records every exchange with the server, serialises the handling of requests, and closes
    admission when the episode ends.

    Each request's body is read first (with a time limit), then the application runs under a lock, so the audit rows
    written meanwhile are that request's own, and the response is sent after the lock is released: a slow client
    holds up nobody. With `count_actions` (API runs) each operation call is charged against the episode's action
    limit before it runs, and refused once a limit is reached, with a message asking for the answer when the episode
    gives a final chance; submitting the answer is not an operation and is never charged or refused for a limit.
    With `count_requests` (a person's session) every request is one action, recorded with what it carried.
    With `cookie` (an agent CLI's browser run) only a client carrying that cookie, which only the harness's own
    browser holds, is served: anything else (an agent trying the server directly) is refused with 403 before it
    reaches the application, uncharged, and kept in `refused`.
    Every exchange is recorded, including one that fails or is cancelled.
    """

    def __init__(self, app, episode=None, count_actions: bool = False, count_requests: bool = False, cookie: tuple[str, str] | None = None):
        self.app, self.episode, self.count_actions, self.count_requests = app, episode, count_actions, count_requests
        self.closed, self.in_flight, self.cookie, self.refused = False, 0, cookie, []
        self._turn = asyncio.Lock()

    def _allowed(self, scope) -> bool:
        if self.cookie is None:
            return True
        name, value = self.cookie
        for key, raw in scope.get("headers", []):
            if key == b"cookie":
                for part in raw.decode(errors="replace").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == name and secrets.compare_digest(v.encode(), value.encode()):
                        return True
        return False

    def close_admission(self) -> None:
        self.closed = True

    def _last_audit_id(self) -> int:
        world = self.app.state.world
        with world.lock:
            return world.one("SELECT coalesce(max(id), 0) AS id FROM audit")["id"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        self.in_flight += 1
        try:
            await self._handle(scope, receive, send)
        finally:
            self.in_flight -= 1

    async def _handle(self, scope, receive, send):
        ep = self.episode
        mark = ep.output_mark if ep is not None else None  # where the agent's own output had got to when this arrived
        body, out = [], {"status": 0, "headers": [], "body": []}
        n, error, audit_ids = 0, None, []
        is_op = self.count_actions and scope["method"] == "POST" and scope["path"].startswith("/api/ops/") and scope["path"] != "/api/ops/submit"
        is_op = is_op and self.app.state.submission is None  # after the answer the application refuses it, uncharged
        is_request = self.count_requests and scope["path"] != "/favicon.ico"

        def reply(status: int, payload: dict):
            out.update(status=status, headers=[(b"content-type", b"application/json")], body=[json.dumps(payload).encode()])

        try:
            try:  # 1. the whole request body, before anything else
                more = True
                while more:
                    message = await asyncio.wait_for(receive(), BODY_SECONDS)
                    if message["type"] != "http.request":
                        raise ConnectionError("the client disconnected before sending its request")
                    body.append(message.get("body", b""))
                    more = message.get("more_body", False)
            except TimeoutError:
                reply(408, {"ok": False, "error": "request_timeout", "message": "The request body was not received in time."})
            # 2. the decision and, under the lock, the application
            if out["status"]:
                pass
            elif not self._allowed(scope):
                self.refused.append(f"{scope['method']} {scope['path']}")
                reply(403, {"ok": False, "error": "forbidden", "message": "Not available."})
            elif self.closed:
                reply(503, {"ok": False, "error": "episode_over", "message": "The episode has ended."})
            elif (is_op or is_request) and not (n := ep.try_charge()):
                form = self.app.state.task is not None and answers.has_form(self.app.state.task)
                reply(429, {"ok": False, "error": ep.stop, "message": ep.note(form=form) if ep.limits.final_chance else "Not carried out: a limit was reached."})
            else:
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": b"".join(body), "more_body": False}
                    await asyncio.Event().wait()  # nothing more will come; a response that listens for a disconnect just waits

                async def keep(message):
                    if message["type"] == "http.response.start":
                        out.update(status=message["status"], headers=list(message.get("headers") or []))
                    elif message["type"] == "http.response.body":
                        out["body"].append(message.get("body", b""))

                async with self._turn:
                    before = self._last_audit_id()
                    try:
                        await self.app(scope, replay, keep)
                    finally:
                        audit_ids = list(range(before + 1, self._last_audit_id() + 1))
            # 3. the response, outside the lock
            await send({"type": "http.response.start", "status": out["status"] or 500, "headers": out["headers"]})
            await send({"type": "http.response.body", "body": b"".join(out["body"])})
        except BaseException as e:
            error = "".join(traceback.format_exception(e)).strip()
            raise
        finally:
            if ep is not None:
                request, response = b"".join(body), b"".join(out["body"])
                headers = {k.decode(errors="replace"): v.decode(errors="replace") for k, v in scope.get("headers", [])}
                content_type = dict(out["headers"]).get(b"content-type", b"").decode()
                ep.http(scope["method"], scope["path"], scope.get("query_string", b"").decode(), headers, request, out["status"] or 500,
                        content_type, response, audit_ids, error, output_mark=mark)  # fmt: skip
                if n and is_request:  # a person's page load or form: what it carried, and the operations it ran
                    query, form = scope.get("query_string", b"").decode(), request.decode(errors="replace")
                    args = dict(parse_qsl(form if scope["method"] == "POST" else query, keep_blank_values=True))
                    ep.action(n, f"{scope['method']} {scope['path']}", args, {"status": out["status"] or 500, "audit_ids": audit_ids},
                              audit_id=audit_ids[-1] if audit_ids else None, error=error)  # fmt: skip
                elif n:
                    try:
                        args, result = json.loads(request or b"{}"), json.loads(response)
                    except ValueError:
                        args, result = request.decode(errors="replace"), response.decode(errors="replace")
                    ep.action(n, scope["path"].removeprefix("/api/ops/"), args, result, audit_id=audit_ids[-1] if audit_ids else None, error=error,
                              **({"output_mark": mark} if mark is not None else {}))  # fmt: skip


@contextmanager
def serve_task(task: Task, mode: str, episode=None, host: str = "127.0.0.1", port: int = 0, person: bool = False, event_log: str | None = None,
               cookie: tuple[str, str] | None = None):  # fmt: skip
    """Yields (base_url, app). `mode` is "ui" or "api" ("both" for a person); the other surface does not exist.

    With an `episode` (eprbench.episode.Episode) every exchange is recorded in its evidence, and so is every attempt
    to submit the answer; in "api" mode each operation call is counted against its action limit, and for a `person`
    every request is one action. With `cookie` (name, value) only a client carrying it is served (Recorder). On
    leaving, admission closes, requests already admitted finish, and the server stops, so nothing can change the
    world once the episode is being graded.
    """
    app = create_app(lambda: build_world(task), mode=mode, actor=task.actor, task=task, event_log=event_log, reads="person" if person else "screenshots")
    app.state.recorder = recorder = Recorder(app, episode, count_actions=mode == "api" and episode is not None and not person,
                                             count_requests=person and episode is not None, cookie=cookie)  # fmt: skip
    if episode is not None:
        app.state.on_submission = lambda record: episode.event("submission", **record)
    if not port:
        with socket.socket() as s:
            s.bind((host, 0))
            port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(recorder, host=host, port=port, log_level="error", timeout_graceful_shutdown=5))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("EPR server failed to start")
        time.sleep(0.01)
    try:
        yield f"http://{host}:{port}", app
    finally:
        recorder.close_admission()
        deadline = time.monotonic() + DRAIN_SECONDS
        while recorder.in_flight and time.monotonic() < deadline:
            time.sleep(0.01)
        server.should_exit = True
        thread.join(timeout=DRAIN_SECONDS)
        if recorder.in_flight or thread.is_alive():
            raise RuntimeError(f"the EPR server did not stop cleanly ({recorder.in_flight} requests still in flight)")


HUMAN_LIMITS = {"max_requests": 0, "max_actions": 1_000_000, "timeout_seconds": 24 * 3600.0, "final_chance": False}


def human_session(task: Task, mode: str = "ui", out_dir=None, host: str = "127.0.0.1", port: int = 8000, event_log: str | None = None,
                  on_ready=None, poll: float = 0.2) -> dict:  # fmt: skip
    """Serve a task to a person and keep the same evidence record an agent's episode leaves (eprbench.episode), so a
    human baseline can be regraded and read in the viewer like any run. The task's actor should be the person (the
    caller gives `task` with actor set to their name), so the checks that credit the actor credit them. Every request
    is one action. The session ends when the person submits the reply page (terminal reason submitted), or when the
    server is interrupted (stopped_without_submit, with no answer). Returns the summary line."""
    from eprbench.episode import Episode, Limits

    ep = Episode(task, harness="human", interface=mode, agent=task.actor, limits=Limits(**HUMAN_LIMITS), out_dir=out_dir,
                 settings={"mode": mode, "user": task.actor})  # fmt: skip
    ep.requests_known = False
    ep.extra["limits_enforced"] = {"max_requests": False, "max_actions": False, "timeout": True, "final_chance_after": []}
    ep.extra["comparability"] |= {"timer_starts": "when the server is ready", "action_unit": "one page load or form submission (an HTTP request)",
                                  "screenshots": "none: a person reads the screen", "batch_stops_on": "not applicable"}  # fmt: skip
    app = None
    with ep, serve_task(task, mode, ep, host=host, port=port, person=True, event_log=event_log) as (url, app):
        ep.start_clock()
        ep.event("session_started", url=url, user=task.actor)
        if on_ready is not None:
            on_ready(url, app)
        interrupted = False
        try:
            while app.state.submission is None and ep.elapsed() < ep.limits.timeout_seconds:
                time.sleep(poll)
        except KeyboardInterrupt:
            interrupted = True
        if app.state.submission is not None:
            ep.end("submitted", app.state.submission["answer"], detail="reply page")
        elif interrupted:
            ep.end("stopped_without_submit", None, detail="the session was stopped without a reply")
        else:
            ep.stop = "timeout"
            ep.end("timeout", None)
    return ep.close(app.state.world if app is not None else None)
