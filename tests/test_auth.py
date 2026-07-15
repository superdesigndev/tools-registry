"""Phase 1.5 — GitHub OAuth + cookie sessions, and the dual-auth path (session OR X-Treg-Token).

The GitHub endpoints are faked with an in-process ASGI app mounted as the registry's outbound http
client (ASGITransport routes every absolute URL to it), so the callback exchange runs for real.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import crypto, session as sess
from treg.api import app
from treg.config import get_settings
from treg.db import reset_db, session_maker
from treg.models import Membership, Org, User


def _github_app() -> FastAPI:
    g = FastAPI()

    @g.post("/login/oauth/access_token")
    async def token() -> dict:
        return {"access_token": "gho_test", "token_type": "bearer"}

    @g.get("/user")
    async def user() -> dict:
        return {"login": "octo", "email": None}  # force the /user/emails path

    @g.get("/user/emails")
    async def emails() -> list:
        return [{"email": "octo@example.com", "primary": True, "verified": True}]

    return g


# ---- session signing (pure) ---------------------------------------------------------------
def test_session_sign_roundtrip_tamper_expiry():
    t = sess.make(42)
    assert sess.read(t) == 42
    assert sess.read(t + "x") is None          # tampered signature
    assert sess.read("garbage") is None         # malformed
    assert sess.read(sess.make(1, ttl=-1)) is None  # expired


@pytest.fixture
async def gc(monkeypatch):
    monkeypatch.setenv("TREG_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("TREG_GITHUB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("TREG_GITHUB_TOKEN_URL", "http://gh/login/oauth/access_token")
    monkeypatch.setenv("TREG_GITHUB_API_URL", "http://gh")
    monkeypatch.setenv("TREG_SESSION_SECRET", "test-session-secret")
    get_settings.cache_clear()
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=_github_app()), base_url="http://gh")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_github_login_creates_user_session_but_no_auto_org(gc):
    r = await gc.get("/auth/github", follow_redirects=False)
    assert r.status_code == 302
    state = gc.cookies.get("treg_oauth_state")
    assert state
    cb = await gc.get(f"/auth/github/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 302 and cb.headers["location"] == "/app"
    assert gc.cookies.get("treg_session")  # session cookie set (secure omitted over http)
    me = await gc.get("/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "octo@example.com"
    # first login creates the USER ONLY — no throwaway personal org; the user names their first team next
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == "octo@example.com"))).scalar_one()
        n = len((await s.execute(select(Membership).where(Membership.user_id == u.id))).scalars().all())
    assert n == 0


async def test_bad_state_rejected(gc):
    await gc.get("/auth/github", follow_redirects=False)
    cb = await gc.get("/auth/github/callback?code=abc&state=WRONG", follow_redirects=False)
    assert cb.status_code == 400


async def test_no_session_no_token_is_401(gc):
    assert (await gc.get("/auth/me")).status_code == 401
    assert (await gc.get("/tools")).status_code == 401


# ---- dual auth: a session acts in an org via X-Treg-Org -----------------------------------
async def _seed(email="dev@x.dev", role="owner", superadmin=False):
    slug = email.split("@")[0] + "-team"  # unique per user
    async with session_maker() as s:
        u = User(email=email, is_superadmin=superadmin)
        s.add(u); await s.flush()
        o = Org(name="Team", slug=slug)
        s.add(o); await s.flush()
        s.add(Membership(user_id=u.id, org_id=o.id, role=role, token_hash=crypto.hash_token("tok-"+email)))
        await s.commit()
        return u.id, o.id, slug


async def test_session_scopes_by_x_treg_org(gc):
    uid, oid, slug = await _seed()
    gc.cookies.set("treg_session", sess.make(uid))
    # no org header → 400 (must choose)
    assert (await gc.get("/tools")).status_code == 400
    # with the org header → 200, scoped to that org
    assert (await gc.get("/tools", headers={"X-Treg-Org": slug})).status_code == 200
    # a member endpoint works by org id too
    assert (await gc.get("/tools", headers={"X-Treg-Org": str(oid)})).status_code == 200
    # /orgs lists the session user's memberships (no org needed)
    orgs = await gc.get("/orgs")
    assert orgs.status_code == 200 and orgs.json()[0]["slug"] == slug


async def test_session_superadmin_reaches_admin(gc):
    uid, _, _ = await _seed(email="root@x.dev", superadmin=True)
    gc.cookies.set("treg_session", sess.make(uid))
    assert (await gc.get("/admin/stats")).status_code == 200
    # a non-superadmin session is refused
    uid2, _, _ = await _seed(email="plain@x.dev", superadmin=False)
    gc.cookies.set("treg_session", sess.make(uid2))
    assert (await gc.get("/admin/stats")).status_code == 403


async def test_cli_token_mints_a_usable_identity_token(clients):
    """GET /auth/cli-token returns a bearer token that actually works (with X-Treg-Org) — this is what
    the dashboard embeds in its copy-paste snippets + the 'copy token' button."""
    r = await clients.get("/auth/cli-token")
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    assert tok and r.json().get("email")
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    # the minted identity token authenticates a real call when paired with X-Treg-Org
    ok = await clients.get("/tools", headers={"X-Treg-Token": tok, "X-Treg-Org": slug})
    assert ok.status_code == 200, ok.text
    # ...and without X-Treg-Org it must ask for the org (identity token isn't org-scoped)
    no_org = await clients.get("/tools", headers={"X-Treg-Token": tok, "X-Treg-Org": ""})
    assert no_org.status_code == 400


async def test_cli_token_requires_auth(clients):
    r = await clients.get("/auth/cli-token", headers={"X-Treg-Token": "nope"})
    assert r.status_code == 401


# ---- Google OAuth (a parallel login door) -------------------------------------------------
def _google_app():
    g = FastAPI()

    @g.post("/token")
    async def token() -> dict:
        return {"access_token": "goog_test", "token_type": "bearer"}

    @g.get("/userinfo")
    async def userinfo() -> dict:
        return {"email": "guser@example.com", "email_verified": True}

    return g


@pytest.fixture
async def goog(monkeypatch):
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("TREG_GOOGLE_TOKEN_URL", "http://gg/token")
    monkeypatch.setenv("TREG_GOOGLE_USERINFO_URL", "http://gg/userinfo")
    monkeypatch.setenv("TREG_SESSION_SECRET", "test-session-secret")
    get_settings.cache_clear()
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=_google_app()), base_url="http://gg")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_google_login_creates_user_session_but_no_auto_org(goog):
    r = await goog.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302 and "accounts.google.com" in r.headers["location"]
    state = goog.cookies.get("treg_oauth_state")
    assert state
    cb = await goog.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 302 and cb.headers["location"] == "/app"
    assert goog.cookies.get("treg_session")
    me = await goog.get("/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "guser@example.com"
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == "guser@example.com"))).scalar_one()
        n = len((await s.execute(select(Membership).where(Membership.user_id == u.id))).scalars().all())
    assert n == 0  # first login registers the user only — no auto personal org


async def test_google_bad_state_rejected(goog):
    await goog.get("/auth/google", follow_redirects=False)
    cb = await goog.get("/auth/google/callback?code=abc&state=WRONG", follow_redirects=False)
    assert cb.status_code == 400


async def test_meta_exposes_google_flag(goog):
    assert (await goog.get("/meta")).json()["google"] is True
