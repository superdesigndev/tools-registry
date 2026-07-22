"""Connections: what a registry connect produces beyond a raw token.

A connect that yields no callable tool is a dead end — the user consented and got nothing. So the
callback records provider/scopes/expiry, auto-provisions the provider's tool bound to the new
credential, and exposes resource discovery so the connection knows what it acts on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from treg import crypto, oauth
from treg.config import get_settings
from treg.models import Secret

# The test upstream serves /token, standing in for Google's token endpoint.
BYO = {
    "name": "plain", "client_id": "cid", "client_secret": "csec",
    "auth_uri": "http://provider/auth", "token_uri": "http://upstream/token",
    "scopes": ["https://www.googleapis.com/auth/webmasters.readonly"],
}


@pytest.fixture
def treg_google_app(monkeypatch):
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "treg-google-cid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "treg-google-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _connect_byo(clients: AsyncClient, **over) -> dict:
    """Drive a full BYO connect against the in-process upstream and return the status payload."""
    body = {**BYO, **over}
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    cb = await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    assert cb.status_code == 200, cb.text
    return (await clients.get(f"/oauth/status/{state}")).json()


# ---- connection metadata -----------------------------------------------------------------
async def test_callback_records_granted_scopes_and_expiry(clients: AsyncClient):
    st = await _connect_byo(clients)
    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    c = conns[st["secret_id"]]
    assert c["scopes"] == ["https://www.googleapis.com/auth/webmasters.readonly"]
    assert c["expires_at"] is not None  # exchange_code always stamps one
    assert c["refreshable"] is True  # the test upstream returns a refresh_token
    assert c["expiry_state"] == "fresh"


async def test_connections_never_leak_token_material(clients: AsyncClient):
    await _connect_byo(clients)
    body = (await clients.get("/connections")).text
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "client_secret" not in body


async def test_byo_connect_has_no_provider(clients: AsyncClient):
    """Only registry connects are attributed to a provider."""
    st = await _connect_byo(clients)
    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[st["secret_id"]]["provider"] == ""


# ---- expiry as its own axis --------------------------------------------------------------
def test_refreshable_credentials_are_always_fresh():
    """treg mints a new access token on demand, so a short expiry is an implementation detail —
    nagging the user about it would be noise."""
    past = datetime(2020, 1, 1)
    assert oauth.expiry_state(past, refreshable=True) == "fresh"


def test_non_refreshable_expiry_is_surfaced():
    """The LinkedIn case: healthy right up until it silently dies."""
    now = datetime(2026, 7, 21)
    assert oauth.expiry_state(now - timedelta(days=1), False, now) == "expired"
    assert oauth.expiry_state(now + timedelta(days=3), False, now) == "expiring"
    assert oauth.expiry_state(now + timedelta(days=30), False, now) == "fresh"
    assert oauth.expiry_state(None, False, now) == "unknown"


# ---- auto-provisioning -------------------------------------------------------------------
async def test_registry_connect_autoprovisions_a_callable_tool(clients: AsyncClient, treg_google_app):
    """The point: after consent the user can immediately make a real proxied call."""
    st = await _connect_byo(
        clients, provider="google-search-console", name="google-search-console",
    )
    assert st["status"] == "done"
    tools = {t["name"]: t for t in (await clients.get("/tools")).json()}
    tool = tools["google-search-console"]
    assert tool["base_url"] == "https://searchconsole.googleapis.com"
    assert tool["bindings"][0]["secret_id"] == st["secret_id"]
    assert tool["bindings"][0]["format"] == "Bearer {secret}"


async def test_reconnect_rebinds_instead_of_duplicating(clients: AsyncClient, treg_google_app):
    first = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    second = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    tools = [t for t in (await clients.get("/tools")).json() if t["name"] == "google-search-console"]
    assert len(tools) == 1, "reconnecting must rebind, not pile up duplicate tools"
    assert tools[0]["bindings"][0]["secret_id"] == second["secret_id"] != first["secret_id"]


async def test_byo_connect_provisions_no_tool(clients: AsyncClient):
    """Without a registry provider treg doesn't know the upstream, so it must not invent one."""
    await _connect_byo(clients)
    assert (await clients.get("/tools")).json() == []


