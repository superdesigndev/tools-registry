"""Transactional email via Resend — the two sends the registry needs:

  1. send_otp    — the 6-digit sign-in code (POST /auth/email/start)
  2. send_invite — a team invitation with its one-time code (POST /orgs/{id}/invites)

Best-effort by design: every send is wrapped so a mail outage can NEVER break sign-in or invite
creation (the code still exists server-side; the CLI/dashboard flows keep working). If no
`TREG_RESEND_API_KEY` is set, sends are skipped (logged), so local/dev without a key is fine.
"""
from __future__ import annotations

import httpx

from .config import get_settings

RESEND_URL = "https://api.resend.com/emails"

# ---- Monologue-skin email chrome (matches the landing page): charcoal ground, hardware
# card, cyan accent, mono everywhere. Inline styles + solid fallbacks only (email-safe).
_MONO = "ui-monospace,Menlo,Consolas,'DM Mono',monospace"
_WRAP = (
    '<div style="background:#151412;padding:36px 16px;font-family:' + _MONO + '">'
    '<div style="max-width:460px;margin:0 auto;background:#1c1b19;border:1px solid #2f2d2a;border-radius:18px;overflow:hidden">'
    '<div style="padding:16px 24px;border-bottom:1px solid #2a2825;background:#232220;'
    'font-family:' + _MONO + ';font-size:14px;color:#8e8c86">'
    '<span style="color:#e4714a">▚</span> <span style="color:#19D0E8;font-weight:600">treg</span>'
    ' <span style="color:#5d5a54">·</span> tools-registry</div>'
    '<div style="padding:26px 24px">{body}</div>'
    '</div>'
    '<div style="max-width:460px;margin:14px auto 0;text-align:center;color:#6d6a63;font-size:11px;font-family:' + _MONO + '">'
    'You received this because someone used this address with tools-registry. If that wasn\'t you, ignore it.</div>'
    '</div>'
)


async def _send(to: str, subject: str, html: str, text: str) -> bool:
    """POST one email to Resend. Returns True on 2xx; never raises."""
    s = get_settings()
    if not s.resend_api_key:
        print(f"[email] no TREG_RESEND_API_KEY — skipping send to {to} ({subject!r})")
        return False
    payload = {"from": s.email_from, "to": [to], "subject": subject, "html": html, "text": text}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {s.resend_api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if r.status_code >= 300:
            print(f"[email] Resend {r.status_code} sending to {to}: {r.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001 — mail must never break the calling flow
        print(f"[email] send to {to} failed: {e}")
        return False


async def send_otp(email: str, code: str, ttl_minutes: int = 10) -> bool:
    body = (
        '<p style="margin:0 0 6px;color:#f2efe8;font-size:16px;font-weight:600">Your sign-in code</p>'
        f'<p style="margin:0 0 18px;color:#8e8c86;font-size:13px;line-height:1.6">Enter this to sign in to tools-registry. It expires in {ttl_minutes} minutes.</p>'
        f'<div style="font-family:{_MONO};font-size:32px;font-weight:700;letter-spacing:8px;color:#19D0E8;background:#0d0c0b;border:1px solid #2f2d2a;border-radius:12px;padding:18px;text-align:center">{code}</div>'
        '<p style="margin:18px 0 0;color:#8e8c86;font-size:12px;line-height:1.6">Didn\'t try to sign in? You can safely ignore this email — no one can act without this code.</p>'
    )
    html = _WRAP.format(body=body)
    text = f"Your tools-registry sign-in code is: {code}\nIt expires in {ttl_minutes} minutes. If you didn't request it, ignore this email."
    return await _send(email, f"{code} is your tools-registry sign-in code", html, text)


async def send_invite(email: str, inviter: str, org_name: str, role: str, code: str,
                      email_token: str, expires_at: str = "", link_base: str = "",
                      shared: str = "") -> bool:
    """`shared` = a human phrase for a share-born invite (e.g. 'the skill “slideshow”') — the email
    then leads with what was shared and its button lands on that page after sign-in."""
    s = get_settings()
    from urllib.parse import quote
    # The link should open on the SAME deployment the inviter was using. The request origin
    # (link_base) captures that; public_url is the fallback for callers without a request.
    base = (link_base or s.public_url).rstrip("/")
    # The link carries `email_token`, NOT `code`: the token exists only in this email, so clicking
    # proves inbox access and /auth/invite-signin may sign the invitee in (POST-confirm, one-time).
    # The visible code below stays the out-of-band credential the admin also holds — join-only.
    url = f"{base}/auth/invite-signin?t={quote(email_token)}"
    exp = f" It expires on {expires_at[:10]}." if expires_at else ""
    headline = (f'<b style="color:#f2efe8">{_esc(inviter)}</b> shared {_esc(shared)} with you — join <b style="color:#f2efe8">{_esc(org_name)}</b> to use it'
                if shared else f'<b style="color:#f2efe8">{_esc(inviter)}</b> invited you to <b style="color:#f2efe8">{_esc(org_name)}</b>')
    body = (
        f'<p style="margin:0 0 6px;color:#f2efe8;font-size:16px;line-height:1.5">{headline}</p>'
        f'<p style="margin:0 0 18px;color:#8e8c86;font-size:13px;line-height:1.6">You\'ve been added as <b style="color:#f2efe8">{_esc(role)}</b> on tools-registry — call the team\'s tools with <span style="color:#19D0E8">no API keys on your machine</span>.{exp}</p>'
        f'<a href="{url}" style="display:inline-block;background:#19D0E8;color:#062a30;text-decoration:none;font-weight:700;font-size:14px;font-family:{_MONO};padding:12px 24px;border-radius:999px">Sign in &amp; accept →</a>'
        f'<p style="margin:18px 0 8px;color:#8e8c86;font-size:12px;line-height:1.6">Sign in with <b style="color:#f2efe8">{_esc(email)}</b> and the invite appears automatically. Prefer a code? Use this one:</p>'
        f'<div style="font-family:{_MONO};font-size:13px;color:#f2efe8;background:#0d0c0b;border:1px solid #2f2d2a;border-radius:12px;padding:12px;word-break:break-all">{_esc(code)}</div>'
    )
    html = _WRAP.format(body=body)
    text = (
        (f"{inviter} shared {shared} with you on tools-registry (team {org_name}, as {role}).\n" if shared
         else f"{inviter} invited you to {org_name} as {role} on tools-registry.\n")
        + f"Sign in at {url} with {email} and the invite appears automatically.\n"
        f"Or accept with this one-time code: {code}.{exp}"
    )
    subject = (f"{inviter} shared {shared} with you on tools-registry" if shared
               else f"{inviter} invited you to {org_name} on tools-registry")
    return await _send(email, subject, html, text)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
