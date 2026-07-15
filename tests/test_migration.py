"""PR1 — the guarded startup migration folds a pre-orgs (flat) DB into the multi-tenant shape.

We build a legacy-shaped SQLite DB by hand (the exact tables/columns the flat era shipped:
`user.token_hash`, a globally-unique `tool.name`, no `org_id` anywhere), then run the real
migration (`SQLModel.metadata.create_all` + `_migrate_to_orgs`, exactly as `init_db` does) and
assert: a default `superdesign` org exists, every legacy user became an owner Membership carrying
its original token, resources landed in the default org, the `user` table shed `token_hash`, and
`tool.name` uniqueness is now per-`(org_id, name)`.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from treg import crypto
from treg.db import _migrate_to_orgs

_LEGACY_USER = """
CREATE TABLE "user" (
  id INTEGER NOT NULL PRIMARY KEY, email VARCHAR NOT NULL, org VARCHAR NOT NULL,
  token_hash VARCHAR NOT NULL, webhook_url VARCHAR, created_at DATETIME NOT NULL
)"""
_LEGACY_TOOL = """
CREATE TABLE tool (
  id INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, owner VARCHAR NOT NULL,
  base_url VARCHAR NOT NULL, host VARCHAR NOT NULL, bindings JSON, health_check JSON,
  bundle_id INTEGER, created_at DATETIME NOT NULL
)"""
_LEGACY_SECRET = """
CREATE TABLE secret (
  id INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, owner VARCHAR NOT NULL,
  kind VARCHAR NOT NULL, value VARCHAR NOT NULL, bundle_id INTEGER,
  health_status VARCHAR, health_detail VARCHAR, health_checked_at DATETIME, created_at DATETIME NOT NULL
)"""


async def _seed_legacy(engine, token: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(_LEGACY_USER))
        await conn.execute(text(_LEGACY_TOOL))
        await conn.execute(text(_LEGACY_SECRET))
        await conn.execute(text("CREATE UNIQUE INDEX ix_tool_name ON tool (name)"))  # flat-era global unique
        await conn.execute(
            text('INSERT INTO "user" (email, org, token_hash, created_at) VALUES (:e, \'default\', :th, :t)'),
            {"e": "unclecode@superdesign.dev", "th": crypto.hash_token(token), "t": "2026-07-01 00:00:00"},
        )
        await conn.execute(
            text('INSERT INTO "user" (email, org, token_hash, created_at) VALUES (:e, \'default\', :th, :t)'),
            {"e": "second@superdesign.dev", "th": crypto.hash_token("other"), "t": "2026-07-01 00:00:00"},
        )
        await conn.execute(
            text("INSERT INTO tool (name, owner, base_url, host, bindings, created_at) "
                 "VALUES ('stripe', 'unclecode@superdesign.dev', 'https://api.stripe.com', 'api.stripe.com', '[]', :t)"),
            {"t": "2026-07-01 00:00:00"},
        )
        await conn.execute(
            text("INSERT INTO secret (name, owner, kind, value, created_at) "
                 "VALUES ('k', 'unclecode@superdesign.dev', 'env', 'enc', :t)"),
            {"t": "2026-07-01 00:00:00"},
        )


async def test_legacy_db_migrates_into_default_org(tmp_path):
    token = crypto.new_token()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'legacy.db'}")
    try:
        await _seed_legacy(engine, token)
        # exactly what init_db does: create the new tables, then migrate.
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            await conn.run_sync(_migrate_to_orgs)

        async with engine.connect() as conn:
            org = (await conn.execute(text("SELECT id, slug FROM org"))).one()
            assert org.slug == "superdesign"

            # both legacy users became OWNER memberships in the default org; the original token survives.
            members = (await conn.execute(text("SELECT user_id, org_id, role, token_hash FROM membership"))).all()
            assert len(members) == 2
            assert all(m.role == "owner" and m.org_id == org.id for m in members)
            assert crypto.hash_token(token) in {m.token_hash for m in members}

            # resources backfilled into the default org.
            assert (await conn.execute(text("SELECT org_id FROM tool"))).scalar() == org.id
            assert (await conn.execute(text("SELECT org_id FROM secret"))).scalar() == org.id

            # user table rebuilt as the current identity shape (legacy columns gone, but the
            # current-schema is_superadmin/suspended columns MUST survive — else every User load 500s).
            user_cols = {r[1] for r in (await conn.execute(text('PRAGMA table_info("user")'))).all()}
            assert "token_hash" not in user_cols and "org" not in user_cols
            assert {"id", "email", "created_at", "is_superadmin", "suspended"} <= user_cols
            # and they're actually queryable (the regression that bricked legacy upgrades)
            assert (await conn.execute(text("SELECT COUNT(*) FROM user WHERE is_superadmin = 0"))).scalar() == 2

            # tool uniqueness is now per-(org_id, name): the global-unique index is gone, composite present.
            tool_idxs = (await conn.execute(text("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='tool'"))).all()
            assert any(ix.name == "uq_tool_org_name" for ix in tool_idxs)
            assert not any(ix.sql and "UNIQUE" in ix.sql and ix.name == "ix_tool_name" for ix in tool_idxs)

            # additive columns landed on the legacy tool table (A4 examples, A6 cli for `treg run`)
            tool_cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(tool)"))).all()}
            assert {"examples", "cli"} <= tool_cols
    finally:
        await engine.dispose()


async def test_usage_columns_added_in_place_to_pre_feature_tables(tmp_path):
    """membership.daily_call_cap (-1) + callrecord.kind ('call') are added in-place by the migration to
    a DB whose tables predate the feature — the real prod upgrade path, where create_all leaves existing
    tables untouched and only the additive ALTER runs. Existing rows get the defaults."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'preusage.db'}")
    try:
        async with engine.begin() as conn:  # tables shaped BEFORE this feature (no new columns)
            await conn.execute(text(
                "CREATE TABLE membership (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "org_id INTEGER NOT NULL, role VARCHAR NOT NULL DEFAULT 'member', token_hash VARCHAR NOT NULL, "
                "webhook_url VARCHAR, created_at DATETIME)"))
            await conn.execute(text(
                "CREATE TABLE callrecord (id INTEGER PRIMARY KEY, org_id INTEGER, user_email VARCHAR NOT NULL, "
                "tool_name VARCHAR NOT NULL, method VARCHAR, path VARCHAR, status_code INTEGER, created_at DATETIME)"))
            await conn.execute(text("INSERT INTO membership (user_id, org_id, role, token_hash, created_at) "
                                    "VALUES (1, 1, 'member', 'th', '2026-07-01 00:00:00')"))
            await conn.execute(text("INSERT INTO callrecord (org_id, user_email, tool_name, method, path, "
                                    "status_code, created_at) VALUES (1, 'a@x.io', 'stripe', 'GET', '/v1', 200, "
                                    "'2026-07-01 00:00:00')"))
        for _ in range(2):  # idempotent: the column-existence guard makes a re-run a no-op
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)  # leaves existing membership/callrecord alone
                await conn.run_sync(_migrate_to_orgs)              # the additive ALTERs run here

        async with engine.connect() as conn:
            mcols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(membership)"))).all()}
            ccols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(callrecord)"))).all()}
            assert "daily_call_cap" in mcols and "kind" in ccols
            assert (await conn.execute(text("SELECT daily_call_cap FROM membership"))).scalar() == -1
            assert (await conn.execute(text("SELECT kind FROM callrecord"))).scalar() == "call"
    finally:
        await engine.dispose()


