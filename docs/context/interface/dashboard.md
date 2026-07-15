---
title: The web dashboard (Ledger, served from FastAPI)
status: shipped
sources:
  - src/treg/web/index.html
  - src/treg/web/tutorial.js
  - src/treg/web/tutorial.html
  - src/treg/web/tour/tour.js
  - src/treg/web/tour/index.html
  - src/treg/api.py
  - src/treg/session.py
related:
  - interface/api.md
  - interface/landing-sandbox.md
  - architecture/super-admin.md
  - architecture/multi-tenancy.md
---

# Web dashboard (Phase 1)

A single-file Vue 3 (CDN) dashboard in `src/treg/web/index.html`, served **same-origin** by the API
(`GET /` → `FileResponse`, `dashboard()` in `api.py`, via `_WEB_DIR`). Same origin = no CORS and it
ships with the server (Render/Fly). Design language: **Ledger** (warm charcoal + clay accent,
mono-forward, dark default + light toggle) — see `docs/style-board.html` / `docs/DASHBOARD-PLAN.md`.

## Shell & design system (2026 rework)
The design tokens are now **shared across every served page** (`index.html`, `tutorial.html`,
`tour/index.html`): **system mono** (`ui-monospace, "SF Mono", …` — `IBM Plex Mono` was never actually
loaded, so this makes rendering consistent for everyone), `--r:14 / --rb:9`, a `14px` base, and one
shared `.btn` / `.iconbtn` height so controls align. The logged-out `/` is now the **landing + sandbox
studio** (see [landing-sandbox](landing-sandbox.md)), not a login box; sign-in is a modal.

The **authed** shell is sidebar-first. The **top bar** is just brand + search. The **left sidebar**
stacks: (top) an **org block** — role + team name — that on click opens a switcher **dropdown** where
each team carries its own **⚙ Settings** (`orgSettings` → switch into it, then open its settings) and
**Switch** (`switchTo`) button (long names truncate, actions pinned right; the click-outside handler
keys on `.orgblock`); (middle) the nav — Tools · Activity · **Usage** (admin/owner) · Team · Getting
started · Tutorial · Admin; (bottom)
the **account** block — avatar · email · theme · sign out. The old top-bar org dropdown and top-right
account controls are gone.

