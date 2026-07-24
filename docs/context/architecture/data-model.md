---
title: Data model — the registry tables, async DB, audit writer
status: shipped
sources:
  - src/treg/models.py
  - src/treg/db.py
  - src/treg/audit.py
  - src/treg/ratestore.py
related:
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
---

# Data model

SQLModel tables in `src/treg/models.py`. Kept minimal on purpose. Org multi-tenancy adds `Org`,
`Membership`, `Invite` and an `org_id` on the resource nouns — the tenancy mechanics live in
[multi-tenancy](multi-tenancy.md); this fragment is the table reference.

- **`Org`** — the tenant that owns resources: `id, name, slug` (unique), `suspended` (admin lock),
  `demo` (a sandbox team seeded by [onboarding](../interface/onboarding.md) — labeled + removable),
  `public_demo` (a team whose member token is PUBLISHED, e.g. on the landing page — non-admin members
  are locked to `/call` + reads and may never act as a user; gated in `api.require_member` /
  `require_identity`), `created_at`.
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
  `status_code`, `kind` (**`call`** = proxy `/call`, **`local_run`** = `/tools/{name}/grant`), `created_at`.
- **`RunRecord`** — the **server-side run** audit row (a `treg run --server` CLI execution — the "kind"
  `server_run` in usage rollups): `org_id`, `user_email`, `bundle_name` (holds the **tool** name since the
  tool-side run unification; column name is historical), `argv` (JSON — never carries a secret value;
  secrets are injected via env, not the command line), `exit_code`, `duration_ms`, `created_at`. Written
  off the request path like `CallRecord`. **Usage metering** (`GET /orgs/{id}/usage`, per-user daily caps)
  counts `CallRecord` + `RunRecord` together — see [the API fragment](../interface/api.md).
- **`Ephemeral`** — short-lived key/value state that must **survive a restart and stay correct across
  instances**: the emailed OTP code + its brute-force counter, and the auth rate-limit sliding windows.
  Keyed by `(ns, k)` — a namespace (`otp` | `otp_start` | `sandbox_hit`) plus the key within it — with an
  opaque JSON `v` and an `expires_at` (rows are swept lazily). This is the DB home for what used to be
  per-process dicts in `api.py` (backlog #3): counters can no longer be reset by a redeploy, and a per-IP
  / per-email cap can't be weakened by running more than one instance. The access helpers live in
  `ratestore.py` (`kv_put`/`kv_get`/`kv_pop`, `rate_check` sliding-window, `sweep`). NOT the CLI-login
  handshake — that is deliberately still in-process (`api._cli_pending`, short-lived, self-heals on retry).

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
after a one-shot snapshot would have gathered) on shutdown and in tests. The engine adds Postgres pool
hygiene (`pool_pre_ping`/`pool_recycle`/sizing) for non-SQLite URLs, and `init_db` refuses to start with
no `TREG_SECRET_KEY` on a real DB (an ephemeral key would lose every stored secret on restart).

> **Tenancy:** every resource noun carries `org_id`; access is scoped to the caller's org. Details:
> [multi-tenancy](multi-tenancy.md).

> **Tenant isolation shipped:** resources are scoped by `org_id` and a token = a `(user, org)` membership.
> See [multi-tenancy](multi-tenancy.md). `owner` (creator email) is retained for audit + the role gate.
