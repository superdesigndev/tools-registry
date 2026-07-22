"""OAuth freshness — treg owns keeping tokens alive. The injector stays dumb; this runs in the
api layer just before a call, and in the health runner.

An oauth secret is a SELF-REFRESHABLE blob:
    {access_token|token, refresh_token, expires_at|expiry, token_uri, client_id, client_secret}
`ensure_fresh` refreshes in place (re-encrypt + persist) when the token is stale. A single-flight
lock per secret id prevents a refresh stampede when many calls hit an expired token at once.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
    # TikTok's token endpoint reads `client_key`; the blob records that at exchange time so a
    # refresh months later still speaks the dialect the grant was minted with.
    cid_param = blob.get("client_id_param") or "client_id"
    resp = await client.post(
        blob.get("token_uri", _DEFAULT_TOKEN_URI),
        data={"grant_type": "refresh_token", "refresh_token": rt, cid_param: cid, "client_secret": csec},
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
def pkce_challenge(verifier: str) -> str:
    """S256 challenge for a PKCE verifier (base64url, no padding)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def consent_url(p: PendingOAuth) -> str:
    """The provider consent URL the user opens.

    access_type=offline + prompt=consent are Google's way of guaranteeing a refresh_token, so the
    credential lands in auto-refresh mode. Providers that want different parameters carry them on
    the registry entry (`auth_params`), which replaces these defaults entirely."""
    q = {
        # TikTok reads `client_key`; everyone else reads the OAuth2 `client_id`.
        getattr(p, "client_id_param", "") or "client_id": p.client_id,
        "redirect_uri": p.redirect_uri,
        "response_type": "code",
        # `scopes` is stored in the provider's own delimiter (space, or comma for TikTok), so it
        # goes onto the URL verbatim — re-joining here would undo that.
        "scope": p.scopes,
        "state": p.state,
    }
    q.update(json.loads(p.auth_params) if p.auth_params else {"access_type": "offline", "prompt": "consent"})
    if p.code_verifier:  # PKCE — X rejects an authorization code exchanged without it
        q["code_challenge"] = pkce_challenge(p.code_verifier)
        q["code_challenge_method"] = "S256"
    return f"{p.auth_uri}?{urlencode(q)}"


async def exchange_code(p: PendingOAuth, code: str, client: httpx.AsyncClient) -> dict:
    """Trade the authorization code for tokens; return a self-refreshable oauth blob."""
    client_secret = crypto.decrypt(p.client_secret)
    cid_param = getattr(p, "client_id_param", "") or "client_id"
    data = {
        "code": code,
        cid_param: p.client_id,
        "redirect_uri": p.redirect_uri,
        "grant_type": "authorization_code",
    }
    if p.code_verifier:
        data["code_verifier"] = p.code_verifier
    kwargs: dict = {}
    if p.token_endpoint_auth_method == "client_secret_basic":
        # X's confidential clients REQUIRE HTTP Basic; sending the secret in the body is rejected.
        kwargs["auth"] = (p.client_id, client_secret)
    else:
        data["client_secret"] = client_secret
    resp = await client.post(p.token_uri, data=data, **kwargs)
    resp.raise_for_status()
    tok = resp.json()
    access = tok.get("access_token")
    if not access:  # a 200 with an error-shaped body — surface the provider's reason, not a KeyError
        raise ValueError(f"token endpoint returned no access_token: {tok.get('error') or tok}")
    blob = {
        "access_token": access,
        "token": access,
        "refresh_token": tok.get("refresh_token"),
        # Always stored under the canonical key — `is_refreshable` and every reader look for
        # "client_id". Only the wire spelling differs, and that travels as client_id_param below.
        "client_id": p.client_id,
        "client_secret": client_secret,
        "token_uri": p.token_uri,
        "expires_at": time.time() + float(tok.get("expires_in") or 3600),
    }
    if cid_param != "client_id":  # TikTok — refresh must post client_key, not client_id
        blob["client_id_param"] = cid_param
    if getattr(p, "long_lived_exchange", False):
        blob = await _extend_meta_token(blob, client)
    return blob


