"""No test may start a real agent CLI (Claude Code, Codex): it would spend the owner's subscription.

Reading command text cannot be the defence: a shell computes its commands and a script's contents are never seen.
So the real CLIs are made unreachable instead, from when conftest.py is imported (before any test module is
collected) to the end of the session:

1. Where they are. The real executables are found once, before anything else changes: on PATH, in the places their
   installers use (every version Claude Code keeps; the native binaries inside Codex's npm package), and wherever
   EPRBENCH_CLAUDE_BIN or EPRBENCH_CODEX_BIN pointed. Each is known by its resolved path and by its file identity
   (device and inode), so another name or a hard link for it is the same file.
2. Only stubs can be found. Stub `claude` and `codex` executables (they refuse, exit 97) come first on PATH, and
   EPRBENCH_CLAUDE_BIN / EPRBENCH_CODEX_BIN name them. The harness locates a preset CLI through those variables alone
   when they are set, never by a PATH search (eprbench.agents.cli_agent.cli_executable). Every environment this process
   gives a child is rewritten so that it still has the stubs first on PATH and the variables naming them; a child
   environment whose variable names a real CLI is refused. So a computed command, a child script, or a child given
   another PATH by this process still finds only a stub.
3. A check at exec time. Every way this process can start a program (subprocess.Popen and so run, call, check_output
   and asyncio's subprocesses; os.system, os.popen, the os.exec*, os.spawn* and os.posix_spawn* families; pty.spawn) is
   wrapped. The program that would run is resolved as exec would resolve it (a path as given; a bare name through the
   PATH of the environment the child gets; `executable=`; /bin/sh for a shell) and refused when that file is a real
   CLI. As a last, heuristic layer the command's words are still scanned for the CLIs' names (a literal `claude -p` in
   a shell string), with only an exact `[cli, "--version"]` or `[cli, "--help"]` let through.

What this does not stop, stated plainly: a test written to defeat it can. A grandchild that sets its own PATH (or
calls a real CLI by its absolute path, computed so that no word of it shows) runs outside this process, where the
check at exec time cannot see; so can a call through ctypes or _posixsubprocess.fork_exec, a test that restores the
originals kept in `_ORIGINAL`, a copy (not a link) of a real binary, or an interpreter started on a real CLI's script
(`node .../codex.js`) by a computed path. The stubs and the variables stop every accidental route; the rest would take
intent, and code review is the defence against that.
"""

import asyncio
import glob
import os
import pty
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

NAMES = ("claude", "codex")
BIN_ENV = {"claude": "EPRBENCH_CLAUDE_BIN", "codex": "EPRBENCH_CODEX_BIN"}  # read by eprbench.agents.cli_agent.cli_executable
ALLOWED = ("--version", "--help", "-h", "-V")
STUB = "#!/bin/sh\necho \"the real {name} CLI is not available in tests (tests/agent_cli_guard.py)\" >&2\nexit 97\n"


