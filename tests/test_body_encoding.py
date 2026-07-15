"""Body-encoding escape hatch.

Some upstream edges (Cloudflare, incl. Render's) 403 a request whose body matches an injection
signature -- a skill recipe or a proxied `call` that legitimately carries SQL/HTML. A client can
base64/gzip-encode the body and mark it with `X-Treg-Body-Encoding`; the edge then sees only opaque
bytes, and the server decodes before any route reads it. This covers both the JSON endpoints
(POST /skills, parsed by Pydantic) and the /call proxy (which relays request.body() upstream).
"""

from __future__ import annotations

import base64
import gzip
import json

import pytest
from httpx import AsyncClient

from treg.api import _decode_request_body


# ---- the pure decoder --------------------------------------------------------------------
def test_decode_base64():
    assert _decode_request_body(base64.b64encode(b"SELECT 1"), "base64") == b"SELECT 1"


def test_decode_gzip():
    assert _decode_request_body(gzip.compress(b"DROP TABLE x"), "gzip") == b"DROP TABLE x"


def test_decode_base64_then_gzip():
    raw = b"UNION SELECT * FROM secrets"
    assert _decode_request_body(base64.b64encode(gzip.compress(raw)), "base64+gzip") == raw


def test_decode_unknown_step_raises():
    with pytest.raises(ValueError):
        _decode_request_body(b"x", "rot13")


# ---- POST /skills (Pydantic): the exact import path the WAF blocked ----------------------
async def test_skills_accepts_encoded_sql_recipe(clients: AsyncClient):
    recipe = "SELECT * FROM users WHERE 1=1; DROP TABLE students; -- WAF bait"
    payload = json.dumps({"name": "waf-recipe", "recipe": recipe, "secrets": [], "tools": []}).encode()
    r = await clients.post(
        "/skills", content=base64.b64encode(payload),
        headers={"X-Treg-Body-Encoding": "base64", "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    bid = next(b["id"] for b in (await clients.get("/bundles")).json() if b["name"] == "waf-recipe")
    assert (await clients.get(f"/bundles/{bid}")).json()["recipe"] == recipe  # stored decoded, verbatim


# ---- the /call proxy relays the DECODED body upstream ------------------------------------
async def test_proxy_relays_decoded_body_upstream(clients: AsyncClient):
    sid = (await clients.post("/secrets", json={"name": "wx-k", "value": "SEK"})).json()["id"]
    await clients.post("/tools", json={"name": "wx", "base_url": "https://api.wx.com", "secret_id": sid})
    sql = "INSERT INTO t VALUES ('a'); SELECT * FROM t WHERE x=1 OR 1=1"
    r = await clients.post(
        "/call/https://api.wx.com/echo", content=base64.b64encode(sql.encode()),
        headers={"X-Treg-Body-Encoding": "base64", "Content-Type": "text/plain"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["body"] == sql  # upstream saw the real body, not the base64 envelope


# ---- a malformed encoded body is a clean 400, not a 500 ----------------------------------
async def test_bad_encoding_is_400(clients: AsyncClient):
    r = await clients.post(
        "/skills", content=b"this is not gzip",
        headers={"X-Treg-Body-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text


# ---- no header = untouched (the common path) --------------------------------------------
async def test_plain_request_unaffected(clients: AsyncClient):
    r = await clients.post("/skills", json={"name": "plain-recipe", "recipe": "hello", "secrets": [], "tools": []})
    assert r.status_code == 200, r.text
