"""Per-member tool access control + local-run toggle (the pre-release ACL).

Proves: `tool_access` gates the proxy call AND both run tiers; `local_run_enabled` gates the local tier;
NULL access = all tools; the owner is never restricted; the access PATCH validates tool names and collapses
'all checked' to NULL; and an invite carries its access onto the accepted membership.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg import crypto
from treg.api import app
from treg.db import reset_db, session_maker
from treg.models import Membership, Org, User


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


async def _mint(email: str, org_id: int, role: str) -> tuple[str, int]:
    token = crypto.new_token()
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u is None:
            u = User(email=email); s.add(u); await s.flush()
        s.add(Membership(user_id=u.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await s.commit()
        uid = u.id
    return token, uid


@pytest.fixture
async def env():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team"); s.add(org); await s.commit(); await s.refresh(org)
            org_id = org.id
        owner, _ = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        # a callable HTTP tool (alpha) + a runnable CLI tool (beta)
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "a-key", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(owner), json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        await c.post("/skills", headers=_h(owner), json={
            "name": "beta", "recipe": "# beta",
            "secrets": [{"local_name": "k", "kind": "env", "value": "s3cr"}],
            "tools": [{"name": "beta", "base_url": "http://upstream",
                       "cli": {"bin": "sh", "inject": [{"secret": "k", "via": "env", "name": "K"}], "enabled": True}}]})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, member=member, member_uid=member_uid)
    await app.state.http.aclose()


async def _set_access(env, tool_access, local=True):
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/access",
                          headers=_h(env.owner), json={"tool_access": tool_access, "local_run_enabled": local})
    assert r.status_code == 200, r.text
    return r.json()


async def test_tool_access_gates_call_and_run(env):
    await _set_access(env, ["alpha"])                       # member may use ONLY alpha
    assert (await env.c.get("/call/alpha", headers=_h(env.member))).status_code != 403  # allowed
    assert (await env.c.get("/call/beta", headers=_h(env.member))).status_code == 403   # denied tool
    # both run tiers on the denied tool are blocked too
    assert (await env.c.post("/run", headers=_h(env.member), json={"tool": "beta"})).status_code == 403
    g = await env.c.post("/tools/beta/grant", headers=_h(env.member), json={"argv": []})
    assert g.status_code == 403 and "access" in g.json()["detail"]


async def test_null_access_allows_all(env):
    # default membership has NULL tool_access → every tool is usable
    assert (await env.c.get("/call/alpha", headers=_h(env.member))).status_code != 403
    assert (await env.c.get("/call/beta", headers=_h(env.member))).status_code != 403


async def test_local_run_toggle_blocks_only_local(env):
    await _set_access(env, None, local=False)              # all tools, but no LOCAL runs
    g = await env.c.post("/tools/beta/grant", headers=_h(env.member), json={"argv": []})
    assert g.status_code == 403 and "local execution is disabled" in g.json()["detail"]
    # re-enabling passes the local-tier gate (a later 403 would be the shared-key proof, not this)
    await _set_access(env, None, local=True)
    g2 = await env.c.post("/tools/beta/grant", headers=_h(env.member), json={"argv": []})
    assert "local execution is disabled" not in g2.text


async def test_set_access_validates_and_collapses(env):
    bad = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/access",
                            headers=_h(env.owner), json={"tool_access": ["nope"], "local_run_enabled": True})
    assert bad.status_code == 422 and "nope" in bad.json()["detail"]
    # selecting EVERY tool collapses to 'all' (NULL) so new tools auto-apply
    out = await _set_access(env, ["alpha", "beta"])
    assert out["tool_access"] is None


async def test_owner_cannot_be_restricted(env):
    owner_uid = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    oid = next(m["user_id"] for m in owner_uid if m["role"] == "owner")
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{oid}/access",
                          headers=_h(env.owner), json={"tool_access": ["alpha"], "local_run_enabled": True})
    assert r.status_code == 403


async def test_list_members_returns_access(env):
    await _set_access(env, ["alpha"], local=False)
    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    me = next(m for m in members if m["user_id"] == env.member_uid)
    assert me["tool_access"] == ["alpha"] and me["local_run_enabled"] is False


async def test_invite_carries_access_onto_membership(env):
    inv = await env.c.post(f"/orgs/{env.org_id}/invites", headers=_h(env.owner),
                           json={"email": "new@x.dev", "role": "member",
                                 "tool_access": ["alpha"], "local_run_enabled": False})
    assert inv.status_code == 200, inv.text
    acc = await env.c.post("/invites/accept", json={"code": inv.json()["code"], "email": "new@x.dev"})
    assert acc.status_code == 200, acc.text
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == "new@x.dev"))).scalar_one()
        m = (await s.execute(select(Membership).where(Membership.user_id == u.id))).scalar_one()
        assert m.tool_access == ["alpha"] and m.local_run_enabled is False


# ---- heavy confirming pass: adversarial edge cases ----------------------------------------
async def test_viewer_is_also_tool_gated(env):
    # a viewer can call (not run); the tool ACL still applies to their proxy calls
    viewer, vuid = await _mint("v@x.dev", env.org_id, "viewer")
    await env.c.patch(f"/orgs/{env.org_id}/members/{vuid}/access", headers=_h(env.owner),
                      json={"tool_access": ["alpha"], "local_run_enabled": True})
    assert (await env.c.get("/call/alpha", headers=_h(viewer))).status_code != 403
    assert (await env.c.get("/call/beta", headers=_h(viewer))).status_code == 403


async def test_admin_can_be_restricted_but_owner_cannot(env):
    admin, auid = await _mint("a@x.dev", env.org_id, "admin")
    await env.c.patch(f"/orgs/{env.org_id}/members/{auid}/access", headers=_h(env.owner),
                      json={"tool_access": ["alpha"], "local_run_enabled": True})
    assert (await env.c.get("/call/beta", headers=_h(admin))).status_code == 403   # admins ARE gated
    assert (await env.c.get("/call/beta", headers=_h(env.owner))).status_code != 403  # owner never is


async def test_member_cannot_set_access(env):
    # granting access is admin+ — a plain member can't widen their own access
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/access", headers=_h(env.member),
                          json={"tool_access": None, "local_run_enabled": True})
    assert r.status_code == 403


async def test_empty_list_blocks_every_tool(env):
    await _set_access(env, [])   # no tools → effectively CLI + proxy disabled for this member
    assert (await env.c.get("/call/alpha", headers=_h(env.member))).status_code == 403
    assert (await env.c.get("/call/beta", headers=_h(env.member))).status_code == 403
    assert (await env.c.post("/run", headers=_h(env.member), json={"tool": "alpha"})).status_code == 403


async def test_tool_access_hides_listings_and_keys(env):
    """The ACL isn't just a call gate — a restricted member's dashboard listings must not reveal
    the tools and credentials they can't use (/tools, /secrets, /health, /tools/by-name)."""
    await _set_access(env, ["alpha"])
    m = _h(env.member)
    assert {t["name"] for t in (await env.c.get("/tools", headers=m)).json()} == {"alpha"}
    assert {s["name"] for s in (await env.c.get("/secrets", headers=m)).json()} == {"a-key"}
    assert {h["name"] for h in (await env.c.get("/health", headers=m)).json()} == {"a-key"}
    assert (await env.c.get("/tools/by-name/alpha", headers=m)).status_code == 200
    assert (await env.c.get("/tools/by-name/beta", headers=m)).status_code == 403  # names the fix, not a fake 404
    # the owner and an unrestricted member still see everything
    assert {t["name"] for t in (await env.c.get("/tools", headers=_h(env.owner))).json()} == {"alpha", "beta"}
    await _set_access(env, None)
    assert {t["name"] for t in (await env.c.get("/tools", headers=m)).json()} == {"alpha", "beta"}
    assert {s["name"] for s in (await env.c.get("/secrets", headers=m)).json()} >= {"a-key"}


