"""Import the NHS Dictionary of Medicines and Devices (dm+d) into a formulary catalogue.

dm+d is published weekly by the NHS Business Services Authority under the Open
Government Licence and downloaded from NHS England's TRUD service (a free
account is needed). A release is a folder of XML files; this module reads the
four it needs:

  f_vtm*.xml         virtual therapeutic moieties: the drug, e.g. "Amoxicillin"
  f_vmp*.xml         virtual medicinal products: a drug in a form and strength,
                     e.g. "Amoxicillin 500mg capsules", with form, routes,
                     ingredient strengths (and whether each is stated as the
                     ingredient or its base), the size of one unit dose (a 2 ml
                     vial) and controlled drug category
  f_ingredient*.xml  ingredient substance names
  f_lookup*.xml      code tables: forms, routes, units, controlled drug categories

and writes a catalogue that a bundle can include with `"catalogues": [...]`:
one formulary entry per drug, each with its products. Safety metadata that dm+d
does not carry (drug classes for allergy matching, maximum doses, flags such as
"weekly") is added by the bundle's own formulary entries, which are merged on
top by drug name.
"""

import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# dm+d route descriptions mapped to the short codes the record stores and prescribers type.
# Anything not listed keeps its dm+d description.
ROUTE_CODES = {
    "Oral": "PO", "Intravenous": "IV", "Subcutaneous": "SC", "Intramuscular": "IM", "Inhalation": "INH",
    "Cutaneous": "TOP", "Transdermal": "TD", "Rectal": "PR", "Vaginal": "PV", "Sublingual": "SL", "Buccal": "BUCC",
    "Oromucosal": "BUCC", "Gastroenteral": "NG", "Intrathecal": "IT", "Epidural": "EPIDURAL", "Ocular": "EYE",
    "Auricular": "EAR", "Nasal": "NASAL", "Intraosseous": "IO", "Intradermal": "ID",
}  # fmt: skip


def _file(release: Path, prefix: str) -> Path:
    # the version digit straight after the prefix keeps f_vmp from also matching f_vmpp (packs)
    matches = sorted(release.glob(f"{prefix}[0-9]*.xml")) or sorted(release.glob(f"**/{prefix}[0-9]*.xml"))
    if not matches:
        raise FileNotFoundError(f"no {prefix}*.xml in {release}")
    return matches[-1]


def _lookup(path: Path) -> dict[str, dict[str, str]]:
    root = ET.parse(path).getroot()
    return {table.tag: {i.findtext("CD"): i.findtext("DESC") for i in table.findall("INFO")} for table in root}


def _rows(root: ET.Element, section: str, row: str) -> list[dict[str, str]]:
    container = root.find(section)
    if container is None:
        return []
    return [{child.tag: child.text for child in element} for element in container.findall(row)]


def _number(text: str | None) -> float | str | None:
    if text is None:
        return None
    value = float(text)
    return int(value) if value.is_integer() else value


