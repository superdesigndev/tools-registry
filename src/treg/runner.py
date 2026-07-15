"""Server-side CLI execution (Tier 0) — run a registered tool's real CLI on the treg host, with
the tool's `cli.inject` secrets injected into the child process, and return its output. The caller
never holds the key; the secret is decrypted here (as it already is for every /call) and placed
only in the child's environment. Both run tiers read the same `Tool.cli` profile — this module is
the server-side consumer of it; the local tier's is the /grant path. See docs/CLI-RUN-PLAN.md.

Tier 0 scope (this module): **static-key (`kind="env"`) secrets** injected under their `cli.inject`
names; a plain subprocess (no container); a per-run throwaway $HOME so concurrent orgs never share a
credential cache; a scrubbed environment that never carries treg's own process env (which holds
TREG_SECRET_KEY); a timeout + output cap; and redaction of secret values from the returned output.

Out of scope here (later phases): OAuth-backed tools (need refresh + a token field, not the raw
blob — route them through the proxy instead), transparent per-call audit, and container isolation.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from . import crypto
from .config import get_settings
from .models import Secret, Tool

try:
    import resource  # POSIX-only; absent on Windows
except ImportError:  # pragma: no cover - the server runs on Linux
    resource = None

DEFAULT_TIMEOUT_S = 120
MAX_OUTPUT_BYTES = 1_000_000  # per stream (stdout / stderr) — a runaway CLI can't exhaust memory
_REDACTION = "***"

# A server-side run holds a subprocess (up to 600s) — cap how many run at once so one member (or the
# whole org) can't exhaust the host. When full, /run returns 429 immediately (no unbounded queue).
MAX_CONCURRENT_RUNS = 4            # global: at most this many server CLI runs at any moment
MAX_CONCURRENT_RUNS_PER_USER = 2   # and no single user may hold more than this many slots


class RunBusy(Exception):
    """Too many concurrent server runs right now — the caller should retry shortly (HTTP 429)."""


_slot_lock = asyncio.Lock()
_active_total = 0
_active_by_user: dict[str, int] = {}


@contextlib.asynccontextmanager
async def run_slot(user_email: str):
    """Reserve one server-run slot for the duration of a run, or raise RunBusy if none is free."""
    global _active_total
    async with _slot_lock:
        if _active_total >= MAX_CONCURRENT_RUNS:
            raise RunBusy("the server is busy running other jobs — try again in a moment")
        if _active_by_user.get(user_email, 0) >= MAX_CONCURRENT_RUNS_PER_USER:
            raise RunBusy(f"you already have {MAX_CONCURRENT_RUNS_PER_USER} server runs in flight — "
                          "wait for one to finish")
        _active_total += 1
        _active_by_user[user_email] = _active_by_user.get(user_email, 0) + 1
    try:
        yield
    finally:
        # Release WITHOUT awaiting the lock — a CancelledError delivered while awaiting it would skip the
        # decrement and leak a slot (wedging /run at 429). There is no await between read and write here,
        # so the GIL makes the counter updates atomic; no lock needed.
        _active_total -= 1
        n = _active_by_user.get(user_email, 1) - 1
        if n <= 0:
            _active_by_user.pop(user_email, None)
        else:
            _active_by_user[user_email] = n


class RunError(Exception):
    """A run could not be started (not runnable, CLI not installed). Distinct from a CLI that runs
    and exits non-zero — that is a normal RunResult with a non-zero exit_code."""


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


def _redact(text: str, values: list[str]) -> str:
    """Replace any secret plaintext the CLI may have echoed (e.g. a --debug config dump) before the
    output leaves the server. Longest-first so a value that contains another is fully masked."""
    for v in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(v, _REDACTION)
    return text


def _lower_rlimit(res_id: int, want_soft: int) -> None:
    """Set a resource's soft limit to `want_soft` without ever trying to RAISE the hard limit (a
    non-root process can't, and that would raise). We only lower — clamp the target to the current
    hard limit and leave the hard limit untouched, so setrlimit can never fail on permission."""
    soft, hard = resource.getrlimit(res_id)
    target = want_soft if hard == resource.RLIM_INFINITY else min(want_soft, hard)
    resource.setrlimit(res_id, (target, hard))


def _rlimit_preexec() -> None:
    """Run in the child after fork, before exec: cap CPU-seconds and max-file-size, and disable core
    dumps (a core would spill the injected secret + process memory to disk). Deliberately NO address-
    space or process-count cap — a virtual-memory cap crashes Go CLIs (gh/stripe), and RLIMIT_NPROC is
    per-uid, shared with the server. Best-effort: a limit we can't set must never block the run."""
    s = get_settings()
    try:
        _lower_rlimit(resource.RLIMIT_CPU, max(1, s.run_cpu_seconds))
        _lower_rlimit(resource.RLIMIT_FSIZE, max(1, s.run_fsize_mb) * 1_000_000)
        _lower_rlimit(resource.RLIMIT_CORE, 0)
    except (ValueError, OSError):  # pragma: no cover - defensive; never fail a run over a limit
        pass


