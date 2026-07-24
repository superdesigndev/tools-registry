"""Milestone 3 — the rest of the Google family, Slack, and X.

X is the interesting one: it rejects an authorization code exchanged without a PKCE verifier, and
rejects the client secret in the request body. Both quirks are captured on the pending connect at
start time so the callback exchanges the code exactly the way the consent URL was built.
"""

from __future__ import annotations

import json
from dataclasses import replace
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
    for k in ("GOOGLE", "SLACK", "X", "TIKTOK"):
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
        "google-ads", "youtube", "linkedin", "slack", "x", "tiktok",
        "facebook", "instagram", "meta-ads",
        # API-key providers (auth_kind="key")
        "apollo", "pdl", "akta", "hunter", "tikhub", "brightdata", "semrush",
    }


def test_default_capability_is_the_broadest():
    """Connect asks for the fullest capability; a narrower one is chosen up front, not bolted on
    afterwards. Every provider's write must be a superset of its read for that to be safe."""
    assert P.GOOGLE_SEARCH_CONSOLE.default_capability == "write"
    assert P.X.default_capability == "write"
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


# ---- TikTok's two quirks -------------------------------------------------------------------
async def test_tiktok_consent_url_uses_client_key_not_client_id(clients: AsyncClient, all_apps):
    """TikTok ignores the OAuth2 spelling. Sending `client_id` gets a consent page that errors out
    rather than an obvious 400, so this is worth pinning."""
    q = _q((await clients.post("/oauth/start", json={"provider": "tiktok"})).json())
    assert q["client_key"] == ["tiktok-cid"]
    assert "client_id" not in q


async def test_tiktok_comma_joins_its_scopes(clients: AsyncClient, all_apps):
    """Space-joined scopes come back from TikTok as scope_not_authorized — it splits on commas."""
    q = _q((await clients.post("/oauth/start", json={"provider": "tiktok"})).json())
    scope = q["scope"][0]
    assert "," in scope and " " not in scope
    assert set(scope.split(",")) == set(P.TIKTOK.scopes_for(P.TIKTOK.default_capability))


async def test_tiktok_granted_scopes_are_stored_space_joined(clients: AsyncClient, all_apps, monkeypatch):
    """The wire dialect must not leak into storage: every reader of granted_scopes uses .split(),
    so a comma-joined grant would read as one bogus scope and report every capability unsatisfied."""
    # Registry mode takes token_uri from the provider, not the body, so point the provider itself at
    # the in-process upstream (frozen dataclass → replace rather than setattr).
    monkeypatch.setitem(P.REGISTRY, "tiktok", replace(P.TIKTOK, token_uri="http://upstream/token"))
    body = {"provider": "tiktok", "capability": "post"}
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    sid = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]

    conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]
    assert set(conn["capabilities"]) == {"read", "draft", "post"}


def test_tiktok_capabilities_are_cumulative():
    """draft must contain read and post must contain draft, or satisfied_capabilities() (which is
    set-containment) reports a connection that can post but cannot read."""
    t = P.TIKTOK
    assert set(t.scopes_for("read")) < set(t.scopes_for("draft")) < set(t.scopes_for("post"))
    assert t.default_capability == "post"
    # video.publish is the whole difference between "we drafted it for you" and "we posted it".
    assert "video.publish" not in t.scopes_for("draft")
    assert "video.publish" in t.scopes_for("post")


# ---- per-provider consent params ---------------------------------------------------------
async def test_google_keeps_offline_consent_params(clients: AsyncClient, all_apps):
    """access_type=offline + prompt=consent is what guarantees Google returns a refresh_token."""
    q = _q((await clients.post("/oauth/start", json={"provider": "google-search-console"})).json())
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]


def test_slack_is_bring_your_own_bot():
    """A Slack bot is workspace-scoped and belongs to the workspace it's installed in. A shared
    treg app would sit between a team and their own messages — and couldn't be installed on their
    behalf anyway — so the user brings their own token instead of consenting to ours."""
    assert P.SLACK.auth_kind == "token"
    assert P.SLACK.scopes == {}, "no consent screen means no capability sizing"
    assert P.SLACK.default_capability == "", "and nothing to default to"


async def test_each_provider_uses_its_own_client_credentials(clients: AsyncClient, all_apps):
    for service, expected in (("google-search-console", "google-cid"), ("x", "x-cid")):
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
    monkeypatch.setenv("TREG_X_CLIENT_ID", "")
    monkeypatch.setenv("TREG_X_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        rows = {p["service"]: p for p in (await clients.get("/oauth/providers")).json()}
        assert rows["x"]["configured"] is False
        r = await clients.post("/oauth/start", json={"provider": "x"})
        assert r.status_code == 422 and "not configured" in r.text
        # a bring-your-own-token provider needs nothing from the deployment, so it stays offerable
        assert rows["slack"]["configured"] is True
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


def test_meta_providers_share_one_app():
    """Facebook and Instagram are one Meta app, deliberately.

    App Review, business verification and Tech Provider status attach to the app, so a second
    OAuth client isolates nothing and starts its approval from zero."""
    assert P.FACEBOOK.client_id_setting == P.INSTAGRAM.client_id_setting == "meta_client_id"
    assert P.FACEBOOK.client_secret_setting == P.INSTAGRAM.client_secret_setting == "meta_client_secret"
    assert P.FACEBOOK.base_url == P.INSTAGRAM.base_url


def test_meta_post_contains_read():
    """satisfied_capabilities() is set containment, so a non-cumulative post would report a
    connection that can publish but 'cannot read' — and the default capability would be wrong."""
    for provider in (P.FACEBOOK, P.INSTAGRAM):
        assert set(provider.scopes["read"]) < set(provider.scopes["post"]), provider.service
        assert provider.default_capability == "post", provider.service


def test_instagram_is_reached_through_a_page():
    """An Instagram professional account has no listing endpoint of its own — it hangs off the
    linked Page — so dropping pages_show_list silently empties the account picker."""
    for cap in P.INSTAGRAM.scopes.values():
        assert "pages_show_list" in cap
    assert P.INSTAGRAM.discover_id_field == "instagram_business_account.id"


def test_meta_asks_for_a_long_lived_token():
    """Meta's code exchange yields a ~1-2h token and no refresh_token. Without the second
    exchange every Meta connection dies the day it is made."""
    assert P.FACEBOOK.long_lived_exchange and P.INSTAGRAM.long_lived_exchange
    assert not P.TIKTOK.long_lived_exchange  # nothing else should have picked it up


def test_instagram_consent_never_mentions_page_publishing():
    """Scopes are per capability. An Instagram connect asking for pages_manage_posts would put
    'manage your Pages' posts' on the consent screen for authority it never uses."""
    for cap in P.INSTAGRAM.scopes.values():
        assert "pages_manage_posts" not in cap


def test_meta_ads_needs_no_second_credential():
    """Google Ads is gated on a developer token from an approved manager account; Meta has no
    equivalent, so a Meta Ads connect must yield a callable tool on its own."""
    assert P.META_ADS.can_autoprovision is True
    assert P.META_ADS.needs_extra_credential is False


def test_meta_ads_read_can_still_list_accounts():
    """/me/adaccounts is a Business asset listing. Drop business_management from read and the
    connect consents cleanly, then offers an empty account picker."""
    for cap in P.META_ADS.scopes.values():
        assert "business_management" in cap
    assert set(P.META_ADS.scopes["read"]) < set(P.META_ADS.scopes["manage"])
    assert "ads_management" not in P.META_ADS.scopes["read"], "read must not be able to spend money"
