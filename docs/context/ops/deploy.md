---
title: Running & deploying the server
status: shipped
sources:
  - src/treg/__main__.py
  - src/treg/config.py
  - src/treg/db.py
  - src/treg/email.py
  - src/treg/audit.py
  - render.yaml
related:
  - architecture/data-model.md
  - foundation/charter.md
---

# Running & deploying

## Entry point (`__main__.py`)
`python -m treg` → `main()` → `uvicorn.run("treg.api:app", host="0.0.0.0", port=int($PORT or 18790))`
(`--reload` optional). It honors `$PORT` (Render/Heroku route + health-check that port). `python -m treg
keygen` prints a Fernet key for `TREG_SECRET_KEY`.

## Startup safety (`db.py init_db`)
- **Fails loud on a missing key + real DB:** if `TREG_SECRET_KEY` is empty and `database_url` isn't
  SQLite, `init_db` raises (an ephemeral key would make every stored secret undecryptable after a
  restart — silent total loss). On SQLite dev it only logs a warning.
- **Postgres pool hygiene:** for non-SQLite URLs the async engine adds `pool_pre_ping=True`,
  `pool_recycle=300`, and sizing (`pool_size=20`, `max_overflow=40`) — avoids post-idle dropped-connection
  500s and pool starvation against the relay's 200-concurrency client.

## Config (`config.py`)
`Settings` (pydantic-settings, env prefix `TREG_`, reads `.env`), cached via `get_settings()`:
- `database_url` — default `sqlite+aiosqlite:///./treg.db` (SQLite dev, Postgres on Render, same code).
  A `field_validator` rewrites a bare `postgres://` / `postgresql://` URL → `postgresql+asyncpg://`, so
  Render's `fromDatabase`-injected URL works unedited (the async engine needs the asyncpg driver).
- `secret_key` — the Fernet key; empty → an ephemeral key is minted at startup (secrets won't survive a
  restart). See [auth-secrets](../architecture/auth-secrets.md).
- `public_url` — default `https://treg.superdesign.dev` (the reference deployment); self-hosters set
  `TREG_PUBLIC_URL`. Used to build the OAuth callback URI.
- `api_token` — a bootstrap caller token (MVP leftover; per-user tokens are the real auth).
- `admin_token` — the cross-tenant **super-admin** bearer (`TREG_ADMIN_TOKEN`); empty disables the env
  path (only `is_superadmin` users reach `/admin`). Keep it long + secret. See
  [super-admin](../architecture/super-admin.md).
- **Registry OAuth-marketplace apps** — treg's OWN approved OAuth clients, so a member can connect a
  provider without registering an app themselves. `google_client_id`/`_secret` backs both Google login
  AND the Google registry connects (Search Console / Analytics / Business Profile) via `/oauth/callback`
  — register both redirect URIs. Google **Ads** is special: `google_ads_client_id`/`_secret` is a
  DEDICATED client in its own Cloud project (a developer token is welded to one project), plus
  `google_ads_developer_token` (treg's token from OUR approved manager account, injected on every Ads
  call as a **platform binding** — see [proxy-model](../architecture/proxy-model.md)). The other
  providers each take a `<name>_client_id`/`_secret` pair: `linkedin_*`, `slack_*`, `x_*`, `tiktok_*`
  (separate sandbox vs prod app), and `meta_*` (ONE Meta app backs both facebook + instagram). Empty for
  a provider ⇒ it lists as **unconfigured** rather than failing part-way through a consent.
- **Landing live-wire (optional):** `demo_stripe_key` (`TREG_DEMO_STRIPE_KEY`, a Stripe **sandbox
  restricted** key) powers the landing sandbox's ONE real upstream call — a sandbox call to the exact
  seeded `stripe` tool relays for real with this key injected; the key exists in no sandbox org. Empty ⇒
  every sandbox call synthesizes, exactly as before the wire existed. `demo_stripe_webhook_secret`
  (`TREG_DEMO_STRIPE_WEBHOOK_SECRET`, `whsec_…`) signs the landing payments feed; empty ⇒ `POST
  /stripe/webhook` is off (`404`, so a deploy without it exposes no unauthenticated POST surface). See
  [api](../interface/api.md).