def _spawn_preexec():
    """The child's preexec_fn for a server run: `resource` present AND rlimits enabled → apply them;
    otherwise None (no preexec). `start_new_session` already gives the child its own process group."""
    if resource is None or not get_settings().run_rlimits:
        return None
    return _rlimit_preexec


def _child_env(secret_env: dict[str, str], home: str) -> dict[str, str]:
    """A scrubbed environment: NOT the server's env (it holds TREG_SECRET_KEY and every other
    server secret). Only the minimum a CLI needs to run, a private HOME, and the injected secrets."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "TMPDIR": home,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    env.update(secret_env)  # secrets injected under their stored names (AGENTMAIL_API_KEY, …)
    return env


async def _read_capped(stream, cap: int, sink: bytearray) -> None:
    """Read at most `cap` bytes into `sink`, then keep draining (discarding) so the child never blocks on
    a full pipe. Writes into a caller-owned buffer so whatever was captured survives even if this reader
    is cancelled (e.g. a grandchild holds the pipe open past the drain timeout)."""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if len(sink) < cap:
            sink.extend(chunk[: cap - len(sink)])


def _kill_group(pgid: int) -> None:
    """SIGKILL a whole process group. Called on EVERY exit (incl. a normal child exit) so a background
    grandchild the CLI spawned — which inherited the injected secret — can't outlive the run. Uses the
    pgid captured at spawn (start_new_session ⇒ pgid == the child's pid); ProcessLookupError just means
    the group is already empty. Synchronous — safe to call while a task is unwinding."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _collect_secret_env(tool: Tool, db: AsyncSession) -> tuple[dict[str, str], list[str]]:
    """The tool's `cli.inject` secrets, decrypted and keyed by the env-var name each entry declares
    (the same profile the local grant path renders — one source of truth for both tiers). OAuth
    (`kind` != "env") secrets are skipped in Tier 0 — they need refresh + a token field, not the
    raw blob."""
    from . import localrun  # lazy: avoid a module cycle (localrun imports nothing from runner)

    inject_names: dict[int, str] = {}
    for entry in (tool.cli or {}).get("inject") or []:
        # Resolve exactly like the local grant path (explicit secret_id → from_binding → the tool's
        # sole bound secret) so an entry that injects on --local can never silently skip on --server.
        sid = localrun._resolve_secret_id(entry, tool)
        if entry.get("via", "env") == "env" and sid and entry.get("name"):
            inject_names[sid] = entry["name"]
    env: dict[str, str] = {}
    values: list[str] = []
    if inject_names:
        secrets = (
            await db.execute(select(Secret).where(Secret.id.in_(inject_names)))  # type: ignore[union-attr]
        ).scalars().all()
        for s in secrets:
            if s.kind != "env":
                continue
            val = crypto.decrypt(s.value)
            env[inject_names[s.id]] = val
            values.append(val)
    return env, values


def resolve_exec_bin(tool: Tool) -> str:
    """The one place a `Tool.cli` profile resolves to the command that will exec. The API's
    allow-list gate and the actual spawn both call this, so they can never check/run different
    strings. Raises RunError when the tool has no cli profile at all."""
    cli = tool.cli or {}
    if not cli:
        raise RunError(
            f"tool {tool.name!r} has no CLI profile — it's an HTTP tool (use `treg call`), or add a "
            '"cli" block to its treg.json to make it runnable.')
    return cli.get("bin") or tool.name


async def run_tool(
    tool: Tool, argv: list[str], db: AsyncSession, *, timeout_s: int = DEFAULT_TIMEOUT_S
) -> RunResult:
    """Execute the tool's CLI (`cli.bin`) with `argv`, secrets injected via env. Raises RunError
    if the tool has no cli profile or the CLI isn't installed on the server."""
    entrypoint = resolve_exec_bin(tool)
    resolved = shutil.which(entrypoint)
    if resolved is None:
        raise RunError(f"CLI {entrypoint!r} is not installed on the treg server")

    secret_env, secret_values = await _collect_secret_env(tool, db)

    home = tempfile.mkdtemp(prefix="treg-run-")
    os.makedirs(os.path.join(home, ".config"), exist_ok=True)
    loop = asyncio.get_running_loop()
    start = loop.time()
    timed_out = False
    out = err = b""
    proc = await asyncio.create_subprocess_exec(
        resolved, *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,  # no interactive prompts server-side
        env=_child_env(secret_env, home),
        cwd=home,
        start_new_session=True,  # own process group → kill the whole tree on timeout/cancel/exit
        preexec_fn=_spawn_preexec(),  # POSIX rlimits (CPU / file-size / no core) — the DoS sandbox
    )
    pgid = proc.pid  # start_new_session ⇒ the child is its group leader; capture before it's reaped
    # Read both streams with a memory cap running CONCURRENTLY with the wait, so a runaway CLI can't
    # buffer gigabytes before truncation (the old communicate() did). Sinks are read at the end even if
    # the reader tasks time out, so partial output isn't lost.
    out_buf, err_buf = bytearray(), bytearray()
    out_task = asyncio.ensure_future(_read_capped(proc.stdout, MAX_OUTPUT_BYTES, out_buf))
    err_task = asyncio.ensure_future(_read_capped(proc.stderr, MAX_OUTPUT_BYTES, err_buf))
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            code = proc.returncode if proc.returncode is not None else -1
        except asyncio.TimeoutError:
            timed_out = True
            code = -signal.SIGKILL
    finally:
        # ALWAYS kill the process GROUP before deleting HOME — on timeout, cancel, error, AND a normal
        # exit. A normal `treg run` finish still leaves any BACKGROUND grandchild the CLI spawned alive
        # with the injected secret in its env; killing the group reaps it. Synchronous, so it runs even
        # while the task is being cancelled.
        _kill_group(pgid)
        # Let the readers finish draining (EOF arrives once the child is dead); cancel if they hang.
        try:
            await asyncio.wait_for(asyncio.gather(out_task, err_task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            out_task.cancel(); err_task.cancel()
        out, err = bytes(out_buf), bytes(err_buf)  # partial output survives a reader timeout
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)  # reap the killed child before rmtree
        except (asyncio.TimeoutError, ProcessLookupError, asyncio.CancelledError):
            pass
        shutil.rmtree(home, ignore_errors=True)
    if timed_out:
        err = (err or b"") + f"\n[treg] killed: exceeded {timeout_s}s timeout".encode()

    dur = int((loop.time() - start) * 1000)
    stdout = _redact((out or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), secret_values)
    stderr = _redact((err or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), secret_values)
    return RunResult(exit_code=code, stdout=stdout, stderr=stderr, duration_ms=dur, timed_out=timed_out)
