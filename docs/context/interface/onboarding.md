---
title: Onboarding — the first-run demo team (dashboard + CLI)
status: shipped
sources:
  - src/treg/demo.py
  - src/treg/cli.py
  - src/treg/web/index.html
related:
  - interface/api.md
  - interface/cli.md
  - interface/dashboard.md
  - architecture/data-model.md
---

# Onboarding

A brand-new user's fastest path to *believing* treg ("call a real API with no key on your machine")
is to **do it** on a team that's already alive. So onboarding hands them a **team they own**,
seeded with teammates, a working tool, and a real audit trail — one backend brain, two faces.

## The one brain — `src/treg/demo.py`

`provision(db, owner, team_name)` seeds a REAL org owned by the caller, marked `Org.demo=True`:

- **Fake teammates** (`TEAMMATES`): Ada·admin, Ben·member, Cora·viewer — roster-only `User` rows
  with `demo=True` on the unusable domain **`demo.treg.local`** (`DEMO_DOMAIN`). Reused across demo
  orgs (email is unique); they get a Membership but **no personal org** and **cannot log in** (see
  the OTP guard below).
- **A working tool** (`echo` → `postman-echo.com`) + its `echo-key` secret, so **Try-it / `treg call`
  returns 200 with the injected `Authorization: Bearer sk-demo-…`** — the aha.
- **Sample activity** (`SAMPLE_CALLS`): a few `CallRecord`s attributed to teammates so Activity is alive.

