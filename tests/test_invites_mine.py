"""Code-free invites: an invite attaches to an email, and proving that email (any login method)
reveals it via /invites/mine and lets you accept it by id — no code required. The code path
(POST /invites/accept) stays as the out-of-band shortcut.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.db import reset_db


@pytest.fixture
async def client():
    await reset_db()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://registry",
        headers={"ngrok-skip-browser-warning": "1"},
    ) as c:
        yield c


async def _otp(c: AsyncClient, email: str) -> str:
    code = (await c.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    return (await c.post("/auth/email/verify", json={"email": email, "code": code})).json()["token"]


def _h(tok: str, org: str | None = None) -> dict:
    h = {"X-Treg-Token": tok}
    if org:
        h["X-Treg-Org"] = org
    return h


async def _make_org_with_invite(c: AsyncClient, owner_email: str, invitee: str, role: str = "member"):
    tok = await _otp(c, owner_email)
    # a fresh user has NO org — creating their first team needs only the identity token (no X-Treg-Org)
    org = (await c.post("/orgs", json={"name": "Superdesign"}, headers=_h(tok))).json()
    await c.post(f"/orgs/{org['org_id']}/invites", json={"email": invitee, "role": role}, headers=_h(tok, org["org"]))
    return tok, org


async def test_invite_seen_and_accepted_without_code(client):
    _, org = await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    bob = await _otp(client, "bob@x.io")

    mine = (await client.get("/invites/mine", headers=_h(bob))).json()
    assert [(m["org"], m["role"]) for m in mine] == [(org["org"], "member")]

    r = await client.post(f"/invites/{mine[0]['id']}/accept", headers=_h(bob))
    assert r.status_code == 200 and r.json()["role"] == "member"

    assert (await client.get("/invites/mine", headers=_h(bob))).json() == []  # consumed
    slugs = {o["slug"] for o in (await client.get("/orgs", headers=_h(bob))).json()}
    assert org["org"] in slugs  # bob is now a member


async def test_cannot_accept_someone_elses_invite(client):
    tom, org = await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    invs = (await client.get(f"/orgs/{org['org_id']}/invites", headers=_h(tom, org["org"]))).json()
    invite_id = invs[0]["id"]

    mallory = await _otp(client, "mallory@evil.io")
    r = await client.post(f"/invites/{invite_id}/accept", headers=_h(mallory))
    assert r.status_code == 403  # the invite is for a different email
    assert (await client.get("/invites/mine", headers=_h(mallory))).json() == []  # sees nothing


async def test_mine_requires_auth(client):
    r = await client.get("/invites/mine")
    assert r.status_code == 401


async def _invite_code(c: AsyncClient, owner_email: str, invitee: str, role: str = "member") -> str:
    tok = await _otp(c, owner_email)
    org = (await c.post("/orgs", json={"name": "Superdesign"}, headers=_h(tok))).json()
    r = await c.post(f"/orgs/{org['org_id']}/invites",
                     json={"email": invitee, "role": role}, headers=_h(tok, org["org"]))
    return r.json()["code"]


async def test_invite_signin_link_never_mints_a_session(client):
    """A forwarded/leaked invite link must NOT log anyone in. /auth/invite-signin only prefills the
    email on the login page; the invitee still has to prove the email through a real door (OTP)."""
    code = await _invite_code(client, "tom@sd.io", "bob@x.io", "member")

    # A brand-new visitor (own clean cookie jar — the shared `client` holds tom's owner session) opens
    # the invite link. It must bounce to login with the email prefilled and set NO session cookie.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as visitor:
        r = await visitor.get(f"/auth/invite-signin?code={code}", follow_redirects=False)
        assert r.status_code == 303
        assert "invite=bob%40x.io" in r.headers["location"]  # → login, email prefilled
        assert "treg_session" not in r.headers.get("set-cookie", "")  # but NOT logged in
        # The link created no session, so this visitor stays anonymous on an identity-gated route.
        assert (await visitor.get("/invites/mine")).status_code == 401


async def test_invite_signin_link_bad_code_lands_on_site(client):
    r = await client.get("/auth/invite-signin?code=nope", follow_redirects=False)
    assert r.status_code == 303 and "invite_expired=1" in r.headers["location"]
    assert "treg_session" not in r.headers.get("set-cookie", "")


# ---- the emailed link's SECOND secret (email_token): inbox-only, may sign the invitee in ----

@pytest.fixture
def sent_invites(monkeypatch):
    """Capture what send_invite is called with — the only place the email_token ever surfaces
    (it's deliberately absent from the create-invite response), exactly like a real inbox."""
    from treg import email as email_mod
    sent = []

    async def _capture(email, inviter, org_name, role, code, email_token, expires_at=""):
        sent.append({"email": email, "code": code, "email_token": email_token})
        return True

    monkeypatch.setattr(email_mod, "send_invite", _capture)
    return sent


async def _fresh(c=None):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://registry")


async def test_email_token_not_in_create_response_and_differs_from_code(client, sent_invites):
    tok = await _otp(client, "tom@sd.io")
    org = (await client.post("/orgs", json={"name": "Superdesign"}, headers=_h(tok))).json()
    r = (await client.post(f"/orgs/{org['org_id']}/invites",
                           json={"email": "bob@x.io", "role": "member"}, headers=_h(tok, org["org"]))).json()
    assert "email_token" not in r  # the admin must never see the inbox-only secret
    assert len(sent_invites) == 1
    assert sent_invites[0]["email_token"] and sent_invites[0]["email_token"] != r["code"]


async def test_email_link_get_confirms_but_never_mints_a_session(client, sent_invites):
    """Mail scanners prefetch GETs — the GET may only render the confirm page, never a session."""
    await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    t = sent_invites[0]["email_token"]
    async with await _fresh() as visitor:
        r = await visitor.get(f"/auth/invite-signin?t={t}", follow_redirects=False)
        assert r.status_code == 200 and "Continue as bob@x.io" in r.text  # the POST-confirm page
        assert "treg_session" not in r.headers.get("set-cookie", "")
        assert (await visitor.get("/invites/mine")).status_code == 401  # still anonymous
        # …and the token is NOT consumed by the GET (a scanner prefetch must not burn the link)
        r2 = await visitor.get(f"/auth/invite-signin?t={t}", follow_redirects=False)
        assert r2.status_code == 200


async def test_email_link_post_signs_in_once_and_lands_on_invite_org(client, sent_invites):
    _, org = await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    t = sent_invites[0]["email_token"]
    async with await _fresh() as visitor:
        r = await visitor.post("/auth/invite-signin", content=f"t={t}",
                               headers={"content-type": "application/x-www-form-urlencoded"},
                               follow_redirects=False)
        assert r.status_code == 303 and f"invite_org={org['org_id']}" in r.headers["location"]
        assert "treg_session" in r.headers.get("set-cookie", "")  # signed in (first click = registration)
        mine = (await visitor.get("/invites/mine")).json()  # the cookie authenticates the session
        assert [m["org_id"] for m in mine] == [org["org_id"]]  # invite still PENDING — accepted in the app
    # one-time: a second POST (forwarded thread, replay) gets no session
    async with await _fresh() as replayer:
        r = await replayer.post("/auth/invite-signin", content=f"t={t}",
                                headers={"content-type": "application/x-www-form-urlencoded"},
                                follow_redirects=False)
        assert r.status_code == 303 and "invite_expired=1" in r.headers["location"]
        assert "treg_session" not in r.headers.get("set-cookie", "")


async def test_email_link_post_refuses_suspended_user(client, sent_invites):
    from sqlmodel import select as _select

    from treg.db import session_maker
    from treg.models import User

    await _otp(client, "bob@x.io")  # user exists…
    async with session_maker() as db:
        u = (await db.execute(_select(User).where(User.email == "bob@x.io"))).scalar_one()
        u.suspended = True
        await db.commit()
    await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    t = sent_invites[0]["email_token"]
    async with await _fresh() as visitor:
        r = await visitor.post("/auth/invite-signin", content=f"t={t}",
                               headers={"content-type": "application/x-www-form-urlencoded"},
                               follow_redirects=False)
        assert r.status_code == 403  # …but banned: may hold the link, gets no session
        assert "treg_session" not in r.headers.get("set-cookie", "")


async def test_email_link_dies_with_the_invite(client, sent_invites):
    """Revoking the invite kills its emailed link too — both secrets hang off the same row."""
    tom, org = await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    invs = (await client.get(f"/orgs/{org['org_id']}/invites", headers=_h(tom, org["org"]))).json()
    await client.delete(f"/orgs/{org['org_id']}/invites/{invs[0]['id']}", headers=_h(tom, org["org"]))
    t = sent_invites[0]["email_token"]
    async with await _fresh() as visitor:
        r = await visitor.get(f"/auth/invite-signin?t={t}", follow_redirects=False)
        assert r.status_code == 303 and "invite_expired=1" in r.headers["location"]


async def test_invites_mine_newest_first_with_created_at(client, sent_invites):
    """Two teams invite the same email → /invites/mine lists the newest invite first (the one whose
    link was most likely just clicked) and carries created_at for the dashboard's sort."""
    _, org1 = await _make_org_with_invite(client, "tom@sd.io", "bob@x.io", "member")
    tok2 = await _otp(client, "ann@other.io")
    org2 = (await client.post("/orgs", json={"name": "Second Team"}, headers=_h(tok2))).json()
    await client.post(f"/orgs/{org2['org_id']}/invites",
                      json={"email": "bob@x.io", "role": "viewer"}, headers=_h(tok2, org2["org"]))
    bob = await _otp(client, "bob@x.io")
    mine = (await client.get("/invites/mine", headers=_h(bob))).json()
    assert [m["org"] for m in mine] == [org2["org"], org1["org"]]  # newest first
    assert all(m.get("created_at") for m in mine)
