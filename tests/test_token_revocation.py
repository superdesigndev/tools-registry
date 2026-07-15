"""Per-user token revocation (backlog #4). A signed identity token / session cookie carries the
token_version it was minted at; bumping User.token_version (via POST /auth/revoke-tokens) invalidates
every token the user holds — the kill switch for a leaked token that doesn't disable the account or
log everyone else out. Legacy tokens (minted before the tv claim existed) default to tv=0, so a plain
deploy revokes nobody.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from treg import session as sess
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


async def _otp_login(c: AsyncClient, email: str) -> str:
    code = (await c.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    return (await c.post("/auth/email/verify", json={"email": email, "code": code})).json()["token"]


async def test_revoke_kills_old_identity_token_and_issues_a_fresh_one(client):
    tok = await _otp_login(client, "leaky@x.io")
    h = {"X-Treg-Token": tok}
    assert (await client.get("/invites/mine", headers=h)).status_code == 200  # works before revoke

    r = await client.post("/auth/revoke-tokens", headers=h)
    assert r.status_code == 200 and r.json()["revoked"] is True
    fresh = r.json()["token"]

    assert (await client.get("/invites/mine", headers=h)).status_code == 401  # old token now dead
    assert (await client.get("/invites/mine", headers={"X-Treg-Token": fresh})).status_code == 200  # fresh works


async def test_revoke_signs_out_other_browser_sessions_but_keeps_the_caller(client):
    code = (await client.post("/auth/email/start", json={"email": "multi@x.io"})).json()["dev_code"]
    verify = await client.post("/auth/email/verify", json={"email": "multi@x.io", "code": code})
    old_cookie = verify.cookies.get("treg_session")
    assert old_cookie

    # A second device holding the SAME (now-to-be-leaked) session cookie.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry",
                           cookies={"treg_session": old_cookie}) as device_b:
        assert (await device_b.get("/invites/mine")).status_code == 200  # both devices valid

        # Device A revokes (cookie-authed); the endpoint re-issues A a fresh cookie in the same response.
        assert (await client.post("/auth/revoke-tokens")).status_code == 200

        assert (await device_b.get("/invites/mine")).status_code == 401  # leaked session is out
        assert (await client.get("/invites/mine")).status_code == 200      # the caller stays in


async def test_revoke_requires_auth(client):
    assert (await client.post("/auth/revoke-tokens")).status_code == 401


def test_legacy_token_without_tv_claim_defaults_to_zero():
    """A token minted before the tv claim existed has no `tv` key. read_claims must treat it as tv=0
    so it still validates against a user whose token_version is 0 (no forced logout on deploy)."""
    raw = json.dumps({"uid": 7, "exp": 9999999999}).encode()  # note: no "tv"
    sig = hmac.new(sess._key(), raw, hashlib.sha256).digest()
    legacy = f"{sess._b64(raw)}.{sess._b64(sig)}"
    claims = sess.read_claims(legacy)
    assert claims == {"uid": 7, "exp": 9999999999, "tv": 0}
