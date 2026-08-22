"""Caller tags (`X-Treg-Meta`) — the attribution half of builder-pays billing.

A builder reselling treg runs their whole user base through ONE org, ONE balance and ONE token, and
tags each call with their own ids (`customer=cust_8123, workspace=ws_9`). This file covers phase 0:
the tags are parsed once, validated strictly, recorded on the audit row, and never reach the provider.

The strictness is the point. A tag that is silently dropped or truncated is usage that falls out of
the builder's invoice without anyone noticing, and a truncated id merges two of their users into one
line — so every violation here is a 422 and never a repair.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from sqlmodel import select

from treg import api as A, audit, crypto, ledger
from treg.config import get_settings
from treg.db import session_maker
from treg.models import Membership, Org, TagSpend, User

EP = "tikhub.tiktok.video.comments"
_EPOCH = datetime(2000, 1, 1)


async def _mk_echo_tool(c: AsyncClient, name: str = "echo") -> None:
    await c.post("/tools", json={"name": name, "base_url": "http://upstream",
                                 "auth": {"kind": "bearer", "secret_name": name}})
    await c.post("/secrets", json={"name": name, "value": "S"})


async def _newest_call(c: AsyncClient) -> dict:
    await audit.drain()  # audit rows are fire-and-forget
    return (await c.get("/calls")).json()[0]


# ---- attribution -------------------------------------------------------------------------------
async def test_tags_land_on_the_audit_row(clients: AsyncClient):
    """The whole bag is recorded, and the primary dimension is copied out to its own columns."""
    await _mk_echo_tool(clients)
    r = await clients.get("/call/echo/anything", headers={
        "X-Treg-Meta": "customer=cust_8123, workspace=ws_9, feature=email-finder"})
    assert r.status_code == 200, r.text
    row = await _newest_call(clients)
    assert row["tags"] == {"customer": "cust_8123", "workspace": "ws_9", "feature": "email-finder"}
    assert row["budget_dim"] == "customer"      # the org's primary dimension
    assert row["budget_val"] == "cust_8123"


async def test_a_bag_without_the_primary_dimension_still_records_its_tags(clients: AsyncClient):
    """Tagging by workspace alone is legitimate — it just has no primary value to index."""
    await _mk_echo_tool(clients)
    await clients.get("/call/echo/x", headers={"X-Treg-Meta": "workspace=ws_9"})
    row = await _newest_call(clients)
    assert row["tags"] == {"workspace": "ws_9"}
    assert row["budget_dim"] == "" and row["budget_val"] == ""


async def test_untagged_calls_are_unchanged(clients: AsyncClient):
    """No header means exactly today's behaviour — this is what protects every existing caller."""
    await _mk_echo_tool(clients)
    r = await clients.get("/call/echo/x")
    assert r.status_code == 200
    row = await _newest_call(clients)
    assert row["tags"] is None
    assert row["budget_dim"] == "" and row["budget_val"] == ""


# ---- validation: every one of these is unbilled usage if we accept it instead --------------------
async def test_oversized_and_malformed_bags_are_refused(clients: AsyncClient):
    await _mk_echo_tool(clients)
    for bad in (
        "a=1,b=2,c=3,d=4,e=5,f=6",                  # 6 keys
        "x" * 33 + "=1",                            # key too long
        "customer=" + "c" * 129,                    # value too long
        "customer",                                 # no `=`
        "CUSTOMER!=1",                              # illegal key characters
        "customer=",                                # empty value
        "customer=a,customer=b",                    # names one key twice
    ):
        r = await clients.get("/call/echo/x", headers={"X-Treg-Meta": bad})
        assert r.status_code == 422, f"{bad!r} should be refused, got {r.status_code}"


async def test_email_shaped_values_are_refused(clients: AsyncClient):
    """The ledger is append-only, so a tag written today cannot be erased on request tomorrow."""
    await _mk_echo_tool(clients)
    r = await clients.get("/call/echo/x", headers={"X-Treg-Meta": "customer=jane@example.com"})
    assert r.status_code == 422
    assert "append-only" in r.json()["detail"]


async def test_a_refused_bag_never_reaches_the_provider(clients: AsyncClient):
    """Parsing happens before anything is resolved or relayed, so a 422 costs nothing upstream."""
    await _mk_echo_tool(clients)
    before = len((await clients.get("/calls")).json())
    r = await clients.get("/call/echo/x", headers={"X-Treg-Meta": "a=1,b=2,c=3,d=4,e=5,f=6"})
    assert r.status_code == 422
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert all(row["status_code"] != 200 for row in rows[:len(rows) - before]), \
        "a rejected bag must not have produced a successful upstream call"


async def test_a_malformed_bag_does_not_burn_an_idempotency_label(clients: AsyncClient):
    """The parse sits ABOVE the idempotency claim — otherwise a typo'd header would lock a label
    for 24 hours and the retry would 409 against a call that never happened."""
    await _mk_echo_tool(clients)
    hdr = {"Idempotency-Key": "retry-1"}
    bad = await clients.get("/call/echo/x", headers={**hdr, "X-Treg-Meta": "customer"})
    assert bad.status_code == 422
    good = await clients.get("/call/echo/x", headers={**hdr, "X-Treg-Meta": "customer=cust_1"})
    assert good.status_code == 200, good.text


