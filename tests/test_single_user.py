"""Frictionless local mode — `curl … | sh` lands on a dashboard you are already signed into.

The whole point is that there is no account, so the ONLY thing standing between this and handing a
stranger the registry is the guard. These tests are mostly about the guard.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import api, session as sess
from treg.api import LOCAL_ORG_NAME, LOCAL_USER_EMAIL, app
from treg.config import Settings, get_settings
from treg.db import reset_db, session_maker
from treg.models import Membership, Org, User


def _settings(**kw) -> Settings:
    base = dict(single_user=True, database_url="sqlite+aiosqlite:///./x.db",
                public_url="http://localhost:18790")
    base.update(kw)
    return Settings(**base)


# ---- the guard: this is the security of the whole feature -------------------------------------
def test_local_mode_needs_sqlite_and_a_loopback_url():
    assert _settings().single_user_ok is True
    assert _settings(public_url="http://127.0.0.1:18790").single_user_ok is True

    # a real deploy — either half is enough to refuse
    assert _settings(database_url="postgresql+asyncpg://u@h/db").single_user_ok is False, \
        "a Postgres URL means a real deploy: no-login must be off"
    assert _settings(public_url="https://treg.to").single_user_ok is False, \
        "a public domain must never serve a no-login dashboard"
    assert _settings(database_url="postgresql+asyncpg://u@h/db",
                     public_url="https://treg.to").single_user_ok is False


def test_it_is_off_unless_explicitly_asked_for():
    assert _settings(single_user=False).single_user_ok is False
    assert get_settings().single_user_ok is False, "the default deployment must never be no-login"


# ---- the bootstrap ----------------------------------------------------------------------------
@pytest.fixture
async def local(tmp_path, monkeypatch):
    """A server in single-user mode, with the token file in a temp dir."""
    await reset_db()
    token_file = tmp_path / "local-token"
    monkeypatch.setattr(api, "get_settings", lambda: _settings(single_user_token_file=str(token_file)))
    await api._bootstrap_single_user()
    yield token_file


async def test_bootstrap_creates_the_owner_and_writes_the_token(local):
    assert local.exists(), "the installer reads this file to point the CLI at the server"
    assert local.read_text().strip(), "a token must actually be written"
    assert oct(local.stat().st_mode)[-3:] == "600", "the token file must not be world-readable"
    async with session_maker() as s:
        user = (await s.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one()
        org = (await s.execute(select(Org).where(Org.slug == LOCAL_ORG_NAME))).scalar_one()
        m = (await s.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org.id))).scalar_one()
    assert m.role == "owner"


async def test_the_token_is_stable_across_restarts(local):
    """Rotating on every boot would break the CLI config the installer just wrote."""
    first = local.read_text()
    await api._bootstrap_single_user()
    await api._bootstrap_single_user()
    assert local.read_text() == first


async def test_a_deleted_token_file_is_re_minted(local):
    """The one case where rotating is right: the user lost the file and needs a way back in."""
    first = local.read_text()
    local.unlink()
    await api._bootstrap_single_user()
    assert local.exists() and local.read_text() != first


async def test_the_minted_token_actually_works(local):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.get("/tools", headers={"X-Treg-Token": local.read_text().strip()})
    assert r.status_code == 200, r.text


async def test_the_dashboard_opens_already_signed_in(local):
    """The feeling we are selling: no signup, no email, no password."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.get("/app")
    assert r.status_code == 200
    cookie = r.cookies.get(sess.COOKIE)
    assert cookie, "local mode must attach a session so there is no login screen"
    async with session_maker() as s:
        user = (await s.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one()
    assert sess.read_claims(cookie)["uid"] == user.id


# ---- and the same routes must stay closed on a normal deployment ------------------------------
async def test_a_normal_deployment_bootstraps_nothing_and_signs_nobody_in():
    await reset_db()
    await api._bootstrap_single_user()  # default settings → guard refuses
    async with session_maker() as s:
        assert (await s.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none() is None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.get("/app")
    assert r.status_code == 200
    assert not r.cookies.get(sess.COOKIE), "a real deploy must never hand out a session"


# ---- the installer is served ------------------------------------------------------------------
async def test_selfhost_script_is_served_and_templated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.get("/selfhost.sh")
    assert r.status_code == 200
    assert "{BASE}" not in r.text, "the serving domain must be templated in"
    assert "TREG_SINGLE_USER=true" in r.text
    assert r.headers["content-type"].startswith("text/x-shellscript")
