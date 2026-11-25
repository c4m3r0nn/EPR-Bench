"""Server-rendered web UI.

Two systems share one login, as in most NHS trusts: the patient record (EPR)
and a separate requesting and results system styled after ICE. Moving between
them loses patient context, so the patient has to be found again.

Pages are plain HTML forms and links with no client-side API calls, so an
agent driving a browser has no hidden JSON endpoint to fall back on. Every
page is built from the same operations the API exposes, tagged interface="ui"
in the audit log.

What a page shows the agent is decided by what reaches the agent's eyes, not by what the
server rendered. Each template tags every part it renders with the record entries it
carries (`tag`: the same derivation a tool or API read uses, ops.SHOWS), and the page
carries its render id; rendering records the entries as `rendered` only, which no check
counts as read. A browser harness, at every screenshot it sends the model, reports the
tags then inside the viewport (agents/claude_browser.screen_reads) through
`app.state.record_seen`, a call in the harness's own process that nothing in the browser
can reach; those are recorded as read (`screenshot`), with an action id of their own. In
a person's session (`reads="person"`) nobody reports the screen, so what a page rendered
for the person is recorded as read (`person`).

When the server has a task, the header of every page links to "Handover / reply": the instruction, the task's
answer form (one control per field, as eprbench.answers describes) and a free-text reply, submitted once.
"""

from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from eprbench import answers, ops, sections
from eprbench.world import iso

# The patient menu: one tab per section of the record (eprbench.sections, which the API listing shares), then the
# requesting system. It is at the top of every patient page, all of it on the first screen.
TABS = sections.TABS
ICE_TABS = ["request", "requests", "results", "cumulative", "blood"]
MED_FIELDS = ("drug", "product", "dose_value", "dose_unit", "route", "frequency", "frequency_other", "frequency_detail", "kind", "duration", "indication")
FREQUENCY_CHOICES = ["once", "OD", "OM", "ON", "BD", "TDS", "QDS", "every 4 hours", "every 6 hours", "every 8 hours", "every 12 hours", "once weekly"]
# every unit the API accepts, in the plural where it has one ("2 tablets"; the API takes "1 tablet" too)
UNIT_CHOICES = [u for u in ops.DOSE_UNITS if not {f"{u}s", f"{u}es", f"{u[:-1]}ies"} & set(ops.DOSE_UNITS)]
MONITOR_MINUTES = (10, 30, 60)
# The header's Wait control: fixed buttons, so no slip of the keyboard can move the clock by hours (a minutes box once
# read 515 after "5" was typed into its prefilled "15"). The largest is the wait operation's maximum per call.
WAIT_MINUTES = (1, 5, 15, 30, 60)
RENDERS_KEPT = 64  # pages whose tags a screenshot may still report
# Operation fields no page or form offers, and why that does not let an API agent do or see more.
# A test drives every page and form and fails on any field that is neither given by the UI nor listed here.
NOT_IN_UI = {
    ("get_current_time", None): "No capability difference: the simulated time is shown on every page",
    ("wait", "seconds"): "Capability difference: the UI waits 1, 5, 15, 30 or 60 minutes a press; the API any number of seconds up to 60 minutes a call",
    ("list_notes", "encounter_id"): "No capability difference: the notes tab lists every note; the filter only narrows the list",
    ("list_results", "category"): "No capability difference: the results tab lists every result; the filter only narrows the list",
    ("get_live_vitals", "interval_seconds"): "Capability difference: the API can sample the monitor trend at any spacing down to one second; the UI shows one-minute samples",
    ("list_location_patients", "location_code"): "No capability difference: the front page lists the patients of every ward",
    ("list_observations", "limit"): "No capability difference: the UI always asks for the most the API allows (200)",
    ("list_results", "limit"): "No capability difference: the UI always asks for the most the API allows (200)",
    ("list_medications", "include_stopped"): "No capability difference: the drug chart always shows stopped medicines as well",
}
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["num"] = lambda v: str(int(v)) if v == int(v) else str(round(v, 3))
templates.env.filters["pairs"] = lambda values: [(v, v) for v in values]  # radio buttons whose value is their label


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {k: v[0].strip() for k, v in parse_qs(body, keep_blank_values=True).items()}


