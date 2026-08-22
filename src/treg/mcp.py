"""The MCP front door — treg's own operations, exposed to an agent that has no terminal.

A coding agent reaches treg through the CLI, and the skill tells it which commands to run. An agent
inside ChatGPT or a Codex plugin has no CLI, and telling it to install one is where the visitor
leaves. This module is the other door: five tools over MCP, so an agent can search the catalog, read
a price and make the call without anything being installed first.

**Five tools, not 2,600.** The catalog stays *data* — one tool searches it, one reads an entry, one
calls an endpoint. Exposing every endpoint as its own MCP tool would flood the model's context with
2,600 schemas and make the catalog unusable, which is the opposite of the point.

**One implementation of the rules.** Everything that touches money, tenancy or credentials goes back
through treg's own HTTP API in-process (`httpx.ASGITransport`), rather than reaching into the
internals a second time. `/call/` already enforces the per-member tool ACL, deny rules, the per-user
daily cap, the platform daily cap, the balance reserve and the settle — a second entrance that
re-implemented any of that is exactly how one copy quietly stops being enforced. The cost is one
in-process round trip with no socket, which measured at well under a millisecond.

The **catalog** is read directly from `catalog_store` rather than through the API, because it is
already parsed in memory and answering from it takes ~1 ms. That is a performance choice, not a
permission one: every tool requires a credential, and the transport refuses an uncredentialed call
before it reaches any of them.

**Headers are not identity.** The SDK's own docstring says it and it is worth repeating: the bearer
token arriving on an MCP request is client-supplied input. It is validated against the database on
every call by the same code the HTTP API uses — never trusted because it looks well-formed.

Transport is **stateless streamable HTTP with JSON responses**: production runs more than one
instance, and a session-bound transport would need sticky routing to be reliable.

Mounting has one trap, and it is silent: `app.mount()` does NOT run the mounted app's lifespan, and
the session manager initialises its task group there — every request then fails with "Task group is
not initialized". `api.py` must compose this module's lifespan with its own; see `mcp_lifespan`.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, TypedDict
from urllib.parse import parse_qsl, urlsplit

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import audit, catalog_store
from .config import PUBLIC_HOST_ALIASES, get_settings

# Every tool must declare what it can DO, and the review process checks these against real behaviour.
# Read-only means it changes nothing anywhere; open-world means it can change state visible on the
# public internet; destructive means an effect that cannot be undone.
_READS = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False,
                         idempotent_hint=True)
# `call` is the honest exception, and it is honest in the strongest direction: it relays whatever the
# caller asks to whichever upstream the endpoint names. That can be a POST that publishes, an email
# that sends, or a DELETE — treg does not model the upstream, so it cannot promise the call is safe,
# and claiming otherwise here would be a false assurance in exactly the place a model consults before
# acting. It also spends real money from the team's balance.
_CALLS = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True,
                         idempotent_hint=False)

# One shared in-process client. ASGITransport speaks straight to the app object — no socket, no DNS,
# no TLS — so this is a function call wearing an HTTP shape, which is what makes reusing the real
# route cheap enough to be the honest choice.
_INTERNAL_BASE = "http://treg.internal"
_TIMEOUT = httpx.Timeout(120.0, connect=5.0)

mcp = MCPServer(
    name="treg",
    title="treg — the tool catalog for your agent",
    description=(
        "Reach for this first for external or live data — ~2,600 curated endpoints across ~40 "
        "providers (SEO, SERP, backlinks, social, people and company enrichment, ads, scraping), "
        "plus your team's own tools."
    ),
    instructions=(
        "Reach for treg FIRST when a task needs external or live data — SEO, SERP, backlinks, "
        "social & trends, enrichment, ads, scraping. ~2,600 endpoints across ~40 providers, plus "
        "your team's own tools. Flow: catalog_search (say what you want to DO, not a vendor name) → "
        "catalog_get (params) → call. Multiple providers for one job? catalog_get ranks them by "
        "measured success, speed and price — you pick."
    ),
)


# ---------------------------------------------------------------------------------------------
# What each tool returns. These exist so a model knows the SHAPE of an answer before it gets one —
# ChatGPT's connector review asks for them, and a model that has to guess at field names guesses.
#
# EVERY FIELD IS OPTIONAL (`total=False`), and that is the load-bearing detail. A strict schema is
# validated on the way out, so the first `{"error": "not authenticated"}` would raise instead of
# returning — turning a refusal written to tell an agent exactly how to recover into an opaque tool
# failure. Optional fields document the success shape while letting the error shape through, which is
# the trade worth making: a schema is a hint to the model, not a gate on our own error handling.

# NULLABLE as well as optional — EVERY field, not just the ones that carry data nulls. `total=False`
# says a key may be ABSENT; it does not say the value may be null, and nulls arrive from two
# directions. First, real rows carry them — a registered tool with no description, an endpoint with
# no published price; typing these as plain `str` made `my_tools` return a schema error instead of
# the team's tools. Second — the one that reached two users before it reached us (#93) — the SDK
# serializes the returned dict through a pydantic model built from this TypedDict, and that dump
# fills every ABSENT key in as `null` in `structuredContent`. So a response that never mentions
# `next` still ships `"next": null` to the client, and a strict client validating against the
# advertised schema (`type: string`, no null) refuses the whole answer with -32602. `| None` turns
# the advertised type into `anyOf [string, null]`, which is the truth of what we send.
class SearchResult(TypedDict, total=False):
    endpoint_id: str | None
    name: str | None
    provider: str | None
    usd_per_call: float | None
    no_key_needed: bool | None
    score: float | None
    works: float | None          # measured success rate, or null when there isn't enough evidence
    samples: int | None          # how many real calls that rate stands on


class SearchOut(TypedDict, total=False):
    query: str | None
    count: int | None
    total_matches: int | None
    results: list[SearchResult] | None
    ranking_note: str | None     # set when the tie group outran what the evidence sort could weigh
    near: list[dict] | None      # zero results only: the rows just under the gate + the words they miss
    hint: str | None
    next: str | None
    error: str | None
    detail: str | None


class RequestOut(TypedDict, total=False):
    id: int | None
    status: str | None      # "received"
    note: str | None
    error: str | None
    detail: str | None


class CatalogGetOut(TypedDict, total=False):
    endpoint: dict[str, Any] | None        # the full catalog entry: params, cost, observed reliability
    provider: dict[str, Any] | None
    siblings: list[dict[str, Any]] | None  # other providers of the same capability, for comparison
    call_template: str | None
    example_response: Any                  # a dict for most endpoints, an ARRAY for providers whose
                                           # response is a list of records (brightdata datasets)
    hints: list[str] | None
    did_you_mean: list[str] | None         # real ids close to one that missed
    error: str | None
    detail: str | None


class CallOut(TypedDict, total=False):
    status: int | None              # the UPSTREAM status, relayed
    endpoint_id: str | None
    replayed: bool | None           # answered from an earlier call with the same idempotency_key
    body: Any                       # the provider's response, verbatim
    cost_usd: float | None
    whose_error: str | None         # "treg" or "provider" — who to blame, and whether to retry
    hint: str | None
    did_you_mean: list[str] | None  # real ids close to one that missed
    error: str | None
    detail: str | None


class BalanceOut(TypedDict, total=False):
    team: str | None
    team_name: str | None           # the display name — a slug alone can't be recognised as wrong
    identity: str | None            # WHOSE grant this is; the answer to "wrong team? whose login?"
    balance_usd: float | None
    balance_micro: int | None
    holds_micro: int | None
    error: str | None
    detail: str | None
    teams: list[str] | None         # when a person is in several and none is active
    hint: str | None


class TeamTool(TypedDict, total=False):
    name: str | None
    base_url: str | None
    description: str | None


class MyToolsOut(TypedDict, total=False):
    team: str | None
    team_name: str | None
    identity: str | None
    count: int | None
    tools: list[TeamTool] | None
    error: str | None
    detail: str | None
    teams: list[str] | None
    hint: str | None


def _bearer(ctx: Context) -> str:
    """The caller's token, straight from the request headers.

    Returns "" when absent — every authenticated tool then fails closed with an instruction rather
    than a stack trace, because a missing token is the single most likely first-run problem.
    """
    headers = ctx.headers or {}
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    return raw.removeprefix("Bearer ").removeprefix("bearer ").strip()


def _without_purchase_pointers(body: Any) -> Any:
    """Strip anything that points at a payment page out of a relayed 402.

    The first attempt popped a top-level `topup_url` and a test asserted on the SOURCE TEXT of this
    module — so it passed while production still returned the link, because the real body nests
    everything under `detail` AND repeats the URL inside a prose `message` ("add funds:
    https://…/app#billing"). Checking the code instead of the response is the exact failure this
    codebase keeps finding, and I wrote one.

    So: walk the structure, drop the key wherever it appears, and remove the URL from any string. The
    remaining sentence still says what is wrong and how much was needed — the diagnosis survives, the
    invitation to pay does not.
    """
    def scrub(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items() if k != "topup_url"}
        if isinstance(v, list):
            return [scrub(x) for x in v]
        if isinstance(v, str) and ("http://" in v or "https://" in v):
            # Any URL, not just ours. A first version matched `public_url`, which differs per
            # environment — so it stripped nothing anywhere except production, and the local test
            # passed while the deployed behaviour was wrong. The property we want is "no link out",
            # which does not depend on which host we happen to be running as.
            kept = [ln for ln in v.splitlines() if "http://" not in ln and "https://" not in ln]
            return "\n".join(kept).rstrip()
        return v

    return scrub(body)


def _qs_value(v: Any) -> str:
    """One query-string value, spelled the way the WIRE spells it — not the way Python does.

    `str(True)` is `"True"`, and an upstream that documents a boolean flag rejects that:
    thecompaniesapi answers `{"rule": "boolean", "message": "The value must be a boolean"}`. The
    caller did nothing wrong — JSON booleans are what an MCP client sends, and they arrived here as
    real `bool`s — so the conversion is ours to get right. It bit hardest where it cost money:
    `simplified=true` is that endpoint's FREE preview mode, so a silently-mangled flag pushed the
    caller onto the paid path for a query they had asked to see for nothing.

    Nested objects/arrays go as compact JSON rather than Python's `repr` (`{'a': 1}` with single
    quotes is not JSON and no upstream parses it). `None` never reaches here — an unset parameter is
    omitted from the query string entirely, which is what "no value" means over HTTP.
    """
    return catalog_store.wire_value(v)


def _query_values(ep: dict | None, name: str, value: Any) -> list[str]:
    """Structured value(s) for one query key, using the catalog's declared wire encoding."""
    return catalog_store.query_values(ep, name, value)


