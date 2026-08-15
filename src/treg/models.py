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
    # A team whose token is published (e.g. on the landing page): non-admin members are locked to
    # /call + reads, and may never act as a user (see api.require_member / require_identity).
    public_demo: bool = Field(default=False)
    # Prepaid balance in micro-USD (1e-6 USD), MATERIALIZED as sum(CreditBlock.remaining) minus the
    # open Holds. It exists as a column, not a query, because it is the hot-path spend gate: one
    # conditional UPDATE against this integer is what stops concurrent agent calls racing past zero
    # (see ledger.reserve). Only `ledger.py` may write it.
    balance_micro: int = Field(default=0)

    # ---- Stripe billing (see billing.py; NO card data ever lands here) ----------------------------
    # The org's Stripe Customer. Created lazily on the first top-up and reused forever after, because
    # it is what carries the saved payment method — a second customer would silently orphan the card.
    stripe_customer_id: str | None = Field(default=None, index=True)
    # The saved card to charge off-session, as a Stripe PaymentMethod id (`pm_…`). An OPAQUE REFERENCE,
    # not card data: treg never sees a PAN, so there is nothing here to leak beyond a token that only
    # works with our own secret key. Null = no card on file → auto-top-up cannot be armed.
    stripe_default_pm: str | None = Field(default=None)
    # Auto-top-up: refill the balance without a human when it dips below the threshold. Off unless the
    # org explicitly turned it on AND recorded consent — an off-session charge with no mandate is an
    # unauthorized charge under PSD2/SCA, so `autotopup_consented_at` gates the charge, not the UI.
    autotopup_enabled: bool = Field(default=False)
    autotopup_threshold_micro: int = Field(default=0)   # 0 = "use the configured default"
    autotopup_amount_micro: int = Field(default=0)      # 0 = "use the configured default"
    autotopup_monthly_cap_micro: int = Field(default=0)  # 0 = "use the configured default"
    # WHEN the org agreed to the threshold/amount it is being charged on. The MIT mandate: a compliance
    # record, which is why it is a timestamp and not a boolean — "they ticked a box at some point" is
    # not defensible in a dispute, "they agreed on 2026-07-30T11:02Z" is.
    autotopup_consented_at: datetime | None = Field(default=None)
    # Why auto-top-up turned itself OFF (e.g. "authentication_required", "max_attempts"). Non-null is
    # the banner the dashboard shows: a silent disable is how an org discovers its agents are broken.
    autotopup_disabled_reason: str | None = Field(default=None)
    # Cross-instance cooldown: the DB, not a process-local lock, is what stops two web workers (or a
    # burst of concurrent calls) from firing two charges as the balance crosses the threshold.
    autotopup_last_attempt_at: datetime | None = Field(default=None)
    autotopup_failures: int = Field(default=0)  # CONSECUTIVE failures; a success resets it to 0
    # A PaymentIntent that needs the cardholder present (3DS). Kept so the dashboard can offer an
    # on-session "finish this payment" link instead of starting a fresh charge the bank will re-decline.
    autotopup_recovery_pi: str | None = Field(default=None)

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
    # Who minted this membership (agents name their creating admin; "" for people who came through
    # a login door or invite — the invite itself records invited_by).
    created_by: str = Field(default="")
    # For an agent promoted from the observed roster: "member-email|runtime" (e.g.
    # "sam@x.dev|claude-code"). The observed-agents view excludes this pair while the agent lives —
    # the detected row "became" this agent — and revoking the agent naturally resurfaces it.
    promoted_from: str = Field(default="")
    webhook_url: str | None = Field(default=None)  # health alerts for this member's org POST here
    # Per-user, per-day usage cap for this org (counts proxy calls + local + server runs). -1 = unlimited
    # (the default — nobody is capped until an admin sets a limit). See api._enforce_daily_cap.
    daily_call_cap: int = Field(default=-1)
    # Per-member tool ACL: NULL = ALL tools in the org (the default — no restriction, no regression); a
    # JSON list of tool NAMES = the ONLY tools this member may call or run. See api._require_tool_access.
    tool_access: list | None = Field(default=None, sa_column=Column("tool_access", JSON, nullable=True))
    # Per-member PROJECT scope: NULL = the whole org (the default); a JSON list of project IDS = the
    # only projects whose tools this member may use. IDs, not slugs, so the hot-path check stays a pure
    # set test (no id→slug query per call) and a project rename can't strand an access list.
    # Composes with tool_access as AND — see api._project_allowed.
    project_access: list | None = Field(default=None, sa_column=Column("project_access", JSON, nullable=True))
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
    # NULL = the whole org; a list of project IDS = the projects to scope them to (see Membership).
    project_access: list | None = Field(default=None, sa_column=Column("project_access", JSON, nullable=True))
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
    # The RUNTIME that made the call — "claude-code", "codex", "cursor", … — self-reported by the
    # treg CLI via X-Treg-Client (attribution, NOT authentication: anything holding the token can
    # claim any name). "" = unreported. What makes the observed-agents roster possible.
    client: str = Field(default="", index=True)
    # ---- marketplace telemetry (NULL on a plain tool call) -------------------------------------
    # What a direct catalog call actually did: which endpoint, whose credential paid for it, what we
    # expected it to cost vs what the provider said it cost, and how big/slow the answer was. The
    # money itself is NOT here — it landed synchronously in the ledger (see ledger.py); this table is
    # analytics and is allowed to lose rows.
    endpoint_id: str | None = Field(default=None, index=True)
    provider: str | None = Field(default=None, index=True)
    # tool | credential | platform — which rung of the credential ladder served it. "platform" is the
    # only one that spends treg's own money, so this is what a spend audit groups by.
    credential_tier: str | None = Field(default=None)
    cost_estimated_micro: int | None = Field(default=None)  # what reserve withheld (platform tier only)
    cost_observed_micro: int | None = Field(default=None)   # the provider's own reported charge, when it reports one
    # What actually hit the org's balance: the settle amount, or 0 on a release/402/429. The estimate
    # alone over-reports a released call as spend, so displays must prefer this when present.
    cost_charged_micro: int | None = Field(default=None)
    duration_ms: int | None = Field(default=None)
    response_bytes: int | None = Field(default=None)
    # sha256 of endpoint_id + the canonicalized query + body — an identity for "the same call again".
    # Bodies are NEVER stored; this is the future cache key and the repeat-rate signal (plan phase 5).
    params_hash: str | None = Field(default=None, index=True)
    # Set when TREG refused the call before a byte went upstream; NULL when the provider answered
    # (whatever its status). Values: auth (bad/expired token) | policy (ACL/deny rule/suspension) |
    # balance (402 insufficient prepaid balance) | cap (429 daily caps) | resolution (no such tool
    # or endpoint) | request (malformed pre-relay: wrong method, missing param, bad body). What
    # separates "the provider failed" from "we said no" — without it a paywall 402 is
    # indistinguishable from a provider error, and provider stats absorb our own refusals.
    refused_by: str | None = Field(default=None, index=True)
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
    client: str = Field(default="")  # runtime attribution, same contract as CallRecord.client
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
    # The registry service this connect came from ("" for a bring-your-own-app connect). Carried
    # through the redirect so the callback knows which provider's tool to auto-provision.
    provider: str = Field(default="")
    # Per-provider auth quirks, captured at start so the callback exchanges the code the same way
    # the consent URL was built. `code_verifier` is PKCE (empty = not used); `auth_params` is a JSON
    # object of extra consent-URL query params.
    code_verifier: str = Field(default="")
    auth_params: str = Field(default="")
    token_endpoint_auth_method: str = Field(default="client_secret_post")
    # TikTok spells the client identifier `client_key` and comma-joins scopes. Snapshotted here for
    # the same reason as the fields above: the callback must speak the dialect the consent URL used.
    client_id_param: str = Field(default="client_id")
    scope_separator: str = Field(default=" ")
    # Meta only: swap the short-lived code-exchange token for a ~60-day one before storing it.
    # Snapshotted for the same reason as the fields above — the callback must not have to look the
    # provider up again to know how the token was meant to be obtained.
    long_lived_exchange: bool = Field(default=False)
    # Which existing connection this consent is REPLACING, if any. Set when the user reconnects or
    # widens access on a specific account; left null when they are adding another one. Without it
    # the callback cannot tell "renew this Slack workspace" from "attach a second Slack workspace",
    # and has to guess — which is why it used to blanket-replace by provider.
    replaces_secret_id: int | None = Field(default=None)
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

    # Connection metadata (registry connects — see oauth_providers.py). Empty `provider` means this
    # credential did not come from the registry (uploaded, or a bring-your-own-app connect).
    provider: str = Field(default="", index=True)
    granted_scopes: str = Field(default="")  # space-joined; what the user ACTUALLY consented to
    resource_ref: str = Field(default="")  # the chosen site / property / account this connection acts on
    # The human name for that ref. Upstream ids are opaque ("properties/384078430"); showing one to
    # a user tells them nothing about which site they picked, so the label is stored alongside it.
    resource_name: str = Field(default="")

    # Expiry is a SEPARATE axis from health_status. health answers "does this credential work";
    # expiry answers "how long will it keep working". A non-refreshable token (LinkedIn issues no
    # refresh_token at the non-partner tier) is perfectly healthy right up until it silently dies,
    # so it has to be surfaced on its own or the user gets no warning at all.
    expires_at: datetime | None = Field(default=None)
    last_refresh_at: datetime | None = Field(default=None)
    last_error: str = Field(default="")

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
    # Optional sub-scope inside the org. NULL = ORG-WIDE — which is every tool that existed before
    # projects, so adding this changed nothing. A project is a LABEL + ACL scope, never a namespace:
    # `name` stays unique per (org_id, name), so no unique constraint had to be rebuilt. See models.Project.
    project_id: int | None = Field(default=None, foreign_key="project.id", index=True)
    created_at: datetime = Field(default_factory=_now)


