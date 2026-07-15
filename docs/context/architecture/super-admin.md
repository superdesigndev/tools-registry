---
title: Super-admin — cross-tenant read + control
status: shipped
sources:
  - src/treg/api.py
  - src/treg/config.py
related:
  - architecture/multi-tenancy.md
  - interface/cli.md
---

# Super-admin (the platform view above orgs)

Everything else is org-scoped; super-admin is the one capability that sees **across all orgs**. It's
deliberately separate from org roles.

## Authorization (`require_superadmin` in api.py) — hybrid
A caller is a super-admin if EITHER:
- the presented `X-Treg-Token` equals the env `admin_token` (`get_settings().admin_token`, from
  `TREG_ADMIN_TOKEN`), compared with `hmac.compare_digest` → principal `"env-admin"`; OR
- the token resolves to a `Membership` whose `User.is_superadmin` is set (and not `suspended`) →
  principal = that user's email.

Otherwise 403. The env key bootstraps; `POST /admin/users/{id}/superadmin` then grants named users the
flag (so a web portal can log in with either). Returns a principal string (for audit).

## Suspension enforcement (in `require_member`)
Two flags gate the **org-scoped** path: `require_member` raises 403 if `user.suspended` ("account
suspended") or `org.suspended` ("org suspended"). Set by the admin endpoints below. Super-admin
endpoints are unaffected (they use `require_superadmin`).

## Endpoints (all under `/admin/*`, gated by `require_superadmin`)
- **Reads:** `admin_stats` (totals, `tools_by_injector`/`tools_by_host`, `credential_health` rollup,
  call volume + success rate, `growth` counts — computed in-process over small result sets),
  `admin_orgs` (every org + member/role/tool/secret/bundle counts), `admin_org_detail`,
  `admin_users` (+ their memberships), `admin_tools`, `admin_calls`, `admin_health` (non-`ok` secrets).
- **Mutations (Phase 2):** `admin_set_superadmin`, `admin_suspend_user`, `admin_delete_user` (removes
  memberships, then `_cascade_delete_org` any org left with zero members, and **promotes a survivor to
  owner** in any org left without one), `admin_suspend_org`, `admin_delete_org` (force, cross-tenant).
  Org deletion shares `_cascade_delete_org` with the owner's own `delete_org` (one cascade helper: tools,
  secrets, bundles, pending-oauth, call records, memberships, then the org).
- **Last-superadmin floor:** `require_superadmin` returns the principal (`"env-admin"` or the user's
  email); the three destructive user ops refuse (`409`) when demoting/suspending/deleting would drop the
  count of active (`is_superadmin and not suspended`) users to zero — so a superadmin can't self-lock the
  platform out of `/admin/*`. The env token bypasses the floor (it can always recover).

## Model + migration
`User` gains `is_superadmin` + `suspended`; `Org` gains `suspended` (booleans). The startup migration
(`db._ensure_bool_col`) adds these columns to existing tables (`ALTER … ADD COLUMN … NOT NULL
DEFAULT 0`), so a live DB upgrades on restart.

## CLI (`interface/cli.md`)
`treg admin login --token` (saves the env key), `admin stats|orgs|org <id>|users|tools|calls|health`,
and `admin grant|revoke|suspend-user|rm-user|suspend-org|rm-org`. `_admin_client` sends the saved
`admin_token` if present, else the active org token (works for an `is_superadmin` user).