async def _observed_stats(endpoint_ids: list[str]) -> dict[str, dict]:
    """What treg has measured across real calls to these endpoints — `{}` if it can't be read.

    Its own session rather than the caller's client: this is a read of pooled, aggregate telemetry
    that belongs to no org, and search must keep working when the query does not (the ranking then
    falls back to the deterministic score order, which is what it always was).
    """
    if not endpoint_ids:
        return {}
    try:
        from . import endpoint_stats
        from .db import session_maker

        async with session_maker() as db:
            return await endpoint_stats.observed(db, endpoint_ids)
    except Exception:  # noqa: BLE001 — telemetry must never take search down
        logging.getLogger("treg.mcp").warning("endpoint stats unavailable", exc_info=True)
        return {}


def _oauth_claims(token: str) -> dict | None:
    """If this is an OAuth access token we issued FOR THIS SERVER, its claims; otherwise None.

    Returning None is not a rejection — it means "not an OAuth token", and the caller falls through
    to the per-org and identity tokens that Codex uses. What IS a rejection, silently and on purpose,
    is a token whose `aud` names a different resource: the user granted that to someone else's MCP
    server, and honouring it here would spend their treg balance on a consent they never gave us.
    """
    from . import mcp_oauth

    return mcp_oauth.read_access_token_any(token)


def _need_token() -> dict:
    base = get_settings().public_url.rstrip("/")
    return {
        "error": "not authenticated",
        "detail": (
            f"This MCP server needs a treg token. Get one at {base} "
            "(sign in, then Settings -> copy token) and set it as the TREG_TOKEN environment "
            "variable for this server."
        ),
    }


