"""treg shell — transparent CLI interception (shell mode), MVP / Phase 1.

Open a subshell in which a team's REGISTERED CLIs (`stripe`, `gh`, `neonctl`, …) run as if they were
installed locally with the team credential, without the member ever holding the key or typing
`treg run`. The mechanic (docs/CLI-SHELL-MODE-PLAN.md): put a private **shim directory first on PATH**
holding one wrapper per registered CLI. The shell resolves a command name against PATH in order, so a
registered CLI finds our shim first and is routed through treg; any other command has no shim and
resolves normally — the "is this registered?" test is done for free by name resolution.

Phase 1 keeps it correct and simple: each shim runs `treg run <tool> -- "$@"`, reusing the whole
local-run path (grant / deny / runner-proof / audit / metering). The in-memory session agent (no key
on disk, ~ms per call) is Phase 2. Nothing here holds a credential — `treg run` fetches the grant per
call exactly as it does today.

Loop avoidance (the one correctness subtlety): the shim dir is first on the SUBSHELL's PATH, but the
shim invokes `treg run` with `PATH=$TREG_SHELL_REALPATH` — the original PATH captured BEFORE the shim
dir was prepended. So `treg run`'s own `shutil.which(<bin>)` resolves the REAL binary, never the shim,
and there is no recursion.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# Banner colour (ANSI only to a real terminal that hasn't opted out; the banner prints to stderr).
# Palette matches the CLI's onboarding chrome: clay accent, green, teal, muted.
def _ansi(code: str) -> str:
    return code if (sys.stderr.isatty() and not os.environ.get("NO_COLOR")) else ""


_CLAY = _ansi("\033[38;2;224;112;63m")
_GREEN = _ansi("\033[38;2;127;174;114m")
_TEAL = _ansi("\033[38;2;95;158;160m")
_MUTED = _ansi("\033[38;2;169;158;136m")
_BOLD, _RESET = _ansi("\033[1m"), _ansi("\033[0m")

# Env vars published into the subshell. TREG_SHELL marks an active session (blocks nesting); _DIR is
# the private session dir (shims live in <dir>/bin); _REALPATH is the clean PATH the shims hand to
# `treg run`; _PID is the `treg shell start` process a `treg shell stop` signals to tear down.
ENV_ACTIVE = "TREG_SHELL"
ENV_DIR = "TREG_SHELL_DIR"
ENV_REALPATH = "TREG_SHELL_REALPATH"
ENV_PID = "TREG_SHELL_PID"

# Sourced by the subshell so the user's normal environment is intact, our shims stay FIRST on PATH
# even if their rc reset it, and the prompt shows a `(treg)` marker. Kept per-shell because the prompt
# variable + config file differ. `$TREG_SHELL_DIR` is read from the env we pass in.
_ZSH_RC = (
    "# treg shell (generated) — restore the user's zsh env, keep our shims first, mark the prompt.\n"
    'ZDOTDIR="$HOME"\n'
    '[ -f "$HOME/.zshrc" ] && source "$HOME/.zshrc"\n'
    'export PATH="$TREG_SHELL_DIR/bin:$PATH"\n'
    'PROMPT="%F{cyan}(treg)%f $PROMPT"\n'
)
_BASH_RC = (
    "# treg shell (generated) — restore the user's bash env, keep our shims first, mark the prompt.\n"
    '[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc"\n'
    'export PATH="$TREG_SHELL_DIR/bin:$PATH"\n'
    'PS1="\\[\\e[36m\\](treg)\\[\\e[0m\\] $PS1"\n'
)


def session_base_dir() -> str:
    """The per-user base under which a session dir is created. Prefer a private, per-user location
    (`XDG_RUNTIME_DIR` on Linux, `TMPDIR` on macOS — both 0700-ish and not world-listable) over a
    shared `/tmp`."""
    for key in ("XDG_RUNTIME_DIR", "TMPDIR"):
        v = os.environ.get(key)
        if v and os.path.isdir(v):
            return v
    return tempfile.gettempdir()


def plan_shims(tools: list[dict], server_for: frozenset[str] = frozenset()) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Decide which CLIs to shadow and where each runs. From `GET /tools`, keep every tool with a
    `cli.bin` that is `cli.enabled` (owner opt-in for local runs — a non-enabled tool's `treg run`
    would 403). A tool named in `server_for` (by bin OR tool name) routes to the SERVER (`treg run
    --server` — the key never touches the machine, output is streamed back) IF it is `server_runnable`;
    otherwise it falls back to local with a warning. Returns `(entries, warnings)` where entries is a
    sorted list of `(bin, tool_name, route)` with route in {'local','server'}. First tool wins if two
    claim the same bin; a bin that isn't a plain filename is skipped (a shim is a file we write)."""
    entries: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for t in tools:
        cli = t.get("cli") or {}
        bin_ = cli.get("bin")
        if not bin_ or not cli.get("enabled"):
            continue
        if os.sep in bin_ or (os.altsep and os.altsep in bin_) or bin_ in (".", ".."):
            continue  # never let a bin name escape the shim dir
        if bin_ in seen:
            continue
        seen.add(bin_)
        name = t["name"]
        want_server = bin_ in server_for or name in server_for
        if want_server and not t.get("server_runnable"):
            warnings.append(f"{bin_}: can't run on the server (not server-runnable) — running it locally.")
            route = "local"
        else:
            route = "server" if want_server else "local"
        entries.append((bin_, name, route))
    return sorted(entries), warnings


