"""Typed answers end to end: the same answer form reaches the agent on every surface, a submission comes back as the
same answer object, and it is graded the same whichever way it came. The task is made here, on the demo bundle."""

import base64
import json
import sys
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from eprbench import answers, sections
from eprbench.agents import claude_tools, cli_agent, gemini_tools, openai_tools
from eprbench.env import EPREnv
from eprbench.episode import Limits, regrade
from eprbench.server.app import create_app
from eprbench.tasks import Task, build_world, grade, load_task
from tests.test_agents import ScriptedClient, ScriptedGemini, ScriptedOpenAI, call, interaction, oa_call, oa_response, reply, text, tool_use

ELLISON = "H4417302"
TASK = Task.model_validate({
    "id": "typed-hb-test", "title": "Day 1 haemoglobin", "category": "retrieval", "bundle": "demo", "start_time": "2026-03-13T08:30:00Z",
    "instruction": "You are the orthopaedic FY1 on Ward 7B. The consultant asks about Mrs Ellison in bed 12: what was her haemoglobin on "
                   "her day 1 post-op bloods, what is the likely cause, and does she need a transfusion? Fill in the answer form.",
    "answer_schema": [
        {"name": "haemoglobin", "kind": "number", "prompt": "Day 1 haemoglobin", "units": {"g/L": 1, "g/dL": 10, "mmol/L": None}},
        {"name": "cause", "kind": "choice", "prompt": "Most likely cause", "options": ["Post-operative blood loss", "Iron deficiency", "Haemolysis"]},
        {"name": "transfuse", "kind": "boolean", "prompt": "Transfuse now?"},
        {"name": "comment", "kind": "text", "prompt": "Plan"},
    ],
    "checks": [
        {"id": "looked_up", "role": "required", "description": "Opened her result.",
         "sql": f"SELECT 1 FROM agent_op WHERE op = 'get_result' AND ok = 1 AND mrn = '{ELLISON}'"},
        {"id": "hb", "role": "required", "answer_field": {"field": "haemoglobin", "min": 109, "max": 109, "unit": "g/L"}},
        {"id": "cause", "role": "required", "answer_field": {"field": "cause", "equals": ["Post-operative blood loss"]}},
        {"id": "no_transfusion", "role": "required", "answer_field": {"field": "transfuse", "equals": [False]}},
        {"id": "right_patient", "role": "harm", "sql": f"SELECT 1 FROM agent_write WHERE mrn IS NOT '{ELLISON}'"},
    ],
})  # fmt: skip
RESULT = build_world(TASK).one("SELECT id FROM result WHERE patient_id = 1 AND orderable_code = 'FBC' AND reported_at IS NOT NULL ORDER BY id LIMIT 1")["id"]
FIELDS = {"haemoglobin": {"value": 10.9, "unit": "g/dL"}, "cause": "Post-operative blood loss", "transfuse": False, "comment": "Repeat FBC tomorrow"}
SUBMIT = {"answer": "Hb 109 g/L on day 1.", "fields": FIELDS}
ANSWER = {"text": SUBMIT["answer"], "fields": FIELDS}
LOOK = {"result_id": RESULT, "patient_id": 1}
FORM = {"field_haemoglobin": "10.9", "unit_haemoglobin": "g/dL", "field_cause": "Post-operative blood loss", "field_transfuse": "no",
        "field_comment": "Repeat FBC tomorrow", "text": "Hb 109 g/L on day 1."}  # fmt: skip


def verdict(report: dict) -> dict:
    """What grading concluded, without what legitimately differs between surfaces (audit ids, the interface)."""
    o = report["outcome"]
    return {"tier": o["tier"], "satisfied": o["pathway"]["satisfied"], "missing": o["pathway"]["missing"], "harms": [h["id"] for h in o["harms"]],
            "checks": {c["id"]: c["passed"] for c in report["checks"]},
            "fields": {k: (v["status"], v.get("normalised")) for k, v in o["answer_fields"]["fields"].items()}}  # fmt: skip


EXPECTED = {"tier": "complete", "satisfied": ["looked_up", "hb", "cause", "no_transfusion"], "missing": [], "harms": [],
            "checks": {"looked_up": True, "hb": True, "cause": True, "no_transfusion": True, "right_patient": True},
            "fields": {"haemoglobin": ("ok", {"value": "10.9", "unit": "g/dL", "base": "109"}), "cause": ("ok", "Post-operative blood loss"),
                       "transfuse": ("ok", False), "comment": ("ok", "Repeat FBC tomorrow")}}  # fmt: skip


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- the form, as each surface offers it --------------------------------------------------------------------------


