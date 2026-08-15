"""Stripe top-ups — the paid door into the ledger, so these tests guard the ways it could take or
lose someone's money.

Covered: who may reach the endpoints; the micro-USD ↔ cents conversion at the Stripe boundary and the
amount gate in front of it; webhook signature rejection; a redelivered webhook crediting exactly once
(Stripe delivers at least once and `stripe events resend` exists); and every auto-top-up guard —
consent, cooldown, monthly cap, consecutive failures, and the `authentication_required` decline that
cannot be retried off-session and must disable itself instead.

No network: `billing._sdk` is the single funnel every Stripe SDK call goes through, so patching it
intercepts all of them. The webhook tests sign their payloads with the real HMAC scheme against a
test secret, so the signature check is exercised for real rather than stubbed out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import stripe
from httpx import ASGITransport, AsyncClient

from conftest import make_upstream

from treg import billing, ledger
from treg.api import app
from treg.config import get_settings
from treg.db import reset_db, session_maker
from treg.models import Org

WHSEC = "whsec_test_secret_for_the_suite"


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    # A key makes billing "configured"; nothing ever reaches Stripe because _sdk is patched per-test.
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(s, "stripe_webhook_secret", WHSEC, raising=False)
    billing._locks.clear()
    billing._scheduled.clear()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


async def _org(c: AsyncClient, email: str = "billing@superdesign.dev") -> tuple[int, str]:
    r = await c.post("/users", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()["org_id"], r.json()["token"]


async def _member(c: AsyncClient, org_id: int, admin_token: str, email: str, role: str = "member") -> str:
    """Add a second person to the org and return THEIR token (via the invite → accept path)."""
    r = await c.post(f"/orgs/{org_id}/invites", json={"email": email, "role": role}, headers=_h(admin_token))
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    r = await c.post("/invites/accept", json={"code": code, "email": email})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _sign(payload: bytes, secret: str = WHSEC, *, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


async def _deliver(c: AsyncClient, event: dict, *, secret: str = WHSEC) -> object:
    payload = json.dumps(event).encode()
    return await c.post("/billing/stripe/webhook", content=payload,
                        headers={"stripe-signature": _sign(payload, secret),
                                 "content-type": "application/json"})


def _pi_event(org_id: int, *, pi: str = "pi_test_1", cents: int = 1000, auto: str = "0",
              kind: str = "payment_intent.succeeded", **extra) -> dict:
    return {"id": f"evt_{pi}", "type": kind, "data": {"object": {
        "id": pi, "object": "payment_intent", "amount": cents, "amount_received": cents,
        "currency": "usd", "status": "succeeded", "payment_method": "pm_card_visa",
        "metadata": {"treg_org_id": str(org_id), "treg_kind": "topup", "treg_auto": auto},
        **extra}}}


async def _set_org(org_id: int, **fields) -> None:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        for k, v in fields.items():
            setattr(org, k, v)
        db.add(org)
        await db.commit()


# ---- units: the ONE place micro-USD meets cents ------------------------------------------------
def test_micro_and_cents_convert_exactly():
    assert billing.micro_to_cents(10_000_000) == 1_000      # $10 → 1000 cents
    assert billing.cents_to_micro(1_000) == 10_000_000
    assert billing.usd_to_micro(5) == 5_000_000
    # Round-trip every preset: a conversion bug here charges a card a different number than the
    # ledger credits, which is the one failure nobody notices until reconciliation.
    for usd in (5, 10, 25, 50, 137):
        assert billing.cents_to_micro(billing.micro_to_cents(billing.usd_to_micro(usd))) == usd * 1_000_000


def test_sub_cent_amounts_are_refused_not_rounded():
    """A call costs ~600 micro-USD. Stripe cannot charge that, and rounding it away silently would
    make the card and the ledger disagree — so the conversion refuses instead."""
    with pytest.raises(billing.TopupRejected):
        billing.micro_to_cents(600)


@pytest.mark.parametrize("amount", [1, 4, 4.99, 0, -10, 10.5, 99_999, "abc", None])
def test_amount_validation_rejects_what_we_will_not_charge(amount):
    with pytest.raises(billing.TopupRejected):
        billing.validate_topup_usd(amount)


@pytest.mark.parametrize("amount", [5, 10, 25, 50, 37, 2_000])
def test_amount_validation_accepts_the_minimum_and_above(amount):
    assert billing.validate_topup_usd(amount) == amount


# ---- endpoint auth ------------------------------------------------------------------------------
async def test_billing_endpoints_require_admin_of_this_org(c: AsyncClient):
    org_id, owner = await _org(c)
    member = await _member(c, org_id, owner, "grunt@superdesign.dev")
    for method, path, body in (("GET", "/billing", None),
                               ("POST", "/billing/topup", {"amount_usd": 10}),
                               ("POST", "/billing/autotopup", {"enabled": False})):
        r = await c.request(method, path, json=body, headers=_h(member))
        assert r.status_code == 403, f"{path} let a plain member in: {r.status_code}"
        r = await c.request(method, path, json=body)
        assert r.status_code in (401, 403), f"{path} answered an anonymous caller: {r.status_code}"


async def test_billing_get_reports_state_for_an_admin(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    r = await c.get("/billing", headers=_h(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["customer"] is False and body["card_on_file"] is False
    assert body["balance_micro"] == get_settings().promo_grant_micro
    assert body["autotopup"]["enabled"] is False and body["autotopup"]["consented_at"] is None
    assert body["topup"]["min_usd"] == 5 and body["topup"]["presets"] == [5, 10, 25, 50]


async def test_billing_is_503_when_stripe_is_not_configured(c: AsyncClient, monkeypatch):
    """A self-hoster with no Stripe key gets a clear "this deployment doesn't sell balance", not a 500."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "", raising=False)
    r = await c.post("/billing/topup", json={"amount_usd": 10}, headers=_h(owner))
    assert r.status_code == 503
    assert (await c.get("/billing", headers=_h(owner))).json()["configured"] is False


