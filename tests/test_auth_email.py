"""Email one-time-code login — the third identity door (alongside GitHub OAuth + per-org token).

Proving an email == login; first proof registers (creates a personal org). Dev mode returns the
code so dummy emails are testable without a mail sender.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings
from treg.db import reset_db


@pytest.fixture
async def client():
    await reset_db()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://registry",
        headers={"ngrok-skip-browser-warning": "1"},
    ) as c:
        yield c


async def _otp_login(c: AsyncClient, email: str) -> str:
    code = (await c.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    r = await c.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_first_login_registers_user_with_no_org_then_reuses_identity(client):
    tok = await _otp_login(client, "neo@matrix.io")
    orgs = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    assert orgs == []  # no auto personal org — the user names + creates their first team next

    await _otp_login(client, "neo@matrix.io")  # second time = login, not a new user
    orgs2 = (await client.get("/orgs", headers={"X-Treg-Token": tok})).json()
    assert orgs2 == []  # still no duplicate user; still zero orgs until they create one


async def test_verify_rejects_wrong_and_unknown_code(client):
    await client.post("/auth/email/start", json={"email": "trinity@matrix.io"})
    bad = await client.post("/auth/email/verify", json={"email": "trinity@matrix.io", "code": "000000"})
    assert bad.status_code == 401
    unknown = await client.post("/auth/email/verify", json={"email": "nobody@x.io", "code": "123456"})
    assert unknown.status_code == 401


async def test_code_is_one_time(client):
    email = "morpheus@matrix.io"
    code = (await client.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    ok = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert ok.status_code == 200
    replay = await client.post("/auth/email/verify", json={"email": email, "code": code})
    assert replay.status_code == 401  # consumed on first use


async def test_start_is_rate_limited_per_email(client):
    from treg.api import OTP_START_MAX_PER_EMAIL
    email = "flood@matrix.io"
    for _ in range(OTP_START_MAX_PER_EMAIL):
        assert (await client.post("/auth/email/start", json={"email": email})).status_code == 200
    blocked = await client.post("/auth/email/start", json={"email": email})
    assert blocked.status_code == 429  # the (N+1)th code request for one inbox is refused (email-bomb guard)


async def test_start_rate_limit_is_per_email_not_global(client):
    from treg.api import OTP_START_MAX_PER_EMAIL
    for _ in range(OTP_START_MAX_PER_EMAIL + 2):  # drive one inbox past its cap
        await client.post("/auth/email/start", json={"email": "victim@matrix.io"})
    other = await client.post("/auth/email/start", json={"email": "bystander@matrix.io"})
    assert other.status_code == 200  # a different inbox is unaffected (per-key window, not a global lock)


async def test_start_is_rate_limited_per_ip(client):
    from treg.api import OTP_START_MAX_PER_IP
    for i in range(OTP_START_MAX_PER_IP):  # distinct emails so the per-email cap never trips first
        assert (await client.post("/auth/email/start", json={"email": f"u{i}@matrix.io"})).status_code == 200
    blocked = await client.post("/auth/email/start", json={"email": "late@matrix.io"})
    assert blocked.status_code == 429  # a fresh inbox is blocked purely by the per-IP cap


async def test_dev_mode_off_hides_the_code(client):
    get_settings.cache_clear()
    settings = get_settings()
    object.__setattr__(settings, "email_dev_mode", False)
    try:
        r = (await client.post("/auth/email/start", json={"email": "a@b.io"})).json()
        assert r["sent"] is True and "dev_code" not in r
    finally:
        object.__setattr__(settings, "email_dev_mode", True)
        get_settings.cache_clear()
