"""The transcript viewer (eprbench view), built from evidence made here with scripted clients: a tool run, an agent
CLI over the API, a browser run from screenshots and a person's session."""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from eprbench import viewer
from eprbench.agents import claude_browser, claude_tools, cli_agent
from eprbench.env import EPREnv
from eprbench.report import episode_id
from eprbench.server.local import human_session
from tests.test_agents import ScriptedClient, centre, reply, text, tool_use
from tests.test_typed_answers import FORM, LOOK, RESULT, SUBMIT, TASK

APIXABAN = {"drug": "Apixaban", "product": "Apixaban 5mg tablets", "dose": "10 mg", "route": "PO", "frequency": "BD", "override_reason": "replacing prophylaxis"}

STREAM_AGENT = r'''
import json, re, sys, time, urllib.request
prompt = sys.argv[1]
url = re.search(r'POST (http://\S+)/api/ops', prompt).group(1)
n = [0]
def say(content):
    n[0] += 1
    print(json.dumps({"type": "assistant", "message": {"id": f"m{n[0]}", "content": content}}), flush=True)
def post(name, body):
    time.sleep(0.2)  # as a real agent runs its command a moment after printing it
    req = urllib.request.Request(f'{url}/api/ops/{name}', json.dumps(body).encode(), {'content-type': 'application/json'})
    return json.load(urllib.request.urlopen(req))
say([{"type": "text", "text": "Checking who is in bed 12."},
     {"type": "tool_use", "id": "p1", "name": "Bash", "input": {"command": f"curl -s -X POST {url}/api/ops/get_patient -d 'patient 2'"}}])
post('get_patient', {"patient_id": 2})
say([{"type": "text", "text": "Not her; the other Margaret."},
     {"type": "tool_use", "id": "p2", "name": "Bash", "input": {"command": f"curl -s -X POST {url}/api/ops/get_patient -d 'patient 1'"}}])
post('get_patient', {"patient_id": 1})
say([{"type": "thinking", "thinking": "The FBC from day 1 is what is needed."}, {"type": "text", "text": "Opening her blood results."},
     {"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": f"curl -s -X POST {url}/api/ops/get_result -d '...'"}}])
out = post('get_result', json.loads(sys.argv[2]))
print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c1", "content": json.dumps(out)}]}}))
say([{"type": "tool_use", "id": "c2", "name": "Bash", "input": {"command": f"curl -s -X POST {url}/api/ops/submit -d '...'"}}])
post('submit', json.loads(sys.argv[3]))
print(json.dumps({"type": "result", "subtype": "success", "result": "Submitted the form.", "usage": {"input_tokens": 10, "output_tokens": 5}}))
'''


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def flat(timeline):
    """Timeline items with groups opened."""
    return [x for i in timeline for x in (i["items"] if i["type"] == "group" else [i])]


def test_review_8_calls_are_paired_with_a_command_only_when_the_record_shows_it():
    log = [{"type": "command", "command": "curl .../api/ops/get_patient", "line": 2, "output": None},
           {"type": "model", "text": ["Next."], "reasoning": [], "line": 4},
           {"type": "command", "command": "python3 script.py", "line": 6, "output": None}]  # fmt: skip
    act = lambda name, mark: {"type": "action", "name": name, "output_mark": mark}  # noqa: E731
    calls = [act("get_patient", 2), act("get_patient", 7), act("get_result", 7), {"type": "end"}]
    out = viewer._interleave(log, calls)
    assert [i["type"] for i in out] == ["command", "action", "model", "command", "group", "end"]
    assert out[4]["label"] == viewer.SERVER_ORDER and [a["name"] for a in out[4]["items"]] == ["get_patient", "get_result"]
    legacy = viewer._interleave(log, [{"type": "action", "name": "get_patient"}, {"type": "end"}])  # no marks: not paired
    assert legacy[0]["type"] == "group"


