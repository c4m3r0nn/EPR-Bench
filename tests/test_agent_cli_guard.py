"""The guard that stops tests starting a real agent CLI (tests/agent_cli_guard.py), against every bypass the review of
2026-11-21 found and the others it asked for. Underneath the guard, the real process-starting functions are replaced by
recorders for each test, so even a guard failure starts nothing."""

import asyncio
import os
import pty
import shutil
import subprocess
import sys

import pytest

from tests import agent_cli_guard as guard

REAL = sorted(guard.REAL) or ["/nonexistent/bin/claude"]


@pytest.fixture
def started(monkeypatch):
    """What would have been started, had the guard let it through."""
    calls = []

    monkeypatch.setattr(guard._ORIGINAL["Popen"], "__init__", lambda self, args, *a, **kw: calls.append(args))  # beneath the guard
    for name in list(guard._ORIGINAL):
        monkeypatch.setitem(guard._ORIGINAL, name, lambda *a, **kw: calls.append(a) or 0)
    return calls


@pytest.mark.parametrize("command, kw", [
    ("claude -p hello", {"shell": True}),
    (b"claude -p hello", {"shell": True}),
    (["sh", "-c", "codex exec hello"], {}),
    (["/bin/zsh", "-lc", "cd /tmp && claude -p hi"], {}),
    ([b"claude", b"-p", b"hello"], {}),
    (["env", "FOO=1", "claude", "-p", "hello"], {}),
    (["uv", "run", "codex", "exec", "hi"], {}),
    (["claude", "--version", "-p", "hello"], {}),  # a --version anywhere no longer exempts the whole command
    (["codex", "--help", "exec", "hello"], {}),
    (["/usr/bin/sandbox-exec", "-p", "(version 1)", "claude", "-p", "x"], {}),
    ([REAL[0], "-p", "hello"], {}),
    (["python3", "-c", "import os; os.system('codex exec hi')"], {}),
])  # fmt: skip
def test_popen_in_every_form_is_refused(started, command, kw):
    with pytest.raises(guard.Refused):
        subprocess.Popen(command, **kw)
    with pytest.raises(guard.Refused):
        subprocess.run(command, **kw)
    assert started == []


def test_exact_version_and_help_queries_are_let_through(started):
    for argv in (["claude", "--version"], ["codex", "--help"], ["codex", "-V"]):
        subprocess.Popen(argv)
    assert len(started) == 3


@pytest.mark.parametrize("call", [
    lambda: os.system("claude -p hello"),
    lambda: os.popen("codex exec hello"),
    lambda: os.execv("/bin/sh", ["sh", "-c", "claude -p hi"]),
    lambda: os.execvp("claude", ["claude", "-p", "hi"]),
    lambda: os.execl(REAL[0], REAL[0], "-p", "hi"),
    lambda: os.execlp("codex", "codex", "exec", "hi"),
    lambda: os.spawnv(os.P_NOWAIT, "/bin/sh", ["sh", "-c", "codex exec hi"]),
    lambda: os.spawnlp(os.P_NOWAIT, "claude", "claude", "-p", "hi"),
    lambda: os.posix_spawn("/bin/sh", ["sh", "-c", "claude -p hi"], dict(os.environ)),
    lambda: os.posix_spawnp("codex", ["codex", "exec", "hi"], dict(os.environ)),
    lambda: pty.spawn(["claude", "-p", "hi"]),
    lambda: asyncio.run(asyncio.create_subprocess_exec("claude", "-p", "hi")),
    lambda: asyncio.run(asyncio.create_subprocess_shell("codex exec hi")),
])  # fmt: skip
def test_every_other_way_to_start_a_program_is_guarded(started, call):
    with pytest.raises(guard.Refused):
        call()
    assert started == []


def test_ordinary_commands_still_run():
    assert subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True).stdout == "ok\n"
    assert subprocess.run("echo docs/codex", shell=True, capture_output=True, text=True).stdout == "docs/codex\n"  # a path, not the CLI


def test_the_stubs_come_first_on_path_and_refuse():
    for name in ("claude", "codex"):
        found = shutil.which(name)
        assert found and found.startswith(guard.STUBS)
        done = subprocess.run([name, "--version"], capture_output=True, text=True)  # allowed through layer 1: the stub answers
        assert done.returncode == 97 and "not available in tests" in done.stderr


def test_the_guard_is_in_place_before_test_modules_are_collected():
    import tests.conftest  # noqa: F401  (conftest activates the guard at import, before pytest collects any test module)

    assert subprocess.Popen is guard.GuardedPopen and guard.STUBS and os.environ["PATH"].startswith(guard.STUBS)


