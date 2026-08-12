---
title: Auth & secrets — injectors, encryption, OAuth freshness, health
status: shipped
sources:
  - src/treg/injectors.py
  - src/treg/crypto.py
  - src/treg/oauth.py
  - src/treg/oauth_providers.py
  - src/treg/health.py
related:
  - architecture/proxy-model.md
  - architecture/data-model.md
  - interface/api.md
---

# Auth & secrets

The hard part: match every credential shape a real skill uses, keep it encrypted, and keep OAuth tokens
alive, without the proxy ever branching on shape.

## Injectors — the seam (`injectors.py`)
The proxy calls `inject(headers, params, binding, secret)`, which dispatches on `binding["injector"]`
through the `INJECTORS` registry (populated by the `@register(name)` decorator). Four shapes, two
mechanics:
- **place a string:** `env_injector`, `cli_auth_injector` → `_place()` renders `binding["format"]` (with
  `{secret}`) into a header or query param per `binding["location"]`/`["name"]`.
- **pull a field from a JSON blob:** `secret_file_injector`, `oauth_injector` → `_token_from_json(blob,
  binding["secret_field"])` extracts a token (default field `access_token`) then `_place()`s it.

`_place()` overwrites a same-named caller param for query bindings so the injected credential wins.
Adding a shape is one function; the proxy never changes.

## Encryption + tokens (`crypto.py`)
Secret values are **Fernet-encrypted at rest**: `encrypt()`/`decrypt()` use the key from
`TREG_SECRET_KEY`, falling back to an ephemeral `_EPHEMERAL` key if unset (so secrets don't survive a
restart — a loud signal to set the key). `new_key()` mints one. Caller tokens: `new_token()`
(urlsafe random) + `hash_token()` (SHA-256); the DB stores only the hash. Values are never returned to
clients.

## OAuth freshness (`oauth.py`)
Two modes, detected by `is_refreshable(blob)` (has `refresh_token` + `client_id` + `client_secret`):
- **auto:** `ensure_fresh(secret, db, client)` — if `is_stale()` (past `expires_at`/`expiry` minus
  `_SKEW=60s`), `refresh()` POSTs `token_uri` (default `_DEFAULT_TOKEN_URI`), re-encrypts + persists the
  new blob, then returns. A **single-flight** `asyncio.Lock` per secret id (`_locks`) plus a
  `db.refresh()` re-check under the lock prevents a refresh stampede. The `_locks` map is now **bounded**:
  before a stale refresh, if it holds more than 512 entries the idle (unheld) locks are dropped — a fresh
  lock is created on next need — so a long-lived worker can't accumulate one lock per secret forever.
  `refresh()` updates both `access_token` and `token` keys so either binding `secret_field` stays fresh.
- **manual:** a bare uploaded token (not refreshable) is injected as-is; the user re-uploads on expiry.

`ensure_fresh` is called by `call_tool()` before injecting, and by the health runner. The injector
stays dumb; one refresh function serves both. Its write-back is **conditional on the prior ciphertext**
(`UPDATE … WHERE value = old`) then reloads the row — so under multiple workers a second refresh can't
clobber a refresh_token the first already rotated (the in-process lock alone doesn't cross processes). `refresh` always stamps a fallback `expires_at` (so a
provider that omits `expires_in` doesn't force a refresh on every call), coerces a null `expires_in`,
and raises a clear error when a 200 body carries no `access_token`; `_expires_at` treats a naive ISO
`expiry` as UTC.

`refresh()` posts the credential's recorded `client_id_param` dialect (TikTok reads `client_key`, not
`client_id`), snapshotted onto the blob at mint time so a refresh months later still speaks the dialect
the grant was minted with.