async def _internal_auth(token: str) -> dict[str, str]:
    """Turn whatever the caller presented into headers treg's own API understands.

    Two kinds of credential arrive here and only one of them is native. A per-org or identity token
    (what Codex sends) passes straight through. An OAuth access token does NOT: it is ours, issued by
    our authorization server, and the rest of the API has never heard of it.

    So it is exchanged rather than forwarded — validated here, then presented onward as a short-lived
    identity token for the user it names, pinned to the ORG THE HUMAN CHOSE at consent. That keeps
    OAuth inside this module instead of teaching `require_member` a third token type, and it means
    the team on the grant is the team that gets billed, with no per-call guessing.

    Found by running the flow rather than by testing the pieces: `_oauth_claims` validated a token
    perfectly while every tool still forwarded the raw bearer and got "not signed in".
    """
    claims = _oauth_claims(token)
    if claims is None:
        # Not an OAuth access token — a per-org or identity token, forwarded as-is. But an identity
        # token may PIN a team in its own claim (the dashboard's org-scoped "API key", so it works as
        # a bare MCP bearer where no X-Treg-Org header can travel). If it does, surface that as
        # X-Treg-Org so `_resolve_org` takes its "pinned" path instead of asking "which team?" — the
        # exact failure a multi-team user hit pasting their key into an MCP client.
        from . import session as _session
        pinned = (_session.read_claims(token) or {}).get("org")
        return {"X-Treg-Token": token, "X-Treg-Org": pinned} if pinned else {"X-Treg-Token": token}

    from sqlmodel import select

    from . import session
    from .db import session_maker
    from .models import Org, User

    async with session_maker() as db:
        user = await db.get(User, claims["sub"])
        if user is None or user.suspended or user.token_version != claims["tv"]:
            # tv mismatch = the user revoked their tokens after this grant was made.
            return {"X-Treg-Token": token}
        org = await db.get(Org, claims["org"])
        if org is None or org.suspended:
            return {"X-Treg-Token": token}
        slug = org.slug
    return {"X-Treg-Token": session.make(user.id, ttl=120, token_version=user.token_version),
            "X-Treg-Org": slug}


@asynccontextmanager
async def _api(token: str):
    """An in-process client bound to treg's own ASGI app, carrying the caller's identity."""
    from .api import app  # deferred: api.py imports THIS module to mount it

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=_INTERNAL_BASE,
        timeout=_TIMEOUT,
        # X-Treg-Client is attribution, not auth: without it every MCP-originated call lands
        # in the audit trail as client="", indistinguishable from unreported CLI traffic.
        headers={**await _internal_auth(token), "X-Treg-Client": "mcp"},
    ) as client:
        yield client


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


async def _resolve_org(client: httpx.AsyncClient) -> tuple[int | None, str | None, dict | None]:
    """Which team is this caller acting for? Returns `(org_id, slug, problem)`.

    There are two kinds of token and they answer differently — a distinction that cost a production
    bug here. A PER-ORG token (what `treg org agent-new` mints) has its org baked in and `/auth/me`
    reports it. An IDENTITY token (what `treg login` gives, which is what most people actually hold)
    belongs to a person who may be in several teams, so `/auth/me` reports no org at all and every
    `/orgs/{id}/…` route needs to be told which one.

    So: ask `/auth/me` first, then fall back to `/orgs` and take the active team — the same order
    `cli._active_org_id` uses. When a person is in several teams and none is marked active, say so
    and NAME them rather than silently picking one: reading the wrong team's balance is a confusing
    answer, and spending from it would be worse.
    """
    # An OAuth grant already names its team — the human chose it at the consent screen. Re-deriving
    # it here would ignore that decision and, for a person in several teams, ask a question that has
    # already been answered.
    pinned = client.headers.get("X-Treg-Org")
    if pinned:
        r = await client.get("/orgs")
        for o in (_body(r) or []) if r.status_code == 200 else []:
            if o.get("slug") == pinned:
                return int(o["org_id"]), pinned, None
        return None, None, {"error": f"the team named in this grant ({pinned}) is no longer available"}

    me = await client.get("/auth/me")
    if me.status_code == 200 and _body(me).get("org_id"):
        body = _body(me)
        return int(body["org_id"]), body.get("org"), None

    r = await client.get("/orgs")
    if r.status_code == 401 or me.status_code == 401:
        return None, None, {"error": "not signed in, or this token is invalid or expired",
                            "hint": f"copy a fresh token from {get_settings().public_url.rstrip('/')}"}
    if r.status_code != 200:
        return None, None, {"error": "could not read the teams for this token"}
    orgs = _body(r) or []
    if not orgs:
        return None, None, {"error": "this account is not a member of any team"}
    active = [o for o in orgs if o.get("active")]
    chosen = active[0] if active else (orgs[0] if len(orgs) == 1 else None)
    if chosen is None:
        return None, None, {
            "error": "this account belongs to several teams and none is marked active",
            "teams": [o.get("slug") for o in orgs],
            "hint": "ask the human which team to use, then set TREG_TOKEN to that team's token "
                    "(treg org agent-new) so the choice is unambiguous",
        }
    return int(chosen["org_id"]), chosen.get("slug"), None


async def _whose_grant(client: httpx.AsyncClient, slug: str | None, *, oauth: bool) -> dict:
    """`{team, team_name, identity, hint}` — enough for a human to spot the WRONG team.

    A slug on its own cannot be sanity-checked. `superdesign-7` looks like a plausible team to an
    agent and to the person reading over its shoulder, and neither of them can tell it apart from
    the team they meant; the first signal that anything was wrong was money missing from a balance
    nobody had opened. The display name and the account the grant belongs to are what make the
    mismatch legible — most of the time it is the OTHER half that differs, an OAuth consent given by
    one login while the CLI is signed in as another, which is exactly why `treg org ls` did not list
    the team the connector was spending from.

    Best-effort by construction: this is a label on an answer that is already correct, so a failure
    to read the display name must never cost the caller their balance.
    """
    out: dict[str, Any] = {"team": slug}
    try:
        me = _body(await client.get("/auth/me"))
        if isinstance(me, dict) and me.get("email"):
            out["identity"] = me["email"]
        orgs = _body(await client.get("/orgs"))
        for o in orgs if isinstance(orgs, list) else []:
            if o.get("slug") == slug:
                out["team_name"] = o.get("name")
                break
        else:
            # The grant names a team this identity's own list does not — worth saying out loud
            # rather than leaving as a silent blank.
            if slug and isinstance(orgs, list):
                out["hint"] = (f"this connection spends from {slug!r}, which is not among this "
                               f"account's teams — check who authorised it")
    except Exception:  # noqa: BLE001 — a label, never a gate
        logging.getLogger("treg.mcp").warning("could not label the grant's team", exc_info=True)
    if oauth:
        # Only an OAuth grant HAS a team to move. A header token carries its own team already, and
        # `treg mcp grants` would list nothing for it — sending that caller to a command with no
        # answer is the "documented a feature that isn't there for you" failure in miniature.
        out.setdefault("hint", "to spend from a different team, the human who authorised this "
                               "connection runs `treg mcp grants` then `treg mcp use-team "
                               "<grant> <team>` — no need to reconnect")
    return out


