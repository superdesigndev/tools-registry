"""The landing page's live payments feed: webhook signature checks + the SSE ring buffer."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from httpx import AsyncClient

import treg.api as api_mod
from treg import pubfeed

SECRET = "whsec_test_secret"


def _sign(payload: bytes, secret: str = SECRET, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _charge_event(amount: int = 420, desc: str = "GRAFFITI <script>", **extra) -> bytes:
    return json.dumps({"type": "charge.succeeded", "data": {"object": {
        "id": "ch_3AbCdEfGhIjKlMnO", "amount": amount, "currency": "usd",
        "created": 1784000000, "description": desc, **extra,
    }}}).encode()


@pytest.fixture(autouse=True)
def _clean_feed(monkeypatch):
    pubfeed.reset()
    monkeypatch.setattr(api_mod.get_settings(), "demo_stripe_webhook_secret", SECRET)
    yield
    pubfeed.reset()


# ---- signature verification ----------------------------------------------------------------
def test_signature_roundtrip():
    body = _charge_event()
    assert pubfeed.verify_signature(body, _sign(body), SECRET)
    assert not pubfeed.verify_signature(body, _sign(body, secret="whsec_wrong"), SECRET)
    assert not pubfeed.verify_signature(b"tampered" + body, _sign(body), SECRET)
    assert not pubfeed.verify_signature(body, "", SECRET)


def test_signature_rejects_stale_timestamp():
    body = _charge_event()
    old = int(time.time()) - pubfeed.SIG_TOLERANCE_S - 10
    assert not pubfeed.verify_signature(body, _sign(body, t=old), SECRET)


def test_signature_accepts_any_v1_during_rotation():
    body = _charge_event()
    t = int(time.time())
    good = _sign(body, t=t).split("v1=")[1]
    header = f"t={t},v1={'0' * 64},v1={good}"
    assert pubfeed.verify_signature(body, header, SECRET)


# ---- the webhook endpoint -------------------------------------------------------------------
async def test_webhook_is_404_when_unconfigured(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(api_mod.get_settings(), "demo_stripe_webhook_secret", "")
    r = await clients.post("/stripe/webhook", content=_charge_event())
    assert r.status_code == 404


async def test_webhook_rejects_bad_signature(clients: AsyncClient):
    r = await clients.post("/stripe/webhook", content=_charge_event(),
                           headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400
    assert len(pubfeed._events) == 0


async def test_webhook_feeds_only_server_chosen_fields(clients: AsyncClient):
    body = _charge_event(amount=777, desc="visitor-typed junk",
                         receipt_url="https://pay.stripe.com/receipts/payment/CAca123")
    r = await clients.post("/stripe/webhook", content=body, headers={"Stripe-Signature": _sign(body)})
    assert r.status_code == 200
    assert len(pubfeed._events) == 1
    event = pubfeed._events[0]
    assert event["amount"] == 777 and event["currency"] == "usd" and event["id_suffix"] == "jKlMnO"
    assert event["receipt_url"] == "https://pay.stripe.com/receipts/payment/CAca123"
    # a hand-typed description never reaches the page — it's replaced by an id-derived name
    assert "visitor-typed junk" not in json.dumps(event)
    assert event["name"] == pubfeed._derived_name("ch_3AbCdEfGhIjKlMnO")


def test_display_name_accepts_only_wordlist_names():
    ours = f"{pubfeed.ADJECTIVES[0]}-{pubfeed.ANIMALS[0]}-42"
    assert pubfeed._display_name({"id": "ch_x", "description": ours}) == ours
    for bad in ("rude-words-42", "swift-otter-4200", "swift-otter", "swift-otter-42-extra",
                "SWIFT-OTTER-42", "", None, 123):
        name = pubfeed._display_name({"id": "ch_x", "description": bad})
        assert name == pubfeed._derived_name("ch_x"), bad
    # derived names are themselves wordlist-shaped (adj-animal-nn)
    adj, animal, n = pubfeed._derived_name("ch_whatever").split("-")
    assert adj in pubfeed.ADJECTIVES and animal in pubfeed.ANIMALS and n.isdigit()


def test_metadata_visitor_beats_description():
    ours = f"{pubfeed.ADJECTIVES[1]}-{pubfeed.ANIMALS[1]}-7"
    got = pubfeed._display_name({"id": "ch_x", "description": "whatever the caller typed",
                                 "metadata": {"visitor": ours}})
    assert got == ours
    # a forged non-wordlist metadata value is still rejected
    got = pubfeed._display_name({"id": "ch_x", "metadata": {"visitor": "hand<crafted>"}})
    assert got == pubfeed._derived_name("ch_x")


def test_receipt_url_must_be_stripe_hosted():
    pubfeed.push_charge({"id": "ch_a", "amount": 1, "currency": "usd", "created": 1,
                         "receipt_url": "https://evil.example/phish"})
    assert pubfeed._events[-1]["receipt_url"] is None
    pubfeed.push_charge({"id": "ch_b", "amount": 1, "currency": "usd", "created": 1,
                         "receipt_url": "https://pay.stripe.com/receipts/x"})
    assert pubfeed._events[-1]["receipt_url"] == "https://pay.stripe.com/receipts/x"


async def test_webhook_ignores_other_event_types(clients: AsyncClient):
    body = json.dumps({"type": "customer.created", "data": {"object": {"id": "cus_1"}}}).encode()
    r = await clients.post("/stripe/webhook", content=body, headers={"Stripe-Signature": _sign(body)})
    assert r.status_code == 200
    assert len(pubfeed._events) == 0


# ---- the SSE stream -------------------------------------------------------------------------
async def test_stream_replays_ring_buffer_then_live():
    pubfeed.push_charge({"id": "ch_1", "amount": 100, "currency": "usd", "created": 1})
    gen = pubfeed.stream()
    first = await gen.__anext__()
    got = json.loads(first.removeprefix("data: "))
    assert got == {"amount": 100, "currency": "usd", "created": 1, "id_suffix": "ch_1",
                   "name": pubfeed._derived_name("ch_1"), "receipt_url": None}
    pubfeed.push_charge({"id": "ch_2", "amount": 200, "currency": "usd", "created": 2})
    second = await gen.__anext__()
    assert json.loads(second.removeprefix("data: "))["amount"] == 200
    await gen.aclose()
    assert len(pubfeed._subscribers) == 0  # unsubscribed on close


async def test_ring_buffer_is_bounded():
    for i in range(pubfeed.FEED_MAX + 15):
        pubfeed.push_charge({"id": f"ch_{i}", "amount": i, "currency": "usd", "created": i})
    assert len(pubfeed._events) == pubfeed.FEED_MAX
    assert pubfeed._events[0]["amount"] == 15  # oldest were evicted
