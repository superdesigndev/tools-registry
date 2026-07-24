---
title: The proxy — faithful credential-injecting relay + tool resolution
status: shipped
sources:
  - src/treg/proxy.py
  - src/treg/api.py
related:
  - architecture/data-model.md
  - architecture/auth-secrets.md
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

**Accept-Encoding is normalized to `identity`** when the caller sent none. `relay()` streams the upstream
body raw (`aiter_raw`), so if the caller doesn't ask for compression httpx would otherwise add its own
`Accept-Encoding: gzip` and hand a plain HTTP client / agent compressed bytes it never requested. Asking
for `identity` keeps what the caller receives matching what the caller requested.

## Tool resolution (`_resolve_call` in api.py)
`* /call/{rest:path}` → `call_tool()` → `_resolve_call(rest, caller.org_id, db)` returns
`(tool, upstream_url)`. **Both shapes are scoped to the caller's org** (`Tool.org_id == org_id`), so two
orgs resolve independently and may reuse a tool name or upstream host; `call_tool` then loads only
same-org secrets. After resolution `call_tool` runs `_enforce_daily_cap` (the per-user daily usage cap —
429 when over; `-1`/default is a no-op, so the hot path adds no query for unmetered members). Two shapes:
- **URL-passthrough (agent-native):** `rest` is the real upstream URL (`/call/https://api.intercom.io/me`).
  `_normalize_scheme()` restores the `https://` a path param collapses to `https:/`. The tool is resolved
  by **host** (`_host_of()` = `urlsplit(...).netloc`, matched against the indexed `Tool.host`) then the
  **longest `base_url` prefix**; a tie → `409`, no match → `404`.
- **Named:** `rest = "<tool>/<path>"` (`rest.partition("/")`), looked up by `Tool.name`; upstream URL =
  `base_url + path`. **No path → the base URL itself, without a trailing slash** — a tool pinned to a
  full resource (`.../v1/charges`) must relay as-is, since Stripe `404`s `/v1/charges/`.

`call_tool()` loads every bound secret (running `oauth.ensure_fresh` on oauth secrets first — see
[auth-secrets](auth-secrets.md)), calls `relay()`, then fires `audit.record_call(...)` off the response
path. Methods allowed: GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS.

**Resolution + error hardening:** the URL-passthrough prefix match respects a **path-segment boundary**
(`norm == base` or `base + "/"`), so `.../v1` no longer matches `.../v10/...` and inject the wrong
credential; the longest-prefix tiebreak compares rstripped lengths (a trailing-slash duplicate is a real
`409`, not a silent winner). When two same-host tools still tie on prefix length, `_resolve_call`
**prefers the registry-provider-backed tool** (one whose binding points at a `Secret` with a `provider`)
over a hand-registered one that often holds a stale credential — a `409` there would break exactly the
agent-facing URL-passthrough callers who never typed a tool name; only a genuine ambiguity (neither or
both provider-owned) still `409`s. Binding validity is checked at **registration** (`_validate_bindings` rejects
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
