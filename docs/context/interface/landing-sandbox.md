---
title: Landing sandbox studio — anonymous try-it, hosted skills, CLI installer
status: shipped
sources:
  - src/treg/sandbox.py
  - src/treg/pubfeed.py
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
**fake keys**. To keep the story clean the seed leaves **one** working endpoint on arrival:
`STRIPE_KEY`→`stripe`, whose base is pinned to the full charges resource
(`https://api.stripe.com/v1/charges`, host `api.stripe.com`, an `env` `Authorization: Bearer {secret}`
binding), which auto-runs so the "no key" aha shows immediately. This exact seeded tool is also the
**live wire** (see below): when the server is configured for it, a call to it is the sandbox's one real
upstream request. `POSTHOG_KEY` is seeded **vault-only** (an entry with no `tool`
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

## Safety: sandbox calls never touch the network (except the one live wire)
`call_tool` in `api.py` checks `demo_sandbox.is_sandbox(caller.org)` and, for a sandbox, short-circuits to
`sandbox.synthesize(...)` instead of `relay()`. `synthesize` runs the **real** `injectors.inject` to
compute exactly what treg would send upstream (the injected header/query), then returns a **labelled
dummy** response — brand-shaped via `SAMPLE_BODIES` (Stripe charge list / PostHog events). So the
injected credential shown is 100% real, but no outbound request is ever made: no SSRF, no open relay,
regardless of the (arbitrary) base_url the visitor typed. Org-scoped tool resolution already prevents a
sandbox token from reaching any tool it didn't register.

## The one live wire (real Stripe test charges)
There is a single deliberate exception, gated on env `TREG_DEMO_STRIPE_KEY` (`settings.demo_stripe_key`,
a Stripe **restricted test key** limited to Charges). When it is set, a sandbox call to the exact seeded
stripe tool relays for real. `call_tool` matches the tool with `demo_sandbox.is_live_tool(tool)` — a strict
fingerprint (`LIVE_HOST == "api.stripe.com"` and `base_url.rstrip("/") == LIVE_BASE
== "https://api.stripe.com/v1/charges"`) — and, for `GET`/`POST` only, calls `_relay_live_demo(...)`. That
helper is intentionally narrower than `relay()`: form-encoded only, the `Authorization: Bearer` header is
built from the **env key** (never from any sandbox secret), and `metadata[visitor]` in the POST body is
**stripped and re-set server-side** to `demo_sandbox.visitor_name(org.slug)` so the identity on the public
feed is always ours. Because the match is exact, editing the tool (base_url, bindings, a lookalike) makes it
stop matching and **fall through to `synthesize`** — there is no key in the sandbox org to exfiltrate.
Two guards keep the demo intact: `_require_not_live_demo_tool` / `_require_not_live_demo_secret` refuse edits
or deletes of the seeded `stripe` tool and its `STRIPE_KEY` while the wire is on (visitor-created tools stay
fully editable). `visitor_name` and `is_live_tool` live in `sandbox.py`; the wordlists (`ADJECTIVES`/
`ANIMALS`) are imported from `pubfeed.py` (leaf module, no import cycle). `mint()` returns the visitor name;
`POST /demo/sandbox` adds `"live"` and `GET /demo/sandbox/live` (`demo_sandbox_live`) reports `{live, visitor}`
for a reused sandbox (the browser keeps one across reloads via `localStorage`, so it may predate the mint that
carried these facts). The front-end live pane (`liveSnippets`, the `SBX` state) shows the visitor a copyable
`curl` that hits their OWN sandbox token.

## The public payments feed (`pubfeed.py`)
`pubfeed.py` is the landing page's **live payments ticker**: a stranger's live-wire charge appears on the
page within seconds, no refresh, as skeptic-proof that the proxy really injected a real key. The path:
visitor `curl` → live wire relays a Stripe test charge → Stripe fires `charge.succeeded` at
`POST /stripe/webhook` (`stripe_webhook`) → `pubfeed.push_charge(...)` → `GET /landing/stripe-feed`
(`landing_stripe_feed`) streams it over Server-Sent Events via `pubfeed.stream()`. The webhook verifies the
`Stripe-Signature` with `pubfeed.verify_signature` (constant-time HMAC-SHA256 over `{t}.{body}`, timestamps
older than `SIG_TOLERANCE_S` rejected as replays, any of several `v1` signatures accepted during rotation);
it returns **404 when `TREG_DEMO_STRIPE_WEBHOOK_SECRET` (`settings.demo_stripe_webhook_secret`) is unset**, so
a deploy without the secret exposes no unauthenticated POST surface. Design points:
- **In-memory + tiny.** A `deque(maxlen=FEED_MAX)` ring buffer plus a set of per-subscriber `asyncio.Queue`s;
  it is a marketing surface, not a system of record. A dropped event on restart costs nothing (Stripe retries),
  and each instance of a multi-instance deploy streams only the deliveries its own webhook received.
- **No visitor-controlled text can reach the page.** `push_charge` copies **only server-chosen fields**
  (amount/currency/created, a 6-char `id_suffix`, and a `receipt_url` only if it starts with
  `https://pay.stripe.com/`) — never `description`. The display **name** (`_display_name`) is accepted only when
  it passes `_is_wordlist_name` (adjective-animal-nnn, both words from the exact `ADJECTIVES`/`ANIMALS` lists,
  number ≤ 999); anything else falls back to `_derived_name`, a deterministic wordlist name hashed from the
  charge id. This is the "graffiti lesson": hand-typed strings can never deface the shared feed.
- **`stream()`** replays the ring buffer to a fresh subscriber, then live events, with a `: ping` keepalive every
  `KEEPALIVE_S`; a subscriber that lags past `_MAX_SUBSCRIBER_LAG` is dropped rather than buffered forever. The
  SSE response sets `X-Accel-Buffering: no` so a reverse proxy does not buffer it. `reset()` is a test hook.

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
(so it targets whatever host is live — a dev box or the real domain). The script now installs from
**PyPI** (`SRC="tools-registry"`, the light CLI package; the FastAPI/DB server stack is the separate
`tools-registry[server]` extra for self-hosters) via `uv tool install --force` → `pipx install --force` →
`pip3 install --user --upgrade`, then runs `treg config --base-url {BASE}`. It also installs the official
**tools-registry skill** into every detected agent via `treg skill bootstrap` (Claude Code, Cursor, Codex,
Gemini, Copilot, OpenCode, Windsurf …), falling back on older CLIs to a Claude-only drop that curls
`{BASE}/skill.md` into `~/.claude/skills/tools-registry`. Because the package is public on PyPI,
`curl … /install.sh | sh` now works for anyone with no repo/git access needed. The **Getting started**
dashboard view (`view==='start'`) surfaces this install command + `treg login`/`onboard`/`add`/`call` and
links to the tutorial and `/llms.txt`; `llms.txt` has a matching **Install the CLI** section.

## Not yet
Wire the landing's sign-up CTAs through to the real account flow post-launch.