## Auth — three doors
Two are **session** (cookie) paths, one is a token fallback:
- **GitHub (`githubLogin`):** `Continue with GitHub` → `/auth/github` → callback sets a signed HttpOnly
  cookie (`session.py` HMAC). (Note: the button routes through a `githubLogin()` method — a Vue template
  expression can't reference the `location` global.)
- **Google (`googleLogin`):** `Continue with Google` → `/auth/google` → callback, same cookie session as
  GitHub. The button shows when `/meta` reports `google:true`.
- **Email one-time code (`emailStart`/`emailVerify`):** enter email → `POST /auth/email/start` (dev code
  shown inline) → enter the code → `/auth/email/verify`, which sets the **same session cookie**, so the
  dashboard just `location.reload()`s into session mode — identical to GitHub.
- Either way the dashboard authenticates with the **cookie** (`credentials:'include'`) + picks the org
  with **`X-Treg-Org`**, detected via `/auth/me` on load. Cookie `Secure` only on HTTPS (`_is_https`).
  For copy-paste convenience it also fetches `GET /auth/cli-token` on load into `myToken` (a minted
  identity token) — the per-tool snippets embed it + `X-Treg-Org` so a copied curl runs as-is, and a
  **"Copy API token"** button (`copyToken`) puts it on the clipboard.
- **"For your agents" sidebar** — copyable **agent instructions** (a prompt to paste into Claude Code /
  Codex), built client-side by `buildAgentPrompt(kind, inclToken)` with the caller's minted token +
  active org slug baked in. Two compact copy-rows: **Setup instruction** (`kind:'admin'`, shown only to
  `canAdmin`) — install the CLI → `treg login --token` + `org use` → read `/llms.txt` → `treg upload
  skills`/`import env` (dry-run first) → `treg health --run`; and **Access instruction** (`kind:'consumer'`,
  everyone) — install → auth → `treg skill install --all` → `treg tool ls` + a test call. The row's ⧉
  (`copyAgentGuide`) copies with the token embedded (paste-and-run); clicking the label opens a preview
  modal (`agentGuide`) with an **Embed my token** toggle (`agentInclToken`) → off yields a
  `<YOUR_TOKEN>` placeholder that's safe to share. Plus the **API token** copy-row (`copyToken`).
- **Token fallback (agents/CLI):** paste one `X-Treg-Token` **per org** into `localStorage` (`treg-dash`).

On load, `loadAll` fetches `/invites/mine` and shows an **invite banner** (`acceptInvite` → `POST
/invites/{id}/accept`). The Organizations view also has **⤷ Join by code** (`joinByCode` → `POST
/invites/accept {code, email:me}`) for a code handed over out-of-band. On first load it lands on the org
with the **most tools** (from `/orgs`' `tool_count`); a tie / all-empty falls back to a **team** org over
the (usually empty) personal one (`isPersonal(o)` = org name == your email) — so imports living in the
personal space are no longer hidden behind an empty team. It tags the personal org `personal`, and the
empty Tools state offers `jumpToTeam()`.

The session cookie is HMAC-signed with `TREG_SESSION_SECRET`/`TREG_SECRET_KEY`, falling back to a
**random per-process key** when neither is set (never a source-visible constant — that would make
cookies forgeable for any uid, incl. a superadmin). It also carries a **`tv` (token_version)** claim
(`sess.make`/`read_claims`); a mismatch against `User.token_version` = revoked, so `POST
/auth/revoke-tokens` can invalidate a user's cookies + CLI tokens without rotating the shared secret. In **token mode** the dashboard carries `org_id`
on the active org (so `activeOrgId` resolves and org-admin writes work), fetches `me` via `/auth/me`
(so `isPersonal` + join-by-code work), and persists a newly-created org's returned token so the team is
enterable; leave/delete forget the active org in both modes. On an org switch, `loadAll` refreshes the
open Secrets panel + Activity log too (was showing the previous org's). Copy buttons fall back to
`execCommand` and only claim success on success; the app shell has a mobile breakpoint.

Server side (`api.py`): `require_identity` (who, from token OR session), `require_member` (a Caller in a
specific org — token bakes the org in; a session picks it via `X-Treg-Org`), and `require_superadmin`
(env token, or a token/session whose user `is_superadmin`). Every fetch also sends
`ngrok-skip-browser-warning: 1`.

## Screens (all read, plus try-it) — wired to existing endpoints
- **Tools** — `GET /tools` + `GET /bundles` + `GET /health`, rendered as a **segmented tabular home**
  (`toolTab`: All / Endpoints / Skills / Recipes): **Endpoints** = tools with no `bundle_id` (registered
  directly), **Skills** = tools *with* a `bundle_id` (came from a skill package, carry a recipe),
  **Recipes** = bundles with no tool. Per-tool **Copy** (syntax-highlighted snippet builder — cURL / CLI /
  Claude Code / Python / Node, cURL default; embeds the real token via a `TREG_API_TOKEN` var + shows it
  ellipsized; `samplePath(host)`/health-check/example fill a runnable `PATH`) and **Try it** (a real
  `* /call/<tool>/<path>`). **Recipes** get their own actions: an **Install** modal (cURL/CLI/Claude Code —
  install, not call), a **view/edit** modal (`openRecipeView` → `PATCH /bundles/{id}` to save the
  SKILL.md; creator/admin only), and delete. On first load the app lands on the org with the most tools
  (`/orgs`' `tool_count`; tie → a team). A tool that carries a local-run profile (`t.cli`) shows a
  **`⌘ run` chip** and a toggle button; `toggleLocalRun` flips `cli.enabled` via `PATCH /tools/{id}` and,
  on enable, shows a dismissible **restricted-key reminder** (the key reaches members' machines during a
  run — see [local-run](../architecture/local-run.md)). A **run-tier chip** shows where it runs:
  **server** (`t.server_runnable` — the key is injected server-side, never on a member's machine) or
  **local-only** (a `config_file`/`device` CLI that authenticates from the member's own machine).
- **Add a skill** — a **folder importer** (`<input webkitdirectory>`): reads the picked folder's files
  client-side → `POST /skills/analyze` classifies each (recipe/tool/needs-creds, the CLI's own scanner) →
  a preview with fill-in for any missing secret → `POST /skills/import` registers the selected. The preview
  also shows a **local-run line** per skill (`skillCliNote`): contract-declared, catalog-known (available
  once an owner enables it), or explicitly unsupported with the reason. The raw JSON payload is an
  "Advanced" fallback. `api()` retries a WAF-blocked body base64-encoded (see WAF note).
- **Secrets** — its own sidebar view (`view==='secrets'`, `canRegister` only): a table of secrets (kind
  chip + owner + delete) + a multi-row add form (`secretRows`) whose kind select includes **`param`** (a
  non-secret value like a project id, shown in clear text with a helper line); `GET`/`POST`/`DELETE
  /secrets`. Pasting a whole `.env` into the name field splits it into editable rows **client-side**
  (Render/Vercel-style — `pasteEnv` → `parseEnvText`: comments/blanks skipped, `export ` stripped, one
  balanced quote pair removed). A single-line `NAME=value` splits only in the *name* field — a value
  containing `=` (base64 pad, connection strings) pastes untouched into the value field; multi-line
  splits from either field.
- **Team settings** (`view==='orgs'`) — the **active** team's settings, not an all-teams list (switching
  now lives in the sidebar dropdown). Renders `Manage {activeName}` — members / invite / pending / danger
  zone for admins (`canAdmin`); a **personal** org shows a focused page with the invite + danger blocks
  hidden. Reached via the sidebar org-dropdown ⚙.
- **Getting started** (`view==='start'`, under Help) — the CLI path: install (`{BASE}/install.sh`),
  `treg login`, `treg onboard`, register-a-tool + no-key call, and links to the interactive tutorial +
  `/llms.txt`. Per-block copy via `copyStart`.
- **Activity** — one time-sorted feed (`activityRows`) merging `GET /calls` (proxy calls) + `GET /runs`
  (CLI executions). Local runs now arrive via `/runs` (tagged `where`), so the calls feed **excludes**
  `local_run` rows to avoid double-counting, and each run row shows a **local/server** chip.
- **Admin** — nav auto-appears iff the caller is `is_superadmin` (read from `/auth/me` on boot — **not**
  by probing `/admin/stats`, which would 403 + log a console error on every load for normal users); `loadAdmin` fetches `stats` + `orgs` + `users` only when you open the panel. Shows `stats` + `orgs` + `users`, each
  with **mutations** (`_adm` helper): `admGrant`/`admSuspendUser`/`admDeleteUser`,
  `admSuspendOrg`/`admDeleteOrg` (inline-confirm deletes). Self-actions are hidden for the current user
  (`u.email===me`) to prevent lockout.
- **First-run onboarding** — a brand-new user has **zero teams** (no auto personal org), so `maybeOnboard`
  shows a **mandatory "name your team" welcome** (`welcome.*`; team name pre-suggested from the email
  domain via `_suggestTeamName`). It is NOT dismissable — no skip, survives Escape/backdrop — the only
  action is `welcomeCreate` (`POST /orgs`, marks onboarded, lands on Secrets with a hint). **Exception —
  an invited user**: `maybeOnboard` checks `pendingInvites` first and, if any, shows a **multi-select
  accept-invite modal** (`inviteChoice` / `openInviteChoice` seeds `inviteSel` with ALL invites checked;
  `sortedInvites` puts the clicked link's team — `inviteLinkOrg` — first) — "Accept & join N teams →" →
  `acceptSelectedInvites` (loops `POST /invites/{id}/accept`, partial failures land in `inviteErr`,
  switches into the linked/first joined team, lands on Tools with a "You joined X, Y" notice) or
  `declineInvite` → the welcome modal on first run ("Create my own team instead"), plain "Not now"
  otherwise. The modal ALSO opens for an already-onboarded user when an invite link lands
  (`?invite_org=` set by the email link's POST — a second-team invite must surface too); a dead
  `invite_org` (already used/revoked) shows an `orgMsg` banner instead. Critical ordering: `loadAll`
  fetches `/invites/mine` **before** its `!myOrgs.length` early-return — an invited user has zero orgs,
  so the old order skipped the invite fetch and forced create-team. The **invite email link signs the
  invitee in** — `GET /auth/invite-signin?t=<email_token>` (`email.send_invite`; the token is an
  inbox-only second secret, split from the admin-visible code): the GET shows a POST-confirm page
  ("Continue as {email} →"), the POST mints the session (one-time, consumes the token) and 303s to
  `/?invite_org=<org_id>`; boot strips the one-shot params (`history.replaceState`) and stashes
  `inviteLinkOrg`. Legacy `?code=` links never mint a session — they 303 to `/?invite=<email>` for a
  normal login (boot prefills `emailInput` + opens the sign-in modal); the invitee proves the email at a
  real door and the invite auto-appears via `/invites/mine` (newest-first). Invalid/expired →
  `/?invite_expired=1`. Neither path consumes the invite (still `pending`); the accept modal does. `loadAll`
  short-circuits the org-scoped fetches while `myOrgs` is empty so no error banner flashes behind it. The
  old demo/guided stepper (`onb.*`) is now opt-in, replayable from Help. A `demo` chip marks a sandbox org.
- **Help → Tutorial** — the full interactive walkthrough, rendered natively (Vue) from the shared
  `window.TREG_TUTORIAL` data (`tutGo`/`tutHL`/`tutCopy`, syntax-highlighted command + output blocks,
  persona chips, and four toggle panels — **Concepts · Roles · Auth shapes · Skills**). The standalone
  `/tutorial` mirrors it and opens a panel from the URL hash (`/tutorial#auth`, `#skills`); the onboarding
  stepper deep-links each step's "Read more" there via `readMore(onbSection)`. See below.

## The tutorial (one source, two renderers)
`src/treg/web/tutorial.js` is the **single source of truth**: `window.TREG_TUTORIAL` (`concepts`, `roles`,
`personas`, `steps[]` = `{part,who,title,explain,cmd,out,notice}`, plus the two focused arrays
`importShell[]` and `access[]`, same step shape) + a self-contained `tregHL(text,lang)` shell/json
highlighter. It's served at `/tutorial.js` and consumed by **both** the dashboard Help view (native Vue
render) and the **standalone** `src/treg/web/tutorial.html` (vanilla render, served at `/tutorial`;
renders `steps` only) — so they can never drift. `docs/tutorial.html` is now a redirect to `/tutorial`;
the prose walkthrough is `docs/TUTORIAL.md`. Editing steps means editing `tutorial.js` only.

**Two focused tutorials as cards** — **Import & shell** (`importShell`, auto-import + shell mode + the
local-run sandbox) and **Team access control** (`access`, per-member tool access + the local-run dial)
are cards on the Help → Tutorial chooser, rendered by **one shared stepper template** in `index.html`
(`helpMode === 'import-shell' || 'access'`), with its own `xtut*`-prefixed state/computed/method names
(`xtut.i`, `xtutSteps`, `xtutStep`, `xtutTitle`, `xtutGo`) so they never collide with the CLI tutorial's
`tut*` names. Two extra persona chips: `you` (green) and `sam` (amber). Each also has a **prose twin**
served as markdown: `web/tutorial-import-shell.md` at `/tutorial-import-shell.md` and
`web/tutorial-access.md` at `/tutorial-access.md` (both `_serve_md`, `{BASE}`-templated) — kept as the
agent-friendly versions; the main tutorial (`tutorial.md` + `docs/TUTORIAL.md`) links them near the top
and `tutorial.js` ends with a "Further tutorials" step pointing at the cards + URLs.

**Dashboard tour** (the web-UI walkthrough — screenshots, not commands): the **Help** nav (`◫ Tutorial`)
opens a **chooser** (`helpMode` = `cli` | `dashboard` | `import-shell` | `access`, plus the Guided-setup
replay) with five cards; the dashboard card renders a native
stepper (`tourGo` via `tourI`, `personaTour`, per-Part `tourMatColor` mats) from **`window.TREG_TOUR`**
(`src/treg/web/tour/tour.js`, one source shared with the standalone page). WebP images live at
`src/treg/web/tour/img/` and are served via a `StaticFiles(html=True)` mount at **`/dashboard-tour/`**
(which also serves the shareable standalone `tour/index.html`). Images are generated by
`docs/dash-tour/capture.py` (Playwright drives the live dashboard as tom/bob/alice via session-cookie
login → WebP); prose mirror is `docs/DASHBOARD-TOUR.md`. The dashboard shell is served with
`Cache-Control: no-cache` so UI edits show on a plain reload.

## Write UI — Phase 2a shipped (org lifecycle)
The **Organizations** view is a management surface (all endpoints already existed; this is pure
front-end). `+ New team` → `createOrg`; for the active non-personal org a **Manage** panel (visible to
admin+ via `canAdmin`) shows `loadOrgAdmin` (members + pending invites), with `sendInvite` (client-side
email-format guard before POST; the `admin` role option is owner-only, mirroring the server rule that
only owners invite admins), `setRole` (owner-only dropdown), `removeMember`, `revokeInvite`, and a danger zone
(`leaveOrg`, `deleteOrg` — confirm-by-name). Destructive actions use **inline** two-step confirms
(`confirmRemove`/`confirmLeave`/`confirmDel`), never native `confirm()`. `loadOrgAdmin` refreshes on
`go('orgs')` + after each switch. The members table also shows each member's **`used_today`** + an inline
**Daily cap** editor (`setCap` → `PATCH …/members/{id}/cap`; `-1` = unlimited), and every member (not just
admins) sees a **"Your usage today: N / cap"** line from `loadMyUsage` (`GET /usage/me`) when a cap is set.
The members table also carries the **per-member tool access control**: a **Tools** cell (`All` chip, or
`N tools ▾` opening an inline checklist of every org tool — `openAccess`/`saveAccess` → `PATCH …/members/
{id}/access`; all-checked collapses to `null` = all) and a **Local run** on/off toggle (`setLocalRun`,
preserving the member's current `tool_access`); the **owner** row's controls are disabled (never
restricted). The **invite** flow adds **All tools / Customize** (a pre-checked checklist via
`openInviteCustomize`) + a **Local runs allowed** toggle, sent as `tool_access`/`local_run_enabled` on the
invite. `saveTool` calls `remindCustomizedAccess` after a *create* — if any member has a customized
selection, a `tut-notice` **toast** reminds the owner the new tool won't reach them (explicit allow-list).

**Usage view** (`view==='usage'`, admin/owner only): `loadUsage` (`GET /orgs/{id}/usage?days=`, a 7/30/90
selector) renders a totals stat-grid, a **by-member** table with the call/local/server split, **top
tools**, and a **per-day** table — the visibility half of usage-metering v1. Refreshes on `go('usage')` +
after a switch; `usage` is in the `popstate` allow-list.

Every view switch runs `resetConfirms()` first (via `go(v)`), so a half-armed inline "click again to
delete" state can't survive into the next view and cause an accidental delete. It now also clears
`confirmDelBundle` (recipe delete) — that one was missing, so navigating away with a recipe delete armed
could delete it on the next matching click. Browser **Back/Forward** (`popstate`) navigates *between*
dashboard views rather than leaving the app; its allow-list now includes the **`secrets`** and
**`start`** (Getting started) views too, so those are reachable by Back/Forward like the rest.

## Write UI — Phase 2b shipped (resource registration)
The **Tools** view registers resources (members+ via `canRegister`; viewers can't). The **Secrets** view
(own sidebar tab) — `loadSecrets` (values never shown) + `addSecrets` (posts each filled `secretRows` row,
per-name errors, `encode:true` body for the edge WAF) + `deleteSecret` (surfaces the 409
bound-secret guard). Both surface their errors on the **Secrets** view via a dedicated `secretErr` banner
(they used to write `toolErr`, which only shows on the Tools view, so a secret failure was silent while
on Secrets). `+ Add tool` / the ✎ row button open one modal (`openAddTool`/`openEditTool` → `saveTool`) — name (locked
on edit) + base_url + a **multi-binding builder**: `tForm.bindings[]` of `{secret_id, injector, location,
name, format, secret_field}` with `addBinding`/`removeBinding`, each carrying a secret picker + placement
(header/query) + `{secret}` format. Create → `POST /tools` (bindings list); edit → `PATCH /tools/{id}`
(base_url + bindings). Tool cards get an inline-confirm delete (`deleteTool`). Delete methods clear the
error banner on success. `+ Skill` (`openAddSkill`/`addSkill`) registers a **bundle** via a pasted
`/skills` payload (recipe + inline-value secrets + tools; bindings reference a secret by `local_name`),
with client-side JSON validation.

## Not yet
OAuth-connect in-browser (the hosted consent + poll flow, `/oauth/*`). Everything else in DASHBOARD-PLAN
(org lifecycle, resource registration incl. multi-binding + edit, skill bundles, super-admin mutations)
has shipped. Packaging: `src/treg/web` lives inside the `treg` package, so the wheel's `packages`
inclusion ships every asset (incl. `tutorial.js`/`tutorial.html`) — no `force-include` (a redundant
one double-adds each file and breaks the wheel build).