class CapabilityPin(SQLModel, table=True):
    """"For THIS job, our team uses THIS provider." One row per (org, capability).

    Not expressible as a DenyRule. A deny is negative and closed — blocking the other eight providers
    of a capability leaves the ninth allowed by accident the day a tenth is added to the catalog. A
    pin is positive and stays correct as the catalog grows.

    It is a real gate, not a hint: a catalog call to a DIFFERENT provider of a pinned capability is
    refused, naming the endpoint the team does use. A hint would be honoured by a well-behaved agent
    and ignored by the one you actually needed to stop — and the point of team policy is that it does
    not depend on the caller's goodwill.

    Deliberately per-capability, not per-provider: "we use Hunter for finding work emails" is a
    decision a team can hold in its head, whereas "we use Hunter" says nothing about the twelve other
    jobs Hunter also happens to serve.

    **Boundary — a pin governs the CATALOG, not every route to a vendor.** It is enforced where an
    endpoint is addressed by its catalog id, which is the only way to reach treg's own key, so it
    cannot be side-stepped to spend OUR money. A team that holds its own key for another provider can
    still call that provider by upstream URL: that is their credential and their bill, and blocking
    it is what `DenyRule` (host-scoped, applied to every shape of call) is for. Two tools, two jobs.
    """

    # UNIQUE(org_id, capability): `set_capability_pin` is a SELECT then an INSERT, so two admins
    # pinning the same job at once — or one admin against two web workers — would write two rows.
    # Enforcement would then read them with scalar_one_or_none() and raise MultipleResultsFound,
    # turning a policy row into a 500 on EVERY call to that capability. Same shape as the
    # double-credit bug in ledger.topup (#45): the database has to be the one that says no.
    __table_args__ = (UniqueConstraint("org_id", "capability", name="uq_pin_org_capability"),)

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="org.id", index=True)
    capability: str = Field(index=True)         # e.g. "people.email.find"
    provider: str                               # a catalog provider service id, e.g. "hunter"
    created_by: str = Field(default="")         # the admin who set it — pins outlive their author
    created_at: datetime = Field(default_factory=_now)