# ---- egress ------------------------------------------------------------------------------------
async def test_no_treg_header_reaches_the_upstream(clients: AsyncClient):
    """Customer ids must not be handed to third-party providers. Asserted by PREFIX, not by name:
    the old per-name list had already missed `x-treg-client`, which leaked to every provider."""
    await _mk_echo_tool(clients)
    r = await clients.get("/call/echo/anything", headers={
        "X-Treg-Meta": "customer=cust_8123",
        "X-Treg-Client": "claude-code",
        "X-Treg-Future": "not-invented-yet",     # a header that does not exist yet must also be dropped
    })
    assert r.status_code == 200, r.text
    seen = {k.lower() for k in r.json()["headers"]}
    assert not [k for k in seen if k.startswith("x-treg-")], f"treg headers leaked upstream: {seen}"


async def test_tags_do_not_disturb_the_relayed_request(clients: AsyncClient):
    """Tagging changes nothing the provider sees — same path, same query, same auth."""
    await _mk_echo_tool(clients)
    plain = (await clients.get("/call/echo/thing?q=1")).json()
    tagged = (await clients.get("/call/echo/thing?q=1",
                                headers={"X-Treg-Meta": "customer=cust_1"})).json()
    assert plain["raw_path"] == tagged["raw_path"]
    assert plain["query"] == tagged["query"]
    assert plain["auth"] == tagged["auth"]



# ---- phase 1: the money rows --------------------------------------------------------------------
# Tier 4 (treg's own key, metered against the org balance) is the only path that spends money, so the
# tests below turn it on the way a deploy does. A fresh org already holds the $1 promo grant.
EP_MICRO = 1_000  # tikhub comments: $0.001/call


@pytest.fixture
def platform_on(monkeypatch):
    for name, value in {"TIKHUB": "PLATFORM-TIKHUB-KEY"}.items():
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{name}", value)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_relay(status_code: int, body: bytes = b"{}"):
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None,
                     force_identity=False):
        async def _stream():
            yield body
        return StreamingResponse(_stream(), status_code=status_code)
    return _relay


async def _org_id(c: AsyncClient) -> int:
    return (await c.get("/orgs")).json()[0]["org_id"]


async def test_a_metered_call_attributes_its_spend_to_every_tag(clients: AsyncClient, platform_on):
    """The same dollar lands under `customer` AND under `workspace` — cost-allocation-tag semantics.
    Summing WITHIN a dimension reconciles to the org total; summing ACROSS them double-counts, which
    is expected and is why an invoice always names the dimension it is grouping by."""
    org_id = await _org_id(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7",
                          headers={"X-Treg-Meta": "customer=cust_A, workspace=ws_9"})
    assert r.status_code == 200, r.text
    charged = int(r.headers["X-Treg-Cost-Micro"])
    assert charged == EP_MICRO

    async with session_maker() as db:
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == charged
        assert await ledger.tag_invoice_since(db, org_id, "workspace", "ws_9", _EPOCH) == charged
        # An untouched value of the same dimension is unaffected.
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_B", _EPOCH) == 0


async def test_untagged_metered_calls_write_no_tag_rows(clients: AsyncClient, platform_on):
    """Existing callers keep working and simply have nothing to attribute — their spend shows up in
    the usage report's `unattributed` bucket rather than under someone else's name."""
    org_id = await _org_id(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        rows = (await db.execute(select(TagSpend).where(TagSpend.org_id == org_id))).scalars().all()
        assert rows == []


async def test_a_released_call_bills_no_tag(clients: AsyncClient, platform_on, monkeypatch):
    """A provider 5xx releases the hold in full, so the builder's user must not be billed for it —
    and the tag rows go with it rather than lingering as phantom spend."""
    monkeypatch.setattr(A, "relay", _fake_relay(503, b"nope"))
    org_id = await _org_id(clients)
    await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    async with session_maker() as db:
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == 0
        assert await ledger.tag_spent_since(db, org_id, "customer", "cust_A", _EPOCH) == 0


async def test_in_flight_spend_counts_toward_a_cap_but_not_an_invoice(clients: AsyncClient):
    """An open hold is committed money (a cap must see it) but not spend (an invoice must not).
    The two reads exist as separate functions for exactly this reason."""
    org_id = await _org_id(clients)
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, EP, 5_000, tags={"customer": "cust_A"})
        assert await ledger.tag_spent_since(db, org_id, "customer", "cust_A", _EPOCH) == 5_000
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == 0
        await ledger.settle(db, call_id, 2_000)
        # Settle rewrites the row to what the call actually consumed, not the estimate it reserved.
        assert await ledger.tag_spent_since(db, org_id, "customer", "cust_A", _EPOCH) == 2_000
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == 2_000


async def test_tag_totals_reconcile_with_org_spend(clients: AsyncClient, platform_on):
    """The identity a builder's invoice depends on: for ANY key, the per-value totals plus whatever
    could not be attributed equal the org's own settled spend for the window. It must hold whichever
    dimension you slice by — that is what proves stacked reports agree with each other."""
    org_id = await _org_id(clients)
    for tags in ("customer=cust_A, workspace=ws_1",
                 "customer=cust_B, workspace=ws_1",
                 "customer=cust_C, workspace=ws_2"):
        assert (await clients.get(f"/call/{EP}?aweme_id=7",
                                  headers={"X-Treg-Meta": tags})).status_code == 200
    await clients.get(f"/call/{EP}?aweme_id=7")          # one untagged call

    async with session_maker() as db:
        org_total = (await ledger.spend_since(db, org_id, _EPOCH))["spend_micro"]
        for dim, expected_values in (("customer", 3), ("workspace", 2)):
            by_value = await ledger.spend_by_tag(db, org_id, dim, _EPOCH)
            assert len(by_value) == expected_values
            unattributed = org_total - sum(by_value.values())
            assert sum(by_value.values()) + unattributed == org_total
            assert unattributed == EP_MICRO, "the untagged call must show up as unattributed"


