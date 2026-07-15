"""PR1 — org isolation + the role gate.

Two orgs (Team A / Team B) are seeded directly via the models (invites/join arrive in PR2), and
memberships mint org-scoped tokens. We prove: a member sees/calls only their active org's tools;
the same tool name and the same upstream host resolve to each org's own secret; cross-org mutation
is a 404; and a member cannot manage a teammate's resource while an admin/owner can.
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


async def _make_org(name: str, slug: str) -> int:
    async with session_maker() as s:
        org = Org(name=name, slug=slug)
        s.add(org)
        await s.commit()
        await s.refresh(org)
        return org.id


async def _mint(email: str, org_id: int, role: str) -> str:
    """Create the user (if new) + a membership in `org_id`; return its fresh org-scoped token."""
    token = crypto.new_token()
    async with session_maker() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(email=email)
            s.add(user)
            await s.flush()
        s.add(Membership(user_id=user.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await s.commit()
    return token


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def env():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        org_a = await _make_org("Team A", "team-a")
        org_b = await _make_org("Team B", "team-b")
        yield SimpleNamespace(
            c=c,
            org_a=org_a,
            org_b=org_b,
            owner_a=await _mint("owner-a@x.dev", org_a, "owner"),
            member_a=await _mint("member-a@x.dev", org_a, "member"),
            owner_b=await _mint("owner-b@x.dev", org_b, "owner"),
        )
    await app.state.http.aclose()


async def _make_tool(c: AsyncClient, token: str, *, name: str, secret_value: str, base_url="http://upstream") -> None:
    sid = (await c.post("/secrets", headers=_h(token), json={"name": f"{name}-key", "value": secret_value})).json()["id"]
    r = await c.post("/tools", headers=_h(token), json={"name": name, "base_url": base_url, "secret_id": sid})
    assert r.status_code == 200, r.text


async def test_list_is_scoped_to_the_callers_org(env):
    await _make_tool(env.c, env.owner_a, name="a-only", secret_value="AAA")
    await _make_tool(env.c, env.owner_b, name="b-only", secret_value="BBB")

    a_tools = {t["name"] for t in (await env.c.get("/tools", headers=_h(env.owner_a))).json()}
    b_tools = {t["name"] for t in (await env.c.get("/tools", headers=_h(env.owner_b))).json()}
    assert a_tools == {"a-only"}
    assert b_tools == {"b-only"}
    # secrets are scoped too
    a_secrets = {s["name"] for s in (await env.c.get("/secrets", headers=_h(env.owner_a))).json()}
    assert a_secrets == {"a-only-key"}


async def test_same_tool_name_resolves_to_each_orgs_own_secret(env):
    # both orgs register a tool called "stripe" with different keys — no collision, each isolated.
    await _make_tool(env.c, env.owner_a, name="stripe", secret_value="A-KEY")
    await _make_tool(env.c, env.owner_b, name="stripe", secret_value="B-KEY")

    ra = await env.c.get("/call/stripe/v1/balance", headers=_h(env.owner_a))
    rb = await env.c.get("/call/stripe/v1/balance", headers=_h(env.owner_b))
    assert ra.json()["auth"] == "Bearer A-KEY"
    assert rb.json()["auth"] == "Bearer B-KEY"


async def test_passthrough_resolves_per_org(env):
    await _make_tool(env.c, env.owner_a, name="ic-a", secret_value="A-TOK", base_url="https://api.intercom.io")
    await _make_tool(env.c, env.owner_b, name="ic-b", secret_value="B-TOK", base_url="https://api.intercom.io")

    ra = await env.c.get("/call/https://api.intercom.io/me", headers=_h(env.owner_a))
    rb = await env.c.get("/call/https://api.intercom.io/me", headers=_h(env.owner_b))
    assert ra.json()["auth"] == "Bearer A-TOK"
    assert rb.json()["auth"] == "Bearer B-TOK"


async def test_cannot_call_a_tool_outside_your_org(env):
    await _make_tool(env.c, env.owner_b, name="b-secret-tool", secret_value="BBB")
    r = await env.c.get("/call/b-secret-tool/x", headers=_h(env.owner_a))
    assert r.status_code == 404


async def test_cross_org_mutation_is_404(env):
    await _make_tool(env.c, env.owner_b, name="b-tool", secret_value="BBB")
    tool_id = (await env.c.get("/tools", headers=_h(env.owner_b))).json()[0]["id"]
    # owner_a cannot even see it exists
    assert (await env.c.delete(f"/tools/{tool_id}", headers=_h(env.owner_a))).status_code == 404
    assert (await env.c.patch(f"/tools/{tool_id}", headers=_h(env.owner_a), json={"base_url": "http://x"})).status_code == 404


async def test_role_gate_member_cannot_manage_a_teammates_resource(env):
    # owner_a creates a tool; member_a (same org) may NOT delete it, but the owner/admin may.
    await _make_tool(env.c, env.owner_a, name="owned-by-owner", secret_value="AAA")
    tool_id = next(t["id"] for t in (await env.c.get("/tools", headers=_h(env.member_a))).json() if t["name"] == "owned-by-owner")

    denied = await env.c.delete(f"/tools/{tool_id}", headers=_h(env.member_a))
    assert denied.status_code == 403

    allowed = await env.c.delete(f"/tools/{tool_id}", headers=_h(env.owner_a))
    assert allowed.status_code == 200


async def test_role_gate_member_manages_own_and_admin_manages_any(env):
    # member_a creates their own tool → they can delete it.
    await _make_tool(env.c, env.member_a, name="member-owned", secret_value="MMM")
    mid = next(t["id"] for t in (await env.c.get("/tools", headers=_h(env.member_a))).json() if t["name"] == "member-owned")
    assert (await env.c.delete(f"/tools/{mid}", headers=_h(env.member_a))).status_code == 200

    # member_a creates another; the owner (admin over the org) can delete a member's resource.
    await _make_tool(env.c, env.member_a, name="member-owned-2", secret_value="MMM")
    mid2 = next(t["id"] for t in (await env.c.get("/tools", headers=_h(env.owner_a))).json() if t["name"] == "member-owned-2")
    assert (await env.c.delete(f"/tools/{mid2}", headers=_h(env.owner_a))).status_code == 200