- `github_client_id` / `github_client_secret` / `session_secret` — GitHub OAuth login for the dashboard
  (`TREG_GITHUB_*`, `TREG_SESSION_SECRET`); empty hides the GitHub button. Callback must be
  `<public_url>/auth/github/callback`. See [dashboard](../interface/dashboard.md).
- `email_dev_mode` — default **False** (returning the OTP in the response is an unauth account-takeover
  vector in prod). When true, `/auth/email/start` returns + logs the 6-digit code so dummy emails are
  testable without a mail sender; when false, the code is **emailed via Resend** (see below). Enable it
  **only** on a trusted dev box (`TREG_EMAIL_DEV_MODE=true`); real deploys must not. **Double guard:** the
  code is exposed only through `Settings.expose_dev_code`, which requires `email_dev_mode` **and** a
  **local sqlite** `database_url` — so even a stray `TREG_EMAIL_DEV_MODE=true` on Postgres (a real deploy)
  can never leak a login code.
- `run_proof` (`TREG_RUN_PROOF`) — the **isolated-runner proof** for `treg run --local`. A local run whose
  grant would return a secret the caller does **not** own (a shared-key tool a member may run but not read)
  must present this value in the `X-Treg-Run-Proof` header — a value held **only** by the root-installed
  `treg-run` runner, never by the member. Empty = shared-key local runs are refused (runs against a
  secret the caller owns still work). To enable shared local runs, set it on the server **and** install it
  via `treg setup-local-run --run-proof`. See [local-run](../architecture/local-run.md).
- `run_allowed_bins` (`TREG_RUN_ALLOWED_BINS`) — the **command allow-list** for `treg run --server`. The
  server executes an entrypoint only if it is a catalog-known CLI (stripe/gh/vercel/…) **or** named in this
  comma-separated list — so a member cannot ask the server to run `bash`/`python` and execute arbitrary
  code as the server user. Extend it as new CLIs are approved.
