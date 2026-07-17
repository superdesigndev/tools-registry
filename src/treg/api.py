"""The API — the only brain. CLI + skill are thin clients over this (charter).

Surface: open user registration (creates a personal org) + per-membership token auth; full CRUD
on secrets and tools; the /skills composer (register a whole skill = bundle + its secrets + its
tool(s) atomically) and /bundles reads; the /call proxy with a fire-and-forget audit record; and
/calls. A tool carries a LIST of bindings (multi-credential), with flat single-binding sugar on POST.

Multi-tenancy: a token = a (user, org) Membership. Every list/create/mutation and the proxy are
scoped to the caller's org; `owner` (creator email) drives the member-vs-admin role gate.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from sqlalchemy import func

INVITE_TTL_DAYS = 7  # invite codes are one-time AND expire after this many days

import httpx
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from . import audit, crypto, demo as demo_seed, email as email_sender, health, injectors, localrun, oauth
from . import pubfeed, ratestore, runner, sandbox as demo_sandbox, session as sess
from .config import get_settings
from .db import get_session, init_db
from .models import ROLE_RANK, Bundle, CallRecord, Invite, Membership, Org, PendingOAuth, RunRecord, Secret, Tool, User
from .proxy import relay


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # One long-lived client for ALL upstream calls (rule 1: keepalive). The pool reuses
    # TCP+TLS connections across requests — the single biggest latency win for a relay.
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200)
    app.state.http = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(30.0))
    try:
        yield
    finally:
        await audit.drain()  # flush pending audit writes before tearing down
        await app.state.http.aclose()


app = FastAPI(title="tools-registry", version="0.0.1", lifespan=lifespan)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """The dashboard is an authenticated app; ship the baseline hardening headers it was missing —
    nosniff, clickjacking protection (X-Frame-Options), and a tight Referrer-Policy. `setdefault`
    so the /call proxy's own stricter CSP/nosniff isn't clobbered."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # HSTS pins the browser to https so a spoofed X-Forwarded-Proto can't downgrade the session
    # cookie onto cleartext (browsers ignore this header when served over http, so dev is unaffected).
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


_BODY_ENC_HEADER = b"x-treg-body-encoding"


def _decode_request_body(raw: bytes, enc: str) -> bytes:
    """Undo the transforms named in `enc` (left to right; `+`/`,`-separated). Supports `base64` and
    `gzip`, combinable (e.g. `base64+gzip` = base64-decode then gunzip). This lets a client smuggle a
    body whose plaintext (SQL, HTML) would otherwise trip an upstream WAF that inspects request bodies
    -- the edge sees only opaque base64, the server restores the real bytes before any route reads them."""
    out = raw
    for step in (s.strip().lower() for s in enc.replace(",", "+").split("+") if s.strip()):
        if step == "base64":
            out = base64.b64decode(out)
        elif step == "gzip":
            out = gzip.decompress(out)
        else:
            raise ValueError(f"unsupported body encoding: {step}")
    return out


class _BodyDecodeMiddleware:
    """Pure-ASGI: when a request carries `X-Treg-Body-Encoding`, decode the body before routing. The
    JSON endpoints (Pydantic re-reads the decoded body) and the /call proxy (which relays
    request.body() upstream) then both see the real bytes. No-op for requests without the header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        enc = next((v.decode("latin-1") for k, v in scope["headers"] if k == _BODY_ENC_HEADER), None)
        if enc is None:
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                break
        try:
            decoded = _decode_request_body(b"".join(chunks), enc)
        except Exception:  # noqa: BLE001 -- a malformed encoded body is a client error, not a 500
            return await JSONResponse({"detail": "invalid X-Treg-Body-Encoding body"}, status_code=400)(scope, receive, send)
        # Strip the marker, drop content-encoding, and fix content-length to the decoded size.
        headers = [(k, v) for k, v in scope["headers"]
                   if k not in (_BODY_ENC_HEADER, b"content-length", b"content-encoding")]
        headers.append((b"content-length", str(len(decoded)).encode("latin-1")))
        new_scope = dict(scope, headers=headers)
        delivered = False

        async def receive_decoded():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": decoded, "more_body": False}
            return {"type": "http.disconnect"}

        return await self.app(new_scope, receive_decoded, send)


app.add_middleware(_BodyDecodeMiddleware)


@app.exception_handler(OverflowError)
async def _id_out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
    # A huge all-digit path param (e.g. /secrets/999…) overflows SQLite's 64-bit INTEGER at bind
    # time; that's a non-existent id, not a server fault — surface a 404 instead of a 500.
    return JSONResponse({"detail": "identifier out of range"}, status_code=404)

_WEB_DIR = Path(__file__).parent / "web"

_app_version_cache: tuple[float, str] | None = None  # (index.html mtime, content hash)


def _app_version() -> str:
    """A stamp that changes with every deploy of the dashboard bundle: a hash of index.html,
    re-derived when the file's mtime moves (so dev --reload picks up edits too). Long-lived tabs
    compare this against the value they booted with and offer a refresh when it drifts."""
    global _app_version_cache
    index = _WEB_DIR / "index.html"
    try:
        mtime = index.stat().st_mtime
    except OSError:
        return "dev"
    if _app_version_cache is None or _app_version_cache[0] != mtime:
        digest = hashlib.sha256(index.read_bytes()).hexdigest()[:12]
        _app_version_cache = (mtime, digest)
    return _app_version_cache[1]


@app.get("/meta")
async def meta() -> dict:
    """Open: what the dashboard needs to render correct, shareable snippets — the public proxy URL
    (so copy/paste snippets use the real domain, not whatever origin the browser happens to be on)
    — plus the bundle version, so an open tab can detect a new deploy and offer a refresh."""
    s = get_settings()
    return {"public_url": s.public_url.rstrip("/"), "github": bool(s.github_client_id),
            "google": bool(s.google_client_id), "app_version": _app_version()}


@app.get("/providers.json", include_in_schema=False)
async def providers_catalog() -> dict:
    """Open: the provider catalog `treg upload` uses to detect env keys → tools. Served so the CLI can
    refresh it centrally (add a provider here → every CLI picks it up) with its bundled copy as fallback.
    See [env-import](../docs/context/interface/env-import.md)."""
    from . import providers as prov
    return {"version": prov.CATALOG_VERSION, "providers": prov.CATALOG}


# ---- human login via GitHub OAuth (dashboard sessions) ------------------------------------
def _is_https(request: Request) -> bool:
    # behind a reverse proxy (Render), TLS is terminated upstream and forwarded as http + X-Forwarded-Proto.
    return request.headers.get("x-forwarded-proto", "").lower() == "https" or request.url.scheme == "https"


def _same_origin(request: Request) -> bool:
    """CSRF guard for cookie-authenticated mutations: the Origin header (when a browser sends one)
    must be this server itself. "Itself" is EITHER the configured public URL or the host the request
    actually arrived on — public_url alone would reject legitimate localhost/dev-box origins."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return True  # non-browser clients (and some same-origin GETs) send no Origin
    if origin == get_settings().public_url.rstrip("/"):
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return origin == f"{'https' if _is_https(request) else 'http'}://{host}"


# In-memory handshake state for `treg login` (single-instance; short-lived, fine to lose on restart).
# Both carry a created-at so abandoned handshakes (unauthenticated, attacker-chosen keys) are swept
# rather than accumulating forever — the results map holds live 30-day tokens, so it must not leak.
_cli_states: dict[str, tuple[str, datetime]] = {}   # oauth state -> (login_id, created_at)
_cli_results: dict[str, tuple[dict, datetime]] = {}  # login_id -> (result, created_at) — a completed login
# login_id -> (pairing_code, attempts_left, created_at). Created by POST /auth/cli/start; the browser must
# echo the code back at approve time (validated server-side) before a token is issued. This is the phishing
# guard: a login the user didn't start has no matching code, and the poll endpoint carries no code to
# brute-force. The code is shown ONLY in the terminal, never in the /login URL.
_cli_pending: dict[str, tuple[str, int, datetime]] = {}
CLI_TOKEN_TTL = 30 * 24 * 3600      # identity token lifetime for the CLI
HANDSHAKE_TTL = 600                  # seconds an abandoned login handshake lingers before eviction
CLI_APPROVE_MAX_TRIES = 8           # wrong pairing-code attempts before a pending login is discarded
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous (no O/0/I/1); matches the CLI's charset


def _prune_handshakes() -> None:
    cutoff = _utcnow_naive() - timedelta(seconds=HANDSHAKE_TTL)
    for k in [k for k, (_, t) in _cli_states.items() if t < cutoff]:
        _cli_states.pop(k, None)
    for k in [k for k, (_, t) in _cli_results.items() if t < cutoff]:
        _cli_results.pop(k, None)
    for k in [k for k, (_, _, t) in _cli_pending.items() if t < cutoff]:
        _cli_pending.pop(k, None)


@app.get("/auth/github")
async def auth_github(request: Request, cli: str = ""):
    s = get_settings()
    if not s.github_client_id:
        raise HTTPException(status_code=503, detail="GitHub login not configured")
    redirect = f"{s.public_url.rstrip('/')}/auth/github/callback"
    state = crypto.new_token()
    if cli:  # this is a `treg login` handshake, not a browser session
        _prune_handshakes()  # evict abandoned handshakes so this map can't grow unbounded
        _cli_states[state] = (cli, _utcnow_naive())
    url = (f"{s.github_authorize_url}?client_id={s.github_client_id}"
           f"&redirect_uri={quote(redirect, safe='')}&scope={quote('read:user user:email')}&state={state}")
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("treg_oauth_state", state, httponly=True, max_age=600, samesite="lax", secure=_is_https(request))
    return resp


_AUTH_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>tools-registry</title>'
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">'
    "<style>"
    ':root{--bg:#151412;--panel:#1c1b19;--ink:#f2efe8;--muted:rgba(242,239,232,.55);'
    '--line:rgba(255,255,255,.1);--accent:#19D0E8;'
    '--mono:"DM Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}'
    "html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:var(--mono)}"
    "body{background:radial-gradient(90% 50% at 50% -10%,rgba(255,255,255,.04),transparent 60%),var(--bg)}"
    ".wrap{min-height:100%;display:flex;align-items:center;justify-content:center;padding:24px}"
    ".card{background:linear-gradient(180deg,#201f1d,#171614);border:1px solid var(--line);border-radius:20px;"
    "padding:34px 40px;max-width:440px;text-align:center;"
    "box-shadow:rgba(255,255,255,.08) 0 1px 0 inset, 0 30px 70px rgba(0,0,0,.5)}"
    ".logo{color:var(--accent);font-size:15px;letter-spacing:.5px;margin-bottom:18px}"
    ".mark{font-size:34px;line-height:1;margin-bottom:14px}"
    'h1{font-family:"Geist Pixel",var(--mono);font-size:22px;margin:0 0 8px;font-weight:400;letter-spacing:0}'
    "p{color:var(--muted);font-size:13.5px;line-height:1.55;margin:0}"
    ".pbtn{display:inline-block;background:linear-gradient(180deg,#fdfcf7,#eae7de);color:#1c1b19;border:0;"
    "border-radius:999px;padding:12px 24px;font:500 14px var(--mono);cursor:pointer;"
    "box-shadow:rgba(178,168,165,.2) -1.3px -1.3px 2.5px 0, rgba(0,0,0,.4) 2px 2px 1.5px 0}"
    "</style></head>"
)


def _auth_page(headline: str, sub: str = "", *, ok: bool = True, status: int = 200) -> HTMLResponse:
    """A brand-styled full-page response for the browser-facing auth flow (GitHub callback)."""
    sub_html = f"<p>{sub}</p>" if sub else ""
    html = (
        f'{_AUTH_HEAD}<body><div class="wrap"><div class="card">'
        f'<div class="logo">▚ tools-registry</div><div class="mark">{"✅" if ok else "⚠️"}</div>'
        f"<h1>{headline}</h1>{sub_html}</div></div></body></html>"
    )
    return HTMLResponse(html, status_code=status)


def _finish_oauth_login(request: Request, user: User, st: tuple | None) -> RedirectResponse:
    """After a GitHub/Google callback proves an identity: set the browser session cookie, then either
    land on the dashboard (a plain browser login) or bounce to /login?cli=<id> so a `treg login`
    handshake goes through the SAME team picker as the other doors (instead of completing blind — which
    would leave the CLI guessing the org). The picker's POST /auth/cli/approve reads this same cookie."""
    login_id = st[0] if st is not None else None
    dest = f"/login?cli={login_id}" if login_id else "/app"
    resp = RedirectResponse(dest, status_code=302)
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    resp.delete_cookie("treg_oauth_state")
    return resp


@app.get("/auth/github/callback")
async def auth_github_callback(
    request: Request, code: str = "", state: str = "",
    treg_oauth_state: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    if not code or not state or state != treg_oauth_state:  # CSRF: state must echo our cookie
        return _auth_page("Login failed", "Bad state. Please start the login again.", ok=False, status=400)
    s = get_settings()
    client = request.app.state.http
    try:
        tok = (await client.post(
            s.github_token_url, headers={"Accept": "application/json"},
            data={"client_id": s.github_client_id, "client_secret": s.github_client_secret,
                  "code": code, "redirect_uri": f"{s.public_url.rstrip('/')}/auth/github/callback"},
        )).json()
        access = tok.get("access_token")
        if not access:
            return _auth_page("Login failed", "No access token from GitHub.", ok=False, status=400)
        gh = {"Authorization": f"Bearer {access}", "Accept": "application/json", "User-Agent": "treg"}
        prof = (await client.get(f"{s.github_api_url}/user", headers=gh)).json()
        email = prof.get("email")
        if not email:
            emails = (await client.get(f"{s.github_api_url}/user/emails", headers=gh)).json()
            if isinstance(emails, list):
                email = (next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
                         or next((e["email"] for e in emails if e.get("verified")), None))
        if not email:
            return _auth_page("Login failed", "No verified email on your GitHub account.", ok=False, status=400)
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] github callback error: {exc}")  # keep internals server-side, not in the response
        return _auth_page("Login failed", "Something went wrong. Please try again.", ok=False, status=502)

    user = await _find_or_create_user(db, email)  # first login = registration (user only; no auto org)
    if user.suspended:  # a banned account may prove its email but must not receive a live session
        return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
    await db.commit()

    # Browser session OR `treg login` handshake — both go through the /login team picker now.
    return _finish_oauth_login(request, user, _cli_states.pop(state, None))


@app.get("/auth/google")
async def auth_google(request: Request, cli: str = ""):
    """Human login via Google OAuth — a parallel door to GitHub, same session/CLI-handshake plumbing."""
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(status_code=503, detail="Google login not configured")
    redirect = f"{s.public_url.rstrip('/')}/auth/google/callback"
    state = crypto.new_token()
    if cli:  # a `treg login` handshake, not a browser session
        _prune_handshakes()
        _cli_states[state] = (cli, _utcnow_naive())
    url = (f"{s.google_authorize_url}?client_id={s.google_client_id}"
           f"&redirect_uri={quote(redirect, safe='')}&response_type=code"
           f"&scope={quote('openid email profile')}&state={state}&prompt=select_account")
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("treg_oauth_state", state, httponly=True, max_age=600, samesite="lax", secure=_is_https(request))
    return resp


@app.get("/auth/google/callback")
async def auth_google_callback(
    request: Request, code: str = "", state: str = "",
    treg_oauth_state: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    if not code or not state or state != treg_oauth_state:  # CSRF: state must echo our cookie
        return _auth_page("Login failed", "Bad state. Please start the login again.", ok=False, status=400)
    s = get_settings()
    client = request.app.state.http
    try:
        tok = (await client.post(
            s.google_token_url, headers={"Accept": "application/json"},
            data={"client_id": s.google_client_id, "client_secret": s.google_client_secret,
                  "code": code, "grant_type": "authorization_code",
                  "redirect_uri": f"{s.public_url.rstrip('/')}/auth/google/callback"},
        )).json()
        access = tok.get("access_token")
        if not access:
            return _auth_page("Login failed", "No access token from Google.", ok=False, status=400)
        prof = (await client.get(
            s.google_userinfo_url,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})).json()
        email = prof.get("email")
        if not email:
            return _auth_page("Login failed", "No email on your Google account.", ok=False, status=400)
        # Identity is keyed by email, so we must only trust a VERIFIED one — else an unverified Google
        # address equal to a victim's registered email would resolve to the victim (account takeover).
        # (Google's userinfo returns email_verified; the GitHub door already filters for verified.)
        if not prof.get("email_verified"):
            return _auth_page("Login failed", "Your Google email isn't verified.", ok=False, status=400)
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] google callback error: {exc}")  # keep internals server-side, not in the response
        return _auth_page("Login failed", "Something went wrong. Please try again.", ok=False, status=502)

    user = await _find_or_create_user(db, email)  # first login = registration (user only; no auto org)
    if user.suspended:
        return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
    await db.commit()

    # Browser session OR `treg login` handshake — both go through the /login team picker now.
    return _finish_oauth_login(request, user, _cli_states.pop(state, None))


def _norm_pair_code(code: str | None) -> str:
    """Normalise a login pairing code for comparison: strip, uppercase, drop separators/whitespace so
    `7f3k`, `7F3K`, ` 7F3K ` all match. Empty stays empty (an empty code never matches)."""
    return "".join((code or "").split()).replace("-", "").upper()


@app.post("/auth/cli/start")
async def auth_cli_start() -> dict:
    """`treg login` calls this FIRST. The SERVER mints both the login_id and a short pairing code and
    remembers them (pending approval). The code is shown only in that terminal; the browser must echo it
    back at approve time, where it's validated server-side, before any token is issued. So a login the
    user didn't start (a phished /login?cli=<id> link) can't be completed, and the poll endpoint carries
    no code to brute-force. Unauthenticated — on its own it grants nothing."""
    _prune_handshakes()
    login_id = _secrets.token_urlsafe(18)
    code = "".join(_secrets.choice(_PAIR_ALPHABET) for _ in range(4))
    _cli_pending[login_id] = (code, CLI_APPROVE_MAX_TRIES, _utcnow_naive())
    return {"login_id": login_id, "code": code}


@app.get("/auth/cli/poll")
async def auth_cli_poll(login_id: str = "") -> dict:
    """The CLI polls this after opening the browser; returns the identity token once, then forgets it.
    A token only lands here after auth_cli_approve validated the terminal pairing code, so a login the
    user didn't approve never yields one — there is nothing here to brute-force (no code parameter)."""
    _prune_handshakes()  # sweep abandoned results (they hold live tokens) so the map can't leak
    entry = _cli_results.pop(login_id, None)
    return entry[0] if entry is not None else {"status": "pending"}


# `treg login` mints the login_id with token_urlsafe(18) (24 chars); anything outside this shape is
# not one of ours. It's echoed into the /login page's JS, so the whitelist is also the XSS guard.
_LOGIN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


class CliApproveIn(BaseModel):
    login_id: str
    code: str | None = None  # the pairing code the user copied from their terminal (phishing guard)
    org: str | None = None  # the team slug the user picked in the /login org picker (optional)


async def _orgs_brief(user: User, db: AsyncSession) -> list[dict]:
    """The user's teams for the /login picker: slug, name, role, tool_count, personal. Sorted so the
    team a CLI login should default to sits first (a real team over the personal org, then most tools).
    `personal` mirrors the dashboard's rule: the auto-created org named after the user's email."""
    memberships = (await db.execute(
        select(Membership).where(Membership.user_id == user.id))).scalars().all()
    org_ids = [m.org_id for m in memberships]
    if not org_ids:
        return []
    orgs = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_(org_ids)))).scalars().all()}
    counts = dict((await db.execute(
        select(Tool.org_id, func.count(Tool.id)).where(Tool.org_id.in_(org_ids)).group_by(Tool.org_id))).all())
    out = []
    for m in memberships:
        o = orgs.get(m.org_id)
        if o is None:
            continue
        out.append({"slug": o.slug, "name": o.name, "role": m.role,
                    "tool_count": counts.get(o.id, 0), "personal": o.name == user.email})
    out.sort(key=lambda r: (r["personal"], -r["tool_count"], r["name"].lower()))
    return out


@app.get("/auth/cli/orgs")
async def auth_cli_orgs(treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session)) -> dict:
    """The /login page fetches this (session-cookie authed) to render the team picker before completing
    a `treg login` handshake. Returns the signed-in user's teams; empty list if no session."""
    user = await _user_from_session(treg_session, db)
    if user is None:
        return {"email": None, "orgs": []}
    return {"email": user.email, "orgs": await _orgs_brief(user, db)}