def browser(run):
    try:
        return run()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium unavailable")
        raise


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    root = tmp_path_factory.mktemp("evidence")
    # tool calls: a treatment that the ward responds to, and a note on the wrong patient
    tool = ScriptedClient(
        reply(NS(type="thinking", thinking="Likely PE; confirm the patient first."), text("Finding her."), tool_use("search_patients", {"query": "Ellison"})),
        reply(tool_use("prescribe_medication", {"patient_id": 1, **APIXABAN}, "t2"), tool_use("write_note", {"patient_id": 2, "kind": "progress", "title": "PE plan", "body": "Apixaban."}, "t3")),
        reply(*[tool_use("wait", {"seconds": 3600}, f"t4{k}") for k in range(5)]),  # five hours, an hour a call (ops.MAX_WAIT_SECONDS)
        reply(tool_use("submit", {"answer": "Started apixaban for the PE."}, "t5")),
    )  # fmt: skip
    claude_tools.run_episode(EPREnv("mgmt-sob-001"), tool, out_dir=root)
    # an agent CLI over the API, printing Claude Code's event stream, on a task with an answer form
    script = root.parent / "stream_agent.py"
    script.write_text(STREAM_AGENT)
    cli_agent.run_episode(TASK, [sys.executable, "-S", str(script), "{prompt}", json.dumps(LOOK), json.dumps(SUBMIT)], out_dir=root, allow_unsandboxed=True)
    # the browser, from screenshots: the first page, a screenshot, then a click through to a patient
    with claude_browser.serve_ui(TASK) as (url, _), __import__("playwright.sync_api").sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        page = chromium.new_page(viewport=claude_browser.VIEWPORT)
        page.goto(url)
        spot = centre(page, 'a[href="/patients/1"]')
        chromium.close()
    ui = ScriptedClient(reply(tool_use("screenshot", {}, "u1"), tool_use("left_click", {"coordinate": spot}, "u2"), tool_use("screenshot", {}, "u3")),
                        reply(text("Hb 109 g/L."), stop_reason="end_turn"))  # fmt: skip
    browser(lambda: claude_browser.run_episode("find-result-001", ui, out_dir=root))
    # a person's session under their own name
    person = TASK.model_copy(update={"actor": "Dr J. Human (FY1)"})

    def work(url, app):
        import httpx

        httpx.get(f"{url}/ice/patients/1", params={"tab": "results", "result_id": RESULT})
        httpx.post(f"{url}/patients/1/notes", data={"kind": "progress", "title": "Hb review", "body": "Hb 109 g/L, no transfusion."})
        httpx.get(f"{url}/patients/1", params={"tab": "notes"})
        submit_reply(url, FORM)

    human_session(person, "ui", root, port=0, on_ready=work, poll=0.01)
    return root


def submit_reply(url, form):
    """The reply page's two steps: review what was entered, then confirm."""
    import httpx

    httpx.post(f"{url}/reply", data=form)
    httpx.post(f"{url}/reply/confirm", data=form)


def build(evidence, tmp_path, **kw):
    out = tmp_path / "view" / "episodes.html"
    built = viewer.build([evidence], out, **kw)
    html = out.read_text()
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1).replace("<\\/", "</"))
    return out, html, data, built