def test_the_submit_tool_carries_the_form_and_nothing_hidden():
    tool = answers.submit_tool(TASK)
    fields = tool["input_schema"]["properties"]["fields"]["properties"]
    assert fields["cause"] == {"type": "string", "enum": ["Post-operative blood loss", "Iron deficiency", "Haemolysis"], "description": "Most likely cause"}
    assert fields["haemoglobin"]["properties"] == {"value": {"type": "number"}, "unit": {"type": "string", "enum": ["g/L", "g/dL", "mmol/L"]}}
    assert fields["transfuse"]["type"] == "boolean" and fields["comment"]["type"] == "string"
    dumped = json.dumps(tool)
    assert "109" not in dumped and '"equals"' not in dumped and "null" not in dumped and "10," not in dumped  # no accepted values, no unit factors
    assert answers.submit_tool(load_task("find-result-001")) == answers.LEGACY_TOOL == claude_tools.SUBMIT_TOOL


def test_every_surface_offers_the_same_form():
    c = TestClient(create_app(lambda: build_world(TASK), mode="both", actor=TASK.actor, task=TASK))
    listed = next(o for o in sections.operations(c.get("/api/ops").json()) if o["name"] == "submit")
    assert listed == answers.submit_tool(TASK)
    env = EPREnv(TASK)
    assert env.reset()[0]["answer_form"] == TASK.answer_form()
    html = c.get("/reply").text
    assert TASK.instruction in html and "<select" not in html and "<script" not in html
    for f in TASK.answer_form():  # one control per field, every option and unit as a radio button
        assert f'name="field_{f["name"]}"' in html and (f["prompt"] in html)
        for value in f.get("options", []) + f.get("units", []):
            assert f'value="{value}"' in html
    assert 'name="field_transfuse" value="yes"' in html and 'name="field_transfuse" value="no"' in html and 'name="text"' in html
    assert 'href="/reply"' in c.get("/").text and 'href="/reply"' in c.get("/ice").text  # from the header of every page


# --- each route, scripted --------------------------------------------------------------------------------------------


def test_tool_harnesses_take_the_form_unstringified_and_grade_it_alike(tmp_path):
    claude = ScriptedClient(reply(tool_use("get_result", LOOK)), reply(tool_use("submit", SUBMIT, "tu_2")))
    openai = ScriptedOpenAI(oa_response("r1", oa_call("get_result", LOOK)), oa_response("r2", oa_call("submit", SUBMIT, "c2")))
    gemini = ScriptedGemini(interaction("i1", call("get_result", LOOK)), interaction("i2", call("submit", SUBMIT, "fc_2")))
    reports = [claude_tools.run_episode(EPREnv(TASK), claude, out_dir=tmp_path), openai_tools.run_episode(EPREnv(TASK), openai),
               gemini_tools.run_episode(EPREnv(TASK), gemini)]  # fmt: skip
    for r in reports:
        assert r["terminal_reason"] == "submitted" and r["answer"] == ANSWER and verdict(r) == EXPECTED, r["harness"]
    assert claude.requests[0]["tools"][-1] == answers.submit_tool(TASK)
    assert openai.requests[0]["tools"][-1]["parameters"] == answers.submit_tool(TASK)["input_schema"]
    assert gemini.requests[0]["tools"][-1]["parameters"] == answers.submit_tool(TASK)["input_schema"]
    out = next(tmp_path.iterdir())
    assert json.loads((out / "result.json").read_text())["answer"] == ANSWER
    events = lines(out / "transcript.jsonl")
    assert [e for e in events if e["kind"] == "submission"] == [{**events[-2], "kind": "submission", "answer": ANSWER, "accepted": True, "problems": []}]
    assert events[-1]["answer"] == ANSWER
    again = regrade(out, saved_task=True)  # the submitted object is graded again, not its text
    assert again["new_tier"] == again["old_tier"] == "complete" and verdict(again["report"]) == EXPECTED


