"""Audit writes — deferred and fire-and-forget (rule #2: never block the proxied response).

`record_call` schedules an insert on its own session and returns immediately; the response
streams without waiting. A strong reference to each task is held until it finishes (otherwise
the event loop may GC a bare create_task). Failures are swallowed: an audit hiccup must never
break a real call. `drain()` flushes pending writes on shutdown / in tests.

Back-pressure (why this matters): each write opens a DB connection, and the pool is small + SHARED
with the request path. Under a burst, uncapped background writes would grab every connection and
starve real calls. So: at most `_MAX_CONCURRENT_WRITES` writes hold a connection at once (a loop-bound
semaphore), and under an extreme burst we DROP audit rows past `_MAX_PENDING` rather than grow without
bound — audit is best-effort; never OOM or wedge the server for it.
"""

from __future__ import annotations

import asyncio

from .db import session_maker
from .models import CallRecord, RunRecord

_pending: set[asyncio.Task] = set()
_MAX_CONCURRENT_WRITES = 4   # cap on audit writes holding a DB connection at once (protect the request pool)
_MAX_PENDING = 5000          # shed load past this: drop the audit row rather than grow unbounded

_sem: asyncio.Semaphore | None = None
_sem_loop = None


def _get_sem() -> asyncio.Semaphore:
    """A semaphore bound to the CURRENT running loop (recreated if the loop changed — test isolation)."""
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)
        _sem_loop = loop
    return _sem


def record_call(
    *, org_id: int | None = None, user_email: str, tool_name: str, method: str, path: str, status_code: int
) -> None:
    _schedule(_write(CallRecord,
        org_id=org_id, user_email=user_email, tool_name=tool_name,
        method=method, path=path, status_code=status_code,
    ))


def record_run(
    *, org_id: int | None = None, user_email: str, bundle_name: str, argv: list, exit_code: int, duration_ms: int
) -> None:
    _schedule(_write(RunRecord,
        org_id=org_id, user_email=user_email, bundle_name=bundle_name,
        argv=argv, exit_code=exit_code, duration_ms=duration_ms,
    ))


def _schedule(coro) -> None:
    if len(_pending) >= _MAX_PENDING:  # shed load — audit is best-effort, never OOM the server
        coro.close()
        return
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write(model, **fields) -> None:
    async with _get_sem():  # cap concurrent DB connections held by audit — never starve the request pool
        try:
            async with session_maker() as session:
                session.add(model(**fields))
                await session.commit()
        except Exception:  # noqa: BLE001 — audit must never surface into a call's result
            pass


async def drain() -> None:
    # Loop until quiescent, not a one-shot snapshot: a call finishing DURING shutdown enqueues a new
    # record_call after we'd have gathered, and that audit write would otherwise be dropped.
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