class DenyRule(SQLModel, table=True):
    """A policy rule evaluated on every proxied call: block this host / path / method.

    treg now denies at three layers, deliberately kept apart because each sees something different:
      - `egress.py`   — the OS firewall around an isolated LOCAL run (which hosts that uid may reach),
      - `localrun.check_deny` — the ARGV of a local CLI run (`--live`, `auth token`, …),
      - **this**      — the HTTP request the PROXY is about to relay (host + path + method).

    Scope: `user_id` NULL = the whole org; set = only that member (so one agent can be held tighter
    than the rest of the team). An empty `host`/`path_prefix`/`method` means "any" — so a rule with
    only `method="DELETE"` blocks every delete in the org.

    `verdict` is `deny` today. It exists so approval-required actions ("hold the call, ask a human")
    can land in this same table later without a migration — mirroring the `verdict` vocabulary
    `localrun.py` already uses for its argv errors.
    """

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    # NULL = applies to everyone in the org. Set = only this member/agent (identified the same way
    # every other member endpoint does, by user id — org_id + user_id IS the membership).
    user_id: int | None = Field(default=None, foreign_key="user.id", index=True)
    # NULL = applies to any tool. Set = only calls made THROUGH a tool in this project — the third
    # optional scope axis, composed with user_id as AND (both NULL-means-any, like project_access).
    # A URL-passthrough call resolves to a tool first, so it carries a project too.
    project_id: int | None = Field(default=None, foreign_key="project.id", index=True)
    host: str = Field(default="")  # netloc, case-insensitive; "" = any host
    path_prefix: str = Field(default="")  # "" = any path
    method: str = Field(default="")  # "" = any method
    verdict: str = Field(default="deny")  # deny | approve (approve = reserved, see docstring)
    note: str = Field(default="")  # why this exists — shown in the refusal so it names its source
    created_by: str = Field(default="")  # admin email (audit)
    created_at: datetime = Field(default_factory=_now)


