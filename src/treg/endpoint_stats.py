"""What the calls we have already served say about each catalog endpoint.

The catalog's own numbers are *claims*: a price read off a rate card, a `verified:` stamp from the
day someone ran it. This module answers the other question — **does it still work, how fast, and
does it charge what it said?** — from `CallRecord`, which has recorded `endpoint_id`, `status_code`,
`duration_ms` and `cost_observed_micro` since the marketplace shipped. Nothing new is collected here;
it was always being written and never read.

This is the half of "compare providers" that only treg can do. Anyone can read a rate card; only the
party that sees every call, across every tenant, can say which of nine email-lookup providers
answered 400 times without failing. It is what makes an agent's choice factual instead of a guess —
see `docs/CAPABILITY-CHOICE-PLAN.md`.

**Aggregate only, and never below a floor.** Rows are pooled across every org, so the output must
carry nothing that could identify a caller: counts, rates and percentiles only — never who, never
when-exactly, never a params_hash. And an endpoint with fewer than `MIN_SAMPLES` calls reports
`samples` and nothing else: with two calls behind it, a "100% success" number is noise dressed as
evidence, and on a quiet endpoint it could also be one org's activity made visible.

Read-only and query-time, like `reconcile.py` — no scheduler, no cache to invalidate, no second copy
of the truth. Percentiles are computed in Python because `percentile_cont` is not portable to SQLite
(the same tradeoff `reconcile.py` documents for its JSON provenance).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .models import CallRecord

WINDOW_DAYS = 30
MIN_SAMPLES = 5          # below this we publish the count and nothing else (see module docstring)
_MAX_ROWS = 20_000       # bound the latency fetch; percentiles do not get truer past this


def _now() -> datetime:
    # Naive UTC — CallRecord.created_at is TIMESTAMP WITHOUT TIME ZONE (models._now).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pct(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile. Deliberately not interpolated: these are milliseconds off a wire,
    and a reader comparing providers gains nothing from a fractional millisecond."""
    if not sorted_values:
        return None
    i = max(0, min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1)))))
    return int(sorted_values[i])


