---
title: The proxy — faithful credential-injecting relay + tool resolution
status: shipped
sources:
  - src/treg/proxy.py
  - src/treg/api.py
related:
  - architecture/data-model.md
  - architecture/auth-secrets.md
  - architecture/ads-conversions.md
  - foundation/charter.md
---

# The proxy (the whole product in one function)

The relay is `relay()` in `src/treg/proxy.py`. The API resolves which tool a request targets and loads
its secrets; `relay()` injects and streams. It runs no business logic and never buffers the body.

## The faithful-relay contract
`relay()` alters **only three things**; everything else is verbatim (method, path, all query params
incl. duplicates, headers, cookies, body bytes):
1. **hop-by-hop transport headers** — `_HOP_BY_HOP` (host, content-length, connection, keep-alive, te,
   trailers, transfer-encoding, upgrade, proxy-*); re-derived per hop or the stream corrupts.
2. **treg's control/infra + edge forwarding headers** — `_CONTROL` (`x-treg-token`, `x-treg-org`,
   `ngrok-skip-browser-warning`, `x-forwarded-*`, `x-real-ip`, `forwarded`, `via`), dropped via
   `_DROP_REQUEST = _HOP_BY_HOP | _CONTROL`, so none leaks upstream. `_scrub_treg_cookies` also strips
   treg's own cookies (`treg_session`, `treg_oauth_state`) from the Cookie header — the dashboard's
   `credentials:'include'` Try-it would otherwise leak our session token — while keeping other cookies.
3. **the injected credential(s)** — each binding overwrites only its target header/param.

> **What treg keeps from a call.** Successes retain no content: the relay forwards bytes and the audit
> row records status, size and timing. A **failed relayed call** — platform, own-key, or plain own-tool
> — is the exception: `CallRecord.error_request` / `error_response` retain a redacted, truncated copy
> of what the caller sent and what the provider (or treg-side 502) answered. Without it a failure is a
> bare status code: `path` holds the catalog URL rather than the caller's parameters and `params_hash`
> is one-way. Metered responses are already buffered by `_buffer_response`; `_peek_stream_head` reads
> only the first 8 KiB of a failed unmetered response and replays every consumed byte before the rest
> of the original iterator, preserving status, raw headers, streaming, and the upstream-close task.
> Caller bodies on unmetered paths are cached only when `Content-Length` is declared and at most 64
> KiB; large/chunked uploads stay streaming and retain only their query-param half. See
> [data-model](data-model.md) for the redaction order, admin-only access, and retention.

Faithfulness mechanics inside `relay()`:
- request headers rebuilt from `request.headers.raw` into an `httpx.Headers` multidict (preserves
  duplicate headers / cookies); injection (`headers[name] = v`) overwrites only the named one.
- query as a list from `request.query_params.multi_items()` (keeps duplicate keys like `?tag=a&tag=b`).
- path rebuilt from `request.scope["raw_path"]` (in `call_tool`), not Starlette's URL-decoded path
  param — percent-encoding survives to the upstream (npm's scoped publish `PUT /@scope%2fname` 404s
  if `%2f` is decoded to a literal slash).
- body streamed via `content=request.stream()` (stream, never buffer). Exception: a caller may
  base64/gzip-encode the body with `X-Treg-Body-Encoding` to slip SQL/HTML past a hosting-edge WAF;
  `_BodyDecodeMiddleware` (in api.py) then buffers + decodes it *before* `relay()` runs, so the relay
  still forwards the real plaintext bytes verbatim upstream. See [api](../interface/api.md).
- upstream call uses the **shared** `client` (the long-lived `httpx.AsyncClient` at `app.state.http`,
  created in `lifespan` — keepalive is the biggest latency win).
- response streamed back with `StreamingResponse(upstream_resp.aiter_raw(), …)`; every upstream response
  header (incl. multiple `Set-Cookie`) is re-attached via `response.raw_headers` minus `_DROP_RESPONSE`,
  and cleaned up with `BackgroundTask(upstream_resp.aclose)`.

A request may carry several credentials: `relay()` loops `tool.bindings` and calls
`injectors.inject(headers, params, binding, crypto.decrypt(secret.value))` per binding.

