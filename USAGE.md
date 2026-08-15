# treg — CLI usage

`treg` is the command-line client for **tools-registry**: call shared team tools without holding
their credentials, and turn your local skills into shareable tools. It's a thin client over the
API (`https://treg.to`); the API is the only brain.

Every command reads `~/.treg/config.json` for the endpoint + your token. Get per-command detail
with `treg <command> --help` (e.g. `treg secret --help`, not `treg "secret add"`).

## Install (once)

```bash
cd tools-registry
uv tool install --editable . --python 3.13     # puts `treg` on your PATH, tracking the dev source
# code edits reflect with no reinstall; re-run with --reinstall only after a dependency change
```

## Core idea

**Two sources of callable tools sit behind the same `/call/`.**

- **The catalogue** — ~2,600 curated external endpoints across ~40 data providers (TikTok, Instagram,
  Reddit, SEO and SERP data, enrichment, scraping…). Call one by its **endpoint id**. If nobody on the
  team holds a key for that provider, treg can serve it on **its own key**, billed per call from the
  team's prepaid balance. No provider signup.
- **The vault** — your team's own tools. Any API key, OAuth connection or vendor CLI a member
  registers becomes callable by every teammate and their agents, without the key ever leaving the
  server.

The vocabulary for the vault half:

- A **tool** = an upstream `base_url` + a list of credential **bindings** (each binding injects one
  secret into the request). A request can carry several (e.g. google-ads: OAuth bearer + a
  `developer-token` header).
- A **skill / bundle** = a recipe (SKILL.md) + its secrets + its tool(s).
- The proxy **relays, never models** the upstream, and **injects auth server-side**, so callers
  never hold the key. Your `X-Treg-Token` is stripped before the upstream sees it.

---

## Setup / identity

Auth is **identity-first**: prove who you are once (GitHub or an email code — first proof also
registers you + creates your personal org), then work across all your orgs. Agents/CI can instead
present a per-org token directly.

| Command | Options | What it does |
|---|---|---|
| `treg config` | `--base-url URL` | show or set the endpoint (prints base URL, email, active org, logged-in) — use `treg org ls` for the full org list |
| `treg login` | _(none)_ | GitHub browser sign-in (register-or-login), stores one identity token |
| `treg login` | `--email EMAIL` | email one-time-code sign-in (register-or-login); prompts for the code |
| `treg login` | `--token TOKEN` | agents/CI: use a per-org token directly |
| `treg logout` | — | clear your stored credentials |
| `treg onboard` | `--mode guided\|quick` · `--name N` · `--yes` · `--reset` | colourful guided first-run — pick **guided build** (you create the team + invite a teammate, step by step) or **quick demo** (we seed a full demo team + tool + activity), ending on a no-key call. Offered `[Y/n]` after your first login; `--reset` removes demo teammates |

```bash
treg config --base-url https://treg.to
treg login                                 # GitHub; or:
treg login --email you@example.com        # emailed 6-digit code (register-or-login)
```

## Teams / orgs

An **org** (team) owns the tools/secrets. After `treg login` the CLI holds a single **identity token**
in `~/.treg/config.json` and sends your **active** org as `X-Treg-Org`; `treg org use <slug>` switches
it. (With `treg login --token`, that per-org token is used directly instead.) Every other command runs
in your active org. Roles: **owner > admin > member** (a member calls + manages
only what they created; admin/owner manage anything in the org; admin+ can invite/remove members).

| Command | Options | What it does |
|---|---|---|
| `treg org create` | `"NAME"` | create a new org; you become its owner (new token, auto-active) |
| `treg org ls` | — | list your orgs (marks the active one) |
| `treg org use` | `SLUG` | switch the active org |
| `treg org invite` | `EMAIL`, `--role member\|admin` | (admin+) create a one-time invite **code** to share |
| `treg org members` | — | (admin+) list members + roles |
| `treg org join` | `CODE`, `--email EMAIL` | redeem a code: registers you if new, joins, saves the org token |

```bash
# owner side
treg org create "Team A"                       # active org is now team-a
treg org invite bob@company.com --role member  # prints e.g. inv_7Kd9x2LmQpR4 — send it to Bob

# Bob's side (no email is sent; he gets the code over Slack/DM)
treg config --base-url https://treg.to
treg org join inv_7Kd9x2LmQpR4 --email bob@company.com   # joins + mints HIS own token
treg tool ls            # sees only Team A's tools
treg org use team-a     # switch orgs anytime; one token per org, never mixed
```

## Secrets (credentials — write-only; the API never returns a stored value)

