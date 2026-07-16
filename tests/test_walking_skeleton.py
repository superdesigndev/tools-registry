"""Step 1 proof: register an ENV-secret tool, call it through the proxy, and confirm the
registry injected the credential the caller never held — and never leaked the secret back.

The echo upstream + authed `clients` fixture live in conftest.py.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from treg.api import app


async def _register_posthog_like(c: AsyncClient, *, auth_in="header") -> str:
    s = await c.post("/secrets", json={"name": "phx", "value": "test-secret-123"})
    assert s.status_code == 200, s.text
    secret_id = s.json()["id"]

    name = f"echo-{auth_in}"
    t = await c.post(
        "/tools",
        json={
            "name": name,
            "base_url": "http://upstream",
            "secret_id": secret_id,
            "auth_in": auth_in,
            "auth_name": "api_key" if auth_in == "query" else "Authorization",
            "auth_format": "{secret}" if auth_in == "query" else "Bearer {secret}",
        },
    )
    assert t.status_code == 200, t.text
    return name


async def test_proxy_injects_header_credential(clients: AsyncClient):
    name = await _register_posthog_like(clients, auth_in="header")
    r = await clients.get(f"/call/{name}/echo")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer test-secret-123"  # injected by the registry


async def test_proxy_injects_query_credential_and_relays_body(clients: AsyncClient):
    name = await _register_posthog_like(clients, auth_in="query")
    r = await clients.post(f"/call/{name}/echo?foo=bar", content=b"hello-body")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["query"]["api_key"] == "test-secret-123"  # injected into query
    assert data["query"]["foo"] == "bar"  # caller's own params preserved
    assert data["body"] == "hello-body"  # body relayed verbatim


async def test_proxy_secret_file_injects_extracted_token(clients: AsyncClient):
    # A `.secret`-style JSON token file (GSC/GCP shape): registry pulls access_token + injects it.
    s = await clients.post(
        "/secrets",
        json={"name": "gsc", "kind": "secret_file", "value": '{"access_token": "AT-XYZ", "refresh_token": "r"}'},
    )
    sid = s.json()["id"]
    t = await clients.post(
        "/tools",
        json={"name": "gsc-tool", "base_url": "http://upstream", "secret_id": sid, "injector": "secret_file"},
    )
    assert t.status_code == 200, t.text
    r = await clients.get("/call/gsc-tool/echo")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer AT-XYZ"  # extracted from the JSON blob, caller never held it


async def test_secret_value_never_returned(clients: AsyncClient):
    await _register_posthog_like(clients)
    r = await clients.get("/secrets")
    assert r.status_code == 200
    assert all("value" not in row for row in r.json())


async def test_auth_required():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.get("/tools")  # no token
        assert r.status_code == 401


async def test_unknown_tool_404(clients: AsyncClient):
    r = await clients.get("/call/nope/echo")
    assert r.status_code == 404


async def test_dashboard_served_at_root(clients: AsyncClient):
    # `/` is the marketing landing; the SPA (login shell + dashboard) lives at /app.
    r = await clients.get("/")
    assert r.status_code == 200
    assert "tools-registry" in r.text and "Sign in" in r.text
    r = await clients.get("/app")
    assert r.status_code == 200
    # the app root div may carry extra attributes (e.g. v-cloak) — match the id, not the exact tag
    assert "tools-registry" in r.text and 'id="app"' in r.text
    # deep links (invite flows etc.) carry query params and still reach the SPA at /
    r = await clients.get("/?invite=x%40y.z")
    assert r.status_code == 200 and 'id="app"' in r.text
