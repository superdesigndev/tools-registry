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


async def test_caller_errors_cannot_lift_one_decided_result_over_the_floor(clients: AsyncClient):
    """The evidence floor applies to the denominator, not unrelated traffic.

    Four malformed caller requests plus one real result used to publish that one result as either
    0% or 100%. Besides being statistically empty, that exposes the outcome of one quiet tenant's
    call — exactly the privacy leak MIN_SAMPLES exists to prevent.
    """
    for ep_id, decided_status in (("one.failure", 405), ("one.success", 200)):
        for _ in range(endpoint_stats.MIN_SAMPLES - 1):
            await _record(ep_id, 422)
        await _record(ep_id, decided_status, ms=42)
        got = (await _observed([ep_id]))[ep_id]
        assert got["samples"] == endpoint_stats.MIN_SAMPLES
        assert all(got[k] is None for k in ("ok_rate", "p50_ms", "p95_ms", "last_ok_days"))


async def test_latency_needs_its_own_success_sample_floor(clients: AsyncClient):
    """A publishable reliability denominator does not make one latency a percentile."""
    for _ in range(endpoint_stats.MIN_SAMPLES - 1):
        await _record("one.latency", 503)
    await _record("one.latency", 200, ms=42)
    got = (await _observed(["one.latency"]))["one.latency"]
    assert got["ok_rate"] == round(1 / endpoint_stats.MIN_SAMPLES, 4)
    assert got["p50_ms"] is None and got["p95_ms"] is None


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


async def test_last_ok_means_the_last_SUCCESS_not_the_last_attempt(clients: AsyncClient):
    """An endpoint whose metadata is wrong fails every call, and `max(created_at)` over ALL rows
    then dated it as freshly working. `tikhub.x.tiktok-ads-search-ads` read `WORKS — (7)` next to
    `LAST OK: today` while every one of those seven calls had been refused 405 by the provider —
    which reads as "fine, just new" and is the opposite of the truth."""
    await _record(EP, 200, days_ago=9)                # the last time it really answered
    for _ in range(7):
        await _record(EP, 500, days_ago=0)            # today's failures must not date it
    got = (await _observed([EP]))[EP]
    assert got["last_ok_days"] == 9


async def test_below_the_floor_NOTHING_about_the_outcome_is_published(clients: AsyncClient):
    """The floor publishes volume, never outcome — not even a yes/no.

    A first cut of the 2026-08-17 fix published `any_ok` here, reasoning that "has it EVER
    answered?" survives any sample size. It broke both of this module's rules at once. On a quiet
    endpoint it exposed the OUTCOME of a single tenant's single call, which is the leak the floor
    exists to prevent. And since `samples` counts 4xx while successes do not, one caller's malformed
    422 published `any_ok: false` — making a healthy endpoint look broken to every other tenant,
    the exact failure `test_a_caller_error_is_not_held_against_the_provider` guards."""
    for _ in range(3):
        await _record(EP, 500)
    thin = (await _observed([EP]))[EP]
    assert thin["samples"] == 3
    assert all(thin[k] is None for k in ("ok_rate", "p50_ms", "p95_ms", "last_ok_days"))
    assert "any_ok" not in thin

    await _record("caller.error.only", 422)
    one_bad_call = (await _observed(["caller.error.only"]))["caller.error.only"]
    assert one_bad_call["ok_rate"] is None, "one agent's bad parameters say nothing about the endpoint"