class CreditBlock(SQLModel, table=True):
    """One funding event's worth of credit — a promo grant or a purchase — and what's left of it.

    Blocks rather than a single scalar balance because the FUNDING SOURCE has to survive: purchased
    credit is a deferred-revenue liability and refundable, promotional credit is a marketing expense
    and never refundable. Spend therefore burns promotional-first, then oldest-purchased-first
    (ledger.settle), which also keeps the refundable pool as small as possible.

    `remaining_micro` is decremented only by settles. `expires_at` is unused today (promo credit has
    no expiry yet) — it exists so adding one is a policy change, not a migration.
    """

    id: str = Field(primary_key=True)  # uuid4 hex
    org_id: int = Field(foreign_key="org.id", index=True)
    kind: str = Field(default="promotional", index=True)  # promotional | purchased
    amount_micro: int  # granted amount, micro-USD (1e-6 USD) — never mutated
    remaining_micro: int  # what's left to spend from this block
    currency: str = Field(default="USD")
    expires_at: datetime | None = Field(default=None)
    # The already-authorized payment this block was funded by (phase 4). Doubles as the idempotency
    # key for ledger.topup — a redelivered webhook must not credit twice. **UNIQUE**, because the
    # check in `topup` is a SELECT then an INSERT: two concurrent deliveries of the same PaymentIntent
    # both find nothing and both credit. Stripe delivers at least once, retries after the 500 the
    # handler deliberately returns, and prod can run more than one instance — so the database has to
    # be the one that says no. NULL is exempt from a unique index, so promo blocks are unaffected.
    stripe_payment_intent: str | None = Field(
        default=None, index=True, sa_column_kwargs={"unique": True})
    created_at: datetime = Field(default_factory=_now)


