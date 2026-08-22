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
    # The product is branded `treg` everywhere since the rename; asserting the old name left main
    # red and every PR failing CI behind it.
    assert "treg" in r.text and "Sign in" in r.text
    r = await clients.get("/app")
    assert r.status_code == 200
    # the app root div may carry extra attributes (e.g. v-cloak) — match the id, not the exact tag
    assert "treg" in r.text and 'id="app"' in r.text
    # deep links (invite flows etc.) carry query params and still reach the SPA at /
    r = await clients.get("/?invite=x%40y.z")
    assert r.status_code == 200 and 'id="app"' in r.text


async def test_meta_reports_the_released_version(clients: AsyncClient):
    """`app_version` is a hash of index.html — it answers "has the dashboard bundle changed?", which
    is what an open tab compares to offer a refresh. It is NOT the release version, and after
    publishing 0.9.0 there was no way to confirm from the live path which version was serving.

    Both are kept because they answer different questions."""
    body = (await clients.get("/meta")).json()
    assert body["treg_version"], "a release check must be able to read the version from outside"
    assert body["treg_version"] != body["app_version"], "these are different questions"
    assert body["treg_version"][0].isdigit(), f"expected a version, got {body['treg_version']!r}"


def test_the_pool_cannot_outnumber_postgres_during_a_deploy():
    """A rolling deploy runs TWO instances against one database. At 20+40 each, that was 120
    potential connections against a basic-plan ceiling of ~100 — a deploy could starve Postgres with
    no bug anywhere, which is exactly what happened on 2026-08-15. Two instances of the current
    numbers must stay comfortably under a 97-connection ceiling."""
    import re
    from pathlib import Path

    import treg.db as db

    src = Path(db.__file__).read_text()
    m = re.search(r"pool_size=(\d+), max_overflow=(\d+)", src)
    assert m, "expected explicit pool bounds for the postgres path"
    per_instance = int(m.group(1)) + int(m.group(2))
    assert per_instance * 2 <= 90, (
        f"two deploy-time instances could hold {per_instance * 2} connections — "
        "that is how the 2026-08-15 outage started")


def test_migrations_fail_fast_rather_than_queueing_the_world():
    """An ALTER on a hot table needs an exclusive lock. Without a lock_timeout it QUEUES behind live
    traffic, and every new query then queues behind IT — both instances starve and the shared
    database wedges (2026-08-15, root cause). The startup path must set the timeout before running
    migrations so a contended deploy fails cleanly instead."""
    import inspect as _inspect

    import treg.db as db

    src = _inspect.getsource(db.init_db)
    assert "lock_timeout" in src, "init_db must set lock_timeout before _migrate_to_orgs"
    assert src.index("lock_timeout") < src.index("_migrate_to_orgs"), (
        "the timeout must be set BEFORE the migrations run")
