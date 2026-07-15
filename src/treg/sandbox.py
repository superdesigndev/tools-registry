"""Landing-page sandbox studio - a login-free, throwaway team a visitor BUILDS in-browser.

A visitor with no account gets a real, short-lived (user, org) + member token. Holding that token
they use the SAME product endpoints the dashboard does - `POST /secrets`, `POST /tools`, `/call/…`,
`POST /skills` - to register their own secret + up to a few endpoints and call them, then copy CLI
commands that keep working from their terminal for the TTL. Sign-up brings it into a real account.

Two things make this safe even though the token can register any endpoint:
  1. **Calls never touch the network.** `synthesize()` runs the REAL injectors to compute exactly
     what treg *would* send upstream, then returns a labelled dummy response. No SSRF, no open relay,
     no arbitrary-host fetch - but the credential injection shown is 100% real (same `injectors.inject`
     the proxy uses). The `/call` handler routes sandbox orgs here instead of `relay()`.
  2. **Caps + TTL + GC.** A sandbox holds at most `MAX_TOOLS`/`MAX_SECRETS`; the whole footprint is
     reaped after `SANDBOX_TTL_MIN` (see `gc()`).

Sandbox orgs are identified by `is_sandbox()` (a `demo` org whose slug starts `sbx-`), which keeps
them distinct from the onboarding demo teams (also `demo`, but team-named). Self-contained
(models + crypto + injectors) → no import cycle with api.py.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from secrets import token_hex

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import crypto, injectors
from .models import Bundle, CallRecord, Invite, Membership, Org, PendingOAuth, Secret, Tool, User

SANDBOX_DOMAIN = "sandbox.treg.local"     # throwaway visitor identities live here (can never log in)
SANDBOX_TTL_MIN = 60
MAX_TOOLS = 3                              # a sandbox visitor may register at most this many endpoints
MAX_SECRETS = 3

# Believable starters (real-brand names, OBVIOUSLY-FAKE placeholder values) so the console is alive on
# arrival and the demo reads instantly. Each value's only job is to appear in the "injected" output; it is
# a credential to nothing, and sandbox calls never leave treg. The visitor edits/removes these and adds
# their own. The placeholders spell DEMO on purpose so no scanner mistakes them for a real key.
#
# ONE live endpoint on arrival (Stripe) keeps the story clean: it auto-runs so the "no key" aha shows
# immediately. POSTHOG_KEY is seeded as a vault secret WITHOUT a tool, so the "add your own" row is
# prefilled with the real PostHog API + this placeholder and the visitor's first action is a single Add.
DEFAULTS = [
    {"secret": "STRIPE_KEY",  "value": "sk_live_DEMO0000PLACEHOLDER", "tool": "stripe",
     "base": "https://api.stripe.com",  "host": "api.stripe.com",
     "example": {"method": "GET", "path": "v1/charges", "note": "list charges"}},
    {"secret": "POSTHOG_KEY", "value": "phx_DEMO0000PLACEHOLDER"},  # vault-only (no tool); the add-row uses it
]

# Brand-shaped dummy payloads so "what the API received" feels like the real endpoint (keyed by host).
SAMPLE_BODIES = {
    "api.stripe.com": {"object": "list", "url": "/v1/charges", "has_more": False,
                       "data": [{"id": "ch_3P9xE2eZvKY", "object": "charge", "amount": 4200,
                                 "currency": "usd", "status": "succeeded", "paid": True}]},
    "app.posthog.com": {"results": [{"event": "$pageview", "count": 1284},
                                    {"event": "signup", "count": 37}], "next": None},
}

_ORG_MODELS = (Tool, Secret, Bundle, PendingOAuth, CallRecord, Invite, Membership)


_SANDBOX_SLUG_RE = re.compile(r"^sbx-[0-9a-f]{12}$")  # the exact mint format — see mint(): sbx-{token_hex(6)}


def is_sandbox(org: Org | None) -> bool:
    """True for a landing-page sandbox org (distinct from onboarding demo teams). Match the EXACT mint
    slug format, so a real team a user names 'sbx …' (slug 'sbx-…') isn't misread as a sandbox."""
    return bool(org) and bool(org.demo) and bool(_SANDBOX_SLUG_RE.match(org.slug or ""))


def is_sandbox_user(user) -> bool:
    """A login-free sandbox visitor (`visitor-…@sandbox.treg.local`). It may act ONLY inside its own
    sandbox org — never create a real team or graduate to a real account (that needs a real sign-in door)."""
    return bool(user) and (getattr(user, "email", "") or "").endswith(f"@{SANDBOX_DOMAIN}")


