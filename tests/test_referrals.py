"""The referral program — a door that hands out money to strangers, so these tests are mostly about
the ways it can be made to hand out money it shouldn't.

Covered: the whole loop (link → cookie → signup → top-up → hold → both grants); that NOTHING is
granted before the hold elapses and exactly one grant lands after, however many times the sweep
runs; every abuse gate (self-referral, a signup with no top-up, a top-up below the minimum, a second
top-up, one card claiming twice, the lifetime cap) — and that a FREE-TIER referrer is still
paid, which is the gate we deliberately removed; the dispute
clawback and its deliberate limit; and that one user can never see another's referrals — the single
failure here that leaks data rather than money.

Stripe is faked exactly as in test_billing.py: `billing._sdk` is the one funnel, and webhook payloads
are signed with the real HMAC scheme so the signature path is exercised rather than stubbed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg import billing, ledger, referrals
from treg.api import app
from treg.config import get_settings
from treg.db import reset_db, session_maker
from treg.models import CreditBlock, Org, Referral, User

WHSEC = "whsec_referral_suite"


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(s, "stripe_webhook_secret", WHSEC, raising=False)
    billing._locks.clear()
    billing._scheduled.clear()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


# ---- helpers -----------------------------------------------------------------------------------
def _ref_cookie(code: str) -> dict:
    """The parked referral code, as a request header. httpx deprecates per-request `cookies=`, and a
    raw Cookie header is also closer to what a browser actually sends."""
    return {"Cookie": f"{referrals_cookie()}={code}"}


async def _signup(c: AsyncClient, email: str, *, ref: str = "") -> tuple[int, str]:
    """Register a user (and their first team), optionally carrying a referral cookie."""
    r = await c.post("/users", json={"email": email}, headers=_ref_cookie(ref) if ref else None)
    assert r.status_code == 200, r.text
    return r.json()["org_id"], r.json()["token"]


def referrals_cookie() -> str:
    from treg.api import REFERRAL_COOKIE
    return REFERRAL_COOKIE


def _sign(payload: bytes) -> str:
    t = int(time.time())
    mac = hmac.new(WHSEC.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


async def _deliver(c: AsyncClient, event: dict):
    payload = json.dumps(event).encode()
    return await c.post("/billing/stripe/webhook", content=payload,
                        headers={"stripe-signature": _sign(payload), "content-type": "application/json"})


def _checkout_event(org_id: int, *, pi: str, cents: int = 1000) -> dict:
    """A `checkout.session.completed` — the door a FIRST top-up always comes through (auto-top-up
    needs a card saved by an earlier manual one), and the only one that carries a fingerprint."""
    return {"id": f"evt_{pi}", "type": "checkout.session.completed", "data": {"object": {
        "id": f"cs_{pi}", "object": "checkout.session", "mode": "payment", "payment_status": "paid",
        "amount_total": cents, "currency": "usd", "payment_intent": pi,
        "metadata": {"treg_org_id": str(org_id), "treg_kind": "topup"}}}}


def _fake_sdk(fingerprint: str | None = "fp_card_A"):
    """Stands in for every Stripe SDK call. The PaymentIntent retrieve is the only one that matters
    here: `_pm_and_fingerprint` expands `payment_method`, so the fake returns the expanded shape."""
    async def sdk(fn, **kw):
        return {"id": kw.get("id", "pi_x"), "object": "payment_intent",
                "payment_method": {"id": "pm_1", "object": "payment_method",
                                   "card": {"fingerprint": fingerprint} if fingerprint else {}}}
    return sdk


async def _topup(c: AsyncClient, monkeypatch, org_id: int, *, pi: str, cents: int = 1000,
                 fingerprint: str | None = "fp_card_A"):
    monkeypatch.setattr(billing, "_sdk", _fake_sdk(fingerprint))
    r = await _deliver(c, _checkout_event(org_id, pi=pi, cents=cents))
    assert r.status_code == 200, r.text
    return r


async def _referral_rows() -> list[Referral]:
    async with session_maker() as db:
        return list((await db.execute(select(Referral))).scalars().all())


async def _age_qualification(days: int = 30) -> None:
    """Backdate every qualified referral so its hold has elapsed. There is no clock to advance and
    no scheduler to run — the hold is `qualified_at` plus a config window, so this is the whole of
    'wait a week'."""
    async with session_maker() as db:
        for row in (await db.execute(select(Referral))).scalars().all():
            if row.qualified_at:
                row.qualified_at = row.qualified_at - timedelta(days=days)
                db.add(row)
        await db.commit()


async def _blocks(org_id: int, kind: str) -> list[CreditBlock]:
    async with session_maker() as db:
        return list((await db.execute(
            select(CreditBlock).where(CreditBlock.org_id == org_id, CreditBlock.kind == kind)
        )).scalars().all())


async def _ready_referrer(c: AsyncClient, monkeypatch, email="ann@superdesign.dev") -> tuple[int, str, str]:
    """A referrer holding a code. Also tops up — not because referring requires it (it does not,
    see test_a_free_tier_referrer_still_earns), but so these tests exercise the ordinary case where
    the referrer is also a paying team."""
    org_id, token = await _signup(c, email)
    await _topup(c, monkeypatch, org_id, pi="pi_referrer_seed", cents=2000, fingerprint="fp_referrer")
    r = await c.post("/referrals/code", headers=_h(token))
    assert r.status_code == 200, r.text
    return org_id, token, r.json()["code"]


# ---- the link ----------------------------------------------------------------------------------
async def test_ref_link_serves_the_landing_and_parks_the_code(c):
    """`/?ref=CODE` must show the PITCH. It used to fall through to the SPA, because the landing
    route treats any query string as the dashboard's — which would send a stranger who clicked a
    friend's link to an empty app shell."""
    r = await c.get("/?ref=ann-ab3cd")
    assert r.status_code == 200
    assert "<!doctype html" in r.text[:200].lower() or "<html" in r.text[:200].lower()
    assert r.cookies.get(referrals_cookie()) == "ann-ab3cd"