async def _extend_meta_token(blob: dict, client: httpx.AsyncClient) -> dict:
    """Swap Meta's short-lived user token for the ~60-day one.

    Meta's authorization-code exchange returns a token good for an hour or two and no
    refresh_token, so a connection made this way is dead by the time anyone uses it. This second
    call is the only way to get a durable user credential out of Facebook Login.

    A failure here is deliberately NOT fatal: the short-lived token is still a working credential,
    and refusing the whole connect would be a worse outcome than a connection the user has to
    remake sooner. `expires_at` keeps telling the truth either way, which is what `needs_reconnect`
    reads.
    """
    resp = await client.get(
        blob["token_uri"],
        params={
            "grant_type": "fb_exchange_token",
            "client_id": blob["client_id"],
            "client_secret": blob["client_secret"],
            "fb_exchange_token": blob["access_token"],
        },
    )
    if resp.status_code != 200:
        return blob
    tok = resp.json()
    access = tok.get("access_token")
    if not access:
        return blob
    return {
        **blob,
        "access_token": access,
        "token": access,
        # Meta omits expires_in when it issues a non-expiring token (system users, some business
        # tokens). Falling back to the 60-day default would then invent an expiry that isn't real
        # and nag the user to reconnect a credential that never dies.
        "expires_at": time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None,
    }


# ---- expiry, as an axis separate from health ---------------------------------------------
# health_status answers "does this credential work". Expiry answers "how long will it keep
# working" — and for a NON-refreshable token those are different questions with different
# answers. A LinkedIn token at the non-partner tier reports perfectly healthy right up until it
# silently dies at ~60 days, so expiry has to be surfaced on its own or the user gets no warning.

EXPIRING_SOON_DAYS = 7


def expiry_of(blob: dict) -> datetime | None:
    """The token's absolute expiry as a UTC-naive datetime (matching how treg stores timestamps)."""
    ts = _expires_at(blob)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def expiry_state(expires_at: datetime | None, refreshable: bool, now: datetime | None = None) -> str:
    """fresh | expiring | expired | unknown.

    A refreshable credential is always `fresh`: treg mints a new access token on demand, so its
    short expiry is an implementation detail the user should never be nagged about. Only a
    credential treg CANNOT renew by itself gets an expiry warning."""
    if refreshable:
        return "fresh"
    if expires_at is None:
        return "unknown"
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if expires_at <= now:
        return "expired"
    if expires_at - now <= timedelta(days=EXPIRING_SOON_DAYS):
        return "expiring"
    return "fresh"


def secret_is_refreshable(secret: Secret) -> bool:
    """Whether treg can renew this credential unattended. Decrypts server-side; the blob never
    leaves this function. A non-oauth secret is never auto-renewable."""
    if secret.kind != "oauth":
        return False
    try:
        return is_refreshable(json.loads(crypto.decrypt(secret.value)))
    except Exception:  # noqa: BLE001 — an unreadable blob is reported as not-refreshable, not a 500
        return False


def connection_view(secret: Secret) -> dict:
    """Everything the dashboard/CLI needs about one connection. Returns metadata only — no token
    material — so it is safe to hand to any org member."""
    refreshable = secret_is_refreshable(secret)
    state = expiry_state(secret.expires_at, refreshable)
    return {
        "id": secret.id,
        "name": secret.name,
        "kind": secret.kind,
        "provider": secret.provider,
        "resource_name": secret.resource_name,
        # The single field a UI or agent should act on: this connection will stop working and
        # only a human re-consent can fix it.
        "needs_reconnect": state in ("expiring", "expired"),
        "resource_ref": secret.resource_ref,
        "scopes": secret.granted_scopes.split() if secret.granted_scopes else [],
        "health": secret.health_status,
        "refreshable": refreshable,
        "expiry_state": state,
        "expires_at": secret.expires_at.isoformat() if secret.expires_at else None,
        "last_refresh_at": secret.last_refresh_at.isoformat() if secret.last_refresh_at else None,
        "last_error": secret.last_error,
        "owner": secret.owner,
        "created_at": secret.created_at.isoformat(),
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
        try:
            fresh = await refresh(blob, client)
        except Exception as exc:  # noqa: BLE001
            # Record WHY renewal failed. Without this a revoked refresh_token just 401s on every
            # call forever with nothing on the connection explaining it.
            await db.execute(
                update(Secret).where(Secret.id == secret.id).values(last_error=str(exc)[:300])
            )
            await db.commit()
            raise
        # Cross-process safety: the in-process lock only serializes THIS worker. Write conditionally
        # on the ciphertext we refreshed from, so a second worker that already rotated the
        # refresh_token can't be clobbered with our now-stale token; then reload whichever won.
        await db.execute(
            update(Secret).where(Secret.id == secret.id, Secret.value == old_value)
            .values(
                value=crypto.encrypt(json.dumps(fresh)),
                expires_at=expiry_of(fresh),
                last_refresh_at=datetime.now(timezone.utc).replace(tzinfo=None),
                last_error="",
            )
        )
        await db.commit()
        await db.refresh(secret)  # adopt the winning blob (ours or the other worker's) for injection
