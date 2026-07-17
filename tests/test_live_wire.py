"""The sandbox live wire: the ONE fingerprint-matched real call inside the anonymous sandbox.

Threat model: the sandbox token is anonymous and self-served, and sandbox visitors can edit their
tools freely. The live relay must (a) inject the env key only for the EXACT seeded stripe tool,
(b) stamp the server-chosen visitor identity over anything the caller sent, and (c) fall back to
the classic synthesize() for anything tampered — because no sandbox org ever holds the real key.
"""

from __future__ import annotations

from httpx import AsyncClient

import treg.api as api_mod
from treg import pubfeed, sandbox

ENV_KEY = "rk_test_ENV_ONLY_KEY"


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


async def _mint(c: AsyncClient) -> dict:
    r = await c.post("/demo/sandbox", headers={"X-Treg-Token": ""})
    assert r.status_code == 200, r.text
    return r.json()


def _enable_live(monkeypatch, key: str = ENV_KEY):
    monkeypatch.setattr(api_mod.get_settings(), "demo_stripe_key", key)


# ---- identity -------------------------------------------------------------------------------
def test_visitor_name_is_deterministic_and_wordlist_shaped():
    a, b = sandbox.visitor_name("sbx-abc123def456"), sandbox.visitor_name("sbx-abc123def456")
    assert a == b
    adj, animal, n = a.split("-")
    assert adj in pubfeed.ADJECTIVES and animal in pubfeed.ANIMALS and n.isdigit()
    assert sandbox.visitor_name("sbx-000000000000") != sandbox.visitor_name("sbx-ffffffffffff")


async def test_mint_and_live_endpoint_report_the_wire(clients: AsyncClient, monkeypatch):
    _enable_live(monkeypatch)
    m = await _mint(clients)
    assert m["live"] is True
    assert m["visitor"] == sandbox.visitor_name(m["org_slug"])
    lw = await clients.get("/demo/sandbox/live", headers=_h(m["token"]))
    assert lw.json() == {"live": True, "visitor": m["visitor"]}
    # a non-sandbox caller has no business here
    assert (await clients.get("/demo/sandbox/live")).status_code == 400


