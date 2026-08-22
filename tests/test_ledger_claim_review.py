"""Focused review repros for the hold-claim and tag-window changes."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlmodel import select

from treg import ledger
from treg.db import session_maker
from treg.models import Hold, LedgerEntry


async def _org_id(client: AsyncClient) -> int:
    return (await client.get("/orgs")).json()[0]["org_id"]


@pytest.mark.anyio
async def test_a_lost_claim_does_not_abort_the_rest_of_the_reap(
    clients: AsyncClient, monkeypatch,
):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        first = await ledger.reserve(db, org_id, "review.first", 10)
        second = await ledger.reserve(db, org_id, "review.second", 10)
        rows = (await db.execute(select(Hold).where(Hold.id.in_([first, second])))).scalars().all()
        for offset, row in enumerate(sorted(rows, key=lambda item: item.id)):
            row.created_at = datetime(2000, 1, 1) + timedelta(seconds=offset)
        await db.commit()

    original_release = ledger.release
    raced = False

    async def release_after_concurrent_delete(db, call_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            async with session_maker() as winner:
                await winner.execute(delete(Hold).where(Hold.id == call_id))
                await winner.commit()
        return await original_release(db, call_id, **kwargs)

    monkeypatch.setattr(ledger, "release", release_after_concurrent_delete)
    # `reap_stale_holds` preloads Hold rows and walks them, calling release() on each. When the first
    # one loses its claim to a concurrent closer, release must NOT roll back: a rollback expires every
    # object in the shared session, so reading the next `hold.id` became implicit async I/O
    # (MissingGreenlet) and the rest of the sweep never ran — stranding money in holds that the reaper
    # exists to return.
    async with session_maker() as db:
        reaped = await ledger.reap_stale_holds(db, org_id=org_id)
    assert reaped == 2, "the sweep must survive a lost claim and consider every stale hold"
    async with session_maker() as db:
        left = (await db.execute(select(Hold).where(Hold.id.in_([first, second])))).scalars().all()
    assert left == [], "the second hold was left stranded by the aborted sweep"


@pytest.mark.anyio
async def test_settlement_crossing_a_window_boundary_keeps_the_usage_identity(
    clients: AsyncClient, monkeypatch,
):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        call_id = await ledger.reserve(
            db, org_id, "review.boundary", 100, tags={"customer": "boundary"})

    boundary = datetime(2026, 1, 2)
    original_entry = ledger._entry

    async def entry_just_before_boundary(*args, **kwargs):
        row = await original_entry(*args, **kwargs)
        if kwargs.get("kind") == "settle":
            row.created_at = boundary - timedelta(microseconds=1)
        return row

    monkeypatch.setattr(ledger, "_entry", entry_just_before_boundary)
    monkeypatch.setattr(ledger, "_now", lambda: boundary + timedelta(microseconds=1))
    async with session_maker() as db:
        assert await ledger.settle(db, call_id, 100) == 100
        attributed = sum((await ledger.spend_by_tag(
            db, org_id, "customer", boundary)).values())
        total = (await ledger.spend_since(db, org_id, boundary))["spend_micro"]
        assert attributed == 100
        assert total == 0

