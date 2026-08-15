"""Async DB engine + session. One read on the hot /call path, so keep it async + lean.

`init_db()` also runs a small, guarded, idempotent migration that folds a pre-orgs (flat) DB
into the multi-tenant shape: existing data lands in a default `superdesign` org, and the old
per-user token becomes an owner Membership. It is a no-op on a fresh or already-migrated DB.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from .config import get_settings

# `expire_on_commit=False` so objects stay usable after commit without a reload round-trip.
# On a real (non-SQLite) DB, add production pool hygiene: pre-ping to drop dead connections
# (Postgres/PgBouncer close idle ones → otherwise a post-idle request 500s) and a recycle window,
# sized against the relay's concurrency so bursts don't starve the pool + time out.
_db_url = get_settings().database_url
_engine_kwargs: dict = {"future": True}
if "sqlite" not in _db_url:
    _engine_kwargs.update(pool_pre_ping=True, pool_recycle=300, pool_size=20, max_overflow=40)
_engine = create_async_engine(_db_url, **_engine_kwargs)
# Public: the audit writer opens its own session here (off the request path — rule #2).
session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

# Resource tables that gained an `org_id` column in the orgs migration.
_ORG_SCOPED_TABLES = ("secret", "tool", "bundle", "callrecord")


async def init_db() -> None:
    # Import models so SQLModel.metadata is populated before create_all.
    from . import models  # noqa: F401

    # Guard against the silent-data-loss footgun: with no TREG_SECRET_KEY, crypto uses a per-process
    # EPHEMERAL Fernet key, so every stored secret becomes undecryptable after a restart. Fine for
    # local SQLite dev; refuse to start on a real DB (where that means losing every credential).
    s = get_settings()
    if not s.secret_key and "sqlite" not in s.database_url:
        raise RuntimeError(
            "TREG_SECRET_KEY is not set on a non-SQLite database — stored secrets would be lost on "
            "the next restart. Set a key (treg keygen) before starting."
        )
    if not s.secret_key:
        import logging
        logging.getLogger("treg").warning(
            "TREG_SECRET_KEY unset — using an EPHEMERAL key; stored secrets will not survive a restart."
        )

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.run_sync(_migrate_to_orgs)


def _migrate_to_orgs(conn) -> None:
    """Sync migration run inside `init_db`'s begin() block. Idempotent + guarded.

    (A) Additive DDL: ensure `org_id` exists on the resource tables (older SQLite DBs created
        before the orgs model won't have it; ADD COLUMN is safe + nullable).
    (B) Legacy backfill (only when the `org` table is empty AND a legacy `user.token_hash`
        column exists): create the default `superdesign` org, turn each legacy user's token into
        an owner Membership, stamp every existing resource with the default org, then rebuild the
        `user` table so it sheds the now-removed legacy columns (token_hash/org/webhook_url).
    """
    insp = inspect(conn)
    tables = set(insp.get_table_names())

    # (A) additive org_id columns (idempotent)
    for tbl in _ORG_SCOPED_TABLES:
        if tbl in tables and "org_id" not in {c["name"] for c in insp.get_columns(tbl)}:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN org_id INTEGER"))

    # (A2) additive: invite.expires_at (added after invites shipped; nullable = never-expires)
    if "invite" in tables and "expires_at" not in {c["name"] for c in insp.get_columns("invite")}:
        # TIMESTAMP, not DATETIME — Postgres has no DATETIME type (sqlite accepts both).
        conn.execute(text("ALTER TABLE invite ADD COLUMN expires_at TIMESTAMP"))

    # (A3) additive: super-admin + suspension flags (added after orgs shipped)
    _ensure_bool_col(conn, insp, tables, "user", "is_superadmin")
    _ensure_bool_col(conn, insp, tables, "user", "suspended")
    _ensure_bool_col(conn, insp, tables, "org", "suspended")

    # (A5) additive: onboarding first-run flag + demo-sandbox markers
    _ensure_bool_col(conn, insp, tables, "user", "onboarded")
    _ensure_bool_col(conn, insp, tables, "user", "demo")
    _ensure_bool_col(conn, insp, tables, "org", "demo")

    # (A15) additive: org.public_demo — a team whose member token is published; locked to /call + reads.
    _ensure_bool_col(conn, insp, tables, "org", "public_demo")

    # (A4) additive: tool.examples (JSON list of {method,path,note}) for the dashboard
    if "tool" in tables and "examples" not in {c["name"] for c in insp.get_columns("tool")}:
        conn.execute(text("ALTER TABLE tool ADD COLUMN examples JSON"))

    # (A6) additive: bundle.files (JSON {relpath: content}) so a whole skill folder travels, not just SKILL.md
    if "bundle" in tables and "files" not in {c["name"] for c in insp.get_columns("bundle")}:
        conn.execute(text("ALTER TABLE bundle ADD COLUMN files JSON"))

    # (A8) additive: tool.cli (JSON run profile for `treg run`, both tiers — docs/CLI-RUN-PLAN.md)
    if "tool" in tables and "cli" not in {c["name"] for c in insp.get_columns("tool")}:
        conn.execute(text("ALTER TABLE tool ADD COLUMN cli JSON"))

    # (A7→A8b) fold legacy bundle run-metadata into Tool.cli (the tool-side unification: one `cli`
    # profile drives BOTH run tiers — docs/CLI-RUN-PLAN.md). Older DBs carry runtime/package/
    # entrypoint/runnable columns on bundle (the model no longer declares them; fresh DBs never get
    # them). For each still-runnable bundle: merge bin/server/package/runtime into its tool's cli
    # block (existing keys win — a contract-declared bin is authoritative); a tool-less runnable
    # bundle gets a minimal CLI-only tool row. Then flip runnable off so the fold is idempotent.
    # Runs after (A8) so tool.cli exists on an old DB being upgraded in place.
    if "bundle" in tables and "runnable" in {c["name"] for c in insp.get_columns("bundle")}:
        _fold_bundle_run_meta(conn)

    # (A9) additive: user.token_version — per-user token revocation (bump = invalidate all issued tokens).
    # "user" is quoted (a reserved word in Postgres) because this ALTER runs in-place on the live PG DB.
    if "user" in tables and "token_version" not in {c["name"] for c in insp.get_columns("user")}:
        conn.execute(text('ALTER TABLE "user" ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0'))

    # (A10) additive: membership.daily_call_cap — per-user, per-day usage cap (-1 = unlimited). See
    # api._enforce_daily_cap. Plain INTEGER default; `membership` is not a reserved word (no quoting).
    if "membership" in tables and "daily_call_cap" not in {c["name"] for c in insp.get_columns("membership")}:
        conn.execute(text("ALTER TABLE membership ADD COLUMN daily_call_cap INTEGER NOT NULL DEFAULT -1"))

    # (A11) additive: callrecord.kind — "call" (proxy) vs "local_run" (grant), for the usage breakdown.
    if "callrecord" in tables and "kind" not in {c["name"] for c in insp.get_columns("callrecord")}:
        conn.execute(text("ALTER TABLE callrecord ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'call'"))

    # (A12) additive: per-member tool ACL — membership+invite tool_access (JSON, NULL=all tools) and
    # local_run_enabled (BOOLEAN, default true). See api._require_tool_access / _require_local_run.
    for tbl in ("membership", "invite"):
        if tbl not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(tbl)}
        if "tool_access" not in cols:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN tool_access JSON"))  # nullable → NULL = all
        if "local_run_enabled" not in cols:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN local_run_enabled BOOLEAN NOT NULL DEFAULT true"))

    # (A13) additive: invite.email_token_hash — the inbox-only sign-in secret for the emailed invite
    # link (split from the admin-visible code; see models.Invite). Nullable: old invites simply have
    # no link-sign-in and fall back to the prefilled-login flow.
    if "invite" in tables and "email_token_hash" not in {c["name"] for c in insp.get_columns("invite")}:
        conn.execute(text("ALTER TABLE invite ADD COLUMN email_token_hash VARCHAR"))

    # (A14) additive: invite.landing — the shared detail page ("/app/skills/<name>") the invitee
    # lands on after invite-signin. Nullable: a plain invite lands on the dashboard as before.
    if "invite" in tables and "landing" not in {c["name"] for c in insp.get_columns("invite")}:
        conn.execute(text("ALTER TABLE invite ADD COLUMN landing VARCHAR"))

    # (A16) additive: connection metadata on secret — which registry provider minted it, the scopes
    # actually granted, the resource it acts on, and the expiry axis (see models.Secret). All
    # nullable/defaulted: an existing uploaded secret simply has no provider and no expiry.
    if "secret" in tables:
        cols = {c["name"] for c in insp.get_columns("secret")}
        for col, ddl in (
            ("provider", "VARCHAR NOT NULL DEFAULT ''"),
            ("granted_scopes", "VARCHAR NOT NULL DEFAULT ''"),
            ("resource_ref", "VARCHAR NOT NULL DEFAULT ''"),
            ("resource_name", "VARCHAR NOT NULL DEFAULT ''"),
            ("expires_at", "TIMESTAMP"),
            ("last_refresh_at", "TIMESTAMP"),
            ("last_error", "VARCHAR NOT NULL DEFAULT ''"),
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE secret ADD COLUMN {col} {ddl}"))

    # (A17) additive: pendingoauth.provider — carried through the redirect so the callback knows
    # which registry provider to attribute the credential to and auto-provision a tool for.
    if "pendingoauth" in tables:
        cols = {c["name"] for c in insp.get_columns("pendingoauth")}
        for col, ddl in (
            ("provider", "VARCHAR NOT NULL DEFAULT ''"),
            # (A18) per-provider auth quirks captured at start, so the callback exchanges the code
            # exactly the way the consent URL was built (PKCE verifier, basic vs post, extra params).
            ("code_verifier", "VARCHAR NOT NULL DEFAULT ''"),
            ("auth_params", "VARCHAR NOT NULL DEFAULT ''"),
            ("token_endpoint_auth_method", "VARCHAR NOT NULL DEFAULT 'client_secret_post'"),
            # (A19) same idea, one layer lower: TikTok renames the client param and comma-joins
            # scopes. Defaulted to the OAuth2 spelling, so in-flight connects are unaffected.
            ("client_id_param", "VARCHAR NOT NULL DEFAULT 'client_id'"),
            ("scope_separator", "VARCHAR NOT NULL DEFAULT ' '"),
            # DEFAULT false, not 0 — Postgres rejects an integer default on a BOOLEAN column
            # (sqlite accepts both, so the test suite cannot catch this; see _ensure_bool_col).
            ("long_lived_exchange", "BOOLEAN NOT NULL DEFAULT false"),
            # (A20) null means "this consent adds a new connection"; an id means "it replaces that
            # one". Null is the safe default for anything already in flight.
            ("replaces_secret_id", "INTEGER"),
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE pendingoauth ADD COLUMN {col} {ddl}"))

    # (A21) additive: PROJECTS — an optional sub-scope inside an org. The `project` table itself is made
    # by create_all (a brand-new table needs no ALTER); these are the three columns that hang off it.
    # All nullable, and NULL everywhere means "org-wide / unrestricted" — so an existing deployment
    # behaves exactly as before until someone creates a project. No BOOLEAN here, so the Postgres
    # integer-default trap (PR #22) doesn't apply. See api._project_allowed / models.Project.
    if "tool" in tables and "project_id" not in {c["name"] for c in insp.get_columns("tool")}:
        conn.execute(text("ALTER TABLE tool ADD COLUMN project_id INTEGER"))  # NULL = org-wide tool
    for tbl in ("membership", "invite"):
        if tbl in tables and "project_access" not in {c["name"] for c in insp.get_columns(tbl)}:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN project_access JSON"))  # NULL = whole org

    # (A22) additive: project-scoped POLICY — a deny rule may name a project, so "block DELETE in
    # the ads project" is expressible. Nullable, NULL = any tool, so existing rules keep their exact
    # meaning; not a BOOLEAN, so the Postgres integer-default trap (PR #22) doesn't apply.
    if "denyrule" in tables and "project_id" not in {c["name"] for c in insp.get_columns("denyrule")}:
        conn.execute(text("ALTER TABLE denyrule ADD COLUMN project_id INTEGER"))  # NULL = any tool

    # (A23) additive: RUNTIME ATTRIBUTION — which coding agent made the call (X-Treg-Client, self-
    # reported by the CLI). '' = unreported, so old rows and non-CLI callers read as before. String
    # default, not BOOLEAN — the PR #22 trap doesn't apply.
    for tbl in ("callrecord", "runrecord"):
        if tbl in tables and "client" not in {c["name"] for c in insp.get_columns(tbl)}:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN client VARCHAR NOT NULL DEFAULT ''"))
    # …and who minted a membership (agents name their creating admin; '' for door/invite joins).
    if "membership" in tables and "created_by" not in {c["name"] for c in insp.get_columns("membership")}:
        conn.execute(text("ALTER TABLE membership ADD COLUMN created_by VARCHAR NOT NULL DEFAULT ''"))

    # (A24) additive: which observed (member|runtime) pair an agent was promoted from — lets the
    # detected roster drop a pair once it has its own identity, and resurface it on revoke.
    if "membership" in tables and "promoted_from" not in {c["name"] for c in insp.get_columns("membership")}:
        conn.execute(text("ALTER TABLE membership ADD COLUMN promoted_from VARCHAR NOT NULL DEFAULT ''"))

    # (A25) additive: org.balance_micro — the materialized prepaid balance in micro-USD (see
    # models.Org / ledger.py). The three ledger TABLES (creditblock/ledgerentry/hold) are brand new,
    # so create_all makes them and they need no ALTER; only this column hangs off an existing table.
    # INTEGER NOT NULL DEFAULT 0 — an existing org starts at zero balance, which is exactly right
    # (promo credit is granted at org creation, never backfilled). Not a BOOLEAN, so the Postgres
    # integer-default trap (PR #22, see _ensure_bool_col) doesn't apply.
    if "org" in tables and "balance_micro" not in {c["name"] for c in insp.get_columns("org")}:
        conn.execute(text("ALTER TABLE org ADD COLUMN balance_micro INTEGER NOT NULL DEFAULT 0"))

    # (A26) additive: STRIPE BILLING on org — the customer/payment-method references and the
    # auto-top-up policy (see models.Org / billing.py). Every column is defaulted to the OFF state, so
    # an existing org keeps behaving exactly as it did: no customer, no card, auto-top-up disabled.
    # `autotopup_enabled` goes through _ensure_bool_col for the Postgres trap it documents (DEFAULT
    # false, never 0); the rest are INTEGER/VARCHAR/TIMESTAMP where that trap doesn't apply.
    if "org" in tables:
        _ensure_bool_col(conn, insp, tables, "org", "autotopup_enabled")
        cols = {c["name"] for c in insp.get_columns("org")}
        for col, ddl in (
            ("stripe_customer_id", "VARCHAR"),
            ("stripe_default_pm", "VARCHAR"),
            ("autotopup_threshold_micro", "INTEGER NOT NULL DEFAULT 0"),  # 0 = use the settings default
            ("autotopup_amount_micro", "INTEGER NOT NULL DEFAULT 0"),
            ("autotopup_monthly_cap_micro", "INTEGER NOT NULL DEFAULT 0"),
            ("autotopup_consented_at", "TIMESTAMP"),      # null = no mandate = no off-session charge
            ("autotopup_disabled_reason", "VARCHAR"),
            ("autotopup_last_attempt_at", "TIMESTAMP"),
            ("autotopup_failures", "INTEGER NOT NULL DEFAULT 0"),
            ("autotopup_recovery_pi", "VARCHAR"),
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE org ADD COLUMN {col} {ddl}"))

    # (A27) additive: MARKETPLACE TELEMETRY on callrecord — which catalog endpoint a /call resolved to,
    # which credential tier paid for it, the estimated/observed cost, and the shape of the answer (see
    # models.CallRecord). Every column is NULLABLE with no default: an existing row genuinely has no
    # endpoint id, and "unknown" must not read as 0 cost. Nullable also keeps the Postgres
    # boolean-default trap (PR #22) out of the picture entirely.
    if "callrecord" in tables:
        cols = {c["name"] for c in insp.get_columns("callrecord")}
        for col, ddl in (
            ("endpoint_id", "VARCHAR"),
            ("provider", "VARCHAR"),
            ("credential_tier", "VARCHAR"),
            ("cost_estimated_micro", "INTEGER"),
            ("cost_observed_micro", "INTEGER"),
            ("cost_charged_micro", "INTEGER"),  # added later (same per-column guard; idempotent)
            ("duration_ms", "INTEGER"),
            ("response_bytes", "INTEGER"),
            ("params_hash", "VARCHAR"),
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE callrecord ADD COLUMN {col} {ddl}"))

    # (A29) additive: callrecord.refused_by — set when treg itself refused the call pre-relay
    # (auth/policy/balance/cap/resolution/request; see models.CallRecord). NULLABLE: NULL means the
    # provider answered, and every pre-existing row that reached upstream is correct as NULL.
    if "callrecord" in tables and "refused_by" not in {c["name"] for c in insp.get_columns("callrecord")}:
        conn.execute(text("ALTER TABLE callrecord ADD COLUMN refused_by VARCHAR"))

    # (A28) corrective: creditblock.stripe_payment_intent must be UNIQUE (the top-up idempotency
    # key). It sits HERE, above the (B) block, because (B) returns early on a fresh/new-schema DB —
    # and a fresh DB created between the ledger landing and this fix is precisely the one that has
    # the table with only a plain index.
    if "creditblock" in tables:
        _ensure_payment_ref_unique(conn, insp)

    # (B) legacy backfill — guarded
    if "org" not in tables:
        return  # defensive: create_all should have made it
    if conn.execute(text("SELECT COUNT(*) FROM org")).scalar():
        return  # already migrated
    user_cols = {c["name"] for c in insp.get_columns("user")} if "user" in tables else set()
    if "token_hash" not in user_cols:
        return  # fresh / new-schema DB — nothing legacy to migrate

    now = datetime.now(timezone.utc).isoformat(sep=" ")
    legacy_users = conn.execute(
        text('SELECT id, email, token_hash, webhook_url, created_at FROM "user"')
    ).fetchall()

    if legacy_users:
        conn.execute(
            # balance_micro and the autotopup_* columns are explicit for the same reason
            # daily_call_cap is below: create_all builds them NOT NULL with no SERVER default, so a raw
            # INSERT must supply the model's default (zero balance, auto-top-up off, no failures).
            text("INSERT INTO org (name, slug, suspended, demo, public_demo, balance_micro, "
                 "autotopup_enabled, autotopup_threshold_micro, autotopup_amount_micro, "
                 "autotopup_monthly_cap_micro, autotopup_failures, created_at) "
                 "VALUES ('superdesign', 'superdesign', false, false, false, 0, false, 0, 0, 0, 0, :t)"),
            {"t": now},
        )
        org_id = conn.execute(text("SELECT id FROM org WHERE slug = 'superdesign'")).scalar()
        # every flat-era user was unscoped/all-powerful → make each an owner of the default org,
        # carrying their existing token so nothing they hold breaks.
        for row in legacy_users:
            conn.execute(
                text(  # daily_call_cap + local_run_enabled are explicit: create_all builds them NOT NULL
                       # with no server default, so this raw INSERT must supply their defaults (unlimited /
                       # local runs allowed). tool_access is nullable (NULL = all tools) so it's omitted.
                    "INSERT INTO membership (user_id, org_id, role, token_hash, webhook_url, daily_call_cap, local_run_enabled, created_by, promoted_from, created_at) "
                    "VALUES (:uid, :oid, 'owner', :th, :wh, -1, 1, '', '', :ct)"
                ),
                {"uid": row.id, "oid": org_id, "th": row.token_hash, "wh": row.webhook_url, "ct": row.created_at or now},
            )
        for tbl in _ORG_SCOPED_TABLES:
            if tbl in tables:
                conn.execute(text(f"UPDATE {tbl} SET org_id = :o WHERE org_id IS NULL"), {"o": org_id})

    if "tool" in tables:
        _fix_tool_uniqueness(conn)
    _rebuild_user_table(conn)


def _fold_bundle_run_meta(conn) -> None:
    """One-time fold of the legacy bundle-side run metadata (runtime/package/entrypoint/runnable)
    into the tool-side `cli` profile. Idempotent: processed bundles get runnable=false, so a rerun
    finds nothing. Engine-portable: JSON is (de)serialized in Python, booleans use true/false
    literals, and rows are updated with bound parameters."""
    import json as _json

    rows = conn.execute(text(
        "SELECT id, org_id, name, owner, runtime, package, entrypoint FROM bundle WHERE runnable = true"
    )).fetchall()
    # Naive UTC datetime, NOT a string: asyncpg rejects a str bound into a TIMESTAMP column, and
    # rejects tz-aware into TIMESTAMP WITHOUT TIME ZONE (the models._now convention).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for b in rows:
        merged_keys = {}
        if b.entrypoint:
            merged_keys["bin"] = b.entrypoint
        if b.package:
            merged_keys["package"] = b.package
        if b.runtime:
            merged_keys["runtime"] = b.runtime
        tool = conn.execute(text(
            "SELECT id, cli FROM tool WHERE bundle_id = :bid ORDER BY id LIMIT 1"), {"bid": b.id}
        ).fetchone()
        if tool is not None:
            cli = tool.cli if isinstance(tool.cli, dict) else _json.loads(tool.cli) if tool.cli else {}
            for k, v in merged_keys.items():
                cli.setdefault(k, v)  # existing keys win — a contract-declared bin is authoritative
            conn.execute(text("UPDATE tool SET cli = :cli WHERE id = :tid"),
                         {"cli": _json.dumps(cli), "tid": tool.id})
        else:
            # a runnable bundle with no tool → give it a minimal CLI-only tool row (local tier off:
            # there is no inject profile to grant; server tier on, matching its old behavior).
            cli = dict(merged_keys, enabled=False)
            cli.setdefault("bin", b.name)
            conn.execute(text(
                "INSERT INTO tool (org_id, name, owner, base_url, host, bindings, examples, cli, bundle_id, created_at) "
                "VALUES (:org, :name, :owner, '', '', :bindings, :examples, :cli, :bid, :t)"),
                {"org": b.org_id, "name": b.name, "owner": b.owner, "bindings": "[]", "examples": "[]",
                 "cli": _json.dumps(cli), "bid": b.id, "t": now})
    if rows:  # one statement for the whole batch (not per-row) — this is what makes reruns no-ops
        conn.execute(text("UPDATE bundle SET runnable = false WHERE runnable = true"))


def _ensure_bool_col(conn, insp, tables, table: str, col: str) -> None:
    """Idempotently add a NOT-NULL boolean column defaulting to false."""
    if table in tables and col not in {c["name"] for c in insp.get_columns(table)}:
        # DEFAULT false, not 0 — Postgres rejects an integer default on a BOOLEAN column (sqlite accepts both).
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT false"))


def _ensure_payment_ref_unique(conn, insp) -> None:
    """`creditblock.stripe_payment_intent` must be UNIQUE — it is the idempotency key for a Stripe
    top-up, and `ledger.topup` checks it with a SELECT before the INSERT. Without the constraint two
    concurrent deliveries of one PaymentIntent both credit the org.

    `create_all` gives a NEW database the unique index for free; this is for one created between the
    ledger landing and this fix. If duplicate refs already exist the index cannot be built — say so
    loudly and leave the data alone, because deciding which of two credits to void is a money
    decision, not a migration's call.
    """
    idx = insp.get_indexes("creditblock")
    if any(i.get("unique") and i.get("column_names") == ["stripe_payment_intent"] for i in idx):
        return
    dupes = conn.execute(text(
        "SELECT stripe_payment_intent, COUNT(*) c FROM creditblock "
        "WHERE stripe_payment_intent IS NOT NULL GROUP BY stripe_payment_intent HAVING COUNT(*) > 1"
    )).fetchall()
    if dupes:
        logging.getLogger("treg.db").error(
            "creditblock has %d payment reference(s) credited more than once (%s); the UNIQUE index "
            "was NOT created. Resolve the duplicates by hand — voiding a credit is a money decision.",
            len(dupes), ", ".join(str(d[0]) for d in dupes[:5]))
        return
    for name in ("ix_creditblock_stripe_payment_intent",):   # drop the plain index it replaces
        if any(i.get("name") == name for i in idx):
            conn.execute(text(f"DROP INDEX {name}"))
    conn.execute(text("CREATE UNIQUE INDEX ix_creditblock_stripe_payment_intent "
                      "ON creditblock (stripe_payment_intent)"))


def _fix_tool_uniqueness(conn) -> None:
    """Flat-era `tool.name` had a GLOBAL unique index (ix_tool_name). Relax it to per-org:
    drop the global-unique index and add a composite UNIQUE(org_id, name), keeping a plain
    lookup index on name. Idempotent-safe (only reached once, inside the guarded backfill).
    """
    idxs = inspect(conn).get_indexes("tool")
    have_composite = any(ix.get("unique") and ix.get("column_names") == ["org_id", "name"] for ix in idxs)
    for ix in idxs:  # drop any unique index over `name` alone
        if ix.get("unique") and ix.get("column_names") == ["name"]:
            conn.execute(text(f'DROP INDEX {ix["name"]}'))
    if not have_composite:
        conn.execute(text("CREATE UNIQUE INDEX uq_tool_org_name ON tool (org_id, name)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tool_name ON tool (name)"))


def _rebuild_user_table(conn) -> None:
    """SQLite can't drop columns portably, so rebuild `user` as the current identity shape, copying
    the rows. Drops the legacy token_hash/org/webhook_url columns that would otherwise break new
    inserts (token_hash was NOT NULL with no default) — but MUST preserve the current-schema
    `is_superadmin`/`suspended` columns (added just above by _ensure_bool_col); omitting them here
    silently deletes them, and since a re-run short-circuits, the API then 500s on every User load.
    """
    conn.execute(
        text("CREATE TABLE user_new (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, "
             "is_superadmin BOOLEAN NOT NULL DEFAULT 0, suspended BOOLEAN NOT NULL DEFAULT 0, "
             "token_version INTEGER NOT NULL DEFAULT 0, "
             "onboarded BOOLEAN NOT NULL DEFAULT 0, demo BOOLEAN NOT NULL DEFAULT 0, "
             "created_at DATETIME)")
    )
    conn.execute(text(
        'INSERT INTO user_new (id, email, is_superadmin, suspended, token_version, onboarded, demo, created_at) '
        'SELECT id, email, is_superadmin, suspended, token_version, onboarded, demo, created_at FROM "user"'
    ))
    conn.execute(text('DROP TABLE "user"'))
    conn.execute(text('ALTER TABLE user_new RENAME TO "user"'))
    conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email ON "user" (email)'))


async def reset_db() -> None:
    """Drop + recreate all tables. Test-only: gives each test a clean registry."""
    from . import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_maker() as session:
        yield session
