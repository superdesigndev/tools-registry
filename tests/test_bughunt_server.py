"""Regression tests for the sustained bug hunt (docs/BUGS.md) — server side.

One test (or small cluster) per fixed bug: email normalization, forgeable-session key, OTP
brute-force, suspended-account doors, invite/role boundaries, resource validation, bundle
integrity, proxy prefix-boundary, and OAuth freshness edge cases. Each fails on the pre-fix code.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import audit, crypto, oauth, session as sess
from treg.api import app
from treg.config import get_settings
from treg.db import reset_db, session_maker
from treg.models import Secret, User


def make_upstream() -> FastAPI:
    up = FastAPI()

    @up.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def echo(request: Request) -> dict:
        return {"path": request.url.path, "auth": request.headers.get("authorization")}

    return up


@pytest.fixture
async def c():
    """A bare, unauthenticated ASGI client against a fresh DB, with the upstream echo wired in."""
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://registry",
        headers={"ngrok-skip-browser-warning": "1"},
    ) as client:
        yield client
    await app.state.http.aclose()


async def _register(c: AsyncClient, email: str) -> dict:
    r = await c.post("/users", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()


async def _otp_login(c: AsyncClient, email: str) -> dict:
    code = (await c.post("/auth/email/start", json={"email": email})).json()["dev_code"]
    r = await c.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def _hdr(token: str) -> dict:
    return {"X-Treg-Token": token}


# ---- email normalization ------------------------------------------------------------------
async def test_email_case_is_one_identity(c):
    """`Bob@X.com` and `bob@x.com` are the same human — no duplicate user/org."""
    reg = await _register(c, "Bob@X.com")
    assert reg["email"] == "bob@x.com"  # stored normalized
    dup = await c.post("/users", json={"email": "bob@x.com"})
    assert dup.status_code == 409  # same identity, not a second user
    # OTP with yet another casing resolves to the SAME single membership.
    tok = (await _otp_login(c, "BOB@x.COM"))["token"]
    orgs = (await c.get("/orgs", headers=_hdr(tok))).json()
    assert len(orgs) == 1


async def test_invite_case_insensitive_accept(c):
    owner = await _register(c, "owner@team.com")
    inv = await c.post(f"/orgs/{owner['org_id']}/invites", headers=_hdr(owner["token"]),
                       json={"email": "Alice@Team.com", "role": "member"})
    assert inv.status_code == 200
    acc = await c.post("/invites/accept", json={"code": inv.json()["code"], "email": "alice@team.com"})
    assert acc.status_code == 200, acc.text  # was 403 before normalization


async def test_otp_start_verify_case_mismatch_ok(c):
    code = (await c.post("/auth/email/start", json={"email": "Neo@Matrix.io"})).json()["dev_code"]
    r = await c.post("/auth/email/verify", json={"email": "neo@matrix.io", "code": code})
    assert r.status_code == 200, r.text  # code minted under one casing, verified under another


# ---- session signing key ------------------------------------------------------------------
async def test_session_key_not_hardcoded_constant():
    """With no secret configured the fallback is a RANDOM per-process key, so a cookie forged with
    the old literal 'dev-session-key' is rejected while a legit token still round-trips."""
    s = get_settings()
    prev = (s.session_secret, s.secret_key)
    object.__setattr__(s, "session_secret", "")
    object.__setattr__(s, "secret_key", "")
    try:
        import base64, hashlib, hmac
        raw = json.dumps({"uid": 1, "exp": int(time.time()) + 999}, separators=(",", ":")).encode()
        sig = hmac.new(b"dev-session-key", raw, hashlib.sha256).digest()
        b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
        forged = f"{b(raw)}.{b(sig)}"
        assert sess.read(forged) is None  # the constant no longer signs anything
        assert sess.read(sess.make(7)) == 7  # real tokens still work
    finally:
        object.__setattr__(s, "session_secret", prev[0])
        object.__setattr__(s, "secret_key", prev[1])


# ---- OTP brute-force ----------------------------------------------------------------------
async def test_otp_code_dies_after_max_wrong_attempts(c):
    from treg.api import MAX_OTP_ATTEMPTS
    good = (await c.post("/auth/email/start", json={"email": "trinity@matrix.io"})).json()["dev_code"]
    for _ in range(MAX_OTP_ATTEMPTS):
        bad = await c.post("/auth/email/verify", json={"email": "trinity@matrix.io", "code": "000001"})
        assert bad.status_code == 401
    # code is now invalidated — even the CORRECT code no longer works
    r = await c.post("/auth/email/verify", json={"email": "trinity@matrix.io", "code": good})
    assert r.status_code == 401


# ---- suspended accounts at the doors ------------------------------------------------------
async def _suspend_user(email: str) -> None:
    async with session_maker() as db:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one()
        u.suspended = True
        await db.commit()


async def test_suspended_user_cannot_otp_login(c):
    await _otp_login(c, "banned@x.io")  # first login creates the user
    await _suspend_user("banned@x.io")
    code = (await c.post("/auth/email/start", json={"email": "banned@x.io"})).json()["dev_code"]
    r = await c.post("/auth/email/verify", json={"email": "banned@x.io", "code": code})
    assert r.status_code == 403  # was 200 (issued a live token to a banned account)


async def test_suspended_user_cannot_accept_invite(c):
    owner = await _register(c, "o2@team.com")
    await _otp_login(c, "ban2@x.io")
    await _suspend_user("ban2@x.io")
    inv = await c.post(f"/orgs/{owner['org_id']}/invites", headers=_hdr(owner["token"]),
                       json={"email": "ban2@x.io", "role": "member"})
    acc = await c.post("/invites/accept", json={"code": inv.json()["code"], "email": "ban2@x.io"})
    assert acc.status_code == 403


# ---- invite / role boundaries -------------------------------------------------------------
async def test_admin_cannot_invite_admin(c):
    owner = await _register(c, "boss@team.com")
    oid = owner["org_id"]
    # onboard an admin
    inv = await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]),
                       json={"email": "adm@team.com", "role": "admin"})
    assert inv.status_code == 200  # owner CAN mint an admin
    admin_tok = (await c.post("/invites/accept",
                              json={"code": inv.json()["code"], "email": "adm@team.com"})).json()["token"]
    # that admin tries to mint another admin — must be refused (owner-only)
    r = await c.post(f"/orgs/{oid}/invites", headers=_hdr(admin_tok),
                     json={"email": "adm2@team.com", "role": "admin"})
    assert r.status_code == 403
    # but may still invite a member
    ok = await c.post(f"/orgs/{oid}/invites", headers=_hdr(admin_tok),
                      json={"email": "mem@team.com", "role": "member"})
    assert ok.status_code == 200


async def test_cannot_invite_an_existing_member(c):
    owner = await _register(c, "own@team.io")
    oid = owner["org_id"]
    inv = await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]),
                       json={"email": "mem@team.io", "role": "member"})
    await c.post("/invites/accept", json={"code": inv.json()["code"], "email": "mem@team.io"})
    # re-inviting a current member is a dead-end (accept would 409) → reject up front
    again = await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]),
                         json={"email": "mem@team.io", "role": "member"})
    assert again.status_code == 409


async def test_reinvite_supersedes_prior_pending(c):
    owner = await _register(c, "own2@team.io")
    oid = owner["org_id"]
    h = _hdr(owner["token"])
    await c.post(f"/orgs/{oid}/invites", headers=h, json={"email": "p@team.io"})
    await c.post(f"/orgs/{oid}/invites", headers=h, json={"email": "p@team.io"})
    pending = (await c.get(f"/orgs/{oid}/invites", headers=h)).json()
    assert sum(1 for i in pending if i["email"] == "p@team.io") == 1  # exactly one live invite, not two


async def test_security_headers_on_api(c):
    r = await c.get("/meta")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"  # clickjacking protection for the authed dashboard
    assert "max-age" in r.headers.get("strict-transport-security", "")  # HSTS pins https


async def test_init_db_refuses_ephemeral_key_on_real_db():
    from treg import db as dbmod
    s = get_settings()
    prev = (s.secret_key, s.database_url)
    object.__setattr__(s, "secret_key", "")
    object.__setattr__(s, "database_url", "postgresql+asyncpg://u:p@host/db")
    try:
        with pytest.raises(RuntimeError, match="TREG_SECRET_KEY"):
            await dbmod.init_db()
    finally:
        object.__setattr__(s, "secret_key", prev[0])
        object.__setattr__(s, "database_url", prev[1])


async def test_health_all_orgs_requires_superadmin(c):
    u = await _register(c, "ha@t.io")
    assert (await c.post("/health/run?all_orgs=1", headers=_hdr(u["token"]))).status_code == 403
    await _make_superadmin("ha@t.io")
    assert (await c.post("/health/run?all_orgs=1", headers=_hdr(u["token"]))).status_code == 200


async def test_logout_rejects_cross_origin(c):
    assert (await c.post("/auth/logout", headers={"origin": "https://evil.example"})).status_code == 403
    assert (await c.post("/auth/logout")).status_code == 200  # no Origin (agent/CLI) is fine


async def test_oauth_start_rejects_foreign_redirect(c):
    u = await _register(c, "or@t.io")
    r = await c.post("/oauth/start", headers=_hdr(u["token"]),
                     json={"name": "s", "client_id": "c", "client_secret": "x", "redirect_uri": "https://evil.example/cb"})
    assert r.status_code == 422


async def test_proxy_response_is_sandboxed(c):
    u = await _register(c, "sb@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={"name": "t", "base_url": "http://upstream", "secret_id": sid})
    r = await c.get("/call/t/echo", headers=_hdr(tok))
    # a browser navigating to /call/… must not execute upstream HTML/JS under treg's origin
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("content-security-policy") == "sandbox"


async def test_upstream_setcookie_cannot_overwrite_treg_cookies(c):
    from starlette.responses import Response
    from httpx import ASGITransport, AsyncClient as AC
    evil = FastAPI()

    @evil.api_route("/{p:path}", methods=["GET"])
    async def h(p):  # an upstream that tries to plant treg's own cookies
        resp = Response(content=b"{}", media_type="application/json")
        resp.raw_headers.append((b"set-cookie", b"treg_session=EVIL; Path=/"))
        resp.raw_headers.append((b"set-cookie", b"good=1; Path=/"))
        return resp

    app.state.http = AC(transport=ASGITransport(app=evil), base_url="http://upstream")
    u = await _register(c, "ck@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={"name": "ck", "base_url": "http://upstream", "secret_id": sid})
    r = await c.get("/call/ck/x", headers=_hdr(tok))
    setc = r.headers.get_list("set-cookie")
    assert not any("treg_session" in x for x in setc)  # our session cookie is NOT overwritable by an upstream
    assert any("good=1" in x for x in setc)             # other upstream cookies still pass through


async def test_oversized_id_is_404_not_500(c):
    u = await _register(c, "big@t.io")
    r = await c.delete("/secrets/99999999999999999999999999", headers=_hdr(u["token"]))
    assert r.status_code == 404  # was an OverflowError 500 (huge id overflows SQLite's 64-bit INTEGER)


async def test_malformed_base_url_is_422(c):
    u = await _register(c, "url@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    r = await c.post("/tools", headers=_hdr(u["token"]),
                     json={"name": "t", "base_url": "http://[::1", "secret_id": sid})
    assert r.status_code == 422  # unbalanced IPv6 bracket → 422, not urlsplit 500


async def test_binding_null_name_rejected(c):
    u = await _register(c, "nn@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    r = await c.post("/tools", headers=_hdr(u["token"]), json={
        "name": "t", "base_url": "http://upstream",
        "bindings": [{"secret_id": sid, "injector": "env", "name": None}]})
    assert r.status_code == 422  # None name → AttributeError 500 at call otherwise


async def test_duplicate_query_binding_names_rejected(c):
    u = await _register(c, "qq@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    r = await c.post("/tools", headers=_hdr(u["token"]), json={
        "name": "t", "base_url": "http://upstream",
        "bindings": [
            {"secret_id": sid, "injector": "env", "location": "query", "name": "key"},
            {"secret_id": sid, "injector": "env", "location": "query", "name": "key"},
        ]})
    assert r.status_code == 422  # the second would silently drop the first at call time


async def test_non_string_token_field_is_502_not_garbage(c):
    import json as J
    u = await _register(c, "nsf@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok),
                        json={"name": "k", "value": J.dumps({"access_token": {"nested": 1}}), "kind": "secret_file"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={
        "name": "t", "base_url": "http://upstream", "secret_id": sid,
        "injector": "secret_file", "secret_field": "access_token"})
    r = await c.get("/call/t/echo", headers=_hdr(tok))
    assert r.status_code == 502  # a dict-valued field is a clear error, not garbage injected as the token


async def test_health_run_survives_injection_error(c):
    import json as J
    u = await _register(c, "hi@t.io")
    tok = u["token"]
    # env (plaintext) secret bound with secret_file injector → _token_from_json raises at probe time
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "not-json", "kind": "env"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={
        "name": "t", "base_url": "http://upstream", "secret_id": sid,
        "injector": "secret_file", "health_check": {"path": "/echo"}})
    r = await c.post("/health/run", headers=_hdr(tok))
    assert r.status_code == 200  # the injection error becomes an "invalid" verdict, not a 500


# ---- cycle-3 regressions ------------------------------------------------------------------
async def test_duplicate_header_binding_names_rejected(c):
    u = await _register(c, "hd@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    r = await c.post("/tools", headers=_hdr(u["token"]), json={
        "name": "t", "base_url": "http://upstream", "bindings": [
            {"secret_id": sid, "injector": "env", "location": "header", "name": "Authorization"},
            {"secret_id": sid, "injector": "env", "location": "header", "name": "authorization"},  # case-insensitive dup
        ]})
    assert r.status_code == 422  # the second would silently overwrite the first at call time


async def test_accept_my_invite_returns_usable_token(c):
    owner = await _register(c, "ai-o@t.io")
    oid = owner["org_id"]
    await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]), json={"email": "ai-m@t.io"})
    joiner = await _otp_login(c, "ai-m@t.io")  # identity token (proven email)
    mine = (await c.get("/invites/mine", headers=_hdr(joiner["token"]))).json()
    r = await c.post(f"/invites/{mine[0]['id']}/accept", headers=_hdr(joiner["token"]))
    assert r.status_code == 200 and r.json().get("token")  # returns a real org-scoped token


async def test_update_secret_kind_requires_json_for_oauth(c):
    u = await _register(c, "uk@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "not-json", "kind": "env"})).json()["id"]
    r = await c.patch(f"/secrets/{sid}", headers=_hdr(tok), json={"kind": "oauth"})
    assert r.status_code == 422  # oauth kind on a non-JSON value would 502 at call time


async def test_unbound_secret_health_resets_to_unknown(c):
    u = await _register(c, "ub@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    t = await c.post("/tools", headers=_hdr(tok), json={
        "name": "t", "base_url": "http://upstream", "secret_id": sid, "health_check": {"path": "/x", "expect_status": 999}})
    await c.post("/health/run", headers=_hdr(tok))
    assert (await c.get("/health", headers=_hdr(tok))).json()[0]["status"] == "invalid"
    await c.delete(f"/tools/{t.json()['id']}", headers=_hdr(tok))  # unbind the secret
    await c.post("/health/run", headers=_hdr(tok))
    assert (await c.get("/health", headers=_hdr(tok))).json()[0]["status"] == "unknown"  # stale verdict cleared


async def test_bad_binding_format_rejected_at_create(c):
    u = await _register(c, "fmt@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    for bad in ("Bearer {secret} {oops}", "tok{"):  # extra placeholder / literal brace → 500 at call
        r = await c.post("/tools", headers=_hdr(u["token"]),
                         json={"name": f"t{bad[:3]}", "base_url": "http://upstream", "secret_id": sid, "auth_format": bad})
        assert r.status_code == 422, bad
    ok = await c.post("/tools", headers=_hdr(u["token"]),
                      json={"name": "good", "base_url": "http://upstream", "secret_id": sid, "auth_format": "Bearer {secret}"})
    assert ok.status_code == 200


async def test_numeric_slug_resolves_as_slug_not_id():
    from treg.api import _resolve_org
    from treg.models import Org
    await reset_db()
    async with session_maker() as db:
        filler = Org(name="filler", slug="filler")
        db.add(filler)
        await db.flush()
        numeric = Org(name="2024", slug="2024")
        db.add(numeric)
        await db.commit()
        got = await _resolve_org("2024", db)
        assert got is not None and got.slug == "2024"  # resolved by slug, not as id 2024


# ---- resource validation ------------------------------------------------------------------
async def test_unknown_injector_rejected_at_create(c):
    u = await _register(c, "dev@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    r = await c.post("/tools", headers=_hdr(u["token"]),
                     json={"name": "bad", "base_url": "http://upstream", "secret_id": sid, "injector": "bogus"})
    assert r.status_code == 422  # was 200 then 500 at call time


async def test_skill_poison_binding_rejected(c):
    u = await _register(c, "dev2@t.io")
    r = await c.post("/skills", headers=_hdr(u["token"]), json={
        "name": "s",
        "tools": [{"name": "t", "base_url": "http://upstream",
                   "bindings": [{"injector": "env", "location": "header", "name": "X", "format": "{secret}"}]}],
    })
    assert r.status_code == 422  # binding has no secret → would 500 on every call


async def test_update_secret_null_name_is_422(c):
    u = await _register(c, "dev3@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    r = await c.patch(f"/secrets/{sid}", headers=_hdr(u["token"]), json={"name": None})
    assert r.status_code == 422


async def test_update_tool_null_base_url_is_422(c):
    u = await _register(c, "dev4@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    tid = (await c.post("/tools", headers=_hdr(u["token"]),
                        json={"name": "t", "base_url": "http://upstream", "secret_id": sid})).json()["id"]
    r = await c.patch(f"/tools/{tid}", headers=_hdr(u["token"]), json={"base_url": None})
    assert r.status_code == 422


async def test_update_secret_value_resets_health(c):
    u = await _register(c, "dev5@t.io")
    sid = (await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v"})).json()["id"]
    async with session_maker() as db:  # pretend a health run marked it ok
        s = await db.get(Secret, sid)
        s.health_status = "ok"
        await db.commit()
    await c.patch(f"/secrets/{sid}", headers=_hdr(u["token"]), json={"value": "rotated"})
    health = (await c.get("/health", headers=_hdr(u["token"]))).json()
    assert health[0]["status"] == "unknown"  # rotation invalidates the stale green


async def test_foreign_bundle_id_rejected(c):
    u = await _register(c, "dev6@t.io")
    r = await c.post("/secrets", headers=_hdr(u["token"]), json={"name": "k", "value": "v", "bundle_id": 999})
    assert r.status_code == 422


async def test_delete_bundle_refuses_to_orphan_shared_secret(c):
    u = await _register(c, "dev7@t.io")
    tok = u["token"]
    bundle = await c.post("/skills", headers=_hdr(tok), json={
        "name": "b",
        "secrets": [{"local_name": "key", "value": "v"}],
        "tools": [{"name": "t1", "base_url": "http://upstream", "bindings": [{"secret": "key"}]}],
    })
    assert bundle.status_code == 200, bundle.text
    sid = bundle.json()["secrets"][0]["id"]
    bid = bundle.json()["id"]
    # a tool OUTSIDE the bundle binds the same secret
    await c.post("/tools", headers=_hdr(tok),
                 json={"name": "t2", "base_url": "http://upstream", "secret_id": sid})
    r = await c.delete(f"/bundles/{bid}", headers=_hdr(tok))
    assert r.status_code == 409  # would otherwise dangle t2's binding


# ---- proxy resolution ---------------------------------------------------------------------
async def test_prefix_match_respects_path_boundary(c):
    u = await _register(c, "dev8@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok),
                 json={"name": "v1", "base_url": "http://upstream/v1", "secret_id": sid})
    # only /v1 is registered; /v10 must NOT match it
    r = await c.get("/call/http://upstream/v10/echo", headers=_hdr(tok))
    assert r.status_code == 404


async def test_trailing_slash_duplicate_is_ambiguous_409(c):
    u = await _register(c, "dev9@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok),
                 json={"name": "a", "base_url": "http://upstream/v1", "secret_id": sid})
    await c.post("/tools", headers=_hdr(tok),
                 json={"name": "b", "base_url": "http://upstream/v1/", "secret_id": sid})
    r = await c.get("/call/http://upstream/v1/echo", headers=_hdr(tok))
    assert r.status_code == 409


async def test_kind_injector_mismatch_is_502_not_500(c):
    u = await _register(c, "dev10@t.io")
    tok = u["token"]
    # a plaintext (non-JSON) secret bound with the oauth injector → _token_from_json raises at call
    sid = (await c.post("/secrets", headers=_hdr(tok),
                        json={"name": "k", "value": "not-json", "kind": "env"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok),
                 json={"name": "t", "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    r = await c.get("/call/t/echo", headers=_hdr(tok))
    assert r.status_code == 502  # was an unhandled 500


# ---- OAuth freshness edges ----------------------------------------------------------------
async def test_refresh_stamps_expiry_when_provider_omits_expires_in():
    async def fake_post(url, data=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"access_token": "NEW"}  # no expires_in
        return R()
    client = type("X", (), {"post": staticmethod(fake_post)})()
    blob = {"refresh_token": "RT", "client_id": "c", "client_secret": "s", "token_uri": "http://x/token"}
    new = await oauth.refresh(blob, client)
    assert new["expires_at"] > time.time() + 60  # a fallback expiry was stamped, not left unknown


async def test_refresh_null_expires_in_does_not_crash():
    async def fake_post(url, data=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"access_token": "NEW", "expires_in": None}
        return R()
    client = type("X", (), {"post": staticmethod(fake_post)})()
    blob = {"refresh_token": "RT", "client_id": "c", "client_secret": "s", "token_uri": "http://x/token"}
    new = await oauth.refresh(blob, client)  # float(None) would have raised TypeError
    assert new["access_token"] == "NEW"


async def test_refresh_missing_access_token_raises_clear_error():
    async def fake_post(url, data=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"error": "invalid_grant"}
        return R()
    client = type("X", (), {"post": staticmethod(fake_post)})()
    blob = {"refresh_token": "RT", "client_id": "c", "client_secret": "s", "token_uri": "http://x/token"}
    with pytest.raises(ValueError, match="invalid_grant"):
        await oauth.refresh(blob, client)


def test_expires_at_treats_naive_iso_as_utc():
    from datetime import datetime, timezone
    naive = "2026-07-03T10:00:00"
    got = oauth._expires_at({"expiry": naive})
    want = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    assert got == want


# ---- second-pass regressions --------------------------------------------------------------
async def test_cannot_lock_out_the_last_superadmin(c):
    admin_tok = (await _otp_login(c, "sole-admin@x.io"))["token"]
    await _make_superadmin("sole-admin@x.io")
    users = (await c.get("/admin/users", headers=_hdr(admin_tok))).json()
    uid = next(u["id"] for u in users if u["email"] == "sole-admin@x.io")
    # self-suspend / self-demote / self-delete of the ONLY active superadmin must all be refused
    assert (await c.post(f"/admin/users/{uid}/suspend", headers=_hdr(admin_tok), json={"value": True})).status_code == 409
    assert (await c.post(f"/admin/users/{uid}/superadmin", headers=_hdr(admin_tok), json={"value": False})).status_code == 409
    assert (await c.delete(f"/admin/users/{uid}", headers=_hdr(admin_tok))).status_code == 409
    # with a SECOND superadmin, demoting the first is fine again
    other = (await _otp_login(c, "second-admin@x.io"))["token"]
    await _make_superadmin("second-admin@x.io")
    assert (await c.post(f"/admin/users/{uid}/superadmin", headers=_hdr(admin_tok), json={"value": False})).status_code == 200


async def test_huge_expires_days_does_not_500(c):
    owner = await _register(c, "ed@team.io")
    r = await c.post(f"/orgs/{owner['org_id']}/invites", headers=_hdr(owner["token"]),
                     json={"email": "x@team.io", "expires_days": 3_000_000})
    assert r.status_code == 200  # clamped, not an OverflowError 500


async def test_revoke_only_pending_invites(c):
    owner = await _register(c, "rv@team.io")
    oid = owner["org_id"]
    inv = await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]), json={"email": "rvm@team.io"})
    await c.post("/invites/accept", json={"code": inv.json()["code"], "email": "rvm@team.io"})
    # the invite is now accepted; find its id and try to "revoke" it
    async with session_maker() as db:
        from treg.models import Invite
        acc = (await db.execute(select(Invite).where(Invite.email == "rvm@team.io"))).scalars().all()
        inv_id = acc[0].id
    r = await c.delete(f"/orgs/{oid}/invites/{inv_id}", headers=_hdr(owner["token"]))
    assert r.status_code == 404  # can't revoke an already-accepted invite


async def test_health_run_does_not_renotify_unevaluated_invalid(c):
    u = await _register(c, "hn@x.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    async with session_maker() as db:  # mark invalid in a past run; no tool binds it now
        s = await db.get(Secret, sid)
        s.health_status = "invalid"
        await db.commit()
    r = (await c.post("/health/run", headers=_hdr(tok))).json()
    assert r["invalid"] == []  # not re-reported/notified — it wasn't evaluated this run


async def test_register_rejects_internal_webhook(c):
    r = await c.post("/users", json={"email": "ssrf@x.io", "webhook_url": "http://169.254.169.254/latest/meta-data"})
    assert r.status_code == 422


def test_safe_webhook_url_blocks_internal():
    from treg.health import safe_webhook_url
    assert safe_webhook_url("https://hooks.example.com/x") is True
    assert safe_webhook_url("http://169.254.169.254/") is False   # link-local (cloud metadata)
    assert safe_webhook_url("http://127.0.0.1/") is False          # loopback
    assert safe_webhook_url("http://10.0.0.5/") is False           # private
    assert safe_webhook_url("http://localhost/") is False
    assert safe_webhook_url("ftp://example.com/") is False         # non-http(s)
    assert safe_webhook_url(None) is False


async def test_oauth_callback_replay_keeps_done(c):
    from treg.models import PendingOAuth
    async with session_maker() as db:
        db.add(PendingOAuth(org_id=1, state="ST123", name="s", owner="o@x.io", client_id="c",
                            client_secret=crypto_encrypt("sec"), auth_uri="a", token_uri="t",
                            redirect_uri="r", status="done", secret_id=5, detail="connected"))
        await db.commit()
    r = await c.get("/oauth/callback", params={"state": "ST123", "code": "spent"})
    assert r.status_code == 200 and "Connected" in r.text  # replay returns the terminal "done" page
    async with session_maker() as db:
        p = (await db.execute(select(PendingOAuth).where(PendingOAuth.state == "ST123"))).scalar_one()
        assert p.status == "done"  # a re-load did NOT flip it to error


def crypto_encrypt(v):
    from treg import crypto
    return crypto.encrypt(v)


def test_prune_handshakes_evicts_stale():
    from datetime import timedelta
    import treg.api as api
    old = api._utcnow_naive() - timedelta(seconds=api.HANDSHAKE_TTL + 60)
    api._cli_states["stale"] = ("lid", old)
    api._cli_results["lidX"] = ({"token": "T"}, old)
    api._cli_pending["lidP"] = ("CODE", 8, old)  # (pairing_code, attempts_left, created_at)
    api._prune_handshakes()
    assert "stale" not in api._cli_states and "lidX" not in api._cli_results and "lidP" not in api._cli_pending


# ---- more invite / admin / health coverage ------------------------------------------------
async def _make_superadmin(email: str) -> None:
    async with session_maker() as db:
        u = (await db.execute(select(User).where(User.email == email))).scalar_one()
        u.is_superadmin = True
        await db.commit()


async def test_deleting_sole_owner_promotes_a_survivor(c):
    owner = await _register(c, "solo@team.com")
    oid = owner["org_id"]
    inv = await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]),
                       json={"email": "member@team.com", "role": "member"})
    await c.post("/invites/accept", json={"code": inv.json()["code"], "email": "member@team.com"})
    # a superadmin deletes the sole owner
    admin_tok = (await _otp_login(c, "root@site.io"))["token"]
    await _make_superadmin("root@site.io")
    owner_uid = (await c.get(f"/admin/orgs/{oid}", headers=_hdr(admin_tok))).json()  # sanity: reachable
    assert owner_uid["id"] == oid
    # find the owner's user id
    users = (await c.get("/admin/users", headers=_hdr(admin_tok))).json()
    owner_id = next(u["id"] for u in users if u["email"] == "solo@team.com")
    r = await c.delete(f"/admin/users/{owner_id}", headers=_hdr(admin_tok))
    assert r.status_code == 200
    # the org still exists and now has an owner (the promoted survivor)
    detail = (await c.get(f"/admin/orgs/{oid}", headers=_hdr(admin_tok))).json()
    assert any(m["role"] == "owner" for m in detail["members"])


async def _suspend_org(oid: int) -> None:
    from treg.models import Org
    async with session_maker() as db:
        o = await db.get(Org, oid)
        o.suspended = True
        await db.commit()


async def test_suspended_org_hides_and_blocks_invites(c):
    owner = await _register(c, "team-owner@x.io")
    oid = owner["org_id"]
    inv = await c.post(f"/orgs/{oid}/invites", headers=_hdr(owner["token"]),
                       json={"email": "joiner@x.io", "role": "member"})
    code = inv.json()["code"]
    joiner_tok = (await _otp_login(c, "joiner@x.io"))["token"]
    await _suspend_org(oid)
    # code-free view no longer lists the suspended org
    mine = (await c.get("/invites/mine", headers=_hdr(joiner_tok))).json()
    assert all(i["org_id"] != oid for i in mine)
    # neither door lets you join it
    assert (await c.post("/invites/accept", json={"code": code, "email": "joiner@x.io"})).status_code == 403


async def test_health_probe_5xx_is_unknown_not_invalid(c):
    from starlette.responses import Response
    from httpx import ASGITransport, AsyncClient as AC
    down = FastAPI()

    @down.api_route("/{p:path}", methods=["GET"])
    async def h(p):
        return Response(status_code=503)

    app.state.http = AC(transport=ASGITransport(app=down), base_url="http://upstream")
    u = await _register(c, "p5@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={
        "name": "t", "base_url": "http://upstream", "secret_id": sid, "health_check": {"path": "/ping"}})
    await c.post("/health/run", headers=_hdr(tok))
    got = (await c.get("/health", headers=_hdr(tok))).json()
    assert got[0]["status"] == "unknown"  # a 503 = upstream trouble, not a bad credential


async def test_health_worst_status_wins_for_shared_secret(c):
    import json as J
    u = await _register(c, "ws@t.io")
    tok = u["token"]
    blob = J.dumps({"access_token": "AT"})  # manual oauth (no refresh fields) → ensure_fresh no-ops
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": blob, "kind": "oauth"})).json()["id"]
    # tool A: probe forced to fail (echo returns 200, expect 999); tool B: same secret, no probe
    await c.post("/tools", headers=_hdr(tok), json={"name": "a", "base_url": "http://upstream", "secret_id": sid,
                 "injector": "oauth", "health_check": {"path": "/x", "expect_status": 999}})
    await c.post("/tools", headers=_hdr(tok), json={"name": "b", "base_url": "http://upstream", "secret_id": sid,
                 "injector": "oauth"})
    await c.post("/health/run", headers=_hdr(tok))
    got = (await c.get("/health", headers=_hdr(tok))).json()
    assert got[0]["status"] == "invalid"  # B's no-probe 'ok' must not overwrite A's probe failure


async def test_failed_call_is_audited(c):
    u = await _register(c, "aud@t.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={"name": "t", "base_url": "http://upstream", "secret_id": sid})
    async with session_maker() as db:  # dangle the binding so the call fails with 409
        s = await db.get(Secret, sid)
        await db.delete(s)
        await db.commit()
    r = await c.get("/call/t/x", headers=_hdr(tok))
    assert r.status_code == 409
    await audit.drain()  # flush the fire-and-forget write
    calls = (await c.get("/calls", headers=_hdr(tok))).json()
    assert any(cc["tool_name"] == "t" and cc["status_code"] == 409 for cc in calls)  # failed attempt is recorded


async def test_expired_oauth_pending_rejected(c):
    from treg.models import PendingOAuth
    from datetime import datetime, timedelta, timezone
    async with session_maker() as db:
        db.add(PendingOAuth(org_id=1, state="OLD", name="s", owner="o@x.io", client_id="c",
                            client_secret=crypto.encrypt("x"), auth_uri="a", token_uri="t",
                            redirect_uri="r", status="pending",
                            created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)))
        await db.commit()
    r = await c.get("/oauth/callback", params={"state": "OLD", "code": "z"})
    assert r.status_code == 400 and "expired" in r.text


async def test_skill_duplicate_local_name_rejected(c):
    u = await _register(c, "dsk@t.io")
    r = await c.post("/skills", headers=_hdr(u["token"]), json={
        "name": "s", "secrets": [{"local_name": "k", "value": "a"}, {"local_name": "k", "value": "b"}], "tools": []})
    assert r.status_code == 422


async def test_auth_me_resolves_a_token(c):
    """The dashboard's token door needs its own email (isPersonal / join-by-code); /auth/me now
    answers for a token, not just a session cookie."""
    reg = await _register(c, "whoami@x.io")
    r = await c.get("/auth/me", headers=_hdr(reg["token"]))
    assert r.status_code == 200 and r.json()["email"] == "whoami@x.io"


async def test_health_run_survives_dangling_binding(c):
    u = await _register(c, "hz@x.io")
    tok = u["token"]
    sid = (await c.post("/secrets", headers=_hdr(tok), json={"name": "k", "value": "v"})).json()["id"]
    await c.post("/tools", headers=_hdr(tok), json={
        "name": "t", "base_url": "http://upstream", "secret_id": sid,
        "health_check": {"path": "/echo"},
    })
    async with session_maker() as db:  # force a dangling binding (secret gone, tool still binds it)
        s = await db.get(Secret, sid)
        await db.delete(s)
        await db.commit()
    r = await c.post("/health/run", headers=_hdr(tok))
    assert r.status_code == 200  # was a 500 from a KeyError in _probe
