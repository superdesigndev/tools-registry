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

# ---- Ledger-ish email chrome: light card, clay accent, mono code. Inline styles only (email-safe).
_WRAP = (
    '<div style="background:#f7f3ea;padding:32px 16px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
    '<div style="max-width:460px;margin:0 auto;background:#fffdf8;border:1px solid #e3dac7;border-radius:12px;overflow:hidden">'
    '<div style="padding:18px 24px;border-bottom:1px solid #e3dac7;font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:700;color:#201c15;letter-spacing:-.3px">▚ tools-registry</div>'
    '<div style="padding:24px">{body}</div>'
    '</div>'
    '<div style="max-width:460px;margin:12px auto 0;text-align:center;color:#877e6c;font-size:11px">You received this because someone used this address with tools-registry. If that wasn\'t you, ignore it.</div>'
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
        '<p style="margin:0 0 6px;color:#201c15;font-size:15px">Your sign-in code</p>'
        f'<p style="margin:0 0 16px;color:#877e6c;font-size:13px">Enter this to sign in to tools-registry. It expires in {ttl_minutes} minutes.</p>'
        f'<div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:32px;font-weight:700;letter-spacing:8px;color:#b8461f;background:#f0eadc;border:1px solid #e3dac7;border-radius:8px;padding:16px;text-align:center">{code}</div>'
        '<p style="margin:16px 0 0;color:#877e6c;font-size:12px">Didn\'t try to sign in? You can safely ignore this email — no one can act without this code.</p>'
    )
    html = _WRAP.format(body=body)
    text = f"Your tools-registry sign-in code is: {code}\nIt expires in {ttl_minutes} minutes. If you didn't request it, ignore this email."
    return await _send(email, f"{code} is your tools-registry sign-in code", html, text)


async def send_invite(email: str, inviter: str, org_name: str, role: str, code: str,
                      email_token: str, expires_at: str = "") -> bool:
    s = get_settings()
    from urllib.parse import quote
    base = s.public_url.rstrip("/")
    # The link carries `email_token`, NOT `code`: the token exists only in this email, so clicking
    # proves inbox access and /auth/invite-signin may sign the invitee in (POST-confirm, one-time).
    # The visible code below stays the out-of-band credential the admin also holds — join-only.
    url = f"{base}/auth/invite-signin?t={quote(email_token)}"
    exp = f" It expires on {expires_at[:10]}." if expires_at else ""
    body = (
        f'<p style="margin:0 0 6px;color:#201c15;font-size:15px"><b>{_esc(inviter)}</b> invited you to <b>{_esc(org_name)}</b></p>'
        f'<p style="margin:0 0 16px;color:#877e6c;font-size:13px">You\'ve been added as <b style="color:#201c15">{_esc(role)}</b> on tools-registry — call the team\'s tools with no API keys on your machine.{exp}</p>'
        f'<a href="{url}" style="display:inline-block;background:#b8461f;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 20px;border-radius:8px">Sign in &amp; accept →</a>'
        f'<p style="margin:16px 0 6px;color:#877e6c;font-size:12px">Sign in with <b>{_esc(email)}</b> and the invite appears automatically. Prefer a code? Use this one:</p>'
        f'<div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;color:#201c15;background:#f0eadc;border:1px solid #e3dac7;border-radius:8px;padding:12px;word-break:break-all">{_esc(code)}</div>'
    )
    html = _WRAP.format(body=body)
    text = (
        f"{inviter} invited you to {org_name} as {role} on tools-registry.\n"
        f"Sign in at {url} with {email} and the invite appears automatically.\n"
        f"Or accept with this one-time code: {code}.{exp}"
    )
    return await _send(email, f"{inviter} invited you to {org_name} on tools-registry", html, text)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