async def mint(db: AsyncSession) -> dict:
    """Create a fresh login-free sandbox team (with a starter secret + endpoint) and RETURN its token
    + the facts the studio needs. Caller need not commit - we commit here."""
    rid = token_hex(6)
    user = User(email=f"visitor-{rid}@{SANDBOX_DOMAIN}", demo=True, onboarded=True)
    db.add(user)
    await db.flush()

    org = Org(name=f"Sandbox {rid}", slug=f"sbx-{rid}", demo=True)
    db.add(org)
    await db.flush()

    token = crypto.new_token()  # the ONE thing we surface (the visitor drives everything with it)
    db.add(Membership(user_id=user.id, org_id=org.id, role="member", token_hash=crypto.hash_token(token)))

    for d in DEFAULTS:  # seed the vault secret; only entries with a "tool" also get a live endpoint
        secret = Secret(org_id=org.id, name=d["secret"], owner=user.email, kind="env",
                        value=crypto.encrypt(d["value"]))
        db.add(secret)
        await db.flush()
        if d.get("tool"):
            db.add(Tool(
                org_id=org.id, name=d["tool"], owner=user.email, base_url=d["base"], host=d["host"],
                bindings=[{"secret_id": secret.id, "injector": "env", "location": "header",
                           "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token"}],
                examples=[d["example"]],
            ))

    await db.commit()
    return {
        "token": token,
        "org_slug": org.slug,
        "max_tools": MAX_TOOLS,
        "max_secrets": MAX_SECRETS,
        "ttl_min": SANDBOX_TTL_MIN,
    }


def synthesize(method: str, upstream_url: str, tool: Tool, secrets: dict[int, Secret],
               query: list[tuple[str, str]] | None = None, body: str = "") -> dict:
    """Compute what treg WOULD send upstream (via the real injectors) and return a labelled dummy
    response - never touching the network. This is the sandbox's stand-in for `relay()`."""
    headers: dict[str, str] = {}
    params: list[tuple[str, str]] = list(query or [])
    for b in tool.bindings:
        sec = secrets.get(b.get("secret_id"))
        if sec is None:
            continue
        injectors.inject(headers, params, b, crypto.decrypt(sec.value))
    injected_h = dict(headers)
    injected_q = [(k, v) for (k, v) in params if (k, v) not in (query or [])]
    data = SAMPLE_BODIES.get(tool.host,
                             {"ok": True, "message": "Authenticated by treg - your key was injected in the proxy."})
    return {
        "sandbox": True,
        "note": "Dummy sandbox response - treg did NOT call the real upstream. Sign up to reach live APIs.",
        "request": {"method": method, "url": upstream_url},
        "injected": {  # exactly what the proxy added server-side - the credential you never sent
            "headers": injected_h,
            "query": dict(injected_q),
        },
        "upstream_would_receive": {  # a believable echo of what your endpoint would have seen
            "method": method,
            "url": upstream_url,
            "headers": {"host": tool.host, **injected_h},
            "query": dict(params),
            "body": (json.loads(body) if body.strip().startswith(("{", "[")) else body) if body else None,
        },
        "data": data,
    }


async def export_skill(db: AsyncSession, org: Org) -> dict:
    """Turn whatever the visitor built (secrets + endpoints) into a shareable **skill** - the
    `POST /skills`-shaped manifest (values redacted to placeholders, since real skills carry values
    in `.secret` files, not the manifest) + a generated SKILL.md recipe + install commands."""
    secrets = (await db.execute(select(Secret).where(Secret.org_id == org.id))).scalars().all()
    tools = (await db.execute(select(Tool).where(Tool.org_id == org.id))).scalars().all()
    id_to_name = {s.id: s.name for s in secrets}

    manifest_secrets = [{"local_name": s.name, "kind": s.kind, "value": f"<paste your {s.name}>"}
                        for s in secrets]
    manifest_tools = []
    for t in tools:
        binds = []
        for b in t.bindings:
            nb = {k: v for k, v in b.items() if k != "secret_id"}
            nb["secret"] = id_to_name.get(b.get("secret_id"), "SECRET")  # skills reference by local_name
            binds.append(nb)
        manifest_tools.append({"name": t.name, "base_url": t.base_url, "bindings": binds,
                               "examples": t.examples or []})

    name = "my-api-skill"
    manifest = {"name": name, "recipe": f"# {name}\n\nCall these endpoints without holding the key.",
                "secrets": manifest_secrets, "tools": manifest_tools}

    lines = [f"# {name}", "",
             "A treg **skill**: a recipe + its endpoints + the secrets they need, bundled so a",
             "teammate installs the capability **without ever holding your keys**.", "",
             "## Endpoints"]
    for t in tools:
        ex = (t.examples or [{}])[0]
        lines.append(f"- `{t.name}` → {t.base_url}"
                     + (f"  (e.g. `{ex.get('method','GET')} {ex.get('path','')}`)" if ex else ""))
    lines += ["", "## Use", "```", "# an agent or you, after installing the skill:",
              f"treg call {tools[0].base_url}/… " if tools else "treg call <url>", "```",
              "", "The key is injected server-side by treg - it never lands on the caller's machine."]
    skill_md = "\n".join(lines)

    return {
        "manifest": manifest,
        "treg_json": json.dumps(manifest, indent=2),
        "skill_md": skill_md,
        "install": [
            "# save the manifest as treg.json in a skill folder, then:",
            "treg skill add ./my-api-skill",
            "# - or paste the manifest into the dashboard's Skills composer.",
        ],
    }


