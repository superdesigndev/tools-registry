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
- **`Org`** — `id, name, slug (unique), created_at`. The tenant that owns secrets/tools/bundles.
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
  token) — NOT reached by the dashboard/CLI login doors, which no longer auto-make an org. `create_org`
  (`POST /orgs`, `require_identity`),
  `list_orgs` (`GET /orgs`), `create_invite` (`POST /orgs/{id}/invites`, admin+), `accept_invite`
  (`POST /invites/accept`, open + code-protected → registers the user if new, joins them to the invited
  team, mints its token; a brand-new invitee joins the invited team **only** — no separate personal org),
  `list_members`
  / `remove_member` (`GET`/`DELETE /orgs/{id}/members[/{user}]`, admin+; owners cannot be removed).
  `_require_admin_of(org_id, caller)` gates the admin endpoints (token must be for that org + role ≥ admin).
- **Org administration:** `set_member_role` (`PATCH /orgs/{id}/members/{user}`, **owner-only** via
  `_require_owner_of`; a `_count_owners` last-owner guard blocks demoting the sole owner — ownership
  transfer = promote another to owner, then step down), `leave_org` (`POST /orgs/{id}/leave`, self-removal,
  same last-owner guard), `delete_org` (`DELETE /orgs/{id}`, owner-only, cascades every org-scoped row).
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
(create_all builds it NOT NULL with no server default). Verified in-place on Postgres.

> Health (`run_all`) takes an `org_id` filter so `/health/run` never leaks other orgs' credentials, and
> alerts resolve the owner's per-org membership webhook. See [auth-secrets](auth-secrets.md).