# ---- manual top-up: Checkout ------------------------------------------------------------------
async def test_topup_creates_a_usd_checkout_and_never_credits(c: AsyncClient, monkeypatch):
    """The endpoint hands back a URL. Balance moves on the webhook — not here, and not on the return
    redirect, which is a URL the payer controls."""
    org_id, owner = await _org(c)
    calls: list[tuple] = []

    async def fake_sdk(fn, /, **kw):
        calls.append((getattr(fn, "__qualname__", str(fn)), kw))
        if "Customer" in str(fn):
            return {"id": "cus_test_1"}
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    monkeypatch.setattr(billing, "_sdk", fake_sdk)
    before = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["balance_micro"]
    r = await c.post("/billing/topup", json={"amount_usd": 25}, headers=_h(owner))
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    assert r.json()["amount_micro"] == 25_000_000
    session_kw = [kw for name, kw in calls if "Session" in name][0]
    # USD explicitly: the Stripe account's own default currency is AUD, and inheriting it would charge
    # a number the ledger then credits as dollars.
    assert session_kw["line_items"][0]["price_data"]["currency"] == "usd"
    assert session_kw["line_items"][0]["price_data"]["unit_amount"] == 2500  # integer cents
    assert session_kw["mode"] == "payment"
    # The card is saved WITH off-session mandate signals, which is what makes auto-top-up legal later.
    assert session_kw["payment_intent_data"]["setup_future_usage"] == "off_session"
    assert session_kw["payment_intent_data"]["metadata"]["treg_org_id"] == str(org_id)
    # Dynamic payment methods: never pin the list, let Stripe pick what converts.
    assert "payment_method_types" not in session_kw
    after = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["balance_micro"]
    assert after == before, "creating a Checkout session must not move the balance"


async def test_topup_rejects_a_below_minimum_amount(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", lambda *a, **k: pytest.fail("must not reach Stripe"))
    r = await c.post("/billing/topup", json={"amount_usd": 1}, headers=_h(owner))
    assert r.status_code == 422 and "minimum" in r.json()["detail"]


async def test_topup_reuses_the_org_stripe_customer(c: AsyncClient, monkeypatch):
    """A second Customer would orphan the saved card and silently break auto-top-up."""
    org_id, owner = await _org(c)
    created = []

    async def fake_sdk(fn, /, **kw):
        if "Customer" in str(fn):
            created.append(kw)
            return {"id": "cus_test_1"}
        return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}

    monkeypatch.setattr(billing, "_sdk", fake_sdk)
    await c.post("/billing/topup", json={"amount_usd": 10}, headers=_h(owner))
    await c.post("/billing/topup", json={"amount_usd": 10}, headers=_h(owner))
    assert len(created) == 1
    async with session_maker() as db:
        assert (await db.get(Org, org_id)).stripe_customer_id == "cus_test_1"


async def test_topup_checkout_asks_stripe_for_an_invoice(c: AsyncClient, monkeypatch):
    """A card receipt proves a charge; an invoice is the document a finance team accepts. The second
    half of this test is the real guard: the invoice must not cost us `setup_future_usage`, because
    that is the saved card and the SCA mandate every later auto-top-up charge runs on."""
    org_id, owner = await _org(c)
    calls: list[tuple] = []

    async def fake_sdk(fn, /, **kw):
        calls.append((getattr(fn, "__qualname__", str(fn)), kw))
        if "Customer" in str(fn):
            return {"id": "cus_test_1"}
        return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}

    monkeypatch.setattr(billing, "_sdk", fake_sdk)
    r = await c.post("/billing/topup", json={"amount_usd": 10}, headers=_h(owner))
    assert r.status_code == 200, r.text
    session_kw = [kw for name, kw in calls if "Session" in name][0]
    assert session_kw["invoice_creation"]["enabled"] is True
    # The org travels onto the invoice too, so one found in the Stripe dashboard resolves to a team.
    assert session_kw["invoice_creation"]["invoice_data"]["metadata"]["treg_org_id"] == str(org_id)
    assert session_kw["payment_intent_data"]["setup_future_usage"] == "off_session"


