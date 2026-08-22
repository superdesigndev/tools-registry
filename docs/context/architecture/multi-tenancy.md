---
title: Multi-tenancy — orgs, memberships, invites, per-org scoping
status: shipped
sources:
  - src/treg/models.py
  - src/treg/api.py
  - src/treg/db.py
related:
  - architecture/data-model.md
  - architecture/proxy-model.md
  - interface/api.md
---

# Multi-tenancy (orgs)

The registry is **tenant-isolated**: an **Org** owns resources, a **User** is a global identity, and a
**Membership** links them with a role and IS where the caller's token lives. A token = a `(user, org)`
pair, so every list/create/mutation and the proxy are scoped to the caller's org. Design source:
`docs/MULTI-TENANCY-PLAN.md` (standalone plan).

## The model (`models.py`)
- **`Org`** — `id, name, slug (unique), suspended, demo, public_demo, created_at`. The tenant that owns
  secrets/tools/bundles. **`public_demo`** marks a team whose member token is PUBLISHED (e.g. on the
  landing page): non-admin members are locked to `/call` + reads and may never act as a user — enforced in
  `require_member` / `require_identity`.
- **`User`** — identity only: `id, email (unique), created_at`. No token, no role.
- **`Membership`** — `user_id, org_id, role (owner|admin|member|viewer), token_hash (idx), webhook_url,
  daily_call_cap` (per-user daily usage cap; `-1` = unlimited, admin-set — see the API fragment's
  usage-metering section), **`tool_access`** (JSON; **NULL = ALL tools** — the default, so nobody is
  restricted on upgrade — else the list of allowed tool NAMES) and **`local_run_enabled`** (bool, default
  true); unique `(user_id, org_id)`. One person in N orgs has N memberships (N tokens). `ROLE_RANK` orders
  owner > admin > member.
- **`Invite`** — `org_id, email, role, code_hash (idx), status (pending|accepted|revoked), invited_by,
  expires_at, email_token_hash (idx, nullable)`, plus **`tool_access` + `local_run_enabled`** (the access
  to seed onto the membership when accepted — set access at invite time, edit later). Attached to an
  **email**: redeem the one-time code, **or** prove that email (any identity door) and accept it code-free
  — the code is a shortcut, not a requirement. `email_token_hash` is the inbox-only **second secret** in
  the emailed link — it can sign the invitee in (`GET/POST /auth/invite-signin?t=`, one-time), while the
  admin-visible code never can.
- **`Project`** — an OPTIONAL sub-scope inside an org (`org_id`, `name`, `slug`, unique `(org_id, slug)`).
  The org stays the hard isolation boundary; a project is a softer grouping on top. Deliberately a
  **label + ACL scope, NOT a namespace**: `Tool.name` stays unique per `(org_id, name)`, so no unique
  constraint had to be rebuilt (`_fix_tool_uniqueness` had to do that once already, and SQLite cannot
  alter constraints portably). `Tool.project_id` NULL = **org-wide**, which is every tool that predates
  projects — so this shipped purely additive. Secrets stay org-level on purpose: one shared credential
  legitimately backs tools in several projects.
- **Machine identities** — a `User` on an **unroutable domain**, which is what makes it a machine
  rather than a person. Two exist: the published demo token (`PUBLIC_DEMO_DOMAIN`) and an **agent**
  (`AGENT_DOMAIN` = `agents.treg.local`). Both are minted by an admin and act ONLY by their token:
  `_is_machine_email` gates them out of every login door and out of `require_identity` (below). An
  agent needs **no new table and no migration** — it is a `Membership`, so it inherits `daily_call_cap`,
  `tool_access`, `local_run_enabled` and per-identity audit for free, which is exactly what makes those
  controls *per-agent*. NOTE: "agent" here is an IDENTITY; `agents.py` is the unrelated skill-directory
  table ("where does each coding agent keep its skills").
- Resource tables (`Secret`/`Tool`/`Bundle`/`CallRecord`/`PendingOAuth`) carry `org_id`; `owner`
  (creator email) is kept for audit + the member role gate. `Tool.name` is unique **per `(org_id, name)`**
  (`UniqueConstraint("org_id", "name")`), so two orgs may reuse a name.

## Enforcement (`api.py`)
- **`require_member`** resolves `X-Treg-Token` → a `Membership` → a `Caller` (`membership, user, org`,
  with `org_id`/`email`/`role` properties). 401 if the token matches no membership.
