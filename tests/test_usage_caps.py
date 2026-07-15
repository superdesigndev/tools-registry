"""Per-user daily usage caps (usage-metering v1). Membership.daily_call_cap (-1 = unlimited) bounds
how many usage events a member may produce per UTC day in an org; the count spans ALL kinds — proxy
calls + local-run grants (CallRecord) + server runs (RunRecord) — enforced before each executes.
Soft by design (best-effort audit → fails open), so these tests seed records deterministically.
"""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.api import _utcnow_naive, count_today
from treg.db import session_maker
from treg.models import CallRecord, Membership, RunRecord, User


async def _set_cap(cap: int, email: str = "tim@superdesign.dev") -> int:
    """Set a member's daily cap directly (the admin endpoint is step 3); returns their org_id."""
    async with session_maker() as s:
        uid = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
        m = (await s.execute(select(Membership).where(Membership.user_id == uid))).scalars().first()
        m.daily_call_cap = cap
        org_id = m.org_id
        await s.commit()
    return org_id


async def _seed_call(org_id: int, email: str, *, days_ago: int = 0) -> None:
    async with session_maker() as s:
        s.add(CallRecord(org_id=org_id, user_email=email, tool_name="t", method="GET", path="/x",
                         status_code=200, created_at=_utcnow_naive() - timedelta(days=days_ago)))
        await s.commit()


async def _seed_run(org_id: int, email: str) -> None:
    async with session_maker() as s:
        s.add(RunRecord(org_id=org_id, user_email=email, bundle_name="t", argv=[], exit_code=0,
                        duration_ms=1, created_at=_utcnow_naive()))
        await s.commit()


async def _mk_echo_tool(c: AsyncClient, name: str = "echo") -> None:
    r = await c.post("/tools", json={"name": name, "base_url": "http://upstream", "bindings": []})
    assert r.status_code == 200, r.text


async def test_call_blocked_once_the_cap_is_reached(clients: AsyncClient):
    await _mk_echo_tool(clients)
    org_id = await _set_cap(1)
    assert (await clients.get("/call/echo/anything")).status_code == 200  # 1st call: under cap
    await audit.drain()  # its CallRecord is fire-and-forget
    blocked = await clients.get("/call/echo/anything")
    assert blocked.status_code == 429 and "daily usage limit" in blocked.json()["detail"]
    _ = org_id


async def test_unlimited_by_default(clients: AsyncClient):
    await _mk_echo_tool(clients)  # cap defaults to -1 → no cap query, never blocked
    for _ in range(5):
        assert (await clients.get("/call/echo/anything")).status_code == 200


async def test_runs_and_grants_count_toward_the_same_cap(clients: AsyncClient):
    """A member can't dodge the cap by switching path: CallRecord (call+local) AND RunRecord (server)
    both count. Seed one of each to reach cap=2, then a proxy call is refused."""
    await _mk_echo_tool(clients)
    org_id = await _set_cap(2)
    await _seed_call(org_id, "tim@superdesign.dev")  # 1 (a prior proxy/local event)
    await _seed_run(org_id, "tim@superdesign.dev")   # 2 (a prior server run)
    blocked = await clients.get("/call/echo/anything")
    assert blocked.status_code == 429  # used=2 (call+run) >= cap=2


async def test_cap_is_per_member_not_global(clients: AsyncClient):
    """Capping one member must not affect another in the same org."""
    await _mk_echo_tool(clients)
    org_id = await _set_cap(1)
    await _seed_call(org_id, "tim@superdesign.dev")  # tim is now at his cap
    # invite bob into the SAME org (default cap -1)
    code = (await clients.post(f"/orgs/{org_id}/invites", json={"email": "bob@x.io", "role": "member"})).json()["code"]
    btok = (await clients.post("/invites/accept", json={"code": code, "email": "bob@x.io"})).json()["token"]

    assert (await clients.get("/call/echo/anything")).status_code == 429  # tim blocked
    bob = await clients.get("/call/echo/anything", headers={"X-Treg-Token": btok})
    assert bob.status_code == 200  # bob unaffected


