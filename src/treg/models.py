"""The registry data model — minimal on purpose (charter: don't invent extra nouns).

Multi-tenancy (orgs) shape: an **Org** is the tenant that owns resources; a **User** is a
global identity; a **Membership** links a user to an org with a role and IS where the caller's
token lives (a token = a (user, org) pair). Secrets/tools/bundles/call records carry `org_id`,
so every list/call/mutation is scoped to the caller's org. See docs/MULTI-TENANCY-PLAN.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

# Role ordering for gates (owner > admin > member > viewer).
# viewer = read + call only (cannot register/manage); member+ can register tools/secrets/skills.
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


def _now() -> datetime:
    # Naive UTC (drop tzinfo): our datetime columns are TIMESTAMP WITHOUT TIME ZONE, and Postgres
    # (asyncpg) rejects a tz-aware value into a naive column. The rest of the app already stores +
    # compares naive UTC (api._utcnow_naive / _as_naive); keep models consistent. SQLite is lax, so
    # this only bites on Postgres — the deploy target.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Org(SQLModel, table=True):
    """A tenant (team). Owns secrets/tools/bundles; resources are scoped by `org_id`.
    Every user gets a personal org on registration (like Vercel/GitHub) — no empty state.
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True, unique=True)
    suspended: bool = Field(default=False)  # a suspended org's members are locked out (403)
    demo: bool = Field(default=False)  # a sandbox team seeded by onboarding — labeled + one-click removable
    created_at: datetime = Field(default_factory=_now)


class User(SQLModel, table=True):
    """A global identity (one email/login). Identity ONLY — the token and role live on
    Membership, so one person in two orgs has two memberships (two tokens), one User.
    """

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    is_superadmin: bool = Field(default=False)  # cross-tenant platform admin (see /admin/*)
    suspended: bool = Field(default=False)  # suspended users cannot authenticate
    # Bumped to revoke every token this user holds at once (session cookie + CLI tokens). A signed
    # token carries the token_version it was minted at; a mismatch = revoked (see session.make/read).
    token_version: int = Field(default=0)
    onboarded: bool = Field(default=False)  # has completed OR skipped first-run onboarding (don't re-offer)
    demo: bool = Field(default=False)  # a fake teammate seeded into a demo team (can't log in; excluded from stats)
    created_at: datetime = Field(default_factory=_now)


class Membership(SQLModel, table=True):
    """Links a User to an Org with a role, and carries that pairing's token.
    The caller presents a token; we store only its SHA-256 hash. Access = "are you a member
    of the org that owns this?" (+ role for destructive actions).
    """

    __table_args__ = (UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    org_id: int = Field(foreign_key="org.id", index=True)
    role: str = Field(default="member")  # owner | admin | member
    token_hash: str = Field(index=True)
    webhook_url: str | None = Field(default=None)  # health alerts for this member's org POST here
    # Per-user, per-day usage cap for this org (counts proxy calls + local + server runs). -1 = unlimited
    # (the default — nobody is capped until an admin sets a limit). See api._enforce_daily_cap.
    daily_call_cap: int = Field(default=-1)
    # Per-member tool ACL: NULL = ALL tools in the org (the default — no restriction, no regression); a
    # JSON list of tool NAMES = the ONLY tools this member may call or run. See api._require_tool_access.
    tool_access: list | None = Field(default=None, sa_column=Column("tool_access", JSON, nullable=True))
    # May this member use the LOCAL run tier (`treg run --local`, the grant)? False → server runs only.
    local_run_enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_now)


class Invite(SQLModel, table=True):
    """A one-time invite code (no email server yet). An admin creates it and shares the code
    (Slack/DM); the invitee redeems it and mints their own org-scoped token. (Used by PR2.)

    TWO secrets, deliberately split: `code_hash` is the admin-visible out-of-band code (returned
    from POST /orgs/{id}/invites so it can be relayed via Slack/DM) — it lets you JOIN but never
    signs you in, because the admin provably holds it. `email_token_hash` is a second secret that
    ONLY travels inside the invite email's link: possession proves inbox access (the same bar as
    the emailed OTP), so /auth/invite-signin may mint a session from it. One-time: nulled on use.
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="org.id", index=True)
    email: str = Field(index=True)
    role: str = Field(default="member")
    code_hash: str = Field(index=True)
    email_token_hash: str | None = Field(default=None, index=True)  # inbox-only sign-in secret (see docstring)
    status: str = Field(default="pending")  # pending | accepted | revoked
    invited_by: str = Field(default="")  # inviter email (audit)
    expires_at: datetime | None = Field(default=None)  # one-time AND time-bounded; None = never
    # Access to seed onto the membership when this invite is accepted (requirement: set access at invite
    # time, modify later). NULL tool_access = all tools; a list = the allowed tool names.
    tool_access: list | None = Field(default=None, sa_column=Column("tool_access", JSON, nullable=True))
    local_run_enabled: bool = Field(default=True)
    # Where the invitee lands after sign-in — a shared detail page ("/app/skills/<name>") when the
    # invite was minted from a share, else NULL for the plain dashboard. Path-only, validated on create.
    landing: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class CallRecord(SQLModel, table=True):
    """Audit: who called which tool, in which org, when, with what result. Written off the
    request path (fire-and-forget) so it never adds latency to a proxied call.
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    user_email: str = Field(index=True)
    tool_name: str = Field(index=True)
    method: str
    path: str
    status_code: int
    # Which execution path produced this row: "call" (proxy /call) or "local_run" (/tools/{name}/grant).
    # Server-side CLI runs live in RunRecord ("server_run"). Lets the usage view break down by kind.
    kind: str = Field(default="call")
    created_at: datetime = Field(default_factory=_now)


