"""OAuth freshness — treg owns keeping tokens alive. The injector stays dumb; this runs in the
api layer just before a call, and in the health runner.

An oauth secret is a SELF-REFRESHABLE blob:
    {access_token|token, refresh_token, expires_at|expiry, token_uri, client_id, client_secret}
`ensure_fresh` refreshes in place (re-encrypt + persist) when the token is stale. A single-flight
lock per secret id prevents a refresh stampede when many calls hit an expired token at once.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from . import crypto
from .models import PendingOAuth, Secret

_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SKEW = 60.0  # refresh this many seconds before actual expiry
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _expires_at(blob: dict) -> float | None:
    if "expires_at" in blob:
        try:
            return float(blob["expires_at"])
        except (TypeError, ValueError):
            return None
    if blob.get("expiry"):  # google authorized_user ISO format
        try:
            dt = datetime.fromisoformat(str(blob["expiry"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:  # a naive ISO expiry is UTC, not the server's local tz
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def is_refreshable(blob: dict) -> bool:
    """Auto mode: the blob carries everything to mint a new token. Otherwise it's MANUAL mode
    (user uploads a token and re-uploads when it expires) and treg just injects it as-is."""
    return all(blob.get(k) for k in ("refresh_token", "client_id", "client_secret"))


def is_stale(blob: dict, skew: float = _SKEW) -> bool:
    exp = _expires_at(blob)
    if exp is None:
        return True  # unknown expiry -> refresh to be safe
    return time.time() > exp - skew


async def refresh(blob: dict, client: httpx.AsyncClient) -> dict:
    """Exchange the refresh_token for a new access token. Returns an updated blob."""
    rt, cid, csec = blob.get("refresh_token"), blob.get("client_id"), blob.get("client_secret")
    if not (rt and cid and csec):
        raise ValueError("oauth secret missing refresh_token / client_id / client_secret")
    resp = await client.post(
        blob.get("token_uri", _DEFAULT_TOKEN_URI),
        data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid, "client_secret": csec},
    )
    resp.raise_for_status()
    tok = resp.json()
    access = tok.get("access_token")
    if not access:  # a 200 with an error-shaped body ({"error":"invalid_grant"}) — surface it clearly
        raise ValueError(f"token endpoint returned no access_token: {tok.get('error') or tok}")
    new = dict(blob)
    new["access_token"] = new["token"] = access  # update both common key names
    # Always stamp an expiry (fallback 1h). A provider that omits/nulls expires_in would otherwise
    # leave the token perpetually "unknown expiry" → is_stale True → a live refresh on EVERY call.
    new["expires_at"] = time.time() + float(tok.get("expires_in") or 3600)
    if tok.get("refresh_token"):  # providers may rotate the refresh token
        new["refresh_token"] = tok["refresh_token"]
    return new


# ---- connect flow (Phase C): mint the first token via browser consent --------------------
def consent_url(p: PendingOAuth) -> str:
    """The provider consent URL the user opens. access_type=offline + prompt=consent ensure a
    refresh_token comes back, so the credential lands in auto-refresh mode."""
    q = {
        "client_id": p.client_id,
        "redirect_uri": p.redirect_uri,
        "response_type": "code",
        "scope": p.scopes,
        "access_type": "offline",
        "prompt": "consent",
        "state": p.state,
    }
    return f"{p.auth_uri}?{urlencode(q)}"


async def exchange_code(p: PendingOAuth, code: str, client: httpx.AsyncClient) -> dict:
    """Trade the authorization code for tokens; return a self-refreshable oauth blob."""
    client_secret = crypto.decrypt(p.client_secret)
    resp = await client.post(
        p.token_uri,
        data={
            "code": code,
            "client_id": p.client_id,
            "client_secret": client_secret,
            "redirect_uri": p.redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    tok = resp.json()
    access = tok.get("access_token")
    if not access:  # a 200 with an error-shaped body — surface the provider's reason, not a KeyError
        raise ValueError(f"token endpoint returned no access_token: {tok.get('error') or tok}")
    return {
        "access_token": access,
        "token": access,
        "refresh_token": tok.get("refresh_token"),
        "client_id": p.client_id,
        "client_secret": client_secret,
        "token_uri": p.token_uri,
        "expires_at": time.time() + float(tok.get("expires_in") or 3600),
    }


async def ensure_fresh(secret: Secret, db: AsyncSession, client: httpx.AsyncClient) -> None:
    """If `secret` is a stale oauth credential, refresh + persist it before it's used. No-op for
    non-oauth kinds and for still-valid tokens. Raises on a failed refresh (caller decides)."""
    if secret.kind != "oauth":
        return
    blob = json.loads(crypto.decrypt(secret.value))
    if not is_refreshable(blob):
        return  # MANUAL mode — inject the uploaded token as-is (user manages freshness)
    if not is_stale(blob):
        return
    if len(_locks) > 512:  # bounded: drop idle (unheld) locks so the map can't grow without limit — a
        for k in [k for k, lk in list(_locks.items()) if not lk.locked()]:  # fresh lock is made on next need
            _locks.pop(k, None)
    async with _locks[secret.id]:
        await db.refresh(secret)  # another worker may have just refreshed it
        old_value = secret.value
        blob = json.loads(crypto.decrypt(old_value))
        if not is_stale(blob):
            return
        fresh = await refresh(blob, client)
        # Cross-process safety: the in-process lock only serializes THIS worker. Write conditionally
        # on the ciphertext we refreshed from, so a second worker that already rotated the
        # refresh_token can't be clobbered with our now-stale token; then reload whichever won.
        await db.execute(
            update(Secret).where(Secret.id == secret.id, Secret.value == old_value)
            .values(value=crypto.encrypt(json.dumps(fresh)))
        )
        await db.commit()
        await db.refresh(secret)  # adopt the winning blob (ours or the other worker's) for injection