@app.post("/auth/cli/approve")
async def auth_cli_approve(
    request: Request, body: CliApproveIn,
    treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
) -> dict:
    """Complete a `treg login` handshake from an EXISTING browser session (the "Continue as" button
    on /login, and the email door after /auth/email/verify sets the cookie). Deliberately a POST with
    a same-origin check — auto-completing on a GET would let a phisher mail out /login?cli=<their-id>
    and poll the victim's identity token straight out of /auth/cli/poll.

    `org` (optional) is the team slug the user picked in the /login org picker; it's validated to be
    one of the user's memberships and passed back to the CLI so it lands on the RIGHT team instead of
    guessing (`_pick_active_org`)."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin approve rejected")
    if not _LOGIN_ID_RE.fullmatch(body.login_id or ""):
        raise HTTPException(status_code=400, detail="bad login_id")
    user = await _user_from_session(treg_session, db)
    if user is None:
        raise HTTPException(status_code=401, detail="no session")
    # The pairing code proves the approver is the same person who ran `treg login` (the code is shown only
    # in that terminal, via POST /auth/cli/start). Validate it HERE — where we have a session + a same-
    # origin check — so a phished /login?cli=<attacker_id> link (whose code the victim doesn't have) can
    # never complete, a mistyped code fails immediately in the browser, and the poll endpoint stays codeless.
    pending = _cli_pending.get(body.login_id)
    if pending is None:
        raise HTTPException(status_code=400, detail="this login has expired — run `treg login` again")
    expected, tries_left, started_at = pending
    typed = _norm_pair_code(body.code)
    if not typed or not hmac.compare_digest(expected.encode(), typed.encode()):
        if tries_left <= 1:  # out of attempts → discard the pending login so the code can't be ground down
            _cli_pending.pop(body.login_id, None)
            raise HTTPException(status_code=400, detail="too many wrong codes — run `treg login` again")
        _cli_pending[body.login_id] = (expected, tries_left - 1, started_at)
        raise HTTPException(status_code=400, detail="that code doesn't match the one in your terminal")
    active_org: str | None = None
    if body.org:
        org = await _resolve_org(body.org, db)
        m = (await db.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org.id))).scalar_one_or_none() if org else None
        if org is None or m is None:
            raise HTTPException(status_code=403, detail="not a member of that team")
        active_org = org.slug
    _cli_pending.pop(body.login_id, None)  # code matched → consume the pending login
    result = {"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version), "email": user.email}
    if active_org:
        result["active_org"] = active_org
    _cli_results[body.login_id] = (result, _utcnow_naive())
    return {"ok": True, "email": user.email, "active_org": active_org}


@app.get("/login", include_in_schema=False)
async def login_page(cli: str = "", treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session)):
    """The universal sign-in page `treg login` opens: reuses an existing dashboard session with one
    click ("Continue as …"), else offers every configured door — GitHub, Google, email one-time code.
    The email door is always present, so login works even with no OAuth app configured."""
    if not cli:
        return RedirectResponse("/app", status_code=302)  # a bare visit belongs on the dashboard
    if not _LOGIN_ID_RE.fullmatch(cli):
        return _auth_page("Login failed", "Bad login link. Run <code>treg login</code> again.", ok=False, status=400)
    s = get_settings()
    user = await _user_from_session(treg_session, db)
    return HTMLResponse(_login_page_html(
        cli, session_email=user.email if user else None,
        github=bool(s.github_client_id), google=bool(s.google_client_id)))


def _login_page_html(login_id: str, *, session_email: str | None, github: bool, google: bool) -> str:
    """Server-rendered /login card. login_id is whitelist-validated by the caller; the session email
    is HTML-escaped (it's the only other interpolated value)."""
    from html import escape

    # A pairing-code block sits above everything: whichever door the user takes, approve() won't complete
    # the CLI handshake until the code shown in their own terminal is echoed back (phishing guard — a
    # login they didn't start has no matching code). A `treg login` link carries the code in the URL
    # fragment, so the JS swaps this input for a read-only display the user just visually confirms;
    # the typed input remains the fallback for links without one. #orgpick is ALWAYS present (filled by loadOrgs when a
    # session exists at load, and after the email door signs in). #doors holds the sign-in options; the
    # divider only shows when a session pre-exists.
    parts: list[str] = [
        '<div id="pcbox"><div class="pklabel">Enter the code shown in your terminal:</div>'
        '<input id="paircode" autocomplete="off" autocapitalize="characters" spellcheck="false" '
        'inputmode="latin" placeholder="e.g. 7F3K" maxlength="9"></div>',
        '<div id="orgpick"></div>',
    ]
    # With a live session the doors are noise — the user is one click from done. Collapse them behind
    # the divider (an accordion); a click expands. No session → no divider, doors always visible.
    if session_email:
        parts.append('<div class="div acc" id="other-acct" onclick="toggleDoors()" role="button" tabindex="0" '
                     'onkeydown="if(event.key===\'Enter\')toggleDoors()">'
                     'use a different account <span id="acc-caret">▸</span></div>')
    doors: list[str] = []
    if github:
        doors.append(f'<a class="btn" href="/auth/github?cli={login_id}">Sign in with GitHub</a>')
    if google:
        doors.append(f'<a class="btn" href="/auth/google?cli={login_id}">Sign in with Google</a>')
    doors.append(
        '<div id="email-door">'
        '<div id="email-row"><input id="em" type="email" placeholder="you@company.com" autocomplete="email">'
        '<button class="btn" onclick="sendCode()">Email me a code</button></div>'
        '<div id="code-row" style="display:none"><input id="code" inputmode="numeric" placeholder="6-digit code">'
        '<button class="btn primary" onclick="verifyCode()">Verify</button></div>'
        '<div class="hint" id="hint"></div></div>')
    doors_style = ' style="display:none"' if session_email else ''
    parts.append(f'<div id="doors" class="stack"{doors_style}>{"".join(doors)}</div>')
    has_session = "true" if session_email else "false"
    return (
        f"{_AUTH_HEAD.replace('</style>', _LOGIN_CSS + '</style>')}"
        f'<body><div class="wrap"><div class="card" id="card">'
        f'<div class="logo">▚ tools-registry</div><h1>Sign in</h1>'
        f'<p>to connect the <b>treg</b> CLI to your account</p>'
        f'<div class="stack">{"".join(parts)}</div><div class="err" id="err"></div>'
        f"</div></div>"
        f"<script>const HAS_SESSION={has_session};{_LOGIN_JS.replace('__LOGIN_ID__', login_id)}</script></body></html>"
    )


_LOGIN_CSS = (
    ".btn{display:block;width:100%;box-sizing:border-box;padding:11px 14px;border-radius:9px;"
    "border:1px solid var(--line);background:#332d23;color:var(--ink);font-family:var(--mono);"
    "font-size:13.5px;cursor:pointer;text-decoration:none;text-align:center}"
    ".btn:hover{border-color:var(--accent)}"
    ".btn.primary{background:var(--accent);border-color:var(--accent);color:#211d16;font-weight:700}"
    ".stack{display:flex;flex-direction:column;gap:10px;margin-top:18px}"
    ".stack>div{display:flex;flex-direction:column;gap:10px}"
    ".div{display:flex;flex-direction:row!important;align-items:center;gap:10px;color:var(--muted);font-size:12px;margin:6px 0 0}"
    ".div:before,.div:after{content:'';flex:1;border-top:1px solid var(--line)}"
    ".div.acc{cursor:pointer;user-select:none}.div.acc:hover{color:var(--ink)}"
    "#email-row,#code-row{display:flex;flex-direction:column;gap:10px}"
    "input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid var(--line);"
    "background:#1c1913;color:var(--ink);font-family:var(--mono);font-size:13.5px}"
    ".err{color:#d78f6c;font-size:12.5px;margin-top:10px;min-height:1em}"
    ".hint{color:var(--muted);font-size:12px}"
    ".muted{color:var(--muted)}"
    ".team{display:flex;justify-content:space-between;align-items:center;gap:10px;text-align:left}"
    ".team .tn{display:flex;flex-direction:column;gap:2px;min-width:0}"
    ".team .tnm{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".team .tm{font-size:11px;color:var(--muted)}"
    ".team.primary .tm{color:#211d16;opacity:.8}"
    ".pklabel{font-size:12px;color:var(--muted);margin:2px 0 2px}"
    "#paircode-show{font-size:22px;font-weight:700;letter-spacing:8px;text-align:center;color:var(--accent);"
    "padding:10px 12px 10px 20px;border:1px dashed var(--line);border-radius:9px;background:#1c1913}"
)

# The page's whole brain: every door funnels into approve(), which completes the CLI handshake.
# done() builds DOM via textContent (the email came over JSON — never trust it into innerHTML).
_LOGIN_JS = """
const LID='__LOGIN_ID__';
// `treg login` puts the pairing code in the URL FRAGMENT (#code=…) — it never reaches the server on
// the GET. When present, show it read-only for a visual match against the terminal instead of making
// the user type it; approve() still sends it for full server-side validation. No fragment (an old CLI,
// or a link someone stripped it from) → the typed-input fallback below stays.
const PAIR=(()=>{const m=/[#&]code=([A-Za-z0-9-]{1,16})/.exec(location.hash||'');return m?m[1].toUpperCase():''})();
if(PAIR){const box=document.getElementById('pcbox');if(box){box.innerHTML='';
 const l=document.createElement('div');l.className='pklabel';l.textContent='Check this code matches your terminal:';box.appendChild(l);
 const c=document.createElement('div');c.id='paircode-show';c.textContent=PAIR;box.appendChild(c);}}
const pairCode=()=>PAIR||((document.getElementById('paircode')||{}).value||'');
// Signed-in users see the doors collapsed behind the "use a different account" divider.
function toggleDoors(){const d=document.getElementById('doors');if(!d)return;
 const open=d.style.display==='none';d.style.display=open?'':'none';
 const c=document.getElementById('acc-caret');if(c)c.textContent=open?'\\u25be':'\\u25b8'}
const err=m=>{document.getElementById('err').textContent=m||''};
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 let d={};try{d=await r.json()}catch(e){}
 if(!r.ok)throw new Error(d.detail||('error '+r.status));return d}
function done(email){const c=document.getElementById('card');c.innerHTML='';
 const mk=(t,cls,txt)=>{const e=document.createElement(t);if(cls)e.className=cls;if(txt)e.textContent=txt;c.appendChild(e);return e};
 mk('div','logo','\\u259a tools-registry');mk('div','mark','\\u2705');
 mk('h1',null,email?('Logged in as '+email):'Logged in');
 mk('p',null,'Return to your terminal. The CLI is finishing up, you can close this tab.')}
async function approve(org){err('');
 const pc=pairCode();
 if(!pc.trim()){err('Enter the code shown in your terminal to continue.');const el=document.getElementById('paircode');if(el)el.focus();return}
 try{const b={login_id:LID,code:pc};if(org)b.org=org;const d=await post('/auth/cli/approve',b);done(d.email)}catch(e){err(e.message)}}
let CREATED_ORG=null;  // remember a just-created team so a retry (e.g. after a wrong code) reuses it, never makes a 2nd
async function createTeam(){err('');
 const pc=pairCode();
 if(!pc.trim()){err('Enter the code shown in your terminal to continue.');const el=document.getElementById('paircode');if(el)el.focus();return}
 const inp=document.getElementById('newteam');const name=(inp&&inp.value||'').trim();if(!name)return err('give your team a name');
 try{if(!CREATED_ORG){const o=await post('/orgs',{name:name});CREATED_ORG=o.org;}await approve(CREATED_ORG);}catch(e){err(e.message)}}
// Render the team picker into #orgpick once a session exists (fetched, so it also runs after the
// email door signs in). One team → a single "Continue as" button; many → a labelled list.
async function loadOrgs(){const box=document.getElementById('orgpick');if(!box)return;
 let d;try{d=await(await fetch('/auth/cli/orgs',{credentials:'include'})).json()}catch(e){return}
 const orgs=d.orgs||[];box.innerHTML='';
 if(!orgs.length){  // brand-new user: no team yet → make them NAME one (never finish the CLI login team-less)
  const l=document.createElement('div');l.className='pklabel';l.textContent='Name your team to finish signing in'+(d.email?(' ('+d.email+')'):'')+':';box.appendChild(l);
  const inp=document.createElement('input');inp.id='newteam';inp.placeholder='Team name, e.g. Superdesign';inp.autocomplete='off';box.appendChild(inp);
  const b=document.createElement('button');b.className='btn primary';b.textContent='Create team \\u2192';b.onclick=createTeam;box.appendChild(b);
  inp.addEventListener('keyup',e=>{if(e.key==='Enter')createTeam()});inp.focus();return}
 if(orgs.length>1){const l=document.createElement('div');l.className='pklabel';l.textContent='Continue as '+d.email+' — pick a team:';box.appendChild(l)}
 orgs.forEach((o,i)=>{const b=document.createElement('button');b.className='btn team'+((i===0&&orgs.length>1)?' primary':'');b.onclick=()=>approve(o.slug);
  const tn=document.createElement('div');tn.className='tn';
  const nm=document.createElement('div');nm.className='tnm';nm.textContent=o.name+(o.personal?' (personal)':'');
  const mt=document.createElement('div');mt.className='tm';mt.textContent=o.role+' · '+o.tool_count+' tool'+(o.tool_count===1?'':'s');
  tn.appendChild(nm);tn.appendChild(mt);b.appendChild(tn);
  if(orgs.length===1){const c=document.createElement('span');c.textContent='→';b.appendChild(c)}
  box.appendChild(b)});
 if(orgs.length===1){box.firstChild.classList.add('primary')}}
async function sendCode(){err('');const em=document.getElementById('em').value.trim();if(!em)return err('enter your email');
 try{const d=await post('/auth/email/start',{email:em});
  document.getElementById('code-row').style.display='';
  document.getElementById('hint').textContent=d.dev_code?('dev code: '+d.dev_code):('code sent to '+d.email);
 }catch(e){err(e.message)}}
async function verifyCode(){err('');const em=document.getElementById('em').value.trim(),co=document.getElementById('code').value.trim();
 if(!co)return err('enter the code');
 try{await post('/auth/email/verify',{email:em,code:co});
  // The email door just set a session cookie — hide the doors and show the team picker.
  const d=document.getElementById('doors');if(d)d.style.display='none';
  const o=document.getElementById('other-acct');if(o)o.style.display='none';
  await loadOrgs();
 }catch(e){err(e.message)}}
if(HAS_SESSION)loadOrgs();
"""


@app.get("/auth/me")
async def auth_me(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Who is the caller? Drives the dashboard's identity display in BOTH session mode (cookie) and
    token mode (X-Treg-Token) — the token door otherwise had no way to learn its own email, which
    broke `isPersonal` and join-by-code."""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        if user is not None and user.suspended:
            user = None
    else:
        user = await _user_from_session(treg_session, db)
    if user is None:
        raise HTTPException(status_code=401, detail="no session")
    return {"email": user.email, "is_superadmin": user.is_superadmin, "onboarded": user.onboarded,
            "github": bool(get_settings().github_client_id)}


@app.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    # A cross-site auto-submitted form could force-logout the victim (the cookie delete is a "simple"
    # request). Bind it to same-origin: reject a request whose Origin isn't treg's own.
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin logout rejected")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sess.COOKIE)
    return resp


# ---- human login via email one-time code (the third identity door) ------------------------
# OTP code + its brute-force counter, and the /auth/email/start throttle, live in the DB (treg.ratestore
# over the Ephemeral table) — NOT per-process dicts — so a restart can't reset them and they stay correct
# across instances (backlog #3). The 'otp' namespace holds {code_hash, attempts} keyed by email; the
# 'otp_start' namespace holds the per-email + per-IP sliding windows (email-bomb + brute-force guard).
EMAIL_CODE_TTL = 10 * 60  # seconds a code stays valid
MAX_OTP_ATTEMPTS = 5  # invalidate a code after this many wrong guesses (brute-force guard)
OTP_NS = "otp"
OTP_START_NS = "otp_start"
OTP_START_WINDOW_S = 900      # 15 minutes
OTP_START_MAX_PER_EMAIL = 5   # code requests for one inbox per window (caps bombing a single victim)
OTP_START_MAX_PER_IP = 30     # code requests from one IP per window (looser — offices/NAT share an IP)


class EmailStartIn(BaseModel):
    email: str


class EmailVerifyIn(BaseModel):
    email: str
    code: str


@app.post("/auth/email/start")
async def auth_email_start(
    request: Request, body: EmailStartIn, db: AsyncSession = Depends(get_session)
) -> dict:
    """Prove ownership of an email: mint a 6-digit code. With no mail sender yet, dev mode returns
    + logs it (so dummy emails are testable); prod will email it instead. Throttled per-email AND per-IP
    (sliding window) so this open endpoint can't be used to email-bomb an inbox or reset the OTP
    brute-force counter at will. All this state is in the DB (survives restart, correct multi-instance)."""
    email = _norm_email(body.email)
    if email.endswith("@" + demo_seed.DEMO_DOMAIN):  # fake onboarding teammates are roster-only — never a login
        raise HTTPException(status_code=400, detail="that's a demo address — pick a real email")
    await ratestore.sweep(db, OTP_START_NS)  # bound the namespace before we add to it
    if not await ratestore.rate_check(
        db, OTP_START_NS,
        [(f"e:{email}", OTP_START_MAX_PER_EMAIL), (f"i:{_client_ip(request)}", OTP_START_MAX_PER_IP)],
        OTP_START_WINDOW_S,
    ):
        await db.commit()  # persist the pruning/sweep even on reject
        raise HTTPException(status_code=429, detail="too many code requests — please wait a few minutes")
    code = f"{_secrets.randbelow(1_000_000):06d}"
    await ratestore.kv_put(db, OTP_NS, email,
                           {"hash": crypto.hash_token(code), "attempts": MAX_OTP_ATTEMPTS}, EMAIL_CODE_TTL)
    await db.commit()
    resp = {"sent": True, "email": email}
    if get_settings().expose_dev_code:  # local sqlite only — never leaks the code on a real (Postgres) deploy
        print(f"[email-otp] {email} -> {code}")  # surfaces in the server log
        resp["dev_code"] = code
    else:
        await email_sender.send_otp(email, code, ttl_minutes=EMAIL_CODE_TTL // 60)  # best-effort; never raises
    return resp


@app.post("/auth/email/verify")
async def auth_email_verify(
    request: Request, body: EmailVerifyIn, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Check the code → find-or-create the user → mint an identity token AND set a browser session
    cookie. The CLI reads the token from the body; the dashboard just reloads into session mode
    (same path as GitHub login) — one endpoint serves both clients."""
    email = _norm_email(body.email)
    entry = await ratestore.kv_get(db, OTP_NS, email)  # None if missing OR expired (kv_get drops expired)
    if entry is None:
        await db.commit()  # persist the lazy delete of an expired code, if any
        raise HTTPException(status_code=401, detail="invalid code")
    if not hmac.compare_digest(entry["hash"], crypto.hash_token(body.code.strip())):
        entry["attempts"] -= 1  # a wrong guess burns an attempt; the code dies after MAX_OTP_ATTEMPTS
        if entry["attempts"] <= 0:
            await ratestore.kv_pop(db, OTP_NS, email)
        else:
            await ratestore.kv_put(db, OTP_NS, email, entry, ttl_s=None)  # keep the code's original expiry
        await db.commit()
        raise HTTPException(status_code=401, detail="invalid code")
    await ratestore.kv_pop(db, OTP_NS, email)  # one-time
    user = await _find_or_create_user(db, email)
    if user.suspended:  # a banned account may prove its email but must not receive a live token
        raise HTTPException(status_code=403, detail="account suspended")
    await db.commit()
    resp = JSONResponse({"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version), "email": user.email})
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


async def _live_invite_by_email_token(db: AsyncSession, t: str) -> Invite | None:
    """Resolve an emailed invite-link token to a live invite: pending, unexpired, unconsumed
    (email_token_hash is nulled on first use), and not pointing at a platform-locked org."""
    t = (t or "").strip()
    if not t:
        return None
    invite = (await db.execute(select(Invite).where(Invite.email_token_hash == crypto.hash_token(t)))
              ).scalar_one_or_none()
    if (invite is None or invite.status != "pending"
            or (invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive())):
        return None
    org = await db.get(Org, invite.org_id)
    if org is None or org.suspended:
        return None
    return invite


@app.get("/auth/invite-signin")
async def auth_invite_signin(
    request: Request, code: str = "", t: str = "",
    treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    """Landing for an invite email link. Two secrets, two very different trust levels:

    `t` (email_token) exists ONLY in the emailed link — possession proves inbox access, the same bar
    as the emailed OTP — so it may sign the invitee in. But not on this GET: corporate mail scanners
    (Outlook SafeLinks etc.) prefetch GET links and would consume a one-time credential before the
    human ever clicks. So the GET only renders a confirm page whose button POSTs the token back;
    the POST below mints the session.

    `code` (legacy + out-of-band) is also returned to the admin who created the invite, so it can
    NEVER be an authentication factor — holding it lets you JOIN (POST /invites/accept), not log in.
    Links carrying ?code= (emails sent before the split, or relayed by an admin) keep their old
    behavior: validate and bounce to the SPA login with the email prefilled; the invitee proves the
    email through a real door (OTP / GitHub / Google) and the invite auto-appears via /invites/mine.
    An invalid/expired secret of either kind just lands on the site."""
    from urllib.parse import quote
    base = get_settings().public_url.rstrip("/")
    if t:
        invite = await _live_invite_by_email_token(db, t)
        if invite is None:
            return RedirectResponse("/?invite_expired=1", status_code=303)
        org = await db.get(Org, invite.org_id)
        # Already signed in as someone ELSE? Warn — continuing replaces that browser session.
        switch_note = ""
        uid = sess.read(treg_session)
        if uid is not None:
            current = await db.get(User, uid)
            if current is not None and current.email != invite.email:
                switch_note = (f"<p>You're currently signed in as <b>{_esc_html(current.email)}</b> — "
                               f"continuing switches this browser to <b>{_esc_html(invite.email)}</b>.</p>")
        return HTMLResponse(
            f'{_AUTH_HEAD}<body><div class="wrap"><div class="card">'
            f'<div class="logo">▚ tools-registry</div><div class="mark">👋</div>'
            f'<h1>Join {_esc_html(org.name if org else "the team")}</h1>'
            f'<p><b>{_esc_html(invite.invited_by or "A teammate")}</b> invited '
            f'<b>{_esc_html(invite.email)}</b> as {_esc_html(invite.role)}.</p>{switch_note}'
            f'<form method="post" action="/auth/invite-signin" style="margin-top:18px">'
            f'<input type="hidden" name="t" value="{_esc_html(t.strip())}">'
            f'<button type="submit" class="pbtn">'
            f'Continue as {_esc_html(invite.email)} →</button></form>'
            f"</div></div></body></html>"
        )
    c = (code or "").strip()
    invite = (await db.execute(select(Invite).where(Invite.code_hash == crypto.hash_token(c)))
              ).scalar_one_or_none() if c else None
    if (invite is None or invite.status != "pending"
            or (invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive())):
        return RedirectResponse("/?invite_expired=1", status_code=303)
    # Code path: same redirect whether or not the email already has an account — the code is a
    # convenience that prefills the sign-in email, never an authentication factor. A suspended
    # account is caught at the real login door, the only place the code path can mint a session.
    return RedirectResponse(f"/?invite={quote(invite.email)}", status_code=303)


@app.post("/auth/invite-signin")
async def auth_invite_signin_confirm(request: Request, db: AsyncSession = Depends(get_session)):
    """The confirm page's POST: the emailed one-time token signs the invitee in. Mirrors the OTP
    door (auth_email_verify) — find-or-create the user, refuse the suspended, set the session
    cookie — because the trust source is identical: only the inbox saw this secret. The token is
    consumed here (one-time) so a link floating in a forwarded thread can't be replayed; the invite
    itself stays PENDING — acceptance happens in the dashboard, where a multi-team invitee can
    accept several at once. Body is parsed by hand (urlencoded form) to avoid the python-multipart
    dependency FastAPI's Form() would pull in."""
    from urllib.parse import parse_qs
    try:
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — any junk body = no token
        form = {}
    t = (form.get("t", [""])[0] or "").strip()
    invite = await _live_invite_by_email_token(db, t)
    if invite is None:  # consumed / expired / revoked / suspended org → the SPA's expired banner
        return RedirectResponse("/?invite_expired=1", status_code=303)
    user = await _find_or_create_user(db, invite.email)  # first click = registration (user only, no auto org)
    if user is None or user.suspended:  # a banned account may hold the link but must not get a session
        return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
    invite.email_token_hash = None  # consume: one sign-in per emailed link
    db.add(invite)
    await db.commit()
    # A share-born invite lands on the shared page itself (the SPA auto-accepts + switches org);
    # a plain invite lands on the dashboard with the accept banner, as before. `landing` was
    # allowlist-validated at create time, so this can never redirect off-app.
    dest = f"{invite.landing}?invite_org={invite.org_id}" if invite.landing else f"/?invite_org={invite.org_id}"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


def _esc_html(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


@app.get("/", include_in_schema=False)
async def landing(request: Request, treg_session: str = Cookie(default=""),
                  db: AsyncSession = Depends(get_session)):
    """Serve the marketing landing at the root. Any query string (invite links, OAuth returns,
    tour deep-links) belongs to the SPA, so those requests fall through to the dashboard —
    the landing is only the clean, parameterless front door. A signed-in visitor belongs on
    the dashboard, so a live session redirects to /app instead of re-showing the pitch."""
    page = _WEB_DIR / "landing.html"
    if page.exists() and not request.query_params:
        if treg_session and await _user_from_session(treg_session, db):
            return RedirectResponse("/app", status_code=302)
        return FileResponse(page, headers={"Cache-Control": "no-cache"})
    return await dashboard()


@app.get("/app", include_in_schema=False)
async def dashboard():
    """Serve the single-file dashboard (same-origin, so it calls this API directly)."""
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _spa_with_og(kind: str, name: str):
    """Serve the SPA at a shareable detail path (/app/skills/x, /app/tools/x) with per-resource
    og/twitter meta so link unfurls show what was shared. The meta echoes only the URL's own
    name segment — no DB read, so an unauthenticated crawler learns nothing it didn't send."""
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    label = "skill" if kind == "skills" else "tool"
    safe = _esc_html(name)
    html = index.read_text(encoding="utf-8").replace(
        "<title>tools-registry</title>",
        f"<title>{safe} · tools-registry</title>\n"
        f'<meta property="og:title" content="{safe} — shared {label}"/>\n'
        f'<meta property="og:description" content="A {label} shared via tools-registry. '
        f'Sign in to preview it and get the one-command install."/>\n'
        f'<meta name="twitter:card" content="summary"/>',
        1,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/app/skills/{name}", include_in_schema=False)
async def dashboard_skill_page(name: str):
    return _spa_with_og("skills", name)


@app.get("/app/tools/{name}", include_in_schema=False)
async def dashboard_tool_page(name: str):
    return _spa_with_og("tools", name)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    """Agent-readable overview (llms.txt convention) — an AI agent that fetches this learns the
    whole registry: the call protocol, discovery, auth, CLI, skills, and links to the tutorial/docs.
    The serving domain is templated in so links stay correct across deploys."""
    f = _WEB_DIR / "llms.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="llms.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/plain; charset=utf-8")


@app.get("/install.sh", include_in_schema=False)
async def install_sh():
    """`curl -fsSL {BASE}/install.sh | sh` — installs the treg CLI and points it at this server.
    The serving domain is templated in so it targets whichever host is live (dev box or the real
    domain after deploy)."""
    f = _WEB_DIR / "install.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="install.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/x-shellscript; charset=utf-8")


def _serve_md(name: str) -> PlainTextResponse:
    """Serve a bundled markdown file as inline text (so "open in new tab" shows it, not a download),
    with the serving domain templated in. Backs the 'copy markdown' buttons on the docs pages."""
    f = _WEB_DIR / name
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8")


@app.get("/quickstart.md", include_in_schema=False)
async def quickstart_md():
    """The quick-start as raw markdown — copy it or open it in a tab and use it anywhere."""
    return _serve_md("quickstart.md")


@app.get("/tutorial.md", include_in_schema=False)
async def tutorial_md():
    """The full tutorial as raw markdown (mirrors the interactive /tutorial)."""
    return _serve_md("tutorial.md")


@app.get("/tutorial-import-shell.md", include_in_schema=False)
async def tutorial_import_shell_md():
    """Focused tutorial: CLI auto-import (`treg upload clis`) + shell mode (`treg shell`) + the
    local-run security sandbox. Linked from the main tutorial."""
    return _serve_md("tutorial-import-shell.md")


@app.get("/tutorial-access.md", include_in_schema=False)
async def tutorial_access_md():
    """Focused tutorial: per-member team access control (which tools a member may use + the local-run
    toggle). Linked from the main tutorial."""
    return _serve_md("tutorial-access.md")


@app.get("/skill.md", include_in_schema=False)
async def skill_md():
    """The OFFICIAL tools-registry Claude skill (3 personas), {BASE}-templated to this server.
    install.sh drops it into ~/.claude/skills/tools-registry/ so agents learn treg at CLI install."""
    return _serve_md("skill.md")


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """The ▚ brand mark. Served at both paths so browsers that auto-request /favicon.ico stop 404ing."""
    ico = _WEB_DIR / "favicon.svg"
    if not ico.exists():
        raise HTTPException(status_code=404, detail="favicon not bundled")
    return FileResponse(ico, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/tutorial.js", include_in_schema=False)
async def tutorial_js():
    """The shared interactive-tutorial data + highlighter (window.TREG_TUTORIAL / tregHL).
    Loaded by both the dashboard Help view and the standalone tutorial page, so they never drift."""
    js = _WEB_DIR / "tutorial.js"
    if not js.exists():
        raise HTTPException(status_code=404, detail="tutorial.js not bundled")
    return FileResponse(js, media_type="application/javascript")


@app.get("/tutorial", include_in_schema=False)
async def tutorial_page():
    """Standalone shareable interactive tutorial (same STEPS[] as the dashboard Help view)."""
    page = _WEB_DIR / "tutorial.html"
    if not page.exists():
        return HTMLResponse("<h3>Tutorial not bundled.</h3>")
    return FileResponse(page)


# The interactive dashboard tour (matted screenshots) — served + its WebP images, at /dashboard-tour/.
_TOUR_DIR = _WEB_DIR / "tour"
if _TOUR_DIR.exists():
    app.mount("/dashboard-tour", StaticFiles(directory=str(_TOUR_DIR), html=True), name="dashboard-tour")


# ---- caller auth (token = a Membership; open registration) --------------------------------
@dataclass
class Caller:
    """The resolved caller: their membership (org + role + token), identity, and org row.
    A token identifies a (user, org) pair, so `org_id`/`email`/`role` all come from here.
    """

    membership: Membership
    user: User
    org: Org

    @property
    def org_id(self) -> int:
        return self.membership.org_id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def role(self) -> str:
        return self.membership.role


async def _membership_by_token(token: str, db: AsyncSession) -> Membership | None:
    if not token:
        return None
    return (
        await db.execute(select(Membership).where(Membership.token_hash == crypto.hash_token(token)))
    ).scalar_one_or_none()


async def _user_from_session(cookie: str, db: AsyncSession) -> User | None:
    claims = sess.read_claims(cookie)
    if claims is None:
        return None
    user = await db.get(User, claims["uid"])
    if user is None or user.suspended or claims["tv"] != user.token_version:  # revoked = tv mismatch
        return None
    return user


async def _resolve_org(ref: str, db: AsyncSession) -> Org | None:
    """Resolve an X-Treg-Org header (a slug, or a numeric id) to an Org. Slug wins first: an
    all-digit slug is producible (`_slugify("2024") == "2024"`), so an id-first lookup would
    reinterpret a member's own slug as a primary key and lock them out of their org."""
    if not ref:
        return None
    by_slug = (await db.execute(select(Org).where(Org.slug == ref))).scalar_one_or_none()
    if by_slug is not None:
        return by_slug
    # int() of a huge all-digit ref would overflow SQLite's 64-bit INTEGER → 500 inside the auth
    # dependency; bound it so an out-of-range X-Treg-Org just falls through to the 400.
    return await db.get(Org, int(ref)) if (ref.isdigit() and int(ref) < 2**63) else None


async def require_identity(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> User:
    """Just *who* the caller is (no org): a token's user, or a session user. 401 otherwise."""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        if m is not None:
            # A published public-demo token must never act as a USER — user-level endpoints mint
            # identity tokens (/auth/cli-token), create real orgs, and accept invites, all of which
            # would let a stranger escape the demo org. Admin+ (the real operator) is exempt.
            org = await db.get(Org, m.org_id)
            if org is not None and org.public_demo and not _role_at_least(m.role, "admin"):
                raise HTTPException(status_code=403, detail=(
                    "this is a public demo token — it can only call the demo team's tools"))
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        if user is not None and not user.suspended:
            return user
        raise HTTPException(status_code=401, detail="invalid token")
    user = await _user_from_session(treg_session, db)
    if user is not None:
        return user
    raise HTTPException(status_code=401, detail="not authenticated")


@app.get("/auth/cli-token")
async def auth_cli_token(user: User = Depends(require_identity)) -> dict:
    """Mint a fresh CLI/bearer token for the authenticated caller (session cookie OR token). Identity
    tokens are stateless (`sess.make`), so handing one out rotates/invalidates nothing — it just lets
    the dashboard embed a working token in copy-paste snippets + a 'copy token' button, so a human
    doesn't have to hunt for it in `~/.treg/config.json`. Pair it with `X-Treg-Org` to pick the org."""
    return {"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version), "email": user.email}


@app.post("/auth/revoke-tokens")
async def auth_revoke_tokens(
    request: Request,
    user: User = Depends(require_identity),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Kill switch for a leaked token: invalidate every signed identity token (from `treg login`) AND
    every browser session this user holds, in one step. Bumping user.token_version makes all previously
    minted tokens (which carry the old tv) mismatch and be rejected. Unlike suspending the account this
    keeps the user active; unlike rotating TREG_SESSION_SECRET it affects ONLY this user. We then re-issue
    a fresh session cookie + token for the caller, so the device that pressed the button stays signed in
    while every other device is signed out. (Org membership tokens from accept-invite are a separate token
    type and are unaffected — those are revoked by removing the membership.)"""
    user.token_version += 1  # same db session as require_identity (FastAPI caches the dependency)
    await db.commit()
    resp = JSONResponse({"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version),
                         "email": user.email, "revoked": True})
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


async def _user_from_identity_token(token: str, db: AsyncSession) -> User | None:
    """A signed identity token (from `treg login`) — same format as the session cookie, sent as a
    bearer by the CLI. Returns the user if valid + not suspended."""
    claims = sess.read_claims(token)
    if claims is None:
        return None
    user = await db.get(User, claims["uid"])
    if user is None or user.suspended or claims["tv"] != user.token_version:  # revoked = tv mismatch
        return None
    return user


async def require_member(
    request: Request,
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> Caller:
    """A caller acting in a specific org. Two ways in:
    - **token** (agents/CLI): the token IS a membership, so the org is baked in.
    - **session** (dashboard): the cookie identifies the user; the org is chosen via `X-Treg-Org`.
    """
    membership = await _membership_by_token(x_treg_token, db) if x_treg_token else None
    if membership is not None:  # per-org token — the org is baked in
        user = await db.get(User, membership.user_id)
        org = await db.get(Org, membership.org_id)
    else:
        # identity token (CLI `treg login`) or a browser session — pick the org via X-Treg-Org
        user = (await _user_from_identity_token(x_treg_token, db)) if x_treg_token else await _user_from_session(treg_session, db)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid token" if x_treg_token else "not authenticated")
        org = await _resolve_org(x_treg_org, db)
        if org is None:
            raise HTTPException(status_code=400, detail="choose an org (send X-Treg-Org)")
        membership = (
            await db.execute(
                select(Membership).where(Membership.user_id == user.id, Membership.org_id == org.id)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=403, detail="not a member of this org")
    if user is None or org is None:
        raise HTTPException(status_code=401, detail="invalid token")
    if user.suspended:
        raise HTTPException(status_code=403, detail="account suspended")
    if org.suspended:
        raise HTTPException(status_code=403, detail="org suspended")
    # Public-demo lockdown: the published token (non-admin roles) may ONLY call tools and read.
    # Centralized here — not per-endpoint — so every mutation (tools, secrets, skills, members,
    # leave, runs) is frozen no matter what routes are added later. Admin+ keeps full control.
    if org.public_demo and not _role_at_least(membership.role, "admin"):
        if not (request.url.path.startswith("/call/") or request.method in ("GET", "HEAD", "OPTIONS")):
            raise HTTPException(status_code=403, detail=(
                "this is a public demo team — its token can only call tools and read"))
    return Caller(membership=membership, user=user, org=org)


async def require_superadmin(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> str:
    """Cross-tenant gate for /admin/*. Authorized by the env admin token, a token whose user is
    is_superadmin, OR a session whose user is is_superadmin. Returns a principal (for audit)."""
    admin = get_settings().admin_token
    if x_treg_token and admin and hmac.compare_digest(x_treg_token, admin):
        return "env-admin"
    user: User | None = None
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
    else:
        user = await _user_from_session(treg_session, db)
    if user is not None and user.is_superadmin and not user.suspended:
        return user.email
    if not x_treg_token and not treg_session:  # nothing presented → not authenticated
        raise HTTPException(status_code=401, detail="not authenticated")
    raise HTTPException(status_code=403, detail="super-admin required")


async def _is_last_active_superadmin(db: AsyncSession, target: User) -> bool:
    """True if `target` is currently the ONLY active (unsuspended) super-admin, so demoting /
    suspending / deleting them would leave the platform with no reachable admin."""
    if not (target.is_superadmin and not target.suspended):
        return False  # not an active super-admin → removing them changes nothing about the floor
    actives = (
        await db.execute(select(User).where(User.is_superadmin.is_(True), User.suspended.is_(False)))
    ).scalars().all()
    return len(actives) <= 1


async def _cascade_delete_org(org: Org, db: AsyncSession) -> None:
    """Delete every org-scoped row then the org. Shared by owner delete_org + admin force-delete."""
    for model in (Tool, Secret, Bundle, PendingOAuth, CallRecord, RunRecord, Invite, Membership):
        for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
            await db.delete(r)
    await db.delete(org)


def _role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, 99)


def _can_manage(caller: Caller, resource) -> bool:
    """Admin/owner may manage any resource in the org; a member only what they created."""
    return _role_at_least(caller.role, "admin") or resource.owner == caller.email


def _require_can_register(caller: Caller) -> None:
    """Registering (secrets/tools/skills/oauth) needs member+. A viewer may only call + read."""
    if not _role_at_least(caller.role, "member"):
        raise HTTPException(status_code=403, detail="viewers can call and read, but cannot register")


def _tool_allowed(caller: Caller, tool_name: str) -> bool:
    """Per-member tool ACL: allowed if the member's `tool_access` is unset (NULL = ALL tools) or names
    this tool. The OWNER is never restricted (the org's authority); admins/members can be."""
    if caller.role == "owner":
        return True
    access = caller.membership.tool_access
    return access is None or tool_name in access


def _require_tool_access(caller: Caller, tool_name: str) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on the member's tool ACL."""
    if not _tool_allowed(caller, tool_name):
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the tool {tool_name!r} in this team — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --tools …`)"))


async def _visible_secret_ids(caller: Caller, db: AsyncSession) -> set[int] | None:
    """The secret ids a tool-restricted member may SEE: the ones wired into their allowed tools
    (HTTP bindings + cli.inject). None = unrestricted (owner / NULL tool_access) — show all. The
    ACL isn't just a call gate: listings must not reveal credentials the member can't use."""
    if caller.role == "owner" or caller.membership.tool_access is None:
        return None
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    ids: set[int] = set()
    for t in tools:
        if not _tool_allowed(caller, t.name):
            continue
        ids |= {b.get("secret_id") for b in (t.bindings or []) if b.get("secret_id") is not None}
        ids |= {e.get("secret_id") for e in ((t.cli or {}).get("inject") or []) if e.get("secret_id") is not None}
    return ids


def _require_local_run(caller: Caller) -> None:
    """Gate the LOCAL run tier on the member's `local_run_enabled` (owner exempt). Off → server only."""
    if caller.role != "owner" and not caller.membership.local_run_enabled:
        raise HTTPException(status_code=403, detail=(
            "local execution is disabled for you — run on the server instead (`treg run --server`), "
            "or ask an admin to enable local runs for your account"))


# ---- schemas ------------------------------------------------------------------------------
class UserIn(BaseModel):
    email: str
    webhook_url: str | None = None


class OrgIn(BaseModel):
    name: str


class InviteIn(BaseModel):
    email: str
    role: str = "member"
    expires_days: int = INVITE_TTL_DAYS
    # Access to seed onto the membership on accept: tool_access None = all tools, a list = the allowed
    # tool names; local_run may be turned off. Both default to the unrestricted state.
    tool_access: list[str] | None = None
    local_run_enabled: bool = True
    landing: str | None = None  # a shared detail page ("/app/skills/<name>") to land on after sign-in


# Landing must be one of OUR detail paths — a path-only allowlist so an emailed invite link can never
# become an open redirect (no scheme, no host, no traversal, single trailing name segment).
_LANDING_RE = re.compile(r"^/app/(skills|tools)/[A-Za-z0-9][A-Za-z0-9._%-]*$")


class AcceptIn(BaseModel):
    code: str
    email: str


class RoleIn(BaseModel):
    role: str


class CapIn(BaseModel):
    daily_call_cap: int  # per-user, per-day usage cap for the member; -1 = unlimited


class AccessIn(BaseModel):
    # tool_access: None = all tools (clear the restriction); a list = the ONLY tool names allowed.
    tool_access: list[str] | None = None
    local_run_enabled: bool = True


class SecretIn(BaseModel):
    name: str
    value: str
    kind: str = "env"
    bundle_id: int | None = None


class SecretUpdate(BaseModel):
    name: str | None = None
    value: str | None = None
    kind: str | None = None


class ToolIn(BaseModel):
    name: str
    base_url: str
    bundle_id: int | None = None
    # Multi-binding (explicit) — each: {secret_id, injector, location, name, format, secret_field}
    bindings: list[dict] | None = None
    # Single-binding sugar (the common case): provide secret_id + placement, get one binding.
    secret_id: int | None = None
    injector: str = "env"
    auth_in: str = "header"
    auth_name: str = "Authorization"
    auth_format: str = "Bearer {secret}"
    secret_field: str = "access_token"
    health_check: dict | None = None  # {method, path, expect_status}
    examples: list[dict] | None = None  # [{method, path, note}]
    cli: dict | None = None  # local-run profile for `treg run` (docs/CLI-RUN-PLAN.md)


class ToolUpdate(BaseModel):
    base_url: str | None = None
    bindings: list[dict] | None = None
    health_check: dict | None = None
    examples: list[dict] | None = None
    cli: dict | None = None  # set/replace the local-run profile; explicit null clears it


class GrantIn(BaseModel):
    argv: list[str] = []  # the CLI args the member is about to run (deny-checked + audited)


class RunReportIn(BaseModel):
    audit_id: int      # the grant's audit row — proves this report follows a real grant
    exit_code: int
    verdict: str       # ok | credential_invalid | unknown_error (client matched stderr locally)


class BundleUpdate(BaseModel):
    recipe: str | None = None  # edit the SKILL.md text of a recipe/skill bundle
    # (Run metadata moved to Tool.cli — a tool with a cli profile is runnable.)


class SkillSecretIn(BaseModel):
    local_name: str  # name within the skill; bindings reference it by this
    value: str
    kind: str = "env"


class SkillToolIn(BaseModel):
    name: str
    base_url: str
    bindings: list[dict] = []  # each binding's "secret" is a local_name, resolved server-side
    health_check: dict | None = None  # optional {method, path, expect_status}
    examples: list[dict] = []  # optional [{method, path, note}]
    cli: dict | None = None  # optional local-run profile; inject entries may reference local_names


class SkillIn(BaseModel):
    name: str
    recipe: str = ""  # the SKILL.md text
    files: dict[str, str] = {}  # companion files {relpath: content} — the rest of the skill folder
    secrets: list[SkillSecretIn] = []
    tools: list[SkillToolIn] = []
    # (Execution config — both run tiers — lives in each tool's `cli` block: bin/server/enabled/inject.)


class SkillFileIn(BaseModel):
    path: str      # the file's path relative to the picked folder (webkitRelativePath)
    content: str


class SkillAnalyzeIn(BaseModel):
    files: list[SkillFileIn] = []


class SkillImportIn(BaseModel):
    files: list[SkillFileIn] = []
    select: list[str] = []           # skill names to register (empty = every ready one)
    env_values: dict[str, str] = {}  # user-filled values for env secrets missing from the upload


class OAuthStartIn(BaseModel):
    name: str  # the secret name to create on success
    client_id: str
    client_secret: str
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    scopes: list[str] = []
    redirect_uri: str | None = None  # defaults to treg's public callback


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:  # e.g. unbalanced IPv6 brackets "http://[::1" → don't 500, reject the input
        raise HTTPException(status_code=422, detail="base_url is not a valid URL")


def _normalize_scheme(rest: str) -> str:
    """A path param collapses `https://` to `https:/`; restore it."""
    for sch in ("https:/", "http:/"):
        if rest.startswith(sch) and not rest.startswith(sch + "/"):
            return sch + "/" + rest[len(sch):]
    return rest


def _flat_binding(body: ToolIn) -> dict:
    return {
        "secret_id": body.secret_id,
        "injector": body.injector,
        "location": body.auth_in,
        "name": body.auth_name,
        "format": body.auth_format,
        "secret_field": body.secret_field,
    }


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "org"


def _norm_email(email: str) -> str:
    """Canonical email identity: trimmed + lowercased. One human = one identity regardless of the
    case they type. Applied at every identity door + every invite comparison so `Bob@X.com` and
    `bob@x.com` never fork into two users / two orgs and an invite is always redeemable."""
    return email.strip().lower()


async def _unique_slug(base: str, db: AsyncSession) -> str:
    slug, i = base, 2
    while (await db.execute(select(Org).where(Org.slug == slug))).scalar_one_or_none() is not None:
        slug, i = f"{base}-{i}", i + 1
    return slug


async def _make_org_membership(
    db: AsyncSession, user: User, name: str, slug_base: str, role: str, webhook_url: str | None = None
) -> tuple[Org, str]:
    """Create an Org + an owner/role Membership for `user`, minting a fresh org-scoped token.
    Returns (org, plaintext token). Caller commits.
    """
    org = Org(name=name, slug=await _unique_slug(slug_base, db))
    db.add(org)
    await db.flush()
    token = crypto.new_token()
    db.add(
        Membership(
            user_id=user.id, org_id=org.id, role=role,
            token_hash=crypto.hash_token(token), webhook_url=webhook_url,
        )
    )
    return org, token


async def _find_or_create_user(db: AsyncSession, email: str) -> User:
    """Find a user by email, else register them — the user ONLY, **no auto personal org**. The shared
    core of every identity door (GitHub / Google / email OTP). A brand-new user therefore lands with
    zero teams and is asked to NAME + CREATE their first team (the dashboard's mandatory welcome, or
    `treg org create`) — we never spawn a throwaway personal org they didn't ask for. Their identity
    token is user-scoped, so it works before they have any org (org chosen per-request via X-Treg-Org).
    Caller commits."""
    email = _norm_email(email)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email)
        db.add(user)
        try:
            await db.flush()  # surfaces the unique-email violation on a concurrent first-login race
        except IntegrityError:
            await db.rollback()  # another worker just created this same new user — reuse theirs
            return (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return user


# ---- users (open registration; personal org + owner membership; token shown once) ---------
@app.post("/users")
async def register_user(body: UserIn, db: AsyncSession = Depends(get_session)) -> dict:
    email = _norm_email(body.email)
    if body.webhook_url and not health.safe_webhook_url(body.webhook_url):  # SSRF guard on the alert URL
        raise HTTPException(status_code=422, detail="webhook_url must be a public http(s) URL")
    if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(email=email)
    db.add(user)
    await db.flush()
    org, token = await _make_org_membership(
        db, user, name=email, slug_base=_slugify(email), role="owner", webhook_url=body.webhook_url
    )
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="email already registered")
    return {"id": user.id, "email": user.email, "org": org.slug, "org_id": org.id, "role": "owner", "token": token}


# ---- orgs, invites, members (multi-tenancy management) ------------------------------------
def _require_admin_of(org_id: int, caller: Caller) -> None:
    """The caller must be acting with THIS org's token (token = a membership) and be admin+."""
    if caller.org_id != org_id or not _role_at_least(caller.role, "admin"):
        raise HTTPException(status_code=403, detail="admin role in this org is required")


def _require_owner_of(org_id: int, caller: Caller) -> None:
    """Owner-only actions (change roles, delete org). Token is org-scoped, so must match."""
    if caller.org_id != org_id or caller.role != "owner":
        raise HTTPException(status_code=403, detail="owner role in this org is required")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=None) if (dt is not None and dt.tzinfo is not None) else dt


async def _count_owners(org_id: int, db: AsyncSession) -> int:
    rows = (
        await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.role == "owner"))
    ).scalars().all()
    return len(rows)


@app.post("/orgs")
async def create_org(
    body: OrgIn, user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    # Any authenticated user spins up a new org and becomes its owner, minting a fresh org-scoped
    # token for it. **require_identity, NOT require_member** — a brand-new user has zero orgs (no auto
    # personal org anymore), so creating their FIRST team must not require already being in one.
    if demo_sandbox.is_sandbox_user(user):  # the anonymous demo can't mint a real team — sign in first
        raise HTTPException(status_code=403, detail=(
            "the demo sandbox can't create a real team — sign in with GitHub, Google, or email to make one"))
    user_id = user.id  # snapshot BEFORE the loop: db.rollback() expires ORM instances, so
    name = body.name   # touching `user` afterwards could trigger a lazy load → MissingGreenlet.
    for _ in range(3):  # a concurrent create can take the slug between _unique_slug and commit — retry
        org = Org(name=name, slug=await _unique_slug(_slugify(name), db))
        db.add(org)
        await db.flush()
        token = crypto.new_token()
        db.add(Membership(user_id=user_id, org_id=org.id, role="owner", token_hash=crypto.hash_token(token)))
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(status_code=409, detail="could not allocate a unique org slug — retry")
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": "owner", "token": token}


@app.get("/orgs")
async def list_orgs(
    user: User = Depends(require_identity),
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    # "active" = the caller's current org — the token's org (token auth) or X-Treg-Org (session).
    current: int | None = None
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        current = m.org_id if m else None
    elif x_treg_org:
        org = await _resolve_org(x_treg_org, db)
        current = org.id if org else None
    memberships = (
        await db.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalars().all()
    org_ids = [m.org_id for m in memberships]
    orgs = {  # one batched query instead of one db.get per membership (N+1 on the org-switcher path)
        o.id: o for o in (await db.execute(
            select(Org).where(Org.id.in_(org_ids))
        )).scalars().all()
    }
    # Tool count per org (one grouped query) so the dashboard can land on the org that actually has
    # tools, instead of a first-run default that may be an empty team.
    tool_counts = dict((await db.execute(
        select(Tool.org_id, func.count(Tool.id)).where(Tool.org_id.in_(org_ids)).group_by(Tool.org_id)
    )).all())
    out: list[dict] = []
    for m in memberships:
        org = orgs.get(m.org_id)
        if org is None:
            continue
        out.append({
            "org_id": org.id, "slug": org.slug, "name": org.name,
            "role": m.role, "active": org.id == current, "demo": org.demo,
            "tool_count": tool_counts.get(org.id, 0),
        })
    return out


@app.post("/orgs/{org_id}/invites")
async def create_invite(
    org_id: int, body: InviteIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_admin_of(org_id, caller)
    if body.role not in ("viewer", "member", "admin"):
        raise HTTPException(status_code=422, detail="role must be 'viewer', 'member', or 'admin'")
    # Role assignment is owner-only (see set_member_role); the invite door must honour the same
    # boundary or an admin could mint fellow admins that they can't otherwise create.
    if body.role == "admin" and caller.role != "owner":
        raise HTTPException(status_code=403, detail="only an owner can invite an admin")
    email = _norm_email(body.email)
    # An email already in the org can't accept a new invite (accept would 409) — reject the dead-end up front.
    existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing_user is not None:
        m = (await db.execute(select(Membership).where(
            Membership.user_id == existing_user.id, Membership.org_id == org_id
        ))).scalar_one_or_none()
        if m is not None:
            raise HTTPException(status_code=409, detail="that email is already a member of this org")
    # Supersede any prior pending invite for this email so there's exactly one live code per invitee
    # (re-inviting used to stack duplicate pending rows that all point at the same seat).
    for prior in (await db.execute(select(Invite).where(
        Invite.org_id == org_id, Invite.email == email, Invite.status == "pending"
    ))).scalars().all():
        await db.delete(prior)
    days = max(1, min(body.expires_days, 3650))  # clamp BOTH ends — a huge value overflows datetime → 500
    expires_at = _utcnow_naive() + timedelta(days=days)
    tool_access = _normalize_tool_access(body.tool_access, await _known_access_names(org_id, db))
    if body.landing is not None and not _LANDING_RE.match(body.landing):
        raise HTTPException(status_code=422, detail="landing must be a detail path like /app/skills/<name>")
    code = crypto.new_token()
    # A SECOND secret for the email link only. The admin gets `code` back (out-of-band relay) so the
    # code can never be a sign-in factor; `email_token` is never returned here — only the inbox sees
    # it, which is what lets /auth/invite-signin treat it like an emailed OTP and mint a session.
    email_token = crypto.new_token()
    invite = Invite(
        org_id=org_id, email=email, role=body.role,
        code_hash=crypto.hash_token(code), email_token_hash=crypto.hash_token(email_token),
        invited_by=caller.email, expires_at=expires_at,
        tool_access=tool_access, local_run_enabled=body.local_run_enabled, landing=body.landing,
    )
    db.add(invite)
    await db.commit()
    org = await db.get(Org, org_id)  # for the invite email's team name
    if not email.endswith("@" + demo_seed.DEMO_DOMAIN):  # don't email the onboarding's fake teammate domain
        scheme = "https" if _is_https(request) else request.url.scheme
        host = request.headers.get("host", "")
        shared = ""  # share-born invite → the email leads with what was shared
        if body.landing:
            kind, _, name = body.landing.removeprefix("/app/").partition("/")
            shared = f'the {"skill" if kind == "skills" else "tool"} “{name}”'
        await email_sender.send_invite(  # best-effort; the code is also returned for out-of-band relay
            email, caller.email, (org.name if org else email), body.role, code, email_token,
            expires_at.isoformat(), link_base=(f"{scheme}://{host}" if host else ""), shared=shared,
        )
    return {"code": code, "email": email, "role": body.role, "org_id": org_id,
            "expires_at": expires_at.isoformat()}  # email_token deliberately NOT returned (inbox-only)


@app.post("/invites/accept")
async def accept_invite(body: AcceptIn, db: AsyncSession = Depends(get_session)) -> dict:
    # Open endpoint, protected by the unguessable one-time code. Registers the user if new,
    # joins them to the org, and mints their own org-scoped token (the admin never sees it).
    invite = (
        await db.execute(select(Invite).where(Invite.code_hash == crypto.hash_token(body.code)))
    ).scalar_one_or_none()
    email = _norm_email(body.email)
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invalid or already-used invite code")
    if invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive():
        raise HTTPException(status_code=410, detail="invite code expired")
    if invite.email != email:
        raise HTTPException(status_code=403, detail="this invite is for a different email")
    org = await db.get(Org, invite.org_id)
    if org is not None and org.suspended:  # don't let anyone join a platform-locked org
        raise HTTPException(status_code=403, detail="org suspended")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and user.suspended:  # a banned user must not accrue new memberships
        raise HTTPException(status_code=403, detail="account suspended")
    if user is None:
        # Brand-new user → create the user only. Accepting the invite below IS their first team
        # (no auto personal org — consistent with the login doors).
        user = User(email=email)
        db.add(user)
        await db.flush()
    existing = (
        await db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == invite.org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this org")
    token = crypto.new_token()
    db.add(Membership(user_id=user.id, org_id=invite.org_id, role=invite.role, token_hash=crypto.hash_token(token),
                      tool_access=invite.tool_access, local_run_enabled=invite.local_run_enabled))
    invite.status = "accepted"
    try:
        await db.commit()  # a concurrent double-accept trips uq_membership_user_org — 409, not 500
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already a member of this org")
    org = await db.get(Org, invite.org_id)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": invite.role, "token": token}


@app.get("/invites/mine")
async def my_invites(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every pending invite addressed to MY email — the code-free door. Proving my email (via any
    login method) is enough to see these; the invite code becomes a shortcut, not a requirement."""
    rows = (
        await db.execute(select(Invite).where(Invite.email == user.email, Invite.status == "pending")
                         .order_by(Invite.created_at.desc()))  # newest first — the invite you just clicked
    ).scalars().all()
    now = _utcnow_naive()
    orgs = {  # batch the org lookup (was one db.get per invite)
        o.id: o for o in (await db.execute(
            select(Org).where(Org.id.in_([inv.org_id for inv in rows]))
        )).scalars().all()
    }
    out = []
    for inv in rows:
        if inv.expires_at is not None and _as_naive(inv.expires_at) < now:
            continue
        org = orgs.get(inv.org_id)
        if org is None or org.suspended:  # a platform-locked org isn't joinable — don't surface it
            continue
        out.append({
            "id": inv.id, "org": org.slug, "org_id": org.id, "name": org.name, "role": inv.role,
            "invited_by": inv.invited_by, "landing": inv.landing,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return out


# ---- onboarding (first-run demo team) -----------------------------------------------------
class OnboardIn(BaseModel):
    team_name: str = "Acme Design"


@app.post("/onboard/demo")
async def onboard_demo(
    body: OnboardIn | None = None,
    user: User = Depends(require_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Seed a sandbox team owned by the caller — fake teammates (one per role) + a working `echo`
    tool + sample activity — so a brand-new user can feel the product immediately. Idempotent
    (reuses an existing demo team); marks the caller onboarded. Same seed for dashboard + CLI."""
    return await demo_seed.provision(db, user, (body.team_name if body else "Acme Design"))


@app.post("/onboard/skip")
async def onboard_skip(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Dismiss onboarding without seeding — so it's never auto-offered again."""
    user.onboarded = True
    await db.commit()
    return {"onboarded": True}


@app.post("/onboard/reset")
async def onboard_reset(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Remove the caller's demo team(s) + demo teammates from their real teams — a clean exit."""
    return await demo_seed.reset(db, user)


# ---- landing-page sandbox studio: an anonymous, throwaway team the visitor builds ----------
# Per-IP limiter for the unauthenticated mint endpoint, in the DB (treg.ratestore) so it survives a
# restart and holds across instances (backlog #3). It caps DB churn from the public landing page (abuse
# is otherwise structurally contained — sandbox calls never touch the network, each sandbox is capped + TTL'd).
SANDBOX_HIT_NS = "sandbox_hit"
SANDBOX_RATE_MAX = 12          # sandboxes per IP per window
SANDBOX_RATE_WINDOW_S = 3600   # 1 hour

# Per-IP limiter for /call with a PUBLIC-DEMO token (the landing page publishes one shared member
# token, so the per-user daily cap is meaningless there — thousands of strangers are one "user").
PUBLIC_DEMO_HIT_NS = "pubdemo_call"
PUBLIC_DEMO_RATE_MAX = 10      # calls per IP per window
PUBLIC_DEMO_RATE_WINDOW_S = 60


def _client_ip(request: Request) -> str:
    """Best-effort client IP — first hop of X-Forwarded-For behind the reverse proxy (Render), else the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


async def _enforce_public_demo_ip_cap(request: Request, db: AsyncSession) -> None:
    """Per-IP cap for a call made with a SHARED public credential — the published demo token or
    the sandbox live wire. Both are one identity for thousands of strangers, so meter by client IP
    rather than by user. Commits the sweep + recorded hit (get_session never auto-commits) and
    raises 429 when the window is exhausted."""
    await ratestore.sweep(db, PUBLIC_DEMO_HIT_NS)
    allowed = await ratestore.rate_check(
        db, PUBLIC_DEMO_HIT_NS, [(_client_ip(request), PUBLIC_DEMO_RATE_MAX)], PUBLIC_DEMO_RATE_WINDOW_S)
    await db.commit()
    if not allowed:
        raise HTTPException(status_code=429, detail=(
            f"demo limit reached ({PUBLIC_DEMO_RATE_MAX} calls/min per IP) — try again in a minute"))


async def _enforce_sandbox_cap(caller: Caller, model, cap: int, noun: str, db: AsyncSession) -> None:
    """Sandbox orgs may hold only a few secrets/endpoints — keep the public playground bounded."""
    if not demo_sandbox.is_sandbox(caller.org):
        return
    n = (await db.execute(select(func.count()).select_from(model).where(model.org_id == caller.org_id))).scalar_one()
    if n >= cap:
        raise HTTPException(status_code=422, detail=f"the sandbox is limited to {cap} {noun} — sign up for more")


# ---- per-user daily usage cap (usage-metering v1) -------------------------------------------
def _day_start_utc() -> datetime:
    """Midnight (00:00) of the current UTC day, naive — matches how *Record.created_at is stored."""
    return _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)


async def count_today(db: AsyncSession, org_id: int | None, user_email: str) -> int:
    """How many usage events this user has produced in this org since midnight UTC: proxy calls +
    local-run grants (both `CallRecord`) plus server runs (`RunRecord`). Two indexed COUNTs."""
    since = _day_start_utc()
    calls = (await db.execute(select(func.count()).select_from(CallRecord).where(
        CallRecord.org_id == org_id, CallRecord.user_email == user_email, CallRecord.created_at >= since,
    ))).scalar_one()
    runs = (await db.execute(select(func.count()).select_from(RunRecord).where(
        RunRecord.org_id == org_id, RunRecord.user_email == user_email, RunRecord.created_at >= since,
    ))).scalar_one()
    return calls + runs


async def _enforce_daily_cap(caller: Caller, db: AsyncSession) -> None:
    """Refuse a call/run once the caller has used their per-user daily cap for this org. `-1` (the
    default) = unlimited, so unmetered members pay ZERO extra queries. The sandbox has its own limiter
    and is exempt. Soft by design: the count reads best-effort `CallRecord`s, so under heavy load it
    can lag slightly and fail OPEN (a few extra slip through) — never closed. See docs/USAGE-METERING-PLAN.md."""
    cap = caller.membership.daily_call_cap
    if cap < 0 or demo_sandbox.is_sandbox(caller.org):
        return
    used = await count_today(db, caller.org_id, caller.email)
    if used >= cap:
        raise HTTPException(status_code=429, detail=(
            f"daily usage limit reached ({used}/{cap}) — ask an admin to raise your cap"))


async def _used_today_by_user(db: AsyncSession, org_id: int) -> dict[str, int]:
    """{user_email: events today} for every member of the org — one grouped COUNT per table, so the
    members list gets everyone's usage without an N+1 fan-out. Spans all kinds (calls + local + server)."""
    since = _day_start_utc()
    counts: dict[str, int] = {}
    for email, n in (await db.execute(select(CallRecord.user_email, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since).group_by(CallRecord.user_email))).all():
        counts[email] = counts.get(email, 0) + n
    for email, n in (await db.execute(select(RunRecord.user_email, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.user_email))).all():
        counts[email] = counts.get(email, 0) + n
    return counts


async def _usage_rollup(db: AsyncSession, org_id: int, since: datetime) -> dict:
    """Aggregate usage since `since` into by-user (with a per-kind split), by-tool, by-day, and totals.
    CallRecord carries `kind` ("call"/"local_run"); every RunRecord is a "server_run". Pure GROUP BY —
    no request/response bodies are read (we don't store them). See docs/USAGE-METERING-PLAN.md."""
    KINDS = ("call", "local_run", "server_run")
    totals = {k: 0 for k in KINDS}
    users: dict[str, dict] = {}

    def _bump(email: str, kind: str, n: int) -> None:
        u = users.setdefault(email, {"user_email": email, **{k: 0 for k in KINDS}})
        u[kind] += n
        totals[kind] += n

    for email, kind, n in (await db.execute(select(CallRecord.user_email, CallRecord.kind, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since
    ).group_by(CallRecord.user_email, CallRecord.kind))).all():
        _bump(email, kind if kind in KINDS else "call", n)  # guard an unexpected kind into "call"
    for email, n in (await db.execute(select(RunRecord.user_email, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.user_email))).all():
        _bump(email, "server_run", n)

    by_user = sorted(
        ({**u, "total": sum(u[k] for k in KINDS)} for u in users.values()),
        key=lambda r: -r["total"])
    totals["total"] = sum(totals[k] for k in KINDS)

    tools: dict[str, int] = {}
    for name, n in (await db.execute(select(CallRecord.tool_name, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since).group_by(CallRecord.tool_name))).all():
        tools[name] = tools.get(name, 0) + n
    for name, n in (await db.execute(select(RunRecord.bundle_name, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.bundle_name))).all():
        tools[name] = tools.get(name, 0) + n
    by_tool = sorted(({"name": k, "total": v} for k, v in tools.items()), key=lambda r: -r["total"])

    days: dict[str, int] = {}  # func.date() → 'YYYY-MM-DD' on sqlite, a date on Postgres; str() both
    for tbl in (CallRecord, RunRecord):
        for d, n in (await db.execute(select(func.date(tbl.created_at), func.count()).where(
                tbl.org_id == org_id, tbl.created_at >= since).group_by(func.date(tbl.created_at)))).all():
            days[str(d)] = days.get(str(d), 0) + n
    by_day = sorted(({"day": k, "total": v} for k, v in days.items()), key=lambda r: r["day"])

    return {"totals": totals, "by_user": by_user, "by_tool": by_tool, "by_day": by_day}


@app.post("/demo/sandbox")
async def demo_sandbox_mint(request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """Mint a login-free, short-lived sandbox TEAM for the landing-page studio: a throwaway org + a
    starter secret + a starter endpoint + a member token, returned so the browser (and the visitor's
    terminal) can register more, call them, and export a skill — all with no account. Sandbox calls
    never touch the network (see call_tool → sandbox.synthesize); rate-limited per IP; GC'd after the
    TTL. No auth — this is the anonymous front door."""
    await ratestore.sweep(db, SANDBOX_HIT_NS)  # evict cold IP keys so the namespace can't grow unbounded
    if not await ratestore.rate_check(db, SANDBOX_HIT_NS,
                                      [(_client_ip(request), SANDBOX_RATE_MAX)], SANDBOX_RATE_WINDOW_S):
        await db.commit()  # persist the sweep even on reject
        raise HTTPException(status_code=429, detail="too many demo sandboxes from here — try again later")
    await db.commit()  # persist the recorded hit before minting
    await demo_sandbox.gc(db)  # opportunistic reap of expired sandboxes
    out = await demo_sandbox.mint(db)
    out["live"] = bool(get_settings().demo_stripe_key)  # is the seeded stripe tool a real wire?
    return out


@app.get("/demo/sandbox/live")
async def demo_sandbox_live(caller: Caller = Depends(require_member)) -> dict:
    """Live-wire facts for an EXISTING sandbox (the browser reuses one via localStorage, so it may
    predate the mint response carrying them): is the wire on, and who am I in the feed."""
    if not demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=400, detail="live-wire info is for the landing-page sandbox only")
    return {"live": bool(get_settings().demo_stripe_key),
            "visitor": demo_sandbox.visitor_name(caller.org.slug)}


# ---- landing-page live payments feed (the public Stripe demo — see pubfeed.py) --------------
@app.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """Stripe → treg: a signed event from the demo sandbox account. Only `charge.succeeded` feeds
    the landing ticker; everything else is acknowledged and dropped. 404 when unconfigured, so a
    deploy without the secret exposes no unauthenticated POST surface."""
    secret = get_settings().demo_stripe_webhook_secret
    if not secret:
        raise HTTPException(status_code=404)
    payload = await request.body()
    if not pubfeed.verify_signature(payload, request.headers.get("stripe-signature", ""), secret):
        raise HTTPException(status_code=400, detail="bad signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad payload")
    if event.get("type") == "charge.succeeded":
        pubfeed.push_charge(event.get("data", {}).get("object", {}) or {})
    return {"received": True}


@app.get("/landing/stripe-feed", include_in_schema=False)
async def landing_stripe_feed() -> StreamingResponse:
    """SSE stream for the landing demo pane: recent charges, then live ones. Unauthenticated by
    design — it carries only server-chosen fields (amount/currency/created/id-suffix)."""
    return StreamingResponse(pubfeed.stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # tell the reverse proxy not to buffer the stream
    })


@app.get("/demo/sandbox/skill")
async def demo_sandbox_skill(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Export whatever the visitor built in their sandbox as a shareable **skill** (treg.json manifest
    + SKILL.md + install commands). Sandbox-only — the payoff that shows what skills are."""
    if not demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=400, detail="skill export is for the landing-page sandbox only")
    return await demo_sandbox.export_skill(db, caller.org)


@app.get("/skills/samples")
async def skill_samples() -> list[dict]:
    """The hosted sample skills the landing offers — each with its files (SKILL.md/treg.json/.secret)
    and the prompt to try. Public: the landing renders these as file packages."""
    base = get_settings().public_url.rstrip("/")
    return [{"name": n, "label": s["label"], "key": s["key"], "prompt": s["prompt"],
             "files": demo_sandbox.skill_files(n, base, None)}
            for n, s in demo_sandbox.SAMPLE_SKILLS.items()]


@app.get("/skills/{name}/install.sh", include_in_schema=False)
async def skill_install(name: str, token: str = ""):
    """`curl -fsSL {BASE}/skills/<name>/install.sh?token=<t> | sh` — writes the skill into
    ./.claude/skills/<name>/ so Claude Code loads it. The token (if given) is baked into the
    recipe's calls; without it the recipe reads the token from `treg login`."""
    if name not in demo_sandbox.SAMPLE_SKILLS:
        raise HTTPException(status_code=404, detail=f"unknown skill {name!r}")
    # The token is interpolated into a shell script the visitor runs (`curl … | sh`). Restrict it to a
    # real token charset so a crafted value can't inject a newline + commands into the generated script.
    if token and not re.fullmatch(r"[A-Za-z0-9_\-]{1,200}", token):
        raise HTTPException(status_code=422, detail="invalid token")
    base = get_settings().public_url.rstrip("/")
    script = demo_sandbox.install_script(name, base, token or None)
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


class TeammateIn(BaseModel):
    email: str


@app.post("/onboard/seed-tool")
async def onboard_seed_tool(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Pre-seed the working `echo` tool into the caller's active team so the no-key call in the
    dashboard onboarding just works (the user builds the team + invites by hand; the tool is on us)."""
    _require_can_register(caller)
    org = await db.get(Org, caller.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    return await demo_seed.seed_tool(db, org, caller.email)


@app.post("/onboard/accept-teammate")
async def onboard_accept_teammate(
    body: TeammateIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Auto-accept the fake teammate the user just invited during onboarding, so it lands in the
    roster instantly (they feel the invite, then see the loop close). Admin+ only, demo email only."""
    _require_admin_of(caller.org_id, caller)
    email = _norm_email(body.email)
    if not email.endswith("@" + demo_seed.DEMO_DOMAIN):
        raise HTTPException(status_code=400, detail="onboarding auto-accept is for demo teammates only")
    inv = (await db.execute(select(Invite).where(
        Invite.org_id == caller.org_id, Invite.email == email, Invite.status == "pending"))).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="no pending invite for that email")
    return await demo_seed.accept_demo_invite(db, caller.org_id, inv)


@app.post("/invites/{invite_id}/accept")
async def accept_my_invite(
    invite_id: int, user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Accept an invite addressed to my already-proven email — no code needed (the identity token
    proves the email). The code path (`POST /invites/accept`) stays for out-of-band joins."""
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invalid or already-used invite")
    if invite.email != user.email:
        raise HTTPException(status_code=403, detail="this invite is for a different email")
    if invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive():
        raise HTTPException(status_code=410, detail="invite expired")
    org = await db.get(Org, invite.org_id)
    if org is not None and org.suspended:  # don't let anyone join a platform-locked org
        raise HTTPException(status_code=403, detail="org suspended")
    existing = (
        await db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == invite.org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this org")
    token = crypto.new_token()  # return the org-scoped token (was minted-then-discarded → an unusable membership)
    db.add(Membership(
        user_id=user.id, org_id=invite.org_id, role=invite.role, token_hash=crypto.hash_token(token),
        tool_access=invite.tool_access, local_run_enabled=invite.local_run_enabled,
    ))
    invite.status = "accepted"
    try:
        await db.commit()  # a concurrent double-accept trips uq_membership_user_org — 409, not 500
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already a member of this org")
    org = await db.get(Org, invite.org_id)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": invite.role, "token": token}


@app.get("/orgs/{org_id}/invites")
async def list_invites(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    await health.gc_expired_invites(db, org_id)  # purge dead codes so the list shows only live ones
    await db.commit()
    rows = (
        await db.execute(select(Invite).where(Invite.org_id == org_id, Invite.status == "pending"))
    ).scalars().all()
    return [
        {
            "id": i.id, "email": i.email, "role": i.role, "invited_by": i.invited_by,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "created_at": i.created_at.isoformat(),
        }
        for i in rows
    ]


@app.delete("/orgs/{org_id}/invites/{invite_id}")
async def revoke_invite(
    org_id: int, invite_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_admin_of(org_id, caller)
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.org_id != org_id or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invite not found")  # can't "revoke" an accepted/consumed one
    await db.delete(invite)  # the code can no longer be accepted
    await db.commit()
    return {"revoked_invite": invite_id}


@app.get("/orgs/{org_id}/members")
async def list_members(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    memberships = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    users = {  # batch the user lookup (was one db.get per member)
        u.id: u for u in (await db.execute(
            select(User).where(User.id.in_([m.user_id for m in memberships]))
        )).scalars().all()
    }
    used = await _used_today_by_user(db, org_id)  # one grouped query, not N+1
    out: list[dict] = []
    for m in memberships:
        user = users.get(m.user_id)
        if user is not None:
            out.append({"user_id": user.id, "email": user.email, "role": m.role,
                        "daily_call_cap": m.daily_call_cap, "used_today": used.get(user.email, 0),
                        "tool_access": m.tool_access, "local_run_enabled": m.local_run_enabled})
    return out


@app.get("/orgs/{org_id}/usage")
async def org_usage(
    org_id: int, days: int = 30,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Usage rollups for an org over the last `days` (admin/owner): by user (with a call/local/server
    split), by tool, by day, and totals — counts only, no request/response bodies. Powers the dashboard
    Usage view."""
    _require_admin_of(org_id, caller)
    days = max(1, min(days, 365))
    since = _day_start_utc() - timedelta(days=days - 1)  # inclusive of today + the prior days-1
    return {"days": days, "since": since.isoformat(), **await _usage_rollup(db, org_id, since)}


@app.patch("/orgs/{org_id}/members/{user_id}/cap")
async def set_member_cap(
    org_id: int, user_id: int, body: CapIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set a member's per-user daily usage cap (admin/owner). `-1` = unlimited; any other negative is
    rejected. Separate from role (owner-only) — capping is a management action, not a privilege change."""
    _require_admin_of(org_id, caller)
    if body.daily_call_cap < -1:
        raise HTTPException(status_code=422, detail="daily_call_cap must be -1 (unlimited) or >= 0")
    membership = (await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    membership.daily_call_cap = body.daily_call_cap
    await db.commit()
    return {"user_id": user_id, "org_id": org_id, "daily_call_cap": body.daily_call_cap}


async def _known_tool_names(org_id: int, db: AsyncSession) -> set[str]:
    rows = (await db.execute(select(Tool.name).where(Tool.org_id == org_id))).all()
    return {r[0] for r in rows}


async def _known_access_names(org_id: int, db: AsyncSession) -> set[str]:
    """Everything an access list may name: tool names (the call/run gate) plus bundle names (the
    skill-visibility gate) — so a recipe-only skill can be granted even though it has no tool."""
    bundles = (await db.execute(select(Bundle.name).where(Bundle.org_id == org_id))).all()
    return await _known_tool_names(org_id, db) | {r[0] for r in bundles}


def _normalize_tool_access(names: list[str] | None, known: set[str]) -> list[str] | None:
    """Validate a requested access list against the org's tools + skills. None → None (all). A list
    must name only real tools/skills (else 422). A list covering EVERYTHING collapses to None."""
    if names is None:
        return None
    unknown = [t for t in names if t not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown tool/skill(s): {', '.join(sorted(set(unknown)))}")
    chosen = set(names)
    return None if chosen >= known and known else sorted(chosen)  # everything checked → 'all' (NULL)


@app.patch("/orgs/{org_id}/members/{user_id}/access")
async def set_member_access(
    org_id: int, user_id: int, body: AccessIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set which tools a member may call/run (`tool_access`: None = all, else the allowed names) and
    whether they may run locally (`local_run_enabled`). Admin/owner only; an owner can't be restricted."""
    _require_admin_of(org_id, caller)
    membership = (await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner":
        raise HTTPException(status_code=403, detail="an owner always has full access; it can't be restricted")
    membership.tool_access = _normalize_tool_access(body.tool_access, await _known_access_names(org_id, db))
    membership.local_run_enabled = body.local_run_enabled
    await db.commit()
    return {"user_id": user_id, "org_id": org_id, "tool_access": membership.tool_access,
            "local_run_enabled": membership.local_run_enabled}


@app.get("/usage/me")
async def my_usage(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's own usage today + cap for the active org — so a member sees 'used / cap' without
    admin access. `cap` is -1 when unlimited."""
    return {"org": caller.org.slug, "used_today": await count_today(db, caller.org_id, caller.email),
            "cap": caller.membership.daily_call_cap}


@app.delete("/orgs/{org_id}/members/{user_id}")
async def remove_member(
    org_id: int, user_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_admin_of(org_id, caller)
    membership = (
        await db.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner":  # only an owner manages owners; an admin cannot remove one
        raise HTTPException(status_code=403, detail="owners cannot be removed")
    await db.delete(membership)  # revokes that user's token for this org
    await db.commit()
    return {"removed": user_id}


@app.patch("/orgs/{org_id}/members/{user_id}")
async def set_member_role(
    org_id: int, user_id: int, body: RoleIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_owner_of(org_id, caller)  # only an owner changes roles (incl. transferring ownership)
    if body.role not in ROLE_RANK:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ROLE_RANK)}")
    membership = (
        await db.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner" and body.role != "owner" and await _count_owners(org_id, db) <= 1:
        raise HTTPException(status_code=409, detail="cannot demote the last owner — promote another owner first")
    membership.role = body.role
    await db.commit()
    return {"user_id": user_id, "role": body.role, "org_id": org_id}


@app.post("/orgs/{org_id}/leave")
async def leave_org(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    if caller.org_id != org_id:  # token is org-scoped: you leave the org whose token you present
        raise HTTPException(status_code=403, detail="use this org's token to leave it")
    if caller.role == "owner" and await _count_owners(org_id, db) <= 1:
        raise HTTPException(status_code=409, detail="you are the last owner — transfer ownership or delete the org")
    await db.delete(caller.membership)  # revokes the caller's token for this org
    await db.commit()
    return {"left_org": org_id}


@app.delete("/orgs/{org_id}")
async def delete_org(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_owner_of(org_id, caller)
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    await _cascade_delete_org(org, db)
    await db.commit()
    return {"deleted_org": org_id}


# ---- public demo token: a publishable, call-only credential for this org -------------------
PUBLIC_DEMO_DOMAIN = "public-demo.treg.local"  # unroutable — the public identity can never log in


def _public_demo_email(org: Org) -> str:
    return f"pub-{org.slug}@{PUBLIC_DEMO_DOMAIN}"


@app.post("/orgs/{org_id}/public-token")
async def create_public_token(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Mint (or ROTATE) the org's publishable token: flips the org to `public_demo` and returns a
    viewer-role token bound to a dedicated can't-log-in identity. Safe to print on a web page:
    the lockdown in require_member/require_identity limits it to /call + reads, /call is per-IP
    rate-limited, and calling this endpoint again replaces the token (instant revocation of the
    old one). Owner-only — publishing a credential is an org-level decision."""
    _require_owner_of(org_id, caller)
    org = caller.org
    email = _public_demo_email(org)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, demo=True)  # demo: excluded from stats; the domain can't receive mail
        db.add(user)
        await db.flush()
    token = crypto.new_token()
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
    if membership is None:
        db.add(Membership(user_id=user.id, org_id=org_id, role="viewer", token_hash=crypto.hash_token(token)))
    else:
        membership.token_hash = crypto.hash_token(token)  # rotate: the previous published token dies here
    org.public_demo = True
    await db.commit()
    return {"token": token, "org": org.slug, "role": "viewer", "email": email,
            "rate_limit": f"{PUBLIC_DEMO_RATE_MAX} calls per {PUBLIC_DEMO_RATE_WINDOW_S}s per IP",
            "note": "this token can only call this org's tools and read — safe to publish; POST again to rotate"}


@app.delete("/orgs/{org_id}/public-token")
async def delete_public_token(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Revoke the publishable token and lift the org's public_demo lockdown."""
    _require_owner_of(org_id, caller)
    org = caller.org
    user = (await db.execute(select(User).where(User.email == _public_demo_email(org)))).scalar_one_or_none()
    if user is not None:
        membership = (await db.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
        if membership is not None:
            await db.delete(membership)
    org.public_demo = False
    await db.commit()
    return {"public_token_revoked": True, "org": org.slug}


# ---- secrets (values are write-only — never returned) -------------------------------------
@app.post("/secrets")
async def create_secret(
    body: SecretIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    await _enforce_sandbox_cap(caller, Secret, demo_sandbox.MAX_SECRETS, "secrets", db)
    await _validate_bundle_id(body.bundle_id, caller.org_id, db)
    secret = Secret(
        org_id=caller.org_id, name=body.name, owner=caller.email, kind=body.kind,
        value=crypto.encrypt(body.value), bundle_id=body.bundle_id,
    )
    db.add(secret)
    await db.commit()
    return _secret_view(secret)


@app.get("/secrets")
async def list_secrets(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()
    visible = await _visible_secret_ids(caller, db)
    if visible is not None:  # tool-restricted member: only the keys wired into their allowed tools
        rows = [s for s in rows if s.id in visible]
    return [_secret_view(s) for s in rows]


@app.patch("/secrets/{secret_id}")
async def update_secret(
    secret_id: int,
    body: SecretUpdate,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="secret not found")
    if not _can_manage(caller, secret):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this secret")
    _require_not_live_demo_secret(caller, secret)
    fields = body.model_dump(exclude_unset=True)
    for k in ("name", "value", "kind"):  # these map to NOT-NULL columns; explicit null is a 422, not a 500
        if k in fields and fields[k] is None:
            raise HTTPException(status_code=422, detail=f"{k} cannot be null")
    # A kind change drives refresh + health + extraction shape; validate a JSON-kind actually has a
    # JSON value (else the tool silently 502s later) and reset the now-meaningless health verdict.
    if "kind" in fields and fields["kind"] != secret.kind:
        if fields["kind"] in ("oauth", "secret_file"):
            raw = fields["value"] if "value" in fields else crypto.decrypt(secret.value)
            try:
                json.loads(raw)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail=f"kind {fields['kind']!r} needs a JSON value")
        secret.health_status, secret.health_detail, secret.health_checked_at = "unknown", "", None
    if "value" in fields:
        fields["value"] = crypto.encrypt(fields["value"])  # re-encrypt on rotate
        # The value is exactly what health measures — a rotation invalidates the prior verdict.
        secret.health_status, secret.health_detail, secret.health_checked_at = "unknown", "", None
    for k, v in fields.items():
        setattr(secret, k, v)
    await db.commit()
    return _secret_view(secret)


@app.delete("/secrets/{secret_id}")
async def delete_secret(
    secret_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="secret not found")
    if not _can_manage(caller, secret):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this secret")
    _require_not_live_demo_secret(caller, secret)
    # bindings live in a JSON column — scan tools IN THIS ORG (registry-scale N is small).
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    if any(b.get("secret_id") == secret_id for t in tools for b in t.bindings):
        raise HTTPException(status_code=409, detail="secret is referenced by a tool binding")
    # a secret used only by a local-run inject (not an HTTP binding) would otherwise be silently
    # deletable, breaking `treg run` — guard those references too.
    if any((e.get("secret_id") == secret_id) for t in tools for e in ((t.cli or {}).get("inject") or [])):
        raise HTTPException(status_code=409, detail="secret is referenced by a tool's local-run (cli) profile")
    await db.delete(secret)
    await db.commit()
    return {"deleted": secret_id}


def _require_not_live_demo_tool(caller: Caller, tool: Tool) -> None:
    """The sandbox's seeded live-wire tool (`stripe`, pinned base) is the demo's centerpiece —
    editing or removing it would break the visitor's own live pane, so refuse. Only the seeded
    name is frozen; visitor-created tools stay fully editable. No-op outside sandboxes / with
    the wire off."""
    if (demo_sandbox.is_sandbox(caller.org) and get_settings().demo_stripe_key
            and tool.name == "stripe" and demo_sandbox.is_live_tool(tool)):
        raise HTTPException(status_code=403, detail=(
            "the live stripe demo endpoint is part of the sandbox — add your own endpoints instead"))


def _require_not_live_demo_secret(caller: Caller, secret: Secret) -> None:
    """Companion guard for the seeded STRIPE_KEY the live tool is bound to."""
    if (demo_sandbox.is_sandbox(caller.org) and get_settings().demo_stripe_key
            and secret.name == "STRIPE_KEY"):
        raise HTTPException(status_code=403, detail=(
            "STRIPE_KEY powers the live stripe demo — add your own keys instead"))


# ---- tools --------------------------------------------------------------------------------
def _require_public_base_url(base_url: str) -> None:
    """A tool's base_url is fetched server-side by the proxy — reject internal / loopback / cloud-metadata
    targets so a member can't turn `treg call` into an SSRF (e.g. base_url=169.254.169.254). Reuses the
    same block-list the webhook path already uses. DNS names are allowed (best-effort)."""
    if not health.safe_webhook_url(base_url):
        raise HTTPException(status_code=422, detail=(
            "base_url must be a public http(s) address — loopback, private, link-local, and cloud-"
            "metadata hosts are refused"))


async def _validate_bundle_id(bundle_id: int | None, org_id: int, db: AsyncSession) -> None:
    """A resource may only attach to a bundle in its OWN org — else it'd be counted by, rendered in,
    and swept up by a foreign org's bundle view/delete (org-scoping leak)."""
    if bundle_id is None:
        return
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != org_id:
        raise HTTPException(status_code=422, detail=f"bundle_id {bundle_id} not found in this org")


async def _require_secret_ownership(secret: Secret, caller: Caller) -> None:
    """A member may bind/inject only a secret they OWN; admins/owners may use any team secret (they set
    up shared tools). Without this, a member could attach a teammate's key to a tool they control and
    exfiltrate it — via the proxy (an attacker `base_url`) or `/grant` on a local-run tool."""
    if not (secret.owner == caller.email or _role_at_least(caller.role, "admin")):
        raise HTTPException(
            status_code=403,
            detail=f"you can only bind a secret you own — secret {secret.id} belongs to another member "
                   "(ask an org admin to wire up a shared-key tool)")


async def _validate_bindings(bindings: list[dict], caller: Caller, db: AsyncSession,
                             grandfather: frozenset = frozenset()) -> None:
    org_id = caller.org_id
    for b in bindings:
        injector = b.get("injector", "env")
        if injector not in injectors.INJECTORS:  # unknown injector 500s the proxy at call time — reject now
            raise HTTPException(status_code=422, detail=f"unknown injector {injector!r}")
        fmt = b.get("format", "{secret}")  # rendered as fmt.format(secret=…) on the hot path
        if not isinstance(fmt, str):
            raise HTTPException(status_code=422, detail="binding format must be a string")
        try:
            fmt.format(secret="x")  # an unexpected placeholder / literal brace would KeyError/ValueError → 500
        except (KeyError, IndexError, ValueError):
            raise HTTPException(status_code=422, detail=f"invalid binding format {fmt!r} — use only {{secret}}")
        # name/secret_field, if present, feed httpx header/param setters and the JSON extractor —
        # a null or non-string there AttributeErrors on the hot path; location must be header|query.
        for key in ("name", "secret_field"):
            if key in b and not (isinstance(b[key], str) and b[key]):
                raise HTTPException(status_code=422, detail=f"binding {key} must be a non-empty string")
        loc = b.get("location", "header")
        if loc not in ("header", "query"):
            raise HTTPException(status_code=422, detail="binding location must be 'header' or 'query'")
        sid = b.get("secret_id")
        secret = await db.get(Secret, sid) if sid is not None else None
        if secret is None or secret.org_id != org_id:
            raise HTTPException(status_code=422, detail=f"binding secret_id {sid} not found")
        if sid not in grandfather:  # a binding already on the tool is grandfathered (don't lock the owner out on edit)
            await _require_secret_ownership(secret, caller)  # can't ADD a teammate's secret
    # Two bindings with the same target name silently overwrite each other at call time (the first
    # credential is dropped) — reject the collision at registration, for BOTH query and header
    # (header names are case-insensitive; `httpx.Headers[name]=…` overwrites just like a query param).
    qnames = [b.get("name", "Authorization") for b in bindings if b.get("location", "header") == "query"]
    qdupes = sorted({n for n in qnames if qnames.count(n) > 1})
    if qdupes:
        raise HTTPException(status_code=422, detail=f"duplicate query binding name(s): {qdupes}")
    hnames = [b.get("name", "Authorization").lower() for b in bindings if b.get("location", "header") == "header"]
    hdupes = sorted({n for n in hnames if hnames.count(n) > 1})
    if hdupes:
        raise HTTPException(status_code=422, detail=f"duplicate header binding name(s): {hdupes}")


def _validate_cli_profile(cli: dict | None) -> None:
    """422 (not a write-through) for a malformed local-run profile — a bad deny regex or inject shape
    must fail HERE, never at grant time (localrun.check_deny skips uncompilable legacy patterns)."""
    if cli is None:
        return
    try:
        localrun.validate_cli_profile(cli)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _validate_cli_secrets(cli: dict | None, caller: Caller, db: AsyncSession,
                                grandfather: frozenset = frozenset()) -> None:
    """Ownership check for secrets a cli.inject entry names by secret_id — same rule as bindings, so a
    member can't launder a teammate's secret into a local-run tool and extract it via /grant."""
    if not cli:
        return
    for e in cli.get("inject") or []:
        sid = e.get("secret_id")
        if sid is None:
            continue
        secret = await db.get(Secret, sid)
        if secret is None or secret.org_id != caller.org_id:
            raise HTTPException(status_code=422, detail=f"cli.inject secret_id {sid} not found")
        if sid not in grandfather:
            await _require_secret_ownership(secret, caller)


def _allowed_server_bins() -> set[str]:
    """The commands `treg run --server` may execute: catalog-known CLIs + an admin allow-list. Blocks a
    member naming `bash`/`python` to run arbitrary code as the server user (docs/CLI-RUN-PLAN.md Option A)."""
    from . import providers as prov
    bins = {(e.get("cli") or {}).get("bin") for e in prov.CATALOG}
    bins.discard(None)
    extra = get_settings().run_allowed_bins
    bins |= {b.strip() for b in extra.split(",") if b.strip()}
    return bins  # type: ignore[return-value]


@app.post("/tools")
async def create_tool(
    body: ToolIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    await _enforce_sandbox_cap(caller, Tool, demo_sandbox.MAX_TOOLS, "endpoints", db)
    if body.bindings is not None:
        bindings = body.bindings
    elif body.secret_id is not None:
        bindings = [_flat_binding(body)]
    else:
        bindings = []  # a public upstream needing no credential is allowed
    _require_public_base_url(body.base_url)  # no SSRF to internal/metadata hosts via the proxy
    await _validate_bindings(bindings, caller, db)
    await _validate_bundle_id(body.bundle_id, caller.org_id, db)
    _validate_cli_profile(body.cli)
    await _validate_cli_secrets(body.cli, caller, db)
    tool = Tool(
        org_id=caller.org_id, name=body.name, owner=caller.email, base_url=body.base_url,
        host=_host_of(body.base_url), bindings=bindings, health_check=body.health_check,
        examples=body.examples or [], cli=body.cli, bundle_id=body.bundle_id,
    )
    db.add(tool)
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"tool name {body.name!r} already exists in this org")
    return _tool_view(tool)


@app.get("/tools")
async def list_tools(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    # The per-member tool ACL hides what it gates: a restricted member's listing shows only their tools.
    return [_tool_view(t) for t in rows if _tool_allowed(caller, t.name)]


@app.get("/tools/by-name/{name}")
async def get_tool_by_name(
    name: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Name-keyed lookup so shareable detail URLs (/app/tools/<name>) resolve without an id."""
    tool = (await db.execute(
        select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name)
    )).scalars().first()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_access(caller, tool.name)  # a 403 names the fix (ask an admin) — clearer than a fake 404
    return _tool_view(tool)


@app.patch("/tools/{tool_id}")
async def update_tool(
    tool_id: int,
    body: ToolUpdate,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="tool not found")
    if not _can_manage(caller, tool):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this tool")
    _require_not_live_demo_tool(caller, tool)
    fields = body.model_dump(exclude_unset=True)
    if "base_url" in fields and fields["base_url"] is None:  # NOT-NULL column + feeds _host_of — 422, not 500
        raise HTTPException(status_code=422, detail="base_url cannot be null")
    if fields.get("base_url"):
        _require_public_base_url(fields["base_url"])  # no SSRF to internal/metadata hosts
    # Secrets ALREADY on the tool are grandfathered on edit — only a NEWLY-added binding/inject must be
    # owned by the caller. Otherwise re-saving a tool an admin wired with a shared key locks its owner out.
    grandfather = frozenset(
        {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}
        | {e.get("secret_id") for e in ((tool.cli or {}).get("inject") or []) if e.get("secret_id") is not None}
    )
    if "bindings" in fields:
        await _validate_bindings(fields["bindings"], caller, db, grandfather)
    if "cli" in fields:  # explicit null clears the profile (turns local runs off entirely)
        _validate_cli_profile(fields["cli"])
        await _validate_cli_secrets(fields["cli"], caller, db, grandfather)
    for k, v in fields.items():
        setattr(tool, k, v)
    if "base_url" in fields:
        tool.host = _host_of(tool.base_url)  # keep the resolution index in sync
    await db.commit()
    return _tool_view(tool)


@app.delete("/tools/{tool_id}")
async def delete_tool(
    tool_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="tool not found")
    if not _can_manage(caller, tool):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this tool")
    _require_not_live_demo_tool(caller, tool)
    await db.delete(tool)
    await db.commit()
    return {"deleted": tool_id}


# ---- local runs (`treg run`): grant + outcome report (docs/CLI-RUN-PLAN.md) -----------------
# Redact obvious credentials a user might type INLINE (`treg run x -- --token sk_live_…`) before the
# argv is persisted to the audit log — known key prefixes, any high-entropy token, JWTs, AND the value
# that follows a credential-looking flag (so a SHORT password like `--password hunter2` is masked too).
_ARGV_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_.\-]{8,}"          # JWT (base64url with dots)
    r"|\b[A-Za-z0-9_\-]{24,}\b")  # any 24+ high-entropy run — deliberately over-masks (git SHAs, UUIDs)
                                  # since in an audit log a false mask is harmless but a real key isn't
_CRED_FLAG = r"--?(?:token|password|passwd|pass|pwd|api[-_]?key|secret|auth|bearer|credential)s?"
_CRED_FLAG_EQ_RE = re.compile(rf"({_CRED_FLAG})=\S+", re.I)
_CRED_FLAG_BARE_RE = re.compile(rf"^{_CRED_FLAG}$", re.I)


def _redact_argv_list(argv: list[str]) -> list[str]:
    """Per-element redaction that also masks the element FOLLOWING a bare credential flag."""
    out: list[str] = []
    mask_next = False
    for a in argv:
        if mask_next:
            out.append("***"); mask_next = False; continue
        if _CRED_FLAG_BARE_RE.match(a):          # `--password` `hunter2` → mask the value that follows
            out.append(a); mask_next = True; continue
        a = _CRED_FLAG_EQ_RE.sub(r"\1=***", a)   # `--password=hunter2`
        out.append(_ARGV_SECRET_RE.sub("***", a))
    return out


def _redact_argv(argv: list[str]) -> str:
    return " ".join(_redact_argv_list(argv))[:500]


async def _grant_audit(db: AsyncSession, caller: Caller, tool_name: str, method: str, path: str, status: int) -> int:
    """A SYNCHRONOUS audit row (unlike record_call): the grant returns its audit id so the
    run-report can prove it follows a real grant. One insert; this is not the hot proxy path."""
    rec = CallRecord(org_id=caller.org_id, user_email=caller.email, tool_name=tool_name,
                     method=method, path=path[:500], status_code=status, kind="local_run")
    db.add(rec)
    await db.commit()
    return rec.id


@app.post("/tools/{name}/grant")
async def grant_local_run(
    name: str,
    body: GrantIn,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint the process material for ONE local run of this tool's CLI: the audited, owner-opt-in
    exception to "values are never returned". OAuth secrets release only the expiring leaf; the
    deny check happens here, where the secret lives. Unlike /call (which injects server-side and
    leaks nothing), a grant HANDS the credential value to the caller's machine — so it needs member+
    (a viewer may call but not extract). Loosening this to a per-tool run ACL is a future policy knob."""
    _require_can_register(caller)
    from . import providers as prov
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_access(caller, tool.name)  # per-member tool ACL (call + both run tiers)
    _require_local_run(caller)               # local tier may be disabled for this member (server-only)
    await _enforce_daily_cap(caller, db)  # a local run counts toward the per-user daily cap
    catalog_cli = (prov.match_skill(tool.name) or {}).get("cli")
    profile = localrun.effective_profile(tool, catalog_cli)
    if profile is None:
        raise HTTPException(status_code=409, detail=(
            f"treg doesn't know how to inject credentials into {tool.name!r}. Add a \"cli\" block to the "
            'skill\'s treg.json — template: {"cli": {"bin": "' + tool.name + '", '
            '"inject": [{"secret": "<local secret name>", "via": "env", "name": "<ENV_VAR>"}]}}'))
    if profile.get("unsupported"):
        raise HTTPException(status_code=409, detail=f"{tool.name}: {profile.get('reason', 'this CLI cannot be injected')}")
    if not profile.get("enabled"):
        raise HTTPException(status_code=403, detail=(
            f"local runs are disabled for {tool.name!r} — an owner/admin can enable them: "
            f"treg tool update {tool.name} --local-run on"))
    denied = localrun.check_deny(profile, body.argv)
    if denied:
        pattern, source = denied
        await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403)
        raise HTTPException(status_code=403, detail=(
            f"denied by {source}: pattern {pattern!r}. The skill's creator controls this list "
            "(cli.deny in treg.json)."))
    # Runner-proof gate (Bug 1). Handing a member a secret they do NOT own — a shared-key tool they may
    # RUN but not SEE — is allowed only for the isolated treg-run runner, which proves itself with a
    # value the member can't read (`X-Treg-Run-Proof`). A direct member call has no proof, so the raw
    # value never reaches the member's eyes. Owned secrets (or an admin) skip this — you can read a key
    # you already hold.
    inject_sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
    needs_proof = False
    if not _role_at_least(caller.role, "admin"):
        for sid in (s for s in inject_sids if s is not None):
            sec = await db.get(Secret, sid)
            if sec is not None and sec.owner != caller.email:
                needs_proof = True
                break
    if needs_proof:
        proof = get_settings().run_proof
        supplied = request.headers.get("X-Treg-Run-Proof", "")
        if not (proof and hmac.compare_digest(supplied, proof)):
            await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403)
            raise HTTPException(status_code=403, detail=(
                "this tool uses another member's key — running it needs the isolated treg-run runner "
                "(an admin sets it up once: `sudo treg setup-local-run --run-proof …`). A direct grant "
                "can't expose someone else's key value to you."))
    try:
        rendered = await localrun.render_grant(tool, profile, db, request.app.state.http)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — a failed oauth refresh must read clearly, like /call
        raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
    audit_id = await _grant_audit(db, caller, tool.name, "GRANT", _redact_argv(body.argv), 200)
    warnings = list(profile.get("warnings") or [])
    ttl = rendered["ttl_seconds"]
    if ttl is not None and ttl <= 0:
        warnings.append("the injected token appears already expired — the run will likely fail; "
                        "an owner may need to reconnect it (treg oauth connect)")
    elif ttl is not None:
        warnings.append(f"the injected token expires in ~{max(1, ttl // 60)} min — "
                        "long-running commands may outlive it")
    return {
        "bin": profile.get("bin", tool.name),
        "inject": rendered["items"],  # delivery-tagged items — the client applies each (env/argv/broker)
        "ttl_seconds": rendered["ttl_seconds"],
        "install": profile.get("install"),
        "noninteractive": profile.get("noninteractive") or [],
        "warnings": warnings,
        "errors": profile.get("errors") or [],
        # Scrub the injected value from the CLI's output when the member doesn't OWN the key (a shared
        # key run through the isolated runner) — so a CLI feature (`gh auth token`, an env dump) can't be
        # used to print it back. Owned/admin runs skip it (you may see your own key) and keep a raw TTY.
        "redact_output": needs_proof,
        "audit_id": audit_id,
    }


@app.post("/tools/{name}/run-report")
async def report_local_run(
    name: str,
    body: RunReportIn,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The client's post-run verdict (it matches stderr against the grant's error patterns LOCALLY
    and sends only this enum — raw output never leaves the machine). credential_invalid flips the
    granted secrets to invalid via the same health fields the runner uses."""
    _require_can_register(caller)  # marking a credential invalid is a register-tier action, not a read
    if body.verdict not in localrun.VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {localrun.VERDICTS}")
    grant_rec = await db.get(CallRecord, body.audit_id)
    # Bind the report to the SAME user who received the grant — otherwise a member could invalidate
    # another user's secrets (a DoS) by guessing a sequential audit_id.
    if (grant_rec is None or grant_rec.org_id != caller.org_id or grant_rec.method != "GRANT"
            or grant_rec.tool_name != name or grant_rec.user_email != caller.email):
        raise HTTPException(status_code=404, detail="no matching grant for that audit_id")
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    marked: list[str] = []
    if body.verdict == "credential_invalid":
        from . import providers as prov
        profile = localrun.effective_profile(tool, (prov.match_skill(tool.name) or {}).get("cli")) or {}
        # Mark only the credentials this run actually INJECTED (the ones the CLI used) — not every HTTP
        # binding — and never a `param` (it's config, not a credential; mirrors health.run_all's guard).
        sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
        now = datetime.now(timezone.utc)
        for sid in [s for s in sids if s is not None]:
            secret = await db.get(Secret, sid)
            if secret is not None and secret.org_id == caller.org_id and secret.kind != "param":
                secret.health_status = "invalid"
                secret.health_detail = f"local run of {tool.name} reported an auth failure (exit {body.exit_code})"
                secret.health_checked_at = now
                marked.append(secret.name)
    await _grant_audit(db, caller, tool.name, "REPORT", f"exit={body.exit_code} verdict={body.verdict}", 200)
    return {"ok": True, "marked_invalid": marked}


# ---- skills (bundle composer): register a whole skill atomically --------------------------
@app.post("/skills")
async def register_skill(
    body: SkillIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Register a skill from a raw payload (recipe + secrets + tools). The dashboard's folder importer
    and the CLI build this same payload; the shared core is `_register_skill_bundle`."""
    return await _register_skill_bundle(body, caller, db)


_SECRET_DIR_RE = re.compile(r"(^|/)\.secrets?(/|$)")


def _sanitize_bundle_files(files: dict) -> dict:
    """Defense-in-depth before persisting companion files (the CLI/dashboard already exclude these):
    drop path-traversal / absolute paths, SKILL.md (that's `recipe`), and anything under a secret dir —
    a secret must NEVER live in the shipped file blob. `skill install` re-checks on the way out too."""
    clean: dict[str, str] = {}
    for raw, content in (files or {}).items():
        p = str(raw).replace("\\", "/")
        if not p or p.startswith("/") or ".." in p.split("/"):   # absolute or traversal → drop
            continue
        if p == "SKILL.md" or _SECRET_DIR_RE.search(p):
            continue
        if not isinstance(content, str):
            continue
        clean[p] = content
    return clean


async def _register_skill_bundle(body: SkillIn, caller: Caller, db: AsyncSession) -> dict:
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):  # a skill import would create unlimited tools/secrets, past the cap
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    names = [s.local_name for s in body.secrets]  # bindings reference secrets by local_name
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:  # a duplicate would silently orphan the first secret (only the last id is kept)
        raise HTTPException(status_code=422, detail=f"duplicate secret local_name(s): {dupes}")
    files = _sanitize_bundle_files(body.files)  # drop unsafe paths / secrets before persisting
    bundle = Bundle(org_id=caller.org_id, name=body.name, owner=caller.email, recipe=body.recipe, files=files)
    db.add(bundle)
    await db.flush()  # assign bundle.id without committing yet

    local_to_id: dict[str, int] = {}
    for s in body.secrets:
        secret = Secret(
            org_id=caller.org_id, name=s.local_name, owner=caller.email, kind=s.kind,
            value=crypto.encrypt(s.value), bundle_id=bundle.id,
        )
        db.add(secret)
        await db.flush()
        local_to_id[s.local_name] = secret.id

    for t in body.tools:
        _require_public_base_url(t.base_url)  # no SSRF to internal/metadata hosts via an imported skill
        resolved: list[dict] = []
        for raw in t.bindings:
            b = dict(raw)
            local = b.pop("secret", None)  # bindings reference secrets by local_name
            if local is not None:
                if local not in local_to_id:
                    raise HTTPException(status_code=422, detail=f"binding references unknown secret {local!r}")
                b["secret_id"] = local_to_id[local]
            resolved.append(b)
        # Same gate as POST /tools: reject unknown injectors / dangling secret_ids here, or the
        # skill door persists a poison tool (missing secret_id → KeyError → 500 on every call).
        await _validate_bindings(resolved, caller, db)
        cli = dict(t.cli) if t.cli else None
        if cli:  # inject entries reference secrets by local_name too — resolve like bindings
            cli["inject"] = [dict(e) for e in cli.get("inject") or []]
            for e in cli["inject"]:
                local = e.pop("secret", None)
                if local is not None:
                    if local not in local_to_id:
                        raise HTTPException(status_code=422, detail=f"cli.inject references unknown secret {local!r}")
                    e["secret_id"] = local_to_id[local]
            _validate_cli_profile(cli)
            await _validate_cli_secrets(cli, caller, db)  # a raw secret_id in the upload must be owned too
        db.add(Tool(
            org_id=caller.org_id, name=t.name, owner=caller.email, base_url=t.base_url,
            host=_host_of(t.base_url), bindings=resolved, health_check=t.health_check,
            examples=t.examples, cli=cli, bundle_id=bundle.id,
        ))

    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="a tool name in this skill already exists in this org")
    return await _bundle_view(bundle.id, db)


# ---- skills: analyze / import an uploaded folder (the dashboard mirror of `treg upload skills`) ----
_SKILL_UPLOAD_MAX_FILES = 600
_SKILL_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # per file
_SKILL_UPLOAD_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # whole upload — cap BEFORE materializing to disk


def _check_upload_size(files: list) -> None:
    """Reject an oversized folder upload early (before writing anything to disk), so a member can't
    exhaust the server with a huge `/skills/analyze|import` body. Per-file cap still applies later."""
    if len(files) > _SKILL_UPLOAD_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"too many files (max {_SKILL_UPLOAD_MAX_FILES})")
    total = sum(len((getattr(f, "content", "") or "").encode("utf-8", "ignore")) for f in files)
    if total > _SKILL_UPLOAD_MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="upload too large (max 20 MB total)")


def _materialize_skill_files(files: list) -> str:
    """Write uploaded skill files into a fresh temp dir so the SAME disk-based scanner the CLI uses
    (skills.scan_skills / _classify) can run on them unchanged. Paths are sanitized against traversal;
    the caller must rmtree the returned dir."""
    root = Path(tempfile.mkdtemp(prefix="treg-skill-")).resolve()
    for f in files[:_SKILL_UPLOAD_MAX_FILES]:
        rel = f.path.replace("\\", "/").lstrip("/")
        dest = (root / rel).resolve()
        if root not in dest.parents:      # a '..' path escaping the temp root — drop it
            continue
        if len(f.content.encode("utf-8", "ignore")) > _SKILL_UPLOAD_MAX_BYTES:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_text(f.content)
        except OSError:
            continue
    return str(root)


def _scan_uploaded_skills(root: str, catalog: list, env_names: set) -> list:
    """Find every skill dir (a dir with a SKILL.md) at any depth under root and classify each with the
    CLI's own `skills._classify` — so the dashboard verdict is identical to `treg upload skills`."""
    from . import skills as sk
    dets = []
    for dirpath, _dirs, filenames in os.walk(root):
        if any(m in filenames for m in ("SKILL.md", "skill.md")):
            dets.append(sk._classify(Path(dirpath), catalog, env_names))
    dets.sort(key=lambda d: d.name)
    return dets


@app.post("/skills/analyze")
async def analyze_skill_folder(
    body: SkillAnalyzeIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Classify an uploaded skill folder WITHOUT registering — the dashboard's verify step. Same
    classifier as `treg upload skills`: recipe-only vs contract vs generated, plus readiness gaps."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    _check_upload_size(body.files)
    from . import providers as prov, convert, skills as sk_mod
    root = _materialize_skill_files(body.files)
    try:
        env_path = Path(root) / ".env"
        env_names = set(prov.var_names(str(env_path))) if env_path.is_file() else set()
        dets = _scan_uploaded_skills(root, prov.CATALOG, env_names)
        existing = {b.name for b in (await db.execute(
            select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()}
        out = []
        for d in dets:
            secs = []
            for s in d.secrets:
                if s.get("file"):
                    secs.append({"name": s["name"], "source": "file", "ref": s["file"],
                                 "present": (Path(d.path) / s["file"]).is_file()})
                elif s.get("env"):
                    secs.append({"name": s["name"], "source": "env", "ref": s["env"],
                                 "present": s["env"] in env_names})
            out.append({"name": d.name, "kind": d.kind, "base_url": d.base_url,
                        "secrets": secs, "gaps": d.gaps, "ready": d.ready,
                        "already": d.name in existing,
                        "cli": sk_mod.cli_preview(d, prov.CATALOG),
                        "recipe_chars": len(convert._read_recipe(Path(d.path)))})
        return {"skills": out}
    finally:
        shutil.rmtree(root, ignore_errors=True)


@app.post("/skills/import")
async def import_skill_folder(
    body: SkillImportIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Register selected skills from an uploaded folder: scan → build the payload (secret VALUES from
    the uploaded files / provided env values) → register each as a bundle. Mirrors `treg upload skills`."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    _check_upload_size(body.files)
    from . import skills as sk, providers as prov
    root = _materialize_skill_files(body.files)
    try:
        env_path = Path(root) / ".env"
        env_names = set(prov.var_names(str(env_path))) if env_path.is_file() else set()
        env_names |= set(body.env_values or {})  # a value the user typed in the dashboard counts as present
        dets = _scan_uploaded_skills(root, prov.CATALOG, env_names)
        want = set(body.select) if body.select else {d.name for d in dets if d.ready}
        chosen = [d for d in dets if d.name in want]
        values: dict[str, str] = {}
        need = sk.env_needs(chosen)
        if need and env_path.is_file():
            values.update(prov.env_values(str(env_path), need))
        values.update(body.env_values or {})
        # Idempotent + crash-proof (like the CLI): skip anything already registered, and never let one
        # skill 500 the whole batch. A name clash on the bundle/tool/secret would otherwise raise an
        # IntegrityError on flush (not on commit, so it escaped the register helper's guard).
        existing_bundles = {b.name for b in (await db.execute(
            select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()}
        existing_tools = {t.name for t in (await db.execute(
            select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()}
        existing_secrets = {s.name for s in (await db.execute(
            select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()}
        from .db import session_maker
        results = []
        for d in chosen:
            if d.gaps:
                results.append({"name": d.name, "ok": False, "error": "; ".join(d.gaps)}); continue
            secret_names = {s["name"] for s in d.secrets}
            if d.name in existing_bundles or d.name in existing_tools or (secret_names & existing_secrets):
                results.append({"name": d.name, "ok": False, "skipped": True, "error": "already registered"}); continue
            try:
                payload = sk.build_payload(d, values)
                # Each skill registers in its OWN session so a failure (bad binding, IntegrityError…)
                # can't poison the shared session for the rest of the batch (greenlet_spawn errors).
                async with session_maker() as sk_db:
                    await _register_skill_bundle(SkillIn(**payload), caller, sk_db)
                existing_bundles.add(d.name); existing_tools.add(d.name); existing_secrets |= secret_names
                results.append({"name": d.name, "ok": True, "kind": d.kind})
            except HTTPException as exc:
                results.append({"name": d.name, "ok": False, "error": str(exc.detail)})
            except Exception:  # noqa: BLE001 -- report per-skill, never 500 the batch
                # A generic message — a raw exception string could echo a fragment of an uploaded secret.
                results.append({"name": d.name, "ok": False, "error": "registration failed"})
        return {"results": results}
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def _bundle_allowed(caller: Caller, bundle: Bundle, db: AsyncSession) -> bool:
    """Skill visibility for a tool-restricted member: the access list may grant a bundle by its own
    name (recipe-only skills) or via any of its tools. Owner / NULL access see everything."""
    if caller.role == "owner" or caller.membership.tool_access is None:
        return True
    access = set(caller.membership.tool_access)
    if bundle.name in access:
        return True
    tools = (await db.execute(select(Tool.name).where(Tool.bundle_id == bundle.id))).all()
    return any(r[0] in access for r in tools)


@app.get("/bundles")
async def list_bundles(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()
    return [{"id": b.id, "name": b.name, "owner": b.owner}
            for b in rows if await _bundle_allowed(caller, b, db)]


@app.get("/bundles/by-name/{name}")
async def get_bundle_by_name(
    name: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Name-keyed lookup so shareable detail URLs (/app/skills/<name>) resolve without an id."""
    bundle = (await db.execute(
        select(Bundle).where(Bundle.org_id == caller.org_id, Bundle.name == name)
    )).scalars().first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not await _bundle_allowed(caller, bundle, db):
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the skill {name!r} in this team — an admin can grant it"))
    return await _bundle_view(bundle.id, db)


@app.get("/bundles/{bundle_id}")
async def get_bundle(
    bundle_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not await _bundle_allowed(caller, bundle, db):  # `treg skill install` uses this route too
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the skill {bundle.name!r} in this team — an admin can grant it"))
    return await _bundle_view(bundle_id, db)


@app.patch("/bundles/{bundle_id}")
async def update_bundle(
    bundle_id: int, body: BundleUpdate,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Edit a bundle's SKILL.md text. Only its creator or an admin may. (Execution config lives on
    the tool's cli profile, not here.)"""
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not _can_manage(caller, bundle):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this recipe")
    fields = body.model_dump(exclude_unset=True)  # exclude_unset so a field left out is untouched
    if fields.get("recipe") is not None:
        bundle.recipe = fields["recipe"]
    await db.commit()
    return await _bundle_view(bundle_id, db)


@app.delete("/bundles/{bundle_id}")
async def delete_bundle(
    bundle_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not _can_manage(caller, bundle):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this bundle")
    bundle_tools = (await db.execute(select(Tool).where(Tool.bundle_id == bundle_id))).scalars().all()
    bundle_tool_ids = {t.id for t in bundle_tools}
    bundle_secrets = (await db.execute(select(Secret).where(Secret.bundle_id == bundle_id))).scalars().all()
    # A bundle secret may be bound by a tool OUTSIDE the bundle (use-without-hold). Deleting it would
    # dangle that binding — the same invariant delete_secret guards with a 409, enforced here too.
    org_tools = (await db.execute(select(Tool).where(Tool.org_id == bundle.org_id))).scalars().all()
    outside = [t for t in org_tools if t.id not in bundle_tool_ids]
    # A bundle secret may be referenced by an outside tool's HTTP binding OR its local-run cli.inject —
    # guard BOTH (delete_secret does), else a local-run tool would dangle a missing secret_id.
    referenced = {b.get("secret_id") for t in outside for b in t.bindings}
    referenced |= {e.get("secret_id") for t in outside for e in ((t.cli or {}).get("inject") or [])}
    if any(s.id in referenced for s in bundle_secrets):
        raise HTTPException(status_code=409, detail="a bundle secret is referenced by a tool outside this bundle")
    for t in bundle_tools:
        await db.delete(t)
    for s in bundle_secrets:
        await db.delete(s)
    await db.delete(bundle)
    await db.commit()
    return {"deleted": bundle_id}


# ---- audit read ---------------------------------------------------------------------------
@app.get("/calls")
async def list_calls(
    limit: int = 50, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    limit = max(1, min(limit, 500))
    rows = (
        await db.execute(
            select(CallRecord)
            .where(CallRecord.org_id == caller.org_id)
            .order_by(CallRecord.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": c.id,
            "user_email": c.user_email,
            "tool_name": c.tool_name,
            "method": c.method,
            "path": c.path,
            "status_code": c.status_code,
            "kind": c.kind,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


@app.get("/runs")
async def list_runs(
    limit: int = 50, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Audit log for CLI executions (`treg run`, both tiers), scoped to the caller's org — each row
    tagged `where`: "server" (RunRecord) or "local" (a `local_run` GRANT on the member's machine).
    Local successes carry no exit code (only failures report back), so `exit_code` is null for them.
    Ids are prefixed (s/l) so the two sources never collide as list keys."""
    limit = max(1, min(limit, 500))
    server = (await db.execute(
        select(RunRecord).where(RunRecord.org_id == caller.org_id)
        .order_by(RunRecord.id.desc()).limit(limit)
    )).scalars().all()
    # A local run is audited as its GRANT (kind="local_run"); the redacted argv lives in `path`.
    local = (await db.execute(
        select(CallRecord).where(
            CallRecord.org_id == caller.org_id, CallRecord.kind == "local_run",
            CallRecord.method == "GRANT")
        .order_by(CallRecord.id.desc()).limit(limit)
    )).scalars().all()
    rows = [
        {"id": f"s{r.id}", "user_email": r.user_email, "tool": r.bundle_name,  # bundle_name = tool (historical)
         "argv": r.argv, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
         "where": "server", "created_at": r.created_at.isoformat()}
        for r in server
    ] + [
        {"id": f"l{c.id}", "user_email": c.user_email, "tool": c.tool_name,
         "argv": (c.path or "").split(), "exit_code": None, "duration_ms": None,
         "where": "local", "created_at": c.created_at.isoformat()}
        for c in local
    ]
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:limit]


# ---- OAuth connect flow (Phase C): mint the first token via browser consent --------------
@app.post("/oauth/start")
async def oauth_start(
    body: OAuthStartIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    state = crypto.new_token()
    treg_callback = f"{get_settings().public_url.rstrip('/')}/oauth/callback"
    # The code must come back to treg's OWN callback — a body-supplied redirect_uri pointing elsewhere
    # turns this into a consent-phishing URL builder (a legit provider link that routes the code away).
    if body.redirect_uri and body.redirect_uri.rstrip("/") != treg_callback:
        raise HTTPException(status_code=422, detail="redirect_uri must be treg's own /oauth/callback")
    redirect_uri = body.redirect_uri or treg_callback
    pending = PendingOAuth(
        org_id=caller.org_id, state=state, name=body.name, owner=caller.email,
        client_id=body.client_id, client_secret=crypto.encrypt(body.client_secret),
        auth_uri=body.auth_uri, token_uri=body.token_uri, scopes=" ".join(body.scopes),
        redirect_uri=redirect_uri,
    )
    db.add(pending)
    await db.commit()
    return {"state": state, "consent_url": oauth.consent_url(pending), "redirect_uri": redirect_uri}


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request, state: str = "", code: str = "", error: str = "",
    db: AsyncSession = Depends(get_session),
):
    # Hit by the BROWSER on redirect — no token; protected by the unguessable `state`.
    pending = (await db.execute(select(PendingOAuth).where(PendingOAuth.state == state))).scalar_one_or_none()
    if pending is None:
        return _auth_page("Connect failed", "Invalid or expired authorization link.", ok=False, status=404)
    if pending.status != "pending":
        # A browser re-load re-hits this URL with a now-spent code; re-exchanging would fail and
        # flip a successful connect's status to "error". Return the terminal result without redoing it.
        if pending.status == "done":
            return _auth_page("Connected", "You can close this tab.")
        return _auth_page("Connect failed", "This authorization already failed. Start the connect again.", ok=False, status=400)
    if _as_naive(pending.created_at) < _utcnow_naive() - timedelta(minutes=health.OAUTH_PENDING_TTL_MIN):
        pending.status, pending.detail = "error", "expired"  # an old state must not stay redeemable
        await db.commit()
        return _auth_page("Connect failed", "This authorization link expired. Start the connect again.", ok=False, status=400)
    if error or not code:
        pending.status, pending.detail = "error", (error or "no authorization code")[:200]
        await db.commit()
        return _auth_page("Connect failed", "Authorization failed. You can close this tab and try again.", ok=False, status=400)
    try:
        blob = await oauth.exchange_code(pending, code, request.app.state.http)
        secret = Secret(
            org_id=pending.org_id, name=pending.name, owner=pending.owner, kind="oauth",
            value=crypto.encrypt(json.dumps(blob)),
        )
        db.add(secret)
        await db.flush()
        pending.status, pending.secret_id, pending.detail = "done", secret.id, "connected"
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth] token exchange failed for state {state}: {exc}")  # detail stays server-side
        pending.status, pending.detail = "error", "token exchange failed"
        await db.commit()
        return _auth_page("Connect failed", "Token exchange failed. You can close this tab and try again.", ok=False, status=502)
    return _auth_page("Connected", "You can close this tab and return to the terminal.")


@app.get("/oauth/status/{state}")
async def oauth_status(
    state: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    pending = (
        await db.execute(
            select(PendingOAuth).where(PendingOAuth.state == state, PendingOAuth.org_id == caller.org_id)
        )
    ).scalar_one_or_none()
    if pending is None:
        raise HTTPException(status_code=404, detail="unknown oauth state")
    return {"status": pending.status, "secret_id": pending.secret_id, "detail": pending.detail, "name": pending.name}


# ---- credential health (Phase B): validate all creds + alert owners ----------------------
@app.post("/health/run")
async def run_health(
    request: Request, all_orgs: bool = False,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    # On-demand + Render-Cron trigger. Refreshes oauth tokens, probes tools, alerts owners.
    # Scoped to the caller's org so a member only ever probes/sees their own org's credentials —
    # EXCEPT a super-admin may pass ?all_orgs=1 to sweep EVERY org (so a single Render Cron token can
    # validate the whole platform, not just its own org).
    if all_orgs:
        if not caller.user.is_superadmin:
            raise HTTPException(status_code=403, detail="all_orgs requires super-admin")
        return await health.run_all(db, request.app.state.http, org_id=None)
    return await health.run_all(db, request.app.state.http, org_id=caller.org_id)


@app.get("/health")
async def get_health(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()
    visible = await _visible_secret_ids(caller, db)
    if visible is not None:  # same visibility rule as /secrets — health mustn't leak hidden keys
        rows = [s for s in rows if s.id in visible]
    return [
        {
            "secret_id": s.id,
            "name": s.name,
            "owner": s.owner,
            "kind": s.kind,
            "status": s.health_status,
            "detail": s.health_detail,
            "checked_at": s.health_checked_at.isoformat() if s.health_checked_at else None,
        }
        for s in rows
    ]


# ---- super-admin: cross-tenant read + control (env token OR is_superadmin user) -----------
class BoolIn(BaseModel):
    value: bool = True


def _tally(items) -> dict:
    d: dict[str, int] = {}
    for k in items:
        d[k] = d.get(k, 0) + 1
    return d


@app.get("/admin/stats")
async def admin_stats(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> dict:
    async def n(model) -> int:
        return (await db.execute(select(func.count()).select_from(model))).scalar() or 0

    tools = (await db.execute(select(Tool))).scalars().all()
    secrets = (await db.execute(select(Secret))).scalars().all()
    calls = (await db.execute(select(CallRecord))).scalars().all()
    users = (await db.execute(select(User))).scalars().all()
    orgs = (await db.execute(select(Org))).scalars().all()
    # Sandbox onboarding data isn't real platform usage — exclude the demo footprint from totals so
    # metrics stay honest (fake teammates, demo teams, and everything scoped to them).
    demo_org_ids = {o.id for o in orgs if o.demo}
    users = [u for u in users if not u.demo]
    orgs = [o for o in orgs if not o.demo]
    tools = [t for t in tools if t.org_id not in demo_org_ids]
    secrets = [s for s in secrets if s.org_id not in demo_org_ids]
    calls = [c for c in calls if c.org_id not in demo_org_ids]
    now = _utcnow_naive()

    def since(rows, days, pred=lambda r: True):
        cut = now - timedelta(days=days)
        return sum(1 for r in rows if _as_naive(r.created_at) >= cut and pred(r))

    ok = sum(1 for c in calls if c.status_code < 400)
    return {
        "totals": {
            "users": len(users), "orgs": len(orgs), "tools": len(tools),
            "secrets": len(secrets), "bundles": await n(Bundle), "calls": len(calls),
            "superadmins": sum(1 for u in users if u.is_superadmin),
            "suspended_orgs": sum(1 for o in orgs if o.suspended),
        },
        "tools_by_injector": _tally(b.get("injector", "?") for t in tools for b in t.bindings),
        "tools_by_host": _tally(t.host for t in tools),
        "credential_health": _tally(s.health_status for s in secrets),
        "calls": {
            "last_7d": since(calls, 7), "last_30d": since(calls, 30), "total": len(calls),
            "success_rate": round(ok / len(calls), 3) if calls else None,
        },
        "growth": {
            "new_users_7d": since(users, 7), "new_users_30d": since(users, 30),
            "new_orgs_7d": since(orgs, 7), "new_orgs_30d": since(orgs, 30),
        },
    }


@app.get("/admin/orgs")
async def admin_orgs(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    orgs = (await db.execute(select(Org))).scalars().all()

    async def _counts(model) -> dict[int, int]:  # one grouped COUNT instead of one-per-org (was O(orgs) queries)
        rows = await db.execute(select(model.org_id, func.count()).group_by(model.org_id))
        return {oid: n for oid, n in rows.all()}

    mems: dict[int, list] = {}
    for m in (await db.execute(select(Membership))).scalars().all():
        mems.setdefault(m.org_id, []).append(m)
    tool_n, secret_n, bundle_n = await _counts(Tool), await _counts(Secret), await _counts(Bundle)
    # Was 1 + 4N serial queries (401 at 100 orgs); now a constant ~4 regardless of tenant count.
    return [
        {
            "id": o.id, "slug": o.slug, "name": o.name, "suspended": o.suspended,
            "members": len(mems.get(o.id, [])), "roles": _tally(m.role for m in mems.get(o.id, [])),
            "tools": tool_n.get(o.id, 0), "secrets": secret_n.get(o.id, 0), "bundles": bundle_n.get(o.id, 0),
            "created_at": o.created_at.isoformat(),
        }
        for o in orgs
    ]


@app.get("/admin/orgs/{org_id}")
async def admin_org_detail(
    org_id: int, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    mem = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    umap = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_([m.user_id for m in mem]))
    )).scalars().all()}  # batched (was one db.get per member)
    members = [{"user_id": m.user_id, "email": umap[m.user_id].email if m.user_id in umap else None, "role": m.role}
               for m in mem]
    tools = (await db.execute(select(Tool).where(Tool.org_id == org_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(Secret.org_id == org_id))).scalars().all()
    recent = (
        await db.execute(select(CallRecord).where(CallRecord.org_id == org_id).order_by(CallRecord.id.desc()).limit(20))
    ).scalars().all()
    return {
        "id": org.id, "slug": org.slug, "name": org.name, "suspended": org.suspended,
        "members": members,
        "tools": [{"id": t.id, "name": t.name, "host": t.host, "owner": t.owner,
                   "injectors": [b.get("injector") for b in t.bindings]} for t in tools],
        "secrets": [{"id": s.id, "name": s.name, "kind": s.kind, "health": s.health_status, "owner": s.owner} for s in secrets],
        "recent_calls": [{"tool": c.tool_name, "method": c.method, "status": c.status_code,
                          "user": c.user_email, "at": c.created_at.isoformat()} for c in recent],
    }


@app.get("/admin/users")
async def admin_users(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    users = (await db.execute(select(User))).scalars().all()
    mems_by_user: dict[int, list] = {}  # all memberships in one query, grouped (was one query per user)
    for m in (await db.execute(select(Membership))).scalars().all():
        mems_by_user.setdefault(m.user_id, []).append(m)
    omap = {o.id: o for o in (await db.execute(  # all referenced orgs in one query (was one db.get per membership)
        select(Org).where(Org.id.in_({m.org_id for ms in mems_by_user.values() for m in ms}))
    )).scalars().all()}
    return [
        {
            "id": u.id, "email": u.email, "is_superadmin": u.is_superadmin, "suspended": u.suspended,
            "orgs": [{"slug": omap[m.org_id].slug if m.org_id in omap else None, "role": m.role}
                     for m in mems_by_user.get(u.id, [])],
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


@app.get("/admin/tools")
async def admin_tools(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    tools = (await db.execute(select(Tool))).scalars().all()
    omap = {o.id: o for o in (await db.execute(  # batched (was one db.get per tool)
        select(Org).where(Org.id.in_({t.org_id for t in tools}))
    )).scalars().all()}
    return [{"id": t.id, "name": t.name, "org": omap[t.org_id].slug if t.org_id in omap else None,
             "host": t.host, "owner": t.owner, "injectors": [b.get("injector") for b in t.bindings]} for t in tools]


@app.get("/admin/calls")
async def admin_calls(
    limit: int = 50, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    limit = max(1, min(limit, 1000))
    rows = (await db.execute(select(CallRecord).order_by(CallRecord.id.desc()).limit(limit))).scalars().all()
    return [{"id": c.id, "org_id": c.org_id, "user": c.user_email, "tool": c.tool_name,
             "method": c.method, "status": c.status_code, "at": c.created_at.isoformat()} for c in rows]


@app.get("/admin/health")
async def admin_health(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.health_status != "ok"))).scalars().all()
    omap = {o.id: o for o in (await db.execute(  # batched (was one db.get per secret)
        select(Org).where(Org.id.in_({s.org_id for s in rows}))
    )).scalars().all()}
    out: list[dict] = []
    for s in rows:
        org = omap.get(s.org_id)
        out.append({"secret_id": s.id, "name": s.name, "org": org.slug if org else None,
                    "kind": s.kind, "status": s.health_status, "detail": s.health_detail})
    return out


@app.post("/admin/users/{user_id}/superadmin")
async def admin_set_superadmin(
    user_id: int, body: BoolIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Demoting the last active super-admin locks everyone out of /admin/* (the env token bypasses).
    if not body.value and principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot demote the last active super-admin")
    user.is_superadmin = body.value
    await db.commit()
    return {"user_id": user_id, "is_superadmin": user.is_superadmin}


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(
    user_id: int, body: BoolIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if body.value and principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot suspend the last active super-admin")
    user.suspended = body.value
    await db.commit()
    return {"user_id": user_id, "suspended": user.suspended}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot delete the last active super-admin")
    mem = (await db.execute(select(Membership).where(Membership.user_id == user_id))).scalars().all()
    affected = {m.org_id for m in mem}
    for m in mem:
        await db.delete(m)
    await db.flush()
    emptied = []
    for oid in affected:
        survivors = (
            await db.execute(select(Membership).where(Membership.org_id == oid).order_by(Membership.id))
        ).scalars().all()
        if not survivors:  # an org left with zero members is dead — cascade it away
            org = await db.get(Org, oid)
            if org is not None:
                await _cascade_delete_org(org, db)
                emptied.append(oid)
        elif not any(m.role == "owner" for m in survivors):
            # Deleting the sole owner would leave an ungovernable org (no one can pass _require_owner_of).
            # Promote the earliest-joined survivor so ownership never evaporates.
            survivors[0].role = "owner"
    await db.delete(user)
    await db.commit()
    return {"deleted_user": user_id, "deleted_empty_orgs": emptied}


@app.post("/admin/orgs/{org_id}/suspend")
async def admin_suspend_org(
    org_id: int, body: BoolIn, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    org.suspended = body.value
    await db.commit()
    return {"org_id": org_id, "suspended": org.suspended}


@app.delete("/admin/orgs/{org_id}")
async def admin_delete_org(
    org_id: int, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    await _cascade_delete_org(org, db)
    await db.commit()
    return {"deleted_org": org_id}


# ---- the proxy: call a tool without holding its credential --------------------------------
async def _resolve_call(rest: str, org_id: int, db: AsyncSession) -> tuple[Tool, str]:
    """Resolve `/call/<rest>` to (tool, full upstream URL), scoped to the caller's org. Shapes:

    - URL-passthrough (agent-facing): rest is the real upstream URL. Resolve the tool by host
      (indexed) + longest base_url prefix — the caller types no treg vocabulary, just the API.
    - Named (CLI/legacy): rest = "<tool-name>/<upstream-path>".

    Both lookups are constrained to `org_id`, so two orgs resolve independently (and may reuse
    a tool name or an upstream host without colliding).
    """
    norm = _normalize_scheme(rest)
    if norm.startswith("http://") or norm.startswith("https://"):
        try:
            host = urlsplit(norm).netloc.lower()
        except ValueError:  # malformed passthrough URL (e.g. unbalanced IPv6 brackets) → 400, not 500
            raise HTTPException(status_code=400, detail="malformed upstream URL")
        candidates = (
            await db.execute(select(Tool).where(Tool.host == host, Tool.org_id == org_id))
        ).scalars().all()
        # Match on a path-segment boundary, not a raw string prefix: base `.../v2` must NOT match
        # request `.../v20/...` (that would inject v2's credential onto an unregistered sibling path).
        def _prefix_match(base: str) -> bool:
            b = base.rstrip("/")
            return norm == b or norm.startswith(b + "/")

        matches = [t for t in candidates if _prefix_match(t.base_url)]
        if not matches:
            raise HTTPException(status_code=404, detail=f"no registered tool for upstream {host!r}")
        # Tiebreak on the NORMALIZED length so `.../v1` and `.../v1/` count equal (a real 409), not
        # one silently "longer" than the other.
        longest = max(len(t.base_url.rstrip("/")) for t in matches)
        top = [t for t in matches if len(t.base_url.rstrip("/")) == longest]
        if len(top) > 1:
            raise HTTPException(status_code=409, detail=f"ambiguous: multiple tools match {host!r}")
        return top[0], norm

    name, _, path = rest.partition("/")
    tool = (
        await db.execute(select(Tool).where(Tool.name == name, Tool.org_id == org_id))
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no tool {name!r} in this org")
    base = tool.base_url.rstrip("/")
    # No path → the base URL itself, WITHOUT a trailing slash: a base pinned to a full resource
    # (e.g. .../v1/charges) must relay as-is — Stripe 404s `/v1/charges/`.
    return tool, (f"{base}/{path.lstrip('/')}" if path else base)


async def _relay_live_demo(request: Request, upstream_url: str, key: str, visitor: str):
    """The sandbox's ONE real upstream call (the landing live wire). Deliberately narrower than
    relay(): form-encoded only, auth header built here from the env key (never from a sandbox
    secret), and `metadata[visitor]` is OVERRIDDEN server-side so the landing feed's name is
    always ours, whatever the caller put in the body."""
    from urllib.parse import parse_qsl, urlencode
    http: httpx.AsyncClient = request.app.state.http
    headers = {"Authorization": f"Bearer {key}"}
    content = None
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        pairs = [(k, v) for k, v in parse_qsl(body, keep_blank_values=True) if k != "metadata[visitor]"]
        pairs.append(("metadata[visitor]", visitor))
        content = urlencode(pairs)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = await http.request(request.method, upstream_url, params=request.query_params.multi_items(),
                           content=content, headers=headers)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@app.api_route(
    "/call/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def call_tool(
    rest: str,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
):
    # Faithful-relay: use the RAW request path, not Starlette's decoded path param. Decoding is
    # lossy — an encoded slash (`%2f`) in `rest` would become a real `/` and change the upstream
    # route (npm's scoped publish `PUT /@scope%2fname` 404s as `/@scope/name`). httpx preserves
    # valid percent-escapes, so the original bytes travel through to the upstream one-to-one.
    raw_path = request.scope.get("raw_path")
    if raw_path:
        _, sep, raw_rest = raw_path.decode("ascii", "replace").partition("/call/")
        if sep:
            rest = raw_rest
    tool, upstream_url = await _resolve_call(rest, caller.org_id, db)
    _require_tool_access(caller, tool.name)  # per-member tool ACL (NULL access = all; admins exempt)
    await _enforce_daily_cap(caller, db)  # per-user daily cap (skips sandbox + unmetered members)
    if caller.org.public_demo and not _role_at_least(caller.role, "admin"):
        await _enforce_public_demo_ip_cap(request, db)  # shared token → meter by client IP, not user

    def _audit(status_code: int) -> None:  # audit the attempt too — failures are results worth recording
        audit.record_call(
            org_id=caller.org_id, user_email=caller.email, tool_name=tool.name,
            method=request.method, path=upstream_url, status_code=status_code,
        )

    # Landing-page sandbox: never touch the network — EXCEPT the one live wire. A call to the
    # exact seeded stripe tool (fingerprint-matched; see sandbox.is_live_tool) relays to the real
    # Stripe test API with the env-held demo key. Any tampered/lookalike tool falls through to
    # synthesize below, so there is never a key to exfiltrate from a sandbox org.
    if demo_sandbox.is_sandbox(caller.org):
        live_key = get_settings().demo_stripe_key
        if live_key and demo_sandbox.is_live_tool(tool) and request.method in ("GET", "POST"):
            await _enforce_public_demo_ip_cap(request, db)  # one shared wire → meter by client IP
            try:
                response = await _relay_live_demo(
                    request, upstream_url, live_key, demo_sandbox.visitor_name(caller.org.slug))
            except httpx.RequestError as exc:
                _audit(502)
                raise HTTPException(status_code=502, detail=f"upstream request failed: {exc}")
            _audit(response.status_code)
            return response
        secrets = {}
        for sid in {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}:
            s = await db.get(Secret, sid)
            if s is not None and s.org_id == caller.org_id:
                secrets[sid] = s
        body = (await request.body()).decode("utf-8", "replace")
        result = demo_sandbox.synthesize(
            request.method, upstream_url, tool, secrets,
            query=request.query_params.multi_items(), body=body)
        _audit(200)
        return JSONResponse(result)

    try:
        # Load every secret the bindings need (api does the DB work; proxy stays I/O-free).
        secrets: dict[int, Secret] = {}
        for sid in {b["secret_id"] for b in tool.bindings}:
            secret = await db.get(Secret, sid)
            if secret is None or secret.org_id != caller.org_id:
                raise HTTPException(status_code=409, detail="a bound secret is missing")
            # treg keeps oauth tokens fresh: refresh in place if stale, before injecting.
            try:
                await oauth.ensure_fresh(secret, db, request.app.state.http)
            except Exception as exc:  # noqa: BLE001 — surface a clear 502 instead of injecting a dead token
                raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
            secrets[sid] = secret
        try:
            response = await relay(request, upstream_url, tool, secrets, request.app.state.http)
        except ValueError as exc:  # a binding/injector mismatch (e.g. non-JSON secret on an oauth binding)
            raise HTTPException(status_code=502, detail=f"credential injection failed: {exc}")
        except httpx.RequestError as exc:  # upstream down/timeout is a gateway fault, not treg's 500
            raise HTTPException(status_code=502, detail=f"upstream request failed: {exc}")
    except HTTPException as exc:
        _audit(exc.status_code)  # record the failed attempt (missing secret / refresh / upstream), then re-raise
        raise
    # Fire-and-forget audit — does not block the streaming response (rule #2).
    _audit(response.status_code)
    return response


# ---- server-side CLI execution (Tier 0 `treg run`) ---------------------------------------
class RunIn(BaseModel):
    tool: str             # the tool name in the caller's org (its `cli` profile drives execution)
    args: list[str] = []  # argv passed to the CLI (secrets are injected via env, never here)
    timeout_s: int | None = None


@app.post("/run")
async def run_tool_server(
    body: RunIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Run a tool's CLI **on the treg server**, with its `cli.inject` secrets injected into the
    child process — the caller never holds the key. Both run tiers read the same `Tool.cli`
    profile; any tool WITH a profile is server-runnable (no per-tool opt-in — unlike the local
    tier, the key never reaches the member, and the bin allow-list still gates what executes).
    See docs/CLI-RUN-PLAN.md.

    member+ (executing argv server-side is a register-tier capability, not a read); the sandbox is
    excluded (it never touches the real world). A non-zero CLI exit is a normal 200 result with
    `exit_code` set; only a failure to *start* (not enabled / CLI absent) is a 4xx."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="CLI run is disabled in the sandbox")
    tool = (
        await db.execute(select(Tool).where(Tool.name == body.tool, Tool.org_id == caller.org_id))
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no tool {body.tool!r} in this org")
    _require_tool_access(caller, tool.name)  # per-member tool ACL
    await _enforce_daily_cap(caller, db)  # a server run counts toward the per-user daily cap
    try:
        exec_bin = runner.resolve_exec_bin(tool)  # the SAME resolution run_tool execs — never diverges
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if exec_bin not in _allowed_server_bins():
        raise HTTPException(status_code=422, detail=(
            f"{exec_bin!r} is not approved for server runs — only catalog-known CLIs may run on the "
            "server (an admin can allow more via TREG_RUN_ALLOWED_BINS). Use `treg run --local` instead."))
    timeout = max(1, min(body.timeout_s or runner.DEFAULT_TIMEOUT_S, 600))
    try:
        async with runner.run_slot(caller.email):  # cap concurrent server runs (global + per-user)
            result = await runner.run_tool(tool, list(body.args), db, timeout_s=timeout)
    except runner.RunBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record_run(
        org_id=caller.org_id, user_email=caller.email, bundle_name=tool.name,
        argv=_redact_argv_list(list(body.args)),  # redact any credential typed inline before it's stored
        exit_code=result.exit_code, duration_ms=result.duration_ms,
    )
    return {
        "tool": tool.name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }


# ---- view helpers (never leak secret values) ----------------------------------------------
def _secret_view(s: Secret) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "owner": s.owner, "bundle_id": s.bundle_id}


def _tool_view(t: Tool) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "owner": t.owner,
        "base_url": t.base_url,
        "host": t.host,
        "bindings": t.bindings,
        "health_check": t.health_check,
        "examples": t.examples or [],
        "cli": t.cli,
        # Server-computed so the dashboard never guesses: a run needs a cli profile, an allow-listed bin
        # (server config the client can't see), AND a server-injectable auth mechanism — a config_file /
        # device CLI authenticates from the member's own machine, so it's local-only (default "env" keeps
        # every pre-auth_mechanism tool server-runnable as before).
        "server_runnable": (bool(t.cli) and (t.cli.get("bin") or t.name) in _allowed_server_bins()
                            and (t.cli.get("auth_mechanism") or "env") in ("env", "argv")),
        "bundle_id": t.bundle_id,
    }


async def _bundle_view(bundle_id: int, db: AsyncSession) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    tools = (await db.execute(select(Tool).where(Tool.bundle_id == bundle_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(Secret.bundle_id == bundle_id))).scalars().all()
    return {
        "id": bundle.id,
        "name": bundle.name,
        "owner": bundle.owner,
        "recipe": bundle.recipe,
        "files": bundle.files or {},   # companion files {relpath: content} — `skill install` writes these
        "tools": [_tool_view(t) for t in tools],
        "secrets": [_secret_view(s) for s in secrets],
    }
