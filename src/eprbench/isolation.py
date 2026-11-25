"""Keeping an agent CLI away from the benchmark's ground truth, and noticing when it was not kept away.

An agent CLI runs as a local program with the user's permissions, and the task definitions, bundles (with their
hidden results), grader code and saved evidence are all files on the same machine. Two layers:

Prevention (macOS): the agent runs under `sandbox-exec` with a profile that denies reading or writing the
repository and its worktrees, the installed package and the evidence directory. Everything else, including the
network, stays allowed, because the agent must reach its own model API. Sandboxes cannot be nested on macOS, so a
CLI that sandboxes its own commands (Codex) is told not to. Copies of the repository elsewhere on disk, and the
internet, are not covered.

Detection (everywhere): the agent's captured output (its messages and the commands it ran) is scanned for the
benchmark canary, for distinctive strings from every task's grading checks, and for protected paths and file://
URLs. Ground-truth strings in the output mark the episode `contaminated`; a reference to a protected path marks it
contaminated too unless the sandbox was in force (then it is recorded as an attempt). Check ids seen in the
output are generic enough to arise by chance, so they only mark the episode `suspected`.

If the sandbox cannot be applied the agent is not run, unless the caller explicitly allows an unsandboxed run.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from eprbench.tasks import CANARY, list_tasks, load_task


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _caches() -> list[Path]:
    """Package caches that may hold a built copy of this package (uv's, pip's)."""
    found = []
    uv = os.environ.get("UV_CACHE_DIR") or (subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True).stdout.strip() if shutil.which("uv") else "")
    for path in (uv, "~/.cache/uv", "~/Library/Caches/pip", "~/.cache/pip"):
        if path and Path(path).expanduser().is_dir():
            found.append(Path(path).expanduser().resolve())
    return found


def protected_paths(extra=()) -> list[str]:
    """Where the ground truth is on this machine: the package, its git repository with every worktree, package caches
    that may hold a built copy, and `extra`. Copies anywhere else (other clones, other environments) are not known."""
    pkg = Path(str(resources.files("eprbench"))).resolve()
    paths = {pkg, *_caches()}
    top = _git(pkg, "rev-parse", "--show-toplevel").strip()
    if top:
        paths.add(Path(top).resolve())
        common = _git(pkg, "rev-parse", "--git-common-dir").strip()
        if common:
            paths.add((Path(top) / common).resolve())
        for line in _git(pkg, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                paths.add(Path(line.removeprefix("worktree ")).resolve())
    paths.update(Path(p).resolve() for p in extra)
    return sorted(str(p) for p in paths)


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(protected: list[str]) -> str:
    """A Seatbelt profile: everything allowed except the protected paths. When this interpreter's virtualenv lies
    inside one (a .venv in the repository), only what starting it needs is let back in (its bin directory and
    pyvenv.cfg), so a Python program can run with -S; its site-packages, which point at the package, stay closed."""
    deny = " ".join(f"(subpath {_quote(p)})" for p in protected)
    rules = ["(version 1)", "(allow default)", f"(deny file-read* file-write* {deny})"]
    prefix = os.path.realpath(sys.prefix)
    if prefix != os.path.realpath(sys.base_prefix) and any(prefix.startswith(p + os.sep) for p in protected):
        rules.append(f"(allow file-read* (literal {_quote(prefix)}) (literal {_quote(prefix + '/pyvenv.cfg')}) (subpath {_quote(prefix + '/bin')}))")
    return "".join(rules)


def sandbox(protected: list[str]) -> tuple[list[str], str | None]:
    """A command prefix that stops what follows reading the protected paths, and None; or [] and why not.

    The sandbox is tested on the spot: a profile that fails to apply, or fails to block a protected file, is not used.
    """
    exe = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or exe is None:
        return [], "sandbox-exec is not available on this platform"
    rules = profile(protected)
    probe = Path(str(resources.files("eprbench").joinpath("tasks.py")))
    try:
        ok = subprocess.run([exe, "-p", "(version 1)(allow default)", "/usr/bin/true"], capture_output=True, timeout=30)
        if ok.returncode != 0:
            return [], "sandbox-exec cannot apply here (already inside a sandbox?): " + ok.stderr.decode(errors="replace").strip()
        leak = subprocess.run([exe, "-p", rules, "/bin/cat", str(probe)], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"sandbox-exec failed: {e}"
    if leak.returncode == 0:
        return [], "the sandbox profile did not block a protected file"
    return [exe, "-p", rules], None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fingerprints() -> list[tuple[str, str, bool]]:
    """(label, string, distinctive) triples that someone who had read the task files or grader might write.
    Distinctive ones (the canary, check SQL, long answer patterns) are evidence; check ids are only grounds for suspicion,
    since an agent can arrive at "result_acknowledged" by itself."""
    found = [("canary", CANARY, True), ("canary", CANARY.split()[1], True)]
    for task_id in list_tasks():
        for check in load_task(task_id).checks:
            if len(check.id) >= 10 and "_" in check.id:
                found.append((f"{task_id} check id", check.id, False))
            if check.sql:
                found.append((f"{task_id} check SQL", _squash(check.sql)[:60], True))
            if check.answer_regex and len(check.answer_regex) >= 12:
                found.append((f"{task_id} answer pattern", check.answer_regex, True))
    return found


def scan(text: str, protected: list[str]) -> dict:
    """Ground truth seen in `text`: distinctive (`content`) or only suspicious (`suspected`), and references to
    protected places (`paths`). Text quoted once inside JSON (as in an event stream) is matched too."""
    unquoted = text.replace("\\n", " ").replace("\\t", " ").replace('\\"', '"').replace("\\\\", "\\")
    hay, lowered = _squash(text) + "\n" + _squash(unquoted), text.lower()
    hits = {(label + ": " + s, distinctive) for label, s, distinctive in fingerprints() if s in hay or json.dumps(s)[1:-1] in text}
    paths = sorted({p for p in protected if p.lower() in lowered})
    for marker in ("file://", "/data/tasks/", "/data/bundles/", "site-packages/eprbench", "src/eprbench"):
        if marker in lowered:
            paths.append(marker)
    return {"content": sorted(h for h, d in hits if d), "suspected": sorted(h for h, d in hits if not d), "paths": paths}
