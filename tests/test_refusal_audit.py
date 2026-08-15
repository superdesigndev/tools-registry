"""Every `/call/` refusal leaves a CallRecord stamped `refused_by` — the funnel's early friction.

Before this, a call treg refused BEFORE the relay fell into two buckets: money refusals were
audited (but indistinguishable from a provider's own 402), and everything earlier — bad token,
unknown tool, ACL, deny rule — left no row at all. The 2026-08-12 Hunter incident is the motivating
case: 309 pre-relay refusals read as "Hunter is failing" until the rows were re-derived from
`duration_ms IS NULL`. `refused_by` makes that distinction a column instead of a forensic exercise.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.db import session_maker
from treg.models import CallRecord


async def _rows() -> list[CallRecord]:
    await audit.drain()  # flush the fire-and-forget writes before reading
    async with session_maker() as db:
        return (await db.execute(select(CallRecord).order_by(CallRecord.id))).scalars().all()


async def _make_echo_tool(clients: AsyncClient, name: str = "echo-tool") -> None:
    s = await clients.post("/secrets", json={"name": f"k-{name}", "value": "sek"})
    await clients.post("/tools", json={"name": name, "base_url": "http://upstream",
                                       "secret_id": s.json()["id"]})


async def test_a_relayed_call_is_not_a_refusal(clients: AsyncClient):
    await _make_echo_tool(clients)
    r = await clients.get("/call/echo-tool/echo?x=1")
    assert r.status_code == 200
    (rec,) = await _rows()
    assert rec.refused_by is None


async def test_unknown_tool_is_recorded_as_a_resolution_refusal(clients: AsyncClient):
    """The pre-refused_by blind spot: a 404 for a tool nobody registered raised before the
    handler's own audit and vanished. The fallback in the exception handler records it."""
    r = await clients.get("/call/no-such-tool/whatever")
    assert r.status_code == 404
    (rec,) = await _rows()
    assert rec.refused_by == "resolution"
    assert rec.tool_name == "no-such-tool"
    assert rec.user_email == "tim@superdesign.dev"  # identity was known — the token was fine
    assert rec.org_id is not None


async def test_bad_token_is_recorded_anonymously_as_an_auth_refusal(clients: AsyncClient):
    """No Caller ever existed, so the row cannot say who — but an anonymous row is still the
    fact that someone knocked with a dead token, which zero rows could never say."""
    r = await clients.get("/call/echo-tool/echo", headers={"X-Treg-Token": "garbage"})
    assert r.status_code == 401
    (rec,) = await _rows()
    assert rec.refused_by == "auth"
    assert rec.org_id is None and rec.user_email == ""


async def test_refusals_reach_the_caller_in_the_audit_log(clients: AsyncClient):
    """`treg audit` is where an org goes to ask "why did my call fail" — the column must be in
    the payload, not only in the table."""
    await clients.get("/call/no-such-tool/whatever")
    await audit.drain()
    (row,) = (await clients.get("/calls")).json()
    assert row["refused_by"] == "resolution"