async def test_junk_ref_is_dropped_not_stored(c):
    """The code reaches a database query, so it is validated on the way in AND on the way out."""
    r = await c.get("/?ref=" + "../../etc/passwd")
    assert r.status_code == 200
    assert r.cookies.get(referrals_cookie()) is None


async def test_other_query_params_still_belong_to_the_spa(c):
    """A referral code must not hijack an invite link or an OAuth return."""
    r = await c.get("/?ref=ann-ab3cd&invite=xyz")
    assert r.status_code == 200
    assert r.cookies.get(referrals_cookie()) is None


async def test_code_is_minted_once_and_is_stable(c, monkeypatch):
    _, token = await _signup(c, "ann@superdesign.dev")
    first = (await c.post("/referrals/code", headers=_h(token))).json()["code"]
    second = (await c.post("/referrals/code", headers=_h(token))).json()["code"]
    assert first == second
    assert referrals.normalize_code(first) == first
    assert first.startswith("ann-")


# ---- the happy path ----------------------------------------------------------------------------
async def test_full_loop_pays_both_sides_after_the_hold(c, monkeypatch):
    ann_org, ann_token, code = await _ready_referrer(c, monkeypatch)
    bob_org, bob_token = await _signup(c, "bob@example.com", ref=code)

    rows = await _referral_rows()
    assert [r.status for r in rows] == ["pending"], "signup alone must owe nothing"

    await _topup(c, monkeypatch, bob_org, pi="pi_bob_1", cents=1000, fingerprint="fp_bob")
    rows = await _referral_rows()
    assert rows[0].status == "qualified"
    s = get_settings()
    # The referee is paid AT ONCE — the balance is their only feedback. The referrer waits out the
    # hold, which is now the only clawback window that remains.
    assert [b.amount_micro for b in await _blocks(bob_org, "referral")] == [s.referral_referred_micro]
    assert await _blocks(ann_org, "referral") == []

    await _age_qualification()
    r = await c.get("/referrals", headers=_h(ann_token))
    assert r.status_code == 200
    body = r.json()

    bob_blocks = await _blocks(bob_org, "referral")
    ann_blocks = await _blocks(ann_org, "referral")
    assert [b.amount_micro for b in bob_blocks] == [s.referral_referred_micro]
    assert [b.amount_micro for b in ann_blocks] == [s.referral_referrer_micro]
    assert body["totals"]["earned_micro"] == s.referral_referrer_micro
    assert body["referrals"][0]["status"] == "paid"
    assert body["referrals"][0]["email"] == "bob@example.com"


