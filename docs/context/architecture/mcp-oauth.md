---
title: MCP — the front door for assistants, and treg as an OAuth authorization server
status: shipped
sources:
  - src/treg/mcp.py
  - src/treg/mcp_oauth.py
  - src/treg/web/connect-demo.html
related:
  - architecture/auth-secrets.md
  - architecture/proxy-model.md
  - architecture/money.md
  - interface/api.md
---

# MCP

Everything else in treg is reached by a CLI or an HTTP call. This is the door an **assistant** comes
through: ChatGPT, Claude Code, Cursor, or anything else that speaks the Model Context Protocol. It is
mounted at `/mcp/` on the same app, so there is one deployment, one database and one set of rules.

## Six tools, and why only six

| Tool | Job |
|---|---|
| `catalog_search` | find endpoints by what you want to DO, with prices |
| `catalog_get` | one endpoint in full: params, cost, reliability, sibling providers |
| `call` | a catalog endpoint by id, or `<tool-name>/<path>` for the team's own tool |
| `balance` | the team's prepaid balance |
| `my_tools` | what the team registered that can be called without holding the key |
| `catalog_request` | file what the catalog is MISSING — the demand signal for what gets added next |

Deliberately not one tool per provider. A catalog of 2,600 endpoints exposed as 2,600 MCP tools would
bury the client's tool list and force a re-connect every time the catalog grew. `catalog_search`
plus `call` covers all of it and stays the same size.

`call` is annotated **destructive + open-world + non-idempotent**, which reads as pessimistic until
you notice treg does not model the upstream: it relays to somebody else's API and cannot know whether
that endpoint charges, writes or deletes. Claiming otherwise would be a guess presented as a fact.
`catalog_request` is the one other non-read: a write, but a harmless one (a row on treg itself,
nothing upstream, nothing spent), so it stays closed-world and non-destructive. It relays to
`POST /tool-requests` so rate limiting and field caps live in one place, forwarding the edge's
`X-Forwarded-For` — the in-process relay would otherwise collapse every MCP caller into one
rate-limit bucket. `catalog_search`'s zero-result hint names it, so an agent that just searched
and found nothing can file the gap in the same session.

## In-process, not over the network

Tools reach the rest of treg through `httpx.ASGITransport` against our own app — a real request
through the real routes, without a socket. That matters because the enforcement rules (deny rules,
capability pins, per-member tool access, the credential ladder, metering) live in those routes. A
second path that "just read the database" would be a second implementation of every one of them,
drifting quietly. The in-process client stamps `X-Treg-Client: mcp` (attribution, never a gate), so
MCP traffic is distinguishable from unreported CLI traffic in the audit trail and analytics.

The catalog is the one exception: read straight from `catalog_store`, which is already parsed in
memory, so a search answers in about a millisecond. That is a **speed** choice, not a permission one.

## `call` speaks every request shape the CLI does

`params` keeps its original method-based role (query string on GET, JSON body on POST). The
explicit slots exist for the shapes that role can't express, mirroring `treg call`'s flags —
each of which was added because a real endpoint needed it:

- `query` — ALWAYS the query string; a list value expands to repeated keys (`?tag=a&tag=b`,
  which a dict would collapse). Composable with `body` on a POST — the Bright Data dataset shape
  (`?dataset_id=…` + array body) was uncallable over MCP without it.
