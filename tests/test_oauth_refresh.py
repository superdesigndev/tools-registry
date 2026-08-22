"""Phase A: treg keeps OAuth tokens fresh. A stale oauth secret is refreshed (via its token_uri)
in place before the call, so the upstream always sees a live token. The upstream's /token route
(conftest) stands in for the provider's token endpoint and returns access_token "REFRESHED".
"""

from __future__ import annotations

import json
import time

from httpx import AsyncClient


def _oauth_blob(access: str, expires_at: float) -> str:
    return json.dumps(
        {
            "access_token": access,
            "refresh_token": "RT",
            "client_id": "cid",
            "client_secret": "csec",
            "token_uri": "http://upstream/token",  # ASGITransport routes this to the test upstream
            "expires_at": expires_at,
        }
    )


async def _register_oauth_tool(c: AsyncClient, name: str, blob: str) -> None:
    sid = (await c.post("/secrets", json={"name": f"{name}-s", "kind": "oauth", "value": blob})).json()["id"]
    r = await c.post("/tools", json={"name": name, "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    assert r.status_code == 200, r.text


async def test_stale_token_is_refreshed_before_call(clients: AsyncClient):
    await _register_oauth_tool(clients, "gsc", _oauth_blob("OLD", expires_at=0))  # already expired
    r = await clients.get("/call/gsc/echo")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer REFRESHED"  # treg refreshed it silently


async def test_valid_token_is_not_refreshed(clients: AsyncClient):
    await _register_oauth_tool(clients, "gsc", _oauth_blob("STILL-GOOD", expires_at=time.time() + 3600))
    r = await clients.get("/call/gsc/echo")
    assert r.json()["auth"] == "Bearer STILL-GOOD"  # untouched — still valid


async def test_manual_mode_token_without_refresh_fields_is_injected_as_is(clients: AsyncClient):
    # MANUAL mode: a bare uploaded token (no refresh_token/client creds), even with no expiry,
    # is injected verbatim — treg never tries to refresh what it can't.
    blob = json.dumps({"access_token": "MANUAL-TOKEN"})
    await _register_oauth_tool(clients, "manual", blob)
    r = await clients.get("/call/manual/echo")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer MANUAL-TOKEN"


async def test_refresh_persists_so_next_call_is_free(clients: AsyncClient):
    await _register_oauth_tool(clients, "gsc", _oauth_blob("OLD", expires_at=0))
    first = await clients.get("/call/gsc/echo")
    assert first.json()["auth"] == "Bearer REFRESHED"
    # second call: token is now fresh + persisted (expires_in 3600), still serves the refreshed one
    second = await clients.get("/call/gsc/echo")
    assert second.json()["auth"] == "Bearer REFRESHED"


# ---- token-endpoint client authentication (X / Pinterest demand HTTP Basic) ---------------------
# These drive oauth.refresh() directly against a strict stand-in that behaves like X: a refresh
# posting client_secret in the body is 401'd. The proxy-level tests above can't catch this because
# conftest's /token accepts anything — which is exactly how the bug shipped: connect worked, and
# every refresh died two hours later in production only.

import httpx

from treg import oauth


def _x_like_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if request.headers.get("authorization", "").startswith("Basic "):
            assert "client_secret=" not in body  # Basic AND body secret would be double-auth
            calls.append("basic")
            return httpx.Response(200, json={"access_token": "VIA-BASIC", "expires_in": 7200})
        calls.append("body")
        return httpx.Response(401, json={"error": "invalid_client"})  # X's actual behavior
    return httpx.MockTransport(handler)


def _basic_blob(**extra) -> dict:
    return {"access_token": "OLD", "refresh_token": "RT", "client_id": "cid",
            "client_secret": "csec", "token_uri": "https://x.test/2/oauth2/token", **extra}


async def test_refresh_honors_recorded_basic_auth_method():
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_x_like_transport(calls)) as client:
        new = await oauth.refresh(_basic_blob(token_endpoint_auth_method="client_secret_basic"), client)
    assert calls == ["basic"]  # no body-auth attempt at all
    assert new["access_token"] == "VIA-BASIC"


async def test_legacy_blob_without_method_retries_with_basic_and_learns():
    # Connections minted before the method was persisted: first attempt is body auth (401),
    # the retry succeeds with Basic, and the blob records what worked so next time is one call.
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_x_like_transport(calls)) as client:
        new = await oauth.refresh(_basic_blob(), client)
    assert calls == ["body", "basic"]
    assert new["access_token"] == "VIA-BASIC"
    assert new["token_endpoint_auth_method"] == "client_secret_basic"


async def test_refresh_body_auth_providers_unaffected():
    # Google et al: secret in the body, no Authorization header — the pre-fix path, byte for byte.
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert "client_secret=csec" in request.read().decode()
        return httpx.Response(200, json={"access_token": "VIA-BODY", "expires_in": 3600})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        new = await oauth.refresh(_basic_blob(), client)
    assert new["access_token"] == "VIA-BODY"
    assert "token_endpoint_auth_method" not in new  # learned nothing; nothing failed


async def test_exchange_code_persists_basic_auth_method_into_blob():
    # The blob is the ONLY thing refresh() has months later — PendingOAuth is long deleted. If the
    # method doesn't ride along here, every X/Pinterest refresh reverts to body auth and 401s.
    from treg import crypto
    from treg.models import PendingOAuth

    p = PendingOAuth(
        state="s", org_id=1, owner="o@x", provider="x", scopes="tweet.read",
        auth_uri="https://x.test/authorize", token_uri="https://x.test/2/oauth2/token",
        client_id="cid", client_secret=crypto.encrypt("csec"), redirect_uri="https://treg/cb",
        token_endpoint_auth_method="client_secret_basic",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        blob = await oauth.exchange_code(p, "the-code", client)
    assert blob["token_endpoint_auth_method"] == "client_secret_basic"

    # And a Google-shaped pending (default method) must NOT grow the field.
    g = PendingOAuth(
        state="s2", org_id=1, owner="o@x", provider="google-analytics", scopes="r",
        auth_uri="https://g.test/auth", token_uri="https://g.test/token",
        client_id="cid", client_secret=crypto.encrypt("csec"), redirect_uri="https://treg/cb",
    )

    def g_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(g_handler)) as client:
        gblob = await oauth.exchange_code(g, "the-code", client)
    assert "token_endpoint_auth_method" not in gblob
