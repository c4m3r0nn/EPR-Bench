import json
import os

import pytest
from pydantic import ValidationError

from eprbench import loader, vitals
from eprbench.identifiers import format_nhs_number, is_valid_nhs_number, synthetic_nhs_numbers
from eprbench.loader import Bundle
from eprbench.news2 import news2
from eprbench.tasks import build_world, invalidate_caches, load_task
from eprbench.world import World, iso, parse_iso


def test_nhs_number_checksum():
    assert is_valid_nhs_number("943 476 5919")  # the published NHS example number
    assert not is_valid_nhs_number("943 476 5918")
    assert not is_valid_nhs_number("12345")
    assert format_nhs_number("9434765919") == "943 476 5919"


def test_synthetic_nhs_numbers_are_valid_and_in_test_range():
    gen = synthetic_nhs_numbers()
    numbers = [next(gen) for _ in range(200)]
    assert len(set(numbers)) == 200
    assert all(n.startswith("999") and is_valid_nhs_number(n) for n in numbers)


def test_bundle_rejects_bad_nhs_number_and_unknown_fields():
    patient = {"mrn": "X1", "nhs_number": "9990000019", "family_name": "A", "given_name": "B", "dob": "2000-01-01", "sex": "F"}
    with pytest.raises(ValidationError):
        Bundle.model_validate({"patients": [patient]})
    with pytest.raises(ValidationError):
        Bundle.model_validate({"patients": [{**patient, "nhs_number": "9990000018", "shoe_size": 7}]})


def test_iso_round_trip():
    assert iso(parse_iso("2026-03-14T09:30:00Z")) == "2026-03-14T09:30:00Z"


def test_clock_and_snapshot_round_trip(world):
    start = world.now
    world.advance(90)
    copy = World.restore(world.snapshot())
    assert copy.now == start + 90
    assert copy.q("SELECT mrn FROM patient ORDER BY mrn") == world.q("SELECT mrn FROM patient ORDER BY mrn")
    copy.advance(10)
    assert world.now == start + 90  # the copy is independent
    with pytest.raises(ValueError):
        world.advance(-1)


NORMAL = {"RR": 16, "SPO2": 97, "ON_O2": 0, "SBP": 125, "HR": 72, "ACVPU": "A", "TEMP": 36.9}


@pytest.mark.parametrize(
    "change, scale, total, risk",
    [
        ({}, 1, 0, "low"),
        ({"RR": 26, "SPO2": 90, "SBP": 108, "HR": 118, "TEMP": 37.4}, 1, 9, "high"),
        ({"ACVPU": "C"}, 1, 3, "low-medium"),
        ({"ON_O2": 1, "SPO2": 94, "HR": 95}, 1, 4, "low"),
        ({"SPO2": 90}, 2, 0, "low"),  # 88-92% is on target for scale 2
        ({"SPO2": 97, "ON_O2": 1}, 2, 5, "medium"),  # over-oxygenated on scale 2
        ({"SBP": 220}, 1, 3, "low-medium"),
        ({"TEMP": 35.0, "RR": 8}, 1, 6, "medium"),
    ],
)
def test_news2(change, scale, total, risk):
    score = news2({**NORMAL, **change}, scale)
    assert (score["total"], score["risk"]) == (total, risk)


def test_news2_needs_a_complete_set():
    assert news2({"HR": 80}) is None


TRACK = {"signal": "HR", "seed": 7, "sd": 2.0, "lo": 20, "hi": 220, "decimals": 0, "keyframes": "[[0, 80], [600, 140]]"}


def test_vitals_are_deterministic_and_follow_the_trend():
    assert [vitals.value_at(TRACK, t) for t in range(0, 600, 7)] == [vitals.value_at(TRACK, t) for t in range(0, 600, 7)]
    early = sum(vitals.value_at(TRACK, t) for t in range(0, 60)) / 60
    late = sum(vitals.value_at(TRACK, t) for t in range(540, 600)) / 60
    assert 78 < early < 90 and 130 < late < 142
    assert vitals.value_at({**TRACK, "seed": 8}, 100) != vitals.value_at(TRACK, 100) or vitals.value_at({**TRACK, "seed": 8}, 101) != vitals.value_at(TRACK, 101)


def test_vitals_noise_is_plausible_and_clamped():
    flat = {**TRACK, "keyframes": "[[0, 80]]"}
    samples = [vitals.value_at(flat, t) for t in range(5000)]
    mean = sum(samples) / len(samples)
    sd = (sum((s - mean) ** 2 for s in samples) / len(samples)) ** 0.5
    assert abs(mean - 80) < 0.5 and 1.0 < sd < 2.5
    assert all(v <= 81 for v in (vitals.value_at({**flat, "hi": 81}, t) for t in range(500)))


# --- caches are keyed by content ----------------------------------------------


@pytest.fixture
def data_copy(tmp_path, monkeypatch):
    """Point the loader at copies of the packaged bundle and imaging catalogue that the test may edit."""
    original = loader.data_file
    for kind, name in (("bundles", "demo"), ("catalogues", "imaging")):
        (tmp_path / kind).mkdir()
        (tmp_path / kind / f"{name}.json").write_bytes(original(kind, name).read_bytes())
    monkeypatch.setattr(loader, "data_file", lambda kind, name: tmp_path / kind / f"{name}.json" if (tmp_path / kind / f"{name}.json").exists() else original(kind, name))
    invalidate_caches()
    yield tmp_path
    invalidate_caches()


def test_an_edited_bundle_is_used_after_invalidation(data_copy):
    task = load_task("order-ctpa-001")
    name = lambda: build_world(task).one("SELECT family_name FROM patient WHERE mrn = 'H4417302'")["family_name"]  # noqa: E731
    assert name() == "Ellison"
    path = data_copy / "bundles" / "demo.json"
    path.write_text(path.read_text().replace('"Ellison"', '"Ellisons"', 1))
    assert name() == "Ellison"
    invalidate_caches()
    assert name() == "Ellisons"


def test_an_edited_catalogue_is_used_after_invalidation(data_copy):
    path = data_copy / "catalogues" / "imaging.json"
    first = loader._catalogue("imaging").orderables[0]
    raw = json.loads(path.read_text())
    raw["orderables"][0]["name"] = "Renamed examination"
    path.write_text(json.dumps(raw))
    assert loader._catalogue("imaging").orderables[0].name == first.name
    invalidate_caches()
    assert loader._catalogue("imaging").orderables[0].name == "Renamed examination"
    assert loader.bundle_digests("demo")["catalogues/imaging.json"] == loader.file_sha256(path)


def test_a_same_size_edit_with_restored_timestamps_is_used_after_invalidation(data_copy):
    path = data_copy / "bundles" / "demo.json"
    task = load_task("order-ctpa-001")
    assert build_world(task).one("SELECT family_name FROM patient WHERE id = 1")["family_name"] == "Ellison"
    st = os.stat(path)
    with open(path, "r+") as f:  # same inode, same size
        content = f.read().replace('"Ellison"', '"Ellisom"', 1)
        f.seek(0)
        f.write(content)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(path).st_size == st.st_size and os.stat(path).st_ino == st.st_ino
    invalidate_caches()
    world = build_world(task)
    assert world.one("SELECT family_name FROM patient WHERE id = 1")["family_name"] == "Ellisom"
    assert world.source_sha256["bundles/demo.json"] == loader.hashlib.sha256(path.read_bytes()).hexdigest()
