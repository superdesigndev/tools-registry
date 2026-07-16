"""URL-passthrough + faithful relay.

The agent passes the REAL upstream URL (it already knows the API); treg resolves the tool by
host + longest base_url prefix, injects, and relays everything verbatim (methods, all query
params incl. duplicates, arbitrary headers, cookies, body) — touching only transport headers,
our control token, and the injected credential.
"""

from __future__ import annotations

from httpx import AsyncClient


async def _register(c: AsyncClient, name: str, base_url: str, value: str = "SEK") -> None:
    sid = (await c.post("/secrets", json={"name": f"{name}-k", "value": value})).json()["id"]
    r = await c.post("/tools", json={"name": name, "base_url": base_url, "secret_id": sid})
    assert r.status_code == 200, r.text


async def test_passthrough_resolves_by_url_and_injects(clients: AsyncClient):
    await _register(clients, "intercom", "https://api.intercom.io")
    r = await clients.get("/call/https://api.intercom.io/echo?per_page=5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["auth"] == "Bearer SEK"          # credential injected by treg
    assert d["query"]["per_page"] == "5"      # caller's real query preserved


async def test_duplicate_query_params_preserved(clients: AsyncClient):
    await _register(clients, "ex", "https://api.ex.com")
    r = await clients.get("/call/https://api.ex.com/echo?tag=a&tag=b&tag=c")
    qm = [tuple(p) for p in r.json()["query_multi"]]
    assert qm.count(("tag", "a")) == 1 and ("tag", "b") in qm and ("tag", "c") in qm  # all three kept


async def test_caller_headers_and_cookies_passthrough_and_token_stripped(clients: AsyncClient):
    await _register(clients, "hx", "https://api.hx.com")
    r = await clients.get(
        "/call/https://api.hx.com/echo",
        headers={"X-Custom": "v1", "Cookie": "a=1; b=2"},
    )
    h = r.json()["headers"]
    assert h["x-custom"] == "v1"               # arbitrary caller header relayed
    assert h["cookie"] == "a=1; b=2"           # cookies relayed verbatim
    assert "x-treg-token" not in h             # our control header never leaks upstream


async def test_control_infra_headers_and_treg_cookie_stripped(clients: AsyncClient):
    await _register(clients, "sec", "https://api.sec.com")
    r = await clients.get(
        "/call/https://api.sec.com/echo",
        headers={
            "X-Treg-Org": "superdesign", "ngrok-skip-browser-warning": "1",
            "X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https", "Via": "1.1 edge",
            "X-Keep": "yes", "Cookie": "treg_session=SECRET; keep=1; treg_oauth_state=xyz",
        },
    )
    h = r.json()["headers"]
    for leak in ("x-treg-org", "ngrok-skip-browser-warning", "x-forwarded-for", "x-forwarded-proto", "via"):
        assert leak not in h, f"{leak} leaked upstream"
    assert h["x-keep"] == "yes"        # unrelated caller header preserved
    assert h["cookie"] == "keep=1"     # treg's own cookies scrubbed, other cookies kept


async def test_longest_prefix_wins(clients: AsyncClient):
    await _register(clients, "broad", "https://api.g2.com", value="BROAD")
    await _register(clients, "narrow", "https://api.g2.com/v2", value="NARROW")
    r = await clients.get("/call/https://api.g2.com/v2/echo")
    assert r.json()["auth"] == "Bearer NARROW"  # the more specific tool is chosen


async def test_ambiguous_host_409(clients: AsyncClient):
    await _register(clients, "g1", "https://api.same.com")
    await _register(clients, "g2", "https://api.same.com")  # identical base -> tie
    r = await clients.get("/call/https://api.same.com/echo")
    assert r.status_code == 409


async def test_unknown_upstream_404(clients: AsyncClient):
    r = await clients.get("/call/https://nope.example.com/echo")
    assert r.status_code == 404


async def test_named_form_still_works(clients: AsyncClient):
    await _register(clients, "echo", "https://api.named.com")
    r = await clients.get("/call/echo/echo")  # <tool>/<path>
    assert r.status_code == 200
    assert r.json()["auth"] == "Bearer SEK"


async def test_orgs_reports_tool_count(clients: AsyncClient):
    """/orgs carries tool_count so the dashboard can land on the org that actually has tools
    (not a first-run default that may be an empty team)."""
    orgs = (await clients.get("/orgs")).json()
    assert orgs and all("tool_count" in o for o in orgs)
    assert sum(o["tool_count"] for o in orgs) == 0          # fresh account, no tools yet
    await _register(clients, "stripe", "https://api.stripe.com/v1")
    orgs = (await clients.get("/orgs")).json()
    assert sum(o["tool_count"] for o in orgs) == 1          # the count reflects the registered tool


async def test_encoded_slash_preserved_named_form(clients: AsyncClient):
    """An encoded slash in the path must reach the upstream still encoded (`%2f`, not `/`) —
    npm's scoped publish route (`PUT /@scope%2fname`) 404s if the proxy decodes it."""
    await _register(clients, "npmreg", "https://registry.npm.test")
    r = await clients.put("/call/npmreg/@superdesign%2ftreg", content=b"{}")
    assert r.status_code == 200, r.text
    assert r.json()["raw_path"].endswith("/@superdesign%2ftreg")


async def test_encoded_slash_preserved_passthrough_form(clients: AsyncClient):
    await _register(clients, "npmreg2", "https://registry.npm2.test")
    r = await clients.put("/call/https://registry.npm2.test/@superdesign%2ftreg", content=b"{}")
    assert r.status_code == 200, r.text
    assert r.json()["raw_path"].endswith("/@superdesign%2ftreg")