async def test_never_worked_is_read_off_DECIDED_samples_only(clients: AsyncClient):
    """"Never worked" has to mean the provider failed the calls that were ITS to answer. Above the
    floor `ok_rate == 0` says exactly that — 4xx is already excluded from the rate — so ranking can
    demote a genuinely broken endpoint without a single caller error being able to trigger it."""
    from treg import catalog_store as cs
    for _ in range(6):
        await _record(EP, 503)                       # the provider failing, decisively
    assert (await _observed([EP]))[EP]["ok_rate"] == 0.0

    for _ in range(6):
        await _record("all.caller.errors", 422)      # six bad requests from callers
    caller_fault = (await _observed(["all.caller.errors"]))["all.caller.errors"]
    assert caller_fault["samples"] == 6 and caller_fault["ok_rate"] is None

    # and ranking follows: decisively-failing sinks below never-measured, caller-errors-only does not
    rows = [({"id": i, "tier": "extended", "verified": None, "cost": None}, 6)
            for i in ("broken", "untried", "caller-fault")]
    order = [ep["id"] for ep, _ in cs.rerank(rows, {
        "broken": {"samples": 6, "ok_rate": 0.0},          # the provider failed every decided call
        "untried": {"samples": 0, "ok_rate": None},        # nobody has called it
        "caller-fault": {"samples": 6, "ok_rate": None},   # six 422s and nothing decided
    })]
    assert order[-1] == "broken"
    assert order.index("caller-fault") < order.index("broken"), \
        "caller errors must not sink an endpoint the way a real failure does"


async def test_a_relayed_405_is_the_CATALOGS_failure_not_the_callers(clients: AsyncClient):
    """The one 4xx the caller cannot have caused.

    On a catalog call the method is not the caller's to choose: `_resolve_marketplace_call` refuses
    a mismatch with a 400 before anything is relayed. So a 405 coming back from the provider means
    the method THIS CATALOG RECORDED was rejected upstream — a stale contract, which is the single
    thing these numbers exist to surface. Counting it as "the caller sent bad parameters" is what
    let `tikhub.x.tiktok-ads-search-ads` show `WORKS — (7)` while being uncallable by anyone."""
    from treg import cli
    for _ in range(7):
        await _record(EP, 405)
    got = (await _observed([EP]))[EP]
    assert got["samples"] == 7
    assert got["ok_rate"] == 0.0, "seven straight 405s are decided evidence, not unknown"
    assert got["last_ok_days"] is None
    assert "0%" in cli._observed_cell(got), cli._observed_cell(got)

    # …and an ordinary caller error is still excluded, or this trades one wrong reading for another
    for _ in range(7):
        await _record("other.endpoint", 422)
    caller_fault = (await _observed(["other.endpoint"]))["other.endpoint"]
    assert caller_fault["ok_rate"] is None


async def test_a_405_endpoint_sinks_in_the_ranking(clients: AsyncClient):
    """The payoff for report #4: the row that cannot be called stops being offered first."""
    from treg import catalog_store as cs
    for _ in range(7):
        await _record(EP, 405)
    stats = {EP: (await _observed([EP]))[EP],
             "rival": {"samples": 17, "ok_rate": 0.8}}
    rows = [({"id": EP, "tier": "extended", "verified": None, "cost": None}, 6),
            ({"id": "rival", "tier": "extended", "verified": None, "cost": None}, 6)]
    assert [e["id"] for e, _ in cs.rerank(rows, stats)] == ["rival", EP]


async def test_HTTP_search_orders_equal_matches_on_observed_evidence(clients: AsyncClient):
    """Pin the HTTP wiring with an order the catalog's default order gets wrong.

    The stale row starts earlier in file order. With 405 classified correctly it must fall behind a
    merely-poor endpoint; removing either the 405 classification or the route's rerank call puts it
    first again.
    """
    stale = "apify.meta-ads.library.search"
    poor = "tikhub.x.tiktok-ads-search-ads"
    for _ in range(endpoint_stats.MIN_SAMPLES):
        await _record(stale, 405)
    for status in (200, 200, 200, 200, 503):
        await _record(poor, status)

    rows = (await clients.get(
        "/catalog/search", params={"q": "ad library", "limit": 100})).json()["results"]
    ids = [row["id"] for row in rows]
    assert ids.index(poor) < ids.index(stale), ids