- `run_rlimits` (`TREG_RUN_RLIMITS`, default **true**) + `run_cpu_seconds` (`TREG_RUN_CPU_SECONDS`,
  default 300) + `run_fsize_mb` (`TREG_RUN_FSIZE_MB`, default 100) — the **resource-limit sandbox** for
  `treg run --server` (`runner._rlimit_preexec`): every run's child gets a CPU-seconds cap, a max-file-size
  cap, and core dumps disabled, so a runaway/hostile CLI can't exhaust the host. A no-op where the POSIX
  `resource` module is unavailable. No address-space/process-count cap (would break Go CLIs / is per-uid).
  This is the **DoS** half of the sandbox; full filesystem/network isolation needs a **container deploy**
  (a planned follow-up — the current Render runtime is the native Python one, which can't run it).
- `proxy_ssrf_check` (`TREG_PROXY_SSRF_CHECK`) — the **call-time SSRF guard** on the proxy: resolve the
  upstream host and refuse an internal/private target. **On by default**; only the test suite disables it
  (its upstream is an in-process ASGI transport, not real DNS).
- `resend_api_key` / `email_from` — transactional email via **Resend** (`src/treg/email.py`): the OTP
  sign-in code + team invitations. Empty key = no real send (dev mode still returns the code; prod
  without a key silently skips — best-effort, never breaks the flow). `email_from` **must** be a
  Resend-verified domain — `treg.superdesign.dev` is **verified** (DKIM `resend._domainkey.treg`, SPF
  MX+TXT on `send.treg`), so the default is `no-reply@treg.superdesign.dev`. **On Render:** set
  `TREG_RESEND_API_KEY`, optionally `TREG_EMAIL_FROM`, and leave `TREG_EMAIL_DEV_MODE` false.

## Web dashboard
`GET /` serves the single-file dashboard (`src/treg/web/index.html`) same-origin; the whole `web/` dir
(incl. `tutorial.js` at `/tutorial.js` and `tutorial.html` at `/tutorial`) ships in the wheel because it
lives inside the `treg` package (the `packages` inclusion covers non-.py assets). See
[dashboard](../interface/dashboard.md).

## Current hosting (shipped)
Deployed on **Render** at `https://treg.superdesign.dev` via the Blueprint below (one web service + a
managed Postgres). The Fernet key lives only in the service's environment — **back it up**; losing it
makes every stored secret unrecoverable. For local dev, `scripts/dev-local.sh up` runs the server with
its own sqlite DB and email dev mode.

## Render (Blueprint)
`render.yaml` at the repo root deploys the whole thing as **one web service + a managed Postgres**
(region `oregon`): `buildCommand: pip install ".[server]"` — the base install is the **CLI only**, so the
server deploy needs the `[server]` extra (FastAPI/DB/crypto); the wheel ships every web asset via the package.
`startCommand: python -m treg`, health check on `/meta`. The DB URL is auto-wired via `fromDatabase`
(config's validator adds the asyncpg driver). Secrets are **dashboard-managed** (`sync: false` — the
Fernet key, session/admin tokens, GitHub OAuth pair, Resend key, and the optional landing live-wire pair
`TREG_DEMO_STRIPE_KEY` + `TREG_DEMO_STRIPE_WEBHOOK_SECRET`); `TREG_PUBLIC_URL`,
`TREG_EMAIL_DEV_MODE=false`, and `TREG_EMAIL_FROM` are set inline. `asyncpg` is a dependency (Postgres
async driver, alongside `aiosqlite`).

**Fresh-Postgres verified:** `init_db`'s `create_all` + the guarded `_migrate_to_orgs` no-op cleanly on
a fresh Postgres (all tables/columns present, idempotent on re-run) — the migration's SQLite-flavoured
raw SQL only fires on a legacy/missing-column DB. **Timestamps must be naive UTC:** the datetime columns
are `TIMESTAMP WITHOUT TIME ZONE`, and asyncpg rejects tz-aware values, so `models._now()` returns naive
UTC (SQLite is lax and hid this; it only bites on Postgres — the deploy target).

**Migration portability (Postgres-safe additive columns).** The additive `ALTER TABLE … ADD COLUMN`
steps in `_migrate_to_orgs` run on **every** startup and are idempotent (guarded by a column-existence
check), so they must be written in SQL that both SQLite and Postgres accept. The rules the code follows:
use `TIMESTAMP` (not `DATETIME` — Postgres has no `DATETIME` type); declare booleans as
`BOOLEAN … DEFAULT false` (not `DEFAULT 0` — Postgres rejects an integer default on a boolean column);
and write boolean literals as `true` / `false` (not `0` / `1`) in any `INSERT`. SQLite accepts all of
these too, so the same statements work on both databases. `_ensure_bool_col` centralizes the boolean case.
Also **quote a reserved-word table name**: the `token_version` step is `ALTER TABLE "user" ADD COLUMN …`
(`user` is reserved in Postgres, where this ALTER runs in-place on the live DB — an existing table isn't
touched by `create_all`, only by the migration). The usage-metering columns (A10 `membership.daily_call_cap
INTEGER DEFAULT -1`, A11 `callrecord.kind VARCHAR DEFAULT 'call'`) follow the same rules but need no
quoting (neither table name is reserved); the legacy owner-Membership backfill `INSERT` supplies
`daily_call_cap` explicitly, since a `create_all` column is NOT NULL with no server default. The later
additive steps follow the same rules: **A15** `org.public_demo BOOLEAN` (via `_ensure_bool_col`, the
publishable call-only token; the legacy-org backfill `INSERT` now lists it explicitly); **A16** the
connection metadata on `secret` (`provider`, `granted_scopes`, `resource_ref`, `resource_name`,
`expires_at`/`last_refresh_at TIMESTAMP`, `last_error`) so the OAuth marketplace can attribute, scope,
and expire a credential; **A17–A20** the per-provider auth quirks on `pendingoauth` carried through the
redirect (`provider`, `code_verifier`, `auth_params`, `token_endpoint_auth_method`, `client_id_param`,
`scope_separator`, `long_lived_exchange BOOLEAN DEFAULT false`, `replaces_secret_id INTEGER`) so the
callback exchanges the code exactly as the consent URL was built.

**Audit back-pressure (`audit.py`).** Audit rows are written off the request path (fire-and-forget), and
each write opens a DB connection from the small pool **shared** with real requests. Two limits keep
best-effort logging from starving that pool: a loop-bound semaphore caps concurrent audit writes at
`_MAX_CONCURRENT_WRITES`, and under an extreme burst the writer **sheds** load — it drops any audit row
past `_MAX_PENDING` rather than let the pending set grow without bound. Audit must never OOM or wedge the
server.

The proxy is thin and IO-bound (a relay, low CPU/memory), so cheap machines scale it.