async def test_caller_tags_cannot_overwrite_ledger_provenance(clients: AsyncClient, platform_on):
    """A hostile bag must not rewrite the money journal. treg's own keys merge LAST now — before this
    change a caller could zero `charged_micro` or forge the `tier` that reconcile.py reads."""
    org_id = await _org_id(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7",
                          headers={"X-Treg-Meta": "charged_micro=0, margin=9, tier=free"})
    assert r.status_code == 200, r.text
    entries = (await clients.get(f"/orgs/{org_id}/balance")).json()["entries"]["items"]
    settle = next(e for e in entries if e["kind"] == "settle")
    assert settle["meta"]["margin"] != "9", "caller rewrote the margin recorded on a money row"
    assert settle["meta"]["consumed_micro"] == EP_MICRO
    reserve = next(e for e in entries if e["kind"] == "reserve")
    assert reserve["meta"]["charged_micro"] == EP_MICRO, "caller zeroed a recorded charge"
    assert reserve["meta"]["tier"] == "platform", "caller overwrote the credential tier"


async def test_deleting_a_team_takes_its_tag_rows(clients: AsyncClient, platform_on):
    """`TagSpend` is org-scoped, so it must go when the team does (tests/test_orgs.py enforces the
    registration; this proves the cascade order actually works with a live row present)."""
    org_id = await _org_id(clients)
    await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    r = await clients.delete(f"/orgs/{org_id}?confirm={slug}")
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        rows = (await db.execute(select(TagSpend).where(TagSpend.org_id == org_id))).scalars().all()
        assert rows == []


# ---- phase 2: budgets, blocking, stacking --------------------------------------------------------
async def _set_budget(c: AsyncClient, org_id: int, dim: str, val: str, **fields) -> dict:
    r = await c.put(f"/orgs/{org_id}/budgets/{dim}/{val}", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


async def _declare_dims(org_id: int, *dims: str) -> None:
    """Declare which tag keys carry budgets for this team (the API for this is the org PATCH; the
    tests set it directly to keep the budget assertions about budgets)."""
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.budget_dims = list(dims)
        await db.commit()


async def test_no_budget_row_means_unlimited(clients: AsyncClient, platform_on):
    """Builders never pre-register a user: the first call for an unknown id just works."""
    org_id = await _org_id(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=brand_new"})
    assert r.status_code == 200, r.text


async def test_a_blocked_tag_is_refused(clients: AsyncClient, platform_on):
    org_id = await _org_id(clients)
    await _set_budget(clients, org_id, "customer", "cust_A", status="blocked")
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "tag_blocked"
    # ...and a different user of the same builder is untouched.
    ok = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_B"})
    assert ok.status_code == 200, ok.text


async def test_a_blocked_tag_is_refused_before_any_cached_answer(clients: AsyncClient, platform_on):
    """Blocking must beat the idempotency replay, or a user blocked at noon keeps being served the
    answer they paid for at 11 — and keeps holding a lock while doing it."""
    org_id = await _org_id(clients)
    hdr = {"X-Treg-Meta": "customer=cust_A", "Idempotency-Key": "retry-1"}
    first = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    assert first.status_code == 200, first.text
    await _set_budget(clients, org_id, "customer", "cust_A", status="blocked")
    again = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    assert again.status_code == 403
    assert "X-Treg-Idempotent-Replay" not in again.headers


async def test_a_spend_cap_refuses_and_names_its_dimension(clients: AsyncClient, platform_on):
    org_id = await _org_id(clients)
    await _set_budget(clients, org_id, "customer", "cust_A", daily_cap_micro=EP_MICRO)
    first = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    assert first.status_code == 200, first.text          # exactly at the cap
    second = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    assert second.status_code == 429, second.text
    d = second.json()["detail"]
    assert d["error"] == "tag_spend_cap_reached"
    assert (d["dim"], d["val"]) == ("customer", "cust_A")
    assert d["cap_micro"] == EP_MICRO


async def test_a_tag_refusal_leaks_nothing_about_the_team(clients: AsyncClient, platform_on):
    """This body is the one a builder renders to their OWN end user. The team's balance, slug and
    top-up link are the builder's private numbers and must not ride along."""
    org_id = await _org_id(clients)
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    await _set_budget(clients, org_id, "customer", "cust_A", daily_cap_micro=1)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    assert r.status_code == 429
    assert set(r.json()["detail"]) == {
        "error", "dim", "val", "spent_micro", "cap_micro", "period", "estimated_cost_micro", "message"}
    blob = r.text
    assert slug not in blob and "balance" not in blob and "topup" not in blob


async def test_stacked_budgets_refuse_independently(clients: AsyncClient, platform_on):
    """A workspace ceiling and a per-user ceiling apply to the same call. The tighter one bites
    first, names itself, and leaves the workspace's other users working."""
    org_id = await _org_id(clients)
    await _declare_dims(org_id, "customer", "workspace")
    await _set_budget(clients, org_id, "workspace", "ws_1", daily_cap_micro=EP_MICRO * 5)
    await _set_budget(clients, org_id, "customer", "cust_A", daily_cap_micro=EP_MICRO)

    hdr_a = {"X-Treg-Meta": "customer=cust_A, workspace=ws_1"}
    hdr_b = {"X-Treg-Meta": "customer=cust_B, workspace=ws_1"}
    assert (await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr_a)).status_code == 200
    capped = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr_a)
    assert capped.status_code == 429
    assert capped.json()["detail"]["dim"] == "customer", "the tighter ceiling must be the one that names itself"
    # cust_B shares the workspace, which still has room.
    assert (await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr_b)).status_code == 200