def shim_script(tool_name: str, treg_bin: str, route: str = "local", real_bin: str | None = None) -> str:
    """A shim's contents: exec `treg run [--server] <tool> -- "$@"` on a CLEAN PATH (so `treg run`
    finds the real binary, not this shim). `--server` runs the CLI on the registry server (key never
    on the machine); default is local. The tier flag goes BEFORE the tool name (treg's own rule) and
    the literal `--` fences treg's parsing from the user's args, which `treg run` then strips — so the
    real CLI receives exactly what the user typed. `exec` replaces the process, so the CLI owns the
    terminal and returns its own exit code (faithful passthrough).

    When `real_bin` is known, shell-completion calls (cobra's `__complete*`, used by gh and most Go
    CLIs) exec the REAL binary directly — completion is local shell metadata that needs no credential,
    and routing it through treg would flood the audit and burn the daily cap one keystroke at a time.
    Completion is always local, even for a server-routed tool."""
    tier = "--server " if route == "server" else ""
    where = "on the registry server" if route == "server" else "locally"
    bypass = ""
    if real_bin:
        bypass = f'case "$1" in __complete*) exec {shlex.quote(real_bin)} "$@" ;; esac\n'
    return (
        "#!/bin/sh\n"
        f"# treg shell shim → tool {tool_name} (runs {where}). Injects the team credential via `treg run`.\n"
        "# Generated by `treg shell start`; do not edit — it vanishes when the shell exits.\n"
        f"{bypass}"
        f'exec env PATH="${ENV_REALPATH}" {shlex.quote(treg_bin)} run {tier}{shlex.quote(tool_name)} -- "$@"\n'
    )


def write_shims(shim_dir: str, entries: list[tuple[str, str, str]], treg_bin: str) -> None:
    """Write one executable shim per (bin, tool, route) into `shim_dir`. The real binary is resolved
    HERE from the current PATH (which does not yet contain the shim dir), so completion calls can
    exec it directly and treg run never recurses into the shim."""
    for bin_, tool_name, route in entries:
        real_bin = shutil.which(bin_)  # the true binary (parent PATH has no shim dir yet)
        p = Path(shim_dir) / bin_
        p.write_text(shim_script(tool_name, treg_bin, route, real_bin))
        os.chmod(p, 0o755)


def _shell_argv(session_dir: str, env: dict) -> list[str]:
    """Build the interactive-subshell argv and, per shell, drop in a prompt/PATH rc. zsh reads
    `$ZDOTDIR/.zshrc`, so we point ZDOTDIR at the session dir; bash takes `--rcfile`. An unknown shell
    gets a plain interactive session (env still set, no prompt marker)."""
    shell_path = env.get("SHELL") or "/bin/sh"
    base = os.path.basename(shell_path)
    if base == "zsh":
        (Path(session_dir) / ".zshrc").write_text(_ZSH_RC)
        env["ZDOTDIR"] = session_dir
        return [shell_path, "-i"]
    if base == "bash":
        rc = Path(session_dir) / "bashrc"
        rc.write_text(_BASH_RC)
        return [shell_path, "--rcfile", str(rc), "-i"]
    return [shell_path, "-i"]


def _print_banner(entries: list[tuple[str, str, str]], ttl_minutes: int | None) -> None:
    """The entry banner: state the intent (what this shell does), list the injected CLIs (marking any
    that run on the server), and give the reminders (other commands are untouched; how to leave)."""
    def _label(bin_: str, route: str) -> str:
        return f"{_GREEN}{bin_}{_RESET}{_TEAL} (server){_RESET}" if route == "server" else f"{_GREEN}{bin_}{_RESET}"
    names = "  ".join(_label(b, route) for b, _, route in entries)
    out = sys.stderr
    print(f"\n{_CLAY}{_BOLD}▚ treg shell{_RESET}{_MUTED} — you're now in a shell where your team's CLIs just work.{_RESET}", file=out)
    print(f"{_MUTED}  The tools below run with the team credential injected for you — no `treg run`,{_RESET}", file=out)
    print(f"{_MUTED}  no keys on this machine, and every call is audited.{_RESET}", file=out)
    print(f"\n  {_BOLD}Injected here ({len(entries)}):{_RESET}  {names}", file=out)
    print(f"{_MUTED}  A CLI marked (server) runs on the registry, not your machine. Everything else{_RESET}", file=out)
    print(f"{_MUTED}  (ls, git, your own tools) behaves exactly as usual.{_RESET}", file=out)
    print("", file=out)
    if ttl_minutes:
        print(f"{_MUTED}  This shell closes automatically in {ttl_minutes} min.{_RESET}", file=out)
    print(f"{_MUTED}  Leave any time with{_RESET} exit {_MUTED}(or Ctrl-D) — your normal shell returns unchanged.{_RESET}\n", file=out)