async def observed(
    db: AsyncSession, endpoint_ids: list[str], *, days: int = WINDOW_DAYS,
) -> dict[str, dict]:
    """Per endpoint id: `{samples, ok_rate, p50_ms, p95_ms, last_ok_days}`.

    Cost drift (what we estimated vs what the provider charged) is deliberately NOT here — it is
    already `reconcile.price_drift`, computed over the same rows for a different audience. Two
    implementations of one number is how they start disagreeing.

    `endpoint_ids` is expected to be small — one endpoint and its capability siblings — so this is
    two bounded queries, not a scan of the audit table.

    A 4xx counts as a **failure of the call**, not of the endpoint: it usually means the caller sent
    the wrong parameters. It is excluded from `ok_rate` entirely rather than counted against the
    provider, because otherwise one agent's bad query would make a healthy endpoint look broken to
    everybody. Only 2xx (success) and 5xx/timeouts (the provider's fault) decide the rate.

    **405 is the exception**, and it is one the rule's own justification demands. "The caller sent
    the wrong parameters" cannot apply to a method the caller was never allowed to pick: a catalog
    call whose method differs from the recorded one is refused with a 400 before it is relayed. So a
    405 coming back from the provider says the recorded method is wrong — a stale catalog contract,
    not a bad query — and it is counted as decided against the endpoint.
    """
    ids = [e for e in dict.fromkeys(endpoint_ids) if e]
    if not ids:
        return {}
    since = _now() - timedelta(days=days)

    rows = (await db.execute(
        select(
            CallRecord.endpoint_id,
            func.count().label("n"),
            func.sum(case((CallRecord.status_code < 300, 1), else_=0)).label("ok"),
            # 5xx, plus the one 4xx the caller cannot possibly have caused: 405. On a CATALOG call
            # the method is not the caller's to choose — `_resolve_marketplace_call` refuses a
            # mismatch with a 400 BEFORE anything is relayed — so a 405 that came back from the
            # provider means the method THIS CATALOG RECORDED was rejected upstream. That is
            # evidence about the catalog being stale, which is the one thing these numbers exist to
            # surface, and lumping it in with "the caller sent bad parameters" is what let seven
            # straight 405s keep reading as `WORKS — (7)` — the exact row the 2026-08-17 report
            # could not interpret.
            func.sum(case(((CallRecord.status_code >= 500) | (CallRecord.status_code == 405), 1),
                          else_=0)).label("bad"),
            # LAST OK means last SUCCESS. This was `max(created_at)` over every row, success or
            # not — so an endpoint that had been called seven times today and failed every one
            # read "LAST OK: today", which is the opposite of the truth and exactly how a broken
            # row passes for a merely new one.
            func.max(case((CallRecord.status_code < 300, CallRecord.created_at))).label("last_ok"),
        )
        .where(CallRecord.endpoint_id.in_(ids), CallRecord.created_at >= since,
               # treg's own refusals (paywall 402s, caps, bad requests never relayed) are facts
               # about the CALLER's account, not the endpoint — they must not even count as
               # samples, or a burst of refused calls dresses itself up as evidence.
               CallRecord.refused_by.is_(None))
        .group_by(CallRecord.endpoint_id)
    )).all()

    lat = (await db.execute(
        select(CallRecord.endpoint_id, CallRecord.duration_ms)
        .where(CallRecord.endpoint_id.in_(ids), CallRecord.created_at >= since,
               CallRecord.duration_ms.is_not(None), CallRecord.status_code < 300)
        .limit(_MAX_ROWS)
    )).all()
    by_id: dict[str, list[int]] = {}
    for ep_id, ms in lat:
        by_id.setdefault(ep_id, []).append(int(ms))

    out: dict[str, dict] = {}
    for ep_id, n, ok, bad, last_ok in rows:
        n, ok, bad = int(n or 0), int(ok or 0), int(bad or 0)
        decided = ok + bad          # 4xx excluded — the caller's fault, not the provider's
        if decided < MIN_SAMPLES:
            # Honest emptiness: say how thin the evidence is, claim nothing from it. An earlier
            # revision of this fix published `any_ok` here — "has it EVER answered?" — on the
            # argument that a yes/no survives any sample size. It doesn't survive THIS module's
            # own two rules, and it broke both. It leaked outcome (not just volume) about a single
            # tenant's single call on a quiet endpoint, which is what the floor exists to prevent;
            # and because `samples` counts 4xx while `ok` does not, one caller's malformed 422
            # produced `any_ok: false` and made a healthy endpoint look broken to everybody — the
            # exact failure the 4xx rule below is written to stop. "Never worked" is now read off
            # `ok_rate == 0`, which is computed only from DECIDED (2xx vs 5xx) samples above the
            # floor, so it cannot be inferred from caller errors at all. The floor must therefore
            # be tested against `decided`, not total traffic: four 422s plus one 405 previously
            # published the outcome of that ONE decided call as 0%, violating both the evidence
            # and privacy reasons for having the floor.
            out[ep_id] = {"samples": n, "ok_rate": None,
                          "p50_ms": None, "p95_ms": None, "last_ok_days": None}
            continue
        ms = sorted(by_id.get(ep_id, []))
        enough_latency = len(ms) >= MIN_SAMPLES
        out[ep_id] = {
            "samples": n,
            "ok_rate": round(ok / decided, 4) if decided else None,
            # A rate may rest on five decided calls while only one succeeded. Calling that single
            # duration p50 AND p95 dresses one observation up as a distribution, so latency has
            # its own successful-sample floor.
            "p50_ms": _pct(ms, 0.50) if enough_latency else None,
            "p95_ms": _pct(ms, 0.95) if enough_latency else None,
            "last_ok_days": (_now() - last_ok).days if last_ok else None,
        }
    for ep_id in ids:                # an endpoint nobody has called says so, rather than vanishing
        out.setdefault(ep_id, {"samples": 0, "ok_rate": None,
                               "p50_ms": None, "p95_ms": None, "last_ok_days": None})
    return out