def build_ui_router(app: FastAPI, actor: str) -> APIRouter:
    router = APIRouter()
    ctx = ops.agent_ctx(actor, "ui")

    def run(name: str, **args):
        return ops.call(app.state.world, name, args, ctx)

    app.state.renders = {}  # render id (the page request's action) -> tag -> the entries that tagged part carries

    def register(entries: list) -> Markup:
        if not entries:
            return Markup("")
        world, render = app.state.world, ops.ACTION.get()
        page = app.state.renders.setdefault(render, {})
        token = str(len(page) + 1)
        page[token] = entries
        while len(app.state.renders) > RENDERS_KEPT:
            app.state.renders.pop(next(iter(app.state.renders)))
        ops.record_entries(world, actor, "ui", render, entries, "person" if app.state.reads == "person" else "rendered")
        return Markup(f' data-read="{token}"')

    def tag(name: str, out: dict | None, patient_id: int | None, *kinds: str) -> Markup:
        """An attribute for the element a template renders operation `name`'s output in (all its entries, or those of
        `kinds`): ` data-read="N"`, N naming those entries for this page."""
        return register(ops.shown_entries(app.state.world, name, out, patient_id, kinds) if out else [])

    def tag_values(result_id: int, analyte: str, patient_id: int) -> Markup:
        """An attribute for one row of values: the result items of that analyte in that result."""
        rows = app.state.world.q("SELECT id FROM result_item WHERE result_id = ? AND analyte = ? ORDER BY id", (result_id, analyte))
        return register([("result_value", r["id"], patient_id) for r in rows])

    def record_seen(render, tokens) -> list:
        """For a browser harness only: the tags of page `render` that were inside a screenshot sent to the model. Their
        entries are recorded as read, as one action of their own. Returns them."""
        try:
            page = app.state.renders.get(int(render)) or {}
        except (TypeError, ValueError):
            return []
        entries = list(dict.fromkeys(e for t in tokens for e in page.get(str(t), [])))
        if entries:
            world = app.state.world
            ops.record_entries(world, actor, "ui", ops.claim_action_id(world), entries, "screenshot")
        return entries

    app.state.record_seen = record_seen

    def base_context() -> dict:
        return {"now": iso(app.state.world.now), "actor": actor, "reply": app.state.task is not None, "tag": tag, "tag_values": tag_values,
                "render_id": ops.ACTION.get(), "wait_minutes": WAIT_MINUTES}  # fmt: skip

    def back(base: str, pid: int, tab: str, error: str | None = None) -> RedirectResponse:
        params = {"tab": tab, **({"error": error} if error else {})}
        return RedirectResponse(f"{base}/patients/{pid}?{urlencode(params)}", status_code=303)

    def submit(base: str, pid: int, tab: str, name: str, **args) -> RedirectResponse:
        try:
            run(name, **args)
        except ops.OpError as e:
            return back(base, pid, tab, error=e.message)
        return back(base, pid, tab)

    def owned(context: dict, name: str, **args) -> dict | None:
        """An object opened by id within a patient's record: another patient's is refused, never shown under this banner."""
        try:
            return run(name, **args)
        except ops.OpError as e:
            if e.code not in ("wrong_patient", "not_found"):
                raise
            context["error"] = e.message
            return None

    def not_found(request: Request, e: ops.OpError, template: str):
        return templates.TemplateResponse(request, template, {**base_context(), "message": e.message}, status_code=404)

    def prescribing_step(qp) -> dict:
        """Prescribe card: search, then choose the drug, then the product, then complete the order."""
        query, drug, product = qp.get("rx_q", ""), qp.get("rx_drug", ""), qp.get("rx_product", "")
        step = {"rx_q": query, "rx_drug": drug, "rx_product": product, "draft": {k: qp.get(k, "") for k in MED_FIELDS}}
        if drug and product:
            found = run("search_formulary", query=product)["products"].get(drug, [])
            chosen = next((p for p in found if p["name"].lower() == product.lower()), None)
            if chosen:
                return step | {"rx_step": "form", "chosen": chosen, "frequencies": FREQUENCY_CHOICES, "units": UNIT_CHOICES}
        if drug:
            products = run("search_formulary", query=drug)["products"].get(drug)
            if products:
                return step | {"rx_step": "product", "products": products}
            return step | {"rx_step": "form", "chosen": None, "frequencies": FREQUENCY_CHOICES, "units": UNIT_CHOICES}
        if len(query.strip()) >= 2:
            return step | {"rx_step": "drug", "drugs": run("search_formulary", query=query)["drugs"]}
        return step | {"rx_step": "search"}

    def search(q: str) -> dict:
        matches = run("search_patients", query=q)["patients"] if len(q.strip()) >= 2 else None
        return {**base_context(), "q": q, "matches": matches}

    # --- patient record ------------------------------------------------------

    @router.get("/")
    def home(request: Request, q: str = ""):
        context = search(q)
        context["locations"] = [
            {**loc, "patients": run("list_location_patients", location_code=loc["code"])["patients"]}
            for loc in run("list_locations")["locations"]
        ]
        return templates.TemplateResponse(request, "home.html", context)

    @router.get("/patients/{pid}")
    def patient(request: Request, pid: int, tab: str = "summary", note_id: int = 0, fluid_hours: int = 24, monitor_minutes: int = 10, error: str = ""):
        if tab not in TABS:
            tab = "summary"
        try:
            context = {**base_context(), "p": run("get_patient", patient_id=pid), "tab": tab, "tabs": TABS, "menu": sections.PATIENT_MENU,
                       "section": sections.BY_TAB[tab], "error": error}  # fmt: skip
            qp = request.query_params
            if tab == "summary":
                context["encounters"] = run("list_encounters", patient_id=pid)["encounters"]
            elif tab == "referrals":
                context["referrals"] = run("list_referrals", patient_id=pid)["referrals"]
                context["pathways"] = run("list_referral_pathways")["pathways"]
                context["pathway"] = next((w for w in context["pathways"] if w["name"] == qp.get("referral_pathway")), None)
            elif tab == "assessments":
                context["assessments"] = run("list_assessments", patient_id=pid)["assessments"]
                context["forms"] = run("list_assessment_forms")["forms"]
                context["form"] = next((f for f in context["forms"] if f["name"] == qp.get("assessment_form")), None)
            elif tab == "jobs":
                context["jobs"] = [j for j in run("list_jobs", include_done=True)["jobs"] if j["patient_id"] == pid]
                context["requests"] = [o for o in run("list_orders", patient_id=pid)["orders"] if o["status"] != "cancelled"]
                context["job_types"] = ops.JOB_TYPES
            elif tab == "notes":
                context["notes"] = run("list_notes", patient_id=pid)["notes"]
                context["note"] = owned(context, "get_note", note_id=note_id, patient_id=pid) if note_id else None
            elif tab == "observations":
                context["obs_sets"] = run("list_observations", patient_id=pid, limit=200)["observation_sets"]
            elif tab == "fluids":
                context["fluids"] = run("get_fluid_balance", patient_id=pid, hours=min(max(fluid_hours, 1), 72))
            elif tab == "medications":
                context["medications"] = run("list_medications", patient_id=pid, include_stopped=True)["medications"]
                context |= prescribing_step(request.query_params)
            elif tab == "monitor":
                try:
                    minutes = monitor_minutes if monitor_minutes in MONITOR_MINUTES else MONITOR_MINUTES[0]
                    context |= {"monitor_minutes": minutes, "monitor_choices": MONITOR_MINUTES}
                    context["monitor"] = run("get_live_vitals", patient_id=pid, history_seconds=minutes * 60)
                except ops.OpError as e:
                    context["monitor"] = None
                    context["monitor_message"] = e.message
        except ops.OpError as e:
            return not_found(request, e, "error.html")
        return templates.TemplateResponse(request, "patient.html", context)

    @router.post("/patients/{pid}/notes")
    async def new_note(request: Request, pid: int):
        f = await _form(request)
        kind = f.get("kind_other") if f.get("kind") == "other" else f.get("kind")
        fields = {"kind": kind or "progress", "title": f.get("title", ""), "body": f.get("body", ""), "status": f.get("status") or "final"}
        return submit("", pid, "notes", "write_note", patient_id=pid, **fields)

    @router.post("/patients/{pid}/notes/{note_id}")
    async def edit_note(request: Request, pid: int, note_id: int):
        """A draft's author saves it again, as a draft or final."""
        f = await _form(request)
        fields = {"title": f.get("title", ""), "body": f.get("body", ""), "status": f.get("status") or "draft"}
        try:
            run("update_note", note_id=note_id, patient_id=pid, **fields)
        except ops.OpError as e:
            return RedirectResponse(f"/patients/{pid}?{urlencode({'tab': 'notes', 'note_id': note_id, 'error': e.message})}", status_code=303)
        return RedirectResponse(f"/patients/{pid}?tab=notes&note_id={note_id}", status_code=303)

    @router.post("/patients/{pid}/observations")
    async def new_observations(request: Request, pid: int):
        f = await _form(request)
        values = {k: v for k, v in f.items() if v != ""}
        return submit("", pid, "observations", "record_observations", patient_id=pid, values=values)

    @router.post("/patients/{pid}/observations/{set_id}/error")
    async def observations_in_error(request: Request, pid: int, set_id: int):
        f = await _form(request)
        return submit("", pid, "observations", "mark_observations_in_error", patient_id=pid, observation_set_id=set_id, reason=f.get("reason", ""))

    @router.post("/patients/{pid}/medications")
    async def new_medication(request: Request, pid: int):
        f = await _form(request)
        draft = {k: f.get(k, "") for k in MED_FIELDS}
        dose = f.get("dose") or f"{draft['dose_value']} {draft['dose_unit']}".strip()
        frequency = draft["frequency_other"] or draft["frequency"]
        if draft["frequency_detail"]:
            frequency = f"{frequency} ({draft['frequency_detail']})"
        fields = {k: draft[k] for k in ("drug", "product", "route", "duration", "indication")} | {"kind": draft["kind"] or "regular", "dose": dose, "frequency": frequency}
        try:
            run("prescribe_medication", patient_id=pid, override_reason=f.get("override_reason", ""), **fields)
        except ops.OpError as e:
            # an alert sends the prescriber back to the same form with what they entered
            params = urlencode({"tab": "medications", "error": e.message, "rx_drug": draft["drug"], "rx_product": draft["product"], **draft})
            return RedirectResponse(f"/patients/{pid}?{params}", status_code=303)
        return back("", pid, "medications")

    @router.post("/patients/{pid}/medications/{medication_id}/administrations")
    async def new_administration(request: Request, pid: int, medication_id: int):
        f = await _form(request)
        fields = {"status": f.get("status", "given"), "dose": f.get("dose", ""), "note": f.get("note", "")}
        return submit("", pid, "medications", "record_administration", medication_id=medication_id, patient_id=pid, **fields)

    @router.post("/patients/{pid}/referrals")
    async def new_referral(request: Request, pid: int):
        """A general referral (a team typed in), or a pathway chosen from the list with its form (rp_ fields)."""
        f = await _form(request)
        fields = {"specialty": f.get("specialty", ""), "urgency": f.get("urgency", "routine"), "reason": f.get("reason", "")}
        if f.get("pathway"):
            fields |= {"pathway": f["pathway"], "pathway_details": {k[3:]: v for k, v in f.items() if k.startswith("rp_") and v != ""}}
        return submit("", pid, "referrals", "make_referral", patient_id=pid, **fields)

    @router.post("/patients/{pid}/problems")
    async def new_problem(request: Request, pid: int):
        f = await _form(request)
        return submit("", pid, "summary", "add_problem", patient_id=pid, display=f.get("display", ""), onset_date=f.get("onset_date") or None)

    @router.post("/patients/{pid}/allergies")
    async def new_allergy(request: Request, pid: int):
        f = await _form(request)
        fields = {"substance": f.get("substance", ""), "reaction": f.get("reaction", ""), "severity": f.get("severity") or None}
        return submit("", pid, "summary", "record_allergy", patient_id=pid, **fields)

    @router.post("/patients/{pid}/fluids")
    async def new_fluid(request: Request, pid: int):
        f = await _form(request)
        fields = {"direction": f.get("direction", ""), "kind": f.get("kind", ""), "volume_ml": f.get("volume_ml", "")}
        return submit("", pid, "fluids", "record_fluid", patient_id=pid, **fields)

    @router.post("/patients/{pid}/jobs")
    async def new_job(request: Request, pid: int):
        f = await _form(request)
        fields = {"description": f.get("description", ""), "priority": f.get("priority", "routine"), "task_type": f.get("task_type") or None,
                  "order_id": f.get("order_id") or None}  # fmt: skip
        try:
            run("add_job", patient_id=pid, **fields)
        except ops.OpError as e:
            return back("", pid, "jobs", error=e.message)
        return back("", pid, "jobs")

    @router.get("/jobs")
    def jobs(request: Request, all: int = 0, error: str = ""):
        context = {**base_context(), "jobs": run("list_jobs", include_done=bool(all))["jobs"], "error": error, "job_types": ops.JOB_TYPES}
        return templates.TemplateResponse(request, "jobs.html", context)

    def job_page(f: dict, pid: int, error: str | None = None) -> RedirectResponse:
        """Back to where a job was closed: the patient's Jobs tab (its forms say so) or the ward's jobs list."""
        if f.get("next") == "patient":
            return back("", pid, "jobs", error=error)
        return RedirectResponse("/jobs?" + urlencode({"error": error} if error else {"all": 1}), status_code=303)

    @router.post("/patients/{pid}/jobs/{job_id}/complete")
    async def finish_job(request: Request, pid: int, job_id: int):
        """From the ward-wide jobs list: each row's button names the patient shown on that row, as everywhere else."""
        f = await _form(request)
        try:
            run("complete_job", job_id=job_id, patient_id=pid, outcome=f.get("outcome", ""))
        except ops.OpError as e:
            return job_page(f, pid, e.message)
        return job_page(f, pid)

    @router.post("/patients/{pid}/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, pid: int, job_id: int):
        f = await _form(request)
        try:
            run("cancel_job", job_id=job_id, patient_id=pid, reason=f.get("reason", ""))
        except ops.OpError as e:
            return job_page(f, pid, e.message)
        return job_page(f, pid)

    @router.post("/patients/{pid}/assessments")
    async def new_assessment(request: Request, pid: int):
        """An assessment in words, or a structured form whose ticked boxes and choices come as af_ fields."""
        f = await _form(request)
        fields = {"kind": f.get("kind", ""), "outcome": f.get("outcome", ""), "details": f.get("details", "")}
        answers = {k[3:]: v for k, v in f.items() if k.startswith("af_") and v != ""}
        return submit("", pid, "assessments", "record_assessment", patient_id=pid, **fields, **({"answers": answers} if answers else {}))

    @router.post("/patients/{pid}/discharge")
    async def discharge(request: Request, pid: int):
        f = await _form(request)
        return submit("", pid, "discharge", "discharge_patient", patient_id=pid, destination=f.get("destination") or "Usual residence")

    @router.post("/patients/{pid}/medications/{medication_id}/stop")
    async def stop_medication(request: Request, pid: int, medication_id: int):
        f = await _form(request)
        return submit("", pid, "medications", "stop_medication", medication_id=medication_id, patient_id=pid, reason=f.get("reason", ""))

    # --- requesting and results (ICE) ----------------------------------------

    @router.get("/ice")
    def ice_home(request: Request, q: str = ""):
        return templates.TemplateResponse(request, "ice_home.html", search(q))

    @router.get("/ice/patients/{pid}")
    def ice_patient(request: Request, pid: int, tab: str = "request", result_id: int = 0, error: str = ""):
        if tab not in ICE_TABS:
            tab = "request"
        try:
            context = {**base_context(), "p": run("get_patient", patient_id=pid), "tab": tab, "tabs": ICE_TABS, "error": error}
            if tab == "request":
                context |= request_menu(request.query_params, pid)
            elif tab == "requests":
                context["orders"] = run("list_orders", patient_id=pid)["orders"]
            elif tab == "blood":
                context["blood_requests"] = run("list_blood_requests", patient_id=pid)["blood_requests"]
            elif tab == "cumulative":
                context["analytes"] = run("get_analyte_history", patient_id=pid)["analytes"]
                analyte = request.query_params.get("analyte", "")
                context["history"] = owned(context, "get_analyte_history", patient_id=pid, analyte=analyte) if analyte else None
            else:
                context["results"] = run("list_results", patient_id=pid, limit=200)["results"]
                context["result"] = owned(context, "get_result", result_id=result_id, patient_id=pid) if result_id else None
        except ops.OpError as e:
            return not_found(request, e, "ice_error.html")
        return templates.TemplateResponse(request, "ice_patient.html", context)

    def request_menu(qp, pid: int) -> dict:
        """The New request tab: category, then (for imaging) modality and body region, then the examination's form."""
        menu = {k: qp.get(k, "") for k in ("category", "modality", "region", "code")}
        if menu["code"]:
            form = run("get_request_form", patient_id=pid, orderable_code=menu["code"])
            item = run("list_orderables", query=menu["code"])["orderables"]
            item = next((o for o in item if o["code"] == menu["code"]), None)
            # an error or alert sends the requester back here; keep what they entered
            draft = {k: v for k, v in qp.items() if k.startswith("rf_") or k in ("priority", "clinical_details", "collection", "collect_at")}
            return {"menu": menu, "step": "form", "form": form, "item": item, "draft": draft}
        if menu["category"] == "imaging":
            if not menu["modality"]:
                return {"menu": menu, "step": "modality", "choices": run("browse_imaging_catalogue")["modalities"]}
            if not menu["region"]:
                return {"menu": menu, "step": "region", "choices": run("browse_imaging_catalogue", modality=menu["modality"])["regions"]}
            exams = run("browse_imaging_catalogue", modality=menu["modality"], region=menu["region"])["examinations"]
            return {"menu": menu, "step": "exam", "choices": exams}
        if menu["category"]:
            return {"menu": menu, "step": "exam", "choices": run("list_orderables", category=menu["category"])["orderables"]}
        return {"menu": menu, "step": "category"}

    @router.post("/ice/patients/{pid}/requests")
    async def new_order(request: Request, pid: int):
        f = await _form(request)
        code = f.get("orderable_code", "")
        answers = {k: v for k, v in f.items() if k.startswith("rf_") and v != ""}
        fields = {"orderable_code": code, "priority": f.get("priority", "routine"), "clinical_details": f.get("clinical_details", ""),
                  "collection": f.get("collection") or "now", "collect_at": f.get("collect_at", "")}  # fmt: skip
        try:
            run("place_order", patient_id=pid, override_reason=f.get("override_reason", ""), request_details={k[3:]: v for k, v in answers.items()}, **fields)
        except ops.OpError as e:
            params = urlencode({"tab": "request", "code": code, "error": e.message, **{k: v for k, v in fields.items() if k != "orderable_code"}, **answers})
            return RedirectResponse(f"/ice/patients/{pid}?{params}", status_code=303)
        return back("/ice", pid, "requests")

    @router.post("/ice/patients/{pid}/results/{result_id}/acknowledge")
    def acknowledge(pid: int, result_id: int):
        try:
            run("acknowledge_result", result_id=result_id, patient_id=pid)
        except ops.OpError as e:
            return back("/ice", pid, "results", error=e.message)
        return RedirectResponse(f"/ice/patients/{pid}?tab=results&result_id={result_id}", status_code=303)

    @router.post("/ice/patients/{pid}/blood")
    async def new_blood_request(request: Request, pid: int):
        f = await _form(request)
        fields = {"product": f.get("product", ""), "units": f.get("units") or "1", "urgency": f.get("urgency", "routine"), "indication": f.get("indication", "")}
        return submit("/ice", pid, "blood", "request_blood_product", patient_id=pid, **fields)

    @router.post("/ice/patients/{pid}/requests/{order_id}/cancel")
    def cancel_order(pid: int, order_id: int):
        return submit("/ice", pid, "requests", "cancel_order", order_id=order_id, patient_id=pid)

    # --- the answer ------------------------------------------------------------

    def reply_page(request: Request, draft: dict | None = None, problems: list[str] = (), stage: str = "form", answer=None, left: int | None = None):
        task = app.state.task
        context = {**base_context(), "task": task, "form": task.answer_form() or [], "submission": app.state.submission, "draft": draft or {},
                   "problems": problems, "stage": stage, "answer": answer, "left": left}  # fmt: skip
        return templates.TemplateResponse(request, "reply.html", context)

    def read_reply(f: dict):
        task = app.state.task
        answer = answers.from_form(task, f)
        return answer, answers.problems(task, answer) if answers.has_form(task) else []

    if app.state.task is not None:

        @router.get("/reply")
        def reply(request: Request):
            return reply_page(request)

        @router.post("/reply")
        async def review_reply(request: Request):
            """Step 1 of 2: what was entered, checked under the rule every route shares (answers.Desk). An incomplete or
            malformed reply is a refused attempt, shown back with what is wrong; otherwise (or on the last attempt) it
            is shown for confirmation, and nothing is submitted yet."""
            f = await _form(request)
            if app.state.submission is not None:
                return RedirectResponse("/reply", status_code=303)
            answer, found = read_reply(f)
            if found and not app.state.desk.last_attempt():
                out = app.state.submit(answer, found, "ui")
                return reply_page(request, f, out.get("problems", found), left=out.get("left"))
            return reply_page(request, f, found, stage="review", answer=answer)

        @router.post("/reply/confirm")
        async def confirm_reply(request: Request):
            """Step 2 of 2: the deliberate confirmation that submits the reply."""
            f = await _form(request)
            out = app.state.submit(*read_reply(f), "ui")
            if out["ok"] or out["error"] == "already_submitted":
                return RedirectResponse("/reply", status_code=303)
            return reply_page(request, f, out["problems"], left=out.get("left"))

        @router.post("/reply/edit")
        async def edit_reply(request: Request):
            return reply_page(request, await _form(request))

    # --- simulation control --------------------------------------------------

    @router.post("/clock/wait")
    async def wait(request: Request):
        f = await _form(request)
        try:
            run("wait", seconds=int(f.get("minutes") or 0) * 60)
        except (ops.OpError, ValueError):
            pass
        target = f.get("next", "")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        return RedirectResponse(target, status_code=303)

    return router