async def test_sweeping_twice_pays_once(c, monkeypatch):
    """The sweep runs from every top-up AND every page load, so it is called far more often than
    there is work. Paying twice would be the most expensive bug in the feature."""
    ann_org, ann_token, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_1", fingerprint="fp_bob")
    await _age_qualification()

    async with session_maker() as db:
        assert await referrals.sweep(db) == 1
        assert await referrals.sweep(db) == 0
        assert await referrals.sweep(db) == 0
    await c.get("/referrals", headers=_h(ann_token))

    assert len(await _blocks(ann_org, "referral")) == 1
    assert len(await _blocks(bob_org, "referral")) == 1


async def test_referral_credit_burns_before_purchased(c, monkeypatch):
    """Referral credit is a marketing expense we can never be asked to return; purchased credit is a
    refundable liability. Spending ours first is what keeps the disputable pool small — and an
    unrecognised block kind would sort LAST, i.e. exactly backwards."""
    ann_org, ann_token, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_1", fingerprint="fp_bob")
    await _age_qualification()
    # Spend $2: more than the $1 signup promo (which also sorts at 0 and is older, so it goes
    # first), and enough to bite into the referral block — while staying well under the $10
    # purchased block, which must not be touched at all.
    async with session_maker() as db:
        await referrals.sweep(db)
        call_id = await ledger.reserve(db, bob_org, "e/1", 2_000_000)
        await ledger.settle(db, call_id, 2_000_000)
        blocks = {b.kind: b for b in (await db.execute(
            select(CreditBlock).where(CreditBlock.org_id == bob_org))).scalars().all()}
    assert blocks["promotional"].remaining_micro == 0, "the older zero-rank block goes first"
    assert blocks["referral"].remaining_micro < blocks["referral"].amount_micro, "referral spent next"
    assert blocks["purchased"].remaining_micro == blocks["purchased"].amount_micro, "purchased untouched"


# ---- the abuse gates ---------------------------------------------------------------------------
async def test_self_referral_is_never_attributed(c, monkeypatch):
    """The cheapest attack and the most common attempt: refer yourself with a second team."""
    _, ann_token, code = await _ready_referrer(c, monkeypatch)
    r = await c.post("/orgs", json={"name": "Ann Second Team"},
                     headers={**_h(ann_token), **_ref_cookie(code)})
    assert r.status_code == 200, r.text
    assert await _referral_rows() == []


async def test_a_short_topup_leaves_the_referral_alive(c, monkeypatch):
    """$5 is the FIRST preset on the billing page. Treating a short payment as a refusal meant the
    most obvious button silently destroyed the reward forever — punishing the exact person we are
    trying to convert, and removing their reason to add the rest. It stays pending."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, bob_token = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_small", cents=500, fingerprint="fp_bob")
    rows = await _referral_rows()
    assert rows[0].status == "pending" and rows[0].reject_reason == ""
    # ...and the offer is still on screen, now asking only for the remainder.
    offer = (await c.get("/billing", headers=_h(bob_token))).json()["referral_offer"]
    assert offer["topped_up_micro"] == 5_000_000
    assert offer["remaining_micro"] == get_settings().referral_min_topup_micro - 5_000_000


async def test_the_threshold_is_cumulative(c, monkeypatch):
    """$5 twice is the same $10 as $10 once. The money still has to arrive, so this costs nothing in
    abuse terms — it only stops us punishing someone for paying in two goes."""
    ann_org, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, bob_token = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_1", cents=500, fingerprint="fp_bob")
    assert (await _referral_rows())[0].status == "pending"
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_2", cents=500, fingerprint="fp_bob")
    assert (await _referral_rows())[0].status == "qualified"
    # The offer is gone (the referee's bonus is already in their balance), and the referrer's lands.
    assert (await c.get("/billing", headers=_h(bob_token))).json()["referral_offer"] is None
    await _age_qualification()
    async with session_maker() as db:
        assert await referrals.sweep(db) == 1
    s = get_settings()
    assert [b.amount_micro for b in await _blocks(ann_org, "referral")] == [s.referral_referrer_micro]


async def test_one_card_can_only_ever_claim_once(c, monkeypatch):
    """An email address is free; a card is not. The fingerprint is the signal that survives a farm
    of fresh addresses, so it is the gate that has to hold."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    cat_org, _ = await _signup(c, "cat@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_same_card")
    await _topup(c, monkeypatch, cat_org, pi="pi_cat", fingerprint="fp_same_card")
    rows = sorted(await _referral_rows(), key=lambda r: r.id)
    assert rows[0].status == "qualified"
    assert rows[1].status == "rejected" and rows[1].reject_reason == "card_already_used"