def test_every_action_result_screenshot_and_outcome_is_in_the_page(evidence, tmp_path):
    out, html, data, built = build(evidence, tmp_path)
    dirs = viewer.find([evidence])
    assert built["episodes"] == len(dirs) == 4 and {r["interface"] for r in data["rows"]} == {"tool", "api", "ui"}
    assert "<script src" not in html and "<link" not in html and "http://" not in html.split('id="data"')[0]  # nothing from the network
    for d in dirs:
        ep = next(e for e in data["episodes"] if e["id"] == episode_id(d.name))
        actions = [i for i in flat(ep["timeline"]) if i["type"] == "action"]
        recorded = [e for e in lines(d / "transcript.jsonl") if e["kind"] == "action"]
        assert [(a["n"], a["name"]) for a in actions if a["n"] is not None] == [(e["action"], e["name"]) for e in recorded if e["action"] is not None]
        task = json.loads((d / "task.json").read_text())
        shown = {o["id"]: o for a in actions for o in a["ops"]}
        for row in lines(d / "audit.jsonl"):
            if row["actor"] == task["actor"]:
                assert shown[row["id"]]["result"] == row["result"] and shown[row["id"]]["op"] == row["op"], row
        for e in recorded:
            for shot in viewer._shots(e["result"]):
                assert any(shot.split("/")[-1] in s for a in actions for s in a["screenshots"])
        report = json.loads((d / "report.json").read_text())
        o = ep["outcome"]
        assert o["tier"] == report["outcome"]["tier"] and o["pathway"] == report["outcome"]["pathway"] and o["harms"] == report["outcome"]["harms"]
        assert [c["id"] for c in ep["checks"]] == [c["id"] for c in report["checks"]] and o["safety_findings"] == report["outcome"]["safety_findings"]
        assert ep["timeline"][-1]["type"] == "end" and ep["timeline"][-1]["answer"] == json.loads((d / "result.json").read_text())["answer"]
    # screenshots are linked relative to the page, and the links resolve
    links = [s for e in data["episodes"] for i in e["timeline"] for s in i.get("screenshots", [])]
    assert links and all((out.parent / s).is_file() for s in links)


def test_the_timeline_reads_as_it_happened(evidence, tmp_path):
    _, _, data, _ = build(evidence, tmp_path)
    by_task = {e["task"]["id"]: e for e in data["episodes"] if e["who"].get("harness") != "human"}
    tool = by_task["mgmt-sob-001"]["timeline"]
    kinds = [i["type"] for i in tool]
    assert kinds[0] == "model" and tool[0]["text"] == ["Finding her."] and tool[0]["reasoning"] == ["Likely PE; confirm the patient first."]
    rx, note = (next(i for i in tool if i.get("name") == n) for n in ("prescribe_medication", "write_note"))
    assert rx["writes"] and not rx["wrong_patient"] and note["writes"] and note["wrong_patient"] and note["patients"] == [2]
    wait = next(k for k, i in enumerate(tool) if i.get("name") == "wait")
    events = [i for i in tool[wait + 1 :] if i["type"] == "event"]  # the ward's response, after the clock passed it
    assert any(e["kind"] == "staff response" and e["name"] == "anticoagulated" for e in events)
    assert any("Apixaban" in w and "given by" in w for e in events for w in e["what"])
    patients = by_task["mgmt-sob-001"]["patients"]
    assert patients["1"]["mrn"] == "H4417302" and patients["2"]["mrn"] == "H4471302" and by_task["mgmt-sob-001"]["task_patients"] == [1]

    api = by_task[TASK.id]["timeline"]
    # each call under the command that made it, as the server saw it arrive: two get_patient commands told apart
    assert [i["type"] for i in api][:10] == ["model", "command", "action", "model", "command", "action", "model", "model", "command", "action"]
    assert api[2]["args"] == {"patient_id": 2} and api[5]["args"] == {"patient_id": 1} and api[9]["name"] == "get_result"
    assert api[6]["reasoning"] == ["The FBC from day 1 is what is needed."]
    sub = next(i for i in api if i["type"] == "submission")
    assert sub["accepted"] and sub["answer"]["fields"]["cause"] == "Post-operative blood loss" and api[-1]["closing_message"] == "Submitted the form."

    ui = by_task["find-result-001"]["timeline"]
    first = next(i for i in ui if i["type"] == "action")
    assert first["name"] == "page loaded before the first action" and {o["op"] for o in first["ops"]} >= {"list_locations"}
    click = next(i for i in ui if i.get("name") == "left_click")
    assert "get_patient" in {o["op"] for o in click["ops"]} and click["patients"] == [1]

    human = next(e for e in data["episodes"] if e["who"]["harness"] == "human")
    assert [i["name"] for i in human["timeline"] if i["type"] == "action"] == [
        "GET /ice/patients/1", "POST /patients/1/notes", "GET /patients/1", "POST /reply", "POST /reply/confirm"]
    assert human["outcome"]["tier"] == "complete"