async def test_the_workspace_ceiling_bites_once_its_users_together_exceed_it(clients: AsyncClient,
                                                                             platform_on):
    org_id = await _org_id(clients)
    await _declare_dims(org_id, "customer", "workspace")
    await _set_budget(clients, org_id, "workspace", "ws_1", daily_cap_micro=EP_MICRO * 2)
    for who in ("cust_A", "cust_B"):
        r = await clients.get(f"/call/{EP}?aweme_id=7",
                              headers={"X-Treg-Meta": f"customer={who}, workspace=ws_1"})
        assert r.status_code == 200, r.text
    blocked = await clients.get(f"/call/{EP}?aweme_id=7",
                                headers={"X-Treg-Meta": "customer=cust_C, workspace=ws_1"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["dim"] == "workspace"


async def test_untagged_traffic_ignores_every_budget(clients: AsyncClient, platform_on):
    org_id = await _org_id(clients)
    await _set_budget(clients, org_id, "customer", "cust_A", daily_cap_micro=1)
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200


async def test_budget_crud_is_admin_only_and_partial(clients: AsyncClient):
    org_id = await _org_id(clients)
    made = await _set_budget(clients, org_id, "customer", "cust_A",
                             daily_cap_micro=5_000, note="trial plan")
    assert made["daily_cap_micro"] == 5_000 and made["status"] == "active"
    # A PUT that only blocks must not wipe the cap someone set last week.
    again = await _set_budget(clients, org_id, "customer", "cust_A", status="blocked")
    assert again["daily_cap_micro"] == 5_000 and again["note"] == "trial plan"
    assert again["status"] == "blocked"
    listed = (await clients.get(f"/orgs/{org_id}/budgets")).json()
    assert [(b["dim"], b["val"]) for b in listed] == [("customer", "cust_A")]
    assert (await clients.delete(f"/orgs/{org_id}/budgets/customer/cust_A")).status_code == 200
    assert (await clients.get(f"/orgs/{org_id}/budgets")).json() == []


# ---- phase 3: idempotency is partitioned by the primary tag --------------------------------------
async def test_two_users_can_reuse_one_idempotency_label(clients: AsyncClient, platform_on):
    """THE cross-tenant leak this feature had to close. One builder token, two of their users, the
    same obvious label, byte-identical requests: without scoping the second is handed the first's
    stored response body — the exact failure IdempotentCall exists to prevent, one level down."""
    hdr = {"Idempotency-Key": "retry-1"}
    a = await clients.get(f"/call/{EP}?aweme_id=7",
                          headers={**hdr, "X-Treg-Meta": "customer=cust_A"})
    b = await clients.get(f"/call/{EP}?aweme_id=7",
                          headers={**hdr, "X-Treg-Meta": "customer=cust_B"})
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    assert "X-Treg-Idempotent-Replay" not in b.headers, "cust_B was served cust_A's stored answer"
    # Both really called the provider, and both were billed.
    org_id = await _org_id(clients)
    async with session_maker() as db:
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == EP_MICRO
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_B", _EPOCH) == EP_MICRO


async def test_one_user_still_replays_their_own_label(clients: AsyncClient, platform_on):
    """Scoping must not break what idempotency is for: the SAME user retrying is still answered from
    the stored response and billed once."""
    hdr = {"Idempotency-Key": "retry-1", "X-Treg-Meta": "customer=cust_A"}
    first = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    again = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    assert first.status_code == 200 and again.status_code == 200
    assert again.headers.get("X-Treg-Idempotent-Replay") == "true"
    org_id = await _org_id(clients)
    async with session_maker() as db:
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == EP_MICRO


async def test_untagged_idempotency_is_unchanged(clients: AsyncClient, platform_on):
    """An untagged caller keeps exactly today's behaviour — the key is stored unscoped."""
    hdr = {"Idempotency-Key": "retry-1"}
    first = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    again = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    assert first.status_code == 200 and again.headers.get("X-Treg-Idempotent-Replay") == "true"


async def test_a_reused_label_on_a_different_request_still_says_so(clients: AsyncClient, platform_on):
    """The fingerprint refusal survives scoping, and the message quotes the label the CALLER wrote —
    not treg's internal scoped form."""
    hdr = {"Idempotency-Key": "retry-1", "X-Treg-Meta": "customer=cust_A"}
    await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    clash = await clients.get(f"/call/{EP}?aweme_id=99", headers=hdr)
    assert clash.status_code == 422
    assert "'retry-1'" in clash.json()["detail"], clash.text


# ---- phase 4: pinning, the team ceiling, export, join key -----------------------------------------
async def _mint_agent(c: AsyncClient, org_id: int, name: str, **kw) -> str:
    r = await c.post(f"/orgs/{org_id}/agents", json={"name": name, **kw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_a_pinned_token_cannot_bill_another_customer(clients: AsyncClient, platform_on):
    """The whole point of handing a scoped token to a customer's own machine. If the header won, that
    customer could retag their calls and walk straight out of their own budget."""
    org_id = await _org_id(clients)
    token = await _mint_agent(clients, org_id, "cust-a-bot", pinned_tags={"customer": "cust_A"})
    hdr = {"X-Treg-Token": token}

    bad = await clients.get(f"/call/{EP}?aweme_id=7",
                            headers={**hdr, "X-Treg-Meta": "customer=cust_B"})
    assert bad.status_code == 403, bad.text
    async with session_maker() as db:
        assert (await db.execute(select(TagSpend))).scalars().all() == [], \
            "a refused pin must not have reserved anything"

    # Naming its OWN customer is fine, so a builder can send the header unconditionally...
    ok = await clients.get(f"/call/{EP}?aweme_id=7",
                           headers={**hdr, "X-Treg-Meta": "customer=cust_A"})
    assert ok.status_code == 200, ok.text
    # ...and sending no header at all still attributes to the pin.
    bare = await clients.get(f"/call/{EP}?aweme_id=7", headers=hdr)
    assert bare.status_code == 200, bare.text
    async with session_maker() as db:
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == EP_MICRO * 2


async def test_rotating_a_pinned_token_keeps_its_pin(clients: AsyncClient):
    """A rotate replaces the token, never the limits — re-minting must not silently unpin."""
    org_id = await _org_id(clients)
    await _mint_agent(clients, org_id, "cust-a-bot", pinned_tags={"customer": "cust_A"})
    r = await clients.post(f"/orgs/{org_id}/agents", json={"name": "cust-a-bot"})
    assert r.status_code == 200, r.text
    assert r.json()["pinned_tags"] == {"customer": "cust_A"}


async def test_a_team_can_lower_its_daily_cap_but_not_raise_it(clients: AsyncClient):
    """Two masters: the team protects itself from a runaway agent, we protect ourselves from a
    catalog mispricing. Lowering is theirs; raising past our ceiling is a conversation."""
    org_id = await _org_id(clients)
    settings = (await clients.get(f"/orgs/{org_id}/settings")).json()
    ceiling = settings["platform_ceiling_micro"]
    assert settings["daily_cap_micro"] == ceiling

    lowered = await clients.patch(f"/orgs/{org_id}/settings", json={"daily_cap_micro": 1_000})
    assert lowered.status_code == 200 and lowered.json()["daily_cap_micro"] == 1_000

    too_big = await clients.patch(f"/orgs/{org_id}/settings",
                                  json={"daily_cap_micro": ceiling + 1})
    assert too_big.status_code == 403
    assert too_big.json()["detail"]["error"] == "above_platform_ceiling"


async def test_the_team_cap_actually_refuses_spend(clients: AsyncClient, platform_on):
    org_id = await _org_id(clients)
    await clients.patch(f"/orgs/{org_id}/settings", json={"daily_cap_micro": EP_MICRO})
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    over = await clients.get(f"/call/{EP}?aweme_id=7")
    assert over.status_code == 429
    assert over.json()["detail"]["error"] == "platform_daily_cap_reached"


async def test_declaring_more_than_three_budget_dimensions_is_refused(clients: AsyncClient):
    org_id = await _org_id(clients)
    r = await clients.patch(f"/orgs/{org_id}/settings",
                            json={"budget_dims": ["a", "b", "c", "d"]})
    assert r.status_code == 422


async def test_usage_by_tag_reconciles_and_reports_the_unattributed(clients: AsyncClient, platform_on):
    """What a builder invoices from. Money comes from the ledger, and the spend they cannot attribute
    is reported rather than dropped — otherwise their books and ours quietly stop agreeing."""
    org_id = await _org_id(clients)
    await clients.patch(f"/orgs/{org_id}/settings", json={"budget_dims": ["customer", "workspace"]})
    for tags in ("customer=cust_A, workspace=ws_1",
                 "customer=cust_B, workspace=ws_1",
                 "customer=cust_C, workspace=ws_2"):
        assert (await clients.get(f"/call/{EP}?aweme_id=7",
                                  headers={"X-Treg-Meta": tags})).status_code == 200
    await clients.get(f"/call/{EP}?aweme_id=7")          # untagged

    for key, expected_rows in (("customer", 3), ("workspace", 2)):
        r = await clients.get(f"/orgs/{org_id}/usage/by-tag?key={key}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert len(d["rows"]) == expected_rows
        assert d["attributed_micro"] + d["unattributed_micro"] == d["total_micro"]
        assert d["unattributed_micro"] == EP_MICRO, "the untagged call must be visible, not dropped"
        assert d["total_micro"] == EP_MICRO * 4


async def test_usage_by_tag_is_admin_only(clients: AsyncClient):
    org_id = await _org_id(clients)
    token = await _mint_agent(clients, org_id, "viewer-bot", role="viewer")
    r = await clients.get(f"/orgs/{org_id}/usage/by-tag", headers={"X-Treg-Token": token})
    assert r.status_code == 403


async def test_every_call_returns_a_join_key(clients: AsyncClient, platform_on):
    """X-Treg-Call-Id is what lets a builder line our records up against their own, row for row."""
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    assert r.status_code == 200
    ref = r.headers["X-Treg-Call-Id"]
    assert ref

    await audit.drain()
    got = await clients.get(f"/calls/{ref}")
    assert got.status_code == 200, got.text
    d = got.json()
    assert d["call"]["tags"] == {"customer": "cust_A"}
    assert d["charged_micro"] == EP_MICRO
    # The ledger half is the durable one — it was written synchronously, unlike the audit row.
    assert [e["kind"] for e in d["ledger"]] == ["reserve", "settle"]


async def test_the_join_key_is_scoped_to_the_team(clients: AsyncClient, platform_on):
    """A reference is not a capability: another team holding one must not be able to read the call,
    or one builder could enumerate another's usage by guessing ids."""
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    ref = r.headers["X-Treg-Call-Id"]
    assert (await clients.get(f"/calls/{ref}")).status_code == 200

    other_org = await _make_org("other", "other-team")
    outsider = await _mint("outsider@example.com", other_org, "admin")
    denied = await clients.get(f"/calls/{ref}", headers={"X-Treg-Token": outsider})
    assert denied.status_code == 404


async def test_calls_can_be_windowed_and_paged(clients: AsyncClient):
    await _mk_echo_tool(clients)
    for _ in range(3):
        await clients.get("/call/echo/x", headers={"X-Treg-Meta": "customer=cust_A"})
    await audit.drain()
    rows = (await clients.get("/calls?days=1")).json()
    assert len(rows) >= 3
    older = (await clients.get(f"/calls?before_id={rows[0]['id']}")).json()
    assert all(r["id"] < rows[0]["id"] for r in older)


async def _make_org(name: str, slug: str) -> int:
    async with session_maker() as db:
        org = Org(name=name, slug=slug)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org.id


async def _mint(email: str, org_id: int, role: str) -> str:
    token = crypto.new_token()
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(email=email)
            db.add(user)
            await db.flush()
        db.add(Membership(user_id=user.id, org_id=org_id, role=role,
                          token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


# ---- the separator-collision attack --------------------------------------------------------------
async def test_a_tag_value_cannot_smuggle_the_idempotency_separator(clients: AsyncClient):
    """`_scoped_idempotency_key` joins the primary tag value to the caller's key with \\x1f. If a value
    could contain that separator, `customer="A" + key="B\\x1fC"` would collide with
    `customer="A\\x1fB" + key="C"` — one of a builder's users served another's cached response, which
    is the exact cross-tenant leak the scoping exists to close."""
    await _mk_echo_tool(clients)
    for hostile in ("customer=A\x1fB", "customer=a b", "customer=a/b", "customer=a,b", "customer=<script>"):
        r = await clients.get("/call/echo/x", headers={"X-Treg-Meta": hostile})
        assert r.status_code == 422, f"{hostile!r} must be refused, got {r.status_code}"


async def test_ordinary_customer_ids_still_pass(clients: AsyncClient):
    """The allowlist has to admit what builders actually use: prefixed ids, uuids, namespaced keys."""
    await _mk_echo_tool(clients)
    for good in ("cust_8123", "ws-9", "acct.42", "tenant:eu-west",
                 "6f1c2b4e-8a9d-4c31-9f77-1a2b3c4d5e6f"):
        r = await clients.get("/call/echo/x", headers={"X-Treg-Meta": f"customer={good}"})
        assert r.status_code == 200, f"{good!r} should be accepted: {r.text}"


async def test_the_collision_is_closed_end_to_end(clients: AsyncClient, platform_on):
    """The attack, driven the way an attacker would: one user tries to reach another's stored answer
    by crafting the key, and is refused at the door."""
    crafted = await clients.get(
        f"/call/{EP}?aweme_id=7",
        headers={"X-Treg-Meta": "customer=A\x1fB", "Idempotency-Key": "C"})
    assert crafted.status_code == 422


async def test_a_pin_cannot_smuggle_what_the_header_cannot(clients: AsyncClient):
    """`pinned_tags` arrives as JSON on the mint endpoint and never passes the header parser, so it
    was a second door onto the same storage keys. Both doors must enforce one rule."""
    org_id = await _org_id(clients)
    for hostile in ({"customer": "A\x1fB"}, {"bad key!": "x"}, {"customer": "a b"},
                    {"customer": "jane@example.com"}):
        r = await clients.post(f"/orgs/{org_id}/agents",
                               json={"name": "bot", "pinned_tags": hostile})
        assert r.status_code == 422, f"{hostile!r} must be refused, got {r.status_code}"
    ok = await clients.post(f"/orgs/{org_id}/agents",
                            json={"name": "bot", "pinned_tags": {"customer": "cust_A"}})
    assert ok.status_code == 200, ok.text


# ---- the invariants an invoice actually rests on -------------------------------------------------
async def test_usage_survives_a_dead_audit_pipeline(clients: AsyncClient, platform_on, monkeypatch):
    """THE test that proves an invoice never depends on a lossy table. `audit._schedule` sheds rows
    past its queue bound and swallows every exception — precisely under the load a successful builder
    generates. With the audit pipeline entirely dead, the money must still be complete."""
    monkeypatch.setattr(audit, "_schedule", lambda coro: coro.close())
    org_id = await _org_id(clients)
    for who in ("cust_A", "cust_B"):
        r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": f"customer={who}"})
        assert r.status_code == 200, r.text

    await audit.drain()
    assert (await clients.get("/calls")).json() == [], "the audit table should be empty for this test"

    d = (await clients.get(f"/orgs/{org_id}/usage/by-tag?key=customer")).json()
    assert d["total_micro"] == EP_MICRO * 2, "spend went missing with the audit pipeline down"
    assert d["attributed_micro"] == EP_MICRO * 2
    assert {r["value"] for r in d["rows"]} == {"cust_A", "cust_B"}
    # Counts survive the outage too, because they are read from the ledger's tag rows rather than
    # from the audit table. Taken from CallRecord they were silently zero here — and zero for every
    # non-primary dimension even with the audit pipeline perfectly healthy.
    assert all(r["calls"] == 1 for r in d["rows"])


async def test_an_overrun_settle_still_reconciles(clients: AsyncClient):
    """When the provider's reported cost EXCEEDS the estimate we reserved, settle takes MORE balance.
    The tag totals must follow the money, not the reservation, or the builder under-invoices."""
    org_id = await _org_id(clients)
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, EP, 1_000, tags={"customer": "cust_A"})
        await ledger.settle(db, call_id, 9_000)          # provider charged 9x the estimate
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == 9_000
        org_total = (await ledger.spend_since(db, org_id, _EPOCH))["spend_micro"]
        assert org_total == 9_000
        by_value = await ledger.spend_by_tag(db, org_id, "customer", _EPOCH)
        assert sum(by_value.values()) == org_total, "tag totals drifted from the ledger on an overrun"


async def test_a_reaped_stale_hold_bills_nobody(clients: AsyncClient):
    """A crash between relay and settle strands a hold; the lazy reaper releases it. The tag rows must
    go with it, or a builder invoices their user for a call that never completed."""
    from datetime import timedelta

    from treg.models import Hold

    org_id = await _org_id(clients)
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, EP, 5_000, tags={"customer": "cust_A"})
        hold = await db.get(Hold, call_id)
        hold.created_at = hold.created_at - timedelta(days=2)   # make it stale
        await db.commit()
        await ledger.reap_stale_holds(db, org_id=org_id)
        assert await ledger.tag_spent_since(db, org_id, "customer", "cust_A", _EPOCH) == 0
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == 0
        assert (await ledger.spend_since(db, org_id, _EPOCH))["spend_micro"] == 0


async def test_another_team_cannot_read_budgets_or_usage(clients: AsyncClient, platform_on):
    """Budgets and usage name a builder's own customers. Another team must reach none of it."""
    org_id = await _org_id(clients)
    await _set_budget(clients, org_id, "customer", "cust_A", daily_cap_micro=5_000)
    await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})

    other_org = await _make_org("rival", "rival-team")
    rival = await _mint("rival@example.com", other_org, "owner")
    h = {"X-Treg-Token": rival}

    assert (await clients.get(f"/orgs/{org_id}/budgets", headers=h)).status_code == 403
    assert (await clients.get(f"/orgs/{org_id}/usage/by-tag", headers=h)).status_code == 403
    assert (await clients.get(f"/orgs/{org_id}/settings", headers=h)).status_code == 403
    assert (await clients.put(f"/orgs/{org_id}/budgets/customer/cust_A",
                              headers=h, json={"status": "blocked"})).status_code == 403
    # ...and its own view is empty rather than leaking the neighbour's numbers.
    own = await clients.get(f"/orgs/{other_org}/usage/by-tag", headers=h)
    assert own.status_code == 200 and own.json()["total_micro"] == 0


async def test_a_provider_credential_refusal_is_never_billed(clients: AsyncClient, platform_on,
                                                             monkeypatch):
    """A 4xx that means OUR key was rejected, exhausted or throttled must not be charged, even on a
    per_call endpoint. The provider bills nothing for these, and passing them on would turn an
    expired platform key into real spend on a team's balance — and, for a builder reselling treg,
    onto their own customers' invoices."""
    org_id = await _org_id(clients)
    before = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]

    for status in (401, 402, 403, 407, 408, 429):
        monkeypatch.setattr(A, "relay", _fake_relay(status, b'{"message":"out of credits"}'))
        r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
        assert r.status_code == status, r.text
        assert r.headers.get("X-Treg-Cost-Micro") == "0", f"{status} was billed"

    assert (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"] == before
    async with session_maker() as db:
        assert await ledger.tag_invoice_since(db, org_id, "customer", "cust_A", _EPOCH) == 0


async def test_a_caller_input_4xx_is_still_billed_on_a_per_call_endpoint(clients: AsyncClient,
                                                                        platform_on, monkeypatch):
    """The other half of the rule stays: the provider DOES charge for accepting a malformed request,
    so a caller's own bad input is on the caller."""
    org_id = await _org_id(clients)
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"bad param"}'))
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=cust_A"})
    assert r.status_code == 400
    # tikhub comments is per_success, so 400 releases; assert the RULE directly for per_call.
    assert A._platform_billable(400, "per_call") is True
    assert A._platform_billable(404, "per_call") is True
    assert A._platform_billable(402, "per_call") is False
    assert A._platform_billable(401, "per_result") is False


# ---- per-dimension defaults with overrides -------------------------------------------------------
async def test_unlimited_until_a_default_is_set(clients: AsyncClient, platform_on):
    """The shipped state: no default, no override, no limit. A team that never opens this page keeps
    behaving exactly as before."""
    org_id = await _org_id(clients)
    for _ in range(3):
        r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=anyone"})
        assert r.status_code == 200, r.text


async def test_a_default_applies_to_every_value_without_an_override(clients: AsyncClient, platform_on):
    """One setting covers a customer base of any size — the whole point. A builder with 10k customers
    cannot write 10k rows."""
    org_id = await _org_id(clients)
    r = await clients.put(f"/orgs/{org_id}/budgets/customer", json={"daily_cap_micro": EP_MICRO})
    assert r.status_code == 200 and r.json()["is_default"], r.text

    for who in ("alice", "bob"):                      # neither has ever been seen before
        ok = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": f"customer={who}"})
        assert ok.status_code == 200, ok.text
        capped = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": f"customer={who}"})
        assert capped.status_code == 429, f"{who} should have hit the default"
        assert capped.json()["detail"]["dim"] == "customer"


async def test_an_override_beats_the_default_in_both_directions(clients: AsyncClient, platform_on):
    """A VIP gets more, a suspect gets less, everyone else keeps the default."""
    org_id = await _org_id(clients)
    await clients.put(f"/orgs/{org_id}/budgets/customer", json={"daily_cap_micro": EP_MICRO})
    await clients.put(f"/orgs/{org_id}/budgets/customer/vip", json={"daily_cap_micro": EP_MICRO * 10})
    await clients.put(f"/orgs/{org_id}/budgets/customer/tight", json={"daily_cap_micro": 1})

    for _ in range(3):                                 # vip sails past the default
        assert (await clients.get(f"/call/{EP}?aweme_id=7",
                                  headers={"X-Treg-Meta": "customer=vip"})).status_code == 200
    tight = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=tight"})
    assert tight.status_code == 429


async def test_being_seen_does_not_create_an_override(clients: AsyncClient, platform_on):
    """THE interaction that would have broken defaults: a registry row is written for every distinct
    value to keep the cardinality check cheap. Counted as an override it would mean the default never
    applied to anybody who had ever called."""
    org_id = await _org_id(clients)
    assert (await clients.get(f"/call/{EP}?aweme_id=7",
                              headers={"X-Treg-Meta": "customer=seen_first"})).status_code == 200
    await clients.put(f"/orgs/{org_id}/budgets/customer", json={"daily_cap_micro": 1})
    after = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "customer=seen_first"})
    assert after.status_code == 429, "the default must reach a value that was already registered"


