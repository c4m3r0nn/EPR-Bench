"""Checks a patient bundle for mistakes the schema cannot catch.

Bundles will be written by generators, including LLMs, so the checks target
the ways generated data goes quietly wrong: references that point nowhere,
safeguards that could never fire, and results that would contradict the
patient's own history.
"""

import itertools
import json
import math
import re
from collections import Counter

from eprbench import ops
from eprbench.loader import Bundle, FormField, _t, catalogue_formulary, catalogue_forms, catalogue_orderables
from eprbench.scenario import FREE_TEXT  # a rule keyed on these is keyed on wording, not on what was done

_SIDE = re.compile(r"\b(left|right|bilateral)\b", re.IGNORECASE)


def _abnormal(item) -> bool:
    if item.value is None:
        return False
    return (item.ref_low is not None and item.value < item.ref_low) or (item.ref_high is not None and item.value > item.ref_high)


def placeholders(bundle: Bundle) -> list[str]:
    """Clinical values marked as placeholders awaiting clinical review: regimen limits, services' names, the
    response delays and content of rules, pathways' forms and assessment forms' criteria, points and bands."""
    out = [f"regimen {g.name!r}: {g.drug} {g.model_dump(include={'unit', 'min_dose', 'max_dose', 'min_per_kg', 'max_per_kg', 'frequencies'}, exclude_none=True)}"
           for g in bundle.regimens if g.placeholder]  # fmt: skip
    out += [f"service {s.name!r}: reached by {', '.join([s.name, *s.aliases])}" for s in bundle.services if s.placeholder]
    out += [f"{p.mrn} {p.family_name}: rule {r.name!r} takes effect {r.delay_s} s after {r.on_op or 'its time'}" for p in bundle.patients for r in p.rules if r.placeholder]
    out += [f"pathway {w.name!r}: {w.label}, fields {', '.join(f.label for f in w.fields) or 'none'}" for w in bundle.pathways if w.placeholder]
    out += [f"assessment form {f.name!r}: {f.label}, {len(f.fields)} criteria and their points, bands {', '.join(b.label for b in f.bands)}"
            for f in catalogue_forms(bundle) if f.placeholder]  # fmt: skip
    return out


def _form_problems(where: str, fields: list[FormField]) -> list[str]:
    problems = [f"{where}: duplicate field {n!r}" for n, c in Counter(f.name for f in fields).items() if c > 1]
    for f in fields:
        if f.kind == "choice" and not f.options:
            problems.append(f"{where}: field {f.name!r} is a choice with no options")
        if f.kind != "choice" and f.options:
            problems.append(f"{where}: field {f.name!r} is {f.kind} but lists options, which only a choice has")
    return problems


def _score_range(fields: list[FormField]) -> tuple[float, float]:
    low = high = 0.0
    for f in fields:
        points = [o.points for o in f.options] if f.kind == "choice" else [f.points, 0] if f.kind in ("tick", "yes_no") else [0]
        low, high = low + min(points), high + max(points)
    return low, high


def _bound_problems(where: str, values: dict, bounds: dict) -> list[str]:
    """Charted values the chart itself would refuse: not a finite number, or outside the kind's bounds."""
    out = []
    for kind, value in values.items():
        if kind in bounds and not isinstance(value, str):
            low, high = bounds[kind]
            if not (math.isfinite(value) and low <= value <= high):
                out.append(f"{where}: {kind} {value:g} is outside {low:g} to {high:g}")
    return out


