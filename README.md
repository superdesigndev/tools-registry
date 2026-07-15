# tools-registry

**A remote registry that turns the team's skills into shareable, callable tools, via a
credential-injecting proxy.** Call an upstream API (PostHog, Stripe, Search Console, Render, …)
with **no key on your machine**; the registry injects auth server-side. There are **two ways to
use a tool**: **`treg call`** proxies an **HTTP API** (secret injected server-side, nothing on your
machine), and **`treg run`** runs a vendor **command-line tool** (`stripe`, `gh`, `vercel`,
`gcloud`, …) with the credential injected, so you use the CLI without owning it or logging in.
Consumers are usually **agents** (Claude Code / Codex / Gemini), but humans can use it too (CLI or
raw HTTP).

Private team repo. This README is the entry point, it links to the deeper docs rather than
repeating them.

---

## Status

**Live now** at **`https://treg.ngrok.app`** (self-hosted on a Mac Studio, port `18790`, exposed
via ngrok).

| Area | State |
|---|---|
| Core engine | ✅ Shipped + tested (184 tests): proxy, 4 auth shapes, full CRUD, audit log, multi-binding tools, skill/bundle composer, URL-passthrough, OAuth auto-refresh + hosted connect flow, health checks, CLI |
| CLI runner | ✅ **`treg run <tool> -- <args>`** runs a vendor CLI (`stripe`, `gh`, `vercel`, …) with the credential injected — `--local` (default; on your machine, isolated under a `treg-run` user on Linux) or `--server` (runs on the registry, key never leaves it). Audit via `treg runs` |
| Teams / orgs | ✅ **Org multi-tenancy shipped**: a token = a `(user, org)` membership; resources scoped per org; roles owner/admin/member; one-time-code invites; `treg org` commands |
| Registered tools | ✅ **intercom, gsc, stripe, posthog, render** — all health-green (in the default `superdesign` org) |
| Onboarding | ✅ **first-run demo team** on both dashboard + CLI (`treg onboard`): seeds teammates + a working tool + sample activity so a new user feels it in seconds; skippable + removable |
| Email | ✅ OTP sign-in codes + team invites emailed via **Resend** (`no-reply@treg.superdesign.dev`) |
| Parked | ⏳ **vercel** (needs a token), **google-ads** (token revoked; its Desktop OAuth client blocks the hosted connect flow) |
| Cloud deploy | ⏳ Not yet, target is **Render** (currently Mac Studio + ngrok) |

---

## Mental model

Think of a **coat check**: you hand over your coat (the secret) once and get a ticket; later anyone
with a valid ticket says "table 5's coat" and the attendant fetches the right one, they never carry
it themselves. The proxy swaps a **tool reference** for the **real secret** on the way out.

- **tool** = an upstream `base_url` + a list of credential **bindings** (each binding injects one
  secret into the request; a request can carry several, e.g. an OAuth bearer *and* a
  `developer-token` header).
- **skill / bundle** = a recipe (`SKILL.md`) + its secrets + its tool(s), registered together.

**The one rule:** the proxy **relays, never models** the upstream, and **injects auth server-side**,
so it survives upstream API changes and callers never hold keys.

---

## Quickstart (3 moves)

```bash
# 1. install the CLI (from the repo root) — editable, tracks the source
uv tool install --editable . --python 3.13

# 2. point at the registry + get your token (saved to ~/.treg/config.json)
treg config --base-url https://treg.ngrok.app
treg login --email you@superdesign.dev

# 3. call a live tool — no key on your machine
treg call stripe v1/balance
```

Your token identifies you on every call (`X-Treg-Token` header) and is the same for all tools.

---

## Two ways to call

### 1. URL-passthrough — the agent-native way (recommended)

You already know the upstream API. Build the **real** request and just prefix it with the proxy:

```
Real request:   GET https://api.intercom.io/conversations?per_page=5
Through treg:   GET https://treg.ngrok.app/call/https://api.intercom.io/conversations?per_page=5
                    header:  X-Treg-Token: <your token>
```

