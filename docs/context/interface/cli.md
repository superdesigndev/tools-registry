---
title: The CLI (treg) + skill scaffolding
status: shipped
sources:
  - src/treg/cli.py
  - src/treg/convert.py
related:
  - interface/api.md
  - interface/skill.md
---

# The `treg` CLI

A thin client over the API in `src/treg/cli.py` — every command is one HTTP call, no logic of its own
(stdlib `argparse`, reuses `httpx`). Entry point `main()` → `build_parser()` → dispatch to a `cmd_*`.
Every command/subcommand carries a `description` + `help` on each argument + a copy-paste **Examples**
epilog (a `mk()` helper + `_ex()` + `RawDescriptionHelpFormatter`), so `treg <cmd> -h` is self-teaching.
`treg --version` / `treg version` print `cli_version()` (package metadata); `treg update` (`cmd_update`)
re-runs the server's `install.sh` to upgrade the CLI in place.

Every command builds its client via `_client(cfg)`, which returns a `_RegistryClient` (an
`httpx.Client` subclass). It survives an upstream WAF: when a request's body is 403'd by an edge (a
403 whose response body is an HTML block page, never treg's own JSON 403s), it re-sends the request
once, base64-encoded with `X-Treg-Body-Encoding: base64`, which the server decodes (see
[api](api.md)). This is what lets `treg upload` push SQL-bearing recipes and `treg call` proxy
SQL/HTML bodies through Cloudflare/Render. Transparent — no effect on any request that isn't blocked.

## Config + client (identity-first)
`~/.treg/config.json` (`CONFIG_PATH`) is v2: `{base_url, token, email, active_org, identity, admin_token}`
— **one bearer token + an active org slug** (`_load_config` migrates a legacy multi-org or flat config on
read, and tolerates a corrupt file as empty so a half-written config can't brick every command).
`_save_config` writes atomically (temp + `os.replace`); `login` persists the token **before** the
best-effort `_pick_active_org` lookup, so a transient `/orgs` failure can't discard a freshly-minted
token. `call --query` is relayed as a list of pairs (duplicate keys survive) and rejects a value with no
`=`; `contract_to_skill_payload`/`load_contract` raise clear errors (naming the entry/file) for a
stale/malformed `treg.json` instead of a bare traceback. Every "read a file / parse inline JSON the
user pointed at" path (`oauth connect`, `tool add/update --binding|--health`, `skill push|scaffold|init`,
`secret add --dir`) exits cleanly instead of tracebacking (`_load_json_arg` + guards); `_parse_bind`
rejects a non-int `secret=`; a one-shot `--org <slug>` override no longer wipes the stored active org on
`leave`/`delete` (`_clear_active_if_targeted`); `oauth connect` exits non-zero on a failed/timed-out
connect. `_client(cfg)` sends `X-Treg-Token: token` plus `X-Treg-Org: <active_org>` (the header is ignored
for a per-org token, and picks the org for an identity token). `_effective_org` applies the global
`--org` override; `_active_org_id` resolves the active org's numeric id via `GET /orgs` (for
`/orgs/{id}/...` endpoints). `_admin_client` uses `admin_token` else the bearer. `_show` pretty-prints +
exits non-zero on HTTP >= 400.

## Commands
- **`config`** (`--base-url`; shows email + active org + logged-in) · **`login`** — three doors in one
  `cmd_login`: default browser handshake — `POST /auth/cli/start` mints the `login_id` **and a short
  pairing code**, opens the universal `/login?cli=<id>#code=<code>` page — the code rides in the URL
  **fragment** (a fragment is never sent to the server, so it stays out of request logs) and the `/login`
  page **displays** it so the user just confirms it matches the terminal instead of typing it (the
  anti-phishing guard: a login you didn't start can't be approved into a token, and the server still
  validates the code at approve time — the guard itself is unchanged). The page reuses an
  existing dashboard session via a **team picker**, else offers GitHub / Google / email-code, then polls
  `/auth/cli/poll` **with no
  code**; the poll result may carry `active_org` = the team picked in the browser, which `cmd_login` adopts
  directly, falling back to `_pick_active_org` only against an older server (where `/start` 404s → a
  locally-minted `login_id`, no code)),
  `login --email you@x.com` (terminal-only email OTP: `POST /auth/email/start` →
  prompts for the 6-digit code → `/auth/email/verify`, storing the identity token), or `login --token <t>`
  for agents/CI — which now **verifies the token via `/auth/me` before saving** (a rejected token exits
  loudly instead of the old misleading "Token saved"; a valid token whose user has no team yet prints a
  "create a team first" hint rather than a silent `Active org: None`). First login by
  any door also registers you (the user only — no auto personal org) · **`logout`** (clears creds).
  After a first human login, `_maybe_offer_onboarding` prompts `[Y/n]` then shows the 3-path menu (TTY-only).
