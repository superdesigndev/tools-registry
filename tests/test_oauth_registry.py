"""The curated OAuth provider registry (oauth_providers.py).

BYO mode stays exactly as it was: the caller brings client_id/secret/URIs. REGISTRY mode is the
other half — name a provider and treg's own approved app supplies the credentials, requesting
only the scopes the chosen capability actually needs.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient

from treg.config import get_settings


@pytest.fixture
def treg_google_app(monkeypatch):
    """treg's own Google client — what a deployment sets to offer registry connects."""
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "treg-google-cid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "treg-google-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _consent_query(payload: dict) -> dict:
    return parse_qs(urlsplit(payload["consent_url"]).query)


async def test_providers_endpoint_lists_the_registry(clients: AsyncClient, treg_google_app):
    rows = {p["service"]: p for p in (await clients.get("/oauth/providers")).json()}
    gsc = rows["google-search-console"]
    assert gsc["configured"] is True
    assert gsc["capabilities"] == ["read", "write"]
    assert gsc["base_url"] == "https://searchconsole.googleapis.com"


async def test_registry_connect_uses_tregs_own_app(clients: AsyncClient, treg_google_app):
    """The caller supplies no credentials at all — that is the whole point."""
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    q = _consent_query(d)
    assert q["client_id"] == ["treg-google-cid"]
    assert q["redirect_uri"][0].endswith("/oauth/callback")
    assert q["state"] == [d["state"]]


async def test_the_broadest_capability_is_the_default(clients: AsyncClient, treg_google_app):
    """A plain Connect asks for write. Least-privilege-by-default meant most users had to connect
    twice — once for read, then again to widen it — which is worse than one honest consent screen."""
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    scope = _consent_query(d)["scope"][0]
    assert "webmasters" in scope and "webmasters.readonly" in scope


async def test_choosing_read_narrows_the_request(clients: AsyncClient, treg_google_app):
    """The choice is made BEFORE consent — a user who only wants read says so up front."""
    d = (await clients.post(
        "/oauth/start", json={"provider": "google-search-console", "capability": "read"}
    )).json()
    assert _consent_query(d)["scope"] == ["https://www.googleapis.com/auth/webmasters.readonly"]


async def test_capabilities_are_cumulative(clients: AsyncClient, treg_google_app):
    """write CONTAINS read. Otherwise picking write would silently cost you read access."""
    from treg import oauth_providers as P
    g = P.GOOGLE_SEARCH_CONSOLE
    assert set(g.scopes_for("read")) < set(g.scopes_for("write"))
    assert g.satisfied_capabilities(g.scopes_for("write")) == ["read", "write"]


async def test_a_search_console_connect_never_requests_ads_or_analytics(
    clients: AsyncClient, treg_google_app
):
    """Per-capability scopes exist so a user connecting Search Console is never shown
    "See, edit, create, and delete your Google Ads accounts and data"."""
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    scope = _consent_query(d)["scope"][0]
    assert "adwords" not in scope
    assert "analytics" not in scope
    assert "business.manage" not in scope


async def test_secret_name_defaults_to_the_service(clients: AsyncClient, treg_google_app):
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    st = (await clients.get(f"/oauth/status/{d['state']}")).json()
    assert st["name"] == "google-search-console"


async def test_an_explicit_name_still_wins(clients: AsyncClient, treg_google_app):
    d = (await clients.post(
        "/oauth/start", json={"provider": "google-search-console", "name": "my-gsc"}
    )).json()
    st = (await clients.get(f"/oauth/status/{d['state']}")).json()
    assert st["name"] == "my-gsc"


async def test_unknown_provider_is_404(clients: AsyncClient, treg_google_app):
    r = await clients.post("/oauth/start", json={"provider": "nope"})
    assert r.status_code == 404
    assert "google-search-console" in r.text  # the error lists what IS known


async def test_unconfigured_provider_is_a_clear_422(clients: AsyncClient, monkeypatch):
    """A deployment that never set treg's Google client should say so, not fail mid-consent."""
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        r = await clients.post("/oauth/start", json={"provider": "google-search-console"})
        assert r.status_code == 422
        assert "not configured" in r.text
        listed = {p["service"]: p for p in (await clients.get("/oauth/providers")).json()}
        assert listed["google-search-console"]["configured"] is False
    finally:
        get_settings.cache_clear()


async def test_unknown_capability_is_422(clients: AsyncClient, treg_google_app):
    r = await clients.post(
        "/oauth/start", json={"provider": "google-search-console", "capability": "nope"}
    )
    assert r.status_code == 422
    assert "no capability" in r.text


async def test_byo_mode_is_unchanged(clients: AsyncClient):
    """The generic path must keep working with no provider named."""
    d = (await clients.post("/oauth/start", json={
        "name": "byo", "client_id": "cid", "client_secret": "csec",
        "auth_uri": "http://provider/auth", "token_uri": "http://upstream/token",
        "scopes": ["https://example.com/scope"],
    })).json()
    q = _consent_query(d)
    assert q["client_id"] == ["cid"]
    assert q["scope"] == ["https://example.com/scope"]


async def test_neither_provider_nor_credentials_is_422(clients: AsyncClient):
    r = await clients.post("/oauth/start", json={"name": "x"})
    assert r.status_code == 422
    assert "provider" in r.text


def test_every_requested_scope_has_a_plain_english_label():
    """The marketplace shows permissions in plain English so a human can decide whether to grant
    them. A provider added with new scopes and no copy would silently fall back to raw URLs."""
    from treg import oauth_providers as P
    unlabelled = sorted({
        sc for prov in P.REGISTRY.values() for scopes in prov.scopes.values() for sc in scopes
        if sc not in P.SCOPE_LABELS
    })
    assert not unlabelled, f"no plain-English label for: {unlabelled}"


def test_every_provider_has_a_card_summary():
    """A card with no summary is a logo and a name — nothing to decide from."""
    from treg import oauth_providers as P
    assert not [p.service for p in P.REGISTRY.values() if not p.summary]


def test_scope_label_falls_back_rather_than_raising():
    """Slack grants implied scopes we never asked for. Showing one raw string beats 500ing the
    whole connection page over unfamiliar copy."""
    from treg import oauth_providers as P
    assert P.scope_label("some:unknown:scope") == "some:unknown:scope"