async def test_a_free_tier_referrer_still_earns(c, monkeypatch):
    """A referrer who has NEVER topped up is paid in full — deliberately.

    There used to be a gate requiring the referrer to have paid us once. It was removed: a top-up is
    not a cost to a self-dealer (it converts into credit they keep), so requiring one of the referrer
    too only added a step that returns its own money — about one extra card per twenty referrals.
    Against that it hid the link from every free-tier user, who on a product pitched as "$1.00 free,
    no card" are most users and the likeliest to tell a friend. The card fingerprint is the gate with
    teeth; this one cost ~90% of legitimate referrers to tax a farm ~5%."""
    ann_org, token = await _signup(c, "ann@superdesign.dev")
    code = (await c.post("/referrals/code", headers=_h(token))).json()["code"]
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")
    rows = await _referral_rows()
    assert rows[0].status == "qualified", "a free-tier referrer must still qualify"
    await _age_qualification()
    async with session_maker() as db:
        await referrals.sweep(db)
    s = get_settings()
    assert [b.amount_micro for b in await _blocks(ann_org, "referral")] == [s.referral_referrer_micro]


async def test_a_later_topup_cannot_earn_a_second_bounty(c, monkeypatch):
    """`pending` is the once-only guard: `qualify` only ever selects a pending row, so once a
    referral has been taken no further payment can re-earn it. That is what replaced the old
    "must be the first purchase" rule, which existed for this and cost us the short-payment case."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_1", fingerprint="fp_bob")
    await _topup(c, monkeypatch, bob_org, pi="pi_bob_2", cents=5000, fingerprint="fp_bob")
    rows = await _referral_rows()
    assert len(rows) == 1 and rows[0].status == "qualified"
    assert rows[0].qualifying_payment_intent == "pi_bob_1", "the first crossing is the one recorded"


async def test_an_org_can_only_be_referred_once(c, monkeypatch):
    """The unique constraint, not `grant(once=True)`, is what arbitrates — so a second attribution
    attempt against the same org must lose."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    _, _, dee_code = await _ready_referrer(c, monkeypatch, email="dee@superdesign.dev")
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    async with session_maker() as db:
        bob = (await db.execute(select(User).where(User.email == "bob@example.com"))).scalars().one()
        again = await referrals.attribute(
            db, user=bob, org=await db.get(Org, bob_org), code=dee_code)
    assert again is None, "the first code keeps the org"
    rows = await _referral_rows()
    assert len(rows) == 1 and rows[0].code == code


async def test_lifetime_cap_records_but_does_not_pay(c, monkeypatch):
    """At the cap the answer is `capped`, not `rejected`: this person referred someone real and
    simply ran out of self-serve allowance. That distinction is what the support reply rests on."""
    monkeypatch.setattr(get_settings(), "referral_cap", 1, raising=False)
    _, ann_token, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    cat_org, _ = await _signup(c, "cat@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")
    await _topup(c, monkeypatch, cat_org, pi="pi_cat", fingerprint="fp_cat")
    rows = sorted(await _referral_rows(), key=lambda r: r.id)
    assert rows[0].status == "qualified"
    assert rows[1].status == "capped" and rows[1].reject_reason == "referrer_at_cap"
    await _age_qualification()
    async with session_maker() as db:
        await referrals.sweep(db)
    assert await _blocks(cat_org, "referral") == [], "a capped referral pays nobody"


# ---- clawback ----------------------------------------------------------------------------------
async def test_dispute_inside_the_hold_cancels_the_referrers_half(c, monkeypatch):
    """The hold now protects ONE side. The referee's bonus went out on qualification and is treated
    like any already-paid credit — logged for a human, never reversed, because no code path here may
    drive a balance negative. Recovering half a bounty is the deliberate price of paying the referee
    immediately, and it is why the per-card gate matters more than this window does."""
    ann_org, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")

    r = await _deliver(c, {"id": "evt_d1", "type": "charge.dispute.created", "data": {"object": {
        "id": "dp_1", "object": "dispute", "payment_intent": "pi_bob"}}})
    assert r.status_code == 200 and r.json()["referrals_cancelled"] == 1

    await _age_qualification()
    async with session_maker() as db:
        assert await referrals.sweep(db) == 0
    assert await _blocks(ann_org, "referral") == [], "the referrer's half never goes out"
    s = get_settings()
    assert [b.amount_micro for b in await _blocks(bob_org, "referral")] == [s.referral_referred_micro]