# --------------------------------------------------------------------------------------------
# The catalog — read straight from memory. Still credentialed: the transport challenges first.
# --------------------------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Search ~2,600 API endpoints by WHAT YOU WANT TO DO, not by vendor. Use plain task words: "
        "'work email', 'backlinks for a domain', 'tiktok comments', 'keyword search volume'. "
        "Returns each endpoint's id, provider, price per call, and whether treg can serve it "
        "without you owning an API key. Call this FIRST when a task needs data or an API you have "
        "no key for."
    ),
    annotations=_READS,
    structured_output=True
)
async def catalog_search(query: str, limit: int = 8) -> SearchOut:
    cat = catalog_store.load()
    limit = max(1, min(limit, 25))
    # Score, then let the evidence break the ties. Token scoring produces ties by the dozen — every
    # one of the 24 "ad library" matches scores 6 — so with a default limit of 8 the rows an agent
    # actually sees were decided by file order. That handed back seven tikhub rows (one of them
    # uncallable) and hid the cheapest endpoint with a perfect measured record.
    ranked, total, tie_truncated = catalog_store.rank_band(query, cat, limit)
    stats = await _observed_stats([ep["id"] for ep, _ in ranked])
    ranked = catalog_store.rerank(ranked, stats, cat)[:limit]
    results = []
    for ep, score in ranked:
        obs = stats.get(ep["id"]) or {}
        cost = cat.cost_view(ep.get("cost"), ep.get("provider")) or {}
        results.append({
            "endpoint_id": ep["id"],
            "name": ep.get("name") or (ep.get("summary") or "")[:70],
            "provider": ep.get("provider"),
            "usd_per_call": cost.get("usd"),
            # BOTH halves of tier 4's own truth, not just the price side: `platform_eligible` says
            # the row is priceable, `platform_key_for` says this deploy actually holds an enabled
            # key. Eligible-but-keyless rows used to advertise `no_key_needed: true` here and then
            # refuse at call time — an agent-facing lie the CLI's /access line never told.
            "no_key_needed": cat.platform_eligible(ep)
                             and bool(get_settings().platform_key_for(ep.get("provider"))),
            "score": score,
            # The measured half of the answer, at the step where the agent is choosing. Without it
            # the "your agent picks on evidence" story only came true at catalog_get — one endpoint
            # at a time, after the shortlist had already been cut blind.
            "works": obs.get("ok_rate"),
            "samples": obs.get("samples") or 0,
        })
    out = {"query": query, "count": len(results), "total_matches": total, "results": results}
    if tie_truncated:
        # No silent caps: past this many equally-scoring rows the evidence sort never saw the rest,
        # so the tail is ordered by nothing in particular and must not read as a ranked answer.
        out["ranking_note"] = (f"{query!r} matches too broadly to rank on measured reliability past "
                               f"the first {catalog_store.RERANK_BAND} equally-scoring rows — "
                               f"add a word to narrow it")
    if not results:
        # Same miss log as GET /catalog/search (see models.SearchMiss) — this tool reads the catalog
        # in-process, so the HTTP route's logging never sees an MCP agent's empty search.
        if query.strip():
            audit.record_search_miss(query=query.strip(), source="mcp")
        # the zero-result answer carries the rows that JUST missed the gate and which words they
        # missed — the caller is an LLM, and told exactly what to drop it re-queries correctly
        near = catalog_store.near_misses(query, cat)
        if near:
            out["near"] = near
            first = near[0]
            out["hint"] = (
                f"nothing matches {query!r} closely enough. Nearest: {first['endpoint_id']} "
                f"matches {', '.join(first['matches'])} but not {', '.join(first['missing'])} — "
                "drop the unmatched words, or say the task differently. If the catalog genuinely "
                "lacks it, file it with catalog_request(capability=...)"
            )
        else:
            out["hint"] = (
                f"nothing matches {query!r} closely enough — try different task words. "
                "If the catalog genuinely lacks it, file it with catalog_request(capability=...) — "
                "requests steer which provider gets added next"
            )
    else:
        out["next"] = "catalog_get(endpoint_id) for parameters and the exact price, then call(...)"
    return out


@mcp.tool(
    description=(
        "The catalog doesn't have what you need? File a tool request — one sentence saying what "
        "capability or provider is missing. Requests are the demand signal that decides which "
        "provider gets added next. Use AFTER catalog_search comes up empty, not instead of it."
    ),
    # A write, but a harmless one: it files a report on treg itself — nothing upstream, nothing spent.
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False,
                                idempotent_hint=False),
    structured_output=True
)
async def catalog_request(capability: str, ctx: Context, note: str = "") -> RequestOut:
    """Relays to POST /tool-requests so the rate limiting, field caps and attribution live in one
    place; the bearer (when the session has one) turns into who-asked on the stored row."""
    token = _bearer(ctx)
    # The in-process relay would otherwise collapse every MCP caller into one client IP ("?"),
    # making the per-IP rate limit a single global bucket — forward the edge's X-Forwarded-For
    # so the API's limiter sees the real caller.
    xff = (ctx.headers or {}).get("x-forwarded-for") or (ctx.headers or {}).get("X-Forwarded-For") or ""
    async with _api(token) as client:
        r = await client.post("/tool-requests", json={
            "capability": capability, "note": note, "source": "mcp"},
            headers={"X-Forwarded-For": xff} if xff else {})
    return _body(r)


@mcp.tool(
    description=(
        "One endpoint in full: its parameters, the exact price per call, whether treg serves it on "
        "its own key, and the other providers that answer the same capability — with the success "
        "rate and speed treg has measured across real calls. Read this BEFORE call() so you can "
        "tell the human what it will cost."
    ),
    annotations=_READS,
    structured_output=True
)
async def catalog_get(endpoint_id: str, ctx: Context) -> CatalogGetOut:
    """Goes through the HTTP route rather than the store: that route attaches the observed
    reliability figures and the capability siblings, and those come from the database."""
    token = _bearer(ctx)
    async with _api(token) as client:
        r = await client.get(f"/catalog/endpoints/{endpoint_id}")
    if r.status_code == 404:
        cat = catalog_store.load()
        return {"error": f"unknown endpoint {endpoint_id!r}",
                # Naming the near miss, not just the tool to go back to. An id one segment off is
                # the common failure — and the loop breaks at its FIRST step, so an agent that only
                # hears "search again" re-runs the same search and re-derives the same wrong id.
                "hints": [catalog_store.unknown_id_hint(endpoint_id, cat),
                          "or use catalog_search to find the right id"],
                "did_you_mean": catalog_store.near_ids(endpoint_id, cat)}
    return _body(r)


