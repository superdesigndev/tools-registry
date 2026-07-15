"""Round-3 fixes — SSRF encodings/call-time, update_tool grandfather, RunRecord cascade, delete_bundle gap."""
from __future__ import annotations

from httpx import AsyncClient
from sqlmodel import select

from treg.health import host_is_public, safe_webhook_url


def test_ssrf_blocks_numeric_ip_encodings():
    for enc in ("http://2130706433/", "http://127.1/", "http://0x7f000001/", "http://017700000001/"):
        assert safe_webhook_url(enc) is False, f"{enc} should be blocked"
    assert safe_webhook_url("https://api.stripe.com/v1") is True


def test_host_is_public_resolves_and_blocks_internal():
    assert host_is_public("localhost") is False
    assert host_is_public("nonexistent.invalid.tld.zzz") is False   # unresolvable → refuse
    assert host_is_public("api.stripe.com") is True


async def test_base_url_rejects_numeric_encoding_at_registration(clients: AsyncClient):
    r = await clients.post("/tools", json={"name": "enc", "base_url": "http://2130706433/"})
    assert r.status_code == 422


async def _member_in_owner_org(clients: AsyncClient, email="m3b@x.dev"):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    inv = (await clients.post(f"/orgs/{org_id}/invites", json={"email": email, "role": "member"})).json()
    tok = (await clients.post("/invites/accept", json={"code": inv["code"], "email": email})).json()["token"]
    return org_id, tok


async def test_update_tool_grandfathers_an_admin_added_shared_binding(clients: AsyncClient):
    _, tokB = await _member_in_owner_org(clients)
    hb = {"X-Treg-Token": tokB}
    # member B (creator) makes a tool binding B's OWN secret
    sB = (await clients.post("/secrets", json={"name": "b-key", "kind": "env", "value": "K"}, headers=hb)).json()["id"]
    tid = (await clients.post("/tools", json={"name": "b-tool", "base_url": "https://api.example.com",
            "bindings": [{"secret_id": sB, "name": "Authorization", "format": "Bearer {secret}"}]}, headers=hb)).json()["id"]
    # the ADMIN (owner fixture) adds a binding to the admin's OWN shared secret
    sA = (await clients.post("/secrets", json={"name": "admin-shared", "kind": "env", "value": "K2"})).json()["id"]
    both = [{"secret_id": sB, "name": "Authorization", "format": "Bearer {secret}"},
            {"secret_id": sA, "name": "X-Admin", "format": "{secret}"}]
    assert (await clients.patch(f"/tools/{tid}", json={"bindings": both})).status_code == 200
    # now B re-saves the tool (base_url + the FULL bindings, as the dashboard does) — must NOT 403
    r = await clients.patch(f"/tools/{tid}", headers=hb, json={"base_url": "https://api.example.com/v2", "bindings": both})
    assert r.status_code == 200, r.text
    # but B ADDING a brand-new admin-owned secret is still blocked
    sA2 = (await clients.post("/secrets", json={"name": "admin-shared2", "kind": "env", "value": "K3"})).json()["id"]
    r2 = await clients.patch(f"/tools/{tid}", headers=hb,
                             json={"bindings": both + [{"secret_id": sA2, "name": "X-New", "format": "{secret}"}]})
    assert r2.status_code == 403


async def test_delete_org_removes_run_records(clients: AsyncClient):
    from treg.db import session_maker
    from treg.models import RunRecord
    # a server-runnable tool + a server run → writes a RunRecord
    await clients.post("/skills", json={"name": "r3", "recipe": "r", "secrets": [],
                                        "tools": [{"name": "r3", "base_url": "https://api.example.com",
                                                   "cli": {"bin": "sh"}}]})
    await clients.post("/run", json={"tool": "r3", "args": ["-c", "true"]})
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as s:
        before = len((await s.execute(select(RunRecord).where(RunRecord.org_id == org_id))).scalars().all())
    assert before >= 1
    r = await clients.delete(f"/orgs/{org_id}")
    assert r.status_code == 200, r.text
    async with session_maker() as s:
        after = len((await s.execute(select(RunRecord).where(RunRecord.org_id == org_id))).scalars().all())
    assert after == 0   # cascade now includes RunRecord (no orphans / no PG FK violation)


async def test_delete_bundle_guards_a_cli_inject_reference(clients: AsyncClient):
    # bundle B owns secret S; a tool OUTSIDE B references S only via cli.inject → deleting B must 409
    b = (await clients.post("/skills", json={"name": "bkt", "recipe": "r",
            "secrets": [{"local_name": "S", "kind": "env", "value": "v"}], "tools": []})).json()
    sid = (await clients.get("/secrets")).json()
    s_id = next(x["id"] for x in sid if x["name"] == "S")
    await clients.post("/tools", json={"name": "runner-tool", "base_url": "https://api.example.com",
                                       "cli": {"enabled": True, "bin": "printenv", "inject": [{"via": "env", "name": "X", "secret_id": s_id}]}})
    r = await clients.delete(f"/bundles/{b['id']}")
    assert r.status_code == 409 and "referenced" in r.text