# ---- resource selection + revoke ---------------------------------------------------------
async def test_set_and_read_back_the_selected_resource(clients: AsyncClient, treg_google_app):
    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    sid = st["secret_id"]
    r = await clients.post(f"/connections/{sid}/resource", json={"resource_ref": "sc-domain:example.com"})
    assert r.status_code == 200 and r.json()["resource_ref"] == "sc-domain:example.com"
    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[sid]["resource_ref"] == "sc-domain:example.com"


async def test_discovery_refused_for_a_provider_that_cannot_discover(clients: AsyncClient):
    st = await _connect_byo(clients)  # BYO → no provider → no discovery
    r = await clients.get(f"/connections/{st['secret_id']}/resources")
    assert r.status_code == 422


async def test_revoke_removes_the_connection(clients: AsyncClient):
    st = await _connect_byo(clients)
    sid = st["secret_id"]
    assert (await clients.delete(f"/connections/{sid}")).status_code == 200
    assert [c for c in (await clients.get("/connections")).json() if c["id"] == sid] == []


async def test_another_orgs_connection_is_not_reachable(clients: AsyncClient):
    """Connections are org-scoped; a bare id must not cross the tenant boundary."""
    st = await _connect_byo(clients)
    other = await clients.post("/users", json={"email": "outsider@example.com"})
    hdr = {"X-Treg-Token": other.json()["token"]}
    r = await clients.get(f"/connections/{st['secret_id']}/resources", headers=hdr)
    assert r.status_code == 404


# ---- the chosen resource must be human-readable -------------------------------------------
async def test_selecting_a_resource_stores_its_readable_name(clients: AsyncClient, treg_google_app):
    """Upstream ids are opaque ("properties/384078430"). Showing one to a user says nothing about
    which property they picked, so the label is stored next to the ref."""
    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    sid = st["secret_id"]
    r = await clients.post(f"/connections/{sid}/resource", json={
        "resource_ref": "properties/384078430", "resource_name": "ai-jason.com",
    })
    assert r.status_code == 200
    assert r.json()["resource_name"] == "ai-jason.com"
    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[sid]["resource_name"] == "ai-jason.com"
    assert conns[sid]["resource_ref"] == "properties/384078430"  # the id is still what we call with


async def test_discovery_backfills_a_missing_resource_name(clients: AsyncClient, treg_google_app, monkeypatch):
    """A target chosen before labels existed (or set via the API, which has no label to give)
    shouldn't force a pointless re-pick just to make the row readable — discovery is already
    holding the upstream's own naming."""
    import dataclasses

    from treg import oauth_providers as P

    # point discovery at the in-process upstream, and read a label field distinct from the id
    test_provider = dataclasses.replace(
        P.REGISTRY["google-search-console"],
        discover_base_url="http://upstream", discover_label_field="displayName",
    )
    monkeypatch.setitem(P.REGISTRY, "google-search-console", test_provider)

    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    sid = st["secret_id"]
    await clients.post(f"/connections/{sid}/resource", json={"resource_ref": "sc-domain:example.com"})
    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[sid]["resource_name"] == "", "no label was supplied, so none is stored yet"

    r = await clients.get(f"/connections/{sid}/resources")
    assert r.status_code == 200, r.text
    assert r.json()["resources"][0]["label"] == "Example (production)"

    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[sid]["resource_name"] == "Example (production)", "discovery should backfill the label"
    assert conns[sid]["resource_ref"] == "sc-domain:example.com", "the id we call with must not change"


async def test_successful_discovery_marks_the_connection_working(clients: AsyncClient, treg_google_app, monkeypatch):
    """Listing resources is a real authenticated upstream call — the best evidence we get that a
    credential works, so it shouldn't be thrown away."""
    import dataclasses

    from treg import oauth_providers as P

    monkeypatch.setitem(P.REGISTRY, "google-search-console", dataclasses.replace(
        P.REGISTRY["google-search-console"], discover_base_url="http://upstream"))
    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    sid = st["secret_id"]
    assert {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]["health"] == "unknown"

    assert (await clients.get(f"/connections/{sid}/resources")).status_code == 200
    assert {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]["health"] == "ok"