**CLI onboarding is 3 paths** (`cmd_onboard` → `_dispatch_onboard`, one of `_run_setup`/`_run_access`/
`_run_demo`; `_PATHS = {1:setup, 2:access, 3:demo}`). A TTY run opens with a one-second `_splash` decrypt
animation (the wordmark reveals behind a ░▒▓ wavefront; any key skips; off-TTY / `NO_COLOR` / dumb
terminals never see it), then `_pick_path` presents an **arrow-key menu** (`_menu` — ↑↓/jk move, ↵ confirm,
1-9 jump-pick; where raw-key mode is unavailable it falls back to questionary). The **interactive default is
Setup**; the smart org-based default (a team with tools → **Connect**, an empty team you admin → **Setup**,
else **Demo**) applies **only non-interactively** (scripted/agent runs stay unchanged). Menu labels:
**Setup** · **Connect existing tool-registry** · **Demo**:
- **Setup** (`_run_setup`, path `setup`) — first asks **"Import skill/secret from where?"** via `_menu`:
  this project / global agent folders / both / an **other project repo** typed inline (a `_menu` type-in row
  with fish-style folder autosuggestion — → / tab accept the ghost completion). "This project" is hidden
  when the cwd is root-ish (`_is_rootish` — `/`, `$HOME`, `/Users`) so Setup can't sweep the whole account;
  a typed path that isn't a directory re-prompts via `questionary.path`. `--source local|global|both` skips
  the question; non-TTY or `--yes` never prompts and keeps the local scan, falling back to global only when
  the project has nothing to share. Global = `agents.detect_installed()` → each agent's `global_dir()`
  (`~/.claude/skills`, `~/.codex/skills`, …), kept only when it actually holds skills. Then imports the cwd
  `.env` (API keys — local scope only; global folders carry no project `.env`) then ALL chosen skill folders
  in **one deduped pass** (`_import_skills` takes a list of dirs — the cwd's top-level skills + every
  agent's project dir `.claude/skills`/`.agents/skills`/… from the `agents` registry, plus the chosen
  global dirs — deduped by skill NAME so a mirror-installed skill isn't prompted twice), with `--no-oauth`
  (no forced browser consent) and a batched `POST /health/run` (surfaces `N healthy · M unchecked (no
  probe)`), then a **✓ Done** hand-off pointing at the team's skills + secret vault (`{base}`). Missing
  skill creds are prompted, not skipped.
- **Connect existing tool-registry** (`_run_access`, path `access`) — lists the team's tools + skills,
  multi-selects skills to `skill install` (one call → one summary; kept skills surfaced), then one no-key
  test call (`_onboard_test_call`, prefers a probe/example path so it hits a REAL endpoint). Never pulls keys.
- **Demo** (`_run_demo`) — a purely **illustrative** walkthrough: **no team is created, nothing is uploaded**
  (avoids the "real data in a throwaway demo org" trap entirely). Four beats: ① `_demo_scan_preview`
  ("Auto-discover local skills & env" — read-only, "this is just a DEMO, nothing is uploaded") → ② "Share
  credentials & skills with your team" (example roles owner/admin/member/viewer) → ③ `_demo_teammate_call` —
  auto-picks ONE **real** callable tool the active team has (excludes `echo`), **displays the real upstream
  URL** (`treg call https://api.resend.com/domains`) so it's unmistakably a real API but **executes via the
  tool-name form** (reliable; the host-passthrough form can be ambiguous with duplicate hosts); Stripe
  example if none → ④ `_demo_call_log` — an illustrative ledger: the call you just made plus example
  teammates on YOUR email domain (so they read as real). The `echo` tool and the old seed-a-team flow are gone.

`provision` (full auto-seed) backs the Demo path. The dashboard first-run stepper still uses the narrower
helpers so the team is the user's own REAL org (not `demo`): `seed_tool(db, org, owner_email)` adds the
`echo` tool+secret (idempotent) for the no-key call; `accept_demo_invite(db, org_id, invite)` creates the
fake teammate (`demo=True`) and accepts a pending invite. `GUIDED_TEAMMATE` = Alex Rivera
(`alex@demo.treg.local`, member).

Idempotent — `existing_demo_org` reuses the caller's demo org instead of stacking. Marks
`owner.onboarded=True`. `reset(db, owner)` deletes every demo org the caller owns (same cascade as
`_cascade_delete_org`), drops demo-teammate memberships from the caller's REAL teams too, and sweeps
any demo user left with zero memberships — a clean exit, no litter.

## Endpoints (`api.py`, all identity/member-scoped)

- `POST /onboard/demo {team_name}` (`require_identity`) → `demo.provision` (CLI quick mode: full seed).
- `POST /onboard/seed-tool` (`require_member`, member+) → `demo.seed_tool` into the active team.
- `POST /onboard/accept-teammate {email}` (`require_member`, admin+, demo-domain only) →
  `demo.accept_demo_invite` — auto-joins the teammate the user just invited.
- `POST /onboard/skip` → sets `onboarded=True` without seeding (dismiss, don't re-offer).
- `POST /onboard/reset` → `demo.reset`.
- `GET /auth/me` returns `onboarded`; `GET /orgs` rows carry `demo`. `create_invite` **skips the Resend
  email** for `@demo.treg.local` invitees.
- **Guards:** `auth_email_start` refuses any `@demo.treg.local` email (400) — fake teammates are never
  a login. `admin_stats` excludes the whole demo footprint (demo users, demo orgs, and everything
  scoped to them) so platform totals stay honest.
- **Schema:** `User.onboarded` / `User.demo` / `Org.demo` (see [data-model](../architecture/data-model.md);
  additive migrations in `db.py`, and `_rebuild_user_table` + the legacy org backfill carry the new cols).

## CLI face (`treg onboard`)

Colourful (ANSI truecolor, Ledger palette; suppressed off-TTY / under `NO_COLOR`). `cmd_onboard` first
ensures an active org (`_pick_active_org` — the flows need an identity token so requests carry
`X-Treg-Org`; a per-org agent token is org-bound), plays `_splash` (skipped for scripted `--path`/`--yes`
runs), then routes through `_pick_path` → `_dispatch_onboard`. Slow steps (team lookup, scans, network
fetches) show a `_spinner`; `_onboard_active_org` caches its `/orgs` result in `_ORG_CACHE` so a single run
never re-fetches. Shared drawing helpers: `_section` dividers, `_brand`, `_cmd` (shows the actual command),
`_kv`, `_tip` amber asides, `_ok`.

The three paths are **Setup / Connect / Demo** (see the path descriptions above): **Setup** and **Connect**
do real work against the active team; **Demo** (`_run_demo`) is illustrative — no team is created, nothing
is uploaded — four beats (`_demo_scan_preview` → roles → `_demo_teammate_call` a real no-key call when a
callable tool exists → `_demo_call_log`), then `_demo_next_steps`.

After a first **human** `treg login`, `_maybe_offer_onboarding` prompts `[Y/n]` then `_pick_path` +
`_dispatch_onboard` — **TTY-only / CI-safe**; a decline posts `/onboard/skip` so it never re-asks.

## Dashboard face (`web/index.html`)

A **docked right-side narrative stepper** (`onb.*` state; content pushed left via `.onb-push` so nothing
overlaps). Boot reads `onboarded` from `/auth/me`; `maybeOnboard()` auto-opens it only for a non-onboarded
session with **no team yet**. The team itself is created up front by the separate **welcome** flow
(`welcomeCreate` → `POST /orgs`), which then lands the user on "Getting started" and launches the stepper
alongside it. **Five steps** (`onbSteps` = Why treg · Set up your vault · Add a teammate · Call without
keys · You're set) tell the story and have the USER do each action, with `onbGoto`/`onbNext`/`onbBack`
walking the panel to the matching page (`orgs` for the roster, `tools` for vault + calling): **Add a
teammate** (`onbInviteTeammate` prefills + invites Alex, then `/onboard/accept-teammate` auto-joins) →
**Call without keys** (`onbAddTool` → `/onboard/seed-tool`, then `onbTryEcho` opens the Try-it drawer,
shifted left via `.drawer.onb-shift` so it doesn't overlap the panel). A step tracker shows
numbered/checked progress; **↺ Restart** (`onbRestart`) and `onbFinish`/**✕** skip (posting
`/onboard/skip`). Each step's **"Read more"** deep-links to the matching tutorial panel via
`readMore(onbSection)` → `/tutorial#<concepts|skills|roles|auth>`. **Help → "Guided setup"** replays
(`replayOnboard`); **"Remove demo"** calls `resetDemo`. A clay **`demo` chip** marks a demo org.