async def test_the_budget_list_shows_policy_not_bookkeeping(clients: AsyncClient, platform_on):
    """Registry rows are hidden: a table of eight rows in which six limited nothing was presenting
    bookkeeping as policy."""
    org_id = await _org_id(clients)
    for who in ("a", "b", "c"):                        # three registry rows, no limits
        await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": f"customer={who}"})
    assert (await clients.get(f"/orgs/{org_id}/budgets")).json() == []

    await clients.put(f"/orgs/{org_id}/budgets/customer", json={"daily_cap_micro": 5_000})
    await clients.put(f"/orgs/{org_id}/budgets/customer/b", json={"daily_cap_micro": 9_000})
    rows = (await clients.get(f"/orgs/{org_id}/budgets")).json()
    assert [(r["val"], r["is_default"]) for r in rows] == [("*", True), ("b", False)], rows


async def test_tag_keys_reports_what_was_sent_not_only_what_is_budgetable(clients: AsyncClient,
                                                                          platform_on):
    """Reporting works on any key; enforcement only on a declared one. The dashboard picker needs the
    first list, and was being given the second."""
    org_id = await _org_id(clients)
    await clients.get(f"/call/{EP}?aweme_id=7",
                      headers={"X-Treg-Meta": "customer=c1, feature=email-finder"})
    d = (await clients.get(f"/orgs/{org_id}/tag-keys")).json()
    assert "feature" in d["seen"] and "customer" in d["seen"]
    assert d["budgetable"] == ["customer"] and "feature" not in d["budgetable"]


