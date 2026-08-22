---
title: Data model — the registry tables, async DB, audit writer
status: shipped
sources:
  - src/treg/models.py
  - src/treg/db.py
  - src/treg/referrals.py
  - src/treg/audit.py
  - src/treg/analytics.py
  - src/treg/ratestore.py
related:
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
  - architecture/ads-conversions.md
---

# Data model

SQLModel tables in `src/treg/models.py`. Kept minimal on purpose. Org multi-tenancy adds `Org`,
`Membership`, `Invite` and an `org_id` on the resource nouns — the tenancy mechanics live in
[multi-tenancy](multi-tenancy.md); this fragment is the table reference.

- **`Org`** — the tenant that owns resources: `id, name, slug` (unique), `suspended` (admin lock),
  `demo` (a sandbox team seeded by [onboarding](../interface/onboarding.md) — labeled + removable),
  `public_demo` (a team whose member token is PUBLISHED, e.g. on the landing page — non-admin members
  are locked to `/call` + reads and may never act as a user; gated in `api.require_member` /
  `require_identity`), `created_at`. **`ad_gclid`/`ad_click_id_type`/`ad_click_at`/`ad_landing`**
  (migration A37, all nullable) — set once, at signup, from the first-party `treg_ad` cookie; never
  overwritten. The historically named `ad_gclid` holds the click value; `ad_click_id_type` says
  `gclid`/`gbraid`/`wbraid`, with NULL meaning a legacy GCLID. **`first_call_at`** (same migration) —
  set once by a guarded UPDATE in the `/call/` handler,
  deliberately NOT derived from `CallRecord` (which `audit.py` sheds under load, undercounting exactly
  when traffic is highest). Both feed [ads-conversions](ads-conversions.md).
- **`User`** — a **global identity** only: `email` (unique), `is_superadmin` + `suspended` (platform
  flags, see [super-admin](super-admin.md)), `token_version` (bump to revoke every session cookie +
  identity token this user holds — the signed token carries the `tv` it was minted at; see `sess.make`
  / `auth_revoke_tokens`), `onboarded` (completed/skipped first-run), `demo` (a
  fake onboarding teammate — can't log in, excluded from stats), `created_at`. (The token + role moved
  to `Membership`; a user in N orgs has N memberships.)
- **`Membership`** — links a user to an org: `user_id`, `org_id`, `role` (owner|admin|member),
  `token_hash` (SHA-256 of the bearer token, shown once), `webhook_url` (health alerts POST here),
  `daily_call_cap` (per-user, per-day usage cap; **-1 = unlimited**, the default — see
  `api._enforce_daily_cap`); unique `(user_id, org_id)`. **A token = a `(user, org)` pair.** `ROLE_RANK`
  orders the roles.
- **`Invite`** — a one-time join code: `org_id, email, role, code_hash (idx), status`
  (pending|accepted|revoked), `invited_by`. Carries a SECOND split secret, `email_token_hash (idx,
  nullable)` — the inbox-only sign-in token embedded ONLY in the invite email's link (the
  admin-visible code is join-only, never an auth factor); nulled on first use (one sign-in per link),
  NULL on pre-split invites (they fall back to the prefilled-login flow). See `api.auth_invite_signin`.
- **`Secret`** — a stored credential: `org_id` (FK, idx), `name`, `owner` (creator email), `kind`
  (`env` | `secret_file` | `oauth` | `cli_auth` | `param`), `value` (**Fernet-encrypted at rest**, never
  returned), `bundle_id` (FK), and health fields `health_status` (`unknown`|`ok`|`invalid`) /
  `health_detail` / `health_checked_at`. `param` is a non-secret value (project/org id) injected like a
  secret but never health-checked. **Connection metadata** (set for registry-minted OAuth connects — see
  the OAuth marketplace / `oauth_providers.py`; empty for uploaded or bring-your-own-app credentials):
  `provider` (**indexed** — which curated registry provider minted it), `granted_scopes` (space-joined,
  what the user ACTUALLY consented to), `resource_ref` + `resource_name` (the chosen site/property/account
  this connection acts on, plus its human label since upstream ids are opaque). **Expiry is a separate
  axis from `health_status`** — `health` says "does it work", expiry says "how long will it keep working"
  (a non-refreshable token stays healthy right up until it silently dies): `expires_at`, `last_refresh_at`,
  `last_error`.
