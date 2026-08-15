"""Intercom Messenger wiring — OFF by default, secret never leaves the server.

Like analytics.py, the contract is mostly negative space: with no TREG_INTERCOM_APP_ID the widget
must not exist anywhere (/meta serves "", the pages' loaders stay inert), and the identity-
verification secret must never appear in any response — only the derived user_hash does. The markup
pins keep the web pages honest: the loader must key off /meta, never a hardcoded workspace id, or
every self-hosted deployment would boot chat into OUR workspace.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path

from treg.config import get_settings

WEB = Path(__file__).resolve().parents[1] / "src" / "treg" / "web"


async def test_meta_off_by_default(clients):
    body = (await clients.get("/meta")).json()
    assert body["intercom_app_id"] == ""


async def test_meta_exposes_app_id_when_configured_and_never_the_secret(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "intercom_app_id", "ic_test_id", raising=False)
    monkeypatch.setattr(get_settings(), "intercom_secret", "super-secret", raising=False)
    r = await clients.get("/meta")
    assert r.json()["intercom_app_id"] == "ic_test_id"
    assert "super-secret" not in r.text


async def test_auth_me_has_no_hash_when_unconfigured(clients):
    body = (await clients.get("/auth/me")).json()
    assert "intercom_user_hash" not in body


async def test_auth_me_user_hash_is_hmac_of_the_email(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "intercom_secret", "s3cret", raising=False)
    r = await clients.get("/auth/me")
    body = r.json()
    # independently recomputed: HMAC-SHA256 of the identifier the Messenger boots with (the email)
    want = hmac.new(b"s3cret", body["email"].encode(), hashlib.sha256).hexdigest()
    assert body["intercom_user_hash"] == want
    assert "s3cret" not in r.text  # the secret itself never leaves the server


def test_pages_gate_on_meta_app_id():
    # each Intercom-bearing page keys its loader off /meta's intercom_app_id (empty = inert)
    for page in ("index.html", "landing.html", "support.html"):
        assert "intercom_app_id" in (WEB / page).read_text(), page


def test_no_hardcoded_workspace_id_anywhere():
    # the only allowed form of the widget URL is the config-driven concatenation
    pat = re.compile(r"widget\.intercom\.io/widget/(?!'\+app)")
    for f in WEB.rglob("*"):
        if f.is_file() and f.suffix in {".html", ".js", ".md", ".txt"}:
            assert not pat.search(f.read_text()), f"hardcoded Intercom workspace id in {f.name}"
