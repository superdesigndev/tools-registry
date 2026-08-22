"""OAuth-billed metering: providers whose upstream bill lands on treg's app (X's pay-per-use).

A registry X connect is the org's own credential (tier 1/2), but every call on it spends treg's
prepaid X credits — so with the provider allow-listed (`TREG_OAUTH_BILLED_PROVIDERS=x`) the call
runs the same reserve→relay→settle path as tier 4. The fences under test: the kill switch defaults
OFF (today's free behavior), a BYO-app connection is never metered (its upstream bill is the
org's own), reads settle against the RESOURCES RETURNED (X bills per resource), writes price up
13x when the post text carries a URL, and an empty balance refuses with an actionable 402.
"""

from __future__ import annotations

import json
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from treg import api as A
from treg import crypto
from treg.config import get_settings
from treg.db import session_maker
from treg.models import Org, Secret, Tool

POSTS = "x.x.user.posts"        # GET /2/users/{id}/tweets — per_result $0.005/post
POST_MICRO = 5_000
# GET /2/users/me — one User resource at $0.010. NOT the $0.001 "owned read": X grants that rate
# only when the caller owns the developer app, and on a registry connect the app is treg's, so a
# member reading their own profile pays the ordinary rate. This said 1_000 until 2026-08-18.
PROFILE_MICRO = 10_000
CREATE_MICRO = 15_000           # POST /2/tweets — per_call $0.015…
CREATE_LINK_MICRO = 200_000     # …but $0.20 when the text carries a URL


@pytest.fixture
def billed_on(monkeypatch):
    """Opt the deployment into charging for X, the way a deploy does."""
    monkeypatch.setenv("TREG_OAUTH_BILLED_PROVIDERS", "x")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _balance(clients: AsyncClient) -> int:
    return (await clients.get(f"/orgs/{await _org_id(clients)}/balance")).json()["balance_micro"]


async def _entries(clients: AsyncClient) -> list[dict]:
    return (await clients.get(f"/orgs/{await _org_id(clients)}/balance")).json()["entries"]["items"]


async def _connect_x(clients: AsyncClient, *, provider: str = "x") -> None:
    """Plant what the connect callback produces: an oauth secret attributed to the provider plus
    the auto-provisioned tool bound to it. `provider=""` is a BYO connect (the caller's own X
    developer app) — the attribution, not the token shape, is what decides who X bills."""
    org_id = await _org_id(clients)
    blob = {"access_token": "XTOK", "refresh_token": "RT", "client_id": "cid",
            "client_secret": "cs", "token_uri": "http://upstream/token",
            "expires_at": time.time() + 9999}
    async with session_maker() as db:
        secret = Secret(org_id=org_id, name="x", owner="tim@superdesign.dev", kind="oauth",
                        value=crypto.encrypt(json.dumps(blob)), provider=provider)
        db.add(secret)
        await db.flush()
        db.add(Tool(org_id=org_id, name="x", owner="tim@superdesign.dev",
                    base_url="https://api.x.com", host="api.x.com",
                    bindings=[{"secret_id": secret.id, "injector": "oauth", "location": "header",
                               "name": "Authorization", "format": "Bearer {secret}",
                               "secret_field": "access_token"}]))
        await db.commit()


# ---- the kill switch and the BYO exemption -----------------------------------------------------
async def test_kill_switch_off_stays_free(clients: AsyncClient):
    """Default deploy = no TREG_OAUTH_BILLED_PROVIDERS: an X connect call is served and nothing
    touches the ledger — exactly today's behavior."""
    await _connect_x(clients)
    r = await clients.get(f"/call/{POSTS}?id=42&max_results=5")
    assert r.status_code == 200, r.text
    assert "x-treg-cost-micro" not in {k.lower() for k in r.headers}
    kinds = {e["kind"] for e in await _entries(clients)}
    assert "reserve" not in kinds and "settle" not in kinds


async def test_byo_connection_never_metered(clients: AsyncClient, billed_on):
    """A BYO-app connect (secret.provider == "") pays X directly — flag on or not, no meter."""
    await _connect_x(clients, provider="")
    r = await clients.get(f"/call/{POSTS}?id=42&max_results=5")
    assert r.status_code == 200, r.text
    assert "x-treg-cost-micro" not in {k.lower() for k in r.headers}
    kinds = {e["kind"] for e in await _entries(clients)}
    assert "reserve" not in kinds and "settle" not in kinds


# ---- metered reads settle against what came back -----------------------------------------------
async def test_read_settles_per_resource_returned(clients: AsyncClient, billed_on, monkeypatch):
    """Asked for 10, got 2: the hold is the estimate (10 × $0.005) but the charge is the response
    (2 × $0.005) — X bills per resource returned and so do we."""
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(200, b'{"data": [{}, {}]}'))
    before = await _balance(clients)
    r = await clients.get(f"/call/{POSTS}?id=42&max_results=10")
    assert r.status_code == 200, r.text
    charged = int(r.headers["x-treg-cost-micro"])
    assert charged == 2 * POST_MICRO
    assert await _balance(clients) == before - charged
    entries = await _entries(clients)
    settle = next(e for e in entries if e["kind"] == "settle")
    assert settle["meta"]["observed_micro"] == 2 * POST_MICRO
    reserve = next(e for e in entries if e["kind"] == "reserve")
    assert reserve["meta"]["estimated_micro"] == 10 * POST_MICRO
    assert reserve["meta"]["tier"] == "oauth"