def _identity(path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _candidates() -> set[str]:
    """Every place a real CLI may be on this machine, looked for before the stubs go on PATH."""
    home = Path.home()
    found = {shutil.which(n) for n in NAMES} | {os.environ.get(v) for v in BIN_ENV.values()}
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", home / ".local/bin", home / ".npm-global/bin", home / ".bun/bin", home / ".claude/local"):
        found |= {str(Path(d) / n) for n in NAMES}
    found |= set(glob.glob(str(home / ".local/share/claude/versions/*")))  # Claude Code's native installer keeps every version
    for exe in [p for p in list(found) if p and Path(p).name == "codex" and os.path.exists(p)]:  # Codex's npm package: its script and native binaries
        package = Path(os.path.realpath(exe)).parent.parent
        if (package / "package.json").exists():
            found |= {p for p in glob.glob(str(package / "**" / "codex*"), recursive=True) if os.path.isfile(p) and os.access(p, os.X_OK)}
    return {p for p in found if p and os.path.isfile(p)}


REAL = {os.path.realpath(p) for p in _candidates()}  # resolved paths of the real executables
REAL_IDS = {i for i in map(_identity, REAL) if i}  # and their file identities, so a hard link is the same file
_ORIGINAL: dict = {}  # the real process-starting functions, kept so the guard (and its tests) can call through
STUBS: str | None = None


class Refused(AssertionError):
    """A test tried to start a real agent CLI."""


def _text(x) -> str:
    if isinstance(x, (bytes, bytearray)):
        return bytes(x).decode(errors="replace")
    return os.fspath(x) if isinstance(x, os.PathLike) else str(x)


def is_real(path) -> bool:
    """Whether the file at `path` is a real agent CLI: by resolved path or by file identity."""
    if not path:
        return False
    path = _text(path)
    return os.path.realpath(path) in REAL or (os.path.exists(path) and _identity(path) in REAL_IDS)


def _env_get(env, key: str):
    if env is None:
        return os.environ.get(key)
    return env.get(key) if key in env else env.get(key.encode()) if hasattr(env, "get") else None


def resolve(program, env=None) -> str | None:
    """The file exec would run for `program`: a path as given; a bare name through the PATH of `env` (the child's
    environment; this process's when None)."""
    program = _text(program)
    if not program:
        return None
    if "/" in program:
        return program
    path = _env_get(env, "PATH")
    return shutil.which(program, path=_text(path) if path is not None else os.defpath)


def check_exec(program, env=None) -> None:
    """Refuse at exec time if the program that would run is a real CLI, or the child's environment points at one."""
    exe = resolve(program, env)
    if exe and is_real(exe):
        raise Refused(f"a test tried to start a real agent CLI: {_text(program)} resolves to {os.path.realpath(exe)}")
    if env is not None:
        for var in BIN_ENV.values():
            value = _env_get(env, var)
            if value and is_real(value):
                raise Refused(f"a test gave a child {var}={_text(value)}, a real agent CLI")


def child_env(env):
    """The environment a child is given, rewritten to keep the stubs first on PATH and the variables naming them (None,
    inheriting this process's environment, already has both)."""
    if env is None or STUBS is None:
        return env
    out = {_text(k): _text(v) for k, v in dict(env).items()}
    rest = [p for p in out.get("PATH", os.defpath).split(os.pathsep) if p and p != STUBS]
    out["PATH"] = os.pathsep.join([STUBS, *rest])
    for name, var in BIN_ENV.items():
        out[var] = os.path.join(STUBS, name)
    return out


# --- the last, heuristic layer: the words of the command ---------------------------------------------------------------


def _words(command) -> list[str]:
    """Every word of a command, a shell string's (and any word that is itself a command line's) included."""
    parts = [_text(command)] if isinstance(command, (str, bytes, bytearray, os.PathLike)) else [_text(a) for a in command]
    words, todo = [], list(parts)
    while todo:
        part = todo.pop()
        words.append(part)
        if any(c in part for c in " \t\n;&|`$()"):
            try:
                split = shlex.split(part, posix=True)
            except ValueError:
                split = part.replace(";", " ").replace("&", " ").replace("|", " ").split()
            todo += [w for w in split if w != part]
            words += re.findall(r"[\w./~+-]+", part)  # a program named inside code or quoting (python -c "os.system('codex ...')")
    return words


def _is_cli(word: str) -> bool:
    word = word.strip().strip("'\"")
    if not word or any(c.isspace() for c in word):  # a command line, not a word: its words are checked one by one
        return False
    if Path(word).name in NAMES and (os.sep not in word or is_real(word) or not os.path.exists(word)):
        return True
    return os.path.exists(word) and is_real(word)


def check(command, shell: bool = False) -> None:
    """Refuse `command` if any word of it names a real agent CLI, except an exact version or help query."""
    if not shell and not isinstance(command, (str, bytes, bytearray)):
        argv = [_text(a) for a in command]
        if len(argv) == 2 and _is_cli(argv[0]) and argv[1] in ALLOWED:
            return
    hits = [w for w in _words(command) if _is_cli(w)]
    if hits:
        raise Refused(f"a test tried to start a real agent CLI ({hits[0]}): {_text(command)[:300] if isinstance(command, (str, bytes)) else [_text(a) for a in command][:8]}")


# --- the wrappers ------------------------------------------------------------------------------------------------------

_SHELL, _ENV = 7, 9  # Popen's positional parameters after args: shell and env


class GuardedPopen(subprocess.Popen):
    def __init__(self, args, *a, **kw):
        a = list(a)
        shell = kw.get("shell", False) or (len(a) > _SHELL and a[_SHELL])
        check(args, shell)
        env = kw.get("env") if "env" in kw else (a[_ENV] if len(a) > _ENV else None)
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        program = kw.get("executable") or (len(a) > 1 and a[1]) or ("/bin/sh" if shell else (argv[0] if argv else None))
        check_exec(program, env)
        if not shell and kw.get("executable") and argv:
            check_exec(argv[0], env)  # what the child calls itself does not run, but refuse a real CLI named there too
        if "env" in kw or len(a) <= _ENV:
            kw["env"] = child_env(env)
        else:
            a[_ENV] = child_env(env)
        super().__init__(args, *a, **kw)


def _wrap(name, getargs):
    """`getargs(*a, **kw)` gives (command, shell, program, env position) for a call of `name`: the env position is the
    index of the child's environment among the positional arguments, -1 for the last, or None when it has none."""

    def guarded(*a, **kw):
        command, shell, program, at = getargs(*a, **kw)
        check(command, shell)
        a = list(a)
        env = None
        if at is not None and a and (at == -1 and isinstance(a[-1], dict) or at != -1 and len(a) > at):
            env = a[at]
        elif "env" in kw:
            env = kw["env"]
        check_exec(program, env)
        if env is not None:
            if "env" in kw:
                kw["env"] = child_env(env)
            else:
                a[at] = child_env(env)
        return _ORIGINAL[name](*a, **kw)

    guarded.__name__ = name
    return guarded


def _argv(path, args):
    return [path, *(list(args)[1:] if args else [])]


def _rest(args):
    return [a for a in args if not isinstance(a, dict)]


TABLE = {
    "system": lambda cmd: (cmd, True, "/bin/sh", None),
    "popen": lambda cmd, *a, **kw: (cmd, True, "/bin/sh", None),
    **{n: (lambda path, args, *r, **kw: (_argv(path, args), False, path, 2)) for n in ("execv", "execve", "execvp", "execvpe")},
    **{n: (lambda path, *args: (_argv(path, _rest(args)), False, path, -1)) for n in ("execl", "execle", "execlp", "execlpe")},
    **{n: (lambda mode, path, args, *r, **kw: (_argv(path, args), False, path, 3)) for n in ("spawnv", "spawnve", "spawnvp", "spawnvpe")},
    **{n: (lambda mode, path, *args: (_argv(path, _rest(args)), False, path, -1)) for n in ("spawnl", "spawnle", "spawnlp", "spawnlpe")},
    **{n: (lambda path, argv, *r, **kw: (_argv(path, argv), False, path, 2)) for n in ("posix_spawn", "posix_spawnp")},
}  # fmt: skip


def install() -> None:
    """Wrap every way to start a program (idempotent)."""
    if _ORIGINAL:
        return
    _ORIGINAL["Popen"] = subprocess.Popen
    subprocess.Popen = GuardedPopen
    for name, getargs in TABLE.items():
        if hasattr(os, name):
            _ORIGINAL[name] = getattr(os, name)
            setattr(os, name, _wrap(name, getargs))
    _ORIGINAL["pty.spawn"] = pty.spawn

    def pty_args(argv, *a, **kw):
        argv = [argv] if isinstance(argv, (str, bytes)) else list(argv)
        return argv, False, argv[0] if argv else None, None

    pty.spawn = _wrap("pty.spawn", pty_args)
    _ORIGINAL["asyncio.create_subprocess_exec"] = asyncio.create_subprocess_exec
    _ORIGINAL["asyncio.create_subprocess_shell"] = asyncio.create_subprocess_shell

    async def exec_(program, *args, **kw):  # its child is started by subprocess.Popen, so GuardedPopen checks it again
        check([program, *args])
        check_exec(program, kw.get("env"))
        return await _ORIGINAL["asyncio.create_subprocess_exec"](program, *args, **kw)

    async def shell_(cmd, **kw):
        check(cmd, True)
        return await _ORIGINAL["asyncio.create_subprocess_shell"](cmd, **kw)

    asyncio.create_subprocess_exec, asyncio.create_subprocess_shell = exec_, shell_
    for module in (asyncio.subprocess,):
        module.create_subprocess_exec, module.create_subprocess_shell = exec_, shell_


def install_stubs() -> str:
    """Stub claude and codex first on PATH, and the harness's variables naming them, for the rest of the session."""
    stubs = os.path.realpath(tempfile.mkdtemp(prefix="eprbench-cli-stubs-"))
    for name in NAMES:
        path = Path(stubs) / name
        path.write_text(STUB.format(name=name))
        path.chmod(0o755)
    os.environ["PATH"] = stubs + os.pathsep + os.environ.get("PATH", "")
    for name, var in BIN_ENV.items():
        os.environ[var] = str(Path(stubs) / name)
    return stubs


def activate() -> None:
    global STUBS
    install()
    if STUBS is None:
        STUBS = install_stubs()
