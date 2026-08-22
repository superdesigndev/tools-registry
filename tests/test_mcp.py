"""The MCP front door — that it answers, and that it enforces the same rules as every other door.

Two halves, and the second is the one that matters. A new entrance onto a paid catalog is only safe
if it cannot see another team's data, cannot spend past the caps, and cannot be talked into serving
an endpoint whose price nobody knows. `mcp.py` gets that by routing through treg's own API rather
than reaching into the internals — these tests are what proves the routing is real and not merely
intended.

The transport is exercised as a real MCP client would: JSON-RPC over the mounted ASGI app.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app

pytestmark = pytest.mark.anyio

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def _rpc(client: AsyncClient, method: str, params=None, token: str | None = None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    # An ABSOLUTE url so the Host header is a real one. The transport enforces DNS-rebinding
    # protection, and the suite's base_url ("http://registry") is deliberately not on the
    # allow-list — see test_an_unknown_host_is_refused.
    r = await client.post("http://localhost/mcp/", json=body, headers=headers)
    return r


async def _call_tool(client: AsyncClient, name: str, args: dict, token: str | None = None) -> dict:
    await _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}}, token)
    r = await _rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    payload = r.json()
    content = (payload.get("result") or {}).get("content") or []
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except ValueError:
            return {"_text": content[0]["text"]}
    return payload


@asynccontextmanager
async def mcp_session(client: AsyncClient):
    """Run the MCP lifespan around a block of requests.

    Deliberately NOT a fixture. The lifespan holds an anyio task group, and a fixture enters it in
    one task and exits it in another — anyio refuses that ("Attempted to exit cancel scope in a
    different task"). Entering it inside the test body keeps both ends in the same task.

    That the mounted app needs its lifespan run at all is the trap the module docstring warns about:
    `app.mount()` does not run it, and the transport builds its task group there. It caught me here
    first, which is the cheapest place for it to happen.

    The default `X-Treg-Token` is dropped: MCP carries identity in `Authorization`, and leaving a
    second credential on the client would let a test pass for the wrong reason.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient as _AC

    from treg import mcp as _mcp

    # A FRESH transport per test: `StreamableHTTPSessionManager.run()` may be called once per
    # instance, so the module-level one cannot be restarted between tests. The tools are the same
    # objects either way — they hang off the shared `mcp` server — and they still reach the real
    # `treg.api.app` internally, so the enforcement under test is the production path.
    fresh = _mcp.build_mcp_app()
    host = FastAPI()
    host.mount("/mcp", fresh)
    client.headers.pop("X-Treg-Token", None)
    async with _mcp.mcp_lifespan(fresh):
        async with _AC(transport=ASGITransport(app=host), base_url="http://localhost") as mc:
            mc.headers.update({k: v for k, v in client.headers.items() if k.lower() == "cookie"})
            yield mc


async def test_the_server_lists_exactly_the_six_tools(clients):
    """Six tools, not 2,600. The catalog is DATA reached through a tool, never a tool per endpoint —
    2,600 schemas would bury the model's context and make the catalog unusable."""
    token = (await clients.post("/users", json={"email": "lister@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}, token)
        r = await _rpc(c, "tools/list", token=token)
        names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"catalog_search", "catalog_get", "call", "balance", "my_tools", "catalog_request"}


async def test_catalog_search_returns_priced_results(clients):
    """Search is the entry point: an agent asks for a task and gets endpoints with prices. Needs a
    credential now, like every tool — see test_EVERY_tool_needs_a_credential_including_the_catalog."""
    token = (await clients.post("/users", json={"email": "searcher@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 3}, token=token)
    assert out["results"], out
    first = out["results"][0]
    assert first["endpoint_id"] and first["provider"]
    assert "usd_per_call" in first and "no_key_needed" in first


async def test_no_key_needed_is_false_when_the_deploy_holds_no_key(clients):
    """`no_key_needed` must mean "THIS deploy will serve it on treg's key", not "the row is priced".
    The test env configures no platform keys at all, so every result — however eligible its price —
    must say False; anything else re-opens the advertise-then-refuse gap this field had."""
    token = (await clients.post("/users", json={"email": "keyless@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 5}, token=token)
    assert out["results"]
    assert all(r["no_key_needed"] is False for r in out["results"]), out["results"]


async def test_search_says_so_when_nothing_matches(clients):
    token = (await clients.post("/users", json={"email": "nomatch@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "zzzz-no-such-capability"}, token=token)
    assert out["count"] == 0
    assert "catalog_request" in out.get("hint", "")  # the empty result names the way to file the gap

    # And the miss is the record (models.SearchMiss) — this tool reads the catalog in-process, so
    # the HTTP route's logging never sees it; the tool must log its own.
    from sqlmodel import select

    from treg import audit
    from treg.db import session_maker
    from treg.models import SearchMiss

    await audit.drain()
    async with session_maker() as s:
        (row,) = (await s.execute(select(SearchMiss))).scalars()
    assert row.query == "zzzz-no-such-capability"
    assert row.source == "mcp"


async def test_catalog_request_files_the_gap_with_attribution(clients):
    """The zero-result hint's payoff: the agent can file the missing capability in the same session,
    and the stored row says who asked (the bearer), not just that someone did."""
    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import ToolRequest

    token = (await clients.post("/users", json={"email": "wisher@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_request",
                               {"capability": "Ahrefs backlinks", "note": "for SEO audits"}, token=token)
    assert out.get("status") == "received", out
    async with session_maker() as s:
        (row,) = (await s.execute(select(ToolRequest))).scalars()
    assert row.capability == "Ahrefs backlinks"
    assert row.source == "mcp"
    assert row.user_email == "wisher@superdesign.dev"


# ---- the half that matters: no token means no data, no spending ----------------------------

@pytest.mark.parametrize("tool", ["call", "balance", "my_tools"])
async def test_every_spending_or_tenant_tool_refuses_without_a_token(clients, tool):
    """A public MCP endpoint onto a paid catalog is the whole risk of this feature. Anything that
    reads a team's data or moves its money must fail closed, and say what to do about it.

    The refusal is now an HTTP 401 carrying `WWW-Authenticate`, rather than an error dict inside a
    200 — see `test_a_protected_tool_answers_401_with_WWW_Authenticate` for why the shape matters.
    This test keeps its original job: proving these three cannot be reached without a credential."""
    args = {"endpoint_id": "tikhub.tiktok.video.comments"} if tool == "call" else {}
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}, headers=MCP_HEADERS)
    assert r.status_code == 401, r.text
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


async def test_a_bogus_token_gets_nothing(clients):
    """Headers are client-supplied input. A well-formed but unknown token must be rejected by the
    database, not accepted because it looks like one."""
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token="tok_not_a_real_token_at_all")
    assert "balance_usd" not in out, out
    assert out.get("error")


async def test_a_real_token_reads_its_OWN_balance(clients):
    """The positive case, and the one that proves the plumbing: a genuine token resolves to its org
    through `/auth/me` and reads that org's balance — the same route the CLI uses."""
    token = (await clients.post("/auth/cli-token")).json()["token"] if False else None
    # the suite's client was created with a real per-org token; recover it before it is dropped
    r = await clients.post("/users", json={"email": "mcpuser@superdesign.dev"})
    token = r.json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=token)
    assert "balance_usd" in out, out
    assert out["balance_usd"] >= 0


async def test_an_IDENTITY_token_resolves_its_team(clients):
    """The bug production found. There are two kinds of token: a PER-ORG token (`treg org agent-new`)
    has its team baked in and `/auth/me` reports it; an IDENTITY token (`treg login` — what most
    people actually hold) belongs to a person who may be in several teams, so `/auth/me` reports no
    org and every `/orgs/{id}/…` route must be told which one. Resolving only the first kind meant
    `balance` answered "could not resolve the team" for the commonest token there is."""
    r = await clients.post("/users", json={"email": "identity-user@superdesign.dev"})
    per_org = r.json()["token"]
    clients.headers["X-Treg-Token"] = per_org
    identity = (await clients.get("/auth/cli-token")).json()["token"]
    assert identity != per_org

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=identity)
    assert "balance_usd" in out, out
    assert out.get("team")


async def test_one_team_cannot_read_another_teams_tools(clients):
    """Tenant isolation, asserted rather than assumed. Two orgs, two tokens: each sees only its own
    registered tools. This is the property a second front door is most likely to lose."""
    a = (await clients.post("/users", json={"email": "org-a@superdesign.dev"})).json()["token"]
    b = (await clients.post("/users", json={"email": "org-b@superdesign.dev"})).json()["token"]
    clients.headers["X-Treg-Token"] = a
    made = await clients.post("/tools", json={"name": "a-only-tool", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        seen_a = await _call_tool(c, "my_tools", {}, token=a)
        seen_b = await _call_tool(c, "my_tools", {}, token=b)
    names_a = {t["name"] for t in seen_a.get("tools", [])}
    names_b = {t["name"] for t in seen_b.get("tools", [])}
    # Not vacuous: org A must genuinely SEE its tool, or "B cannot see it" proves nothing.
    assert names_a, f"org A saw no tools at all — the assertion below would pass for free: {seen_a}"
    assert "a-only-tool" in names_a
    assert "a-only-tool" not in names_b, f"org B saw org A's tool: {seen_b}"


async def test_call_reaches_the_TEAMS_OWN_tools_too(clients):
    """`my_tools` lists what the team registered; `call` must be able to call it.

    The first version pre-checked the catalog and refused anything absent, which made `my_tools` a
    list of things an agent could see and never use — found by trying it on production. `/call/`
    already resolves a team's own tool first and falls back to a catalog id, so the fix was to stop
    second-guessing it."""
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text
    token = clients.headers.get("X-Treg-Token")

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "echo/anything"}, token=token)
    assert out.get("status") == 200, out
    assert "unknown endpoint" not in json.dumps(out)


async def test_call_refuses_an_endpoint_that_does_not_exist(clients):
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "nope.not.real"}, token="tok_whatever")
    assert "unknown endpoint" in out.get("error", "")
    assert "catalog_search" in out.get("hint", "")   # names the way out, rather than leaving it guessing


async def test_tool_descriptions_do_not_promise_routing(clients):
    """The charter's standing rule, and the one the landing page already had to be corrected for:
    treg COMPARES providers and the caller chooses. These descriptions are read by every model that
    installs the plugin, so a false claim here travels further than the website's did."""
    token = (await clients.post("/users", json={"email": "descs@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}, token)
        r = await _rpc(c, "tools/list", token=token)
        blob = json.dumps(r.json()).lower()
    for claim in ("routes for you", "automatic failover", "fails over", "picks the best provider"):
        assert claim not in blob


async def test_an_unknown_host_is_refused(clients):
    """The SDK's DNS-rebinding protection, proven live rather than trusted.

    It ships ON with an EMPTY allow-list, which 421s EVERYTHING — a deploy looks healthy until the
    first tool call. `mcp._allowed_hosts()` builds the list from this deployment's `public_url` plus
    the loopback names, so this asserts both directions: a known host works (every other test) and an
    unknown one does not."""
    token = (await clients.post("/users", json={"email": "host@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        r = await c.post("http://evil.example.com/mcp/", json={"jsonrpc": "2.0", "id": 1,
                         "method": "tools/list"},
                         headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 421, r.text  # a credential passes auth; the HOST guard still refuses


async def test_the_deployments_own_host_is_allowed():
    """The list must contain the host treg actually answers on, or production 421s every call."""
    from urllib.parse import urlsplit

    from treg.config import get_settings
    from treg.mcp import _allowed_hosts

    assert urlsplit(get_settings().public_url).netloc in _allowed_hosts()


async def test_the_price_is_visible_before_spending(clients):
    """An agent that cannot see a price before calling cannot warn the human, and the skill's rule is
    to state the cost first. Search must carry the number."""
    token = (await clients.post("/users", json={"email": "pricer@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 5}, token=token)
    assert any(r.get("usd_per_call") is not None for r in out["results"])


# ---- what the plugin directory's review checks ---------------------------------------------

async def test_every_tool_declares_what_it_can_do(clients):
    """The submission portal validates `readOnlyHint`/`openWorldHint`/`destructiveHint` against the
    tool's real behaviour, and a model consults them before acting. The four reading tools change
    nothing; `call` is the honest exception — it relays whatever the caller asks to whichever
    upstream the endpoint names, which can be a POST that publishes, an email that sends or a DELETE.
    treg does not model the upstream, so it cannot promise the call is safe, and saying otherwise
    here would be a false assurance in the exact place it matters."""
    from treg.mcp import mcp as server

    ann = {t.name: t.annotations for t in await server.list_tools()}
    assert set(ann) == {"catalog_search", "catalog_get", "call", "balance", "my_tools",
                        "catalog_request"}
    for name in ("catalog_search", "catalog_get", "balance", "my_tools"):
        a = ann[name]
        assert a and a.read_only_hint is True, name
        assert a.destructive_hint is False and a.open_world_hint is False, name
    a = ann["call"]
    assert a.read_only_hint is False
    assert a.destructive_hint is True and a.open_world_hint is True
    # catalog_request writes (a row on treg itself) but touches nothing upstream and spends nothing.
    a = ann["catalog_request"]
    assert a.read_only_hint is False
    assert a.destructive_hint is False and a.open_world_hint is False


async def test_the_domain_challenge_is_404_until_configured(clients):
    """Empty means unset, and unset must 404. An empty 200 would read to the portal as a
    verification that never completes, which is harder to debug than a plain absence."""
    r = await clients.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 404


async def test_the_domain_challenge_returns_the_token_ALONE(clients, monkeypatch):
    """The portal is explicit: that URL must return the token and nothing else — not JSON, not a
    list, not several tokens. Returning a JSON body here is a documented rejection."""
    from treg import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "openai_apps_challenge", "tok-abc-123", raising=False)
    r = await clients.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200
    assert r.text == "tok-abc-123"
    assert r.headers["content-type"].startswith("text/plain")


async def test_a_BROWSER_origin_is_accepted(clients):
    """`"*"` is not a wildcard in this SDK — origins are compared literally, and only a `:*` port
    suffix is special. Setting `allowed_origins=["*"]` therefore allowed exactly one origin, the
    literal string "*", and refused every browser with "Invalid Origin header".

    Nothing caught it: the suite and every CLI client send NO Origin header, so the check never ran
    until a real web page called /mcp/. This test sends one deliberately."""
    from treg.config import get_settings

    origin = get_settings().public_url.rstrip("/")
    token = (await clients.post("/users", json={"email": "browser@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "browser", "version": "1"}}},
            headers={**MCP_HEADERS, "Origin": origin, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"a browser at our own origin must be served: {r.text[:120]}"


async def test_an_UNKNOWN_origin_is_still_refused(clients):
    """The protection has to remain real — the fix widens the list, it does not remove the check."""
    token = (await clients.post("/users", json={"email": "origin@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**MCP_HEADERS, "Origin": "https://attacker.example",
                     "Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text  # a credential passes auth; the ORIGIN guard still refuses


async def test_every_tool_declares_what_it_RETURNS(clients):
    """ChatGPT's connector review flags a tool with no output schema, and a model that has to guess
    at field names guesses. Each tool now declares its shape."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        assert t.output_schema, f"{t.name} has no output schema"
        assert t.output_schema.get("properties"), f"{t.name}'s schema is empty"


async def test_the_output_schema_does_not_BREAK_the_error_paths(clients):
    """The load-bearing detail. A strict schema is validated on the way out, so `{"error": "not
    authenticated"}` would RAISE instead of returning — turning a refusal written to tell an agent
    how to recover into an opaque tool failure.

    Every field is optional so both shapes pass. A schema is a hint to the model, not a gate on our
    own error handling."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        assert not t.output_schema.get("required"), (
            f"{t.name} has required output fields — the first error response will raise")

    # and prove it end to end, not just in the schema. A BAD token rather than none: a missing
    # credential is now answered at the transport with a 401, so it never reaches the tool — and it
    # is the tool's own error shape that has to survive the schema.
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token="not-a-real-token")
    assert out.get("error"), out
    assert "hint" in out or "detail" in out, "the recovery instruction must survive the schema"


async def test_the_schema_tolerates_NULLS_not_just_missing_keys(clients):
    """`total=False` says a key may be ABSENT; it does not say the value may be null, and nulls reach
    the client from two directions. Real rows carry them — a registered tool with no description, an
    endpoint with no published price. And the SDK serializes every response through the
    TypedDict-derived model, which fills ABSENT keys in as `null` in structuredContent — so a
    response that never mentions `next` still ships `"next": null`, and a strict client validating
    against a `type: string` schema refuses the whole answer with -32602 (issue #93, and a second
    independent report the same day).

    So EVERY field of EVERY tool must allow null, not just the ones known to carry data nulls.
    Asserted on the schema so the next field added is held to the same rule."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        schemas = [t.output_schema] + list(t.output_schema.get("$defs", {}).values())
        for schema in schemas:
            for field, spec in schema.get("properties", {}).items():
                # `Any` renders as an unconstrained schema (no type at all) — that accepts null.
                unconstrained = "type" not in spec and "anyOf" not in spec
                allows_null = unconstrained or "null" in str(spec)
                assert allows_null, (f"{t.name}: {schema.get('title')}.{field} does not allow null — "
                                     f"the SDK fills absent keys in as null, so a strict client "
                                     f"will refuse the whole response")


async def test_structured_content_VALIDATES_against_the_advertised_schema(clients):
    """End to end, as the two field reports arrived: a strict client validates structuredContent
    against the outputSchema from tools/list and refuses the response on any mismatch. The schema
    test above checks what we promise; this checks what we actually ship — with jsonschema playing
    the strict client, so a serialization change in the SDK cannot regress this silently."""
    import jsonschema

    from treg.mcp import mcp as server

    schemas = {t.name: t.output_schema for t in await server.list_tools()}
    token = (await clients.post("/users", json={"email": "strict@superdesign.dev"})).json()["token"]

    async with mcp_session(clients) as c:
        # The exact failing calls from the reports: a search (next set, hint absent) and the two
        # tools whose error shape carries teams/hint — plus a catalog_get error path.
        for tool, args in [("catalog_search", {"query": "backlinks"}),
                           ("catalog_get", {"endpoint_id": "no.such.endpoint"}),
                           # its example_response is an ARRAY of records, which a dict-typed field
                           # refused server-side — the third null/shape mismatch found in the wild
                           ("catalog_get", {"endpoint_id": "brightdata.linkedin.user.profile"}),
                           ("balance", {}),
                           ("my_tools", {})]:
            await _rpc(c, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "strict", "version": "1"}}, token)
            r = await _rpc(c, "tools/call", {"name": tool, "arguments": args}, token)
            structured = (r.json().get("result") or {}).get("structuredContent")
            assert structured is not None, f"{tool} returned no structuredContent: {r.text[:200]}"
            jsonschema.validate(structured, schemas[tool])  # raises on any mismatch


async def test_large_mcp_responses_are_gzipped_AT_THE_ORIGIN(clients):
    """The fix that actually works for edge re-compression (issue #100). Render's edge ignored
    `no-transform` and kept Brotli-compressing large responses — which a real client stack
    (httpx + brotlicffi) fails to decode and then hangs on. An edge only compresses what arrives
    UNCOMPRESSED, so the origin gzips its own responses: the edge passes them through, and gzip
    decodes via zlib everywhere.

    Asserted the way the field report arrived: a large catalog_get with `Accept-Encoding: br, gzip`
    (what httpx sends) must come back gzip — OUR encoding — not unencoded (which the edge would
    Brotli) and not br."""
    import json as _json

    token = (await clients.post("/users", json={"email": "gz@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gz", "version": "1"}}, token)
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "catalog_get",
                       "arguments": {"endpoint_id": "brightdata.linkedin.user.profile"}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}",
                     "Accept-Encoding": "br, gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip", dict(r.headers)
    # httpx transparently gunzips `.content` — that it parses is the end-to-end decode proof,
    # through the exact client library the field reports used.
    body = _json.loads(r.content)
    assert body.get("result"), body

    # And a client that does NOT accept gzip gets identity from us — GZipMiddleware respects
    # Accept-Encoding rather than forcing an encoding the client cannot decode.
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gz2", "version": "1"}}, token)
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "catalog_get",
                       "arguments": {"endpoint_id": "brightdata.linkedin.user.profile"}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}",
                     "Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers, dict(r.headers)


async def test_mcp_responses_forbid_edge_TRANSFORMS(clients):
    """Production sits behind Render's Cloudflare edge, which Brotli-compresses responses unless the
    origin says not to. At least one real client stack (httpx + brotlicffi, issue #93) dies decoding
    large compressed bodies and then hangs to its own timeout. `Cache-Control: no-transform` is the
    origin's standard "do not re-encode" (RFC 9111), and Cloudflare honours it; `no-store` rides
    along because these responses are per-caller and priced.

    Asserted on both an authenticated answer and the 401 challenge — the header wrapper is outermost
    precisely so the challenge path carries it too."""
    token = (await clients.post("/users", json={"email": "edge@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        challenged = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=MCP_HEADERS)
        answered = await _rpc(c, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "edge", "version": "1"}}, token)
    assert challenged.status_code == 401
    for r in (challenged, answered):
        assert r.headers.get("cache-control") == "no-store, no-transform", dict(r.headers)


# ---- refusing in the right SHAPE, not just refusing ------------------------------------------

@pytest.mark.parametrize("tool", ["call", "balance", "my_tools", "catalog_search", "catalog_get"])
async def test_a_protected_tool_answers_401_with_WWW_Authenticate(clients, tool):
    """The spec has a protected resource reply 401 with `WWW-Authenticate: Bearer
    resource_metadata="…"`, because that header is how a client DISCOVERS it must authenticate and
    where to start. A friendly sentence inside a 200 tells a human what happened and tells a program
    nothing.

    This passed unnoticed because ChatGPT authenticates up front. A client that connects first and
    discovers auth lazily — which the spec allows — would read 200 as success."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {}}}, headers=MCP_HEADERS)
    assert r.status_code == 401, r.text
    challenge = r.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer "), challenge
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge
    assert r.json()["resource_metadata"].endswith("/.well-known/oauth-protected-resource")


@pytest.mark.parametrize("tool,args", [("catalog_search", {"query": "backlinks"}),
                                       ("catalog_get", {"endpoint_id": "hunter.people.email.find"})])
async def test_EVERY_tool_needs_a_credential_including_the_catalog(clients, tool, args):
    """One rule instead of two. An earlier version left the catalog tools open so a client could
    browse before signing up, which made the contract "some tools need auth, some do not" — something
    each client has to learn by trying.

    This is about a predictable contract, not about hiding the catalog: /catalog/search is still
    public on the WEBSITE, which the landing page and `treg catalog search` both rely on."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}, headers=MCP_HEADERS)
    assert r.status_code == 401, r.text
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


async def test_EAGER_auth_challenges_initialize_itself(clients):
    """Eager, not lazy. Every treg tool needs auth, so there is nothing to browse anonymously — and
    the spec's canonical flow challenges the client's FIRST request (`initialize`) so OAuth runs
    before the session proceeds. Leaving it open was why a client showed "Connected" and never
    prompted. So `initialize` and `tools/list` without a credential are 401 + WWW-Authenticate, same
    as a tool call."""
    async with mcp_session(clients) as c:
        for method in ("initialize", "tools/list"):
            params = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "t", "version": "1"}} if method == "initialize" else None
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params:
                body["params"] = params
            r = await c.post("http://localhost/mcp/", json=body, headers=MCP_HEADERS)
            assert r.status_code == 401, f"{method}: {r.status_code}"
            challenge = r.headers.get("www-authenticate", "")
            assert "resource_metadata=" in challenge and 'scope="' in challenge, challenge


async def test_a_notification_and_ping_pass_without_a_token(clients):
    """Not everything is challenged: a JSON-RPC notification carries no id and expects no response,
    and `ping` is the liveness check — 401ing either would be a challenge nobody can act on. Only
    id-bearing requests (initialize, tools/*, …) need a credential."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", headers=MCP_HEADERS,
                         json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code != 401, r.text
        r = await c.post("http://localhost/mcp/", headers=MCP_HEADERS,
                         json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code != 401, r.text


async def test_a_BAD_token_is_the_tool_s_business_not_the_transport_s(clients):
    """The challenge fires only when there is NO credential. Deciding whether a token is valid needs
    the database, and doing that in transport middleware would put a second authentication
    implementation in front of the first."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200
    assert "error" in json.loads(r.json()["result"]["content"][0]["text"])


async def test_call_accepts_an_ARRAY_body(clients):
    """DataForSEO — the largest provider in the catalog at 217 endpoints — takes an ARRAY of task
    objects on every one of its `live` POST routes. A dict-only `params` made all of them uncallable
    with a pydantic type error, which reads as "you passed it wrong" rather than "this tool cannot
    express that".

    Found by trying a real call for the demo, not by reading the signature."""
    token = (await clients.post("/users", json={"email": "arraybody@superdesign.dev"})).json()["token"]
    # register the tool AS THAT TOKEN's org — a tool created in another org is correctly invisible
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    if prev:
        clients.headers["X-Treg-Token"] = prev
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/anything", "method": "POST",
            "params": [{"keyword": "payment api", "depth": 10}]}, token=token)
    assert out.get("status") == 200, out
    assert "validation error" not in json.dumps(out)


async def test_a_list_is_refused_for_a_GET_with_a_clear_reason(clients):
    """A query string is key/value pairs, so a list has no meaning there. Saying so beats a pydantic
    trace that blames the caller for the tool's own limitation."""
    token = (await clients.post("/users", json={"email": "listget@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "hunter.people.email.find", "params": [{"x": 1}]}, token=token)
    assert "must be an object, not a list" in json.dumps(out), out


def test_a_relayed_402_carries_NO_link_out(clients):
    """ChatGPT's submission form asks whether a plugin "links or directs users out of ChatGPT to make
    purchases", and only PHYSICAL goods can be supported. treg sells prepaid API credit — a digital
    good — so a top-up link made the honest answer a yes in the one category they cannot support.

    Asserted on a REAL 402 body, not on the source. My first version checked the module's text,
    passed, and shipped a production response that still contained the link: the body nests under
    `detail` and repeats the URL inside a prose `message`. Checking the code instead of the response
    is precisely the failure this codebase keeps catching, and I wrote one.
    """
    from treg.mcp import _without_purchase_pointers

    real = {"detail": {
        "error": "insufficient_balance",
        "message": ("akta.companies.enrich would cost ~$0.875 on treg's akta key and this team's "
                    "balance is $0.5765.\n  add funds:      https://treg.to/app#billing"
                    "\n  or use your own key: treg connections connect --provider akta"),
        "balance_micro": 576500, "estimated_cost_micro": 875000,
        "topup_url": "/app#billing", "provider": "akta"}}
    blob = json.dumps(_without_purchase_pointers(real))
    assert "http://" not in blob and "https://" not in blob, blob
    assert "topup_url" not in blob, blob


def test_stripping_the_link_keeps_the_DIAGNOSIS(clients):
    """Removing the invitation to pay must not remove the explanation. An agent still needs to know
    it ran out of money, how short it was, and that its own key is an alternative — otherwise the
    refusal is indistinguishable from a broken endpoint."""
    from treg.mcp import _without_purchase_pointers

    real = {"detail": {
        "error": "insufficient_balance",
        "message": ("akta.companies.enrich would cost ~$0.875 and this team's balance is $0.5765."
                    "\n  add funds:      https://treg.to/app#billing"
                    "\n  or use your own key: treg connections connect --provider akta"),
        "balance_micro": 576500, "estimated_cost_micro": 875000}}
    out = _without_purchase_pointers(real)
    blob = json.dumps(out)
    assert "insufficient_balance" in blob and "would cost" in blob
    assert "connections connect" in blob, "the own-key alternative must survive"
    assert out["detail"]["balance_micro"] == 576500


def test_the_strip_does_not_depend_on_which_host_we_run_as(clients):
    """A first version matched `public_url`, which differs per environment — so it stripped nothing
    anywhere except production, and the local test passed while the deployed behaviour was wrong."""
    from treg.mcp import _without_purchase_pointers

    for host in ("https://treg.ngrok.app", "http://127.0.0.1:18790", "https://anything.example"):
        out = _without_purchase_pointers({"m": f"pay here: {host}/app#billing"})
        assert "http" not in json.dumps(out), host


async def test_a_402_THROUGH_THE_CALL_TOOL_carries_no_link(clients, monkeypatch):
    """The test the previous two should have been. They exercised `_without_purchase_pointers`
    directly and passed even with the strip DELETED from `call` — the helper worked and nothing
    connected it to the response a user sees.

    This drives the real path: drain the balance, call a metered endpoint through the MCP tool, and
    assert on what comes back.
    """
    from sqlmodel import select

    from treg.config import get_settings
    from treg.db import session_maker
    from treg.models import Org

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATKEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()

    token = (await clients.post("/users", json={"email": "broke402@superdesign.dev"})).json()["token"]
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    if prev:
        clients.headers["X-Treg-Token"] = prev
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
        org.balance_micro = 0            # spent out
        db.add(org)
        await db.commit()

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "tikhub.tiktok.video.comments",
            "params": {"aweme_id": "7"}}, token=token)
    get_settings.cache_clear()

    assert out.get("status") == 402, out
    blob = json.dumps(out)
    assert "http://" not in blob and "https://" not in blob, f"a link out survived: {blob}"
    assert "topup_url" not in blob, blob
    assert "not enough for this call" in blob, "the diagnosis must survive"


# ---- the expired-token challenge: what makes silent refresh possible -------------------------

async def test_an_EXPIRED_access_token_gets_401_invalid_token(clients):
    """RFC 6750 §3.1. A client whose access token expired presents it anyway; the resource answers
    401 with `error="invalid_token"`, and THAT is the cue on which the client silently runs its
    refresh grant. Our first challenge only covered the missing-header case, so an expired token
    sailed through to the tool's friendly prose in a 200 — and Claude Code, told nothing, gave up
    with "requires re-authorization" instead of refreshing."""
    from treg import mcp_oauth
    dead = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=mcp_oauth.mcp_resource_url(),
                                       scope="treg:call", ttl=-60)  # born expired
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {dead}"})
    assert r.status_code == 401, r.text
    challenge = r.headers.get("www-authenticate", "")
    assert 'error="invalid_token"' in challenge, challenge
    assert "resource_metadata=" in challenge
    assert r.json()["error"] == "invalid_token"


async def test_a_wrong_audience_access_token_gets_401_invalid_token(clients):
    """A grant consented to a DIFFERENT resource must not be honoured here — and the refusal should
    still be the machine-readable 401, not tool prose."""
    from treg import mcp_oauth
    other = mcp_oauth.make_access_token(user_id=7, org_id=3,
                                        audience="https://evil.example/mcp/", scope="treg:call")
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {other}"})
    assert r.status_code == 401, r.text
    assert 'error="invalid_token"' in r.headers.get("www-authenticate", "")


async def test_a_per_org_token_still_reaches_the_tool(clients):
    """Only bearers that CLAIM to be our OAuth access tokens are judged by the transport. A per-org
    or identity token (the Codex env-var path) is the API's to validate downstream — the middleware
    must pass it through, valid or not, rather than mislabel it `invalid_token` (its holder has no
    refresh grant to run)."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": "Bearer not-an-oauth-token-shape"})
    assert r.status_code == 200, r.text  # the tool answers (with its own error prose) — not a 401


async def test_call_passes_an_idempotency_key_through(clients):
    """The feature was built for agents and MCP is the agent path, so leaving `call` unable to send a
    key made it unreachable from the surface it was for.

    The key is the CALLER's, never derived from the request: two identical searches an hour apart are
    new work, not a retry, and a server-invented key would hand back the stale answer — a 24-hour
    cache wearing an idempotency badge."""
    token = (await clients.post("/users", json={"email": "mcpidem@superdesign.dev"})).json()["token"]
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    if prev:
        clients.headers["X-Treg-Token"] = prev
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/anything", "method": "POST",
            "params": {"x": 1}, "idempotency_key": "agent-retry-1"}, token=token)
    assert out.get("status") == 200, out


async def test_the_key_is_optional_and_described_for_the_model(clients):
    """A model can only use it if the description says WHEN. The distinction that matters is retry
    versus new work, because getting it wrong returns stale data rather than failing loudly."""
    from treg.mcp import mcp as server

    tool = [t for t in await server.list_tools() if t.name == "call"][0]
    assert "idempotency_key" in tool.input_schema["properties"]
    assert "idempotency_key" not in (tool.input_schema.get("required") or [])
    desc = tool.description or ""
    assert "repeating a call whose answer you did not receive" in desc
    assert "new call, not a retry" in desc, "the model must be told when NOT to reuse a key"


async def test_the_same_key_through_MCP_bills_once(clients, monkeypatch):
    """End to end on the agent path: an agent retries with the same key, the provider is reached
    once, and the balance moves once."""
    from sqlmodel import select

    from treg.config import get_settings
    from treg.db import session_maker
    from treg.models import Org

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATKEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()

    token = (await clients.post("/users", json={"email": "mcponce@superdesign.dev"})).json()["token"]
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    if prev:
        clients.headers["X-Treg-Token"] = prev

    async def balance() -> int:
        async with session_maker() as db:
            org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
            return org.balance_micro or 0

    args = {"endpoint_id": "tikhub.tiktok.video.comments", "params": {"aweme_id": "7"},
            "idempotency_key": "one-piece-of-work"}
    before = await balance()
    async with mcp_session(clients) as c:
        first = await _call_tool(c, "call", args, token=token)
    assert first.get("status") == 200, first
    charged = before - await balance()
    assert charged > 0, "the first call must bill"

    after_first = await balance()
    async with mcp_session(clients) as c:
        second = await _call_tool(c, "call", args, token=token)
    get_settings.cache_clear()

    assert second.get("status") == 200, second
    assert second.get("replayed") is True, "the retry must be marked as a replay"
    assert second.get("body") == first.get("body"), "and return the same answer"
    assert await balance() == after_first, "and bill nothing"


# ---------------------------------------------------------------------------------------------
# `call` request-shape parity with the CLI (`treg call`)
#
# Every flag on `treg call` exists because a real endpoint needed it — --query alongside --data
# (Bright Data's ?dataset_id=… + array body), --header (Google Ads' login-customer-id), raw
# non-JSON bodies, repeated query keys, inline ?a=b in a passthrough URL that httpx silently
# drops. The MCP tool must express the same shapes or a class of endpoints is CLI-only.
# The team-tool "echo" relays to the conftest upstream, which reports exactly what arrived.
# ---------------------------------------------------------------------------------------------

async def _register_echo(clients) -> str:
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text
    return clients.headers.get("X-Treg-Token")


async def test_call_sends_query_AND_body_together(clients):
    """The Bright Data shape: a POST whose routing lives in the query string and whose input is
    the body. `params` alone cannot say both — `query` + `body` can."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/trigger", "method": "POST",
            "query": {"dataset_id": "gd_123", "format": "json"},
            "body": [{"url": "https://example.com/in/someone"}]}, token=token)
    assert out.get("status") == 200, out
    seen = json.loads(out["body"]) if isinstance(out.get("body"), str) else out["body"]
    assert dict(seen["query_multi"])["dataset_id"] == "gd_123"
    assert json.loads(seen["body"]) == [{"url": "https://example.com/in/someone"}]


async def test_call_relays_extra_headers_but_never_tregs_own(clients):
    """--header parity (login-customer-id is the canonical need). treg's own auth/routing headers
    are filtered from the relay: the MCP bearer IS the identity and must not be maskable."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/ads",
            "headers": {"login-customer-id": "1234567890", "X-Treg-Token": "evil",
                        "Authorization": "Bearer evil"}}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    assert seen["headers"].get("login-customer-id") == "1234567890"
    assert seen["headers"].get("x-treg-token") != "evil"


async def test_call_sends_a_raw_string_body_with_content_type(clients):
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/xml", "method": "POST",
            "body": "<xml>hi</xml>", "content_type": "application/xml"}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    assert seen["body"] == "<xml>hi</xml>"
    assert seen["headers"].get("content-type") == "application/xml"


async def test_call_string_body_sniffs_json_and_implies_post(clients):
    """A string body that parses as JSON travels as application/json (the CLI's sniff rule), and
    giving a body without a method means POST (curl's convention)."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/j", "body": '{"a": 1}'}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    assert seen["headers"].get("content-type") == "application/json"
    assert json.loads(seen["body"]) == {"a": 1}


async def test_call_repeated_query_keys_survive(clients):
    """?tag=a&tag=b — a dict keeps only the last; a list value must expand to repeated keys."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/multi", "query": {"tag": ["a", "b"], "one": "x"}}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    tags = [v for k, v in seen["query_multi"] if k == "tag"]
    assert tags == ["a", "b"]


async def test_call_inline_query_in_a_passthrough_path_is_not_dropped(clients):
    """httpx DROPS a URL's existing query string whenever params= is passed — the upstream then
    answers with default/wrong data and NO error. The CLI guards this; the MCP tool must too."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/graph/me?fields=id,name", "query": {"limit": "5"}}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    q = dict(seen["query_multi"])
    assert q.get("fields") == "id,name", "the inline query must reach the upstream"
    assert q.get("limit") == "5"


async def test_call_refuses_ambiguous_params_plus_explicit_slot(clients):
    """`params` claiming the same position as an explicit slot is refused loudly — a silent
    merge is how a wrong request gets sent."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/x", "method": "POST",
            "params": {"a": 1}, "body": {"b": 2}}, token=token)
        assert "`body` OR `params`" in out.get("error", ""), out
        out2 = await _call_tool(c, "call", {
            "endpoint_id": "echo/x", "method": "GET",
            "params": {"a": "1"}, "query": {"b": "2"}}, token=token)
        assert "`query` OR `params`" in out2.get("error", ""), out2


async def test_call_resolves_the_team_for_an_identity_token(clients):
    """The same production bug `balance` had, on the spending path: an IDENTITY token (`treg
    login` — what most people hold) belongs to a person who may be in several teams, and /call
    answers it with a raw "choose an org (send X-Treg-Org)" 400 — a header hint an MCP caller
    cannot act on. `call` must resolve the team exactly as `balance` does."""
    r = await clients.post("/users", json={"email": "call-identity@superdesign.dev"})
    per_org = r.json()["token"]
    clients.headers["X-Treg-Token"] = per_org
    identity = (await clients.get("/auth/cli-token")).json()["token"]
    made = await clients.post("/tools", json={"name": "echo2", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "echo2/ping"}, token=identity)
    assert out.get("status") == 200, out
    assert "choose an org" not in json.dumps(out)


# ---- the bug reports of 2026-08-17: a day of real calls, five things that cost the caller -------
async def test_an_id_that_misses_by_one_segment_names_the_real_one(clients):
    """`lusha.companies-signals` for `lusha.x.companies-signals` — what a model produces relaying an
    id through a summary. It broke search → get → call at its FIRST step, and "use catalog_search"
    sends the agent back to the step that produced the wrong id in the first place."""
    token = (await clients.post("/users", json={"email": "nearmiss@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        got = await _call_tool(c, "catalog_get", {"endpoint_id": "lusha.companies-signals"}, token=token)
        called = await _call_tool(c, "call", {"endpoint_id": "lusha.companies-signals"}, token=token)
    assert got["did_you_mean"] == ["lusha.x.companies-signals"]
    assert called["did_you_mean"] == ["lusha.x.companies-signals"]
    assert "lusha.x.companies-signals" in called["hint"]


async def test_a_boolean_query_param_goes_on_the_wire_as_a_boolean(clients):
    """`str(True)` is `"True"`, which every upstream that documents a boolean rejects. It bit
    hardest where it cost money: `simplified=true` is thecompaniesapi's FREE mode, so the mangled
    flag pushed callers onto the paid path for a query they had asked to preview for nothing."""
    from treg import mcp as _mcp
    assert _mcp._qs_value(True) == "true"
    assert _mcp._qs_value(False) == "false"
    assert _mcp._qs_value(1) == "1" and _mcp._qs_value("x") == "x"
    assert _mcp._qs_value({"a": 1}) == '{"a":1}'      # never Python's single-quoted repr


async def test_catalog_query_arrays_use_the_endpoints_declared_wire_encoding(monkeypatch):
    """A structured MCP list is not synonymous with repeated query keys.

    Meta declares one compact JSON array value for every array on the endpoint, while an unmodelled
    team tool keeps the longstanding repeated-key default. This goes through `call`, not only
    `_query_values`: the first version stayed green if the MCP call site stopped using the helper.
    """
    from treg import catalog_store as cs
    from treg import mcp as _mcp

    cat = cs.load()
    meta = cat.by_id["meta-ad-library.meta-ads.library.search"]
    assert _mcp._query_values(meta, "ad_reached_countries", ["US"]) == ['["US"]']
    assert _mcp._query_values(None, "tag", ["a", "b"]) == ["a", "b"]

    captured = {}

    class _Client:
        headers: dict = {}

        async def request(self, method, path, **kwargs):
            import httpx
            captured.update(method=method, path=path, **kwargs)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, path))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _api(_token):
        yield _Client()

    async def _org(_client):
        return 1, "team", None

    monkeypatch.setattr(_mcp, "_api", _api)
    monkeypatch.setattr(_mcp, "_resolve_org", _org)
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    await _mcp.call("meta-ad-library.meta-ads.library.search", params={
        "ad_reached_countries": ["US"],
    }, ctx=ctx)
    assert captured["params"] == [("ad_reached_countries", '["US"]')]


async def test_an_unset_query_param_is_omitted_rather_than_sent_as_None(clients):
    """`None` means "no value", and `?limit=None` is not that — it is a string an upstream parses."""
    assert (await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})).status_code == 200
    token = clients.headers.get("X-Treg-Token")
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "echo/anything",
                                           "params": {"kept": True, "off": False,
                                                      "dropped": None}}, token=token)
    sent = (out.get("body") or {}).get("query") or {}
    assert sent == {"kept": "true", "off": "false"}


async def test_search_survives_missing_a_few_words_of_an_agent_sentence(clients):
    """Agents query in sentences, and demanding every word zeroed real ones (models.SearchMiss,
    2026-08-19): "company job postings hiring open jobs linkedin" found nothing while three
    endpoints matched 6 of its 7 words — the only miss was "linkedin" on rows shelved under
    `companies`, or "open" on the one shelved under `linkedin`. A query may now miss one word in
    three, and idf weighting keeps the order on the rare words rather than the filler."""
    from treg import catalog_store as cs
    cat = cs.load()
    # the two logged SearchMiss queries, verbatim
    rows, total = cs.search("company job postings hiring open jobs linkedin", cat, 8)
    assert total > 0
    assert {"apollo.companies.jobs", "apify.linkedin.search.jobs",
            "leadmagic.x.jobs-search-v3"} <= {ep["id"] for ep, _ in rows}
    # rank 1 must be the JOB (a companies.search row), never pinned to one provider — any newly
    # added provider of the same capability may legitimately outscore the incumbents
    rows, _ = cs.search("company search filter by location industry headcount growth", cat, 8)
    assert rows and rows[0][0]["capability"] == "companies.search"
    # the refinement property the old rule protected still holds: with one or two words there is no
    # miss allowance, so "tiktok comments" must not return every tiktok endpoint
    _, both = cs.search("tiktok comments", cat, 1)
    _, all_tiktok = cs.search("tiktok", cat, 1)
    assert 0 < both < all_tiktok
    for ep, _ in cs.search("tiktok comments", cat, 10**6)[0]:
        fields = cs._haystacks(ep, cat)
        assert any("tiktok" in t for _, t in fields) and any("comment" in t for _, t in fields)
    # aliases.yaml bridges the agent's word into the catalog's word at the same weight: the catalog
    # says "crypto", nobody's endpoint text says "cryptocurrency", and substring containment only
    # works in one direction
    rows, total = cs.search("current price of a cryptocurrency", cat, 3)
    assert total and rows[0][0]["id"].startswith("coingecko."), [ep["id"] for ep, _ in rows]
    # function words are dropped before the miss allowance is computed, so they cannot crowd out
    # the words that select ("on", "this" are not evidence about any endpoint)
    rows, _ = cs.search("trending repositories on github this week", cat, 3)
    assert rows and rows[0][0]["id"] == "scrapecreators.x.v1-github-trending-repositories"
    # single letters can never select: "K&L" must not let k + l decide admission, and the company
    # job ("enrich by name") must lead instead of 67 rows of noise (logged miss, 2026-08-20)
    rows, total = cs.search("K&L Gates company lookup", cat, 8)
    assert 0 < total < 30 and rows[0][0]["capability"].startswith("companies.")
    # the jobs rows must survive an industry qualifier the catalog never says ("law firm"), via
    # the openings->postings and firm->company aliases (logged miss, 2026-08-20)
    rows, total = cs.search("law firm job openings hiring signal", cat, 8)
    assert total and {"apollo.companies.jobs", "apify.linkedin.search.jobs"} <= {ep["id"] for ep, _ in rows}
    # a zero-result answer names the rows just under the gate and the exact unmatched words
    near = cs.near_misses("law firm dinosaur excavation permits", cat)
    assert all(n["missing"] for n in near) if near else True
    zero_q = "resolve company name to linkedin slug"
    if cs.search(zero_q, cat, 1)[1] == 0:
        assert cs.near_misses(zero_q, cat), "a zero result must surface its near-misses"


async def test_search_breaks_ties_on_what_treg_has_MEASURED(clients):
    """Token scoring ties by the dozen — every "ad library" match scores 6 — so with a default limit
    of 8 the rows an agent saw were decided by file order: seven tikhub rows, one of them
    uncallable, and the cheapest endpoint with a perfect record cut off below the fold."""
    from treg import catalog_store as cs
    cat = cs.load()
    ranked, _, truncated = cs.rank_band("ad library", cat, 8)
    assert not truncated, "24 matches sit well inside the band"
    ids = [ep["id"] for ep, _ in ranked]
    good, broken = "scrapecreators.x.v1-tiktok-ad-library-search", "tikhub.x.tiktok-ads-search-ads"
    assert {good, broken} <= set(ids), "both are in the band before any evidence is applied"

    stats = {good: {"samples": 16, "ok_rate": 1.0},
             broken: {"samples": 12, "ok_rate": 0.0}}
    reranked = cs.rerank(ranked, stats, cat)
    out = [ep["id"] for ep, _ in reranked]
    assert out[0] == good, "a perfect measured record wins its score group outright"
    # …and the one that has never once answered sinks to the bottom of that group. Not to the bottom
    # of the whole list: relevance still ranks above evidence, so a less relevant row stays below a
    # broken-but-more-relevant one rather than being promoted past it by a failure count.
    broken_score = next(s for ep, s in reranked if ep["id"] == broken)
    assert [ep["id"] for ep, s in reranked if s == broken_score][-1] == broken


async def test_balance_says_WHOSE_grant_and_which_team_by_name(clients):
    """A slug alone cannot be sanity-checked: `superdesign-7` looks plausible to the agent and to
    the human reading over its shoulder. The display name and the account that authorised the
    connection are what make a wrong team legible before the spending is noticed."""
    token = (await clients.post("/users", json={"email": "whose@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=token)
    assert out["team"] and out["team_name"]
    assert out["identity"] == "whose@superdesign.dev"
    # This one is a HEADER token, whose team is baked in — so it is labelled, but not sent to
    # `treg mcp grants`, which would list nothing for it. The OAuth half is asserted in
    # test_mcp_oauth.py::test_balance_tells_an_oauth_caller_how_to_move_the_team.
    assert "use-team" not in (out.get("hint") or "")


async def test_the_tie_band_covers_the_WHOLE_equal_scoring_group(clients, monkeypatch):
    """Reranking a slice that was already cut mid-tie cannot put back the row the cut dropped. The
    band therefore keeps taking while the score stays equal — and when a query ties so broadly that
    even the ceiling can't hold the group, it SAYS so rather than presenting an unranked tail as a
    ranked answer."""
    from treg import catalog_store as cs
    cat = cs.load()
    rows, total, truncated = cs.rank_band("ad library", cat, 8)
    scores = [s for _, s in rows]
    assert len(rows) > 8 and not truncated
    everything, _ = cs.search("ad library", cat, 10**6)
    assert scores.count(scores[-1]) == sum(1 for _, s in everything if s == scores[-1]), \
        "the group straddling the cut is taken whole, or the cut is still arbitrary"

    # The boundary the "observe, don't infer" fix exists for: a tie group that EXACTLY fills the
    # ceiling is NOT truncated. Inferring it from `len(kept) >= RERANK_BAND` answered True here and
    # told the caller to narrow a query that had in fact been ranked in full. "ad library" has a
    # 17-row group at limit=8, so a ceiling of 17 sits exactly on it and 16 cuts it.
    band_at = len(rows)
    monkeypatch.setattr(cs, "RERANK_BAND", band_at)
    assert cs.rank_band("ad library", cat, 8)[2] is False, "a group that exactly fills is not cut"
    monkeypatch.setattr(cs, "RERANK_BAND", band_at - 1)
    assert cs.rank_band("ad library", cat, 8)[2] is True, "one row short of the group IS a cut"
    monkeypatch.undo()

    # a single word ties across hundreds — bounded, and the bound is announced
    wide, wide_total, wide_trunc = cs.rank_band("tiktok", cat, 8)
    assert wide_total > cs.RERANK_BAND and wide_trunc
    assert len(wide) <= cs.RERANK_BAND
    token = (await clients.post("/users", json={"email": "wide@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "tiktok", "limit": 8}, token=token)
    assert "narrow" in (out.get("ranking_note") or ""), out.get("ranking_note")


async def test_a_near_miss_never_suggests_a_DIFFERENT_provider(clients):
    """A suggestion claims the caller mistyped. `apollo.people.email.find` is not a typo for
    `hunter.people.email.find` — it is another vendor, another price and another credential, so on a
    path that spends money that is provider routing wearing a spellcheck's clothes. treg compares
    providers and the caller chooses."""
    from treg import catalog_store as cs
    cat = cs.load()
    assert cs.near_ids("lusha.companies-signals", cat) == ["lusha.x.companies-signals"]
    for crossing in ("apollo.people.email.find", "fake.companies-signals", "hunter.tiktok.video.comments"):
        for suggested in cs.near_ids(crossing, cat):
            assert cat.by_id[suggested]["provider"] == crossing.split(".")[0], suggested

    # POSITIVE cases for the two id shapes the same-provider rule silently killed. Without these the
    # assertion above is vacuous for them — "suggests nothing" trivially satisfies "suggests nothing
    # cross-provider", which is how a fix that helped no hyphenated provider kept a green test.
    hyphenated = next(e for e in cat.by_id if e.startswith("google-ads.x."))
    assert cs.near_ids(hyphenated.replace(".x.", ".", 1), cat) == [hyphenated], \
        "a provider whose name contains a hyphen must still resolve"
    x_owned = next(e for e in cat.by_id if e.split(".")[0] == "x")
    assert x_owned in cs.near_ids(x_owned, cat), \
        "the provider literally named 'x' must not be erased by stripping the 'x' tier marker"


def test_a_header_token_is_not_told_to_run_a_command_that_lists_nothing():
    """`treg mcp grants` only has an answer for an OAuth grant. A header token already carries its
    own team, so pointing it at that command sends it to an empty list."""
    import asyncio
    from treg import mcp as _mcp

    class _Dead:
        headers: dict = {}
        async def get(self, *a, **k):
            raise RuntimeError("no api here — the label must degrade, not gate")

    plain = asyncio.run(_mcp._whose_grant(_Dead(), "superdesign", oauth=False))
    granted = asyncio.run(_mcp._whose_grant(_Dead(), "superdesign", oauth=True))
    assert "use-team" not in (plain.get("hint") or "")
    assert "use-team" in granted["hint"]
    assert plain["team"] == "superdesign", "and it still labels the team it does know"


async def test_the_SEARCH_TOOL_itself_ranks_on_evidence_not_just_the_helper(clients):
    """The helpers were tested; the wiring was not. `rerank()` could have been dropped from both
    call sites and every ranking test would still have passed, because they call the helper
    directly. This one goes through the MCP tool with real rows in the database."""
    from treg import endpoint_stats
    from treg.db import session_maker
    from treg.models import CallRecord

    broken = "apify.meta-ads.library.search"  # earlier in file order: rerank must move it
    good = "tikhub.x.tiktok-ads-search-ads"
    async with session_maker() as db:
        for status in (200, 200, 200, 200, 503):
            db.add(CallRecord(org_id=1, user_email="a@b.c", tool_name=good, method="GET", path="/x",
                              status_code=status, endpoint_id=good, duration_ms=100))
        for _ in range(5):      # the uncallable one, failing the way the report saw it
            db.add(CallRecord(org_id=1, user_email="a@b.c", tool_name=broken, method="POST", path="/x",
                              status_code=405, endpoint_id=broken, duration_ms=100))
        await db.commit()
    assert endpoint_stats.MIN_SAMPLES <= 5

    token = (await clients.post("/users", json={"email": "ranker@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "ad library", "limit": 25}, token=token)
    ids = [r["endpoint_id"] for r in out["results"]]
    assert ids.index(good) < ids.index(broken), ids
    good_row = next(r for r in out["results"] if r["endpoint_id"] == good)
    broken_row = next(r for r in out["results"] if r["endpoint_id"] == broken)
    assert good_row["works"] == 0.8 and broken_row["works"] == 0.0