**Platform bindings — injecting treg's OWN credential.** A binding with a `platform_setting` key (instead
of a `secret_id`) injects one of treg's own credentials read from `get_settings()` — the Google Ads
developer token is the case that exists. The value never lives in the org's secret store, so a tenant
can't read it or extract it through a local run; a missing setting is a clean `502`
(`this server has no <setting> configured`). Used by the OAuth-marketplace auto-provisioner for a provider
that needs a second credential treg holds centrally (see [api](../interface/api.md)).

A separate case that looks similar but is NOT a platform binding: the Google Ads **conversion**
uploader (`adsconv.py`) also spends treg's own platform connection, but it is not a caller-issued
`/call/` request at all, so it never reaches `relay()` or `injectors.py` — it reads the platform org's
stored OAuth secret directly and builds its own headers. See [ads-conversions](ads-conversions.md).

**Accept-Encoding is normalized to `identity`** when the caller sent none. `relay()` streams the upstream
body raw (`aiter_raw`), so if the caller doesn't ask for compression httpx would otherwise add its own
`Accept-Encoding: gzip` and hand a plain HTTP client / agent compressed bytes it never requested. Asking
for `identity` keeps what the caller receives matching what the caller requested.

## Tool resolution (`_resolve_call` in api.py)
`* /call/{rest:path}` → `call_tool()` → `_resolve_call(rest, caller, db)` returns
`(tool, upstream_url)`. **Both shapes are scoped to the caller's org** (`Tool.org_id == org_id`), so two
orgs resolve independently and may reuse a tool name or upstream host; `call_tool` then loads only
same-org secrets. After resolution `call_tool` runs `_enforce_daily_cap` (the per-user daily usage cap —
429 when over; `-1`/default is a no-op, so the hot path adds no query for unmetered members). Two shapes:
- **URL-passthrough (agent-native):** `rest` is the real upstream URL (`/call/https://api.intercom.io/me`).
  `_normalize_scheme()` restores the `https://` a path param collapses to `https:/`. The tool is resolved
  by **host** (`_host_of()` = `urlsplit(...).netloc`, matched against the indexed `Tool.host`) then the
  **longest `base_url` prefix**; a tie → `409`, no match → `404` (or `403` when the caller's ACL is the
  only thing that removed the match — see below).
- **Named:** `rest = "<tool>/<path>"` (`rest.partition("/")`), looked up by `Tool.name`; upstream URL =
  `base_url + path`. **No path → the base URL itself, without a trailing slash** — a tool pinned to a
  full resource (`.../v1/charges`) must relay as-is, since Stripe `404`s `/v1/charges/`.

Named misses also inspect the org's caller-usable own tools on the error path. When a dotted operation
name shares its provider/first segment with one (for example `google-analytics.report` beside the
connected `google-analytics` tool), the 404 carries `hint` plus `did_you_mean` and points at
`/call/google-analytics/<path>`. If that dotted name is a real catalog endpoint, the hint follows the
catalog fall-through and is attached only if the marketplace credential ladder also dead-ends. Catalog
near-id matching remains provider-local and takes precedence for genuine misspellings.

If both shapes miss with 404, a dotted target gets one final lookup in the endpoint catalog. A live
row enters `_resolve_marketplace_call` and its credential ladder. `_marketplace_upstream` fills catalog
path placeholders by percent-encoding raw values, but preserves a value containing a valid `%HH` escape;
this prevents an already encoded Search Console property id such as `sc-domain%3Aexample.com` becoming
double-encoded as `%253A`. Literal/invalid percent signs remain encoded. A `retired`/`broken` tombstone is
instead refused with 410, its `status_note`, and its optional `superseded_by`, before credentials are
selected or the relay can run; the refusal is audited as `refused_by=retired`. This ordering is
deliberate: an org's own tool named exactly like the old catalog id already resolved above and is not
shadowed, while URL passthrough has no catalog-id shape to catch accidentally.