# untreated-deterioration did not fire (she was anticoagulated); unreferred-decline did (another patient, during the wait)
SECRETS = ["untreated-deterioration", "treatment-anticoagulation", "agent_op", "agent_write", "answer_regex", '"equals"',
           '"sql"', "SELECT "]  # fmt: skip


def test_hidden_material_is_not_in_a_public_or_blind_page(evidence, tmp_path):
    _, private, _, _ = build(evidence, tmp_path)
    assert "agent_op" in private and '"equals"' in private  # the clinician's own page shows how each check works
    assert "untreated-deterioration" not in private and "treatment-anticoagulation" not in private  # unfired rules and regimens never appear
    for flags in ({"public": True}, {"blind": True}):
        out, html, data, built = build(evidence, tmp_path / next(iter(flags)), **flags)
        for secret in SECRETS:
            assert secret not in html, (flags, secret)
        assert str(evidence) not in html
        if flags.get("blind"):
            for name in ("claude", "Claude", "anthropic", "opus", "claude_tools", "cli_agent", "claude_browser", "Dr J. Human", "stream_agent"):
                assert name not in html, name
            key = json.loads(Path(built["key"]).read_text())
            assert len(set(key["codes"].values())) == 4 and set(key["episodes"]) == {r["id"] for r in data["rows"]}
            assert {r["who"] for r in data["rows"]} == set(key["codes"].values())
            again = viewer.build([evidence], out, blind=True)  # the same key gives the same codes
            assert {r["who"] for r in again["rows"]} == set(key["codes"].values())
            links = [s for e in data["episodes"] for i in e["timeline"] for s in i.get("screenshots", [])]
            assert links and all(s.startswith("episodes_files/") and (out.parent / s).is_file() for s in links)


def test_reviews_round_trip_through_the_page(evidence, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    out, _, data, _ = build(evidence, tmp_path)
    target = next(r for r in data["rows"] if r["task_id"] == "mgmt-sob-001")
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        page = chromium.new_page(viewport={"width": 390, "height": 844})  # a phone
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(out.as_uri())
        assert page.locator("#index tbody tr").count() == 4
        page.select_option('select[data-k="interface"]', "tool")
        assert page.locator("#index tbody tr").count() == 1
        page.click(f'#index tbody tr[data-id="{target["id"]}"]')
        page.wait_for_selector("#detail:not([hidden]) .review")  # the episode opens on the hash change
        assert page.locator("text=WRONG PATIENT").count() >= 1 and page.locator("text=staff response").count() >= 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")  # readable at phone width
        page.check('input[name="verdict"][value="disagree"]')
        page.check('input[name="mytier"][value="harmful"]')
        page.fill("#comment", "The note on the wrong patient is the harm.")
        page.check('.check-toggle[data-check="right_patient_only"]')
        with page.expect_download() as download:
            page.click("#export")
        exported = json.loads(Path(download.value.path()).read_text())
        chromium.close()
    assert not errors
    mine = exported["reviews"][target["id"]]
    assert exported["format"] == "eprbench-reviews/1" and mine["verdict"] == "disagree" and mine["tier"] == "harmful"
    assert mine["comment"] == "The note on the wrong patient is the harm." and mine["checks"] == {"right_patient_only": "disagree"}
    assert mine["automatic_tier"] == "harmful" and mine["task_id"] == "mgmt-sob-001"

    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(exported))
    with sync_api.sync_playwright() as p:  # a fresh browser imports the file
        chromium = p.chromium.launch()
        page = chromium.new_page()
        page.goto(out.as_uri())
        page.once("dialog", lambda d: d.accept())
        page.set_input_files("#import", str(path))
        page.goto(out.as_uri() + "#" + target["id"])
        page.wait_for_selector("#detail:not([hidden]) .review")
        assert page.is_checked('input[name="verdict"][value="disagree"]') and page.input_value("#comment").startswith("The note")
        chromium.close()

    from eprbench.__main__ import main

    sys.argv = ["eprbench", "view", str(evidence), "--out", str(tmp_path / "merged.html"), "--reviews", str(path)]
    try:
        main()
    finally:
        sys.argv = sys.argv[:1]
    merged = (tmp_path / "merged.html").read_text()
    assert "The note on the wrong patient is the harm." in merged  # the reviews travel with the page


