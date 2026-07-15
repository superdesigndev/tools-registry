---
title: Landing sandbox studio — anonymous try-it, hosted skills, CLI installer
status: shipped
sources:
  - src/treg/sandbox.py
  - src/treg/api.py
  - src/treg/web/index.html
  - src/treg/web/install.sh
related:
  - interface/dashboard.md
  - interface/onboarding.md
  - interface/api.md
---

# Landing sandbox studio

The logged-out `/` is no longer a login box — it's a **landing page with a live, no-login sandbox
studio** (the `v-if="!authed"` branch of `index.html`, `.lp` container). A visitor builds a real
mini-registry in the browser and keeps using it from their terminal, all without an account. The
engine is `src/treg/sandbox.py` + a handful of `api.py` endpoints; the front-end drives it with `sbx*`
Vue methods (`sbxInit`/`startSandbox`/`refreshSandbox`/`sbxAddSecret`/`sbxAddTool`/`runTool`).

## The throwaway team (`sandbox.py`)
`mint(db)` creates a login-free team: a `visitor-<hex>@sandbox.treg.local` `User` (can never sign in),
a `demo` `Org` slugged `sbx-<hex>`, a member `Membership` whose **token is returned** (unlike
onboarding's `demo.py`, which discards it), plus seeded starters from `DEFAULTS` — real-brand names,
**fake keys**. To keep the story clean the seed leaves **one** live endpoint on arrival:
`STRIPE_KEY`→`stripe`→api.stripe.com (an `env` `Authorization: Bearer {secret}` binding), which auto-runs
so the "no key" aha shows immediately. `POSTHOG_KEY` is seeded **vault-only** (an entry with no `tool`
key, so `mint` creates the secret but no Tool); the front-end prefills the "add your own" row with the
real PostHog API + that key, so the visitor's first action is a single **Add**. `is_sandbox(org)` =
`org.demo && _SANDBOX_SLUG_RE.match(org.slug)` — it matches the **exact mint slug format**
(`^sbx-[0-9a-f]{12}$`, i.e. `sbx-<token_hex(6)>`), NOT a loose `startswith("sbx-")`, so a real team a
user happens to name "sbx …" (slug `sbx-…`) is not misread as a sandbox. It also stays distinct from
onboarding demo teams (also `demo`, but team-named).

`is_sandbox_user(user)` is the companion check on the **visitor** (email ends in `@sandbox.treg.local`,
`SANDBOX_DOMAIN`). Such a login-free visitor may act ONLY inside its own sandbox org — it can **never
create a real team** (`POST /orgs` → `create_org` returns 403: "sign in with GitHub, Google, or email")
nor otherwise graduate to a real account. Escaping the sandbox requires a real sign-in door.

## Guided tour (the branded coach)
On page load `sbxInit`→`_sbxGreet` auto-starts a coach walkthrough (both fresh + reused-sandbox paths;
no once-per-visitor gate). A **coach-mark** anchored above each target element (`sbxTourPlace` positions
it via `getBoundingClientRect`, re-anchors on scroll/resize) types out (`_sbxType`) a message and moves
through 5 steps: vault → workspace (endpoint tab) → workspace (skills tab) → result → curl, flipping
`demo.view` per step. The selected element gets a **teal** spotlight ring (`.tour-spot`) while the coach
stays **orange** (deliberately complementary, not all-orange). Skippable (✕ / Skip / ← Back); state in
`demo.tour`.

The visitor holds that token and calls the **same product endpoints** the dashboard does —
`POST /secrets`, `POST /tools`, `/call/*` — so it is a genuine registry, not a mock.

## Safety: sandbox calls never touch the network
`call_tool` in `api.py` checks `demo_sandbox.is_sandbox(caller.org)` and, for a sandbox, short-circuits to
`sandbox.synthesize(...)` instead of `relay()`. `synthesize` runs the **real** `injectors.inject` to
compute exactly what treg would send upstream (the injected header/query), then returns a **labelled
dummy** response — brand-shaped via `SAMPLE_BODIES` (Stripe charge list / PostHog events). So the
injected credential shown is 100% real, but no outbound request is ever made: no SSRF, no open relay,
regardless of the (arbitrary) base_url the visitor typed. Org-scoped tool resolution already prevents a
sandbox token from reaching any tool it didn't register.

Bounds: `MAX_TOOLS`/`MAX_SECRETS` (3) enforced by `_enforce_sandbox_cap` on `POST /tools|/secrets`;
`SANDBOX_TTL_MIN` (60) + `gc(db)` reaps expired visitors (their org + all org-scoped rows), run
opportunistically on each mint; a per-IP in-memory limiter (`_SANDBOX_HITS`, `SANDBOX_RATE_MAX`, via the
shared `_rate_limit`/`_rate_sweep` sliding window — which now also evicts cold IP keys so the map can't
grow unbounded) guards `POST /demo/sandbox` (`demo_sandbox_mint`). The browser reuses one sandbox across reloads via
`localStorage['treg-sbx']`. **Skill import is disabled in a sandbox org** — `POST /skills` (register),
`POST /skills/analyze`, and `POST /skills/import` all check `is_sandbox(caller.org)` and 403 ("skill
import is disabled in the sandbox"), because a skill package could register unlimited tools/secrets past
the `MAX_TOOLS`/`MAX_SECRETS` cap.

`export_skill(db, org)` → `GET /demo/sandbox/skill` turns whatever the visitor built into a shareable
skill manifest (treg.json + SKILL.md, secret values redacted to placeholders).

## Hosted sample skills + the "Run in Claude Code" flow
`SAMPLE_SKILLS` (`posthog-insights`, `stripe-billing`) each mirror a seeded tool so an installed skill's
proxied calls resolve against the visitor's sandbox. `skill_files(name, base, token)` builds the three
files a skill folder is — `SKILL.md` (agent recipe: call the treg proxy, key injected server-side),
`treg.json` (wiring: base_url + which secret by name), `.secret` (empty; value stays in the vault).
- `GET /skills/samples` (`skill_samples`) — public JSON of each sample + its files (for the landing).
- `GET /skills/{name}/install.sh?token=` (`skill_install`) — `install_script(name, base, token)` emits a
  POSIX `sh` that `mkdir -p ./.claude/skills/<name>` and writes the files (token baked in via quoted
  heredocs), so `curl … | sh` from a project dir installs the skill for Claude Code to load. Its recipe
  calls `{BASE}/call/…` with the caller's treg token — the API key never lands on the machine. The
  `token` query param is **charset-restricted** (`re.fullmatch(r"[A-Za-z0-9_\-]{1,200}")`, else 422)
  because it is interpolated into that shell script — a crafted value can't inject a newline + extra
  commands into the generated `curl … | sh`.

## CLI installer
`GET /install.sh` (`install_sh`) serves `src/treg/web/install.sh`, `{BASE}`-templated like `llms.txt`
(so it targets whatever host is live — ngrok now, the real domain after deploy). The script installs the
`treg` CLI via `uv tool install` → `pipx` → `pip3` and runs `treg config --base-url {BASE}`.
**Caveat:** the repo is private and the package is not on PyPI, so `curl … /install.sh | sh` works today
for teammates with repo access (git creds); a fully public install needs a PyPI publish (or a
public repo) — a launch step to fold into the Render deploy. The **Getting started** dashboard view
(`view==='start'`) surfaces this install command + `treg login`/`onboard`/`add`/`call` and links to the
tutorial and `/llms.txt`; `llms.txt` gained a matching **Install the CLI** section.

## Not yet
Publish `treg` to PyPI (or make the repo public / serve a slim wheel) so the installer is public; wire
the landing's sign-up CTAs through to the real account flow post-launch.
