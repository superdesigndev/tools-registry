"""Step 3: per-user auth, full CRUD, and the fire-and-forget audit record.

Reuses the in-process echo upstream + authed client fixture from the walking-skeleton tests.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.db import reset_db


# ---- registration + auth ------------------------------------------------------------------
async def test_register_returns_token_once_and_dedupes_email():
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.post("/users", json={"email": "a@x.dev"})
        assert r.status_code == 200
        assert r.json()["token"]  # shown exactly once
        again = await c.post("/users", json={"email": "a@x.dev"})
        assert again.status_code == 409  # email already registered


async def test_bad_token_rejected(clients: AsyncClient):
    r = await clients.get("/tools", headers={"X-Treg-Token": "garbage"})
    assert r.status_code == 401


# ---- secret CRUD --------------------------------------------------------------------------
async def test_secret_crud_and_owner_stamp(clients: AsyncClient):
    s = await clients.post("/secrets", json={"name": "k", "value": "v1"})
    sid = s.json()["id"]
    assert s.json()["owner"] == "tim@superdesign.dev"  # stamped from the caller's identity

    # rotate the value (re-encrypted server-side; value never returned)
    u = await clients.patch(f"/secrets/{sid}", json={"value": "v2", "name": "k2"})
    assert u.status_code == 200
    assert u.json()["name"] == "k2"
    assert "value" not in u.json()

    d = await clients.delete(f"/secrets/{sid}")
    assert d.status_code == 200
    assert (await clients.patch(f"/secrets/{sid}", json={"name": "x"})).status_code == 404


async def test_cannot_delete_secret_in_use(clients: AsyncClient):
    s = await clients.post("/secrets", json={"name": "k", "value": "v"})
    sid = s.json()["id"]
    await clients.post("/tools", json={"name": "t1", "base_url": "http://upstream", "secret_id": sid})
    r = await clients.delete(f"/secrets/{sid}")
    assert r.status_code == 409  # referenced by a tool


# ---- tool CRUD ----------------------------------------------------------------------------
async def test_tool_crud_and_duplicate_name(clients: AsyncClient):
    s = await clients.post("/secrets", json={"name": "k", "value": "v"})
    sid = s.json()["id"]
    t = await clients.post("/tools", json={"name": "t", "base_url": "http://upstream", "secret_id": sid})
    tid = t.json()["id"]
    assert t.json()["owner"] == "tim@superdesign.dev"

    dup = await clients.post("/tools", json={"name": "t", "base_url": "http://x", "secret_id": sid})
    assert dup.status_code == 409  # unique tool name

    u = await clients.patch(f"/tools/{tid}", json={"base_url": "http://changed"})
    assert u.json()["base_url"] == "http://changed"

    bad = await clients.patch(f"/tools/{tid}", json={"bindings": [{"secret_id": 9999, "injector": "env"}]})
    assert bad.status_code == 422  # a binding's secret must exist

    assert (await clients.delete(f"/tools/{tid}")).status_code == 200
    assert (await clients.delete(f"/tools/{tid}")).status_code == 404


# ---- audit --------------------------------------------------------------------------------
async def test_call_is_audited(clients: AsyncClient):
    from treg import audit

    s = await clients.post("/secrets", json={"name": "k", "value": "sek"})
    sid = s.json()["id"]
    await clients.post("/tools", json={"name": "echo-tool", "base_url": "http://upstream", "secret_id": sid})

    r = await clients.get("/call/echo-tool/echo?x=1")
    assert r.status_code == 200
    await audit.drain()  # flush the fire-and-forget write before asserting

    calls = (await clients.get("/calls")).json()
    assert len(calls) == 1
    rec = calls[0]
    assert rec["tool_name"] == "echo-tool"
    assert rec["user_email"] == "tim@superdesign.dev"
    assert rec["method"] == "GET"
    assert rec["status_code"] == 200