**A dead end names its capability siblings.** Both refusals that end the ladder — the `410` tombstone
with no `superseded_by`, and the tier-3 `404` when no credential can be found — append
`_capability_alternatives(ep)`: the other providers catalogued for the same `capability`, cheapest
first, each marked *callable now on treg's key* (both halves of `_platform_offer`'s tier-4 test hold)
or *needs your own `<provider>` credential*. It is derived from `cat.endpoints`, which `_parse` has
already stripped of marked rows, so a retirement stops being suggested the moment it is marked and no
list is maintained by hand.

Two facts motivate it. 41 of the 50 TikHub retirements have no same-provider successor, so
`superseded_by` is structurally silent for them and a cross-provider sibling is the only migration
path left. And on 2026-08-19 one org spent 268 calls on `meta-ad-library.meta-ads.library.search`,
which treg holds no key for, while `scrapecreators.x.v1-facebook-adlibrary-search-ads` — the same
capability string, on a key treg already had — answered 192 of 208 calls for fourteen other teams.
The refusal knew the capability the whole time.

This **compares, it does not route**: treg never fails over on the caller's behalf, so the refusal
stands, nothing is substituted, and the choice stays with the caller. The helper is deliberately
synchronous and I/O-free — observed success would need `endpoint_stats.observed` and a database
round-trip on an error path, which is how a 404 becomes a 500, and `catalog get` already ranks the
same siblings by observed success when the caller follows the pointer. It can only see curated
`capability` values: an endpoint with a blank capability is invisible as an alternative, which is an
argument for filling those in rather than for fuzzy id matching.

**ACL-filtered candidates.** `_resolve_call` takes the **caller** and filters passthrough candidates by
`_tool_usable` (project scope AND the per-tool list) **before** the longest-prefix tiebreak. A same-host
tool the caller cannot use must not be able to cause a `409` — or win the tiebreak — for someone who
cannot even see it in `list_tools`. This only NARROWS the candidate set, so it can never grant access:
whatever resolves still passes `_require_tool_use`. The named shape needs no filter (it resolves one
tool, then the gate runs).

**"Not yours" is a `403`, not a `404`.** Narrowing the candidate set to empty first read as *nothing is
registered here*, so a caller with no access was told the tool did not EXIST — which sends an admin
hunting for a registration that is already there (round-4 finding #3). `_resolve_call` keeps the
**unfiltered** host matches alongside the filtered ones: if a tool would have matched and only the ACL
removed it, the answer is `403`, the same verdict the named shape has always given. A host with nothing
registered is still `404`. The `403` names only the **host the caller typed**, never the tool name the
ACL is there to hide.

**Policy deny (`_enforce_deny`, `_deny_match`).** After resolution and the tool ACL, the resolved
upstream is matched against the org's `DenyRule` rows (org-wide + the ones aimed at this caller) →
`403` naming the rule. Evaluating the **resolved** upstream is what makes both call shapes equally
gated — a caller cannot dodge a rule by switching to URL-passthrough — and the relay does not follow
redirects, so a blocked host is not reachable via a 3xx bounce. The path match is anchored at a
segment boundary (`/v1/charges` must not match `/v1/chargesX`), the same trap `_resolve_call` guards.
It applies to **every role including owner** (a guardrail, not a permission tier) and to both run
tiers, where the tool's own `base_url` host stands in for the request path. `_deny_match` is pure, so
it unit-tests without a DB — mirroring `localrun.check_deny`, which is the same idea one layer down
(argv instead of URL). Zero rules = one indexed query and no behavior change. A rule may also carry a
`project_id`: it then fires only on calls through that project's tools (every enforcement point has a
resolved Tool by then, so `_enforce_deny` takes `tool.project_id`); an org-wide-tool call is never
caught by a project rule. The three scope axes — host/path/method, member, project — are ANDed and
each is NULL-means-any.

**Whose refusal is this?** Every treg-side error on a `/call/` path carries `X-Treg-Error: 1`
(`_mark_treg_own_errors`, see [api](../interface/api.md)) — status and body unchanged. A caller cannot
otherwise tell treg's 404 ("no tool registered for that host") from the vendor's own; the
[local proxy](local-proxy.md) uses the marker to explain a failure without ever rewriting a real vendor
response.

