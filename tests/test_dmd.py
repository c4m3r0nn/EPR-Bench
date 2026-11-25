"""The dm+d importer, against a real (tiny) dm+d release published under the Open Government Licence."""

import json
from pathlib import Path

from eprbench import dmd
from eprbench.loader import Bundle

RELEASE = Path(__file__).parent / "fixtures" / "dmd"


def test_reads_products_with_form_route_strength_and_drug():
    products = {p["name"]: p for p in dmd.read_release(RELEASE)}
    tablet = products["Co-amilofruse 5mg/40mg tablets"]
    assert tablet["drug"] == "Co-amilofruse" and tablet["form"] == "Tablet" and tablet["routes"] == ["PO"]
    assert tablet["unit_dose_form"] == "tablet" and tablet["controlled_drug"] is None
    assert tablet["strengths"] == [
        {"ingredient": "Amiloride hydrochloride", "basis": None, "value": 5, "unit": "mg", "per_value": None, "per_unit": None},
        {"ingredient": "Furosemide", "basis": None, "value": 40, "unit": "mg", "per_value": None, "per_unit": None},
    ]
    assert "Co-amilofruse 2.5mg/20mg tablets" in products


def test_catalogue_groups_products_by_drug_and_loads_as_a_bundle(tmp_path):
    out = tmp_path / "formulary.json"
    assert dmd.write_catalogue(RELEASE, out) == 2
    catalogue = json.loads(out.read_text())
    assert catalogue["source"].startswith("NHS Dictionary of Medicines and Devices")
    bundle = Bundle.model_validate(catalogue)
    assert [d.name for d in bundle.formulary] == ["Co-amilofruse"] and len(bundle.formulary[0].products) == 2
    assert dmd.to_catalogue(dmd.read_release(RELEASE), drugs=["paracetamol"])["formulary"] == []


def test_route_codes_cover_the_common_routes():
    for name, code in [("Oral", "PO"), ("Intravenous", "IV"), ("Subcutaneous", "SC"), ("Gastroenteral", "NG"), ("Transdermal", "TD")]:
        assert dmd.ROUTE_CODES[name] == code


def test_pack_files_are_not_mistaken_for_product_files(tmp_path):
    """A full release also has f_vmpp (packs) and f_ampp files, whose names start like f_vmp and f_amp."""
    import shutil

    for f in RELEASE.glob("*.xml"):
        shutil.copy(f, tmp_path / f.name)
    (tmp_path / "f_vmpp2_3260821.xml").write_text('<?xml version="1.0"?><VIRTUAL_MED_PRODUCT_PACK><VMPPS></VMPPS></VIRTUAL_MED_PRODUCT_PACK>')
    assert len(dmd.read_release(tmp_path)) == 2


def test_reads_the_basis_of_strength_and_the_size_of_a_unit_dose(tmp_path):
    """What a strength is a strength of (a salt or its base), and how much one vial, sachet or syringe holds:
    both are needed to check a dose such as '1 vial' or '2 tablets' of a salt against a maximum."""
    import shutil

    for f in RELEASE.glob("*.xml"):
        shutil.copy(f, tmp_path / f.name)
    ing = tmp_path / "f_ingredient2_3260821.xml"
    ing.write_text(ing.read_text().replace("<ING>", "<ING><ISID>999001</ISID><NM>Amiloride</NM></ING><ING>", 1))
    vmp = tmp_path / "f_vmp2_3260821.xml"
    text = vmp.read_text()
    # the 5 mg amiloride is stated as the base; the 2.5mg/20mg product becomes a 2 ml vial
    text = text.replace("<ISID>387516008</ISID>\n            <BASIS_STRNTCD>0001</BASIS_STRNTCD>", "<ISID>387516008</ISID>\n            <BASIS_STRNTCD>0002</BASIS_STRNTCD><BS_SUBID>999001</BS_SUBID>")
    tablets = "<UDFS>1</UDFS>\n            <UDFS_UOMCD>428673006</UDFS_UOMCD>\n            <UNIT_DOSE_UOMCD>428673006</UNIT_DOSE_UOMCD>"
    assert text.index(tablets) < text.index("318136009")  # the first product is the 2.5mg/20mg one
    text = text.replace(tablets, "<UDFS>2</UDFS><UDFS_UOMCD>258773002</UDFS_UOMCD><UNIT_DOSE_UOMCD>415818006</UNIT_DOSE_UOMCD>", 1)
    text = text.replace("</VIRTUAL_PRODUCT_INGREDIENT>", "<VPI><VPID>318135008</VPID><ISID>387475002</ISID><BASIS_STRNTCD>0001</BASIS_STRNTCD><STRNT_NMRTR_VAL>10</STRNT_NMRTR_VAL><STRNT_NMRTR_UOMCD>258684004</STRNT_NMRTR_UOMCD><STRNT_DNMTR_VAL>1</STRNT_DNMTR_VAL><STRNT_DNMTR_UOMCD>258773002</STRNT_DNMTR_UOMCD></VPI></VIRTUAL_PRODUCT_INGREDIENT>")
    vmp.write_text(text)
    products = {p["name"]: p for p in dmd.read_release(tmp_path)}
    tablet, vial = products["Co-amilofruse 5mg/40mg tablets"], products["Co-amilofruse 2.5mg/20mg tablets"]
    assert tablet["strengths"][0]["basis"] == "Amiloride" and tablet["strengths"][1]["basis"] is None
    assert (tablet["unit_dose_size"], tablet["unit_dose_size_unit"]) == (1, "tablet")
    assert (vial["unit_dose_form"], vial["unit_dose_size"], vial["unit_dose_size_unit"]) == ("vial", 2, "ml")
    catalogue = {p["name"]: p for p in dmd.to_catalogue(list(products.values()))["formulary"][0]["products"]}
    # the catalogue keeps both on the strengths (a product has no other place for them), only when they add something
    assert catalogue["Co-amilofruse 5mg/40mg tablets"]["strengths"][0] == {"ingredient": "Amiloride hydrochloride", "basis": "Amiloride", "value": 5, "unit": "mg"}
    assert catalogue["Co-amilofruse 2.5mg/20mg tablets"]["strengths"] == [
        {"ingredient": "Furosemide", "value": 10, "unit": "mg", "per_value": 1, "per_unit": "ml", "unit_dose_size": 2, "unit_dose_size_unit": "ml"}
    ]
    Bundle.model_validate(dmd.to_catalogue(list(products.values())))  # still a valid bundle
