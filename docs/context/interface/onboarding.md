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

**CLI onboarding is 3 paths** (`cmd_onboard` → `_pick_path`, `_PATHS = {1:setup, 2:access, 3:demo}`; smart
default from the active org: a team with tools → **Connect**, an empty team you admin → **Setup**, else
**Demo**). Menu labels: **Setup** · **Connect existing tool-registry** · **Demo**:
- **Setup** (`_run_setup`, path `setup`) — first asks **"Import from where?"** (this project / global agent
  folders / both; `--source local|global|both` skips the question; non-TTY or `--yes` never prompts and
  keeps the local scan, falling back to global only when the project has nothing to share). Global =
  `agents.detect_installed()` → each agent's `global_dir()` (`~/.claude/skills`, `~/.codex/skills`, …),
  kept only when it actually holds skills. Then imports the cwd `.env` (API keys — local scope only; global
  folders carry no project `.env`) then ALL chosen skill folders in **one deduped pass** (`_import_skills`
  takes a list of dirs — the cwd's top-level skills + every agent's project dir
  `.claude/skills`/`.agents/skills`/… from the `agents` registry, plus the chosen global dirs — deduped by
  skill NAME so a mirror-installed skill isn't prompted twice), with `--no-oauth` (no forced browser consent), a batched
  `POST /health/run` (surfaces `N healthy · M unchecked (no probe)`), then a **✓ Done** hand-off: the
  dashboard team URL (`{base}/#orgs`), the invite command, and the teammate's install snippet (framed as
  *their* machine, "pick Connect"). Missing skill creds are prompted, not skipped.
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

Colourful (ANSI truecolor, Ledger palette; suppressed off-TTY / under `NO_COLOR`). `_pick_mode` offers
**both** styles (`--mode` skips the prompt):
- **`quick`** → `_run_quick`: `POST /onboard/demo` (full auto-seed), then walk ① roster → ② the no-key
  call → ③ the audit log.
- **`guided`** → `_run_guided`: a **detailed 11-section tutorial** that *shows and explains the actual
  command* at each step (helpers `_section` dividers, `_cmd`, `_tip` amber asides; Ledger colours —
  clay headers/commands, teal values/token, green ✓, amber tips): ① create team (`treg org create`,
  `org ls`/`use`) → ② roles + invite (`org invite`, `members`, `set-role`) → ③ register a tool
  (`secret add`, `tool add --base-url --secret`, bindings) → ④ the no-key call + URL-passthrough →
  ⑤ skills (folder + `treg.json`, `skill init/add`) → ⑥ audit → ⑦ other auth shapes (`oauth connect`,
  file creds, multi-`--bind`) → ⑧ health (`health --run`) → ⑨ manage (`tool/secret ls/rm/update`) →
  ⑩ team admin (`org invites/revoke/leave/delete`, `admin`) → ⑪ point an agent at it (URL-passthrough
  curl). The team + teammate are built for real via `POST /orgs` + `/onboard/accept-teammate`.

After a first **human** `treg login`, `_maybe_offer_onboarding` prompts `[Y/n]` then `_pick_mode` —
**TTY-only / CI-safe**; a decline posts `/onboard/skip`. `_run_onboarding` sets the active org first so
requests carry `X-Treg-Org` (the flows need an identity token — a per-org agent token is org-bound).

## Dashboard face (`web/index.html`)

A **docked right-side narrative stepper** (`onb.*` state; content pushed left via `.onb-push` so nothing
overlaps). Boot reads `onboarded` from `/auth/me`; `maybeOnboard()` auto-opens it only for a non-onboarded
session with **no team yet**. Seven steps (`onbSteps`) tell the story and have the USER do each action:
Why → Personal space → **Create a team** (`onbCreateTeam` → `POST /orgs`) → Active team → **Add a teammate**
(`onbInviteTeammate` prefills + invites Alex, then `/onboard/accept-teammate` auto-joins) → **Call with no
key** (`onbAddTool` → `/onboard/seed-tool`, then `onbTryEcho` opens the Try-it drawer, shifted left via
`.drawer.onb-shift` so it doesn't overlap the panel) → You're set. A step tracker shows numbered/checked
progress; **↺ Restart** (`onbRestart`) and the **✕** skip. Each step's **"Read more"** deep-links to the
matching tutorial panel via `readMore(onbSection)` → `/tutorial#roles|auth|skills`. **Help → "Guided setup"**
replays (`replayOnboard`); **"Remove demo"** calls `resetDemo`. A clay **`demo` chip** marks a demo org.