def _window_problems(who: str, code: str, item, windows: list) -> list[str]:
    """Results that depend on what was asked for. Branches must name the request's own fields and values; every value
    of a field a window branches on needs a result, from a branch or the window's own explicit default, so a request
    never falls silently to the catalogue normal; an examination done on one side must not report a finding on the
    other only (a bilateral report may name either side); and a side-specific result needs an explicit result for a
    bilateral request."""
    problems, choices = [], ops.request_choices(item.model_dump()) if item else {}
    for w in windows:
        for b in w.branches:
            for field, want in b.request.items():
                if field not in choices:
                    problems.append(f"{who}: {code}: a result branch branches on {field!r}, which its request has no choice of")
                    continue
                for v in [want] if isinstance(want, str) else want:
                    if v not in choices[field]:
                        problems.append(f"{who}: {code}: a result branch branches on {field} {v!r}, which the request form does not offer")
            sides = {s.lower() for s in _SIDE.findall(b.report_text or "")}
            asked = {b.request["laterality"]} if isinstance(b.request.get("laterality"), str) else set(b.request.get("laterality") or [])
            if asked and sides and "bilateral" not in asked and not sides & asked:
                problems.append(f"{who}: {code}: a result branch for a request with laterality {' or '.join(sorted(asked))} reports a {sorted(sides)[0]}-sided finding")
        fields = sorted({f for b in w.branches for f in b.request if f in choices})
        if fields and not (w.report_text or w.items):
            for combo in itertools.product(*(sorted(choices[f]) for f in fields)):
                request = dict(zip(fields, combo))
                if not any(ops.request_matches(b.request, request) for b in w.branches):
                    asked = " and ".join(f"{f} {v}" for f, v in request.items())
                    problems.append(f"{who}: {code}: a result window has no result for a request with {asked}; add a branch for it, or give the window "
                                    "its own result (an explicit normal, if that is what such a request shows)")  # fmt: skip
        sided = {v for b in w.branches for v in ([b.request["laterality"]] if isinstance(b.request.get("laterality"), str) else b.request.get("laterality", []))}
        if sided & {"left", "right"} and "bilateral" not in sided:
            problems.append(f"{who}: {code}: a result window gives a result for one side but none for a bilateral request, which would get the "
                            "window's default; add a bilateral branch that reports each side")  # fmt: skip
        if item is not None and item.lateral and not any("laterality" in b.request for b in w.branches):
            side = _SIDE.search(w.report_text or "")
            if side:
                problems.append(f"{who}: {code} is requested for a side, but a result window reports a {side[1].lower()}-sided finding whatever side is "
                                "asked for; give the window branches on laterality")  # fmt: skip
    return problems


