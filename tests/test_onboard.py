"""Onboarding — the demo-team provisioner + /onboard/* endpoints.

Seeds a real team owned by the caller, populated with fake teammates (roster-only), a working
tool, and sample activity; is idempotent; skippable; resettable; keeps demo data out of platform
stats; and refuses login for the fake-teammate domain.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import make_upstream

from treg.api import app
from treg.db import reset_db, session_maker
from treg.models import CallRecord, Membership, Org, Secret, Tool, User

ADMIN = "ENV-ADMIN-SECRET"


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


@pytest.fixture
async def c(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    from treg.config import get_settings
    get_settings.cache_clear()
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()
    get_settings.cache_clear()


async def _register(c: AsyncClient, email: str) -> str:
    return (await c.post("/users", json={"email": email})).json()["token"]


async def test_onboard_seeds_a_populated_team(c):
    tok = await _register(c, "founder@x.io")
    r = await c.post("/onboard/demo", json={"team_name": "Acme Design"}, headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    assert d["org"] == "acme-design" and d["tool"] == "echo" and len(d["teammates"]) == 3

    # roster = owner + the three role-laddered teammates; tool bound to a secret; live audit trail.
    # (assert against the DB — the founder's personal-org token can't act in the new demo org.)
    oid = d["org_id"]
    async with session_maker() as s:
        mems = (await s.execute(select(Membership).where(Membership.org_id == oid))).scalars().all()
        users = {u.id: u for u in (await s.execute(select(User))).scalars().all()}
        roles = {users[m.user_id].email: m.role for m in mems}
        assert roles["founder@x.io"] == "owner"
        assert roles["ada@demo.treg.local"] == "admin"
        assert roles["ben@demo.treg.local"] == "member"
        assert roles["cora@demo.treg.local"] == "viewer"

        tool = (await s.execute(select(Tool).where(Tool.org_id == oid, Tool.name == "echo"))).scalar_one()
        assert tool.bindings and tool.bindings[0]["name"] == "Authorization"
        assert (await s.execute(select(Secret).where(Secret.org_id == oid, Secret.name == "echo-key"))).scalar_one()
        calls = (await s.execute(select(CallRecord).where(CallRecord.org_id == oid))).scalars().all()
        assert len(calls) >= 3

    # caller is now marked onboarded
    assert (await c.get("/auth/me", headers=_h(tok))).json()["onboarded"] is True


async def test_onboard_is_idempotent(c):
    tok = await _register(c, "f2@x.io")
    a = (await c.post("/onboard/demo", headers=_h(tok))).json()
    b = (await c.post("/onboard/demo", headers=_h(tok))).json()
    assert b["reused"] is True and a["org"] == b["org"]
    # exactly one demo org, three demo teammates (not doubled)
    async with session_maker() as s:
        assert len((await s.execute(select(Org).where(Org.demo == True))).scalars().all()) == 1  # noqa: E712
        assert len((await s.execute(select(User).where(User.demo == True))).scalars().all()) == 3  # noqa: E712


async def test_skip_marks_onboarded_without_seeding(c):
    tok = await _register(c, "skip@x.io")
    assert (await c.post("/onboard/skip", headers=_h(tok))).json()["onboarded"] is True
    assert (await c.get("/auth/me", headers=_h(tok))).json()["onboarded"] is True
    async with session_maker() as s:
        assert (await s.execute(select(Org).where(Org.demo == True))).first() is None  # noqa: E712


async def test_reset_removes_demo_footprint(c):
    tok = await _register(c, "r@x.io")
    d = (await c.post("/onboard/demo", headers=_h(tok))).json()
    rr = await c.post("/onboard/reset", headers=_h(tok))
    assert d["org"] in rr.json()["removed"]
    async with session_maker() as s:
        assert (await s.execute(select(Org).where(Org.demo == True))).first() is None  # noqa: E712
        assert (await s.execute(select(User).where(User.demo == True))).first() is None  # noqa: E712


async def test_demo_teammate_cannot_request_a_login_code(c):
    r = await c.post("/auth/email/start", json={"email": "ada@demo.treg.local"})
    assert r.status_code == 400


async def test_seed_tool_and_accept_teammate(c):
    # the dashboard narrative: the user creates a REAL team, seeds the echo tool, and invites a
    # demo teammate that auto-joins. Use an IDENTITY token (like the dashboard session) so X-Treg-Org
    # resolves the active team across orgs — a per-org token is bound to one org.
    code = (await c.post("/auth/email/start", json={"email": "builder@x.io"})).json()["dev_code"]
    tok = (await c.post("/auth/email/verify", json={"email": "builder@x.io", "code": code})).json()["token"]
    org = (await c.post("/orgs", json={"name": "Acme"}, headers=_h(tok))).json()  # first team: identity token only, no org yet
    oh = {**_h(tok), "X-Treg-Org": org["org"]}
    # seed the tool
    st = await c.post("/onboard/seed-tool", headers=oh)
    assert st.status_code == 200 and st.json()["tool"] == "echo"
    assert any(t["name"] == "echo" for t in (await c.get("/tools", headers=oh)).json())
    # user invites the demo teammate, then onboarding auto-accepts it
    await c.post(f"/orgs/{org['org_id']}/invites", json={"email": "alex@demo.treg.local", "role": "member"}, headers=oh)
    at = await c.post("/onboard/accept-teammate", json={"email": "alex@demo.treg.local"}, headers=oh)
    assert at.status_code == 200
    members = {m["email"]: m["role"] for m in (await c.get(f"/orgs/{org['org_id']}/members", headers=oh)).json()}
    assert members.get("alex@demo.treg.local") == "member"  # joined instantly
    # accept-teammate refuses a non-demo email
    await c.post(f"/orgs/{org['org_id']}/invites", json={"email": "real@person.com", "role": "member"}, headers=oh)
    bad = await c.post("/onboard/accept-teammate", json={"email": "real@person.com"}, headers=oh)
    assert bad.status_code == 400


async def test_demo_footprint_excluded_from_admin_stats(c):
    tok = await _register(c, "real@x.io")  # 1 real user + a personal org
    before = (await c.get("/admin/stats", headers=_h(ADMIN))).json()["totals"]
    await c.post("/onboard/demo", headers=_h(tok))  # +3 demo users, +1 demo org, +1 tool, +3 calls
    after = (await c.get("/admin/stats", headers=_h(ADMIN))).json()["totals"]
    assert after["users"] == before["users"]  # the three fake teammates don't count
    assert after["orgs"] == before["orgs"]     # the demo team doesn't count
    assert after["tools"] == before["tools"]   # nor its tool
