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
    """Reconnecting updates the SAME connection and keeps one tool pointing at it — no duplicate
    tool, and no second credential for the same provider."""
    first = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    second = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    tools = [t for t in (await clients.get("/tools")).json() if t["name"] == "google-search-console"]
    assert len(tools) == 1, "reconnecting must rebind, not pile up duplicate tools"
    assert first["secret_id"] == second["secret_id"]
    assert tools[0]["bindings"][0]["secret_id"] == second["secret_id"]


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


# ---- disconnecting must not leave a broken tool behind -------------------------------------
async def test_revoke_removes_the_provider_tool_it_provisioned(clients: AsyncClient, treg_google_app):
    """A tool bound to a deleted credential isn't "still configured", it's broken — and it only
    says so at call time with "a bound secret is missing"."""
    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    sid = st["secret_id"]
    assert any(t["name"] == "google-search-console" for t in (await clients.get("/tools")).json())

    r = await clients.delete(f"/connections/{sid}")
    assert r.status_code == 200
    assert r.json()["removed_tools"] == ["google-search-console"]
    assert not any(t["name"] == "google-search-console" for t in (await clients.get("/tools")).json())


async def test_revoke_keeps_a_user_built_tool_but_drops_the_dead_binding(clients: AsyncClient):
    """Their own tool with several credentials must survive — minus the one that's gone."""
    st = await _connect_byo(clients)  # BYO: no provider, so no auto-provisioned tool
    sid = st["secret_id"]
    other = (await clients.post("/secrets", json={"name": "OTHER", "value": "k"})).json()
    mine = (await clients.post("/tools", json={
        "name": "mine", "base_url": "http://upstream",
        "bindings": [{"secret_id": sid}, {"secret_id": other["id"], "name": "X-Other"}],
    })).json()
    def _mine(tools):
        return next(t for t in tools if t["name"] == "mine")

    assert len(_mine((await clients.get("/tools")).json())["bindings"]) == 2

    await clients.delete(f"/connections/{sid}")
    tool = _mine((await clients.get("/tools")).json())
    assert [b["secret_id"] for b in tool["bindings"]] == [other["id"]], "keeps the surviving credential"


# ---- treg's own credentials, not the user's ------------------------------------------------
async def test_platform_credential_is_bound_from_settings_not_an_org_secret(
    clients: AsyncClient, treg_google_app, monkeypatch
):
    """Google Ads needs a developer token that takes WEEKS of Google approval to obtain. Making
    each user supply their own would defeat the point of a hosted registry — so treg supplies it,
    and it must never land in the tenant's secret store where they could read or extract it."""
    from treg import oauth_providers as P
    from treg.config import get_settings

    monkeypatch.setenv("TREG_GOOGLE_ADS_DEVELOPER_TOKEN", "treg-dev-token")
    get_settings.cache_clear()
    try:
        assert P.GOOGLE_ADS.can_autoprovision, "with treg's token, Ads needs nothing from the user"
        await _connect_byo(clients, provider="google-ads", name="google-ads")

        tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "google-ads")
        by_name = {b["name"]: b for b in tool["bindings"]}
        assert by_name["Authorization"]["secret_id"], "the user's OAuth is still per-org"
        dev = by_name["developer-token"]
        assert dev.get("platform_setting") == "google_ads_developer_token"
        assert "secret_id" not in dev or dev["secret_id"] is None

        names = [s["name"] for s in (await clients.get("/secrets")).json()]
        assert not any("developer-token" in n for n in names), \
            "treg's credential must not be copied into the org's secrets"
    finally:
        get_settings.cache_clear()


async def test_without_a_platform_token_the_user_is_asked(clients: AsyncClient, treg_google_app, monkeypatch):
    """A self-hosted deployment with no developer token of its own falls back to prompting."""
    from treg import oauth_providers as P
    from treg.config import get_settings

    monkeypatch.setenv("TREG_GOOGLE_ADS_DEVELOPER_TOKEN", "")
    get_settings.cache_clear()
    try:
        assert P.GOOGLE_ADS.can_autoprovision is False
        st = await _connect_byo(clients, provider="google-ads", name="google-ads")
        conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[st["secret_id"]]
        assert conn["needs_extra_credential"] is True
    finally:
        get_settings.cache_clear()


