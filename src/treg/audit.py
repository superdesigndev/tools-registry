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
import logging

from .db import session_maker
from .models import CallRecord, RunRecord, SearchMiss

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
    *, org_id: int | None = None, user_email: str, tool_name: str, method: str, path: str,
    status_code: int, client: str = "", refused_by: str | None = None, telemetry: dict | None = None
) -> None:
    """`telemetry` carries the marketplace/spend columns (endpoint_id, provider, credential_tier,
    cost_*_micro, duration_ms, response_bytes, params_hash) — absent for a plain tool call, where they
    stay NULL. It is still fire-and-forget: the money landed in the ledger synchronously, so losing a
    row here costs analytics, not accounting. `refused_by` marks a call TREG refused before anything
    went upstream (see models.CallRecord) — NULL whenever the provider actually answered."""
    _schedule(_write(CallRecord,
        org_id=org_id, user_email=user_email, tool_name=tool_name,
        method=method, path=path, status_code=status_code, client=client, refused_by=refused_by,
        **_known_fields(CallRecord, telemetry),
    ))


def _known_fields(model, telemetry: dict | None) -> dict:
    """Drop telemetry keys the model has no column for, loudly.

    `telemetry` is splatted straight into the model constructor, so ONE unknown key used to raise
    inside `_write` — where the except swallows it — and the whole row vanished with no trace. That
    is the worst possible failure for an audit table: a telemetry field added a commit before its
    migration would silently delete every row it touched. An unknown key must cost one column, never
    the row.
    """
    if not telemetry:
        return {}
    known = {k: v for k, v in telemetry.items() if k in model.model_fields}
    if len(known) != len(telemetry):
        logging.getLogger("treg.audit").warning(
            "dropping unknown %s telemetry keys %s — is a migration missing?",
            model.__name__, sorted(set(telemetry) - set(known)))
    return known


def record_search_miss(*, query: str, source: str) -> None:
    """A catalog search that matched nothing — logged so the misses can steer ingest (see
    models.SearchMiss). Same contract as every write here: fire-and-forget, and a dropped row
    under load costs a data point, never a search response."""
    _schedule(_write(SearchMiss, query=query[:300], source=source))


def record_run(
    *, org_id: int | None = None, user_email: str, bundle_name: str, argv: list, exit_code: int,
    duration_ms: int, client: str = ""
) -> None:
    _schedule(_write(RunRecord,
        org_id=org_id, user_email=user_email, bundle_name=bundle_name,
        argv=argv, exit_code=exit_code, duration_ms=duration_ms, client=client,
    ))


_shed = 0  # audit rows dropped by back-pressure this process; only ever grows


def _schedule(coro) -> None:
    if len(_pending) >= _MAX_PENDING:  # shed load — audit is best-effort, never OOM the server
        coro.close()
        # Say so. Shedding bypasses `_write` entirely, so the warning there never fires for it, and
        # a shed row is invisible: the audit table simply has less in it. For a table whose job is
        # to record what happened, "quiet" and "quietly broken" must not look identical — and the
        # failure-evidence columns ride this same path, so a burst silently loses exactly the errors
        # someone would go looking for. Logged on the first drop and then every 1,000th, so a long
        # incident cannot itself flood the log.
        global _shed
        _shed += 1
        if _shed == 1 or _shed % 1000 == 0:
            logging.getLogger("treg.audit").warning(
                "audit back-pressure: %d row(s) dropped this process (pending at %d)",
                _shed, _MAX_PENDING)
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
            # Swallowed on purpose, but no longer SILENT: this used to be a bare `pass`, so a bad
            # write was indistinguishable from a call that never happened. The row is still lost —
            # that is the contract — but now something says so.
            logging.getLogger("treg.audit").warning(
                "audit write dropped for %s", model.__name__, exc_info=True)


async def drain() -> None:
    # Loop until quiescent, not a one-shot snapshot: a call finishing DURING shutdown enqueues a new
    # record_call after we'd have gathered, and that audit write would otherwise be dropped.
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