`call_tool()` loads every bound secret (running `oauth.ensure_fresh` on oauth secrets first — see
[auth-secrets](auth-secrets.md)), calls `relay()`, then fires `audit.record_call(...)` off the response
path — and, when a PostHog key is configured, mirrors the same funnel as a `tool_called` product-analytics
event (`analytics.capture`, see [data-model](data-model.md)): vendor = the catalog provider slug, or the
upstream host for own tools. Methods allowed: GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS.

**Resolution + error hardening:** the URL-passthrough prefix match respects a **path-segment boundary**
(`norm == base` or `base + "/"`), so `.../v1` no longer matches `.../v10/...` and inject the wrong
credential; the longest-prefix tiebreak compares rstripped lengths (a trailing-slash duplicate is a real
`409`, not a silent winner). When two same-host tools still tie on prefix length, `_resolve_call`
**prefers the registry-provider-backed tool** (one whose binding points at a `Secret` with a `provider`)
over a hand-registered one that often holds a stale credential — a `409` there would break exactly the
agent-facing URL-passthrough callers who never typed a tool name; only a genuine ambiguity (neither or
both provider-owned) still `409`s. That 409 names every caller-usable colliding tool and directs the
caller to the unambiguous `/call/<name>/<path>` form. Binding validity is checked at **registration** (`_validate_bindings` rejects
an unknown `injector` and a cross-org/dangling `secret_id`; `register_skill` runs the same gate), and
`call_tool` translates a call-time injector `ValueError` and an upstream `httpx.RequestError` into a
`502` instead of an unhandled 500 (and audits the failed attempt, not just successes). A binding
`format` is validated to render with only `{secret}` and `name`/`secret_field` to be non-empty strings;
duplicate `location:"query"` binding names are rejected (they'd silently overwrite each other).
`health._probe` skips a dangling binding rather than `KeyError`-ing the whole run.

**Relay security + faithfulness (bug-hunt):** the response side strips a `Set-Cookie` for treg's own
cookie names (an upstream must not overwrite `treg_session`/`treg_oauth_state` — fixation) and adds
`X-Content-Type-Options: nosniff` + `Content-Security-Policy: sandbox` (a browser navigating to `/call/…`
must not execute upstream HTML/JS under treg's authenticated origin). It keeps `Content-Length` on a
bodyless reply (HEAD/204/304), only carries a request body when the caller sent one (no bogus chunked
frame on a GET), and honors headers a peer marks hop-by-hop via its `Connection` header (RFC 7230).
`injectors._token_from_json` rejects a non-string field value instead of injecting garbage.

**Call-time SSRF guard (DNS-rebinding defence).** Just before the upstream `send`, `relay()`
re-resolves the upstream host (`health.host_is_public`, gated by the `proxy_ssrf_check` setting) and
refuses with a `502` if any resolved address is internal (loopback/private/link-local/reserved/multicast).
This catches the case where a `base_url` was public at **registration** but its DNS now points at an
internal target like `169.254.169.254` or localhost — the registration-time check alone can't stop a name
that resolves differently later. Registration itself (`health.safe_webhook_url`, reused for `base_url`)
also rejects numeric IP encodings — decimal/hex/octal/short forms like `2130706433` / `0x7f000001` /
`127.1` are normalized via `inet_aton` and re-checked, so they can't sneak past the literal-IP block.
(A narrow resolve-vs-connect race remains; pinning the resolved IP would need a custom transport.)

> Why relay instead of modeling the upstream: [foundation/charter.md](../foundation/charter.md).

## treg's own headers never reach the upstream — by PREFIX, not by name

`proxy._DROP_REQUEST` used to enumerate our control headers, and the enumeration had already failed:
`x-treg-client` was never in it, so every provider we relay to had been receiving the caller's runtime
name. Adding `x-treg-meta` (which carries a reselling builder's customer ids) to that list would have
repeated the defect one header later.

So the rule is structural: **any request header whose lowercased name starts with `x-treg-` is
dropped** before relay (`proxy._is_dropped_request_header`). `_CONTROL` remains for the non-prefixed
infra names — `ngrok-…`, `x-forwarded-*`, `via` — which have no shared prefix to key on.

The test asserts an *invented* header (`X-Treg-Future`) is dropped too, so the guarantee is about the
prefix rather than about today's list.