def _teardown(session_dir: str) -> None:
    """Remove the session dir (shims, rc files) — idempotent. There is no credential on disk to wipe:
    Phase 1 holds nothing; `treg run` fetches each grant fresh and never persists it."""
    shutil.rmtree(session_dir, ignore_errors=True)


def _run_subshell(argv: list[str], env: dict, ttl_seconds: int | None = None) -> int:
    """Run the interactive subshell as a child and return its exit code. The parent IGNORES SIGINT /
    SIGQUIT (they belong to whatever runs in the foreground of the interactive shell) and handles
    SIGTERM / SIGHUP by stopping the child — so `treg shell stop` (which signals this process) and a
    closed terminal both return control here for teardown, with no orphaned subshell. A `ttl_seconds`
    hard cap closes the session automatically when it elapses (a daemon timer that terminates the
    child); it is a convenience only — Phase-1 holds no credential, so nothing is left to time out."""
    proc = subprocess.Popen(argv, env=env)  # noqa: S603 — argv list, no shell

    def _stop(_signum, _frame):
        if proc.poll() is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass

    def _on_ttl():
        if proc.poll() is None:
            print("\n▚ treg shell reached its time limit — closing.", file=sys.stderr)
            _stop(None, None)

    timer = threading.Timer(ttl_seconds, _on_ttl) if ttl_seconds else None
    if timer is not None:
        timer.daemon = True
        timer.start()

    prev: dict = {}
    for name in ("SIGINT", "SIGQUIT"):
        s = getattr(signal, name, None)
        if s is not None:
            prev[s] = signal.signal(s, signal.SIG_IGN)
    for name in ("SIGTERM", "SIGHUP"):
        s = getattr(signal, name, None)
        if s is not None:
            prev[s] = signal.signal(s, _stop)
    try:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                continue  # SIG_IGN should preempt this; keep waiting for the child regardless
    finally:
        if timer is not None:
            timer.cancel()
        for s, h in prev.items():
            signal.signal(s, h)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def start_session(entries: list[tuple[str, str, str]], treg_bin: str, ttl_minutes: int | None = None) -> int:
    """Create a private session dir, write the shims, launch the subshell with the shim dir first on
    PATH, and tear everything down when it exits. `ttl_minutes` is an optional hard time cap. Returns
    the subshell's exit code."""
    base = session_base_dir()
    session_dir = tempfile.mkdtemp(prefix="treg-shell-", dir=base)  # 0700 by mkdtemp
    os.chmod(session_dir, 0o700)
    shim_dir = os.path.join(session_dir, "bin")
    os.makedirs(shim_dir)
    os.chmod(shim_dir, 0o700)
    write_shims(shim_dir, entries, treg_bin)

    real_path = os.environ.get("PATH", "")
    env = dict(os.environ)
    env["PATH"] = shim_dir + os.pathsep + real_path
    env[ENV_ACTIVE] = "1"
    env[ENV_DIR] = session_dir
    env[ENV_REALPATH] = real_path  # the clean PATH the shims hand to `treg run` (loop avoidance)
    env[ENV_PID] = str(os.getpid())

    argv = _shell_argv(session_dir, env)
    _print_banner(entries, ttl_minutes)
    try:
        return _run_subshell(argv, env, ttl_seconds=ttl_minutes * 60 if ttl_minutes else None)
    finally:
        _teardown(session_dir)
        print("▚ treg shell closed.", file=sys.stderr)


def stop_session() -> None:
    """`treg shell stop`, run from inside a session: signal the `treg shell start` process to stop the
    subshell (a child process can't terminate its parent shell directly, so it asks the treg parent to,
    which returns the user to their real shell and tears the session down). Outside a session, explain."""
    if os.environ.get(ENV_ACTIVE) != "1":
        sys.exit("treg: no treg shell is active here. Start one with `treg shell start`.")
    pid = os.environ.get(ENV_PID)
    if not pid:
        sys.exit("treg: this treg shell has no controller to signal — type `exit` (or Ctrl-D) to leave.")
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ValueError, ProcessLookupError, OSError) as exc:
        sys.exit(f"treg: could not stop the shell ({exc}) — type `exit` (or Ctrl-D) to leave.")