async def test_invite_email_token_column_added_in_place(tmp_path):
    """invite.email_token_hash (the inbox-only sign-in secret, A12) is added in-place to a DB whose
    invite table predates the split-secret feature; existing invites get NULL (no link sign-in —
    they fall back to the prefilled-login flow)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'preemailtok.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE invite (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, "
                "email VARCHAR NOT NULL, role VARCHAR NOT NULL DEFAULT 'member', code_hash VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL DEFAULT 'pending', invited_by VARCHAR NOT NULL DEFAULT '', "
                "expires_at TIMESTAMP, created_at DATETIME)"))
            await conn.execute(text(
                "INSERT INTO invite (org_id, email, code_hash, created_at) "
                "VALUES (1, 'bob@x.io', 'ch', '2026-07-01 00:00:00')"))
        for _ in range(2):  # idempotent: the column-existence guard makes a re-run a no-op
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
                await conn.run_sync(_migrate_to_orgs)
        async with engine.connect() as conn:
            cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(invite)"))).all()}
            assert "email_token_hash" in cols
            assert (await conn.execute(text("SELECT email_token_hash FROM invite"))).scalar() is None
    finally:
        await engine.dispose()


async def test_migration_is_idempotent(tmp_path):
    token = crypto.new_token()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'legacy2.db'}")
    try:
        await _seed_legacy(engine, token)
        for _ in range(2):  # running init_db twice must not double-migrate or error
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
                await conn.run_sync(_migrate_to_orgs)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT COUNT(*) FROM org"))).scalar() == 1
            assert (await conn.execute(text("SELECT COUNT(*) FROM membership"))).scalar() == 2
    finally:
        await engine.dispose()


async def test_bundle_run_meta_folds_into_tool_cli(tmp_path):
    """The tool-side unification fold: an old DB whose bundles carry runtime/package/entrypoint/
    runnable gets that metadata merged into each bundle's tool `cli` block (existing keys win);
    a tool-less runnable bundle gets a minimal CLI-only tool row. Idempotent (runnable flips off)."""
    import json

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'fold.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            # recreate the legacy bundle-side run columns (the current model no longer declares them)
            for col in ("runtime", "package", "entrypoint"):
                await conn.execute(text(f"ALTER TABLE bundle ADD COLUMN {col} VARCHAR"))
            await conn.execute(text("ALTER TABLE bundle ADD COLUMN runnable BOOLEAN NOT NULL DEFAULT false"))
            t = "2026-07-01 00:00:00"
            # bundle 1: runnable, HAS a tool whose cli already declares a bin (contract wins)
            await conn.execute(text(
                "INSERT INTO bundle (org_id, name, owner, recipe, files, runtime, package, entrypoint, runnable, created_at) "
                "VALUES (1, 'with-tool', 'o@x', '', '{}', 'node', 'agentmail-cli', 'agentmail', true, :t)"), {"t": t})
            await conn.execute(text(
                "INSERT INTO tool (org_id, name, owner, base_url, host, bindings, examples, cli, bundle_id, created_at) "
                "VALUES (1, 'with-tool', 'o@x', 'https://api.x.com', 'api.x.com', '[]', '[]', :cli, 1, :t)"),
                {"cli": json.dumps({"bin": "declared-bin", "enabled": True}), "t": t})
            # bundle 2: runnable, NO tool → a minimal CLI-only tool row must be created
            await conn.execute(text(
                "INSERT INTO bundle (org_id, name, owner, recipe, files, entrypoint, runnable, created_at) "
                "VALUES (1, 'toolless', 'o@x', '', '{}', 'sh', true, :t)"), {"t": t})
            await conn.run_sync(_migrate_to_orgs)
            await conn.run_sync(_migrate_to_orgs)  # idempotent: the second pass finds nothing runnable

        async with engine.connect() as conn:
            cli1 = json.loads((await conn.execute(
                text("SELECT cli FROM tool WHERE name = 'with-tool'"))).scalar())
            assert cli1["bin"] == "declared-bin"          # the contract-declared bin wins over entrypoint
            assert cli1["package"] == "agentmail-cli" and cli1["runtime"] == "node"
            cli2 = json.loads((await conn.execute(
                text("SELECT cli FROM tool WHERE name = 'toolless'"))).scalar())
            assert cli2["bin"] == "sh" and cli2["enabled"] is False
            assert (await conn.execute(text("SELECT COUNT(*) FROM tool WHERE name = 'toolless'"))).scalar() == 1
            assert (await conn.execute(text("SELECT COUNT(*) FROM bundle WHERE runnable = true"))).scalar() == 0
    finally:
        await engine.dispose()
