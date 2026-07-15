"""DB-backed ephemeral state: OTP codes + auth rate-limit windows (backlog #3).

These used to be module-level dicts in `api.py` — correct only on ONE process. On a restart the
counters reset (an attacker just waits for a redeploy), and on two+ instances a per-IP/per-email cap
becomes N× weaker and an OTP written on one worker is invisible to another. Moving them to a single
`Ephemeral` table (keyed by namespace + key, opaque JSON value, `expires_at`) fixes both: the state
persists and is shared.

Two shapes live here:
  * **key/value with TTL** (`kv_put`/`kv_get`/`kv_pop`) — the OTP code + its attempt counter.
  * **sliding-window rate limit** (`rate_check`) — the OTP-start + sandbox throttles; each key's row
    holds a list of recent hit timestamps (wall-clock epoch seconds, so they're comparable across
    processes — the old code used `time.monotonic()`, which is per-process and can't be shared).

Growth is bounded by `expires_at` + `sweep` (delete expired rows), so an unauthenticated caller can't
make the table grow without limit. Writes are per-request and low-volume (auth endpoints only).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Ephemeral


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def sweep(db: AsyncSession, ns: str | None = None) -> None:
    """Delete expired rows (optionally within one namespace) — the bound on table growth. Call it
    before adding to an unauthenticated-reachable namespace, mirroring the old `_rate_sweep`."""
    stmt = delete(Ephemeral).where(Ephemeral.expires_at < _utcnow_naive())
    if ns is not None:
        stmt = stmt.where(Ephemeral.ns == ns)
    await db.execute(stmt)


async def kv_get(db: AsyncSession, ns: str, k: str) -> dict | None:
    """The value for (ns, k), or None if missing or expired (an expired row is removed in passing)."""
    row = await db.get(Ephemeral, (ns, k))
    if row is None:
        return None
    if row.expires_at < _utcnow_naive():
        await db.delete(row)
        return None
    return row.v


async def kv_put(db: AsyncSession, ns: str, k: str, v: dict, ttl_s: float | None) -> None:
    """Upsert (ns, k) → `v`. `ttl_s` sets a fresh expiry; pass ttl_s=None to keep the existing expiry
    (used when only the payload changes, e.g. decrementing the OTP attempt counter without extending
    the code's lifetime)."""
    row = await db.get(Ephemeral, (ns, k))
    if row is None:
        exp = _utcnow_naive() + timedelta(seconds=ttl_s or 0)
        db.add(Ephemeral(ns=ns, k=k, v=v, expires_at=exp))
        return
    row.v = v  # reassign (not in-place) so SQLAlchemy marks the JSON column dirty
    if ttl_s is not None:
        row.expires_at = _utcnow_naive() + timedelta(seconds=ttl_s)
    db.add(row)


async def kv_pop(db: AsyncSession, ns: str, k: str) -> dict | None:
    """Read-and-delete (ns, k) atomically-ish within this session. Returns the value if it was still
    live (not expired), else None. The row is always removed."""
    row = await db.get(Ephemeral, (ns, k))
    if row is None:
        return None
    v, exp = row.v, row.expires_at
    await db.delete(row)
    return v if exp >= _utcnow_naive() else None


async def rate_check(
    db: AsyncSession, ns: str, limits: list[tuple[str, int]], window_s: float
) -> bool:
    """All-or-nothing sliding-window limiter over the (ns, key) rows. `limits` is a list of
    (key, max_hits): the request is allowed only if EVERY key has fewer than its cap of hits within
    the last `window_s`. On allow, one hit (now) is appended under every key and the rows are
    persisted with a fresh expiry; on reject, nothing is written. Each key's expired timestamps are
    pruned before the check. Wall-clock timestamps so the window is comparable across processes."""
    now = time.time()
    pruned: dict[str, list[float]] = {}
    for key, _ in limits:
        row = await db.get(Ephemeral, (ns, key))
        hits = (row.v or {}).get("hits", []) if row is not None else []
        pruned[key] = [t for t in hits if now - t < window_s]
    ok = all(len(pruned[key]) < mx for key, mx in limits)
    if ok:
        for key, _ in limits:
            pruned[key].append(now)
            await kv_put(db, ns, key, {"hits": pruned[key]}, ttl_s=window_s)
    return ok
