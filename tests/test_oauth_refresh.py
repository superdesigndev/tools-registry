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