def lint_bundle(bundle: Bundle) -> list[str]:
    """Return human-readable problems, empty if the bundle is clean."""
    problems = []
    catalogue = catalogue_orderables(bundle)
    orderables = {o.code for o in catalogue}
    fallback = {o.code: o.fallback for o in catalogue}
    locations = {loc.code: loc for loc in bundle.locations}
    merged = catalogue_formulary(bundle)
    formulary = {d.name.lower(): d for d in merged}
    product_routes, product_bases = {}, {}
    for d in merged:
        product_routes.setdefault(d.name.lower(), set()).update(r for p in d.products for r in p.routes)
        product_bases.setdefault(d.name.lower(), set()).update((s.get("basis") or s.get("ingredient") or "").lower() for p in d.products for s in p.strengths)
    allergy_terms = set(formulary) | {c.lower() for d in merged for c in d.classes}
    services = {s.name.lower() for s in bundle.services}
    regimens = {g.name for g in bundle.regimens}
    pathways = {w.name: w for w in bundle.pathways}
    items = {o.code: o for o in catalogue}

    for field, values in (("mrn", [p.mrn for p in bundle.patients]), ("nhs_number", [p.nhs_number for p in bundle.patients])):
        problems += [f"duplicate {field} {v}" for v, n in Counter(values).items() if n > 1]

    for r in bundle.phlebotomy_rounds:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", r):
            problems.append(f"phlebotomy round {r!r} is not a time as HH:MM")
    for what, overrides in (("turnaround", bundle.turnaround_overrides), ("repeat interval", bundle.repeat_interval_overrides)):
        for code, seconds in overrides.items():
            if code not in orderables:
                problems.append(f"{what} override for {code!r}, which is not in the catalogue")
            elif seconds <= 0:
                problems.append(f"{what} override for {code!r} must be positive")
    bounds = ops.OBSERVATION_BOUNDS | dict(bundle.observation_bounds)
    for kind, (low, high) in bundle.observation_bounds.items():
        if kind not in ops.OBSERVATION_BOUNDS:
            problems.append(f"observation bounds for {kind!r}, which is not a numeric observation")
        elif low >= high:
            problems.append(f"observation bounds for {kind}: {low:g} is not below {high:g}")

    problems += [f"duplicate pathway {n!r}" for n, c in Counter(w.name for w in bundle.pathways).items() if c > 1]
    for w in bundle.pathways:
        if w.service is not None and w.service.lower() not in services:
            problems.append(f"pathway {w.name!r} reaches {w.service!r}, which is not a service")
        problems += _form_problems(f"pathway {w.name!r}", w.fields)
    forms = catalogue_forms(bundle)
    problems += [f"duplicate assessment form {n!r}" for n, c in Counter(f.name for f in bundle.assessment_forms).items() if c > 1]
    for f in forms:
        where = f"assessment form {f.name!r}"
        if f.name in ops.ASSESSMENT_KINDS:
            problems.append(f"{where} has the name of a built-in assessment")
        problems += _form_problems(where, f.fields)
        bands = sorted(f.bands, key=lambda b: (b.min is not None, b.min or 0))
        for a, b in zip(bands, bands[1:]):
            if a.max is None or b.min is None or a.max >= b.min:
                problems.append(f"{where}: bands {a.value!r} and {b.value!r} overlap")
        if f.bands:
            for score in _score_range(f.fields):
                if not any((b.min is None or b.min <= score) and (b.max is None or score <= b.max) for b in f.bands):
                    problems.append(f"{where}: a score of {score:g} falls in no band")

    for p in bundle.patients:
        who = f"{p.mrn} {p.family_name}"
        refs = {e.ref for e in p.encounters}
        problems += [f"{who}: duplicate encounter ref {r!r}" for r, n in Counter(e.ref for e in p.encounters).items() if n > 1]

        for e in p.encounters:
            if e.location and e.location not in locations:
                problems.append(f"{who}: encounter {e.ref!r} is in unknown location {e.location!r}")
        for s in p.observations:
            problems += _bound_problems(f"{who}: observations at {s.taken_at}", s.values, bounds)
        for kind, records in (("note", p.notes), ("observation set", p.observations), ("result", p.results), ("medication", p.medications), ("problem", p.problems)):
            for r in records:
                if r.encounter is not None and r.encounter not in refs:
                    problems.append(f"{who}: a {kind} points at unknown encounter {r.encounter!r}")

        windows = p.result_windows + p.latent_results
        effect_windows = [w for rule in p.rules for w in (*rule.effects.result_windows, *rule.effects.latent_results)]
        for r in [*p.results, *windows, *effect_windows]:
            if r.orderable_code not in orderables:
                problems.append(f"{who}: result for {r.orderable_code!r}, which is not in the catalogue")
        for code in sorted({w.orderable_code for w in [*windows, *effect_windows]}):
            problems += _window_problems(who, code, items.get(code), [w for w in [*windows, *effect_windows] if w.orderable_code == code])

        # A previously abnormal test needs a declared trajectory: an open-ended result window saying what a repeat
        # shows from then on. Without one a repeat repeats the last value indefinitely, or comes back normal where
        # the test falls back to its normal (found on the first live run: Hb 138 after a post-op 109).
        persistent = {w.orderable_code for w in windows if w.valid_to is None}
        for code in sorted({r.orderable_code for r in p.results if any(_abnormal(i) for i in r.items)} - persistent):
            then = "come back normal" if fallback.get(code) == "normal" else "repeat the last value (until it is too old, then the normal)"
            problems.append(f"{who}: {code} was abnormal in the past but has no open-ended result window, so a repeat would {then}")

        # Without a window, a test's last-value fallback needs the latest value of each analyte in the catalogue's
        # unit (or one it converts from) and recent enough; otherwise a repeat silently comes back normal.
        current = max((_t(e.admitted_at) for e in p.encounters if e.discharged_at is None), default=None)
        for o in catalogue:
            if o.fallback != "last_value" or o.code in persistent or o.normal is None:
                continue
            for item in o.normal.items:
                history = sorted(((_t(r.performed_at), i) for r in p.results for i in r.items if i.analyte == item.analyte and i.value is not None), key=lambda x: x[0])
                if not history:
                    continue
                when, last = history[-1]
                if ops.convert_unit(last.value, last.unit, item.unit) is None:
                    problems.append(f"{who}: {o.code}: the latest {item.analyte} ({last.value:g} {last.unit}) cannot be converted to {item.unit}, "
                                    "so a repeat would come back as the catalogue normal; add a result window or declare fallback normal")  # fmt: skip
                elif current is not None and _abnormal(last) and current - when > o.fallback_max_age_days * 86400:
                    problems.append(f"{who}: {o.code}: the latest {item.analyte} was abnormal but is older than {o.fallback_max_age_days:g} days at "
                                    "admission, so a repeat would come back as the catalogue normal; add a result window")  # fmt: skip

        for m in p.medications:
            drug = formulary.get(m.drug.lower())
            if drug is None:
                problems.append(f"{who}: {m.drug!r} is prescribed but not in the formulary")
            elif (product_routes.get(m.drug.lower()) or set(drug.routes)) and m.route not in (product_routes.get(m.drug.lower()) or set(drug.routes)):
                problems.append(f"{who}: {m.drug} is prescribed {m.route}, which the formulary does not permit")

        for a in p.allergies:
            if a.substance.lower() not in allergy_terms:
                problems.append(f"{who}: allergy to {a.substance!r} matches no formulary drug or class, so no alert can fire")

        names = {r.name for r in p.rules}
        signals = {v.signal for v in p.vitals_tracks}
        problems += [f"{who}: duplicate rule name {n!r}" for n, c in Counter(r.name for r in p.rules).items() if c > 1]
        for r in p.rules:
            if r.on_op is not None and (r.on_op not in ops.REGISTRY or not ops.REGISTRY[r.on_op].writes):
                problems.append(f"{who}: rule {r.name!r} is triggered by {r.on_op!r}, which is not a write operation")
            for unless in r.unless_names:
                if unless not in names:
                    problems.append(f"{who}: rule {r.name!r} depends on unknown rule {unless!r}")
            if r.encounter not in (None, "current") and r.encounter not in refs:
                problems.append(f"{who}: rule {r.name!r} is active only during encounter {r.encounter!r}, which the patient does not have")
            if r.not_before and r.not_after and _t(r.not_before) > _t(r.not_after):
                problems.append(f"{who}: rule {r.name!r} can never fire: not_before is after not_after")
            for field in sorted(set(r.match) & set(FREE_TEXT)):
                problems.append(f"{who}: rule {r.name!r} matches free text ({field}); key it on structured fields (pathway, fields, service, regimen)")
            for when in [r.when, *(v.when for v in r.variants)]:
                rule_pathways = [r.when["pathway"]] if isinstance(r.when.get("pathway"), str) else r.when.get("pathway") or []
                for pathway in [p for p in rule_pathways if p]:
                    if pathway not in pathways:
                        if when is r.when:
                            problems.append(f"{who}: rule {r.name!r} waits for pathway {pathway!r}, which the bundle does not define")
                        continue
                    known = {f.name: {o.value for o in f.options} or ({"yes", "no"} if f.kind in ("tick", "yes_no") else None) for f in pathways[pathway].fields}
                    for field, want in (when.get("fields") or {}).items():
                        if field not in known:
                            problems.append(f"{who}: rule {r.name!r} waits for {field!r}, which pathway {pathway!r} has no field for")
                        elif known[field] is not None and not set([want] if isinstance(want, str) else want) <= known[field]:
                            problems.append(f"{who}: rule {r.name!r} waits for {field} {want!r}, which pathway {pathway!r} does not offer")
                for codes in (when.get("record") or {}).values():
                    for c in [codes] if isinstance(codes, str) else codes:
                        if c not in orderables:
                            problems.append(f"{who}: rule {r.name!r} looks on the record for {c!r}, which is not in the catalogue")
            for s in [o for o in r.effects.observations] + [o for v in r.variants for o in v.effects.observations]:
                problems += _bound_problems(f"{who}: rule {r.name!r}", s.values, bounds)
            for signal in sorted(set(r.effects.vitals) - signals):
                problems.append(f"{who}: rule {r.name!r} bends {signal}, which has no monitor track")
            if "regimen" in r.when and r.when["regimen"] not in regimens:
                problems.append(f"{who}: rule {r.name!r} needs regimen {r.when['regimen']!r}, which the bundle does not define")
            if "service" in r.when and str(r.when["service"]).lower() not in services:
                problems.append(f"{who}: rule {r.name!r} waits for a referral to {r.when['service']!r}, which is not a service")
            if r.effects.referral_update and r.on_op != "make_referral":
                problems.append(f"{who}: rule {r.name!r} replies to a referral but is not triggered by one")
            if r.effects.administer and r.on_op != "prescribe_medication":
                problems.append(f"{who}: rule {r.name!r} gives the prescription that triggered it but is not triggered by prescribing")
            for m in r.effects.medications:
                if m.drug.lower() not in formulary:
                    problems.append(f"{who}: rule {r.name!r} prescribes {m.drug!r}, which is not in the formulary")

        if p.vitals_tracks and not any(e.location in locations and locations[e.location].live_monitoring for e in p.encounters):
            problems.append(f"{who}: has monitor tracks but is never in a monitored location")

    known_classes = {c for d in merged for c in d.classes}
    for r in bundle.prescribing_rules:
        if not (r.drug_regex or r.drug_class):
            problems.append(f"prescribing rule {r.name!r} targets no drug")
        if not (r.problem_regex or r.with_class or (r.analyte and (r.below is not None or r.above is not None))):
            problems.append(f"prescribing rule {r.name!r} has no condition, so it would fire on every prescription")
        for pattern in (r.drug_regex, r.problem_regex):
            try:
                re.compile(pattern or "")
            except re.error:
                problems.append(f"prescribing rule {r.name!r} has an invalid regular expression: {pattern}")
        for cls in (r.drug_class, r.with_class):
            if cls and cls not in known_classes:
                problems.append(f"prescribing rule {r.name!r} refers to class {cls!r}, which no formulary drug has")

    for d in bundle.formulary:
        unknown = sorted(set(d.routes) - set(ops.ROUTES))
        if unknown:
            problems.append(f"formulary: {d.name} lists unknown routes {json.dumps(unknown)}")
        # The maximum doses are of the drug or of the salt it declares; a product whose strength is stated as
        # something else can never be converted, so every dose of it would go unchecked.
        bases = product_bases.get(d.name.lower(), set()) - {""}
        basis = (d.max_dose_basis or d.name).lower()
        if (d.max_dose_mg is not None or d.max_daily_mg is not None) and bases and basis not in bases:
            problems.append(f"formulary: {d.name}'s maximum doses are of {d.max_dose_basis or d.name!r}, but its products state their strength as "
                            f"{', '.join(sorted(bases))}, so no dose of it can be checked")  # fmt: skip

    for g in bundle.regimens:
        if g.drug.lower() not in formulary:
            problems.append(f"regimen {g.name!r}: {g.drug!r} is not in the formulary")
        if all(v is None for v in (g.min_dose, g.max_dose, g.min_per_kg, g.max_per_kg)):
            problems.append(f"regimen {g.name!r}: {g.drug} has no dose limits, so any dose would count")
        for f in g.frequencies:
            try:
                if ops._clean_frequency(f) != f:
                    raise ops.OpError("", "")
            except ops.OpError:
                problems.append(f"regimen {g.name!r}: {g.drug} frequency {f!r} is not as the prescribing picklist writes it")
                continue
            if ops.doses_per_day(f) is None:
                problems.append(f"regimen {g.name!r}: {g.drug} frequency {f!r} has no number of doses per day, so it cannot be matched")
        for route in sorted(set(g.routes) - set(ops.ROUTES)):
            problems.append(f"regimen {g.name!r}: {g.drug} route {route!r} is unknown")
    return problems
