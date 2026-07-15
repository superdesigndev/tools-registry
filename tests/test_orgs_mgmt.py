"""PR2 — org management: create orgs, invite by one-time code, join, list/remove members.

Everything goes through the real API (registration mints a personal-org token; the invite code
is the only out-of-band secret). Proves the full invite -> accept flow for new AND existing
users, the role gates (member cannot invite/list/remove; admin can), email/code validation, and
that an admin cannot remove an owner.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from sqlalchemy import text

from conftest import make_upstream

from treg.api import app
from treg.db import reset_db, session_maker


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def c():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


async def _register(c: AsyncClient, email: str) -> str:
    return (await c.post("/users", json={"email": email})).json()["token"]


async def _team_with_member(c: AsyncClient):
    """owner + a joined member. Returns (org_id, owner_token, member_token, owner_uid, member_uid)."""
    otok = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(otok), json={"name": "Team A"})).json()
    org_id = team["org_id"]
    otok = team["token"]
    code = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "m@x.dev", "role": "member"})).json()["code"]
    mtok = (await c.post("/invites/accept", json={"code": code, "email": "m@x.dev"})).json()["token"]
    members = (await c.get(f"/orgs/{org_id}/members", headers=_h(otok))).json()
    owner_uid = next(m["user_id"] for m in members if m["role"] == "owner")
    member_uid = next(m["user_id"] for m in members if m["role"] == "member")
    return org_id, otok, mtok, owner_uid, member_uid


async def test_create_org_returns_a_working_token(c):
    owner = await _register(c, "owner@x.dev")
    r = await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org"] == "team-a"
    team_token = body["token"]
    # the new token works and starts empty (its own org)
    assert (await c.get("/tools", headers=_h(team_token))).json() == []
    # the caller now belongs to their personal org + Team A
    orgs = {o["slug"]: o for o in (await c.get("/orgs", headers=_h(owner))).json()}
    assert "team-a" in {o["slug"] for o in (await c.get("/orgs", headers=_h(team_token))).json()}
    assert "team-a" in orgs


async def test_invite_and_accept_new_user(c):
    owner = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})).json()
    org_id = team["org_id"]
    inv = await c.post(f"/orgs/{org_id}/invites", headers=_h(team["token"]), json={"email": "bob@x.dev", "role": "member"})
    assert inv.status_code == 200, inv.text
    code = inv.json()["code"]

    acc = await c.post("/invites/accept", json={"code": code, "email": "bob@x.dev"})
    assert acc.status_code == 200, acc.text
    bob_token = acc.json()["token"]
    assert acc.json()["role"] == "member"
    # Bob is now in Team A and sees its (empty) inventory
    assert acc.json()["org_id"] == org_id
    assert (await c.get("/tools", headers=_h(bob_token))).status_code == 200
    # a brand-new invitee joins the invited team ONLY — no separate personal org is spun up
    assert "personal" not in acc.json()
    orgs = {o["slug"] for o in (await c.get("/orgs", headers=_h(bob_token))).json()}
    assert orgs == {"team-a"}
    # the code is one-time: a second accept fails
    assert (await c.post("/invites/accept", json={"code": code, "email": "bob@x.dev"})).status_code == 404


async def test_invite_and_accept_existing_user(c):
    owner = await _register(c, "owner@x.dev")
    await _register(c, "carol@x.dev")  # Carol already has an identity + personal org
    team = (await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})).json()
    code = (await c.post(f"/orgs/{team['org_id']}/invites", headers=_h(team["token"]),
                         json={"email": "carol@x.dev", "role": "admin"})).json()["code"]
    acc = await c.post("/invites/accept", json={"code": code, "email": "carol@x.dev"})
    assert acc.status_code == 200 and acc.json()["role"] == "admin"
    assert "personal" not in acc.json()  # existing user already has their own org; none created
    # Carol now has personal + Team A
    slugs = {o["slug"] for o in (await c.get("/orgs", headers=_h(acc.json()["token"]))).json()}
    assert "team-a" in slugs


async def test_email_mismatch_and_bad_code_rejected(c):
    owner = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})).json()
    code = (await c.post(f"/orgs/{team['org_id']}/invites", headers=_h(team["token"]),
                         json={"email": "bob@x.dev"})).json()["code"]
    assert (await c.post("/invites/accept", json={"code": code, "email": "eve@x.dev"})).status_code == 403
    assert (await c.post("/invites/accept", json={"code": "inv_bogus", "email": "bob@x.dev"})).status_code == 404


async def test_member_cannot_invite_but_admin_can(c):
    owner = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})).json()
    org_id, otok = team["org_id"], team["token"]
    # add a plain member
    code = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "m@x.dev", "role": "member"})).json()["code"]
    mtok = (await c.post("/invites/accept", json={"code": code, "email": "m@x.dev"})).json()["token"]
    # member cannot invite or list members
    assert (await c.post(f"/orgs/{org_id}/invites", headers=_h(mtok), json={"email": "x@x.dev"})).status_code == 403
    assert (await c.get(f"/orgs/{org_id}/members", headers=_h(mtok))).status_code == 403
    # add an admin, who then CAN invite
    code2 = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "a@x.dev", "role": "admin"})).json()["code"]
    atok = (await c.post("/invites/accept", json={"code": code2, "email": "a@x.dev"})).json()["token"]
    assert (await c.post(f"/orgs/{org_id}/invites", headers=_h(atok), json={"email": "y@x.dev"})).status_code == 200


async def test_viewer_can_call_and_read_but_not_register(c):
    owner = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})).json()
    org_id, otok = team["org_id"], team["token"]
    # owner registers a secret + tool that the viewer will be able to see/call
    sid = (await c.post("/secrets", headers=_h(otok), json={"name": "k", "value": "V"})).json()["id"]
    await c.post("/tools", headers=_h(otok), json={"name": "echo", "base_url": "http://upstream", "secret_id": sid})
    # invite a viewer
    code = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "vi@x.dev", "role": "viewer"})).json()["code"]
    acc = await c.post("/invites/accept", json={"code": code, "email": "vi@x.dev"})
    assert acc.status_code == 200 and acc.json()["role"] == "viewer"
    vtok = acc.json()["token"]

    # CAN read + call
    assert {t["name"] for t in (await c.get("/tools", headers=_h(vtok))).json()} == {"echo"}
    assert (await c.get("/call/echo/anything", headers=_h(vtok))).status_code == 200
    # CANNOT register secrets/tools/skills, nor invite
    assert (await c.post("/secrets", headers=_h(vtok), json={"name": "x", "value": "y"})).status_code == 403
    assert (await c.post("/tools", headers=_h(vtok), json={"name": "t2", "base_url": "http://upstream"})).status_code == 403
    assert (await c.post("/skills", headers=_h(vtok), json={"name": "s", "recipe": ""})).status_code == 403
    assert (await c.post(f"/orgs/{org_id}/invites", headers=_h(vtok), json={"email": "z@x.dev"})).status_code == 403


async def test_admin_cannot_remove_owner_but_can_remove_member(c):
    owner = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(owner), json={"name": "Team A"})).json()
    org_id, otok = team["org_id"], team["token"]
    # owner_user_id
    owner_uid = next(m["user_id"] for m in (await c.get(f"/orgs/{org_id}/members", headers=_h(otok))).json() if m["role"] == "owner")
    # admin joins
    acode = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "a@x.dev", "role": "admin"})).json()["code"]
    atok = (await c.post("/invites/accept", json={"code": acode, "email": "a@x.dev"})).json()["token"]
    # member joins
    mcode = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "m@x.dev", "role": "member"})).json()["code"]
    await c.post("/invites/accept", json={"code": mcode, "email": "m@x.dev"})
    member_uid = next(m["user_id"] for m in (await c.get(f"/orgs/{org_id}/members", headers=_h(otok))).json() if m["role"] == "member")

    # admin cannot remove the owner
    assert (await c.delete(f"/orgs/{org_id}/members/{owner_uid}", headers=_h(atok))).status_code == 403
    # admin CAN remove the member — which revokes that member's token
    assert (await c.delete(f"/orgs/{org_id}/members/{member_uid}", headers=_h(atok))).status_code == 200


# ---- PR: org-admin completeness (role change, leave, delete, invite expiry) ----------------
async def test_owner_changes_role_others_cannot(c):
    org_id, otok, mtok, owner_uid, member_uid = await _team_with_member(c)
    # owner promotes the member to admin
    r = await c.patch(f"/orgs/{org_id}/members/{member_uid}", headers=_h(otok), json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # the promoted admin still cannot change roles (owner-only) — try to demote the owner
    r2 = await c.patch(f"/orgs/{org_id}/members/{owner_uid}", headers=_h(mtok), json={"role": "member"})
    assert r2.status_code == 403


async def test_cannot_demote_last_owner(c):
    org_id, otok, _, owner_uid, _ = await _team_with_member(c)
    r = await c.patch(f"/orgs/{org_id}/members/{owner_uid}", headers=_h(otok), json={"role": "member"})
    assert r.status_code == 409


async def test_transfer_ownership_then_demote(c):
    org_id, otok, mtok, owner_uid, member_uid = await _team_with_member(c)
    # promote member to owner (now two owners) → original owner can be demoted
    assert (await c.patch(f"/orgs/{org_id}/members/{member_uid}", headers=_h(otok), json={"role": "owner"})).status_code == 200
    assert (await c.patch(f"/orgs/{org_id}/members/{owner_uid}", headers=_h(otok), json={"role": "member"})).status_code == 200


async def test_leave_org_revokes_token_and_last_owner_blocked(c):
    org_id, otok, mtok, owner_uid, member_uid = await _team_with_member(c)
    # member leaves → token no longer works
    assert (await c.post(f"/orgs/{org_id}/leave", headers=_h(mtok))).status_code == 200
    assert (await c.get("/tools", headers=_h(mtok))).status_code == 401
    # the last owner cannot leave
    assert (await c.post(f"/orgs/{org_id}/leave", headers=_h(otok))).status_code == 409


async def test_delete_org_cascades_and_is_owner_only(c):
    org_id, otok, mtok, _, _ = await _team_with_member(c)
    sid = (await c.post("/secrets", headers=_h(otok), json={"name": "k", "value": "V"})).json()["id"]
    await c.post("/tools", headers=_h(otok), json={"name": "echo", "base_url": "http://upstream", "secret_id": sid})
    # a member cannot delete the org
    assert (await c.delete(f"/orgs/{org_id}", headers=_h(mtok))).status_code == 403
    # the owner can — and every membership is gone (both tokens now invalid)
    assert (await c.delete(f"/orgs/{org_id}", headers=_h(otok))).status_code == 200
    assert (await c.get("/tools", headers=_h(otok))).status_code == 401
    assert (await c.get("/tools", headers=_h(mtok))).status_code == 401


async def test_expired_invite_is_rejected(c):
    otok = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(otok), json={"name": "Team A"})).json()
    code = (await c.post(f"/orgs/{team['org_id']}/invites", headers=_h(team["token"]),
                         json={"email": "late@x.dev", "role": "member"})).json()["code"]
    # backdate the invite so it's expired
    async with session_maker() as s:
        await s.execute(text("UPDATE invite SET expires_at = '2000-01-01 00:00:00'"))
        await s.commit()
    r = await c.post("/invites/accept", json={"code": code, "email": "late@x.dev"})
    assert r.status_code == 410


async def test_invite_response_carries_expiry(c):
    otok = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(otok), json={"name": "Team A"})).json()
    r = await c.post(f"/orgs/{team['org_id']}/invites", headers=_h(team["token"]),
                     json={"email": "b@x.dev", "role": "member", "expires_days": 3})
    assert r.status_code == 200 and "expires_at" in r.json()


async def test_list_and_revoke_invites(c):
    otok = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(otok), json={"name": "Team A"})).json()
    org_id, otok = team["org_id"], team["token"]
    inv = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "p@x.dev", "role": "member"})).json()
    # admin+ sees it listed as pending
    listed = (await c.get(f"/orgs/{org_id}/invites", headers=_h(otok))).json()
    assert [i["email"] for i in listed] == ["p@x.dev"]
    inv_id = listed[0]["id"]
    # a member cannot list invites
    mcode = (await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "m@x.dev"})).json()["code"]
    mtok = (await c.post("/invites/accept", json={"code": mcode, "email": "m@x.dev"})).json()["token"]
    assert (await c.get(f"/orgs/{org_id}/invites", headers=_h(mtok))).status_code == 403
    # revoke -> the code can no longer be accepted, and it's gone from the list
    assert (await c.delete(f"/orgs/{org_id}/invites/{inv_id}", headers=_h(otok))).status_code == 200
    assert (await c.post("/invites/accept", json={"code": inv["code"], "email": "p@x.dev"})).status_code == 404
    remaining = [i["email"] for i in (await c.get(f"/orgs/{org_id}/invites", headers=_h(otok))).json()]
    assert "p@x.dev" not in remaining


async def test_expired_invites_are_garbage_collected_on_list(c):
    otok = await _register(c, "owner@x.dev")
    team = (await c.post("/orgs", headers=_h(otok), json={"name": "Team A"})).json()
    org_id, otok = team["org_id"], team["token"]
    await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "old@x.dev"})
    await c.post(f"/orgs/{org_id}/invites", headers=_h(otok), json={"email": "live@x.dev"})
    # expire the first
    async with session_maker() as s:
        await s.execute(text("UPDATE invite SET expires_at='2000-01-01 00:00:00' WHERE email='old@x.dev'"))
        await s.commit()
    listed = [i["email"] for i in (await c.get(f"/orgs/{org_id}/invites", headers=_h(otok))).json()]
    assert listed == ["live@x.dev"]  # expired one was garbage-collected
    # and it's actually deleted from the DB, not just hidden
    async with session_maker() as s:
        left = (await s.execute(text("SELECT COUNT(*) FROM invite WHERE email='old@x.dev'"))).scalar()
    assert left == 0
