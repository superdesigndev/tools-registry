"""treg as an OAuth **authorization server** — the metadata, and the tokens it will issue.

Everywhere else treg speaks OAuth as a *client*: `oauth.py` builds PKCE challenges to sign in with
GitHub and to connect a provider account. This is the other direction — the thing that mints tokens
rather than the thing that redeems them.

This module is deliberately the FIRST piece built, and on its own it issues nothing. It is the part
that **refuses**: the metadata that tells a client what we support, and the validation that rejects a
token which is expired, forged, or minted for somebody else's server. Building the refusal before
the issuance means there is never a window where we accept tokens we have not yet learned to check.

**The `aud` claim is the load-bearing one.** The MCP spec has the client send `resource=<the mcp
url>` on both the authorize and the token request, and the authorization server must copy it into the
token's audience. Without checking it, a token a user granted to *some other* MCP server would work
on ours — the user consented to that server, not to treg, and we would be spending their treg balance
on the strength of it. `read_access_token` therefore takes the expected audience as a REQUIRED
argument. There is no default and no "skip the check" path, because the one thing that must never
happen by accident is not checking.

Not ChatGPT-specific. The MCP authorization spec is the same for every compliant client, so this
serves Claude Code, Cursor and anything else that implements it. See `docs/MCP-OAUTH-PLAN.md`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urljoin

from .config import PUBLIC_HOST_ALIASES, get_settings

# How long an access token lives. Short, because a refresh token will exist to renew it and a leaked
# access token is only as dangerous as its remaining life.
ACCESS_TTL_SECONDS = 3600

# Marks a token as an MCP access token and nothing else. treg already mints session cookies and
# identity tokens with the same HMAC construction, so without a type marker a session cookie would
# validate here (and vice versa) — one class of token would silently become another, which is a
# privilege escalation waiting for someone to notice it before we do.
_TOKEN_TYPE = "treg-mcp-at"


def _key() -> bytes:
    # Same secret as browser sessions, deliberately: one secret to rotate, and rotating it should
    # invalidate every credential at once rather than leaving some class of token still valid.
    from . import session

    return session._key()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mcp_resource_url() -> str:
    """The canonical identifier for the thing being protected — our MCP endpoint.

    Must match what clients send as `resource`, byte for byte, because that string is what ends up in
    the token's audience and what we compare against. The trailing slash is part of it: the transport
    is mounted at `/mcp` and served at `/mcp/`, and a client that resolves the metadata will use the
    form we publish here.
    """
    return urljoin(get_settings().public_url.rstrip("/") + "/", "mcp/")


def mcp_resource_audiences() -> set[str]:
    """Every audience an access token may legitimately carry: the canonical resource URL plus every
    reference-deployment alias. A grant keeps the audience that was consented to for its whole
    lifetime — refresh reissues `row.resource` — so validating against the canonical URL alone
    would 401 every pre-move grant forever, with refresh unable to recover. SYMMETRIC on purpose:
    grants minted on treg.to must equally survive a TREG_PUBLIC_URL rollback to the old name."""
    return {mcp_resource_url(), *(f"https://{h}/mcp/" for h in PUBLIC_HOST_ALIASES)}


def normalize_resource(resource: str) -> str:
    """Map any slash-variant spelling of OUR OWN resource URLs onto the canonical member of
    `mcp_resource_audiences()`; anything else passes through untouched. Authorization accepts
    `…/mcp` via a forgiving compare, but a token whose audience is stored with that spelling would
    fail the exact audience match forever — so every store/mint/compare site normalizes first."""
    r = (resource or "").rstrip("/")
    for aud in mcp_resource_audiences():
        if r == aud.rstrip("/"):
            return aud
    return resource


def read_access_token_any(token: str) -> dict | None:
    """`read_access_token` against each legitimate audience — the resource-server-side companion to
    `mcp_resource_audiences()`."""
    for aud in mcp_resource_audiences():
        claims = read_access_token(token, expected_audience=aud)
        if claims is not None:
            return claims
    return None


def protected_resource_metadata() -> dict:
    """`/.well-known/oauth-protected-resource` — "who guards this, and what may you ask for?"."""
    base = get_settings().public_url.rstrip("/")
    return {
        "resource": mcp_resource_url(),
        "authorization_servers": [base],
        "scopes_supported": ["treg:catalog", "treg:call", "treg:read"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/llms.txt",
    }


def authorization_server_metadata() -> dict:
    """`/.well-known/oauth-authorization-server` — how to get a token.

    `code` alone: no implicit grant, which OAuth 2.1 drops for good reason. `S256` alone: a client
    offering `plain` is a client whose codes can be replayed. `none` for client auth is correct for
    public clients that use PKCE, which is what every MCP client is.
    """
    base = get_settings().public_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["treg:catalog", "treg:call", "treg:read"],
        # ChatGPT sends an HTTPS metadata URL as its client_id instead of registering. Advertising
        # this does not replace `registration_endpoint` above — Claude Code and most other clients
        # register dynamically, and supporting only one of the two would lock the others out.
        "client_id_metadata_document_supported": True,
        "service_documentation": f"{base}/llms.txt",
    }


def make_access_token(*, user_id: int, org_id: int, audience: str, scope: str = "",
                      ttl: int = ACCESS_TTL_SECONDS, token_version: int = 0) -> str:
    """Mint an access token bound to a user, ONE team, and one audience.

    `org_id` is on the token rather than resolved per call on purpose. A person can belong to several
    teams, and the question "which team is this spending from?" must be answered once, by a human, at
    the consent screen — not guessed at request time. It is also what makes a granted token
    revocable in a way the user understands: they granted this client access to that team.
    """
    raw = json.dumps({
        "typ": _TOKEN_TYPE,
        "sub": int(user_id),
        "org": int(org_id),
        "aud": audience,
        "scope": scope,
        "tv": int(token_version),
        "exp": int(time.time()) + int(ttl),
    }, separators=(",", ":")).encode()
    sig = hmac.new(_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def looks_like_access_token(token: str) -> bool:
    """Does this bearer CLAIM to be one of our OAuth access tokens? (No validation — a discriminator.)

    The transport needs to tell "an expired/broken access token" apart from "a per-org token the API
    validates downstream", because the first must be answered `401 error="invalid_token"` (the signal
    that tells an OAuth client to run its refresh grant) while the second must pass through untouched.
    Reading the unverified payload's `typ` is safe for THAT question: a forger gains nothing by making
    an invalid token get the more honest status code."""
    if not token or "." not in token:
        return False
    try:
        return json.loads(_unb64(token.split(".", 1)[0])).get("typ") == _TOKEN_TYPE
    except Exception:  # noqa: BLE001 — not even parseable → not our shape
        return False


def read_access_token(token: str, *, expected_audience: str) -> dict | None:
    """Validate an access token and return its claims, or None.

    `expected_audience` is required and has no default. A token carries the resource its user
    consented to; accepting one addressed elsewhere would let a grant made to another MCP server
    spend a treg balance. Every other check here is ordinary — signature, type, expiry — but this is
    the one that is specific to being a resource server rather than a login system.
    """
    if not token or "." not in token or not expected_audience:
        return None
    try:
        payload, signature = token.split(".", 1)
        raw = _unb64(payload)
        expected_sig = hmac.new(_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected_sig):
            return None
        data = json.loads(raw)
        if data.get("typ") != _TOKEN_TYPE:
            return None                      # a session cookie is not an access token
        if data.get("aud") != expected_audience:
            return None                      # minted for a different resource — not ours to honour
        if int(data.get("exp", 0)) < time.time():
            return None
        return {"sub": int(data["sub"]), "org": int(data["org"]), "aud": data["aud"],
                "scope": data.get("scope", ""), "tv": int(data.get("tv", 0)),
                "exp": int(data["exp"])}
    except Exception:  # noqa: BLE001 — a malformed token is simply not a token
        return None


def verify_pkce(verifier: str, challenge: str) -> bool:
    """`S256` only. `plain` is in the spec and is worthless: it makes the challenge equal to the
    secret, so anyone who intercepts the authorization request can redeem the code."""
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return hmac.compare_digest(_b64(digest), challenge)


# ---------------------------------------------------------------------------------------------
# Client registration — two doors in, one row shape out
# ---------------------------------------------------------------------------------------------

# A client-id metadata document is fetched from a URL the CALLER chose, which makes it a
# request-forgery primitive unless it is fenced. The fence: https only, no redirects followed, a
# public address at connect time, a short timeout, and a size cap. `health` already owns both halves
# of the address check and is reused rather than reimplemented — a second copy of an SSRF guard is a
# second thing to forget to update.
_CIMD_TIMEOUT_S = 5.0
_CIMD_MAX_BYTES = 64 * 1024
_CIMD_REFRESH_S = 24 * 3600


def valid_redirect_uri(uri: str) -> bool:
    """A redirect URI must be https, or a loopback http address.

    Loopback is the documented exception and a real need: a CLI client listens on 127.0.0.1 to catch
    the code, and there is no way for it to hold a certificate. Everything else must be https,
    because an authorization code travelling over http is a code an eavesdropper can redeem.
    """
    from urllib.parse import urlsplit

    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if not u.hostname or u.fragment:      # a fragment cannot be matched exactly and hides intent
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "::1", "localhost")


def redirect_uri_allowed(client, uri: str) -> bool:
    """EXACT match against what the client registered — with ONE spec-mandated exception for loopback.

    Not a prefix or a host comparison. Prefix matching is the classic way this check is defeated —
    `https://good.example.com.evil.test/` starts with the registered origin, and an open redirect
    under a registered host turns one sloppy page into stolen authorization codes.

    The exception, per **RFC 8252 §7.3**: a native/CLI client (Claude Code, Codex, Cursor) listens on
    a loopback port it cannot know in advance, so it registers a PORTLESS loopback URI
    (`http://localhost/callback`) and sends the actual ephemeral port at authorize time
    (`http://localhost:52713/callback`). The AS **MUST** allow the port to vary. So for a loopback
    request we match scheme + host + path and IGNORE the port. This is safe where the public-host case
    is not: the redirect lands on the USER'S OWN machine, so there is no remote party to steal the code
    — the open-redirect attack the exact-match defends against does not exist on 127.0.0.1.
    """
    if not uri:
        return False
    registered = client.redirect_uris or []
    if uri in registered:
        return True
    from urllib.parse import urlsplit

    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    loopback = {"127.0.0.1", "::1", "localhost"}
    if u.scheme != "http" or u.hostname not in loopback:
        return False        # only loopback http gets the port-flexible path; everything else is exact
    for r in registered:
        try:
            ru = urlsplit(r)
        except ValueError:
            continue
        if ru.scheme == "http" and ru.hostname in loopback \
                and ru.hostname == u.hostname and (ru.path or "/") == (u.path or "/"):
            return True     # same loopback host + path, any port — RFC 8252 §7.3
    return False


async def fetch_client_id_metadata(url: str) -> dict | None:
    """Fetch a client-id metadata document, or None if it is not safe or not usable.

    Returning None rather than raising: an unreachable or malformed document is a client we cannot
    identify, which is a refusal, not a server error.
    """
    from urllib.parse import urlsplit

    import httpx

    from . import health

    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        return None
    if not health.safe_webhook_url(url) or not health.host_is_public(u.hostname):
        return None
    try:
        async with httpx.AsyncClient(timeout=_CIMD_TIMEOUT_S, follow_redirects=False) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200 or len(r.content) > _CIMD_MAX_BYTES:
            return None
        doc = r.json()
    except Exception:  # noqa: BLE001 — any failure to fetch is "cannot identify this client"
        return None
    if not isinstance(doc, dict):
        return None
    # The document must claim the URL it was served from. Without this, a document hosted anywhere
    # could assert someone else's client_id and inherit their consent.
    if doc.get("client_id") not in (None, url):
        return None
    redirects = [r for r in (doc.get("redirect_uris") or []) if isinstance(r, str)]
    redirects = [r for r in redirects if valid_redirect_uri(r)]
    if not redirects:
        return None
    return {
        "client_id": url,
        "client_name": str(doc.get("client_name") or url)[:200],
        "client_uri": str(doc.get("client_uri") or "")[:500],
        "logo_uri": str(doc.get("logo_uri") or "")[:500],
        "redirect_uris": redirects[:20],
        "scope": str(doc.get("scope") or "")[:200],
    }


def new_client_id() -> str:
    """Opaque, for dynamically registered clients. Not guessable, and not carrying meaning: a
    client_id that encoded anything would tempt something downstream into parsing it."""
    import secrets

    return "treg-client-" + secrets.token_urlsafe(24)