# --- the independent review's findings -----------------------------------------------------------------------------


def one(evidence, tmp_path, pick) -> Path:
    """A copy of one saved episode, to change without touching the others."""
    import shutil

    d = next(d for d in viewer.find([evidence]) if pick(json.loads((d / "result.json").read_text())))
    dest = tmp_path / "copy" / d.name
    shutil.copytree(d, dest)
    return dest.parent


def chromium_page(sync_api, p, out, errors):
    try:
        chromium = p.chromium.launch()
    except Exception as e:
        pytest.skip(f"chromium unavailable: {e}")
    page = chromium.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (errors.append("dialog: " + d.message), d.accept()))
    page.goto(out.as_uri())
    return chromium, page


def test_review_2_public_and_blind_pages_never_name_a_real_person(evidence, tmp_path):
    _, private, _, _ = build(evidence, tmp_path)
    assert "Dr J. Human (FY1)" in private  # the clinician's own page may
    for flags in ({"public": True}, {"blind": True}):
        out, html, data, built = build(evidence, tmp_path / next(iter(flags)), **flags)
        assert "Dr J. Human" not in html and "J. Human" not in html, flags
        human = next(e for e in data["episodes"] if e["task"]["actor"].startswith("Clinician "))
        code = human["task"]["actor"]
        assert any(code in json.dumps(o["result"]) for i in human["timeline"] if i["type"] == "action" for o in i["ops"])  # authorship in EPR content
        key = json.loads(Path(built["key"]).read_text())
        assert key["people"] == {"Dr J. Human (FY1)": code}
        assert "screenshots of a person's session are never taken" in html


def test_review_3_imported_reviews_are_kept_to_the_page_and_sanitised(evidence, tmp_path):
    _, _, data, _ = build(evidence, tmp_path)
    target = next(r for r in data["rows"] if r["task_id"] == "mgmt-sob-001")["id"]
    leaky = {"format": "eprbench-reviews/1", "reviews": {
        target: {"task_id": "mgmt-sob-001", "automatic_tier": "harmful", "who": "anthropic/claude-opus-5-5", "model": "claude-opus-5-5", "verdict": "agree",
                 "tier": None, "checks": {}, "updated_at": "2026-11-15T20:00:00Z",
                 "comment": "claude-opus-5-5 did this; see /Users/someone/evidence/run-1 and key sk-abcdefghijklmnopqrstuvwx"},
        "000000000000": {"task_id": "elsewhere", "comment": "an episode not on this page", "verdict": "agree"}}}  # fmt: skip
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(leaky))
    _, html, data, _ = build(evidence, tmp_path / "blind", blind=True, reviews=str(path))
    assert set(data["reviews"]) == {target} and "an episode not on this page" not in html
    for leak in ("claude-opus-5-5", "/Users/someone", "sk-abcdefghijklmnopqrstuvwx"):
        assert leak not in html, leak
    assert "model" not in data["reviews"][target]  # only the fields a review has
    assert data["reviews"][target]["verdict"] == "agree" and data["reviews"][target]["who"] == next(r["who"] for r in data["rows"] if r["id"] == target)
    _, html, data, _ = build(evidence, tmp_path / "public", public=True, reviews=str(path))
    assert data["reviews"][target]["comment"] == "" and "did this" not in html
    _, _, data, _ = build(evidence, tmp_path / "public2", public=True, reviews=str(path), include_review_comments=True)
    assert "did this" in data["reviews"][target]["comment"] and "/Users/someone" not in data["reviews"][target]["comment"]


NASTY = "</script><script>window.__pwned=1</script><!-- <img src=x onerror=\"window.__pwned=2\"> \"quoted\" 'single'    ‮RTL 💉 <\\/ </SCRIPT >"