**Connect flow (mint the first token):** `consent_url(pending)` builds the provider consent URL
(default `access_type=offline` + `prompt=consent` so a refresh token comes back); `exchange_code(pending,
code, client)` trades the auth code for tokens and returns a self-refreshable blob. Both honor
per-provider quirks carried on the `PendingOAuth` (snapshotted from the registry entry, below): a provider's
`auth_params` **replaces** the Google defaults entirely (LinkedIn/X/TikTok/Meta reject `access_type`);
PKCE (`pkce_challenge()` — X requires a verifier); `token_endpoint_auth_method` = `client_secret_basic`
(X puts the secret in HTTP Basic, not the body); the `client_id_param`/`scope_separator` dialect; and
`long_lived_exchange` (`_extend_meta_token()` swaps Meta's ~1-hour code-exchange token for its ~60-day
one, non-fatal on failure). Driven by the `/oauth/*` endpoints ([interface/api.md](../interface/api.md)).

**Expiry as a separate axis (`expiry_of` / `expiry_state` / `connection_view`).** Health answers "does
this credential work"; expiry answers "how long will it keep working" — different questions for a
**non-refreshable** token (a LinkedIn non-partner token reads healthy right up until it silently dies at
~60 days). `secret_is_refreshable(secret)` decrypts server-side (blob never leaves the function) to tell
auto from manual; `expiry_state(expires_at, refreshable)` returns `fresh|expiring|expired|unknown` — a
refreshable credential is **always** `fresh` (treg mints on demand, so the user is never nagged), only an
unrenewable one earns a warning (`EXPIRING_SOON_DAYS=7`). `connection_view()` is the metadata-only shape
(no token material) the dashboard/CLI read, with a single actionable `needs_reconnect` flag.

## Curated OAuth provider registry (`oauth_providers.py`)
Two ways to connect a provider. **Bring-your-own (BYO):** `POST /oauth/start` takes a caller-supplied
`client_id`/`client_secret`/URIs — works for any OAuth2 provider. **Curated:** for the providers where
**treg itself holds the approved app** (Google Search Console/Analytics/Business Profile/Ads, YouTube,
LinkedIn, X, TikTok, Facebook, Instagram, Meta Ads — added PRs #20/#21), the user picks a provider and
consents, supplying nothing. The asymmetry is the point of a hosted registry: the gating cost on these
platforms is the *approval* (a Google Ads developer token, Meta App Review), not the OAuth dance — treg
has already cleared it. treg's own client id/secret load from `Settings` (named by
`client_id_setting`/`client_secret_setting`, so they come from `.env` like every other setting).

Each entry is a frozen `OAuthProvider` dataclass; `REGISTRY` is the `{service: provider}` map. Key
module symbols:
- `get(service)` — look up one provider. `credentials(provider)` — treg's own id/secret (raises if this
  deployment hasn't set them). `is_configured(provider)` — whether this deployment can offer it (a
  `token`-kind provider needs nothing from treg, so always offerable).
- `listing()` — the marketplace payload (`GET /oauth/providers`): every provider, grouped by
  `CATEGORY_ORDER`, each flagged `configured`, with per-capability scopes already in plain English via
  `scope_label()`/`SCOPE_LABELS` (a lookup keyed by the raw scope string;
  `test_every_requested_scope_has_a_plain_english_label` guards it).
- `consent_notice` — one line the dashboard shows **before** the consent popup opens, for a provider
  whose consent screen names something the user has not seen on treg. Only the Meta family carries one:
  the shared Meta app is registered as **Crewlet**, a sibling product of the same company (Superdesign
  Dev Inc), and Facebook renders only that bare app name with no parent business, so without the notice
  a treg user is asked to authorize a stranger. It rides `listing()` like any other provider field, so
  the UI never hard-codes which services get one; `test_only_the_meta_family_carries_a_consent_notice`
  derives the set from `auth_uri == _META_AUTH` rather than a service list. Meta Developer Policy 1.6
  wants the relationship disclosed at the point of consent, which is why this is not a docs link.
- **Scopes are per CAPABILITY, not per provider** (`scopes: dict[capability -> list[scope]]`). Capabilities
  are cumulative supersets (write ⊇ read); `default_capability` is the **broadest** (an agent product needs
  write eventually, so one honest consent screen beats connecting twice). `scopes_for()` /
  `satisfied_capabilities()` decide when a later capability needs a re-consent.