async def test_dispute_does_not_touch_the_balance(c, monkeypatch):
    """The clawback cancels a BONUS, never a top-up. treg has no path that drives a balance
    negative, and refunding a payment is a human decision — that boundary is deliberate."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, bob_token = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")
    async with session_maker() as db:
        before = await ledger.balance_of(db, bob_org)
    await _deliver(c, {"id": "evt_d1", "type": "charge.refunded", "data": {"object": {
        "id": "ch_1", "object": "charge", "payment_intent": "pi_bob"}}})
    async with session_maker() as db:
        assert await ledger.balance_of(db, bob_org) == before


# ---- the read model ----------------------------------------------------------------------------
async def test_a_user_never_sees_another_users_referrals(c, monkeypatch):
    """This response carries the referred person's email address, so a scoping mistake leaks another
    user's data rather than merely miscounting. The one bug here worse than losing money."""
    _, ann_token, code = await _ready_referrer(c, monkeypatch)
    _, dee_token, _ = await _ready_referrer(c, monkeypatch, email="dee@superdesign.dev")
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")

    ann = (await c.get("/referrals", headers=_h(ann_token))).json()
    dee = (await c.get("/referrals", headers=_h(dee_token))).json()
    assert [r["email"] for r in ann["referrals"]] == ["bob@example.com"]
    assert dee["referrals"] == []
    assert dee["totals"]["signed_up"] == 0


async def test_referrals_page_requires_a_signed_in_person(c):
    assert (await c.get("/referrals")).status_code == 401
    assert (await c.post("/referrals/code")).status_code == 401


async def test_a_brand_new_account_gets_a_working_link_immediately(c, monkeypatch):
    """No payment, no waiting: someone who signed up a minute ago can share their link. This is the
    whole point of removing the top-up gate — the free-tier user IS the referrer we want."""
    _, token = await _signup(c, "ann@superdesign.dev")
    body = (await c.get("/referrals", headers=_h(token))).json()
    assert body["eligible"] is True
    assert body["link"].endswith("?ref=" + body["code"]) and body["code"]
    assert body["credit_org"] is not None, "the reward needs somewhere to land"


async def test_admin_report_totals_pending_before_it_is_spent(c, monkeypatch):
    """`pending_payout_micro` is what the sweep will grant once holds elapse — the number that says
    whether the program is affordable BEFORE the money leaves."""
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "adm_referrals")
    get_settings.cache_clear()
    # cache_clear() builds a NEW Settings, so the fixture's Stripe patches (applied to the old
    # instance) are gone with it — and every webhook below would 400 on the signature check.
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(s, "stripe_webhook_secret", WHSEC, raising=False)
    try:
        _, _, code = await _ready_referrer(c, monkeypatch)
        bob_org, _ = await _signup(c, "bob@example.com", ref=code)
        await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")
        r = await c.get("/admin/referrals", headers={"X-Treg-Token": "adm_referrals"})
        assert r.status_code == 200, r.text
        s = get_settings()
        assert r.json()["counts"] == {"qualified": 1}
        assert r.json()["pending_payout_micro"] == s.referral_referrer_micro + s.referral_referred_micro
        assert r.json()["paid_micro"] == 0
    finally:
        get_settings.cache_clear()


async def test_admin_report_is_superadmin_only(c, monkeypatch):
    _, token, _ = await _ready_referrer(c, monkeypatch)
    assert (await c.get("/admin/referrals", headers=_h(token))).status_code == 403