- **`Tool`** — a callable capability: `org_id` (FK, idx), `name` (**unique per `(org_id, name)`**),
  `owner`, `base_url`, `host` (netloc of base_url, **indexed** for URL-passthrough resolution),
  `bindings` (a **JSON list** — see below), `health_check` (optional JSON), `examples` (optional JSON
  list `[{method,path,note}]` surfaced in the dashboard), `cli` (optional JSON local-run profile for
  `treg run --local` — see [local-run](local-run.md)), `bundle_id` (FK).
- **`Bundle`** — a skill (pure packaging): `org_id` (FK), `name`, `owner`, `recipe` (the SKILL.md text),
  **`files`** (JSON `{relpath: content}` — the rest of the folder: reference docs, scripts, nested subdirs,
  minus secrets + binaries, so a WHOLE skill folder travels via `skill install`), grouping its secrets +
  tool(s). Run config for **both** `treg run` tiers now lives on **`Tool.cli`** (the tool-side
  unification, PR #3); the old bundle-side `runtime`/`package`/`entrypoint`/`runnable` columns were folded
  into `Tool.cli` by a startup migration and are no longer declared (they may persist physically in old
  DBs, unread).

- **`PendingOAuth`** — an in-flight connect flow: `org_id` (FK), `state` (unique, the CSRF/lookup key),
  `client_id`, `client_secret` (encrypted), `auth_uri`, `token_uri`, `scopes`, `redirect_uri`, `status`
  (`pending`|`done`|`error`), `secret_id` (the secret created on success), `detail`. **Marketplace/quirk
  fields**, all defaulted, carried through the redirect so the callback exchanges the code exactly the way
  the consent URL was built: `provider` (which curated registry provider this connect is for — `""` for a
  bring-your-own-app connect), `code_verifier` (PKCE; `""` = unused), `auth_params` (JSON of extra
  consent-URL query params), `token_endpoint_auth_method` (`client_secret_post` default),
  `client_id_param` + `scope_separator` (TikTok spells the client id `client_key` and comma-joins scopes),
  `long_lived_exchange` (Meta only — swap the short-lived token for a ~60-day one before storing), and
  `replaces_secret_id` (which existing connection this consent REPLACES — null = add a new one, so the
  callback no longer has to blanket-replace by provider).
- **`CallRecord`** — the proxied-call audit row: `org_id`, `user_email`, `tool_name`, `method`, `path`,
  `status_code`, `kind` (**`call`** = proxy `/call`, **`local_run`** = `/tools/{name}/grant`), `created_at`,
  and `refused_by` (migration A29, nullable): set when **treg itself refused the call before anything went
  upstream** — `auth` | `policy` | `balance` | `cap` | `resolution` | `request` — and NULL whenever the
  provider actually answered. Every `/call/` refusal now leaves a row: the in-handler audits stamp their own
  kind, and refusals that raise **before** the handler's audit exists (bad token, unknown tool, ACL, deny
  rule, daily cap) are recorded by a fallback in the `X-Treg-Error` exception handler — the one place every
  refusal passes through — using the identity stashed in `request.state` (a bad-token 401 records
  anonymously). It is what tells "the provider failed" apart from "we said no": a paywall 402 must not read
  as a provider error, and `endpoint_stats` excludes refused rows entirely.
  It also carries **`error_request` / `error_response`** (migration A36, nullable) — redacted,
  truncated evidence for a **failed relayed call**, and the one exception to "bodies are never
  stored". PR #139's platform-only policy was deliberately reversed after production showed zero
  evidence on own-tool failures. The fields are now written for marketplace calls at every tier and
  for plain own tools, from three places: the response path (the provider's own body, since a relayed
  non-2xx returns as a `Response` and is never raised, plus
  an **allowlisted set of response headers** — `Retry-After`, `WWW-Authenticate`, the rate-limit
  trio, request/trace ids — because an empty-bodied 401 or 429 is otherwise undiagnosable and those
  headers *are* the answer); the `except HTTPException` branch (treg's own `detail`, covering
  the 502s — upstream timeout, failed injection, SSRF refusal — where a bare status says least); and
  the reserve refusal, where `detail` names **which** cap was hit, since every 429 collapses to
  `refused_by='cap'` and that one value spans member, tag, org, platform and trial limits.
  Never written for a success; `/calls` defers and omits both fields, keeping them admin-only.
  Redaction is exact-match-first for every injected credential: platform settings and decrypted org
  Secrets. OAuth/secret-file JSON masks the raw blob, injected field, `Bearer` form, and string values
  under the sensitive-key allowlist (including the binding's `secret_field`), while diagnosis fields
  such as `scope` and `token_type` survive. Only then do pattern rules run, because a provider quoting
  the received key back in a 401 can defeat any regex. Masking happens **before** truncation, since
  truncating first can leave a partial key that no longer matches. Exact matches cover every spelling
  a provider can echo, not only what treg sent: percent-encoded in **both** cases (`quote()` emits
  uppercase, servers echo lower), `quote_plus`, JSON-escaped, and — for the Basic-auth providers whose platform
  value is *itself* the base64 of `login:password` (dataforseo, moz; see `config.py`) — the **decoded
  credential and each half**, since a provider that decodes Basic and reports
  `received_username`/`received_password` echoes the key in a form containing neither the blob nor
  `Basic <blob>`. Any credential-rendering error replaces evidence with
  `'<redacted: could not render credentials for masking>'` instead of risking unmasked content or a
  500. Behind all of it sits a **fail-closed** check: after masking, a normalised copy
  (percent-decoded, JSON-unescaped, lowercased) is re-scanned, and if a secret survived a transform
  nobody anticipated the whole snippet is replaced. Losing a message beats leaking a credential.
  Unmetered request bodies are cached only with a declared `Content-Length` at or below 64 KiB; failed
  streaming responses contribute only their first 8 KiB and are replayed byte-for-byte to the caller.
  Aged out to `'<expired>'` after 14 days by
  `GET /admin/errors` — not on the request path, because `get_session` never commits and a lazy
  marker written there would roll back, leaving the purge to run on every failed call.
- **`ToolRequest`** — a "the catalog doesn't have X" report (`POST /tool-requests`, open + per-IP
  rate-limited): `capability` (the headline, ≤200 chars), `query` (the search that came up empty —
  auto-filled by agents, the dedup/priority signal), `note`, `contact`, `source` (`web` | `cli` |
  `mcp` | `api`), `status` (`open` | `done` | `dismissed`, flipped by hand), and **nullable**
  `org_id`/`user_email` — identity is attribution when the caller happens to have one, never a
  requirement, because the usual filer is an agent with zero results and no token. Reviewed by
  querying the table; a Slack notifier may hang off the insert later, but the row is the record.
- **`SearchMiss`** — a catalog search that returned **nothing**: `query` (capped to 300 chars),
  `source` (`api` — the HTTP route serving web + CLI + raw API — or `mcp`), `created_at`. The demand
  signal one step before a `ToolRequest`: most agents that miss never file, so the query text is all
  they leave. Written fire-and-forget through `audit.record_search_miss` (dropped rows cost
  analytics, never a search) from both search paths — `GET /catalog/search` and the in-process MCP
  `catalog_search` tool. Deliberately identity-free; surfaced by `scripts/usage_report.py`, which
  reads misses against the catalog to split coverage gaps from naming/discovery failures.
- **`RunRecord`** — the **server-side run** audit row (a `treg run --server` CLI execution — the "kind"
  `server_run` in usage rollups): `org_id`, `user_email`, `bundle_name` (holds the **tool** name since the
  tool-side run unification; column name is historical), `argv` (JSON — never carries a secret value;
  secrets are injected via env, not the command line), `exit_code`, `duration_ms`, `created_at`. Written
  off the request path like `CallRecord`. **Usage metering** (`GET /orgs/{id}/usage`, per-user daily caps)
  counts `CallRecord` + `RunRecord` together — see [the API fragment](../interface/api.md).
- **`AdConversion`** — the Google Ads conversion outbox: `org_id`, `action` (`signup`|`first_call`|
  `paid`), `dedupe_key`, `value_usd_micro`, `created_at`, `uploaded_at` (NULL = not yet uploaded),
  `next_attempt_at` (backoff), `failed_at` (terminal/dead-letter state), `attempts`, `error`. The
  latter two timestamp columns are migration A38. A pending row has all three state timestamps NULL;
  uploaded and failed are explicit, mutually exclusive terminal states. Unique on `(org_id, action)`
  — the sole idempotency mechanism, not a check-then-insert. Durable by design (written synchronously
  in the firing code's transaction, unlike `audit.py`/`analytics.py`, which are droppable); a
  background worker uploads it later. Full chain and the one non-atomic fire site:
  [ads-conversions](ads-conversions.md).
- **`Ephemeral`** — short-lived key/value state that must **survive a restart and stay correct across
  instances**: the emailed OTP code + its brute-force counter, and the auth rate-limit sliding windows.
  Keyed by `(ns, k)` — a namespace (`otp` | `otp_start` | `sandbox_hit`) plus the key within it — with an
  opaque JSON `v` and an `expires_at` (rows are swept lazily). This is the DB home for what used to be
  per-process dicts in `api.py` (backlog #3): counters can no longer be reset by a redeploy, and a per-IP
  / per-email cap can't be weakened by running more than one instance. The access helpers live in
  `ratestore.py` (`kv_put`/`kv_get`/`kv_pop`, `rate_check` sliding-window, `sweep`). NOT the CLI-login
  handshake — that is deliberately still in-process (`api._cli_pending`, short-lived, self-heals on retry).

- **`DenyRule`** — org policy over what may be CALLED: `org_id`, nullable `user_id` (NULL = the whole
  org, set = one member/agent), nullable `project_id` (NULL = any tool, set = only calls **through**
  that project's tools — migration A22; `delete_project` sweeps the rules that named it, the same
  dangling-FK reasoning as `_drop_member_deny_rules`), `host` / `path_prefix` / `method` (an empty
  field means **any**, so a rule carrying only `method="DELETE"` blocks every delete), `verdict`,
  `note`, `created_by`. The table itself is new, so `create_all` makes it. `verdict` is `deny` today
  and exists so approval-required actions can land here later without a migration, mirroring the
  `verdict` vocabulary `localrun.py` already uses. Enforcement: [proxy-model](proxy-model.md).
- **Runtime attribution** — `CallRecord.client` + `RunRecord.client` (migration A23, `''` default):
  which coding agent made the call (`X-Treg-Client`, self-reported by the CLI via env fingerprints;
  attribution, never authentication). Feeds `GET /orgs/{id}/agents/observed` — one row per
  (member, runtime), the auto-captured half of the agents story. `Membership.created_by` (same
  step) names the admin who minted an agent; `''` for door/invite joins.

## Bindings (the multi-credential shape)
`Tool.bindings` is a JSON list; each entry is
`{secret_id, injector, location, name, format, secret_field}` — one credential injection. A request
applies **all** of a tool's bindings (e.g. google-ads = an oauth bearer + a `developer-token` header).
The API builds a single-binding tool from flat fields via `_flat_binding()`; injection is in
[auth-secrets](auth-secrets.md).

## Async DB (`db.py`)
One async SQLAlchemy engine (`_engine`) + a public `session_maker` (the audit writer opens its own
session here). `init_db()` creates tables **and runs the guarded orgs migration** (`_migrate_to_orgs` —
see [multi-tenancy](multi-tenancy.md)); that migration also does the small additive `ADD COLUMN` steps for
columns added after a table shipped (e.g. `tool.examples`, `tool.cli` for local runs, `org.public_demo`
(A15), the seven `secret` connection-metadata columns (A16), and the eight `pendingoauth` marketplace/quirk
columns (A17–A20) — guarded by a column-existence check, so it is idempotent on both SQLite and Postgres).
**Postgres BOOLEAN default fix:** boolean columns added here use `DEFAULT false`, never `DEFAULT 0` —
Postgres rejects an integer default on a `BOOLEAN` column (SQLite accepts both, so the test suite alone
cannot catch it), which is why `pendingoauth.long_lived_exchange` is spelled `BOOLEAN NOT NULL DEFAULT
false`. `reset_db()` is test-only (drop +
recreate); `get_session()` is the FastAPI dependency. SQLite locally (`aiosqlite`), Postgres on Render, same code. **Timestamps are
naive UTC:** `_now()` (the `created_at` default) drops tzinfo because the columns are `TIMESTAMP WITHOUT
TIME ZONE` and asyncpg rejects tz-aware values on Postgres; the app compares naive UTC throughout
(`api._utcnow_naive` / `_as_naive`).

## Audit writer (`audit.py`)
`record_call(**fields)` (a `CallRecord`, now including `org_id`) and `record_run(**fields)` (a
`RunRecord`) schedule an insert on their **own** session via `asyncio.create_task` so the response never
waits on it (fire-and-forget). Tasks are held in `_pending` against GC; failures are swallowed (an audit
hiccup must not break a call). `call_tool` records the **attempt** on its failure branches too (missing
secret / refresh / upstream), not just successes. **Back-pressure:** each write opens a connection from
the small pool shared with the request path, so a loop-bound semaphore caps concurrent audit writes at
`_MAX_CONCURRENT_WRITES`, and under an extreme burst `_schedule` **sheds** load — it drops any row past
`_MAX_PENDING` rather than grow unbounded — so best-effort logging can never starve real calls. `drain()`
**loops until quiescent** (a call finishing during shutdown enqueues a new task
after a one-shot snapshot would have gathered) on shutdown and in tests.

## Product analytics writer (`analytics.py`)
The same lossy discipline as `audit.py`, but the sink is PostHog's `/batch/` endpoint, not the DB.
`capture(distinct_id, event, properties, groups=)` is synchronous and **never raises** (call sites sit
inside the Stripe webhook, where an exception would 500 and trigger a retry of an already-credited
payment); it queues up to `_MAX_PENDING` events (drop-newest past the bound) and one flusher task
micro-batches them (`_BATCH_MAX` per POST, at most every `_FLUSH_INTERVAL_S`) via a per-flush httpx
client — no semaphore, because HTTP to PostHog never touches the DB pool. **Empty `posthog_key` = the
module is off** (self-hosters and the test suite send nothing). `$groups: {team: org_slug}` mirrors the
browser's `posthog.group('team', slug)` and `distinct_id` is the user email, so server events join the
same PostHog person/group the SPA identifies. Emitters: `call_tool`'s `_audit` funnel (`tool_called`,
with the catalog `provider` as vendor or the upstream host for own tools), `billing_topup`
(`topup_started`), and `billing._credit` (`topup_completed`, gated on `fresh`). Drained in the lifespan
`finally` after `audit.drain()`. The engine adds Postgres pool
hygiene (`pool_pre_ping`/`pool_recycle`/sizing) for non-SQLite URLs, and `init_db` refuses to start with
no `TREG_SECRET_KEY` on a real DB (an ephemeral key would lose every stored secret on restart).

> **Tenancy:** every resource noun carries `org_id`; access is scoped to the caller's org. Details:
> [multi-tenancy](multi-tenancy.md).

> **Tenant isolation shipped:** resources are scoped by `org_id` and a token = a `(user, org)` membership.
> See [multi-tenancy](multi-tenancy.md). `owner` (creator email) is retained for audit + the role gate.

## `CapabilityPin` — "for this job, our team uses this provider"

One row per `(org, capability)`. Deliberately **not** a `DenyRule`: a deny is negative and closed, so
blocking eight of nine providers leaves the ninth allowed the day a tenth joins the catalog. A pin is
positive and stays correct as the catalog grows.

It is a gate, not a hint — a catalog call to a different provider of a pinned capability is refused
in `_resolve_marketplace_call`, before anything is reserved, and the 403 carries `use_endpoint` so
the caller is told what to use instead rather than just "no". Both halves are validated against the
catalog when the pin is set, because a typo would otherwise block a job the team really uses and
surface at 3am in an agent's log. See [catalog](catalog.md) and `docs/CAPABILITY-CHOICE-PLAN.md`.

## OAuth (the MCP authorization server)

Four tables, all added with the MCP front door. See `architecture/mcp-oauth.md` for the reasoning.

| Table | Holds | Note |
|---|---|---|
| `OAuthClient` | a client that may ask for a token | one row shape for both DCR and CIMD, so authorize/consent/token never ask how it arrived |
| `OAuthCode` | a one-time authorization code | deleted on redemption, not flagged — a used code that still exists is a race |
| `OAuthGrant` | mutable authority for one refresh family | `current_org_id` is where future tokens spend; `granted_at` is the stable consent time |
| `OAuthRefresh` | a refresh token, **hashed** | `family_id` groups every descendant of one grant, so a replay can revoke all of them |

`OAuthCode` and `OAuthRefresh` are org-scoped and therefore listed in `_ORG_SCOPED_MODELS`; `OAuthGrant`
is cleared explicitly by `_cascade_delete_org` because its FK is intentionally named `current_org_id`.
The cascade revokes the union of families that name the deleted team through current authority or
any historical `OAuthRefresh.org_id`: deleting only a retired provenance row would erase the replay
evidence while leaving its live descendants usable. `OAuthClient` is not org-scoped — a client is
global, and nothing about it belongs to one team. Each `OAuthRefresh.org_id` is immutable issue
provenance; moving a family updates only `OAuthGrant.current_org_id`.

## Caller tags (`X-Treg-Meta`)

Two new tables and a handful of columns carry a reselling builder's attribution. The design rationale
lives in [money](money.md); this is the shape.

| Table | Row means | Written by |
|---|---|---|
| `TagSpend` | what one call cost, attributed to ONE of its tags | `ledger.py` only, in the money transaction |
| `TagBudget` | one builder-set limit on one `(dim, val)`, and the registry entry that bounds cardinality | `api.py` (auto-created on first sighting) |

`CallRecord` gains `call_ref` (the `X-Treg-Call-Id` echoed to the caller and used as the ledger's
`call_id` on a metered call — one value joins the audit row, the money rows and the builder's own
records), `budget_dim`/`budget_val` (the indexed copy of the primary pair) and `tags` (the whole bag).

> `audit.record_call` splats its `telemetry` dict as `**kwargs` into `CallRecord()`, and `audit._write`
> swallows every exception. **A telemetry key without a matching column used to silently kill every
> audit write** — the table went dark with no error anywhere. Fixed alongside migration A36:
> `_known_fields` filters telemetry against `CallRecord.model_fields`, so an unknown key now costs one
> column and logs a warning naming it, and the surviving swallow in `_write` logs instead of passing.
> Columns and telemetry keys should still land together — the guard makes a mismatch survivable and
> visible, not correct.

`Org` gains `budget_dims` (which keys may carry budgets, ≤3), `primary_dim` (the one that scopes
idempotency) and `daily_cap_micro` (the team's own spend ceiling, 0 = follow the deployment default).
`Membership` gains `pinned_tags`.

Migrations `A30`-`A32` in `db.py` add the columns, guarded as usual; `TagSpend` and `TagBudget` are new
tables and need no DDL. Note `A31` also required adding the two new NOT NULL `org` columns to the raw
`INSERT INTO org` in the legacy `(B)` backfill: a column `create_all` builds from a SQLModel default is
NOT NULL with **no server default**, so raw SQL must supply it (ops/deploy.md §migration portability).

## `Referral` — one invitation, and what it owes

Written by `referrals.py`; the money it results in is granted through `ledger.grant`. See
[money](money.md) for the policy and the gates. Two things about the SHAPE belong here:

**Two UNIQUE columns do the arbitration, not application code.** `referred_org_id` (an org can be
referred exactly once, ever) and `qualifying_payment_intent` (one payment funds one qualification).
`ledger.grant(once=True)` was not enough: its check is a SELECT with no backing unique index, which
survives a retry but not two concurrent redemptions — and this is money owed to a third party, not a
signup promo. NULL is exempt from a unique index, so any number of `pending` rows coexist.

**`status` is a ladder and every terminal state is kept**, never deleted:
`pending` (signed up, owes nothing) → `qualified` (friend paid, owes both bonuses after the hold) →
`paid`; or `capped` (referrer out of self-serve allowance) / `rejected` (a gate said no, or the
funding payment was reversed inside the hold — `reject_reason` says which). A deleted row cannot
answer "why did I not get paid", which is the first question this feature generates.

`User.referral_code` is on the USER, not the Org: a person refers a friend, and anyone may create
unlimited orgs, so a per-org code would hand the same human unlimited codes to farm with. It is
minted lazily on first visit to the Referrals page — NULL is the normal state.

`Referral.card_fingerprint` holds Stripe's stable per-card id. It is **not card data** (opaque
outside our own Stripe account) and lives here alone, never on `Org`, which keeps
`Org.stripe_default_pm`'s no-card-data posture intact.