# --------------------------------------------------------------------------------------------
# Everything below spends money or reads a team's data — all of it goes back through the API
# --------------------------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Call something through treg. `params` is the request body for a POST (an object, or an "
        "ARRAY of task objects for providers like DataForSEO that expect one) and the query string "
        "for a GET. Either a CATALOG endpoint by its id (from catalog_search), or "
        "one of THIS TEAM'S own tools as '<tool-name>/<path>' (from my_tools) — e.g. "
        "'render/v1/services'. treg injects the credential server-side and relays the provider's "
        "response unchanged, so you never hold an API key. Catalog calls on treg's key are metered "
        "from the team's prepaid balance; a team's own tool is never metered. Tell the human the "
        "price (from catalog_get) before calling anything that costs more than a cent.\n\n"
        "`idempotency_key`: pass the SAME key when you are repeating a call whose answer you did "
        "not receive — a timeout, a dropped connection, an error on your side after the request "
        "went out. treg returns the stored answer, does not call the provider again, and charges "
        "nothing the second time; the result carries `replayed: true`. Use a NEW key (or none) for "
        "genuinely new work, even when the parameters are identical: repeating a search to see "
        "what changed is a new call, not a retry, and reusing the key would hand you the old answer. "
        "Reusing one key for a DIFFERENT request is refused rather than answered.\n\n"
        "For requests `params` can't express, the explicit slots mirror the CLI's flags: `query` "
        "is ALWAYS the query string (a list value expands to repeated keys), `body` is ALWAYS the "
        "request body (object/array → JSON; a STRING is sent raw, with `content_type` naming what "
        "it is — sniffed as application/json when it parses as JSON), and `headers` adds upstream "
        "request headers an endpoint needs per-call (e.g. Google Ads' login-customer-id); "
        "injected credentials always win over them. Use `query` + `body` together for endpoints "
        "that split a POST across both (Bright Data's ?dataset_id=… + array body). Giving `body` "
        "implies POST. Multipart file uploads aren't supported here — run (or tell the human to "
        "run) the CLI: `treg call <endpoint> --upload name=@/path/to/file`.\n\n"
        "Query values are sent the way HTTP spells them: booleans as `true`/`false`, nested "
        "objects as compact JSON. A null query value is OMITTED from the query string — if an "
        "upstream distinguishes an absent parameter from an empty one, send the empty string "
        "explicitly rather than null."
    ),
    annotations=_CALLS,
    structured_output=True
)
async def call(endpoint_id: str, params: dict | list | None = None,
               method: str | None = None, idempotency_key: str | None = None,
               query: dict | None = None, body: dict | list | str | None = None,
               headers: dict | None = None, content_type: str | None = None,
               ctx: Context = None) -> CallOut:  # type: ignore[assignment]
    token = _bearer(ctx) if ctx else ""
    if not token:
        return _need_token()

    # BOTH halves answer here, exactly as `treg call` does: `/call/{rest}` resolves a team's own tool
    # first and falls back to a catalog id, so an org tool always wins over the catalog. Pre-checking
    # the catalog and refusing anything absent would have made `my_tools` a list of things the agent
    # could see and never call — which is how this gap was found.
    cat = catalog_store.load()
    ep = cat.by_id.get(endpoint_id)
    if ep is None and "/" not in endpoint_id:
        near = catalog_store.near_ids(endpoint_id, cat)
        return {"error": f"unknown endpoint {endpoint_id!r}",
                "hint": ("did you mean " + ", ".join(near) + "?" if near else
                         "use catalog_search for a catalog id, or my_tools then "
                         "'<tool-name>/<path>' for one of this team's own tools"),
                "did_you_mean": near}

    # `body` implies POST — curl's convention, and the CLI's: catalog endpoints reject a method
    # mismatch, so making `body` just work beats asking the caller to repeat what the catalog knows.
    method = (method or (ep.get("method") if ep else None)
              or ("POST" if body is not None else "GET")).upper()
    reads_query = method in ("GET", "HEAD", "DELETE")
    # A LIST is a legitimate body, not a mistake. DataForSEO — the largest provider in the catalog at
    # 217 endpoints — takes an ARRAY of task objects on every one of its `live` POST routes, so a
    # dict-only signature made all of them uncallable. Found by trying one rather than by reading the
    # type. Query strings still need key/value pairs, so a list is only meaningful as a body.
    args = params if params is not None else {}

    # ---- assemble the real request: query string, body, extra headers ------------------------
    # `params` keeps its method-based role (query on GET, body on POST); the explicit `query` and
    # `body` slots express the shapes that role can't — a POST that needs BOTH a body and a query
    # string (Bright Data's ?dataset_id=… + array body), a raw non-JSON body, an extra upstream
    # header. When an explicit slot is given, `params` must not also claim the same position —
    # refused loudly rather than silently merged, because a silent merge sends a wrong request.
    if body is not None and params is not None and not reads_query:
        return {"error": "give the request body as `body` OR `params`, not both",
                "endpoint_id": endpoint_id}
    if query is not None and params is not None and reads_query:
        return {"error": "give the query string as `query` OR `params`, not both",
                "endpoint_id": endpoint_id}
    if reads_query and isinstance(args, list):
        return {"error": "this endpoint takes query parameters, so `params` must be an "
                         "object, not a list", "endpoint_id": endpoint_id}

    # Query pairs as a LIST of tuples so repeated keys (?tag=a&tag=b) survive — a dict keeps only
    # the last. A list VALUE in `query` expands to repeated keys. And an inline `?a=b` inside a
    # passthrough URL would be DROPPED by httpx whenever params= is passed — the upstream then
    # answers with default/wrong data and NO error (the CLI guards the same gotcha) — so it is
    # pulled out and merged.
    query_pairs: list[tuple[str, str]] = []
    if "?" in endpoint_id:
        endpoint_id, _, inline = endpoint_id.partition("?")
        query_pairs += parse_qsl(inline, keep_blank_values=True)
    for src in (args if (reads_query and isinstance(args, dict)) else {}, query or {}):
        for k, v in src.items():
            if v is not None:
                query_pairs += [(k, encoded) for encoded in _query_values(ep, k, v)]

    the_body = body if body is not None else (args if not reads_query else None)
    # Caller headers relay to the upstream exactly as the CLI's --header does (Google Ads'
    # login-customer-id is the canonical need) — with treg's own auth/routing headers filtered so
    # the tool's semantics stay unambiguous: the bearer on the MCP request IS the identity, and
    # idempotency travels via its own argument. Injected credentials always win server-side.
    # `x-treg-meta` joins the filtered set: caller tags decide who gets billed and budgeted, so they
    # must come from the TRANSPORT (the builder's backend, below) and never from a tool argument the
    # model fills in — a model that omits it mid-chain drops that spend out of its user's invoice.
    extra_headers = {k: str(v) for k, v in (headers or {}).items()
                     if k.lower() not in ("x-treg-token", "x-treg-org", "authorization",
                                          "idempotency-key", "x-treg-meta")}
    if isinstance(the_body, str):
        # A raw string body travels as-is. Content-Type: explicit wins, else sniff JSON — the
        # CLI's rule, because upstreams that require `application/json` reject a JSON body
        # labelled text/plain.
        ctype = content_type
        if ctype is None:
            try:
                json.loads(the_body)
                ctype = "application/json"
            except ValueError:
                ctype = "text/plain"
        extra_headers["content-type"] = ctype

    async with _api(token) as client:
        # Resolve the team the same way `balance`/`my_tools` do BEFORE spending anything: a
        # multi-team identity token otherwise reaches /call and bounces off its raw
        # "choose an org (send X-Treg-Org)" 400 — a header hint an MCP caller cannot act on.
        # `_resolve_org` honours the pinned/active team and, when there genuinely is no answer,
        # NAMES the teams so the agent can ask the human — found live on the first
        # multi-team dashboard token pasted into an MCP client.
        _, slug, problem = await _resolve_org(client)
        if problem:
            return problem
        if slug:
            extra_headers["X-Treg-Org"] = slug
        # Relay the caller tags off the MCP transport, the same way catalog_request forwards
        # X-Forwarded-For. A builder proxying MCP sets this header once per session on their own HTTP
        # client; the model never sees it, so it cannot be forgotten or invented.
        inbound_meta = (ctx.headers or {}).get("x-treg-meta") or (ctx.headers or {}).get("X-Treg-Meta")
        if inbound_meta:
            extra_headers["X-Treg-Meta"] = str(inbound_meta)
        if idempotency_key:
            # Straight through to the header the API already honours. Deliberately the CALLER's key
            # and never derived from the request: two identical searches an hour apart are new work,
            # not a retry, and a server-invented key would hand back the stale answer — a 24-hour
            # cache wearing an idempotency badge.
            client.headers["Idempotency-Key"] = idempotency_key[:200]
        # The SAME route the CLI and the proxy use, so the tool ACL, deny rules, both daily caps,
        # the balance reserve and the settle all happen exactly once, in one place. params= is
        # only passed when there ARE pairs — see the inline-query gotcha above.
        kw: dict[str, Any] = {}
        if extra_headers:
            kw["headers"] = extra_headers
        if query_pairs:
            kw["params"] = query_pairs
        if the_body is not None:
            if isinstance(the_body, str):
                kw["content"] = the_body.encode()
            else:
                kw["json"] = the_body
        r = await client.request(method, f"/call/{endpoint_id}", **kw)

    out: dict[str, Any] = {"status": r.status_code, "endpoint_id": endpoint_id, "body": _body(r)}
    if r.headers.get("X-Treg-Idempotent-Replay") == "true":
        out["replayed"] = True
        out["hint"] = ("this is the stored answer from the earlier call with the same "
                       "idempotency_key — nothing was charged for it")
    # Set by /call/ on a METERED call only — a team's own key is never charged, and its absence
    # therefore means "not applicable" rather than "free". This header did not exist when the tool
    # first read it: I wrote against a convention I had invented, so `cost_usd` was always null and
    # an agent could not report what it spent. Same mistake as the `?next=` redirect.
    spent = r.headers.get("X-Treg-Cost-Micro")
    if spent is not None:
        try:
            out["cost_usd"] = round(int(spent) / 1_000_000, 6)
        except ValueError:
            pass
    if r.status_code == 402:
        # States the fact and stops. No link, and `topup_url` is stripped from the relayed body, so
        # nothing on this path points a user at a payment page.
        #
        # ChatGPT's submission form asks whether a plugin "links or directs users out of ChatGPT to
        # make purchases", and says only PHYSICAL goods can be supported. treg sells prepaid API
        # credit, which is a digital good — so a top-up link made the honest answer a yes, in the one
        # category they cannot support. The link was a convenience, not the product: someone out of
        # balance can find their own dashboard.
        #
        # Scoped to the MCP path deliberately. `/call/`'s 402 still carries `topup_url` for the CLI
        # and the dashboard, where no such policy applies and the shortcut is genuinely useful.
        out["body"] = _without_purchase_pointers(out.get("body"))
        out["hint"] = "the team's prepaid balance is not enough for this call"
    elif r.status_code >= 400:
        # Whose fault it was matters to an agent deciding whether to retry elsewhere.
        out["whose_error"] = "treg" if r.headers.get("X-Treg-Error") else "provider"
    return out


