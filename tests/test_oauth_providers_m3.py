"""Milestone 3 — the rest of the Google family, Slack, and X.

X is the interesting one: it rejects an authorization code exchanged without a PKCE verifier, and
rejects the client secret in the request body. Both quirks are captured on the pending connect at
start time so the callback exchanges the code exactly the way the consent URL was built.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import oauth
from treg import oauth_providers as P
from treg.config import get_settings
from treg.db import session_maker
from treg.models import PendingOAuth


@pytest.fixture
def all_apps(monkeypatch):
    for k in ("GOOGLE", "SLACK", "X"):
        monkeypatch.setenv(f"TREG_{k}_CLIENT_ID", f"{k.lower()}-cid")
        monkeypatch.setenv(f"TREG_{k}_CLIENT_SECRET", f"{k.lower()}-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _q(payload: dict) -> dict:
    return parse_qs(urlsplit(payload["consent_url"]).query)


# ---- registry shape ----------------------------------------------------------------------
def test_every_provider_is_registered():
    assert set(P.REGISTRY) == {
        "google-search-console", "google-analytics", "google-business-profile",
        "google-ads", "linkedin", "slack", "x",
    }


def test_default_capability_is_the_broadest():
    """Connect asks for the fullest capability; a narrower one is chosen up front, not bolted on
    afterwards. Every provider's write must be a superset of its read for that to be safe."""
    assert P.GOOGLE_SEARCH_CONSOLE.default_capability == "write"
    assert P.X.default_capability == "write"
    assert P.SLACK.default_capability == "write"
    assert P.GOOGLE_ADS.default_capability == "manage"  # it has no read-only mode
    for provider in P.REGISTRY.values():
        caps = provider.capabilities
        if "read" in caps and "write" in caps:
            assert set(provider.scopes["read"]) < set(provider.scopes["write"]), provider.service


def test_google_ads_refuses_to_autoprovision():
    """Ads needs a developer-token header too; a bearer-only tool would 401 on first use."""
    assert P.GOOGLE_ADS.can_autoprovision is False
    assert "developer-token" in P.GOOGLE_ADS.extra_credential_note
    assert P.GOOGLE_SEARCH_CONSOLE.can_autoprovision is True


def test_x_write_keeps_offline_access():
    """Without offline.access the token can't be refreshed and every X connection becomes a
    manual-reconnect chore within hours."""
    assert "offline.access" in P.X.scopes_for("write")
    assert "offline.access" in P.X.scopes_for("read")


# ---- X's two quirks ----------------------------------------------------------------------
async def test_x_consent_url_carries_a_pkce_challenge(clients: AsyncClient, all_apps):
    d = (await clients.post("/oauth/start", json={"provider": "x"})).json()
    q = _q(d)
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"], "X rejects a code exchanged without a verifier"
    # the verifier itself must stay server-side
    assert "code_verifier" not in q


async def test_pkce_challenge_matches_the_stored_verifier(clients: AsyncClient, all_apps):
    d = (await clients.post("/oauth/start", json={"provider": "x"})).json()
    challenge = _q(d)["code_challenge"][0]
    async with session_maker() as db:
        p = (await db.execute(select(PendingOAuth).where(PendingOAuth.state == d["state"]))).scalars().one()
    assert p.code_verifier
    assert oauth.pkce_challenge(p.code_verifier) == challenge
    assert p.token_endpoint_auth_method == "client_secret_basic"


async def test_google_does_not_use_pkce(clients: AsyncClient, all_apps):
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    assert "code_challenge" not in _q(d)


# ---- per-provider consent params ---------------------------------------------------------
async def test_google_keeps_offline_consent_params(clients: AsyncClient, all_apps):
    """access_type=offline + prompt=consent is what guarantees Google returns a refresh_token."""
    q = _q((await clients.post("/oauth/start", json={"provider": "google-search-console"})).json())
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]


async def test_slack_does_not_get_googles_params(clients: AsyncClient, all_apps):
    """Slack rejects them; sending Google's defaults everywhere would break the flow."""
    q = _q((await clients.post("/oauth/start", json={"provider": "slack"})).json())
    assert "access_type" not in q and "prompt" not in q


async def test_each_provider_uses_its_own_client_credentials(clients: AsyncClient, all_apps):
    for service, expected in (("google-search-console", "google-cid"), ("slack", "slack-cid"), ("x", "x-cid")):
        q = _q((await clients.post("/oauth/start", json={"provider": service})).json())
        assert q["client_id"] == [expected], service


# ---- scope gap detection (the re-consent trigger) -----------------------------------------
def test_satisfied_capabilities_detects_a_scope_gap():
    """Providers never backfill scopes onto an issued grant — a later capability needs re-consent,
    and this is how we know to prompt instead of letting the call 403."""
    gsc = P.GOOGLE_SEARCH_CONSOLE
    read_only = gsc.scopes_for("read")
    assert gsc.satisfied_capabilities(read_only) == ["read"]
    assert "write" not in gsc.satisfied_capabilities(read_only)
    both = read_only + gsc.scopes_for("write")
    assert set(gsc.satisfied_capabilities(both)) == {"read", "write"}


async def test_unconfigured_providers_are_listed_but_flagged(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_SLACK_CLIENT_ID", "")
    monkeypatch.setenv("TREG_SLACK_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        rows = {p["service"]: p for p in (await clients.get("/oauth/providers")).json()}
        assert rows["slack"]["configured"] is False
        r = await clients.post("/oauth/start", json={"provider": "slack"})
        assert r.status_code == 422 and "not configured" in r.text
    finally:
        get_settings.cache_clear()


# ---- the gap, surfaced on the connection --------------------------------------------------
async def test_connection_reports_the_capability_it_lacks(clients: AsyncClient, all_apps):
    """Connect read-only, then see that `write` is named as missing — the reconnect trigger."""
    body = {
        "provider": "google-search-console", "capability": "read",
        "token_uri": "http://upstream/token",  # the in-process upstream stands in for Google
    }
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    sid = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]

    conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]
    assert conn["capabilities"] == ["read"]
    assert conn["missing_capabilities"] == ["write"]


async def test_byo_connection_has_no_capability_fields(clients: AsyncClient):
    body = {"name": "byo", "client_id": "c", "client_secret": "s",
            "auth_uri": "http://p/auth", "token_uri": "http://upstream/token", "scopes": ["x"]}
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    sid = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]
    conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]
    assert "missing_capabilities" not in conn  # nothing to compare against without a provider


# ---- LinkedIn -----------------------------------------------------------------------------
def test_linkedin_has_one_capability():
    """These scopes let a member read their own profile and post as themselves. A read-only
    LinkedIn connection could do nothing but identify you, so there is no second option worth
    asking about — and a dialog with one real choice is just friction."""
    assert P.LINKEDIN.capabilities == ["write"]
    assert "w_member_social" in P.LINKEDIN.scopes_for("write")


async def test_linkedin_does_not_get_googles_consent_params(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_LINKEDIN_CLIENT_ID", "li-cid")
    monkeypatch.setenv("TREG_LINKEDIN_CLIENT_SECRET", "li-csec")
    get_settings.cache_clear()
    try:
        d = (await clients.post("/oauth/start", json={"provider": "linkedin"})).json()
        q = _q(d)
        assert q["client_id"] == ["li-cid"]
        assert "access_type" not in q and "prompt" not in q, "LinkedIn rejects Google's params"
        assert "w_member_social" in q["scope"][0]
    finally:
        get_settings.cache_clear()