class LedgerEntry(SQLModel, table=True):
    """APPEND-ONLY money journal: every balance/block movement writes exactly one row, in the same
    transaction as the movement itself. Nothing edits or deletes a row — a correction is a new
    compensating entry. This is the audit trail we reconcile against the provider's own bill, so it
    is written SYNCHRONOUSLY in-request and never through `audit.py` (which drops rows past its
    queue bound and swallows exceptions — right for analytics, fatal for money).

    `amount_micro` is SIGNED from the org's point of view: a grant/topup/release is positive, a
    reserve/settle is negative. `call_id` correlates the reserve→settle / reserve→release pair.
    """

    id: str = Field(primary_key=True)  # uuid4 hex
    org_id: int = Field(foreign_key="org.id", index=True)
    block_id: str | None = Field(default=None, index=True)
    # grant | topup | reserve | settle | release | refund | adjustment | expiry
    kind: str = Field(index=True)
    amount_micro: int  # signed (see docstring)
    call_id: str | None = Field(default=None, index=True)
    endpoint_id: str | None = Field(default=None)
    # Free-form provenance: estimated vs observed cost, the margin applied, payment ref, shortfalls.
    meta: dict = Field(default_factory=dict, sa_column=Column("meta", JSON))
    created_at: datetime = Field(default_factory=_now, index=True)


class Hold(SQLModel, table=True):
    """An OPEN reservation: money taken out of `Org.balance_micro` for a call that hasn't finished.

    `id` IS the call_id, so the settle/release that closes it needs no second lookup. Rows are
    deleted on settle/release, which makes the table a live list of in-flight spend — and lets the
    reaper (ledger.reap_stale_holds) find the ones a crash between relay and settle stranded.
    """

    id: str = Field(primary_key=True)  # == call_id
    org_id: int = Field(foreign_key="org.id", index=True)
    endpoint_id: str = Field(default="")
    amount_micro: int  # what was withheld from the balance (margin already applied)
    created_at: datetime = Field(default_factory=_now, index=True)


class Project(SQLModel, table=True):
    """An optional sub-scope INSIDE an org — "one team roster, several projects".

    The org stays the hard isolation boundary (every query is still scoped by `org_id`); a project is
    a softer grouping on top of it. Deliberately a **label + ACL scope, not a namespace**: `Tool.name`
    remains unique per `(org_id, name)`, so no unique constraint had to be rebuilt and two projects
    cannot hold a same-named tool. `Tool.project_id` NULL means org-wide, which is what every tool
    that predates projects is — so this was purely additive.

    Secrets stay org-level on purpose: one shared credential legitimately backs tools in several
    projects, so scoping it would pose a question with no good answer.
    """

    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_project_org_slug"),)

    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    name: str
    slug: str = Field(index=True)  # the human handle inside the org
    created_by: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)


class OAuthClient(SQLModel, table=True):
    """An MCP client that may ask treg for a token — ChatGPT, Claude Code, Cursor, anything.

    Two ways a client arrives here and ONE row shape afterwards, which is the point: everything
    downstream (authorize, consent, token) reads this table and never asks how the client got in.

    - **dcr** — dynamic client registration (RFC 7591). The client POSTs its name and redirect URIs
      and we mint an opaque `client_id`. This is what Claude Code and most clients do.
    - **cimd** — a client-id metadata document. The `client_id` IS an https URL that we fetch to
      learn the name and redirect URIs. This is what ChatGPT prefers.

    Supporting only one would lock the other family out, and we would not have noticed: the client
    we happened to test with first is the one that does not need registration.

    `redirect_uris` is the security-critical column. An authorization code is delivered to a redirect
    URI, so a client that could name an arbitrary one at authorize time could have codes posted to an
    attacker. They are fixed here at registration and matched EXACTLY later — no prefix matching,
    which is the classic way this check is defeated.
    """

    __table_args__ = (UniqueConstraint("client_id", name="uq_oauth_client_id"),)

    id: int | None = Field(default=None, primary_key=True)
    client_id: str = Field(index=True)
    kind: str = Field(default="dcr")            # "dcr" | "cimd"
    client_name: str = Field(default="")
    client_uri: str = Field(default="")
    logo_uri: str = Field(default="")
    redirect_uris: list = Field(default_factory=list,
                                sa_column=Column("redirect_uris", JSON, nullable=False))
    scope: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)
    # cimd only: documents change, so a cached copy has to be refreshable rather than permanent.
    refreshed_at: datetime | None = Field(default=None)