- `body` — ALWAYS the body. Object/array → JSON; a STRING is sent raw with `content_type` naming
  it (sniffed as `application/json` when it parses as JSON — the CLI's rule). A body implies POST.
- `headers` — extra upstream headers (`login-customer-id` is the canonical need). treg's own
  auth/routing headers are filtered from the relay; injected credentials always win server-side.
- An inline `?a=b` inside a passthrough URL is pulled out and merged — httpx silently DROPS a
  URL's query string whenever `params=` is passed, the same gotcha `cmd_call` guards.
- `params` claiming the same position as an explicit slot is refused loudly, never merged.
- Multipart file upload stays CLI-only (`treg call --upload`) — MCP callers hold no files.
- `call` resolves the TEAM the way `balance` does (`_resolve_org`, before anything is spent): a
  multi-team identity token used to bounce off /call's raw `choose an org (send X-Treg-Org)`
  400 — a header hint an MCP caller can't act on; now it gets the pinned/active team, or the
  friendly error that NAMES the teams.

## `call` takes an `idempotency_key`

Optional, and it is the caller's. Pass the same key when repeating a call whose answer never arrived:
treg replays the stored response, does not reach the provider, and charges nothing, with
`replayed: true` on the result.

It exists because the feature was built for agents and MCP is the agent path. Without it the whole
thing was unreachable from the surface it was for.

Deliberately NOT derived server-side from the endpoint and parameters, which was proposed and
rejected: two identical searches an hour apart are new work, treg cannot tell that from a retry, and
a server-invented key would quietly serve stale data. The tool description therefore spends its words
on WHEN to use it, because that is where a mistake costs something. Reuse for a different request is
refused rather than answered, so the failure is loud.

Full reasoning, storage rules and the concurrency guard: `architecture/money.md`.

## Output schemas: every field optional AND nullable

Each tool declares what it returns (ChatGPT's connector review asks; a model that must guess at
field names guesses). Two rules, both learned the hard way:

**Every field is optional** (`total=False`). The SDK validates a strict schema on the way out, so a
required field would turn the first `{"error": "not authenticated"}` into an opaque tool failure
instead of a recoverable refusal.

**Every field is nullable** (`| None`) — not just the ones that carry data nulls (a tool with no
description, an endpoint with no price). The SDK serializes each response through the
TypedDict-derived pydantic model, and that dump fills every **absent** key in as `null` in
`structuredContent`. So a response that never mentions `next` still ships `"next": null`, and a
strict client validating against an advertised `type: string` refuses the whole answer with -32602.
Two independent field reports arrived the same day (issue #93 and one on X) before this was caught:
FastMCP's own client is lenient, so nothing local ever tripped it. The suite now validates real
`structuredContent` against the advertised schema with `jsonschema` playing the strict client.

A field whose real payloads vary in shape is `Any`, not a union: `call.body` relays whatever the
provider sent, and `catalog_get.example_response` is a dict for most endpoints but an ARRAY for
providers whose response is a list of records (brightdata datasets) — typing it `dict` made the
server's own outbound validation refuse the whole catalog entry.

## Responses are gzip-compressed at the origin — the edge must find nothing to do

Production sits behind Render's managed edge — no account or dashboard of ours — which
Brotli-compresses large responses on the way out. At least one real client stack (httpx +
brotlicffi, issue #93) dies mid-decode on that output and then hangs to its own timeout, minutes
after the upstream answered in seconds.

The first fix was `Cache-Control: no-store, no-transform` (the `NoTransformResponses` wrapper,
outermost so 401 challenges carry it too) — the origin's standard "do not re-encode" (RFC 9111).
**Render's edge ignores it** (issue #100: `content-encoding: br` arrived in production right next to
the header). The header stays because it is correct and free, but the working fix is different: the
MCP app gzips its own responses (`GZipMiddleware` inside `build_mcp_app`, ≥1KB). An edge only
compresses what arrives uncompressed — a response already carrying `Content-Encoding: gzip` passes
through — and gzip decodes via zlib on every mainstream client, sidestepping the brotli decoder
entirely. Only a client accepting br-but-not-gzip (no mainstream stack does this) would still meet
the edge's Brotli.

Post-deploy check, any time this path changes: a large authenticated `catalog_get` over `/mcp/` with
`Accept-Encoding: br, gzip` must come back `content-encoding: gzip`, not `br`.

## Authentication: eager, every request

`RequireAuthForProtectedTools` answers **any** uncredentialed MCP request with **401** and a
`WWW-Authenticate: Bearer scope="…" resource_metadata="…"` header. The header is the point — a
friendly error inside a 200 tells a human what happened and tells a program nothing. The challenge
lives in front of the transport because a tool function can set neither a status code nor a header.

**Eager, not lazy — a deliberate reversal.** The first version left `initialize` and `tools/list`
open so a client could browse before authenticating. But every treg tool needs auth, so there is
nothing to browse anonymously, and the open handshake had a real cost: a client (Claude Code, Cursor)
`initialize`d, got a 200, showed **"✓ Connected"**, and never prompted — connected-but-unusable, the
"sign in" surfacing only later as prose inside a tool result. The MCP spec's canonical flow instead
challenges the client's FIRST request so OAuth runs before the session proceeds (Stripe/Subframe/
AuthKit MCP servers all do this; FastMCP tracks a 401-free `initialize` as a bug). So the challenge
now fires for **every id-bearing JSON-RPC request** — `initialize`, `tools/list`, `tools/call`. Only
**notifications** (no id, no response expected) and **`ping`** (liveness) pass without a credential;
`.well-known/*` discovery is separate GET routes, untouched — that IS the discovery the client needs.
The challenge also carries `scope` (spec SHOULD) so a client requests least-privilege scopes up front.

The middleware judges two cases, not one. **No credential** → the plain challenge above. **A dead
access token** — a bearer that *claims* to be our OAuth access token (`looks_like_access_token`
reads the unverified payload's `typ`) but fails `read_access_token` (expired, bad signature, wrong
audience) → 401 with **`error="invalid_token"`** (RFC 6750 §3.1). That error code is what an OAuth
client runs its refresh grant on; without it, an expired token sailed through to the tool's friendly
prose in a 200 and Claude Code gave up with "requires re-authorization" instead of silently
refreshing. Access-token validation is stateless (HMAC + expiry), so the transport can afford it.
What stays out of the middleware is anything needing the **database**: a per-org or identity token
(the Codex env-var path) passes through, valid or not, for the tool to validate downstream — its
holder has no refresh grant to run, and judging it here would put a second authentication
implementation in front of the first. 

The transport's own DNS-rebinding host check (421) and Origin check (403) sit *behind* this
middleware, so a credentialed request with a bad host or origin is still refused by them — auth does
not mask the transport guard.

# treg as an authorization server

Elsewhere treg speaks OAuth as a **client** (`oauth.py` signs in with GitHub, connects a provider
account). Here it is the thing that **issues** tokens. Different direction, different module.

Built refusal-first: the metadata and the `aud` check landed before anything could issue a token, so
there was never a window where the server accepted credentials it had not learned to check.

## The `aud` claim carries the weight

The spec has a client send `resource=<the mcp url>` on the authorize and token requests, and the
server copy it into the token's audience. Without checking it, a token a user granted to *another*
MCP server would work here — they consented to that server, not to treg, and we would spend their
balance on it.

`read_access_token` therefore takes `expected_audience` as a **required argument with no default**.
A test asserts that calling without it raises rather than defaulting to permissive.

`/oauth/authorize` also refuses a `resource` we do not serve, up front. Accepting one mints a token
that is valid, well-formed and silently useless — the failure then surfaces at the first tool call as
"not signed in", pointing the reader at authentication when the problem was the audience.

**The audience set is canonical + legacy** (`mcp_resource_audiences()`: `public_url` plus
`config.PUBLIC_HOST_ALIASES` — the treg.superdesign.dev → treg.to move, SYMMETRIC so grants
minted on either name survive a `TREG_PUBLIC_URL` flip in either direction). A pre-move grant carries
the old resource URL as its audience for its whole lifetime, because refresh reissues the audience
that was consented to (`row.resource`); validating against the canonical URL alone would 401 every
pre-move grant with refresh unable to recover. The transport validates via `read_access_token_any`,
and `/oauth/token` treats the two names as the same resource (`api._same_mcp_resource`).
Slash-variant spellings are healed by `normalize_resource()` at every store/mint/compare site:
authorize accepts `…/mcp` via a forgiving compare, and a token whose audience kept that spelling
would fail the exact audience match forever.

## Two doors in, one row out

`OAuthClient` holds both kinds of client:

- **DCR** (RFC 7591) — `POST /oauth/register`. Claude Code and most clients.
- **CIMD** — the `client_id` IS an https URL we fetch. What ChatGPT uses.

Supporting one and not the other locks out a whole family of clients, and it is the kind of gap that
hides: the client you test with is the one that does not need the other path.

The CIMD fetch is fenced because the URL is caller-chosen: https only, no redirects followed, a public
address at connect time, 5s timeout, 64KB cap, reusing `health.safe_webhook_url` and
`health.host_is_public` rather than a second copy. The document must also **claim its own URL**, or a
document hosted anywhere could assert someone else's client_id and inherit their consent.

`redirect_uris` are matched **exactly**, never by prefix — `https://good.test/cb.evil` starts with the
registered value, and an open redirect under a registered host turns one sloppy page into stolen
codes.

## The consent screen is the security boundary

It is the only place a human sees what they are granting, so it says it in words — *this spends the
team's balance*, *uses the keys your team registered, without seeing them* — rather than listing
scopes. It warns when a client registered itself, because DCR is open and anyone can arrive with any
name.

It carries the **team picker**, and each option shows that team's balance. Which team a client spends
from is decided here, once, and fixed on the token for the life of the grant: a person in several
teams is asked rather than guessed at. Showing the balance is not decoration — picking a $0.00 team
is the failure this screen exists to prevent, and it happened before the balances were added.

Approval is a POST (a GET that granted access could be triggered by any page that can navigate),
same-origin, and the page inherits `X-Frame-Options: DENY`.

`Origin: null` is accepted **only** when `Sec-Fetch-Site` corroborates it. A browser reports an opaque
origin after certain redirect chains — a consent page reached by way of a sign-in bounce — and
treating that as cross-site made approval fail intermittently.

## Codes and refresh

Authorization codes are single-use and **deleted before validation**, not flagged: holding one while
checking leaves a window where two redemptions both read it. Same reasoning as the conditional UPDATE
in `ledger.reserve` — let the database arbitrate.

Refresh tokens rotate, and the retired row is **kept** so a replay is recognisable. A deleted token
looks merely unknown; a retired one says somebody used a credential that had already been spent. At
that point a client retrying after a dropped response and a thief with a copy are indistinguishable,
so the whole `family_id` is revoked. Being wrong that way costs one sign-in; being wrong the other way
costs somebody's balance.

## Tokens are exchanged, not forwarded

`_internal_auth` validates an OAuth access token and presents it onward as a short-lived (120s)
identity token for the user it names, pinned to the org on the grant. That keeps OAuth inside `mcp.py`
instead of teaching `require_member` a third token type, and it means `_resolve_org` must honour the
pinned team rather than re-deriving it.

Both were found by running the flow rather than testing the pieces: `_oauth_claims` validated tokens
perfectly while every tool still forwarded the raw bearer and got "not signed in".

## Sign-in mid-authorization

A signed-out visitor to `/oauth/authorize` has the destination parked in a short-lived cookie and
resumed at the **dashboard**, which is the one point every browser sign-in door ends at. It stores a
relative path and honours only `/oauth/authorize`, so it cannot become a general "send me anywhere
after login" primitive.

## `/connect-demo`

A page that pretends to be someone else's app and runs the whole flow — register, consent popup,
token exchange, tool calls. It uses only public endpoints; being served from treg's domain gives it
nothing. It exists because a failure inside ChatGPT surfaces as a shrug, and it earned itself
immediately: it found that browsers were refused outright (`"*"` is not a wildcard in the SDK's
origin check) and that consent failed intermittently on `Origin: null`.

## What is deliberately NOT here

- **No per-provider MCP tools.** See above.
- **No routing or failover.** treg publishes facts and calls what it is told; the agent chooses.
- **No second copy of the enforcement rules.** Everything goes through the API's own routes.

## Two ways to authenticate the MCP server: OAuth, or a header

OAuth (above) is the click-to-connect path. There is also a **headless** path, and it is the default
the installer uses: a **team-pinned token as an `Authorization: Bearer` header**. `treg mcp install`
(`mcp_install.py`, sibling of `treg skill bootstrap`) registers the server into every supported agent
with that header — Claude Code via its own `claude mcp add --scope user` (user-global, not the
default project scope; it owns its format and redacts the token), Cursor and opencode via their
documented JSON (`~/.cursor/mcp.json`, `~/.config/opencode/opencode.json`). Codex (TOML +
`bearer_token_env_var`), Hermes (yaml) and OpenClaw are **reported, not written** — their formats
aren't safely expressible from the light CLI (no toml writer, yaml is a server-only dep), so we print
the exact manual step rather than a config we haven't runtime-verified.

The command **verifies the token against `/auth/me` before writing anything** — the same check
`treg login --token` runs. Learned the hard way: without it, a garbage token fans out silently into
every agent on the machine and surfaces days later as per-provider "invalid token" errors inside
whichever agent tries a call — catalog reads still work (they don't validate the token downstream),
which makes it look like a provider outage rather than a setup problem. The garbage in question was
the test suite's own dummy: `install_mcp(only=[])` read an empty list as "no filter" and wrote
`Bearer K` into the developer's real configs on every suite run — `only=[]` now means *none*, and
the test isolates HOME.

Why a header works even though treg advertises OAuth: a client only falls back to OAuth discovery on
a **401**, and treg returns **200** for a valid header — verified against Claude Code, which
otherwise prefers OAuth (issue #59467). The determinant is "does the server 200 a valid header," not
"does it advertise OAuth"; AgentKey behaves the same way. The token is the dashboard's org-baked
"API key" (see [dashboard](../interface/dashboard.md)), so it carries the team and needs no second
header. `curl {BASE}/install.sh | sh -s -- --token <key>` runs the whole thing — install, sign in,
`treg mcp install` — in one paste.
