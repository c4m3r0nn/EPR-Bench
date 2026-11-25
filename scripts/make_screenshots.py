"""Screenshots of the EPR's web UI for the README and the site (docs/img/), made from the packaged preview cases.

    uv run --with pillow==13.0.0 python scripts/make_screenshots.py            # writes docs/img/*.png
    uv run python scripts/make_screenshots.py --no-optimise                    # without Pillow: Chromium's own PNGs

Each picture is a real page of the record, served exactly as an agent on the browser route sees it (UI-only mode, a
1280x800 Chromium viewport), from a preview task's starting world. Where a picture needs something to have happened
first (a prescription refused by an allergy alert, a request on the requests list, a job on the jobs list), the script
does it through the UI's own forms, so nothing is drawn that the record would not show. The worlds are deterministic,
so the pictures are too, apart from font rendering on different machines.

Optimising: each PNG is reduced to a 256-colour palette without dithering (UI pages have a few hundred colours, mostly
anti-aliasing shades), which roughly halves the size and leaves the text sharp; metadata chunks are dropped.
"""

import argparse
import io
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "img"
VIEWPORT = {"width": 1280, "height": 800}

DVT, STEMI, HYPOK = "preview-dvt-discharge-judgement", "preview-stemi-judgement", "preview-hypok-judgement"
E, W, B = 1, 3, 4  # Mrs Ellison, Mr Whitcombe, Mrs Brightwell in the preview bundle


def optimise(data: bytes) -> bytes:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGB")
    out = io.BytesIO()
    im.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(out, "PNG", optimize=True)
    return out.getvalue()


def post(page, base: str, path: str, form: dict) -> str:
    """Submit a form as the page's own form would (same cookies), and return where the UI sends the browser next."""
    r = page.request.post(base + path, form=form, max_redirects=0)
    assert r.status in (302, 303), (path, r.status, r.text()[:300])
    return r.headers["location"]


