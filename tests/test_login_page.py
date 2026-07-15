"""The universal /login page + /auth/cli/approve — `treg login` opens /login?cli=<id>, which
reuses an existing dashboard session (one click) or offers every configured door. Approve is a
POST with a same-origin check so a phished GET link can't complete a handshake by itself.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg import crypto, session as sess
from treg.api import app
from treg.config import get_settings
from treg.db import reset_db, session_maker
from treg.models import Membership, Org, Tool, User

LID = "abcDEF123-_x"  # a valid login_id shape (only for page-render tests; approve needs a started one)


async def _start(web) -> tuple[str, str]:
    """Begin a login the way the CLI does: the SERVER mints the login_id + pairing code."""
    d = (await web.post("/auth/cli/start")).json()
    return d["login_id"], d["code"]


@pytest.fixture
async def web(monkeypatch):
    """An anonymous browser-shaped client. GitHub configured, Google not — so the page's
    door-rendering is assertable both ways."""
    monkeypatch.setenv("TREG_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("TREG_GITHUB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "")  # explicit: a dev's .env may configure Google
    monkeypatch.setenv("TREG_SESSION_SECRET", "test-session-secret")
    get_settings.cache_clear()
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    get_settings.cache_clear()


async def _seed_user(email="pat@x.dev") -> int:
    async with session_maker() as s:
        u = User(email=email)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_user_org(email="pat@x.dev", team="Acme", slug="acme", tools=0):
    """A user with a membership in one team (+ optional tool count) — for the /login org picker."""
    async with session_maker() as s:
        u = User(email=email); s.add(u); await s.flush()
        o = Org(name=team, slug=slug); s.add(o); await s.flush()
        s.add(Membership(user_id=u.id, org_id=o.id, role="owner", token_hash=crypto.hash_token("t-"+slug)))
        for i in range(tools):
            s.add(Tool(org_id=o.id, name=f"{slug}-tool-{i}", owner=email, base_url="https://api.x.dev",
                       host="api.x.dev", bindings=[]))
        await s.commit()
        return u.id, o


# ---- the page ------------------------------------------------------------------------------
async def test_login_without_cli_redirects_to_dashboard(web):
    r = await web.get("/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


async def test_login_rejects_malformed_login_id(web):
    r = await web.get("/login?cli=<script>alert(1)</script>")
    assert r.status_code == 400
    r = await web.get("/login?cli=ab")  # too short
    assert r.status_code == 400


async def test_login_page_shows_configured_doors(web):
    r = await web.get(f"/login?cli={LID}")
    assert r.status_code == 200
    html = r.text
    assert f"/auth/github?cli={LID}" in html      # GitHub configured → its door renders
    assert "/auth/google" not in html             # Google not configured → no door
    assert "Email me a code" in html              # the email door is always present
    assert "HAS_SESSION=false" in html            # no session → picker won't auto-load


async def test_login_page_offers_session_reuse(web):
    """With a session, the page ships the picker container + HAS_SESSION=true (the "Continue as" /
    team list is rendered client-side by loadOrgs from /auth/cli/orgs)."""
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))
    r = await web.get(f"/login?cli={LID}")
    assert r.status_code == 200
    assert 'id="orgpick"' in r.text and "HAS_SESSION=true" in r.text
    assert "or use a different account" in r.text  # doors still offered under a divider
    web.cookies.clear()
    r2 = await web.get(f"/login?cli={LID}")  # no session
    assert "HAS_SESSION=false" in r2.text and "or use a different account" not in r2.text


# ---- the org picker: /auth/cli/orgs + approve with a chosen team ---------------------------
async def test_cli_orgs_lists_the_users_teams(web):
    uid, _ = await _seed_user_org(team="Acme", slug="acme", tools=3)
    web.cookies.set("treg_session", sess.make(uid))
    d = (await web.get("/auth/cli/orgs")).json()
    assert d["email"] == "pat@x.dev"
    assert d["orgs"] and d["orgs"][0]["slug"] == "acme" and d["orgs"][0]["tool_count"] == 3
    # no session → empty (drives the JS to just show the doors)
    web.cookies.clear()
    anon = (await web.get("/auth/cli/orgs")).json()
    assert anon == {"email": None, "orgs": []}


async def test_cli_orgs_sorts_team_before_personal(web):
    """The personal org (named after the email) sorts last, the team with tools first — so the CLI
    lands on the real team, not the empty personal space."""
    uid, _ = await _seed_user_org(email="dev@x.dev", team="dev@x.dev", slug="dev-personal", tools=0)
    async with session_maker() as s:
        o = Org(name="Superdesign", slug="superdesign"); s.add(o); await s.flush()
        s.add(Membership(user_id=uid, org_id=o.id, role="member", token_hash=crypto.hash_token("t2")))
        s.add(Tool(org_id=o.id, name="stripe", owner="dev@x.dev", base_url="https://api.stripe.com",
                   host="api.stripe.com", bindings=[]))
        await s.commit()
    web.cookies.set("treg_session", sess.make(uid))
    orgs = (await web.get("/auth/cli/orgs")).json()["orgs"]
    assert [o["slug"] for o in orgs] == ["superdesign", "dev-personal"]
    assert orgs[0]["personal"] is False and orgs[1]["personal"] is True


async def test_approve_with_org_scopes_the_handshake(web):
    uid, org = await _seed_user_org(team="Acme", slug="acme")
    web.cookies.set("treg_session", sess.make(uid))
    lid, code = await _start(web)
    r = await web.post("/auth/cli/approve", json={"login_id": lid, "org": "acme", "code": code})
    assert r.status_code == 200 and r.json()["active_org"] == "acme"
    d = (await web.get(f"/auth/cli/poll?login_id={lid}")).json()  # poll carries no code
    assert d["active_org"] == "acme" and d["token"]  # the CLI adopts the chosen team, no guessing


async def test_approve_rejects_a_foreign_org(web):
    uid, _ = await _seed_user_org(team="Acme", slug="acme")
    async with session_maker() as s:  # a team the user is NOT a member of
        s.add(Org(name="Other", slug="other")); await s.commit()
    web.cookies.set("treg_session", sess.make(uid))
    lid, code = await _start(web)
    r = await web.post("/auth/cli/approve", json={"login_id": lid, "org": "other", "code": code})
    assert r.status_code == 403


# ---- approve: completing the handshake from a session --------------------------------------
async def test_approve_completes_the_cli_handshake(web):
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))
    lid, code = await _start(web)
    r = await web.post("/auth/cli/approve", json={"login_id": lid, "code": code})
    assert r.status_code == 200 and r.json()["email"] == "pat@x.dev"
    # the CLI's (codeless) poll now yields a working identity token, exactly once
    d = (await web.get(f"/auth/cli/poll?login_id={lid}")).json()
    assert d["email"] == "pat@x.dev" and d["token"]
    me = await web.get("/auth/me", headers={"X-Treg-Token": d["token"]})  # token path wins over the cookie
    assert me.status_code == 200 and me.json()["email"] == "pat@x.dev"
    again = (await web.get(f"/auth/cli/poll?login_id={lid}")).json()
    assert again == {"status": "pending"}  # single-use


async def test_approve_requires_a_session(web):
    r = await web.post("/auth/cli/approve", json={"login_id": LID})
    assert r.status_code == 401


async def test_approve_accepts_same_host_origin(web):
    """A browser on localhost sends Origin=<the host it's on>, which is NOT the configured
    public_url — approve must accept the request's own host, not just public_url (regression:
    the first cut compared Origin to public_url only and broke every localhost login)."""
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))
    lid, code = await _start(web)
    r = await web.post("/auth/cli/approve", json={"login_id": lid, "code": code},
                       headers={"Origin": "http://registry"})  # matches the test client's Host
    assert r.status_code == 200


async def test_approve_rejects_cross_origin(web):
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))
    r = await web.post("/auth/cli/approve", json={"login_id": LID},
                       headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_approve_rejects_bad_login_id(web):
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))
    r = await web.post("/auth/cli/approve", json={"login_id": "no"})
    assert r.status_code == 400


# ---- the email door end-to-end: start → verify → approve → poll -----------------------------
async def test_email_door_completes_the_handshake(web):
    start = await web.post("/auth/email/start", json={"email": "new@x.dev"})
    code = start.json()["dev_code"]  # TREG_EMAIL_DEV_MODE=true in conftest
    verify = await web.post("/auth/email/verify", json={"email": "new@x.dev", "code": code})
    assert verify.status_code == 200 and web.cookies.get("treg_session")  # verify set the session
    lid, code = await _start(web)
    r = await web.post("/auth/cli/approve", json={"login_id": lid, "code": code})
    assert r.status_code == 200
    d = (await web.get(f"/auth/cli/poll?login_id={lid}")).json()
    assert d["email"] == "new@x.dev" and d["token"]


# ---- the pairing-code phishing guard (#5) --------------------------------------------------
async def test_login_page_renders_the_pairing_code_input(web):
    assert 'id="paircode"' in (await web.get(f"/login?cli={LID}")).text  # the code field is always present


async def test_start_mints_a_login_id_and_code(web):
    d = (await web.post("/auth/cli/start")).json()
    assert len(d["login_id"]) >= 8 and len(d["code"]) == 4  # server issues both; the code is short


async def test_approve_requires_a_started_login_and_matching_code(web):
    """The phishing block, enforced at approve: a login not begun via /start can't complete, and a
    wrong/blank code is refused — so a mailed /login?cli=<attacker_id> the victim never started (or
    whose code they don't have) never yields a token. A codeless poll of it returns pending."""
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))

    # a login_id that was never started (what an attacker's mailed link may carry) → refused, no token
    bogus = await web.post("/auth/cli/approve", json={"login_id": LID, "code": "7F3K"})
    assert bogus.status_code == 400
    assert (await web.get(f"/auth/cli/poll?login_id={LID}")).json() == {"status": "pending"}

    lid, code = await _start(web)
    assert (await web.post("/auth/cli/approve", json={"login_id": lid})).status_code == 400          # no code
    assert (await web.post("/auth/cli/approve", json={"login_id": lid, "code": "XXXX"})).status_code == 400  # wrong
    assert (await web.get(f"/auth/cli/poll?login_id={lid}")).json() == {"status": "pending"}          # still nothing
    ok = await web.post("/auth/cli/approve", json={"login_id": lid, "code": code.lower()})  # case-insensitive
    assert ok.status_code == 200
    assert (await web.get(f"/auth/cli/poll?login_id={lid}")).json()["token"]  # the real approver is served


async def test_wrong_code_attempts_are_capped(web):
    """Brute-forcing the short code is bounded: after CLI_APPROVE_MAX_TRIES misses the pending login is
    discarded, so the real code can no longer be ground down (and the correct code then also fails)."""
    from treg.api import CLI_APPROVE_MAX_TRIES
    uid = await _seed_user()
    web.cookies.set("treg_session", sess.make(uid))
    lid, code = await _start(web)
    for _ in range(CLI_APPROVE_MAX_TRIES):
        assert (await web.post("/auth/cli/approve", json={"login_id": lid, "code": "0000"})).status_code == 400
    after = await web.post("/auth/cli/approve", json={"login_id": lid, "code": code})  # even the RIGHT code
    assert after.status_code == 400  # the pending login was discarded after too many misses