def test_review_4_hostile_text_cannot_break_or_script_the_page(evidence, tmp_path):
    import sqlite3

    sync_api = pytest.importorskip("playwright.sync_api")
    root = one(evidence, tmp_path, lambda r: r["task_id"] == "mgmt-sob-001")
    d = next(root.iterdir())
    events = lines(d / "transcript.jsonl")
    for e in events:
        if e["kind"] == "response":
            e["body"]["content"].insert(0, {"type": "text", "text": "model says " + NASTY})
    (d / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    result = json.loads((d / "result.json").read_text())
    (d / "result.json").write_text(json.dumps({**result, "answer": "answer " + NASTY}))
    audit = lines(d / "audit.jsonl")
    audit[0]["result"] = {"note": "EPR text " + NASTY}
    (d / "audit.jsonl").write_text("\n".join(json.dumps(a) for a in audit) + "\n")
    db = sqlite3.connect(d / "final.sqlite")  # a saved world is an SQLite image: change a patient's name in it
    db.execute("UPDATE patient SET family_name = ? WHERE id = 1", ("Ellison" + NASTY,))
    db.commit()
    db.close()
    out = tmp_path / "nasty.html"
    viewer.build([root], out)
    errors = []
    with sync_api.sync_playwright() as p:
        chromium, page = chromium_page(sync_api, p, out, errors)
        assert page.locator("#index tbody tr").count() == 1
        page.click("#index tbody tr")
        page.wait_for_selector("#detail:not([hidden]) .review")
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        shown = page.inner_text("#detail")
        assert page.evaluate("window.__pwned") is None and not errors, errors
        assert "model says </script><script>window.__pwned=1</script>" in shown and "answer </script>" in shown and "EPR text </script>" in shown
        assert "ELLISON</SCRIPT>" in shown.upper().replace(" ", "")
        chromium.close()


def test_review_9_a_hostile_or_malformed_reviews_file_is_refused_whole(evidence, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    out, _, data, _ = build(evidence, tmp_path)
    target = data["rows"][0]["id"]
    files = {
        "proto": {"format": "eprbench-reviews/1", "reviews": {"__proto__": {"verdict": "agree", "polluted": 1}, target: {"verdict": "agree"}}},
        "nested": {"format": "eprbench-reviews/1", "reviews": {target: {"verdict": "agree", "checks": {"constructor": "disagree"}}}},
        "badtype": {"format": "eprbench-reviews/1", "reviews": {target: {"verdict": "agree"}, "x": {"verdict": 5}}},
        "nulls": {"format": "eprbench-reviews/1", "reviews": {target: {"verdict": "disagree", "tier": None, "comment": None, "checks": None}, "y": None}},
    }
    errors = []
    with sync_api.sync_playwright() as p:
        chromium, page = chromium_page(sync_api, p, out, errors)
        for name, content in files.items():
            path = tmp_path / f"{name}.json"
            path.write_text(json.dumps(content))
            page.set_input_files("#import", str(path))
            page.wait_for_timeout(100)
        assert page.evaluate("({}).polluted === undefined && Object.prototype.verdict === undefined")
        stored = page.evaluate("JSON.parse(localStorage.getItem(STORE) || '{}')")
        chromium.close()
    refusals = [e for e in errors if e.startswith("dialog: Could not import")]
    assert len(refusals) == 3 and not [e for e in errors if not e.startswith("dialog:")], errors
    assert list(stored) == [target] and stored[target]["verdict"] == "disagree"  # only the clean file applied; its null record skipped


def test_readiness_reviews_imported_or_stored_in_the_browser_are_sanitised_and_kept_to_their_page(evidence, tmp_path):
    """Readiness review: the build sanitised reviews loaded with --reviews, but a review imported in the browser, or
    left in its storage by another page, kept raw agent names, comments and other episodes on a blind or public page."""
    sync_api = pytest.importorskip("playwright.sync_api")
    private, _, data, _ = build(evidence, tmp_path / "private")
    blind, _, blind_data, _ = build(evidence, tmp_path / "blind", blind=True)
    public, _, _, _ = build(evidence, tmp_path / "public", public=True)
    with_comments, _, _, _ = build(evidence, tmp_path / "with", blind=True, include_review_comments=True)
    target = next(r for r in data["rows"] if r["task_id"] == "mgmt-sob-001")["id"]
    label = next(r["who"] for r in blind_data["rows"] if r["id"] == target)
    comment = ("Dr Q. Hartwell-Ngata (my registrar) agrees: claude-opus-5-5 did this; see /Users/someone/evidence/run-1 and key "
               "sk-abcdefghijklmnopqrstuvwx <img src=x onerror=\"window.__pwned=1\">")  # a person's name the browser could never know to replace
    leaky = {"format": "eprbench-reviews/1", "reviews": {
        target: {"task_id": "mgmt-sob-001", "automatic_tier": "harmful", "who": "anthropic/claude-opus-5-5", "interface": "tool", "verdict": "agree",
                 "tier": "partial", "checks": {"right_patient_only": "disagree", "made-up check by gpt-6": "disagree"}, "updated_at": "openai 2026", "comment": comment},
        "000000000000": {"task_id": "elsewhere", "comment": "an episode not on this page", "verdict": "agree"}}}  # fmt: skip
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(leaky))
    leaks = ("Hartwell", "claude", "openai", "gpt-6", "/Users/someone", "sk-abcdefghijklmnopqrstuvwx", "000000000000", "an episode not on this page")
    errors = []
    with sync_api.sync_playwright() as p:
        chromium, page = chromium_page(sync_api, p, private, errors)  # one browser: every file:// page shares its storage
        page.set_input_files("#import", str(path))  # the clinician's own page keeps what they imported
        page.wait_for_timeout(100)
        assert page.evaluate("Object.keys(REVIEWS).length") == 2
        page.goto(private.as_uri() + "#" + target)
        page.wait_for_selector("#detail:not([hidden]) .review")
        private_shown = page.input_value("#comment")
        page.goto(blind.as_uri())
        assert page.evaluate("Object.keys(REVIEWS).length") == 0  # nothing from another page's storage
        page.set_input_files("#import", str(path))
        page.wait_for_timeout(100)
        mine = page.evaluate(f"REVIEWS[{json.dumps(target)}]")
        stored = page.evaluate("localStorage.getItem(STORE)")
        with page.expect_download() as download:
            page.click("#export")
        exported = Path(download.value.path()).read_text()
        page.goto(blind.as_uri() + "#" + target)
        page.wait_for_selector("#detail:not([hidden]) .review")
        shown = page.input_value("#comment")
        page.fill("#comment", "My own note on this episode.")  # what the reviewer types on the blind page is theirs to keep
        page.reload()
        page.wait_for_selector("#detail:not([hidden]) .review")
        typed = page.input_value("#comment")
        page.goto(with_comments.as_uri())
        page.set_input_files("#import", str(path))
        page.wait_for_timeout(100)
        kept = page.evaluate(f"REVIEWS[{json.dumps(target)}]")["comment"]
        page.goto(public.as_uri())
        page.set_input_files("#import", str(path))
        page.wait_for_timeout(100)
        public_review = page.evaluate(f"REVIEWS[{json.dumps(target)}]")
        page.goto(public.as_uri() + "#" + target)
        page.wait_for_selector("#detail:not([hidden]) .review")
        comment_box = page.is_disabled("#comment")
        assert page.evaluate("window.__pwned") is None
        chromium.close()
    assert not [e for e in errors if not e.startswith("dialog: Imported")], errors
    for text in (json.dumps(mine), stored, exported, shown):
        for leak in leaks:
            assert leak not in text, (leak, text)
    assert mine["who"] == label and mine["interface"] == "tool" and mine["verdict"] == "agree" and mine["tier"] == "partial"
    assert mine["checks"] == {"right_patient_only": "disagree"} and mine["updated_at"] == ""
    assert mine["comment"] == "" and shown == ""  # an imported comment is dropped whole on a blind page: no partial scrubbing
    assert list(json.loads(exported)["reviews"]) == [target] and typed == "My own note on this episode."
    assert private_shown == comment  # the clinician's own page shows imported text as text, never as HTML
    assert kept == comment  # a page built with reviewers' comments keeps them as given
    assert public_review["comment"] == "" and comment_box  # a public page keeps no comments unless built to


def png_with_text(data: bytes, text: bytes) -> bytes:
    import struct
    import zlib

    chunk = b"tEXt" + b"Software\x00" + text
    extra = struct.pack(">I", len(chunk) - 4) + chunk + struct.pack(">I", zlib.crc32(chunk))
    return data[:33] + extra + data[33:]  # after the signature and IHDR


def test_review_10_blind_screenshots_carry_no_metadata_or_original_names(evidence, tmp_path):
    root = one(evidence, tmp_path, lambda r: r["interface"] == "ui" and r.get("harness") == "claude_browser")
    d = next(root.iterdir())
    for shot in (d / "screenshots").iterdir():
        shot.write_bytes(png_with_text(shot.read_bytes(), b"claude-opus-5-5 via anthropic"))
    out = tmp_path / "blind" / "page.html"
    viewer.build([root], out, blind=True)
    files = sorted((out.parent / "page_files").rglob("*.png"))
    assert files and all(f.name.split(".")[0].isdigit() and len(f.name) == 7 for f in files)  # 001.png, ...
    for f in files:
        data = f.read_bytes()
        assert data.startswith(b"\x89PNG") and data.endswith(b"IEND\xaeB`\x82") and b"tEXt" not in data and b"claude" not in data
    viewer.build([root], tmp_path / "embedded.html", blind=True, embed=True)
    import base64

    for b64 in re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", (tmp_path / "embedded.html").read_text()):
        assert b"claude" not in base64.b64decode(b64)


def test_a_regraded_page_shows_the_current_marking_and_says_when_the_tier_changed(evidence, tmp_path):
    """eprbench view --regrade: each outcome is the saved world graded again with the current packaged task (as
    eprbench regrade), and a tier that differs from the one recorded when the episode ran is stated in the timeline."""
    import shutil

    copy = tmp_path / "evidence"
    shutil.copytree(evidence, copy)
    d = next(d for d in viewer.find([copy]) if json.loads((d / "result.json").read_text()).get("task_id") == "mgmt-sob-001")  # a packaged task
    report = json.loads((d / "report.json").read_text())
    current = report["outcome"]["tier"]
    report["outcome"]["tier"] = "harmful" if current != "harmful" else "complete"  # as if the marking had changed since
    (d / "report.json").write_text(json.dumps(report))
    _, _, plain, _ = build(copy, tmp_path / "plain")
    _, _, data, _ = build(copy, tmp_path / "regraded", regrade=True, public=True)
    by_id = {e["id"]: e for e in data["episodes"]}
    assert all(e["regraded"]["tier"] == e["outcome"]["tier"] for e in data["episodes"])
    changed = by_id[episode_id(d.name)]
    assert changed["outcome"]["tier"] == current and changed["regraded"]["tier_when_run"] == report["outcome"]["tier"]
    notes = [i["text"] for i in flat(changed["timeline"]) if i["type"] == "note"]
    assert any(f"the tier was {report['outcome']['tier']} when the episode ran, and is {current} now" in n for n in notes)
    assert next(e for e in plain["episodes"] if e["id"] == changed["id"])["outcome"]["tier"] == report["outcome"]["tier"]  # without --regrade, as saved
    others = [e for e in data["episodes"] if e["id"] != changed["id"]]
    assert all(any("is the same as when the episode ran" in i.get("text", "") for i in flat(e["timeline"])) for e in others)
    assert all("predicate" not in c for e in data["episodes"] for c in e["task"]["checks"])  # still public: no definitions