async def test_invoice_events_are_acknowledged_but_never_credit(c: AsyncClient):
    """`invoice_creation` makes Stripe emit invoice.* for every top-up. Crediting on those as well as
    on the PaymentIntent would be a second door onto the same money."""
    org_id, owner = await _org(c)
    before = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["balance_micro"]
    event = {"id": "evt_inv_1", "type": "invoice.paid", "data": {"object": {
        "id": "in_test_1", "object": "invoice", "amount_paid": 100_000, "currency": "usd",
        "metadata": {"treg_org_id": str(org_id), "treg_kind": "topup"}}}}
    r = await _deliver(c, event)
    assert r.status_code == 200, r.text
    assert r.json().get("handled") is False
    after = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["balance_micro"]
    assert after == before


# ---- the hosted portal --------------------------------------------------------------------------
async def test_portal_returns_a_one_time_url_for_a_paying_org(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    calls: list[tuple] = []

    async def fake_sdk(fn, /, **kw):
        calls.append((getattr(fn, "__qualname__", str(fn)), kw))
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1"}

    monkeypatch.setattr(billing, "_sdk", fake_sdk)
    r = await c.post("/billing/portal", headers=_h(owner))
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://billing.stripe.com/")
    name, kw = calls[0]
    assert "billing_portal" in name.lower() or "Session" in name
    assert kw["customer"] == "cus_test_1"
    assert kw["return_url"].endswith("/app#billing")


async def test_portal_refuses_an_org_with_no_stripe_customer(c: AsyncClient, monkeypatch):
    """Minting a Customer just to open an empty portal would fill the Stripe account with teams that
    never bought anything — and the portal would have nothing to show them."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", lambda *a, **k: pytest.fail("must not reach Stripe"))
    r = await c.post("/billing/portal", headers=_h(owner))
    assert r.status_code == 422
    assert (await c.get("/billing", headers=_h(owner))).json()["portal"] is False


async def test_portal_is_advertised_once_the_org_has_a_customer(c: AsyncClient):
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    assert (await c.get("/billing", headers=_h(owner))).json()["portal"] is True


async def test_portal_is_503_when_stripe_is_not_configured(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "", raising=False)
    assert (await c.post("/billing/portal", headers=_h(owner))).status_code == 503


# ---- payment history ----------------------------------------------------------------------------
def _charge(pi: str, *, invoice: str | None = None, receipt: str = "https://pay.stripe.com/r/1") -> dict:
    return {"id": f"ch_{pi}", "payment_intent": pi, "receipt_url": receipt, "invoice": invoice}


def _invoice(iid: str, *, number: str = "TREG-0001") -> dict:
    return {"id": iid, "number": number,
            "invoice_pdf": f"https://pay.stripe.com/invoice/{iid}/pdf",
            "hosted_invoice_url": f"https://invoice.stripe.com/i/{iid}"}


def _docs_sdk(charges: list[dict], invoices: list[dict]):
    async def fake_sdk(fn, /, **kw):
        return {"data": invoices if "Invoice" in str(fn) else charges}
    return fake_sdk


async def test_history_links_each_purchase_to_its_invoice(c: AsyncClient, monkeypatch):
    """Rows come from our own credit blocks so the history can never contradict the balance; Stripe is
    asked only for the documents. A payment with no invoice still gets its receipt."""
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    async with session_maker() as db:
        await ledger.topup(db, org_id, 10_000_000, "pi_manual", meta={"source": "stripe"})
        await ledger.topup(db, org_id, 25_000_000, "pi_auto", meta={"auto": True, "source": "stripe"})

    monkeypatch.setattr(billing, "_sdk", _docs_sdk(
        [_charge("pi_manual", invoice="in_1"), _charge("pi_auto")], [_invoice("in_1")]))
    r = await c.get("/billing/history", headers=_h(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stripe_ok"] is True
    rows = {i["payment_intent"]: i for i in body["items"]}
    assert rows["pi_manual"]["amount_micro"] == 10_000_000
    assert rows["pi_manual"]["invoice_pdf"].endswith("/pdf")
    assert rows["pi_manual"]["invoice_number"] == "TREG-0001"
    assert rows["pi_manual"]["auto"] is False
    # The auto charge is a bare PaymentIntent — no invoice exists for it, so the receipt is the link.
    assert rows["pi_auto"]["invoice_pdf"] == ""
    assert rows["pi_auto"]["receipt_url"].startswith("https://pay.stripe.com/")
    assert rows["pi_auto"]["auto"] is True
    # Newest first.
    assert [i["payment_intent"] for i in body["items"]] == ["pi_auto", "pi_manual"]


async def test_history_excludes_the_signup_promo(c: AsyncClient, monkeypatch):
    """The free grant is balance, not a payment — listing it would offer an invoice for money nobody
    paid."""
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    monkeypatch.setattr(billing, "_sdk", _docs_sdk([], []))
    assert (await c.get("/billing/history", headers=_h(owner))).json()["items"] == []


async def test_history_survives_a_stripe_outage(c: AsyncClient, monkeypatch):
    """A Stripe hiccup should cost the payer their download button, not their payment history."""
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    async with session_maker() as db:
        await ledger.topup(db, org_id, 10_000_000, "pi_manual", meta={"source": "stripe"})

    async def boom(fn, /, **kw):
        raise stripe.APIConnectionError("stripe is down")

    monkeypatch.setattr(billing, "_sdk", boom)
    r = await c.get("/billing/history", headers=_h(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stripe_ok"] is False
    assert body["items"][0]["amount_micro"] == 10_000_000  # the amount is still right
    assert body["items"][0]["invoice_pdf"] == "" and body["items"][0]["receipt_url"] == ""


async def test_history_never_moves_money(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    await _set_org(org_id, stripe_customer_id="cus_test_1")
    async with session_maker() as db:
        await ledger.topup(db, org_id, 10_000_000, "pi_manual", meta={"source": "stripe"})
    monkeypatch.setattr(billing, "_sdk", _docs_sdk([_charge("pi_manual", invoice="in_1")], [_invoice("in_1")]))
    before = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["balance_micro"]
    await c.get("/billing/history", headers=_h(owner))
    after = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["balance_micro"]
    assert after == before


async def test_history_and_portal_need_admin_of_this_org(c: AsyncClient, monkeypatch):
    """A card and an invoice archive are the org's money, not a member's business — the same gate as
    the rest of /billing."""
    org_id, owner = await _org(c)
    member = await _member(c, org_id, owner, "grunt@superdesign.dev")
    monkeypatch.setattr(billing, "_sdk", lambda *a, **k: pytest.fail("must not reach Stripe"))
    for method, path in (("GET", "/billing/history"), ("POST", "/billing/portal")):
        assert (await c.request(method, path, headers=_h(member))).status_code == 403
        assert (await c.request(method, path)).status_code in (401, 403)


async def test_history_of_a_team_that_never_paid_is_empty_not_an_error(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", lambda *a, **k: pytest.fail("no customer — must not ask Stripe"))
    r = await c.get("/billing/history", headers=_h(owner))
    assert r.status_code == 200 and r.json()["items"] == []


# ---- webhook: signature ------------------------------------------------------------------------
async def test_webhook_rejects_a_bad_signature(c: AsyncClient):
    org_id, _ = await _org(c)
    payload = json.dumps(_pi_event(org_id)).encode()
    for header in ("", "t=1,v1=deadbeef", _sign(payload, "whsec_the_wrong_secret")):
        r = await c.post("/billing/stripe/webhook", content=payload,
                         headers={"stripe-signature": header})
        assert r.status_code == 400, f"a payload signed with {header!r} was accepted"
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == get_settings().promo_grant_micro


async def test_webhook_rejects_a_stale_timestamp(c: AsyncClient):
    """A captured-and-replayed delivery: correctly signed, hours old. The SDK's tolerance window
    refuses it, which is the whole point of signing `{t}.{body}` rather than the body alone."""
    org_id, _ = await _org(c)
    payload = json.dumps(_pi_event(org_id)).encode()
    old = int(time.time()) - 7200
    r = await c.post("/billing/stripe/webhook", content=payload,
                     headers={"stripe-signature": _sign(payload, t=old)})
    assert r.status_code == 400


async def test_webhook_404s_when_no_secret_is_configured(c: AsyncClient, monkeypatch):
    """Same posture as the demo feed's webhook: an unconfigured deploy exposes no unauthenticated POST."""
    org_id, _ = await _org(c)
    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", "", raising=False)
    payload = json.dumps(_pi_event(org_id)).encode()
    r = await c.post("/billing/stripe/webhook", content=payload,
                     headers={"stripe-signature": _sign(payload)})
    assert r.status_code == 404


# ---- webhook: crediting + idempotency ----------------------------------------------------------
async def test_payment_intent_succeeded_credits_the_balance_once(c: AsyncClient, monkeypatch):
    """The redelivery case. Stripe delivers at least once, retries for days, and `stripe events
    resend` exists — so the same event twice must credit once."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    promo = get_settings().promo_grant_micro
    event = _pi_event(org_id, pi="pi_credit_once", cents=1000)

    first = await _deliver(c, event)
    assert first.status_code == 200, first.text
    assert first.json()["credited"] is True
    second = await _deliver(c, event)
    assert second.status_code == 200 and second.json()["credited"] is False

    body = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()
    assert body["balance_micro"] == promo + 10_000_000  # 1000 cents == $10 == 10_000_000 micro
    topups = [e for e in body["entries"]["items"] if e["kind"] == "topup"]
    assert len(topups) == 1, "a redelivered webhook wrote a second ledger entry"
    assert [b["kind"] for b in body["blocks"]].count("purchased") == 1


async def test_checkout_completed_and_payment_intent_are_the_same_credit(c: AsyncClient, monkeypatch):
    """Both events fire for one purchase, in an order Stripe does not promise. Keying idempotency on
    the PaymentIntent id is what makes either one safe to arrive first."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    pi = "pi_one_purchase"
    session_event = {"id": "evt_cs", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "object": "checkout.session", "mode": "payment", "payment_status": "paid",
        "amount_total": 2500, "currency": "usd", "payment_intent": pi,
        "metadata": {"treg_org_id": str(org_id), "treg_kind": "topup"}}}}
    assert (await _deliver(c, session_event)).json()["credited"] is True
    assert (await _deliver(c, _pi_event(org_id, pi=pi, cents=2500))).json()["credited"] is False
    body = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()
    assert body["balance_micro"] == get_settings().promo_grant_micro + 25_000_000
    assert len([e for e in body["entries"]["items"] if e["kind"] == "topup"]) == 1