async def test_tool_access_gates_skill_visibility(env):
    """The access list also gates which SKILLS a restricted member can see: granted by the bundle's
    own name (recipe-only skills) or via any of its tools. /bundles + both bundle getters comply."""
    await env.c.post("/skills", headers=_h(env.owner),
                     json={"name": "gamma", "recipe": "# gamma", "secrets": [], "tools": []})
    m = _h(env.member)

    await _set_access(env, ["alpha"])  # a bare endpoint grant — no skills granted at all
    assert {b["name"] for b in (await env.c.get("/bundles", headers=m)).json()} == set()
    assert (await env.c.get("/bundles/by-name/beta", headers=m)).status_code == 403

    await _set_access(env, ["gamma"])  # recipe-only skill granted by its own name
    assert {b["name"] for b in (await env.c.get("/bundles", headers=m)).json()} == {"gamma"}
    ok = await env.c.get("/bundles/by-name/gamma", headers=m)
    assert ok.status_code == 200
    assert (await env.c.get(f"/bundles/{ok.json()['id']}", headers=m)).status_code == 200  # skill install route

    await _set_access(env, ["beta"])  # an integration skill granted via its tool's name
    assert {b["name"] for b in (await env.c.get("/bundles", headers=m)).json()} == {"beta"}

    # the owner is never restricted
    assert {b["name"] for b in (await env.c.get("/bundles", headers=_h(env.owner))).json()} == {"beta", "gamma"}
