---
name: add-oauth-provider
description: Add a provider to treg's OAuth registry (the ones treg holds its own approved app for). Use when asked to "add YouTube/Notion/Meta OAuth", "support connecting X", "add a new OAuth provider", or when a connect flow, capability picker, channel/account picker, or provider health probe needs building. Covers the code changes, the platform-side approval steps, and the pitfalls that don't announce themselves.
---

# Adding an OAuth provider

Two connect modes exist. **BYO** (`POST /oauth/start` with a caller-supplied
client_id/secret) already works for any OAuth2 provider and needs no code. This
skill is the **registry** path: treg owns the registered app, so the user picks a
provider and supplies nothing. Only add a provider here if treg holds — or is
willing to go get — the platform approval behind it.

Read `src/treg/oauth_providers.py`'s module docstring first. The rule it states is
the one most easily broken: **scopes are per capability, never per provider.**

## The code changes

All of these, in order. Skipping any one produces a provider that looks fine in
`/oauth/providers` and fails somewhere the tests don't reach.

### 1. Credentials → `src/treg/config.py`

Add `<name>_client_id` / `<name>_client_secret` to `Settings`, loaded from
`TREG_<NAME>_CLIENT_ID` / `_SECRET`. **Reuse an existing pair when the platform
uses one app for several products** — all four Google providers plus YouTube
share `google_client_id`, because Google verification and API audits are scoped to
the *Cloud project*, not to the OAuth client. A second client in the same project
isolates nothing.

### 2. The provider → `src/treg/oauth_providers.py`

