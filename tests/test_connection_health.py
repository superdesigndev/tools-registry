"""Milestone 2 — expiry as a first-class signal.

The failure this exists to prevent: a credential treg cannot renew itself (LinkedIn issues no
refresh_token at the non-partner tier) probes perfectly healthy right up until it abruptly stops
working. `health_status` cannot express that — it answers "does this work", and the honest answer
is yes, right up to the end. So expiry is swept separately and surfaced as `needs_reconnect`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import crypto, health, oauth
from treg.config import get_settings
from treg.db import session_maker
from treg.models import Secret


@pytest.fixture
def treg_google_app(monkeypatch):
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "treg-google-cid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "treg-google-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

BYO = {
    "name": "conn", "client_id": "cid", "client_secret": "csec",
    "auth_uri": "http://provider/auth", "token_uri": "http://upstream/token",
    "scopes": ["s"],
}


async def _connect(clients: AsyncClient, **over) -> int:
    body = {**BYO, **over}
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    return (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]


async def _make_non_refreshable(secret_id: int, expires_in_days: float) -> None:
    """Strip the refresh_token — the LinkedIn shape — and set an explicit expiry."""
    async with session_maker() as db:
        s = (await db.execute(select(Secret).where(Secret.id == secret_id))).scalars().one()
        blob = json.loads(crypto.decrypt(s.value))
        blob.pop("refresh_token", None)
        s.value = crypto.encrypt(json.dumps(blob))
        s.expires_at = datetime.now() + timedelta(days=expires_in_days)
        await db.commit()


# ---- the core distinction ----------------------------------------------------------------
async def test_a_dying_credential_is_flagged_even_though_health_is_not_invalid(clients: AsyncClient):
    """The whole point: status stays benign, needs_reconnect goes true."""
    sid = await _connect(clients)
    await _make_non_refreshable(sid, expires_in_days=2)

    rows = {r["secret_id"]: r for r in (await clients.get("/health")).json()}
    row = rows[sid]
    assert row["status"] != "invalid", "the credential still works — health must not cry wolf"
    assert row["needs_reconnect"] is True
    assert row["expiry_state"] == "expiring"
    assert row["refreshable"] is False


async def test_a_refreshable_credential_never_asks_for_reconnect(clients: AsyncClient):
    """treg renews these itself; nagging about their 1-hour expiry would be pure noise."""
    sid = await _connect(clients)
    rows = {r["secret_id"]: r for r in (await clients.get("/health")).json()}
    assert rows[sid]["refreshable"] is True
    assert rows[sid]["needs_reconnect"] is False
    assert rows[sid]["expiry_state"] == "fresh"


async def test_expired_non_refreshable_is_flagged(clients: AsyncClient):
    sid = await _connect(clients)
    await _make_non_refreshable(sid, expires_in_days=-1)
    rows = {r["secret_id"]: r for r in (await clients.get("/health")).json()}
    assert rows[sid]["expiry_state"] == "expired"
    assert rows[sid]["needs_reconnect"] is True


# ---- the sweep sees what the probe cannot ------------------------------------------------
async def test_health_run_reports_expiring_even_when_unbound(clients: AsyncClient):
    """An unbound credential is never probed — but it can still be days from dying."""
    sid = await _connect(clients)  # BYO connect provisions no tool, so nothing binds it
    await _make_non_refreshable(sid, expires_in_days=3)

    out = (await clients.post("/health/run")).json()
    expiring_ids = {r["secret_id"] for r in out.get("expiring", [])}
    assert sid in expiring_ids, "the expiry sweep must cover secrets no probe touches"


async def test_healthy_long_lived_credential_is_not_reported(clients: AsyncClient):
    sid = await _connect(clients)
    await _make_non_refreshable(sid, expires_in_days=90)
    out = (await clients.post("/health/run")).json()
    assert sid not in {r["secret_id"] for r in out.get("expiring", [])}


# ---- surfaced on the connection itself ---------------------------------------------------
async def test_connections_carry_needs_reconnect(clients: AsyncClient):
    sid = await _connect(clients)
    await _make_non_refreshable(sid, expires_in_days=1)
    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[sid]["needs_reconnect"] is True


# ---- refresh failures are recorded, not swallowed -----------------------------------------
async def test_a_failed_refresh_records_why(clients: AsyncClient, treg_google_app):
    """A revoked refresh_token used to 401 forever with nothing on the connection explaining it.

    Uses a registry connect so the credential is BOUND to an auto-provisioned tool — the health
    run only attempts a refresh for credentials some tool actually uses."""
    sid = await _connect(clients, provider="google-search-console", name="google-search-console")
    async with session_maker() as db:
        s = (await db.execute(select(Secret).where(Secret.id == sid))).scalars().one()
        blob = json.loads(crypto.decrypt(s.value))
        blob["token_uri"] = "http://upstream/token-broken"  # upstream 404s → refresh raises
        blob["expires_at"] = 0  # force staleness
        s.value = crypto.encrypt(json.dumps(blob))
        await db.commit()

    await clients.post("/health/run")

    conns = {c["id"]: c for c in (await clients.get("/connections")).json()}
    assert conns[sid]["last_error"], "the reason a renewal failed must be visible on the connection"


def test_expiry_state_boundaries():
    now = datetime(2026, 7, 21)
    assert oauth.expiry_state(now + timedelta(days=oauth.EXPIRING_SOON_DAYS - 1), False, now) == "expiring"
    assert oauth.expiry_state(now + timedelta(days=oauth.EXPIRING_SOON_DAYS + 1), False, now) == "fresh"


def test_needs_reconnect_ignores_non_oauth_secrets():
    s = Secret(name="k", kind="env", value=crypto.encrypt("plain"))
    assert health.needs_reconnect(s) is False