async def test_yesterdays_usage_does_not_count_today(clients: AsyncClient):
    await _mk_echo_tool(clients)
    org_id = await _set_cap(1)
    await _seed_call(org_id, "tim@superdesign.dev", days_ago=1)  # yesterday — outside today's window
    assert (await clients.get("/call/echo/anything")).status_code == 200  # today's count is still 0


async def _get_org_id(email: str = "tim@superdesign.dev") -> int:
    async with session_maker() as s:
        uid = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
        return (await s.execute(select(Membership.org_id).where(Membership.user_id == uid))).scalars().first()


async def _invite(clients: AsyncClient, org_id: int, email: str, role: str) -> str:
    code = (await clients.post(f"/orgs/{org_id}/invites", json={"email": email, "role": role})).json()["code"]
    return (await clients.post("/invites/accept", json={"code": code, "email": email})).json()["token"]


# ---- step 3: the endpoints -----------------------------------------------------------------
async def test_usage_endpoint_rolls_up_by_user_tool_and_day(clients: AsyncClient):
    org_id = await _get_org_id()
    await _seed_call(org_id, "tim@superdesign.dev")  # 2 calls + 1 server run = 3 events
    await _seed_call(org_id, "tim@superdesign.dev")
    await _seed_run(org_id, "tim@superdesign.dev")
    u = (await clients.get(f"/orgs/{org_id}/usage")).json()
    assert u["totals"] == {"call": 2, "local_run": 0, "server_run": 1, "total": 3}
    assert u["by_user"][0]["user_email"] == "tim@superdesign.dev" and u["by_user"][0]["total"] == 3
    assert sum(d["total"] for d in u["by_day"]) == 3 and u["by_tool"]  # today's bucket + tool present


async def test_usage_endpoint_is_admin_only(clients: AsyncClient):
    org_id = await _get_org_id()
    vtok = await _invite(clients, org_id, "v@x.io", "viewer")
    assert (await clients.get(f"/orgs/{org_id}/usage", headers={"X-Treg-Token": vtok})).status_code == 403


async def test_set_cap_is_admin_only_and_validated(clients: AsyncClient):
    org_id = await _get_org_id()
    mtok = await _invite(clients, org_id, "m@x.io", "member")
    mid = [x["user_id"] for x in (await clients.get(f"/orgs/{org_id}/members")).json() if x["email"] == "m@x.io"][0]

    ok = await clients.patch(f"/orgs/{org_id}/members/{mid}/cap", json={"daily_call_cap": 5})
    assert ok.status_code == 200 and ok.json()["daily_call_cap"] == 5
    assert (await clients.patch(f"/orgs/{org_id}/members/{mid}/cap", json={"daily_call_cap": -1})).status_code == 200  # unlimited
    assert (await clients.patch(f"/orgs/{org_id}/members/{mid}/cap", json={"daily_call_cap": -5})).status_code == 422  # invalid
    # the member cannot set caps (not an admin)
    assert (await clients.patch(f"/orgs/{org_id}/members/{mid}/cap", json={"daily_call_cap": 1},
                                headers={"X-Treg-Token": mtok})).status_code == 403


async def test_list_members_shows_cap_and_used_today(clients: AsyncClient):
    org_id = await _set_cap(7)
    await _seed_call(org_id, "tim@superdesign.dev")
    tim = [x for x in (await clients.get(f"/orgs/{org_id}/members")).json() if x["email"] == "tim@superdesign.dev"][0]
    assert tim["daily_call_cap"] == 7 and tim["used_today"] == 1


async def test_usage_me(clients: AsyncClient):
    org_id = await _set_cap(9)
    await _seed_call(org_id, "tim@superdesign.dev")
    me = (await clients.get("/usage/me")).json()
    assert me["cap"] == 9 and me["used_today"] == 1 and me["org"]


async def test_count_today_sums_calls_and_runs(clients: AsyncClient):
    org_id = await _set_cap(-1)  # cap irrelevant here; just exercising the counter
    email = "tim@superdesign.dev"
    async with session_maker() as s:
        before = await count_today(s, org_id, email)
    await _seed_call(org_id, email)
    await _seed_run(org_id, email)
    await _seed_call(org_id, email, days_ago=2)  # old — must NOT count
    async with session_maker() as s:
        after = await count_today(s, org_id, email)
    assert after - before == 2