# ---- hosted sample skills (the "Run in Claude Code" flow) ---------------------------------
# Each sample matches a tool the sandbox already seeds, so the installed skill's proxied calls
# resolve against the visitor's sandbox and return the synthetic response. A skill is just a
# recipe (SKILL.md) + wiring (treg.json) - the credential stays in the vault; the caller sends
# only their treg token and treg injects the key.
SAMPLE_SKILLS = {
    "posthog-insights": {
        "label": "PostHog Insights - product analytics",
        "key": "POSTHOG_KEY", "base_url": "https://app.posthog.com",
        "prompt": "use posthog-insights to list recent events",
        "endpoints": [
            {"method": "GET", "path": "api/projects/@current/events", "note": "recent events"},
            {"method": "POST", "path": "api/projects/@current/insights", "note": "run a query"},
        ],
    },
    "stripe-billing": {
        "label": "Stripe Billing - charges & invoices",
        "key": "STRIPE_KEY", "base_url": "https://api.stripe.com",
        "prompt": "use stripe-billing to list recent charges",
        "endpoints": [
            {"method": "GET", "path": "v1/charges", "note": "list charges"},
            {"method": "POST", "path": "v1/invoices", "note": "draft an invoice"},
        ],
    },
}


def _skill_md(name: str, sk: dict, base: str, token: str | None) -> str:
    tok = token or "$TREG_TOKEN"
    out = [
        "---", f"name: {name}",
        f"description: {sk['label']}. Calls the API through treg - the key is injected server-side, never held here.",
        "---", "", f"# {name}", "", sk["label"] + ".", "",
        "Call the API **through treg**: you send only a token; treg injects the real credential.",
        "Never put an API key in a command.", "", "## Actions",
    ]
    for e in sk["endpoints"]:
        out += [f"- **{e['note']}**", "  ```",
                f'  curl -s -X {e["method"]} {base}/call/{sk["base_url"]}/{e["path"]} -H "X-Treg-Token: {tok}"', "  ```"]
    if not token:
        out += ["", "> Set `TREG_TOKEN` to your treg token (from `treg login`, in `~/.treg/config.json`)."]
    return "\n".join(out)


def _skill_treg_json(name: str, sk: dict) -> str:
    return json.dumps({
        "name": name, "base_url": sk["base_url"],
        "secrets": [{"name": sk["key"], "kind": "env"}],
        "bindings": [{"secret": sk["key"], "name": "Authorization", "format": "Bearer {secret}"}],
        "examples": [{"method": e["method"], "path": e["path"]} for e in sk["endpoints"]],
    }, indent=2)


def _skill_secret(sk: dict) -> str:
    return (f"# Referenced by NAME - the value never ships in the skill folder.\n"
            f"{sk['key']}=      # empty on disk. treg holds the value and injects it server-side.")


def skill_files(name: str, base: str, token: str | None = None) -> dict:
    """The three files that make up the skill folder Claude Code loads."""
    sk = SAMPLE_SKILLS[name]
    return {"SKILL.md": _skill_md(name, sk, base, token),
            "treg.json": _skill_treg_json(name, sk),
            ".secret": _skill_secret(sk)}


def install_script(name: str, base: str, token: str | None = None) -> str:
    """A POSIX `sh` script: creates ./.claude/skills/<name>/ (if absent) and writes the skill
    files there, so `curl … | sh` from a project dir installs the skill for Claude Code."""
    sk = SAMPLE_SKILLS[name]
    files = skill_files(name, base, token)

    def heredoc(fname: str, content: str) -> str:  # quoted heredoc → no shell expansion of content
        return f'cat > "$DIR/{fname}" <<\'TREG_SKILL_EOF\'\n{content}\nTREG_SKILL_EOF'

    lines = [
        "#!/bin/sh", "set -e", f'NAME="{name}"', 'DIR=".claude/skills/$NAME"', 'mkdir -p "$DIR"',
        heredoc("SKILL.md", files["SKILL.md"]),
        heredoc("treg.json", files["treg.json"]),
        heredoc(".secret", files[".secret"]),
        f'echo "✓ installed {name} into ./$DIR"',
        f'echo "Open Claude Code here, then ask:  {sk["prompt"]}"',
    ]
    return "\n".join(lines) + "\n"


async def gc(db: AsyncSession) -> int:
    """Reap sandboxes older than the TTL - the throwaway user, its org, and every org-scoped row.
    Runs opportunistically each time a new sandbox is minted. Commits itself."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=SANDBOX_TTL_MIN)
    visitors = (await db.execute(
        select(User).where(User.email.like(f"visitor-%@{SANDBOX_DOMAIN}")))).scalars().all()
    n = 0
    for u in visitors:
        ca = u.created_at
        ca = ca.replace(tzinfo=None) if (ca is not None and ca.tzinfo) else ca
        if ca is not None and ca >= cutoff:
            continue
        mems = (await db.execute(select(Membership).where(Membership.user_id == u.id))).scalars().all()
        for m in mems:
            org = await db.get(Org, m.org_id)
            if org is None:
                continue
            for model in _ORG_MODELS:
                for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
                    await db.delete(r)
            await db.delete(org)
        await db.delete(u)
        n += 1
    if n:
        await db.commit()
    return n