treg resolves the tool by the upstream **host**, injects the credential, and relays **everything
faithfully** (method, all query params incl. duplicates, your headers, cookies, body). Your
`X-Treg-Token` is stripped before the upstream sees it. No treg-specific syntax to learn.

### 2. CLI shorthand

```bash
treg call intercom conversations --query per_page=5
treg call posthog api/projects/95715/insights --method GET
```

---

## Run a vendor CLI (`treg run`)

Some tools ship as a **command-line program** (`stripe`, `gh`, `vercel`, `gcloud`, …), not a plain
HTTP API. `treg run <tool> -- <args>` runs that CLI **with the org's credential injected**, so you
use it without owning the key or logging in. Everything after `--` is passed straight to the vendor
CLI:

```bash
treg run stripe -- get /v1/balance
treg run gh -- pr list
```

**Two execution tiers:**

- **`--local`** (default) — the CLI runs **on your machine**. On Linux it is isolated under a
  dedicated `treg-run` user (a different uid can't read the process's env or memory) once you install
  it with `sudo treg setup-local-run`; on macOS it's best-effort.
- **`--server`** — for catalog-known CLIs, the CLI runs **on the registry server** and streams its
  output back, so the **key never reaches your machine** (add `--timeout <sec>` to cap it).

```bash
treg run --server agentmail-cli inboxes list
```

`treg runs` is the audit log for CLI runs (who ran which tool, when, and the exit code). A
recipe-only catalog CLI skill (e.g. `stripe-cli`) **auto-becomes runnable when imported** — no extra
setup.

---

## What's registered now

| Tool | Upstream | Example call |
|---|---|---|
| **intercom** | `https://api.intercom.io` | `treg call intercom me` |
| **gsc** | `https://searchconsole.googleapis.com` | `treg call gsc webmasters/v3/sites` |
| **stripe** | `https://api.stripe.com` | `treg call stripe v1/balance` |
| **posthog** | `https://eu.posthog.com` | `treg call posthog api/projects/95715/` |
| **render** | `https://api.render.com` | `treg call render v1/services --query limit=5` |

`gsc` is OAuth and **auto-refreshes** its token. Discover the current set anytime: `treg tool ls`.
Check credential health: `treg health` (or `treg health --run` to re-validate now).

---

## Architecture

**Request flow for `/call`:** resolve tool (by URL host + longest `base_url` prefix, or by name) →
decrypt its secret(s) → apply each binding's injector → stream to the upstream → fire-and-forget
audit record. The proxy does no business logic and never buffers the body.

**Module map** (`src/treg/`):

| Module | Role |
|---|---|
| `proxy.py` | `relay()` — the whole product in one function: a faithful streaming proxy |
| `injectors.py` | the auth-shape seam: `env`, `cli_auth`, `secret_file`, `oauth` place a secret into a header/query |
| `oauth.py` | token freshness (single-flight refresh) + the connect flow (consent URL, code exchange) |
| `health.py` | credential health: refresh oauth, probe tools, webhook the owner of anything broken |
| `convert.py` | scaffold a skill directory into a registerable bundle manifest |
| `api.py` | the API — the only brain; CLI + skill are thin clients over it |
| `cli.py` | the `treg` CLI |
| `models.py` | SQLModel tables: `Org`, `User`, `Membership`, `Invite`, `Secret`, `Tool`, `Bundle`, `PendingOAuth`, `CallRecord` |
| `crypto.py` `config.py` `db.py` `audit.py` | Fernet encryption + tokens · settings · async DB · deferred audit writer |

**The 4 auth shapes** (per binding `injector`): `env` (plain string / API key) · `secret_file` (a
JSON token file, pull a field) · `oauth` (a JSON OAuth token, auto-refreshed if refreshable) ·
`cli_auth` (material lifted from a CLI's keychain).

**Faithful-relay contract:** the proxy alters **only** three things, everything else is verbatim:
1. hop-by-hop transport headers (re-derived per hop),
2. treg's own control + edge-forwarding headers (`x-treg-token`, `x-treg-org`,
   `ngrok-skip-browser-warning`, `x-forwarded-*`, `via`, …) and treg's session cookie — all stripped,
   never leak upstream,
3. the injected credential(s).

