---
title: Auth & secrets — injectors, encryption, OAuth freshness, health
status: shipped
sources:
  - src/treg/injectors.py
  - src/treg/crypto.py
  - src/treg/oauth.py
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

**Connect flow (mint the first token):** `consent_url(pending)` builds the provider consent URL
(`access_type=offline` + `prompt=consent` so a refresh token comes back); `exchange_code(pending, code,
client)` trades the auth code for tokens and returns a self-refreshable blob. Driven by the
`/oauth/*` endpoints ([interface/api.md](../interface/api.md)).

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
expire after `OAUTH_PENDING_TTL_MIN`).

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