Add the `OAuthProvider` and **register it in `REGISTRY`** (easy to forget; the
provider silently doesn't exist until you do).

Capabilities are **cumulative supersets**, never swaps:

```python
scopes={
    "read":   [READ_SCOPES],
    "post":   [*READ_SCOPES, UPLOAD],           # post CONTAINS read
    "manage": [*READ_SCOPES, UPLOAD, WRITE],    # manage CONTAINS post
}
```

`satisfied_capabilities()` is set-containment, so a non-cumulative `write` yields a
connection that can write but reports "no read". `default_capability` is the
broadest (most scopes) — deliberately, see its docstring.

Split a capability whenever the platform splits the scope. YouTube needs
`read`/`post`/`manage` because uploading a video and being able to edit or delete
one are different Google scopes; collapsing them means a connection that can post
and then never fix a typo.

Per-provider quirks worth knowing: `auth_params={}` for providers that reject
Google's `access_type`/`prompt` (LinkedIn, Slack, X), `pkce=True` +
`token_endpoint_auth_method="client_secret_basic"` for X, `extra_credential_*` when
a second credential rides along (Google Ads' developer token).

### 3. Discovery — "which account does this connection act on?"

Set `discover_path` / `discover_key` / `discover_id_field` / `discover_label_field`.

- Label and id fields are **dotted paths** (`_dig`), so nested values work:
  `discover_label_field="snippet.title"`.
- `discover_base_url` when listing lives on a different host than the data API
  (GA4 reports come from analyticsdata, properties from analyticsadmin).
- `discover_nested_key` when the rows are nested one level down.
- `enrich_*` when the listing returns bare ids and a second call is needed for a
  human name (Google Ads).
- Query strings in `discover_path` are fine — discovery does not pass `params`.

Leave it unset if the credential acts on the whole account; the UI then says
"whole account" rather than showing an empty picker.

### 4. `probe_path` — health + the Tools "Use" prefill

A cheap authenticated GET **on `base_url`** (not `discover_base_url` — the probe
runs against the provisioned tool's own host). Without it the tool reads
"unchecked" forever and the Try panel opens blank.

Prefer the path that returns a *human-recognisable* field, and keep it identical to
the sample path in step 5 — `openUse` prefers `health_check.path` over the sample
map, so if they differ the prefill silently changes after a reconnect.

### 5. Dashboard → `src/treg/web/index.html`

Provider rows, the Connect button and the pickers are all data-driven from
`listing()`. **No new provider needs a UI change** — except:

- **A new capability name** must be added to `capLabel` / `capHelp` (~line 2762).
  An unknown name renders as the bare key plus "Requests the *x* scopes."
- **`samplePath`** (~line 3024) is a hardcoded host→path map used when a tool has
  no `health_check` yet. Add the host so the Try panel prefills for tools that were
  provisioned before the probe existed.

### 6. Tests

`tests/test_oauth_providers_m3.py::test_every_provider_is_registered` asserts an
**exact set** of service names. Add yours or the suite fails.

Then: `uv run pytest -q` (full suite — the registry touches health, tools and
connections).

## The platform side (the actual long pole)

The code is an afternoon. Approval is weeks. Do these in parallel, not in series.

1. **Enable the APIs** on the project *before* touching the consent screen — scope
   pickers usually only list scopes belonging to enabled APIs.
2. **Add the scopes.** Ask only for what you can *demonstrate*; see pitfalls.
3. **Redirect URI** must match `{TREG_PUBLIC_URL}/oauth/callback` exactly.
4. **Verification / app review**, where the platform gates sensitive scopes.
5. **Separate product audits** — these are extra and often slower than the OAuth
   review: YouTube's compliance audit + quota extension, Business Profile API
   access, the Google Ads developer token, LinkedIn's Community Management API.

### For the verification submission

- **App name, homepage, privacy policy and demo video must all agree.** The
  consent-screen brand is what reviewers compare everything against — a mismatch
  is a routine rejection.
- **The privacy policy must name the platform's data explicitly** and link the
  platform's ToS plus the provider's privacy policy.
- **The demo video must show each requested scope actually in use**, with the
  client id visible in the consent URL. This is the real constraint on scope
  count — see the first pitfall.

## Pitfalls

Each of these cost real time.

**Never request a scope you can't film.** "Requested scopes exceed what the demo
shows" is the standard rejection. YouTube's `youtubepartner` needs a YouTube CMS
account to make any call at all, so an app without one literally cannot produce
the evidence. Copying a scope list off another product imports approvals it holds
and you don't — ask for what your own demo can show, and add the rest later.

**A short-lived token with no `refresh_token` is a connection that is already dead.** Meta's
authorization-code exchange returns a **1-2 hour** user token and never issues a refresh_token, so
a provider added without `long_lived_exchange=True` produces connections that break before anyone
uses them — the connect succeeds, the probe passes, the tool 401s that afternoon. The extra
`grant_type=fb_exchange_token` call buys ~60 days, which is the ceiling for a Meta user token; it
still can't renew unattended, so it surfaces through `needs_reconnect` like LinkedIn rather than
pretending to auto-heal. Before trusting any new provider, read what its token endpoint actually
returned: `expires_in` absent does not mean "never expires", and the 3600s default in
`exchange_code` will happily invent an expiry that isn't real.

**Publishing status "Testing" expires refresh tokens after 7 days.** Every
connection then breaks weekly — fatal for treg, and invisible until day 8. Confirm
the app is **In production** before trusting any connection made during setup.

**The "hasn't verified this app" interstitial is expected pre-approval.** Click
through via Advanced. Demo videos are necessarily filmed in that state.

**Scope pickers filter on the scope URL, not the product name.**
`yt-analytics.readonly` does not contain the string "youtube". Use the "manually
add scopes" box when the picker won't cooperate.

**`TREG_PUBLIC_URL` decides the redirect_uri, not the port you're browsing.**
Running two dev servers and connecting from the wrong one sends the callback to
the other server, where your session cookie doesn't apply.

**`health_check` attaches at connect time.** Adding a `probe_path` does nothing for
already-connected tools until they reconnect (the existing-tool branch of
`_autoprovision_provider_tool` backfills it).

**httpx replaces a URL's query string whenever `params` is passed — even `[]`.**
`_probe` merges with `copy_add_param` for exactly this reason; don't "simplify" it
back, or any probe path carrying its own query gets truncated and reports a healthy
credential as invalid. Covered by
`test_probe_preserves_a_query_string_in_the_probe_path`.

**`.ttable .tn` is `white-space:nowrap`, and `.ttable-wrap` is `overflow:hidden`.**
Long text in a `.tn` cell widens the table past the modal and *clips the action
button off-screen* rather than scrolling to it — the row becomes unclickable. The
capability modal deliberately does not use `.tn`.

**Test upstreams must be defined at module scope.** Test files use
`from __future__ import annotations`, so FastAPI resolves a route's `Request`
annotation from module globals; a route declared inside a test function 422s
instead of matching.

## Verify before calling it done

```
uv run pytest -q                      # full suite
scripts/dev-local.sh up               # see .agents/skills/dev-local
```

Then, in the dashboard: Connect → capability picker reads in plain English →
consent → account/channel picker shows real names (not bare ids) → `#tools` → Use →
Send returns live data. Only the last step proves the credential, the binding, the
proxy and the base URL are all correct together.
