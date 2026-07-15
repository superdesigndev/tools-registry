"""Secret-at-rest encryption (Fernet). Decision #2: secrets are never stored plaintext.

One symmetric key (TREG_SECRET_KEY). The proxy decrypts only at call time, in memory,
and never returns the plaintext to a client.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets

from cryptography.fernet import Fernet

from .config import get_settings

# Mint an ephemeral key once per process if none is configured (dev only — secrets
# won't survive a restart, which is the intended loud signal to set TREG_SECRET_KEY).
_EPHEMERAL = Fernet.generate_key()


def _fernet() -> Fernet:
    key = get_settings().secret_key
    return Fernet(key.encode() if key else _EPHEMERAL)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def new_key() -> str:
    """A fresh urlsafe key for TREG_SECRET_KEY."""
    return Fernet.generate_key().decode()


# ---- caller API tokens --------------------------------------------------------------------
# A token is high-entropy random, so a plain SHA-256 (not a slow password hash) is the right
# store: we look up by hash, the token is shown to the user exactly once at registration.
def new_token() -> str:
    return _secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