# ---- the referee's side: telling them the bonus exists -----------------------------------------
async def test_a_referred_team_is_told_the_minimum_on_its_billing_page(c, monkeypatch):
    """The referee does not know a bonus exists, and the FIRST top-up preset ($5) is below the
    minimum — so without this the most-clicked button silently forfeits the reward."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    _, bob_token = await _signup(c, "bob@example.com", ref=code)
    offer = (await c.get("/billing", headers=_h(bob_token))).json()["referral_offer"]
    s = get_settings()
    assert offer == {"referred_micro": s.referral_referred_micro,
                     "referrer_micro": s.referral_referrer_micro,
                     "min_topup_micro": s.referral_min_topup_micro,
                     "topped_up_micro": 0,
                     "remaining_micro": s.referral_min_topup_micro,
                     "hold_days": s.referral_hold_days,
                     "referrer_masked": "a•••@superdesign.dev"}


async def test_a_team_that_arrived_on_its_own_sees_no_offer(c, monkeypatch):
    """Null, not a zeroed object: the billing page must look exactly as it did before this shipped
    for the teams that were never referred."""
    _, token = await _signup(c, "solo@example.com")
    assert (await c.get("/billing", headers=_h(token))).json()["referral_offer"] is None


async def test_the_referee_is_paid_the_instant_they_qualify(c, monkeypatch):
    """The two sides are not in the same position. The referrer has a Referrals page where a pending
    reward is legible; the referee has none, so the BALANCE is their only feedback and a bonus that
    is merely "coming" is indistinguishable from one that never happened. Reported exactly that way.
    """
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, bob_token = await _signup(c, "bob@example.com", ref=code)
    before = (await c.get("/billing", headers=_h(bob_token))).json()["balance_micro"]
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", cents=1000, fingerprint="fp_bob")
    s = get_settings()
    after = (await c.get("/billing", headers=_h(bob_token))).json()["balance_micro"]
    assert after == before + 10_000_000 + s.referral_referred_micro, "top-up AND bonus, immediately"
    assert [b.amount_micro for b in await _blocks(bob_org, "referral")] == [s.referral_referred_micro]


async def test_the_referrer_still_waits_out_the_hold(c, monkeypatch):
    """Only the referee's half goes early. Nothing about the referrer's experience needs it sooner,
    and the hold is the only clawback window there is."""
    ann_org, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")
    assert await _blocks(ann_org, "referral") == [], "the referrer is not paid on qualification"
    await _age_qualification()
    async with session_maker() as db:
        assert await referrals.sweep(db) == 1
    s = get_settings()
    assert [b.amount_micro for b in await _blocks(ann_org, "referral")] == [s.referral_referrer_micro]


async def test_the_sweep_does_not_pay_the_referee_a_second_time(c, monkeypatch):
    """`_pay` still has a referee branch as a fallback for a failed instant grant, so it must be
    guarded on `referred_block_id` — unguarded it would hand out a second bonus a week later."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, _ = await _signup(c, "bob@example.com", ref=code)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", fingerprint="fp_bob")
    await _age_qualification()
    async with session_maker() as db:
        await referrals.sweep(db)
    s = get_settings()
    assert [b.amount_micro for b in await _blocks(bob_org, "referral")] == [s.referral_referred_micro]


async def test_the_offer_says_when_each_side_is_paid(c, monkeypatch):
    """"We'll add $5" with no timing is what made a correct payout look like a failure."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    _, bob_token = await _signup(c, "bob@example.com", ref=code)
    offer = (await c.get("/billing", headers=_h(bob_token))).json()["referral_offer"]
    assert offer["hold_days"] == get_settings().referral_hold_days


async def test_the_advertised_minimum_is_the_one_qualify_enforces(c, monkeypatch):
    """The offer and the gate read the same setting. If they ever diverge we would be promising a
    bonus at an amount that does not earn one — the worst possible bug on this screen."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    bob_org, bob_token = await _signup(c, "bob@example.com", ref=code)
    offer = (await c.get("/billing", headers=_h(bob_token))).json()["referral_offer"]
    cents = int(offer["min_topup_micro"] // 10_000)
    await _topup(c, monkeypatch, bob_org, pi="pi_bob", cents=cents, fingerprint="fp_bob")
    rows = await _referral_rows()
    assert rows[0].status == "qualified", "the advertised minimum must actually qualify"


def test_the_referrer_is_named_but_never_in_full():
    """A referral link is PUBLIC: an influencer posts theirs, and printing the full address would
    publish their email to every stranger who signs up through it — a harvestable list at exactly the
    volume this program is built to produce. The domain survives because that is what makes a real
    friend recognisable; the local part collapses to one character plus a FIXED bullet run, so the
    mask does not leak its own length."""
    assert referrals.mask_email("jason@superdesign.dev") == "j•••@superdesign.dev"
    assert referrals.mask_email("jz@superdesign.dev") == "j•••@superdesign.dev", "length must not leak"
    assert referrals.mask_email("notanemail") == ""
    assert referrals.mask_email("") == ""


async def test_the_referee_never_receives_the_referrers_real_address(c, monkeypatch):
    """The whole point of the mask. Asserted on the wire, not on the helper, because the leak that
    matters is the one that ships in a response body."""
    _, _, code = await _ready_referrer(c, monkeypatch)
    _, bob_token = await _signup(c, "bob@example.com", ref=code)
    body = (await c.get("/billing", headers=_h(bob_token))).text
    assert "ann@superdesign.dev" not in body
    assert "a•••@superdesign.dev" in body
