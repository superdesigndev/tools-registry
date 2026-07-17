"""Public-demo token: a publishable, call-only credential (the landing-page Stripe demo).

The threat model: the token is printed on a public web page, so a stranger holds a real
membership token. The lockdown must hold at BOTH auth layers — require_member (org-scoped
mutations) and require_identity (user-level escape hatches like /auth/cli-token and POST /orgs).
"""

from __future__ import annotations

from httpx import AsyncClient

import treg.api as api_mod


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


async def _org_with_stripe_tool(c: AsyncClient) -> int:
    """The default client (owner of their personal org) registers a pinned secret + tool.
    Returns the org id."""
    org_id = next(o["org_id"] for o in (await c.get("/orgs")).json() if o["active"])
    s = await c.post("/secrets", json={"name": "STRIPE_KEY", "value": "rk_test_x"})
    assert s.status_code == 200, s.text
    t = await c.post("/tools", json={
        "name": "stripe", "base_url": "http://upstream/v1/charges", "secret_id": s.json()["id"]})
    assert t.status_code == 200, t.text
    return org_id


async def _mint_public(c: AsyncClient, org_id: int) -> str:
    r = await c.post(f"/orgs/{org_id}/public-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "viewer"
    return body["token"]


# ---- minting ------------------------------------------------------------------------------
async def test_mint_is_owner_only(clients: AsyncClient):
    org_id = await _org_with_stripe_tool(clients)
    # a joined member may not publish the org's credential
    code = (await clients.post(f"/orgs/{org_id}/invites",
                               json={"email": "m@x.dev", "role": "member"})).json()["code"]
    mtok = (await clients.post("/invites/accept", json={"code": code, "email": "m@x.dev"})).json()["token"]
    r = await clients.post(f"/orgs/{org_id}/public-token", headers=_h(mtok))
    assert r.status_code == 403
    assert (await clients.post(f"/orgs/{org_id}/public-token")).status_code == 200


async def test_public_token_calls_and_key_is_injected(clients: AsyncClient):
    org_id = await _org_with_stripe_tool(clients)
    pub = await _mint_public(clients, org_id)
    r = await clients.post("/call/http://upstream/v1/charges", headers=_h(pub), content="amount=420")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer rk_test_x"  # the vaulted key was injected server-side


async def test_named_call_with_empty_path_has_no_trailing_slash(clients: AsyncClient):
    """A base pinned to a full resource (…/v1/charges) must relay AS-IS: Stripe 404s `/v1/charges/`."""
    org_id = await _org_with_stripe_tool(clients)
    pub = await _mint_public(clients, org_id)
    r = await clients.post("/call/stripe", headers=_h(pub), content="amount=1")
    assert r.status_code == 200, r.text
    assert r.json()["raw_path"] == "/v1/charges"


# ---- the lockdown -------------------------------------------------------------------------
async def test_public_token_reads_but_cannot_mutate(clients: AsyncClient):
    org_id = await _org_with_stripe_tool(clients)
    pub = await _mint_public(clients, org_id)
    assert (await clients.get("/tools", headers=_h(pub))).status_code == 200
    assert (await clients.get("/secrets", headers=_h(pub))).status_code == 200
    for method, path, payload in [
        ("post", "/secrets", {"name": "EVIL", "value": "x"}),
        ("post", "/tools", {"name": "exfil", "base_url": "http://attacker.example"}),
        ("post", f"/orgs/{org_id}/leave", None),
        ("delete", f"/orgs/{org_id}", None),
        ("post", f"/orgs/{org_id}/public-token", None),  # can't rotate itself
    ]:
        r = await getattr(clients, method)(path, headers=_h(pub), **({"json": payload} if payload else {}))
        assert r.status_code == 403, f"{method} {path} → {r.status_code}: {r.text}"


async def test_public_token_cannot_act_as_a_user(clients: AsyncClient):
    """The user-level escape hatches: minting an identity token or creating a real org."""
    org_id = await _org_with_stripe_tool(clients)
    pub = await _mint_public(clients, org_id)
    assert (await clients.get("/auth/cli-token", headers=_h(pub))).status_code == 403
    assert (await clients.post("/orgs", headers=_h(pub), json={"name": "escape"})).status_code == 403


async def test_owner_keeps_full_control_of_a_public_demo_org(clients: AsyncClient):
    org_id = await _org_with_stripe_tool(clients)
    await _mint_public(clients, org_id)
    # the owner's own token still mutates freely — the lockdown is role-scoped, not org-wide
    r = await clients.post("/secrets", json={"name": "ANOTHER", "value": "y"})
    assert r.status_code == 200, r.text


# ---- rotation + revocation ----------------------------------------------------------------
async def test_reminting_rotates_the_token(clients: AsyncClient):
    org_id = await _org_with_stripe_tool(clients)
    old = await _mint_public(clients, org_id)
    new = await _mint_public(clients, org_id)
    assert (await clients.get("/tools", headers=_h(old))).status_code == 401
    assert (await clients.get("/tools", headers=_h(new))).status_code == 200


async def test_delete_revokes_and_unlocks(clients: AsyncClient):
    org_id = await _org_with_stripe_tool(clients)
    pub = await _mint_public(clients, org_id)
    r = await clients.delete(f"/orgs/{org_id}/public-token")
    assert r.status_code == 200
    assert (await clients.get("/tools", headers=_h(pub))).status_code == 401  # membership gone
    # the org is a normal team again (flag off) — owner unaffected throughout
    org = next(o for o in (await clients.get("/orgs")).json() if o["org_id"] == org_id)
    assert org["slug"]  # still listed


# ---- per-IP rate limit on /call -----------------------------------------------------------
async def test_public_calls_are_rate_limited_per_ip(clients: AsyncClient, monkeypatch):
    org_id = await _org_with_stripe_tool(clients)
    pub = await _mint_public(clients, org_id)
    monkeypatch.setattr(api_mod, "PUBLIC_DEMO_RATE_MAX", 3)
    for _ in range(3):
        assert (await clients.post("/call/stripe", headers=_h(pub), content="a=1")).status_code == 200
    r = await clients.post("/call/stripe", headers=_h(pub), content="a=1")
    assert r.status_code == 429
    # the owner's own calls are NOT metered by the public limiter
    assert (await clients.post("/call/stripe", content="a=1")).status_code == 200