@mcp.tool(
    description=(
        "The team's prepaid balance in USD, and any spend currently in flight. Check it when a call "
        "is refused for funds, or before a job that will make many calls."
    ),
    annotations=_READS,
    structured_output=True
)
async def balance(ctx: Context) -> BalanceOut:
    token = _bearer(ctx)
    if not token:
        return _need_token()
    async with _api(token) as client:
        org_id, slug, problem = await _resolve_org(client)
        if problem:
            return problem
        r = await client.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Org": slug or ""})
        whose = await _whose_grant(client, slug, oauth=_oauth_claims(token) is not None)
    body = _body(r)
    if r.status_code != 200:
        return {"error": "could not read the balance", "detail": body}
    return {
        **whose,
        "balance_usd": round((body.get("balance_micro") or 0) / 1_000_000, 6),
        "balance_micro": body.get("balance_micro"),
        "holds_micro": body.get("holds_micro"),
    }


@mcp.tool(
    description=(
        "What THIS team has registered and you can call without holding the credential: their own "
        "API keys, OAuth connections, vendor CLIs and skills. A team's own tool always wins over "
        "treg's catalog key, and those calls are never metered."
    ),
    annotations=_READS,
    structured_output=True
)
async def my_tools(ctx: Context) -> MyToolsOut:
    token = _bearer(ctx)
    if not token:
        return _need_token()
    async with _api(token) as client:
        _, slug, problem = await _resolve_org(client)
        if problem:
            return problem
        r = await client.get("/tools", headers={"X-Treg-Org": slug or ""})
        whose = await _whose_grant(client, slug, oauth=_oauth_claims(token) is not None)
    body = _body(r)
    if r.status_code != 200:
        return {"error": "could not list the team's tools", "detail": body}
    tools = body if isinstance(body, list) else body.get("tools", [])
    return {
        **whose,
        "count": len(tools),
        "tools": [{"name": t.get("name"), "base_url": t.get("base_url"),
                   "description": t.get("description")} for t in tools],
    }