class OAuthCode(SQLModel, table=True):
    """A one-time authorization code: the few seconds between a human approving and a client
    redeeming.

    Every field here exists to bind the code to the exact request that created it, because a code is
    a bearer credential travelling through a browser redirect — through the user's history, possibly
    a referrer header, possibly a proxy log.

    - `client_id` + `redirect_uri`: a code minted for one client, deliverable to one place. Without
      both, a code intercepted from one client could be redeemed by another.
    - `code_challenge`: PKCE. The redeemer must prove it knows the verifier, so a code stolen in
      transit is worthless to whoever stole it.
    - `resource`: what the user consented to, carried into the token's `aud`. This is what stops a
      grant for one MCP server working against another.
    - `org_id`: WHICH TEAM. A person may belong to several, and this is the answer they chose — not
      one the server guesses later.

    Rows are deleted on redemption rather than flagged. A used code that still exists is a race
    waiting for two redemptions to read it before either writes; deletion makes the database the
    arbiter, the same reasoning as the conditional UPDATE in `ledger.reserve`.
    """

    __table_args__ = (UniqueConstraint("code", name="uq_oauth_code"),)

    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True)
    client_id: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    org_id: int = Field(foreign_key="org.id", index=True)
    redirect_uri: str
    code_challenge: str = Field(default="")
    resource: str = Field(default="")
    scope: str = Field(default="")
    expires_at: datetime
    created_at: datetime = Field(default_factory=_now)


