"""Focused adversarial verification for tag billing."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from starlette.requests import Request

from treg import api as A, audit, crypto, ledger, localproxy
from treg.config import get_settings
from treg.db import session_maker
from treg.models import CallRecord, CreditBlock, Hold, LedgerEntry, Membership, Org, TagSpend, User


EP = "tikhub.tiktok.video.comments"
EP_MICRO = 1_000


@pytest.fixture
def platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _org_id(client: AsyncClient) -> int:
    return (await client.get("/orgs")).json()[0]["org_id"]


async def _make_org(name: str, slug: str, *, grant_micro: int = 100_000) -> int:
    async with session_maker() as db:
        org = Org(name=name, slug=slug)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        await ledger.grant(db, org.id, amount_micro=grant_micro, once=False)
        return org.id


def _request_with_meta(value: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": f"/call/{EP}",
        "query_string": b"",
        "headers": [(b"x-treg-meta", value.encode("latin-1"))],
    })


def _assert_meta_rejected(value: str) -> None:
    with pytest.raises(HTTPException) as exc:
        A._parse_call_meta(_request_with_meta(value))
    assert exc.value.status_code == 422


@pytest.mark.anyio
async def test_attack_1_all_ingress_paths_reject_storage_key_delimiters(
    clients: AsyncClient, platform_on,
):
    # Direct HTTP/API ingress. HTTPX itself rejects newlines, so exercise that byte through the ASGI
    # Request object consumed by the same parser; the other delimiters go through the real route.
    for raw in ("customer=a\x1fb", "customer=a,b"):
        response = await clients.get(f"/call/{EP}?aweme_id=1", headers={"X-Treg-Meta": raw})
        assert response.status_code == 422, (raw, response.text)
    _assert_meta_rejected("customer=a\nb")

    # MCP forwards the transport value with str(...) into this API header. Preserve that exact
    # programmatic conversion and prove the downstream parser remains the validation boundary.
    for raw in ("customer=a\x1fb", "customer=a\nb", "customer=a,b"):
        _assert_meta_rejected(str(raw))

    # Characters whose NFKC form contains punctuation, plus a combining sequence, must be rejected
    # before either TagSpend or the scoped idempotency key can receive them.
    for value in ("a，b", "a．b", "e\u0301"):
        with pytest.raises(HTTPException) as exc:
            A._validate_tag_pair("customer", value)
        assert exc.value.status_code == 422

    meta = A._parse_call_meta(_request_with_meta("customer=safe_value"))
    stored_key = A._scoped_idempotency_key("retry-1", meta)
    assert stored_key == "safe_value\x1fretry-1"
    assert all(bad not in meta.primary_val for bad in ("\x1f", "\n", ","))


@pytest.mark.anyio
async def test_attack_2_usage_identity_survives_five_tags_zero_release_settle_and_org_overlap(
    clients: AsyncClient, platform_on, monkeypatch,
):
    org_id = await _org_id(clients)
    tags = {
        "customer": "shared",
        "workspace": "ws_1",
        "project": "proj_1",
        "team": "team_1",
        "cohort": "cohort_1",
    }
    header = ", ".join(f"{key}={value}" for key, value in tags.items())
    response = await clients.get(f"/call/{EP}?aweme_id=five", headers={"X-Treg-Meta": header})
    assert response.status_code == 200, response.text

    for dim in tags:
        usage = (await clients.get(f"/orgs/{org_id}/usage/by-tag?key={dim}")).json()
        assert usage["attributed_micro"] + usage["unattributed_micro"] == usage["total_micro"]

    async with session_maker() as db:
        zero_id = await ledger.reserve(db, org_id, EP, 500, tags={"customer": "zero"})
        assert await ledger.settle(db, zero_id, 0) == 0

        raced_id = await ledger.reserve(db, org_id, EP, 500, tags={"customer": "raced"})

    # Force both lifecycle operations to observe the same Hold before either is allowed to proceed.
    original_get = AsyncSession.get
    readers = 0
    both_loaded = asyncio.Event()

    async def synchronized_get(self, entity, ident, **kwargs):
        nonlocal readers
        row = await original_get(self, entity, ident, **kwargs)
        if entity is Hold and ident == raced_id:
            readers += 1
            if readers == 2:
                both_loaded.set()
            await asyncio.wait_for(both_loaded.wait(), timeout=5)
        return row

    monkeypatch.setattr(AsyncSession, "get", synchronized_get)

    async def attempt_settle():
        async with session_maker() as db:
            return await ledger.settle(db, raced_id, 500)

    async def attempt_release():
        async with session_maker() as db:
            return await ledger.release(db, raced_id, reason="concurrent release")

    # A losing SQLite writer may surface a lock error; the accounting identity must still hold after
    # both attempts have completed or rolled back.
    await asyncio.gather(attempt_settle(), attempt_release(), return_exceptions=True)

    other_org_id = await _make_org("Other adversarial org", "other-adversarial-org")
    async with session_maker() as db:
        other_call = await ledger.reserve(
            db, other_org_id, EP, 700, tags={"customer": "shared"})
        assert await ledger.settle(db, other_call, 700) == 700
        this_org_shared = await ledger.tag_invoice_since(
            db, org_id, "customer", "shared", datetime(2000, 1, 1))
        other_org_shared = await ledger.tag_invoice_since(
            db, other_org_id, "customer", "shared", datetime(2000, 1, 1))
        assert this_org_shared == EP_MICRO
        assert other_org_shared == 700

    usage = (await clients.get(f"/orgs/{org_id}/usage/by-tag?key=customer")).json()
    assert usage["attributed_micro"] + usage["unattributed_micro"] == usage["total_micro"]


@pytest.mark.anyio
async def test_attack_3_settle_and_release_cannot_reattribute_reserved_tags(clients: AsyncClient):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        mutable_tags = {"customer": "reserved_owner"}
        settled_id = await ledger.reserve(db, org_id, EP, 600, tags=mutable_tags)
        mutable_tags["customer"] = "mutated_owner"
        assert await ledger.settle(
            db, settled_id, 600, meta={"customer": "settle_attacker"}) == 600

        assert await ledger.tag_invoice_since(
            db, org_id, "customer", "reserved_owner", datetime(2000, 1, 1)) == 600
        for attacker in ("mutated_owner", "settle_attacker"):
            assert await ledger.tag_invoice_since(
                db, org_id, "customer", attacker, datetime(2000, 1, 1)) == 0

        released_id = await ledger.reserve(
            db, org_id, EP, 400, tags={"customer": "released_owner"})
        assert await ledger.release(
            db, released_id, reason="test", meta={"customer": "release_attacker"}) == 400
        rows = (await db.execute(select(TagSpend).where(
            TagSpend.hold_id == released_id))).scalars().all()
        assert rows == []
        assert await ledger.tag_invoice_since(
            db, org_id, "customer", "release_attacker", datetime(2000, 1, 1)) == 0


@pytest.mark.anyio
async def test_attack_4_concurrent_prechecks_overshoot_is_bounded_not_exact(
    clients: AsyncClient, platform_on, monkeypatch,
):
    org_id = await _org_id(clients)
    cap_micro = 2 * EP_MICRO
    budget = await clients.put(
        f"/orgs/{org_id}/budgets/customer/race", json={"daily_cap_micro": cap_micro})
    assert budget.status_code == 200, budget.text

    original = A._enforce_tag_budgets
    ready = 0
    all_prechecked = asyncio.Event()

    async def synchronized_precheck(caller, meta, db, add_micro=None):
        nonlocal ready
        await original(caller, meta, db, add_micro=add_micro)
        if add_micro is not None:
            ready += 1
            if ready == 4:
                all_prechecked.set()
            await asyncio.wait_for(all_prechecked.wait(), timeout=5)

    monkeypatch.setattr(A, "_enforce_tag_budgets", synchronized_precheck)
    responses = await asyncio.gather(*(
        clients.get(
            f"/call/{EP}?aweme_id=race-{index}",
            headers={"X-Treg-Meta": "customer=race"},
        )
        for index in range(4)
    ))
    assert [response.status_code for response in responses] == [200, 200, 200, 200]

    async with session_maker() as db:
        spent = await ledger.tag_spent_since(
            db, org_id, "customer", "race", datetime(2000, 1, 1, tzinfo=timezone.utc))
        rows = (await db.execute(select(TagSpend).where(
            TagSpend.org_id == org_id,
            TagSpend.dim == "customer",
            TagSpend.val == "race",
        ))).scalars().all()
        one_reservation = max(row.amount_micro for row in rows)

    # The bound is CONCURRENCY × one reservation, not one reservation. This test forces the
    # worst case — every request held at the check until all four have passed it — so all four
    # commit. That is the documented, deliberate behaviour of a soft cap: `ledger.reserve` is exact
    # because the balance is a materialized column it can gate on with one conditional UPDATE, while
    # a per-tag total is an aggregate with no such column. Tightening it would need a second
    # materialized authority on spend (reset daily, decremented on release, corrected on settle
    # divergence) — four new ways to disagree with ledger.py, which is the only module allowed to
    # move money. The hard gates (org balance, per-org daily cap) sit behind this one.
    #
    # What must hold: the overshoot is bounded and every committed call is accounted for. Never
    # describe this cap to a builder as an exact limit.
    in_flight = 4
    assert spent - cap_micro <= in_flight * one_reservation, (
        f"cap={cap_micro}, spent={spent}, one_reservation={one_reservation}")
    assert spent == in_flight * one_reservation, "every committed call must still be attributed"


# ---- additional incident-review attacks --------------------------------------------------------
async def _mint_membership(email: str, org_id: int, role: str = "member") -> str:
    token = crypto.new_token()
    async with session_maker() as db:
        user = User(email=email)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org_id, role=role,
                          token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


def _drop_audit(coro) -> None:
    coro.close()


@pytest.mark.anyio
async def test_review_usage_window_uses_settlement_time(clients: AsyncClient):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, EP, 1_234, tags={"customer": "boundary"})
        row = (await db.execute(select(TagSpend).where(TagSpend.hold_id == call_id))).scalar_one()
        row.created_at = datetime(2000, 1, 1)
        await db.commit()
        assert await ledger.settle(db, call_id, 1_234) == 1_234

    usage = (await clients.get(f"/orgs/{org_id}/usage/by-tag?key=customer&days=1")).json()
    assert [(row["value"], row["charged_micro"]) for row in usage["rows"]] == [("boundary", 1_234)]
    assert usage["attributed_micro"] == usage["total_micro"] == 1_234
    assert usage["unattributed_micro"] == 0


@pytest.mark.anyio
async def test_review_reserve_failure_rolls_back_tag_and_balance_together(clients: AsyncClient):
    org_id = await _org_id(clients)
    call_id = f"duplicate-{uuid.uuid4().hex}"
    async with session_maker() as db:
        before = await ledger.balance_of(db, org_id)
        await ledger.reserve(db, org_id, EP, 100, tags={"customer": "first"}, call_id=call_id)
    async with session_maker() as db:
        with pytest.raises(IntegrityError):
            await ledger.reserve(db, org_id, EP, 200, tags={"customer": "second"}, call_id=call_id)
        await db.rollback()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == before - 100
        rows = (await db.execute(select(TagSpend).where(TagSpend.hold_id == call_id))).scalars().all()
        assert [(row.val, row.amount_micro) for row in rows] == [("first", 100)]
        await ledger.release(db, call_id, reason="cleanup")


@pytest.mark.anyio
async def test_review_concurrent_settle_release_preserves_the_money_invariant(
    clients: AsyncClient, monkeypatch,
):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        call_id = await ledger.reserve(
            db, org_id, EP, 500, tags={"customer": "settle-release-race"})

    original_get = AsyncSession.get
    readers = 0
    both_loaded = asyncio.Event()

    async def synchronized_get(self, entity, ident, **kwargs):
        nonlocal readers
        row = await original_get(self, entity, ident, **kwargs)
        if entity is Hold and ident == call_id:
            readers += 1
            if readers == 2:
                both_loaded.set()
            await asyncio.wait_for(both_loaded.wait(), timeout=5)
        return row

    monkeypatch.setattr(AsyncSession, "get", synchronized_get)

    async def settle():
        async with session_maker() as db:
            return await ledger.settle(db, call_id, 500)

    async def release():
        async with session_maker() as db:
            return await ledger.release(db, call_id, reason="racing_release")

    results = await asyncio.gather(settle(), release(), return_exceptions=True)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        blocks = (await db.execute(select(CreditBlock).where(
            CreditBlock.org_id == org_id))).scalars().all()
        holds = (await db.execute(select(Hold).where(Hold.org_id == org_id))).scalars().all()
        assert org.balance_micro == sum(block.remaining_micro for block in blocks) - sum(
            hold.amount_micro for hold in holds), results
        terminal = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id,
            LedgerEntry.kind.in_(["settle", "release"]),
        ))).scalars().all()
        assert len(terminal) == 1, (results, [(entry.kind, entry.amount_micro) for entry in terminal])


@pytest.mark.anyio
async def test_review_pin_bypass_matrix_never_attributes_a_different_value(
    clients: AsyncClient, platform_on,
):
    org_id = await _org_id(clients)
    made = await clients.post(
        f"/orgs/{org_id}/agents",
        json={"name": "review-pinned", "pinned_tags": {"customer": "cust_A"}},
    )
    assert made.status_code == 200, made.text
    token = made.json()["token"]
    attempts = (
        ({"X-Treg-Token": token}, 200),
        ({"X-Treg-Token": token, "X-Treg-Meta": " CUSTOMER = cust_A "}, 200),
        ({"X-Treg-Token": token, "X-Treg-Meta": "customer=CUST_A"}, 403),
        ({"X-Treg-Token": token, "X-Treg-Meta": "customer=cust_A, workspace=ws_9"}, 200),
        ([("X-Treg-Token", token), ("X-Treg-Meta", "customer=cust_A"),
          ("X-Treg-Meta", "customer=cust_B")], 200),
        ([("X-Treg-Token", token), ("X-Treg-Meta", "customer=cust_B"),
          ("X-Treg-Meta", "customer=cust_A")], 403),
    )
    successes = 0
    for headers, expected in attempts:
        response = await clients.get(f"/call/{EP}?aweme_id=pin", headers=headers)
        assert response.status_code == expected, (headers, response.status_code, response.text)
        successes += response.status_code == 200
    # Exercise a non-ASCII lookalike through the shared pin/header validator.
    with pytest.raises(HTTPException) as exc:
        A._validate_tag_pair("customer", "cust_\u00c0")
    assert exc.value.status_code == 422
    async with session_maker() as db:
        assert await ledger.tag_invoice_since(
            db, org_id, "customer", "cust_B", datetime(2000, 1, 1)) == 0
        assert await ledger.tag_invoice_since(
            db, org_id, "customer", "cust_A", datetime(2000, 1, 1)) == successes * EP_MICRO


@pytest.mark.anyio
async def test_review_pins_cannot_bypass_the_five_pair_limit(clients: AsyncClient):
    org_id = await _org_id(clients)
    response = await clients.post(
        f"/orgs/{org_id}/agents",
        json={"name": "six-pins", "pinned_tags": {f"tag{i}": f"v{i}" for i in range(6)}},
    )
    assert response.status_code == 422, response.text


@pytest.mark.anyio
async def test_review_distinct_memberships_never_share_idempotent_bodies(
    clients: AsyncClient, platform_on, monkeypatch,
):
    org_id = await _org_id(clients)
    token_a = await _mint_membership(f"a-{uuid.uuid4().hex}@example.com", org_id)
    token_b = await _mint_membership(f"b-{uuid.uuid4().hex}@example.com", org_id)

    async def relay_by_token(request, upstream_url, tool, secrets, client, drop_params=None,
                             force_identity=False):
        body = request.headers["X-Treg-Token"].encode()

        async def stream():
            yield body

        return StreamingResponse(stream(), status_code=200, media_type="text/plain")

    monkeypatch.setattr(A, "relay", relay_by_token)
    common = {"X-Treg-Meta": "customer=same", "Idempotency-Key": "same-key"}
    first_a = await clients.get(f"/call/{EP}?aweme_id=idem", headers={**common, "X-Treg-Token": token_a})
    first_b = await clients.get(f"/call/{EP}?aweme_id=idem", headers={**common, "X-Treg-Token": token_b})
    replay_a = await clients.get(f"/call/{EP}?aweme_id=idem", headers={**common, "X-Treg-Token": token_a})
    replay_b = await clients.get(f"/call/{EP}?aweme_id=idem", headers={**common, "X-Treg-Token": token_b})
    assert first_a.content == replay_a.content == token_a.encode()
    assert first_b.content == replay_b.content == token_b.encode()
    assert first_a.content != first_b.content


@pytest.mark.anyio
async def test_review_replay_keeps_the_original_call_id(clients: AsyncClient, platform_on):
    headers = {"X-Treg-Meta": "customer=cust_A", "Idempotency-Key": "review-call-id"}
    first = await clients.get(f"/call/{EP}?aweme_id=idem", headers=headers)
    replay = await clients.get(f"/call/{EP}?aweme_id=idem", headers=headers)
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert replay.headers["X-Treg-Call-Id"] == first.headers["X-Treg-Call-Id"]


def test_review_idempotency_separator_and_post_fingerprint_are_unambiguous():
    scoped = {
        A._scoped_idempotency_key(key, A.CallMeta(tags={"customer": customer}))
        for customer in ("A", "AB", "A-B", "A:B")
        for key in ("C", f"B{A._IDEM_SCOPE_SEP}C", f"{A._IDEM_SCOPE_SEP}C")
    }
    assert len(scoped) == 12
    original = A._request_fingerprint("POST", "endpoint", b'{"amount":1}', "mode=fast")
    assert original == A._request_fingerprint("post", "endpoint", b'{"amount":1}', "mode=fast")
    assert original != A._request_fingerprint("POST", "endpoint", b'{"amount":2}', "mode=fast")
    assert original != A._request_fingerprint("POST", "endpoint", b'{"amount":1}', "mode=slow")


@pytest.mark.anyio
async def test_review_primary_call_cap_survives_audit_loss(
    clients: AsyncClient, platform_on, monkeypatch,
):
    org_id = await _org_id(clients)
    response = await clients.put(
        f"/orgs/{org_id}/budgets/customer/cust_A", json={"calls_per_day": 1})
    assert response.status_code == 200, response.text
    monkeypatch.setattr(audit, "_schedule", _drop_audit)
    headers = {"X-Treg-Meta": "customer=cust_A"}
    assert (await clients.get(f"/call/{EP}?aweme_id=one", headers=headers)).status_code == 200
    refused = await clients.get(f"/call/{EP}?aweme_id=two", headers=headers)
    assert refused.status_code == 429, refused.text


@pytest.mark.anyio
async def test_review_non_primary_call_cap_and_export_counts_work(
    clients: AsyncClient, platform_on,
):
    org_id = await _org_id(clients)
    response = await clients.patch(
        f"/orgs/{org_id}/settings", json={"budget_dims": ["customer", "workspace"]})
    assert response.status_code == 200, response.text
    response = await clients.put(
        f"/orgs/{org_id}/budgets/workspace/ws_9", json={"calls_per_day": 1})
    assert response.status_code == 200, response.text
    headers = {"X-Treg-Meta": "customer=cust_A, workspace=ws_9"}
    assert (await clients.get(f"/call/{EP}?aweme_id=one", headers=headers)).status_code == 200
    await audit.drain()
    usage = (await clients.get(f"/orgs/{org_id}/usage/by-tag?key=workspace")).json()
    assert usage["rows"][0]["calls"] == 1
    refused = await clients.get(f"/call/{EP}?aweme_id=two", headers=headers)
    assert refused.status_code == 429, refused.text


@pytest.mark.anyio
async def test_review_every_ledger_writer_overrides_false_provenance(clients: AsyncClient):
    org_id = await _org_id(clients)
    suffix = uuid.uuid4().hex
    async with session_maker() as db:
        grant = await ledger.grant(
            db, org_id, amount_micro=10, kind=f"promo-{suffix}", once=False,
            meta={"block_kind": "spoofed"})
        topup = await ledger.topup(
            db, org_id, 10, f"payment-{suffix}", meta={"payment_ref": "spoofed"})
        call_id = await ledger.reserve(
            db, org_id, EP, 10, call_id=f"settle-{suffix}",
            meta={"estimated_micro": -1, "charged_micro": -1, "margin": "spoofed"})
        await ledger.settle(
            db, call_id, 5,
            meta={"reserved_micro": -1, "observed_micro": -1, "settled_micro": -1,
                  "consumed_micro": -1, "refunded_micro": -1, "margin": "spoofed",
                  "block_shortfall_micro": 999})
        release_id = await ledger.reserve(db, org_id, EP, 10, call_id=f"release-{suffix}")
        await ledger.release(db, release_id, reason="real", meta={"reason": "spoofed"})
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id == org_id))).scalars().all()
        assert next(e for e in entries if e.kind == "grant" and e.block_id == grant.id).meta[
            "block_kind"] == f"promo-{suffix}"
        assert next(e for e in entries if e.kind == "topup" and e.block_id == topup.id).meta[
            "payment_ref"] == f"payment-{suffix}"
        settle = next(e for e in entries if e.kind == "settle" and e.call_id == call_id)
        assert settle.meta["reserved_micro"] == 10
        assert settle.meta["consumed_micro"] == 5
        assert settle.meta["block_shortfall_micro"] == 0
        assert next(e for e in entries if e.kind == "release" and e.call_id == release_id).meta[
            "reason"] == "real"
        assert all(isinstance(entry.amount_micro, int) for entry in entries)


def test_review_local_proxy_keeps_meta_but_replaces_identity():
    cfg = localproxy.ProxyConfig(token="proxy", treg_token="real-token", org="real-org")
    out = localproxy._forward_headers(cfg, [
        ("X-Treg-Meta", "customer=cust_A"), ("X-Treg-Token", "stolen"),
        ("X-Treg-Org", "victim"), ("X-Custom", "kept"),
    ])
    grouped: dict[str, list[str]] = {}
    for key, value in out:
        grouped.setdefault(key.lower(), []).append(value)
    assert grouped["x-treg-meta"] == ["customer=cust_A"]
    assert grouped["x-treg-token"] == ["real-token"]
    assert grouped["x-treg-org"] == ["real-org"]
    assert grouped["x-custom"] == ["kept"]
