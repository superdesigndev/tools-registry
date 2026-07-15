---
name: tools-registry
description: Call shared team tools without holding their credentials, and turn your local skills into shareable tools. Use when you need to hit an upstream API (PostHog, GSC, Google Ads, Intercom, Stripe, …) but don't have the key locally, or when you want to register/share a skill so teammates' agents can call it. Three personas in one skill — consumer (call), creator (register/share), admin (manage).
---

# tools-registry — call shared tools, share your own

A remote registry + **credential-injecting proxy**. You make the *real* upstream API call; it is
routed through the proxy, which injects the auth **server-side**. You never hold the secret.

- **Endpoint:** `{BASE}`  ·  **CLI:** `treg`  ·  the CLI is a thin client over the API.
- **Auth:** every call sends `X-Treg-Token: <your token>`.
- **Core idea:** a **tool** = an upstream base URL + a list of credential **bindings**. A **skill/bundle**
  = a recipe (SKILL.md) + its secrets + its tool(s). The proxy *relays, never models* the upstream.

## First: install + sign in
```bash
curl -fsSL {BASE}/install.sh | sh     # installs the CLI + points it here
treg login                            # browser sign-in (GitHub / Google / email code) — first login registers you
treg login --email you@company.com    # terminal-only alternative (emailed 6-digit code)
treg login --token <per-org-token>    # non-interactive (agents/CI)
```
Everything runs in your **active org** (first login creates a personal one). Team invites arrive by
email — see them with `treg invites`, accept with `treg accept` (or `treg org join <code>`). Switch
teams: `treg org switch <slug>`.

## Persona 1 — CONSUMER (call a tool you don't own the key for)
**You already know the upstream API. Just build the real request and prefix it.** No treg
vocabulary, no special params — use the API exactly as its own docs say:
```
<the real request>:  GET https://api.intercom.io/conversations?per_page=5
through treg:         GET {BASE}/call/https://api.intercom.io/conversations?per_page=5
                          + header:  X-Treg-Token: <your token>
```
treg resolves the tool by the upstream host, injects the credential server-side, and relays
**everything faithfully** (method, all query params, your headers, cookies, body). Your
`X-Treg-Token` is stripped before the upstream sees it. Works for GET/POST/PUT/PATCH/DELETE.

Discover what's registered: `treg tool ls` · `treg skill ls`. (CLI shorthand also exists:
`treg call <tool> <path>`, but the URL-passthrough above is the agent-native way.)

**Run a registered CLI tool** — the command-line complement to `treg call` (which proxies HTTP
APIs). `treg run <tool> -- <cli args>` runs a vendor CLI (stripe, gh, vercel, gcloud…) with the key
injected, so you never hold or log into it. Two tiers: `--local` (default, runs on this machine) ·
`--server` (runs on the registry server, key never reaches here). `treg runs` is the run audit log.
```
treg run stripe -- get /v1/balance          # local
treg run --server agentmail-cli inboxes list # server-side
```

## Persona 2 — CREATOR (turn keys + local skills into shared tools)
**Bulk (the fast path):** point treg at a directory — it detects provider keys in the `.env` AND
scans skill subdirs, then registers what you pick:
```bash
treg upload                       # both sides of the cwd; `treg upload env|skills --dir <d>` to restrict
```
**Single-credential tool (the common case):**
```bash
treg secret add posthog-key --value "$POSTHOG_API_KEY"          # or --file ./.secret/token.json
treg tool add posthog --base-url https://us.posthog.com --secret posthog-key
# query-key API instead of a bearer header:
treg tool add serpapi --base-url https://serpapi.com --secret <name-or-id> \
  --auth-in query --auth-name api_key --auth-format '{secret}'
```
**A whole skill (recipe + secrets + tool, possibly multi-credential):**
```bash
treg skill scaffold ~/.claude/skills/google-ads --out gads.json
#   -> walks the dir: captures SKILL.md as the recipe + every .secret/* as a secret.
#   -> YOU then edit gads.json: set base_url, and complete each binding (location/name/format).
#      e.g. google-ads needs TWO bindings on one request:
#        Authorization: Bearer {access_token}  (injector: oauth)
#        developer-token: {secret}             (injector: env)
treg skill push gads.json                                        # registers the bundle atomically
```
Share with a teammate: give them the endpoint + tool name. Their agent calls it with **no key**.

**Auth shapes** (per binding `injector`, = the secret's `kind`): `env` (plain string) ·
`secret_file` (JSON token file, pull `secret_field`) · `oauth` (JSON token, auto-refreshed) ·
`cli_auth` (material lifted from a CLI's keychain). Multiple bindings apply to every request.

**OAuth, two modes (treg keeps it fresh):** if the oauth secret carries `refresh_token` +
`client_id` + `client_secret`, treg **auto-refreshes** it before it expires (you never re-upload).
If it's just a bare token, that's **manual mode**, treg injects it as-is and you re-upload when it
expires. Same storage; a credential can graduate from manual to auto with no migration.

**Getting the first OAuth token, two ways (your choice):**
- **Manual:** do your own OAuth locally, then `treg secret add gsc --file token.json --kind oauth`.
- **Hosted connect:** `treg oauth connect gsc --client-secret client_secret.json --scopes <scope>`
  → prints a consent URL; you approve in the browser; treg captures the token directly.
  One-time setup: add `{BASE}/oauth/callback` to your OAuth app's redirect URIs.

## Persona 3 — ADMIN (manage teams + monitor)
```bash
treg tool ls / secret ls / skill ls / calls          # inventory + audit log — scoped to the active org
treg tool rm <id> / secret rm <id> / skill rm <id>   # secret rm is blocked while a tool binds it
treg health            # status of every credential in this org (ok | invalid | unknown)
treg health --run      # re-check now: refresh oauth tokens, probe each tool, alert owners
```
**Teams / orgs** (owner > admin > member > viewer; a member manages only what they created):
```bash
treg org create "Team A"                       # you become owner (auto-active)
treg org invite bob@company.com --role member  # admin+; emails the invite (a one-time code is the fallback)
treg org members                               # admin+; who's in the active org
treg org ls / treg org switch <slug>           # your orgs / switch active
```
The invitee signs in with the invited email and runs `treg accept` — no code handling needed
(the code path still works: `treg org join <code>`). A brand-new invitee also gets their own
**personal org** (no empty state), so removing them from a team never locks them out. Give a tool
a probe so treg can validate it: `health_check: {method, path, expect_status}` (e.g. intercom `{"path":"me"}`).

## Rules
- Secrets are **write-only** — the API never returns a stored value.
- A tool may bind a secret **someone else uploaded** (use-without-hold) — that's the point.
- The proxy doesn't understand the upstream; if a call fails, the status you see is the upstream's truth.
- More: `{BASE}/llms.txt` (agent onboarding) · `{BASE}/tutorial` (interactive walkthrough).