class OAuthRefresh(SQLModel, table=True):
    """A refresh token: the thing that keeps a connector working past the access token's hour.

    Stored as a HASH, like every other credential treg holds. A database copy is a database leak, and
    a refresh token is the long-lived half — the one worth stealing.

    **Rotation with reuse detection**, which is the whole reason this table has a `family_id`. Each
    refresh mints a replacement and retires the old row. If a retired token is ever presented again,
    two things are possible and we cannot tell them apart: a client retried after a dropped response,
    or somebody else has a copy. Treating that as ordinary would leave a thief with a working
    credential, so the entire family is revoked and the human signs in again. An interrupted client
    reconnects; a thief loses the token. The failure mode is inconvenience on one side and containment
    on the other, which is the right way round.

    `org_id` rides along because it is what the human chose at consent. A refresh must not become a
    chance to quietly re-pick a team.
    """

    __table_args__ = (UniqueConstraint("token_hash", name="uq_oauth_refresh_token"),)

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True)
    family_id: str = Field(index=True)      # every descendant of one grant shares this
    client_id: str = Field(index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    org_id: int = Field(foreign_key="org.id", index=True)
    resource: str = Field(default="")
    scope: str = Field(default="")
    expires_at: datetime
    created_at: datetime = Field(default_factory=_now)
    # Set when this row is superseded or killed. A row is kept rather than deleted precisely so a
    # replay can be RECOGNISED — deleting it would make a stolen token look merely unknown.
    retired_at: datetime | None = Field(default=None)
    retired_reason: str = Field(default="")


class IdempotentCall(SQLModel, table=True):
    """One remembered answer, so a retried call is not paid for twice.

    An agent retries far more than a person does. Most of those retries are already free: a 5xx, a
    timeout or a network error is never billed. The case this table exists for is narrower and worse
    — treg called the provider, the provider succeeded AND CHARGED US, and the response was lost on
    the way back. The agent retries, and without this we call the provider again and bill again.

    Remembering only that a key was "already billed" would not be enough: we would still make the
    second upstream call, so we would still pay the provider and would simply be absorbing the double
    cost instead of passing it on. The answer has to be replayed so the second request never leaves
    treg.

    **Scoped to the CALLER, never to the key alone.** Keys are chosen by clients, so the same string
    will be picked twice. Scoped by key alone that collision serves one team's response to another,
    which is the single failure here that leaks data instead of money.

    The caller is `membership_id`, because that is what every door already resolves to: a person's
    token, an agent token and an OAuth grant all become one `Membership` (see `Caller`). So one rule
    covers every case — the label belongs to whoever called — and it does not matter whether that is
    a human, an agent, or two agents inside one team.

    Per caller rather than per team on purpose. Two lazily-written agents in one team will both reach
    for `retry-1`; scoped per team they collide and the second gets a confusing refusal, scoped per
    caller they simply never meet. Nothing is given up: this is strictly narrower than per-team, so
    the cross-tenant leak stays closed. Stripe scopes per API key for the same reason.

    `request_fingerprint` catches a client reusing one key for a DIFFERENT request. That is a caller
    bug, and returning the old answer would hide it, so the mismatch is refused loudly instead.

    `status` is `pending` while the upstream call is in flight and `done` once stored. The pending row
    is written BEFORE the call, so two retries arriving together race on the unique constraint and the
    loser waits for the winner instead of making a second call. Same reasoning as the conditional
    UPDATE in `ledger.reserve`: where two paths read before either writes, the database has to be the
    one that says no.
    """

    __table_args__ = (UniqueConstraint("membership_id", "key", name="uq_idem_caller_key"),)

    id: int | None = Field(default=None, primary_key=True)
    # org_id is kept alongside the caller so the row is still org-scoped for deletion and audit:
    # a membership can go away while the team remains, and the stored answer belongs to the team
    # that paid for it.
    org_id: int = Field(foreign_key="org.id", index=True)
    membership_id: int = Field(foreign_key="membership.id", index=True)
    key: str = Field(index=True)
    request_fingerprint: str = Field(default="")
    endpoint_id: str = Field(default="")
    status: str = Field(default="pending")     # "pending" | "done"
    # Only ever set for a METERED success. A team calling on its own key is billed by the provider,
    # not by us, and storing those responses would hold someone's data for a reason that helps nobody.
    response_status: int | None = Field(default=None)
    response_body: bytes | None = Field(default=None)
    response_media_type: str = Field(default="")
    charged_micro: int = Field(default=0)
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime


class ToolRequest(SQLModel, table=True):
    """A "the catalog doesn't have X" report — filed from the catalog page, the CLI, or by an
    agent mid-search over MCP. Demand signal for which provider to key next; reviewed by querying
    this table (a Slack notifier may hang off the insert later, but the row is the record).

    Deliberately anonymous-friendly: an agent that just got zero results usually holds no token,
    and a signup wall here would cost us exactly the signal we want. So identity fields are
    nullable, the endpoint is open, and per-IP rate limiting (ratestore) is the abuse valve.
    """

    id: int | None = Field(default=None, primary_key=True)
    # Filled when the caller was authenticated (session or token); NULL for anonymous filings.
    org_id: int | None = Field(default=None, foreign_key="org.id", index=True)
    user_email: str = Field(default="")
    # What they wanted: free-text capability/provider ("Ahrefs backlinks", "weather API"), plus the
    # search query that came up empty — auto-filled by agents, the best dedup/priority signal.
    capability: str = Field(index=True)
    query: str = Field(default="")
    note: str = Field(default="")
    contact: str = Field(default="")  # optional reach-back (email/handle); free text, unverified
    source: str = Field(default="web", index=True)  # web | cli | mcp | api
    status: str = Field(default="open", index=True)  # open | done | dismissed — flipped by hand
    created_at: datetime = Field(default_factory=_now, index=True)