async def test_empty_page_settles_at_zero(clients: AsyncClient, billed_on, monkeypatch):
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(200, b'{"data": []}'))
    before = await _balance(clients)
    r = await clients.get(f"/call/{POSTS}?id=42&max_results=10")
    assert r.status_code == 200, r.text
    assert int(r.headers["x-treg-cost-micro"]) == 0
    assert await _balance(clients) == before


async def test_upstream_5xx_releases_the_hold(clients: AsyncClient, billed_on, monkeypatch):
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(503, b"upstream sad"))
    before = await _balance(clients)
    r = await clients.get(f"/call/{POSTS}?id=42&max_results=10")
    assert r.status_code == 503
    assert await _balance(clients) == before
    kinds = [e["kind"] for e in await _entries(clients)]
    assert "release" in kinds and "settle" not in kinds


# ---- passthrough pays the same price as the catalog id -----------------------------------------
async def test_url_passthrough_is_priced_off_the_matched_endpoint(
    clients: AsyncClient, billed_on, monkeypatch
):
    """Calling the URL directly must not be a discount door: /2/users/me lands on the curated
    profile read and bills its per_result $0.010."""
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(200, b'{"data": {"id": "1"}}'))
    r = await clients.get("/call/https://api.x.com/2/users/me")
    assert r.status_code == 200, r.text
    assert int(r.headers["x-treg-cost-micro"]) == PROFILE_MICRO
    settle = next(e for e in await _entries(clients) if e["kind"] == "settle")
    assert settle["endpoint_id"] == "x.x.user.profile"


async def test_unmatched_passthrough_gets_the_default_read_rate(
    clients: AsyncClient, billed_on, monkeypatch
):
    """A route the catalog doesn't know still costs treg money — the provider-level default
    ($0.005/result) covers it, settled against the resources that actually came back."""
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(200, b'{"data": [{}, {}, {}]}'))
    r = await clients.get("/call/https://api.x.com/2/nonexistent/route?max_results=50")
    assert r.status_code == 200, r.text
    assert int(r.headers["x-treg-cost-micro"]) == 3 * POST_MICRO
    settle = next(e for e in await _entries(clients) if e["kind"] == "settle")
    assert settle["endpoint_id"] == "x.passthrough"


# ---- writes ------------------------------------------------------------------------------------
async def test_post_create_is_per_call(clients: AsyncClient, billed_on, monkeypatch):
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(201, b'{"data": {"id": "1", "text": "hi"}}'))
    r = await clients.post("/call/x.x.post.create",
                           json={"text": "Shipping something new today."})
    assert r.status_code == 201, r.text
    assert int(r.headers["x-treg-cost-micro"]) == CREATE_MICRO


async def test_post_with_url_prices_13x(clients: AsyncClient, billed_on, monkeypatch):
    """$0.015 → $0.20 when the text carries a URL — the single biggest mispricing risk, sniffed
    from the body at estimate time."""
    await _connect_x(clients)
    monkeypatch.setattr(A, "relay", _stub_relay(201, b'{"data": {"id": "1"}}'))
    r = await clients.post("/call/x.x.post.create",
                           json={"text": "read this: https://example.com/post"})
    assert r.status_code == 201, r.text
    assert int(r.headers["x-treg-cost-micro"]) == CREATE_LINK_MICRO


# ---- the money fences --------------------------------------------------------------------------
async def test_empty_balance_is_an_actionable_402(clients: AsyncClient, billed_on):
    await _connect_x(clients)
    async with session_maker() as db:
        await db.execute(update(Org).values(balance_micro=0))
        await db.commit()
    r = await clients.get(f"/call/{POSTS}?id=42&max_results=10")
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == "insufficient_balance"
    assert "developer app" in detail["message"], "the fix for a billed OAuth call is BYO, not a key"


# ---- pricing units -----------------------------------------------------------------------------
def test_post_has_link_sniffs_only_the_text_field():
    assert A._post_has_link(b'{"text": "see https://a.example"}')
    assert A._post_has_link(b'{"text": "see www.example.com"}')
    assert not A._post_has_link(b'{"text": "no links here"}')
    assert not A._post_has_link(b'{"text": "plain", "quote_tweet_id": "123"}')
    assert not A._post_has_link(b'not json')
    assert not A._post_has_link(b"")


def test_billed_endpoint_match_prefers_exact_over_template():
    ep = A._billed_endpoint_match("x", "GET", "/2/users/me")
    assert ep and ep["id"] == "x.x.user.profile"
    ep = A._billed_endpoint_match("x", "GET", "/2/users/44196397/tweets")
    assert ep and ep["id"] == "x.x.user.posts"
    assert A._billed_endpoint_match("x", "GET", "/2/no/such/route") is None


def _stub_relay(status_code: int, body: bytes):
    """A relay stand-in returning a fixed upstream outcome (same shape as
    test_marketplace_call._fake_relay — duplicated to keep the two files importable alone)."""
    from fastapi.responses import StreamingResponse

    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        async def _stream():
            yield body

        return StreamingResponse(_stream(), status_code=status_code)

    return _relay