- **`_role_at_least` + `_can_manage`**: admin/owner may manage any resource in the org; a member only
  what they created (`resource.owner == caller.email`). Update/delete return 404 when the resource is in
  another org, 403 when the role gate fails. **`_require_can_register`** gates create (secrets/tools/
  skills/oauth): a **viewer** (rank below member) may `call` + list only, and gets 403 on any register.
  `ROLE_RANK` orders owner > admin > member > viewer.
- **Per-member tool ACL (the release feature).** `_require_tool_access(caller, tool.name)` gates **all**
  use of a tool — the proxy `call_tool`, the server `run_tool_server`, AND the local `grant_local_run`:
  allowed if the member's `tool_access` is NULL (all) or names the tool; the **owner is exempt**
  (`_tool_allowed`), admins + members can be restricted. `_require_local_run(caller)` additionally gates the
  LOCAL tier on `local_run_enabled` (off → server runs only). Set via `set_member_access`
  (`PATCH /orgs/{id}/members/{user}/access`, admin+; an owner can't be restricted): `_normalize_tool_access`
  validates the names against the org's tools (422 on unknown) and **collapses an all-tools selection back
  to NULL** so a fully-checked member keeps auto-getting new tools. It's an **explicit allow-list**: a
  *customized* member does NOT auto-get a newly-registered tool (the dashboard toasts a reminder). `Invite`
  carries `tool_access`/`local_run_enabled` (validated at `create_invite`) → copied onto the membership at
  both accept doors. `list_members` returns both fields.
- **Agents (`create_agent` / `list_agents` / `revoke_agent`, `/orgs/{id}/agents`, admin+).** Mints a
  member identity for a machine caller, reusing the `create_public_token` recipe (re-POST the same name
  **rotates** — the old token dies there; revoke deletes the membership). Three invariants, each closing
  a real hole:
  1. **An agent token can never act as a USER.** `create_org` depends on `require_identity`, so without
     the `_is_machine_email` refusal there an agent could create a fresh org **in which it is owner** —
     and owners are exempt from `_require_tool_access` / `_require_local_run`, escaping every limit on it.
  2. **An agent can never be an owner** — blocked in `create_agent` AND in `set_member_role`, for the
     same exemption reason.
  3. **The address is org-scoped** (`agent-{org.slug}-{name}@…`, mirroring `_public_demo_email`): two
     orgs must each own an agent called `deploy` without sharing one `User` row, or a superadmin
     suspending one tenant's agent would kill the other's. Agents are always looked up by
     *(org + domain)*, never by recomputing the address, so an org rename can't orphan them.
  Every identity door is blocked at the shared choke point `_find_or_create_user`, plus `register_user`
  (which predates it and creates a `User` directly) and `auth_email_start` (refuse early, mint no code).
  `list_members` carries `is_agent` so one roster can show people and machines apart.
  **A rotate replaces the TOKEN, never the limits.** Because rotate is the same endpoint as create, an
  absent optional field used to fall back to its permissive default — and the dashboard's Rotate button
  sends only `{name, role, daily_call_cap}`, so a scoped agent silently became unrestricted
  (`tool_access=None` = every tool) just by getting a new token: round-4 blocker #2. `create_agent` now
  writes a field **only when the caller actually sent it** (`body.model_fields_set`, the shape
  `set_member_access` already used for `project_access`); a brand-new agent, having nothing to keep,
  still takes the documented defaults. `AgentIn` now DOES take `project_access` (slugs or ids, via
  `_normalize_project_access`) so an agent can be project-scoped at mint time — under the same
  sent-guard, so a rotate that omits it still preserves it. `Membership.created_by` (migration A23)
  stamps the minting admin, giving every agent an owner in the roster.
- **Observed agents (`GET /orgs/{id}/agents/observed`, admin+).** The OTHER half of the agents story:
  the runtimes already calling under members' own tokens. The CLI fingerprints its host runtime
  (`CLAUDECODE` → `claude-code`, `CODEX_*`, `CURSOR_*`, …; `TREG_CLIENT` overrides) and sends
  `X-Treg-Client`; `_client_of` normalizes it (slug ≤32, versions stripped, unknown-but-well-formed
  kept so a new runtime needs no release) onto `CallRecord.client` / `RunRecord.client` at all three
  audit points. The endpoint aggregates 30 days into one row per (member, runtime), excluding
  `''`/`cli` (a roster listing every human twice teaches nothing) and machine identities (already
  attributed to themselves). **Attribution, never authentication** — anything holding the token can
  claim any name, so nothing gates on it; scoping a detected agent for real = minting it a token
  (the dashboard's "Scope this agent" promotion).
- **Two ACL axes, composed as AND** (`_tool_usable` = `_tool_allowed` AND `_project_allowed`). The
  project scope is the coarse dial, `tool_access` the fine one; both are NULL-means-everything and the
  owner is exempt from both. `project_access` holds project **IDs**, not slugs, so the hot-path check is
  a pure set test (no id→slug query per call) and a rename cannot strand an access list.
  `project_access=[X]` with `tool_access=NULL` means "every tool in project X, **including ones added
  later**" — the composition that makes the coarse dial useful alone. `_normalize_project_access`
  accepts slugs or ids, 422s on an unknown one, and collapses an all-projects selection back to NULL
  (mirroring `_normalize_tool_access`). Endpoints: `create_project` / `list_projects` (a scoped member
  sees only their own) / `delete_project` (admin+) — deleting **frees** its tools back to org-wide rather
  than hiding them, and drops the id from every member's scope, **storing an emptied list as `[]`, never
  NULL**. NULL means *every project*, so collapsing `[]` would hand a member scoped to only the deleted
  project the run of every OTHER project's tools — a privilege escalation fired by an unrelated delete
  (round-4 blocker #1, `test_security_round4.py`). `[]` already carries the intended meaning (org-wide
  tools only), and nobody is locked out because the **freed tools** are what they keep: whatever the
  member could reach before the delete they can still reach after it. That, not a widened scope, is
  what "never lock anyone out" rests on. Invites carry `project_access` onto the membership at both
  accept doors, exactly as `tool_access` does.
- Every list filters by `caller.org_id`; every create stamps `org_id = caller.org_id` +
  `owner = caller.email`; `_resolve_call` scopes **both** the named lookup and the host/longest-prefix
  passthrough to the org; `call_tool` loads only same-org secrets. See [proxy-model](proxy-model.md).
- **Registration is shared across doors:** `_find_or_create_user(db, email)` finds a user or creates them
  — **the user ONLY, no auto personal org** (as of the no-personal-org change). Every identity door calls
  it (GitHub / Google callbacks, email OTP), so "first proof = registration" is identical. A brand-new
  user therefore lands with **zero teams** and must name + create their first one (the dashboard's
  mandatory welcome, or `treg org create`); their identity token is user-scoped so it works before any
  org exists. **`create_org` uses `require_identity`, NOT `require_member`** — else a zero-org user could
  never make their first team. See [api](../interface/api.md).
- **Code-free invites:** `my_invites` (`GET /invites/mine`, `require_identity`) lists pending invites for
  the caller's proven email; `accept_my_invite` (`POST /invites/{id}/accept`, `require_identity`) joins
  with no code (403 if `invite.email != user.email`, 409 if already a member). The code path stays.
- **Org management endpoints:** `register_user` (`POST /users`, legacy open-registration, used by the
  test fixture) still creates the user + an org + owner membership via `_make_org_membership` (mints the
  token) — NOT reached by the dashboard/CLI login doors, which no longer auto-make an org. Both this door
  and `create_org` below now also read the first-party ad-click cookie (`api._ad_attribution_from`) and,
  when enabled and present, stamp `Org.ad_gclid`/`ad_click_id_type`/`ad_landing`/`ad_click_at` on the
  new org — preserving whether the click was a GCLID, GBRAID or WBRAID — see
  [ads-conversions](ads-conversions.md). `create_org`
  (`POST /orgs`, `require_identity`),
  `list_orgs` (`GET /orgs`), `create_invite` (`POST /orgs/{id}/invites`, admin+), `accept_invite`
  (`POST /invites/accept`, open + code-protected → registers the user if new, joins them to the invited
  team, mints its token; a brand-new invitee joins the invited team **only** — no separate personal org),
  `list_members`
  / `remove_member` (`GET`/`DELETE /orgs/{id}/members[/{user}]`, admin+; owners cannot be removed).
  `_require_admin_of(org_id, caller)` gates the admin endpoints (token must be for that org + role ≥ admin).
- **An identity leaving takes its policy with it (`_drop_member_deny_rules`).** A `DenyRule` aimed at
  one caller (`user_id` set) is meaningless once that caller is gone, and it lingers in the Policy
  table naming a user id nobody can resolve. `remove_member`, `leave_org` and `revoke_agent` sweep the
  rules for that `(user_id, org_id)`; `admin_delete_user` sweeps **every org's** rules for that user,
  because `DenyRule.user_id` is a foreign key and a surviving row would dangle — Postgres rejects that
  outright, while SQLite only hides it by not enforcing FKs (so the test suite alone cannot catch it).
  ORG-wide rules (`user_id` NULL) are never touched: they are about the team, not about one caller.
  Mirrors how `delete_project` sweeps the id it deletes out of every `project_access`.
- **Org administration:** `set_member_role` (`PATCH /orgs/{id}/members/{user}`, **owner-only** via
  `_require_owner_of`; a `_count_owners` last-owner guard blocks demoting the sole owner — ownership
  transfer = promote another to owner, then step down), `leave_org` (`POST /orgs/{id}/leave`, self-removal,
  same last-owner guard), `delete_org` (`DELETE /orgs/{id}`, owner-only, cascades every org-scoped row —
  including any pending `AdConversion`, which `_ORG_SCOPED_MODELS` now lists: a queued conversion belongs
  to the team it would be attributed to).
- **Invites lifecycle:** one-time **and** time-bounded — `Invite.expires_at` (default `INVITE_TTL_DAYS`),
  `accept_invite` returns `410` past expiry. `list_invites` (`GET /orgs/{id}/invites`, admin+) and
  `revoke_invite` (`DELETE /orgs/{id}/invites/{invite}`, admin+); expired codes are garbage-collected by
  `health.gc_expired_invites` (opportunistically on list, periodically in the health run).

## Hardening (invariants enforced)
- **Email is a case-insensitive identity.** `_norm_email` (strip + lowercase) is applied at every
  identity door and every invite comparison, so `Bob@X.com` and `bob@x.com` are one user/one personal
  org and an invite is always redeemable regardless of the case typed.
- **Invite hygiene.** `create_invite` refuses to invite an email that is already a member (409, no
  dead-end invite) and **supersedes** any prior pending invite for that email (one live code per
  invitee). `revoke_invite` only deletes a still-`pending` invite. An admin may not issue an `admin`
  invite (owner-only, mirroring `set_member_role`). Suspended users/orgs can neither view nor accept.
- **Governance never evaporates.** `admin_delete_user` promotes the earliest-joined survivor to owner
  when it removes an org's sole owner; the accept/create paths return a clean `409` (not a 500) on the
  membership/slug uniqueness race (`create_org` retries with a fresh `_unique_slug`).
- **Slug vs id.** `_resolve_org` resolves `X-Treg-Org` by slug first (an all-digit slug like `2024` is
  producible and must not be reinterpreted as a primary key).

## Startup migration (`db.py`)
`init_db()` runs `_migrate_to_orgs` inside its `begin()` block — **guarded + idempotent**. It (A) adds
`org_id` columns to legacy resource tables (`ALTER TABLE ADD COLUMN`), and (B), only when the `org` table
is empty and a legacy `user.token_hash` column exists, creates the default `superdesign` org, turns each
flat-era user's token into an **owner Membership**, backfills `org_id` on all resources, relaxes the
global `tool.name` unique index to the composite (`_fix_tool_uniqueness`), and rebuilds the `user` table
identity-only (`_rebuild_user_table`, since SQLite can't drop columns portably). A fresh DB (create_all
already the new shape) short-circuits. **`_rebuild_user_table` must preserve every current-schema column**
(`is_superadmin`/`suspended` from `_ensure_bool_col`, plus `token_version` — the additive `ALTER "user"
ADD COLUMN token_version` at (A9)) when it copies rows — recreating `user` with only `(id,email,created_at)`
silently drops them and, because a re-run short-circuits, permanently 500s every User load on a legacy
upgrade. (`"user"` is quoted in the `ALTER` — a reserved word in Postgres, where this runs in-place.)
(A12) adds `tool_access` (JSON, nullable) + `local_run_enabled` (`BOOLEAN NOT NULL DEFAULT true`) to
**both** `membership` and `invite`; the legacy owner-backfill INSERT names `local_run_enabled` explicitly
(create_all builds it NOT NULL with no server default). Verified in-place on Postgres. Later additive steps
follow the same guarded pattern: (A14) `invite.landing`, (A15) `org.public_demo`, (A16) the seven `secret`
connection-metadata columns (`provider`/`granted_scopes`/`resource_ref`/`resource_name`/`expires_at`/
`last_refresh_at`/`last_error`), (A17–A20) the eight `pendingoauth` OAuth-marketplace/quirk columns
(`provider`/`code_verifier`/`auth_params`/`token_endpoint_auth_method`/`client_id_param`/`scope_separator`/
`long_lived_exchange`/`replaces_secret_id`). **Postgres BOOLEAN default fix (PR #22):** every boolean added
in-place uses `DEFAULT false`, never `DEFAULT 0` — Postgres rejects an integer default on a `BOOLEAN`
column (SQLite accepts both, so the test suite alone cannot catch it); `pendingoauth.long_lived_exchange`
is spelled `BOOLEAN NOT NULL DEFAULT false`, and the legacy `INSERT INTO org (…)` backfill names
`public_demo` explicitly with a `false` literal. **(A35) OAuth grant authority** backfills the new
`oauthgrant` table from each refresh family's oldest row with a portable, idempotent
`INSERT … SELECT … WHERE NOT EXISTS`; future team moves update that family row and leave historical
token `org_id` values untouched. Because a rolling deploy can run an old binary after A35 and create
only `OAuthRefresh`, request-time `_ensure_grant` repeats that oldest-row reconstruction with a
concurrency-safe upsert before refresh, grant listing, or a team move. **(A21) PROJECTS** follows the
same shape: the `project` table itself needs no ALTER (a brand-new table is created by `create_all`),
so the step only adds the three
columns that hang off it — `tool.project_id` (INTEGER, nullable) plus `project_access` (JSON, nullable) on
**both** `membership` and `invite`. Every one is nullable and NULL means *org-wide / unrestricted*, so an
existing deployment behaves exactly as before until someone creates a project; and because none of them is a
BOOLEAN, the Postgres integer-default trap does not apply here. Note what (A21) deliberately does NOT do:
`Tool.name` keeps its `(org_id, name)` unique constraint, because projects are a **label + ACL scope, not a
namespace** — making them a namespace would have meant rebuilding that constraint, which `_fix_tool_uniqueness`
already had to do once and which SQLite cannot express portably. **`DenyRule`** (policy) needs no migration
step at all for the same new-table reason.

> Health (`run_all`) takes an `org_id` filter so `/health/run` never leaks other orgs' credentials, and
> alerts resolve the owner's per-org membership webhook. See [auth-secrets](auth-secrets.md).

## Caller tags are a label, not a tenancy boundary

A builder reselling treg tags each call with their own ids (`X-Treg-Meta: customer=cust_8123,
workspace=ws_9`) so they can attribute, budget and invoice their users. Those tags drive real money
decisions — see [money](money.md) — but they change nothing about isolation.

**The org remains the only hard boundary.** A tag is caller-asserted: anyone holding the token can
send any value, exactly like `X-Treg-Client`. That is acceptable because every budget and every report
a tag touches belongs to the team that sent it, so the only party who can mis-tag is the one who owns
the consequences. It is *not* acceptable as a wall between mutually distrusting parties, and nothing
in the codebase treats it as one.

The rule to give builders: **tag for counting, token for control.** Start everyone on tags; mint a
scoped agent token for the few who need real separation — different tool access, or a credential that
runs on the end user's own machine. A pinned token (`Membership.pinned_tags`) is the one case where a
tag stops being caller-asserted: the pin beats the header and a mismatch is a 403, because a token
handed to one user must not be able to bill another.

Two consequences worth stating plainly:

- **`TagBudget` never grows a balance column.** One org, one balance. Budgets are ceilings on a shared
  pot, not sub-accounts; per-user balances would be a second money authority and are out of scope.
- **`TagSpend` and `TagBudget` are org-scoped** and registered in `api._ORG_SCOPED_MODELS`, `TagSpend`
  ahead of `LedgerEntry`/`Hold` because it references them. `tests/test_orgs.py` walks the models and
  fails if a new `org_id` table is missed.