async def test_id_only_listings_are_enriched_with_real_names(clients: AsyncClient, treg_google_app, monkeypatch):
    """Google Ads lists ["customers/6186675831", …] and nothing else. "6186675831" tells a user
    nothing about which account they're picking, so a provider can declare a per-row name lookup."""
    import dataclasses

    from treg import oauth_providers as P

    monkeypatch.setitem(P.REGISTRY, "google-search-console", dataclasses.replace(
        P.REGISTRY["google-search-console"],
        discover_base_url="http://upstream",
        # the echo upstream reflects the request, so dig a value we know will be there
        enrich_path="/name/{id}", enrich_body={"q": "x"}, enrich_label_path="query.named",
    ))
    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    r = await clients.get(f"/connections/{st['secret_id']}/resources")
    assert r.status_code == 200
    # enrichment ran without breaking the listing; every row still has an id
    assert all(x["id"] for x in r.json()["resources"])


async def test_a_failed_name_lookup_keeps_the_row(clients: AsyncClient, treg_google_app, monkeypatch):
    """A user may lack access to some accounts the listing returned — a partial list beats an
    error, so a failed lookup must leave the row with its id rather than dropping it."""
    import dataclasses

    from treg import oauth_providers as P

    monkeypatch.setitem(P.REGISTRY, "google-search-console", dataclasses.replace(
        P.REGISTRY["google-search-console"],
        discover_base_url="http://upstream",
        enrich_path="/does-not-exist/{id}", enrich_body={}, enrich_label_path="nope.nope",
    ))
    st = await _connect_byo(clients, provider="google-search-console", name="google-search-console")
    r = await clients.get(f"/connections/{st['secret_id']}/resources")
    assert r.status_code == 200
    assert len(r.json()["resources"]) == 2, "rows survive a failed name lookup"


# ---- widening access upgrades the connection, it doesn't clone it --------------------------
async def test_reconnecting_a_provider_replaces_its_connection(clients: AsyncClient, treg_google_app):
    """Two rows for the same provider — one read-only, one write-only — is not a state a user
    should ever be able to reach by clicking "Enable write"."""
    first = await _connect_byo(clients, provider="google-search-console", capability="read")
    second = await _connect_byo(clients, provider="google-search-console", capability="write")

    gsc = [c for c in (await clients.get("/connections")).json() if c["provider"] == "google-search-console"]
    assert len(gsc) == 1, "widening access must upgrade the connection, not add another"
    assert first["secret_id"] == second["secret_id"] == gsc[0]["id"]


async def test_enabling_write_keeps_read(clients: AsyncClient, treg_google_app):
    """A capability is a superset, never a swap — otherwise the connection ends up able to write
    while reporting "no read"."""
    await _connect_byo(clients, provider="google-search-console", capability="read")
    await _connect_byo(clients, provider="google-search-console", capability="write")

    conn = next(c for c in (await clients.get("/connections")).json()
                if c["provider"] == "google-search-console")
    assert set(conn["capabilities"]) == {"read", "write"}
    assert conn["missing_capabilities"] == []


async def test_reconnect_rebinds_the_tool_to_the_same_secret(clients: AsyncClient, treg_google_app):
    await _connect_byo(clients, provider="google-search-console", capability="read")
    await _connect_byo(clients, provider="google-search-console", capability="write")
    tools = [t for t in (await clients.get("/tools")).json() if t["name"] == "google-search-console"]
    conn = next(c for c in (await clients.get("/connections")).json()
                if c["provider"] == "google-search-console")
    assert len(tools) == 1
    assert tools[0]["bindings"][0]["secret_id"] == conn["id"]