async def test_mint_reports_wire_off_when_unconfigured(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(api_mod.get_settings(), "demo_stripe_key", "")
    assert (await _mint(clients))["live"] is False


# ---- the live relay -------------------------------------------------------------------------
async def test_live_call_relays_with_env_key_and_stamped_visitor(clients: AsyncClient, monkeypatch):
    _enable_live(monkeypatch)
    m = await _mint(clients)
    r = await clients.post("/call/https://api.stripe.com/v1/charges", headers=_h(m["token"]),
                           content="amount=420&currency=usd&source=tok_visa")
    assert r.status_code == 200, r.text
    echo = r.json()
    assert "sandbox" not in echo  # a REAL relay, not synthesize()
    assert echo["auth"] == f"Bearer {ENV_KEY}"  # env key injected — never a sandbox secret
    body = echo["body"].replace("%5B", "[").replace("%5D", "]")
    assert f"metadata[visitor]={m['visitor']}" in body


async def test_caller_supplied_visitor_metadata_is_overridden(clients: AsyncClient, monkeypatch):
    _enable_live(monkeypatch)
    m = await _mint(clients)
    r = await clients.post("/call/https://api.stripe.com/v1/charges", headers=_h(m["token"]),
                           content="amount=1&metadata[visitor]=rude-words-1")
    body = r.json()["body"].replace("%5B", "[").replace("%5D", "]")
    assert "rude-words-1" not in body
    assert f"metadata[visitor]={m['visitor']}" in body
    assert body.count("metadata[visitor]") == 1


async def test_live_get_lists_for_real(clients: AsyncClient, monkeypatch):
    _enable_live(monkeypatch)
    m = await _mint(clients)
    r = await clients.get("/call/https://api.stripe.com/v1/charges", headers=_h(m["token"]))
    assert r.status_code == 200
    assert r.json()["auth"] == f"Bearer {ENV_KEY}"  # relayed, key injected


# ---- containment ----------------------------------------------------------------------------
async def test_unconfigured_wire_synthesizes_as_before(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(api_mod.get_settings(), "demo_stripe_key", "")
    m = await _mint(clients)
    r = await clients.post("/call/https://api.stripe.com/v1/charges", headers=_h(m["token"]),
                           content="amount=420")
    assert r.json().get("sandbox") is True  # the classic dummy — network untouched


async def test_live_tool_and_key_are_frozen(clients: AsyncClient, monkeypatch):
    """The demo centerpiece can't be edited or removed — not via UI, not via raw API."""
    _enable_live(monkeypatch)
    m = await _mint(clients)
    tools = (await clients.get("/tools", headers=_h(m["token"]))).json()
    stripe = next(t for t in tools if t["name"] == "stripe")
    secrets = (await clients.get("/secrets", headers=_h(m["token"]))).json()
    key = next(s for s in secrets if s["name"] == "STRIPE_KEY")
    for method, path, payload in [
        ("patch", f"/tools/{stripe['id']}", {"base_url": "https://api.stripe.com/v1/customers"}),
        ("delete", f"/tools/{stripe['id']}", None),
        ("patch", f"/secrets/{key['id']}", {"value": "sk_evil"}),
        ("delete", f"/secrets/{key['id']}", None),
    ]:
        r = await getattr(clients, method)(path, headers=_h(m["token"]),
                                           **({"json": payload} if payload else {}))
        assert r.status_code == 403, f"{method} {path} → {r.status_code}: {r.text}"


async def test_lookalike_tool_relays_but_stays_deletable(clients: AsyncClient, monkeypatch):
    """A visitor-made tool with the pinned base also rides the wire (same privilege, no extra
    exposure — the key is still env-injected) but is NOT frozen: only the seeded 'stripe' is."""
    _enable_live(monkeypatch)
    m = await _mint(clients)
    r = await clients.post("/tools", headers=_h(m["token"]), json={
        "name": "mystripe", "base_url": "https://api.stripe.com/v1/charges", "bindings": []})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert (await clients.delete(f"/tools/{tid}", headers=_h(m["token"]))).status_code == 200


async def test_wire_off_leaves_seeded_tool_editable(clients: AsyncClient, monkeypatch):
    """Without the env key there's no live wire — the sandbox is the classic freely-editable
    studio, and a repointed stripe tool just synthesizes (nothing to protect)."""
    monkeypatch.setattr(api_mod.get_settings(), "demo_stripe_key", "")
    m = await _mint(clients)
    tools = (await clients.get("/tools", headers=_h(m["token"]))).json()
    stripe = next(t for t in tools if t["name"] == "stripe")
    r = await clients.patch(f"/tools/{stripe['id']}", headers=_h(m["token"]),
                            json={"base_url": "https://api.stripe.com/v1/customers"})
    assert r.status_code == 200, r.text
    r = await clients.post("/call/https://api.stripe.com/v1/customers/x", headers=_h(m["token"]),
                           content="ignored")
    assert r.json().get("sandbox") is True  # dummy, NOT a real relay


async def test_other_sandbox_tools_still_synthesize(clients: AsyncClient, monkeypatch):
    _enable_live(monkeypatch)
    m = await _mint(clients)
    await clients.post("/tools", headers=_h(m["token"]), json={
        "name": "mine", "base_url": "http://upstream/anything",
        "bindings": []})
    r = await clients.get("/call/mine/x", headers=_h(m["token"]))
    assert r.json().get("sandbox") is True


async def test_live_calls_are_rate_limited_per_ip(clients: AsyncClient, monkeypatch):
    _enable_live(monkeypatch)
    monkeypatch.setattr(api_mod, "PUBLIC_DEMO_RATE_MAX", 2)
    m = await _mint(clients)
    for _ in range(2):
        assert (await clients.post("/call/https://api.stripe.com/v1/charges", headers=_h(m["token"]),
                                   content="amount=1")).status_code == 200
    r = await clients.post("/call/https://api.stripe.com/v1/charges", headers=_h(m["token"]),
                           content="amount=1")
    assert r.status_code == 429