**OAuth, three ways to get the first token:** *manual upload* (drop in a `token.json`) · *auto-refresh*
(if the token carries `refresh_token` + client creds, treg keeps it fresh, you never re-upload) ·
*hosted connect flow* (`treg oauth connect` → browser consent → treg captures the token itself).

**Health checks:** give a tool an optional probe (`{method, path, expect_status}`); a periodic run
(on demand or via cron) validates every credential, refreshes OAuth, and webhooks the owner of any
that break.

Deep design lives in [`docs/context/`](docs/context/README.md) (per-subsystem fragments).

---

## The API

Base URL `https://treg.ngrok.app`. All endpoints require the `X-Treg-Token` header **except** the two
marked **open**. Full request/response detail: run the server and open `/docs` (OpenAPI).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/users` | **open** — register, returns a token shown once |
| `POST/GET/PATCH/DELETE` | `/secrets[/{id}]` | manage secrets (values are write-only, never returned) |
| `POST/GET/PATCH/DELETE` | `/tools[/{id}]` | manage tools + bindings |
| `POST` | `/skills` | composer: create a bundle + its secrets + tool(s) atomically |
| `GET/DELETE` | `/bundles[/{id}]` | list / inspect / delete bundles (delete cascades) |
| `GET` | `/calls` | audit log (who called what, when, status) |
| `POST` | `/oauth/start` | begin a connect flow → returns consent URL + state |
| `GET` | `/oauth/callback` | **open** — browser redirect target (protected by unguessable `state`) |
| `GET` | `/oauth/status/{state}` | poll a pending connect |
| `POST` `GET` | `/health/run` · `/health` | run credential health / read status |
| `*` | `/call/{rest:path}` | **the proxy** — passthrough (`/call/https://real.api/...`) or named (`/call/<tool>/<path>`) |

CLI-level reference: [`USAGE.md`](USAGE.md).

---

## Register a new tool or skill (creator flow)

```bash
# single-key tool (API key)
treg secret add render-key --value "$RENDER_API_KEY" --kind env         # or --file / --dir
treg tool add render --base-url https://api.render.com --secret <id> \
  --health '{"path":"v1/services","expect_status":200}'

# multi-credential tool (e.g. google-ads: OAuth bearer + a developer-token header)
treg tool add google-ads --base-url https://googleads.googleapis.com \
  --bind "secret=<oauth-id>,injector=oauth" \
  --bind "secret=<dev-id>,name=developer-token,format={secret}"

# a whole skill directory -> bundle
treg skill scaffold ~/.claude/skills/intercom --out intercom.json   # discovers recipe + secrets
#   edit intercom.json: set base_url + bindings, then:
treg skill push intercom.json

# OAuth via the browser (mints the first token, treg holds it)
treg oauth connect gsc --client-secret client_secret.json --scopes https://www.googleapis.com/auth/webmasters.readonly
#   note: requires a *Web* OAuth client with https://treg.ngrok.app/oauth/callback whitelisted
```

`--secret add` accepts `--value`, `--file <path>`, or `--dir <skilldir>` (auto-finds the file of the
given `--kind` under `.secret/.secrets`). Full options: [`USAGE.md`](USAGE.md). First-time bootstrap:
[`docs/ONBOARDING.md`](docs/ONBOARDING.md).

---

## Running the server

**Hosted now:** Mac Studio, `python -m treg` on port **18790**, exposed via the ngrok endpoint `treg`
→ `treg.ngrok.app`.

**Run locally:**

```bash
uv sync                        # create the venv from uv.lock
uv run python -m treg          # serve on 0.0.0.0:18790 (add --reload for dev)
uv run python -m treg keygen   # print a fresh Fernet key for TREG_SECRET_KEY
```

**Environment** (prefix `TREG_`, read from `.env`):