# --- the audit of the paired run (docs/codex/paired-run-2026-11-21): command text is not the defence -----------------
# These start real processes through the guard. Each asks only for --version, so even a guard failure would cost nothing.


def test_the_harness_finds_the_clis_only_through_the_variables_which_name_the_stubs():
    from eprbench.agents import cli_agent, cli_browser

    for name, var in guard.BIN_ENV.items():
        assert os.environ[var] == os.path.join(guard.STUBS, name)
        assert cli_agent.cli_executable(name) == os.environ[var]
        assert cli_agent.preset_argv(name, False, None, None)[0] == os.environ[var]
        assert cli_browser.preset_argv(name, False, None, None)[0] == os.environ[var]
        assert cli_agent.cli_version(cli_agent.cli_executable(name)) is not None  # the stub answers (exit 97), nothing else


def test_a_set_variable_is_never_followed_by_a_path_search(monkeypatch):
    from eprbench.agents import cli_agent

    monkeypatch.setenv("EPRBENCH_CLAUDE_BIN", "/nonexistent/claude-bin")
    assert cli_agent.cli_executable("claude") == "/nonexistent/claude-bin"
    assert cli_agent.preset_argv("claude", False, None, None)[0] == "/nonexistent/claude-bin"
    assert cli_agent.cli_version(cli_agent.cli_executable("claude")) is None  # not found: no fallback to a claude on PATH


@pytest.mark.parametrize("command", [
    ["sh", "-c", "c=cla; ${c}ude --version"],  # a computed command: no word of it names the CLI
    ["sh", "-c", 'eval "$(printf "%s%s" co dex) --version"'],
])  # fmt: skip
def test_a_computed_shell_command_reaches_only_a_stub(command):
    done = subprocess.run(command, capture_output=True, text=True)
    assert done.returncode == 97 and "not available in tests" in done.stderr


def test_an_unchecked_child_script_reaches_only_a_stub(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\nexec $(echo Y2xhdWRl | base64 -d) --version\n")  # "claude", decoded only by the child
    done = subprocess.run(["sh", str(script)], capture_output=True, text=True)
    assert done.returncode == 97 and "not available in tests" in done.stderr


def test_a_child_given_another_path_still_finds_the_stubs_first():
    real_dirs = [os.path.dirname(p) for p in REAL if os.sep in p]
    env = {**os.environ, "PATH": os.pathsep.join([*real_dirs, "/usr/bin", "/bin"])}
    done = subprocess.run(["sh", "-c", "c=cod; ${c}ex --version"], env=env, capture_output=True, text=True)
    assert done.returncode == 97 and "not available in tests" in done.stderr


def test_a_childs_environment_is_rewritten_to_keep_the_stubs_and_the_variables():
    env = guard.child_env({"PATH": "/usr/bin:/bin", "HOME": "/h"})
    assert env["PATH"].split(os.pathsep)[0] == guard.STUBS and env["HOME"] == "/h"
    assert all(env[v] == os.path.join(guard.STUBS, n) for n, v in guard.BIN_ENV.items())
    assert guard.child_env(None) is None  # it inherits os.environ, which already has them


@pytest.fixture
def fake_real(tmp_path, monkeypatch):
    """A stand-in for a real CLI's executable, known to the guard as one (so this runs wherever the real CLIs are absent)."""
    exe = tmp_path / "versions" / "9.9.9"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho REAL-CLI-RAN\n")
    exe.chmod(0o755)
    monkeypatch.setattr(guard, "REAL", guard.REAL | {os.path.realpath(exe)})
    monkeypatch.setattr(guard, "REAL_IDS", guard.REAL_IDS | {guard._identity(exe)})
    return exe


def test_any_name_or_link_resolving_to_a_real_cli_is_refused_at_exec(fake_real, tmp_path, started):
    link = tmp_path / "helper"
    link.symlink_to(fake_real)
    hard = tmp_path / "copy-by-link"
    os.link(fake_real, hard)  # another name for the same file: refused by identity, not by name
    for argv, kw in [([str(link)], {}), (["helper"], {"env": {"PATH": str(tmp_path)}}), ([str(hard), "x"], {}), (["ls"], {"executable": str(link)})]:
        with pytest.raises(guard.Refused):
            subprocess.Popen(argv, **kw)
    with pytest.raises(guard.Refused):
        os.posix_spawnp("helper", ["helper"], {"PATH": str(tmp_path)})
    with pytest.raises(guard.Refused):
        os.execve(str(hard), [str(hard)], {})
    with pytest.raises(guard.Refused):  # a child's variable pointed back at a real CLI
        subprocess.Popen(["sh", "-c", "true"], env={**os.environ, "EPRBENCH_CLAUDE_BIN": str(link)})
    assert started == []