- **`onboard`** (`cmd_onboard`, `--path setup|access|demo`/`--source local|global|both`/`--name`/`--yes`/
  `--reset`; `--mode` hidden, back-compat `quick`→demo) — a TTY run opens with a one-second `_splash`
  decrypt animation (the wordmark reveals behind a ░▒▓ wavefront; any key skips; off-TTY/`NO_COLOR`/dumb
  terminals never see it), then `_pick_path` presents an **arrow-key menu** (`_menu` — ↑↓/jk move, ↵
  confirm, 1-9 jump-pick; falls back to questionary where raw-key mode is unavailable). The interactive
  default is **Set up**; the smart org-based default (team-with-tools → Access, empty admin team → Set up,
  else Demo) applies only non-interactively. **Set up** (`_run_setup`) asks "Import skill/secret from
  where?" — this project / global agent folders `~/.claude/skills` etc. / both / an **other project repo**
  typed inline (a `_menu` type-in row with fish-style folder autosuggestion; "this project" is hidden from
  a root-ish folder via `_is_rootish` so it can't sweep `$HOME`), unless `--source` pins it — then imports
  the chosen `.env` + skills and runs a batched `health --run`. **Access** (`_run_access`, list tools+skills
  → multi-select `skill install` → a no-key test call). **Demo** (`_run_demo`) is now purely
  **illustrative** — no team is created, nothing is uploaded — showing the loop across four beats (scan
  preview → roles → a real no-key call if the active team has a callable tool → the audit log). See
  [onboarding](onboarding.md).
- **`invites`** (`cmd_invites` → `GET /invites/mine`) lists invites addressed to your proven email;
  **`accept <org-slug>`** (`cmd_accept`) accepts one code-free (finds it in `/invites/mine`, `POST
  /invites/{id}/accept`, sets it active). The code path stays as `org join <code>`.
- **`org`** — `create "<name>"` (become owner), `ls` (marks the active one), `use <slug>` (switch active),
  `invite <email> [--role viewer|member|admin] [--expires-days N] [--tools a,b | --all-tools]
  [--local-run on|off]` (admin+; prints the one-time code; `_resolve_tool_access` offers an all-or-customise
  checklist prompt when neither flag is given on a TTY), `access <user_id> [--tools a,b | --all-tools]
  [--local-run on|off]` (`cmd_org_access`, admin+; sets which tools a member may use + the local-run toggle,
  keeping the unspecified field's current value → `PATCH /orgs/{id}/members/{user}/access`),
  `invites` (admin+; lists live pending, purges expired), `revoke <invite_id>` (admin+), `members` (admin+;
  each row now carries `tool_access` + `local_run_enabled`),
  `set-role <user_id> <role>` (owner-only), `join <code> --email you@…`, `leave`, `delete <slug>`
  (owner-only; must name the org — confirm-by-name). A global **`--org <slug>`** flag (stripped in
  `main` via `_pop_org_flag`, applied through `_ORG_OVERRIDE`/`_effective_org`) runs **any** command in
  that org instead of the active one. See [multi-tenancy](../architecture/multi-tenancy.md).
- **`secret add`** (`name`; `--value` | **`--env-var VAR [--env-file PATH]`** | `--file` | `--dir`; `--kind`) ·
  **`secret ls`** · **`secret rm`** · **`secret update ID`** (`--name`/`--value`/`--kind` → `PATCH /secrets/{id}`;
  only the given fields). `--dir` auto-discovers the file via `convert.find_secret_file`; a file-sourced value (`--dir`/`--file`,
  and the `treg.json` contract secret read in `contract_to_skill_payload`) is now `.strip()`ed, so a
  trailing newline can't become an illegal header/env value downstream. **`--env-var`** reads
  ONE named var from an `.env` (default `./.env`) via `providers.env_values` — the correct, value-internal way
  to register an **unmatched** key: it strips a balanced quote pair (so `KEY="v"` stores `v`, not `"v"` — the
  malformation agents hit hand-extracting with grep/cut) and the value never lands on the command line.
- **`add`** — a friendly top-level shortcut for `tool add`: `treg add <name> --base-url URL [--secret
  <name|id>]` (`cmd_add`). `--secret` accepts a secret **name** (resolved to its id via `_resolve_secret_ref`)
  or an id; default injection is a Bearer token in the `Authorization` header. `--header`/`--format`
  override it; `--base` aliases `--base-url`.
- **`tool add`** (`name`; `--base-url`; single-binding `--secret`/`--injector`/`--auth-*`/`--secret-field`;
  friendly multi-binding `--bind 'secret=ID,injector=oauth,name=...,format=...'` parsed by `_parse_bind`;
  raw `--binding '<json>'`; `--health '<json>'`) · **`tool ls`** · **`tool rm`** · **`tool update ID`**
  (`--base-url`/`--bind`/`--binding`/`--health` → `PATCH /tools/{id}`).
- **`import [env|skills|clis]`** (`--dir`/`--env-file`/`--skills-dir`; `--select a,b` | `--all` |
  `--dry-run`; `--status`; `--replace`; `--no-oauth`; `--llm` …) — scan a directory/machine and register
  what it finds; bare = **all three** (env + skills + clis).
  **env:** detect provider keys → secrets + tools (bearer/api-key/query/basic auto, OAuth pairs a
  per-provider connect, `--llm` for unknowns). **skills:** each skill subdir → a tool (from `treg.json`
  or generated from its script/secret) or a recipe-only bundle. **clis:** scan the machine for INSTALLED
  catalog CLIs (`shutil.which`), classify each (`providers.classify_cli`), auto-register the ready ones on
  the right tier (server-injected key / local **secret-less**, stored as an explicit `inject: []` so the
  catalog's inject can't merge back at grant time), and print an actionable, **plain-text** gap report (no
  emoji/colour, one CLI per line) — every missing piece names the fix (`set STRIPE_API_KEY` / `run gcloud
  auth login`); fix + re-run (idempotent; `--status`/`--dry-run` report only). **`--add BIN`** registers an
  INSTALLED cli that's NOT in the
  catalog (prompts for its key env var + API base_url) and prints a catalog-entry snippet to share; an
  unknown bin isn't server-allow-listed, so it runs locally until an admin allow-lists it. Brains in
  `providers.py` + `skills.py`; see [env-import](env-import.md).
- **`call`** (`target`, optional `path`; `--method`, `--query K=V` repeatable, `--data`, `--file`,
  `--content-type`, `--header 'K: V'` repeatable) → two shapes: named `call <tool> <path>` or agent-native
  single URL `call https://host/full/path` (path omitted) → both hit `/call/<rest>`. **`--header`** adds an
  extra request header the binding can't know (e.g. Google Ads' per-call `login-customer-id`); an
  **injected credential always wins**, so a `--header` can never overwrite the secret the proxy injects ·
  **`calls`** (`--limit`).
- **`run`** (`treg run <tool> [--local|--server] [--] <cli args…>`, `cmd_run`) — a **dispatcher** that picks
  a tier by flag: `_run_local` (default) or `_run_server`. `args` is an `argparse.REMAINDER`, which silently
  swallows a treg flag typed AFTER the tool name; `cmd_run` guards against that by reading the **real**
  `sys.argv` and refusing a tier flag (`--server`/`--local`/`--timeout`) placed after the tool but before the
  `--` separator — while still letting a flag after `--` reach the vendor CLI (so `treg run db -- --timeout
  30` works and passes `--timeout` to the CLI). Two execution tiers (see [CLI-RUN-PLAN](../../CLI-RUN-PLAN.md)):
  - **`--local`** (default, `_run_local`) — run the vendor CLI on THIS machine as a dedicated `treg-run`
    user so the credential is unreadable by the member (see [local-run](../architecture/local-run.md)). On
    Linux with local-run set up, the member hands off via `sudo -u treg-run <runner>`, passing its own
    token through the environment; the **runner** (`cmd_run_helper`, the hidden `__run-helper`, running as
    treg-run) fetches the grant (`POST /tools/{name}/grant`), runs the CLI with the credential, tees stderr
    to match the profile's `errors` → a translated message + `run-report` (verdict enum only), and passes
    the exit code through. Shared core `_run_helper` — which, on a **shared-key** run (grant sets
    `redact_output`), scrubs the injected value out of the CLI's stdout/stderr via `_StreamRedactor`
    (boundary-safe streaming). Without setup it runs as the member, **best-effort**, with a warning.
    **`setup-local-run`** (`cmd_setup_local_run`, run once with sudo — now **Linux AND macOS**; macOS creates
    the treg-run user via `dscl`/`_create_run_user`) creates the treg-run user, installs the runner
    (root-owned, can only invoke `__run-helper`), writes a narrow sudoers rule, and installs the **egress
    allow-list** (`_install_egress` → [local-run](../architecture/local-run.md); `--no-egress` skips it,
    `--refresh-egress` re-resolves drifting IPs, `--registry` sets the host to allow). Its **`--run-proof`**
    flag installs the runner proof at `/etc/treg-run/proof` (root-owned, mode 0400, readable only by
    treg-run); the runner script exports it as `TREG_RUN_PROOF` and `_run_helper` sends it as
    `X-Treg-Run-Proof` on the grant call — which is how the server releases a SHARED (non-owned) key to the
    isolated runner but refuses a direct member call (without `--run-proof`, only owned-key tools run
    locally). `treg tool update <id> --local-run on|off` flips `cli.enabled`. **`--fs-jail`** (opt-in, macOS)
    confines the CLI's file writes to a private scratch (`fsjail.macos_profile` + `sandbox-exec`, forwarded
    via `TREG_RUN_FSJAIL`) so it can't drop the key in a member-readable file — see [local-run](../architecture/local-run.md).
  - **`--server`** (Tier 0, `_run_server`) — run a runnable skill's CLI **on the registry server** (`POST
    /run`, `--timeout` cap 600), secrets injected server-side, stdout/stderr + exit code streamed back.
    **`runs`** (`cmd_runs`, `--limit`) shows the run audit log — now **BOTH tiers**: `GET /runs` merges
    server runs and local grants, each tagged `where` (`server`|`local`; a local success has a null exit
    code, since only failures report back).
  **`calls`** shows the local `GRANT`/`DENY`/`REPORT` audit rows.
- **`shell`** (`cmd_shell_start`/`cmd_shell_stop`) — **`shell start`** opens a subshell where the team's
  registered CLIs run with the credential injected transparently (a shim dir first on `PATH` routes each to
  `treg run`); **`shell stop`** (or `exit`) leaves. `--server-for a,b` routes named tools to the server
  (key never on the machine, if `server_runnable`), `--ttl MIN` auto-closes. See [shell](shell.md).
- **`admin`** (super-admin, cross-tenant): `login --token`, `stats`, `orgs`, `org <id>`, `users`,
  `tools`, `calls`, `health`, `grant`/`revoke <user_id>`, `suspend-user`/`rm-user <user_id>`,
  `suspend-org`/`rm-org <org_id>`. `_admin_client` sends the saved `admin_token` (`treg admin login`)
  or falls back to the active org token (works for an `is_superadmin` user). See
  [super-admin](../architecture/super-admin.md).
- **`skill`** (`init --dir`, `add --dir`, `scaffold <dir> [--out]`, `push <file>`, `ls`, `rm`) — see below.
- **`health`** (`--run`).
- **`oauth`** — **`oauth providers`** (`cmd_oauth_providers` → `GET /oauth/providers`) lists the services
  treg holds its **own** approved OAuth app for. **`oauth connect`** (`cmd_oauth_connect`) has two modes:
  **registry** — `--provider <service>` (e.g. `google-search-console`), optional `--capability` to pick a
  scope set (default read) and optional `name`, so treg's app supplies the client credentials; or
  **bring-your-own** — `name --client-secret <file> --scopes …` reads your own Google OAuth client JSON
  (`_byo_body`). Either posts `/oauth/start`, prints the consent URL, and polls `/oauth/status` ~5 min.
- **`connections`** (`cmd_connections_ls`/`_resources`/`_use`/`_rm`) — your connected accounts: **`ls`**
  (`GET /connections`, health + expiry), **`resources <id>`** (`GET /connections/{id}/resources` — the
  sites/properties/accounts it can act on), **`use <id> <resource_ref>`** (`POST /connections/{id}/resource`
  — select which one), **`rm <id>`** (`DELETE /connections/{id}` — disconnect).

`_parse_bind` defaults every field to a bearer `Authorization` header; only `secret=` is required, so a
multi-credential tool needs no JSON.

## Skill scaffolding + the `treg.json` contract (`convert.py`)
`scaffold_skill(dir)` walks a skill directory (`_SECRET_DIRS` = `.secret`/`.secrets`, `_RECIPE_FILES` =
SKILL.md/README.md) and emits a `/skills` manifest **stub**: the recipe + every credential file as a
secret (kind guessed by `_guess_kind` — JSON with `refresh_token` → `oauth`, other JSON → `secret_file`,
plain → `env`), and a tool with `base_url` and per-binding placement left as `FILL` for the agent to
complete. `find_secret_file(dir, kind)` (used by `secret add --dir`) matches by `_matches_kind` and
returns exactly one file or raises (none / ambiguous).

**The `treg.json` sidecar contract** (`CONTRACT_FILE`) lets a skill self-describe its registration so
treg is one command. `generate_contract(dir)` is the *semi-automation helper*: it auto-discovers secrets
(+ kinds), `_guess_base_url(dir)` scans SKILL.md + `*.py` for the upstream host (skipping doc hosts via
`_DOC_HOST_HINTS`), and emits **non-colliding** bindings via `auto_bindings` (shared with the dashboard's
`_classify`): the primary oauth/bearer token → `Authorization: Bearer`, each additional credential → its
own filename-derived header (`developer_token` → `developer-token`), and OAuth app config
(`client_secret.json`, `_is_app_config`) is skipped. `_oauth_secret_field` detects Google's `token` vs
`access_token`. base_url: a skill-name catalog match (`providers.match_skill`, e.g. google-ads/gsc) wins
over the `_guess_base_url` heuristic; whatever it still can't resolve confidently is listed in `_fill` for
the user. `treg skill init --dir` writes it; `treg skill add --dir` registers it
(`load_contract` + `contract_to_skill_payload` load the named secret files → `POST /skills`, so no secret
values live in the file; a `file:` path is resolved via `resolve_secret_path`, which swaps `.secret`↔`.secrets`
when the exact spelling is absent, so a shared contract survives the per-machine secret-dir spelling drift). `secret add --dir` **syncs back** into an existing `treg.json`
(`_sync_contract_secret`) so CLI-driven changes stay authoritative. **`treg skill install <name>`**
(or `--all`, `--dir`) does the reverse — pulls a bundle from the registry (`GET /bundles/{id}`) and
writes `<dir>/<name>/SKILL.md` PLUS its **companion files** (`_write_bundle_files` reconstructs the whole
folder from `bundle.files` — reference docs, scripts, nested subdirs; each path re-checked to stay inside
the skill dir, secrets never shipped), so a teammate installs a complete shared skill with one command;
a tool-backed skill notes its registered tools to call via `treg call`. A skill folder that **already
exists on disk is kept, not overwritten** (unless `--force`); the run ends with an **actionable summary
of the kept skills** + the `--force` hint, so a caller (agent or human) decides whether to overwrite —
the Access agent-instruction defers to this output rather than restating the rule. The push side (`build_payload` /
`contract_to_skill_payload`) collects those files via `skills.collect_files` (excludes `.secret*`,
`SKILL.md`, `treg.json`, VCS/build junk, binaries, oversized files).