| Command | Options | What it does |
|---|---|---|
| `treg secret add NAME` | `--value V` \| `--file PATH`, `--kind env\|secret_file\|oauth\|cli_auth` | upload a credential (a string value, or a file's contents) |
| `treg secret ls` | — | list (name / kind / owner) |
| `treg secret rm ID` | — | delete (blocked while a tool binds it) |

```bash
treg secret add posthog-key --value "$POSTHOG_API_KEY"
treg secret add gsc --file ./.secrets/token.json --kind oauth
```

## Tools

| Command | Options | What it does |
|---|---|---|
| `treg tool add NAME` | `--base-url URL` (required) | register a tool |
| — single-binding | `--secret ID`, `--injector`, `--auth-in`, `--auth-name`, `--auth-format`, `--secret-field` | the common case |
| — multi-binding (friendly) | `--bind 'secret=ID,injector=,location=,name=,format=,secret_field='` (repeatable) | only `secret=` is required |
| — multi-binding (raw) | `--binding '<json>'` (repeatable) | full binding dict, advanced |
| — health probe | `--health '{"path":"me","expect_status":200}'` | optional validation probe |
| `treg tool ls` | — | list tools + their bindings |
| `treg tool rm ID` | — | delete |

**Defaults** for a single `--secret` and for each `--bind`:
`injector=env`, `location`/`auth-in=header`, `name`/`auth-name=Authorization`,
`format`/`auth-format=Bearer {secret}`, `secret-field=access_token`.

```bash
# single credential
treg tool add posthog --base-url https://us.posthog.com --secret 3
# query-key API instead of a bearer header
treg tool add gsc --base-url https://searchconsole.googleapis.com --secret 3 \
  --injector oauth --secret-field token
# two credentials on one request (google-ads)
treg tool add google-ads --base-url https://googleads.googleapis.com \
  --bind "secret=4,injector=oauth" \
  --bind "secret=5,name=developer-token,format={secret}"
```

## The catalogue (call an API you have no key for)

| Command | Options | What it does |
|---|---|---|
| `treg catalog search` | `"what you want to do"` | find endpoints by capability |
| `treg catalog get` | `ENDPOINT_ID` | docs, parameters, **the price**, and how you would be served |
| `treg call ENDPOINT_ID` | `--query K=V`, `--data STR` | call it |
| `treg catalog request` | `"what's missing"` | searched, not there? file it — requests steer what gets added next |

```bash
treg catalog search "instagram profile"
treg catalog get tikhub.tiktok.user.profile          # shows the price BEFORE you spend
treg call tikhub.tiktok.user.profile --query uniqueId=tiktok
```

**How a catalogued call is served — the credential ladder, in order:**

1. the team registered its own tool for that provider → that tool, that key;
2. the team stored a secret for the provider → injected through a virtual tool;
3. neither → **treg's own key**, metered against the team's prepaid balance.

Rung 3 only applies where treg has both a key and a published price for that endpoint; anything
unpriced is refused rather than served, and you are told to connect your own key. Your own key is
never billed to the balance.

## Balance & top-up

Only calls on **treg's key** (rung 3 above) cost balance. Everything else — your own keys, your own
tools, vendor CLIs — is free of it.

| Command | Options | What it does |
|---|---|---|
| `treg balance` | `--limit N`, `--json` | credit left, calls in flight, recent spend |
| `treg topup` | | add funds, or set up automatic top-ups |

```bash
treg balance                 # every new team starts with $1.00 of free credit
treg balance --json          # integer micro-USD (1e-6 USD) — the unit the ledger uses
treg topup
```

Out of balance is an HTTP **402** carrying `balance_micro`, `estimated_cost_micro` and a `topup_url`,
so an agent can act on it without reading prose. There is also a per-day ceiling on spend against
treg's keys, so a runaway agent has a bounded blast radius.

## Calling

| Command | Options | What it does |
|---|---|---|
| `treg call TOOL PATH` | `--method GET`, `--query K=V` (repeatable), `--data STR`, `--file PATH` | proxy a call (named form) |
| `treg calls` | `--limit N` | audit log (who called which tool, when, status) |

```bash
treg call intercom conversations --query per_page=5
treg call posthog api/projects --method POST --data '{"name":"x"}'
```

**Agent-native (raw HTTP) form** — build the real upstream URL and prefix it; no CLI needed:

```
GET https://treg.to/call/https://api.intercom.io/conversations?per_page=5
    + header:  X-Treg-Token: <your token>
```

treg resolves the tool by the upstream host, injects the credential, and relays everything
faithfully (method, all query params incl. duplicates, headers, cookies, body).

## Running vendor CLIs

`treg call` proxies **HTTP APIs**. `treg run` is its command-line complement: it runs a **vendor
CLI** (stripe, gh, vercel, gcloud, flyctl…) with the tool's credential injected server-side, so the
member runs the CLI **without ever holding or logging into the key**. A recipe-only catalog-CLI
skill (e.g. a `stripe-cli` SKILL.md) auto-becomes runnable — `treg upload` recognises it via the
provider catalog and wires the credential, so `treg run` works with no `treg.json`.

The tool owner opts in per tool for **local** runs (the key reaches the member's machine): the
dashboard `⌘ run` toggle, or `treg tool update <id> --local-run on|off`. **Server** runs need no
opt-in — the key never leaves the registry, and the server's bin allow-list gates what may execute.
One `cli` profile on the tool drives both tiers (`bin`, `inject`, `deny`, `enabled`).

| Command | Options | What it does |
|---|---|---|
| `treg run TOOL -- <cli args>` | `--local` (default) \| `--server`, `--timeout N` ([--server] only) | run the tool's CLI with its credential injected; everything after `--` goes to the CLI verbatim |
| `treg runs` | `--limit N` | CLI-run audit log (who ran which tool, when, exit code) |
| `treg setup-local-run` | `--run-proof VALUE`, `--member USER` | (admin, Linux, `sudo`, once) install the isolated `treg-run` runner |

**Two tiers:**

- `--local` (default) — runs on the member's **own machine**. On Linux, an admin runs `sudo treg
  setup-local-run` **once** so the CLI runs under a dedicated `treg-run` system user and the
  credential never touches the member's own uid; on macOS it's best-effort (runs as the member,
  with a warning). A member may run a tool whose key **they own**; a **shared** (teammate/admin) key
  requires the isolated runner **and** the server's `TREG_RUN_PROOF` (pass it as `--run-proof` at
  setup).
- `--server` — runs on the registry **server** (Tier 0); only catalog-known CLIs (or ones in
  `TREG_RUN_ALLOWED_BINS`) may execute; stdout/stderr + exit code are streamed back. Use this when
  the key must never reach the machine at all.

```bash
treg run stripe -- get /v1/balance          # local: CLI runs here, key injected, never held
treg run gh -- pr list
treg run --server agentmail-cli inboxes list # server-side: key never leaves the registry
sudo treg setup-local-run --run-proof "$TREG_RUN_PROOF"   # Linux admin, once
treg runs --limit 20
```

## Catching your own calls

`treg run` covers a vendor **CLI**, and `treg call` covers an HTTP call you ask treg to make. Neither
helps when a program makes its own request to `api.stripe.com` — treg is invisible to it and it has no
key.

### The normal way: `treg <command>`

Put `treg` in front of any command:

```bash
treg claude                  # a Claude Code session using the team's shared credentials
treg codex                   # same for Codex
treg node server.js          # your app, with the team's keys, without holding any
treg python train.py
treg with -- npm test        # the explicit form, for anything that confuses the parser
```

The proxy needs one compiled package (`cryptography`) to generate this machine's certificate
authority. The `install.sh` installer includes it; if you installed with plain `pip` and it is
missing, treg offers to add it the right way for your install the first time you use the feature.

**This is opt-in per command, and that is the point.** treg is the parent process, so the setting
applies to that command and its children only. `treg claude` uses the team's shared access; plain
`claude` is completely untouched and uses your own local keys. Nothing is written to any config file,
so there is nothing to undo and no session is ever changed behind your back.

While it runs, an HTTPS call to a **registered** host goes through the registry, which adds the
credential **on the server**. Every other address — including your agent's own calls to
`api.anthropic.com` or `api.openai.com` — goes straight out and cannot be read by us.

If a `treg serve` proxy is already running it is used and left alone; otherwise treg starts a private
one on a port the operating system picks (so two sessions never collide) and stops it when your command
exits.

**One caveat if you write Node.** Node's built-in `fetch` ignores proxy settings until **Node 24**. On
older Node a plain `fetch()` is not captured and the call goes out with no credential. Use a client that
reads the environment (`axios`, `got`, undici's `ProxyAgent`) or upgrade. curl, git, Python
`requests`/`httpx` and Deno work at any version. There is a runnable example in
[`examples/proxy-demo/`](examples/proxy-demo/) that shows both, and explains the workaround for older
Node.

### The subshell: `treg shell --proxy`

`treg run` covers a vendor **CLI**, and `treg call` covers an HTTP call you ask treg to make. Neither
helps when an agent writes its own script that talks to `api.stripe.com` directly — treg is invisible to
it and the script has no key.

`treg shell start --proxy` closes that gap. Inside that shell, an HTTPS call to a **registered** host is
routed through the registry, which adds the credential **on the server** and returns the vendor's answer
unchanged. Your code needs no key and no treg-specific lines:

```bash
treg shell start --proxy                 # the banner lists the hosts being captured
curl https://api.stripe.com/v1/balance   # no key anywhere — treg injected it server-side
python my_script.py                      # same for anything the script calls
exit                                     # the proxy stops with the shell
```

How it works: the shell sets `HTTPS_PROXY` and a trust bundle **for that shell only**, using a
certificate authority generated on your machine (private key `0600`, valid two years, never shared).
**The system trust store is never modified** — nothing outside that shell trusts it, not your browser
and not the operating system.

What it does **not** touch: every address that is not a registered tool, including your agent's own
calls to `api.anthropic.com` or `api.openai.com`. Those are tunnelled without being read, and no
certificate is ever generated for them.

| Option | What it does |
|---|---|
| `--proxy` | turn it on (off by default) |
| `--proxy-port N` | listen somewhere other than 127.0.0.1:18791 |
| `--renew-ca` | regenerate this machine's certificate authority before starting |

### As a background service (`treg serve`)

If you would rather keep your own shell than enter a subshell, run the same proxy as a service:

```bash
treg serve start                  # starts in the background, prints the next line for you
eval "$(treg serve env)"          # point THIS terminal at it (repeat in any other terminal)
curl https://api.stripe.com/v1/balance
treg serve status                 # port, team, and the hosts being captured
eval "$(treg serve env --unset)"  # stop using it in this terminal
treg serve stop                   # stop the service
```

`treg serve start --foreground` stays attached instead of detaching, which is what you want for a
service manager or when reading its log (`~/.treg/proxy/serve.log`).

One difference worth knowing. `treg shell --proxy` keeps its access token in the subshell's
environment and both disappear together. A service has to be findable by other terminals, so it writes
its port and token to `~/.treg/proxy/proxy.json` (owner-readable only, mode `0600`). That file holds
the proxy's own token, never a vendor key — but it is a file on disk, which the subshell version does
not have. Choose accordingly.

Limits worth knowing: a client that pins its certificate will refuse the interception (use `treg run` or
`treg call` for that one); every captured call takes one extra hop through the registry, so it fails if
the registry is down; a **hosted** MCP server makes its calls on someone else's machine, so it is not
covered. If a call fails, treg says so in its own words — "no tool is registered for this host", "ask an
admin" — rather than leaving you to read a bare 404 as the vendor's answer.

## Skills (bundles)

| Command | Options | What it does |
|---|---|---|
| `treg skill scaffold DIR` | `--out FILE` | walk a skill dir → a manifest stub (recipe + secrets discovered; you fill `base_url` + bindings) |
| `treg skill push FILE` | — | register a completed manifest (bundle + secrets + tool(s) atomically) |
| `treg skill ls` | — | list bundles |
| `treg skill rm ID` | — | delete a bundle (cascades to its tools + secrets) |

```bash
treg skill scaffold ~/.claude/skills/intercom --out intercom.json
#   edit intercom.json: set base_url + bindings
treg skill push intercom.json
```

## Bulk upload (scan → upload)

| Command | Options | What it does |
|---|---|---|
| `treg scan [env\|skills]` | `--dir D`, `--env-file F`, `--skills-dir D` | read-only preview: list the keys & skills upload would register (nothing leaves the machine) |
| `treg upload [env\|skills]` | `--dir D`, `--select a,b` \| `--all`, `--replace`, `--no-oauth`, `--llm …` | register a `.env`'s provider keys and/or a skills folder in one pass |

```bash
treg scan                        # what's here? (keys + skills, read-only)
treg upload --all                # register everything the scan found
treg upload env --select openai,stripe
treg upload skills --dir ~/.claude/skills --all
```

Idempotent — re-run any time (skips what's registered; `--replace` updates). Non-interactive
(agent/CI) runs refuse without `--all`/`--select`.

## OAuth (connect flow) + health

| Command | Options | What it does |
|---|---|---|
| `treg oauth connect NAME` | `--client-secret PATH`, `--scopes S1 S2 …` | browser consent → treg captures the first token and stores it as an oauth secret |
| `treg health` | `--run` | show every credential's status; `--run` re-checks now (refresh oauth, probe tools, alert owners) |

```bash
# one-time: add https://treg.to/oauth/callback to your OAuth app's redirect URIs
treg oauth connect gads --client-secret ./client_secret.json \
  --scopes https://www.googleapis.com/auth/adwords
treg health --run
```

---

## OAuth: two modes (treg owns freshness)

- **Auto-refresh** — if the oauth secret carries `refresh_token` + `client_id` + `client_secret`,
  treg refreshes it before it expires (you never re-upload). The `oauth connect` flow always lands
  here.
- **Manual** — a bare uploaded token is injected as-is; you re-upload when it expires.

Same storage; a credential graduates manual → auto with no migration. `treg health` flags any
credential that stops working and webhooks the owner (if a `webhook_url` was set at registration).

## Auth shapes (per binding `injector`)

- `env` — plain string (API keys)
- `secret_file` — a JSON token file; pull `secret_field`
- `oauth` — a JSON OAuth token; pull `secret_field` (auto-refreshed if refreshable)
- `cli_auth` — material lifted from a CLI's keychain (placed like a string)
