"""The reference harnesses, exercised with scripted stand-ins for the model."""

from types import SimpleNamespace as NS

import pytest

from eprbench.agents import claude_tools
from eprbench.env import EPREnv
from eprbench.episode import Limits
from eprbench.tasks import grade, load_task
from tests.conftest import CTPA_ELLISON


def text(s):
    return NS(type="text", text=s)


def tool_use(name, input, id="tu_1"):
    return NS(type="tool_use", id=id, name=name, input=input)


def reply(*blocks, stop_reason="tool_use"):
    return NS(content=list(blocks), stop_reason=stop_reason, usage=NS(input_tokens=100, output_tokens=10))


def unmetered_test_client(client):
    """Mark a scripted stand-in for a provider's SDK client as one: a harness then runs it without a spend meter, and
    records that it did (agents.check_metering). A real client is never marked, so it never runs unmetered by default."""
    from eprbench.agents import UNMETERED_TEST_CLIENT

    setattr(client, UNMETERED_TEST_CLIENT, True)
    return client


class ScriptedClient:
    """Stands in for anthropic.Anthropic(): returns canned responses and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        unmetered_test_client(self)
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


CTPA = {"patient_id": 1, "orderable_code": "CTPA", "priority": "urgent", "clinical_details": "D5 post TKR, pleuritic pain", "request_details": CTPA_ELLISON}


def test_tool_harness_plays_an_episode_to_submission():
    client = ScriptedClient(
        reply(text("Finding the patient."), tool_use("search_patients", {"query": "Ellison"})),
        reply(tool_use("place_order", CTPA, "tu_2"), tool_use("get_patient", {"patient_id": 404}, "tu_3")),
        reply(tool_use("submit", {"answer": "Requested."}, "tu_4")),
    )
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client, model="claude-opus-5-5")
    assert report["success"] and report["ended_by"] == "submit" and report["interface"] == "tool"
    assert report["terminal_reason"] == "submitted" and report["answer"] == "Requested." and not report["truncated"] and report["steps"] == 3
    assert report["usage"] == {"input_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 30, "requests": 3}

    first, second, third = client.requests
    assert first["model"] == "claude-opus-5-5" and first["messages"][0]["content"].startswith("You are the orthopaedic FY1")
    assert {t["name"] for t in first["tools"]} >= {"search_patients", "place_order", "submit"}
    assert "tool_choice" not in first and "thinking" not in first and "fallbacks" not in first
    # every tool call in a turn is answered in one user message, errors flagged
    results = third["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_2", "tu_3"]
    assert [r["is_error"] for r in results] == [False, True] and "not_found" in results[1]["content"]


def test_tool_harness_treats_closing_text_as_the_answer():
    client = ScriptedClient(
        reply(tool_use("list_results", {"patient_id": 1})),
        reply(text("Her haemoglobin was 109 g/L."), stop_reason="end_turn"),
    )
    report = claude_tools.run_episode(EPREnv("find-result-001"), client)
    assert report["success"] and report["ended_by"] == "end_turn"
    assert report["terminal_reason"] == "stopped_without_submit" and report["answer"] == "Her haemoglobin was 109 g/L."


def test_tool_harness_records_a_refusal_without_falling_back():
    client = ScriptedClient(reply(stop_reason="refusal"))
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client)
    assert report["ended_by"] == "refusal" and report["terminal_reason"] == "refusal" and report["score"] == 0.0 and len(client.requests) == 1


def time_calls(n):
    return [reply(tool_use("get_current_time", {}, f"tu_{i}")) for i in range(n)]


def test_tool_harness_action_limit_gives_one_final_chance():
    task = load_task("find-result-001").model_copy(update={"max_steps": 1})  # the task's own allowance no longer applies
    client = ScriptedClient(*time_calls(3), reply(text("Hb 109 g/L."), stop_reason="end_turn"))
    report = claude_tools.run_episode(EPREnv(task), client, limits=Limits(max_actions=2))
    assert report["terminal_reason"] == "action_limit" and report["truncated"] and report["steps"] == 2
    assert report["usage"]["requests"] == 4 and report["final_chance"] == {"reason": "action_limit", "request": 4, "answer": "Hb 109 g/L.", "late": False}
    assert report["answer"] == "Hb 109 g/L." and report["success"]  # graded for review, though a truncated run is not a result
    last = client.requests[-1]["messages"][-1]["content"]
    assert last[0]["is_error"] and "action_limit" in last[0]["content"]  # the third call was not carried out
    assert last[-1]["type"] == "text" and "limit on actions" in last[-1]["text"]


def test_tool_harness_request_limit_and_no_final_chance():
    client = ScriptedClient(*time_calls(2), reply(text("Done."), stop_reason="end_turn"))
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client, limits=Limits(max_requests=2))
    assert report["terminal_reason"] == "model_request_limit" and report["steps"] == 2 and report["usage"]["requests"] == 3
    assert report["final_chance"]["answer"] == "Done." and "limit on model turns" in client.requests[-1]["messages"][-1]["content"][-1]["text"]

    client = ScriptedClient(*time_calls(2))
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client, limits=Limits(max_requests=1, final_chance=False))
    assert report["terminal_reason"] == "model_request_limit" and len(client.requests) == 1 and report["answer"] is None and report["final_chance"] is None


def test_tool_harness_submitting_on_the_last_allowed_action_is_not_truncated():
    client = ScriptedClient(reply(tool_use("list_results", {"patient_id": 1})), reply(tool_use("submit", {"answer": "Hb 109 g/L"}, "tu_2")))
    report = claude_tools.run_episode(EPREnv("find-result-001"), client, limits=Limits(max_actions=1, max_requests=2))
    assert report["terminal_reason"] == "submitted" and not report["truncated"] and report["success"]


def test_tool_harness_timeout():
    import time

    client = ScriptedClient(*time_calls(1), reply(text("Out of time."), stop_reason="end_turn"))
    create = client.messages.create
    client.messages.create = lambda **kw: (time.sleep(0.2), create(**kw))[1]  # a slow model
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client, limits=Limits(timeout_seconds=0.1))
    assert report["terminal_reason"] == "timeout" and report["steps"] == 0 and report["final_chance"]["answer"] == "Out of time."

    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), ScriptedClient(), limits=Limits(timeout_seconds=0))
    assert report["terminal_reason"] == "timeout" and report["usage"]["requests"] == 0 and "error" not in report


def test_tool_harness_error_is_recorded_and_the_world_still_graded():
    client = ScriptedClient(reply(tool_use("place_order", CTPA)))  # the second request finds nothing to pop: an API failure
    report = claude_tools.run_episode(EPREnv("order-ctpa-001"), client)
    assert report["terminal_reason"] == "harness_error" and report["error"].startswith("IndexError")
    assert report["score"] > 0 and any(s["op"] == "place_order" for s in report["trajectory"])


# --- browser harness ---------------------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")
from eprbench.agents import claude_browser  # noqa: E402


def test_key_names_are_translated():
    assert claude_browser.to_playwright_key("Return") == "Enter"
    assert claude_browser.key_combo("ctrl+a", "darwin") == "Meta+a" and claude_browser.key_combo("ctrl+a", "linux") == "Control+a"  # tests/test_keys.py
    assert claude_browser.to_playwright_key("shift+Page_Down") == "Shift+PageDown"


@pytest.fixture
def chromium():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"chromium unavailable: {e}")
        yield browser
        browser.close()


def centre(page, selector):
    box = page.locator(selector).first.bounding_box()
    return [round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2)]


def test_a_task_can_be_completed_with_mouse_and_keyboard_alone(chromium):
    task = load_task("order-ctpa-001")
    with claude_browser.serve_ui(task) as (url, app):
        page = chromium.new_page(viewport=claude_browser.VIEWPORT)
        page.goto(url)
        screen = claude_browser.Screen(page)

        shot = screen.run("screenshot", {})
        assert shot[0]["source"]["media_type"] == "image/png" and len(shot[0]["source"]["data"]) > 1000

        screen.run("left_click", {"coordinate": centre(page, 'a.applink[href="/ice"]')})
        assert "ICE" in page.title()
        screen.run("left_click", {"coordinate": centre(page, "#ice-search")})
        screen.run("type", {"text": "H4417302"})
        screen.run("key", {"text": "Return"})
        screen.run("left_click", {"coordinate": centre(page, "#search-results a")})
        assert "ELLISON" in page.title()

        def click(selector):
            # stands in for the agent scrolling until the target is on screen
            page.locator(selector).first.scroll_into_view_if_needed()
            screen.run("left_click", {"coordinate": centre(page, selector)})

        def fill(selector, text):
            click(selector)
            screen.run("type", {"text": text})

        for menu_item in ("Imaging (radiology)", "CT", "Chest", "CT Pulmonary angiogram"):
            click(f"ul.menu a:text-is('{menu_item}')")
        assert "Latest eGFR" in page.content()
        click("#order-priority input[value=urgent]")
        fill("#order-clinical_details", "D5 post R TKR, pleuritic chest pain, hypoxia")
        fill("#order-clinical_question", "?PE")
        fill("#order-egfr", "78")
        fill("#order-egfr_date", "2026-03-10")
        click("#order-contrast_reaction input[value=no]")
        click("#order-mobility input[value=bed]")
        screen.run("scroll", {"coordinate": [640, 400], "scroll_direction": "down", "scroll_amount": 5})
        click("#order-form button")
        assert "CT Pulmonary angiogram" in page.content() and "in progress" in page.content()

        zoomed = screen.run("zoom", {"region": [0, 0, 400, 200]})
        assert zoomed[0]["type"] == "image"
        assert screen.run("cursor_position", {})[0]["text"].startswith("[")
        with pytest.raises(ValueError):
            screen.run("teleport", {})

        report = grade(app.state.world, task)
        page.close()
    assert report["success"], [c for c in report["checks"] if not c["passed"]]
    assert app.state.world.one("SELECT DISTINCT interface FROM audit WHERE op = 'place_order'")["interface"] == "ui"


def test_browser_harness_loop_and_result_shape():
    client = ScriptedClient(
        reply(tool_use("screenshot", {}, "tu_1"), tool_use("teleport", {}, "tu_2"), tool_use("screenshot", {}, "tu_3")),
        reply(text("Hb was 109 g/L."), stop_reason="end_turn"),
    )
    try:
        report = claude_browser.run_episode("find-result-001", client)
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise
    # the failed teleport is charged; a screenshot is a screenshot request, not an action
    assert report["success"] and report["interface"] == "ui" and report["steps"] == 1 and report["screenshot_actions"] == 1 and report["terminal_reason"] == "submitted"
    assert report["answer"] == "Hb was 109 g/L." and report["limits"]["max_requests"] == Limits.max_requests
    first, second = client.requests
    assert first["tools"] == [{"type": "computer_toolset_20260801"}]
    results = second["messages"][-1]["content"]
    assert all(r["toolset_name"] == "computer" for r in results)
    assert results[0]["content"][0]["type"] == "image"
    assert results[1]["is_error"] and "teleport" in results[1]["content"]
    assert results[2]["is_error"] and results[2]["content"].startswith("Not executed")


def test_cli_agent_harness_runs_any_command_against_the_api(tmp_path):
    """A stand-in "agent" that reads the API address from its prompt and requests the CTPA."""
    import sys

    from eprbench.agents import cli_agent

    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import json, os, re, sys, urllib.request\n"
        "prompt = sys.argv[1]\n"
        "url = re.search(r'POST (http://\\S+)/api/ops', prompt).group(1)\n"
        "def call(name, **args):\n"
        "    req = urllib.request.Request(f'{url}/api/ops/{name}', json.dumps(args).encode(), {'content-type': 'application/json'})\n"
        "    return json.load(urllib.request.urlopen(req))['result']\n"
        "pid = call('search_patients', query='H4417302')['patients'][0]['patient_id']\n"
        "call('place_order', patient_id=pid, orderable_code='CTPA', priority='urgent', clinical_details='D5 post TKR', request_details=dict(clinical_question='?PE', egfr='78', egfr_date='2026-03-10', contrast_reaction='no', mobility='bed'))\n"
        "assert 'order-ctpa' not in ' '.join(os.listdir('.'))\n"
        "print('Requested. cwd files:', os.listdir('.'))\n"
    )
    report = cli_agent.run_episode("order-ctpa-001", [sys.executable, "-S", str(script), "{prompt}"], allow_unsandboxed=True)  # sandboxed wherever it can be
    assert report["success"] and report["ended_by"] == "exit" and report["interface"] == "api" and report["terminal_reason"] == "submitted"
    assert report["steps"] == 2 and not report["contaminated"]
    assert report["answer"] == "Requested. cwd files: []"  # ran in an empty directory, not the repository
    assert report["agent"].startswith("python")


def test_cli_agent_harness_reports_failure_and_answer_file(tmp_path):
    import sys

    from eprbench.agents import cli_agent

    script = tmp_path / "writer.py"
    script.write_text("import sys\nopen(sys.argv[1], 'w').write('Hb 109 g/L')\nsys.exit(3)\n")
    report = cli_agent.run_episode("find-result-001", [sys.executable, "-S", str(script), "{answer_file}"], allow_unsandboxed=True)
    assert report["ended_by"] == "exit_3" and report["answer"] == "Hb 109 g/L" and report["success"] and report["terminal_reason"] == "harness_error"


class ScriptedGemini:
    """Stands in for genai.Client(): canned interactions, recorded requests."""

    def __init__(self, *interactions):
        self.queue = list(interactions)
        self.requests = []
        unmetered_test_client(self)
        self.interactions = NS(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.queue.pop(0)


def interaction(id, *steps, text=""):
    return NS(id=id, steps=list(steps), output_text=text, usage=NS(total_input_tokens=50, total_output_tokens=5))


def call(name, arguments, id="fc_1"):
    return NS(type="function_call", name=name, arguments=arguments, id=id)


def test_gemini_harness_plays_an_episode():
    from eprbench.agents import gemini_tools

    client = ScriptedGemini(
        interaction("i1", NS(type="thought"), call("search_patients", {"query": "Ellison"})),
        interaction("i2", call("place_order", CTPA, "fc_2"), call("get_patient", {"patient_id": 404}, "fc_3")),
        interaction("i3", call("submit", {"answer": "Requested."}, "fc_4")),
    )
    report = gemini_tools.run_episode(EPREnv("order-ctpa-001"), client)
    assert report["success"] and report["ended_by"] == "submit" and report["model"] == "gemini-3.8-flash"
    assert report["usage"] == {"input_tokens": 150, "output_tokens": 15, "requests": 3}
    first, second, third = client.requests
    assert first["input"].startswith("You are the orthopaedic FY1") and "previous_interaction_id" not in first
    assert {"type": "function", "name": "submit"}.items() <= first["tools"][-1].items() and "parameters" in first["tools"][0]
    assert second["previous_interaction_id"] == "i1" and third["previous_interaction_id"] == "i2"
    results = third["input"]
    assert [(r["type"], r["call_id"]) for r in results] == [("function_result", "fc_2"), ("function_result", "fc_3")]
    assert "not_found" in results[1]["result"][0]["text"]


def test_gemini_harness_takes_closing_text_as_the_answer():
    from eprbench.agents import gemini_tools

    client = ScriptedGemini(interaction("i1", call("list_results", {"patient_id": 1})), interaction("i2", text="Hb 109 g/L"))
    report = gemini_tools.run_episode(EPREnv("find-result-001"), client)
    assert report["success"] and report["ended_by"] == "end_turn" and report["terminal_reason"] == "stopped_without_submit"


def test_gemini_harness_action_limit_gives_one_final_chance():
    from eprbench.agents import gemini_tools

    client = ScriptedGemini(
        interaction("i1", call("list_results", {"patient_id": 1}), call("get_current_time", {}, "fc_2")),
        interaction("i2", call("submit", {"answer": "Hb 109 g/L"}, "fc_3")),
    )
    report = gemini_tools.run_episode(EPREnv("find-result-001"), client, limits=Limits(max_actions=1))
    assert report["terminal_reason"] == "action_limit" and report["steps"] == 1 and report["final_chance"]["answer"] == "Hb 109 g/L"
    final = client.requests[-1]["input"]
    assert "action_limit" in final[1]["result"][0]["text"] and final[-1]["type"] == "text" and "limit on actions" in final[-1]["text"]


class ScriptedOpenAI:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []
        unmetered_test_client(self)
        self.responses = NS(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.queue.pop(0)


def oa_response(id, *output, text="", status="completed"):
    return NS(id=id, output=list(output), output_text=text, status=status, usage=NS(input_tokens=40, output_tokens=4))


def oa_call(name, arguments, call_id="call_1"):
    import json

    return NS(type="function_call", name=name, arguments=arguments if isinstance(arguments, str) else json.dumps(arguments), call_id=call_id)


def test_openai_harness_plays_an_episode():
    from eprbench.agents import openai_tools

    client = ScriptedOpenAI(
        oa_response("r1", NS(type="reasoning"), oa_call("search_patients", {"query": "Ellison"})),
        oa_response("r2", oa_call("place_order", CTPA, "c2"), oa_call("get_patient", "{not json", "c3")),
        oa_response("r3", oa_call("submit", {"answer": "Requested."}, "c4")),
    )
    report = openai_tools.run_episode(EPREnv("order-ctpa-001"), client)
    assert report["success"] and report["ended_by"] == "submit" and report["model"] == "gpt-5.4-mini"
    assert report["usage"] == {"input_tokens": 120, "output_tokens": 12, "requests": 3}
    first, second, third = client.requests
    assert first["instructions"].startswith("You are a doctor") and "previous_response_id" not in first
    assert first["tools"][0]["type"] == "function" and first["tools"][0]["strict"] is False
    assert third["previous_response_id"] == "r2"
    assert [o["call_id"] for o in third["input"]] == ["c2", "c3"] and "invalid_json" in third["input"][1]["output"]


def test_openai_harness_takes_closing_text_as_the_answer():
    from eprbench.agents import openai_tools

    client = ScriptedOpenAI(oa_response("r1", oa_call("list_results", {"patient_id": 1})), oa_response("r2", text="Hb 109 g/L"))
    report = openai_tools.run_episode(EPREnv("find-result-001"), client)
    assert report["success"] and report["terminal_reason"] == "stopped_without_submit" and report["answer"] == "Hb 109 g/L"


def test_openai_harness_malformed_calls_are_bounded_by_the_request_limit():
    from eprbench.agents import openai_tools

    client = ScriptedOpenAI(*[oa_response(f"r{i}", oa_call("get_patient", "{not json", f"c{i}")) for i in range(3)], oa_response("r9", text="Gave up."))
    report = openai_tools.run_episode(EPREnv("order-ctpa-001"), client, limits=Limits(max_requests=3))
    # each malformed call is charged as an action, as a failed UI action is
    assert report["terminal_reason"] == "model_request_limit" and report["steps"] == 3 and report["usage"] == {"input_tokens": 160, "output_tokens": 16, "requests": 4}
    final = client.requests[-1]["input"]
    assert "invalid_json" in final[0]["output"] and final[-1] == {"role": "user", "content": final[-1]["content"]} and "limit on model turns" in final[-1]["content"]
    assert report["final_chance"]["answer"] == "Gave up."


def test_openai_harness_records_a_refusal():
    from eprbench.agents import openai_tools

    client = ScriptedOpenAI(oa_response("r1", NS(type="message", content=[NS(type="refusal", refusal="I can't help with that.")])))
    report = openai_tools.run_episode(EPREnv("order-ctpa-001"), client)
    assert report["terminal_reason"] == "refusal" and report["score"] == 0.0


def oa_computer_call(call_id, *actions, checks=()):
    return NS(type="computer_call", call_id=call_id, action=None, actions=list(actions), pending_safety_checks=list(checks))


def test_openai_browser_harness_drives_the_ui():
    """Scripted 'model' that finds a result in ICE using OpenAI action shapes."""
    from eprbench.agents import openai_browser

    task = load_task("find-result-001")
    # positions measured on the real pages, so the scripted clicks land where a model's would
    with claude_browser.serve_ui(task) as (url, _app), sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        page = chromium.new_page(viewport=claude_browser.VIEWPORT)
        page.goto(url)
        spots = {"ice": centre(page, 'a.applink[href="/ice"]')}
        page.goto(url + "/ice")
        spots["search"] = centre(page, "#ice-search")
        page.goto(url + "/ice?q=H4417302")
        spots["patient"] = centre(page, "#search-results a")
        page.goto(url + "/ice/patients/1")
        spots["results"] = centre(page, "nav.tabs a:text-is('Results')")
        page.goto(url + "/ice/patients/1?tab=results")
        spots["fbc"] = centre(page, "#results a:text-is('Full blood count')")
        chromium.close()

    def click(name):
        x, y = spots[name]
        return NS(type="click", button="left", x=x, y=y, keys=None)

    check = NS(id="sc1", code="sensitive_domain", message="Medical record")
    client = ScriptedOpenAI(
        oa_response("r1", oa_computer_call("c1", NS(type="screenshot"))),
        oa_response("r2", oa_computer_call("c2", click("ice"), click("search"), NS(type="type", text="H4417302"), NS(type="keypress", keys=["ENTER"]), checks=[check])),
        oa_response("r3", oa_computer_call("c3", click("patient"), click("results"), click("fbc"))),
        oa_response("r4", oa_computer_call("c4", NS(type="scroll", x=640, y=400, scroll_x=0, scroll_y=300, keys=None), NS(type="teleport"))),
        oa_response("r5", text="Her day 1 haemoglobin was 109 g/L."),
    )
    report = openai_browser.run_episode(task, client)
    assert report["success"], [c for c in report["checks"] if not c["passed"]]
    assert report["interface"] == "ui" and report["api_safety_checks"] == 1 and report["ended_by"] == "end_turn" and report["terminal_reason"] == "submitted"
    assert any(step["op"] == "get_result" and step["interface"] == "ui" for step in report["trajectory"])  # the clicks reached the result
    second = client.requests[1]
    assert client.requests[0]["tools"] == [{"type": "computer"}] and second["previous_response_id"] == "r1"
    output = second["input"][0]
    assert output["type"] == "computer_call_output" and output["output"]["image_url"].startswith("data:image/png;base64,")
    assert client.requests[2]["input"][0]["acknowledged_safety_checks"] == [{"id": "sc1", "code": "sensitive_domain", "message": "Medical record"}]


def browser_or_skip(run):
    try:
        return run()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise


def test_browser_harness_limits_and_evidence(tmp_path):
    import hashlib
    import json

    client = ScriptedClient(
        reply(tool_use("screenshot", {}, "tu_1"), tool_use("screenshot", {}, "tu_2")),
        reply(text("Hb 109 g/L."), stop_reason="end_turn"),
    )
    report = browser_or_skip(lambda: claude_browser.run_episode("find-result-001", client, limits=Limits(max_screenshots=1), out_dir=tmp_path))
    assert report["terminal_reason"] == "action_limit" and report["limit_reached"] == "max_screenshots" and report["final_chance"]["answer"] == "Hb 109 g/L."
    assert report["steps"] == 0 and report["screenshot_actions"] == 1
    last = client.requests[-1]["messages"][-1]["content"]
    assert last[1]["content"] == "Not executed: a limit was reached." and "limit on actions" in last[-1]["text"]

    out = tmp_path / next(p.name for p in tmp_path.iterdir())
    events = [json.loads(line) for line in (out / "transcript.jsonl").read_text().splitlines()]
    action = next(e for e in events if e["kind"] == "action")
    shot = action["result"][0]["source"]
    assert "data" not in shot and hashlib.sha256((out / shot["screenshot"]).read_bytes()).hexdigest() == shot["sha256"]
    # the screenshot sent back to the model is the same file, referred to where it was sent
    sent = [e for e in events if e["kind"] == "request"][-1]["params"]["messages"]
    assert sent[-1]["content"][0]["content"][0]["source"]["sha256"] == shot["sha256"]
    pages = [json.loads(line) for line in (out / "http.jsonl").read_text().splitlines()]
    assert pages[0]["path"] == "/" and pages[0]["status"] == 200 and (out / pages[0]["response_file"]).read_text().startswith("<!")


def test_openai_browser_harness_counts_every_request_including_the_last():
    from eprbench.agents import openai_browser

    client = ScriptedOpenAI(
        oa_response("r1", oa_computer_call("c1", NS(type="screenshot"))),
        oa_response("r2", oa_computer_call("c2", NS(type="screenshot"))),
        oa_response("r3", text="Hb 109 g/L"),
    )
    report = browser_or_skip(lambda: openai_browser.run_episode("find-result-001", client, limits=Limits(max_requests=2)))
    assert report["terminal_reason"] == "model_request_limit" and report["final_chance"]["answer"] == "Hb 109 g/L"
    assert report["usage"] == {"input_tokens": 120, "output_tokens": 12, "requests": 3} == {**report["usage"], "requests": len(client.requests)}
    assert client.requests[-1]["input"][-1]["role"] == "user" and client.requests[-1]["input"][0]["type"] == "computer_call_output"