class RunRecord(SQLModel, table=True):
    """Audit for server-side CLI runs (`treg run`): who ran which tool's CLI, with what args, in
    which org, and the result. Written off the request path (fire-and-forget) like CallRecord.
    `argv` never contains a secret value (secrets are injected via env, not the command line).
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    user_email: str = Field(index=True)
    bundle_name: str = Field(index=True)  # the TOOL name since the tool-side unification (column name is historical)
    argv: list = Field(default_factory=list, sa_column=Column("argv", JSON))
    exit_code: int
    duration_ms: int
    created_at: datetime = Field(default_factory=_now)


class Bundle(SQLModel, table=True):
    """A skill: the named grouping of a recipe (SKILL.md) + its secrets + its tool(s) — pure
    packaging. "Register a skill" creates a bundle; its secrets/tools point back via `bundle_id`.

    Execution config (both `treg run` tiers) lives on `Tool.cli` — one profile, read by the local
    grant path and the server runner alike (see docs/CLI-RUN-PLAN.md). The old bundle-side run
    columns (runtime/package/entrypoint/runnable) were folded into `Tool.cli` by a startup
    migration; they may still exist physically in older databases but nothing reads them.
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    name: str = Field(index=True)
    owner: str = Field(default="bootstrap", index=True)  # creator email (audit)
    recipe: str = Field(default="")  # the SKILL.md text (the shareable how-to)
    # Companion files so a whole skill folder travels, not just SKILL.md: {relpath: text-content},
    # nested paths allowed (e.g. "reference/fields.md", "scripts/run.py"). Excludes secrets + binaries;
    # `skill install` reconstructs the tree. Text only — a skill folder is assumed small.
    files: dict = Field(default_factory=dict, sa_column=Column("files", JSON))
    created_at: datetime = Field(default_factory=_now)


class Ephemeral(SQLModel, table=True):
    """Short-lived server state that must survive a restart and stay correct across instances:
    the emailed OTP code + its brute-force counter, and the auth rate-limit sliding windows. Keyed
    by (ns, k) — a namespace ('otp' | 'otp_start' | 'sandbox_hit') plus the key within it; `v` is an
    opaque JSON payload; rows past `expires_at` are swept lazily (see treg.ratestore). This is the
    DB home for what used to be per-process dicts (backlog #3) — so counters can't be reset by a
    restart and stay correct on more than one instance. NOT the CLI-login handshake, which is
    deliberately still in-process (short-lived, self-heals on retry — see api._cli_pending)."""

    ns: str = Field(primary_key=True)
    k: str = Field(primary_key=True)
    v: dict = Field(default_factory=dict, sa_column=Column("v", JSON, nullable=False))
    expires_at: datetime = Field(index=True)


class PendingOAuth(SQLModel, table=True):
    """An in-flight OAuth connect (Phase C). `state` is the unguessable lookup/CSRF key carried
    through the provider redirect. `client_secret` is encrypted at rest. On callback we exchange
    the code for tokens and create the resulting oauth Secret (in `org_id`), then mark this done.
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    state: str = Field(index=True, unique=True)
    name: str
    owner: str = Field(index=True)
    client_id: str
    client_secret: str  # encrypted
    auth_uri: str
    token_uri: str
    scopes: str = ""  # space-joined
    redirect_uri: str
    status: str = Field(default="pending")  # pending | done | error
    secret_id: int | None = Field(default=None)
    detail: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)


class Secret(SQLModel, table=True):
    """A stored credential blob. `value` is Fernet-encrypted (see crypto.py).

    `kind` selects the injector used at call time (env | secret_file | cli_auth | oauth).
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    name: str = Field(index=True)
    owner: str = Field(default="bootstrap", index=True)  # creator email (audit)
    kind: str = Field(default="env")
    value: str  # encrypted at rest; never returned to clients
    bundle_id: int | None = Field(default=None, foreign_key="bundle.id", index=True)
    # Freshness/validity — set by the health runner (Phase B). status: unknown | ok | invalid.
    health_status: str = Field(default="unknown")
    health_detail: str = Field(default="")
    health_checked_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class Tool(SQLModel, table=True):
    """A registered capability: a name + an upstream base + a LIST of credential bindings.

    The proxy *relays* — it never models the upstream. Each binding in `bindings` is one
    injection applied to every call: {secret_id, injector, location, name, format, secret_field}.
    A request may carry several (e.g. google-ads: OAuth bearer + developer-token header). A
    binding's secret may be one another member uploaded (use-without-hold). `name` is unique
    per org (two orgs may register the same tool name).
    """

    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_tool_org_name"),)

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    name: str = Field(index=True)
    owner: str = Field(default="bootstrap", index=True)  # creator email (audit)
    base_url: str  # e.g. https://us.posthog.com
    host: str = Field(default="", index=True)  # netloc of base_url — indexed for URL-passthrough resolution
    bindings: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    # Optional usage examples surfaced in the dashboard: [{method, path, note}]. Per-tool, since
    # every upstream differs. Filled from a skill's treg.json `examples` (or set via tool add/update).
    examples: list[dict] = Field(default_factory=list, sa_column=Column("examples", JSON))
    # Optional health probe: {method, path, expect_status} — the runner calls it to validate creds.
    health_check: dict | None = Field(default=None, sa_column=Column("health_check", JSON))
    # Optional local-run profile (`treg run`): {enabled, bin, inject[], deny[], deny_defaults,
    # noninteractive}. Creator-declared via treg.json `cli` (enabled=true) or catalog-attached
    # (disabled until the owner opts in). See docs/CLI-RUN-PLAN.md.
    cli: dict | None = Field(default=None, sa_column=Column("cli", JSON))
    bundle_id: int | None = Field(default=None, foreign_key="bundle.id", index=True)
    created_at: datetime = Field(default_factory=_now)
