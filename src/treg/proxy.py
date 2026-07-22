"""The relay — a faithful, smart proxy. The whole product in one function.

Faithfulness contract — it alters ONLY these, everything else is relayed verbatim
(method, path, ALL query params incl. duplicates, caller headers, caller cookies, body bytes streamed):
  1. transport/hop-by-hop headers — re-derived for the new hop (forwarding stale ones corrupts
     the stream); httpx sets them correctly upstream.
  2. treg's own control/infra headers + the edge's forwarding headers (`x-treg-*`,
     `ngrok-skip-browser-warning`, `x-forwarded-*`, `x-real-ip`, `forwarded`, `via`) — stripped so
     they never leak upstream. treg's own cookies (`treg_session`, `treg_oauth_state`) are scrubbed
     from the Cookie header too (the dashboard's `credentials:'include'` Try-it would otherwise leak
     our session token to the upstream); any other caller cookies are preserved.
  3. the credential(s) the tool's bindings inject — overwrite only their target header/param.

It never buffers the body (rule 5: stream, don't duplicate) and uses the shared long-lived
httpx client (rule 1: keepalive). Secrets are passed already-loaded (api does the DB work).
"""

from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from . import crypto, injectors
from .models import Secret, Tool

# Connection-level headers that belong to a single hop and must NOT be forwarded as-is.
_HOP_BY_HOP = frozenset(
    {
        "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    }
)
# treg's own control/infra headers + the edge's forwarding headers — never leak to the upstream.
_CONTROL = frozenset(
    {
        "x-treg-token", "x-treg-org", "ngrok-skip-browser-warning",
        "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port",
        "x-real-ip", "forwarded", "via",
    }
)
_DROP_REQUEST = _HOP_BY_HOP | _CONTROL
_DROP_RESPONSE = _HOP_BY_HOP
_TREG_COOKIES = frozenset({"treg_session", "treg_oauth_state"})  # our cookies, scrubbed from Cookie


def _scrub_treg_cookies(headers: httpx.Headers) -> None:
    """Drop treg's own cookies from the forwarded Cookie header so a dashboard `credentials:'include'`
    call never leaks our session token upstream. Other caller cookies are kept; an emptied header is removed."""
    cookie = headers.get("cookie")
    if not cookie:
        return
    kept = [
        c.strip() for c in cookie.split(";")
        if c.strip() and c.split("=", 1)[0].strip().lower() not in _TREG_COOKIES
    ]
    if kept:
        headers["cookie"] = "; ".join(kept)
    else:
        del headers["cookie"]


async def relay(
    request: Request,
    upstream_url: str,
    tool: Tool,
    secrets: dict[int, Secret],
    client: httpx.AsyncClient,
) -> StreamingResponse:
    # Headers: preserve everything (incl. duplicates / cookies) except hop-by-hop + our token.
    # `.raw` is the original (bytes, bytes) pairs; httpx.Headers is a multidict, so binding
    # injection (headers[name]=v) overwrites only its target and leaves the rest untouched.
    # RFC 7230 §6.1: also drop any header NAMED in the caller's own Connection header.
    req_drop = _DROP_REQUEST | _connection_named(request.headers.get("connection"))
    headers = httpx.Headers(
        [(k, v) for k, v in request.headers.raw if k.decode("latin-1").lower() not in req_drop]
    )
    _scrub_treg_cookies(headers)  # keep caller cookies, drop treg's own session cookie
    # Mirror the caller's compression choice. We relay the upstream body RAW (aiter_raw), so if the
    # upstream compresses, the caller receives compressed bytes. httpx supplies its own
    # `Accept-Encoding: gzip,…` whenever the request doesn't carry one — which would make us hand
    # gzip to a caller who never asked for it (binary garbage to any plain HTTP client or agent).
    # Asking for identity keeps what the caller gets matching what the caller requested.
    if "accept-encoding" not in headers:
        headers["accept-encoding"] = "identity"
    # Query: a list of pairs preserves duplicate keys verbatim (?tag=a&tag=b).
    params: list[tuple[str, str]] = list(request.query_params.multi_items())

    # Apply every binding (a request may need several credentials at once).
    for binding in tool.bindings:
        secret = secrets[binding["secret_id"]]
        injectors.inject(headers, params, binding, crypto.decrypt(secret.value))

    # Only carry a body when the caller actually sent one — otherwise passing an (unsized) stream
    # makes httpx frame the request `Transfer-Encoding: chunked`, putting a bogus body-frame on a
    # GET/HEAD/OPTIONS (which strict upstreams reject).
    content = request.stream() if _has_body(request) else None
    upstream_req = client.build_request(
        request.method, upstream_url, headers=headers, params=params, content=content
    )
    # Call-time SSRF guard: resolve the upstream host NOW and refuse an internal target — defeats DNS
    # rebinding (base_url was public at registration, its DNS now points at 169.254.169.254 / localhost).
    from . import health
    from .config import get_settings
    from fastapi import HTTPException
    if get_settings().proxy_ssrf_check and not health.host_is_public(upstream_req.url.host):
        raise HTTPException(status_code=502, detail="upstream host resolves to a non-public address")
    upstream_resp = await client.send(upstream_req, stream=True)

    response = StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        background=BackgroundTask(upstream_resp.aclose),
    )
    # Response drop set: hop-by-hop + whatever the upstream marked hop-by-hop via its Connection
    # header. Keep upstream Content-Length on a bodyless reply (HEAD/204/304) — that's the whole
    # point of HEAD; for a normal GET we re-frame so it's dropped.
    drop_resp = _DROP_RESPONSE | _connection_named(upstream_resp.headers.get("connection"))
    if request.method == "HEAD" or upstream_resp.status_code in (204, 304):
        drop_resp = drop_resp - {"content-length"}
    # Relay every remaining upstream header verbatim (incl. multiple Set-Cookie), EXCEPT a
    # Set-Cookie for one of treg's own cookies — a registered upstream must not be able to
    # overwrite the operator's treg_session / treg_oauth_state under treg's origin (fixation).
    response.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in upstream_resp.headers.multi_items()
        if k.lower() not in drop_resp and not _is_treg_setcookie(k, v)
    ]
    # The proxy is for API calls, but a browser could navigate to /call/… (authorized by the session
    # cookie) and render arbitrary upstream text/html AS AN ACTIVE DOCUMENT under treg's own origin —
    # reflected XSS with access to the operator's same-origin session. Neutralize it: nosniff + a
    # sandbox CSP (no script execution, no same-origin) on every relayed response. Agents ignore these.
    response.raw_headers.append((b"x-content-type-options", b"nosniff"))
    response.raw_headers.append((b"content-security-policy", b"sandbox"))
    return response


def _connection_named(conn: str | None) -> frozenset[str]:
    """The header names a peer marked connection-scoped via its `Connection` header (RFC 7230)."""
    if not conn:
        return frozenset()
    return frozenset(t.strip().lower() for t in conn.split(",") if t.strip())


def _has_body(request: Request) -> bool:
    cl = request.headers.get("content-length")
    if cl is not None and cl != "0":
        return True
    return "chunked" in request.headers.get("transfer-encoding", "").lower()


def _is_treg_setcookie(name: str, value: str) -> bool:
    return name.lower() == "set-cookie" and value.split("=", 1)[0].strip().lower() in _TREG_COOKIES