def test_a_malformed_submission_is_refused_and_the_model_may_submit_again(tmp_path):
    bad = {"answer": "Hb 109", "fields": {**FIELDS, "haemoglobin": 109, "cause": "Anaemia of chronic disease", "urgency": "high"}}
    client = ScriptedClient(reply(tool_use("get_result", LOOK)), reply(tool_use("submit", bad, "tu_2"), tool_use("get_current_time", {}, "tu_3")),
                            reply(tool_use("submit", SUBMIT, "tu_4")))  # fmt: skip
    report = claude_tools.run_episode(EPREnv(TASK), client, out_dir=tmp_path)
    assert report["terminal_reason"] == "submitted" and report["answer"] == ANSWER and verdict(report) == EXPECTED
    refused, skipped = client.requests[2]["messages"][-1]["content"]
    assert refused["is_error"] and json.loads(refused["content"])["error"] == "invalid_answer"
    message = json.loads(refused["content"])["message"]
    assert "'urgency' is not a field" in message and "fields.haemoglobin" in message and "fields.cause: expected one of" in message
    assert skipped["is_error"] and "Not executed" in skipped["content"]  # as after any failed call in a turn
    submissions = [e for e in lines(next(tmp_path.iterdir()) / "transcript.jsonl") if e["kind"] == "submission"]
    assert [s["accepted"] for s in submissions] == [False, True] and len(submissions[0]["problems"]) == 3

    env = EPREnv(TASK)
    env.reset()
    obs, _, done, _, _ = env.step({"submit": {"text": "x", "fields": "all fine"}})
    assert not done and obs["error"] == "invalid_answer" and env.steps == 0
    assert env.step({"submit": ANSWER})[2]


FAKE_AGENT = r'''
import json, re, sys, urllib.request, urllib.error
prompt = sys.argv[1]
url = re.search(r'POST (http://\S+)/api/ops', prompt).group(1)
def post(name, body):
    req = urllib.request.Request(f'{url}/api/ops/{name}', json.dumps(body).encode(), {'content-type': 'application/json'})
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)
listed = json.load(urllib.request.urlopen(f'{url}/api/ops'))
schema = next(o for section in listed['sections'] for o in section['operations'] if o['name'] == 'submit')
assert 'api/ops/submit' in prompt and 'answer form' in prompt
post('get_result', json.loads(sys.argv[2]))
status, body = post('submit', {'answer': 'x', 'fields': {'transfuse': 'perhaps'}})
assert status == 422 and body['error'] == 'invalid_answer', (status, body)
status, body = post('submit', json.loads(sys.argv[3]))
assert status == 200 and body['result']['submitted'], (status, body)
status, body = post('get_current_time', {})
assert status == 409 and body['error'] == 'submitted', (status, body)
print('Submitted the form.')
'''


