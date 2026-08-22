---
title: The web dashboard (Ledger, served from FastAPI)
status: shipped
sources:
  - src/treg/web/index.html
  - src/treg/web/vendor/README.md
  - src/treg/web/vendor/vue-3.5.41.global.prod.js
  - src/treg/web/tutorial.js
  - src/treg/web/tutorial.html
  - src/treg/web/tour/tour.js
  - src/treg/web/tour/index.html
  - src/treg/api.py
  - src/treg/session.py
related:
  - interface/api.md
  - interface/landing-sandbox.md
  - architecture/catalog.md
  - architecture/super-admin.md
  - architecture/multi-tenancy.md
  - architecture/ads-conversions.md
---

# Web dashboard (Phase 1)

A single-file Vue 3 dashboard in `src/treg/web/index.html`, served **same-origin** by the API
(`GET /` → `FileResponse`, `dashboard()` in `api.py`, via `_WEB_DIR`). Same origin = no CORS and it
ships with the server (Render/Fly). Design language: **Ledger** (warm charcoal + clay accent,
mono-forward, dark default + light toggle) — see `docs/style-board.html` / `docs/DASHBOARD-PLAN.md`.

### Vue is vendored, not fetched from a CDN
There is no bundler, so Vue arrives as a plain `<script src>` — but from **`/vendor/`**, served off
`src/treg/web/vendor/` by an `_ImmutableStatic` mount in `api.py`, never from unpkg. It used to come
from `unpkg.com/vue@3`, and a visitor whose network could not reach unpkg got a **blank signed-in
dashboard with no error** ([#137](https://github.com/superdesigndev/treg/issues/137): mainland-China
`ERR_CONNECTION_CLOSED`, then `Vue is not defined`). The landing has no external scripts at all, so
the symptom read as "sign-in broke the site" when it was only "the dashboard needs one more origin".

Two rules follow, and both are load-bearing:

- **Pin the version in the filename** (`vue-3.5.41.global.prod.js`) and verify new bytes against a
  second CDN before committing them — see `src/treg/web/vendor/README.md`. A floating `vue@3` tag is
  arbitrary future code running in an authenticated session; that is why it is gone.
- **Nothing in the dashboard's critical path may be third-party.** Still CDN-hosted and *not*
  critical: the `@lobehub` agent icons (`agentIcon`/`agentIconInv`) and Google Fonts — those degrade
  to broken images and system fonts rather than a blank page.

A **loader guard** sits right after the script tag. `[v-cloak]{display:none}` hides the un-compiled
template until Vue mounts, which is precisely what made #137 silent — so the guard checks whether
`#app` is still cloaked ~1.5s after `load` and, if it is, replaces the blank with a readable message,
a reload button, and the issues link. Anything that stops Vue mounting now says so on screen.

`index.html`'s closing `<script src="/adtrack.js">` loads the first-party ad-click capture script on
every page render (dashboard included, since a visitor can arrive on `/app` from an ad) — no Google
tag, first-party cookie only; see [ads-conversions](../architecture/ads-conversions.md).

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
keys on `.orgblock`); (middle) the nav — Tools · **Secrets** (member+) · **Marketplace** (member+, the
OAuth-connect view — `go('connections')`) · Activity · **Usage** (admin/owner) · Team · Getting
started · Admin, then a Help group of two external links (**Open source** (the GitHub repo) ·
**Discord community**) —
the Tutorial nav entry was removed 2026-08-12 in their favor; the `help` view itself survives and
is still reachable (welcome flow, in-app links); (bottom)
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
  For copy-paste convenience it also fetches `GET /auth/cli-token` on load into `myToken` — sent WITH
  the active-org header, so the minted identity token is **team-pinned** (`sess.make(..., org=slug)`,
  a stateless `org` claim; the endpoint only pins a team the caller is a member of). That is the point:
  the "API key" then works as a **bare bearer** — pasted into an MCP server's `Authorization` header,
  where no `X-Treg-Org` can travel, it still resolves to that team (fixing the "several teams, none
  active" wall a multi-team user hit). `require_member` uses the token's org claim when no header is
  sent (the header still overrides), and the MCP layer surfaces it via `_internal_auth`. `myToken` is
  re-minted whenever the active org changes (`_myTokenOrg` guard) so it always names the shown team.
  The per-tool snippets embed it (+ `X-Treg-Org`, now redundant but harmless), and **"Copy API token"**
  (`copyToken`) puts it on the clipboard.
- **"For your agents" sidebar** — ONE copyable **agent instruction** (a prompt to paste into Claude
  Code / Codex), built client-side by `buildAgentPrompt(kind, inclToken)` with the caller's minted
  token + active org slug baked in. It was two prompts (admin "Setup" / consumer "Connect"), each
  ~30 lines that re-explained the catalog, the ladder, prices and the balance — knowledge that lives
  in the skill `install.sh` installs and in `/llms.txt`. A prompt can only carry what a FILE cannot:
  the token, the team, permission, and "do it now". So `kind` is now **ignored** (call sites keep
  their argument; there is one text to keep true), and one instruction serves everyone because it
  needs no path chosen up front — step 4 (find the catalog tools that fit this project, price them,
  call one on approval) works with nothing registered, and step 5 offers the sharing path only if
  the user has keys worth sharing. The row's ⧉
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

**Support chat (Intercom):** `initIntercom()` sits next to `initAnalytics()` and follows the same
opt-in gate — no `meta.intercom_app_id`, no widget. It boots **after** `/auth/me` resolves (not at the
`/meta` fetch) so the common case boots identified once: `email` + `user_hash` (from `/auth/me`,
see [api](api.md)) + `company` = the active team slug. Email is **never** sent without `user_hash` (that's the
impersonation vector identity verification closes) — no hash or no login means anonymous visitor
chat, which is also what `landing.html`/`support.html` do with a tiny `/meta`-gated inline loader.
`switchOrg`/team-create call `intercomUpdate()` so the company tracks the active team; `logout()`
calls `Intercom('shutdown')` so the next user on the machine can't read the previous conversations.

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
  splits from either field. The name field carries a **`<datalist>` of pasted-secret provider services**
  (`keyNameSuggestions` — `auth_kind` `key`/`token`, the names the marketplace ladder's tier 2 matches
  by `Secret.name == service`), and each row gets a `secretNameHint` line: an exact service name is
  confirmed ("Apollo.io catalog calls will use this key automatically"), a near miss (`APOLLO_API_KEY`,
  `tikhub-key` — suffix-stripped, case-folded) gets the exact name plus a one-click **rename** link,
  and any other name gets silence — most secrets back the org's own tools and can be called anything.
  Providers are loaded on entering the view (`go('secrets')` also fires `loadConnections` when the
  list is empty) so the suggestions exist on a cold deep link.
- **Team** (`view==='orgs'`) — the active team, now split into **tabs** (`orgTab`):
  **Members · Projects · Policy · Billing · Team settings**. (The former **My teams** tab is gone — it
  duplicated the sidebar picker; its New team / Join by code / Paste token actions live in Team
  settings now.) Header reads `Team: {activeName} — you are {activeRole}` and points at the sidebar
  picker for switching.
  - **Members** — the roster (role, daily cap, today's usage, tool ACL, project scope, local-run toggle).
    Inviting is no longer a separate section: it sits here behind an **"＋ Add member"** toggle
    (`showInvite`), which is where people look for it. The inline access editor now carries **project
    checkboxes** (`projDraft`) beside the tool ones, each collapsing "all checked" back to *all* so future
    tools/projects are inherited; a zero-project team gets a pointer to the Projects tab instead of a
    silently-missing section. `list_members` returns `is_agent`, so people and machines are
    distinguishable in one roster.
  - **Projects** — create / list / delete, and the tool count is now a button: it expands an inline
    **project editor** (`editProj`/`projToolDraft`) listing every tool with a checkbox — check to add,
    uncheck to free back to team-wide; a tool living in another project is chipped and checking it
    moves it (saved as per-tool `PATCH /tools/{id}`). The tab copy states the deliberate design:
    secrets stay team-level. Delete still states that tools become team-wide rather than deleted.
  - **Policy** — deny rules (host / path / method / who / **project** / note), listed and removable.
    The "who" select labels agents; the project select scopes a rule to one project's tools. Below,
    **Per-tool CLI blocks** (read-only, from `GET /orgs/{id}/policy/cli-deny`) lists each CLI tool's
    argv deny patterns with their source (skill vs catalog), under a line naming all three deny
    layers — HTTP rules, argv patterns, OS sandbox — so the whole "what is blocked" picture is one
    screen.
  - **Billing** (admin+) — balance, the top-up presets, the auto-top-up toggle with its verbatim
    PSD2/SCA mandate text, and below them **Payment history**: date, amount, an `auto` marker, and one
    link per row — *Invoice* when Stripe issued one (manual top-ups do; automatic ones can't), else
    *Receipt*, else an em dash. Amounts come from our own ledger and the links from Stripe, so when
    Stripe is unreachable the table still renders and a line under it says the links, not the numbers,
    are missing. A **Manage billing** button opens Stripe's hosted portal (card, billing address, tax
    ID, the full invoice archive); it is hidden until `billing.portal` is true, which needs a Stripe
    customer, which a team gets on its first payment — so a new team never sees a button that errors.
  - **Team settings** — deliberately JUST the **Danger zone** (leave / delete), visible to EVERY role
    (leaving is self-service, and `loadOrgAdmin` lands a non-admin here). New team / Join by code /
    Paste token live only in the sidebar picker — cut from this tab on founder review; a personal
    team shows a one-line explainer instead of an empty page.
- **Agents live INSIDE Team → Members now** — the separate Agents page and its sidebar entry are
  GONE (founder call on review: an agent IS a membership, so two rosters was one too many). One roster
  (`rosterMembers`): each person, then `↳` the agents they minted (short name + "owned by", never
  the machine address; Setup / Rotate / Revoke actions inline), then the runtimes **detected** in
  their traffic (`observedAgents`, from `GET /orgs/{id}/agents/observed`) as `↳ codex · detected`
  rows with a **Scope this agent** button (`promoteObserved`) that opens the Add-agent form
  prefilled and linked (`promotePending` → `promoted_from`, so the detected row disappears on
  Create and returns on revoke). "Add to this team" toggles **＋ Add member** (invite) |
  **＋ Add agent** (mint form: name / role / cap + project picker `agentProjSel`, all-checked =
  omitted = every project). The once-only token card renders at the top of the tab. Rotate
  (re-POSTs the same name, so the old token dies), **Revoke**, and an inline cap editor. Rotate sends
  only `{name, role, daily_call_cap}` **on purpose**: `create_agent` leaves every field the client does
  not send exactly as it was, so the agent's tool ACL and project scope survive the rotate. They used to
  be silently cleared by this very button — see [multi-tenancy](../architecture/multi-tenancy.md). A minted token
  appears in an accented card that says it is shown **once**; under it are three paste-ready snippets
  (`agentSnip`: Environment / Give it to your agent / Verify) built by the `agentSnippet` computed, mirroring
  the Tools snippet block. Because a stored token is hashed and unrecoverable, each row also has a **Setup**
  button (`showAgentSetup`) that reopens those snippets for an existing agent using `$TREG_TOKEN` as a
  placeholder, telling the user to Rotate if the token was lost. Choosing `role=admin` raises a
  confirmation spelling out that an admin agent can register/delete tools and secrets and manage members.
- **Tool form** — Add/Edit now carries a **Project** dropdown (default "team-wide"); `loadProjectsIfNeeded`
  fetches the list on demand so it works even if the Team page was never opened.
- **Getting started** (`view==='start'`, the FIRST nav item, above Catalog; the sidebar's team half is
  labeled **"Your vault"** — Jason's explicit naming call, overriding the no-"vault" vocabulary rule) —
  two numbered **step cards** (`.start-card`):
  **① Set up your \<agent\>** — an agent dropdown (`startAgentOpen`, shares `welcome.agent` +
  `welcomeAgents`/`welcomeMoreAgents` with the first-run modal; "Docs" links `/tutorial`) above the
  per-agent setup line (`welcomeSetupCmd` = `buildAgentPrompt` — `set up treg — <proxy>/llms.txt with token <T>, team <slug>`; the long multi-step agent prompt is retired, llms.txt itself now carries the setup flow, the do-not-stop authorization framing and the star ask, and its money rules no longer demand per-call price confirmation) and, combined into the
  same card, **Your API key** (`myToken`, masked with a `startTokenShow` reveal + copy — the key
  already exists, so there is no "create key" step). **② Try it out** — four copyable example
  prompts (`tryExamples`, category-labeled `.try-card`s: Social/Trends/SEO/Enrichment — concrete
  live-data asks a bare agent can't answer), then an `.oauth-div` divider ("also connect OAuth
  accounts for new agent capabilities") over three grouped rows of `.prov-chip` logo chips
  (`tryOauth` — Post on social: X/YouTube/TikTok/LinkedIn with a dotted "`N` coming soon"
  `.soon-note` whose `title` tooltip lists Instagram/Facebook Pages · Manage ad campaigns:
  Google/Meta Ads · SEO on your own site: GA/GSC/GBP) — each chip `openProvider(service)` into the
  marketplace connect page.
  Below, a collapsed `<details>` ("Prefer the terminal?") keeps the old two-tab manual walkthrough
  (`startTab`: **Access** install → login → catalog search/call/balance; **Setup** scan/upload then
  the team-use commands). Per-block copy via `copyStart`.
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
  domain via `_suggestTeamName`). Step 0 is NOT dismissable — no skip, survives Escape/backdrop — the only
  action is `welcomeCreate` (`POST /orgs`, marks onboarded). Three more steps follow **inside the same
  modal**: an **agent picker** (`welcome.step===1` — OpenClaw / Hermes Agent / Claude.ai / Claude Code /
  Codex, plus a "More" expander with opencode / pi / Cursor / Gemini CLI / Other; LobeHub icons via
  unpkg, theme-aware light/dark variants through `agentIcon`) with Skip/Next; the **setup block**
  (`step===2` — "In your agent's chat, send:", the setup line + team/token as one unit —
  `welcomeSetupFull` copies with the real token, `welcomeSetupMasked` displays it masked behind a
  Show/Hide-key toggle) with Back/Next; and **"Try it out"** (`step===3`, wider modal — a "waiting for your agent" status line (pulsing
  `.wc-waitdot`, no 🎉), the `tryExamples` copy cards and the `tryOauth` connect chips, footer **"Skip"**
  `welcomeFinish` + **"Browse all catalog →"** `go('connections')`). Skip and Go-to-Getting-started call `welcomeFinish`
  → close + `go('start')` (Getting started, also the default landing for any signed-in arrival with no
  deep link/hash).
  **Exception —
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
  old demo/guided stepper (`onb.*`) is removed entirely. A `demo` chip marks a sandbox org.
- **Tutorial (`view==='help'`; no nav entry since 2026-08-12)** — the full interactive walkthrough, rendered natively (Vue) from the shared
  `window.TREG_TUTORIAL` data (`tutGo`/`tutHL`/`tutCopy`, syntax-highlighted command + output blocks,
  persona chips, and four toggle panels — **Concepts · Roles · Auth shapes · Skills**). The standalone
  `/tutorial` mirrors it and opens a panel from the URL hash (`/tutorial#auth`, `#skills`). See below.

## Marketplace — the in-browser OAuth-connect UI (`view==='connections'` / `'provider'`)
The dashboard now runs the whole **hosted connect flow** in the browser, so a member can attach a
provider account (Google Analytics, Search Console, Google Ads, Slack, Meta/Facebook/Instagram, X,
TikTok, LinkedIn, YouTube, …) without touching the CLI. `loadConnections` fetches **`GET /oauth/providers`**
(server route `oauth_providers_list` → `oauth_providers.listing()`, each row carrying `service`,
`display_name`, `category`, `summary`, `capabilities`, `scope_detail`, `auth_kind`, `supports_discovery`,
and a **`configured`** flag = whether *this* deployment holds the provider's client credentials) plus
**`GET /connections`** (`list_connections` — the org's existing grants).

The list view opens on a **tab bar** (`.mk-tabs`, `mkTabs` computed): `All`, then **one tab per catalog
category derived from the data**, then `Platform`. The middle is deliberately not a hard-coded list —
categories keep changing (`Social media` became `Social`, `China Social` is new, and the AI-search shelf
has been called both `AI Search` and `AEO / GEO`) and a hard-coded list silently drops the tiles it does
not name. The strip **scrolls with its scrollbar hidden** (`scrollbar-width:none` +
`::-webkit-scrollbar{display:none}` on `.mk-tabs`); a right-edge `mask-image` fade is what hints there is
more, and it falls on empty space when the tabs all fit. The rule under the tabs lives on the
**`.mk-tabs-wrap`** parent, because a masked border fades out 26px short of the right edge and reads as a
rendering fault. Every tab but the last is the **platform axis**
(which data you want — see the catalog section below); the **last tab, `Platform`, is this integration marketplace**
(which account you hold): providers grouped by category (`providerGroups` computed →
`shownGroups` filtered by the `mkCat` chip row) as a **list** (`.prov-list`, one `.prov-row` per
provider), not the old card grid — this tab is where "bring your own key" lands, and forty cards put
every name on a different left edge; a row keeps them in one scannable column (name + truncated summary,
auth kind, connect state, and an **inline Connect / Add key** action that calls `startConnect(p)` right
from the row — the `tokenAsk`/`capAsk` modals are global, so the common path is one click). The whole row
still opens the provider page (`openProvider(service)`); each row has `id="prov-<service>"` so a
`goByok(service)` jump can scroll to it and flash it (`.prov-row.focus`, `byokFocus` state, self-clearing).
Rows show the **provider logo** served by convention from
**`/logos/<service>.svg`** (`.plogo-tile`/`.plogo`, `@error` hides a missing file) — the `StaticFiles`
mount `_LOGO_DIR` (`src/treg/web/logos/`). `connCount` labels how many accounts are already connected.
The tab bar itself is `v-if`'d on `plats.list.length` and `mkTabActive` collapses to `'platform'` when
the catalog is absent, so a build that predates `/catalog` renders exactly the old marketplace.

The catalog page's header carries a **Request a tool** button (`reqAsk` modal): a short form —
what's missing, an optional note, a contact field only when signed out (`!me`) — POSTed to
`/tool-requests` with `source: web`; the capability input pre-fills from the live search box `q`,
because the button is most often pressed right after a search found nothing. Beside it sit a
**Bring your own key** button (`goByok()` — the ink-fill primary; it only switches to the Platform
tab, but naming the action is what makes the tab findable) and
**List as vendor** (`vendorAsk` modal): a vendor pastes the
two-sentence prompt (`vendorPromptText`, built on `proxy` = the server's public URL) into their own
coding agent, which follows the hosted `GET /vendor-listing` instructions and raises the listing PR —
including the vendor's contact email, which is how a test credential gets arranged.

**One integration** is its own view (`view==='provider'`, `mkProvider`/`mkConns` keyed on `mkService`) at a
shareable path **`/app/marketplace/<service>`** (server route `dashboard_marketplace` — plain SPA, **no**
og meta, since the page is only meaningful to a signed-in member; client route `mkFromPath`/`openProvider`
push `/app/marketplace/<service>` into history). It lists the connected accounts (each account = its own
tool name so an agent can call a specific one), their health/expiry chips, and a **Permissions** panel
(`mkGranted` marks which capabilities are already granted; `scope_detail` gives the exact upstream scopes
on hover).

**Consent disclosure.** A provider row may carry a **`consent_notice`**, rendered as a `.mk-notice` panel
in two places: under the summary on the integration page (beside Connect) and inside the `capAsk` modal,
i.e. everywhere the popup can be triggered from. It is a plain surface, not `.banner` — `.banner` is red
and would read as a failure rather than something to read before consenting. Today only the Meta family
(`facebook`, `instagram`, `meta-ads`) sets one: their shared Meta app is registered as **Crewlet**, so
Facebook's consent screen shows a name the user never saw on treg. The template is `v-if`'d on the field,
so nothing here names those three services; adding a notice is a registry edit (see
`architecture/auth-secrets.md`), not a dashboard edit.

**Connecting** (`startConnect` picks the shape): a **token** provider (`auth_kind==='token'` — "bring your
own bot") opens the `tokenAsk` modal → `submitToken` → **`POST /connections/token`** (`connect_with_token`);
a provider with 2+ capabilities opens the `capAsk` modal (`capLabel`/`capHelp` explain each, e.g. TikTok's
weaker `draft`) → `chooseCapability`; otherwise it goes straight to `connectProvider`. `connectProvider`
does **`POST /oauth/start`** then opens the consent screen in a **popup** and **polls** `GET /oauth/status/{state}`
every 2s (state stays in the dashboard rather than relying on the popup talking back); on `done` it re-reads
`/connections` + `loadAll`, and if the provider `supports_discovery` and no resource is chosen yet it opens
the resource picker. **Post-connect setup:** `openResources` (**`GET /connections/{id}/resources`**,
`connection_resources` — a live upstream round-trip, so the modal opens first with a spinner) → `chooseResource`
(**`POST /connections/{id}/resource`**) sets the default account/property; `saveExtraCred` (**`POST
/connections/{id}/extra-credential`**, `set_extra_credential`) supplies a second credential a grant needs to
be callable (e.g. Google Ads' developer token — surfaced by the `needSecondCred`/`mkNeedsCred` banners);
`enableCapability`/`reconnect` re-run consent to widen scopes or refresh a `staleConns` credential; `disconnect`
(inline-confirm → **`DELETE /connections/{id}`**, `revoke_connection`). Server-side, a successful connect
**auto-provisions the tool** (`_autoprovision_provider_tool`) and records the account identity/resource labels
(`_record_connected_identity`, `_enrich_resource_labels`). The three post-connect dialogs (`tokenAsk`, `capAsk`,
`resPick`) live at **app-root level**, not nested in the view — a nested copy failed to render on an integration
page (Connect looked dead).

## Endpoint catalog — the platform axis of the marketplace (`view==='platform'`)

> **This view is also the public catalog.** `/catalog` and `/catalog/<slug>` serve this same
> `index.html`, and everything below renders for a signed-out visitor too — the catalog API needs no
> session. `publicCatalog` (set in the boot from `catalogFromPath()`) hides what does: the org
> switcher, vault, activity, team, the "not connected" badge, Try-it and the connect/BYOK buttons.
> There is no second implementation of any of this; see [seo](seo.md) for why, and for the `#prerender`
> fallback that carries the text to crawlers that run no scripts. `index.html`'s own `robots: noindex`
> is stripped on those two URLs only.

The marketplace's second browse surface answers "what data can I actually pull?" rather than "whose
account can I attach?" — see `architecture/catalog.md` for the data behind it, and it is the marketplace's
**default** view. `loadPlatforms` reads **`GET /catalog/platforms`** (once per session; cached on
`plats.loaded`), whose rows carry a **`category`** and a **`featured`** rank (`int|null`). `platCategories`
groups the rows **by whatever category they carry**, sorts those groups into the founder's canonical
reading order (SEO · Social · Advertising · Enrichment · E-commerce · Reviews & Apps · China Social ·
Community, then anything new alphabetically) and **drops `Other`** — the taxonomy's bucket for things like
`account`, whose capabilities only make sense inside a platform page, never as a tile. The order list is
only an *order*: a category the catalog invents still gets a shelf and a tab, at the end — but at the end
is where a RENAMED category silently lands, which is how `AEO / GEO` once sorted after `Community`, so a
rename means editing this list. The **answer engines** (ChatGPT, Perplexity, Gemini, Claude, Doubao, AI
search overall) were that shelf twice over; they are **unfeatured SEO platforms** now, which lands them in
SEO's overflow row rather than in a category of six. `platCatGroups` is that list whole on the
`All` tab and filtered to one entry on a category tab, so the section headings never disappear and the
page never loses its place.

**Featured shelves.** A category of 14 platforms is a wall you scroll past rather than read, so past **8
tiles** `platCatGroups` shows only the rows with a `featured` rank as full tiles and collapses the rest
into one **`.pt-more` row**: a stack of the hidden platforms' little marks (`.pt-mini`) plus
`moreLabel(rest)` — *"See Zhihu, WeChat Channels, and 6 more"* — two names so the row says what *kind* of
thing is hidden, then a count, then an **arrow** that slides on hover — the row's whole affordance. It is
deliberately NOT a dashed box: a dashed border reads as a drop zone or a placeholder and drew more
attention than the cards above it. Clicking sets `platShelfOpen[category]` and the shelf renders whole,
inline — which is how the six answer engines surface under SEO. Tiles sort by **rank ascending, then endpoint count descending** (the unranked tail has nothing
else left to sort by). Two guards: a category whose rows are *all* unranked is never collapsed (an empty
grid over a "more" row would hide the whole category behind a click), and the shelf header's count is
`g.total` — the whole category, not the visible tiles, so "SEO 5" can't sit under a tab reading "SEO 10".

Each shelf is headed by a **real heading** (`.sec-head` → `<h2 class="sec-h">` at 19px semibold, a count
pill, and the one-line explainer under it) rather than the small muted caption the rest of the dashboard
uses for table groups. The marketplace is *browsed by category*, so the category has to be the loudest
thing on the page after its title; as a caption the whole surface read as undifferentiated.

**The platform card** (`.pt-card`, a 320px-min responsive grid) carries everything needed to choose a
platform without opening it, and every field comes off the `/catalog/platforms` row — no detail call:

- **Head** — the platform's own mark (`/logos/platforms/<slug>.svg`, a second convention alongside the
  provider logos), the short label (`platShort` drops the ` — gloss` / ` (parenthetical)` the catalog
  labels carry), and the **category** as a muted subtitle. The name **wraps to two lines** rather than
  ellipsising: "Google Search Con…" is a card that cannot say what it is.
- **Connection state** (`.pt-conn`, top-right) — a green **`Connected`** `.chip.go` when `platConnected`
  finds *any* provider serving this platform already connected, else a muted "not connected". This corner
  used to carry a provider logo stack; it now answers the only browse-time question that changes what you
  do next — call it today, or sign up first. *Which* provider serves it is a decision for the platform
  page, not a fact worth a card slot.
- **No summary paragraph.** It repeated what the name and category already said, and made every card
  tall enough that a shelf of twelve became a scroll — cards went from ~200px to ~110px when it came
  out, which is what left room for the name to wrap. The data is still served and still used: it is
  the card's hover `title`.
- **Footer** — the endpoint count on the left, and on the right the starting price from the row's
  `price_from`, via `platPrice` (see **Prices are unified USD** below). The USD figure stands alone —
  the native amount ("1 credit") used to trail it in parentheses and broke the card layout on long
  prices; it lives in the hover `title` now.
  A `price_from` that exists but publishes no number renders **nothing** — "from —" says less than
  silence. "From" is a **floor**, and an `oauth` integration among the platform's providers makes the
  floor $0 (the account you connect is the licence): **any** *unmetered* OAuth provider ⇒ **"free with
  your account"**, even when metered providers also serve the platform and publish a rate — Google Ads is
  served by its own OAuth integration *and* by scrapers, and must not read "from $0.00188". A
  **`metered` provider is the exception, and it is not a special case so much as the same rule**: the
  account you connect is the licence only where the licence is what you are paying for, and X bills
  *treg's app* per use, so a connected X account changes who made the call and not who is billed. The
  metered rate moves into the tooltip ("without it, metered providers serve this from …"). A key-auth
  provider with no published rate stays silent. Note that `price_from` arrives as `null` *or* as an
  empty `{}`, and the empty object has to be normalised to null first — being truthy, it otherwise
  short-circuits the auth-kind branch and silently costs an OAuth-only platform its "free with your
  account".

**Prices are unified USD.** Every price the marketplace displays — the card footer, the capability card's
"from", and the per-endpoint cost chip — is the **server's computed `usd`** field on `cost` / `price_from`,
formatted by `usdNum`: two significant figures under a dollar (`$0.015`, `$0.00015`), cents at or above one.
The FX table lives in the catalog (`fx.yaml`) so a rate refresh re-prices every surface at once, and the
dashboard carries **no** conversion constant of its own — one here would drift from the CLI the moment the
table changed. Wherever the provider bills in something else, the native figure follows as a muted
`.cost-nat` suffix (`¥0.10`, two decimals — money keeps its cents) with the conversion spelled out in the
tooltip, so nobody has to wonder whether we invented the number. Two carve-outs: `type: free` keeps its
"free" / "free with your account" wording, and a `quota_rows` price is excluded **before** `usd` is read —
it is a row count, not money, and the server would convert it into dollars quite happily.

Every catalogued platform is currently drawn, but a missing file falls back through
`@error → platLogoBad[slug]` to a **generated initial tile** coloured by a
hash of the slug (`platTileBg`), stable across reloads and needing no colour table. Everything catalog-related is
**additive and failure-tolerant** — `loadPlatforms` swallows its error, so a deployment whose build predates
`/catalog` shows the marketplace exactly as it was rather than an error or an empty section.

**A platform** is its own view (`openPlatform(slug)` → `loadPlatform` → **`GET /catalog/platforms/{slug}`**).
Unlike `/app/marketplace/<service>` it is a **hash route** — `/app#platform/<slug>` — because there is
no server route that would serve the SPA for a hard reload of a `/app/platforms/<slug>` path; boot and
`popstate` read it via `platformFromHash`. The page is **ONE ledger** — a single table sectioned by
DOMAIN (user · video · search · shop · …) — rendered from the response's `domains[]`, which the server
has already ordered and merged (`catalog_store.domain_rows`). The old per-capability card stack made the
shape of a platform unreadable: every job looked the same size and nothing could be compared without
opening two cards.

Its header is **`.plat-head`, a single stacked column** — mark + title on one full-width line, the intro
under it at a readable measure, then the providers as their own wrapping `.plat-provs` row. It
deliberately does *not* use the two-column `.tut-head` the other pages share: a platform can be served
by a dozen providers (`people` has 8, `companies` 10), and as a right-hand column that chip list takes
half the width and wraps the title into a three-line ribbon ("People & / contact / data").

**Sections are domains, `other` always last.** A domain is the subject an endpoint is about within its
platform, resolved once at load time (`catalog_store._domain`): an explicit `domain:` in the yaml, else
the capability id's middle segment (`tiktok.video.comments` → `video`), else a keyword read off the
**path** — never the summary, since prose says "the Live SERP API…" about endpoints that are nothing of
the kind — else the path's grouping segment (`/v3/backlinks/anchors/live` → `backlinks`, with delivery
modes like `/live`, versions and `/json` stripped first), else `other`. Sections run busiest-first with
`other` pinned to the end: it is the junk drawer, and its position is the one that carries meaning.

**A domain section renders only if a browse row lands in it, and all the plumbing collapses into one
section.** The page loads `?include_hidden=1`, so `account`/`utility` endpoints arrive tagged by
`kind`; they are the provider's own machinery (webhooks, saved lists, token exchanges, enum lookups),
not the data anyone came to browse. Filing them per-domain conjured sections that existed only because
a hidden endpoint carried that capability id — the People page grew CAMPAIGNS 0, LOCATION 0, PERSON 0,
SCHOOL 0, TITLE 0, each with nothing in it but an expander. So: a domain needs at least one visible
row to exist, and **every** management endpoint on the platform lands in a single collapsed
**Actions** section at the foot of the ledger, its domain ignored, counted in its heading
("Actions · 24"). Inside, they render as ordinary rows plus a `kind` chip — account vs utility is the
only thing distinguishing one from the next. A platform with no such endpoints (telegram) grows no
Actions section at all. Because Actions is platform-wide rather than a domain, selecting a domain chip
**hides** it rather than filtering it, and neither the chip counts, the `All` count nor the
`N rows · M endpoints` line ever counts it — opening Actions must not make the browse surface appear
to grow.

**Within a section, merged rows lead.** A capability **two or more providers** implement is ONE row —
that comparison is the reason the catalog groups by capability at all, and burying it under fifty
single endpoints is how the old page hid it. Everything else is a single row led by the endpoint's
**`name`** (its curated short title), falling back to a **clipped** `summary` — `clip(…, 90)` cuts at
a word boundary and the `.lsum b` two-line clamp catches the rest, because a summary is documentation
prose and DataForSEO's run to a paragraph. The full text is never lost: the clipped row keeps it in a
`title` attribute and the expansion shows it whole. The capability id stays in the data as a join key
and never becomes a heading.

**The merged row's middle cell is a strip of THREE pills and a `+N`** — never four, and it cannot
wrap (`flex-wrap:nowrap; overflow:hidden`). A wrapped strip gave the shelf ragged row heights and
left the title cell's border ending mid-row; a collapsed merged row is now exactly as tall as a
single one (37.5px on every row of tiktok, web and google). The hidden providers' names ride in the
`+N` chip's tooltip, and the full list is one click down on the sub-rows.

A pill is **per provider** (`provPills`), not per endpoint — TikHub's four takes on the same job
would otherwise repeat four identical pills — carrying the provider's name, its **cheapest priced**
endpoint's price, and a ✓ if any of its endpoints is verified. Since only three are ever shown they
are sorted **cheapest first, then verified**, with the providers that have no price to show (whose
pill would be a bare name) at the tail. `pillPrice` shows a price only when there IS one: a published
number or `free`; a `quota_rows` label only if it fits in eight characters ("2 rows" yes); and
**nothing at all** for a credit-metered or dashboard-only rate. That last rule is why the pills fit —
four copies of "per result · price in provider dashboard" is what wrapped the row in the first place,
and it is also why `costShort` says **`credit-priced`** on the sub-rows while the sentence explaining
the unit and where the rate lives sits in the expanded facts list. The cheapest across the whole row
stays in the price column, and the provider/endpoint counts live in the cell's tooltip rather than on
a second line of their own.

**Merged rows expand in TWO levels.** Clicking one opens its providers as collapsed `.lsub` sub-rows —
one line each: logo, name, `costShort`, ✓/·, the connected chip, and a truncated `METHOD path`.
Clicking a sub-row (`toggleEp` → `epOpen[e.id]`) opens **that** provider's instruction. Dropping six
full parameter tables on one click buried the comparison the merge exists to make. A single row has
nothing to compare, so it skips the middle level and renders its detail straight away — the SAME
`.lep` block either way (`v-if="r.kind!=='merged' || epOpen[e.id]"`), so the two paths cannot present
the instruction differently. Inside a merged sub-row the detail drops the provider/route header the
sub-row above already shows, and leads with the chips.

**The filter bar is sticky** under the top bar, and the section headings stick under *it* (`--lbar-top` /
`--lsec-top`); the domain chips **scroll** rather than wrap, because a bar that grew a second row as you
filtered would push the headings out from under it. Text, `verified only` and the domain chips narrow the
same row list (`platRowsPreDomain` → `platLedger`); a section with no surviving rows disappears rather
than showing an empty heading, chip counts are taken after the other two filters so a chip never promises
rows they have already removed, and a live `N rows · M endpoints` line counts both — a merged row stands
for several endpoints. Both the wrapper and the table drop their `overflow` clip (an `overflow:hidden`
ancestor is a scroll container, and a sticky heading inside one never escapes it) and the table is
`table-layout:fixed`, so a nowrap path or `treg call` line scrolls **inside** its cell instead of widening
the table past the page.

**No ledger cell may carry its own `display`.** A `<td>` with `display:flex` stops being a table-cell: the
browser wraps it in an anonymous cell that stretches to the row height while the flex box sizes to its
content and keeps the border. The separator under column one then lands ~1px above the one under column
two — a seam running the length of the table, with the hover and connected-row backgrounds split along it.
The layout flex lives on `.lsum-i`, a wrapper INSIDE the cell. A markup test asserts it, and a DOM sweep
over four platforms at two widths, collapsed and expanded, found every cell of every row sharing one top
and one bottom.

The price column is the same unified USD as everywhere else (`capCheapest` → `costUsd`, native figure as a
muted `.cost-nat` suffix). Two things can never win "cheapest": an endpoint with no published rate, and a
`quota_rows` price (a row quota is not a price, and "from —" would be worse than naming the cheapest rate
we do know). A **connected** `own_account` or `free` row counts as **free** (`capFree`) — the OAuth account
you already hold is the licence — **unless the provider is `metered`** (`catMetered`, from `/oauth/providers`),
where the upstream bills treg's app per call and connecting changes nothing about the price. Reading the
flag off the server rather than naming X here means the display follows `TREG_OAUTH_BILLED_PROVIDERS`:
throw the kill switch and the rows go back to reading free, because they are. When nothing is priced but a row carries a `cost.note`, the cell reads
**"see provider"** rather than an em-dash: the enrichment providers (Apollo, PDL, Hunter, Coresignal,
Lusha, Diffbot…) bill in their own credits, so their price *is* documented, just not in dollars — and that
is the whole People/Company half of the catalog.

Each `.lep` block is provider logo + name, `METHOD path` (mono), a compact cost chip (`costLabel`:
`$0.015/success (¥0.10)`, `1 row`, `free`, and `per success · price in provider dashboard` when the
billing unit is known but the rate is not published), a `verified <date>` / `unverified` chip, a **scope**
chip, and a tier chip. Scope is the load-bearing distinction in a mixed list: `own_account` rows (the
OAuth providers) read **`your account`** in teal with the hint "reads the account YOU connect via OAuth,
not arbitrary public accounts", while `any_account` scraper rows read a muted `any account`. Under them
sit the parameters block, the provider-wide facts (`epFacts`: the cost note, `limits` and the rate card,
served once per provider in the response's `providers` map rather than copied onto 2,000 rows), the
paste-ready **`treg call`** line the row carries as `call_template` with a Copy button, the docs link and
the lazy example toggle. **Connection awareness** reuses the marketplace's own state: `catConnected` reads
`connCount` (from `/connections`), a row with a connected provider carries a green rule down its leading
edge (`.lrow.go td:first-child`), and the **Connect** button deep-links to the provider's existing
integration page — shown only when `mkKnown(service)`, since the catalog can name a provider this
deployment carries no client credentials for.

**The runnable green.** `.chip.ok` is not styled anywhere in the file, so it renders as muted grey — which
is how a ready capability came to look identical to an unavailable one. `.chip.go` (+ the haloed `.godot`)
is the marketplace's single "you can call this right now" green, used in exactly three places: the
platform card's `Connected` corner, a connected provider's `connected` chip, and a ledger row whose
provider is connected, which carries the green as a rule down its leading edge (`.lrow.go td:first-child`)
so it survives being skimmed. Everything unconnected stays muted.

An expanded row leads with the endpoint's chips and summary, then splits into **two tabs**: **Request**
(the parameters, the provider facts, and the `treg call` line) and **Example response** (the captured
JSON). Stacked, those two documents made the expansion a page you scrolled rather than read. Request
leads — it is the half that tells you whether the endpoint is callable at all — and the response tab is
**not rendered at all** when `has_example` is false. Not greyed out, and no "no example captured"
placeholder either: a disabled tab is a promise the catalog can't keep, and it draws the eye to the one
thing that isn't there. Those endpoints show a single tab, which reads as a label for the pane under it.
`epTabOf` also folds a stale `res` state back to `req`, so an endpoint can never be left showing a pane
whose tab is gone.

Both panes are the **same bounded box**: `.prm` and `.cat-ex pre` cap at **320px** and scroll inside
themselves. A DataForSEO body carries thirty parameters, and uncapped a single expansion pushed every
row below it off the screen.

The tab bar's right side carries the provider's **docs** (falling back to its pricing page), then the
two run actions — and which one is primary depends on the provider's `auth_kind` (`mkOauth(service)`).
For a **key/token** provider, **▶ Try it** (`openEpTry`) is the ink-fill **primary** — trying on treg's
own key is what most visitors want — and **Bring your own key** (`goByok(provider)` — jumps to the
Catalog's Platform tab with that provider's row scrolled into view and flashed, so the user sees where
their key lives among the rest; formerly `openProvider` straight to the detail page) is the secondary
ghost beside it. The same `goByok` jump is offered from a platform page's provider row (passing the
provider only when the platform has exactly one) and from the Try-it drawer's "can't run this here"
banner, which now carries a real **Connect** / **Bring your own key** button instead of prose alone. For an **OAuth** provider treg *can't* serve on
its own key (calls act as your account), so the order flips: **Connect {provider}** is the ink-fill
primary and Try-it is secondary. Once an account is connected the connect/own-key button is replaced in
place by the green **`Connected`** chip. Everything here renders identically in a single row's expansion
and in a merged row's provider sub-row, because both paths share the one `.lep` block.

**The Try-it drawer (`epTry`) is four tabs** (`epTryTab`, default **AI Agent**): **AI Agent** — the
one-line setup (`epTrySetupLine`, with team + token embedded **here only**, a copy-and-run-now context;
the setup line everywhere else stays clean) plus a ready "Use treg to call `<id>` — `<summary>`" prompt
(`epTryAgentUse`); **CLI** — install/login, `treg catalog get <id>`, and the filled `treg call <id>
--query …` (`epTryCliCall`); **API** — the `curl {BASE}/call/<id>?<query>` passthrough with the token
header (`epTryCurl`, adding `X-Treg-Org` in session mode since the minted token is an identity token);
and **Manual** — the live test form (params + `❯ Run`, disabled with a reason when the access dry-run
says this org can't call it) that the drawer used to be by itself.

**Example responses** load when their tab is FIRST opened (`setEpTab` → `loadExample`, guarded by
`if(this.platEx[e.id]) return`), never with the page and never twice — a platform can carry hundreds of
endpoints and the captured responses are the heaviest thing in the catalog.

**Parameters** come from the row's own `input` field (`{pathParams?, queryParams?, body?, bodyType?,
note?}`, each param map being `{name: {type, required, note, example}}`) and render *before* the example
toggle: the response half was already there and this half was not, which made every endpoint look
uncallable until you left for the provider's docs. `paramSections` groups them **query → path → body**,
the order you fill them in for the common GET; the body section labels its `bodyType`, and `input.note`
becomes a hint line above the whole block. `fmtExample` stringifies object/array examples so they don't
render as `[object Object]`. `input` is null on **every** extended endpoint (~1850 of them, against 250
mapped ones), so the empty case is the common one and gets an explicit "the provider's docs have them"
line rather than an empty table. `.prm-t` explicitly resets the global `table`/`th` chrome (panel
background, border, radius, filled header bar), which otherwise reads as a stray highlight inside the
`.prm` box and clips the first column against the table's own border. Navigation runs both ways: an integration page carries a
**Covered in the catalog** chip row (`mkPlatforms`) into the platform pages, and each platform page
header links back out to the providers that serve it (`platProviders`). `tests/test_dashboard_markup.py`
locks the structure (top-level view, the row/detail `<template>` pair inside the `.ttable`, the
`v-if`'d tab bar and its `platform` fallback, the derived tab list and category order, tiles wearing the
platform's own logo with the generated-initial fallback, the `Platform` tab still carrying the provider
shelves and their connect flow, the category heading being a real heading, the card's four regions
(mark + name + category, the connected-state corner, the count/price footer — and NO summary
paragraph, with the name wrapping instead of ellipsising), the
unified-USD price rule (server `usd`, no local FX constant, native suffix, `{}`-normalisation,
`quota_rows` excluded first) and its unmetered-OAuth-only "free with your account" branch,
the runnable green on all three of its surfaces, the stacked platform header, the always-both
provider/endpoint counts, the credit-priced fallback ranking ahead of "price not published",
the parameters block sitting before the example
toggle with its query/path/body order and its no-params fallback,
the featured-shelf split and its two guards, the ledger being one table
with `other`-last domain sections that need a visible row to exist, the single platform-wide Actions
section holding every management endpoint, and merged-before-single rows, a row title that is a name or a clipped
summary and never a paragraph, the collapsed merged row's non-wrapping three-pills-and-a-count strip, its pills being per-provider,
sorted cheapest-first and priced only when the price is a real number,
the two-level expansion (provider sub-rows, then one detail block shared with the single-row path), the
long metered phrasing never reaching a collapsed line, the filter bar's three controls and their chip
counts, both sticky layers and the overflow rules that let them stick, the two-tab expansion (Request first, no response tab at
all without an example, both panes capped at 320px), the prominent Connect in the tab bar with its
Connected state, the `treg call` line and the provider facts, the cross-currency cheapest rules, the credit-priced "see provider" fallback, the scope
chips, and lazy examples). `tests/test_catalog_api.py` locks the server half: the section order, the
merged/single split, the domain resolution ladder, and a delivery-mode path segment never becoming a
subject.

## Code surfaces (every page)
Snippet blocks (`.lc-codewrap` on Getting started, the in-app CLI tutorial's `.term` panes, the
standalone `/tutorial`, the connect/setup instruction panes, the ledger's `treg call` line and captured
responses) are **theme-aware, surface and ink together**. They were not: the generic
`[data-theme=light] pre` rule outranks `.lc-codewrap pre`, so light mode painted a light panel INSIDE
the dark wrapper while the syntax ramp stayed drawn for a dark block — a near-white command on a
near-white background, bright cyan URLs, and a dark copy pill. That is also why the earlier
"terminal surfaces stay dark in both themes" rule is gone: in light mode a code block is now a light
block with dark ink, which is what makes the ramp legible.

Two token sets carry it (index.html §3.8, mirrored in tutorial.html, which has its own copy of the
sheet): `--code-bg` / `--code-ink` / `--code-line` / `--code-btn` for the surface, and
`--sx-cmd` / `--sx-var` / `--sx-str` / `--sx-flag` / `--sx-cmt` / `--sx-punct` for the ramp. Every
light value clears **4.5:1** on `--code-bg` (measured worst case across all pages: 4.67 light, 5.62
dark, including the muted "expected result" panes, which use the ramp's comment grey rather than
`--muted` at 3.7:1). The inner `<pre>` rule is `:root`-qualified so it out-specifies BOTH generic
`pre` rules, and a markup test forbids a hex literal inside any `.hl-*` / `.s-*` rule — a literal is
one theme's value, and three earlier generations of this ramp each left one behind.

## Shareable detail pages (`/app/skills/<name>`, `/app/tools/<name>`)
A skill or a tool has its own deep-linkable page so a member can **share** the exact thing (`view==='detail'`,
`detail={kind,name}`). Server routes `dashboard_skill_page` (`/app/skills/{name}`) and `dashboard_tool_page`
(`/app/tools/{name}`) both call **`_spa_with_og`**, which serves the same SPA but injects per-resource
`og:`/`twitter:` meta so a pasted link unfurls — the meta echoes **only the URL's own name segment**
(HTML-escaped via `_esc_html`, **no DB read**, so an unauthenticated crawler learns nothing). The client
resolves the record by **name** (not id, so the URL is stable): `openDetail`/`loadDetail` fetch
**`/bundles/by-name/{name}`** (`get_bundle_by_name`) for a skill or **`/tools/by-name/{name}`**
(`get_tool_by_name`) for a tool. A **skill** page renders a "Use with your agent" copy-prompt (`detailPrompt`
— no token embedded), a bundled-tools/secrets chip row, and a file browser over the SKILL package
(`detailTree`/`detailFileContent`, `detailFile`); a **tool** page shows upstream + credential chips
(`credChips`), examples, and the CLI/guardrails block, linking back to its parent skill (`detailParentSkill`).
Actions: **⧉ Copy link** (`detailShareUrl`), **▶ Try it** / **⚙ Configure** (`tryDetailTool`/`configureTool`
via `fullTool`, which resolves the full record from a skill's tool summary), and **Share…** (`canAdmin` only).

The Tools list routes its rows through `rowTarget`/`rowHref`/`rowOpen` — a skill-born tool (has `bundle_id`)
opens its **skill** page (the shareable thing), a bare endpoint opens its **tool** page. **Share…**
(`openShare`/`sendShare`) invites someone by email with a **`landing`** field on the invite
(`POST /orgs/{id}/invites`, server-validated to a `/app/skills|tools/<name>` path) plus optional scoped
`tool_access` (unchecked "full access" → the skill + its bundled tools only). The emailed one-click link IS
the consent: on arrival `autoAcceptShare` accepts the matching pending invite silently and enters that team.
If the link resolves to a team the caller is **already** in but a different one, `findDetailOrg` probes the
caller's other teams and switches silently on a unique hit; a 404 with no match shows an "ask for an invite"
message. Boot + `popstate` route these paths (`routeFromPath`); a detail/marketplace path is stashed in
`localStorage['treg-next']` across an OAuth sign-in hop (the callback always lands on `/app`, which would
otherwise drop the path).

## The tutorial (one source, two renderers)
`src/treg/web/tutorial.js` is the **single source of truth**: `window.TREG_TUTORIAL` (`concepts`, `roles`,
`personas`, `steps[]` = `{part,who,title,explain,cmd,out,notice}`, plus the two focused arrays
`importShell[]` and `access[]`, same step shape) + a self-contained `tregHL(text,lang)` shell/json
highlighter. (The `CONCEPTS` proxy analogy was reworded from "a coat check" to **"a bank teller"** — you
hand over your token, the teller fetches the real secret from the vault and makes the call for you; the key
never crosses the counter.) It's served at `/tutorial.js` with **`Cache-Control: no-cache`**, and `/tutorial` rewrites the
`<script src>` to carry **tutorial.js's own mtime** (`?v=<mtime>`) — not `_app_version()`, which
hashes `index.html` and would not move when only the tutorial changed. Both are needed: the page
includes the file by a bare path, so a browser that cached it before the header existed applies a
heuristic lifetime and never revalidates, and an edited tutorial silently keeps serving the old
steps. Consumed by **both** the dashboard Help view (native Vue
render) and the **standalone** `src/treg/web/tutorial.html` (vanilla render, served at `/tutorial`;
renders `steps` only) — so they can never drift. `docs/tutorial.html` is now a redirect to `/tutorial`;
the prose walkthrough is `docs/TUTORIAL.md`. Editing steps means editing `tutorial.js` only.

**Two focused tutorials as cards** — **Import & shell** (`importShell`, auto-import + shell mode + the
local-run sandbox) and **Team access control** (`access`, per-member tool access + the local-run dial)
are cards on the tutorial chooser (`view==='help'`), rendered by **one shared stepper template** in `index.html`
(`helpMode === 'import-shell' || 'access'`), with its own `xtut*`-prefixed state/computed/method names
(`xtut.i`, `xtutSteps`, `xtutStep`, `xtutTitle`, `xtutGo`) so they never collide with the CLI tutorial's
`tut*` names. Two extra persona chips: `you` (green) and `sam` (amber). Each also has a **prose twin**
served as markdown: `web/tutorial-import-shell.md` at `/tutorial-import-shell.md` and
`web/tutorial-access.md` at `/tutorial-access.md` (both `_serve_md`, `{BASE}`-templated) — kept as the
agent-friendly versions; the main tutorial (`tutorial.md` + `docs/TUTORIAL.md`) links them near the top
and `tutorial.js` ends with a "Further tutorials" step pointing at the cards + URLs.

**Dashboard tour** (the web-UI walkthrough — screenshots, not commands): the tutorial view
(`go('help')` — no side-nav entry anymore)
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
OAuth-connect in-browser (the hosted consent + poll flow, `/oauth/*`) **has now shipped** — see the
Marketplace section above. Everything in DASHBOARD-PLAN (org lifecycle, resource registration incl.
multi-binding + edit, skill bundles, super-admin mutations, OAuth connect, shareable detail pages) has
shipped. Packaging: `src/treg/web` lives inside the `treg` package, so the wheel's `packages`
inclusion ships every asset (incl. `tutorial.js`/`tutorial.html`) — no `force-include` (a redundant
one double-adds each file and breaks the wheel build).

## The Referrals view

A top-level `<template v-if="view==='referrals'">`, plus a nav button and a second entry point under
the balance chip (where someone is already thinking about what treg costs them).

**`'referrals'` must appear in BOTH view whitelists** — `viewFromHash()` and the `popstate` handler.
`go('referrals')` works on click regardless of them; those two lists are what make the view survive
a RELOAD and a BACK button. Missing either is invisible in review and in clicking around, which is
precisely the silent failure CLAUDE.md warns about. Pinned by a test that counts both.

**The link is NOT gated behind paying us.** Every signed-in person gets one, free tier included —
see [money](../architecture/money.md) for why that gate was removed. The `!eligible` branch survives
only for the degenerate case of owning no team to pay a reward into, and a test asserts it never
again asks anyone to add funds.

`GET /referrals` mints the code as well as sweeping, so the page is one call and `link` is never
empty on a first visit.

**Every status renders a reason** (`refStatus`), including `capped` and `rejected`. "I referred
someone and got nothing" is the ticket this program generates, and the answer belongs on the page
rather than in an email to us.

Opening the page **has a side effect**: `GET /referrals` runs the payout sweep, so a user checking
whether their reward has landed is the one who makes it land. There is no scheduler in treg, so this
work rides on a request someone is already making.

### The billing page's referral prompt

The Billing tab renders `billing.referral_offer` when the team was referred: a green note naming the
minimum, and `+$X bonus` on each preset that clears it (`refPresetBonus`). Both are measured
against `remaining_micro`, not the full minimum — the threshold is cumulative, so a team that has
already added $5 is asked for "$5 more" and sees the bonus marked on the $5 button. The note says
the referee is credited **straight away** and the referrer after the hold — that timing is
load-bearing copy, not decoration: the referee has no Referrals page, so an unstated delay is what
made a correct payout look like a failure. Both are needed — the
amount is chosen at the buttons, and the first preset ($5) is below the $10 minimum, so a note on its
own would let the most-clicked button quietly forfeit the reward. Null offer = the page renders
exactly as it did before this shipped.