def read_release(release: str | Path) -> list[dict]:
    """Every valid virtual medicinal product in a release, with its drug, form, routes and strengths."""
    release = Path(release)
    lookup = _lookup(_file(release, "f_lookup"))
    units, forms, routes = lookup["UNIT_OF_MEASURE"], lookup["FORM"], lookup["ROUTE"]
    controlled = lookup["CONTROL_DRUG_CATEGORY"]
    statuses = lookup["VIRTUAL_PRODUCT_PRES_STATUS"]

    vtms = {r["VTMID"]: r["NM"] for r in (dict((c.tag, c.text) for c in v) for v in ET.parse(_file(release, "f_vtm")).getroot().findall("VTM"))}
    ingredients = {
        r["ISID"]: r["NM"] for r in (dict((c.tag, c.text) for c in i) for i in ET.parse(_file(release, "f_ingredient")).getroot().findall("ING"))
    }
    vmp_root = ET.parse(_file(release, "f_vmp")).getroot()

    strengths = defaultdict(list)
    for vpi in _rows(vmp_root, "VIRTUAL_PRODUCT_INGREDIENT", "VPI"):
        strengths[vpi["VPID"]].append(
            {
                "ingredient": ingredients.get(vpi["ISID"], vpi["ISID"]),
                # what the strength is a strength of, when dm+d states it as the base rather than the ingredient
                # (amoxicillin trihydrate capsules are labelled in amoxicillin); None means the ingredient itself
                "basis": ingredients.get(vpi.get("BS_SUBID"), vpi.get("BS_SUBID")) if vpi.get("BASIS_STRNTCD") == BASE_SUBSTANCE else None,
                "value": _number(vpi.get("STRNT_NMRTR_VAL")),
                "unit": units.get(vpi.get("STRNT_NMRTR_UOMCD")),
                "per_value": _number(vpi.get("STRNT_DNMTR_VAL")),
                "per_unit": units.get(vpi.get("STRNT_DNMTR_UOMCD")),
            }
        )
    form_of = {r["VPID"]: forms.get(r["FORMCD"], r["FORMCD"]) for r in _rows(vmp_root, "DRUG_FORM", "DFORM")}
    routes_of = defaultdict(list)
    for r in _rows(vmp_root, "DRUG_ROUTE", "DROUTE"):
        name = routes.get(r["ROUTECD"], r["ROUTECD"])
        routes_of[r["VPID"]].append(ROUTE_CODES.get(name, name))
    controlled_of = {r["VPID"]: controlled.get(r["CATCD"], r["CATCD"]) for r in _rows(vmp_root, "CONTROL_DRUG_INFO", "CONTROL_INFO")}

    products = []
    for vmp in _rows(vmp_root, "VMPS", "VMP"):
        if vmp.get("INVALID") == "1":
            continue
        vpid = vmp["VPID"]
        cd = controlled_of.get(vpid)
        products.append(
            {
                "vpid": vpid,
                "name": vmp["NM"],
                "drug": vtms.get(vmp.get("VTMID")),
                "form": form_of.get(vpid),
                "routes": sorted(set(routes_of[vpid])),
                "strengths": strengths[vpid],
                "unit_dose_form": units.get(vmp.get("UNIT_DOSE_UOMCD")),
                "unit_dose_size": _number(vmp.get("UDFS")),  # one unit dose holds this much: a vial, 2 ml
                "unit_dose_size_unit": units.get(vmp.get("UDFS_UOMCD")),
                "controlled_drug": None if cd in (None, "No Controlled Drug Status") else cd,
                "prescribing_status": statuses.get(vmp.get("PRES_STATCD"), vmp.get("PRES_STATCD")),
            }
        )
    return products


BASE_SUBSTANCE = "0002"  # BASIS_OF_STRNTH: the strength is stated as the base substance
NOT_A_ROUTE = "Route of administration not applicable"
NEVER_PRESCRIBABLE = "Never Valid To Prescribe As A VMP"


def to_catalogue(products: list[dict], drugs: list[str] | None = None, source: str | None = None) -> dict:
    """Group prescribable medicines under their drug, optionally keeping only the named drugs (case-insensitive).

    Devices (no drug), products never prescribable as a VMP, and dm+d's "route not applicable" placeholder are left out.
    """
    wanted = {d.lower() for d in drugs} if drugs else None
    grouped = defaultdict(list)
    for p in products:
        if p["drug"] is None or p.get("prescribing_status") == NEVER_PRESCRIBABLE:
            continue
        if wanted is not None and p["drug"].lower() not in wanted:
            continue
        item = {k: p[k] for k in ("name", "form", "unit_dose_form", "controlled_drug", "vpid") if p.get(k) is not None}
        item["routes"] = [r for r in p["routes"] if r != NOT_A_ROUTE]
        # a catalogue product has no field for the size of its unit dose, so it travels with each strength,
        # and only when it adds something (a vial of 2 ml, not a tablet of 1 tablet)
        size = {}
        if p.get("unit_dose_size") is not None and p.get("unit_dose_size_unit") not in (None, p.get("unit_dose_form")):
            size = {"unit_dose_size": p["unit_dose_size"], "unit_dose_size_unit": p["unit_dose_size_unit"]}
        strengths = [{k: v for k, v in {**s, **size}.items() if v is not None} for s in p["strengths"]]
        if strengths:
            item["strengths"] = strengths
        grouped[p["drug"]].append(item)
    formulary = [
        {"name": drug, "routes": sorted({r for p in items for r in p["routes"]}), "products": sorted(items, key=lambda p: p["name"])}
        for drug, items in sorted(grouped.items())
    ]
    label = f"NHS Dictionary of Medicines and Devices{f' ({source})' if source else ''}, NHS Business Services Authority, Open Government Licence v3.0"
    return {"source": label, "formulary": formulary}


def write_catalogue(release: str | Path, out: str | Path, drugs: list[str] | None = None, source: str | None = None) -> int:
    catalogue = to_catalogue(read_release(release), drugs, source)
    # one drug per line: small enough to diff, without the size of fully indented JSON
    lines = ",\n".join(json.dumps(d, separators=(",", ":")) for d in catalogue["formulary"])
    Path(out).write_text(f'{{"source":{json.dumps(catalogue["source"])},"formulary":[\n{lines}\n]}}\n')
    return sum(len(d["products"]) for d in catalogue["formulary"])