- `auth_kind` = `"oauth"` (treg's app), `"token"` (Slack — a workspace-scoped bot the user creates and
  pastes; `is_token_kind`), or `"key"` (an **API-key provider** connected by pasting a key: Apollo, PDL,
  Akta, Hunter, Crunchbase, Lusha, Coresignal, Diffbot, The Companies API, LeadMagic on a new
  **Enrichment** shelf, TikHub + Bright Data + Just One API under
  Social, under **SEO** Semrush + DataForSEO, SE Ranking, Moz, Majestic, Serpstat, Parallel under
  **Other** (raw `x-api-key`, with an authenticated non-mutating monitor-list probe), and under
  **Advertising** the ad-intel keys SpyFu, Apify, Meta Ad Library, SerpApi — alongside the OAuth ad
  platforms Google Ads + Meta Ads and the **unconfigured** Microsoft Ads, Snapchat Ads, Pinterest Ads
  (standard OAuth, live once this deployment sets their client credentials) and TikTok Ads (a
  placeholder: its non-standard app_id/auth_code/Access-Token flow needs oauth.py work before it runs)). A
  `token` and a `key` share ONE connect/verify/auto-provision path, so `uses_pasted_secret` (`token | key`)
  gates it while `is_token_kind` stays narrow for Slack's bot-only copy; a key provider needs nothing from
  treg, so `is_configured` is always true for it. The pasted credential rides in a header
  (`token_header`/`token_format`, default `Authorization: Bearer {secret}`) or a **query param**
  (`token_location="query"` + `token_param` — Semrush spells its key `?key=`); the connect probe hits
  `base_url`+`probe_path`, or an absolute `probe_url` when the cheapest key-check lives on another host
  (Semrush's balance endpoint on `www.semrush.com`). Validity is read from the HTTP status, a truthy JSON
  `token_verify_field` (Slack's `ok`, Apollo's `is_logged_in` — both answer 200 even on a **bad** key), a
  `token_ok_field`==`token_ok_value` match (Majestic's `Code`=="OK"), a `token_reject_field` present
  (Serpstat's `error`), or an `ERROR`-prefixed text body (Semrush). The connect probe may be a POST with a
  `probe_json` body (Serpstat's JSON-RPC), and `token_encode="base64"` turns a pasted `login:password` into
  the Basic blob for `Basic {secret}` (DataForSEO, Moz). `can_autoprovision` (has a `base_url` and either needs no
  second credential or treg holds it) drives auto-building a callable tool on a successful connect;
  `needs_extra_credential` covers Google Ads' `developer-token` header (a second binding the operator supplies).
- Post-connect helpers the dashboard/CLI drive: resource **discovery** (`supports_discovery`,
  `discover_*` — which site/property/account this connection acts on), row **enrichment**
  (`supports_enrichment`, `enrich_*` — Google Ads returns bare ids, so a per-row lookup fills the human name),
  and **identity** (`has_identity`, `identity_*` — providers with nothing to pick, like LinkedIn/X/TikTok,
  capture who consented instead). A `probe_path` gives registry tools a real health check.

## Credential health (`health.py`)
`run_all(db, client, org_id=None)` iterates tools (filtered to `org_id` when set, so `/health/run` never
leaks another org's credentials): `oauth.ensure_fresh` each oauth secret (a failed refresh → the secret
is `_mark`ed `invalid`), then runs the tool's optional probe via `_probe()` (an injected request to
`health_check.path`, checked against `expect_status`; a non-dict `health_check` is ignored). **Each tool
is processed inside its own try/except** — one bad tool (malformed `health_check`, weird binding, decrypt
error) marks its secrets `unknown` and the batch continues, so a single tool can never 500 the whole run
(regression: it once did). Bindings are read with `b.get("secret_id")`, skipping any without a live secret. Per-secret status is persisted; the run notifies/reports only the secrets **evaluated this run** (not
every persisted-`invalid` secret, or a since-unbound one would be re-alerted forever). a secret unbound from its last tool is reset to `unknown` (no frozen stale verdict). `_notify()`
best-effort POSTs invalid credentials to the owner's `webhook_url` — searching **all** the owner's
memberships (webhooks are usually set only on the personal org, so a team-org credential would otherwise
never alert), then falling back to a current org-owner's webhook if the owner has left — but only to a
**`safe_webhook_url`** target: `webhook_url` is user-set (even via the
unauthenticated `register_user`), so non-http(s) / loopback / private / link-local hosts are rejected at
set-time and re-checked before POST (blind-SSRF guard). Triggered on demand or by a cron hitting
`POST /health/run` (a super-admin may pass `?all_orgs=1` so one cron token sweeps the whole platform).
Verdicts follow **worst-status-wins** within a run (a no-probe tool can't downgrade a secret a real
probe just marked `invalid`), a transport error / `5xx` / `429` maps to `unknown` (not a false `invalid`
+ webhook spam), an injection failure maps to `invalid`, and only secrets **evaluated this run** are
notified/reported. The run also calls `gc_expired_invites(db, org_id)` + `gc_stale_pending_oauth(db,
org_id)` (abandoned OAuth connects hold an encrypted client_secret + a replayable `state`, so they
expire after `OAUTH_PENDING_TTL_MIN`). Alongside the probe verdicts the run sweeps an **expiring** list
over **every** oauth secret via `needs_reconnect()` (built on `oauth.expiry_state`) — not just the ones a
tool probe touched — because an unbound, unprobed, perfectly-healthy credential can still be days from
silent death (the LinkedIn shape). `_view()` now carries `provider`/`refreshable`/`expiry_state`/`expires_at`
so the caller sees both axes. `_probe()` merges a binding's query onto the URL with `copy_add_param` rather
than passing `params=` (httpx would otherwise **replace** a probe path's own query string, e.g. YouTube's
`?part=snippet&mine=true`, and fail a healthy credential).

## Storage / security posture (MVP)
TLS-only in transit (paste/upload over https, like GitHub/Vercel secrets); Fernet at rest. Per-membership
tokens gate the API (`require_member`, [interface/api.md](../interface/api.md)) and scope every call to an
org ([multi-tenancy](multi-tenancy.md)). "Use-without-hold": a tool binding may reference a secret a
teammate uploaded in the same org; the key stays server-side. Later: local-key end-to-end encryption,
finer permission tiers.

## Secret kinds + the one release exception
`kind` also gained **`param`** — a non-secret value (project/org id) stored and injected like a secret but
never health-checked or marked invalid (config, not a credential). Every kind is still Fernet-encrypted at
rest and never returned by `_secret_view`. The **only** sanctioned path that returns a value is a local-run
**grant** ([local-run](local-run.md)): member+ only, owner-opt-in per tool, audited, and for oauth it
releases only the short-lived leaf (the access token) — `refresh_token`/`client_secret` never leave the
server (an oauth `secret_field` allow-list enforces this). Even then the value is not handed to the
member: on Linux the CLI runs as a dedicated `treg-run` user, so the credential lives under that user
(unreadable by the member's uid), not on the member's own account. A deliberate, narrow exception.

**Ownership boundary (who may use which secret).** A member may only **bind/inject a secret they own**
(`_validate_bindings` / `_validate_cli_secrets`, both calling `_require_secret_ownership`); admins/owners
may wire up shared-key tools. This stops a
member laundering a teammate's key into a tool they control (then exfiltrating it via the proxy's
`base_url` or via `/grant`). Editing a tool **grandfathers** the secrets already on it — only a
newly-added binding/inject is ownership-checked — so re-saving an admin-wired shared-key tool doesn't lock
its owner out. And a `/grant` that would return a secret the caller does **not** own (a
shared-key tool they may run but not read) requires the **runner proof** (`X-Treg-Run-Proof` ==
`TREG_RUN_PROOF`, held only by the root-installed `treg-run` runner) — so a direct member call can't read
someone else's key value. A tool's `base_url` is validated against the internal-address block-list (loopback/private/link-local/
metadata, incl. numeric IP encodings) at registration AND the proxy re-resolves the host at call time
(`health.host_is_public`, gated by `proxy_ssrf_check`) — no SSRF, even via DNS rebinding.
