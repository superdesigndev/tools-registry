---
title: Running & deploying the server
status: shipped
sources:
  - src/treg/__main__.py
  - src/treg/web/selfhost.sh
  - src/treg/config.py
  - src/treg/db.py
  - src/treg/email.py
  - src/treg/audit.py
  - render.yaml
related:
  - architecture/data-model.md
  - architecture/ads-conversions.md
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
- `public_url` — default `https://treg.to`; the reference deployment is cut over in STAGES —
  render.yaml deliberately still sets the OLD domain so the migration code deploys inert, and the
  flip (then email, then client releases) each land as their own change. Both prod hostnames stay
  valid forever (`config.PUBLIC_HOST_ALIASES`); marketing pages 301 old→canonical only. Verify each
  phase with `scripts/smoke-domain.sh pre|post`. Self-hosters set
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
  (separate sandbox vs prod app), `meta_*` (ONE Meta app backs both facebook + instagram), and the
  Advertising OAuth platforms `microsoft_ads_*`, `snapchat_ads_*`, `tiktok_ads_*`, `pinterest_*` (all
  unset by default, so those providers ship **unconfigured** until a deployment registers a dev app).
  Empty for a provider ⇒ it lists as **unconfigured** rather than failing part-way through a consent.
- **Ad conversion tracking** — `google_ads_customer_id` (the target Ads account) and
  `ads_conv_refresh_token` (treg's OWN long-lived refresh token for the Data Manager uploader,
  minted once, out of band, by an operator via the OAuth playground with scope
  `https://www.googleapis.com/auth/datamanager`, then pasted in as a platform setting). **Both**
  must be set or the whole feature is off (`adsconv.enabled()`): the capture script is empty,
  attribution cookies are ignored, conversions are not queued, and the background uploader is not
  started. The refresh token is exchanged for an access token against the SAME client it was issued
  against — `google_ads_client_id`/`_secret` above, reused here rather than duplicated — never
  against `google_client_id` or a customer's own OAuth app; the exchanged access token is cached in
  process memory with its expiry so the uploader (which drains every ~300s) doesn't re-exchange on
  every pass. `google_ads_developer_token` is NOT part of this feature — Data Manager has no
  developer-token header at all; that setting only backs the read-side Ads catalog calls through
  `oauth_providers.GOOGLE_ADS`. For manager-account auth, set optional
  `google_ads_login_customer_id` to the manager MCC id; direct client auth leaves it empty. This is
  a PLATFORM credential, deliberately separate from a customer's `google-ads` OAuth connection
  (which grants only `adwords`, never `datamanager` — that scope would otherwise show up on every
  customer's consent screen for a permission only treg's own marketing uploader uses). Self-hosters
  and the test suite carry zero ad-conversion machinery by default. See
  [ads-conversions](../architecture/ads-conversions.md).
- **Landing live-wire (optional):** `demo_stripe_key` (`TREG_DEMO_STRIPE_KEY`, a Stripe **sandbox
  restricted** key) powers the landing sandbox's ONE real upstream call — a sandbox call to the exact
  seeded `stripe` tool relays for real with this key injected; the key exists in no sandbox org. Empty ⇒
  every sandbox call synthesizes, exactly as before the wire existed. `demo_stripe_webhook_secret`
  (`TREG_DEMO_STRIPE_WEBHOOK_SECRET`, `whsec_…`) signs the landing payments feed; empty ⇒ `POST
  /stripe/webhook` is off (`404`, so a deploy without it exposes no unauthenticated POST surface). See
  [api](../interface/api.md).
