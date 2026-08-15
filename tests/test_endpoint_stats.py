"""Observed reliability per catalog endpoint — the half of "compare providers" only treg can answer.

These pin the judgement calls, not the arithmetic: which failures count against a provider, when we
refuse to publish a rate at all, and that nothing identifying a caller leaks into an aggregate.
See docs/CAPABILITY-CHOICE-PLAN.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from treg import endpoint_stats
from treg.db import session_maker
from treg.models import CallRecord

EP = "tikhub.tiktok.user.profile"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _record(endpoint_id: str, status: int, ms: int = 100, *, days_ago: int = 0, org_id: int = 1,
                  refused_by: str | None = None):
    async with session_maker() as db:
        db.add(CallRecord(org_id=org_id, user_email="a@b.c", tool_name=endpoint_id, method="GET",
                          path="/x", status_code=status, endpoint_id=endpoint_id,
                          duration_ms=None if refused_by else ms, refused_by=refused_by,
                          created_at=_now() - timedelta(days=days_ago)))
        await db.commit()


async def _observed(ids, **kw):
    async with session_maker() as db:
        return await endpoint_stats.observed(db, ids, **kw)


async def test_thin_evidence_publishes_the_count_and_nothing_else(clients: AsyncClient):
    """Two calls behind a 100% success rate is noise dressed as evidence — and on a quiet endpoint
    the number could expose one org's activity. Below the floor we say how thin it is, and stop."""
    for _ in range(endpoint_stats.MIN_SAMPLES - 1):
        await _record(EP, 200)
    got = (await _observed([EP]))[EP]
    assert got["samples"] == endpoint_stats.MIN_SAMPLES - 1
    assert got["ok_rate"] is None and got["p50_ms"] is None


async def test_a_caller_error_is_not_held_against_the_provider(clients: AsyncClient):
    """A 4xx usually means the caller sent bad parameters. Counting it against the endpoint would let
    one agent's mistake make a healthy provider look broken to everybody, so it is excluded."""
    for _ in range(6):
        await _record(EP, 200)
    for _ in range(6):
        await _record(EP, 422)          # the caller's fault — must not move the rate
    got = (await _observed([EP]))[EP]
    assert got["samples"] == 12         # still counted as traffic…
    assert got["ok_rate"] == 1.0        # …but the rate is decided only by 2xx vs 5xx


async def test_a_treg_refusal_is_not_evidence_about_the_endpoint(clients: AsyncClient):
    """A paywall 402 or a daily-cap 429 never reached the provider — it says the CALLER's account
    ran dry, nothing about the endpoint. Refused rows must not even count as samples: the Hunter
    incident (2026-08-12) put 309 refusals next to 488 real calls, and counting them would have
    made a healthy endpoint look busier and flakier than it was."""
    for _ in range(6):
        await _record(EP, 200)
    for _ in range(9):
        await _record(EP, 402, refused_by="balance")
    got = (await _observed([EP]))[EP]
    assert got["samples"] == 6          # the refusals are not traffic the provider ever saw
    assert got["ok_rate"] == 1.0


async def test_a_provider_failure_does_move_the_rate(clients: AsyncClient):
    for _ in range(6):
        await _record(EP, 200)
    for _ in range(2):
        await _record(EP, 503)
    assert (await _observed([EP]))[EP]["ok_rate"] == 0.75


async def test_latency_percentiles_come_from_successful_calls(clients: AsyncClient):
    for ms in (10, 20, 30, 40, 50, 5000):
        await _record(EP, 200, ms)
    got = (await _observed([EP]))[EP]
    assert got["p50_ms"] in (30, 40)         # nearest-rank, not interpolated
    assert got["p95_ms"] == 5000             # the tail is the point of p95


async def test_old_calls_fall_out_of_the_window(clients: AsyncClient):
    for _ in range(6):
        await _record(EP, 200, days_ago=99)
    assert (await _observed([EP], days=30))[EP]["samples"] == 0


async def test_an_uncalled_endpoint_says_so_rather_than_vanishing(clients: AsyncClient):
    """A missing key would read as "no opinion" to a caller looping over siblings; an explicit zero
    reads as "nobody has tried this one", which is the actual fact."""
    got = await _observed(["nobody.has.called.this"])
    assert got["nobody.has.called.this"]["samples"] == 0


async def test_aggregates_pool_across_orgs_but_carry_nothing_identifying(clients: AsyncClient):
    for _ in range(3):
        await _record(EP, 200, org_id=1)
    for _ in range(3):
        await _record(EP, 200, org_id=2)
    got = (await _observed([EP]))[EP]
    assert got["samples"] == 6                                   # pooled across tenants
    assert set(got) == {"samples", "ok_rate", "p50_ms", "p95_ms", "last_ok_days"}
    assert not any(k in got for k in ("org_id", "user_email", "params_hash", "client"))


async def test_the_catalog_page_carries_the_numbers_for_every_alternative(clients: AsyncClient):
    """The choice is made on this page, so the evidence has to arrive with it — an agent will not
    make a second round-trip to compare reliability."""
    for _ in range(6):
        await _record(EP, 200)
    r = await clients.get(f"/catalog/endpoints/{EP}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"]["observed"]["samples"] == 6
    for sib in body["siblings"]:
        assert "observed" in sib          # every alternative is comparable, or the page is useless


# ---- the LAST OK column: measurement beats a stamp, and the two never look alike -------------
def test_measured_last_ok_beats_the_verification_stamp():
    """A real call is stronger evidence than a hand-run stamp, so it wins — and it prints bare,
    while the stamp prints with a ✓ so nobody reads a dated claim as live traffic."""
    from treg import cli
    measured = cli._last_ok_cell({"observed": {"last_ok_days": 3}, "verified": "2020-01-01"})
    assert measured == "3d" and "✓" not in measured

    stamped = cli._last_ok_cell({"observed": {"last_ok_days": None}, "verified": "2026-08-01"})
    assert "✓" in stamped                      # visibly a claim, not a measurement


def test_last_ok_says_nothing_when_it_knows_nothing():
    """An endpoint nobody has called AND nobody has verified is the one worth being wary of, so it
    must not borrow confidence from a blank."""
    from treg import cli
    assert "—" in cli._last_ok_cell({"observed": None, "verified": None})


def test_a_call_today_reads_as_today():
    from treg import cli
    assert cli._last_ok_cell({"observed": {"last_ok_days": 0}}) == "today"
