---
title: The API — the only brain (FastAPI)
status: shipped
sources:
  - src/treg/api.py
  - src/treg/email.py
  - src/treg/runner.py
  - src/treg/ratestore.py
related:
  - interface/cli.md
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
---

# The API

FastAPI `app` in `src/treg/api.py`. Everything the CLI + skill do is one HTTP call over this. `lifespan`
runs `init_db()` and creates the shared keepalive `httpx.AsyncClient` at `app.state.http` (and
`audit.drain()`s on shutdown).

## WAF escape hatch — `X-Treg-Body-Encoding`
Some hosting edges (Cloudflare, including Render's) 403 any request whose **body** matches an
injection signature — a skill recipe or a proxied `call` that legitimately carries SQL/HTML. The
pure-ASGI `_BodyDecodeMiddleware` (registered via `app.add_middleware`) lets a client smuggle such a
body past the edge: send it base64/gzip-encoded and set `X-Treg-Body-Encoding: base64` (or
`gzip`, or `base64+gzip`). The middleware calls `_decode_request_body()` to restore the real bytes,
fixes `content-length`, and hands the decoded body to routing — so both the Pydantic JSON endpoints
(e.g. `POST /skills`) and the `/call` proxy (which relays `request.body()` upstream) see plaintext. A
malformed encoded body is a clean 400. No header ⇒ untouched. The CLI's `_RegistryClient` uses this
automatically on a WAF 403 (see [cli](cli.md)).

## Auth
`require_member()` reads the `X-Treg-Token` header, hashes it (`crypto.hash_token`), looks up the
`Membership` by `token_hash`, and returns a `Caller` (`membership, user, org` + `org_id`/`email`/`role`);
401 on missing/invalid. Every scoped endpoint depends on it **except** `POST /users` + `POST
/invites/accept` (open, self-registering) and `GET /oauth/callback` (browser-hit, protected by `state`).
Authz = org scoping + a role gate: `_can_manage` lets admin/owner manage any org resource, a member only
what they created; `_require_admin_of` gates the org-admin endpoints. See
[multi-tenancy](../architecture/multi-tenancy.md).

## Endpoints
- **Users / orgs:** `register_user` (`POST /users`, open, legacy — used by the test fixture) creates the
  user + an org + owner membership and returns a token **once**; the dashboard/CLI login doors do NOT go
  through it (they create the user only, no auto org). `create_org` (`POST /orgs`, `require_identity` so a
  zero-org user can make their first team) + `list_orgs` (`GET /orgs`,
  each org carries a `tool_count` — one grouped query — so the dashboard can land on the org with tools);
  invites via `create_invite` (`POST /orgs/{id}/invites`, admin+) → one-time code (**emailed** via
  `email.send_invite`, best-effort, along with a separate inbox-only `email_token` sign-in link — the
  token is never in the JSON response; see the invite sign-in link below), `accept_invite`
  (`POST /invites/accept`, open) → registers/joins + mints a token. **Code-free invites:** an invite is
  addressed to an email, so `my_invites` (`GET /invites/mine`, `require_identity`) lists every pending
  invite for the caller's proven email and `accept_my_invite` (`POST /invites/{id}/accept`,
  `require_identity`) accepts one with no code (403 if the invite's email ≠ yours). `list_members` /
  `remove_member`
  (`GET`/`DELETE /orgs/{id}/members[/{user}]`, admin+); `set_member_role` (`PATCH …/members/{user}`,
  owner-only), `leave_org` (`POST /orgs/{id}/leave`), `delete_org` (`DELETE /orgs/{id}`, owner-only — via
  `_cascade_delete_org`, which now also sweeps each org's `RunRecord` rows);
  `list_invites` / `revoke_invite` (`GET`/`DELETE /orgs/{id}/invites[/{id}]`, admin+). Full behavior:
  [multi-tenancy](../architecture/multi-tenancy.md).
- **Usage metering + caps** (usage-metering v1, `docs/USAGE-METERING-PLAN.md`): `org_usage`
  (`GET /orgs/{id}/usage?days=`, admin+) rolls up `CallRecord` + `RunRecord` since the window start into
  **by-user** (with a `call`/`local_run`/`server_run` split), **by-tool**, **by-day**, and totals — pure
  `GROUP BY`, **no request/response bodies** (they aren't stored). `set_member_cap`
  (`PATCH /orgs/{id}/members/{user}/cap`, admin+) sets `Membership.daily_call_cap` (`-1` = unlimited,
  rejects `< -1`). `my_usage` (`GET /usage/me`, any member) returns the caller's own `used_today` + `cap`.
  `list_members` also returns each member's `daily_call_cap` + `used_today`. **Enforcement:**
  `_enforce_daily_cap` runs at the top of `call_tool`, `run_tool_server`, and `grant_local_run` (so no
  path dodges the cap); `count_today` = today's `CallRecord` + `RunRecord` for the user. `-1` (default)
  skips the count entirely (zero overhead); the sandbox is exempt. **Soft by design** — it counts the
  best-effort `CallRecord`, so under load it fails *open*, never closed.
- **Super-admin (cross-tenant, `require_superadmin`):** `/admin/stats|orgs|orgs/{id}|users|tools|calls|
  health` (reads) + `/admin/users/{id}/superadmin|suspend`, `DELETE /admin/users/{id}`,
  `/admin/orgs/{id}/suspend`, `DELETE /admin/orgs/{id}` (Phase-2). See
  [super-admin](../architecture/super-admin.md).
- **Secrets:** `create_secret` / `list_secrets` / `update_secret` (re-encrypts on value change) /
  `delete_secret` (409 if a tool binding references it). Values never returned (`_secret_view`).
- **Tools:** `create_tool` (bindings via `body.bindings`, or `_flat_binding(body)` sugar, or `[]`;
  validated by `_validate_bindings`; `host` derived by `_host_of`; optional `examples`; optional `cli`
  local-run profile validated by `_validate_cli_profile`), `list_tools`, `update_tool` (re-derives host on
  base_url change; `cli` set/clear here — this is how the local-run toggle flips `cli.enabled`),
  `delete_tool`. View via `_tool_view` (now includes `cli`). `delete_secret` refuses a secret referenced by
  a tool binding **or** a `cli.inject` entry.
  - **Owner-only binding.** `_validate_bindings` (HTTP bindings) and `_validate_cli_secrets` (local-run
    `cli.inject` entries) require the caller to **own** every secret they bind/inject, via
    `_require_secret_ownership`; only an **admin/owner** may wire up a shared-key tool with a teammate's
    secret. This stops a member laundering another member's key into a tool they control and then
    extracting it (through the proxy's `base_url` or a `/grant`). `update_tool` **grandfathers** the
    secrets already on the tool (it passes their ids as a `grandfather` set) — only a **newly-added**
    binding/inject is ownership-checked, so re-saving a tool an admin wired with a shared key doesn't lock
    its owner out on edit. The skill/folder importer runs the same checks.
  - **No SSRF at registration.** `_require_public_base_url` (reusing `health.safe_webhook_url`) rejects a
    `base_url` pointing at loopback / private / link-local / cloud-metadata hosts — including numeric IP
    encodings (decimal/hex/octal/short forms) — on `create_tool`, `update_tool`, and each imported skill
    tool, so a member can't turn `treg call` into a request to an internal address. The proxy also
    **re-resolves** the host at call time (`health.host_is_public`) to defeat DNS rebinding — see
    [proxy-model](../architecture/proxy-model.md).
- **Local runs (`treg run --local`, see [local-run](../architecture/local-run.md)):** `grant_local_run`
  (`POST /tools/{name}/grant`) is the one audited, owner-opt-in exception to "values are never returned" —
  member+ only (a viewer may call but not extract a value). It matches the catalog profile
  (`providers.match_skill`), server-side deny-checks the argv, renders the credential (oauth → leaf only),
  and writes a synchronous `GRANT`/`DENY` audit row (argv redacted of key-shaped tokens by `_redact_argv`).
  **Runner-proof gate:** returning a secret the caller does **not** own (a shared-key tool they may run but
  not read) requires the header `X-Treg-Run-Proof` to equal `TREG_RUN_PROOF` — the value held only by the
  isolated `treg-run` runner, which the member's own uid can't read. A caller who owns the injected secret
  (or is an admin) skips the gate. On refusal a `DENY` audit row is written and the grant is 403'd, so a
  direct member call can never read a teammate's key value. The grant response also carries
  **`redact_output`** (true exactly when the caller doesn't own the key, i.e. the runner-proof case) — the
  client then scrubs the injected value from the CLI's output (see [local-run](../architecture/local-run.md)).
  `report_local_run` (`POST /tools/{name}/run-report`) takes the client's verdict enum (never raw output);
  `credential_invalid` marks the injected secret(s) invalid via the health fields, skipping `param` kind.
- **Server runs (`treg run --server`, Tier 0):** `POST /run` runs a **runnable bundle's** CLI on the
  server via `runner.run_bundle` (secrets injected into a scrubbed child env, per-run temp `$HOME`, argv
  array — no shell), returns `{stdout, stderr, exit_code, timed_out}` and writes a `RunRecord`. `GET /runs`
  (`list_runs`) is now a **unified** execution log: it merges server `RunRecord`s with local-run `GRANT`
  `CallRecord`s, each tagged `where` (`server`|`local`), ids prefixed `s`/`l`, newest first (a local
  success has a null `exit_code`, since only failures report back). Bundle run-metadata (`runtime`/`package`/`entrypoint`/`runnable`) is set via
  `PATCH /bundles/{id}` (CLI `skill runtime`). **Command allow-list:** the bundle's exec command
  (`entrypoint`/`package`/name) must be a **catalog-known CLI** or an admin-listed one in
  `TREG_RUN_ALLOWED_BINS` (`_allowed_server_bins`); naming `bash`/`python` to run arbitrary code as the
  server user is 422'd (`--local` is the path for anything else). Run-metadata command names are also shape-
  checked by `_validate_run_meta` (plain command name — no path separators, spaces, or shell characters).
  The sandbox is excluded, and `/run` is member+ (executing argv server-side is a register-tier capability).
  - **Resource-limit sandbox (`runner.py`, the DoS half of the server-run sandbox):** every server-run
    child is spawned with POSIX rlimits via a `preexec_fn` (`_spawn_preexec`/`_rlimit_preexec`) — a
    CPU-seconds cap, a max-file-size cap, and core dumps disabled (a core would spill the injected secret
    to disk). Env-gated (`TREG_RUN_RLIMITS` on by default; `TREG_RUN_CPU_SECONDS`, `TREG_RUN_FSIZE_MB`),
    a no-op where `resource` is unavailable. Deliberately **no** address-space or process-count cap — a
    virtual-memory cap crashes Go CLIs (gh/stripe/doctl) and `RLIMIT_NPROC` is per-uid, shared with the
    server. Full **filesystem/network** isolation needs a container deploy and is a planned follow-up.
- **Meta:** `meta` (`GET /meta`, open) → `{public_url, github}` for the dashboard.
- **Provider catalog:** `providers_catalog` (`GET /providers.json`, open) → `{version, providers}` — the
  catalog `treg upload` uses to detect env keys → tools; served so the CLI can refresh centrally. See
  [env-import](env-import.md).
- **Auth — three identity doors** (all resolve to a user via the shared `_find_or_create_user`, so
  first-proof = registration — the **user only, no auto personal org**; a brand-new user lands with zero
  teams and names their first via the mandatory welcome / `treg org create`): **GitHub** — `auth_github` (`GET /auth/github`,
  `?cli=<id>` for the CLI handshake), `auth_github_callback` (browser → signed cookie, CLI → stashes an
  identity token), `auth_cli_poll` (`GET /auth/cli/poll?login_id=<id>` → the CLI collects its identity
  token once; **carries no code** — nothing to brute-force). **The handshake starts server-side** —
  `auth_cli_start` (`POST /auth/cli/start`, unauthenticated) mints BOTH the `login_id` and a short
  **pairing code** (`_PAIR_ALPHABET`, 4 chars) held in `_cli_pending`; `treg login` shows the code only in
  the terminal (never in the URL). **The universal sign-in page** — `login_page` (`GET /login?cli=<id>`, the page `treg login` opens; no
  `cli` → redirect to `/`; the id is whitelist-validated by `_LOGIN_ID_RE`, which is also the XSS guard
  since it's echoed into the page's JS): with a live session it shows a **team picker** (the JS
  `loadOrgs` fetches `auth_cli_orgs` — `GET /auth/cli/orgs`, session-authed, `_orgs_brief` returns the
  user's teams sorted **team-first, personal-last, most-tools-first**; one team → a single "Continue as"
  button, many → a labelled list; **zero teams → an inline "name your team" input** (`createTeam` → `POST
  /orgs` → approve with the new slug) so a brand-new CLI login never completes team-less), else every configured door (GitHub/Google buttons link to the `?cli=`
  flows; the email form drives `auth_email_start`/`verify` then loads the picker — always present, so
  login works with no OAuth app configured). `auth_cli_approve` (`POST /auth/cli/approve`,
  session-cookie-authed) completes the handshake by stashing the identity token under the given
  `login_id`, plus the **`org`** the user picked (validated to be one of their memberships) as
  `active_org` in the poll result — so the CLI lands on the RIGHT team instead of guessing. It requires
  the **pairing code** to be typed into the page (`#paircode`) and validates it against `_cli_pending`
  server-side (`_norm_pair_code`, case-insensitive; `CLI_APPROVE_MAX_TRIES` wrong tries then the pending
  login is discarded) — so a mistyped code fails immediately in the browser, and a **phished**
  `/login?cli=<attacker-id>` link (whose code the victim doesn't have, or that was never `start`ed) can
  never complete. Deliberately a POST guarded by `_same_origin` (Origin must be the configured
  `public_url` **or** the request's own host — public_url alone broke localhost). The GitHub/Google
  callbacks share `_finish_oauth_login`, which sets the session cookie then bounces a CLI handshake back
  to `/login?cli=<id>` so **all four doors** go through the same picker. `auth_logout` uses the same
  `_same_origin` guard.
  **Google** — `auth_google` / `auth_google_callback` (`GET /auth/google[/callback]`): the same
  session + CLI-handshake plumbing as GitHub (token from `google_token_url`, email from
  `google_userinfo_url`), gated on `google_client_id` and surfaced via `/meta`'s `google` flag. The
  callback now **requires `email_verified`** on the Google profile (like the GitHub door) — identity is
  keyed by email, so an unverified Google address equal to a victim's registered email would otherwise
  resolve to the victim (account takeover).
  **Email one-time code** — `auth_email_start` (`POST /auth/email/start`, mints a 6-digit code stored
  **in the DB** — `ratestore` over the `Ephemeral` table, namespace `otp`; the `dev_code` is put in the
  response + logged **only** when `get_settings().expose_dev_code` — true on a local sqlite box, never on
  a real Postgres deploy — otherwise the code is **emailed via Resend** — `email.send_otp`, best-effort)
  and `auth_email_verify` (`POST /auth/email/verify` → mints an identity token **and** sets the session
  cookie, so the CLI and dashboard share one endpoint). A wrong code burns one of `MAX_OTP_ATTEMPTS`
  before the code dies (brute-force cap). `/start` is **rate-limited** per-email AND per-IP
  (`ratestore.rate_check` sliding window in namespace `otp_start`, `OTP_START_MAX_PER_EMAIL`/`_PER_IP`) so
  it can't email-bomb an inbox or reset the attempt counter at will. **All this — the code, its attempt
  counter, and the throttle windows — is DB-backed (backlog #3), so a restart can't reset the caps and
  they stay correct across instances** (rows are swept by `expires_at` + `ratestore.sweep`; the landing
  `/demo/sandbox` throttle shares the same table, namespace `sandbox_hit`). The one remaining in-process
  piece is the short-lived CLI-login handshake (`_cli_pending`, self-heals on retry). A **suspended**
  account is refused at every door. **Invite sign-in link** —
  an invite carries TWO split secrets (`models.Invite`): the admin-visible `code` (returned from
  `create_invite` for out-of-band relay — join-only, NEVER an auth factor, since the admin provably
  holds it) and an inbox-only `email_token` (stored as `email_token_hash`, embedded ONLY in the email's
  link — possession proves inbox access, the same bar as the emailed OTP). `auth_invite_signin`
  (`GET /auth/invite-signin?t=<email_token>`, the email button): the GET renders a **confirm page**
  only (mail scanners prefetch GETs; a one-time credential must survive that) — the page's button
  POSTs the token back, and `auth_invite_signin_confirm` (`POST /auth/invite-signin`, urlencoded form
  parsed by hand to avoid the python-multipart dep) re-validates, `_find_or_create_user`s, refuses the
  suspended, **consumes the token** (`email_token_hash=None`, one-time) and mints the session cookie →
  303 `/?invite_org=<org_id>` (the dashboard opens its multi-select accept modal on that org). The
  invite itself stays `pending` — acceptance happens in the app so a multi-team invitee can accept
  several at once. The **legacy `?code=` path stays**: it never mints a session — validate and 303 to
  `/?invite=<email>` (a prefilled normal login; the invitee proves the email at a real door and the
  invite auto-appears via `/invites/mine`, now **ordered newest-first + `created_at`**). Invalid/expired
  either way → `/?invite_expired=1`. `auth_me`
  (`GET /auth/me`) answers for a **token**
  (`X-Treg-Token`) as well as a session cookie, so the dashboard's token door can learn its own email.
  `auth_cli_token` (`GET /auth/cli-token`, `require_identity`) mints a fresh **identity token** for the
  caller (session OR token); the dashboard embeds it in copy-paste snippets + a "copy token" button (pair
  with `X-Treg-Org` to pick the org). Signed session cookies + identity tokens carry a **`tv`
  (token_version)** claim bound to the user row (`sess.make`/`read_claims`, checked in `_user_from_session`
  / `_user_from_identity_token`); `auth_revoke_tokens` (`POST /auth/revoke-tokens`, `require_identity`)
  bumps `User.token_version`, invalidating every token that user holds at once — the kill switch for a
  leaked token that (unlike suspension) keeps the account and (unlike rotating `TREG_SESSION_SECRET`)
  affects only that user; it re-issues a fresh cookie + token so the caller stays signed in. A token with
  no `tv` (minted before this shipped) reads as `tv=0`, so a plain deploy revokes nobody.
  Plus `auth_me` (returns `onboarded`), `auth_logout`, and **onboarding** — `POST /onboard/demo|skip|reset`
  (`require_identity`) seed/dismiss/remove a first-run demo team (see [onboarding](onboarding.md)). Triple resolution: `require_identity`/`require_member`/
  `require_superadmin` accept a per-org **token**, a signed **identity token** (bearer, from `treg login`)
  + `X-Treg-Org`, or the browser **session cookie** + `X-Treg-Org`. See [dashboard](dashboard.md) + [cli](cli.md).
- **Static (dashboard + tutorials):** `dashboard` (`GET /`, `FileResponse` + `Cache-Control: no-cache`),
  `tutorial_js` (`GET /tutorial.js` — shared `window.TREG_TUTORIAL` + `hl()`), `tutorial_page`
  (`GET /tutorial` — standalone CLI tutorial). The **dashboard tour** is a `StaticFiles(html=True)` mount
  at `/dashboard-tour/` (serves `web/tour/` — `tour.js`, the standalone `index.html`, and the WebP
  `img/`). `favicon` (`GET /favicon.svg` + `/favicon.ico`). `llms_txt` (`GET /llms.txt`) serves
  `web/llms.txt` as `text/plain` with `{BASE}` templated from `public_url` — the [llms.txt](https://llmstxt.org)
  agent-onboarding file (call protocol + discovery + auth + CLI + skills + doc links). See [dashboard](dashboard.md).
  `install_sh` (`GET /install.sh`, `{BASE}`-templated) serves the CLI installer (`web/install.sh`).
  `terms_page` (`GET /terms`) + `privacy_page` (`GET /privacy`) serve the hosted registry's legal pages
  (`_legal_page`, no-cache) with `legal_css` (`GET /legal.css`) as the shared skin — `/privacy` is also
  the URL given to OAuth providers at app-verification time, so don't rename it. Provider brand marks are
  mounted at `/logos` (`StaticFiles` over `web/logos/`, resolved by convention `logos/<service>.svg`).
  `dashboard_marketplace` (`GET /app/marketplace/{service}`) serves the plain SPA (a connect page is only
  meaningful to a signed-in member, so no OG meta).
  `_serve_md` backs `quickstart_md` (`GET /quickstart.md`) + `tutorial_md` (`GET /tutorial.md`) —
  `{BASE}`-templated markdown served as inline `text/plain` (so "open in new tab" shows it, not a
  download); the docs pages' **Copy markdown** dropdowns (copy / open-in-tab) fetch these.
  Browser-facing auth pages (GitHub callback, OAuth-connect result) render via `_auth_page` (brand card).
- **Landing sandbox + hosted skills:** `demo_sandbox_mint` (`POST /demo/sandbox`, open, per-IP
  rate-limited) mints an anonymous throwaway team (its response now carries `live` = whether the seeded
  stripe tool is a real wire); `demo_sandbox_skill` (`GET /demo/sandbox/skill`) exports what the visitor
  built. `skill_samples` (`GET /skills/samples`, open) + `skill_install`
  (`GET /skills/{name}/install.sh?token=`) host sample skills. `call_tool` short-circuits **sandbox**
  orgs to `sandbox.synthesize` (real injection, no network). Caps via `_enforce_sandbox_cap`. Full
  behavior: [landing-sandbox](landing-sandbox.md).
  - **The one live wire (real Stripe demo).** When `demo_stripe_key` is set, a sandbox call to the exact
    seeded `stripe` tool (fingerprint-matched by `demo_sandbox.is_live_tool`, GET/POST only) is relayed
    for real to Stripe's test API via `_relay_live_demo` — a deliberately narrower relay: the auth header
    is built from the env key (never from a sandbox secret, which doesn't hold it), the body is
    form-encoded, and `metadata[visitor]` is overridden server-side. Metered per client IP
    (`_enforce_public_demo_ip_cap`) since the wire is one shared credential. `demo_sandbox_live`
    (`GET /demo/sandbox/live`) reports `live` + the visitor's feed name for an existing sandbox.
    `_require_not_live_demo_tool`/`_require_not_live_demo_secret` freeze the seeded `stripe` tool and its
    `STRIPE_KEY` against edit/delete so a visitor can't break their own live pane. The public payments
    feed: `stripe_webhook` (`POST /stripe/webhook`, 404 when `demo_stripe_webhook_secret` unset, verifies
    the signature via `pubfeed.verify_signature`, pushes a `charge.succeeded` into `pubfeed.push_charge`)
    and `landing_stripe_feed` (`GET /landing/stripe-feed`, unauthenticated SSE via `pubfeed.stream`,
    server-chosen fields only).
- **Public demo token (publishable, call-only credential):** `create_public_token`
  (`POST /orgs/{id}/public-token`, owner-only) flips the org to `public_demo` and mints a **viewer-role**
  token bound to a dedicated can't-log-in identity (`pub-<slug>@public-demo.treg.local`) — safe to print
  on a web page. Re-POSTing **rotates** (instant revocation of the old one); `delete_public_token`
  (`DELETE …`) revokes and lifts the lockdown. **Lockdown is centralized in the auth deps:** when
  `org.public_demo` and the role is below admin, `require_member` allows only `/call/*` + GET/HEAD/OPTIONS
  (every mutation is frozen no matter what routes are added later), and `require_identity` refuses the
  token entirely (it must never act as a user — mint identity tokens, create orgs, accept invites). Its
  `/call` traffic is metered per client IP (`_enforce_public_demo_ip_cap`, `PUBLIC_DEMO_HIT_NS`,
  ~10 calls/min/IP) since one token stands in for thousands of strangers.
- **Skills / bundles:** `register_skill` (`POST /skills`) composes a `Bundle` + its secrets + tools
  atomically, resolving each binding's `secret` local-name to the created secret id; the shared core is
  `_register_skill_bundle` (also used by the folder importer). `list_bundles`, `get_bundle`,
  `delete_bundle` (cascades; it 409s if a bundle secret is referenced by a tool **outside** the bundle —
  now guarding both an outside HTTP binding **and** an outside `cli.inject` entry, matching
  `delete_secret`, so a local-run tool can't be left with a dangling secret_id), and `update_bundle`
  (`PATCH /bundles/{id}`, creator/admin only) edits a recipe's SKILL.md text **and** the run-metadata
  (`runtime`/`package`/`entrypoint`/`runnable`). `_bundle_view`. **Folder importer** (dashboard mirror of `treg upload skills`):
  `analyze_skill_folder` (`POST /skills/analyze`) writes uploaded files to a temp dir and runs the CLI's
  own `skills.scan_skills`/`_classify` to classify each (recipe-only / contract / generated) **without**
  registering; `import_skill_folder` (`POST /skills/import`) scans + `build_payload`s + registers the
  selected ones (`_materialize_skill_files` sandboxes the upload). `list_orgs` now carries `tool_count`.
- **Audit:** `list_calls` (`GET /calls`, limit clamped 1–500; each row carries its `kind` —
  `call`/`local_run` — for the Activity + Usage views).
- **OAuth connect + the provider marketplace:** `oauth_start` (`POST /oauth/start`) creates a
  `PendingOAuth` and returns `consent_url` + `state` + `redirect_uri`; `oauth_callback`
  (`GET /oauth/callback`, open) exchanges the code and creates/updates the oauth secret; `oauth_status`
  polls. **Two modes** (`OAuthStartIn`): **BYO** (supply `client_id`/`client_secret`/`auth_uri`/
  `token_uri`/`scopes`) or **REGISTRY** (supply `provider` + optional `capability`) where treg fills
  everything from **its own approved OAuth app** — the marketplace. `oauth_providers_list`
  (`GET /oauth/providers`) lists the providers treg holds an app for, each flagged `configured` (false
  when this deployment hasn't set that provider's client credentials). In registry mode `oauth_start`
  reads the provider from `oauth_providers.get`, resolves scopes via `scopes_for(capability)`, and
  stashes every per-provider auth quirk on the `PendingOAuth` (PKCE `code_verifier`, `auth_params`,
  `token_endpoint_auth_method`, `client_id_param`, `scope_separator`, `long_lived_exchange`) so the
  callback exchanges the code exactly the way the consent URL was built. `connection_id` (BYO or
  registry) targets ONE existing connection to **reconnect/widen** it — scoped to the caller's org and
  matched to the provider, recorded as `replaces_secret_id` — instead of adding another account.
  **Callback does the real work:** it either replaces the named connection (`replaces_secret_id`) or
  adds a new one named by `_free_connection_name` (the first account for a provider keeps the bare
  service name — `google-search-console` — later ones get `-2`/`-3`), normalizes `granted_scopes` to
  space-joined, sets `expires_at` (`oauth.expiry_of`), then `_autoprovision_provider_tool` binds the
  fresh credential to the provider's API as a callable tool (idempotent by (org, name); a token-kind
  provider gets an `env` header binding, an oauth one gets a `Bearer {access_token}` binding; a provider
  needing treg's own second credential — Google Ads' developer token — also gets a **platform binding**,
  see [proxy-model](../architecture/proxy-model.md)) and `_record_connected_identity` best-effort asks
  the provider who connected. See [auth-secrets](../architecture/auth-secrets.md).
- **Connections (the marketplace's dashboard surface):** `list_connections` (`GET /connections`) returns
  every OAuth/registry credential in the org — metadata only, no token material — with health, expiry,
  and (for a known provider) `capabilities`/`missing_capabilities` + extra-credential notes. The filter
  is `kind=="oauth" OR provider!=""` so a bring-your-own-token provider (a plain `env` string, e.g.
  Slack) still lists. `connection_resources` (`GET /connections/{id}/resources`) **live-fetches** what a
  connection can act on (GSC sites, GA properties, Ads accounts), enriching id-only rows with the
  upstream's human name concurrently (`_enrich_resource_labels`) and recording the successful upstream
  call as proof of health; `set_connection_resource` (`POST …/resource`) pins the chosen `resource_ref`
  + `resource_name`. `connect_with_token` (`POST /connections/token`) connects a bring-your-own-bot-token
  provider (Slack), **verifying the token against the provider's probe before storing** and then
  auto-provisioning its tool. `set_extra_credential` (`POST /connections/{id}/extra-credential`) stores
  the second credential a provider needs when treg does NOT hold it centrally (rare) and finishes the
  tool with BOTH bindings. `revoke_connection` (`DELETE /connections/{id}`) deletes the credential and
  cleans up: it removes the tool treg auto-provisioned for the provider and drops the dead binding from
  any user-built tool, leaving that tool's other bindings intact. All `require_can_register`
  (member+). Helpers: `_owned_connection`, `_dig` (dotted-path walk).
- **Health:** `run_health` (`POST /health/run`) → `health.run_all`; `get_health` (`GET /health`) now
  returns `health._view(s)` plus a `needs_reconnect` flag (`health.needs_reconnect`) so a credential treg
  can't renew announces itself before it dies.
- **The proxy:** `call_tool` (`* /call/{rest:path}`) → `_resolve_call` → `_enforce_daily_cap` (the
  per-user daily cap; 429 when over) → (public-demo token → `_enforce_public_demo_ip_cap`) → load secrets
  (+ `ensure_fresh`) → `relay()` → `audit.record_call`. A **platform binding** carries no `secret_id`
  (its value comes from settings at relay time), so secret-loading now skips `secret_id is None`. Detail
  in [proxy-model](../architecture/proxy-model.md).

## Schemas
Pydantic input models: `UserIn`, `OrgIn` / `InviteIn` / `AcceptIn`, `EmailStartIn` / `EmailVerifyIn`,
`SecretIn` / `SecretUpdate`,
`ToolIn` (flat single-binding sugar + optional `bindings` + `health_check` + `cli`) / `ToolUpdate` (incl.
`cli`), `SkillIn` (`SkillSecretIn` + `SkillToolIn`, whose `cli` inject entries reference secrets by
local_name), `GrantIn` (argv) / `RunReportIn` (audit_id + exit_code + verdict), `OAuthStartIn` (now
BYO-or-registry: `provider` / `capability` / `connection_id` plus the BYO `client_id`/`secret`/URIs/
`scopes`), and the connection models `ResourceRefIn`, `TokenConnectIn`, `ExtraCredentialIn`. Output
helpers `_secret_view` / `_tool_view` / `_bundle_view` never leak secret values — `_tool_view` returns
`health_check` + `examples` + `cli` (it once omitted `health_check`, so a tool's probe was stored but never
surfaced by `GET /tools` / `/bundles/{id}`).

## Cross-cutting hardening (bug-hunt)
- **Security headers:** a `@app.middleware` adds `X-Content-Type-Options: nosniff`, `X-Frame-Options:
  DENY`, `Referrer-Policy: no-referrer`, and HSTS to every response (`setdefault`, so the `/call`
  proxy's stricter CSP/nosniff wins).
- **No 500 on bad ids/URLs:** an `OverflowError` handler turns an oversized all-digit id into a `404`
  (SQLite's 64-bit INTEGER); `_host_of`/`_resolve_call` guard `urlsplit` `ValueError` (malformed
  `base_url`/passthrough) into a `422`/`400`.
- **CSRF/redirect:** `auth_logout` rejects a cross-`Origin` request (forced-logout CSRF); `oauth_start`
  pins `redirect_uri` to treg's own `/oauth/callback` (consent-phishing guard).
- **Config default:** returning the OTP in the response (`dev_code`) is now gated by a dedicated
  `expose_dev_code` — true only on a local sqlite box, never on a real (Postgres) deploy — so a
  misconfigured `email_dev_mode` can't leak the code and enable an unauth takeover in prod.

Full endpoint list + the running server's OpenAPI: `README.md` and `/docs`. CLI-level usage: `USAGE.md`.
