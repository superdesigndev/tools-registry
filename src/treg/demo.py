"""Onboarding demo-team provisioner — one seed shape, used by BOTH the dashboard and the CLI.

Creates a REAL team owned by the caller, populated so it's alive from second one:
  - fake teammates (roster-only User rows, `demo=True`, on an unusable domain so they can never
    log in — see the guard in api.auth_email_start), one per role to show the ladder;
  - a working `echo` tool (calls through the proxy with an injected key — the "aha");
  - a few sample CallRecords attributed to teammates, so Activity isn't empty.

`reset` removes the whole demo footprint (org cascade + orphaned demo users) so onboarding leaves
no litter. Self-contained (models + crypto only) → no import cycle with api.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import crypto
from .models import Bundle, CallRecord, Membership, Org, PendingOAuth, Secret, Tool, User

DEMO_DOMAIN = "demo.treg.local"      # fake teammates live here; api refuses login for this domain
DEMO_TOOL = "echo"
DEMO_BASE = "https://postman-echo.com"
DEMO_SECRET_VALUE = "sk-demo-onboarding-123"  # visible in the echoed response — that's the point

# Fake teammates — chosen to show the whole role ladder (the caller is the owner).
TEAMMATES = [
    ("Ada Lovelace", "ada",  "admin"),
    ("Ben Carter",   "ben",  "member"),
    ("Cora Diaz",    "cora", "viewer"),
]

# Sample activity: (teammate handle, method, path, status, minutes-ago) — a believable little trail.
SAMPLE_CALLS = [
    ("ada",  "GET",  f"{DEMO_BASE}/get",  200, 4),
    ("cora", "GET",  f"{DEMO_BASE}/get",  200, 2),
    ("ben",  "POST", f"{DEMO_BASE}/post", 200, 1),
]

_ORG_MODELS = (Tool, Secret, Bundle, PendingOAuth, CallRecord, Membership)  # Invite added below


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "team"


async def _unique_slug(base: str, db: AsyncSession) -> str:
    slug, i = base, 2
    while (await db.execute(select(Org).where(Org.slug == slug))).scalar_one_or_none() is not None:
        slug, i = f"{base}-{i}", i + 1
    return slug


async def existing_demo_org(db: AsyncSession, owner: User) -> Org | None:
    """The demo team this user already owns, if any (onboarding is idempotent)."""
    mems = (await db.execute(select(Membership).where(
        Membership.user_id == owner.id, Membership.role == "owner"))).scalars().all()
    for m in mems:
        org = await db.get(Org, m.org_id)
        if org is not None and org.demo:
            return org
    return None


async def provision(db: AsyncSession, owner: User, team_name: str) -> dict:
    """Create + seed the demo team owned by `owner`. Idempotent: reuses an existing demo org.
    Marks the owner `onboarded`. Caller need not commit — we commit here."""
    existing = await existing_demo_org(db, owner)
    if existing is not None:
        owner.onboarded = True
        await db.commit()
        return _view(existing, reused=True)

    name = (team_name or "").strip() or "Acme Design"
    org = Org(name=name, slug=await _unique_slug(_slug(name), db), demo=True)
    db.add(org)
    await db.flush()

    # owner membership (token minted but never surfaced — the human uses their session/identity token)
    db.add(Membership(user_id=owner.id, org_id=org.id, role="owner",
                      token_hash=crypto.hash_token(crypto.new_token())))

    # fake teammates — reuse the same User row across demo orgs (email is unique)
    for full_name, handle, role in TEAMMATES:
        email = f"{handle}@{DEMO_DOMAIN}"
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u is None:
            u = User(email=email, demo=True, onboarded=True)
            db.add(u)
            await db.flush()
        db.add(Membership(user_id=u.id, org_id=org.id, role=role,
                          token_hash=crypto.hash_token(crypto.new_token())))

    # a working tool + its secret (echo → postman-echo, injected server-side)
    secret = Secret(org_id=org.id, name="echo-key", owner=owner.email, kind="env",
                    value=crypto.encrypt(DEMO_SECRET_VALUE))
    db.add(secret)
    await db.flush()
    db.add(Tool(
        org_id=org.id, name=DEMO_TOOL, owner=owner.email, base_url=DEMO_BASE, host="postman-echo.com",
        bindings=[{"secret_id": secret.id, "injector": "env", "location": "header",
                   "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token"}],
        examples=[{"method": "GET", "path": "get", "note": "echo a GET"},
                  {"method": "POST", "path": "post", "note": "echo a POST body"}],
    ))

    # sample activity so the ledger is alive. Naive UTC — TIMESTAMP WITHOUT TIME ZONE columns +
    # asyncpg reject tz-aware values (see models._now); the whole app stores naive UTC.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for handle, method, path, status, mins in SAMPLE_CALLS:
        db.add(CallRecord(org_id=org.id, user_email=f"{handle}@{DEMO_DOMAIN}", tool_name=DEMO_TOOL,
                          method=method, path=path, status_code=status,
                          created_at=now - timedelta(minutes=mins)))

    owner.onboarded = True
    await db.commit()
    return _view(org, reused=False)


async def reset(db: AsyncSession, owner: User) -> dict:
    """Remove every demo team this owner has, plus any demo users left with no memberships."""
    removed = []
    while True:
        org = await existing_demo_org(db, owner)
        if org is None:
            break
        from .models import Invite  # local to keep the module list obvious above
        for model in (*_ORG_MODELS, Invite):
            for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
                await db.delete(r)
        removed.append(org.slug)
        await db.delete(org)
        await db.flush()
    # dashboard flow: demo teammates the user invited into their OWN (real) teams — drop those memberships
    my_org_ids = {m.org_id for m in (await db.execute(
        select(Membership).where(Membership.user_id == owner.id))).scalars().all()}
    for oid in my_org_ids:
        for m in (await db.execute(select(Membership).where(Membership.org_id == oid))).scalars().all():
            u = await db.get(User, m.user_id)
            if u is not None and u.demo:
                await db.delete(m)
    await db.flush()
    # sweep orphaned demo users (fake teammates no longer in any org)
    demo_users = (await db.execute(select(User).where(User.demo == True))).scalars().all()  # noqa: E712
    for u in demo_users:
        still = (await db.execute(select(Membership).where(Membership.user_id == u.id))).scalars().first()
        if still is None:
            await db.delete(u)
    await db.commit()
    return {"removed": removed}


# ---- dashboard narrative: user builds the team by hand; we seed the tool + auto-join one teammate ----
GUIDED_TEAMMATE = {"name": "Alex Rivera", "email": f"alex@{DEMO_DOMAIN}", "role": "member"}


async def seed_tool(db: AsyncSession, org: Org, owner_email: str) -> dict:
    """Pre-seed the working echo tool (+ its secret) into the user's own team, so the no-key call
    works without tool-setup friction. Idempotent."""
    if (await db.execute(select(Tool).where(Tool.org_id == org.id, Tool.name == DEMO_TOOL))).scalar_one_or_none() is not None:
        return {"tool": DEMO_TOOL, "reused": True}
    secret = Secret(org_id=org.id, name="echo-key", owner=owner_email, kind="env", value=crypto.encrypt(DEMO_SECRET_VALUE))
    db.add(secret)
    await db.flush()
    db.add(Tool(org_id=org.id, name=DEMO_TOOL, owner=owner_email, base_url=DEMO_BASE, host="postman-echo.com",
                bindings=[{"secret_id": secret.id, "injector": "env", "location": "header",
                           "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token"}],
                examples=[{"method": "GET", "path": "get", "note": "echo a GET"},
                          {"method": "POST", "path": "post", "note": "echo a POST body"}]))
    await db.commit()
    return {"tool": DEMO_TOOL, "reused": False}


async def accept_demo_invite(db: AsyncSession, org_id: int, invite) -> dict:
    """The 'invite a teammate' step: the user created a real invite for a demo email — create that
    fake teammate (demo=True, roster-only) and accept it, so it lands in the roster instantly."""
    email = invite.email
    u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if u is None:
        u = User(email=email, demo=True, onboarded=True)
        db.add(u)
        await db.flush()
    if (await db.execute(select(Membership).where(Membership.user_id == u.id, Membership.org_id == org_id))).scalar_one_or_none() is None:
        db.add(Membership(user_id=u.id, org_id=org_id, role=invite.role, token_hash=crypto.hash_token(crypto.new_token())))
    invite.status = "accepted"
    await db.commit()
    return {"email": email, "role": invite.role}


def _view(org: Org, reused: bool) -> dict:
    return {
        "org": org.slug, "org_id": org.id, "name": org.name, "reused": reused,
        "teammates": [{"name": n, "email": f"{h}@{DEMO_DOMAIN}", "role": r} for n, h, r in TEAMMATES],
        "tool": DEMO_TOOL,
    }