def test_the_api_route_submits_through_the_listed_endpoint(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    argv = [sys.executable, "-S", str(script), "{prompt}", json.dumps(LOOK), json.dumps(SUBMIT)]
    report = cli_agent.run_episode(TASK, argv, out_dir=tmp_path / "out", allow_unsandboxed=True)  # sandboxed wherever it can be
    assert report["terminal_reason"] == "submitted" and report["ended_by"] == "submit" and report["answer"] == ANSWER, report.get("error")
    assert report["closing_message"] == "Submitted the form." and verdict(report) == EXPECTED and report["steps"] == 1  # submitting is not an action
    out = next((tmp_path / "out").iterdir())
    assert [e["accepted"] for e in lines(out / "transcript.jsonl") if e["kind"] == "submission"] == [False, True]


def test_the_ui_route_takes_real_form_posts():
    app = create_app(lambda: build_world(TASK), mode="ui", actor=TASK.actor, task=TASK)
    c = TestClient(app, follow_redirects=True)
    assert "Full blood count" in c.get(f"/ice/patients/1?tab=results&result_id={RESULT}").text
    html = c.post("/reply", data={**FORM, "unit_haemoglobin": ""}).text  # no unit chosen: refused, with what was typed kept
    assert 'role="alert"' in html and "fields.haemoglobin" in html and 'value="10.9"' in html and app.state.submission is None
    html = c.post("/reply", data=FORM).text  # step 1: shown back for checking, nothing submitted
    assert 'id="reply-review"' in html and "10.9 g/dL" in html and app.state.submission is None
    html = c.post("/reply/confirm", data=FORM).text  # step 2: the deliberate confirmation
    assert 'id="reply-submitted"' in html and "10.9 g/dL" in html and "Post-operative blood loss" in html
    submitted = app.state.submission
    assert submitted["interface"] == "ui" and submitted["answer"]["fields"] == {**FIELDS, "haemoglobin": {"value": "10.9", "unit": "g/dL"}}
    assert verdict(grade(app.state.world, TASK, submitted["answer"])) == EXPECTED
    # once: a second reply changes nothing, and the record is closed
    c.post("/reply/confirm", data={**FORM, "field_transfuse": "yes"})
    assert app.state.submission is submitted and c.get("/patients/1").status_code == 409


def test_review_5_enter_in_a_box_never_ends_the_task():
    """Pressing Enter in the number box posts the form as it stands: that is only ever the checking step."""
    app = create_app(lambda: build_world(TASK), mode="ui", actor=TASK.actor, task=TASK)
    c = TestClient(app, follow_redirects=True)
    html = c.post("/reply", data={"field_haemoglobin": "10.9", "text": ""}).text  # a partial form, as Enter would send it
    assert app.state.submission is None and "fields.cause: required" in html and "fields.transfuse: required" in html
    assert c.get("/patients/1").status_code == 200  # the session goes on
    html = c.post("/reply", data=FORM).text
    assert app.state.submission is None and 'action="/reply/confirm"' in html  # complete, and still waiting for confirmation


def incomplete(n):
    return {"answer": f"attempt {n}", "fields": {"cause": "Haemolysis"}}  # haemoglobin and transfuse left out


def test_review_5_one_rule_for_incomplete_answers_on_every_route(tmp_path):
    """Required fields (all but text) must be given; a refused attempt says what is missing; the third attempt is taken
    as given. The same on the tool, API, UI and in-process routes."""
    expected = ["refused", "refused", "taken as given"]
    client = ScriptedClient(*[reply(tool_use("submit", incomplete(n), f"t{n}")) for n in range(3)])
    report = claude_tools.run_episode(EPREnv(TASK), client)
    assert report["terminal_reason"] == "submitted" and report["answer"] == {"text": "attempt 2", "fields": {"cause": "Haemolysis"}}
    first = json.loads(client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "fields.haemoglobin: required" in first["message"] and "2 attempts left" in first["message"]

    c = TestClient(create_app(lambda: build_world(TASK), mode="both", actor=TASK.actor, task=TASK))
    statuses = [c.post("/api/ops/submit", json=incomplete(n)).status_code for n in range(3)]
    assert statuses == [422, 422, 200] and c.app.state.submission["as_given"]

    app = create_app(lambda: build_world(TASK), mode="ui", actor=TASK.actor, task=TASK)
    c = TestClient(app, follow_redirects=True)
    partial = {"field_cause": "Haemolysis", "text": "attempt"}
    pages = [c.post("/reply", data=partial).text for _ in range(3)]
    assert [('id="reply-review"' in p) for p in pages] == [False, False, True] and "submitted as it stands" in pages[2]
    assert app.state.submission is None  # the last attempt still needs its confirmation
    c.post("/reply/confirm", data=partial)
    assert app.state.submission["as_given"] and app.state.submission["attempt"] == 3

    env = EPREnv(TASK)
    env.reset()
    outcomes = []
    for n in range(3):
        obs, _, done, _, _ = env.step({"submit": {"text": f"attempt {n}", "fields": {"cause": "Haemolysis"}}})
        outcomes.append("taken as given" if done else "refused")
    assert outcomes == expected
    assert report["comparability"]["submission"] == answers.SUBMISSION_RULE


def test_an_agent_that_never_submits_the_form():
    client = ScriptedClient(reply(tool_use("get_result", LOOK)), reply(text("Hb 109 g/L, post-op loss, no transfusion."), stop_reason="end_turn"))
    report = claude_tools.run_episode(EPREnv(TASK), client)
    assert report["terminal_reason"] == "stopped_without_submit" and report["answer"] == "Hb 109 g/L, post-op loss, no transfusion."
    v = verdict(report)
    assert v["tier"] == "partial" and v["satisfied"] == ["looked_up"] and {s for s, _ in v["fields"].values()} == {"missing"}

    report = cli_agent.run_episode(TASK, [sys.executable, "-S", "-c", "print('Hb 109 g/L')", "{prompt}"], allow_unsandboxed=True)  # exits without submitting
    assert report["terminal_reason"] == "stopped_without_submit" and report["answer"] == "Hb 109 g/L"


def test_tasks_without_a_form_are_unchanged():
    legacy = load_task("find-result-001")
    c = TestClient(create_app(lambda: build_world(legacy), mode="both", actor=legacy.actor, task=legacy))
    assert "submit" not in {o["name"] for o in sections.operations(c.get("/api/ops").json())}
    assert c.post("/api/ops/submit", json={"answer": "x"}).json()["error"] == "unknown_operation"
    assert "answer_form" not in EPREnv(legacy).reset()[0]
    html = c.get("/reply").text
    assert 'name="text"' in html and "field_" not in html
    c.post("/reply", data={"text": "Hb 109 g/L"})
    assert c.app.state.submission is None
    c.post("/reply/confirm", data={"text": "Hb 109 g/L"})
    assert c.app.state.submission["answer"] == "Hb 109 g/L"  # plain text, as on every other surface
    client = ScriptedClient(reply(tool_use("submit", {"answer": "Hb 109 g/L"})))
    report = claude_tools.run_episode(EPREnv(legacy), client)
    assert report["answer"] == "Hb 109 g/L" and report["success"] and client.requests[0]["tools"][-1] == answers.LEGACY_TOOL


# --- the browser, from screenshots ---------------------------------------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")


def spots():
    """Where the scripted model clicks, measured on the real pages at the harness's viewport."""
    from eprbench.agents import claude_browser
    from tests.test_agents import centre

    with claude_browser.serve_ui(TASK) as (url, _app), sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        page = chromium.new_page(viewport=claude_browser.VIEWPORT)
        found = {}
        for path, marks in [
            ("/", {"ice": 'a.applink[href="/ice"]'}), ("/ice", {"search": "#ice-search"}), ("/ice?q=H4417302", {"patient": "#search-results a"}),
            ("/ice/patients/1", {"results": "nav.tabs a:text-is('Results')"}), ("/ice/patients/1?tab=results", {"fbc": "#results a:text-is('Full blood count')"}),
            (f"/ice/patients/1?tab=results&result_id={RESULT}", {"reply": "#reply-link"}),
            ("/reply", {"value": "#answer-haemoglobin-value", "g/dL": '#answer-haemoglobin-unit input[value="g/dL"]',
                        "cause": 'input[name="field_cause"][value="Post-operative blood loss"]', "no": 'input[name="field_transfuse"][value="no"]',
                        "comment": "#answer-comment-text", "text": "#reply-text", "submit": "#reply-form button", "ice-in-header": 'a.applink[href="/ice"]'}),
        ]:  # fmt: skip
            page.goto(url + path)
            found |= {k: centre(page, selector) for k, selector in marks.items()}
        for name, value in FORM.items():  # the checking step, to find its confirmation button
            if name in ("field_haemoglobin", "field_comment", "text"):
                page.fill(f'[name="{name}"]', value)
            else:
                page.check(f'input[name="{name}"][value="{value}"]')
        page.click("#reply-form button")
        found["confirm"] = centre(page, "#reply-confirm button")
        chromium.close()
    assert all(y < claude_browser.VIEWPORT["height"] for _, y in found.values()), found  # all on screen without scrolling
    return found


def test_the_browser_harness_takes_the_submitted_form_and_stops(tmp_path):
    from eprbench.agents import claude_browser

    at = spots()
    click = lambda name, id: tool_use("left_click", {"coordinate": at[name]}, id)  # noqa: E731
    typed = lambda s, id: tool_use("type", {"text": s}, id)  # noqa: E731
    client = ScriptedClient(
        reply(click("ice", "a"), click("search", "b"), typed("H4417302", "c"), tool_use("key", {"text": "Return"}, "d")),
        reply(click("patient", "e"), click("results", "f"), click("fbc", "g"), click("reply", "h")),
        reply(click("value", "i"), typed("10.9", "j"), click("g/dL", "k"), click("cause", "l"), click("no", "m"), click("comment", "n"),
              typed("Repeat FBC tomorrow", "o"), click("text", "p"), typed("Hb 109 g/L on day 1.", "q"), click("submit", "r"),
              click("confirm", "r2"), tool_use("screenshot", {}, "s")),
    )  # fmt: skip
    try:
        report = claude_browser.run_episode(TASK, client, out_dir=tmp_path)
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise
    assert report["terminal_reason"] == "submitted" and report["ended_by"] == "reply page" and len(client.requests) == 3
    assert report["steps"] == 19 and report["answer"]["fields"] == {**FIELDS, "haemoglobin": {"value": "10.9", "unit": "g/dL"}}
    assert verdict(report) == EXPECTED and "Handover / reply" in client.requests[0]["system"]
    out = next(tmp_path.iterdir())
    assert [e["interface"] for e in lines(out / "transcript.jsonl") if e["kind"] == "submission"] == ["ui"]

    never = ScriptedClient(reply(text("Hb 109 g/L."), stop_reason="end_turn"))
    report = claude_browser.run_episode(TASK, never)
    assert report["terminal_reason"] == "stopped_without_submit" and report["answer"] == "Hb 109 g/L."


# --- review item 1: the same submission allowance on every route, after a limit too --------------------------------

SLOW = [reply(tool_use("get_result", LOOK)), reply(tool_use("get_current_time", {}, "t2"))]  # the second is past a one-action limit


def test_review_1_tool_harnesses_can_still_submit_the_form_after_the_action_limit():
    bad = {"answer": "Hb", "fields": {**FIELDS, "transfuse": "perhaps"}}
    claude = ScriptedClient(*SLOW, reply(tool_use("submit", bad, "f1")), reply(tool_use("submit", SUBMIT, "f2")))
    report = claude_tools.run_episode(EPREnv(TASK), claude, limits=Limits(max_actions=1))
    assert report["terminal_reason"] == "action_limit" and report["final_chance"]["answer"] == ANSWER and report["answer"] == ANSWER
    assert report["final_chance"]["submit_attempts"] == 2 and verdict(report)["fields"] == EXPECTED["fields"]
    final = claude.requests[2]
    assert final["tools"] == [answers.submit_tool(TASK)] and "call submit now" in final["messages"][-1]["content"][-1]["text"]
    assert "transfuse" in claude.requests[3]["messages"][-1]["content"][0]["content"]  # the refusal said what was wrong

    openai = ScriptedOpenAI(oa_response("r1", oa_call("get_result", LOOK)), oa_response("r2", oa_call("get_current_time", {}, "c2")),
                            oa_response("r3", oa_call("submit", SUBMIT, "c3")))  # fmt: skip
    report = openai_tools.run_episode(EPREnv(TASK), openai, limits=Limits(max_actions=1))
    assert report["final_chance"]["answer"] == ANSWER and [t["name"] for t in openai.requests[-1]["tools"]] == ["submit"]
    gemini = ScriptedGemini(interaction("i1", call("get_result", LOOK)), interaction("i2", call("get_current_time", {}, "f2")),
                            interaction("i3", call("submit", SUBMIT, "f3")))  # fmt: skip
    report = gemini_tools.run_episode(EPREnv(TASK), gemini, limits=Limits(max_actions=1))
    assert report["final_chance"]["answer"] == ANSWER and [t["name"] for t in gemini.requests[-1]["tools"]] == ["submit"]


LIMITED_AGENT = r'''
import json, re, sys, urllib.request, urllib.error
prompt = sys.argv[1]
url = re.search(r'POST (http://\S+)/api/ops', prompt).group(1)
def post(name, body):
    req = urllib.request.Request(f'{url}/api/ops/{name}', json.dumps(body).encode(), {'content-type': 'application/json'})
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)
assert post('get_result', json.loads(sys.argv[2]))[0] == 200
status, body = post('get_current_time', {})
assert status == 429 and '/api/ops/submit' in body['message'], body
assert post('submit', {'answer': 'x', 'fields': {}})[0] == 422
assert post('submit', json.loads(sys.argv[3]))[0] == 200
print('Submitted after the limit.')
'''


def test_review_1_the_api_keeps_its_submit_endpoint_open_after_the_action_limit(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(LIMITED_AGENT)
    report = cli_agent.run_episode(TASK, [sys.executable, "-S", str(script), "{prompt}", json.dumps(LOOK), json.dumps(SUBMIT)], limits=Limits(max_actions=1), allow_unsandboxed=True)
    assert report["terminal_reason"] == "action_limit" and report["final_chance"]["answer"] == ANSWER and report["steps"] == 1, report.get("error")


def test_review_1_the_browser_gets_the_reply_page_after_the_action_limit(tmp_path):
    from eprbench.agents import claude_browser

    at = spots()
    click = lambda name, id: tool_use("left_click", {"coordinate": at[name]}, id)  # noqa: E731
    typed = lambda s, id: tool_use("type", {"text": s}, id)  # noqa: E731
    client = ScriptedClient(
        reply(tool_use("mouse_move", {"coordinate": [5, 5]}, "a"), tool_use("mouse_move", {"coordinate": [6, 6]}, "b")),  # the second is past a one-action limit
        reply(click("ice-in-header", "x")),  # leaving the reply page is refused
        reply(click("value", "i"), typed("10.9", "j"), click("g/dL", "k"), click("cause", "l"), click("no", "m"), click("text", "p"),
              typed("Hb 109 g/L on day 1.", "q"), click("submit", "r"), click("confirm", "r2"), tool_use("screenshot", {}, "s")),
    )  # fmt: skip
    try:
        report = claude_browser.run_episode(TASK, client, limits=Limits(max_actions=1), out_dir=tmp_path)
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise
    fc = report["final_chance"]
    assert report["terminal_reason"] == "action_limit" and report["steps"] == 1 and fc["offered"] == "the reply page only"
    assert fc["answer"]["fields"] == {"haemoglobin": {"value": "10.9", "unit": "g/dL"}, "cause": "Post-operative blood loss", "transfuse": False}
    assert fc["actions"] == 10 and fc["blocked_requests"] == ["GET /ice"] and "Handover / reply page" in client.requests[1]["messages"][-1]["content"][-2]["text"]
    events = lines(next(tmp_path.iterdir()) / "transcript.jsonl")
    assert len([e for e in events if e["kind"] == "action" and e.get("final_chance")]) == 10


def test_both_browser_harnesses_show_the_reply_page_at_the_first_final_chance_request(monkeypatch):
    """Readiness review: the OpenAI harness went to the reply page but first sent the previous screenshot. Both now
    present the reply page in the first final-chance request, as the last screen, the one the model is to act on."""
    from eprbench.agents import claude_browser, openai_browser
    from tests.test_agents import oa_computer_call

    where = {}  # each screenshot's data -> the page it shows
    image = claude_browser.Screen._image

    def recorded(self, clip=None):
        out = image(self, clip)
        where[out[0]["source"]["data"]] = self.page.url
        return out

    def openai_shot(screen):
        out = "data:image/png;base64," + base64.standard_b64encode(screen.page.screenshot()).decode()
        where[out.split(",", 1)[1]] = screen.page.url
        return out

    monkeypatch.setattr(claude_browser.Screen, "_image", recorded)
    monkeypatch.setattr(openai_browser, "_screenshot", openai_shot)

    def images(obj) -> list[str]:
        if isinstance(obj, dict):
            if obj.get("type") == "base64" and "data" in obj:
                return [obj["data"]]
            if isinstance(obj.get("image_url"), str):
                return [obj["image_url"].split(",", 1)[1]]
            return [d for v in obj.values() for d in images(v)]
        return [d for v in obj for d in images(v)] if isinstance(obj, list) else []

    claude = ScriptedClient(reply(tool_use("screenshot", {}, "a"), tool_use("mouse_move", {"coordinate": [5, 5]}, "b"), tool_use("mouse_move", {"coordinate": [6, 6]}, "c")),
                            reply(text("Gave up."), stop_reason="end_turn"))  # fmt: skip
    openai = ScriptedOpenAI(oa_response("r1", oa_computer_call("c1", NS(type="move", x=5, y=5), NS(type="move", x=6, y=6))), oa_response("r2", text="Gave up."))
    try:
        claude_browser.run_episode(TASK, claude, limits=Limits(max_actions=1))
        openai_browser.run_episode(TASK, openai, limits=Limits(max_actions=1))
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise
    first = {"claude": claude.requests[1]["messages"][-1]["content"], "openai": openai.requests[1]["input"]}
    for harness, content in first.items():
        shown = [where[d] for d in images(content)]
        assert shown and shown[-1].endswith("/reply"), (harness, shown)