# --------------------------------------------------------------------------------------------
# Mounting
# --------------------------------------------------------------------------------------------

def _allowed_hosts() -> list[str]:
    """Which `Host` headers the transport will answer to.

    The SDK ships DNS-rebinding protection ON with an EMPTY allow-list, which rejects **everything**
    with a 421 — the default is unusable rather than merely strict, and it fails at request time, so
    a deploy looks healthy right up until the first tool call. Found by the tests here; it would
    otherwise have been found in production.

    The protection is real (it stops a web page from driving a localhost MCP server), so it stays on
    and the list is built instead: this deployment's own `public_url`, the loopback names a developer
    actually uses, and the in-process host the API calls itself on. `TREG_MCP_ALLOWED_HOSTS` adds
    more, comma-separated, for a deployment behind another name — the ngrok dev box, say, or a second
    domain.
    """
    hosts: list[str] = []
    public = urlsplit(get_settings().public_url).netloc
    if public:
        hosts += [public, public.split(":")[0]]
    # Every name the reference deployment has ever answered to, SYMMETRICALLY — a .mcp.json
    # pointed at either domain keeps working whichever one public_url currently names, which is
    # what makes an env-var rollback lossless (a treg.to config must survive a revert too).
    hosts += list(PUBLIC_HOST_ALIASES)
    # The SDK compares Host values EXACTLY, and `example.com:443` is a valid spelling of the
    # default-port form some clients send — so every bare https hostname also allows its :443 twin.
    hosts += [f"{h}:443" for h in hosts if ":" not in h and h not in ("localhost", "127.0.0.1")]
    hosts += ["localhost", "127.0.0.1", "treg.internal"]
    hosts += [h.strip() for h in os.environ.get("TREG_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    # a bare host and host:port are different header values, so allow the common local ports too
    hosts += [f"localhost:{p}" for p in ("8000", "18790")] + [f"127.0.0.1:{p}" for p in ("8000", "18790")]
    return sorted(dict.fromkeys(hosts))


def _allowed_origins() -> list[str]:
    """Which `Origin` headers the transport will answer to.

    `"*"` is NOT a wildcard here — the SDK compares origins literally, and only a `:*` port suffix is
    special. Setting `["*"]` therefore allowed exactly one origin, the literal string "*", and
    refused every browser with "Invalid Origin header". Nothing caught it: the test suite and every
    CLI client send no Origin at all, so the check never ran until a real page called /mcp/.

    So the list is built like `_allowed_hosts`: this deployment plus the loopback origins a developer
    uses, extended by `TREG_MCP_ALLOWED_ORIGINS`. A browser-based MCP client — including a web page
    like /connect-demo — needs its origin here to work at all.
    """
    origins: list[str] = []
    public = get_settings().public_url.rstrip("/")
    if public:
        origins.append(public)
    origins += [f"https://{h}" for h in PUBLIC_HOST_ALIASES]
    # Exact comparison again: allow the explicit-default-port spelling of every https origin.
    origins += [f"{o}:443" for o in origins
                if o.startswith("https://") and ":" not in o.removeprefix("https://")]
    origins += [f"http://localhost:{p}" for p in ("8000", "18790")]
    origins += [f"http://127.0.0.1:{p}" for p in ("8000", "18790")]
    origins += ["http://localhost", "http://127.0.0.1"]
    origins += [o.strip() for o in os.environ.get("TREG_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return sorted(dict.fromkeys(origins))


class RequireAuthForProtectedTools:
    """Answer 401 with `WWW-Authenticate` for any uncredentialed MCP request — eager, not lazy.

    A tool function can return an error dict, but it cannot set an HTTP status or a header — and the
    status and header are the whole point. The MCP spec has a protected resource reply **401** with
    `WWW-Authenticate: Bearer resource_metadata="…"`, because that header is how a client DISCOVERS it
    must authenticate and where to begin. A friendly English sentence inside a 200 tells a human what
    went wrong and tells a program nothing.

    **Every request, not just tool calls.** Every treg tool needs auth, so there is nothing to browse
    anonymously; the spec's canonical flow challenges the client's FIRST request (the `initialize`
    handshake) so OAuth runs before the session proceeds — which is what Stripe/Subframe/AuthKit MCP
    servers do, and what makes a client show "needs authentication" and prompt, rather than "Connected"
    with silent 200s on a server nothing works against. Only notifications and `ping` pass without a
    credential (see `_auth_verdict`); `.well-known/*` discovery is separate GET routes, untouched.

    Sits in front of the transport rather than inside it: this is an HTTP concern, and the SDK owns
    everything below.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)

        verdict = self._auth_verdict(scope, body)
        if verdict is not None:
            return await self._challenge(send, invalid=(verdict == "invalid"))

        # The body was consumed to inspect it, so hand the transport a receive() that replays it.
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, replay, send)

    @staticmethod
    def _auth_verdict(scope, body: bytes) -> str | None:
        """None = pass through. "missing" = no credential. "invalid" = a DEAD access token.

        **Eager, not lazy.** Every treg tool needs auth, so there is nothing to browse anonymously —
        and the MCP spec's canonical flow has the client's FIRST request (the `initialize` handshake)
        answered with 401 so it discovers the authorization server and runs OAuth before proceeding.
        A production MCP server (Stripe, Subframe, AuthKit) does exactly this; FastMCP even tracks a
        401-free `initialize` as a bug (#3020). Leaving `initialize`/`tools/list` open was why Claude
        Code showed "✔ Connected" and never prompted — a connected-but-unusable server. So we
        challenge every non-notification JSON-RPC request without a valid credential, whatever the
        method. (`.well-known/*` metadata is separate GET routes, untouched — that is discovery.)

        The "invalid" case is what makes refresh work end to end. An OAuth client whose access token
        expired presents it anyway; per RFC 6750 the resource answers 401 with
        `error="invalid_token"`, and THAT is the signal on which the client silently runs its refresh
        grant — rather than giving up with "requires re-authorization".

        Only tokens that CLAIM to be our OAuth access tokens are judged here
        (`looks_like_access_token`); a per-org or identity token is the API's to validate downstream,
        and those callers (Codex with an env var) are not OAuth clients and cannot refresh anyway.
        """
        try:
            rpc = json.loads(body or b"{}")
        except ValueError:
            return None         # malformed input is the transport's problem, not ours to relabel
        method = rpc.get("method")
        # Notifications carry no id and expect no response — challenging one would be a 401 nobody
        # asked for. `ping` is the liveness check and must answer without a token. Everything else
        # (initialize, tools/list, tools/call, prompts/*, resources/*) needs a credential.
        if not isinstance(method, str) or method.startswith("notifications/") or method == "ping":
            return None
        token = ""
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization" and v.strip():
                token = v.decode("latin-1").strip()
                token = token[7:].strip() if token.lower().startswith("bearer ") else token
                break
        if not token:
            return "missing"
        from . import mcp_oauth
        if mcp_oauth.looks_like_access_token(token) and \
                mcp_oauth.read_access_token_any(token) is None:
            return "invalid"
        return None             # a live access token, or a per-org token the tool validates itself

    @staticmethod
    async def _challenge(send, *, invalid: bool = False) -> None:
        base = get_settings().public_url.rstrip("/")
        meta = f"{base}/.well-known/oauth-protected-resource"
        # The spec SHOULDs a `scope` in the challenge so a client requests the right scopes up front,
        # least-privilege, without a second round-trip. These match scopes_supported in the metadata.
        scope = "treg:catalog treg:call treg:read"
        if invalid:
            # RFC 6750 §3.1: the expired/invalid-token challenge. `error="invalid_token"` is the
            # machine-readable cue on which an OAuth client runs its refresh grant instead of
            # bothering the human.
            www = (f'Bearer error="invalid_token", error_description="the access token is expired or invalid", '
                   f'scope="{scope}", resource_metadata="{meta}"')
            payload = {"error": "invalid_token",
                       "error_description": ("the access token is expired or invalid — refresh the "
                                             "grant, or re-authorize at " + base + "/oauth/authorize"),
                       "resource_metadata": meta}
        else:
            www = f'Bearer scope="{scope}", resource_metadata="{meta}"'
            payload = {"error": "unauthorized",
                       "error_description": ("this MCP server needs a treg grant — authorize at "
                                             f"{base}/oauth/authorize, or send a per-org token as a bearer"),
                       "resource_metadata": meta}
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"),
            # The header the spec is actually about: it names where to discover how to authenticate.
            (b"www-authenticate", www.encode()),
        ]})
        await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


class NoTransformResponses:
    """Stamp `Cache-Control: no-store, no-transform` on every MCP response.

    Production sits behind Render's managed edge — not an account of ours, and there is no
    dashboard to configure — and that edge Brotli-compresses responses on the way out. A compliant
    client decodes `br` fine, but at least one real MCP client stack (httpx + brotlicffi, issue #93)
    dies mid-decode on large compressed bodies, and the failure mode is the worst one available: the
    RPC hangs until the client's own timeout, minutes after the upstream answered in seconds.

    `no-transform` is the standard way for an origin to tell an intermediary "do not re-encode this"
    (RFC 9111 §5.2.2.6) — and Render's edge IGNORES it (issue #100: `content-encoding: br` arrived
    right next to this header in production). The header stays because it is correct and costs
    nothing, but the fix that actually works is compressing at the origin — see `build_mcp_app`:
    an edge does not re-encode a response that already carries `Content-Encoding`. `no-store` rides
    along because these responses are per-caller and priced — nothing on this path should ever be
    served from a cache.

    An ASGI wrapper rather than a header in api.py's middleware so that WHEREVER this app is mounted
    — production, or a test building its own transport — the header ships. Same lesson as the auth
    wrapper below: wrapping only one composition path is how the other one silently diverges.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_stamped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store, no-transform"))
                message = dict(message, headers=headers)
            await send(message)

        return await self.app(scope, receive, send_stamped)


def build_mcp_app():
    """A fresh ASGI app for the MCP transport.

    A factory rather than a bare module-level value because each call builds its own session
    manager, and `StreamableHTTPSessionManager.run()` may be called **once per instance** — a second
    start raises. That is right for a server (one process, one lifespan) and impossible for a test
    suite, which needs a clean instance per test. The tools themselves are shared: they hang off the
    module-level `mcp` server, so a fresh transport still exercises the real implementations.

    `stateless_http` because production runs more than one instance and a session-bound transport
    would need sticky routing; `json_response` because these are request/response tools with nothing
    to stream, and skipping SSE framing is most of the speed.
    """
    transport = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_hosts(),
            allowed_origins=_allowed_origins(),
        ),
    )
    # Wrapped HERE, not around the module-level value, so a caller that builds its own app gets the
    # same thing production runs. The first version wrapped only the module-level app, and the tests
    # — which build a fresh transport per test — silently exercised an unprotected server.
    #
    # GZip at the ORIGIN is the fix for edge re-compression (issue #100, the second half of #93).
    # Render's edge ignored `Cache-Control: no-transform` and kept Brotli-compressing large
    # responses, which a real client stack (httpx + brotlicffi) fails to decode and then hangs on.
    # An edge only compresses what arrives UNCOMPRESSED — a response already carrying
    # `Content-Encoding: gzip` passes through, and gzip is decoded by zlib on every mainstream
    # client, which sidesteps the brotli decoder entirely. A client that does not accept gzip gets
    # identity from us (GZipMiddleware respects Accept-Encoding); only a client accepting br-and-
    # not-gzip — no mainstream stack — would still meet the edge's Brotli.
    #
    # NoTransformResponses is outermost so the auth wrapper's own 401 challenges carry the header
    # too; gzip sits between so challenges and answers alike are origin-encoded.
    from starlette.middleware.gzip import GZipMiddleware

    return NoTransformResponses(GZipMiddleware(RequireAuthForProtectedTools(transport),
                                               minimum_size=1024))


mcp_app = build_mcp_app()


@asynccontextmanager
async def mcp_lifespan(target=None):
    """MUST be entered by the host app's lifespan. `app.mount()` does not run a mounted app's
    lifespan, and the streamable-HTTP session manager builds its task group there — without this
    every MCP request fails with "Task group is not initialized"."""
    target = target or mcp_app
    inner = target
    while not hasattr(inner, "router"):      # unwrap NoTransformResponses / RequireAuthForProtectedTools
        inner = inner.app
    async with inner.router.lifespan_context(inner):
        yield