| Var | Default | Purpose |
|---|---|---|
| `TREG_DATABASE_URL` | `sqlite+aiosqlite:///./treg.db` | DB URL (SQLite for dev, Postgres on Render) |
| `TREG_SECRET_KEY` | *(empty)* | Fernet key for secret-at-rest; empty → an ephemeral key is minted (secrets won't survive a restart) |
| `TREG_PUBLIC_URL` | `https://treg.ngrok.app` | treg's public base, used to build the OAuth callback URI (not in `.env.example` yet) |
| `TREG_ADMIN_TOKEN` | _(empty)_ | cross-tenant **super-admin** bearer; presenting it authorizes every `/admin/*` endpoint. Empty disables the env path (only `is_superadmin` users reach `/admin`). Keep it long + secret. |
| `TREG_EMAIL_DEV_MODE` | `false` | when true, `/auth/email/start` returns the OTP in its response (no mail sender needed) — **dev/local only**, never in prod. |
| `TREG_API_TOKEN` | `dev-token` | MVP leftover — **not referenced anywhere in the code**; authorizes nothing (caller auth is per-membership tokens / identity tokens / session cookies). |

> **⚠️ Back these up before moving or redeploying:** `.env` (holds the Fernet key) and
> `treg-server.db` (holds every registered tool + encrypted secret) are **gitignored** and live only
> on the host. Lose the Fernet key and every stored secret becomes unrecoverable.

---

## Repo layout

```
tools-registry/
├── src/treg/            # the package (api, cli, proxy, injectors, oauth, health, convert, models, …)
├── tests/               # 184 tests (see below)
├── docs/
│   ├── context/         # design fragments (codemap system) + generated index
│   └── ONBOARDING.md    # first-time bootstrap
├── src/treg/web/skill.md # the shippable "tools-registry" Claude skill (served at /skill.md)
├── meetings/            # kickoff transcript (design source of record)
├── .claude/skills/tools-registry-context/   # doc-maintenance skill (/tools-registry-context)
├── USAGE.md             # full treg CLI reference
├── CLAUDE.md            # project instructions (stale — see Status)
└── pyproject.toml
```

---

## Tests

```bash
uv run pytest -q     # 184 tests
```

Coverage: proxy walking-skeleton, all injector shapes, per-user auth + CRUD + audit, skill composer,
URL-passthrough + faithful relay, OAuth refresh + connect flow, health checks, skill scaffolding, CLI.

---

## Roadmap

- **Deploy to Render** (leave Mac-Studio + ngrok behind).
- **Finish the parked tools:** vercel (needs a token), google-ads (needs a fresh, non-revoked token).
- **First proving ground:** convert every credentialed `superdesign-agi` skill into a tool,
  intercom / gsc / stripe / posthog / render are done.
- **Later:** MCP support, finer permission tiers, at-rest key-management hardening, possible Loopni merge.

---

## For AI coding agents

If you're **Claude Code / Codex** working in a repo and need an API the team shares, **don't hunt for
keys**:

1. Take the real upstream request URL you'd normally make.
2. Prefix it with `https://treg.ngrok.app/call/` and send header `X-Treg-Token: <token>`.
3. Discover what's available with `treg tool ls`.

Example:
```bash
curl "https://treg.ngrok.app/call/https://api.intercom.io/me" -H "X-Treg-Token: $TREG_TOKEN"
```

**Make your agent a treg master in one fetch:** point it at **[`https://treg.ngrok.app/llms.txt`](https://treg.ngrok.app/llms.txt)**
— an [llms.txt](https://llmstxt.org)-format overview (the call protocol, discovery, auth, CLI, skills,
and links to the tutorial/docs). Reading that file is enough to use the whole registry.

To work on **this repo's** design docs, use the **`/tools-registry-context`** skill, it loads the
right `docs/context/` fragment for what you're touching and keeps the docs in sync.

---

## Links

- [`USAGE.md`](USAGE.md) — full CLI reference
- [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — first-time bootstrap
- [`src/treg/web/skill.md`](src/treg/web/skill.md) — the shippable Claude skill (consumer / creator / admin); served `{BASE}`-templated at `/skill.md`, auto-installed to `~/.claude/skills/tools-registry/` by `install.sh`
- [`docs/context/`](docs/context/README.md) — design fragments (architecture, auth, interfaces, ops)
- [`meetings/2026-06-30-jason-tools-registry.md`](meetings/2026-06-30-jason-tools-registry.md) — kickoff / design source of record
- Repo: https://github.com/superdesigndev/tools-registry