async def test_setting_a_limit_declares_the_dimension(clients: AsyncClient, platform_on):
    """Setting a limit IS the declaration. Requiring a separate settings PATCH first made the common
    path a hidden two-step: the tag is visible in usage, so a person expects to be able to cap it."""
    org_id = await _org_id(clients)
    assert (await clients.get(f"/orgs/{org_id}/settings")).json()["budget_dims"] == ["customer"]

    r = await clients.put(f"/orgs/{org_id}/budgets/workspace/ws_9", json={"daily_cap_micro": EP_MICRO})
    assert r.status_code == 200, r.text
    assert (await clients.get(f"/orgs/{org_id}/settings")).json()["budget_dims"] == \
        ["customer", "workspace"]

    # ...and it now actually enforces, which is the whole point of declaring it.
    ok = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "workspace=ws_9"})
    assert ok.status_code == 200, ok.text
    capped = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Meta": "workspace=ws_9"})
    assert capped.status_code == 429 and capped.json()["detail"]["dim"] == "workspace"


async def test_the_dimension_bound_still_holds(clients: AsyncClient):
    """The cap is what keeps the call path cheap — each dimension is another indexed lookup per call.
    Past it, refuse and name the ones in use, because only the team knows which to give up."""
    org_id = await _org_id(clients)
    for dim in ("customer", "workspace", "project"):
        assert (await clients.put(f"/orgs/{org_id}/budgets/{dim}/x",
                                  json={"daily_cap_micro": 5_000})).status_code == 200
    r = await clients.put(f"/orgs/{org_id}/budgets/cohort/x", json={"daily_cap_micro": 5_000})
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["error"] == "too_many_budget_dimensions" and d["limit"] == 3
    assert sorted(d["declared"]) == ["customer", "project", "workspace"]


async def test_the_integration_skill_is_served_and_templated(clients: AsyncClient):
    """`/integrate.md` is a front door: a builder pastes it into their repo and points a coding agent
    at it, so a stale `{BASE}` or a 404 breaks an integration before it starts."""
    r = await clients.get("/integrate.md")
    assert r.status_code == 200, r.text
    body = r.text
    assert "{BASE}" not in body, "the serving host must be templated in"
    # The load-bearing claims — if any of these names drift, the skill teaches an integration that
    # does not work, and nothing else in the suite would notice.
    for must in ("X-Treg-Meta", "X-Treg-Call-Id", "X-Treg-Cost-Micro", "X-Treg-Error",
                 "usage/by-tag", "/budgets/", "attributed", "unattributed",
                 "Idempotency-Key", "--pin"):
        assert must in body, f"integrate.md no longer mentions {must!r}"