async def test_credit_uses_the_amount_stripe_charged_not_the_metadata(c: AsyncClient, monkeypatch):
    """Only the collected amount may become balance. Metadata is a hint; the charge is the fact."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    event = _pi_event(org_id, pi="pi_partial", cents=1000)
    event["data"]["object"]["amount_received"] = 700  # the card only cleared $7
    await _deliver(c, event)
    body = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()
    assert body["balance_micro"] == get_settings().promo_grant_micro + 7_000_000


async def test_an_unrelated_payment_is_ignored(c: AsyncClient, monkeypatch):
    """The account also takes the landing-page demo's charges. `treg_kind` is what keeps them out of
    someone's balance."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    event = _pi_event(org_id, pi="pi_demo")
    event["data"]["object"]["metadata"] = {"visitor": "swift-otter-12"}
    r = await _deliver(c, event)
    assert r.status_code == 200 and r.json()["handled"] is False
    body = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()
    assert body["balance_micro"] == get_settings().promo_grant_micro


async def test_setup_intent_succeeded_saves_the_card_and_arms_a_consented_org(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    await _set_org(org_id, autotopup_consented_at=datetime.now(timezone.utc).replace(tzinfo=None),
                   autotopup_disabled_reason="no_card", stripe_customer_id="cus_1")
    event = {"id": "evt_si", "type": "setup_intent.succeeded", "data": {"object": {
        "id": "seti_1", "object": "setup_intent", "payment_method": "pm_saved_1",
        "metadata": {"treg_org_id": str(org_id), "treg_kind": "autotopup_card"}}}}
    r = await _deliver(c, event)
    assert r.status_code == 200 and r.json()["payment_method_saved"] is True
    state = (await c.get("/billing", headers=_h(owner))).json()
    assert state["card_on_file"] is True
    # Consent was already recorded and the only thing missing was a card — so it arms itself.
    assert state["autotopup"]["enabled"] is True and state["autotopup"]["disabled_reason"] is None


# ---- auto top-up: preferences + consent --------------------------------------------------------
async def test_enabling_autotopup_without_consent_is_refused(c: AsyncClient, monkeypatch):
    """An off-session charge with no recorded mandate is an unauthorized charge under PSD2/SCA."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    r = await c.post("/billing/autotopup", json={"enabled": True}, headers=_h(owner))
    assert r.status_code == 422 and "consent" in r.json()["detail"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
    assert org.autotopup_enabled is False and org.autotopup_consented_at is None


async def test_enabling_autotopup_stores_consent_and_returns_a_card_flow(c: AsyncClient, monkeypatch):
    """No card yet: consent + numbers are stored, and Stripe's hosted page collects the card. That
    ORDER is the point — the human agreed to these numbers before any card existed."""
    org_id, owner = await _org(c)

    async def fake_sdk(fn, /, **kw):
        if "Customer" in str(fn):
            return {"id": "cus_test_1"}
        assert kw.get("mode") == "setup", kw
        return {"id": "cs_setup", "url": "https://checkout.stripe.com/c/pay/cs_setup"}

    monkeypatch.setattr(billing, "_sdk", fake_sdk)
    r = await c.post("/billing/autotopup",
                     json={"enabled": True, "consent": True, "threshold_usd": 5, "amount_usd": 25},
                     headers=_h(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["setup_url"].startswith("https://checkout.stripe.com/")
    assert body["autotopup"]["consented_at"] is not None
    assert body["autotopup"]["amount_micro"] == 25_000_000
    assert body["autotopup"]["threshold_micro"] == 5_000_000
    # Not armed until there is something to charge — an enabled policy with no card would just fail.
    assert body["autotopup"]["enabled"] is False
    assert body["autotopup"]["disabled_reason"] == "no_card"


async def test_disabling_autotopup_clears_the_failure_banner(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    await _set_org(org_id, autotopup_enabled=True, autotopup_disabled_reason="card_declined",
                   autotopup_consented_at=billing._now())
    r = await c.post("/billing/autotopup", json={"enabled": False}, headers=_h(owner))
    assert r.status_code == 200
    assert r.json()["autotopup"]["enabled"] is False
    assert r.json()["autotopup"]["disabled_reason"] is None


# ---- auto top-up: the charge and its guards ----------------------------------------------------
async def _armed(org_id: int, **over) -> None:
    """Put an org in the state where an auto-charge is legitimate: consent, customer, card, enabled."""
    fields = dict(autotopup_enabled=True, autotopup_consented_at=billing._now(),
                  stripe_customer_id="cus_armed", stripe_default_pm="pm_armed",
                  autotopup_threshold_micro=5_000_000, autotopup_amount_micro=10_000_000,
                  autotopup_monthly_cap_micro=100_000_000, balance_micro=100)
    fields.update(over)
    await _set_org(org_id, **fields)


async def _attempt(org_id: int) -> dict:
    async with session_maker() as db:
        return await billing.attempt_auto_topup(db, org_id)


def _pi_ok(pi_id: str = "pi_auto_1", cents: int = 1000):
    async def fake_sdk(fn, /, **kw):
        assert kw.get("off_session") is True and kw.get("confirm") is True
        assert kw.get("currency") == "usd"
        assert kw.get("idempotency_key", "").startswith("treg_")
        fake_sdk.calls.append(kw)
        return {"id": pi_id, "status": "succeeded", "amount": cents, "amount_received": cents}
    fake_sdk.calls = []
    return fake_sdk


async def test_auto_topup_charges_off_session_and_credits(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    await _armed(org_id)
    sdk = _pi_ok()
    monkeypatch.setattr(billing, "_sdk", sdk)
    result = await _attempt(org_id)
    assert result["ok"] is True, result
    assert sdk.calls[0]["amount"] == 1000 and sdk.calls[0]["metadata"]["treg_auto"] == "1"
    body = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()
    assert body["balance_micro"] == 100 + 10_000_000
    entry = [e for e in body["entries"]["items"] if e["kind"] == "topup"][0]
    assert entry["meta"]["auto"] is True


async def test_auto_topup_webhook_redelivery_does_not_double_credit(c: AsyncClient, monkeypatch):
    """The off-session charge credits immediately (the succeeded status came back on OUR request), so
    Stripe's later webhook for the same PaymentIntent must be a no-op."""
    org_id, owner = await _org(c)
    await _armed(org_id)
    monkeypatch.setattr(billing, "_sdk", _pi_ok("pi_auto_dup"))
    assert (await _attempt(org_id))["ok"] is True
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    r = await _deliver(c, _pi_event(org_id, pi="pi_auto_dup", cents=1000, auto="1"))
    assert r.json()["credited"] is False
    body = (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()
    assert body["balance_micro"] == 100 + 10_000_000
    assert len([e for e in body["entries"]["items"] if e["kind"] == "topup"]) == 1


def test_the_idempotency_key_collapses_a_burst_but_not_a_changed_card():
    """Stripe ERRORS on a reused key whose parameters changed, so the key has to cover everything that
    can change. Same crossing + same card = one charge no matter how many callers notice the dip; a
    replaced card must not inherit the poisoned key of the decline that caused the replacement.
    (Found live: swapping the card after a failure died on "keys for idempotent requests can only be
    used with the same parameters".)"""
    burst = {billing._idempotency_key(1, 10_000_000, 0, "pm_a") for _ in range(5)}
    assert len(burst) == 1
    assert billing._idempotency_key(1, 10_000_000, 0, "pm_b") not in burst   # a different card
    assert billing._idempotency_key(1, 10_000_000, 1, "pm_a") not in burst   # a later crossing
    assert billing._idempotency_key(2, 10_000_000, 0, "pm_a") not in burst   # a different org
    assert billing._idempotency_key(1, 25_000_000, 0, "pm_a") not in burst   # a different amount


async def test_auto_topup_refuses_without_recorded_consent(c: AsyncClient, monkeypatch):
    org_id, _ = await _org(c)
    await _armed(org_id, autotopup_consented_at=None)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    assert (await _attempt(org_id))["reason"] == "no_consent"


async def test_auto_topup_refuses_without_a_card(c: AsyncClient, monkeypatch):
    org_id, _ = await _org(c)
    await _armed(org_id, stripe_default_pm=None)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    assert (await _attempt(org_id))["reason"] == "no_card"


async def test_auto_topup_respects_the_cooldown(c: AsyncClient, monkeypatch):
    """A burst of calls all noticing the same low balance must not each fire a charge; the cooldown is
    stamped in the DB, so it also holds across web workers."""
    org_id, _ = await _org(c)
    await _armed(org_id, autotopup_last_attempt_at=billing._now() - timedelta(seconds=60))
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    assert (await _attempt(org_id))["reason"] == "cooldown"


async def test_auto_topup_stops_at_the_monthly_cap(c: AsyncClient, monkeypatch):
    org_id, _ = await _org(c)
    await _armed(org_id, autotopup_monthly_cap_micro=15_000_000)
    monkeypatch.setattr(billing, "_sdk", _pi_ok("pi_cap_1"))
    assert (await _attempt(org_id))["ok"] is True          # $10 of a $15 cap
    await _set_org(org_id, autotopup_last_attempt_at=None, balance_micro=100)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    result = await _attempt(org_id)                        # a second $10 would exceed it
    assert result["reason"] == "monthly_cap"
    assert result["spent_micro"] == 10_000_000 and result["cap_micro"] == 15_000_000


async def test_monthly_spend_counts_only_automatic_topups(c: AsyncClient, monkeypatch):
    """The cap bounds what happens WITHOUT a human. A person choosing to add funds is not that."""
    org_id, _ = await _org(c)
    async with session_maker() as db:
        await ledger.topup(db, org_id, 50_000_000, "pi_manual", meta={"auto": False})
        await ledger.topup(db, org_id, 10_000_000, "pi_auto", meta={"auto": True})
        assert await billing.monthly_autotopup_spend(db, org_id) == 10_000_000


async def test_auto_topup_skips_when_the_balance_recovered(c: AsyncClient, monkeypatch):
    """Re-read under the lock: a manual top-up (or the winner of a race) may have already funded it."""
    org_id, _ = await _org(c)
    await _armed(org_id, balance_micro=50_000_000)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    assert (await _attempt(org_id))["reason"] == "above_threshold"


async def test_authentication_required_disables_autotopup_with_a_reason(c: AsyncClient, monkeypatch):
    """3-D Secure cannot be satisfied off-session, so retrying is pointless — it disables itself and
    keeps the PaymentIntent so the dashboard can offer an on-session recovery."""
    org_id, owner = await _org(c)
    await _armed(org_id)

    async def fake_sdk(fn, /, **kw):
        err = stripe.CardError("authentication required", param=None, code="authentication_required")
        err.error = stripe.ErrorObject.construct_from(
            {"code": "authentication_required", "payment_intent": {"id": "pi_needs_3ds"}}, "sk_test")
        raise err

    monkeypatch.setattr(billing, "_sdk", fake_sdk)
    result = await _attempt(org_id)
    assert result["reason"] == "authentication_required"
    assert result["payment_intent"] == "pi_needs_3ds"
    state = (await c.get("/billing", headers=_h(owner))).json()["autotopup"]
    assert state["enabled"] is False
    assert state["disabled_reason"] == "authentication_required"
    assert state["recovery_payment_intent"] == "pi_needs_3ds"


async def test_repeated_declines_disable_autotopup_after_max_attempts(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    await _armed(org_id)

    async def declining(fn, /, **kw):
        err = stripe.CardError("card declined", param=None, code="card_declined")
        err.error = stripe.ErrorObject.construct_from({"code": "card_declined"}, "sk_test")
        raise err

    monkeypatch.setattr(billing, "_sdk", declining)
    limit = get_settings().autotopup_max_attempts
    for n in range(1, limit + 1):
        await _set_org(org_id, autotopup_last_attempt_at=None)  # skip the cooldown, not the counter
        result = await _attempt(org_id)
        if n < limit:
            assert result["failures"] == n, result
    state = (await c.get("/billing", headers=_h(owner))).json()["autotopup"]
    assert state["enabled"] is False and state["disabled_reason"].startswith("max_attempts")


async def test_a_failed_payment_webhook_counts_only_against_automatic_charges(c: AsyncClient, monkeypatch):
    """A human who saw a decline on the hosted page can just try again; an unattended charge that
    keeps failing has to stop by itself."""
    org_id, owner = await _org(c)
    await _armed(org_id)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    manual = _pi_event(org_id, pi="pi_manual_fail", kind="payment_intent.payment_failed", auto="0")
    manual["data"]["object"]["last_payment_error"] = {"code": "card_declined"}
    await _deliver(c, manual)
    assert (await c.get("/billing", headers=_h(owner))).json()["autotopup"]["failures"] == 0
    auto = _pi_event(org_id, pi="pi_auto_fail", kind="payment_intent.payment_failed", auto="1")
    auto["data"]["object"]["last_payment_error"] = {"code": "card_declined"}
    await _deliver(c, auto)
    assert (await c.get("/billing", headers=_h(owner))).json()["autotopup"]["failures"] == 1


async def test_a_failed_authentication_webhook_disables_autotopup(c: AsyncClient, monkeypatch):
    org_id, owner = await _org(c)
    await _armed(org_id)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    event = _pi_event(org_id, pi="pi_3ds_webhook", kind="payment_intent.payment_failed", auto="1")
    event["data"]["object"]["last_payment_error"] = {"code": "authentication_required"}
    r = await _deliver(c, event)
    assert r.json()["disabled"] == "authentication_required"
    state = (await c.get("/billing", headers=_h(owner))).json()["autotopup"]
    assert state["enabled"] is False and state["disabled_reason"] == "authentication_required"


# ---- the fire-and-forget trigger ---------------------------------------------------------------
async def test_the_scheduler_hook_is_a_no_op_for_an_unarmed_org(c: AsyncClient, monkeypatch):
    """`maybe_schedule_autotopup` sits on a request path, so its cheap checks must reject before any
    task exists — an org with no card must cost a dict lookup, not a Stripe round-trip."""
    org_id, _ = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        assert billing.maybe_schedule_autotopup(org) is False
        org.autotopup_enabled = True
        org.autotopup_consented_at = billing._now()
        assert billing.maybe_schedule_autotopup(org) is False  # still no card


async def test_the_scheduler_hook_fires_for_an_armed_low_org(c: AsyncClient, monkeypatch):
    org_id, _ = await _org(c)
    await _armed(org_id)
    fired: list[int] = []

    async def fake_run(oid):
        fired.append(oid)
        billing._scheduled.discard(oid)

    monkeypatch.setattr(billing, "_run_autotopup", fake_run)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        assert billing.maybe_schedule_autotopup(org) is True
    import asyncio
    await asyncio.sleep(0)  # let the created task run
    assert fired == [org_id]


async def test_reading_the_balance_never_waits_on_stripe(c: AsyncClient, monkeypatch):
    """The balance endpoint is the trigger point until phase 3 hooks `reserve`. It must schedule, not
    await — a Stripe round-trip in a read path is latency an agent pays for nothing."""
    org_id, owner = await _org(c)
    await _armed(org_id)
    monkeypatch.setattr(billing, "_sdk", lambda *a, **k: pytest.fail("Stripe was called inline"))
    scheduled: list[int] = []
    monkeypatch.setattr(billing, "_run_autotopup", lambda oid: scheduled.append(oid) or _noop())
    r = await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))
    assert r.status_code == 200
    assert scheduled == [org_id]


async def _noop():
    return None


async def _no_sdk(fn, /, **kw):
    """A Stripe call the test did not expect. Anything reaching here is a code path that would have
    hit the network in production."""
    raise AssertionError(f"unexpected Stripe call: {fn} {kw}")


# ---- product analytics (PostHog) ----------------------------------------------------------------
async def test_topup_completed_fires_once_across_redelivery(c: AsyncClient, monkeypatch):
    """One PostHog `topup_completed` per PaymentIntent — a webhook redelivery re-credits nothing
    and must re-emit nothing (same `fresh` guard as the receipt email)."""
    from treg import analytics
    org_id, _owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)

    async def _no_post(batch):  # the flusher must not reach the real PostHog host
        pass
    monkeypatch.setattr(analytics, "_post", _no_post)
    analytics._queue.clear()

    event = _pi_event(org_id, pi="pi_analytics_once", cents=1500)
    assert (await _deliver(c, event)).json()["credited"] is True
    assert (await _deliver(c, event)).json()["credited"] is False

    done = [e for e in analytics._queue if e["event"] == "topup_completed"]
    assert len(done) == 1, "a redelivered webhook emitted a second revenue event"
    props = done[0]["properties"]
    assert props["amount_micro"] == 15_000_000  # canonical integer micro-USD
    assert props["auto"] is False
    assert props["balance_after_micro"] == get_settings().promo_grant_micro + 15_000_000
    assert props["$groups"] == {"team": props["org"]}
    assert done[0]["distinct_id"] == "billing@superdesign.dev"
    analytics._queue.clear()


async def test_analytics_outage_cannot_500_the_webhook(c: AsyncClient, monkeypatch):
    """A handler exception returns 500 ON PURPOSE (Stripe retries) — so analytics must be
    incapable of causing one, even when its own machinery breaks."""
    from treg import analytics
    org_id, _owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)

    async def _no_post(batch):
        pass
    monkeypatch.setattr(analytics, "_post", _no_post)

    def _explode():
        raise RuntimeError("flusher scheduling broke")
    monkeypatch.setattr(analytics, "_ensure_flusher", _explode)

    r = await _deliver(c, _pi_event(org_id, pi="pi_analytics_broken", cents=500))
    assert r.status_code == 200 and r.json()["credited"] is True
    analytics._queue.clear()


async def test_no_posthog_key_means_no_events(c: AsyncClient, monkeypatch):
    from treg import analytics
    org_id, _owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    analytics._queue.clear()  # default settings: no key
    assert (await _deliver(c, _pi_event(org_id, pi="pi_no_key", cents=500))).status_code == 200
    assert analytics._queue == []
