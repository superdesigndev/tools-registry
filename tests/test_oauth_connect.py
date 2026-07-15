"""Phase C: the hosted OAuth connect flow. start -> (browser consent) -> callback exchanges the
code for tokens and creates the oauth secret -> status reports done. The test upstream's /token
stands in for the provider's token endpoint.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from httpx import AsyncClient

START = {
    "name": "gsc",
    "client_id": "cid",
    "client_secret": "csec",
    "auth_uri": "http://provider/auth",
    "token_uri": "http://upstream/token",  # ASGITransport -> test upstream
    "scopes": ["https://www.googleapis.com/auth/webmasters.readonly"],
}


async def test_start_builds_a_proper_consent_url(clients: AsyncClient):
    d = (await clients.post("/oauth/start", json=START)).json()
    q = parse_qs(urlsplit(d["consent_url"]).query)
    assert q["client_id"] == ["cid"]
    assert q["state"] == [d["state"]]
    assert q["access_type"] == ["offline"]            # so a refresh_token comes back
    assert q["redirect_uri"][0].endswith("/oauth/callback")
    assert q["scope"][0].startswith("https://www.googleapis.com/auth/webmasters")


async def test_callback_exchanges_code_and_creates_oauth_secret(clients: AsyncClient):
    d = (await clients.post("/oauth/start", json=START)).json()
    state = d["state"]

    cb = await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    assert cb.status_code == 200 and "Connected" in cb.text

    st = (await clients.get(f"/oauth/status/{state}")).json()
    assert st["status"] == "done"
    sid = st["secret_id"]

    # the created secret is a working, auto-refreshable oauth credential
    secrets = {s["name"]: s for s in (await clients.get("/secrets")).json()}
    assert secrets["gsc"]["kind"] == "oauth"

    await clients.post("/tools", json={"name": "gsc-tool", "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    r = await clients.get("/call/gsc-tool/echo")
    assert r.json()["auth"] == "Bearer REFRESHED"  # the exchanged access token is injected


async def test_callback_unknown_state_404(clients: AsyncClient):
    r = await clients.get("/call".replace("/call", "/oauth/callback") + "?code=x&state=nope")
    assert r.status_code == 404


async def test_callback_provider_error_marks_error(clients: AsyncClient):
    state = (await clients.post("/oauth/start", json=START)).json()["state"]
    cb = await clients.get(f"/oauth/callback?error=access_denied&state={state}")
    assert cb.status_code == 400
    st = (await clients.get(f"/oauth/status/{state}")).json()
    assert st["status"] == "error" and "access_denied" in st["detail"]