- **Frictionless local mode** (`single_user`, `single_user_token_file`): `curl {BASE}/selfhost.sh | sh`
  brings up a registry on the caller's own machine that they are **already signed into** — no account,
  email or password. `lifespan` calls `_bootstrap_single_user()`, which idempotently creates the
  `you@local.treg` owner + `personal` team and writes the token (0600) for the installer to hand to the
  CLI; the token is **stable across restarts** (re-minted only if the file is deleted), and `dashboard()`
  attaches a session when there is none. It adopts an org **only through a membership this identity
  already has** — never by looking one up by the slug `personal`. On a database that is not fresh (a
  restored dump, a hosted registry run locally) that lookup joined a team belonging to someone else **as
  owner**, and an owner is exempt from every ACL (round-4 finding #5). A new team therefore takes a free
  slug via `_unique_slug` (`personal-2`, …) instead of colliding; a fresh box still gets the clean name.
  Gated by **`single_user_ok`**, which mirrors `expose_dev_code`:
  it demands a **local sqlite** DB **and** a **loopback `public_url`**, so a stray `TREG_SINGLE_USER=true`
  on a real deploy does nothing. A no-login dashboard on a public host would hand over the whole registry.
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
- `intercom_app_id` / `intercom_secret` (`TREG_INTERCOM_APP_ID` / `TREG_INTERCOM_SECRET`) — support
  chat via the **Intercom Messenger** (treg's own workspace). Empty app_id = the widget is OFF
  everywhere — `/meta`
  serves `""` and every page's loader stays inert, so self-hosters ship no third-party chat. The
  app_id is public; the secret signs `user_hash` (identity verification) and never reaches the browser.
- `resend_api_key` / `email_from` — transactional email via **Resend** (`src/treg/email.py`): the OTP
  sign-in code + team invitations. Empty key = no real send (dev mode still returns the code; prod
  without a key silently skips — best-effort, never breaks the flow). `email_from` **must** be a
  Resend-verified domain — `treg.to` is **verified** (DKIM + SPF records on the treg.to zone;
  `treg.superdesign.dev` stays verified as a fallback), so the default is `no-reply@treg.to`. **On Render:** set
  `TREG_RESEND_API_KEY`, optionally `TREG_EMAIL_FROM`, and leave `TREG_EMAIL_DEV_MODE` false.

## Web dashboard
`GET /` serves the single-file dashboard (`src/treg/web/index.html`) same-origin; the whole `web/` dir
(incl. `tutorial.js` at `/tutorial.js` and `tutorial.html` at `/tutorial`) ships in the wheel because it
lives inside the `treg` package (the `packages` inclusion covers non-.py assets). See
[dashboard](../interface/dashboard.md).

## Current hosting (shipped)
Deployed on **Render** at `https://treg.to` (with `treg.superdesign.dev` attached as the legacy
alias — never remove it: installed CLIs/skills point there with tokens) via the Blueprint below (one web service + a
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
callback exchanges the code exactly as the consent URL was built; and **A35** backfills one
`oauthgrant` authority row per existing refresh family from its oldest token, using portable,
idempotent `INSERT … SELECT … WHERE NOT EXISTS` SQL. Because a rolling deploy keeps an old binary
alive after that snapshot, API `_ensure_grant` also reconstructs any later old-binary family at first
refresh, listing, or team move with an `ON CONFLICT DO NOTHING` upsert supported by SQLite and
Postgres; the oldest token's `created_at` remains the consent time; **A36** adds nullable
`callrecord.error_request`/`error_response` evidence; **A37** adds nullable Ads attribution and
`first_call_at` columns to `org`; and **A38** adds nullable retry/dead-letter timestamps to the Ads
conversion outbox. The A37/A38 timestamps use portable `TIMESTAMP` DDL and require no backfill.

**Audit back-pressure (`audit.py`).** Audit rows are written off the request path (fire-and-forget), and
each write opens a DB connection from the small pool **shared** with real requests. Two limits keep
best-effort logging from starving that pool: a loop-bound semaphore caps concurrent audit writes at
`_MAX_CONCURRENT_WRITES`, and under an extreme burst the writer **sheds** load — it drops any audit row
past `_MAX_PENDING` rather than let the pending set grow without bound. Audit must never OOM or wedge the
server. Shedding is the *only* loss that should ever happen: `record_call` splats its telemetry dict
into `CallRecord(**fields)`, so a key with no matching column used to raise inside `_write`, where the
except swallowed it, and the whole row disappeared — a telemetry field deployed one commit ahead of its
migration would have silently emptied the table. `_known_fields` now drops unknown keys (logging which
ones), and `_write`'s swallow logs a warning with the traceback. **A quiet audit table is now a bug you
can see in the logs**, not one you find out about weeks later.

The proxy is thin and IO-bound (a relay, low CPU/memory), so cheap machines scale it.

## The per-org daily spend cap

`TREG_PLATFORM_DAILY_CAP_USD` (default **$100**) is the per-org, per-UTC-day ceiling on tier-4 spend
*and* the ceiling a team may raise its own `Org.daily_cap_micro` to. The effective cap is the lower of
the two (`api._effective_daily_cap`).

It was $5, which is a sane blast radius for one team and fatal for a platform running its whole
customer base through a single org — they hit it on day one. Raising it per-team is now a `PATCH
/orgs/{id}/settings` a team can make itself up to the ceiling, so onboarding a high-volume builder is
a conversation rather than an env-var edit that lifts the rail for every team at once.

It is a **blast-radius limit**, not a billing control: it exists because auto-top-up refills the
balance, so the balance alone is not a ceiling against a runaway agent or a mispriced catalog entry.
Enforced fail-closed.

## X pay-per-use billing (switch ON since 2026-08-18)

`TREG_OAUTH_BILLED_PROVIDERS` — comma-separated providers whose upstream bill lands on TREG's
developer app (X moved to pay-per-use: the app owner pays per call, whoever's token made it). A
provider named here has its registry-connect calls metered against the org balance, `tier: oauth`
in the ledger. Empty = those calls are free (the pre-2026-08-18 behaviour). Currently `x`.
BYO-app connections are never metered. Ongoing spend is visible in the reconcile reports under
`tier: oauth`; the burn from the free period is only in console.x.com.

## Market data platform keys (2026-08-16)

Five more `TREG_PLATFORM_KEY_*` env vars beside the originals, and the providers must ALSO be in
`TREG_PLATFORM_PROVIDERS` (both halves, or tier 4 refuses):

- `TREG_PLATFORM_KEY_COINGECKO` — PRO key; billed $0.00029/credit
- `TREG_PLATFORM_KEY_MARKETSTACK` — billed $0.000999/call against a 10,000/mo vendor cap
- `TREG_PLATFORM_KEY_FINNHUB`, `_TWELVEDATA`, `_TIINGO` — FREE-tier keys serving $0 trial pools,
  capped per team per day from fx.yaml (`treg_trial`). These are free accounts: if one is
  terminated, the pool dies gracefully (calls refuse, nothing bills) — replace the key or demote
  the provider to own-key-only by removing it from the allow-list.

## Enrichment platform keys (2026-08-20)

Seven more slots (`TREG_PLATFORM_KEY_COMPANYENRICH`, `_OCEANIO`, `_PREDICTLEADS` — base64 of
`api_key:api_token` —, `_FINDYMAIL`, `_BRANDDEV`, `_ICYPEAS`, `_LEADSFORGE`), all UNFUNDED at merge:
declared in `render.yaml` and `config.py` so tier 4 can be turned on per provider by funding the
account, setting the env var, and adding the service to `TREG_PLATFORM_PROVIDERS`. Ocean.io stays
refused even with a key until `fx.yaml` gets a real `usd` rate (its plan price is not machine-readable).
Tomba has NO slot on purpose: its data routes need a key+secret header pair and the platform-binding
path injects one value — wire a paired platform binding before offering tomba on tier 4.

## Creator-data platform key (2026-08-21)

`TREG_PLATFORM_KEY_INFLUENCERSCLUB` — the dashboard API key (a JWT), sent as `Authorization: Bearer`.
Declared in `config.py` and `render.yaml`; the catalog's 12 priced routes are platform-eligible
(`fx.yaml` $0.598/credit = our own $299/mo-for-500-credits plan, bought 2026-08-21; the public
page's "as low as $0.23" is the top of the volume slider, not what we pay. Every per-route credit
count was observed live). The account is FUNDED: set the env var and add `influencersclub` to
`TREG_PLATFORM_PROVIDERS`. Mind the 60s gateway 504 on cold enrichment calls: under `per_success`
settlement a 504 relays as a failure and settles at 0, but the vendor charged two of ours — a small,
bounded leak on the 0.03 tier, worth watching in the first reconcile report.

## A db.py change needs a Postgres-shaped deploy plan

SQLite cannot catch this class: it has no connection pool and no lock queue. Two rules, both from the
2026-08-15 outage (an ALTER on `callrecord` queued behind live traffic, every new query queued behind
the ALTER, both instances starved, and the shared Postgres stayed wedged until a database restart):

- Startup migrations run with `lock_timeout = 5s` (set in `init_db`, postgres only). A contended
  deploy therefore FAILS CLEANLY — prod keeps serving the old code — and the right response is to
  redeploy at a quieter moment, not to raise the timeout.
- The pool is per instance and a rolling deploy runs two: keep `pool_size + max_overflow` such that
  DOUBLE it stays under the database plan's connection ceiling. A guard test pins this.

If a deploy fails with a lock timeout in the logs, that is the mechanism working. If the database
itself stops accepting connections, restart the POSTGRES resource, not the web service — an app
restart cannot release server-side slots (learned the hard way).