def shots(page, base: str, task_id: str):
    """(file name, caption) for each picture of this task's world, taking it as it goes."""
    go = lambda path: page.goto(base + path, wait_until="networkidle")  # noqa: E731
    if task_id == DVT:
        go(f"/patients/{E}?tab=summary")
        yield "patient-summary.png", "Patient summary with the patient menu (Mrs Ellison, day 4 after a knee replacement)"
        go(f"/patients/{E}?tab=notes")
        yield "notes-list.png", "Notes list"
        go(f"/patients/{E}?tab=notes&note_id=9")
        page.locator("#note-body, .note-body, pre").first.scroll_into_view_if_needed() if page.locator("#note-body, .note-body, pre").count() else None
        yield "note-physio.png", "A note: the physiotherapist's stairs assessment, with calf measurements"
        go(f"/patients/{E}?tab=observations")
        yield "observations-news2.png", "Observations with NEWS2"
        # an allergy alert: co-amoxiclav for a patient with a recorded penicillin allergy, through the prescribing form
        form = {"drug": "Co-amoxiclav", "product": "Co-amoxiclav 500mg/125mg tablets", "dose_value": "625", "dose_unit": "mg", "route": "PO",
                "frequency": "TDS", "frequency_other": "", "frequency_detail": "", "kind": "regular", "duration": "", "indication": "Wound cellulitis"}  # fmt: skip
        go(post(page, base, f"/patients/{E}/medications", form))
        alert = page.locator(".error, [role=alert]").first
        if alert.count():
            alert.scroll_into_view_if_needed()
        yield "prescribing-allergy-alert.png", "The prescribing form, sent back with an allergy alert (penicillin allergy) that needs a reason to override"
        go(f"/patients/{E}?tab=medications")
        yield "medications.png", "The drug chart"
        go(f"/patients/{E}?tab=assessments&assessment_form=wells_dvt")
        form_el = page.locator("form").filter(has_text="Wells").first
        if form_el.count():
            form_el.scroll_into_view_if_needed()
        yield "assessment-wells.png", "Assessments: the structured Wells DVT form"
        go(f"/ice/patients/{E}?tab=request&code=US-DOPPLER-LL")
        yield "ice-request-form.png", "ICE, the separate requesting system: the venous Doppler request form"
    elif task_id == STEMI:
        go(f"/patients/{W}?tab=monitor")
        yield "bedside-monitor.png", "The bedside monitor in resus (Mr Whitcombe)"
        go(f"/patients/{W}?tab=referrals&referral_pathway=ppci")
        f = page.locator("form").filter(has_text="STEMI").first
        if f.count():
            f.scroll_into_view_if_needed()
        yield "referral-ppci.png", "Referrals: the Primary PCI activation pathway form"
        go("/reply")
        page.get_by_label("Inferior ST-elevation myocardial infarction").check()
        page.fill("#answer-plan-text", "PPCI activated; ticagrelor 180 mg loading dose charted; no nitrates (BP 98/62).")
        page.fill("#reply-text", "Inferior STEMI. Cath lab activated and accepted; loading dose charted.")
        page.locator("#answer-diagnosis").scroll_into_view_if_needed()
        yield "handover-reply.png", "Handover / reply: the task's answer form, typed in (a diagnosis to choose, a plan in words)"
    elif task_id == HYPOK:
        go(post(page, base, f"/ice/patients/{B}/requests", {"orderable_code": "UE", "priority": "routine", "collection": "phlebotomy_round",
                                                            "collect_at": "", "clinical_details": "K+ 3.1 on admission; recheck", "override_reason": ""}))  # fmt: skip
        go(post(page, base, f"/ice/patients/{B}/requests", {"orderable_code": "MG", "priority": "routine", "collection": "now",
                                                            "collect_at": "", "clinical_details": "K+ 3.1; magnesium not checked this admission", "override_reason": ""}))  # fmt: skip
        go(f"/ice/patients/{B}?tab=requests")
        yield "ice-requests.png", "ICE: the patient's requests (two made through the request form)"
        go(f"/ice/patients/{B}?tab=cumulative&analyte=Potassium")
        table = page.locator("section", has_text="Potassium, oldest first").last
        page.evaluate("y => window.scrollTo(0, y)", max(0, table.evaluate("e => e.getBoundingClientRect().bottom + window.scrollY") - VIEWPORT["height"] + 24))
        page.wait_for_timeout(200)
        yield "cumulative-potassium.png", "ICE cumulative results: Mrs Brightwell's potassium over time, GP and hospital"
        go(post(page, base, f"/patients/{B}/jobs", {"description": "Chase tomorrow's U&E and magnesium", "task_type": "chase_result", "priority": "routine"}))
        go("/jobs")
        yield "jobs-list.png", "The ward jobs list"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--no-optimise", action="store_true", help="keep Chromium's PNGs as they are (no Pillow needed)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "src"))
    from playwright.sync_api import sync_playwright

    from eprbench.server.local import serve_task
    from eprbench.tasks import load_task

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for task_id in (DVT, STEMI, HYPOK):
                with serve_task(load_task(task_id), "ui") as (base, _app):
                    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
                    for name, caption in shots(page, base, task_id):
                        data = page.screenshot(type="png")
                        (out / name).write_bytes(data if args.no_optimise else optimise(data))
                        made.append((name, caption))
                    page.close()
        finally:
            browser.close()
    lines = ["| File | What it shows |", "| --- | --- |"] + [f"| `{n}` | {c} |" for n, c in made]
    (out / "README.md").write_text("# Screenshots of the EPR's web UI\n\nMade by `scripts/make_screenshots.py` from the packaged preview "
                                   "cases (fictional patients), at 1280x800 as an agent on the browser route sees the record.\n\n" + "\n".join(lines) + "\n")  # fmt: skip
    for n, c in made:
        print(f"{n}\t{(out / n).stat().st_size // 1024} KB\t{c}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
