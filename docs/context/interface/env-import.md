---
title: Import — scan a .env AND/OR a skills dir, auto-register as tools + bundles
status: in-progress
sources:
  - src/treg/providers.py
  - src/treg/skills.py
related:
  - interface/cli.md
  - interface/skill.md
  - architecture/auth-secrets.md
  - architecture/data-model.md
---

# Import (`treg upload`; read-only preview = `treg scan`)

Instead of adding tools one by one, `treg upload` scans a directory and registers what it finds.
Bare `treg upload` does **both** sides of the dir; `treg upload env` / `treg upload skills` restrict it
(`--dir` sets the location, default cwd). `cmd_import` in `cli.py` is the dispatcher.

- **env side** (`providers.py`): scans a `.env`, detects third-party **provider API keys**, and
  registers the chosen ones as secrets + tools.
- **skill side** (`skills.py`): scans a directory of skills (each a subdir with SKILL.md) and registers
  each as a tool (+ recipe) or a recipe-only bundle — see "Skill directories" below.

## The provider catalog (`CATALOG`)
~80 curated providers (`CATALOG_VERSION`, now **9** — added `probe` = a cheap authenticated GET path per
provider so an imported tool self-validates, then a wave of `cli` local-run blocks plus the CLI-only
providers Google Cloud, Azure, and Supabase, and per-CLI `deny` rules for leaky subcommands — see the `cli`
block below),
each `{provider, tokens, base_url, auth}`. `tokens` are
distinctive name components (OPENAI, RESEND…) matched against the **underscore-split** var name, so an
app prefix (`TREG_RESEND_API_KEY`) still resolves via its `RESEND` component. `_match_provider` prefers
the **longest** matching token (HUGGINGFACE over HF). `auth` is the provider's default **shape** — the
recipe for turning the stored secret into an authenticated request, which is the crux (a base_url alone
isn't enough), and `build_binding` maps each shape to a treg binding:
- `bearer` → header `Authorization: Bearer {secret}`
- `api_key_header` → header `<header>: {secret}` (or a custom `format`, e.g. Discord `Bot {secret}`,
  PagerDuty `Token token={secret}`, Unsplash `Client-ID {secret}`)
- `query` → the secret is placed in a query param (`location:"query"`, e.g. SerpAPI `?api_key=`)
- `basic` → a **pair** (Twilio `ACCOUNT_SID`+`AUTH_TOKEN`); the CLI base64s `id:secret` into ONE secret
  and binds `Authorization: Basic {secret}` (`basic_parts` splits id vs secret)
- `oauth2` → a **pair** (`CLIENT_ID`+`CLIENT_SECRET`) → the connect flow, not a static binding

An entry may also carry a `skills:[…]` alias list, matched by `match_skill(name)` against a SKILL FOLDER
name (case/punctuation-insensitive, aliases or the provider name) — the door for **file-credential skills
that have no env var to key on** (OAuth token files). Such entries carry `tokens: []` so the env scanner
never mis-detects them as a simple bearer key: e.g. **Google Ads** (`googleads.googleapis.com`) and
**Google Search Console** (`searchconsole.googleapis.com`), whose real auth is OAuth + an extra header.
A skill-name match is authoritative for the tool's `base_url` (a curated host beats a heuristic guess).

An entry may also carry a **`cli` block** — the run profile for `treg run`
([local-run](../architecture/local-run.md)): `{bin, install, inject:[…], deny:[…], errors:[…],
noninteractive, warnings, verified, auth_mechanism, detect, login_cmd, beta}`. `verified` holds the date of a
real machine test (docs lie — Vercel ships an env var it ignores, so it injects via `--token`; documented ≠
verified); `beta` marks an unverified entry. Several entries now carry **`deny`** patterns for subcommands
that would print the injected key or run member code as the isolated runner (`gh extension`/`alias`/`auth
token`/`--show-token`, `flyctl|turso auth token`, `doppler|infisical run`, …) — enforced by `check_deny` at
grant (see [local-run](../architecture/local-run.md)). `CATALOG_VERSION` is now **9**. An `unsupported:true` block is first-class: it tells the analyzer
WHY and what to do instead (e.g. **Azure** — device-login only → register a service principal as an HTTP tool).
The catalog can never ENABLE a local run — only the owner's `tool.cli.enabled` does.

**`auth_mechanism`** (`env|argv|config_file|device`, validated in `localrun.validate_cli_profile`) is the
field the CLI auto-importer (below) routes on: `env`/`argv` are **server-injectable** (treg holds + injects
the key → either run tier works); `config_file` is **local-only** (the credential lives in the CLI's own
config — e.g. `aws`, `gcloud`); `device` is report-only. **`detect: {config_paths}`** is a login-state hint
(a path present ⇒ the CLI is logged in), **`login_cmd`** the exact login command for the gap report
(`gh auth login`, …). `server_runnable` (in `_tool_view`) now requires `auth_mechanism in (env, argv)`, so a
config_file/device tool is honestly surfaced as local-only.

## CLI auto-import (`treg scan clis` / `treg upload clis`) — scan the machine, register, report
The machine that runs `treg scan`/`treg upload` is the only place that knows which CLIs are **installed**
and logged in. (`import` was renamed: `treg scan` = read-only preview, `treg upload` = register; `treg
import` is a hidden back-compat alias of upload.) `_import_clis` (`cli.py`, dispatched for the `clis` mode
of `scan`/`upload`) walks the catalog's `cli`
entries: `shutil.which(bin)` (installed?), the credential env var present (`providers.cli_env_var`), and any
`detect.config_paths` present (logged in?). **`providers.classify_cli`** (pure, unit-tested) turns those
facts into a decision — `ready`(server|local) · `needs_key` · `needs_login` · `unsupported` · `not_installed`.
Ready ones are registered (`_register_cli_tool`): **server tier** stores the key + binds it (an HTTP tool AND
a server-injected run); **local tier** is secret-less — it stores an **explicit `inject: []`** (not a
dropped key) so the catalog's own inject can't merge back in at grant time (the CLI reads its own config).
Then an **actionable report** where every gap names the exact fix (`set STRIPE_API_KEY` / `run gcloud auth
login`); fix it and re-run (idempotent; `--replace` re-creates, `--status`/`--dry-run` report without
writing). See `docs/CLI-AUTOIMPORT-PLAN.md`.

**Secret kind `param`.** A non-secret value (a project id, an org id) stored like a secret and injected the
same way — into a CLI env var (`CLOUDSDK_CORE_PROJECT`) or an HTTP query/header binding. It reuses the
secret storage + binding machinery; health checks and the local-run invalid-marking both skip it (it is
config, not a credential).

## Classification (`scan_env` → `list[Detection]`)
Reads **names only** (`var_names` splits at the first `=` and discards the value). Each variable lands
in one bucket, precedence provider-first:
1. **matched** — a catalog provider (single bearer/api-key credential). BUT a provider token on a
   clearly-CONFIG var (a `CONFIG_HINTS` component — HOST/URL/ID/REGION… — with no `SECRET_HINTS`
   component) is **config, not a credential**: `POSTHOG_HOST`, `POSTHOG_PROJECT_ID`, `SUPABASE_URL` are
   skipped, while `POSTHOG_API_KEY` / `RENDER_API` still match. `plan_actions` also disambiguates
   duplicate tool names (`GITHUB_TOKEN` + `GH_TOKEN` → `github`, `github-2`).
2. **oauth_pair / basic_pair** — `PAIR_FORMS` recognizes `CLIENT_ID`+`CLIENT_SECRET` (oauth2) and
   `ACCOUNT_SID`+`AUTH_TOKEN` (basic) and **groups the two vars into one** Detection (a lone half is
   kept but flagged *incomplete*). This is the "GitHub isn't a Bearer key" fix — a client pair is an
   OAuth app, not a token.
3. **app_internal** — the `APP_INTERNAL` denylist (SECRET_KEY, SESSION_SECRET, JWT_SECRET, DATABASE_URL,
   ADMIN_TOKEN…). The app's OWN secrets, **never** offered as a tool and never LLM-resolved. Checked
   *after* provider match, so `OPENAI_SECRET_KEY` still reads as a real OpenAI key.
4. **unknown_secret** — has a `SECRET_HINTS` component (KEY/TOKEN/SECRET…) but no provider → Phase-4
   LLM/manual candidate.
5. **config** — everything else (LOG_LEVEL, PORT…), skipped.

## Planning + registering (`plan_actions`, `build_binding`, `env_values`)
`plan_actions` turns each *offerable* detection into an `Action`: a `matched` one becomes a supported
plan with a `tool_name` (`_slug(provider)`), `base_url`, a `binding` from `build_binding(auth)`
(bearer/api-key only; basic/oauth2/query return `None` → deferred with a reason), and `health` =
`{path: provider.probe, expect_status: 200}` when the catalog carries a `probe`. `cmd_import` then:
select (interactive `questionary` checkbox, or `--select a,b` / `--all` / `--dry-run`) → for each
chosen action `env_values` reads **only** that var's value → `POST /secrets {name,value,kind:"env"}` →
`POST /tools {name,base_url,bindings:[{…,secret_id}],health_check}`. That probe means `treg health --run`
actually validates env-imported keys (else they'd be `unknown`) and onboarding's test call
(`cli._testable_path`) hits a REAL endpoint (render→`services`, vercel→`v2/user`) instead of the base-URL
root, which 404s and looks like a bad credential. `--dry-run` prints the plan with **no network
and no values**. Errors (e.g. a sandbox cap) are reported per-tool, never fatal.

## OAuth pairs — sequential consent (`_import_oauth_loop`)
A complete `oauth_pair` whose provider has connect endpoints in the catalog (`oauth_ready`) is handled
**after** the key registration, one provider at a time: `_import_oauth_loop` prompts `i/N: connect
<provider>? [y = connect / n = skip / a = skip all]`, and on `y` runs `_import_oauth_connect` (reads the
`CLIENT_ID`/`CLIENT_SECRET` via `oauth_parts` + `env_values`, `POST /oauth/start` with the catalog's
`auth_uri`/`token_uri`/`scopes`, prints the redirect URI to allow + the consent URL, polls
`/oauth/status/{state}`) before advancing to `i+1/N`. `a` stops the rest; only runs on a TTY (or is
skipped with a hint); `--no-oauth` opts out. Catalog entries carry an `oauth` block for GitHub, Slack,
GitLab, Notion, Discord, Linear. Basic pairs (Twilio) are still deferred.

> **Safety:** detection never reads a value; only `env_values` does, and only for the vars being
> registered. The binding is the standard treg shape — see [auth-secrets](../architecture/auth-secrets.md)
> for how `{secret}` is injected on the `/call` path, and [data-model](../architecture/data-model.md)
> for `Tool.bindings`.

## Catalog distribution (`GET /providers.json` + `_load_catalog`)
The catalog is served at **`GET /providers.json`** (`api.py`, open, `{version, providers}` from
`CATALOG_VERSION`/`CATALOG`) so it can grow **centrally** — add a provider server-side and every CLI
picks it up, no new release. `cli._load_catalog` refreshes from `<base_url>/providers.json`, caches to
`~/.treg/providers-cache.json`, and falls back to the cache then the **bundled `CATALOG`** when offline
(so `treg upload` always works). `scan_env(path, catalog=…)` takes the resolved catalog; `--dry-run`
uses the bundled one to honor its no-network promise.

**Why not a blind mega-ingest:** a probe of APIs.guru (2,529 APIs) showed the auto-extracted metadata
is unreliable for our purpose — e.g. the `github.com` entry resolves to a random third-party splash
URL, Stripe's auth reads as `basic`, several (OpenAI/Notion) don't expose a parseable scheme, and it
*misses* modern providers we hand-curate (Anthropic, Cloudflare). So the catalog stays **curated +
hand-verified**; an ingest can only feed *suggestions* a human/LLM confirms.

## LLM fallback for unknowns (`treg upload --llm`)
`unknown_secret` vars (a credential with no catalog match) can be resolved by an LLM: `--llm` (with
`--llm-token` or `TREG_LLM_TOKEN`) sends the **names only** to an **OpenAI-compatible** endpoint
(`_llm_chat`; default `--llm-model gemini-2.5-flash` via Gemini's compat `--llm-base-url`). The pure
`providers.llm_prompt`/`llm_parse` build the request + parse the JSON reply (tolerating prose, dropping
entries without a var/base_url/known shape). Each resolution is shown and **confirmed by the user**
(`LLM suggests … Register? [y/N]`) before a secret + tool are created via `build_binding` — same path as
a catalog match. The LLM only runs on a real run (not `--dry-run`), and app-internal secrets never reach
it (they're excluded before this step).

## Skill directories (`treg upload skills`, `skills.py`)
`scan_skills(dir, catalog, env_names)` classifies every skill subdir into a `SkillDetection`:
- **contract** — already has a `treg.json` (`convert.load_contract`); used verbatim (google-ads, gsc).
  A contract `file:` path is resolved via `convert.resolve_secret_path`, which tolerates the
  `.secret`/`.secrets` spelling drift (both are gitignored, so the spelling differs per machine): the
  exact path wins, else the leading secret-dir segment is swapped — so a treg.json written against
  `.secret/token.json` still finds `.secrets/token.json`. Used on both the readiness check and `build_payload`.
- **generated** — an API-tool skill with no contract: base_url from a **catalog match on the skill name**
  (`providers.match_skill`, authoritative) → the script's `API="…"` (`_BASE_RE`) → `convert._guess_base_url`;
  the credential from a local `.secret*` file **or** the auth env var the script reads (`_ENV_RE` →
  cross-referenced against the provider `CATALOG` for base_url + auth shape). File-credential bindings come
  from `convert.auto_bindings` (shared with `generate_contract`, so CLI + dashboard agree). A needed env var
  missing from `env_names` is recorded as a **gap** (skipped, not a broken tool) — so the skills door must
  see the RIGHT `.env`: `cmd_import` resolves it via `_find_env_upwards(skills_dir)` (a skills dir like
  `./.claude/skills` sits UNDER a project whose `.env` is at the root), else a skill whose credential is a
  shared-`.env` var (render/vercel, no local `.secrets/`) would gap "needs env var … not found" and be
  skipped. `treg upload` also merges `os.environ` into `env_names`, so a credential already exported in
  your shell is found without a gap. The scan line prints which `.env` was used.
- **catalog CLI recipe** — a secret-less skill (SKILL.md only) whose NAME matches a catalog `cli` entry
  (e.g. `stripe-cli`). Instead of a plain recipe, `_catalog_cli_detection` turns it into a **runnable cli
  tool**: it attaches the catalog cli profile (enabled), sets `base_url`, and makes each injectable env
  credential an **env-sourced secret** discovered from the machine at import (and **asks once**, hidden
  `getpass`, for any still missing). Registered, it runs via `treg run --local <name>` (not the proxy). A
  hand-written `treg.json` in the folder still wins over this (the contract path is checked first).
- **recipe_only** — a knowledge/workflow skill (SKILL.md, no external authed API, not a catalog CLI):
  published as a recipe-only **bundle** (the SKILL.md text, no tool/secret) so the whole team library
  lives in one installable place. `POST /skills` already accepts `tools:[]`/`secrets:[]` — no server change.

`build_payload` turns a detection into a `POST /skills` body (file secrets read from disk — **`.strip()`ed**
so a trailing `\n` in a token file can't become an illegal header value — env secrets from `env_values`,
recipe_only ships just the recipe; **`skills.collect_files` always adds the folder's companion files**
`{relpath: content}` so the whole skill travels, minus `.secret*`/`SKILL.md`/`treg.json`/junk/binaries);
`write_contract` persists the generated `treg.json` back into the skill
dir. `cmd_import` → `_import_skills` scans → multi-select (same questionary/`--select`/`--all` UX as env;
an **env-var gap is checkable by default** — `_only_resolvable_gaps` — since it's fixable) →
`_prompt_missing_skill_creds` **asks (hidden `getpass`) for any credential a chosen skill needs but the
`.env` lacks** (e.g. google-ads' `GOOGLE_ADS_DEVELOPER_TOKEN`, intercom's `INTERCOM_TOKEN`), filling
`values` + clearing the satisfied gap so the skill registers instead of being skipped (blank input = still
skipped) → writes contracts → pushes → reports. Duplicate tool name → 409, reported (wipe to re-push).

**Multi-credential skills — distinct headers, not a collision (`convert.auto_bindings`).** A skill that
ships **several credential files** (e.g. google-ads: `token.json` + `developer_token` + `client_secret.json`)
used to bind each to `Authorization` and get rejected at registration ("duplicate header binding"). The
generator now avoids that on the fly, for BOTH the CLI (`generate_contract`) and the dashboard (`_classify`),
via the shared `convert.auto_bindings`: the **primary token** (the oauth/bearer credential, else the first)
→ `Authorization: Bearer {secret}`; **every other credential** → its own header derived from its filename
(`developer_token` → `developer-token`, value injected as-is); and OAuth **app config** (`client_secret.json`
/ `credentials.json`, matched by `_is_app_config`) is skipped entirely (never a request credential — its
client_id/secret already live inside the token blob). So multi-credential skills import cleanly with no
hand-written `treg.json`. (The base_url may still guess wrong — a correct upstream host + any bespoke
placement remain reasons to commit a `treg.json`, which overrides the generator.) `_flag_header_collisions`
stays as a guard for a hand-written contract that still collides.

**Dashboard folder importer** reuses this SAME classifier server-side: `POST /skills/analyze` writes the
uploaded files to a temp dir and runs `scan_skills`/`_classify` (verdict identical to the CLI); `POST
/skills/import` builds + registers the selected ones. Import is **idempotent + crash-proof**: it skips
anything already registered and registers **each skill in its own `session_maker()` session**, so one
failure (bad binding, IntegrityError) can't poison the shared session for the rest of the batch
(`greenlet_spawn` errors) or 500 it. See [api](api.md) + [dashboard](dashboard.md).

## Safety + correctness guards
- **Non-interactive runs never import silently.** With no TTY and neither `--all` nor `--select`, both
  sides refuse (agents/CI must state intent) rather than registering every key/skill unprompted.
- **Signing secrets aren't callable keys.** `*_WEBHOOK_SECRET` / `*_SIGNING_KEY` classify app_internal
  even when a provider token matches (`ALWAYS_INTERNAL`); `*_MODEL` / `*_DSN` / `*_BASE` are config.
- **`*_AUTH_TOKEN` is a Bearer token** for a non-basic provider (Sentry), not half a Twilio Basic pair.
- **LLM base_urls are validated** — `_safe_base_url` rejects non-https / loopback / private / link-local
  hosts before a resolved unknown is registered (SSRF/hallucination guard); resolutions de-dup by var.
- **Skill detection** keys on `SKILL.md` (a README-only dir isn't a phantom skill), skips dot-dirs,
  reads `.sh`/`.js`/`.ts` scripts (+ `$VAR` reads) at any depth, and can target a single skill dir.
  A `treg.json` contract's `health`/`examples` carry into the tool, and its readiness is validated
  (missing file / absent env var → a gap, so it can't silently push a broken tool).

## Re-running (idempotency)
Import is safe to re-run (the dev loop is wipe-and-rerun). Both sides look up what's already registered
and **skip by name** — a re-run reports "already registered" and creates nothing new (a recipe-only
bundle has no tool, so the server's tool-name 409 wouldn't catch it; the CLI check does). `--replace`
deletes-then-recreates the matching tool/bundle/secret so nothing duplicates; env-import also checks the
tool name **before** POSTing the secret, so a clash never leaves an orphan secret. `treg skill install`
skips an existing local `SKILL.md` unless `--force`, de-dups by name, and rejects unsafe path segments.

## Planned (not yet built)
- **Catalog growth** — keep expanding the curated core; a verified-ingest feeder (APIs.guru/Pipedream)
  as suggestions, never auto-trusted. (21 CLI entries so far; the `beta` flag marks unverified ones.)
- **Skill polish** — `--llm` to resolve a generated skill's missing base_url; `--replace` to re-push.
- **CLI auto-import polish** (phases 3–4) — prompt for an UNKNOWN installed CLI (bin + env var) and
  optionally contribute it back to the catalog; a dashboard "Setup needed" badge for a tool whose `cli`
  is disabled / missing its key. (The machine scan + classify + register + gap report shipped — see the
  "CLI auto-import" section above.)
