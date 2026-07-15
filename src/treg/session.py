"""Signed session cookies for the web dashboard (human login).

A session is a tiny HMAC-signed token `<b64(payload)>.<b64(sig)>` carrying the user id + expiry.
Stateless (no DB table): we trust the signature. Agents/CLI keep using `X-Treg-Token`; this is only
for browser sessions after GitHub OAuth. Key = `TREG_SESSION_SECRET` (falls back to the Fernet key).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time

from .config import get_settings

TTL_SECONDS = 7 * 24 * 3600
COOKIE = "treg_session"

# When no signing secret is configured we fall back to a RANDOM per-process key (mirrors
# crypto._EPHEMERAL), NOT a source-visible constant: a static "dev-session-key" would let anyone
# who reads the code forge a session cookie for any user id (incl. a superadmin) — full auth
# bypass. Ephemeral means sessions simply don't survive a restart, the intended loud signal to
# set TREG_SESSION_SECRET / TREG_SECRET_KEY.
_EPHEMERAL_KEY = _secrets.token_bytes(32)


def _key() -> bytes:
    s = get_settings()
    configured = s.session_secret or s.secret_key
    return configured.encode() if configured else _EPHEMERAL_KEY


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make(user_id: int, ttl: int = TTL_SECONDS, token_version: int = 0) -> str:
    # `tv` binds the token to the user's current token_version; bumping that row invalidates every
    # token minted at an older version (see api._revoke path). Callers pass user.token_version.
    raw = json.dumps({"uid": user_id, "exp": int(time.time()) + ttl, "tv": token_version},
                     separators=(",", ":")).encode()
    sig = hmac.new(_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def read_claims(cookie: str) -> dict | None:
    """Return the token's claims ({uid, exp, tv}) if it is validly signed and unexpired, else None.
    `tv` defaults to 0 for tokens minted before token_version existed, so old tokens stay valid
    against a user whose token_version is still 0 (no forced logout on deploy)."""
    if not cookie or "." not in cookie:
        return None
    try:
        p, s = cookie.split(".", 1)
        raw = _unb64(p)
        expected = hmac.new(_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(s), expected):
            return None
        data = json.loads(raw)
        if int(data.get("exp", 0)) < time.time():
            return None
        return {"uid": int(data["uid"]), "exp": int(data["exp"]), "tv": int(data.get("tv", 0))}
    except Exception:  # noqa: BLE001 — any malformed cookie is simply "no session"
        return None


def read(cookie: str) -> int | None:
    """Return just the user id if the cookie is validly signed and unexpired, else None. Does NOT
    check token_version — callers that can load the user (api._user_from_*) use read_claims and
    compare tv against the row; use this only where the DB user isn't available."""
    claims = read_claims(cookie)
    return claims["uid"] if claims else None
