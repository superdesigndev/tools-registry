"""Phase B — credential health: periodically prove every credential and alert its owner.

`run_all` is triggered on demand (POST /health/run) and by a Render Cron. For each tool it:
  1. refreshes any oauth secret (a failed refresh = invalid), then
  2. runs the tool's optional probe ({method, path, expect_status}) with the credential injected.
It records status (unknown | ok | invalid) on each secret, and webhooks the owners of invalid ones.
The runner reuses the SAME refresh path as the live call (oauth.ensure_fresh) — one source of truth.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from . import crypto, injectors, oauth
from .models import Invite, Membership, PendingOAuth, Secret, Tool, User

OAUTH_PENDING_TTL_MIN = 30  # an in-flight OAuth connect (holds an encrypted client_secret + a CSRF state) expires after this


def safe_webhook_url(url: str | None) -> bool:
    """A webhook_url is user-set and treg POSTs to it server-side — reject non-http(s) and internal
    targets (loopback/private/link-local/reserved literal IPs, localhost/*.local) so it can't be used
    as a blind-SSRF primitive against the metadata endpoint or internal services."""
    if not url:
        return False
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not a standard literal — but decimal/hex/octal/short forms (2130706433, 0x7f000001, 127.1) still
        # resolve to an IP and would reach loopback/internal. Normalize via inet_aton and re-check; a real
        # DNS name fails inet_aton and gets the best-effort allow (call-time resolution catches rebinding).
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            return True  # a genuine DNS name
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast)


def host_is_public(host: str) -> bool:
    """Call-time SSRF guard: RESOLVE `host` and require every address to be public. Defeats DNS
    rebinding — a name that passed the registration check but now points at an internal IP. (A narrow
    resolve-vs-connect race remains; pinning the IP would need a custom transport.)"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # unresolvable → refuse rather than let httpx try
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


_RANK = {"ok": 0, "unknown": 1, "invalid": 2}  # severity order for worst-status-wins within a run


def _mark(secret: Secret, status: str, detail: str, when: datetime) -> None:
    secret.health_status = status
    secret.health_detail = detail
    secret.health_checked_at = when


def _view(s: Secret) -> dict:
    return {
        "secret_id": s.id,
        "name": s.name,
        "owner": s.owner,
        "kind": s.kind,
        "status": s.health_status,
        "detail": s.health_detail,
        "checked_at": s.health_checked_at.isoformat() if s.health_checked_at else None,
    }


async def _probe(tool: Tool, smap: dict[int, Secret], client: httpx.AsyncClient) -> tuple[str, str]:
    hc = tool.health_check or {}
    headers: dict[str, str] = {}
    params: list = []
    url = f"{tool.base_url.rstrip('/')}/{str(hc.get('path', '')).lstrip('/')}"
    try:
        for b in tool.bindings:
            s = smap.get(b["secret_id"])  # a dangling binding (secret deleted) → skip, don't KeyError
            if s is None:
                continue
            injectors.inject(headers, params, b, crypto.decrypt(s.value))  # a bad binding must not 500 the run
        r = await client.request(hc.get("method", "GET"), url, headers=headers, params=params, timeout=15.0)
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        return "invalid", f"injection failed: {exc}"  # a bad binding IS a credential/config problem
    except Exception as exc:  # noqa: BLE001 — transport/timeout says nothing about the credential
        return "unknown", f"probe unreachable: {exc}"
    expect = hc.get("expect_status")
    if expect:
        return ("ok" if r.status_code == expect else "invalid"), f"probe HTTP {r.status_code}"
    if r.status_code < 400:
        return "ok", f"probe HTTP {r.status_code}"
    if r.status_code >= 500 or r.status_code == 429:  # upstream down / rate-limited ≠ bad credential
        return "unknown", f"probe HTTP {r.status_code}"
    return "invalid", f"probe HTTP {r.status_code}"  # 4xx (401/403/other) = credential/config problem


async def gc_expired_invites(db: AsyncSession, org_id: int) -> int:
    """Delete expired invites in an org (their codes can never be accepted). Caller commits.
    Runs opportunistically when invites are listed and periodically in the health run."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = (await db.execute(select(Invite).where(Invite.org_id == org_id))).scalars().all()
    n = 0
    for inv in rows:
        exp = inv.expires_at
        if exp is not None and (exp.replace(tzinfo=None) if exp.tzinfo else exp) < now:
            await db.delete(inv)
            n += 1
    return n


async def gc_stale_pending_oauth(db: AsyncSession, org_id: int) -> int:
    """Delete in-flight OAuth connects older than the TTL — each holds an encrypted client_secret and
    an otherwise-indefinitely-valid CSRF `state`. Caller commits."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=OAUTH_PENDING_TTL_MIN)
    rows = (await db.execute(select(PendingOAuth).where(PendingOAuth.org_id == org_id))).scalars().all()
    n = 0
    for p in rows:
        ca = p.created_at
        if ca is not None and (ca.replace(tzinfo=None) if ca.tzinfo else ca) < cutoff:
            await db.delete(p)
            n += 1
    return n


async def run_all(db: AsyncSession, client: httpx.AsyncClient, org_id: int | None = None) -> dict:
    now = datetime.now(timezone.utc)
    secret_q, tool_q = select(Secret), select(Tool)
    if org_id is not None:  # scope the run to one org (the caller's) — no cross-tenant leakage
        secret_q = secret_q.where(Secret.org_id == org_id)
        tool_q = tool_q.where(Tool.org_id == org_id)
    secrets = {s.id: s for s in (await db.execute(secret_q)).scalars().all()}
    tools = (await db.execute(tool_q)).scalars().all()
    evaluated: set[int] = set()

    def apply(s: Secret, sid: int, status: str, detail: str) -> None:
        if s.kind == "param":  # a param (project id, org id…) is config, not a credential — no verdicts
            return
        # Worst-status-wins WITHIN a run: a secret shared by several tools must never be downgraded
        # by a later tool (e.g. a no-probe tool marking a probe-failed oauth secret back to "ok").
        if sid in evaluated and _RANK[status] <= _RANK.get(s.health_status, 1):
            return
        _mark(s, status, detail, now)
        evaluated.add(sid)

    for tool in tools:
        # One bad tool (a malformed health_check, a weird binding, a decrypt error) must NEVER 500 the
        # whole batch — a health run has to be resilient. Isolate each tool; on an unexpected error mark
        # its secrets "unknown" (a runner problem, not proof the credential is bad) and keep going.
        try:
            smap = {sid: secrets[sid] for b in tool.bindings
                    if (sid := b.get("secret_id")) is not None and sid in secrets}
            oauth_bad = False
            for sid, s in smap.items():
                if s.kind == "oauth":
                    try:
                        await oauth.ensure_fresh(s, db, client)
                    except Exception as exc:  # noqa: BLE001
                        apply(s, sid, "invalid", f"oauth refresh failed: {exc}")
                        oauth_bad = True
            if oauth_bad:
                continue  # a dead credential can't pass a probe; already flagged
            hc = tool.health_check if isinstance(tool.health_check, dict) else None  # ignore a malformed hc
            if hc and hc.get("path") is not None:
                status, detail = await _probe(tool, smap, client)
                for sid, s in smap.items():
                    apply(s, sid, status, detail)
            else:
                for sid, s in smap.items():
                    if s.kind == "oauth":
                        apply(s, sid, "ok", "oauth refresh ok")  # refresh succeeded above (won't downgrade a probe-fail)
                    # non-oauth without a probe can't be validated without calling — leave as-is
        except Exception as exc:  # noqa: BLE001 — contain the blast radius to this one tool
            for b in tool.bindings:
                sid = b.get("secret_id")
                if sid in secrets:
                    apply(secrets[sid], sid, "unknown", f"health check errored: {str(exc)[:120]}")
        # Persist THIS tool's verdicts before moving on — so the whole sweep isn't one long transaction
        # holding a DB connection idle across every slow probe (bad for Postgres). expire_on_commit=False
        # keeps the in-memory objects usable for the worst-status-wins logic in later iterations.
        await db.commit()

    # A secret no longer bound to any tool carries a frozen verdict about a binding that's gone —
    # reset it to "unknown" so /health and /admin/health don't report a permanently-stale ok/invalid.
    bound_ids = {b.get("secret_id") for tool in tools for b in tool.bindings}
    for sid, s in secrets.items():
        if sid not in bound_ids and sid not in evaluated and s.health_status != "unknown":
            _mark(s, "unknown", "no longer bound to any tool", now)

    if org_id is not None:
        await gc_expired_invites(db, org_id)  # periodic cleanup of dead invite codes
        await gc_stale_pending_oauth(db, org_id)  # and abandoned OAuth connects
    await db.commit()
    # Notify/report only what THIS run actually checked — a secret marked invalid in a past run but
    # no longer bound to any tool would otherwise be re-alerted on every run, forever.
    invalid = [secrets[sid] for sid in evaluated if secrets[sid].health_status == "invalid"]
    await _notify(invalid, db, client)
    return {
        "checked": len(evaluated),
        "invalid": [_view(s) for s in invalid],
        "all": [_view(s) for s in secrets.values()],
    }


async def _notify(invalid: list[Secret], db: AsyncSession, client: httpx.AsyncClient) -> None:
    """Best-effort: POST invalid credentials to the owner's per-org webhook. Never raises.

    The webhook now lives on the owner's Membership in the secret's org, so group by
    (org_id, owner) and resolve that membership's webhook_url.
    """
    if not invalid:
        return
    groups: dict[tuple[int | None, str], list[dict]] = {}
    for s in invalid:
        groups.setdefault((s.org_id, s.owner), []).append(_view(s))
    for (org_id, owner), items in groups.items():
        webhook = None
        user = (await db.execute(select(User).where(User.email == owner))).scalar_one_or_none()
        if user is not None:
            # A webhook_url is typically set only on the owner's PERSONAL-org membership at
            # registration (team memberships carry none), so search ALL their memberships,
            # preferring the one in this org.
            mems = (await db.execute(select(Membership).where(Membership.user_id == user.id))).scalars().all()
            mems.sort(key=lambda m: 0 if m.org_id == org_id else 1)
            for m in mems:
                if safe_webhook_url(m.webhook_url):
                    webhook = m.webhook_url
                    break
        if webhook is None:
            # Owner has LEFT the org entirely — fall back to any current org-owner's webhook so an
            # invalid credential is never left un-alerted.
            for om in (await db.execute(select(Membership).where(
                Membership.org_id == org_id, Membership.role == "owner"
            ))).scalars().all():
                if safe_webhook_url(om.webhook_url):
                    webhook = om.webhook_url
                    break
        if webhook is None:
            continue
        try:
            await client.post(webhook, json={"owner": owner, "invalid_credentials": items}, timeout=10.0)
        except Exception:  # noqa: BLE001 — an alert hiccup must never break the run
            pass
