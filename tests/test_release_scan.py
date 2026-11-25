"""The release scan (scripts/release_scan.py) finds what must not be published, and only that."""

import importlib.util
import json
import random
from pathlib import Path

import pytest

from eprbench.identifiers import is_valid_nhs_number, synthetic_nhs_numbers

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("release_scan", ROOT / "scripts" / "release_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def kinds(text: str) -> list[str]:
    return [f.kind for f in scan.scan_text("x", text)]


def test_it_finds_keys_home_paths_and_real_looking_nhs_numbers():
    real = next(n for n in (f"{random.Random(1).randrange(10**9):09d}{d}" for d in range(10)) if is_valid_nhs_number(n) and not n.startswith("999"))
    for text, kind in [
        # built at run time, so the scan of this file does not report its own examples
        ("ANTHROPIC_API_KEY=" + "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz", "credential-like string"),
        ('"api_key": ' + '"abcdefghijklmnopqrstuvwxyz0123"', "credential-like string"),
        ("AIza" + "b" * 35, "credential-like string"), ("-----BEGIN RSA " + "PRIVATE KEY-----", "credential-like string"), ("AKIA" + "A" * 16, "credential-like string"),
        ("OPENAI_API_KEY=" + "abcdefghijklmnopqrstuvwxyz012345", "credential-like string"), ("secret: " + "Zm9vYmFyYmF6cXV4cXV1eGNvcmdl", "credential-like string"),
        ("export AUTH_TOKEN=" + "abcdefghijklmnopqrstuvwxyz012345", "credential-like string"),
        ("see /Users/" + "jsmith/Projects/x.py", "home-directory path"), ("cd /home/" + "runner/work", "home-directory path"), ("C:\\Users\\" + "jsmith\\x", "home-directory path"),
        (f"NHS no. {real[:3]} {real[3:6]} {real[6:]}", "NHS number outside the 999 test range"), (f'"nhs_number": "{real}"', "NHS number outside the 999 test range"),
        ("contact: j.smith" + "@" + "example.org", "email address"), ("<first.last+tag" + "@" + "mail.nhs.uk>", "email address"),
        ("Author: C <someone" + "@" + "me.com>", "email address"),
    ]:  # fmt: skip
        assert kind in kinds(text), text


def test_it_leaves_test_range_numbers_timestamps_and_ordinary_text_alone():
    test_range = next(synthetic_nhs_numbers())
    for text in [f"NHS {test_range}", f"{test_range[:3]} {test_range[3:6]} {test_range[6:]}", "taken_at 1773404400, 1773406200 and 1773410400",
                 "sk-short", "token = 'abc'", '"max_tokens": "stopped_without_submit"', "/Users/Shared/x", "src/eprbench/ops.py", "phone 0123 456 7890",
                 "react@18.3.1", "uses actions/checkout@v4", "@pytest.mark.parametrize", "user @ host", "pip install x@1.2"]:  # fmt: skip
        assert kinds(text) == [], text
    invalid = "943 476 5911"  # NHS format, failing the check digit
    assert not is_valid_nhs_number(invalid) and kinds(invalid) == []
    for n in (f"{random.Random(i).randrange(10**10):010d}" for i in range(500)):  # the script's check digit is the package's
        assert scan.nhs_valid(n) == is_valid_nhs_number(n), n


def test_it_scans_the_tracked_tree_and_checks_the_canary(tmp_path):
    findings = scan.scan()
    tracked = set(scan.tracked())
    assert all(f.path in tracked for f in findings)
    assert not [f for f in findings if f.path.startswith("src/eprbench/data/tasks/preview-")]  # the preview tasks carry the canary
    names = [".env", ".envproduction", "deploy/.env.local", "config/prod.env"]
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("X=1\n")
    (tmp_path / "src/eprbench/data/tasks").mkdir(parents=True)
    (tmp_path / "src/eprbench/data/tasks/t.json").write_text("{}")
    found = scan.scan(tmp_path, [*names, "src/eprbench/data/tasks/t.json", "README.env.md"])
    assert sorted(f.path for f in found if f.kind == "tracked environment file") == sorted(names)
    assert {f.kind for f in found} == {"tracked environment file", "task or bundle file without the canary"}


def test_the_licences_notice_and_citation_must_be_tracked(tmp_path):
    assert not [f for f in scan.scan() if f.kind == "required release file missing"]  # all four are in the tracked tree
    (tmp_path / "LICENSE").write_text("x")
    (tmp_path / "NOTICE").write_text("x")
    found = scan.scan(tmp_path, ["LICENSE", "README.md"], required=scan.REQUIRED)
    assert sorted(f.path for f in found if f.kind == "required release file missing") == ["CITATION.cff", "LICENSE-CONTENT.md", "NOTICE"]  # NOTICE exists but is not tracked


def test_the_checklists_findings_section_is_what_a_fresh_scan_gives():
    """The checklist states no count by hand: its section is regenerated (release_scan.py --write-checklist)."""
    doc = (ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text()
    shown = doc[doc.index(scan.BEGIN) : doc.index(scan.END) + len(scan.END)]
    assert shown == scan.section(scan.scan(), scan.allowlist()), "run: uv run python scripts/release_scan.py --write-checklist"


def test_the_allowlist_covers_named_fixtures_only_and_cannot_go_stale(tmp_path):
    allowed = scan.allowlist()
    assert allowed and all(len(e["reason"]) > 20 for e in allowed)
    fake = "sk-" + "planted-0123456789abcdefgh"
    (tmp_path / "a.py").write_text(f"KEY = '{fake}'\n")
    (tmp_path / "b.py").write_text(f"KEY = '{fake}'\n")
    found = scan.scan(tmp_path, ["a.py", "b.py"])
    entry = {"path": "a.py", "kind": "credential-like string", "sha256": found[0].sha256, "reason": "a planted fake key"}
    gone = {"path": "c.py", "kind": "home-directory path", "sha256": "0" * 64, "reason": "a fixture since removed"}
    real, fixtures, stale = scan.split(found, [entry, gone])
    assert [f.path for f in real] == ["b.py"] and [f.path for f, _ in fixtures] == ["a.py"] and stale == [gone]  # the same text elsewhere is still found
    section = scan.section(found, [entry, gone])
    assert "Findings to resolve before publishing: 2." in section and fake not in section  # the section names no secret
    bad = tmp_path / "allow.json"
    bad.write_text(json.dumps({"allow": [{"path": "a.py", "kind": "credential-like string", "sha256": "x"}]}))
    with pytest.raises(ValueError, match="reason"):
        scan.allowlist(bad)


def test_the_public_tree_mode_scans_every_file_binary_included(tmp_path):
    """--public-tree scans an exported tree, not git's list: every file, the owner's account name and the macOS home
    prefix in any file (a PNG's metadata included), and files over 25 MB, as well as everything the tracked scan finds."""
    for name in scan.REQUIRED:
        (tmp_path / name).write_text("x\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("ok\nran in " + scan.LOCAL + "someone/x by " + scan.OWNER.upper() + "\n")
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16 + b"tEXtpath\0" + scan.LOCAL.encode() + scan.OWNER.encode()
    (tmp_path / "docs" / "shot.png").write_bytes(png)
    with open(tmp_path / "big.bin", "wb") as fh:
        fh.truncate(scan.MAX_BYTES + 1)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(scan.OWNER)  # never part of an export, never scanned
    found = {(f.path, f.kind, f.line) for f in scan.scan_public_tree(tmp_path)}
    assert found == {("docs/a.md", "home-directory path", 2), ("docs/a.md", "local path prefix", 2), ("docs/a.md", "the owner's account name", 2),
                     ("docs/shot.png", "local path prefix", 0), ("docs/shot.png", "the owner's account name", 0), ("big.bin", "file over 25 MB", 0)}  # fmt: skip
    assert scan.main(["--public-tree", str(tmp_path)]) == 1
    for p in ("docs/a.md", "docs/shot.png", "big.bin"):
        (tmp_path / p).unlink()
    assert scan.main(["--public-tree", str(tmp_path)]) == 0
