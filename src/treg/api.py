"""The API — the only brain. CLI + skill are thin clients over this (charter).

Surface: open user registration (creates a personal org) + per-membership token auth; full CRUD
on secrets and tools; the /skills composer (register a whole skill = bundle + its secrets + its
tool(s) atomically) and /bundles reads; the /call proxy with a fire-and-forget audit record; and
/calls. A tool carries a LIST of bindings (multi-credential), with flat single-binding sugar on POST.

Multi-tenancy: a token = a (user, org) Membership. Every list/create/mutation and the proxy are
scoped to the caller's org; `owner` (creator email) drives the member-vs-admin role gate.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import html as _html
from functools import lru_cache
import hmac
import html as html_mod
import json
import logging
import os
import re
import secrets as _secrets
import shutil
import tempfile
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit, urlunsplit

from sqlalchemy import case, delete, func, or_, text, update

INVITE_TTL_DAYS = 7  # invite codes are one-time AND expire after this many days

import httpx
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import select

from . import adsconv, agent_pages, analytics, audit, billing, catalog_store, crypto, demo as demo_seed, email as email_sender, endpoint_stats, health, injectors, ledger, localrun, oauth
from . import oauth_providers
from . import pubfeed, ratestore, reconcile, referrals, runner, sandbox as demo_sandbox, session as sess
from .config import LEGACY_PUBLIC_HOSTS, PUBLIC_HOST_ALIASES, get_settings, platform_setting_name
from .db import get_session, init_db, session_maker
from .models import (ROLE_RANK, AdConversion, Bundle, CallRecord, CapabilityPin, CreditBlock,
                     DenyRule, Hold, IdempotentCall, Invite, LedgerEntry, Membership, OAuthClient,
                     OAuthCode, OAuthGrant, OAuthRefresh, Org, PendingOAuth, Project, Referral,
                     RunRecord, Secret, TagBudget, TagSpend, Tool, ToolRequest, User)
from .proxy import relay


LOCAL_USER_EMAIL = "you@local.treg"   # the single-user identity; a real address is never needed
LOCAL_ORG_NAME = "personal"


async def _bootstrap_single_user() -> None:
    """Frictionless local mode: make the machine's owner exist, so `curl … | sh` lands on a dashboard
    that is already signed in — no account, no email, no password.

    Idempotent, and the token is STABLE across restarts (rotating it every boot would break the CLI
    config the installer just wrote). It is re-minted only when the token file is missing, i.e. the
    user deleted it and needs a new one. Gated by `single_user_ok`, which refuses anything that isn't
    a local sqlite box — see config.Settings.
    """
    s = get_settings()
    if not s.single_user_ok:
        return
    path = Path(s.single_user_token_file).expanduser()
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none()
        if user is None:
            user = User(email=LOCAL_USER_EMAIL, onboarded=True)
            db.add(user)
            await db.flush()
        # Adopt an org ONLY through a membership this identity already has. Looking one up by the
        # slug `personal` and joining it as owner would, on a database that is not fresh, hand the
        # password-less local identity ownership of a team that belongs to someone else — and an
        # owner is exempt from every ACL. A new team therefore takes a FREE slug (`personal-2`, …)
        # rather than colliding with whatever already holds `personal`.
        membership = (await db.execute(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)
        )).scalars().first()
        token = ""
        if membership is None:
            org = Org(name=LOCAL_ORG_NAME.title(), slug=await _unique_slug(LOCAL_ORG_NAME, db))
            db.add(org)
            await db.flush()
            token = crypto.new_token()  # first boot
            membership = Membership(user_id=user.id, org_id=org.id, role="owner",
                                    token_hash=crypto.hash_token(token))
            db.add(membership)
        else:
            org = await db.get(Org, membership.org_id)
            if not path.exists():
                token = crypto.new_token()  # the token file was removed — mint a replacement
                membership.token_hash = crypto.hash_token(token)
        team = org.slug if org is not None else LOCAL_ORG_NAME
        await db.commit()
    if token:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        path.chmod(0o600)  # the installer reads it; nobody else should
    shown = token or "(unchanged — see " + str(path) + ")"
    print(f"\n  treg is ready — no account needed."
          f"\n  Dashboard  {s.public_url}/app"
          f"\n  Team       {team}"
          f"\n  Token      {shown}\n", flush=True)


async def _local_owner(db: AsyncSession) -> User | None:
    """The single-user identity, if this deployment is in that mode."""
    if not get_settings().single_user_ok:
        return None
    return (await db.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none()


# The MCP front door is OPTIONAL at import time. It lives in the `[server]` extra, and a deploy that
# has not picked the dependency up yet must still serve the API — a missing optional feature is not a
# reason for the whole registry to refuse to boot.
try:
    from . import mcp as _mcp
except Exception:  # pragma: no cover - exercised by deploys without the extra
    _mcp = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _backfill_provider_extra_tools()
    await _bootstrap_single_user()
    # One long-lived client for ALL upstream calls (rule 1: keepalive). The pool reuses
    # TCP+TLS connections across requests — the single biggest latency win for a relay.
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200)
    app.state.http = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(float(get_settings().call_timeout_s)))
    # Off unless configured (adsconv.enabled() requires the customer id, developer token and OAuth
    # org slug) — keeps the test suite and self-hosted instances completely inert by default.
    ads_task = asyncio.create_task(adsconv.worker(session_maker, app.state.http)) \
        if adsconv.enabled() else None
    try:
        if _mcp is None:
            yield
        else:
            # `app.mount()` does NOT run a mounted app's lifespan, and the streamable-HTTP session
            # manager builds its task group there. Without entering it here, every /mcp request fails
            # with "Task group is not initialized" — silently, and only under real traffic.
            async with _mcp.mcp_lifespan():
                yield
    finally:
        if ads_task is not None:
            ads_task.cancel()
        await audit.drain()  # flush pending audit writes before tearing down
        await analytics.drain()  # best-effort flush of queued analytics events
        await app.state.http.aclose()


# `/docs` is OURS — a server-rendered reference (see `docs_page`). FastAPI's stock Swagger UI moves
# to `/docs/api`: it is a 1 KB JavaScript shell to anything that does not run scripts, it was titled
# "tools-registry - Swagger UI" with FastAPI's own favicon, and it was the only thing we offered a
# crawler looking for our API. ReDoc is off — two consoles for one schema is one too many.
app = FastAPI(title="treg", version="0.0.1", lifespan=lifespan,
              docs_url="/docs/api", redoc_url=None)


# The pre-treg.to hostnames must keep answering the API forever — every installed CLI, skill.md
# and .mcp.json in the wild points here with a Bearer token, and most HTTP clients STRIP the
# Authorization header when a redirect crosses hosts (and some MCP clients follow no redirects at
# all). So only browser-facing marketing pages redirect to the canonical host; everything else —
# /call/, /mcp/, auth flows, webhooks, agent-fetched pages like /vendor-listing, install scripts
# fetched by `curl | sh` without -L — is served in place on both hosts.
_LEGACY_HOSTS = set(LEGACY_PUBLIC_HOSTS)
# Marketing pages — but only for ANONYMOUS visitors. A session cookie is host-scoped, so bouncing a
# signed-in browser to the canonical host silently logs it out mid-flow (the invite confirmation,
# for one, sets a legacy-host session and then lands on `/?invite_org=…`).
# robots.txt and sitemap.xml join them for a search-engine reason rather than a marketing one: the
# sitemap names canonical `public_url` URLs, and a sitemap whose own address is on a different host
# than the URLs inside it is cross-submission — a crawler is entitled to ignore the lot. Redirecting
# both means the legacy name resolves to one crawlable site, not a duplicate of it.
_REDIRECT_PATHS = {"/", "/login", "/terms", "/privacy", "/support", "/contact", "/help",
                   "/tutorial", "/robots.txt", "/sitemap.xml", "/catalog"}
# The auth ENTRY points redirect unconditionally, and that is a correctness fix, not a marketing
# one: each parks a host-scoped cookie and then continues on `public_url` — started on the legacy
# host, the continuation never sees the cookie. /auth/github + /auth/google set the CSRF state
# cookie the provider callback must find ("Bad state" otherwise); GET /oauth/authorize, signed out,
# parks the whole authorization request in `treg_oauth_return` and sends the browser through `/` to
# sign in. Exact paths only; the /callback routes (and POST /oauth/authorize, the consent approval)
# must keep serving in place — the middleware only touches GET/HEAD.
_REDIRECT_ALWAYS = {"/auth/github", "/auth/google", "/oauth/authorize"}


def _login_callback_base(request: Request) -> str:
    """The base URL a GitHub/Google login round-trip is anchored to. Normally `public_url` — but
    the provider compares the exchange's `redirect_uri` byte-for-byte against the one the
    authorization request named, so a flow living on a legacy host (a login in flight across the
    cutover deploy, with its state cookie and provider registration both on the old name) must
    keep building the OLD host's callback. Any recognized alias Host therefore wins — in BOTH
    directions, so a login minted on treg.to also survives a TREG_PUBLIC_URL rollback."""
    host = request.headers.get("host", "").split(":")[0].rstrip(".").lower()
    if host in PUBLIC_HOST_ALIASES:
        return f"https://{host}"
    return get_settings().public_url.rstrip("/")


@app.middleware("http")
async def _legacy_host_redirect(request: Request, call_next):
    """Redirect marketing pages (301) and auth entries (302) from a legacy host to the canonical
    host. Auth entries get a temporary redirect: their URLs carry one-shot OAuth parameters, and a
    cached permanent answer is exactly the wrong thing to keep."""
    host = request.headers.get("host", "").split(":")[0].rstrip(".").lower()
    if request.method in ("GET", "HEAD") and host in _LEGACY_HOSTS:
        path = request.url.path
        always = path in _REDIRECT_ALWAYS
        if always or (path in _REDIRECT_PATHS and sess.COOKIE not in request.cookies):
            canonical = get_settings().public_url.rstrip("/")
            # hostname equality, not substring: a self-hoster whose public_url IS a legacy host
            # must keep serving in place, but "not-treg.superdesign.dev" must not.
            if host != ((urlsplit(canonical).hostname or "").rstrip(".").lower()):
                target = canonical + path
                if request.url.query:
                    target += "?" + request.url.query
                return RedirectResponse(target, status_code=302 if always else 301)
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """The dashboard is an authenticated app; ship the baseline hardening headers it was missing —
    nosniff, clickjacking protection (X-Frame-Options), and a tight Referrer-Policy. `setdefault`
    so the /call proxy's own stricter CSP/nosniff isn't clobbered."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # HSTS pins the browser to https so a spoofed X-Forwarded-Proto can't downgrade the session
    # cookie onto cleartext (browsers ignore this header when served over http, so dev is unaffected).
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


_BODY_ENC_HEADER = b"x-treg-body-encoding"


def _decode_request_body(raw: bytes, enc: str) -> bytes:
    """Undo the transforms named in `enc` (left to right; `+`/`,`-separated). Supports `base64` and
    `gzip`, combinable (e.g. `base64+gzip` = base64-decode then gunzip). This lets a client smuggle a
    body whose plaintext (SQL, HTML) would otherwise trip an upstream WAF that inspects request bodies
    -- the edge sees only opaque base64, the server restores the real bytes before any route reads them."""
    out = raw
    for step in (s.strip().lower() for s in enc.replace(",", "+").split("+") if s.strip()):
        if step == "base64":
            out = base64.b64decode(out)
        elif step == "gzip":
            out = gzip.decompress(out)
        else:
            raise ValueError(f"unsupported body encoding: {step}")
    return out


class _BodyDecodeMiddleware:
    """Pure-ASGI: when a request carries `X-Treg-Body-Encoding`, decode the body before routing. The
    JSON endpoints (Pydantic re-reads the decoded body) and the /call proxy (which relays
    request.body() upstream) then both see the real bytes. No-op for requests without the header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        enc = next((v.decode("latin-1") for k, v in scope["headers"] if k == _BODY_ENC_HEADER), None)
        if enc is None:
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                break
        try:
            decoded = _decode_request_body(b"".join(chunks), enc)
        except Exception:  # noqa: BLE001 -- a malformed encoded body is a client error, not a 500
            return await JSONResponse({"detail": "invalid X-Treg-Body-Encoding body"}, status_code=400)(scope, receive, send)
        # Strip the marker, drop content-encoding, and fix content-length to the decoded size.
        headers = [(k, v) for k, v in scope["headers"]
                   if k not in (_BODY_ENC_HEADER, b"content-length", b"content-encoding")]
        headers.append((b"content-length", str(len(decoded)).encode("latin-1")))
        new_scope = dict(scope, headers=headers)
        delivered = False

        async def receive_decoded():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": decoded, "more_body": False}
            return {"type": "http.disconnect"}

        return await self.app(new_scope, receive_decoded, send)


app.add_middleware(_BodyDecodeMiddleware)


@app.exception_handler(OverflowError)
async def _id_out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
    # A huge all-digit path param (e.g. /secrets/999…) overflows SQLite's 64-bit INTEGER at bind
    # time; that's a non-existent id, not a server fault — surface a 404 instead of a 500.
    return JSONResponse({"detail": "identifier out of range"}, status_code=404)


def _refusal_kind(status_code: int) -> str | None:
    """Which gate said no, from the status treg chose for it (models.CallRecord.refused_by).

    Statuses map 1:1 because each gate owns its code on `/call/`: the vendor's own 401/404/429
    never comes through here — a relayed response is a Response, not an HTTPException. 5xx maps
    to None: a 502 is the upstream failing to answer, which is a fact about the provider, and
    must not be counted as a treg refusal."""
    if status_code >= 500:
        return None
    return {401: "auth", 402: "balance", 403: "policy", 404: "resolution", 410: "retired",
            429: "cap"}.get(status_code, "request")


@app.exception_handler(StarletteHTTPException)
async def _mark_treg_own_errors(request: Request, exc: StarletteHTTPException):
    """Tag treg's OWN refusals on `/call/` with `X-Treg-Error`, then answer exactly as before.

    A caller cannot otherwise tell a treg 404 ("no tool registered for that host") from the vendor's
    own 404 — both are a status code and some JSON. The local proxy needs that distinction to explain
    a failure without ever rewriting a real vendor response, and an agent reading a raw 403 needs to
    know whether to fix its request or ask an admin. The header is only ever ADDED; the status and the
    body are untouched, and a client that ignores it sees exactly what it saw before."""
    resp = await http_exception_handler(request, exc)
    if request.url.path.startswith("/call/"):
        resp.headers["X-Treg-Error"] = "1"
        # Refusals that raised before the handler's own audit ran (bad token, unknown tool, ACL,
        # deny rule, daily cap) would otherwise leave NO row — the funnel's early friction was
        # invisible until this. Identity comes from request.state (stashed at handler entry); a
        # bad-token 401 never had one, and an anonymous row is still the fact that someone knocked.
        if not getattr(request.state, "call_audited", False):
            org_id, email = getattr(request.state, "call_identity", (None, ""))
            rest = request.url.path[len("/call/"):]
            audit.record_call(
                org_id=org_id, user_email=email, tool_name=rest.split("/", 1)[0] or "—",
                method=request.method, path=request.url.path, status_code=exc.status_code,
                client=_client_of(request), refused_by=_refusal_kind(exc.status_code))
        # A failed call must not keep its idempotency label. The claim is taken before the upstream
        # call, and a request that dies anywhere after that — a bad parameter, a deny rule, an empty
        # balance — would otherwise hold the label for the whole window and answer every retry with
        # 409. Worse than the problem this feature exists to solve, and found by the test for it.
        #
        # Here because this is the ONE place every refusal passes through; the handler has a dozen
        # raise points and releasing at each would be a dozen chances to miss one.
        await _release_idempotent_claim(request)
    return resp

_WEB_DIR = Path(__file__).parent / "web"

_app_version_cache: tuple[float, str] | None = None  # (index.html mtime, content hash)


@lru_cache(maxsize=1)
def _treg_version() -> str:
    """The released package version, read from installed metadata.

    Read directly rather than through `cli.cli_version`, which does the same thing: importing
    `treg.cli` here costs ~200ms on first call and pulls the entire CLI into the server process for
    one string. Cached because package metadata cannot change while the process runs.
    """
    try:
        from importlib.metadata import version

        return version("tools-registry")
    except Exception:  # noqa: BLE001 — an editable/source run has no installed metadata
        return "dev"


def _app_version() -> str:
    """A stamp that changes with every deploy of the dashboard bundle: a hash of index.html,
    re-derived when the file's mtime moves (so dev --reload picks up edits too). Long-lived tabs
    compare this against the value they booted with and offer a refresh when it drifts."""
    global _app_version_cache
    index = _WEB_DIR / "index.html"
    try:
        mtime = index.stat().st_mtime
    except OSError:
        return "dev"
    if _app_version_cache is None or _app_version_cache[0] != mtime:
        digest = hashlib.sha256(index.read_bytes()).hexdigest()[:12]
        _app_version_cache = (mtime, digest)
    return _app_version_cache[1]


@app.get("/meta")
async def meta() -> dict:
    """Open: what the dashboard needs to render correct, shareable snippets — the public proxy URL
    (so copy/paste snippets use the real domain, not whatever origin the browser happens to be on)
    — plus the bundle version, so an open tab can detect a new deploy and offer a refresh.

    `treg_version` and `app_version` answer DIFFERENT questions and both are worth having.
    `app_version` is a hash of index.html: it changes whenever the dashboard bundle does, which is
    what an open tab compares to offer a refresh. `treg_version` is the released package version,
    which is what a release check needs — after publishing 0.9.0 there was no way to confirm from the
    live path which version was actually serving, only the commit id.
    """
    s = get_settings()
    return {"public_url": s.public_url.rstrip("/"), "github": bool(s.github_client_id),
            "google": bool(s.google_client_id), "app_version": _app_version(),
            "treg_version": _treg_version(),
            # public ingestion key — only present when this deployment opts in (self-hosters send nothing)
            "posthog_key": s.posthog_key, "posthog_host": s.posthog_host.rstrip("/") if s.posthog_key else "",
            # public workspace id — only present when this deployment opts in (self-hosters load no widget)
            "intercom_app_id": s.intercom_app_id}


@app.get("/providers.json", include_in_schema=False)
async def providers_catalog() -> dict:
    """Open: the provider catalog `treg upload` uses to detect env keys → tools. Served so the CLI can
    refresh it centrally (add a provider here → every CLI picks it up) with its bundled copy as fallback.
    See [env-import](../docs/context/interface/env-import.md)."""
    from . import providers as prov
    return {"version": prov.CATALOG_VERSION, "providers": prov.CATALOG}


# ---- endpoint catalog (what you can DO once connected) ------------------------------------
# The credential registry above answers "how do I connect Moz?"; these answer "and then what can I
# call?" — platform-grouped operations with cost, verification date and captured example responses.
# Open like /providers.json: the data is curated public documentation, no org's anything is in it.
# See docs/context/architecture/catalog.md.
def _provider_display(service: str) -> str:
    p = oauth_providers.get(service)
    return p.display_name if p else service


def _platform_rows() -> list[dict]:
    """The platform shelves, busiest first — one builder shared by the JSON route below and the
    server-rendered /catalog page, so the two can never disagree about what is on the shelf."""
    cat = catalog_store.load()
    rows = []
    for slug, plat in cat.platforms.items():
        # The census counts the BROWSE surface only: account/utility ("management") endpoints are
        # real inventory but they are not what a marketplace tile advertises, so they never inflate
        # the endpoint/capability/verified counts or the "from …" price. They still ship in the
        # platform-detail list (with `kind` set) — see catalog_platform's ?include_hidden.
        eps = [e for e in cat.for_platform(slug) if e["kind"] not in catalog_store.HIDDEN_KINDS]
        if not eps:  # a taxonomy entry no provider implements (or only plumbing) is grid noise
            continue
        rows.append({
            "slug": slug,
            "label": plat["label"],
            "category": plat["category"],
            "featured": plat.get("featured"),  # rank within its category's Featured shelf; null = not featured
            "summary": plat.get("summary", ""),
            # cheapest priced endpoint, for the card's "from …" corner. Ordering compares the
            # server-computed USD figure, so CNY rows and provider-credit rows sort against dollar
            # rows honestly; the ORIGINAL value+currency rides along for display.
            # cheapest PAID option — zero-cost utility routes (rate-card freebies) would otherwise
            # advertise a misleading "from $0"; genuinely free own-account access is signaled by the
            # UI separately when a platform has only free endpoints (price_from stays null).
            "price_from": min(
                (c for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]),
                key=lambda c: c["usd"],
                default=None,
            ),
            "capabilities": len({e["capability"] for e in eps if e["capability"]}),
            "endpoints": len(eps),
            "verified": len([e for e in eps if e["verified"]]),
            "providers": sorted({e["provider"] for e in eps}),
        })
    rows.sort(key=lambda r: (-r["endpoints"], r["slug"]))
    return rows


@app.get("/catalog/platforms")
async def catalog_platforms() -> dict:
    """Open: the platform shelves of the endpoint catalog, busiest first."""
    return {"platforms": _platform_rows(), "generated_from": "catalog"}


@app.get("/catalog/platforms/{slug}")
async def catalog_platform(slug: str, include_hidden: int = 0) -> dict:
    """Open: one platform's operations, grouped by capability so the same job across providers sits
    on one row — that grouping is what makes comparison (and a future failover router) possible.

    By default the account/utility ("management") endpoints are dropped from every shape below — the
    browse view is data + action. `?include_hidden=1` returns the whole surface (each endpoint still
    carries `kind`, so a client can file the plumbing behind an expander); `hidden_count` always
    reports how many were set aside so a caller can label that expander without a second request."""
    cat = catalog_store.load()
    eps = cat.for_platform(slug)
    if not eps:
        raise HTTPException(status_code=404, detail=f"unknown platform {slug!r}")
    hidden_count = len([e for e in eps if e["kind"] in catalog_store.HIDDEN_KINDS])
    if not include_hidden:
        eps = [e for e in eps if e["kind"] not in catalog_store.HIDDEN_KINDS]
    grouped: dict[str, list[dict]] = {}
    extended: list[dict] = []
    pairs: list[tuple[dict, dict]] = []
    for ep in eps:
        view = catalog_store.endpoint_view(ep, _provider_display(ep["provider"]), cat)
        pairs.append((ep, view))
        if ep["capability"]:
            grouped.setdefault(ep["capability"], []).append(view)
        else:
            extended.append(view)
    for views in grouped.values():
        # core before mapped-extended, verified before not — same convention as search ranking
        views.sort(key=lambda v: (v["tier"] != "core", not v["verified"], v["id"]))
    return {
        "platform": {"slug": slug,
                     "label": cat.platforms.get(slug, {}).get("label", slug),
                     "category": cat.platforms.get(slug, {}).get("category", "Other")},
        # The capability grouping `treg catalog` renders. The dashboard reads `domains` below, but
        # this is the shape the CLI has been written against since the catalog shipped, and the same
        # endpoints appear in both — a client picks the axis it wants, neither is a subset.
        "capabilities": [
            {"id": cap, "description": cat.capabilities.get(cap, ""), "endpoints": grouped[cap]}
            for cap in sorted(grouped)
        ],
        "extended": extended,
        # account/utility endpoints set aside — how many, so the page can label its expander. When
        # ?include_hidden=1 they are already folded into the shapes above (tagged by `kind`).
        "hidden_count": hidden_count,
        # The ledger the platform page renders: sections by subject, ordered and merged server-side
        # so every client shows the same page (see `catalog_store.domain_rows`).
        "domains": catalog_store.domain_rows(pairs, cat.capabilities),
        # Provider-wide facts (limits, pricing page, docs), once per provider rather than copied onto
        # every row — an expanded endpoint needs them and shouldn't cost a second request.
        "providers": {
            service: {"service": service, "display_name": _provider_display(service),
                      **cat.provider_meta.get(service, {})}
            for service in sorted({ep["provider"] for ep in eps})
        },
    }


async def _observed_or_empty(db: AsyncSession, endpoint_ids: list[str]) -> dict[str, dict]:
    """What the served calls say about these endpoints — or `{}` if that query is unavailable.

    Telemetry must never take the catalog down: the catalog answers signed-out readers and is the
    step every agent starts from, while these numbers are an enrichment on top of it.
    """
    try:
        return await endpoint_stats.observed(db, endpoint_ids)
    except Exception:  # noqa: BLE001
        logging.getLogger("treg.catalog").warning("endpoint stats unavailable", exc_info=True)
        return {}


@app.get("/catalog/search")
async def catalog_search(q: str = "", limit: int = 25,
                         db: AsyncSession = Depends(get_session)) -> dict:
    """Open: free-text search across the whole catalog — the DISCOVER half of the loop.

    An agent that knows what it wants ("tiktok comments") shouldn't have to guess which platform
    shelf hides it. Ranking is plain token matching (see `catalog_store.search`) so results are
    reproducible and explainable; equal scores — the common case, not the edge — then break on what
    treg has MEASURED and on price, so the cut stops being file order. `hints` carries the next
    command, since finding the endpoint is never the goal — inspecting or calling it is."""
    cat = catalog_store.load()
    limit = max(1, min(limit, 100))
    ranked, total, tie_truncated = catalog_store.rank_band(q, cat, limit)
    stats = await _observed_or_empty(db, [ep["id"] for ep, _ in ranked])
    ranked = catalog_store.rerank(ranked, stats, cat)[:limit]
    results = [
        catalog_store.endpoint_view(ep, _provider_display(ep["provider"]), cat)
        | catalog_store.endpoint_context(ep, cat)
        # The evidence that decided the order, shown rather than merely applied: a caller comparing
        # two rows should be able to see WHY one is above the other.
        | {"score": score, "observed": stats.get(ep["id"])}
        for ep, score in ranked
    ]
    if not q.strip():
        hints = ["pass ?q= — e.g. /catalog/search?q=tiktok+comments"]
    elif not results:
        # The miss IS the signal: log it (fire-and-forget, see models.SearchMiss) so the queries the
        # catalog couldn't answer surface in the usage report next to the ToolRequests they rarely
        # become. One source for this whole route — web, CLI and raw API all arrive here, and
        # guessing which from headers would be a made-up column.
        audit.record_search_miss(query=q.strip(), source="api")
        hints = [f"nothing matches {q!r} closely enough — try different task words, or browse `treg catalog` for the platform shelves",
                 "still missing? POST /tool-requests {\"capability\": \"<what you need>\"} — "
                 "requests steer which provider gets added next"]
    else:
        hints = [f"treg catalog get {results[0]['id']}   # params, cost and an example response",
                 f"{catalog_store.call_template(ranked[0][0])}   # run it — key injected server-side"]
        if total > len(results):
            hints.append(f"{total - len(results)} more matches — raise limit (max 100)")
        if tie_truncated:
            # No silent caps. Every row here scored the same, more of them scored the same than the
            # evidence sort was allowed to weigh, so the tail of this list is back to being ordered
            # by nothing in particular — say so instead of letting it read as a ranked answer.
            hints.append(f"{q!r} matches too broadly to rank on measured reliability past the first "
                         f"{catalog_store.RERANK_BAND} equally-scoring rows — add a word to narrow it")
    out = {"query": q, "count": len(results), "total": total, "results": results, "hints": hints}
    if not results and q.strip():
        # the rows that JUST missed the admission gate and which words they missed — an agent (or
        # the CLI display) turns this straight into the corrected query
        near = catalog_store.near_misses(q, cat)
        if near:
            out["near"] = near
            first = near[0]
            hints.insert(1, f"nearest: {first['endpoint_id']} matches "
                            f"{', '.join(first['matches'])} but not {', '.join(first['missing'])}")
    return out


@app.get("/catalog/endpoints/{endpoint_id}")
async def catalog_endpoint(endpoint_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    """Open: everything about ONE endpoint — the INSPECT half of the loop.

    Deliberately one round-trip: params, cost, the sibling providers offering the same capability
    (so a price/verification comparison needs no second call), a paste-ready `call_template`, and the
    captured example response inline — an agent shouldn't have to fetch /catalog/examples to learn
    the response shape it is about to parse."""
    cat = catalog_store.load()
    ep = cat.by_id.get(endpoint_id)
    if ep is None:
        # Name the near misses. An id that is one segment off is the common miss, and a bare 404
        # ends the search → get → call loop at its first step with nothing to try next.
        raise HTTPException(status_code=404, detail={
            "error": f"unknown endpoint {endpoint_id!r}",
            "hint": catalog_store.unknown_id_hint(endpoint_id, cat),
            "did_you_mean": catalog_store.near_ids(endpoint_id, cat)})
    view = (catalog_store.endpoint_view(ep, _provider_display(ep["provider"]), cat)
            | catalog_store.endpoint_context(ep, cat))
    siblings = [
        catalog_store.endpoint_view(other, _provider_display(other["provider"]), cat)
        for other in sorted(cat.for_capability(ep["capability"]), key=lambda e: e["id"])
        if other["id"] != ep["id"]
    ]
    example = None
    path = catalog_store.example_path(endpoint_id)
    if path is not None and path.is_file():
        try:
            example = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            example = None
    # What the calls we have already served say about this endpoint and its alternatives — the half
    # of "compare providers" that only treg can answer (see endpoint_stats + CAPABILITY-CHOICE-PLAN).
    # Attached to the SAME response because the choice is made here; a second round-trip to compare
    # reliability is a round-trip an agent will skip.
    stats = await _observed_or_empty(db, [endpoint_id] + [s["id"] for s in siblings])
    view = view | {"observed": stats.get(endpoint_id)}
    siblings = [s | {"observed": stats.get(s["id"])} for s in siblings]

    return {
        "endpoint": view,
        "provider": {"service": ep["provider"], "display_name": _provider_display(ep["provider"]),
                     **cat.provider_meta.get(ep["provider"], {})},
        "siblings": siblings,
        "call_template": catalog_store.call_template(ep),
        "example_response": example,
        "hints": [f"{catalog_store.call_template(ep)}   # run it — key injected server-side"]
                 + ([f"treg catalog get {siblings[0]['id']}   # the same job from {siblings[0]['provider']}"]
                    if siblings else []),
    }


@app.get("/catalog/examples/{endpoint_id}", include_in_schema=False)
async def catalog_example(endpoint_id: str) -> Response:
    """Open: the captured response of one endpoint, as recorded by scripts/catalog_verify.py.

    The id is resolved through the loaded catalog BEFORE any path is built, so raw input never
    reaches the filesystem. The files are truncated + PII-scrubbed public data (see the fragment)."""
    path = catalog_store.example_path(endpoint_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail=f"no example response for {endpoint_id!r}")
    return Response(content=path.read_bytes(), media_type="application/json")


# ---- the crawlable catalog: /catalog and /catalog/<slug> -------------------------------------
#
# The JSON routes above are what agents and the dashboard read. These two render the SAME data as
# server-side HTML, because until now none of it had a URL: the dashboard browses platforms through
# hash routes (/app#platform/<slug>) behind a login, so ~2,600 endpoints across 80 shelves were
# invisible to every crawler and every AI answer engine. No JavaScript here on purpose — the text IS
# the product surface, and it has to be readable by something that will not run a script or click.
#
# `/catalog/<slug>` is registered after the JSON routes so /catalog/platforms, /catalog/search,
# /catalog/endpoints/… and /catalog/examples/… keep matching first. Registration order alone is a
# thin guarantee, so the reserved names are also refused explicitly below.
_CATALOG_RESERVED = {"platforms", "search", "endpoints", "examples"}

_GH = "https://github.com/superdesigndev/treg"


def _usd_short(usd: float) -> str:
    """A dollar figure a person can read. `%g` flips to scientific notation below 1e-4, and a shelf
    advertising "from $1.2e-07 per call" reads as a bug rather than as a price — so anything under
    a hundredth of a cent is labelled as such instead."""
    if not usd:
        return "free"
    return "<$0.0001" if usd < 0.0001 else f"${usd:.3g}"


def _price_label(cost: dict | None) -> str:
    """A price in ONE currency, so rows down a page stay comparable. Mirrors `_cost_usd` in cli.py
    rather than importing it: pulling treg.cli into the server process costs ~200ms and drags the
    whole CLI in for one string (see `_treg_version`)."""
    if not isinstance(cost, dict):
        return ""
    usd = cost.get("usd")
    if usd is None:
        return "own account"     # no rate published — never invent a dollar figure
    if not usd:
        return "free"
    unit = {"per_call": "call", "per_result": "result", "per_success": "success"}.get(
        cost.get("type"), "call")
    return f"{_usd_short(usd)}/{unit}"


def _css_stamp(name: str = "catalog.css") -> str:
    """The stylesheet's own mtime, stamped onto its URL. Skins are served with a real max-age
    (they are static and every page pulls them), so without a stamp an edited skin keeps rendering
    from the browser's copy until the cache expires — the trap `/tutorial.js` already guards."""
    f = _WEB_DIR / name
    try:
        return str(int(f.stat().st_mtime))
    except OSError:
        return "0"


def _serp_desc(text: str, limit: int = 155) -> str:
    """A meta description Google will print whole. Past ~155 characters it truncates mid-sentence,
    so cut at the last sentence that fits, then at the last word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "? ", "! "):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[:i + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "."


def _page(title: str, description: str, path: str, body: str, ld: list[dict],
          *, nav_current: str = "", head_extra: str = "", css: str = "catalog.css") -> HTMLResponse:
    """The shared shell for every server-rendered page. One place that owns <title>, the meta
    description, the canonical, the og/twitter card and the JSON-LD, so a new page cannot ship
    without them — that omission is exactly what left the landing page bare for a year.

    The "Start free" CTA carries `?ref=<page>`: a logged-out visit to bare `/app` is bounced to the
    marketing landing with nothing open, which loses the page the visitor was reading. With `ref`
    the app keeps them and opens sign-in in place (see the boot in index.html), and the page that
    produced the signup is recorded."""
    base = get_settings().public_url.rstrip("/")
    ref = quote(path.strip("/").replace("/", "-") or "home", safe="")
    t, d = _esc_html(title), _esc_html(description)
    url = _esc_html(base + path)  # `path` reaches attribute context — escape it like title/description
    # `<` escaped to its \u form inside the JSON: a catalog label containing "</script>" would
    # otherwise close the block early and put the rest of the payload into the document as markup.
    # Still valid JSON, so parsers and Google's validator read it unchanged.
    blocks = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c")
        + "</script>"
        for b in ld)
    def navlink(href: str, label: str, extra: str = "") -> str:
        cur = ' aria-current="page"' if href == nav_current else ""
        return f'<a href="{href}"{cur}{extra}>{label}</a>'
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{t}</title>
<meta name="description" content="{d}"/>
<link rel="canonical" href="{url}"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<meta property="og:type" content="website"/>
<meta property="og:site_name" content="treg"/>
<meta property="og:url" content="{url}"/>
<meta property="og:title" content="{t}"/>
<meta property="og:description" content="{d}"/>
<meta property="og:image" content="{base}/media/og.png"/>
<meta property="og:image:width" content="1200"/>
<meta property="og:image:height" content="630"/>
<meta property="og:image:alt" content="treg.to: one key for the whole tool catalog, priced per call"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:title" content="{t}"/>
<meta name="twitter:description" content="{d}"/>
<meta name="twitter:image" content="{base}/media/og.png"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=Inter:wght@400;450;500;600;650;700&family=DM+Mono:ital,wght@0,400;0,500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/{css}?v={_css_stamp(css)}"/>
{head_extra}
{blocks}
</head>
<body>
<div class="navwrap"><nav class="nav">
  <a class="brand" href="/"><span class="glyph">▚</span> treg</a>
  <div class="links">
    {navlink("/catalog", "Catalog")}
    {navlink("/tutorial", "Tutorial")}
    {navlink("/docs", "API")}
    <a class="hidem" href="{_GH}" target="_blank" rel="noopener">GitHub ↗</a>
    <a class="candy" href="/app?ref={ref}">Start free</a>
  </div>
</nav></div>
{body}
<footer>
  <div class="foot-in">
    <div class="brand"><span class="glyph">▚</span> treg</div>
    <span style="font-family:var(--mono);font-size:12px">· 100% open source</span>
    <span class="sp"></span>
    <a href="/catalog">catalog</a><a href="/tutorial">docs</a><a href="/llms.txt">llms.txt</a
    ><a href="{_GH}" target="_blank" rel="noopener">github ↗</a><a href="/docs">api</a
    ><a href="/terms">terms</a><a href="/privacy">privacy</a>
  </div>
</footer>
</body>
</html>""", headers={"Cache-Control": "public, max-age=600"})


def _spa_catalog_page(title: str, description: str, path: str, ld: list[dict],
                      prerender: str) -> HTMLResponse:
    """Serve the dashboard SPA at a PUBLIC catalog URL, with the head a crawler needs.

    The public catalog is not a second implementation of the marketplace — it IS the marketplace.
    `/catalog` and `/catalog/<slug>` hand back `index.html`, and the Vue app renders the same
    platform views a member sees (its catalog API is unauthenticated, so it works signed out; see
    `publicCatalog` in index.html). That is the whole point: one UI, so the two can never drift
    apart visually the way a hand-built copy would.

    Two things have to be added on the way out:

    1. **The head.** The SPA ships one bare `<title>treg</title>`. Every catalog URL needs its own
       title, description, canonical, og/twitter card and JSON-LD, so they are substituted in here —
       the same trick `_spa_with_og` uses for shared skill/tool links.
    2. **A no-JS fallback.** Vue compiles `#app`'s own innerHTML as its template, so prerendered
       markup cannot go inside it. `#prerender` is therefore a SIBLING, removed by the app on boot.
       It is deliberately plainer than the Vue view — the ledger's row-merging is a chain of
       client-side computeds, and reproducing it server-side would recreate exactly the duplicate
       implementation this design avoids. It carries the TEXT (names, summaries, providers, prices),
       which is what a crawler that does not run scripts is here for.
    """
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    base = get_settings().public_url.rstrip("/")
    t, d = _esc_html(title), _esc_html(description)
    # `path` carries the {slug} from the URL. Today an unknown slug 404s in catalog_platform before
    # it reaches here, so a quote can't get this far — but that is an upstream lookup's side effect,
    # not a guarantee this function makes. Escape it where it is used, so a future "slug not found →
    # suggestions" page cannot turn a canonical tag into a reflected XSS.
    url = _esc_html(base + path)
    blocks = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c") + "</script>"
        for b in ld)
    meta = (
        f"<title>{t}</title>\n"
        f'<meta name="description" content="{d}"/>\n'
        f'<link rel="canonical" href="{url}"/>\n'
        f'<meta name="robots" content="index, follow"/>\n'   # index.html defaults to noindex
        f'<meta property="og:type" content="website"/>\n'
        f'<meta property="og:site_name" content="treg"/>\n'
        f'<meta property="og:url" content="{url}"/>\n'
        f'<meta property="og:title" content="{t}"/>\n'
        f'<meta property="og:description" content="{d}"/>\n'
        f'<meta property="og:image" content="{base}/media/og.png"/>\n'
        f'<meta property="og:image:width" content="1200"/>\n'
        f'<meta property="og:image:height" content="630"/>\n'
        f'<meta name="twitter:card" content="summary_large_image"/>\n'
        f'<meta name="twitter:title" content="{t}"/>\n'
        f'<meta name="twitter:description" content="{d}"/>\n'
        f'<meta name="twitter:image" content="{base}/media/og.png"/>\n'
        + blocks
    )
    html = index.read_text(encoding="utf-8")
    # index.html carries `robots: noindex` for the authenticated app; these URLs are public, and the
    # `index, follow` in `meta` only wins if the noindex is gone. Stripped BEFORE `meta` is spliced
    # in, so this scan only ever runs over the static bundle — never over a string carrying a
    # caller-supplied title, which is what made it a ReDoS candidate rather than a fixed-cost pass.
    html = re.sub(r'<meta name="robots" content="noindex[^>]*>\s*', "", html, count=1)
    # Match whatever title the page carries, not one exact string — a rename in the dashboard must
    # not be able to switch every catalog page's head off without a word (the same failure
    # `_spa_with_og` was written to survive).
    html, hits = re.subn(r"<title>.*?</title>", lambda _m: meta, html, count=1,
                         flags=re.IGNORECASE | re.DOTALL)
    if not hits:
        html = html.replace("<head>", "<head>\n" + meta, 1)
    marker = '<div id="app"'
    if marker in html:
        html = html.replace(marker, f'<div id="prerender">{prerender}</div>\n{marker}', 1)
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


# The fallback's own skin. Scoped to #prerender and written against the dashboard's OWN tokens
# (already defined in index.html), so it reads as the same product for the moment it is on screen.
_PRERENDER_CSS = """<style>
#prerender{max-width:1100px;margin:0 auto;padding:38px 26px 60px;font-family:var(--sans,system-ui);
  color:var(--ink,#1a1a1a)}
#prerender h1{font-size:30px;letter-spacing:-.01em;margin:0 0 8px}
#prerender .lede{color:var(--muted,#7c7c7c);margin:0 0 20px;max-width:64ch}
#prerender h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted2,#989898);margin:26px 0 10px;padding-bottom:8px;
  border-bottom:1px solid var(--line,#26262322)}
#prerender ul{list-style:none;margin:0;padding:0}
#prerender li{padding:9px 0;border-bottom:1px solid var(--line,#26262322)}
#prerender li b{font-weight:600}
#prerender li i{font-style:normal;color:var(--muted,#7c7c7c);display:block;font-size:13.5px}
#prerender .m{font-family:var(--mono,ui-monospace);font-size:11.5px;
  color:var(--muted2,#989898);margin-top:3px;display:block}
#prerender a{color:var(--teal,#1a7da6);text-decoration:none}
</style>"""


@app.get("/catalog", include_in_schema=False)
async def catalog_index():
    """The catalog index — the marketplace's Catalog view, on a public, indexable URL."""
    base = get_settings().public_url.rstrip("/")
    rows = _platform_rows()
    # The WHOLE catalog, not the sum of the tiles: a tile counts only its browse surface, so the
    # account/utility endpoints (real inventory, listed on each shelf page) would go uncounted and
    # this page would quietly contradict the number on the landing.
    cat = catalog_store.load()
    total_eps = len(cat.endpoints)
    providers = sorted({e["provider"] for e in cat.endpoints})

    cats: dict[str, list[dict]] = {}
    for row in rows:
        cats.setdefault(row["category"], []).append(row)
    sections = []
    for name, items in cats.items():
        lis = []
        for r in items:
            price = _price_label(r["price_from"])
            vendors = ", ".join(_provider_display(p) for p in r["providers"])
            lis.append(
                f'<li><b><a href="/catalog/{_esc_html(r["slug"])}">{_esc_html(r["label"])}</a></b>'
                f'<i>{_esc_html(r["summary"])}</i>'
                f'<span class="m">{r["endpoints"]} endpoints · {r["capabilities"]} capabilities'
                + (f" · from {_esc_html(price)}" if price else "")
                + f" · {_esc_html(vendors)}</span></li>")
        sections.append(f"<h2>{_esc_html(name)}</h2><ul>{''.join(lis)}</ul>")

    prerender = (_PRERENDER_CSS
                 + "<h1>The tool catalog</h1>"
                 + f'<p class="lede">{total_eps:,} endpoints across {len(rows)} platforms and '
                   f"{len(providers)} providers — every tool your agent can call through one key, "
                   "priced up front and billed per call, with no provider signup.</p>"
                 + "".join(sections))

    ld = [
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": "treg tool catalog",
         "description": f"{total_eps} API endpoints across {len(rows)} platforms, callable through one key.",
         "numberOfItems": len(rows),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": r["label"],
              "url": f"{base}/catalog/{r['slug']}"}
             for i, r in enumerate(rows, 1)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"}]},
    ]
    return _spa_catalog_page(
        f"Tool catalog — {total_eps:,} API endpoints your agent can call | treg",
        f"Browse {total_eps:,} endpoints across {len(rows)} platforms and {len(providers)} providers "
        "— SEO, social, enrichment, ads and scraping data. One key, priced per call, no provider signup.",
        "/catalog", ld, prerender)


@app.get("/catalog/{slug}", include_in_schema=False)
async def catalog_page(slug: str):
    """One platform shelf — the marketplace's platform view, on a public, indexable URL."""
    if slug in _CATALOG_RESERVED:
        raise HTTPException(status_code=404, detail=f"unknown platform {slug!r}")
    # include_hidden=1, exactly as the SPA asks for it (see `loadPlatform`): the account/utility
    # endpoints are real inventory and the page files them in their own section rather than hiding
    # them. Asking for a different population than the view that is about to replace this would put
    # two different endpoint counts on one URL.
    detail = await catalog_platform(slug, include_hidden=1)
    base = get_settings().public_url.rstrip("/")
    plat = detail["platform"]
    label, category = plat["label"], plat["category"]
    row = next((r for r in _platform_rows() if r["slug"] == slug), None)
    summary = (row or {}).get("summary", "")
    caps = detail["capabilities"]
    eps = [e for cap in caps for e in cap["endpoints"]] + detail["extended"]
    prices = [c["usd"] for e in eps if isinstance(c := e.get("cost"), dict) and c.get("usd")]
    cheapest = _usd_short(min(prices)) if prices else ""

    blocks = []
    for cap in caps:
        lis = []
        for e in cap["endpoints"]:
            price = _price_label(e.get("cost"))
            bits = [_esc_html(e["provider_display"])]
            if e.get("verified"):
                bits.append("live-verified")
            if price:
                bits.append(_esc_html(price))
            bits.append(_esc_html(e["id"]))
            lis.append(f'<li><b>{_esc_html(e["name"])}</b>'
                       f'<i>{_esc_html(e.get("summary") or "")}</i>'
                       f'<span class="m">{" · ".join(bits)}</span></li>')
        blocks.append(f'<h2>{_esc_html(cap["description"] or cap["id"])}</h2><ul>{"".join(lis)}</ul>')

    provs = ", ".join(p["display_name"] for p in detail["providers"].values())
    prerender = (_PRERENDER_CSS
                 + f'<p class="m"><a href="/catalog">← Catalog</a> · {_esc_html(category)}</p>'
                 + f"<h1>{_esc_html(label)}</h1>"
                 + f'<p class="lede">{_esc_html(summary)} {len(eps)} endpoints from '
                   f"{_esc_html(provs)}"
                 + (f", from {_esc_html(cheapest)} per call" if cheapest else "")
                 + ". Jobs that several providers do sit on one row, so you can compare price and "
                   "coverage before you spend a call — <b>choosing is yours</b>; treg does not route "
                   "between providers automatically.</p>"
                 + "".join(blocks))

    desc = (f"{len(eps)} {label.lower()} API endpoints from "
            f"{', '.join(p['display_name'] for p in list(detail['providers'].values())[:3])}"
            + (f", from {cheapest} per call" if cheapest else "")
            + ". Call them through one treg key — no provider signup.")
    ld = [
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": f"{label} — API endpoints on treg",
         "numberOfItems": len(caps),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": cap["description"] or cap["id"],
              "url": f"{base}/catalog/{slug}#{cap['id']}"}
             for i, cap in enumerate(caps, 1)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"},
            {"@type": "ListItem", "position": 3, "name": label, "item": f"{base}/catalog/{slug}"}]},
    ]
    return _spa_catalog_page(f"{label} API — {len(eps)} endpoints, priced per call | treg",
                             desc[:300], f"/catalog/{slug}", ld, prerender)


# --------------------------------------------------------------------------- /agents/<agent>

def _hosted() -> bool:
    """True on the reference deployment only. The agent pages describe treg.to's own listings (the
    ChatGPT plugin, the OAuth connector, the free grant), none of which is true of a self-hosted
    registry, so off these hosts the pages do not exist rather than lie."""
    host = (urlsplit(get_settings().public_url).hostname or "").lower()
    return host in PUBLIC_HOST_ALIASES


def _catalog_census() -> tuple[int, int]:
    """(browse-surface endpoint count, platform count): the two numbers the agent pages state."""
    cat = catalog_store.load()
    browse = [e for e in cat.endpoints if e["kind"] not in catalog_store.HIDDEN_KINDS]
    return len(browse), len({e["platform"] for e in browse})


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _logo(domain: str | None, alt: str) -> str:
    """A 20px brand mark from the favicon service the landing uses, or the treg glyph when the
    brand is unknown (never a wrong logo)."""
    if not domain:
        return '<span class="lg lg-none" aria-hidden="true">▚</span>'
    return (f'<img class="lg" src="https://www.google.com/s2/favicons?domain={_esc_html(domain)}&amp;sz=64" '
            f'alt="{_esc_html(alt)}" width="20" height="20" loading="lazy"/>')


def _use_case_page_for(category: str, label: str) -> str | None:
    """The spoke URL for a job on the agent page, or None when no page has been written for it."""
    cslug = agent_pages.category_slug(category)
    for (c, j), spec in agent_pages.USE_CASE_PAGES.items():
        if c == cslug and spec["label"] == label:
            return f"/use-cases/{c}/{j}"
    return None


def _use_case_caps(category_slug: str, label: str) -> tuple[str, ...]:
    for category, jobs in agent_pages.USE_CASES:
        if agent_pages.category_slug(category) == category_slug:
            for lbl, caps in jobs:
                if lbl == label:
                    return caps
    return ()


def _menu_rows(cat, category: str, jobs) -> list[dict]:
    """The use-case menu for one category, priced from the catalog. Shared by the HTML and the
    Markdown renderings of the agent page so the two can never list different jobs."""
    rows = []
    for label, caps in jobs:
        eps = [e for cid in caps for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
        if not eps:  # the test forbids this, but a page must never render an empty promise
            continue
        prices = [c["usd"] for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]]
        plats, seen = [], set()
        for cid in caps:
            ceps = [e for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
            if not ceps:
                continue
            slug = ceps[0]["platform"]
            plats.append({"cap": cid, "slug": slug, "dup": slug in seen,
                          "label": (cat.platforms.get(slug) or {}).get("label") or slug,
                          "domain": agent_pages.PLATFORM_DOMAINS.get(slug)})
            seen.add(slug)
        rows.append({"label": label, "caps": caps, "platforms": plats,
                     "providers": len({e["provider"] for e in eps}),
                     "verified": sum(1 for e in eps if e["verified"]),
                     "from_usd": min(prices) if prices else None,
                     # no priced endpoint at all = the team's own account does the job, unmetered
                     "own_account": not prices,
                     "page": _use_case_page_for(category, label)})
    return rows


_COPY_JS = """
<script>
document.querySelectorAll('button[data-copy]').forEach(function(b){
  b.addEventListener('click', async function(){
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent='copied'; b.classList.add('done');
          setTimeout(function(){ b.textContent='copy'; b.classList.remove('done'); }, 1400); } catch(e) {}
  });
});
</script>"""

_MD_ALT = '<link rel="alternate" type="text/markdown" href="{href}"/>'


@app.get("/agents/{agent}.md", include_in_schema=False)
@app.get("/agents/{agent}", include_in_schema=False)
async def agent_page(request: Request, agent: str):
    """One client: "I use ChatGPT, what can it do now?" A rotating "The ChatGPT plugin for <role>"
    hero, the install steps for that client, then the use-case menu: plain-words jobs under buyer
    categories, each priced from the catalog. The menu is `agent_pages.USE_CASES`, the same
    taxonomy the use-case pages hang from, so the agent page is the map of the whole site.
    `/agents/<agent>.md` is the same page as Markdown, for agents and answer engines."""
    as_md = request.url.path.endswith(".md")
    raw = agent[:-3] if agent.endswith(".md") else agent
    # Resolve to the dict's OWN key, never the request's bytes: `agent` is interpolated into the
    # canonical, the rel=alternate href and the JSON-LD breadcrumb below, and a path parameter
    # must not reach those unescaped (CodeQL py/reflective-xss). The lookup is case-insensitive,
    # so a differently-cased URL would otherwise serve a 200 whose canonical points at itself: a
    # duplicate page. Send it to the one spelling instead.
    agent = next((k for k in agent_pages.AGENTS if k == raw.lower()), None)
    if agent is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown agent")
    if raw != agent:
        return RedirectResponse(f"/agents/{agent}" + (".md" if as_md else ""), status_code=301)
    spec = agent_pages.AGENTS[agent]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    n_eps, n_plats = _catalog_census()
    n, p = f"{n_eps:,}", str(n_plats)
    name = spec["name"]
    title = spec["title"].format(n=n, p=p)
    desc = _serp_desc(spec["description"].format(n=n, p=p))
    definition = spec["definition"].format(n=n, p=p)
    menu = [(category, agent_pages.CATEGORY_PROMPTS.get(category, ""), _menu_rows(cat, category, jobs))
            for category, jobs in agent_pages.USE_CASES]
    steps_text = [re.sub(r"<[^>]+>", "", st) for st in spec["install_steps"]]

    if as_md:
        md = [f"# {title}", "", definition, "", f"## Install in {name}", ""]
        md += [f"{i}. {html_mod.unescape(st)}" for i, st in enumerate(steps_text, 1)]
        md += ["", f"## What {name} can do now", "",
               "One row per job. Prices are the provider's own rate with $0.000 markup; rows marked FREE run on your own account and are never metered.", ""]
        for category, prompt, rows in menu:
            md += [f"### {category}", ""]
            if prompt:
                md += [f"Try: \"{prompt}\"", ""]
            for r in rows:
                plats = ", ".join(pl["label"] for pl in r["platforms"] if not pl["dup"])
                price = "FREE with your own account" if r["own_account"] else f"from {_usd_short(r['from_usd'])}"
                link = f"{base}{r['page']}" if r["page"] else f"{base}/catalog/{r['platforms'][0]['slug']}"
                md.append(f"- [{r['label']}]({link}): {plats}. {r['providers']} provider{'s' if r['providers'] != 1 else ''}, {price}.")
            md.append("")
        md += ["## Questions", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += [f"HTML version: {base}/agents/{agent}", f"Setup line for any agent: {agent_pages.SETUP_LINE.format(base=base)}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # Only the FIRST role is in the H1 markup: a crawler reads "…plugin for SEO experts", not nine
    # roles run together. The rest ride in a JSON block and the script appends them.
    roles = f'<span class="ri on">{_esc_html(agent_pages.ROLES[0])}</span>'
    more_roles = json.dumps(list(agent_pages.ROLES[1:])).replace("<", "\\u003c")
    steps = "".join(
        f'<div class="steplabel"><span class="n">{i}</span><b>{st}</b></div>'
        for i, st in enumerate(spec["install_steps"], 1))
    shot = (f'<div class="sample"><div class="sbar">{_esc_html(spec.get("install_image_bar") or name)}</div>'
            f'<img src="{_esc_html(spec["install_image"])}" alt="{_esc_html(spec["install_image_alt"])}" '
            f'loading="lazy" style="display:block;width:100%"/>'
            + (f'<div class="sbar" style="border-top:1px solid var(--line);border-bottom:0">'
               f'{_esc_html(spec["install_image_caption"])}</div>' if spec.get("install_image_caption") else "")
            + '</div>' if spec.get("install_image") else "")

    # the platform marks in the hero: the busiest shelves, deduped by brand
    hero_tiles, seen_brand = [], set()
    for _cat_name, _prompt, rows in menu:
        for r in rows:
            for pl in r["platforms"]:
                root = ".".join((pl["domain"] or "").split(".")[-2:])
                if pl["domain"] and root not in seen_brand and len(hero_tiles) < 14:
                    seen_brand.add(root)
                    hero_tiles.append(f'<span class="ptile" title="{_esc_html(pl["label"])}">'
                                      f'{_logo(pl["domain"], pl["label"])}</span>')

    cards, sections = [], []
    for category, prompt, rows in menu:
        anchor = _anchor(category)
        priced = [r["from_usd"] for r in rows if r["from_usd"]]
        free_all = all(r["own_account"] for r in rows)
        meta = (f'{len(rows)} jobs &middot; <b style="color:var(--green)">free</b> on your account' if free_all
                else f'{len(rows)} jobs &middot; from {_esc_html(_usd_short(min(priced)))}' if priced
                else f"{len(rows)} jobs")
        blurb = agent_pages.CATEGORY_BLURBS.get(category, "").format(agent=name)
        cards.append(f'<a class="card" href="#{anchor}"><h4>{_esc_html(category)}</h4>'
                     f'<p>{_esc_html(blurb)}</p>'
                     f'<p style="font-family:var(--mono);font-size:11.5px;color:var(--muted2)">{meta}</p></a>')
        body_rows = []
        for r in rows:
            chips, seen_p = [], set()
            for pl in r["platforms"]:
                if pl["slug"] in seen_p:
                    body_rows.append("")  # keep data-cap discoverable below
                    continue
                seen_p.add(pl["slug"])
                chips.append(f'<a href="/catalog/{_esc_html(pl["slug"])}#{_esc_html(pl["cap"])}" '
                             f'data-cap="{_esc_html(pl["cap"])}">{_logo(pl["domain"], pl["label"])}{_esc_html(pl["label"])}</a>')
            hidden = "".join(f'<span data-cap="{_esc_html(pl["cap"])}" hidden></span>'
                             for pl in r["platforms"] if pl["dup"])
            price = ('<span style="color:var(--green)">free, your account</span>' if r["own_account"]
                     else f'{_esc_html(_usd_short(r["from_usd"]))}')
            name_cell = (f'<a href="{r["page"]}"><b>{_esc_html(r["label"])}</b></a>' if r["page"]
                         else f'<b>{_esc_html(r["label"])}</b>')
            body_rows.append(
                f'<tr><td>{name_cell}{hidden}</td>'
                f'<td style="color:var(--muted)">{" &middot; ".join(chips)}</td>'
                f'<td>{r["providers"]}</td><td>{price}</td></tr>')
        sections.append(
            f'<section id="{anchor}"><div class="wrap"><div class="seclab">{_esc_html(category)}</div>'
            f'<h2>{_esc_html(blurb)}</h2>'
            + (f'<p>Try: <i>&ldquo;{_esc_html(prompt)}&rdquo;</i></p>' if prompt else "")
            + '<div class="tablewrap"><table><thead><tr><th>Job</th><th>Where</th><th>Providers</th>'
              '<th>From</th></tr></thead><tbody>'
            + "".join(body_rows) + '</tbody></table></div></div></section>')

    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / '
        f'<a href="/agents/{_esc_html(agent)}">{_esc_html(name)}</a></div>'
        f'<div class="kicker">{n} endpoints &middot; {p} platforms &middot; $0.000 markup</div>'
        f'<h1>The {_esc_html(name)} plugin for <span class="roleslot" id="roleslot">'
        f'<span class="rw" id="rolewheel">{roles}</span></span></h1>'
        f'<script type="application/json" id="roles-more">{more_roles}</script>'
        f'<div class="lede">{_esc_html(definition)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=agents-{_esc_html(agent)}">Start free</a>'
        '<a class="ghostbtn" href="#use-cases">See what it can do</a></div>'
        '<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'<div class="subline">Your own keys always win and are never metered. '
        f'{_esc_html(name)} sees the price before it spends.</div>'
        + (f'<div class="provstrip"><div class="pl">a few of the {p} platforms</div>'
           f'<div class="ptiles">{"".join(hero_tiles)}</div></div>' if hero_tiles else "")
        + '</div></div>'

        f'<section id="install"><div class="wrap"><div class="seclab">Get started</div>'
        f'<h2>Install in {_esc_html(name)}</h2>{steps}{shot}</div></section>'

        '<section id="use-cases"><div class="wrap"><div class="seclab">The menu</div>'
        f'<h2>What {_esc_html(name)} can do now</h2>'
        '<p>By job, not by endpoint. The price is the lowest provider&rsquo;s own rate with $0.000 added by '
        'treg.to; <b>free</b> means the job runs on an account you already own and is never metered. Where '
        f'several providers do one job, {_esc_html(name)} sees them side by side and choosing is yours.</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<p style="margin-top:20px"><a href="/catalog">Browse all {n} endpoints &rarr;</a> &middot; '
        f'<a href="/use-cases">read the job guides &rarr;</a></p></div></section>'

        + "".join(sections)

        + f'<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you install</h2>{faq_html}</div></section>'

        + '<div class="final"><div class="wrap">'
          f'<h2>Give {_esc_html(name)} the tools</h2>'
          f'<a class="candy" href="/app?ref=agents-{_esc_html(agent)}-final">Start free</a>'
          '<div class="trust">$1.00 of calls free per new team &middot; '
          '<a href="/catalog">browse the catalog</a></div></div></div>'

        + """
<style>
.hero h1{line-height:1.16}
.roleslot{display:inline-block;height:1.16em;overflow:hidden;vertical-align:bottom;position:relative}
.roleslot .rw{display:flex;flex-direction:column;align-items:flex-start;transition:transform .62s cubic-bezier(.2,.7,.2,1)}
.roleslot .ri{height:1.16em;line-height:1.16;flex:none;white-space:nowrap;transition:opacity .4s}
.roleslot .ri:not(.on){opacity:.25}
@media (prefers-reduced-motion:reduce){.roleslot .rw{transition:none}}
</style>
<script>
(function(){
  var w=document.getElementById('rolewheel'); if(!w) return;
  try { JSON.parse((document.getElementById('roles-more')||{}).textContent||'[]').forEach(function(r){
    var s=document.createElement('span'); s.className='ri'; s.textContent=r; w.appendChild(s); }); } catch(e) {}
  var items=w.children, i=0, slot=document.getElementById('roleslot');
  if(matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  function fit(){ slot.style.width=items[i].getBoundingClientRect().width+'px'; }
  fit(); addEventListener('resize', fit);
  setInterval(function(){
    if(scrollY>innerHeight*.8) return;
    i=(i+1)%items.length; w.style.transform='translateY(-'+(i*1.16)+'em)';
    for(var k=0;k<items.length;k++) items[k].classList.toggle('on',k===i);
    fit();
  },3000);
})();
</script>""")

    ld = [
        {"@context": "https://schema.org", "@type": "SoftwareApplication", "name": "treg.to",
         "applicationCategory": "DeveloperApplication", "operatingSystem": "Web",
         "url": base + "/", "description": desc,
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD",
                    "description": "Free to install. Calls are metered per call from a prepaid balance at the "
                                   "provider's own rate with no markup; every new team starts with $1.00 free."}},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": name, "item": f"{base}/agents/{agent}"}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/agents/{agent}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/agents/{agent}.md"), css="usecase.css")


def _uc_agent() -> tuple[str, str]:
    """(slug, display name) of the client the use-case pages use as the example."""
    slug = agent_pages.DEFAULT_AGENT
    return slug, agent_pages.AGENTS[slug]["name"]


_UNIT_WORDS = {"per_success": "found", "per_call": "call", "per_result": "result"}


def _uc_providers(cat, eps: list[dict], obs: dict) -> list[dict]:
    """One row per provider for this job: cheapest priced endpoint, best-sampled observed stats,
    the union of its accepted inputs, and the platform it serves."""
    def usd(e):
        cv = cat.cost_view(e.get("cost"), e.get("provider"))
        return cv["usd"] if cv and cv["usd"] else None

    # Keyed by (provider, platform), not provider alone: one provider often serves several
    # platforms for the same job (ScrapeCreators does Instagram AND YouTube), and collapsing those
    # into one row silently drops a whole platform from a multi-platform page.
    out = []
    for prov, plat in sorted({(e["provider"], e["platform"]) for e in eps}):
        peps = [e for e in eps if e["provider"] == prov and e["platform"] == plat]
        priced = sorted([(usd(e), e) for e in peps if usd(e)], key=lambda t: t[0])
        cheapest_e = priced[0][1] if priced else None
        stats = [(obs.get(e["id"]) or {}) for e in peps]
        best = max((st for st in stats if st.get("samples")), key=lambda st: st["samples"], default=None)
        ins = []
        for e in peps:
            inp = e.get("input") or {}
            for section in ("queryParams", "pathParams", "body", "headers"):
                for k, v in (inp.get(section) or {}).items():
                    if isinstance(v, dict) and k not in ins:
                        ins.append(k)
        slug = plat
        out.append({
            "id": prov, "name": _provider_display(prov), "eps": peps,
            "domain": agent_pages.PROVIDER_DOMAINS.get(prov),
            "platform": slug, "platform_label": (cat.platforms.get(slug) or {}).get("label") or slug,
            "usd": priced[0][0] if priced else None,
            "unit": _UNIT_WORDS.get((cheapest_e.get("cost") or {}).get("type"), "call") if cheapest_e else "",
            "cheapest_ep": cheapest_e, "inputs": ins[:6],
            "verified": max((e.get("verified") or "" for e in peps), default=""),
            "ok_rate": best.get("ok_rate") if best else None,
            "p50": best.get("p50_ms") if best else None,
            "samples": best.get("samples") if best else 0,
        })
    return out


def _uc_call(e: dict) -> str:
    tr = e.get("test_request") or {}
    q = " ".join(f"--query {k}={v}" for k, v in (tr.get("queryParams") or {}).items())
    parts = [f"treg call {e['id']}"]
    if q:
        parts.append(q)
    if tr.get("body"):
        parts.append("--data '" + json.dumps(tr["body"], separators=(",", ":")) + "'")
    return " ".join(parts)


@app.get("/use-cases/{category}/{job}.md", include_in_schema=False)
@app.get("/use-cases/{category}/{job}", include_in_schema=False)
async def use_case_job_page(request: Request, category: str, job: str,
                            db: AsyncSession = Depends(get_session)):
    """One job. The reader does one thing, the prompt; everything else is what the agent sees
    before it calls. The page takes one of three FORMS, chosen from the data rather than by hand:

      short      one provider, so there is nothing to compare (all of "connect your own accounts")
      platforms  the job spans several platforms, which are not alternatives to one another
      compare    several providers doing one job on one platform: the full comparison

    Everything job-specific comes from `agent_pages.USE_CASE_PAGES`; the example client comes from
    `DEFAULT_AGENT`, so writing page two is data entry. `.md` serves the same page as Markdown.
    """
    as_md = request.url.path.endswith(".md")
    raw = (category, job[:-3] if job.endswith(".md") else job)
    # Same rule as `agent_page`: the slugs reach the canonical and the JSON-LD, so they come from
    # the table's own key, and a differently-cased URL is redirected rather than duplicated.
    key = next((k for k in agent_pages.USE_CASE_PAGES
                if k == (raw[0].lower(), raw[1].lower())), None)
    if key is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown use case")
    if raw != key:
        return RedirectResponse(f"/use-cases/{key[0]}/{key[1]}" + (".md" if as_md else ""),
                                status_code=301)
    # Fresh names on purpose: rebinding the parameters themselves does not read as a taint kill to
    # CodeQL, and the request's spelling must not be what the page prints.
    cat_slug, job_slug = key
    spec = agent_pages.USE_CASE_PAGES[key]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    agent_slug, agent_name = _uc_agent()
    cat_label = next((c for c, _ in agent_pages.USE_CASES if agent_pages.category_slug(c) == cat_slug), cat_slug)
    caps = _use_case_caps(cat_slug, spec["label"])
    eps = [e for cid in caps for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
    if not eps:
        raise HTTPException(status_code=404, detail="no endpoints for this job")
    obs = await _observed_or_empty(db, [e["id"] for e in eps])
    provs = _uc_providers(cat, eps, obs)

    def usd_of(e):
        cv = cat.cost_view(e.get("cost"), e.get("provider"))
        return cv["usd"] if cv and cv["usd"] else None

    platforms = sorted({p["platform_label"] for p in provs})
    form = "short" if len(provs) == 1 else ("platforms" if len(platforms) > 1 else "compare")
    # data-provider must stay unique in the DOM when one provider appears under two platforms
    for pr in provs:
        pr["row_id"] = pr["id"] if form != "platforms" else f'{pr["id"]}-{pr["platform"]}'
    noun = spec.get("result_noun", "result")

    # Cheapest is claimed PER BILLING UNIT. A per-call endpoint that returns a thousand rows is not
    # dearer than a per-result one, and ranking them together names the wrong winner: 38 of the 66
    # jobs on the menu mix units.
    cheapest_by_unit: dict[str, dict] = {}
    for pr in provs:
        u = pr["unit"]
        if pr["usd"] and (u not in cheapest_by_unit or pr["usd"] < cheapest_by_unit[u]["usd"]):
            cheapest_by_unit[u] = pr
    units = list(cheapest_by_unit)
    headline = cheapest_by_unit[units[0]] if units else None
    reliable = sorted([p for p in provs if p["samples"] and p["ok_rate"] is not None],
                      key=lambda p: (-p["ok_rate"], p["p50"] or 9e9, -p["samples"]))
    n = str(len({p["id"] for p in provs}))
    n_ver = sum(1 for e in eps if e.get("verified"))
    latest_verified = max((e.get("verified") or "" for e in eps), default="")
    setup = agent_pages.SETUP_LINE.format(base=base)

    def money(x):
        return _usd_short(x)

    def pct(x):
        return f"{round(x * 100)}%" if x is not None else ""

    def ms(x):
        return (f"{x/1000:.1f}s" if x >= 1000 else f"{int(x)}ms") if x else ""

    def unit_plural(u: str) -> str:
        return {"found": f"{noun}s found", "result": "results"}.get(u, "calls")

    title = spec.get("title", "{sentence}: {n} providers | treg.to").format(
        sentence=spec["sentence"], n=n, agent=agent_name,
        cheapest=money(headline["usd"]) if headline else "free on your own account")
    lede = spec["lede"].format(n=n, agent=agent_name,
                               cheapest=money(headline["usd"]) if headline else "free on your own account")
    bits_desc = [spec["sentence"] + "."]
    if form == "short":
        bits_desc.append("Runs on the account you already own, so treg.to never meters it.")
    elif headline:
        bits_desc.append(f"{n} providers compared, cheapest {money(headline['usd'])} per {headline['unit']}.")
    bits_desc.append(f"The prompt that works in {agent_name}, with the price shown before the call.")
    desc = _serp_desc(" ".join(bits_desc))

    if as_md:
        md = [f"# {spec['sentence']}", "", lede, "",
              f"## What's the best way to ask {agent_name}?", "",
              f"Setup line (paste into any agent): `{setup}`", "",
              f'Then ask: "{spec["prompt"]}"', ""]
        md += [f"- **{t}** {d}" for t, d in spec["prompt_why"]]
        md += ["", "## Why go through treg.to", ""] + [f"- **{t}** {d}" for t, d in agent_pages.WHY_TREG]
        if form == "short":
            e0 = provs[0]["eps"][0]
            md += ["", "## How it works", "",
                   f"One provider does this job: {provs[0]['name']} (`{e0['id']}`), on the account you already own. "
                   "You connect it once, treg.to keeps the token server side, and the call is never metered.",
                   "", f"    {_uc_call(e0)}", ""]
        else:
            md += ["", f"## Behind the scenes: what {agent_name} sees before it calls", "",
                   f"treg.to does not choose for you. It hands {agent_name} this comparison and it picks, "
                   "or you tell it how.", ""]
            if units:
                md += [f"### {spec.get('q_cheapest', 'Which is cheapest?')}", ""]
                for u in units:
                    pu = cheapest_by_unit[u]
                    md.append(f"- Cheapest per {u}: {pu['name']} at {money(pu['usd'])} (`{pu['cheapest_ep']['id']}`)")
                if len(units) > 1:
                    md += ["", "Those units are not interchangeable: one call can return many results, "
                               "so compare on the unit you will actually be billed in."]
            if reliable:
                md += ["", f"### {spec.get('q_reliable', 'Which is the most reliable?')}", ""]
                md += [f"- {p['name']}: {pct(p['ok_rate'])} over {p['samples']} calls, {ms(p['p50'])} median"
                       for p in reliable[:6]]
                md += ["", "Measured on treg.to traffic; not a controlled benchmark."]
            md += ["", f"### {spec.get('q_compare', 'How do they compare?')}", ""]
            for plat in (platforms if form == "platforms" else [None]):
                rows_ = [p for p in provs if plat is None or p["platform_label"] == plat]
                if plat:
                    md += [f"#### {plat}", ""]
                md += ["| Provider | Price | Accepts | Verified |", "|---|---|---|---|"]
                for p in sorted(rows_, key=lambda p: (p["usd"] is None, p["usd"] or 0)):
                    price = f"{money(p['usd'])} per {p['unit']}" if p["usd"] else "own account, free"
                    md.append(f"| {p['name']} | {price} | {', '.join(p['inputs'])} | {p['verified'] or 'unverified'} |")
                md.append("")
        md += ["Endpoints:", ""] + [f"- `{e['id']}`: {_uc_call(e)}" for e in eps]
        if spec.get("voices"):
            md += ["", "## What people actually struggle with", "", spec["voices_intro"], ""]
            for head, quote, who, url, answer in spec["voices"]:
                md += [f"**{head}**", "", f'> "{quote}" ({who}: {url})', "",
                       f"What this page can do about it: {answer}", ""]
        md += ["", "## What actually differs", ""] + [f"- {x}" for x in spec["notes"]]
        md += ["", f"## {spec.get('what_is_heading', 'What is this?')}", "", spec["what_is"], "", "## Questions", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += [f"HTML version: {base}/use-cases/{cat_slug}/{job_slug}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # ---------------------------------------------------------------- html (landing-page skin)
    ptiles = "".join(
        f'<span class="ptile" title="{_esc_html(p["name"])}">{_logo(p["domain"], p["name"])}</span>'
        for p in provs[:12] if p["domain"])
    provstrip = (f'<div class="provstrip"><div class="pl">compared on this page</div>'
                 f'<div class="ptiles">{ptiles}</div></div>' if ptiles else "")
    agent_icons = "".join(
        f'<span class="ptile" title="{_esc_html(label)}">'
        f'<img src="https://unpkg.com/@lobehub/icons-static-png@latest/light/{icon}.png" alt="{_esc_html(label)}" loading="lazy"/></span>'
        for aid, label, icon in agent_pages.AGENT_ICONS[:6])
    hero_price = (f"from {_esc_html(money(headline['usd']))} per {headline['unit']}"
                  if headline else "free on the account you already own")

    def promptbox(label: str, text: str) -> str:
        return ('<div class="promptbox"><div class="ph">'
                f'<span>{_esc_html(label)}</span>'
                f'<button class="copybtn" data-copy="{_esc_html(text)}">copy</button></div>'
                f'<pre>{_esc_html(text)}</pre></div>')

    why_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                        for t, d in spec["prompt_why"])
    treg_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                         for t, d in agent_pages.WHY_TREG)

    def price_cell(p: dict) -> str:
        return (f'{_esc_html(money(p["usd"]))} <span style="color:var(--muted2)">per {p["unit"]}</span>'
                if p["usd"] else '<span style="color:var(--green)">free, your own account</span>')

    def rel_cell(p: dict) -> str:
        return (f'{pct(p["ok_rate"])} <span style="color:var(--muted2)">({p["samples"]} calls)</span>'
                if p["samples"] else '<span style="color:var(--muted2)">not yet measured</span>')

    def prov_table(rows_: list[dict]) -> str:
        body_rows = "".join(
            f'<tr data-provider="{_esc_html(p["id"])}">'
            f'<td><b>{_logo(p["domain"], p["name"])}{_esc_html(p["name"])}</b></td>'
            f'<td>{price_cell(p)}</td>'
            f'<td style="color:var(--muted)">{_esc_html(", ".join(p["inputs"]) or "see endpoints")}</td>'
            f'<td>{rel_cell(p)}</td>'
            f'<td style="color:var(--muted2)">{_esc_html(p["verified"] or "unverified")}</td>'
            '</tr>' for p in rows_)
        return ('<div class="tablewrap"><table><thead><tr>'
                '<th>Provider</th><th>Price</th><th>Accepts</th><th>Success rate</th><th>Verified</th>'
                f'</tr></thead><tbody>{body_rows}</tbody></table></div>')

    sections = []
    if form == "short":
        p0, e0 = provs[0], provs[0]["eps"][0]
        sections.append(
            '<section id="how"><div class="wrap"><div class="seclab">How it works</div>'
            f'<h2>One provider, on the account you already own</h2>'
            f'<p>{_logo(p0["domain"], p0["name"])}<b>{_esc_html(p0["name"])}</b> answers this job. You connect it once, '
            'treg.to keeps the token server side, and the call is never metered.</p>'
            f'<div class="sample"><div class="sbar">the call</div><pre>{_esc_html(_uc_call(e0))}</pre></div>'
            f'<p style="font-size:12.5px;color:var(--muted)">Every endpoint on this connection is listed on the '
            f'<a href="/catalog/{_esc_html(e0["platform"])}">{_esc_html((cat.platforms.get(e0["platform"]) or {}).get("label") or e0["platform"])} shelf</a>.</p>'
            + '</div></section>')
    else:
        inner = [f'<p>treg.to does not choose for you. It hands {_esc_html(agent_name)} this comparison, with the '
                 f'price shown before any call, and {_esc_html(agent_name)} picks. Or you <b>tell it how</b>: '
                 '"cheapest", "most reliable", "the one that takes what I have", or a provider by name.</p>']
        if headline:
            cheap_cards = "".join(
                f'<div class="card"><h4>Cheapest per {_esc_html(u)}</h4>'
                f'<p>{_logo(cheapest_by_unit[u]["domain"], cheapest_by_unit[u]["name"])}'
                f'<b>{_esc_html(cheapest_by_unit[u]["name"])}</b> at {_esc_html(money(cheapest_by_unit[u]["usd"]))}'
                + (f' &middot; {_esc_html(cheapest_by_unit[u]["platform_label"])}' if form == "platforms" else "")
                + '</p></div>' for u in units)
            inner.append(f'<h3 id="cheapest">{_esc_html(spec.get("q_cheapest", "Which is cheapest?"))}</h3>'
                         f'<div class="cards">{cheap_cards}</div>')
            if len(units) > 1:
                inner.append('<blockquote>Those units are not interchangeable: one call can return many results, '
                             'so compare on the unit you will actually be billed in.</blockquote>')
        if reliable:
            rel_rows = "".join(
                f'<tr><td><b>{_logo(p["domain"], p["name"])}{_esc_html(p["name"])}</b></td>'
                f'<td>{pct(p["ok_rate"])}</td><td>{ms(p["p50"])}</td><td style="color:var(--muted2)">{p["samples"]} calls</td></tr>'
                for p in reliable[:6])
            inner.append(f'<h3 id="reliable">{_esc_html(spec.get("q_reliable", "Which is the most reliable?"))}</h3>'
                         '<div class="tablewrap"><table><thead><tr><th>Provider</th><th>Success</th><th>Median</th>'
                         f'<th>Sample</th></tr></thead><tbody>{rel_rows}</tbody></table></div>'
                         '<blockquote>Measured on treg.to traffic: real calls, real inputs, and sample sizes differ '
                         'by provider. Live reliability, not a controlled benchmark.</blockquote>')
        inner.append(f'<h3 id="compare">{_esc_html(spec.get("q_compare", "How do they compare?"))}</h3>')
        if form == "platforms":
            for plat in platforms:
                rows_ = sorted([p for p in provs if p["platform_label"] == plat],
                               key=lambda p: (p["usd"] is None, p["usd"] or 0))
                inner.append(f'<h4 data-platform-group="{_esc_html(plat)}">{_esc_html(plat)}</h4>' + prov_table(rows_))
        else:
            inner.append(prov_table(sorted(provs, key=lambda p: (p["usd"] is None, p["usd"] or 0))))
        if headline and headline["cheapest_ep"]:
            shelves = ", ".join(
                f'<a href="/catalog/{_esc_html(sl)}">{_esc_html((cat.platforms.get(sl) or {}).get("label") or sl)}</a>'
                for sl in sorted({p["platform"] for p in provs}))
            inner.append(
                '<h3>Run one</h3>'
                f'<div class="sample"><div class="sbar">the cheapest verified call</div>'
                f'<pre>{_esc_html(_uc_call(headline["cheapest_ep"]))}</pre></div>'
                f'<p style="font-size:12.5px;color:var(--muted)">Swap the id for any provider above. '
                f'All {len(eps)} endpoints behind this job, with their parameters and captured responses, '
                f'are on the {shelves} shelf.</p>')
        inner.append(
            '<h3>How these numbers are made</h3>'
            '<div class="who">'
            '<div><b>Prices</b>Each provider&rsquo;s own published rate, converted to US dollars for one chargeable '
            'event of the unit they bill in. treg.to adds $0.000. Where a provider bills in credits, the conversion '
            'uses the rate on their public pricing page'
            + (f', last checked {_esc_html(latest_verified)}.' if latest_verified else '.') + '</div>'
            '<div><b>Success rate</b>treg.to&rsquo;s own served calls over the last 30 days: 2xx counts as a success, '
            '5xx and timeouts as a failure. A 4xx is excluded, because it usually means the caller sent bad '
            'parameters and one bad query should not make a healthy endpoint look broken.</div>'
            '<div><b>What this is not</b>A controlled benchmark. These are real calls with real inputs, so sample '
            'sizes and the difficulty of what was asked differ by provider. Treat the rates as live reliability, '
            'not a like-for-like test.</div>'
            '<div><b>Verified</b>The date treg.to last called the endpoint end to end and confirmed the shape of '
            'its response and the price it charged.</div>'
            '</div>')
        sections.append(f'<section id="bts"><div class="wrap"><div class="seclab">Behind the scenes</div>'
                        f'<h2>What {_esc_html(agent_name)} sees before it calls</h2>'
                        + "".join(inner) + '</div></section>')

    voices_html = "".join(
        f'<h3>{_esc_html(head)}</h3>'
        f'<blockquote>&ldquo;{_esc_html(quote)}&rdquo; '
        f'<a href="{_esc_html(url)}" rel="nofollow noopener" target="_blank">{_esc_html(who)}</a></blockquote>'
        f'<p><b>What this page can do about it:</b> {_esc_html(answer)}</p>'
        for head, quote, who, url, answer in spec.get("voices", []))
    voices_section = ('<section id="voices"><div class="wrap"><div class="seclab">From the field</div>'
                      '<h2>What people actually struggle with</h2>'
                      f'<p>{_esc_html(spec.get("voices_intro", ""))}</p>{voices_html}</div></section>'
                      if spec.get("voices") else "")
    notes = "".join(f'<h4>{_esc_html(x.split(".")[0])}.</h4><p>{_esc_html(x.split(".", 1)[1].strip())}</p>'
                    if "." in x else f"<p>{_esc_html(x)}</p>" for x in spec["notes"])
    related = "".join(
        f'<a class="card" href="{_use_case_page_for(cat_label, lbl) or ("/agents/" + agent_slug + "#" + agent_pages.category_slug(cat_label))}">'
        f'<h4>{_esc_html(lbl)}</h4><p>Another job in {_esc_html(cat_label.lower())}.</p></a>'
        for lbl in spec.get("related", ()))
    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    # The "instead of" anchor: what the same job costs on subscriptions from the providers on this
    # page whose plan prices are recorded in marketing/landing/_facts.md, against a real run here.
    # Only sourced figures are named; with none, the anchor is the catalog's own spread.
    plans = [(p["name"], agent_pages.PLAN_PRICES[p["id"]]) for p in provs
             if p["id"] in agent_pages.PLAN_PRICES]
    pricewall = ""
    if headline:
        run_n = 100
        run_cost = headline["usd"] * run_n
        if plans:
            plans = sorted(plans, key=lambda t: -t[1])[:2]
            old_total = sum(v for _, v in plans)
            old_note = " + ".join(f"{k} ${v}/mo" for k, v in plans) + ", at list"
            old_v, old_k = f"${old_total}/mo", "instead of"
        else:
            dearest = max((p for p in provs if p["usd"]), key=lambda p: p["usd"])
            old_total = dearest["usd"] * run_n
            old_note = f"{dearest['name']}, the dearest here, for the same {run_n}"
            old_v, old_k = f"${old_total:,.2f}", "the wide end"
        pricewall = (
            '<section id="cost"><div class="wrap"><div class="seclab">The economics</div>'
            f'<h2>What {run_n} of these actually costs</h2>'
            '<div class="pricewall">'
            f'<div class="pw old"><div class="k">{old_k}</div><div class="v">{old_v}</div>'
            f'<div class="s">{_esc_html(old_note)}</div></div>'
            '<div class="arrow">&rarr;</div>'
            f'<div class="pw new"><div class="k">you pay</div><div class="v">${run_cost:,.2f}</div>'
            f'<div class="s">{run_n} &times; {_esc_html(money(headline["usd"]))} at {_esc_html(headline["name"])}, '
            'metered per call</div></div></div>'
            '<p style="font-size:12.5px;color:var(--muted)">Subscription figures are provider list prices recorded in '
            'treg.to&rsquo;s own catalog grid; per-call prices are what treg.to charges today, with $0.000 added.</p>'
            '</div></section>')

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / <a href="/use-cases">Use cases</a> / '
        f'<a href="/agents/{agent_slug}#{agent_pages.category_slug(cat_label)}">{_esc_html(cat_label)}</a></div>'
        f'<div class="kicker">{n} providers &middot; {hero_price} &middot; $0.000 markup</div>'
        f'<h1>{_esc_html(spec["sentence"])}</h1>'
        f'<div class="lede">{_esc_html(lede)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=uc-{_esc_html(job_slug)}">Start free</a>'
        '<a class="ghostbtn" href="#bts">See the comparison</a></div>'
        f'<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'<div class="subline">{n_ver} of {len(eps)} endpoints on this page are live-verified against the provider.</div>'
        f'{provstrip}</div></div>'

        + pricewall +
        '<section id="ask"><div class="wrap"><div class="seclab">Try it</div>'
        f'<h2>What&rsquo;s the best way to ask {_esc_html(agent_name)}?</h2>'
        f'<div class="steplabel"><span class="n">1</span><b>Set your agent up, once</b></div>'
        + promptbox("in your agent's chat", setup)
        + f'<div class="steplabel"><span class="n">2</span><b>Ask for the job</b></div>'
        + promptbox("the prompt", spec["prompt"])
        + f'<div class="provstrip"><div class="pl">works in</div><div class="ptiles">{agent_icons}</div></div>'
        + f'<h3>Why this prompt works</h3><div class="cards">{why_cards}</div>'
        + (f'<div class="sample"><div class="sbar">{_esc_html(agent_name)}</div>'
           f'<img src="{_esc_html(spec["result_image"])}" alt="{_esc_html(agent_name)} answering" '
           'style="display:block;width:100%"/></div>' if spec.get("result_image") else "")
        + '</div></section>'

        '<section id="why"><div class="wrap"><div class="seclab">Why treg.to</div>'
        '<h2>Why go through treg.to</h2>'
        f'<div class="cards">{treg_cards}</div></div></section>'

        + "".join(sections) + voices_section

        + '<section id="notes"><div class="wrap"><div class="seclab">The detail</div>'
          f'<h2>What actually differs</h2>{notes}</div></section>'

        + f'<section id="what"><div class="wrap"><div class="seclab">Background</div>'
          f'<h2>{_esc_html(spec.get("what_is_heading", "What is this?"))}</h2>'
          f'<p>{_esc_html(spec["what_is"])}</p></div></section>'

        + f'<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you start</h2>{faq_html}</div></section>'

        + (f'<section id="related"><div class="wrap"><div class="seclab">Related</div>'
           f'<h2>Other jobs your agent can do</h2><div class="cards">{related}</div></div></section>' if related else "")
        + _COPY_JS)
    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Use cases", "item": base + "/use-cases"},
            {"@type": "ListItem", "position": 3, "name": cat_label,
             "item": f"{base}/agents/{agent_slug}#{agent_pages.category_slug(cat_label)}"},
            {"@type": "ListItem", "position": 4, "name": spec["sentence"],
             "item": f"{base}/use-cases/{cat_slug}/{job_slug}"}]},
        {"@context": "https://schema.org", "@type": "ItemList", "name": title, "numberOfItems": len(provs),
         "itemListElement": [{"@type": "ListItem", "position": i, "name": p["name"],
                              "url": f"{base}/use-cases/{cat_slug}/{job_slug}#compare"}
                             for i, p in enumerate(provs, 1)]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/use-cases/{cat_slug}/{job_slug}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/use-cases/{cat_slug}/{job_slug}.md"),
                 css="usecase.css")


@app.get("/use-cases", include_in_schema=False)
async def use_cases_hub():
    """The hub the spokes hang from. A sitemap is not a crawl path: before this existed, the only
    link into a use-case page was one row on one agent page's menu."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    _, agent_name = _uc_agent()
    by_cat: dict[str, list[str]] = {}
    for (c, j), spec in agent_pages.USE_CASE_PAGES.items():
        label = next((cl for cl, _ in agent_pages.USE_CASES if agent_pages.category_slug(cl) == c), c)
        caps = _use_case_caps(c, spec["label"])
        eps = [e for cid in caps for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
        nprov = len({e["provider"] for e in eps})
        prices = [cv["usd"] for e in eps if (cv := cat.cost_view(e.get("cost"), e.get("provider"))) and cv["usd"]]
        meta = (f"{nprov} provider{'s' if nprov != 1 else ''} &middot; from {_esc_html(_usd_short(min(prices)))}"
                if prices else "free on your own account")
        blurb = spec["lede"].format(n=nprov, agent=agent_name,
                                    cheapest=_usd_short(min(prices)) if prices else "free")
        by_cat.setdefault(label, []).append(
            f'<a class="pcard" href="/use-cases/{c}/{j}"><h3>{_esc_html(spec["sentence"])}</h3>'
            f'<p>{_esc_html(blurb[:140])}</p><div class="meta">{meta}</div></a>')
    blocks = "".join(f'<section class="cat"><h2 id="{_anchor(c)}">{_esc_html(c)}</h2>'
                     f'<div class="grid">{"".join(v)}</div></section>' for c, v in by_cat.items())
    body = (
        '<main class="wrap"><div class="phead">'
        '<div class="crumbs"><a href="/">treg.to</a> / <a href="/use-cases">Use cases</a></div>'
        '<h1>What you can have your agent do</h1>'
        '<p class="lede">One page per job: the prompt that works, what the call costs, and every provider '
        'that does it. All of it through one treg.to key, at the provider&rsquo;s own rate with $0.000 markup.</p>'
        '</div>' + blocks
        + '<section class="cat"><h2>Everything else</h2><div class="cap"><p style="margin:0">These are the jobs '
          'written up so far. The full menu is on the agent pages, and the whole catalog is at '
          '<a href="/catalog">/catalog</a>.</p></div></section></main>')
    ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
        {"@type": "ListItem", "position": 2, "name": "Use cases", "item": base + "/use-cases"}]}]
    return _page("What you can have your agent do | treg.to",
                 "One page per job: the prompt that works in ChatGPT or Claude, what the call costs, and "
                 "every provider that does it, compared. One treg.to key, no markup.",
                 "/use-cases", body, ld)


@app.get("/catalog.css", include_in_schema=False)
async def catalog_css():
    """The shared skin for /catalog, /catalog/<slug> and /docs — the landing's tokens, one copy."""
    f = _WEB_DIR / "catalog.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="catalog.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "public, max-age=600"})


# ---- the API reference ------------------------------------------------------------------------
# Prose first, because the schema cannot say the load-bearing part: what /call/ actually does. Kept
# short and factual — the tutorial teaches, this page is the reference a reader lands on from search.
_DOCS_INTRO = """
<h2>How a call works</h2>
<p>You make the <b>real upstream request</b> — the provider's own path, its own parameters, its own
response. treg injects the credential server-side and relays the answer verbatim. Nothing here
models a provider's API, which is why an upstream change does not break us and why the caller never
holds a secret.</p>
<pre class="call">curl -H "Authorization: Bearer $TREG_TOKEN" \\
  "{BASE}/call/moz.web.url.metrics"</pre>
<p>Prefix any catalogued endpoint id with <code>/call/</code>. If your team has its own key for that
provider, treg uses it and the call is <b>not metered</b>; otherwise eligible endpoints are served on
treg's key and metered against your prepaid balance at the provider's own rate.</p>

<h2>Finding an endpoint</h2>
<p>Search by what you want to <i>do</i>, not by vendor: <code>GET /catalog/search?q=backlinks</code>.
When several providers can do the same job, <code>/catalog/platforms/{slug}</code> lists them side by
side with measured success rate, speed and price. <b>Choosing is yours</b> — treg compares, but it
does not route between providers automatically and does not fail over.</p>
<p>The whole catalog is also browsable as pages: <a href="/catalog">/catalog</a>.</p>

<h2>Other ways in</h2>
<p><a href="/llms.txt">/llms.txt</a> is the file to point a coding agent at — it teaches the whole
protocol in one fetch. <code>curl -fsSL {BASE}/install.sh | sh</code> installs the CLI. The MCP
endpoint is at <code>{BASE}/mcp</code>. An interactive console for everything below lives at
<a href="/docs/api">/docs/api</a>.</p>

<h2>Endpoints</h2>
<p>Authenticated requests carry <code>Authorization: Bearer &lt;token&gt;</code> (or
<code>X-Treg-Token</code>). The catalog routes are open and need no token.</p>
"""


@app.get("/docs", include_in_schema=False)
async def docs_page():
    """The API reference, rendered server-side from the OpenAPI schema.

    Replaces the stock Swagger UI at this path (now /docs/api), which was a script shell — the
    landing page linked "api" here and a crawler that followed it found an empty document.
    """
    base = get_settings().public_url.rstrip("/")
    schema = app.openapi()

    def rank(path: str) -> tuple:
        """The proxy first, then the catalog, then the rest alphabetically. Sorting purely by path
        opened the reference on /admin/* — super-admin plumbing, and the worst possible first
        impression of the API on a page built to be someone's search result."""
        return (0 if path.startswith("/call/") else 1 if path.startswith("/catalog") else 2, path)

    # Auth travels the same way on every route; naming it on all 135 rows is noise, and the page
    # says it once above. `/admin/*` is super-admin only — still in openapi.json, not advertised here.
    _PLUMBING = {"x-treg-token", "treg_session", "authorization"}
    ops = []
    for path in sorted(schema.get("paths", {}), key=rank):
        if path.startswith("/admin"):
            continue
        for method, op in sorted(schema["paths"][path].items()):
            if method.lower() == "head":     # implied by GET; see `_openapi_without_head`
                continue
            params = ", ".join(p["name"] for p in op.get("parameters", []) or []
                               if p["name"].lower() not in _PLUMBING)
            summary = op.get("summary") or ""
            # FastAPI takes the description from the docstring; only the first paragraph belongs on
            # a reference index, and the rest is written for maintainers rather than callers.
            desc = (op.get("description") or "").strip().split("\n\n")[0].replace("\n", " ")
            ops.append(
                f'<div class="op"><div class="sig"><span class="verb">{_esc_html(method.upper())}</span>'
                f'<code>{_esc_html(path)}</code></div>'
                + (f"<p>{_esc_html(summary or desc)}</p>" if (summary or desc) else "")
                + (f'<div class="params">{_esc_html(params)}</div>' if params else "")
                + "</div>")

    body = f"""<main class="wrap">
<div class="phead">
  <div class="crumbs"><a href="/">treg</a> / api</div>
  <h1>API reference</h1>
  <p class="lede">One base URL, one token. Call any of {len(ops)} documented operations, or proxy a
  real request to any of 2,630 catalogued provider endpoints through <code>/call/</code>.</p>
  <div class="facts">
    <span>base <b>{_esc_html(base)}</b></span>
    <span><b>Bearer</b> token auth</span>
    <span><a href="/openapi.json">openapi.json</a></span>
    <span><a href="/docs/api">interactive console</a></span>
  </div>
</div>
<section class="cat">
  <div class="prose">{_DOCS_INTRO.replace("{BASE}", _esc_html(base))}</div>
  {"".join(ops)}
</section>
</main>"""
    ld = [{"@context": "https://schema.org", "@type": "TechArticle",
           "headline": "treg API reference",
           "description": "How to call 2,630 provider API endpoints through one treg token.",
           "url": f"{base}/docs"}]
    return _page("API reference — call any tool through one endpoint | treg",
                 "The treg HTTP API: proxy a real request to any of 2,630 catalogued provider "
                 "endpoints through /call/, with the credential injected server-side. Plus the "
                 "catalog, org, billing and tool-management routes.",
                 "/docs", body, ld, nav_current="/docs")


# ---- "the catalog doesn't have X" — tool requests -------------------------------------------
TOOLREQ_HIT_NS = "toolreq"
TOOLREQ_RATE_MAX = 10          # filings per IP per window
TOOLREQ_RATE_WINDOW_S = 3600   # 1 hour
TOOLREQ_SOURCES = {"web", "cli", "mcp", "api"}


class ToolRequestIn(BaseModel):
    capability: str          # what they wanted — "Ahrefs backlinks", "flight prices", a provider name
    query: str = ""          # the catalog search that came up empty (agents auto-fill this)
    note: str = ""
    contact: str = ""        # optional reach-back; free text, unverified
    source: str = "web"      # web | cli | mcp | api


@app.post("/tool-requests", include_in_schema=False)
async def create_tool_request(
    body: ToolRequestIn,
    request: Request,
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Open: file a "the catalog doesn't have X" report. No auth on purpose — the filer is most
    often an agent that just got zero search results and holds no token, and a signup wall here
    costs exactly the demand signal the catalog team wants. Per-IP rate limiting (ratestore) is
    the abuse valve, same shape as POST /demo/sandbox; field caps bound the row.

    Identity is attribution, never authorization: when the caller happens to be signed in (token
    or same-origin session), the row records who asked so they can be told when it lands — a
    forged cross-origin cookie POST gets stored as anonymous, not rejected, hence the
    `_same_origin` gate on the cookie path only."""
    await ratestore.sweep(db, TOOLREQ_HIT_NS)
    if not await ratestore.rate_check(db, TOOLREQ_HIT_NS,
                                      [(_client_ip(request), TOOLREQ_RATE_MAX)], TOOLREQ_RATE_WINDOW_S):
        await db.commit()  # persist the sweep even on reject
        raise HTTPException(status_code=429, detail="too many tool requests from here — try again later")
    capability = body.capability.strip()
    if not capability:
        raise HTTPException(status_code=422, detail="say what tool/capability you need")
    if len(capability) > 200:
        raise HTTPException(status_code=422, detail="capability is a headline — keep it under 200 chars")
    org_id, user_email = None, ""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        if user is not None and not user.suspended:
            org_id, user_email = (m.org_id if m else None), user.email
    elif treg_session and _same_origin(request):
        user = await _user_from_session(treg_session, db)
        if user is not None:
            user_email = user.email
    row = ToolRequest(
        org_id=org_id,
        user_email=user_email,
        capability=capability,
        query=body.query.strip()[:300],
        note=body.note.strip()[:2000],
        contact=body.contact.strip()[:200],
        source=body.source if body.source in TOOLREQ_SOURCES else "api",
    )
    db.add(row)
    await db.commit()
    return {"id": row.id, "status": "received",
            "note": "logged — requests steer which provider gets keyed next"}


# ---- human login via GitHub OAuth (dashboard sessions) ------------------------------------
def _is_https(request: Request) -> bool:
    # behind a reverse proxy (Render), TLS is terminated upstream and forwarded as http + X-Forwarded-Proto.
    return request.headers.get("x-forwarded-proto", "").lower() == "https" or request.url.scheme == "https"


def _same_origin(request: Request) -> bool:
    """CSRF guard for cookie-authenticated mutations: the Origin header (when a browser sends one)
    must be this server itself. "Itself" is EITHER the configured public URL or the host the request
    actually arrived on — public_url alone would reject legitimate localhost/dev-box origins."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return True  # non-browser clients (and some same-origin GETs) send no Origin
    if origin == get_settings().public_url.rstrip("/"):
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if origin == f"{'https' if _is_https(request) else 'http'}://{host}":
        return True
    # `Origin: null` is a browser telling us the submitting document has an OPAQUE origin, which it
    # does after certain redirect chains — a consent form reached by way of a sign-in bounce through
    # GitHub, for instance. It is not evidence of a cross-site request, and treating it as one made
    # the OAuth consent screen fail intermittently: refused on the attempt that went through
    # sign-in, accepted on the retry that did not.
    #
    # `Sec-Fetch-Site` is the right corroboration. It is set by the browser and cannot be written by
    # script, so a page on another site cannot forge `same-origin` — which is exactly what Origin was
    # being used to prove.
    if origin == "null" and request.headers.get("sec-fetch-site") in ("same-origin", "none"):
        return True
    return False


# In-memory handshake state for `treg login` (single-instance; short-lived, fine to lose on restart).
# Both carry a created-at so abandoned handshakes (unauthenticated, attacker-chosen keys) are swept
# rather than accumulating forever — the results map holds live 30-day tokens, so it must not leak.
_cli_states: dict[str, tuple[str, datetime]] = {}   # oauth state -> (login_id, created_at)
_cli_results: dict[str, tuple[dict, datetime]] = {}  # login_id -> (result, created_at) — a completed login
# login_id -> (pairing_code, attempts_left, created_at). Created by POST /auth/cli/start; the browser must
# echo the code back at approve time (validated server-side) before a token is issued. This is the phishing
# guard: a login the user didn't start has no matching code, and the poll endpoint carries no code to
# brute-force. The code is shown ONLY in the terminal, never in the /login URL.
_cli_pending: dict[str, tuple[str, int, datetime]] = {}
CLI_TOKEN_TTL = 30 * 24 * 3600      # identity token lifetime for the CLI
HANDSHAKE_TTL = 600                  # seconds an abandoned login handshake lingers before eviction
CLI_APPROVE_MAX_TRIES = 8           # wrong pairing-code attempts before a pending login is discarded
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous (no O/0/I/1); matches the CLI's charset


def _prune_handshakes() -> None:
    cutoff = _utcnow_naive() - timedelta(seconds=HANDSHAKE_TTL)
    for k in [k for k, (_, t) in _cli_states.items() if t < cutoff]:
        _cli_states.pop(k, None)
    for k in [k for k, (_, t) in _cli_results.items() if t < cutoff]:
        _cli_results.pop(k, None)
    for k in [k for k, (_, _, t) in _cli_pending.items() if t < cutoff]:
        _cli_pending.pop(k, None)


@app.get("/auth/github")
async def auth_github(request: Request, cli: str = ""):
    s = get_settings()
    if not s.github_client_id:
        raise HTTPException(status_code=503, detail="GitHub login not configured")
    redirect = f"{_login_callback_base(request)}/auth/github/callback"
    state = crypto.new_token()
    if cli:  # this is a `treg login` handshake, not a browser session
        _prune_handshakes()  # evict abandoned handshakes so this map can't grow unbounded
        _cli_states[state] = (cli, _utcnow_naive())
    url = (f"{s.github_authorize_url}?client_id={s.github_client_id}"
           f"&redirect_uri={quote(redirect, safe='')}&scope={quote('read:user user:email')}&state={state}")
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("treg_oauth_state", state, httponly=True, max_age=600, samesite="lax", secure=_is_https(request))
    return resp


_AUTH_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>tools-registry</title>'
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">'
    "<style>"
    ':root{--bg:#151412;--panel:#1c1b19;--ink:#f2efe8;--muted:rgba(242,239,232,.55);'
    '--line:rgba(255,255,255,.1);--accent:#19D0E8;'
    '--mono:"DM Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}'
    "html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:var(--mono)}"
    "body{background:radial-gradient(90% 50% at 50% -10%,rgba(255,255,255,.04),transparent 60%),var(--bg)}"
    ".wrap{min-height:100%;display:flex;align-items:center;justify-content:center;padding:24px}"
    ".card{background:linear-gradient(180deg,#201f1d,#171614);border:1px solid var(--line);border-radius:20px;"
    "padding:34px 40px;max-width:440px;text-align:center;"
    "box-shadow:rgba(255,255,255,.08) 0 1px 0 inset, 0 30px 70px rgba(0,0,0,.5)}"
    ".logo{color:var(--accent);font-size:15px;letter-spacing:.5px;margin-bottom:18px}"
    ".mark{font-size:34px;line-height:1;margin-bottom:14px}"
    'h1{font-family:"Geist Pixel",var(--mono);font-size:22px;margin:0 0 8px;font-weight:400;letter-spacing:0}'
    "p{color:var(--muted);font-size:13.5px;line-height:1.55;margin:0}"
    ".pbtn{display:inline-block;background:linear-gradient(180deg,#fdfcf7,#eae7de);color:#1c1b19;border:0;"
    "border-radius:999px;padding:12px 24px;font:500 14px var(--mono);cursor:pointer;"
    "box-shadow:rgba(178,168,165,.2) -1.3px -1.3px 2.5px 0, rgba(0,0,0,.4) 2px 2px 1.5px 0}"
    "</style></head>"
)


def _auth_page(headline: str, sub: str = "", *, ok: bool = True, status: int = 200) -> HTMLResponse:
    """A brand-styled full-page response for the browser-facing auth flow (GitHub callback)."""
    sub_html = f"<p>{sub}</p>" if sub else ""
    html = (
        f'{_AUTH_HEAD}<body><div class="wrap"><div class="card">'
        f'<div class="logo">▚ tools-registry</div><div class="mark">{"✅" if ok else "⚠️"}</div>'
        f"<h1>{headline}</h1>{sub_html}</div></div></body></html>"
    )
    return HTMLResponse(html, status_code=status)


def _finish_oauth_login(request: Request, user: User, st: tuple | None) -> RedirectResponse:
    """After a GitHub/Google callback proves an identity: set the browser session cookie, then either
    land on the dashboard (a plain browser login) or bounce to /login?cli=<id> so a `treg login`
    handshake goes through the SAME team picker as the other doors (instead of completing blind — which
    would leave the CLI guessing the org). The picker's POST /auth/cli/approve reads this same cookie."""
    login_id = st[0] if st is not None else None
    dest = f"/login?cli={login_id}" if login_id else "/app"
    resp = RedirectResponse(dest, status_code=302)
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    resp.delete_cookie("treg_oauth_state")
    return resp


@app.get("/auth/github/callback")
async def auth_github_callback(
    request: Request, code: str = "", state: str = "",
    treg_oauth_state: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    if not code or not state or state != treg_oauth_state:  # CSRF: state must echo our cookie
        return _auth_page("Login failed", "Bad state. Please start the login again.", ok=False, status=400)
    s = get_settings()
    client = request.app.state.http
    try:
        tok = (await client.post(
            s.github_token_url, headers={"Accept": "application/json"},
            data={"client_id": s.github_client_id, "client_secret": s.github_client_secret,
                  "code": code, "redirect_uri": f"{_login_callback_base(request)}/auth/github/callback"},
        )).json()
        access = tok.get("access_token")
        if not access:
            return _auth_page("Login failed", "No access token from GitHub.", ok=False, status=400)
        gh = {"Authorization": f"Bearer {access}", "Accept": "application/json", "User-Agent": "treg"}
        prof = (await client.get(f"{s.github_api_url}/user", headers=gh)).json()
        email = prof.get("email")
        if not email:
            emails = (await client.get(f"{s.github_api_url}/user/emails", headers=gh)).json()
            if isinstance(emails, list):
                email = (next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
                         or next((e["email"] for e in emails if e.get("verified")), None))
        if not email:
            return _auth_page("Login failed", "No verified email on your GitHub account.", ok=False, status=400)
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] github callback error: {exc}")  # keep internals server-side, not in the response
        return _auth_page("Login failed", "Something went wrong. Please try again.", ok=False, status=502)

    user = await _find_or_create_user(db, email)  # first login = registration (user only; no auto org)
    if user.suspended:  # a banned account may prove its email but must not receive a live session
        return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
    await db.commit()

    # Browser session OR `treg login` handshake — both go through the /login team picker now.
    return _finish_oauth_login(request, user, _cli_states.pop(state, None))


@app.get("/auth/google")
async def auth_google(request: Request, cli: str = ""):
    """Human login via Google OAuth — a parallel door to GitHub, same session/CLI-handshake plumbing."""
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(status_code=503, detail="Google login not configured")
    redirect = f"{_login_callback_base(request)}/auth/google/callback"
    state = crypto.new_token()
    if cli:  # a `treg login` handshake, not a browser session
        _prune_handshakes()
        _cli_states[state] = (cli, _utcnow_naive())
    url = (f"{s.google_authorize_url}?client_id={s.google_client_id}"
           f"&redirect_uri={quote(redirect, safe='')}&response_type=code"
           f"&scope={quote('openid email profile')}&state={state}&prompt=select_account")
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("treg_oauth_state", state, httponly=True, max_age=600, samesite="lax", secure=_is_https(request))
    return resp


@app.get("/auth/google/callback")
async def auth_google_callback(
    request: Request, code: str = "", state: str = "",
    treg_oauth_state: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    if not code or not state or state != treg_oauth_state:  # CSRF: state must echo our cookie
        return _auth_page("Login failed", "Bad state. Please start the login again.", ok=False, status=400)
    s = get_settings()
    client = request.app.state.http
    try:
        tok = (await client.post(
            s.google_token_url, headers={"Accept": "application/json"},
            data={"client_id": s.google_client_id, "client_secret": s.google_client_secret,
                  "code": code, "grant_type": "authorization_code",
                  "redirect_uri": f"{_login_callback_base(request)}/auth/google/callback"},
        )).json()
        access = tok.get("access_token")
        if not access:
            return _auth_page("Login failed", "No access token from Google.", ok=False, status=400)
        prof = (await client.get(
            s.google_userinfo_url,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})).json()
        email = prof.get("email")
        if not email:
            return _auth_page("Login failed", "No email on your Google account.", ok=False, status=400)
        # Identity is keyed by email, so we must only trust a VERIFIED one — else an unverified Google
        # address equal to a victim's registered email would resolve to the victim (account takeover).
        # (Google's userinfo returns email_verified; the GitHub door already filters for verified.)
        if not prof.get("email_verified"):
            return _auth_page("Login failed", "Your Google email isn't verified.", ok=False, status=400)
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] google callback error: {exc}")  # keep internals server-side, not in the response
        return _auth_page("Login failed", "Something went wrong. Please try again.", ok=False, status=502)

    user = await _find_or_create_user(db, email)  # first login = registration (user only; no auto org)
    if user.suspended:
        return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
    await db.commit()

    # Browser session OR `treg login` handshake — both go through the /login team picker now.
    return _finish_oauth_login(request, user, _cli_states.pop(state, None))


def _norm_pair_code(code: str | None) -> str:
    """Normalise a login pairing code for comparison: strip, uppercase, drop separators/whitespace so
    `7f3k`, `7F3K`, ` 7F3K ` all match. Empty stays empty (an empty code never matches)."""
    return "".join((code or "").split()).replace("-", "").upper()


@app.post("/auth/cli/start")
async def auth_cli_start() -> dict:
    """`treg login` calls this FIRST. The SERVER mints both the login_id and a short pairing code and
    remembers them (pending approval). The code is shown only in that terminal; the browser must echo it
    back at approve time, where it's validated server-side, before any token is issued. So a login the
    user didn't start (a phished /login?cli=<id> link) can't be completed, and the poll endpoint carries
    no code to brute-force. Unauthenticated — on its own it grants nothing."""
    _prune_handshakes()
    login_id = _secrets.token_urlsafe(18)
    code = "".join(_secrets.choice(_PAIR_ALPHABET) for _ in range(4))
    _cli_pending[login_id] = (code, CLI_APPROVE_MAX_TRIES, _utcnow_naive())
    return {"login_id": login_id, "code": code}


@app.get("/auth/cli/poll")
async def auth_cli_poll(login_id: str = "") -> dict:
    """The CLI polls this after opening the browser; returns the identity token once, then forgets it.
    A token only lands here after auth_cli_approve validated the terminal pairing code, so a login the
    user didn't approve never yields one — there is nothing here to brute-force (no code parameter)."""
    _prune_handshakes()  # sweep abandoned results (they hold live tokens) so the map can't leak
    entry = _cli_results.pop(login_id, None)
    return entry[0] if entry is not None else {"status": "pending"}


# `treg login` mints the login_id with token_urlsafe(18) (24 chars); anything outside this shape is
# not one of ours. It's echoed into the /login page's JS, so the whitelist is also the XSS guard.
_LOGIN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


class CliApproveIn(BaseModel):
    login_id: str
    code: str | None = None  # the pairing code the user copied from their terminal (phishing guard)
    org: str | None = None  # the team slug the user picked in the /login org picker (optional)


async def _orgs_brief(user: User, db: AsyncSession) -> list[dict]:
    """The user's teams for the /login picker: slug, name, role, tool_count, personal. Sorted so the
    team a CLI login should default to sits first (a real team over the personal org, then most tools).
    `personal` mirrors the dashboard's rule: the auto-created org named after the user's email."""
    memberships = (await db.execute(
        select(Membership).where(Membership.user_id == user.id))).scalars().all()
    org_ids = [m.org_id for m in memberships]
    if not org_ids:
        return []
    orgs = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_(org_ids)))).scalars().all()}
    counts = dict((await db.execute(
        select(Tool.org_id, func.count(Tool.id)).where(Tool.org_id.in_(org_ids)).group_by(Tool.org_id))).all())
    out = []
    for m in memberships:
        o = orgs.get(m.org_id)
        if o is None:
            continue
        out.append({"slug": o.slug, "name": o.name, "role": m.role,
                    "tool_count": counts.get(o.id, 0), "personal": o.name == user.email})
    out.sort(key=lambda r: (r["personal"], -r["tool_count"], r["name"].lower()))
    return out


@app.get("/auth/cli/orgs")
async def auth_cli_orgs(treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session)) -> dict:
    """The /login page fetches this (session-cookie authed) to render the team picker before completing
    a `treg login` handshake. Returns the signed-in user's teams; empty list if no session."""
    user = await _user_from_session(treg_session, db)
    if user is None:
        return {"email": None, "orgs": []}
    return {"email": user.email, "orgs": await _orgs_brief(user, db)}


@app.post("/auth/cli/approve")
async def auth_cli_approve(
    request: Request, body: CliApproveIn,
    treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
) -> dict:
    """Complete a `treg login` handshake from an EXISTING browser session (the "Continue as" button
    on /login, and the email door after /auth/email/verify sets the cookie). Deliberately a POST with
    a same-origin check — auto-completing on a GET would let a phisher mail out /login?cli=<their-id>
    and poll the victim's identity token straight out of /auth/cli/poll.

    `org` (optional) is the team slug the user picked in the /login org picker; it's validated to be
    one of the user's memberships and passed back to the CLI so it lands on the RIGHT team instead of
    guessing (`_pick_active_org`)."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin approve rejected")
    if not _LOGIN_ID_RE.fullmatch(body.login_id or ""):
        raise HTTPException(status_code=400, detail="bad login_id")
    user = await _user_from_session(treg_session, db)
    if user is None:
        raise HTTPException(status_code=401, detail="no session")
    # The pairing code proves the approver is the same person who ran `treg login` (the code is shown only
    # in that terminal, via POST /auth/cli/start). Validate it HERE — where we have a session + a same-
    # origin check — so a phished /login?cli=<attacker_id> link (whose code the victim doesn't have) can
    # never complete, a mistyped code fails immediately in the browser, and the poll endpoint stays codeless.
    pending = _cli_pending.get(body.login_id)
    if pending is None:
        raise HTTPException(status_code=400, detail="this login has expired — run `treg login` again")
    expected, tries_left, started_at = pending
    typed = _norm_pair_code(body.code)
    if not typed or not hmac.compare_digest(expected.encode(), typed.encode()):
        if tries_left <= 1:  # out of attempts → discard the pending login so the code can't be ground down
            _cli_pending.pop(body.login_id, None)
            raise HTTPException(status_code=400, detail="too many wrong codes — run `treg login` again")
        _cli_pending[body.login_id] = (expected, tries_left - 1, started_at)
        raise HTTPException(status_code=400, detail="that code doesn't match the one in your terminal")
    active_org: str | None = None
    if body.org:
        org = await _resolve_org(body.org, db)
        m = (await db.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org.id))).scalar_one_or_none() if org else None
        if org is None or m is None:
            raise HTTPException(status_code=403, detail="not a member of that team")
        active_org = org.slug
    _cli_pending.pop(body.login_id, None)  # code matched → consume the pending login
    result = {"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version), "email": user.email}
    if active_org:
        result["active_org"] = active_org
    _cli_results[body.login_id] = (result, _utcnow_naive())
    return {"ok": True, "email": user.email, "active_org": active_org}


@app.get("/login", include_in_schema=False)
async def login_page(cli: str = "", treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session)):
    """The universal sign-in page `treg login` opens: reuses an existing dashboard session with one
    click ("Continue as …"), else offers every configured door — GitHub, Google, email one-time code.
    The email door is always present, so login works even with no OAuth app configured."""
    if not cli:
        return RedirectResponse("/app", status_code=302)  # a bare visit belongs on the dashboard
    if not _LOGIN_ID_RE.fullmatch(cli):
        return _auth_page("Login failed", "Bad login link. Run <code>treg login</code> again.", ok=False, status=400)
    s = get_settings()
    user = await _user_from_session(treg_session, db)
    return HTMLResponse(_login_page_html(
        cli, session_email=user.email if user else None,
        github=bool(s.github_client_id), google=bool(s.google_client_id)))


def _login_page_html(login_id: str, *, session_email: str | None, github: bool, google: bool) -> str:
    """Server-rendered /login card. login_id is whitelist-validated by the caller; the session email
    is HTML-escaped (it's the only other interpolated value)."""
    from html import escape

    # A pairing-code block sits above everything: whichever door the user takes, approve() won't complete
    # the CLI handshake until the code shown in their own terminal is echoed back (phishing guard — a
    # login they didn't start has no matching code). A `treg login` link carries the code in the URL
    # fragment, so the JS swaps this input for a read-only display the user just visually confirms;
    # the typed input remains the fallback for links without one. #orgpick is ALWAYS present (filled by loadOrgs when a
    # session exists at load, and after the email door signs in). #doors holds the sign-in options; the
    # divider only shows when a session pre-exists.
    parts: list[str] = [
        '<div id="pcbox"><div class="pklabel">Enter the code shown in your terminal:</div>'
        '<input id="paircode" autocomplete="off" autocapitalize="characters" spellcheck="false" '
        'inputmode="latin" placeholder="e.g. 7F3K" maxlength="9"></div>',
        '<div id="orgpick"></div>',
    ]
    # With a live session the doors are noise — the user is one click from done. Collapse them behind
    # the divider (an accordion); a click expands. No session → no divider, doors always visible.
    if session_email:
        parts.append('<div class="div acc" id="other-acct" onclick="toggleDoors()" role="button" tabindex="0" '
                     'onkeydown="if(event.key===\'Enter\')toggleDoors()">'
                     'use a different account <span id="acc-caret">▸</span></div>')
    doors: list[str] = []
    if github:
        doors.append(f'<a class="btn" href="/auth/github?cli={login_id}">Sign in with GitHub</a>')
    if google:
        doors.append(f'<a class="btn" href="/auth/google?cli={login_id}">Sign in with Google</a>')
    doors.append(
        '<div id="email-door">'
        '<div id="email-row"><input id="em" type="email" placeholder="you@company.com" autocomplete="email">'
        '<button class="btn" onclick="sendCode()">Email me a code</button></div>'
        '<div id="code-row" style="display:none"><input id="code" inputmode="numeric" placeholder="6-digit code">'
        '<button class="btn primary" onclick="verifyCode()">Verify</button></div>'
        '<div class="hint" id="hint"></div></div>')
    doors_style = ' style="display:none"' if session_email else ''
    parts.append(f'<div id="doors" class="stack"{doors_style}>{"".join(doors)}</div>')
    has_session = "true" if session_email else "false"
    return (
        f"{_AUTH_HEAD.replace('</style>', _LOGIN_CSS + '</style>')}"
        f'<body><div class="wrap"><div class="card" id="card">'
        f'<div class="logo">▚ tools-registry</div><h1>Sign in</h1>'
        f'<p>to connect the <b>treg</b> CLI to your account</p>'
        f'<div class="stack">{"".join(parts)}</div><div class="err" id="err"></div>'
        f"</div></div>"
        f"<script>const HAS_SESSION={has_session};{_LOGIN_JS.replace('__LOGIN_ID__', login_id)}</script></body></html>"
    )


_LOGIN_CSS = (
    ".btn{display:block;width:100%;box-sizing:border-box;padding:11px 14px;border-radius:9px;"
    "border:1px solid var(--line);background:#332d23;color:var(--ink);font-family:var(--mono);"
    "font-size:13.5px;cursor:pointer;text-decoration:none;text-align:center}"
    ".btn:hover{border-color:var(--accent)}"
    ".btn.primary{background:var(--accent);border-color:var(--accent);color:#211d16;font-weight:700}"
    ".stack{display:flex;flex-direction:column;gap:10px;margin-top:18px}"
    ".stack>div{display:flex;flex-direction:column;gap:10px}"
    ".div{display:flex;flex-direction:row!important;align-items:center;gap:10px;color:var(--muted);font-size:12px;margin:6px 0 0}"
    ".div:before,.div:after{content:'';flex:1;border-top:1px solid var(--line)}"
    ".div.acc{cursor:pointer;user-select:none}.div.acc:hover{color:var(--ink)}"
    "#email-row,#code-row{display:flex;flex-direction:column;gap:10px}"
    "input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid var(--line);"
    "background:#1c1913;color:var(--ink);font-family:var(--mono);font-size:13.5px}"
    ".err{color:#d78f6c;font-size:12.5px;margin-top:10px;min-height:1em}"
    ".hint{color:var(--muted);font-size:12px}"
    ".muted{color:var(--muted)}"
    ".team{display:flex;justify-content:space-between;align-items:center;gap:10px;text-align:left}"
    ".team .tn{display:flex;flex-direction:column;gap:2px;min-width:0}"
    ".team .tnm{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".team .tm{font-size:11px;color:var(--muted)}"
    ".team.primary .tm{color:#211d16;opacity:.8}"
    ".pklabel{font-size:12px;color:var(--muted);margin:2px 0 2px}"
    "#paircode-show{font-size:22px;font-weight:700;letter-spacing:8px;text-align:center;color:var(--accent);"
    "padding:10px 12px 10px 20px;border:1px dashed var(--line);border-radius:9px;background:#1c1913}"
)

# The page's whole brain: every door funnels into approve(), which completes the CLI handshake.
# done() builds DOM via textContent (the email came over JSON — never trust it into innerHTML).
_LOGIN_JS = """
const LID='__LOGIN_ID__';
// `treg login` puts the pairing code in the URL FRAGMENT (#code=…) — it never reaches the server on
// the GET. When present, show it read-only for a visual match against the terminal instead of making
// the user type it; approve() still sends it for full server-side validation. No fragment (an old CLI,
// or a link someone stripped it from) → the typed-input fallback below stays.
const PAIR=(()=>{const m=/[#&]code=([A-Za-z0-9-]{1,16})/.exec(location.hash||'');return m?m[1].toUpperCase():''})();
if(PAIR){const box=document.getElementById('pcbox');if(box){box.innerHTML='';
 const l=document.createElement('div');l.className='pklabel';l.textContent='Check this code matches your terminal:';box.appendChild(l);
 const c=document.createElement('div');c.id='paircode-show';c.textContent=PAIR;box.appendChild(c);}}
const pairCode=()=>PAIR||((document.getElementById('paircode')||{}).value||'');
// Signed-in users see the doors collapsed behind the "use a different account" divider.
function toggleDoors(){const d=document.getElementById('doors');if(!d)return;
 const open=d.style.display==='none';d.style.display=open?'':'none';
 const c=document.getElementById('acc-caret');if(c)c.textContent=open?'\\u25be':'\\u25b8'}
const err=m=>{document.getElementById('err').textContent=m||''};
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 let d={};try{d=await r.json()}catch(e){}
 if(!r.ok)throw new Error(d.detail||('error '+r.status));return d}
function done(email){const c=document.getElementById('card');c.innerHTML='';
 const mk=(t,cls,txt)=>{const e=document.createElement(t);if(cls)e.className=cls;if(txt)e.textContent=txt;c.appendChild(e);return e};
 mk('div','logo','\\u259a tools-registry');mk('div','mark','\\u2705');
 mk('h1',null,email?('Logged in as '+email):'Logged in');
 mk('p',null,'Return to your terminal. The CLI is finishing up.');
 // Don't strand the tab: the approve() above set a session cookie, so /app works. Count down to
 // Getting started, but let a click beat the clock — and a second click on "stay" cancel it.
 const row=mk('p','hint','');let left=10;
 const go=document.createElement('a');go.href='/app#start';go.textContent='Open Getting started now \\u2192';
 go.style.color='var(--accent)';row.appendChild(go);
 const cnt=document.createElement('span');row.appendChild(cnt);
 const stay=document.createElement('a');stay.href='#';stay.textContent='stay here';stay.className='muted';
 stay.style.marginLeft='8px';stay.style.textDecoration='underline';row.appendChild(stay);
 const tick=()=>{cnt.textContent=' \\u00b7 auto in '+left+'s \\u00b7 '};tick();
 const timer=setInterval(()=>{left--;if(left<=0){clearInterval(timer);location.href='/app#start'}else tick()},1000);
 stay.onclick=e=>{e.preventDefault();clearInterval(timer);row.textContent='';
  const a=document.createElement('a');a.href='/app#start';a.textContent='Open Getting started \\u2192';
  a.style.color='var(--accent)';row.appendChild(a)}}
async function approve(org){err('');
 const pc=pairCode();
 if(!pc.trim()){err('Enter the code shown in your terminal to continue.');const el=document.getElementById('paircode');if(el)el.focus();return}
 try{const b={login_id:LID,code:pc};if(org)b.org=org;const d=await post('/auth/cli/approve',b);done(d.email)}catch(e){err(e.message)}}
let CREATED_ORG=null;  // remember a just-created team so a retry (e.g. after a wrong code) reuses it, never makes a 2nd
async function createTeam(){err('');
 const pc=pairCode();
 if(!pc.trim()){err('Enter the code shown in your terminal to continue.');const el=document.getElementById('paircode');if(el)el.focus();return}
 const inp=document.getElementById('newteam');const name=(inp&&inp.value||'').trim();if(!name)return err('give your team a name');
 try{if(!CREATED_ORG){const o=await post('/orgs',{name:name});CREATED_ORG=o.org;}await approve(CREATED_ORG);}catch(e){err(e.message)}}
// Render the team picker into #orgpick once a session exists (fetched, so it also runs after the
// email door signs in). One team → a single "Continue as" button; many → a labelled list.
async function loadOrgs(){const box=document.getElementById('orgpick');if(!box)return;
 let d;try{d=await(await fetch('/auth/cli/orgs',{credentials:'include'})).json()}catch(e){return}
 const orgs=d.orgs||[];box.innerHTML='';
 if(!orgs.length){  // brand-new user: no team yet → make them NAME one (never finish the CLI login team-less)
  const l=document.createElement('div');l.className='pklabel';l.textContent='Name your team to finish signing in'+(d.email?(' ('+d.email+')'):'')+':';box.appendChild(l);
  const inp=document.createElement('input');inp.id='newteam';inp.placeholder='Team name, e.g. Superdesign';inp.autocomplete='off';box.appendChild(inp);
  const b=document.createElement('button');b.className='btn primary';b.textContent='Create team \\u2192';b.onclick=createTeam;box.appendChild(b);
  inp.addEventListener('keyup',e=>{if(e.key==='Enter')createTeam()});inp.focus();return}
 if(orgs.length>1){const l=document.createElement('div');l.className='pklabel';l.textContent='Continue as '+d.email+' — pick a team:';box.appendChild(l)}
 orgs.forEach((o,i)=>{const b=document.createElement('button');b.className='btn team'+((i===0&&orgs.length>1)?' primary':'');b.onclick=()=>approve(o.slug);
  const tn=document.createElement('div');tn.className='tn';
  const nm=document.createElement('div');nm.className='tnm';nm.textContent=o.name+(o.personal?' (personal)':'');
  const mt=document.createElement('div');mt.className='tm';mt.textContent=o.role+' · '+o.tool_count+' tool'+(o.tool_count===1?'':'s');
  tn.appendChild(nm);tn.appendChild(mt);b.appendChild(tn);
  if(orgs.length===1){const c=document.createElement('span');c.textContent='→';b.appendChild(c)}
  box.appendChild(b)});
 if(orgs.length===1){box.firstChild.classList.add('primary')}}
async function sendCode(){err('');const em=document.getElementById('em').value.trim();if(!em)return err('enter your email');
 try{const d=await post('/auth/email/start',{email:em});
  document.getElementById('code-row').style.display='';
  document.getElementById('hint').textContent=d.dev_code?('dev code: '+d.dev_code):('code sent to '+d.email);
 }catch(e){err(e.message)}}
async function verifyCode(){err('');const em=document.getElementById('em').value.trim(),co=document.getElementById('code').value.trim();
 if(!co)return err('enter the code');
 try{await post('/auth/email/verify',{email:em,code:co});
  // The email door just set a session cookie — hide the doors and show the team picker.
  const d=document.getElementById('doors');if(d)d.style.display='none';
  const o=document.getElementById('other-acct');if(o)o.style.display='none';
  await loadOrgs();
 }catch(e){err(e.message)}}
if(HAS_SESSION)loadOrgs();
"""


def _intercom_user_hash(email: str) -> str:
    """Intercom identity verification: HMAC-SHA256 of the identifier the dashboard boots the
    Messenger with (the email), keyed by the workspace secret — so a third party who knows an email
    can't impersonate that user in support chat. Empty when unconfigured (self-hosted: no widget)."""
    secret = get_settings().intercom_secret
    if not secret:
        return ""
    return hmac.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()


@app.get("/auth/me")
async def auth_me(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Who is the caller? Drives the dashboard's identity display in BOTH session mode (cookie) and
    token mode (X-Treg-Token) — the token door otherwise had no way to learn its own email, which
    broke `isPersonal` and join-by-code."""
    membership = None
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        membership = m
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        if user is not None and user.suspended:
            user = None
    else:
        user = await _user_from_session(treg_session, db)
    if user is None:
        raise HTTPException(status_code=401, detail="no session")
    out = {"email": user.email, "is_superadmin": user.is_superadmin, "onboarded": user.onboarded,
           "github": bool(get_settings().github_client_id)}
    if (ich := _intercom_user_hash(user.email)):
        out["intercom_user_hash"] = ich
    if membership is not None:
        # The org this token IS. A machine identity cannot call GET /orgs — `require_identity`
        # refuses it on purpose, since `create_org` hangs off that dependency and an agent could
        # otherwise mint an org it owns. But it still has to learn its OWN org id to reach any
        # /orgs/{id}/... route, and being unable to told `treg balance` there was "no active org".
        org = await db.get(Org, membership.org_id)
        out |= {"org_id": membership.org_id, "org": org.slug if org else None,
                "role": membership.role}
    return out


@app.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    # A cross-site auto-submitted form could force-logout the victim (the cookie delete is a "simple"
    # request). Bind it to same-origin: reject a request whose Origin isn't treg's own.
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin logout rejected")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sess.COOKIE)
    return resp


# ---- human login via email one-time code (the third identity door) ------------------------
# OTP code + its brute-force counter, and the /auth/email/start throttle, live in the DB (treg.ratestore
# over the Ephemeral table) — NOT per-process dicts — so a restart can't reset them and they stay correct
# across instances (backlog #3). The 'otp' namespace holds {code_hash, attempts} keyed by email; the
# 'otp_start' namespace holds the per-email + per-IP sliding windows (email-bomb + brute-force guard).
EMAIL_CODE_TTL = 10 * 60  # seconds a code stays valid
MAX_OTP_ATTEMPTS = 5  # invalidate a code after this many wrong guesses (brute-force guard)
OTP_NS = "otp"
OTP_START_NS = "otp_start"
OTP_START_WINDOW_S = 900      # 15 minutes
OTP_START_MAX_PER_EMAIL = 5   # code requests for one inbox per window (caps bombing a single victim)
OTP_START_MAX_PER_IP = 30     # code requests from one IP per window (looser — offices/NAT share an IP)


class EmailStartIn(BaseModel):
    email: str


class EmailVerifyIn(BaseModel):
    email: str
    code: str


@app.post("/auth/email/start")
async def auth_email_start(
    request: Request, body: EmailStartIn, db: AsyncSession = Depends(get_session)
) -> dict:
    """Prove ownership of an email: mint a 6-digit code. With no mail sender yet, dev mode returns
    + logs it (so dummy emails are testable); prod will email it instead. Throttled per-email AND per-IP
    (sliding window) so this open endpoint can't be used to email-bomb an inbox or reset the OTP
    brute-force counter at will. All this state is in the DB (survives restart, correct multi-instance)."""
    email = _norm_email(body.email)
    if email.endswith("@" + demo_seed.DEMO_DOMAIN):  # fake onboarding teammates are roster-only — never a login
        raise HTTPException(status_code=400, detail="that's a demo address — pick a real email")
    if _is_machine_email(email):  # agents / the public token act by token only — same rule, said early
        raise HTTPException(status_code=403, detail="this address cannot be used to sign in")
    await ratestore.sweep(db, OTP_START_NS)  # bound the namespace before we add to it
    if not await ratestore.rate_check(
        db, OTP_START_NS,
        [(f"e:{email}", OTP_START_MAX_PER_EMAIL), (f"i:{_client_ip(request)}", OTP_START_MAX_PER_IP)],
        OTP_START_WINDOW_S,
    ):
        await db.commit()  # persist the pruning/sweep even on reject
        raise HTTPException(status_code=429, detail="too many code requests — please wait a few minutes")
    code = f"{_secrets.randbelow(1_000_000):06d}"
    await ratestore.kv_put(db, OTP_NS, email,
                           {"hash": crypto.hash_token(code), "attempts": MAX_OTP_ATTEMPTS}, EMAIL_CODE_TTL)
    await db.commit()
    resp = {"sent": True, "email": email}
    if get_settings().expose_dev_code:  # local sqlite only — never leaks the code on a real (Postgres) deploy
        print(f"[email-otp] {email} -> {code}")  # surfaces in the server log
        resp["dev_code"] = code
    else:
        await email_sender.send_otp(email, code, ttl_minutes=EMAIL_CODE_TTL // 60)  # best-effort; never raises
    return resp


@app.post("/auth/email/verify")
async def auth_email_verify(
    request: Request, body: EmailVerifyIn, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Check the code → find-or-create the user → mint an identity token AND set a browser session
    cookie. The CLI reads the token from the body; the dashboard just reloads into session mode
    (same path as GitHub login) — one endpoint serves both clients."""
    email = _norm_email(body.email)
    entry = await ratestore.kv_get(db, OTP_NS, email)  # None if missing OR expired (kv_get drops expired)
    if entry is None:
        await db.commit()  # persist the lazy delete of an expired code, if any
        raise HTTPException(status_code=401, detail="invalid code")
    if not hmac.compare_digest(entry["hash"], crypto.hash_token(body.code.strip())):
        entry["attempts"] -= 1  # a wrong guess burns an attempt; the code dies after MAX_OTP_ATTEMPTS
        if entry["attempts"] <= 0:
            await ratestore.kv_pop(db, OTP_NS, email)
        else:
            await ratestore.kv_put(db, OTP_NS, email, entry, ttl_s=None)  # keep the code's original expiry
        await db.commit()
        raise HTTPException(status_code=401, detail="invalid code")
    await ratestore.kv_pop(db, OTP_NS, email)  # one-time
    user = await _find_or_create_user(db, email)
    if user.suspended:  # a banned account may prove its email but must not receive a live token
        raise HTTPException(status_code=403, detail="account suspended")
    await db.commit()
    resp = JSONResponse({"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version), "email": user.email})
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


async def _live_invite_by_email_token(db: AsyncSession, t: str) -> Invite | None:
    """Resolve an emailed invite-link token to a live invite: pending, unexpired, unconsumed
    (email_token_hash is nulled on first use), and not pointing at a platform-locked org."""
    t = (t or "").strip()
    if not t:
        return None
    invite = (await db.execute(select(Invite).where(Invite.email_token_hash == crypto.hash_token(t)))
              ).scalar_one_or_none()
    if (invite is None or invite.status != "pending"
            or (invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive())):
        return None
    org = await db.get(Org, invite.org_id)
    if org is None or org.suspended:
        return None
    return invite


@app.get("/auth/invite-signin")
async def auth_invite_signin(
    request: Request, code: str = "", t: str = "",
    treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    """Landing for an invite email link. Two secrets, two very different trust levels:

    `t` (email_token) exists ONLY in the emailed link — possession proves inbox access, the same bar
    as the emailed OTP — so it may sign the invitee in. But not on this GET: corporate mail scanners
    (Outlook SafeLinks etc.) prefetch GET links and would consume a one-time credential before the
    human ever clicks. So the GET only renders a confirm page whose button POSTs the token back;
    the POST below mints the session.

    `code` (legacy + out-of-band) is also returned to the admin who created the invite, so it can
    NEVER be an authentication factor — holding it lets you JOIN (POST /invites/accept), not log in.
    Links carrying ?code= (emails sent before the split, or relayed by an admin) keep their old
    behavior: validate and bounce to the SPA login with the email prefilled; the invitee proves the
    email through a real door (OTP / GitHub / Google) and the invite auto-appears via /invites/mine.
    An invalid/expired secret of either kind just lands on the site."""
    from urllib.parse import quote
    base = get_settings().public_url.rstrip("/")
    if t:
        invite = await _live_invite_by_email_token(db, t)
        if invite is None:
            return RedirectResponse("/?invite_expired=1", status_code=303)
        org = await db.get(Org, invite.org_id)
        # Already signed in as someone ELSE? Warn — continuing replaces that browser session.
        switch_note = ""
        uid = sess.read(treg_session)
        if uid is not None:
            current = await db.get(User, uid)
            if current is not None and current.email != invite.email:
                switch_note = (f"<p>You're currently signed in as <b>{_esc_html(current.email)}</b> — "
                               f"continuing switches this browser to <b>{_esc_html(invite.email)}</b>.</p>")
        return HTMLResponse(
            f'{_AUTH_HEAD}<body><div class="wrap"><div class="card">'
            f'<div class="logo">▚ tools-registry</div><div class="mark">👋</div>'
            f'<h1>Join {_esc_html(org.name if org else "the team")}</h1>'
            f'<p><b>{_esc_html(invite.invited_by or "A teammate")}</b> invited '
            f'<b>{_esc_html(invite.email)}</b> as {_esc_html(invite.role)}.</p>{switch_note}'
            f'<form method="post" action="/auth/invite-signin" style="margin-top:18px">'
            f'<input type="hidden" name="t" value="{_esc_html(t.strip())}">'
            f'<button type="submit" class="pbtn">'
            f'Continue as {_esc_html(invite.email)} →</button></form>'
            f"</div></div></body></html>"
        )
    c = (code or "").strip()
    invite = (await db.execute(select(Invite).where(Invite.code_hash == crypto.hash_token(c)))
              ).scalar_one_or_none() if c else None
    if (invite is None or invite.status != "pending"
            or (invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive())):
        return RedirectResponse("/?invite_expired=1", status_code=303)
    # Code path: same redirect whether or not the email already has an account — the code is a
    # convenience that prefills the sign-in email, never an authentication factor. A suspended
    # account is caught at the real login door, the only place the code path can mint a session.
    return RedirectResponse(f"/?invite={quote(invite.email)}", status_code=303)


@app.post("/auth/invite-signin")
async def auth_invite_signin_confirm(request: Request, db: AsyncSession = Depends(get_session)):
    """The confirm page's POST: the emailed one-time token signs the invitee in. Mirrors the OTP
    door (auth_email_verify) — find-or-create the user, refuse the suspended, set the session
    cookie — because the trust source is identical: only the inbox saw this secret. The token is
    consumed here (one-time) so a link floating in a forwarded thread can't be replayed; the invite
    itself stays PENDING — acceptance happens in the dashboard, where a multi-team invitee can
    accept several at once. Body is parsed by hand (urlencoded form) to avoid the python-multipart
    dependency FastAPI's Form() would pull in."""
    from urllib.parse import parse_qs
    try:
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — any junk body = no token
        form = {}
    t = (form.get("t", [""])[0] or "").strip()
    invite = await _live_invite_by_email_token(db, t)
    if invite is None:  # consumed / expired / revoked / suspended org → the SPA's expired banner
        return RedirectResponse("/?invite_expired=1", status_code=303)
    user = await _find_or_create_user(db, invite.email)  # first click = registration (user only, no auto org)
    if user is None or user.suspended:  # a banned account may hold the link but must not get a session
        return _auth_page("Account suspended", "This account has been suspended.", ok=False, status=403)
    invite.email_token_hash = None  # consume: one sign-in per emailed link
    db.add(invite)
    await db.commit()
    # A share-born invite lands on the shared page itself (the SPA auto-accepts + switches org);
    # a plain invite lands on the dashboard with the accept banner, as before. `landing` was
    # allowlist-validated at create time, so this can never redirect off-app.
    dest = f"{invite.landing}?invite_org={invite.org_id}" if invite.landing else f"/?invite_org={invite.org_id}"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


def _esc_html(s: str) -> str:
    """The stdlib escaper, not a hand-rolled replace() chain.

    Same four substitutions as before plus `'` -> `&#x27;`, so every call site is at least as safe.
    The reason to delegate is not correctness but legibility to tooling: static analysis models
    `html.escape` as an XSS sanitizer and cannot know that a private chain of `.replace()` calls is
    one, so every escaped value stayed 'tainted' and the real sinks were buried in false positives.
    """
    return _html.escape(str(s), quote=True)


@app.get("/", include_in_schema=False)
async def landing(request: Request, treg_session: str = Cookie(default=""),
                  db: AsyncSession = Depends(get_session)):
    """Serve the marketing landing at the root. Any query string (invite links, OAuth returns,
    tour deep-links) belongs to the SPA, so those requests fall through to the dashboard —
    the landing is only the clean, parameterless front door. A signed-in visitor belongs on
    the dashboard, so a live session redirects to /app instead of re-showing the pitch.

    `?ref=<code>` is the ONE exception, and it has to be: a referral link's whole job is to show a
    stranger the pitch. Falling through to the SPA would send someone who has never heard of treg
    to an empty dashboard shell — so a lone `ref` counts as parameterless, and the code is parked in
    a cookie on the way past. It is only redeemed much later, when they create their first team.
    """
    page = _WEB_DIR / "landing.html"
    ref = referrals.normalize_code(request.query_params.get("ref", ""))
    # Only `ref` may be present. Anything else alongside it belongs to the SPA, and a referral code
    # is not a reason to hijack an invite or an OAuth return.
    ref_only = set(request.query_params.keys()) <= {"ref"}
    if page.exists() and (not request.query_params or (ref and ref_only)):
        if treg_session and await _user_from_session(treg_session, db):
            return RedirectResponse("/app", status_code=302)
        # Read-and-substitute rather than a bare FileResponse: the canonical, og:url and og:image
        # are `{BASE}`-templated so they name the serving host. Hardcoded, a self-hosted registry
        # would tell crawlers its front page really lives on treg.to.
        html = page.read_text(encoding="utf-8").replace(
            "{BASE}", get_settings().public_url.rstrip("/"))
        resp = HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        if ref:
            _remember_referral(resp, request, ref)
        return resp
    return await dashboard(request, treg_session, db)


@app.get("/app", include_in_schema=False)
async def dashboard(
    request: Request, treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
):
    """Serve the single-file dashboard (same-origin, so it calls this API directly).

    Also the place a parked OAuth authorization resumes. Every browser sign-in door — GitHub, Google,
    the email code — ends here, so honouring the cookie at this ONE point covers all of them, rather
    than threading a return value through five handlers that each finish differently (two redirect,
    one answers JSON).

    In frictionless local mode the dashboard opens ALREADY SIGNED IN: with no valid session we
    attach one for the machine's single user, so `curl … | sh` reaches a working dashboard without
    an account. Only reachable when `single_user_ok` holds (local sqlite + loopback URL), so this
    can never hand a session to a stranger on a real deploy.
    """
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    signed_in = await _user_from_session(treg_session, db)
    # A parked authorization resumes here, but ONLY once the user is actually signed in — otherwise
    # this would bounce them back to /oauth/authorize, which would bounce them here again.
    if signed_in and (parked := _take_oauth_return(request)) is not None:
        resume = RedirectResponse(parked, status_code=302)
        resume.delete_cookie(OAUTH_RETURN_COOKIE)
        return resume
    resp = FileResponse(index, headers={"Cache-Control": "no-cache"})
    if not signed_in:
        owner = await _local_owner(db)
        if owner is not None:
            resp.set_cookie(sess.COOKIE, sess.make(owner.id, token_version=owner.token_version),
                            httponly=True, samesite="lax",
                            secure=_is_https(request),
                            max_age=sess.TTL_SECONDS)
    return resp


def _spa_with_og(kind: str, name: str):
    """Serve the SPA at a shareable detail path (/app/skills/x, /app/tools/x) with per-resource
    og/twitter meta so link unfurls show what was shared. The meta echoes only the URL's own
    name segment — no DB read, so an unauthenticated crawler learns nothing it didn't send."""
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    label = "skill" if kind == "skills" else "tool"
    safe = _esc_html(name)
    meta = (
        f"<title>{safe} · Treg</title>\n"
        f'<meta property="og:title" content="{safe} — shared {label}"/>\n'
        f'<meta property="og:description" content="A {label} shared via Treg. '
        f'Sign in to preview it and get the one-command install."/>\n'
        f'<meta name="twitter:card" content="summary"/>'
    )
    # Match WHATEVER title the page carries, not one exact string. It was pinned to
    # `<title>tools-registry</title>`, the page says `<title>treg</title>`, so the replacement
    # silently did nothing and every shared link unfurled blank — a rename in the dashboard must
    # not be able to switch this off without a word.
    html, hits = re.subn(r"<title>.*?</title>", lambda _m: meta, index.read_text(encoding="utf-8"),
                         count=1, flags=re.IGNORECASE | re.DOTALL)
    if not hits:  # no title at all: still emit the meta rather than serve a bare page
        html = html.replace("<head>", "<head>\n" + meta, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/app/marketplace/{service}", include_in_schema=False)
async def dashboard_marketplace(
    service: str, request: Request, treg_session: str = Cookie(default=""),  # noqa: ARG001 — the SPA reads the path itself
    db: AsyncSession = Depends(get_session),
):
    """One integration's page. Served as the plain SPA: unlike /app/skills/<x> there is no og meta
    to add, because a marketplace page is only meaningful to a signed-in member of the org."""
    return await dashboard(request, treg_session, db)


@app.get("/app/skills/{name}", include_in_schema=False)
async def dashboard_skill_page(name: str):
    return _spa_with_og("skills", name)


@app.get("/app/tools/{name}", include_in_schema=False)
async def dashboard_tool_page(name: str):
    return _spa_with_og("tools", name)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    """Agent-readable overview (llms.txt convention) — an AI agent that fetches this learns the
    whole registry: the call protocol, discovery, auth, CLI, skills, and links to the tutorial/docs.
    The serving domain is templated in so links stay correct across deploys."""
    f = _WEB_DIR / "llms.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="llms.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/plain; charset=utf-8")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """Crawler policy. `{BASE}`-templated like llms.txt, so a self-hosted registry advertises its own
    sitemap rather than treg.to's."""
    f = _WEB_DIR / "robots.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="robots.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "max-age=3600"})


# The outcome landing pages: one per vertical, the destinations for search ads and the organic
# `/use-cases/` cluster. Their COPY is generated from marketing/landing/*.md — never hand-edit
# the HTML in web/, it is overwritten by that build. The slug is the public URL and is quoted in
# live ad campaigns, so treat this map as an API: add freely, never rename or remove without a
# redirect.
_USE_CASES = {
    "seo-data-for-ai-agents": "usecase-seo.html",
    "lead-enrichment-for-ai-agents": "usecase-enrichment.html",
    "social-trend-research-for-ai-agents": "usecase-social.html",
    "competitor-ad-research-for-ai-agents": "usecase-ads.html",
    "company-research-for-ai-agents": "usecase-company.html",
}


# The pages a crawler should know about. Everything here must answer 200 to a GET — a sitemap that
# lists a redirect or a 404 is worse than no sitemap, so `tests/test_seo.py` walks every entry.
# Deliberately absent: /contact and /help (alias URLs for the one support.html), /vendor-listing.md
# (the text/plain twin of /vendor-listing), /login (302s to /app), /app* (authenticated SPA),
# /connect-demo (noindex by design), and the shell installers.
_SITEMAP_PAGES: tuple[tuple[str, str, str], ...] = (
    # (path, source file for lastmod — "" means use the catalog's, priority)
    ("/", "landing.html", "1.0"),
    ("/catalog", "", "0.9"),
    ("/tutorial", "tutorial.html", "0.8"),
    ("/docs", "", "0.7"),
    ("/resources", "resources.html", "0.8"),
    ("/vendor-listing", "vendor-listing.md", "0.5"),
    ("/support", "support.html", "0.4"),
    ("/terms", "terms.html", "0.2"),
    ("/privacy", "privacy.html", "0.2"),
    # The outcome pages. Listed WITHOUT a trailing slash on purpose: `/use-cases/<slug>/` 307s to
    # this form, and a sitemap that lists a redirect is worse than no sitemap. Their canonical tags
    # match these exactly. `_USE_CASES` is the one source for the set, so a new page is listed the
    # moment it is routed, and `tests/test_seo.py` will fail if one stops answering 200.
    *(
        (f"/use-cases/{slug}", name, "0.8")
        for slug, name in _USE_CASES.items()
    ),
)


@lru_cache(maxsize=1)
def _catalog_mtime() -> str:
    """The newest mtime under the catalog directory, as a sitemap `lastmod` date. The catalog is
    read-only and changes only on deploy, so one scan per process is enough."""
    newest = 0.0
    for f in (Path(catalog_store.__file__).parent / "catalog").rglob("*.yaml"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:  # noqa: PERF203 -- a file vanishing mid-scan is not worth failing the sitemap
            continue
    return _iso_day(newest)


def _iso_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ""


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    """Generated, not bundled: 80 of its URLs are the catalog's platform shelves, which move with the
    catalog rather than with a checked-in file. Every URL is absolute on `public_url` so a self-host
    publishes its own pages, and so the copy served on a legacy host still names the canonical one."""
    base = get_settings().public_url.rstrip("/")
    cat_day = _catalog_mtime()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    def add(path: str, lastmod: str, priority: str) -> None:
        out.append("<url>")
        out.append(f"<loc>{_esc_html(base + path)}</loc>")
        if lastmod:
            out.append(f"<lastmod>{lastmod}</lastmod>")
        out.append(f"<priority>{priority}</priority>")
        out.append("</url>")

    for path, src, priority in _SITEMAP_PAGES:
        day = cat_day
        if src:
            f = _WEB_DIR / src
            day = _iso_day(f.stat().st_mtime) if f.exists() else ""
        add(path, day, priority)
    for row in _platform_rows():
        add(f"/catalog/{row['slug']}", cat_day, "0.6")
    # The agent pages exist only on the hosted deployment (see `_hosted`); their lastmod follows the
    # hand-written copy, which is what changes between deploys.
    if _hosted():
        copy_day = _iso_day(Path(agent_pages.__file__).stat().st_mtime)
        for slug in agent_pages.AGENTS:
            add(f"/agents/{slug}", copy_day, "0.8")
        add("/use-cases", copy_day, "0.8")
        for (c, j) in agent_pages.USE_CASE_PAGES:
            add(f"/use-cases/{c}/{j}", copy_day, "0.7")
    out.append("</urlset>")
    return Response("\n".join(out), media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=3600"})


@app.get("/install.sh", include_in_schema=False)
async def install_sh():
    """`curl -fsSL {BASE}/install.sh | sh` — installs the treg CLI and points it at this server.
    The serving domain is templated in so it targets whichever host is live (dev box or the real
    domain after deploy)."""
    f = _WEB_DIR / "install.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="install.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/x-shellscript; charset=utf-8")


@app.get("/selfhost.sh", include_in_schema=False)
async def selfhost_sh():
    """`curl -fsSL {BASE}/selfhost.sh | sh` — run your OWN registry locally, with no account.

    Different from install.sh, which only installs the CLI and points it at THIS server. This one
    brings up a server on the caller's machine in single-user mode, so they land on a dashboard that
    is already signed in. Value first, account later."""
    f = _WEB_DIR / "selfhost.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="selfhost.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/x-shellscript; charset=utf-8")


def _serve_md(name: str) -> PlainTextResponse:
    """Serve a bundled markdown file as inline text (so "open in new tab" shows it, not a download),
    with the serving domain templated in. Backs the 'copy markdown' buttons on the docs pages."""
    f = _WEB_DIR / name
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8")


@app.get("/quickstart.md", include_in_schema=False)
async def quickstart_md():
    """The quick-start as raw markdown — copy it or open it in a tab and use it anywhere."""
    return _serve_md("quickstart.md")


@app.get("/tutorial.md", include_in_schema=False)
async def tutorial_md():
    """The full tutorial as raw markdown (mirrors the interactive /tutorial)."""
    return _serve_md("tutorial.md")


@app.get("/tutorial-import-shell.md", include_in_schema=False)
async def tutorial_import_shell_md():
    """Focused tutorial: CLI auto-import (`treg upload clis`) + shell mode (`treg shell`) + the
    local-run security sandbox. Linked from the main tutorial."""
    return _serve_md("tutorial-import-shell.md")


@app.get("/tutorial-access.md", include_in_schema=False)
async def tutorial_access_md():
    """Focused tutorial: per-member team access control (which tools a member may use + the local-run
    toggle). Linked from the main tutorial."""
    return _serve_md("tutorial-access.md")


@app.get("/vendor-listing", include_in_schema=False)
@app.get("/vendor-listing.md", include_in_schema=False)
async def vendor_listing_md(request: Request):
    """Vendor listing instructions — what a vendor's coding agent reads before raising a PR that
    adds their API to the catalog. Linked from the dashboard's "List your API" modal."""
    resp = _serve_md("vendor-listing.md")
    # Two URLs, one document. `text/plain` cannot carry a <link rel=canonical>, so the duplicate is
    # suppressed with the header equivalent: /vendor-listing is the indexed one (it is what the
    # sitemap lists), /vendor-listing.md keeps serving agents and stays out of the index.
    if request.url.path.endswith(".md"):
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/integrate.md", include_in_schema=False)
async def integrate_md():
    """The BUILDER skill: how to put treg inside your own product and bill your own customers for it.

    Distinct from `skill.md`, which teaches an agent to USE treg. This one is pasted into a builder's
    repo and pointed at their coding agent, so it leads with the per-customer billing model — the
    part that changes how the plumbing is written, and therefore has to be read before any of it is.
    """
    return _serve_md("integrate.md")


@app.get("/skill.md", include_in_schema=False)
async def skill_md():
    """The OFFICIAL treg Claude skill (3 personas), {BASE}-templated to this server.
    install.sh drops it into ~/.claude/skills/treg/ so agents learn treg at CLI install."""
    return _serve_md("skill.md")


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """The ▚ brand mark. Served at both paths so browsers that auto-request /favicon.ico stop 404ing."""
    ico = _WEB_DIR / "favicon.svg"
    if not ico.exists():
        raise HTTPException(status_code=404, detail="favicon not bundled")
    return FileResponse(ico, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/tutorial.js", include_in_schema=False)
async def tutorial_js():
    """The shared interactive-tutorial data + highlighter (window.TREG_TUTORIAL / tregHL).
    Loaded by both the dashboard Help view and the standalone tutorial page, so they never drift."""
    js = _WEB_DIR / "tutorial.js"
    if not js.exists():
        raise HTTPException(status_code=404, detail="tutorial.js not bundled")
    # no-cache, like index.html and the landing: the page includes this as a bare `<script
    # src="/tutorial.js">` with no version query, so without the header a browser keeps serving the
    # tutorial from before the last deploy until someone hard-refreshes.
    return FileResponse(js, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/legal.css", include_in_schema=False)
async def legal_css():
    """The shared skin for /terms and /privacy (landing-page tokens, one copy)."""
    f = _WEB_DIR / "legal.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="legal.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache"})


def _legal_page(name: str) -> HTMLResponse:
    page = _WEB_DIR / name
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    # `{BASE}`-substituted rather than sent as a plain FileResponse, so each page's canonical and
    # og:url name the host actually serving it. A hardcoded treg.to would tell a self-hosted
    # registry's crawler that the real page lives on someone else's domain.
    base = get_settings().public_url.rstrip("/")
    html = page.read_text(encoding="utf-8").replace("{BASE}", base)
    # no-cache: a legal page must not be served stale after we publish an update.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/terms", include_in_schema=False)
async def terms_page():
    """Terms of Service for the HOSTED registry (self-hosted instances are governed by LICENSE)."""
    return _legal_page("terms.html")


@app.get("/privacy", include_in_schema=False)
async def privacy_page():
    """Privacy policy. Also the URL given to OAuth providers at app-registration/verification time
    (Google requires a reachable privacy policy carrying the Limited Use disclosure), so this path
    is effectively public API — don't rename it without updating the provider consoles."""
    return _legal_page("privacy.html")


@app.get("/adtrack.js", include_in_schema=False)
async def adtrack_js():
    """First-party ad-click capture (see the file itself): sets the `treg_ad` cookie that
    `_ad_attribution_from` reads at signup. No Google script, no third-party request."""
    headers = {"Cache-Control": "no-cache"}
    if not adsconv.enabled():
        # The page keeps one static script include, but an unconfigured/self-hosted deployment must
        # not collect an advertising cookie at all. A stale cookie is also ignored at signup below.
        return Response(content="", media_type="application/javascript", headers=headers)
    f = _WEB_DIR / "adtrack.js"
    if not f.exists():
        raise HTTPException(status_code=404, detail="adtrack.js not bundled")
    # no-cache, same reasoning as tutorial.js: served as a bare `<script src="/adtrack.js">` with no
    # version query, so without this header a browser would keep an ad-window-stale copy after a fix.
    return FileResponse(f, media_type="application/javascript", headers=headers)


@app.get("/resources", include_in_schema=False)
async def resources_page():
    """The hub for the outcome pages. It exists for two reasons beyond navigation: without it the
    `/use-cases/*` pages are orphans that no crawler reaches, and it gives the footer one durable
    link instead of five that grow every time a page is added."""
    page = _WEB_DIR / "resources.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="resources.html not bundled")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/usecase.css", include_in_schema=False)
async def usecase_css():
    """The shared skin for /use-cases/* (landing-page tokens, one copy — same deal as legal.css)."""
    f = _WEB_DIR / "usecase.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="usecase.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/use-cases/{slug}", include_in_schema=False)
async def use_case_page(slug: str):
    """One outcome page. Unlike the root landing this does NOT redirect a signed-in visitor to
    /app: these are ad destinations, and bouncing a returning user away from the page they paid
    to reach would make the campaign data unreadable."""
    name = _USE_CASES.get(slug.strip("/").lower())
    if not name:
        raise HTTPException(status_code=404, detail="unknown use case")
    page = _WEB_DIR / name
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    # no-cache: these are edited against live campaign data and must never serve stale.
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


class OAuthClientRegistration(BaseModel):
    """RFC 7591 registration request. Extra fields are ignored rather than refused — clients send
    plenty we do not use, and rejecting an unknown key would break them for no benefit."""

    client_name: str = ""
    redirect_uris: list[str] = []
    client_uri: str = ""
    logo_uri: str = ""
    scope: str = ""


@app.post("/oauth/register", include_in_schema=False)
async def oauth_register(body: OAuthClientRegistration,
                         db: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Dynamic client registration (RFC 7591) — how Claude Code and most MCP clients arrive.

    Open by design: the spec has clients register unauthenticated, and a registration grants nothing
    on its own. Every token still requires a human to sign in and approve at the consent screen, so
    the worst a spurious registration achieves is a row in a table.

    What is NOT open is the redirect URI. It is fixed here and matched exactly at authorize time,
    because that is where authorization codes get delivered.
    """
    from . import mcp_oauth

    uris = [u for u in body.redirect_uris if mcp_oauth.valid_redirect_uri(u)]
    if not uris:
        return JSONResponse(status_code=400, content={
            "error": "invalid_redirect_uri",
            "error_description": ("at least one https redirect_uri is required (http is accepted "
                                  "only for 127.0.0.1 / localhost, for CLI clients)")})
    client = OAuthClient(
        client_id=mcp_oauth.new_client_id(), kind="dcr",
        client_name=(body.client_name or "unnamed client")[:200],
        client_uri=body.client_uri[:500], logo_uri=body.logo_uri[:500],
        redirect_uris=uris[:20], scope=body.scope[:200])
    db.add(client)
    await db.commit()
    return JSONResponse(status_code=201, content={
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })


async def _resolve_oauth_client(client_id: str, db: AsyncSession) -> OAuthClient | None:
    """One client row, whichever door it came through.

    A registered client is a lookup. A client_id that is an https URL is a metadata document: fetched
    on first sight, cached as a row, and refreshed when stale — documents change, and a cache that
    never expires would pin a client to redirect URIs it has since retired.
    """
    from . import mcp_oauth

    row = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
           ).scalar_one_or_none()
    fresh_enough = row is not None and (
        row.kind != "cimd" or (row.refreshed_at is not None and
                               (datetime.now(timezone.utc).replace(tzinfo=None) - row.refreshed_at
                                ).total_seconds() < mcp_oauth._CIMD_REFRESH_S))
    if fresh_enough:
        return row
    if not client_id.startswith("https://"):
        return row  # a dcr client we do not know is simply unknown
    doc = await mcp_oauth.fetch_client_id_metadata(client_id)
    if doc is None:
        return row  # keep a stale copy over nothing: a transient fetch failure is not a revocation
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None:
        row = OAuthClient(client_id=client_id, kind="cimd")
        db.add(row)
    row.kind, row.refreshed_at = "cimd", now
    row.client_name, row.client_uri = doc["client_name"], doc["client_uri"]
    row.logo_uri, row.redirect_uris, row.scope = doc["logo_uri"], doc["redirect_uris"], doc["scope"]
    await db.commit()
    await db.refresh(row)
    return row


AUTH_CODE_TTL_S = 300   # a code is redeemed within seconds; five minutes is generous, not a window
REFRESH_TTL_S = 30 * 24 * 3600   # a connector the user still uses keeps working for a month


async def _ensure_grant(family_id: str, db: AsyncSession) -> OAuthGrant | None:
    """Return this family's authority row, reconstructing a rolling-deploy gap if necessary.

    A35 backfilled every family that existed when a new instance started, but a rolling deploy runs
    old and new binaries together. An old instance can therefore issue another OAuthRefresh AFTER
    the one-time backfill, without the OAuthGrant row it knows nothing about. Listing then showed a
    null team, team moves answered 404, and the first rotation made the grant look newly consented.

    The oldest refresh row is the only surviving consent-time authority for such a family. The raw
    upsert is intentional: both supported databases implement this spelling, and ON CONFLICT makes
    two new instances repairing the same old-binary write converge instead of racing into a unique
    constraint failure.
    """
    grant = await db.get(OAuthGrant, family_id)
    if grant is not None:
        return grant
    oldest = (await db.execute(select(OAuthRefresh).where(
        OAuthRefresh.family_id == family_id
    ).order_by(OAuthRefresh.created_at, OAuthRefresh.id).limit(1))).scalars().first()
    if oldest is None:
        return None
    await db.execute(text(
        "INSERT INTO oauthgrant (family_id, current_org_id, granted_at) "
        "VALUES (:family_id, :org_id, :granted_at) "
        "ON CONFLICT (family_id) DO NOTHING"
    ), {"family_id": family_id, "org_id": oldest.org_id, "granted_at": oldest.created_at})
    return await db.get(OAuthGrant, family_id)


async def _family_org(family_id: str, db: AsyncSession) -> int | None:
    """Which team future tokens in this grant family spend from.

    Mutable family authority has its own row. Token rows retain the team each token was issued under
    so a later replay audit keeps its original attribution. The previous oldest-token authority made
    moves stick across refresh races, but only by rewriting retired history.

    The residual window is the one no design without distributed locking removes: an access token
    already minted for the old team keeps working until it expires (≤ ACCESS_TTL_SECONDS). The
    FAMILY, though, converges on the move — the next rotation reads this row and mints for the new
    team — so the move is never undone, only briefly overlapped.
    """
    grant = await _ensure_grant(family_id, db)
    return grant.current_org_id if grant is not None else None


def _refresh_is_live(row: OAuthRefresh, *, now: datetime | None = None) -> bool:
    """One definition of a usable grant token, shared by refresh, listing and team moves."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return row.retired_at is None and row.expires_at >= now


async def _issue_refresh(*, family_id: str, client_id: str, user_id: int, org_id: int,
                         resource: str, scope: str, db: AsyncSession) -> str:
    """Mint a refresh token and store only its hash — a database copy is a database leak."""
    import secrets as _s

    if await db.get(OAuthGrant, family_id) is None:
        db.add(OAuthGrant(family_id=family_id, current_org_id=org_id))
    token = _s.token_urlsafe(40)
    db.add(OAuthRefresh(
        token_hash=crypto.hash_token(token), family_id=family_id, client_id=client_id,
        user_id=user_id, org_id=org_id, resource=resource, scope=scope,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=REFRESH_TTL_S)))
    return token


async def _revoke_refresh_family(family_id: str, reason: str, db: AsyncSession) -> int:
    """Kill every refresh token descended from one grant.

    Called when a retired token is presented again. We cannot tell a client retrying after a dropped
    response from a thief replaying a stolen copy — so we assume the worse one, because the cost of
    being wrong is a re-login rather than someone else's balance.
    """
    rows = (await db.execute(select(OAuthRefresh).where(
        OAuthRefresh.family_id == family_id, OAuthRefresh.retired_at.is_(None)))).scalars().all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for r in rows:
        r.retired_at, r.retired_reason = now, reason
        db.add(r)
    return len(rows)

_CONSENT_CSS = """
.consent{max-width:460px;text-align:left}
.consent h1{font-size:20px;margin:0 0 4px}
.consent .who{color:var(--ink55,#8a8a8a);font-size:13.5px;margin:0 0 18px}
.consent .grants{list-style:none;padding:0;margin:0 0 18px}
.consent .grants li{padding:7px 0 7px 22px;position:relative;font-size:13.5px;line-height:1.45}
.consent .grants li:before{content:"›";position:absolute;left:6px;color:#e0703f}
.consent label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--ink55,#8a8a8a);margin:0 0 6px}
.consent select{width:100%;padding:9px 10px;border-radius:8px;font:inherit;font-size:14px;
  background:#1a1a1a;color:inherit;border:1px solid #333;margin-bottom:16px}
.consent .row{display:flex;gap:10px}
.consent button{flex:1;padding:10px 14px;border-radius:8px;font:inherit;font-size:14px;cursor:pointer;
  border:1px solid #333;background:#1a1a1a;color:inherit}
.consent button.primary{background:#e0703f;border-color:#e0703f;color:#161310;font-weight:600}
.consent .fine{color:var(--ink55,#8a8a8a);font-size:12px;margin:14px 0 0;line-height:1.5}
"""


def _consent_page(*, client_name: str, client_uri: str, user_email: str, teams: list,
                  hidden: dict, unverified: bool) -> HTMLResponse:
    """The one place a human sees what they are granting — so it says it in words, not scopes.

    Deliberately plain about the two things that cost money or leak data: this client will be able to
    spend the team's balance, and to use the keys the team registered. A consent screen that lists
    `treg:call` and calls it informed is a formality, not a decision.

    The team picker is here rather than anywhere else because this is the only moment a human is
    present to answer it. `balance` used to have to refuse and ask when someone belonged to several
    teams; that question belongs at the grant, once.
    """
    import html as _h

    def _label(t: dict) -> str:
        bal = t.get("balance_usd")
        if bal is None:
            money = ""
        elif bal <= 0:
            # Named, not hidden: an empty team is still a legitimate choice when the work uses the
            # team's OWN keys, which are never metered. Saying "no balance" is the useful warning;
            # removing the option would be wrong.
            money = "  ·  no balance — catalog calls will be refused"
        else:
            money = f"  ·  ${bal:.2f}"
        return f'{t["slug"]} — {t["role"]}{money}'

    opts = "".join(
        f'<option value="{t["org_id"]}">{_h.escape(_label(t))}</option>' for t in teams)
    fields = "".join(
        f'<input type="hidden" name="{_h.escape(k)}" value="{_h.escape(str(v))}"/>'
        for k, v in hidden.items())
    who = _h.escape(client_name or "An application")
    where = (f' <span class="who">({_h.escape(client_uri)})</span>' if client_uri else "")
    warn = ("" if not unverified else
            '<p class="fine"><b>This application registered itself.</b> treg has not reviewed it — '
            'only continue if you recognise it and started this yourself.</p>')
    return HTMLResponse(
        f"{_AUTH_HEAD.replace('</style>', _CONSENT_CSS + '</style>')}"
        f'<body><div class="wrap"><div class="card consent">'
        f'<div class="logo">▚ tools-registry</div>'
        f"<h1>{who} wants to use treg</h1>{where}"
        f'<p class="who">Signed in as {_h.escape(user_email)}</p>'
        f'<ul class="grants">'
        f"<li>Search the catalog and read prices</li>"
        f"<li>Call tools on your team's behalf — <b>this spends the team's balance</b></li>"
        f"<li>Use the API keys and connections your team has registered, without seeing them</li>"
        f"<li>Read the team's balance</li>"
        f"</ul>"
        f'<form method="post" action="/oauth/authorize">{fields}'
        f'<label for="org_id">Which team?</label>'
        f'<select id="org_id" name="org_id" required>{opts}</select>'
        f'<div class="row">'
        f'<button type="submit" name="decision" value="deny">Cancel</button>'
        f'<button type="submit" name="decision" value="allow" class="primary">Allow</button>'
        f"</div></form>{warn}"
        f'<p class="fine">You can revoke this at any time from your treg dashboard.</p>'
        f"</div></div></body></html>")


OAUTH_RETURN_COOKIE = "treg_oauth_return"


def _remember_oauth_return(resp, request: Request) -> None:
    """Park where to come back to after the user signs in.

    A RELATIVE path, deliberately — never a full URL. A stored absolute URL would have to be
    validated against our own origin before being redirected to, and getting that check subtly wrong
    is how open redirects happen. A path cannot leave the site.

    Short-lived: this is a detour of seconds, and a stale one would silently hijack the next sign-in.
    """
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    resp.set_cookie(OAUTH_RETURN_COOKIE, target, httponly=True, samesite="lax",
                    secure=_is_https(request), max_age=600)


def _take_oauth_return(request: Request) -> str | None:
    """The parked destination, if it is one we actually park — else None.

    Only `/oauth/authorize` is honoured. Accepting any path would turn this cookie into a general
    "redirect me anywhere after login" primitive, which is a phishing aid rather than a feature.
    """
    # Starlette quotes a cookie value containing separators, and not every client strips the quotes
    # back off. Tolerating them here costs nothing; assuming they are absent cost a failing test and
    # would have cost a silently-dropped authorization in production.
    target = (request.cookies.get(OAUTH_RETURN_COOKIE) or "").strip('"')
    return target if target.startswith("/oauth/authorize?") else None


REFERRAL_COOKIE = "treg_ref"
# A month. The gap between clicking a friend's link and actually creating a team is measured in days
# for the people this program is for — they read the landing, think about it, and come back. Shorter
# would silently drop the referrals that took the longest to convert, which are exactly the genuine
# ones; much longer would keep attributing a signup to a link somebody forgot they ever clicked.
REFERRAL_COOKIE_MAX_AGE = 30 * 24 * 3600


def _remember_referral(resp, request: Request, code: str) -> None:
    """Park a referral code from `/?ref=…` until the visitor creates their first team.

    Same shape as `_remember_oauth_return`: httponly (no script needs it), `samesite=lax` so it
    survives the click through to a GitHub/Google sign-in and back, and `secure` only when we are
    actually on HTTPS so local development still works.

    First code wins is NOT enforced here — the cookie is simply overwritten. Someone who clicks two
    different referral links before signing up gets attributed to the second, which is both the
    normal advertising convention (last touch) and the one that needs no extra state.
    """
    resp.set_cookie(REFERRAL_COOKIE, code, httponly=True, samesite="lax",
                    secure=_is_https(request), max_age=REFERRAL_COOKIE_MAX_AGE)


def _take_referral(request: Request) -> str:
    """The parked code, revalidated. "" when there is none or it is not a shape we ever mint.

    Validated on READ as well as on write, exactly like `_take_oauth_return`: a cookie is
    attacker-supplied, and this value reaches a database query. `normalize_code` is a strict
    allowlist, so anything odd becomes "" and the signup simply proceeds unreferred.
    """
    return referrals.normalize_code((request.cookies.get(REFERRAL_COOKIE) or "").strip('"'))


def _wrong_resource(resource: str) -> str | None:
    """Is this `resource` one we actually protect? Returns an error message, or None if fine.

    Refusing early matters more than it looks. `resource` becomes the token's audience, and the MCP
    server accepts only its own — so accepting a resource we do not serve mints a token that is
    valid, well-formed, and silently useless. That failure surfaces later, at the first tool call,
    as "not signed in", which points the reader at authentication when the real problem was the
    audience. Found exactly that way: an independent MCP client sent the URL it was connecting to
    rather than the canonical identifier from our metadata, and got a token that could never work.

    Empty is allowed: a client that omits `resource` gets our canonical one, which is what it would
    have discovered anyway.
    """
    from . import mcp_oauth

    if not resource:
        return None
    canonical = mcp_oauth.mcp_resource_url()
    # The legacy hosts' resource URLs stay valid: a pre-move client discovered its `resource` from
    # the old domain's metadata and will keep sending it for the lifetime of the grant.
    if any(resource.rstrip("/") == aud.rstrip("/") for aud in mcp_oauth.mcp_resource_audiences()):
        return None
    return (f"this server issues tokens for {canonical} only — use the `resource` value from "
            f"/.well-known/oauth-protected-resource")


def _same_mcp_resource(a: str, b: str) -> bool:
    """Whether two `resource` values name this same MCP server. Exact match, slash-variant match,
    or BOTH normalize into the canonical+legacy audience set — the domain move renamed the
    resource without changing it, so a grant consented on one name must stay exchangeable and
    refreshable by a client re-based onto the other (in either direction)."""
    from . import mcp_oauth

    na, nb = mcp_oauth.normalize_resource(a), mcp_oauth.normalize_resource(b)
    if a == b or na == nb:
        return True
    auds = mcp_oauth.mcp_resource_audiences()
    return na in auds and nb in auds


def _oauth_error(redirect_uri: str, state: str, error: str, desc: str = ""):
    """OAuth errors go BACK TO THE CLIENT via the redirect, once we trust the redirect.

    Before the client and redirect_uri are validated we must NOT redirect — bouncing an error to an
    unvalidated URI is an open redirect, and it would leak `state` to whoever asked for it. Those
    cases raise a plain 400 instead, which is why this helper is only ever called after validation.
    """
    from urllib.parse import urlencode

    q = {"error": error}
    if desc:
        q["error_description"] = desc
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


async def _authorize_request(client_id: str, redirect_uri: str, response_type: str,
                             code_challenge: str, code_challenge_method: str,
                             db: AsyncSession):
    """Validate an authorization request. Returns (client, error_response) — exactly one is None.

    Order matters and is deliberate: identify the client and its redirect FIRST, because until both
    are known-good there is nowhere safe to send an error. Everything after that can be reported to
    the client properly.
    """
    from . import mcp_oauth

    client = await _resolve_oauth_client(client_id, db) if client_id else None
    if client is None:
        return None, JSONResponse(status_code=400, content={
            "error": "invalid_client",
            "error_description": "unknown client_id — register first, or serve a client-id metadata document"})
    if not mcp_oauth.redirect_uri_allowed(client, redirect_uri):
        # NOT a redirect: we do not bounce errors to a URI the client has not proven is theirs.
        return None, JSONResponse(status_code=400, content={
            "error": "invalid_request",
            "error_description": "redirect_uri does not exactly match one registered for this client"})
    return client, None


@app.get("/oauth/authorize", include_in_schema=False)
async def oauth_authorize(
    request: Request,
    client_id: str = Query(default=""), redirect_uri: str = Query(default=""),
    response_type: str = Query(default="code"), scope: str = Query(default=""),
    state: str = Query(default=""), code_challenge: str = Query(default=""),
    code_challenge_method: str = Query(default=""), resource: str = Query(default=""),
    treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    """What is this client asking for, and on behalf of which team?

    Step 3 answers that as JSON; step 4 puts a consent page on top of the same checks. It issues
    NOTHING — approval is a POST, because a GET that granted access could be triggered by any page
    that can make the browser navigate.
    """
    client, err = await _authorize_request(client_id, redirect_uri, response_type,
                                           code_challenge, code_challenge_method, db)
    if err is not None:
        return err
    if response_type != "code":
        return _oauth_error(redirect_uri, state, "unsupported_response_type",
                            "only the authorization code flow is supported")
    if not code_challenge or code_challenge_method != "S256":
        return _oauth_error(redirect_uri, state, "invalid_request",
                            "PKCE with code_challenge_method=S256 is required")
    if (bad_target := _wrong_resource(resource)) is not None:
        return _oauth_error(redirect_uri, state, "invalid_target", bad_target)

    user = await _user_from_session(treg_session, db)
    if user is None:
        # Sign in first, then come back to THIS request. Parked in a cookie rather than a `?next=`
        # query the sign-in page would have to understand — the first version invented that
        # convention and nothing implemented it, so the user signed in and landed on the dashboard
        # with the authorization silently dropped.
        resp = RedirectResponse("/", status_code=302)
        _remember_oauth_return(resp, request)
        return resp

    memberships = (await db.execute(
        select(Membership).where(Membership.user_id == user.id))).scalars().all()
    teams = []
    for m in memberships:
        org = await db.get(Org, m.org_id)
        if org is not None and not org.suspended:
            # The BALANCE belongs on this list. Choosing a team here decides which balance the
            # client spends for the life of the grant, and a list of slugs makes the one question
            # that matters — "which of these can actually pay?" — invisible at the moment of
            # choosing. Unclecode picked a $0.00 team on the first real ChatGPT connect and the call
            # was refused; nothing on the page could have told him.
            teams.append({"org_id": org.id, "slug": org.slug, "role": m.role,
                          "balance_usd": round((org.balance_micro or 0) / 1_000_000, 4)})
    if not teams:
        return _oauth_error(redirect_uri, state, "access_denied",
                            "this account is not a member of any team")

    hidden = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": response_type,
              "scope": scope, "state": state, "code_challenge": code_challenge,
              "code_challenge_method": code_challenge_method, "resource": resource}
    if "application/json" in (request.headers.get("accept") or ""):
        return {
            "client": {"client_id": client.client_id, "name": client.client_name,
                       "uri": client.client_uri, "kind": client.kind},
            "redirect_uri": redirect_uri, "scope": scope, "resource": resource,
            "user": user.email,
            # The team picker. A person may belong to several, and which one this client may spend
            # from is a decision for the human here — not something resolved per call later.
            "teams": teams,
            "approve_with": "POST /oauth/authorize with the same parameters plus org_id",
        }
    return _consent_page(client_name=client.client_name, client_uri=client.client_uri,
                         user_email=user.email, teams=teams, hidden=hidden,
                         unverified=(client.kind == "dcr"))


@app.post("/oauth/authorize", include_in_schema=False)
async def oauth_authorize_approve(
    request: Request,
    decision: str = Form(default="allow"),
    client_id: str = Form(default=""), redirect_uri: str = Form(default=""),
    response_type: str = Form(default="code"), scope: str = Form(default=""),
    state: str = Form(default=""), code_challenge: str = Form(default=""),
    code_challenge_method: str = Form(default=""), resource: str = Form(default=""),
    org_id: int = Form(default=0),
    treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session),
):
    """The human decided. On approval, mint a one-time code bound to everything that made this
    request; on anything else, tell the client no."""
    import secrets as _s

    from . import mcp_oauth

    # The consent form is the security boundary, so the submission must have come from OUR page.
    # Without this, a page anywhere could auto-submit a form and grant itself a team's balance —
    # the user is signed in, so their cookie would ride along. Same guard `auth_logout` uses.
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail=(
            "cross-origin authorization rejected — this form must be submitted from treg's own "
            f"consent page (saw Origin: {request.headers.get('origin') or 'none'}, "
            f"Sec-Fetch-Site: {request.headers.get('sec-fetch-site') or 'none'})"))

    client, err = await _authorize_request(client_id, redirect_uri, response_type,
                                           code_challenge, code_challenge_method, db)
    if err is not None:
        return err
    if decision != "allow":
        # Cancel is a real answer and the client is entitled to hear it, rather than hang.
        return _oauth_error(redirect_uri, state, "access_denied", "the user declined")
    if (bad_target := _wrong_resource(resource)) is not None:
        return _oauth_error(redirect_uri, state, "invalid_target", bad_target)
    if not code_challenge or code_challenge_method != "S256":
        return _oauth_error(redirect_uri, state, "invalid_request",
                            "PKCE with code_challenge_method=S256 is required")

    user = await _user_from_session(treg_session, db)
    if user is None:
        return JSONResponse(status_code=401, content={"error": "access_denied",
                                                      "error_description": "not signed in"})
    # The chosen team must be one this user actually belongs to — the field is client-supplied.
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
    if membership is None:
        return _oauth_error(redirect_uri, state, "access_denied",
                            "choose a team you are a member of")

    code = OAuthCode(
        code=_s.token_urlsafe(32), client_id=client.client_id, user_id=user.id, org_id=org_id,
        redirect_uri=redirect_uri, code_challenge=code_challenge,
        resource=mcp_oauth.normalize_resource(resource) if resource else mcp_oauth.mcp_resource_url(),
        scope=scope,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=AUTH_CODE_TTL_S))
    db.add(code)
    await db.commit()

    from urllib.parse import urlencode

    q = {"code": code.code}
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


async def _refresh_grant(*, refresh_token: str, client_id: str, resource: str,
                         db: AsyncSession, bad):
    """Exchange a refresh token for a new access token, ROTATING the refresh token as we go.

    The retired row is kept, not deleted. That is what makes a replay recognisable: a deleted token
    looks merely unknown, while a retired one tells us somebody used a credential that had already
    been spent — and at that point the safe reading is that it was copied.
    """
    from . import mcp_oauth

    if not refresh_token:
        return bad("invalid_request", "refresh_token is required")
    row = (await db.execute(select(OAuthRefresh).where(
        OAuthRefresh.token_hash == crypto.hash_token(refresh_token)))).scalar_one_or_none()
    if row is None:
        return bad("invalid_grant", "unknown refresh token")

    if row.retired_at is not None:
        # Already spent. Either a client retried after a dropped response, or someone else has a
        # copy — indistinguishable from here, so assume the worse one and end the whole family. The
        # cost of being wrong is one sign-in; the cost of the other mistake is somebody's balance.
        killed = await _revoke_refresh_family(row.family_id, "reuse detected", db)
        await db.commit()
        audit.record_call(org_id=row.org_id, user_email="", tool_name="oauth.refresh_reuse",
                          method="POST", path="/oauth/token", status_code=400, client="",
                          telemetry={"family": row.family_id, "revoked": killed})
        return bad("invalid_grant",
                   "this refresh token was already used — the grant has been revoked, sign in again")

    if not _refresh_is_live(row):
        return bad("invalid_grant", "refresh token expired")
    if client_id and client_id != row.client_id:
        return bad("invalid_grant", "refresh token was issued to a different client")
    if resource and not _same_mcp_resource(resource, row.resource):
        return bad("invalid_target", "resource does not match the one that was consented to")

    user = await db.get(User, row.user_id)
    if user is None or user.suspended:
        return bad("invalid_grant", "the account behind this grant is no longer active")
    live_org_id = await _family_org(row.family_id, db) or row.org_id
    org = await db.get(Org, live_org_id)
    if org is None or org.suspended:
        return bad("invalid_grant", "the team on this grant is no longer available")
    # STILL a member? The grant is the user's consent to spend a TEAM's balance, and leaving (or
    # being removed from) that team ends the standing they consented with. Without this a grant
    # kept minting tokens forever: every downstream call was refused by `require_member`, so the
    # damage was bounded, but the grant lay dormant and sprang back to life — with no new consent —
    # the day the membership was restored.
    still_in = (await db.execute(select(Membership).where(
        Membership.user_id == row.user_id, Membership.org_id == live_org_id))).scalar_one_or_none()
    if still_in is None:
        await _revoke_refresh_family(row.family_id, "membership ended", db)
        await db.commit()
        return bad("invalid_grant",
                   "the account behind this grant is no longer a member of its team — sign in again")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row.retired_at, row.retired_reason = now, "rotated"
    db.add(row)
    replacement = await _issue_refresh(family_id=row.family_id, client_id=row.client_id,
                                       user_id=row.user_id, org_id=live_org_id,
                                       resource=row.resource, scope=row.scope, db=db)
    access = mcp_oauth.make_access_token(
        user_id=row.user_id, org_id=live_org_id, scope=row.scope,
        audience=mcp_oauth.normalize_resource(row.resource),  # heal pre-normalization spellings
        token_version=user.token_version)
    await db.commit()
    return JSONResponse({"access_token": access, "token_type": "Bearer",
                         "expires_in": mcp_oauth.ACCESS_TTL_SECONDS, "scope": row.scope,
                         "refresh_token": replacement})


@app.post("/oauth/revoke", include_in_schema=False)
async def oauth_revoke(token: str = Form(default=""),
                       db: AsyncSession = Depends(get_session)) -> JSONResponse:
    """RFC 7009. Revoking a refresh token ends its whole family — a user who disconnects an app
    means all of it, not the one string they happened to send.

    Always answers 200, as the RFC requires: an unknown token is already revoked as far as the caller
    is concerned, and saying otherwise would turn this into an oracle for guessing valid tokens.
    """
    if token:
        row = (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.token_hash == crypto.hash_token(token)))).scalar_one_or_none()
        if row is not None:
            await _revoke_refresh_family(row.family_id, "revoked by client", db)
            await db.commit()
    return JSONResponse({"ok": True})


@app.post("/oauth/token", include_in_schema=False)
async def oauth_token(
    grant_type: str = Form(default=""), code: str = Form(default=""),
    redirect_uri: str = Form(default=""), client_id: str = Form(default=""),
    code_verifier: str = Form(default=""), resource: str = Form(default=""),
    refresh_token: str = Form(default=""),
    db: AsyncSession = Depends(get_session),
):
    """Exchange a code for an access token.

    Errors here are JSON, not redirects: this is a back-channel call from the client itself, and
    there is no browser to send anywhere.
    """
    from . import mcp_oauth

    def bad(err: str, desc: str, status: int = 400):
        return JSONResponse(status_code=status,
                            content={"error": err, "error_description": desc})

    if grant_type == "refresh_token":
        return await _refresh_grant(refresh_token=refresh_token, client_id=client_id,
                                    resource=resource, db=db, bad=bad)
    if grant_type != "authorization_code":
        return bad("unsupported_grant_type",
                   "supported grants: authorization_code, refresh_token")

    row = (await db.execute(select(OAuthCode).where(OAuthCode.code == code))
           ).scalar_one_or_none() if code else None
    if row is None:
        return bad("invalid_grant", "unknown or already-redeemed code")

    # DELETE FIRST. A code is single-use, and holding it while validating leaves a window where two
    # redemptions both read it. Everything below is validated against values already in hand.
    await db.delete(row)
    await db.commit()

    if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        return bad("invalid_grant", "code expired")
    if client_id and client_id != row.client_id:
        return bad("invalid_grant", "code was issued to a different client")
    if redirect_uri != row.redirect_uri:
        return bad("invalid_grant", "redirect_uri does not match the one the code was issued for")
    if not mcp_oauth.verify_pkce(code_verifier, row.code_challenge):
        return bad("invalid_grant", "code_verifier does not match the code_challenge")
    if resource and not _same_mcp_resource(resource, row.resource):
        return bad("invalid_target", "resource does not match the one that was consented to")

    user = await db.get(User, row.user_id)
    if user is None or user.suspended:
        return bad("invalid_grant", "the account behind this grant is no longer active")

    token = mcp_oauth.make_access_token(
        user_id=row.user_id, org_id=row.org_id, scope=row.scope,
        audience=mcp_oauth.normalize_resource(row.resource),  # heal pre-normalization spellings
        token_version=user.token_version)
    import secrets as _s

    refresh = await _issue_refresh(family_id=_s.token_urlsafe(16), client_id=row.client_id,
                                   user_id=row.user_id, org_id=row.org_id, resource=row.resource,
                                   scope=row.scope, db=db)
    await db.commit()
    return JSONResponse({"access_token": token, "token_type": "Bearer",
                         "expires_in": mcp_oauth.ACCESS_TTL_SECONDS, "scope": row.scope,
                         "refresh_token": refresh})


@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def oauth_protected_resource():
    """Tells an MCP client which authorization server guards /mcp/ and what it may ask for.

    Two paths for one document: the spec has clients look it up either at the host root or under the
    resource's own path, and which one a given client tries is not something we get to choose.
    """
    from . import mcp_oauth

    return JSONResponse(mcp_oauth.protected_resource_metadata(),
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def oauth_authorization_server():
    """How to get a token: the authorize and token endpoints, S256, and that we accept both dynamic
    registration and a client-id metadata document."""
    from . import mcp_oauth

    return JSONResponse(mcp_oauth.authorization_server_metadata(),
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/.well-known/openai-apps-challenge", include_in_schema=False)
async def openai_apps_challenge():
    """Domain-verification token for the OpenAI plugin directory.

    The portal issues a token and fetches it here to confirm we control the host serving the MCP
    endpoint. It must return THAT token as plain text and nothing else — the documentation is
    explicit that JSON, a list, or several tokens all fail. 404 when unset, which is correct for
    every deployment that is not ours: an empty file would read as a verification that never
    completes.
    """
    token = (get_settings().openai_apps_challenge or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="not configured")
    return PlainTextResponse(token, headers={"Cache-Control": "no-store"})


def _skill_frontmatter() -> dict[str, str]:
    """The bundled skill's frontmatter, read at request time rather than duplicated in code — the
    description is what drives discovery in every registry, and a second copy of it would drift."""
    f = _WEB_DIR / "skill.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail="skill.md not bundled")
    text = f.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise HTTPException(status_code=404, detail="skill.md has no frontmatter")
    out: dict[str, str] = {}
    for line in text.split("---", 2)[1].strip().splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


@app.get("/.well-known/skills/index.json", include_in_schema=False)
async def well_known_skills_index():
    """Advertise treg's own skill under the agentskills.io well-known convention.

    This makes THIS host a first-class skill source: an agent that supports the standard can install
    treg from treg.to directly, with no directory, no review queue and no third party in the middle
    — the same skill the plugins ship and `install.sh` drops, reached by whoever asks the domain.
    """
    fm = _skill_frontmatter()
    return JSONResponse({"skills": [{
        "name": fm.get("name", "treg"),
        "description": fm.get("description", ""),
        "files": ["SKILL.md"],
    }]})


@app.get("/.well-known/skills/treg/SKILL.md", include_in_schema=False)
async def well_known_skill_md():
    """The skill itself, at the path `index.json` promises. Deliberately the same `_serve_md` the
    canonical `/skill.md` uses, so `{BASE}` is templated to the serving host here too — a self-hosted
    registry advertises ITSELF, not treg.to."""
    return _serve_md("skill.md")


@app.get("/connect-demo", include_in_schema=False)
async def connect_demo_page():
    """A page that PRETENDS to be someone else's app, so the OAuth flow can be seen end to end.

    It uses only public endpoints — register, authorize, token, revoke, and /mcp/ — with nothing
    privileged about being served from treg's own domain. The point is to watch the whole dance in a
    browser before trusting it inside ChatGPT, where a failure surfaces as a shrug rather than an
    error message.
    """
    return _legal_page("connect-demo.html")


@app.get("/connect-demo/callback", include_in_schema=False)
async def connect_demo_callback():
    """Where treg sends the browser back. Hands the code to the opener and closes."""
    return _legal_page("connect-demo-callback.html")


@app.get("/support", include_in_schema=False)
@app.get("/contact", include_in_schema=False)
@app.get("/help", include_in_schema=False)
async def support_page():
    """How to get help. Three paths for one page because people guess differently, and because a
    plugin-directory listing must give a Support URL that resolves — a 404 there reads as an
    abandoned product. Like `/privacy`, this path is effectively public API once it is filed with a
    directory or an OAuth console: don't rename it without updating them."""
    return _legal_page("support.html")


@app.get("/tutorial", include_in_schema=False)
async def tutorial_page():
    """Standalone shareable interactive tutorial (same STEPS[] as the dashboard Help view)."""
    page = _WEB_DIR / "tutorial.html"
    if not page.exists():
        return HTMLResponse("<h3>Tutorial not bundled.</h3>")
    # Stamp the tutorial.js URL with the bundle version. `no-cache` alone is not enough: a browser
    # that cached the file BEFORE that header existed applies a heuristic lifetime and never
    # revalidates, so an edited tutorial silently keeps serving the old steps (cost an hour to find).
    js = _WEB_DIR / "tutorial.js"
    stamp = int(js.stat().st_mtime) if js.exists() else 0   # tutorial.js's OWN mtime: _app_version()
    html = page.read_text(encoding="utf-8").replace(       # hashes index.html and would not move
        'src="/tutorial.js"', f'src="/tutorial.js?v={stamp}"')
    html = html.replace("{BASE}", get_settings().public_url.rstrip("/"))  # canonical + og:url
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# Provider logos, resolved by convention: /logos/<service>.svg, matching `service` in
# oauth_providers.py. Keyed off the name the registry already has, so adding a provider needs no
# second registration step — drop the file in and it appears. Public and unauthenticated: they are
# brand marks, not data, and the dashboard renders them before the caller is known.
_LOGO_DIR = _WEB_DIR / "logos"
if _LOGO_DIR.exists():
    app.mount("/logos", StaticFiles(directory=str(_LOGO_DIR)), name="logos")


# Demo recordings — the plugin-directory submission requires a publicly reachable video URL, and
# hosting it ourselves means no third-party account decides whether reviewers can watch it.
_MEDIA_DIR = _WEB_DIR / "media"
if _MEDIA_DIR.exists():
    app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")


# The interactive dashboard tour (matted screenshots) — served + its WebP images, at /dashboard-tour/.
_TOUR_DIR = _WEB_DIR / "tour"
if _TOUR_DIR.exists():
    app.mount("/dashboard-tour", StaticFiles(directory=str(_TOUR_DIR), html=True), name="dashboard-tour")


# Third-party front-end libraries, vendored rather than pulled from a CDN at page load. The
# dashboard is a single hand-written Vue file with no bundler, so Vue arrives as a plain <script>
# — and while that script came from unpkg.com, any network that cannot reach unpkg rendered the
# signed-in dashboard as a blank page (issue #137: a mainland-China visitor, ERR_CONNECTION_CLOSED,
# then `Vue is not defined`). Serving it ourselves means the dashboard depends on exactly one
# origin: whoever served the page can serve its runtime. It also closes the supply-chain hole in
# the old floating `vue@3` tag, which let whatever npm published next run in an authed session.
# Filenames carry their version, so a bump is a visible one-line change and caches never collide.
class _ImmutableStatic(StaticFiles):
    """StaticFiles that says out loud what its filenames already promise.

    Every file here is version-stamped, so a given URL's bytes never change — a new version is a
    new URL. Without the header a browser still caches, but heuristically, on its own guess; being
    explicit means a returning visitor re-fetches nothing and a bump is picked up instantly.
    """
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


_VENDOR_DIR = _WEB_DIR / "vendor"
if _VENDOR_DIR.exists():
    app.mount("/vendor", _ImmutableStatic(directory=str(_VENDOR_DIR)), name="vendor")


# ---- caller auth (token = a Membership; open registration) --------------------------------
@dataclass
class Caller:
    """The resolved caller: their membership (org + role + token), identity, and org row.
    A token identifies a (user, org) pair, so `org_id`/`email`/`role` all come from here.
    """

    membership: Membership
    user: User
    org: Org

    @property
    def org_id(self) -> int:
        return self.membership.org_id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def role(self) -> str:
        return self.membership.role


async def _membership_by_token(token: str, db: AsyncSession) -> Membership | None:
    if not token:
        return None
    return (
        await db.execute(select(Membership).where(Membership.token_hash == crypto.hash_token(token)))
    ).scalar_one_or_none()


async def _user_from_session(cookie: str, db: AsyncSession) -> User | None:
    claims = sess.read_claims(cookie)
    if claims is None:
        return None
    user = await db.get(User, claims["uid"])
    if user is None or user.suspended or claims["tv"] != user.token_version:  # revoked = tv mismatch
        return None
    return user


async def _resolve_org(ref: str, db: AsyncSession) -> Org | None:
    """Resolve an X-Treg-Org header (a slug, or a numeric id) to an Org. Slug wins first: an
    all-digit slug is producible (`_slugify("2024") == "2024"`), so an id-first lookup would
    reinterpret a member's own slug as a primary key and lock them out of their org."""
    if not ref:
        return None
    by_slug = (await db.execute(select(Org).where(Org.slug == ref))).scalar_one_or_none()
    if by_slug is not None:
        return by_slug
    # int() of a huge all-digit ref would overflow SQLite's 64-bit INTEGER → 500 inside the auth
    # dependency; bound it so an out-of-range X-Treg-Org just falls through to the 400.
    return await db.get(Org, int(ref)) if (ref.isdigit() and int(ref) < 2**63) else None


async def require_identity(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> User:
    """Just *who* the caller is (no org): a token's user, or a session user. 401 otherwise."""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        if m is not None:
            # A published public-demo token must never act as a USER — user-level endpoints mint
            # identity tokens (/auth/cli-token), create real orgs, and accept invites, all of which
            # would let a stranger escape the demo org. Admin+ (the real operator) is exempt.
            org = await db.get(Org, m.org_id)
            if org is not None and org.public_demo and not _role_at_least(m.role, "admin"):
                raise HTTPException(status_code=403, detail=(
                    "this is a public demo token — it can only call the demo team's tools"))
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        # A machine token must never act as a USER. `create_org` depends on THIS dependency, so an
        # agent could otherwise create a fresh org in which it is the OWNER — and owners are exempt
        # from `_require_tool_access` and `_require_local_run`, escaping every limit set on it.
        if user is not None and _is_machine_email(user.email):
            raise HTTPException(status_code=403, detail=(
                "this token belongs to a machine identity — it can call this team's tools, "
                "but cannot act as a user"))
        if user is not None and not user.suspended:
            return user
        raise HTTPException(status_code=401, detail="invalid token")
    user = await _user_from_session(treg_session, db)
    if user is not None:
        return user
    raise HTTPException(status_code=401, detail="not authenticated")


@app.get("/auth/cli-token")
async def auth_cli_token(
    user: User = Depends(require_identity),
    x_treg_org: str = Header(default=""),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint a fresh CLI/bearer token for the authenticated caller (session cookie OR token). Identity
    tokens are stateless (`sess.make`), so handing one out rotates/invalidates nothing — it just lets
    the dashboard embed a working token in copy-paste snippets + a 'copy token' button, so a human
    doesn't have to hunt for it in `~/.treg/config.json`.

    When the caller names a team (the dashboard sends `X-Treg-Org` for the active org, and only after
    confirming membership), the org slug is BAKED into the token. That is what makes the dashboard's
    "your API key" work as a bare bearer where no `X-Treg-Org` header can travel — pasted into an MCP
    server's Authorization it resolves to that team, no header, no per-org agent token to manage. A
    caller in one team who sends no header still gets a plain token (MCP auto-selects the sole team)."""
    org_slug = None
    if x_treg_org:
        org = await _resolve_org(x_treg_org, db)
        if org is not None:
            m = (await db.execute(select(Membership).where(
                Membership.user_id == user.id, Membership.org_id == org.id))).scalar_one_or_none()
            if m is not None:               # only pin a team the caller actually belongs to
                org_slug = org.slug
    return {"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version, org=org_slug),
            "email": user.email, "org": org_slug}


@app.post("/auth/revoke-tokens")
async def auth_revoke_tokens(
    request: Request,
    user: User = Depends(require_identity),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Kill switch for a leaked token: invalidate every signed identity token (from `treg login`) AND
    every browser session this user holds, in one step. Bumping user.token_version makes all previously
    minted tokens (which carry the old tv) mismatch and be rejected. Unlike suspending the account this
    keeps the user active; unlike rotating TREG_SESSION_SECRET it affects ONLY this user. We then re-issue
    a fresh session cookie + token for the caller, so the device that pressed the button stays signed in
    while every other device is signed out. (Org membership tokens from accept-invite are a separate token
    type and are unaffected — those are revoked by removing the membership.)"""
    user.token_version += 1  # same db session as require_identity (FastAPI caches the dependency)
    await db.commit()
    resp = JSONResponse({"token": sess.make(user.id, CLI_TOKEN_TTL, user.token_version),
                         "email": user.email, "revoked": True})
    resp.set_cookie(sess.COOKIE, sess.make(user.id, token_version=user.token_version), httponly=True,
                    samesite="lax", secure=_is_https(request), max_age=sess.TTL_SECONDS)
    return resp


async def _user_from_identity_token(token: str, db: AsyncSession) -> User | None:
    """A signed identity token (from `treg login`) — same format as the session cookie, sent as a
    bearer by the CLI. Returns the user if valid + not suspended."""
    claims = sess.read_claims(token)
    if claims is None:
        return None
    user = await db.get(User, claims["uid"])
    if user is None or user.suspended or claims["tv"] != user.token_version:  # revoked = tv mismatch
        return None
    return user


async def require_member(
    request: Request,
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> Caller:
    """A caller acting in a specific org. Two ways in:
    - **token** (agents/CLI): the token IS a membership, so the org is baked in.
    - **session** (dashboard): the cookie identifies the user; the org is chosen via `X-Treg-Org`.
    """
    membership = await _membership_by_token(x_treg_token, db) if x_treg_token else None
    if membership is not None:  # per-org token — the org is baked in
        user = await db.get(User, membership.user_id)
        org = await db.get(Org, membership.org_id)
    else:
        # identity token (CLI `treg login`) or a browser session — pick the org via X-Treg-Org
        user = (await _user_from_identity_token(x_treg_token, db)) if x_treg_token else await _user_from_session(treg_session, db)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid token" if x_treg_token else "not authenticated")
        # The X-Treg-Org header wins; a team-pinned identity token (org baked into its claim) is the
        # fallback, so a copyable "API key" resolves as a BARE bearer where no header can travel — an
        # MCP server's Authorization. The header still overrides, so one token can act on another team
        # when the caller can set it (the CLI does). Only the token owner could sign it, so trusting
        # its own org claim grants nothing they could not already reach.
        org_ref = x_treg_org or ((sess.read_claims(x_treg_token) or {}).get("org", "") if x_treg_token else "")
        org = await _resolve_org(org_ref, db)
        if org is None:
            raise HTTPException(status_code=400, detail="choose an org (send X-Treg-Org)")
        membership = (
            await db.execute(
                select(Membership).where(Membership.user_id == user.id, Membership.org_id == org.id)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=403, detail="not a member of this org")
    if user is None or org is None:
        raise HTTPException(status_code=401, detail="invalid token")
    if user.suspended:
        raise HTTPException(status_code=403, detail="account suspended")
    if org.suspended:
        raise HTTPException(status_code=403, detail="org suspended")
    # Public-demo lockdown: the published token (non-admin roles) may ONLY call tools and read.
    # Centralized here — not per-endpoint — so every mutation (tools, secrets, skills, members,
    # leave, runs) is frozen no matter what routes are added later. Admin+ keeps full control.
    if org.public_demo and not _role_at_least(membership.role, "admin"):
        if not (request.url.path.startswith("/call/") or request.method in ("GET", "HEAD", "OPTIONS")):
            raise HTTPException(status_code=403, detail=(
                "this is a public demo team — its token can only call tools and read"))
    return Caller(membership=membership, user=user, org=org)


async def require_superadmin(
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> str:
    """Cross-tenant gate for /admin/*. Authorized by the env admin token, a token whose user is
    is_superadmin, OR a session whose user is is_superadmin. Returns a principal (for audit)."""
    admin = get_settings().admin_token
    if x_treg_token and admin and hmac.compare_digest(x_treg_token, admin):
        return "env-admin"
    user: User | None = None
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
    else:
        user = await _user_from_session(treg_session, db)
    if user is not None and user.is_superadmin and not user.suspended:
        return user.email
    if not x_treg_token and not treg_session:  # nothing presented → not authenticated
        raise HTTPException(status_code=401, detail="not authenticated")
    raise HTTPException(status_code=403, detail="super-admin required")


async def _is_last_active_superadmin(db: AsyncSession, target: User) -> bool:
    """True if `target` is currently the ONLY active (unsuspended) super-admin, so demoting /
    suspending / deleting them would leave the platform with no reachable admin."""
    if not (target.is_superadmin and not target.suspended):
        return False  # not an active super-admin → removing them changes nothing about the floor
    actives = (
        await db.execute(select(User).where(User.is_superadmin.is_(True), User.suspended.is_(False)))
    ).scalars().all()
    return len(actives) <= 1


# Every model carrying an `org_id`. Deleting the org without clearing these leaves rows pointing at
# a row that no longer exists, and the delete fails with a 500 at the foreign key.
#
# This list has to be kept in step with the schema, and twice it was not: the money tables arrived
# with the prepaid balance and `CapabilityPin` with capability pins, and neither was added here. The
# effect was invisible until someone tried it — since every NEW team is granted $1.00, every team has
# a CreditBlock, so NO team could be deleted at all. `test_org_delete_clears_every_org_scoped_table`
# now walks the models module and fails if a new one is ever missed, rather than trusting this list.
#
# Order matters: LedgerEntry references a CreditBlock, so it goes first.
_ORG_SCOPED_MODELS = (
    Tool, Secret, Bundle, PendingOAuth, CallRecord, RunRecord, Invite, DenyRule, Project,
    CapabilityPin,
    TagBudget,
    TagSpend,  # before the money tables it attributes: its rows reference a Hold that is about to go
    LedgerEntry, Hold, CreditBlock,
    OAuthCode, OAuthRefresh,   # grants naming a team that no longer exists
    IdempotentCall,            # a remembered answer belongs to the team that paid for it
    ToolRequest,  # attribution rows go with the team; anonymous filings carry no org_id and stay
    AdConversion,  # pending Google Ads conversions belong to the team they'd be attributed to
    Membership,   # last: it is what makes the caller a member of the org being deleted
)


async def _cascade_delete_org(org: Org, db: AsyncSession) -> None:
    """Delete every org-scoped row then the org. Shared by owner delete_org + admin force-delete."""
    # OAuthGrant names its mutable team `current_org_id` to distinguish family authority from the
    # immutable `OAuthRefresh.org_id` provenance. A family can name this team on EITHER side: after
    # a move, only a retired provenance row still names the former team. Deleting just that row
    # destroys the replay evidence while leaving the live family authorised elsewhere, so a stolen
    # old token becomes "unknown" instead of revoking every descendant. Revoke the union of both
    # paths; preserving historical provenance across team deletion would need a nullable/soft FK.
    authority_grants = (await db.execute(select(OAuthGrant).where(
        OAuthGrant.current_org_id == org.id))).scalars().all()
    provenance_families = (await db.execute(select(OAuthRefresh.family_id).where(
        OAuthRefresh.org_id == org.id))).scalars().all()
    family_ids = {grant.family_id for grant in authority_grants} | set(provenance_families)
    if family_ids:
        # Delete the WHOLE family, including rows issued under other teams. Keeping only the live
        # destination token would be exactly the partial revocation that reuse detection forbids.
        for token in (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.family_id.in_(family_ids)))).scalars().all():
            await db.delete(token)
        grants = (await db.execute(select(OAuthGrant).where(
            OAuthGrant.family_id.in_(family_ids)))).scalars().all()
    else:
        grants = []
    for grant in grants:
        await db.delete(grant)
    await db.flush()
    for model in _ORG_SCOPED_MODELS:
        for r in (await db.execute(select(model).where(model.org_id == org.id))).scalars().all():
            await db.delete(r)
        await db.flush()   # honour the ordering above rather than leaving it to the unit of work
    await db.delete(org)


def _role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, 99)


def _can_manage(caller: Caller, resource) -> bool:
    """Admin/owner may manage any resource in the org; a member only what they created."""
    return _role_at_least(caller.role, "admin") or resource.owner == caller.email


def _require_can_register(caller: Caller) -> None:
    """Registering (secrets/tools/skills/oauth) needs member+. A viewer may only call + read."""
    if not _role_at_least(caller.role, "member"):
        raise HTTPException(status_code=403, detail="viewers can call and read, but cannot register")


def _tool_allowed(caller: Caller, tool_name: str) -> bool:
    """Per-member tool ACL: allowed if the member's `tool_access` is unset (NULL = ALL tools) or names
    this tool. The OWNER is never restricted (the org's authority); admins/members can be."""
    if caller.role == "owner":
        return True
    access = caller.membership.tool_access
    return access is None or tool_name in access


def _require_tool_access(caller: Caller, tool_name: str) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on the member's tool ACL."""
    if not _tool_allowed(caller, tool_name):
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the tool {tool_name!r} in this team — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --tools …`)"))


def _project_allowed(caller: Caller, tool: Tool) -> bool:
    """Per-member PROJECT scope, the coarse dial above the per-tool one.

    NULL `project_access` = the whole org (the default, so nothing changed when projects landed), and a
    tool with NULL `project_id` is ORG-WIDE and always in scope — which is every tool that existed
    before projects. Owner is never restricted, matching `_tool_allowed`. Pure: `project_access` holds
    project IDs, so this is a set test with no query, even on the proxy's hot path."""
    if caller.role == "owner":
        return True
    access = caller.membership.project_access
    return access is None or tool.project_id is None or tool.project_id in access


def _tool_usable(caller: Caller, tool: Tool) -> bool:
    """The two ACL axes compose as AND: the project scope AND the per-tool list must both allow it.
    `project_access=[X]` with `tool_access=NULL` therefore means "every tool in project X, including
    ones added later" — the composition that makes the coarse dial useful on its own."""
    return _tool_allowed(caller, tool.name) and _project_allowed(caller, tool)


def _require_tool_use(caller: Caller, tool: Tool) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on BOTH ACL axes."""
    _require_tool_access(caller, tool.name)
    if not _project_allowed(caller, tool):
        raise HTTPException(status_code=403, detail=(
            f"the tool {tool.name!r} belongs to a project you're not scoped to — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --projects …`)"))


def _deny_match(rules: list[DenyRule], host: str, path: str, method: str) -> DenyRule | None:
    """The FIRST rule that matches — pure, so it unit-tests without a DB (like `localrun.check_deny`).

    An empty field on a rule means "any", so `{method: "DELETE"}` blocks every delete and
    `{host: "api.stripe.com"}` blocks that upstream entirely. Host is compared case-insensitively;
    the path match is a prefix, anchored at `/` so `/v1/charges` cannot be dodged by `/v1/chargesX`.
    """
    host, method = host.lower(), method.upper()
    path = path or "/"
    for r in rules:
        if r.host and r.host.lower() != host:
            continue
        if r.method and r.method.upper() != method:
            continue
        if r.path_prefix:
            p = (r.path_prefix if r.path_prefix.startswith("/") else "/" + r.path_prefix).rstrip("/")
            # Anchored at a segment boundary: `/v1/charges` must NOT match `/v1/chargesX`.
            if not (path == p or path.startswith(p + "/")):
                continue
        return r
    return None


async def _org_deny_rules(caller: Caller, db: AsyncSession) -> list[DenyRule]:
    """This caller's applicable rules: the org-wide ones plus the ones aimed at them specifically."""
    return list((await db.execute(select(DenyRule).where(
        DenyRule.org_id == caller.org_id,
        or_(DenyRule.user_id.is_(None), DenyRule.user_id == caller.membership.user_id),
    ))).scalars().all())


async def _enforce_deny(
    caller: Caller, url: str, method: str, db: AsyncSession, tool_project_id: int | None = None
) -> None:
    """Block a call the org's policy forbids. Deliberately applies to EVERY role including owner: a
    deny rule is a guardrail, not a permission tier — an owner who disagrees deletes the rule rather
    than quietly bypassing it. The refusal names the rule, mirroring `localrun.check_deny`'s
    "a refusal can name its source".

    `tool_project_id` = the project of the tool this call goes through (every enforcement point has
    resolved a Tool by then). A project-scoped rule (`project_id` set) fires only on that project's
    tools; an org-wide-tool call (`tool_project_id` None) is never caught by one."""
    rules = [r for r in await _org_deny_rules(caller, db)
             if r.project_id is None or r.project_id == tool_project_id]
    if not rules:
        return  # the common path costs one indexed query and nothing else
    try:
        parts = urlsplit(url)
    except ValueError:
        return
    rule = _deny_match(rules, parts.netloc, parts.path, method)
    if rule is None:
        return
    why = f" ({rule.note})" if rule.note else ""
    scope = "this team" if rule.user_id is None else "you"
    in_proj = " in this project" if rule.project_id is not None else ""
    raise HTTPException(status_code=403, detail=(
        f"blocked by a policy rule on {scope}{in_proj}{why} — "
        f"{rule.method or 'any'} {rule.host or 'any host'}{rule.path_prefix or ''}"))


async def _visible_secret_ids(caller: Caller, db: AsyncSession) -> set[int] | None:
    """The secret ids a tool-restricted member may SEE: the ones wired into their allowed tools
    (HTTP bindings + cli.inject). None = unrestricted (owner / NULL tool_access) — show all. The
    ACL isn't just a call gate: listings must not reveal credentials the member can't use."""
    if caller.role == "owner" or caller.membership.tool_access is None:
        return None
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    ids: set[int] = set()
    for t in tools:
        if not _tool_usable(caller, t):
            continue
        ids |= {b.get("secret_id") for b in (t.bindings or []) if b.get("secret_id") is not None}
        ids |= {e.get("secret_id") for e in ((t.cli or {}).get("inject") or []) if e.get("secret_id") is not None}
    return ids


def _require_local_run(caller: Caller) -> None:
    """Gate the LOCAL run tier on the member's `local_run_enabled` (owner exempt). Off → server only."""
    if caller.role != "owner" and not caller.membership.local_run_enabled:
        raise HTTPException(status_code=403, detail=(
            "local execution is disabled for you — run on the server instead (`treg run --server`), "
            "or ask an admin to enable local runs for your account"))


# ---- schemas ------------------------------------------------------------------------------
class UserIn(BaseModel):
    email: str
    webhook_url: str | None = None


class OrgIn(BaseModel):
    name: str


class InviteIn(BaseModel):
    email: str
    role: str = "member"
    expires_days: int = INVITE_TTL_DAYS
    # Access to seed onto the membership on accept: tool_access None = all tools, a list = the allowed
    # tool names; local_run may be turned off. Both default to the unrestricted state.
    tool_access: list[str] | None = None
    project_access: list[str | int] | None = None  # None = the whole org; slugs/ids = the scoped set
    local_run_enabled: bool = True
    landing: str | None = None  # a shared detail page ("/app/skills/<name>") to land on after sign-in


# Landing must be one of OUR detail paths — a path-only allowlist so an emailed invite link can never
# become an open redirect (no scheme, no host, no traversal, single trailing name segment).
_LANDING_RE = re.compile(r"^/app/(skills|tools)/[A-Za-z0-9][A-Za-z0-9._%-]*$")


class AcceptIn(BaseModel):
    code: str
    email: str


class RoleIn(BaseModel):
    role: str


class CapIn(BaseModel):
    daily_call_cap: int  # per-user, per-day usage cap for the member; -1 = unlimited


class AccessIn(BaseModel):
    # tool_access: None = all tools (clear the restriction); a list = the ONLY tool names allowed.
    tool_access: list[str] | None = None
    # project_access: None = the whole org; a list of project SLUGS or IDS = the only projects allowed.
    # Accepts slugs because that's the human handle; stored as ids (see _normalize_project_access).
    project_access: list[str | int] | None = None
    local_run_enabled: bool = True


class SecretIn(BaseModel):
    name: str
    value: str
    kind: str = "env"
    bundle_id: int | None = None


class SecretUpdate(BaseModel):
    name: str | None = None
    value: str | None = None
    kind: str | None = None


class ToolIn(BaseModel):
    name: str
    base_url: str
    bundle_id: int | None = None
    # Multi-binding (explicit) — each: {secret_id, injector, location, name, format, secret_field}
    bindings: list[dict] | None = None
    # Single-binding sugar (the common case): provide secret_id + placement, get one binding.
    secret_id: int | None = None
    injector: str = "env"
    auth_in: str = "header"
    auth_name: str = "Authorization"
    auth_format: str = "Bearer {secret}"
    secret_field: str = "access_token"
    health_check: dict | None = None  # {method, path, expect_status}
    examples: list[dict] | None = None  # [{method, path, note}]
    cli: dict | None = None  # local-run profile for `treg run` (docs/CLI-RUN-PLAN.md)
    project: str | int | None = None  # project slug or id; None = org-wide (the default)


class ToolUpdate(BaseModel):
    base_url: str | None = None
    bindings: list[dict] | None = None
    health_check: dict | None = None
    examples: list[dict] | None = None
    cli: dict | None = None  # set/replace the local-run profile; explicit null clears it
    project: str | int | None = None  # move between projects; explicit null makes it org-wide


class GrantIn(BaseModel):
    argv: list[str] = []  # the CLI args the member is about to run (deny-checked + audited)


class RunReportIn(BaseModel):
    audit_id: int      # the grant's audit row — proves this report follows a real grant
    exit_code: int
    verdict: str       # ok | credential_invalid | unknown_error (client matched stderr locally)


class BundleUpdate(BaseModel):
    recipe: str | None = None  # edit the SKILL.md text of a recipe/skill bundle
    # (Run metadata moved to Tool.cli — a tool with a cli profile is runnable.)


class SkillSecretIn(BaseModel):
    local_name: str  # name within the skill; bindings reference it by this
    value: str
    kind: str = "env"


class SkillToolIn(BaseModel):
    name: str
    base_url: str
    bindings: list[dict] = []  # each binding's "secret" is a local_name, resolved server-side
    health_check: dict | None = None  # optional {method, path, expect_status}
    examples: list[dict] = []  # optional [{method, path, note}]
    cli: dict | None = None  # optional local-run profile; inject entries may reference local_names


class SkillIn(BaseModel):
    name: str
    recipe: str = ""  # the SKILL.md text
    files: dict[str, str] = {}  # companion files {relpath: content} — the rest of the skill folder
    secrets: list[SkillSecretIn] = []
    tools: list[SkillToolIn] = []
    # (Execution config — both run tiers — lives in each tool's `cli` block: bin/server/enabled/inject.)


class SkillFileIn(BaseModel):
    path: str      # the file's path relative to the picked folder (webkitRelativePath)
    content: str


class SkillAnalyzeIn(BaseModel):
    files: list[SkillFileIn] = []


class SkillImportIn(BaseModel):
    files: list[SkillFileIn] = []
    select: list[str] = []           # skill names to register (empty = every ready one)
    env_values: dict[str, str] = {}  # user-filled values for env secrets missing from the upload


class OAuthStartIn(BaseModel):
    """Two modes. BYO: supply client_id/client_secret/auth_uri/token_uri/scopes yourself.
    REGISTRY: supply `provider` (+ optional `capability`) and treg fills all of it from its own
    approved app — see oauth_providers.py."""

    name: str = ""  # the secret name to create on success; defaults to the provider service
    provider: str | None = None  # registry service id, e.g. "google-search-console"
    capability: str | None = None  # which scope set to request (default: read)
    # Reconnect/widen an EXISTING connection instead of adding another one. Omit to add.
    connection_id: int | None = None
    client_id: str = ""
    client_secret: str = ""
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    scopes: list[str] = []
    redirect_uri: str | None = None  # defaults to treg's public callback


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:  # e.g. unbalanced IPv6 brackets "http://[::1" → don't 500, reject the input
        raise HTTPException(status_code=422, detail="base_url is not a valid URL")


def _normalize_scheme(rest: str) -> str:
    """A path param collapses `https://` to `https:/`; restore it."""
    for sch in ("https:/", "http:/"):
        if rest.startswith(sch) and not rest.startswith(sch + "/"):
            return sch + "/" + rest[len(sch):]
    return rest


def _flat_binding(body: ToolIn) -> dict:
    return {
        "secret_id": body.secret_id,
        "injector": body.injector,
        "location": body.auth_in,
        "name": body.auth_name,
        "format": body.auth_format,
        "secret_field": body.secret_field,
    }


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "org"


def _norm_email(email: str) -> str:
    """Canonical email identity: trimmed + lowercased. One human = one identity regardless of the
    case they type. Applied at every identity door + every invite comparison so `Bob@X.com` and
    `bob@x.com` never fork into two users / two orgs and an invite is always redeemable."""
    return email.strip().lower()


async def _unique_slug(base: str, db: AsyncSession) -> str:
    slug, i = base, 2
    while (await db.execute(select(Org).where(Org.slug == slug))).scalar_one_or_none() is not None:
        slug, i = f"{base}-{i}", i + 1
    return slug


async def _make_org_membership(
    db: AsyncSession, user: User, name: str, slug_base: str, role: str, webhook_url: str | None = None
) -> tuple[Org, str]:
    """Create an Org + an owner/role Membership for `user`, minting a fresh org-scoped token.
    Returns (org, plaintext token). Caller commits.
    """
    org = Org(name=name, slug=await _unique_slug(slug_base, db))
    db.add(org)
    await db.flush()
    token = crypto.new_token()
    db.add(
        Membership(
            user_id=user.id, org_id=org.id, role=role,
            token_hash=crypto.hash_token(token), webhook_url=webhook_url,
        )
    )
    return org, token


async def _grant_signup_promo(db: AsyncSession, org: Org) -> None:
    """Give a BRAND-NEW org its promotional balance, so an agent's first call needs no key and no card
    (`settings.promo_grant_micro`, $1 by default). Called after the org is committed, from every door
    that creates a real team — `ledger.grant` is idempotent per (org, kind), so a retried signup or a
    second door can't double-grant, and existing orgs are never backfilled.

    Demo/sandbox teams are created elsewhere (demo.py / sandbox.py) and deliberately get nothing: a
    published demo token must not be able to spend real money. A grant failure must not fail the
    signup — the org exists, and it can be topped up — so it is logged, not raised.
    """
    if org is None or org.id is None or org.demo or org.public_demo:
        return
    try:
        # Queue BEFORE granting: adsconv.queue() only adds a row inside a SAVEPOINT, it never commits.
        # ledger.grant() commits internally, so calling it second is what makes its commit durable for
        # BOTH rows in one transaction — the event and its conversion must land together (see
        # adsconv.queue's docstring). Reordering this silently reintroduces a two-transaction gap.
        # Same door, same once-only guarantee: this function is already the single place a brand-new
        # real team comes into existence.
        try:
            await adsconv.queue(db, org, adsconv.ACTION_SIGNUP)
        except Exception as exc:  # noqa: BLE001 — its OWN guard, deliberately, not the outer one
            # Because the queue now runs FIRST, sharing the outer except would mean an unexpected
            # failure here (anything but the IntegrityError queue() already absorbs) skips the grant
            # entirely and costs the team its $1 promotional credit. A marketing metric must not be
            # able to take away a product benefit: swallow it here so the grant still runs.
            logging.getLogger("treg").warning("ad conversion queue failed for org %s: %s", org.id, exc)
        await ledger.grant(db, org.id)  # commits — absorbs the queued conversion row too
    except Exception as exc:  # noqa: BLE001 — the team is already created; don't 500 the signup over credit
        logging.getLogger("treg").warning("promo grant failed for org %s: %s", org.id, exc)


async def _find_or_create_user(db: AsyncSession, email: str) -> User:
    """Find a user by email, else register them — the user ONLY, **no auto personal org**. The shared
    core of every identity door (GitHub / Google / email OTP). A brand-new user therefore lands with
    zero teams and is asked to NAME + CREATE their first team (the dashboard's mandatory welcome, or
    `treg org create`) — we never spawn a throwaway personal org they didn't ask for. Their identity
    token is user-scoped, so it works before they have any org (org chosen per-request via X-Treg-Org).
    Caller commits."""
    email = _norm_email(email)
    # Machine identities (agents, the published demo token) are minted by an admin and act ONLY by
    # their token. This is the single choke point every identity door shares, so blocking here means
    # no door — GitHub, Google, email OTP, invite sign-in — can hand a human an agent's identity.
    # (The domains are unroutable, so a code could never be delivered anyway; this makes it explicit.)
    if _is_machine_email(email):
        raise HTTPException(status_code=403, detail="this address cannot be used to sign in")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email)
        db.add(user)
        try:
            await db.flush()  # surfaces the unique-email violation on a concurrent first-login race
        except IntegrityError:
            await db.rollback()  # another worker just created this same new user — reuse theirs
            return (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return user


def _ad_attribution_from(request: Request) -> tuple[str, str, str]:
    """Return (click-id field, click-id, landing), with legacy GCLID-cookie compatibility."""
    if not adsconv.enabled():
        return "", "", ""
    raw = request.cookies.get("treg_ad") or ""
    if not raw:
        return "", "", ""
    first, separator, rest = unquote(raw).partition("|")
    if separator and first in ("gclid", "gbraid", "wbraid"):
        click_id, _, landing = rest.partition("|")
        click_field = first
    else:
        # Old cookies were `CLICK_ID|landing` and always held a GCLID.
        click_field, click_id, landing = "gclid", first, rest
    return click_field, click_id.strip()[:255], landing.strip()[:64]


# ---- users (open registration; personal org + owner membership; token shown once) ---------
@app.post("/users")
async def register_user(body: UserIn, request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    email = _norm_email(body.email)
    # This door creates a User directly (it predates `_find_or_create_user`), so it needs the same
    # machine-domain block — otherwise open registration could squat an agent address.
    if _is_machine_email(email):
        raise HTTPException(status_code=403, detail="this address cannot be used to sign in")
    if body.webhook_url and not health.safe_webhook_url(body.webhook_url):  # SSRF guard on the alert URL
        raise HTTPException(status_code=422, detail="webhook_url must be a public http(s) URL")
    if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(email=email)
    db.add(user)
    await db.flush()
    org, token = await _make_org_membership(
        db, user, name=email, slug_base=_slugify(email), role="owner", webhook_url=body.webhook_url
    )
    click_field, gclid, landing = _ad_attribution_from(request)
    if gclid:
        org.ad_gclid = gclid
        org.ad_click_id_type = click_field
        org.ad_landing = landing or None
        org.ad_click_at = _utcnow_naive()  # naive UTC: asyncpg rejects tz-aware into a
                                            # TIMESTAMP WITHOUT TIME ZONE column (see models._now).
        db.add(org)
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="email already registered")
    await _grant_signup_promo(db, org)
    # Both org-creating doors redeem, for the same reason both grant the signup promo: this one
    # predates `_find_or_create_user` but still ends with a person owning a fresh team and a balance.
    await _redeem_referral(db, request, user, org)
    return {"id": user.id, "email": user.email, "org": org.slug, "org_id": org.id, "role": "owner", "token": token}


# ---- orgs, invites, members (multi-tenancy management) ------------------------------------
def _require_admin_of(org_id: int, caller: Caller) -> None:
    """The caller must be acting with THIS org's token (token = a membership) and be admin+."""
    if caller.org_id != org_id or not _role_at_least(caller.role, "admin"):
        raise HTTPException(status_code=403, detail="admin role in this org is required")


def _require_owner_of(org_id: int, caller: Caller) -> None:
    """Owner-only actions (change roles, delete org). Token is org-scoped, so must match."""
    if caller.org_id != org_id or caller.role != "owner":
        raise HTTPException(status_code=403, detail="owner role in this org is required")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now_ms() -> int:
    """A monotonic millisecond stamp for measuring a call's duration — never the wall clock, which can
    step backwards (NTP) and produce a negative latency."""
    return int(time.monotonic() * 1000)


def _as_naive(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=None) if (dt is not None and dt.tzinfo is not None) else dt


async def _count_owners(org_id: int, db: AsyncSession) -> int:
    rows = (
        await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.role == "owner"))
    ).scalars().all()
    return len(rows)


@app.post("/orgs")
async def create_org(
    body: OrgIn, request: Request,
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session),
) -> dict:
    # Any authenticated user spins up a new org and becomes its owner, minting a fresh org-scoped
    # token for it. **require_identity, NOT require_member** — a brand-new user has zero orgs (no auto
    # personal org anymore), so creating their FIRST team must not require already being in one.
    if demo_sandbox.is_sandbox_user(user):  # the anonymous demo can't mint a real team — sign in first
        raise HTTPException(status_code=403, detail=(
            "the demo sandbox can't create a real team — sign in with GitHub, Google, or email to make one"))
    user_id = user.id  # snapshot BEFORE the loop: db.rollback() expires ORM instances, so
    name = body.name   # touching `user` afterwards could trigger a lazy load → MissingGreenlet.
    click_field, gclid, landing = _ad_attribution_from(request)  # the OTHER signup door
    # (browser sign-in → mandatory first-team creation), separate from register_user's /users. A
    # browser visitor who clicked an ad lands here, not there, so attribution must be read in both.
    for _ in range(3):  # a concurrent create can take the slug between _unique_slug and commit — retry
        org = Org(name=name, slug=await _unique_slug(_slugify(name), db))
        db.add(org)
        await db.flush()
        token = crypto.new_token()
        db.add(Membership(user_id=user_id, org_id=org.id, role="owner", token_hash=crypto.hash_token(token)))
        if gclid:
            org.ad_gclid = gclid
            org.ad_click_id_type = click_field
            org.ad_landing = landing or None
            org.ad_click_at = _utcnow_naive()  # naive UTC: asyncpg rejects tz-aware into a
                                                # TIMESTAMP WITHOUT TIME ZONE column (see models._now).
            db.add(org)
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(status_code=409, detail="could not allocate a unique org slug — retry")
    await _grant_signup_promo(db, org)
    await _redeem_referral(db, request, user, org)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": "owner", "token": token}


async def _redeem_referral(db: AsyncSession, request: Request, user: User, org: Org) -> None:
    """Attribute a brand-new team to whoever's link brought them here. Owes nothing yet — the bonus
    is earned at the team's first paid top-up, not at signup (see referrals.py).

    Team creation is the right and only redemption point: `_find_or_create_user` deliberately makes
    no org, so this is where a person first becomes a tenant with a balance. It fires on EVERY team
    a user creates, and `referrals.attribute` is what refuses the ones that should not count — a
    self-referral, a demo team, or an org that already carries a referral.

    Swallow-and-log, matching `_grant_signup_promo` immediately above: a referral is a marketing
    nicety and a signup is not. Nothing here may ever be the reason someone cannot make a team.
    """
    try:
        code = _take_referral(request)
        if code:
            await referrals.attribute(db, user=user, org=org, code=code)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("treg").warning("referral attribution failed for org %s: %s", org.id, exc)


class GrantTeamIn(BaseModel):
    team: str = ""          # the slug (or numeric id) of a team the signed-in user belongs to


@app.get("/oauth/grants", include_in_schema=False)
async def oauth_grants(user: User = Depends(require_identity),
                       db: AsyncSession = Depends(get_session)) -> list[dict]:
    """The MCP connections this account has granted, and which team each one spends from.

    The team on a grant was chosen once, on a consent screen, and after that it was invisible from
    every side: the client reports a slug, the CLI lists the teams of whichever identity is logged
    in THERE, and the two need not be the same account at all. Somebody spent from a team they could
    not see listed anywhere and had no way to recognise as wrong.
    """
    rows = (await db.execute(select(OAuthRefresh).where(
        OAuthRefresh.user_id == user.id, OAuthRefresh.retired_at.is_(None)
    ).order_by(OAuthRefresh.created_at.desc()))).scalars().all()
    rows = [row for row in rows if _refresh_is_live(row)]
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:                      # one entry per GRANT, not per rotation of its token
        if row.family_id in seen:
            continue
        seen.add(row.family_id)
        # Listing and refresh must read the SAME family authority. A token row's org_id is immutable
        # issue provenance and may legitimately name the team used before a later move.
        grant = await _ensure_grant(row.family_id, db)
        org_id = grant.current_org_id if grant is not None else None
        org = await db.get(Org, org_id) if org_id is not None else None
        client = (await db.execute(select(OAuthClient).where(
            OAuthClient.client_id == row.client_id))).scalar_one_or_none()
        out.append({
            "grant": row.family_id,
            "client": (client.client_name if client else "") or row.client_id,
            "team": org.slug if org else None,
            "team_name": org.name if org else None,
            "granted": grant.granted_at.isoformat(timespec="seconds") if grant else None,
        })
    # GET normally reads only, but repairing a family created by an old rolling-deploy instance is
    # a durable compatibility backfill. Without this commit the response looks healed once while
    # the inserted authority row is rolled back when the request session closes.
    await db.commit()
    return out


@app.post("/oauth/grants/{family_id}/team", include_in_schema=False)
async def oauth_grant_set_team(family_id: str, body: GrantTeamIn,
                               user: User = Depends(require_identity),
                               db: AsyncSession = Depends(get_session)) -> dict:
    """Re-point a live grant at another of the user's teams — without re-doing the OAuth dance.

    The team is stored on the refresh family rather than only inside the issued access token, so
    moving it is a row update and the next refresh picks it up. Re-consenting works too, but it
    means disconnecting a working connector in whatever client holds it, and "the only way to fix
    which balance this spends is to tear the connection down" is not an answer for the person who
    just found out they were spending from the wrong one.

    Only the GRANT'S OWN user may move it, and only to a team THEY are a member of — a grant must
    never become a way to reach a team the consent screen would not have offered.
    """
    from . import mcp_oauth

    rows = (await db.execute(select(OAuthRefresh).where(
        OAuthRefresh.family_id == family_id, OAuthRefresh.user_id == user.id,
        OAuthRefresh.retired_at.is_(None)))).scalars().all()
    rows = [row for row in rows if _refresh_is_live(row)]
    if not rows:
        raise HTTPException(status_code=404, detail=f"no live grant {family_id!r} on this account")
    org = await _resolve_org(body.team, db)
    member = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org.id))).scalar_one_or_none() if org else None
    # ONE answer for "no such team" and "a team that isn't yours". Told apart, this route reports
    # whether an arbitrary slug exists on treg — a slug-existence oracle any signed-in account could
    # walk. The caller's own teams are already listed to them by `treg org ls`, so the distinction
    # buys them nothing they cannot see elsewhere.
    if org is None or org.suspended or member is None:
        raise HTTPException(status_code=404, detail=(
            f"no team {body.team!r} on this account — see `treg org ls` for the teams you can use"))
    # Change only family authority. Token rows are evidence of where each historical bearer was
    # issued and must stay immutable, especially retired rows kept for reuse detection.
    grant = await _ensure_grant(family_id, db)
    if grant is None:  # the live-row check above makes this defensive, not a normal outcome
        raise HTTPException(status_code=404, detail=f"no live grant {family_id!r} on this account")
    grant.current_org_id = org.id
    db.add(grant)
    await db.commit()
    return {
        "grant": family_id, "team": org.slug, "team_name": org.name,
        # Access tokens live an hour and carry the old team until the client refreshes. Saying so
        # is the difference between "it didn't work" and "it hasn't taken effect yet".
        "note": (f"new calls spend from {org.slug!r} once the client refreshes its access token "
                 f"(within {mcp_oauth.ACCESS_TTL_SECONDS // 60} minutes)"),
    }


@app.get("/orgs")
async def list_orgs(
    user: User = Depends(require_identity),
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    # "active" = the caller's current org — the token's org (token auth) or X-Treg-Org (session).
    current: int | None = None
    if x_treg_token and (m := await _membership_by_token(x_treg_token, db)):
        current = m.org_id
    else:
        # Identity token or session: X-Treg-Org wins, then a team-pinned identity token's own org
        # claim — the same precedence `require_member` uses to authorize the call. Without the claim
        # fallback, no org is marked active for a team-pinned token and clients guess (badly).
        ref = x_treg_org or ((sess.read_claims(x_treg_token) or {}).get("org", "") if x_treg_token else "")
        org = await _resolve_org(ref, db)
        current = org.id if org else None
    memberships = (
        await db.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalars().all()
    org_ids = [m.org_id for m in memberships]
    orgs = {  # one batched query instead of one db.get per membership (N+1 on the org-switcher path)
        o.id: o for o in (await db.execute(
            select(Org).where(Org.id.in_(org_ids))
        )).scalars().all()
    }
    # Tool count per org (one grouped query) so the dashboard can land on the org that actually has
    # tools, instead of a first-run default that may be an empty team.
    tool_counts = dict((await db.execute(
        select(Tool.org_id, func.count(Tool.id)).where(Tool.org_id.in_(org_ids)).group_by(Tool.org_id)
    )).all())
    out: list[dict] = []
    for m in memberships:
        org = orgs.get(m.org_id)
        if org is None:
            continue
        out.append({
            "org_id": org.id, "slug": org.slug, "name": org.name,
            "role": m.role, "active": org.id == current, "demo": org.demo,
            "tool_count": tool_counts.get(org.id, 0),
        })
    return out


@app.post("/orgs/{org_id}/invites")
async def create_invite(
    org_id: int, body: InviteIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_admin_of(org_id, caller)
    if body.role not in ("viewer", "member", "admin"):
        raise HTTPException(status_code=422, detail="role must be 'viewer', 'member', or 'admin'")
    # Role assignment is owner-only (see set_member_role); the invite door must honour the same
    # boundary or an admin could mint fellow admins that they can't otherwise create.
    if body.role == "admin" and caller.role != "owner":
        raise HTTPException(status_code=403, detail="only an owner can invite an admin")
    email = _norm_email(body.email)
    # An email already in the org can't accept a new invite (accept would 409) — reject the dead-end up front.
    existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing_user is not None:
        m = (await db.execute(select(Membership).where(
            Membership.user_id == existing_user.id, Membership.org_id == org_id
        ))).scalar_one_or_none()
        if m is not None:
            raise HTTPException(status_code=409, detail="that email is already a member of this org")
    # Supersede any prior pending invite for this email so there's exactly one live code per invitee
    # (re-inviting used to stack duplicate pending rows that all point at the same seat).
    for prior in (await db.execute(select(Invite).where(
        Invite.org_id == org_id, Invite.email == email, Invite.status == "pending"
    ))).scalars().all():
        await db.delete(prior)
    days = max(1, min(body.expires_days, 3650))  # clamp BOTH ends — a huge value overflows datetime → 500
    expires_at = _utcnow_naive() + timedelta(days=days)
    tool_access = _normalize_tool_access(body.tool_access, await _known_access_names(org_id, db))
    project_access = await _normalize_project_access(body.project_access, org_id, db)
    if body.landing is not None and not _LANDING_RE.match(body.landing):
        raise HTTPException(status_code=422, detail="landing must be a detail path like /app/skills/<name>")
    code = crypto.new_token()
    # A SECOND secret for the email link only. The admin gets `code` back (out-of-band relay) so the
    # code can never be a sign-in factor; `email_token` is never returned here — only the inbox sees
    # it, which is what lets /auth/invite-signin treat it like an emailed OTP and mint a session.
    email_token = crypto.new_token()
    invite = Invite(
        org_id=org_id, email=email, role=body.role,
        code_hash=crypto.hash_token(code), email_token_hash=crypto.hash_token(email_token),
        invited_by=caller.email, expires_at=expires_at,
        tool_access=tool_access, project_access=project_access,
        local_run_enabled=body.local_run_enabled, landing=body.landing,
    )
    db.add(invite)
    await db.commit()
    org = await db.get(Org, org_id)  # for the invite email's team name
    if not email.endswith("@" + demo_seed.DEMO_DOMAIN):  # don't email the onboarding's fake teammate domain
        scheme = "https" if _is_https(request) else request.url.scheme
        host = request.headers.get("host", "")
        shared = ""  # share-born invite → the email leads with what was shared
        if body.landing:
            kind, _, name = body.landing.removeprefix("/app/").partition("/")
            shared = f'the {"skill" if kind == "skills" else "tool"} “{name}”'
        await email_sender.send_invite(  # best-effort; the code is also returned for out-of-band relay
            email, caller.email, (org.name if org else email), body.role, code, email_token,
            expires_at.isoformat(), link_base=(f"{scheme}://{host}" if host else ""), shared=shared,
        )
    return {"code": code, "email": email, "role": body.role, "org_id": org_id,
            "expires_at": expires_at.isoformat()}  # email_token deliberately NOT returned (inbox-only)


@app.post("/invites/accept")
async def accept_invite(body: AcceptIn, db: AsyncSession = Depends(get_session)) -> dict:
    # Open endpoint, protected by the unguessable one-time code. Registers the user if new,
    # joins them to the org, and mints their own org-scoped token (the admin never sees it).
    invite = (
        await db.execute(select(Invite).where(Invite.code_hash == crypto.hash_token(body.code)))
    ).scalar_one_or_none()
    email = _norm_email(body.email)
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invalid or already-used invite code")
    if invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive():
        raise HTTPException(status_code=410, detail="invite code expired")
    if invite.email != email:
        raise HTTPException(status_code=403, detail="this invite is for a different email")
    org = await db.get(Org, invite.org_id)
    if org is not None and org.suspended:  # don't let anyone join a platform-locked org
        raise HTTPException(status_code=403, detail="org suspended")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and user.suspended:  # a banned user must not accrue new memberships
        raise HTTPException(status_code=403, detail="account suspended")
    if user is None:
        # Brand-new user → create the user only. Accepting the invite below IS their first team
        # (no auto personal org — consistent with the login doors).
        user = User(email=email)
        db.add(user)
        await db.flush()
    existing = (
        await db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == invite.org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this org")
    token = crypto.new_token()
    db.add(Membership(user_id=user.id, org_id=invite.org_id, role=invite.role, token_hash=crypto.hash_token(token),
                      tool_access=invite.tool_access, project_access=invite.project_access,
                      local_run_enabled=invite.local_run_enabled))
    invite.status = "accepted"
    try:
        await db.commit()  # a concurrent double-accept trips uq_membership_user_org — 409, not 500
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already a member of this org")
    org = await db.get(Org, invite.org_id)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": invite.role, "token": token}


@app.get("/invites/mine")
async def my_invites(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every pending invite addressed to MY email — the code-free door. Proving my email (via any
    login method) is enough to see these; the invite code becomes a shortcut, not a requirement."""
    rows = (
        await db.execute(select(Invite).where(Invite.email == user.email, Invite.status == "pending")
                         .order_by(Invite.created_at.desc()))  # newest first — the invite you just clicked
    ).scalars().all()
    now = _utcnow_naive()
    orgs = {  # batch the org lookup (was one db.get per invite)
        o.id: o for o in (await db.execute(
            select(Org).where(Org.id.in_([inv.org_id for inv in rows]))
        )).scalars().all()
    }
    out = []
    for inv in rows:
        if inv.expires_at is not None and _as_naive(inv.expires_at) < now:
            continue
        org = orgs.get(inv.org_id)
        if org is None or org.suspended:  # a platform-locked org isn't joinable — don't surface it
            continue
        out.append({
            "id": inv.id, "org": org.slug, "org_id": org.id, "name": org.name, "role": inv.role,
            "invited_by": inv.invited_by, "landing": inv.landing,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return out


# ---- onboarding (first-run demo team) -----------------------------------------------------
class OnboardIn(BaseModel):
    team_name: str = "Acme Design"


@app.post("/onboard/demo")
async def onboard_demo(
    body: OnboardIn | None = None,
    user: User = Depends(require_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Seed a sandbox team owned by the caller — fake teammates (one per role) + a working `echo`
    tool + sample activity — so a brand-new user can feel the product immediately. Idempotent
    (reuses an existing demo team); marks the caller onboarded. Same seed for dashboard + CLI."""
    return await demo_seed.provision(db, user, (body.team_name if body else "Acme Design"))


@app.post("/onboard/skip")
async def onboard_skip(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Dismiss onboarding without seeding — so it's never auto-offered again."""
    user.onboarded = True
    await db.commit()
    return {"onboarded": True}


@app.post("/onboard/reset")
async def onboard_reset(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Remove the caller's demo team(s) + demo teammates from their real teams — a clean exit."""
    return await demo_seed.reset(db, user)


# ---- landing-page sandbox studio: an anonymous, throwaway team the visitor builds ----------
# Per-IP limiter for the unauthenticated mint endpoint, in the DB (treg.ratestore) so it survives a
# restart and holds across instances (backlog #3). It caps DB churn from the public landing page (abuse
# is otherwise structurally contained — sandbox calls never touch the network, each sandbox is capped + TTL'd).
SANDBOX_HIT_NS = "sandbox_hit"
SANDBOX_RATE_MAX = 12          # sandboxes per IP per window
SANDBOX_RATE_WINDOW_S = 3600   # 1 hour

# Per-IP limiter for /call with a PUBLIC-DEMO token (the landing page publishes one shared member
# token, so the per-user daily cap is meaningless there — thousands of strangers are one "user").
PUBLIC_DEMO_HIT_NS = "pubdemo_call"
PUBLIC_DEMO_RATE_MAX = 10      # calls per IP per window
PUBLIC_DEMO_RATE_WINDOW_S = 60


def _client_ip(request: Request) -> str:
    """Best-effort client IP — first hop of X-Forwarded-For behind the reverse proxy (Render), else the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


async def _enforce_public_demo_ip_cap(request: Request, db: AsyncSession) -> None:
    """Per-IP cap for a call made with a SHARED public credential — the published demo token or
    the sandbox live wire. Both are one identity for thousands of strangers, so meter by client IP
    rather than by user. Commits the sweep + recorded hit (get_session never auto-commits) and
    raises 429 when the window is exhausted."""
    await ratestore.sweep(db, PUBLIC_DEMO_HIT_NS)
    allowed = await ratestore.rate_check(
        db, PUBLIC_DEMO_HIT_NS, [(_client_ip(request), PUBLIC_DEMO_RATE_MAX)], PUBLIC_DEMO_RATE_WINDOW_S)
    await db.commit()
    if not allowed:
        raise HTTPException(status_code=429, detail=(
            f"demo limit reached ({PUBLIC_DEMO_RATE_MAX} calls/min per IP) — try again in a minute"))


async def _enforce_sandbox_cap(caller: Caller, model, cap: int, noun: str, db: AsyncSession) -> None:
    """Sandbox orgs may hold only a few secrets/endpoints — keep the public playground bounded."""
    if not demo_sandbox.is_sandbox(caller.org):
        return
    n = (await db.execute(select(func.count()).select_from(model).where(model.org_id == caller.org_id))).scalar_one()
    if n >= cap:
        raise HTTPException(status_code=422, detail=f"the sandbox is limited to {cap} {noun} — sign up for more")


# ---- per-user daily usage cap (usage-metering v1) -------------------------------------------
def _day_start_utc() -> datetime:
    """Midnight (00:00) of the current UTC day, naive — matches how *Record.created_at is stored."""
    return _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)


async def count_today(db: AsyncSession, org_id: int | None, user_email: str) -> int:
    """How many usage events this user has produced in this org since midnight UTC: proxy calls +
    local-run grants (both `CallRecord`) plus server runs (`RunRecord`). Two indexed COUNTs."""
    since = _day_start_utc()
    calls = (await db.execute(select(func.count()).select_from(CallRecord).where(
        CallRecord.org_id == org_id, CallRecord.user_email == user_email, CallRecord.created_at >= since,
    ))).scalar_one()
    runs = (await db.execute(select(func.count()).select_from(RunRecord).where(
        RunRecord.org_id == org_id, RunRecord.user_email == user_email, RunRecord.created_at >= since,
    ))).scalar_one()
    return calls + runs


async def _enforce_daily_cap(caller: Caller, db: AsyncSession) -> None:
    """Refuse a call/run once the caller has used their per-user daily cap for this org. `-1` (the
    default) = unlimited, so unmetered members pay ZERO extra queries. The sandbox has its own limiter
    and is exempt. Soft by design: the count reads best-effort `CallRecord`s, so under heavy load it
    can lag slightly and fail OPEN (a few extra slip through) — never closed. See docs/USAGE-METERING-PLAN.md."""
    cap = caller.membership.daily_call_cap
    if cap < 0 or demo_sandbox.is_sandbox(caller.org):
        return
    used = await count_today(db, caller.org_id, caller.email)
    if used >= cap:
        raise HTTPException(status_code=429, detail=(
            f"daily usage limit reached ({used}/{cap}) — ask an admin to raise your cap"))


async def _used_today_by_user(db: AsyncSession, org_id: int) -> dict[str, int]:
    """{user_email: events today} for every member of the org — one grouped COUNT per table, so the
    members list gets everyone's usage without an N+1 fan-out. Spans all kinds (calls + local + server)."""
    since = _day_start_utc()
    counts: dict[str, int] = {}
    for email, n in (await db.execute(select(CallRecord.user_email, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since).group_by(CallRecord.user_email))).all():
        counts[email] = counts.get(email, 0) + n
    for email, n in (await db.execute(select(RunRecord.user_email, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.user_email))).all():
        counts[email] = counts.get(email, 0) + n
    return counts


async def _usage_rollup(db: AsyncSession, org_id: int, since: datetime) -> dict:
    """Aggregate usage since `since` into by-user (with a per-kind split), by-tool, by-day, and totals.
    CallRecord carries `kind` ("call"/"local_run"); every RunRecord is a "server_run". Pure GROUP BY —
    no request/response bodies are read (we don't store them). See docs/USAGE-METERING-PLAN.md."""
    KINDS = ("call", "local_run", "server_run")
    totals = {k: 0 for k in KINDS}
    users: dict[str, dict] = {}

    def _bump(email: str, kind: str, n: int) -> None:
        u = users.setdefault(email, {"user_email": email, **{k: 0 for k in KINDS}})
        u[kind] += n
        totals[kind] += n

    for email, kind, n in (await db.execute(select(CallRecord.user_email, CallRecord.kind, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since
    ).group_by(CallRecord.user_email, CallRecord.kind))).all():
        _bump(email, kind if kind in KINDS else "call", n)  # guard an unexpected kind into "call"
    for email, n in (await db.execute(select(RunRecord.user_email, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.user_email))).all():
        _bump(email, "server_run", n)

    by_user = sorted(
        ({**u, "total": sum(u[k] for k in KINDS)} for u in users.values()),
        key=lambda r: -r["total"])
    totals["total"] = sum(totals[k] for k in KINDS)

    tools: dict[str, int] = {}
    for name, n in (await db.execute(select(CallRecord.tool_name, func.count()).where(
            CallRecord.org_id == org_id, CallRecord.created_at >= since).group_by(CallRecord.tool_name))).all():
        tools[name] = tools.get(name, 0) + n
    for name, n in (await db.execute(select(RunRecord.bundle_name, func.count()).where(
            RunRecord.org_id == org_id, RunRecord.created_at >= since).group_by(RunRecord.bundle_name))).all():
        tools[name] = tools.get(name, 0) + n
    by_tool = sorted(({"name": k, "total": v} for k, v in tools.items()), key=lambda r: -r["total"])

    days: dict[str, int] = {}  # func.date() → 'YYYY-MM-DD' on sqlite, a date on Postgres; str() both
    for tbl in (CallRecord, RunRecord):
        for d, n in (await db.execute(select(func.date(tbl.created_at), func.count()).where(
                tbl.org_id == org_id, tbl.created_at >= since).group_by(func.date(tbl.created_at)))).all():
            days[str(d)] = days.get(str(d), 0) + n
    by_day = sorted(({"day": k, "total": v} for k, v in days.items()), key=lambda r: r["day"])

    # What those calls COST the team on treg's own keys — read from the ledger (the authority on money)
    # rather than from the audit rows, which are fire-and-forget and may be incomplete. One aggregate.
    spend = await ledger.spend_since(db, org_id, since)
    return {"totals": totals, "by_user": by_user, "by_tool": by_tool, "by_day": by_day, "spend": spend}


@app.post("/demo/sandbox")
async def demo_sandbox_mint(request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """Mint a login-free, short-lived sandbox TEAM for the landing-page studio: a throwaway org + a
    starter secret + a starter endpoint + a member token, returned so the browser (and the visitor's
    terminal) can register more, call them, and export a skill — all with no account. Sandbox calls
    never touch the network (see call_tool → sandbox.synthesize); rate-limited per IP; GC'd after the
    TTL. No auth — this is the anonymous front door."""
    await ratestore.sweep(db, SANDBOX_HIT_NS)  # evict cold IP keys so the namespace can't grow unbounded
    if not await ratestore.rate_check(db, SANDBOX_HIT_NS,
                                      [(_client_ip(request), SANDBOX_RATE_MAX)], SANDBOX_RATE_WINDOW_S):
        await db.commit()  # persist the sweep even on reject
        raise HTTPException(status_code=429, detail="too many demo sandboxes from here — try again later")
    await db.commit()  # persist the recorded hit before minting
    await demo_sandbox.gc(db)  # opportunistic reap of expired sandboxes
    out = await demo_sandbox.mint(db)
    out["live"] = bool(get_settings().demo_stripe_key)  # is the seeded stripe tool a real wire?
    return out


@app.get("/demo/sandbox/live")
async def demo_sandbox_live(caller: Caller = Depends(require_member)) -> dict:
    """Live-wire facts for an EXISTING sandbox (the browser reuses one via localStorage, so it may
    predate the mint response carrying them): is the wire on, and who am I in the feed."""
    if not demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=400, detail="live-wire info is for the landing-page sandbox only")
    return {"live": bool(get_settings().demo_stripe_key),
            "visitor": demo_sandbox.visitor_name(caller.org.slug)}


# ---- landing-page live payments feed (the public Stripe demo — see pubfeed.py) --------------
@app.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """Stripe → treg: a signed event from the demo sandbox account. Only `charge.succeeded` feeds
    the landing ticker; everything else is acknowledged and dropped. 404 when unconfigured, so a
    deploy without the secret exposes no unauthenticated POST surface."""
    secret = get_settings().demo_stripe_webhook_secret
    if not secret:
        raise HTTPException(status_code=404)
    payload = await request.body()
    if not pubfeed.verify_signature(payload, request.headers.get("stripe-signature", ""), secret):
        raise HTTPException(status_code=400, detail="bad signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad payload")
    if event.get("type") == "charge.succeeded":
        pubfeed.push_charge(event.get("data", {}).get("object", {}) or {})
    return {"received": True}


@app.get("/landing/stripe-feed", include_in_schema=False)
async def landing_stripe_feed() -> StreamingResponse:
    """SSE stream for the landing demo pane: recent charges, then live ones. Unauthenticated by
    design — it carries only server-chosen fields (amount/currency/created/id-suffix)."""
    return StreamingResponse(pubfeed.stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # tell the reverse proxy not to buffer the stream
    })


@app.get("/demo/sandbox/skill")
async def demo_sandbox_skill(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Export whatever the visitor built in their sandbox as a shareable **skill** (treg.json manifest
    + SKILL.md + install commands). Sandbox-only — the payoff that shows what skills are."""
    if not demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=400, detail="skill export is for the landing-page sandbox only")
    return await demo_sandbox.export_skill(db, caller.org)


@app.get("/skills/samples")
async def skill_samples() -> list[dict]:
    """The hosted sample skills the landing offers — each with its files (SKILL.md/treg.json/.secret)
    and the prompt to try. Public: the landing renders these as file packages."""
    base = get_settings().public_url.rstrip("/")
    return [{"name": n, "label": s["label"], "key": s["key"], "prompt": s["prompt"],
             "files": demo_sandbox.skill_files(n, base, None)}
            for n, s in demo_sandbox.SAMPLE_SKILLS.items()]


@app.get("/skills/{name}/install.sh", include_in_schema=False)
async def skill_install(name: str, token: str = ""):
    """`curl -fsSL {BASE}/skills/<name>/install.sh?token=<t> | sh` — writes the skill into
    ./.claude/skills/<name>/ so Claude Code loads it. The token (if given) is baked into the
    recipe's calls; without it the recipe reads the token from `treg login`."""
    if name not in demo_sandbox.SAMPLE_SKILLS:
        raise HTTPException(status_code=404, detail=f"unknown skill {name!r}")
    # The token is interpolated into a shell script the visitor runs (`curl … | sh`). Restrict it to a
    # real token charset so a crafted value can't inject a newline + commands into the generated script.
    if token and not re.fullmatch(r"[A-Za-z0-9_\-]{1,200}", token):
        raise HTTPException(status_code=422, detail="invalid token")
    base = get_settings().public_url.rstrip("/")
    script = demo_sandbox.install_script(name, base, token or None)
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


class TeammateIn(BaseModel):
    email: str


@app.post("/onboard/seed-tool")
async def onboard_seed_tool(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Pre-seed the working `echo` tool into the caller's active team so the no-key call in the
    dashboard onboarding just works (the user builds the team + invites by hand; the tool is on us)."""
    _require_can_register(caller)
    org = await db.get(Org, caller.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    return await demo_seed.seed_tool(db, org, caller.email)


@app.post("/onboard/accept-teammate")
async def onboard_accept_teammate(
    body: TeammateIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Auto-accept the fake teammate the user just invited during onboarding, so it lands in the
    roster instantly (they feel the invite, then see the loop close). Admin+ only, demo email only."""
    _require_admin_of(caller.org_id, caller)
    email = _norm_email(body.email)
    if not email.endswith("@" + demo_seed.DEMO_DOMAIN):
        raise HTTPException(status_code=400, detail="onboarding auto-accept is for demo teammates only")
    inv = (await db.execute(select(Invite).where(
        Invite.org_id == caller.org_id, Invite.email == email, Invite.status == "pending"))).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="no pending invite for that email")
    return await demo_seed.accept_demo_invite(db, caller.org_id, inv)


@app.post("/invites/{invite_id}/accept")
async def accept_my_invite(
    invite_id: int, user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)
) -> dict:
    """Accept an invite addressed to my already-proven email — no code needed (the identity token
    proves the email). The code path (`POST /invites/accept`) stays for out-of-band joins."""
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invalid or already-used invite")
    if invite.email != user.email:
        raise HTTPException(status_code=403, detail="this invite is for a different email")
    if invite.expires_at is not None and _as_naive(invite.expires_at) < _utcnow_naive():
        raise HTTPException(status_code=410, detail="invite expired")
    org = await db.get(Org, invite.org_id)
    if org is not None and org.suspended:  # don't let anyone join a platform-locked org
        raise HTTPException(status_code=403, detail="org suspended")
    existing = (
        await db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == invite.org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this org")
    token = crypto.new_token()  # return the org-scoped token (was minted-then-discarded → an unusable membership)
    db.add(Membership(
        user_id=user.id, org_id=invite.org_id, role=invite.role, token_hash=crypto.hash_token(token),
        tool_access=invite.tool_access, project_access=invite.project_access,
        local_run_enabled=invite.local_run_enabled,
    ))
    invite.status = "accepted"
    try:
        await db.commit()  # a concurrent double-accept trips uq_membership_user_org — 409, not 500
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already a member of this org")
    org = await db.get(Org, invite.org_id)
    return {"org": org.slug, "org_id": org.id, "name": org.name, "role": invite.role, "token": token}


@app.get("/orgs/{org_id}/invites")
async def list_invites(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    await health.gc_expired_invites(db, org_id)  # purge dead codes so the list shows only live ones
    await db.commit()
    rows = (
        await db.execute(select(Invite).where(Invite.org_id == org_id, Invite.status == "pending"))
    ).scalars().all()
    return [
        {
            "id": i.id, "email": i.email, "role": i.role, "invited_by": i.invited_by,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "created_at": i.created_at.isoformat(),
        }
        for i in rows
    ]


@app.delete("/orgs/{org_id}/invites/{invite_id}")
async def revoke_invite(
    org_id: int, invite_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_admin_of(org_id, caller)
    invite = await db.get(Invite, invite_id)
    if invite is None or invite.org_id != org_id or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invite not found")  # can't "revoke" an accepted/consumed one
    await db.delete(invite)  # the code can no longer be accepted
    await db.commit()
    return {"revoked_invite": invite_id}


@app.get("/orgs/{org_id}/members")
async def list_members(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    memberships = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    users = {  # batch the user lookup (was one db.get per member)
        u.id: u for u in (await db.execute(
            select(User).where(User.id.in_([m.user_id for m in memberships]))
        )).scalars().all()
    }
    used = await _used_today_by_user(db, org_id)  # one grouped query, not N+1
    out: list[dict] = []
    for m in memberships:
        user = users.get(m.user_id)
        if user is not None:
            out.append({"user_id": user.id, "email": user.email, "role": m.role,
                        "daily_call_cap": m.daily_call_cap, "used_today": used.get(user.email, 0),
                        "tool_access": m.tool_access, "project_access": m.project_access,
                        "local_run_enabled": m.local_run_enabled,
                        # so the dashboard can separate people from machines in one roster —
                        # agents carry their short name + owner, so the UI never shows the raw
                        # machine address and can group each agent under its creator
                        "is_agent": _is_agent_email(user.email),
                        "name": (_agent_name(caller.org, user.email)
                                 if _is_agent_email(user.email) else None),
                        "created_by": m.created_by})
    return out


@app.get("/orgs/{org_id}/usage")
async def org_usage(
    org_id: int, days: int = 30,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Usage rollups for an org over the last `days` (admin/owner): by user (with a call/local/server
    split), by tool, by day, and totals — counts only, no request/response bodies. Powers the dashboard
    Usage view."""
    _require_admin_of(org_id, caller)
    days = max(1, min(days, 365))
    since = _day_start_utc() - timedelta(days=days - 1)  # inclusive of today + the prior days-1
    return {"days": days, "since": since.isoformat(), **await _usage_rollup(db, org_id, since)}


@app.get("/orgs/{org_id}/balance")
async def org_balance(
    org_id: int, limit: int = 20, offset: int = 0,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The org's prepaid balance. Amounts are integer micro-USD (`*_micro`) with a display-only USD
    twin — never compute against the USD field (see ledger.py on why money is integers here).

    **Two audiences, one route.** Any MEMBER sees the figure and the in-flight holds: they are the
    ones spending it, every agent is told to run `treg balance` after a call, and a 402 already hands
    them `balance_micro` anyway — refusing the same number here while shipping it in an error was
    incoherent. The FUNDING DETAIL is admin+: the credit blocks (what was bought, when, what is left
    of each) and the ledger, which together are the org's purchase history, not its wallet.
    """
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="not a member of this org")
    detailed = _role_at_least(caller.role, "admin")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    balance = await ledger.balance_of(db, org_id)
    # Auto-top-up's trigger point until phase 3 calls it right after `reserve`. Fire-and-forget by
    # contract (see billing.maybe_schedule_autotopup): it starts a background task at most, so no
    # Stripe latency lands in this response, and reading a balance can therefore never be slow.
    billing.maybe_schedule_autotopup(caller.org)
    blocks = await ledger.blocks_of(db, org_id)
    holds = await ledger.open_holds_of(db, org_id)
    entries = await ledger.entries_of(db, org_id, limit=limit, offset=offset)
    return {
        "org_id": org_id,
        "balance_micro": balance,
        "balance_usd": ledger.usd(balance),
        "promo_grant_micro": get_settings().promo_grant_micro,
        # admin+ only — see the docstring: the wallet is everyone's, the purchase history is not
        "blocks": [] if not detailed else [
            {"id": b.id, "kind": b.kind, "amount_micro": b.amount_micro,
             "remaining_micro": b.remaining_micro, "remaining_usd": ledger.usd(b.remaining_micro),
             "currency": b.currency, "expires_at": b.expires_at.isoformat() if b.expires_at else None,
             "created_at": b.created_at.isoformat() if b.created_at else None}
            for b in blocks
        ],
        "holds": [
            {"call_id": h.id, "endpoint_id": h.endpoint_id, "amount_micro": h.amount_micro,
             "created_at": h.created_at.isoformat() if h.created_at else None}
            for h in holds
        ],
        "entries": {
            "limit": limit, "offset": offset,
            "items": [] if not detailed else [
                {"id": e.id, "kind": e.kind, "amount_micro": e.amount_micro,
                 "amount_usd": ledger.usd(e.amount_micro), "block_id": e.block_id,
                 "call_id": e.call_id, "endpoint_id": e.endpoint_id, "meta": e.meta,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in entries
            ],
        },
    }


@app.get("/orgs/{org_id}/tag-keys")
async def list_tag_keys(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Every tag key this team has actually SENT, plus the ones it may budget on.

    Two different questions that had been given one answer. Reporting works on ANY key — the money
    for an undeclared one is folded in Python — while ENFORCEMENT only works on a declared key,
    because it needs an index. Feeding a reporting picker the declared list hid `feature=` and
    friends from the dashboard even though the API served them fine.
    """
    _require_admin_of(org_id, caller)
    seen = (await db.execute(
        select(TagSpend.dim).where(TagSpend.org_id == org_id).distinct())).scalars().all()
    declared = _budget_dims_of(caller.org)
    return {"seen": sorted(set(seen)), "budgetable": declared,
            "primary": _primary_dim_of(caller)}


@app.get("/orgs/{org_id}/usage/by-tag")
async def usage_by_tag(
    org_id: int, key: str | None = None, days: int = 30,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """What each value of one tag consumed — the numbers a reselling builder invoices from.

    MONEY COMES FROM THE LEDGER, never from `CallRecord`. Audit rows are fire-and-forget and the queue
    sheds them under load, which is precisely the traffic a successful builder generates; an invoice
    built on them would under-bill silently and unrecoverably. Call COUNTS come from the audit table,
    where losing a row costs a slightly low count and nothing else.

    `unattributed` is reported explicitly rather than dropped. A builder reconciling this against their
    own ledger has to see the spend they cannot attribute to anyone — silently omitting it is how the
    two sets of books stop agreeing without anybody noticing.
    """
    _require_admin_of(org_id, caller)
    days = max(1, min(days, 365))
    since = _day_start_utc() - timedelta(days=days - 1)
    dim = (key or _primary_dim_of(caller)).strip().lower()

    by_value = await ledger.spend_by_tag(db, org_id, dim, since)
    org_total = (await ledger.spend_since(db, org_id, since))["spend_micro"]
    # Counts come from the tag rows too. `CallRecord` holds only the primary dimension, so counting
    # there reported 0 for every non-primary key while the money column was correct — a report that
    # disagrees with itself is worse than one that admits its grain.
    counts = await ledger.calls_by_tag(db, org_id, dim, since)
    rows = [{"value": val, "charged_micro": micro, "charged_usd": ledger.usd(micro),
             "calls": int(counts.get(val, 0))}
            for val, micro in sorted(by_value.items(), key=lambda kv: -kv[1])]
    attributed = sum(by_value.values())
    return {
        "key": dim, "days": days, "since": since.isoformat(),
        "rows": rows,
        # The identity a builder's invoice rests on: these three reconcile against the team's own
        # settled spend for the window, whichever dimension they slice by.
        "attributed_micro": attributed,
        "unattributed_micro": org_total - attributed,
        "total_micro": org_total, "total_usd": ledger.usd(org_total),
    }


# ---- team settings: the spend ceiling and the tag dimensions ---------------------------------
class OrgSettingsIn(BaseModel):
    daily_cap_micro: int | None = None
    budget_dims: list[str] | None = None
    primary_dim: str | None = None


@app.get("/orgs/{org_id}/settings")
async def get_org_settings(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The team's spend ceiling and tag configuration. Readable by any member — a limit nobody can see
    is a limit that turns into a support ticket the first time an agent trips it."""
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="not your org")
    org = caller.org
    return {"daily_cap_micro": _effective_daily_cap(org),
            "daily_cap_set_by_team": int(org.daily_cap_micro or 0) or None,
            "platform_ceiling_micro": get_settings().platform_daily_cap_micro,
            "budget_dims": _budget_dims_of(org), "primary_dim": _primary_dim_of(caller)}


@app.patch("/orgs/{org_id}/settings")
async def set_org_settings(
    org_id: int, body: OrgSettingsIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set the team's own spend ceiling and which tag keys carry budgets. Admin+.

    A team may LOWER its ceiling freely; raising it past the platform ceiling is refused rather than
    silently clamped, because a builder who thinks they set $500/day and actually got $5 discovers it
    as an outage in the middle of their launch.
    """
    _require_admin_of(org_id, caller)
    org = caller.org
    sent = body.model_fields_set
    if "daily_cap_micro" in sent and body.daily_cap_micro is not None:
        ceiling = get_settings().platform_daily_cap_micro
        if body.daily_cap_micro < 0:
            raise HTTPException(status_code=422, detail="daily_cap_micro must be 0 or more")
        if body.daily_cap_micro > ceiling:
            raise HTTPException(status_code=403, detail={
                "error": "above_platform_ceiling", "requested_micro": body.daily_cap_micro,
                "ceiling_micro": ceiling,
                "message": (f"${ledger.usd(ceiling):g}/day is the ceiling we allow for a team. Ask us "
                            f"to raise it — reselling volume is a conversation, not a setting."),
            })
        org.daily_cap_micro = body.daily_cap_micro
    if "budget_dims" in sent and body.budget_dims is not None:
        dims = [d.strip().lower() for d in body.budget_dims if d and d.strip()]
        if len(dims) > _MAX_BUDGET_DIMS:
            raise HTTPException(status_code=422, detail=(
                f"at most {_MAX_BUDGET_DIMS} budget dimensions — each one is an indexed lookup on "
                f"every call and a row per distinct value"))
        for d in dims:
            if not _META_KEY_RE.match(d):
                raise HTTPException(status_code=422, detail=f"{d!r} is not a valid tag key")
        org.budget_dims = dims or None
    if "primary_dim" in sent and body.primary_dim:
        if not _META_KEY_RE.match(body.primary_dim):
            raise HTTPException(status_code=422, detail=f"{body.primary_dim!r} is not a valid tag key")
        org.primary_dim = body.primary_dim
    await db.commit()
    return await get_org_settings(org_id, caller, db)


# ---- per-tag budgets: what a reselling builder sets on THEIR users ---------------------------
class TagBudgetIn(BaseModel):
    daily_cap_micro: int | None = None
    monthly_cap_micro: int | None = None
    calls_per_day: int | None = None
    status: str | None = None
    note: str | None = None


def _tag_budget_view(row: TagBudget) -> dict:
    return {"dim": row.dim, "val": row.val, "is_default": row.val == TAG_DEFAULT,
            "daily_cap_micro": row.daily_cap_micro,
            "monthly_cap_micro": row.monthly_cap_micro, "calls_per_day": row.calls_per_day,
            "status": row.status, "note": row.note,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@app.get("/orgs/{org_id}/budgets")
async def list_tag_budgets(
    org_id: int, dim: str | None = None,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Every per-tag limit this team has SET — the per-dimension defaults plus the overrides.

    Registry rows are excluded. One is created per distinct value so the cardinality check stays a
    cheap lookup, and listing them made a table of eight rows in which six limited nothing: the
    bookkeeping was being presented as if it were policy.

    Admin+, because a budget names the team's customers.
    """
    _require_admin_of(org_id, caller)
    q = select(TagBudget).where(TagBudget.org_id == org_id, TagBudget.auto.is_(False))
    if dim:
        q = q.where(TagBudget.dim == dim)
    rows = (await db.execute(q.order_by(TagBudget.dim, TagBudget.val))).scalars().all()
    # Defaults first within each dimension — they are what everything else is an exception to.
    rows.sort(key=lambda r: (r.dim, r.val != TAG_DEFAULT, r.val))
    return [_tag_budget_view(r) for r in rows]


@app.put("/orgs/{org_id}/budgets/{dim}")
async def set_tag_default(
    org_id: int, dim: str, body: TagBudgetIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set the DEFAULT limit for a whole dimension — what every value inherits without an override.

    Unlimited until this is set: a team that never calls it behaves exactly as before. Changing it
    takes effect on the next call for everyone without an override, since resolution happens per
    call — so lowering a default is a live change across the whole customer base.
    """
    return await set_tag_budget(org_id, dim, TAG_DEFAULT, body, caller, db)


@app.put("/orgs/{org_id}/budgets/{dim}/{val}")
async def set_tag_budget(
    org_id: int, dim: str, val: str, body: TagBudgetIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set (or update) one limit — `PUT /orgs/1/budgets/customer/cust_8123 {"daily_cap_micro": 5000000}`.

    An UPSERT that leaves unsent fields alone, the same `model_fields_set` shape `create_agent` uses:
    a PUT that only flips `status` must not silently wipe the caps someone set last week.
    """
    _require_admin_of(org_id, caller)
    if not _META_KEY_RE.match(dim):
        raise HTTPException(status_code=422, detail=f"{dim!r} is not a valid tag key")
    if val != TAG_DEFAULT:
        _validate_tag_pair(dim, val)  # same rule as the call path — this value becomes a storage key
    declared = _budget_dims_of(caller.org)
    if dim not in declared:
        # Setting a limit IS the declaration. Requiring a separate PATCH first made the common path a
        # hidden two-step: the tag shows up in usage reports, so a person reasonably expects to be
        # able to cap it, and got a 422 telling them to go configure something else first.
        #
        # The BOUND still holds, because it is what keeps the call path cheap — each declared
        # dimension is another indexed lookup on every proxied call and another row per value. Past
        # the limit, refuse and say which ones are in use, since only the team knows which to drop.
        if len(declared) >= _MAX_BUDGET_DIMS:
            raise HTTPException(status_code=422, detail={
                "error": "too_many_budget_dimensions", "dim": dim, "declared": declared,
                "limit": _MAX_BUDGET_DIMS,
                "message": (f"budgets are already set up on {', '.join(declared)} — {_MAX_BUDGET_DIMS} "
                            f"is the limit, because each one is checked on every call. Remove one "
                            f"first if you want to budget on {dim!r} instead."),
            })
        caller.org.budget_dims = [*declared, dim]
        declared = caller.org.budget_dims
    if body.status is not None and body.status not in ("active", "blocked"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'blocked'")
    row = await _tag_budget(db, org_id, dim, val, create=True)
    row.auto = False  # a human set this: it is policy now, not registry bookkeeping
    sent = body.model_fields_set
    for field in ("daily_cap_micro", "monthly_cap_micro", "calls_per_day", "status", "note"):
        if field in sent:
            setattr(row, field, getattr(body, field))
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return _tag_budget_view(row)


@app.delete("/orgs/{org_id}/budgets/{dim}/{val}")
async def delete_tag_budget(
    org_id: int, dim: str, val: str,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Drop a limit. The tag keeps being recorded and invoiced — only the ceiling goes away."""
    _require_admin_of(org_id, caller)
    row = await _tag_budget(db, org_id, dim, val)
    if row is None:
        raise HTTPException(status_code=404, detail="no budget for that tag")
    await db.delete(row)
    await db.commit()
    return {"deleted": {"dim": dim, "val": val}}


# ---- billing: Stripe top-ups (see billing.py) -----------------------------------------------
class TopupIn(BaseModel):
    amount_usd: float | None = None


class AutoTopupIn(BaseModel):
    enabled: bool
    threshold_usd: float | None = None
    amount_usd: float | None = None
    monthly_cap_usd: float | None = None
    # Explicit, per-request agreement to unattended charges — the MIT mandate. Required to ENABLE when
    # there is no timestamp on file; ignored when disabling (nobody consents to stopping).
    consent: bool = False


def _billing_org(caller: Caller) -> Org:
    """Billing acts on the caller's OWN org and needs admin+ — the same gate as /usage and /balance,
    because a card and a spend policy are the org's money, not a member's preference."""
    _require_admin_of(caller.org_id, caller)
    if caller.org is None:
        raise HTTPException(status_code=404, detail="org not found")
    return caller.org


def _return_base(request: Request) -> str:
    """Where Stripe sends the payer back — the deployment they were actually using, not whatever
    `public_url` says, so a local or preview server returns to itself."""
    host = request.headers.get("host", "")
    if not host:
        return ""
    return f"{'https' if _is_https(request) else request.url.scheme}://{host}"


@app.get("/billing")
async def billing_get(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The org's billing state: whether top-ups are available at all on this deployment, whether
    there's a Stripe customer and a saved card, the auto-top-up policy + why it's off if it is, and how
    much of this month's automatic cap has been used."""
    org = _billing_org(caller)
    state = await billing.billing_state(db, org)
    # Merged HERE rather than inside `billing_state`, so billing.py keeps its one job (Stripe) and
    # does not grow a second reason to know about referrals. This is the screen where a referred team
    # is already deciding how much to add, so it is the only place the minimum actually changes a
    # decision — see referrals.offer_for_org.
    state["referral_offer"] = await referrals.offer_for_org(db, org.id)
    return state


@app.post("/billing/topup")
async def billing_topup(
    request: Request, body: TopupIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Start a hosted Stripe Checkout for a one-off top-up and return its URL.

    Returns a URL, not a credit: the balance moves when Stripe's webhook says the payment succeeded
    (see billing.py). Nothing about this response — including a payer who "completes" the success
    redirect by hand — can create balance.
    """
    org = _billing_org(caller)
    amount = body.amount_usd if body.amount_usd is not None else get_settings().topup_default_usd
    try:
        out = await billing.create_topup_checkout(
            db, org, amount, return_base=_return_base(request), email=caller.email)
    except billing.BillingNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except billing.TopupRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    # The one place the actual payer's identity exists — the webhook that later credits the
    # balance is org-scoped, so the started/completed funnel joins on the team group.
    analytics.capture(caller.email, "topup_started",
                      {"amount_usd": amount, "org": org.slug}, groups={"team": org.slug})
    return out


@app.post("/billing/autotopup")
async def billing_autotopup(
    request: Request, body: AutoTopupIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set the org's auto-top-up policy (and record consent when enabling).

    Enabling without a card on file is not an error — the preferences and the consent are stored and a
    Stripe-hosted card-capture URL comes back in `setup_url`. Finishing that page fires
    `setup_intent.succeeded`, which saves the payment method and arms the policy. That ordering is
    deliberate: consent is recorded against the numbers the human saw, before any card exists.
    """
    org = _billing_org(caller)
    if body.enabled and not body.consent and not org.autotopup_consented_at:
        raise HTTPException(
            status_code=422,
            detail="enabling auto top-up requires consent: true (you are authorizing charges to a "
                   "saved card without being present)")
    try:
        await billing.set_autotopup(
            db, org, enabled=body.enabled, consent=body.consent,
            threshold_usd=body.threshold_usd, amount_usd=body.amount_usd,
            monthly_cap_usd=body.monthly_cap_usd)
    except billing.TopupRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    state = await billing.billing_state(db, org)
    if body.enabled and not org.stripe_default_pm:
        try:
            state["setup_url"] = (await billing.create_setup_checkout(
                db, org, return_base=_return_base(request), email=caller.email))["url"]
        except billing.BillingNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
    return state


@app.get("/billing/history")
async def billing_history(
    limit: int = Query(24, ge=1, le=100),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """This team's completed top-ups, newest first, each with its invoice PDF or card receipt.

    Read-only in both directions: it moves no money, and the amounts come from our own credit blocks
    rather than from Stripe, so the history can never contradict the balance. Stripe is asked only for
    the document links, and `stripe_ok: false` says they were unavailable — the payments listed are
    still correct.
    """
    org = _billing_org(caller)
    return await billing.list_payments(db, org, limit=limit)


@app.post("/billing/portal")
async def billing_portal(
    request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """A one-time link into Stripe's hosted billing portal — card, billing address, tax ID, and the
    full invoice archive. 422 until the team has a Stripe customer, which it gets on its first
    payment; `billing_state`'s `portal` flag is what the UI hides the button on."""
    org = _billing_org(caller)
    try:
        return await billing.create_portal_session(db, org, return_base=_return_base(request))
    except billing.BillingNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except billing.TopupRejected as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---- referrals ---------------------------------------------------------------------------------
# Deliberately `require_identity`, not `require_member`: a referral belongs to a PERSON, not to one
# of their teams (see models.Referral). The reward lands in an org, but which org is our decision,
# not the caller's — so nothing here is scoped by `X-Treg-Org`.
@app.get("/referrals")
async def my_referrals(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session),
) -> dict:
    """This person's referral link and everyone who has used it.

    Also runs the payout sweep, scoped to this user. There is no scheduler in treg, so the two
    trigger points are any top-up (`billing._credit`) and this page — which means someone checking
    whether their reward has landed is the one who makes it land. That is the same lazy,
    caller-pays-for-their-own-cleanup bargain as `ledger.reap_stale_holds`.
    """
    # Mint the code here too, not only on POST. Asking for this page IS the lazy trigger the code
    # was always meant to hang off, and every caller needs a usable `link` — a response carrying an
    # empty one is a footgun for any client that doesn't know to POST first.
    try:
        await referrals.ensure_code(db, user)
    except Exception as exc:  # noqa: BLE001 — a code we couldn't mint is an empty link, not a 500
        logging.getLogger("treg").warning("referral code mint failed for user %s: %s", user.id, exc)
    try:
        await referrals.sweep(db, referrer_user_id=user.id)
    except Exception as exc:  # noqa: BLE001 — pragma: no cover
        logging.getLogger("treg").warning("referral sweep failed for user %s: %s", user.id, exc)
    return await referrals.summary(db, user)


@app.post("/referrals/code")
async def mint_referral_code(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint this person's referral code, or return the one they already have.

    Idempotent, so the dashboard can call it every time the page opens without checking first. Codes
    are minted here rather than at signup because most people never open this page, and a code
    nobody has seen is a unique index entry earning nothing.
    """
    code = await referrals.ensure_code(db, user)
    return {"code": code, "link": f"{get_settings().public_url.rstrip('/')}/?ref={code}"}


@app.post("/billing/stripe/webhook", include_in_schema=False)
async def billing_stripe_webhook(request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """Stripe → treg: the ONLY door through which a payment becomes balance.

    A DIFFERENT endpoint from the landing demo's `/stripe/webhook`, with a different signing secret:
    they are different Stripe accounts' events with different consequences, and sharing a path would
    mean one secret could authorize the other's effects. 404 when unconfigured, so a deploy without the
    secret exposes no unauthenticated POST surface.
    """
    if not get_settings().stripe_webhook_secret:
        raise HTTPException(status_code=404)
    payload = await request.body()
    try:
        event = billing.verify_event(payload, request.headers.get("stripe-signature", ""))
    except ValueError:
        # Deliberately terse: a signature oracle should not explain itself.
        raise HTTPException(status_code=400, detail="bad signature")
    try:
        result = await billing.handle_webhook_event(db, event)
    except Exception as e:  # noqa: BLE001
        # 500 tells Stripe to retry, which is what we want for a transient failure — but log loudly:
        # an event that never succeeds is money someone paid and didn't get.
        logging.getLogger("treg.billing").exception("webhook %s failed: %s", event.get("type"), e)
        raise HTTPException(status_code=500, detail="webhook handling failed")
    return {"received": True, **result}


@app.patch("/orgs/{org_id}/members/{user_id}/cap")
async def set_member_cap(
    org_id: int, user_id: int, body: CapIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set a member's per-user daily usage cap (admin/owner). `-1` = unlimited; any other negative is
    rejected. Separate from role (owner-only) — capping is a management action, not a privilege change."""
    _require_admin_of(org_id, caller)
    if body.daily_call_cap < -1:
        raise HTTPException(status_code=422, detail="daily_call_cap must be -1 (unlimited) or >= 0")
    membership = (await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    membership.daily_call_cap = body.daily_call_cap
    await db.commit()
    return {"user_id": user_id, "org_id": org_id, "daily_call_cap": body.daily_call_cap}


async def _known_tool_names(org_id: int, db: AsyncSession) -> set[str]:
    rows = (await db.execute(select(Tool.name).where(Tool.org_id == org_id))).all()
    return {r[0] for r in rows}


async def _known_access_names(org_id: int, db: AsyncSession) -> set[str]:
    """Everything an access list may name: tool names (the call/run gate) plus bundle names (the
    skill-visibility gate) — so a recipe-only skill can be granted even though it has no tool."""
    bundles = (await db.execute(select(Bundle.name).where(Bundle.org_id == org_id))).all()
    return await _known_tool_names(org_id, db) | {r[0] for r in bundles}


def _normalize_tool_access(names: list[str] | None, known: set[str]) -> list[str] | None:
    """Validate a requested access list against the org's tools + skills. None → None (all). A list
    must name only real tools/skills (else 422). A list covering EVERYTHING collapses to None."""
    if names is None:
        return None
    unknown = [t for t in names if t not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown tool/skill(s): {', '.join(sorted(set(unknown)))}")
    chosen = set(names)
    return None if chosen >= known and known else sorted(chosen)  # everything checked → 'all' (NULL)


@app.patch("/orgs/{org_id}/members/{user_id}/access")
async def set_member_access(
    org_id: int, user_id: int, body: AccessIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set which tools a member may call/run (`tool_access`: None = all, else the allowed names) and
    whether they may run locally (`local_run_enabled`). Admin/owner only; an owner can't be restricted."""
    _require_admin_of(org_id, caller)
    membership = (await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner":
        raise HTTPException(status_code=403, detail="an owner always has full access; it can't be restricted")
    membership.tool_access = _normalize_tool_access(body.tool_access, await _known_access_names(org_id, db))
    # Only touch the project scope when the caller actually SENT the field. Without this, any client
    # that PATCHes just tool_access or local_run_enabled (the dashboard's local-run toggle does exactly
    # that) would silently clear the member's project scoping, because the field defaults to None.
    if "project_access" in body.model_fields_set:
        membership.project_access = await _normalize_project_access(body.project_access, org_id, db)
    membership.local_run_enabled = body.local_run_enabled
    await db.commit()
    return {"user_id": user_id, "org_id": org_id, "tool_access": membership.tool_access,
            "project_access": membership.project_access,
            "local_run_enabled": membership.local_run_enabled}


@app.get("/usage/me")
async def my_usage(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's own usage today + cap for the active org — so a member sees 'used / cap' without
    admin access. `cap` is -1 when unlimited."""
    return {"org": caller.org.slug, "used_today": await count_today(db, caller.org_id, caller.email),
            "cap": caller.membership.daily_call_cap}


@app.delete("/orgs/{org_id}/members/{user_id}")
async def remove_member(
    org_id: int, user_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_admin_of(org_id, caller)
    membership = (
        await db.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if membership.role == "owner":  # only an owner manages owners; an admin cannot remove one
        raise HTTPException(status_code=403, detail="owners cannot be removed")
    await db.delete(membership)  # revokes that user's token for this org
    await _drop_member_deny_rules(db, user_id, org_id)
    await db.commit()
    return {"removed": user_id}


@app.patch("/orgs/{org_id}/members/{user_id}")
async def set_member_role(
    org_id: int, user_id: int, body: RoleIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_owner_of(org_id, caller)  # only an owner changes roles (incl. transferring ownership)
    if body.role not in ROLE_RANK:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ROLE_RANK)}")
    membership = (
        await db.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this org")
    if body.role == "owner":
        # Owners short-circuit `_tool_allowed` and `_require_local_run`, so an owner machine identity
        # would silently bypass the tool ACL and the local-run gate placed on it.
        target = await db.get(User, user_id)
        if target is not None and _is_machine_email(target.email):
            raise HTTPException(status_code=422, detail="a machine identity cannot be an owner")
    if membership.role == "owner" and body.role != "owner" and await _count_owners(org_id, db) <= 1:
        raise HTTPException(status_code=409, detail="cannot demote the last owner — promote another owner first")
    membership.role = body.role
    await db.commit()
    return {"user_id": user_id, "role": body.role, "org_id": org_id}


@app.post("/orgs/{org_id}/leave")
async def leave_org(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    if caller.org_id != org_id:  # token is org-scoped: you leave the org whose token you present
        raise HTTPException(status_code=403, detail="use this org's token to leave it")
    if caller.role == "owner" and await _count_owners(org_id, db) <= 1:
        raise HTTPException(status_code=409, detail="you are the last owner — transfer ownership or delete the org")
    await db.delete(caller.membership)  # revokes the caller's token for this org
    await _drop_member_deny_rules(db, caller.membership.user_id, org_id)  # same sweep as remove_member
    await db.commit()
    return {"left_org": org_id}


@app.delete("/orgs/{org_id}")
async def delete_org(
    org_id: int, confirm: str = Query(..., min_length=1),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a team and everything in it (owner only). `?confirm=<slug>` must match.

    The confirmation is REQUIRED by the API, not just collected by the clients that already ask for
    it. This route is irreversible and sits one path segment above every other org route, so any
    client that normalizes `..` turns `DELETE /orgs/{id}/<anything>/..` into this call — that is how
    `treg org unpin ..` deleted a team during testing. A request arriving without the slug it is
    about to destroy is not a request anyone meant to send."""
    _require_owner_of(org_id, caller)
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    if confirm != org.slug:
        raise HTTPException(status_code=422, detail=(
            f"to delete this team, confirm with its slug: ?confirm={org.slug}"))
    await _cascade_delete_org(org, db)
    await db.commit()
    return {"deleted_org": org_id}


# ---- machine identities: the publishable demo token, and agents ----------------------------
# Both are Users on an UNROUTABLE domain, which is what makes them machines rather than people: no
# login door can ever resolve one (guarded in `_find_or_create_user`) and neither may act as a USER
# (guarded in `require_identity`). Everything else they inherit from Membership for free.
PUBLIC_DEMO_DOMAIN = "public-demo.treg.local"  # unroutable — the public identity can never log in
# NOTE: "agent" here is an IDENTITY — a coding agent / automation that calls treg. It is NOT the
# skill-directory table in `agents.py` (which answers "where does each coding agent keep its skills").
# The words collide, the concepts don't; kept apart deliberately.
AGENT_DOMAIN = "agents.treg.local"  # unroutable — an agent acts only by its token


def _is_agent_email(email: str) -> bool:
    return _norm_email(email).endswith(f"@{AGENT_DOMAIN}")


def _is_machine_email(email: str) -> bool:
    """An identity minted by an admin for a machine — never a person who can sign in."""
    return _is_agent_email(email) or _norm_email(email).endswith(f"@{PUBLIC_DEMO_DOMAIN}")


def _public_demo_email(org: Org) -> str:
    return f"pub-{org.slug}@{PUBLIC_DEMO_DOMAIN}"


def _agent_email(org: Org, name: str) -> str:
    """Org-SCOPED on purpose: two orgs must each be able to own an agent called `deploy` without
    sharing one User row (`User.email` is unique). Sharing would mean a superadmin suspending or
    deleting one tenant's agent silently killed the other tenant's too."""
    return f"agent-{org.slug}-{_slugify(name)}@{AGENT_DOMAIN}"


def _agent_name(org: Org, email: str) -> str:
    """The friendly name back out of the address (the name isn't stored — the address IS the id)."""
    local = email.split("@", 1)[0]
    prefix = f"agent-{org.slug}-"
    return local[len(prefix):] if local.startswith(prefix) else local


@app.post("/orgs/{org_id}/public-token")
async def create_public_token(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Mint (or ROTATE) the org's publishable token: flips the org to `public_demo` and returns a
    viewer-role token bound to a dedicated can't-log-in identity. Safe to print on a web page:
    the lockdown in require_member/require_identity limits it to /call + reads, /call is per-IP
    rate-limited, and calling this endpoint again replaces the token (instant revocation of the
    old one). Owner-only — publishing a credential is an org-level decision."""
    _require_owner_of(org_id, caller)
    org = caller.org
    email = _public_demo_email(org)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, demo=True)  # demo: excluded from stats; the domain can't receive mail
        db.add(user)
        await db.flush()
    token = crypto.new_token()
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
    if membership is None:
        db.add(Membership(user_id=user.id, org_id=org_id, role="viewer", token_hash=crypto.hash_token(token)))
    else:
        membership.token_hash = crypto.hash_token(token)  # rotate: the previous published token dies here
    org.public_demo = True
    await db.commit()
    return {"token": token, "org": org.slug, "role": "viewer", "email": email,
            "rate_limit": f"{PUBLIC_DEMO_RATE_MAX} calls per {PUBLIC_DEMO_RATE_WINDOW_S}s per IP",
            "note": "this token can only call this org's tools and read — safe to publish; POST again to rotate"}


@app.delete("/orgs/{org_id}/public-token")
async def delete_public_token(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Revoke the publishable token and lift the org's public_demo lockdown."""
    _require_owner_of(org_id, caller)
    org = caller.org
    user = (await db.execute(select(User).where(User.email == _public_demo_email(org)))).scalar_one_or_none()
    if user is not None:
        membership = (await db.execute(select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
        if membership is not None:
            await db.delete(membership)
    org.public_demo = False
    await db.commit()
    return {"public_token_revoked": True, "org": org.slug}


# ---- agents: a member identity for a machine caller ----------------------------------------
# An agent is JUST a Membership whose user lives on AGENT_DOMAIN, which is why this needs no new
# table and no migration: it inherits every per-member control already in place — `daily_call_cap`
# (enforced by `_enforce_daily_cap`), `tool_access` (`_require_tool_access`, on the proxy AND both run
# tiers), `local_run_enabled`, and per-identity audit, since `CallRecord.user_email` already stamps
# every call. Giving the agent its own identity is what makes all of that per-agent.
class AgentIn(BaseModel):
    name: str
    role: str = "member"  # never "owner" (see below); "admin" is owner-granted only
    daily_call_cap: int = -1  # -1 = unlimited, mirroring set_member_cap
    tool_access: list[str] | None = None  # None = every tool, mirroring set_member_access
    project_access: list | None = None  # None = every project; slugs or ids, mirroring set_member_access
    local_run_enabled: bool = True
    # Set by the dashboard's "Scope this agent" promotion: the observed (member, runtime) pair this
    # agent replaces, so the detected roster can drop it while the agent lives.
    promoted_member: str | None = None
    promoted_client: str | None = None
    # Pin this token to one tag value — {"customer": "cust_A"} — for a token that will run on
    # that customer's own machine. The pin then WINS over whatever header the holder sends.
    pinned_tags: dict | None = None


@app.post("/orgs/{org_id}/agents")
async def create_agent(
    org_id: int, body: AgentIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint (or ROTATE) an agent token: a member identity for a machine caller, with its own cap, tool
    ACL and audit trail. Re-POSTing the same name rotates the token — the previous one dies here, the
    same instant-revocation idiom as the public token. Admin+; only an owner may mint an admin agent.

    On a ROTATE, a field the caller did not send is LEFT AS IT IS — a rotate replaces the token, never
    the agent's limits. Reading an absent field as its default would silently widen a scoped agent to
    every tool (`tool_access=None`), to unlimited calls (`daily_call_cap=-1`) and back to local runs
    (`local_run_enabled=True`) — the dashboard's Rotate button sends only {name, role, cap}, so this
    would fire on the ordinary path. Same shape `set_member_access` already uses for `project_access`."""
    _require_admin_of(org_id, caller)
    name = (body.name or "").strip()
    if not name or not _slugify(name):
        raise HTTPException(status_code=422, detail="name is required")
    if body.role not in ROLE_RANK:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ROLE_RANK)}")
    if body.role == "owner":
        raise HTTPException(status_code=422, detail="an agent cannot be an owner")
    if body.role == "admin" and caller.role != "owner":
        raise HTTPException(status_code=403, detail="only an owner can create an admin agent")
    if body.daily_call_cap < -1:
        raise HTTPException(status_code=422, detail="daily_call_cap must be -1 (unlimited) or >= 0")

    email = _agent_email(caller.org, name)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email)  # NOT demo=True: unlike the public token, agent traffic counts in usage
        db.add(user)
        await db.flush()
    token = crypto.new_token()
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user.id, Membership.org_id == org_id))).scalar_one_or_none()
    # A brand-new agent takes the defaults; a rotate keeps whatever it already had unless told otherwise.
    is_new = membership is None
    sent = body.model_fields_set

    def _keep(field: str, current):
        return getattr(body, field) if (is_new or field in sent) else current

    if is_new:
        membership = Membership(user_id=user.id, org_id=org_id, role=body.role,
                                token_hash=crypto.hash_token(token), created_by=caller.email)
        db.add(membership)
    else:
        membership.token_hash = crypto.hash_token(token)  # rotate: the previous token dies here
        membership.role = _keep("role", membership.role)
    membership.daily_call_cap = _keep("daily_call_cap", membership.daily_call_cap)
    if is_new or "tool_access" in sent:  # only re-validate what the caller actually sent
        membership.tool_access = _normalize_tool_access(
            body.tool_access, await _known_access_names(org_id, db))
    if is_new or "project_access" in sent:
        membership.project_access = await _normalize_project_access(body.project_access, org_id, db)
    if is_new or "promoted_member" in sent or "promoted_client" in sent:
        member = _norm_email(body.promoted_member or "")
        client = _norm_client(body.promoted_client or "")
        membership.promoted_from = f"{member}|{client}" if member and client else ""
    membership.local_run_enabled = _keep("local_run_enabled", membership.local_run_enabled)
    pins = _keep("pinned_tags", membership.pinned_tags)
    if pins:
        if len(pins) > _META_MAX_KEYS:
            raise HTTPException(status_code=422, detail=(
                f"a token may be pinned to at most {_META_MAX_KEYS} tags"))
        # Same validation the header path applies. A pin is written straight into the tag bag by
        # `_parse_call_meta`, so an unvalidated one would reach the idempotency scope and the money
        # rows without ever passing the parser.
        pins = dict(_validate_tag_pair(k, v) for k, v in pins.items())
    membership.pinned_tags = pins or None
    await db.commit()
    return {"token": token, "name": name, "email": email, "org": caller.org.slug, "user_id": user.id,
            "role": membership.role, "daily_call_cap": membership.daily_call_cap,
            "tool_access": membership.tool_access, "project_access": membership.project_access,
            "local_run_enabled": membership.local_run_enabled,
            "pinned_tags": membership.pinned_tags,
            "note": "save this token now — it is shown once; POST the same name again to rotate it"}


@app.get("/orgs/{org_id}/agents")
async def list_agents(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every agent identity in the org, with its limits and today's usage. Never the token."""
    _require_admin_of(org_id, caller)
    memberships = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    users = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_([m.user_id for m in memberships]))
    )).scalars().all()}
    used = await _used_today_by_user(db, org_id)
    agent_emails = [u.email for u in users.values() if _is_agent_email(u.email)]
    # "connected" = the agent has EVER called in as itself (checkin or any real call) — what the
    # token card polls to flip to ✓ the moment the setup instruction's final step runs.
    seen = set((await db.execute(
        select(CallRecord.user_email).where(
            CallRecord.org_id == org_id, CallRecord.user_email.in_(agent_emails)).distinct()
    )).scalars().all()) if agent_emails else set()
    out: list[dict] = []
    for m in memberships:
        user = users.get(m.user_id)
        if user is None or not _is_agent_email(user.email):
            continue
        out.append({"user_id": user.id, "name": _agent_name(caller.org, user.email),
                    "email": user.email, "role": m.role, "daily_call_cap": m.daily_call_cap,
                    "used_today": used.get(user.email, 0), "tool_access": m.tool_access,
                    "project_access": m.project_access,  # the dashboard renders this column
                    "local_run_enabled": m.local_run_enabled, "pinned_tags": m.pinned_tags,
                    "created_at": m.created_at,
                    "created_by": m.created_by, "promoted_from": m.promoted_from,
                    "connected": user.email in seen})
    return out


@app.post("/agents/checkin")
async def agent_checkin(
    request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The handshake at the end of the setup instruction: the agent calls in AS ITSELF, proving the
    token landed in the right environment. Audited synchronously (not fire-and-forget) so the
    dashboard's poll sees `connected` flip the moment this returns. Works for any member token —
    for a human it is just a no-op ping."""
    rec = CallRecord(org_id=caller.org_id, user_email=caller.email, tool_name="—",
                     method="CHECKIN", path="agent connected", status_code=200, kind="checkin",
                     client=_client_of(request))
    db.add(rec)
    await db.commit()
    return {"connected": True, "you": caller.email, "org": caller.org.slug}


@app.get("/orgs/{org_id}/agents/observed")
async def list_observed_agents(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """The agents ALREADY running under members' own tokens, discovered from traffic: one row per
    (member, runtime) seen in the last 30 days, e.g. "sam@… / claude-code". The zero-setup half
    of the agents story — nobody mints anything, the roster fills itself from `CallRecord.client`
    (and RunRecord). Self-reported attribution, not authentication, which is why this view only
    informs; scoping one for real = mint it a token ("Scope this agent" in the dashboard).

    Machine identities are excluded — their calls are already attributed to themselves. So is a
    plain terminal (`client` in ('', 'cli')): a roster that lists every human twice teaches nothing.
    """
    _require_admin_of(org_id, caller)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # A pair that was PROMOTED — it has its own agent identity now — leaves the detected roster;
    # revoking that agent deletes its membership, which resurfaces the pair automatically.
    promoted = {tuple(m.promoted_from.split("|", 1)) for m in (await db.execute(
        select(Membership).where(Membership.org_id == org_id,
                                 Membership.promoted_from != ""))).scalars().all()}
    rows: dict[tuple[str, str], dict] = {}
    for model, email_col, when_col, client_col in (
        (CallRecord, CallRecord.user_email, CallRecord.created_at, CallRecord.client),
        (RunRecord, RunRecord.user_email, RunRecord.created_at, RunRecord.client),
    ):
        # One grouped pass per table: the 30-day totals plus today's slice as a conditional sum,
        # so a second identical GROUP BY isn't needed just to get `used_today`.
        q = (select(email_col, client_col, func.count(), func.max(when_col),
                    func.sum(case((when_col >= today, 1), else_=0)))
             .where(model.org_id == org_id, when_col >= since,
                    client_col.not_in(("", "cli")))
             .group_by(email_col, client_col))
        for email, client, count, last_seen, today_count in (await db.execute(q)).all():
            if _is_machine_email(email) or (email, client) in promoted:
                continue
            cur = rows.setdefault((email, client), {"member": email, "client": client,
                                                    "calls_30d": 0, "used_today": 0,
                                                    "last_seen": last_seen})
            cur["calls_30d"] += count
            cur["used_today"] += today_count
            cur["last_seen"] = max(cur["last_seen"], last_seen)
    return sorted(rows.values(), key=lambda r: (r["member"], r["client"]))


@app.delete("/orgs/{org_id}/agents/{user_id}")
async def revoke_agent(
    org_id: int, user_id: int,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke an agent: delete its membership, which kills its token immediately."""
    _require_admin_of(org_id, caller)
    user = await db.get(User, user_id)
    if user is None or not _is_agent_email(user.email):
        raise HTTPException(status_code=404, detail="unknown agent")
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == user_id, Membership.org_id == org_id))).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    email = user.email  # read before the delete — the row is expired after commit
    await db.delete(membership)
    await _drop_member_deny_rules(db, user_id, org_id)  # a rule aimed at a caller that no longer exists
    await db.flush()
    # The identity is org-scoped, so once its last membership is gone the User row has no purpose.
    if (await db.execute(select(Membership).where(
            Membership.user_id == user_id))).scalars().first() is None:
        await db.delete(user)
    await db.commit()
    return {"revoked": True, "email": email}


# ---- projects: an optional sub-scope inside an org ------------------------------------------
class ProjectIn(BaseModel):
    name: str


def _project_view(p: Project, tool_count: int | None = None) -> dict:
    out = {"id": p.id, "name": p.name, "slug": p.slug, "created_by": p.created_by,
           "created_at": p.created_at}
    if tool_count is not None:
        out["tool_count"] = tool_count
    return out


async def _normalize_project_access(
    refs: list[str | int] | None, org_id: int, db: AsyncSession
) -> list[int] | None:
    """Turn slugs/ids into the stored list of project IDS, mirroring `_normalize_tool_access`:
    validate against the org's own projects (422 on unknown — never silently ignore a typo) and
    **collapse an all-projects selection back to NULL**, so a fully-scoped member keeps
    auto-inheriting projects created later."""
    if refs is None:
        return None
    known = (await db.execute(select(Project).where(Project.org_id == org_id))).scalars().all()
    by_slug = {p.slug: p.id for p in known}
    by_id = {p.id for p in known}
    ids: set[int] = set()
    for ref in refs:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            pid = int(ref)
            if pid not in by_id:
                raise HTTPException(status_code=422, detail=f"unknown project {ref!r} in this team")
            ids.add(pid)
        elif ref in by_slug:
            ids.add(by_slug[ref])
        else:
            raise HTTPException(status_code=422, detail=f"unknown project {ref!r} in this team")
    if known and ids >= by_id:
        return None  # every project selected = unrestricted, so store it as such
    return sorted(ids)


async def _resolve_project(ref: str | int | None, org_id: int, db: AsyncSession) -> Project | None:
    """A project by slug or id, scoped to the org (404 across orgs). None/'' = org-wide."""
    if ref is None or ref == "":
        return None
    q = select(Project).where(Project.org_id == org_id)
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        q = q.where(Project.id == int(ref))
    else:
        q = q.where(Project.slug == _slugify(str(ref)))
    project = (await db.execute(q)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project {ref!r} in this team")
    return project


@app.post("/orgs/{org_id}/projects")
async def create_project(
    org_id: int, body: ProjectIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Create a project — a sub-scope inside the team (admin+). Tools can then be filed under it and
    members scoped to it. Creating one changes nothing on its own: existing tools stay org-wide."""
    _require_admin_of(org_id, caller)
    name = (body.name or "").strip()
    slug = _slugify(name)
    if not name or not slug:
        raise HTTPException(status_code=422, detail="name is required")
    if (await db.execute(select(Project).where(
            Project.org_id == org_id, Project.slug == slug))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"a project {slug!r} already exists in this team")
    project = Project(org_id=org_id, name=name, slug=slug, created_by=caller.email)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_view(project, tool_count=0)


@app.get("/orgs/{org_id}/projects")
async def list_projects(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """The team's projects. A member scoped to some of them sees only those (the ACL hides what it
    gates, matching how `list_tools` behaves)."""
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="use this team's token")
    projects = (await db.execute(select(Project).where(Project.org_id == org_id))).scalars().all()
    access = caller.membership.project_access
    if caller.role != "owner" and access is not None:
        projects = [p for p in projects if p.id in access]
    counts = dict((await db.execute(
        select(Tool.project_id, func.count(Tool.id)).where(Tool.org_id == org_id).group_by(Tool.project_id)
    )).all())
    return [_project_view(p, tool_count=counts.get(p.id, 0)) for p in projects]


@app.delete("/orgs/{org_id}/projects/{project_id}")
async def delete_project(
    org_id: int, project_id: int,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a project. Its tools are NOT deleted — they fall back to org-wide, which is the safe
    direction (a tool that quietly vanished from every listing would be far worse than one that
    briefly becomes visible to the whole team). Members scoped to it lose that entry.

    The freed tools are what keeps a scoped member from being locked out: they were the member's only
    tools and they are now org-wide, so the member keeps exactly what they had. The scope list itself
    must NOT be widened to do that (see below)."""
    _require_admin_of(org_id, caller)
    project = await db.get(Project, project_id)
    if project is None or project.org_id != org_id:  # 404 across orgs — never confirm another org's ids
        raise HTTPException(status_code=404, detail="unknown project")
    freed = 0
    for tool in (await db.execute(select(Tool).where(Tool.project_id == project_id))).scalars().all():
        tool.project_id = None
        freed += 1
    for m in (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all():
        if m.project_access and project_id in m.project_access:
            # Store the remaining ids AS THEY ARE, empty list included. Collapsing `[]` to NULL here
            # would read as "every project" and hand a member scoped to only this project access to
            # every OTHER project's tools — a privilege escalation triggered by an unrelated delete.
            # `[]` is already the right meaning: org-wide tools only, which now include the freed ones.
            m.project_access = [p for p in m.project_access if p != project_id]
    # A rule scoped to this project can never fire again — and DenyRule.project_id is a foreign key,
    # so a surviving row would dangle (Postgres rejects that; SQLite only hides it). Same sweep-on-
    # departure idiom as _drop_member_deny_rules.
    for rule in (await db.execute(select(DenyRule).where(
            DenyRule.project_id == project_id))).scalars().all():
        await db.delete(rule)
    await db.delete(project)
    await db.commit()
    return {"deleted": project_id, "tools_made_org_wide": freed}


# ---- deny rules: org policy over what may be called ----------------------------------------
PROXY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class DenyRuleIn(BaseModel):
    host: str = ""  # a bare netloc or a full URL (we take its host)
    path_prefix: str = ""
    method: str = ""
    user_id: int | None = None  # None = the whole org; set = only that member/agent
    project_id: int | None = None  # None = any tool; set = only calls through that project's tools
    note: str = ""


async def _drop_member_deny_rules(db: AsyncSession, user_id: int, org_id: int | None = None) -> int:
    """Delete the member-scoped rules that named a member/agent who is going away — the caller they
    were written for no longer exists, so the rule can never fire again. Left behind, they show up in
    the Policy table as a row naming a user id the team can no longer see or clean up. Mirrors how
    `delete_project` sweeps the id it deletes out of every `project_access`.

    `org_id` set = that org only (the member left THIS team but may still be in others). `org_id`
    None = every org, for when the USER row itself is deleted — `DenyRule.user_id` is a foreign key,
    so a surviving rule would dangle, which Postgres rejects outright (SQLite does not enforce it by
    default, which is why only a real deployment would have shown this).

    ORG-wide rules (`user_id` NULL) are untouched: they are about the team, not about one caller.
    The caller commits — this only stages the deletes, so it composes with the removal itself."""
    q = select(DenyRule).where(DenyRule.user_id == user_id)
    if org_id is not None:
        q = q.where(DenyRule.org_id == org_id)
    stale = (await db.execute(q)).scalars().all()
    for rule in stale:
        await db.delete(rule)
    return len(stale)


def _deny_view(r: DenyRule) -> dict:
    return {"id": r.id, "host": r.host, "path_prefix": r.path_prefix, "method": r.method,
            "user_id": r.user_id, "scope": "org" if r.user_id is None else "member",
            "project_id": r.project_id,
            "verdict": r.verdict, "note": r.note, "created_by": r.created_by,
            "created_at": r.created_at}


class CapabilityPinIn(BaseModel):
    capability: str
    provider: str


@app.get("/orgs/{org_id}/pins")
async def list_capability_pins(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """The team's provider choices, per capability. Readable by any member — an agent has to know
    what it is allowed to call, and finding out by being refused is a wasted round-trip."""
    if caller.org_id != org_id:      # the token IS the membership (same rule as every org route)
        raise HTTPException(status_code=403, detail="not a member of this org")
    rows = (await db.execute(select(CapabilityPin).where(CapabilityPin.org_id == org_id)
                             .order_by(CapabilityPin.capability))).scalars().all()
    return [{"id": r.id, "capability": r.capability, "provider": r.provider,
             "created_by": r.created_by, "created_at": r.created_at} for r in rows]


@app.post("/orgs/{org_id}/pins")
async def set_capability_pin(
    org_id: int, body: CapabilityPinIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Pin a capability to one provider for the whole team (admin+). Re-pinning replaces.

    Both halves are validated against the catalog: an unknown capability, or a provider that does
    not actually serve it, would be a rule that silently blocks every call to a job the team
    genuinely uses — a typo must fail here, loudly, not at 3am in an agent's log."""
    _require_admin_of(org_id, caller)
    cat = catalog_store.load()
    serving = cat.for_capability(body.capability)
    if not serving:
        raise HTTPException(status_code=422, detail=f"unknown capability {body.capability!r}")
    providers = sorted({e["provider"] for e in serving})
    if body.provider not in providers:
        raise HTTPException(status_code=422, detail=(
            f"{body.provider!r} does not serve {body.capability!r} — "
            f"these do: {', '.join(providers)}"))
    # Read the caller's email NOW, as a plain string. A rollback below expires every ORM instance
    # behind `caller`, and touching one afterwards lazy-loads outside the async context —
    # MissingGreenlet, which is how this first failed under concurrency.
    who = caller.email
    row = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == org_id,
        CapabilityPin.capability == body.capability))).scalars().first()
    if row is None:
        row = CapabilityPin(org_id=org_id, capability=body.capability, provider=body.provider,
                            created_by=who)
        db.add(row)
    else:
        row.provider, row.created_by = body.provider, who
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to another admin (or another web worker). The UNIQUE index is what actually
        # prevents the duplicate; this makes losing look like the sequential path — re-apply onto
        # the winner's row rather than handing back a 500 for a pin that plainly succeeded.
        await db.rollback()
        row = (await db.execute(select(CapabilityPin).where(
            CapabilityPin.org_id == org_id,
            CapabilityPin.capability == body.capability))).scalars().first()
        if row is None:
            raise
        row.provider, row.created_by = body.provider, who
        await db.commit()
    return {"capability": body.capability, "provider": body.provider,
            "alternatives": [p for p in providers if p != body.provider]}


@app.delete("/orgs/{org_id}/pins")
async def clear_capability_pin(
    org_id: int, capability: str = Query(..., min_length=1, max_length=200),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a pin (admin+) — the capability goes back to the caller's choice.

    The capability is a QUERY parameter, not a path segment, and that is a safety decision rather
    than a style one. As `/orgs/{id}/pins/{capability}` it was one keystroke from a catastrophe:
    every normalizing HTTP client (httpx included) rewrites `/orgs/1/pins/..` to `/orgs/1` BEFORE
    sending it — which is DELETE /orgs/{id}, the delete-the-team route. `treg org unpin ..` really
    did destroy an org in testing. Server-side validation cannot defend against it, because the
    rewrite happens in the client; taking the value out of the path removes the class entirely."""
    _require_admin_of(org_id, caller)
    rows = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == org_id, CapabilityPin.capability == capability))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"no pin for {capability!r}")
    for row in rows:      # all of them, in case a duplicate predates the unique index
        await db.delete(row)
    await db.commit()
    return {"capability": capability, "pinned": False}


@app.post("/orgs/{org_id}/deny")
async def create_deny_rule(
    org_id: int, body: DenyRuleIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Block calls to a host / path / method for the whole team, or for one member or agent (admin+).

    Enforced on the proxy AND both run tiers, and it applies to every role including owner — a deny
    rule is a guardrail, not a permission tier."""
    _require_admin_of(org_id, caller)
    host = (body.host or "").strip().lower()
    if "://" in host:  # pasting a base_url is the obvious thing to try, so accept it
        host = urlsplit(host).netloc.lower()
    method = (body.method or "").strip().upper()
    path_prefix = (body.path_prefix or "").strip()
    if method and method not in PROXY_METHODS:
        raise HTTPException(status_code=422, detail=f"method must be one of {list(PROXY_METHODS)}")
    if not (host or path_prefix or method):
        # An all-empty rule matches every request — refuse it rather than silently freezing the org.
        raise HTTPException(status_code=422, detail="give at least one of host, path_prefix or method")
    if body.user_id is not None:
        target = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == body.user_id))).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="not a member of this org")
    if body.project_id is not None:
        project = await db.get(Project, body.project_id)
        if project is None or project.org_id != org_id:  # 404 across orgs, like everywhere else
            raise HTTPException(status_code=404, detail="unknown project")
    rule = DenyRule(org_id=org_id, user_id=body.user_id, project_id=body.project_id,
                    host=host, path_prefix=path_prefix,
                    method=method, note=(body.note or "").strip(), created_by=caller.email)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _deny_view(rule)


@app.get("/orgs/{org_id}/deny")
async def list_deny_rules(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    _require_admin_of(org_id, caller)
    rules = (await db.execute(select(DenyRule).where(DenyRule.org_id == org_id))).scalars().all()
    return [_deny_view(r) for r in rules]


@app.get("/orgs/{org_id}/policy/cli-deny")
async def list_cli_deny(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """READ-ONLY: every CLI tool's effective argv deny patterns, so the Policy screen can show the
    whole "what is blocked" picture in one place. These live on the TOOL (treg.json `cli.deny` +
    catalog defaults, see `localrun.effective_profile`), not in the DenyRule table — editing them
    means editing the skill or the catalog, which is why this endpoint only reports (admin+)."""
    _require_admin_of(org_id, caller)
    from . import providers as prov
    out: list[dict] = []
    tools = (await db.execute(select(Tool).where(Tool.org_id == org_id))).scalars().all()
    for tool in tools:
        profile = localrun.effective_profile(tool, (prov.match_skill(tool.name) or {}).get("cli"))
        if not profile or not profile.get("deny"):
            continue
        own = set(profile.get("_own_deny") or [])
        out.append({"tool": tool.name, "enabled": bool(profile.get("enabled")),
                    "patterns": [{"pattern": p,
                                  "source": "skill" if p in own else "catalog"}
                                 for p in profile["deny"]]})
    return out


@app.delete("/orgs/{org_id}/deny/{rule_id}")
async def delete_deny_rule(
    org_id: int, rule_id: int,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_admin_of(org_id, caller)
    rule = await db.get(DenyRule, rule_id)
    if rule is None or rule.org_id != org_id:  # 404 across orgs — never confirm another org's ids
        raise HTTPException(status_code=404, detail="unknown rule")
    await db.delete(rule)
    await db.commit()
    return {"deleted": rule_id}


# ---- secrets (values are write-only — never returned) -------------------------------------
@app.post("/secrets")
async def create_secret(
    body: SecretIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    await _enforce_sandbox_cap(caller, Secret, demo_sandbox.MAX_SECRETS, "secrets", db)
    await _validate_bundle_id(body.bundle_id, caller.org_id, db)
    secret = Secret(
        org_id=caller.org_id, name=body.name, owner=caller.email, kind=body.kind,
        value=crypto.encrypt(body.value), bundle_id=body.bundle_id,
    )
    db.add(secret)
    await db.commit()
    return _secret_view(secret)


@app.get("/secrets")
async def list_secrets(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()
    visible = await _visible_secret_ids(caller, db)
    if visible is not None:  # tool-restricted member: only the keys wired into their allowed tools
        rows = [s for s in rows if s.id in visible]
    return [_secret_view(s) for s in rows]


@app.patch("/secrets/{secret_id}")
async def update_secret(
    secret_id: int,
    body: SecretUpdate,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="secret not found")
    if not _can_manage(caller, secret):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this secret")
    _require_not_live_demo_secret(caller, secret)
    fields = body.model_dump(exclude_unset=True)
    for k in ("name", "value", "kind"):  # these map to NOT-NULL columns; explicit null is a 422, not a 500
        if k in fields and fields[k] is None:
            raise HTTPException(status_code=422, detail=f"{k} cannot be null")
    # A kind change drives refresh + health + extraction shape; validate a JSON-kind actually has a
    # JSON value (else the tool silently 502s later) and reset the now-meaningless health verdict.
    if "kind" in fields and fields["kind"] != secret.kind:
        if fields["kind"] in ("oauth", "secret_file"):
            raw = fields["value"] if "value" in fields else crypto.decrypt(secret.value)
            try:
                json.loads(raw)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail=f"kind {fields['kind']!r} needs a JSON value")
        secret.health_status, secret.health_detail, secret.health_checked_at = "unknown", "", None
    if "value" in fields:
        fields["value"] = crypto.encrypt(fields["value"])  # re-encrypt on rotate
        # The value is exactly what health measures — a rotation invalidates the prior verdict.
        secret.health_status, secret.health_detail, secret.health_checked_at = "unknown", "", None
    for k, v in fields.items():
        setattr(secret, k, v)
    await db.commit()
    return _secret_view(secret)


@app.delete("/secrets/{secret_id}")
async def delete_secret(
    secret_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="secret not found")
    if not _can_manage(caller, secret):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this secret")
    _require_not_live_demo_secret(caller, secret)
    # bindings live in a JSON column — scan tools IN THIS ORG (registry-scale N is small).
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    if any(b.get("secret_id") == secret_id for t in tools for b in t.bindings):
        raise HTTPException(status_code=409, detail="secret is referenced by a tool binding")
    # a secret used only by a local-run inject (not an HTTP binding) would otherwise be silently
    # deletable, breaking `treg run` — guard those references too.
    if any((e.get("secret_id") == secret_id) for t in tools for e in ((t.cli or {}).get("inject") or [])):
        raise HTTPException(status_code=409, detail="secret is referenced by a tool's local-run (cli) profile")
    await db.delete(secret)
    await db.commit()
    return {"deleted": secret_id}


def _require_not_live_demo_tool(caller: Caller, tool: Tool) -> None:
    """The sandbox's seeded live-wire tool (`stripe`, pinned base) is the demo's centerpiece —
    editing or removing it would break the visitor's own live pane, so refuse. Only the seeded
    name is frozen; visitor-created tools stay fully editable. No-op outside sandboxes / with
    the wire off."""
    if (demo_sandbox.is_sandbox(caller.org) and get_settings().demo_stripe_key
            and tool.name == "stripe" and demo_sandbox.is_live_tool(tool)):
        raise HTTPException(status_code=403, detail=(
            "the live stripe demo endpoint is part of the sandbox — add your own endpoints instead"))


def _require_not_live_demo_secret(caller: Caller, secret: Secret) -> None:
    """Companion guard for the seeded STRIPE_KEY the live tool is bound to."""
    if (demo_sandbox.is_sandbox(caller.org) and get_settings().demo_stripe_key
            and secret.name == "STRIPE_KEY"):
        raise HTTPException(status_code=403, detail=(
            "STRIPE_KEY powers the live stripe demo — add your own keys instead"))


# ---- tools --------------------------------------------------------------------------------
def _require_public_base_url(base_url: str) -> None:
    """A tool's base_url is fetched server-side by the proxy — reject internal / loopback / cloud-metadata
    targets so a member can't turn `treg call` into an SSRF (e.g. base_url=169.254.169.254). Reuses the
    same block-list the webhook path already uses. DNS names are allowed (best-effort)."""
    if not health.safe_webhook_url(base_url):
        raise HTTPException(status_code=422, detail=(
            "base_url must be a public http(s) address — loopback, private, link-local, and cloud-"
            "metadata hosts are refused"))


async def _validate_bundle_id(bundle_id: int | None, org_id: int, db: AsyncSession) -> None:
    """A resource may only attach to a bundle in its OWN org — else it'd be counted by, rendered in,
    and swept up by a foreign org's bundle view/delete (org-scoping leak)."""
    if bundle_id is None:
        return
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != org_id:
        raise HTTPException(status_code=422, detail=f"bundle_id {bundle_id} not found in this org")


async def _require_secret_ownership(secret: Secret, caller: Caller) -> None:
    """A member may bind/inject only a secret they OWN; admins/owners may use any team secret (they set
    up shared tools). Without this, a member could attach a teammate's key to a tool they control and
    exfiltrate it — via the proxy (an attacker `base_url`) or `/grant` on a local-run tool."""
    if not (secret.owner == caller.email or _role_at_least(caller.role, "admin")):
        raise HTTPException(
            status_code=403,
            detail=f"you can only bind a secret you own — secret {secret.id} belongs to another member "
                   "(ask an org admin to wire up a shared-key tool)")


async def _validate_bindings(bindings: list[dict], caller: Caller, db: AsyncSession,
                             grandfather: frozenset = frozenset()) -> None:
    org_id = caller.org_id
    for b in bindings:
        # A `platform_setting` binding injects one of TREG's own credentials (a tier-4 provider key, the
        # Google Ads developer token) — relay resolves it from settings and never looks at secret_id, so
        # a caller-supplied one would be a straight read of our key through any tool they register.
        # Only the server builds these (_provider_bindings / _platform_bindings); user input never may.
        if b.get("platform_setting"):
            raise HTTPException(status_code=422, detail=(
                "a binding may not name a platform_setting — treg's own credentials are server-managed "
                "(they are attached by `connections connect`, or injected by the marketplace ladder)"))
        injector = b.get("injector", "env")
        if injector not in injectors.INJECTORS:  # unknown injector 500s the proxy at call time — reject now
            raise HTTPException(status_code=422, detail=f"unknown injector {injector!r}")
        fmt = b.get("format", "{secret}")  # rendered as fmt.format(secret=…) on the hot path
        if not isinstance(fmt, str):
            raise HTTPException(status_code=422, detail="binding format must be a string")
        try:
            fmt.format(secret="x")  # an unexpected placeholder / literal brace would KeyError/ValueError → 500
        except (KeyError, IndexError, ValueError):
            raise HTTPException(status_code=422, detail=f"invalid binding format {fmt!r} — use only {{secret}}")
        # name/secret_field, if present, feed httpx header/param setters and the JSON extractor —
        # a null or non-string there AttributeErrors on the hot path; location must be header|query.
        for key in ("name", "secret_field"):
            if key in b and not (isinstance(b[key], str) and b[key]):
                raise HTTPException(status_code=422, detail=f"binding {key} must be a non-empty string")
        loc = b.get("location", "header")
        if loc not in ("header", "query"):
            raise HTTPException(status_code=422, detail="binding location must be 'header' or 'query'")
        sid = b.get("secret_id")
        secret = await db.get(Secret, sid) if sid is not None else None
        if secret is None or secret.org_id != org_id:
            raise HTTPException(status_code=422, detail=f"binding secret_id {sid} not found")
        if sid not in grandfather:  # a binding already on the tool is grandfathered (don't lock the owner out on edit)
            await _require_secret_ownership(secret, caller)  # can't ADD a teammate's secret
    # Two bindings with the same target name silently overwrite each other at call time (the first
    # credential is dropped) — reject the collision at registration, for BOTH query and header
    # (header names are case-insensitive; `httpx.Headers[name]=…` overwrites just like a query param).
    qnames = [b.get("name", "Authorization") for b in bindings if b.get("location", "header") == "query"]
    qdupes = sorted({n for n in qnames if qnames.count(n) > 1})
    if qdupes:
        raise HTTPException(status_code=422, detail=f"duplicate query binding name(s): {qdupes}")
    hnames = [b.get("name", "Authorization").lower() for b in bindings if b.get("location", "header") == "header"]
    hdupes = sorted({n for n in hnames if hnames.count(n) > 1})
    if hdupes:
        raise HTTPException(status_code=422, detail=f"duplicate header binding name(s): {hdupes}")


def _validate_cli_profile(cli: dict | None) -> None:
    """422 (not a write-through) for a malformed local-run profile — a bad deny regex or inject shape
    must fail HERE, never at grant time (localrun.check_deny skips uncompilable legacy patterns)."""
    if cli is None:
        return
    try:
        localrun.validate_cli_profile(cli)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _validate_cli_secrets(cli: dict | None, caller: Caller, db: AsyncSession,
                                grandfather: frozenset = frozenset()) -> None:
    """Ownership check for secrets a cli.inject entry names by secret_id — same rule as bindings, so a
    member can't launder a teammate's secret into a local-run tool and extract it via /grant."""
    if not cli:
        return
    for e in cli.get("inject") or []:
        sid = e.get("secret_id")
        if sid is None:
            continue
        secret = await db.get(Secret, sid)
        if secret is None or secret.org_id != caller.org_id:
            raise HTTPException(status_code=422, detail=f"cli.inject secret_id {sid} not found")
        if sid not in grandfather:
            await _require_secret_ownership(secret, caller)


def _allowed_server_bins() -> set[str]:
    """The commands `treg run --server` may execute: catalog-known CLIs + an admin allow-list. Blocks a
    member naming `bash`/`python` to run arbitrary code as the server user (docs/CLI-RUN-PLAN.md Option A)."""
    from . import providers as prov
    bins = {(e.get("cli") or {}).get("bin") for e in prov.CATALOG}
    bins.discard(None)
    extra = get_settings().run_allowed_bins
    bins |= {b.strip() for b in extra.split(",") if b.strip()}
    return bins  # type: ignore[return-value]


@app.post("/tools")
async def create_tool(
    body: ToolIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    await _enforce_sandbox_cap(caller, Tool, demo_sandbox.MAX_TOOLS, "endpoints", db)
    if body.bindings is not None:
        bindings = body.bindings
    elif body.secret_id is not None:
        bindings = [_flat_binding(body)]
    else:
        bindings = []  # a public upstream needing no credential is allowed
    _require_public_base_url(body.base_url)  # no SSRF to internal/metadata hosts via the proxy
    await _validate_bindings(bindings, caller, db)
    await _validate_bundle_id(body.bundle_id, caller.org_id, db)
    _validate_cli_profile(body.cli)
    await _validate_cli_secrets(body.cli, caller, db)
    project = await _resolve_project(body.project, caller.org_id, db)
    tool = Tool(
        org_id=caller.org_id, name=body.name, owner=caller.email, base_url=body.base_url,
        host=_host_of(body.base_url), bindings=bindings, health_check=body.health_check,
        examples=body.examples or [], cli=body.cli, bundle_id=body.bundle_id,
        project_id=project.id if project else None,
    )
    db.add(tool)
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"tool name {body.name!r} already exists in this org")
    return _tool_view(tool)


@app.get("/tools")
async def list_tools(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    # The per-member tool ACL hides what it gates: a restricted member's listing shows only their tools.
    return [_tool_view(t) for t in rows if _tool_usable(caller, t)]


@app.get("/tools/by-name/{name}")
async def get_tool_by_name(
    name: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Name-keyed lookup so shareable detail URLs (/app/tools/<name>) resolve without an id."""
    tool = (await db.execute(
        select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name)
    )).scalars().first()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_use(caller, tool)  # a 403 names the fix (ask an admin) — clearer than a fake 404
    return _tool_view(tool)


@app.patch("/tools/{tool_id}")
async def update_tool(
    tool_id: int,
    body: ToolUpdate,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="tool not found")
    if not _can_manage(caller, tool):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this tool")
    _require_not_live_demo_tool(caller, tool)
    fields = body.model_dump(exclude_unset=True)
    if "base_url" in fields and fields["base_url"] is None:  # NOT-NULL column + feeds _host_of — 422, not 500
        raise HTTPException(status_code=422, detail="base_url cannot be null")
    if fields.get("base_url"):
        _require_public_base_url(fields["base_url"])  # no SSRF to internal/metadata hosts
    # Secrets ALREADY on the tool are grandfathered on edit — only a NEWLY-added binding/inject must be
    # owned by the caller. Otherwise re-saving a tool an admin wired with a shared key locks its owner out.
    grandfather = frozenset(
        {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}
        | {e.get("secret_id") for e in ((tool.cli or {}).get("inject") or []) if e.get("secret_id") is not None}
    )
    if "bindings" in fields:
        await _validate_bindings(fields["bindings"], caller, db, grandfather)
    if "cli" in fields:  # explicit null clears the profile (turns local runs off entirely)
        _validate_cli_profile(fields["cli"])
        await _validate_cli_secrets(fields["cli"], caller, db, grandfather)
    if "project" in fields:  # slug/id in, column out; explicit null = back to org-wide
        project = await _resolve_project(fields.pop("project"), caller.org_id, db)
        tool.project_id = project.id if project else None
    for k, v in fields.items():
        setattr(tool, k, v)
    if "base_url" in fields:
        tool.host = _host_of(tool.base_url)  # keep the resolution index in sync
    await db.commit()
    return _tool_view(tool)


@app.delete("/tools/{tool_id}")
async def delete_tool(
    tool_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="tool not found")
    if not _can_manage(caller, tool):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this tool")
    _require_not_live_demo_tool(caller, tool)
    await db.delete(tool)
    await db.commit()
    return {"deleted": tool_id}


# ---- local runs (`treg run`): grant + outcome report (docs/CLI-RUN-PLAN.md) -----------------
# Redact obvious credentials a user might type INLINE (`treg run x -- --token sk_live_…`) before the
# argv is persisted to the audit log — known key prefixes, any high-entropy token, JWTs, AND the value
# that follows a credential-looking flag (so a SHORT password like `--password hunter2` is masked too).
_ARGV_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    # JWT (base64url with dots). `\b` and the POSSESSIVE `++` are load-bearing, not tidying: without
    # the anchor, input like "eyJeyJeyJ…" offers a fresh start position every three characters and
    # each one scans forward for a `.`, which is quadratic. Anchoring leaves one start; `++` removes
    # backtracking within an attempt (it cannot change what matches here — the class excludes `.`,
    # so the run always ends at the first one). Same shape guards the argv rule below.
    r"|\beyJ[A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]{8,}"
    r"|\b[A-Za-z0-9_\-]{24,}\b")  # any 24+ high-entropy run — deliberately over-masks (git SHAs, UUIDs)
                                  # since in an audit log a false mask is harmless but a real key isn't
_CRED_FLAG = r"--?(?:token|password|passwd|pass|pwd|api[-_]?key|secret|auth|bearer|credential)s?"
_CRED_FLAG_EQ_RE = re.compile(rf"({_CRED_FLAG})=\S+", re.I)
_CRED_FLAG_BARE_RE = re.compile(rf"^{_CRED_FLAG}$", re.I)


def _redact_argv_list(argv: list[str]) -> list[str]:
    """Per-element redaction that also masks the element FOLLOWING a bare credential flag."""
    out: list[str] = []
    mask_next = False
    for a in argv:
        if mask_next:
            out.append("***"); mask_next = False; continue
        if _CRED_FLAG_BARE_RE.match(a):          # `--password` `hunter2` → mask the value that follows
            out.append(a); mask_next = True; continue
        a = _CRED_FLAG_EQ_RE.sub(r"\1=***", a)   # `--password=hunter2`
        out.append(_ARGV_SECRET_RE.sub("***", a))
    return out


def _redact_argv(argv: list[str]) -> str:
    return " ".join(_redact_argv_list(argv))[:500]


def _norm_client(raw: str) -> str:
    """A runtime name as a short slug. One spelling for both ends of the roster: what an incoming
    header is stored as, and what `promoted_from` must match to hide a detected pair."""
    return re.sub(r"[^a-z0-9-]", "",
                  raw.strip().lower().split("/", 1)[0])[:32]  # "claude-code/1.2" → "claude-code"


def _client_of(request: Request | None) -> str:
    """The calling RUNTIME from X-Treg-Client — attribution, not authentication (anything holding
    the token can claim any name, so this informs the roster and never gates anything). An
    unknown-but-well-formed name is kept, so a new runtime shows up without a release."""
    return _norm_client(request.headers.get("X-Treg-Client", "") if request is not None else "")


async def _grant_audit(db: AsyncSession, caller: Caller, tool_name: str, method: str, path: str,
                       status: int, client: str = "") -> int:
    """A SYNCHRONOUS audit row (unlike record_call): the grant returns its audit id so the
    run-report can prove it follows a real grant. One insert; this is not the hot proxy path."""
    rec = CallRecord(org_id=caller.org_id, user_email=caller.email, tool_name=tool_name,
                     method=method, path=path[:500], status_code=status, kind="local_run",
                     client=client)
    db.add(rec)
    await db.commit()
    return rec.id


@app.post("/tools/{name}/grant")
async def grant_local_run(
    name: str,
    body: GrantIn,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint the process material for ONE local run of this tool's CLI: the audited, owner-opt-in
    exception to "values are never returned". OAuth secrets release only the expiring leaf; the
    deny check happens here, where the secret lives. Unlike /call (which injects server-side and
    leaks nothing), a grant HANDS the credential value to the caller's machine — so it needs member+
    (a viewer may call but not extract). Loosening this to a per-tool run ACL is a future policy knob."""
    _require_can_register(caller)
    from . import providers as prov
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_use(caller, tool)  # per-member tool + project ACL (call + both run tiers)
    _require_local_run(caller)               # local tier may be disabled for this member (server-only)
    await _enforce_deny(caller, tool.base_url, "", db, tool.project_id)  # host-level policy (see run_tool_server)
    await _enforce_daily_cap(caller, db)  # a local run counts toward the per-user daily cap
    catalog_cli = (prov.match_skill(tool.name) or {}).get("cli")
    profile = localrun.effective_profile(tool, catalog_cli)
    if profile is None:
        raise HTTPException(status_code=409, detail=(
            f"treg doesn't know how to inject credentials into {tool.name!r}. Add a \"cli\" block to the "
            'skill\'s treg.json — template: {"cli": {"bin": "' + tool.name + '", '
            '"inject": [{"secret": "<local secret name>", "via": "env", "name": "<ENV_VAR>"}]}}'))
    if profile.get("unsupported"):
        raise HTTPException(status_code=409, detail=f"{tool.name}: {profile.get('reason', 'this CLI cannot be injected')}")
    if not profile.get("enabled"):
        raise HTTPException(status_code=403, detail=(
            f"local runs are disabled for {tool.name!r} — an owner/admin can enable them: "
            f"treg tool update {tool.name} --local-run on"))
    denied = localrun.check_deny(profile, body.argv)
    if denied:
        pattern, source = denied
        await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403, _client_of(request))
        raise HTTPException(status_code=403, detail=(
            f"denied by {source}: pattern {pattern!r}. The skill's creator controls this list "
            "(cli.deny in treg.json)."))
    # Runner-proof gate (Bug 1). Handing a member a secret they do NOT own — a shared-key tool they may
    # RUN but not SEE — is allowed only for the isolated treg-run runner, which proves itself with a
    # value the member can't read (`X-Treg-Run-Proof`). A direct member call has no proof, so the raw
    # value never reaches the member's eyes. Owned secrets (or an admin) skip this — you can read a key
    # you already hold.
    inject_sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
    needs_proof = False
    if not _role_at_least(caller.role, "admin"):
        for sid in (s for s in inject_sids if s is not None):
            sec = await db.get(Secret, sid)
            if sec is not None and sec.owner != caller.email:
                needs_proof = True
                break
    if needs_proof:
        proof = get_settings().run_proof
        supplied = request.headers.get("X-Treg-Run-Proof", "")
        if not (proof and hmac.compare_digest(supplied, proof)):
            await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403, _client_of(request))
            raise HTTPException(status_code=403, detail=(
                "this tool uses another member's key — running it needs the isolated treg-run runner "
                "(an admin sets it up once: `sudo treg setup-local-run --run-proof …`). A direct grant "
                "can't expose someone else's key value to you."))
    try:
        rendered = await localrun.render_grant(tool, profile, db, request.app.state.http)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — a failed oauth refresh must read clearly, like /call
        raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
    audit_id = await _grant_audit(db, caller, tool.name, "GRANT", _redact_argv(body.argv), 200, _client_of(request))
    warnings = list(profile.get("warnings") or [])
    ttl = rendered["ttl_seconds"]
    if ttl is not None and ttl <= 0:
        warnings.append("the injected token appears already expired — the run will likely fail; "
                        "an owner may need to reconnect it (treg oauth connect)")
    elif ttl is not None:
        warnings.append(f"the injected token expires in ~{max(1, ttl // 60)} min — "
                        "long-running commands may outlive it")
    return {
        "bin": profile.get("bin", tool.name),
        "inject": rendered["items"],  # delivery-tagged items — the client applies each (env/argv/broker)
        "ttl_seconds": rendered["ttl_seconds"],
        "install": profile.get("install"),
        "noninteractive": profile.get("noninteractive") or [],
        "warnings": warnings,
        "errors": profile.get("errors") or [],
        # Scrub the injected value from the CLI's output when the member doesn't OWN the key (a shared
        # key run through the isolated runner) — so a CLI feature (`gh auth token`, an env dump) can't be
        # used to print it back. Owned/admin runs skip it (you may see your own key) and keep a raw TTY.
        "redact_output": needs_proof,
        "audit_id": audit_id,
    }


@app.post("/tools/{name}/run-report")
async def report_local_run(
    name: str,
    body: RunReportIn,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The client's post-run verdict (it matches stderr against the grant's error patterns LOCALLY
    and sends only this enum — raw output never leaves the machine). credential_invalid flips the
    granted secrets to invalid via the same health fields the runner uses."""
    _require_can_register(caller)  # marking a credential invalid is a register-tier action, not a read
    if body.verdict not in localrun.VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {localrun.VERDICTS}")
    grant_rec = await db.get(CallRecord, body.audit_id)
    # Bind the report to the SAME user who received the grant — otherwise a member could invalidate
    # another user's secrets (a DoS) by guessing a sequential audit_id.
    if (grant_rec is None or grant_rec.org_id != caller.org_id or grant_rec.method != "GRANT"
            or grant_rec.tool_name != name or grant_rec.user_email != caller.email):
        raise HTTPException(status_code=404, detail="no matching grant for that audit_id")
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    marked: list[str] = []
    if body.verdict == "credential_invalid":
        from . import providers as prov
        profile = localrun.effective_profile(tool, (prov.match_skill(tool.name) or {}).get("cli")) or {}
        # Mark only the credentials this run actually INJECTED (the ones the CLI used) — not every HTTP
        # binding — and never a `param` (it's config, not a credential; mirrors health.run_all's guard).
        sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
        now = datetime.now(timezone.utc)
        for sid in [s for s in sids if s is not None]:
            secret = await db.get(Secret, sid)
            if secret is not None and secret.org_id == caller.org_id and secret.kind != "param":
                secret.health_status = "invalid"
                secret.health_detail = f"local run of {tool.name} reported an auth failure (exit {body.exit_code})"
                secret.health_checked_at = now
                marked.append(secret.name)
    await _grant_audit(db, caller, tool.name, "REPORT", f"exit={body.exit_code} verdict={body.verdict}", 200)
    return {"ok": True, "marked_invalid": marked}


# ---- skills (bundle composer): register a whole skill atomically --------------------------
@app.post("/skills")
async def register_skill(
    body: SkillIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Register a skill from a raw payload (recipe + secrets + tools). The dashboard's folder importer
    and the CLI build this same payload; the shared core is `_register_skill_bundle`."""
    return await _register_skill_bundle(body, caller, db)


_SECRET_DIR_RE = re.compile(r"(^|/)\.secrets?(/|$)")


def _sanitize_bundle_files(files: dict) -> dict:
    """Defense-in-depth before persisting companion files (the CLI/dashboard already exclude these):
    drop path-traversal / absolute paths, SKILL.md (that's `recipe`), and anything under a secret dir —
    a secret must NEVER live in the shipped file blob. `skill install` re-checks on the way out too."""
    clean: dict[str, str] = {}
    for raw, content in (files or {}).items():
        p = str(raw).replace("\\", "/")
        if not p or p.startswith("/") or ".." in p.split("/"):   # absolute or traversal → drop
            continue
        if p == "SKILL.md" or _SECRET_DIR_RE.search(p):
            continue
        if not isinstance(content, str):
            continue
        clean[p] = content
    return clean


async def _register_skill_bundle(body: SkillIn, caller: Caller, db: AsyncSession) -> dict:
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):  # a skill import would create unlimited tools/secrets, past the cap
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    names = [s.local_name for s in body.secrets]  # bindings reference secrets by local_name
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:  # a duplicate would silently orphan the first secret (only the last id is kept)
        raise HTTPException(status_code=422, detail=f"duplicate secret local_name(s): {dupes}")
    files = _sanitize_bundle_files(body.files)  # drop unsafe paths / secrets before persisting
    bundle = Bundle(org_id=caller.org_id, name=body.name, owner=caller.email, recipe=body.recipe, files=files)
    db.add(bundle)
    await db.flush()  # assign bundle.id without committing yet

    local_to_id: dict[str, int] = {}
    for s in body.secrets:
        secret = Secret(
            org_id=caller.org_id, name=s.local_name, owner=caller.email, kind=s.kind,
            value=crypto.encrypt(s.value), bundle_id=bundle.id,
        )
        db.add(secret)
        await db.flush()
        local_to_id[s.local_name] = secret.id

    for t in body.tools:
        _require_public_base_url(t.base_url)  # no SSRF to internal/metadata hosts via an imported skill
        resolved: list[dict] = []
        for raw in t.bindings:
            b = dict(raw)
            local = b.pop("secret", None)  # bindings reference secrets by local_name
            if local is not None:
                if local not in local_to_id:
                    raise HTTPException(status_code=422, detail=f"binding references unknown secret {local!r}")
                b["secret_id"] = local_to_id[local]
            resolved.append(b)
        # Same gate as POST /tools: reject unknown injectors / dangling secret_ids here, or the
        # skill door persists a poison tool (missing secret_id → KeyError → 500 on every call).
        await _validate_bindings(resolved, caller, db)
        cli = dict(t.cli) if t.cli else None
        if cli:  # inject entries reference secrets by local_name too — resolve like bindings
            cli["inject"] = [dict(e) for e in cli.get("inject") or []]
            for e in cli["inject"]:
                local = e.pop("secret", None)
                if local is not None:
                    if local not in local_to_id:
                        raise HTTPException(status_code=422, detail=f"cli.inject references unknown secret {local!r}")
                    e["secret_id"] = local_to_id[local]
            _validate_cli_profile(cli)
            await _validate_cli_secrets(cli, caller, db)  # a raw secret_id in the upload must be owned too
        db.add(Tool(
            org_id=caller.org_id, name=t.name, owner=caller.email, base_url=t.base_url,
            host=_host_of(t.base_url), bindings=resolved, health_check=t.health_check,
            examples=t.examples, cli=cli, bundle_id=bundle.id,
        ))

    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="a tool name in this skill already exists in this org")
    return await _bundle_view(bundle.id, db)


# ---- skills: analyze / import an uploaded folder (the dashboard mirror of `treg upload skills`) ----
_SKILL_UPLOAD_MAX_FILES = 600
_SKILL_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # per file
_SKILL_UPLOAD_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # whole upload — cap BEFORE materializing to disk


def _check_upload_size(files: list) -> None:
    """Reject an oversized folder upload early (before writing anything to disk), so a member can't
    exhaust the server with a huge `/skills/analyze|import` body. Per-file cap still applies later."""
    if len(files) > _SKILL_UPLOAD_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"too many files (max {_SKILL_UPLOAD_MAX_FILES})")
    total = sum(len((getattr(f, "content", "") or "").encode("utf-8", "ignore")) for f in files)
    if total > _SKILL_UPLOAD_MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="upload too large (max 20 MB total)")


def _materialize_skill_files(files: list) -> str:
    """Write uploaded skill files into a fresh temp dir so the SAME disk-based scanner the CLI uses
    (skills.scan_skills / _classify) can run on them unchanged. Paths are sanitized against traversal;
    the caller must rmtree the returned dir."""
    root = Path(tempfile.mkdtemp(prefix="treg-skill-")).resolve()
    for f in files[:_SKILL_UPLOAD_MAX_FILES]:
        rel = f.path.replace("\\", "/").lstrip("/")
        dest = (root / rel).resolve()
        if root not in dest.parents:      # a '..' path escaping the temp root — drop it
            continue
        if len(f.content.encode("utf-8", "ignore")) > _SKILL_UPLOAD_MAX_BYTES:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_text(f.content)
        except OSError:
            continue
    return str(root)


def _scan_uploaded_skills(root: str, catalog: list, env_names: set) -> list:
    """Find every skill dir (a dir with a SKILL.md) at any depth under root and classify each with the
    CLI's own `skills._classify` — so the dashboard verdict is identical to `treg upload skills`."""
    from . import skills as sk
    dets = []
    for dirpath, _dirs, filenames in os.walk(root):
        if any(m in filenames for m in ("SKILL.md", "skill.md")):
            dets.append(sk._classify(Path(dirpath), catalog, env_names))
    dets.sort(key=lambda d: d.name)
    return dets


@app.post("/skills/analyze")
async def analyze_skill_folder(
    body: SkillAnalyzeIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Classify an uploaded skill folder WITHOUT registering — the dashboard's verify step. Same
    classifier as `treg upload skills`: recipe-only vs contract vs generated, plus readiness gaps."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    _check_upload_size(body.files)
    from . import providers as prov, convert, skills as sk_mod
    root = _materialize_skill_files(body.files)
    try:
        env_path = Path(root) / ".env"
        env_names = set(prov.var_names(str(env_path))) if env_path.is_file() else set()
        dets = _scan_uploaded_skills(root, prov.CATALOG, env_names)
        existing = {b.name for b in (await db.execute(
            select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()}
        out = []
        for d in dets:
            secs = []
            for s in d.secrets:
                if s.get("file"):
                    secs.append({"name": s["name"], "source": "file", "ref": s["file"],
                                 "present": (Path(d.path) / s["file"]).is_file()})
                elif s.get("env"):
                    secs.append({"name": s["name"], "source": "env", "ref": s["env"],
                                 "present": s["env"] in env_names})
            out.append({"name": d.name, "kind": d.kind, "base_url": d.base_url,
                        "secrets": secs, "gaps": d.gaps, "ready": d.ready,
                        "already": d.name in existing,
                        "cli": sk_mod.cli_preview(d, prov.CATALOG),
                        "recipe_chars": len(convert._read_recipe(Path(d.path)))})
        return {"skills": out}
    finally:
        shutil.rmtree(root, ignore_errors=True)


@app.post("/skills/import")
async def import_skill_folder(
    body: SkillImportIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Register selected skills from an uploaded folder: scan → build the payload (secret VALUES from
    the uploaded files / provided env values) → register each as a bundle. Mirrors `treg upload skills`."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    _check_upload_size(body.files)
    from . import skills as sk, providers as prov
    root = _materialize_skill_files(body.files)
    try:
        env_path = Path(root) / ".env"
        env_names = set(prov.var_names(str(env_path))) if env_path.is_file() else set()
        env_names |= set(body.env_values or {})  # a value the user typed in the dashboard counts as present
        dets = _scan_uploaded_skills(root, prov.CATALOG, env_names)
        want = set(body.select) if body.select else {d.name for d in dets if d.ready}
        chosen = [d for d in dets if d.name in want]
        values: dict[str, str] = {}
        need = sk.env_needs(chosen)
        if need and env_path.is_file():
            values.update(prov.env_values(str(env_path), need))
        values.update(body.env_values or {})
        # Idempotent + crash-proof (like the CLI): skip anything already registered, and never let one
        # skill 500 the whole batch. A name clash on the bundle/tool/secret would otherwise raise an
        # IntegrityError on flush (not on commit, so it escaped the register helper's guard).
        existing_bundles = {b.name for b in (await db.execute(
            select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()}
        existing_tools = {t.name for t in (await db.execute(
            select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()}
        existing_secrets = {s.name for s in (await db.execute(
            select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()}
        from .db import session_maker
        results = []
        for d in chosen:
            if d.gaps:
                results.append({"name": d.name, "ok": False, "error": "; ".join(d.gaps)}); continue
            secret_names = {s["name"] for s in d.secrets}
            if d.name in existing_bundles or d.name in existing_tools or (secret_names & existing_secrets):
                results.append({"name": d.name, "ok": False, "skipped": True, "error": "already registered"}); continue
            try:
                payload = sk.build_payload(d, values)
                # Each skill registers in its OWN session so a failure (bad binding, IntegrityError…)
                # can't poison the shared session for the rest of the batch (greenlet_spawn errors).
                async with session_maker() as sk_db:
                    await _register_skill_bundle(SkillIn(**payload), caller, sk_db)
                existing_bundles.add(d.name); existing_tools.add(d.name); existing_secrets |= secret_names
                results.append({"name": d.name, "ok": True, "kind": d.kind})
            except HTTPException as exc:
                results.append({"name": d.name, "ok": False, "error": str(exc.detail)})
            except Exception:  # noqa: BLE001 -- report per-skill, never 500 the batch
                # A generic message — a raw exception string could echo a fragment of an uploaded secret.
                results.append({"name": d.name, "ok": False, "error": "registration failed"})
        return {"results": results}
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def _bundle_allowed(caller: Caller, bundle: Bundle, db: AsyncSession) -> bool:
    """Skill visibility for a tool-restricted member: the access list may grant a bundle by its own
    name (recipe-only skills) or via any of its tools. Owner / NULL access see everything."""
    if caller.role == "owner" or caller.membership.tool_access is None:
        return True
    access = set(caller.membership.tool_access)
    if bundle.name in access:
        return True
    tools = (await db.execute(select(Tool.name).where(Tool.bundle_id == bundle.id))).all()
    return any(r[0] in access for r in tools)


@app.get("/bundles")
async def list_bundles(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()
    return [{"id": b.id, "name": b.name, "owner": b.owner}
            for b in rows if await _bundle_allowed(caller, b, db)]


@app.get("/bundles/by-name/{name}")
async def get_bundle_by_name(
    name: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Name-keyed lookup so shareable detail URLs (/app/skills/<name>) resolve without an id."""
    bundle = (await db.execute(
        select(Bundle).where(Bundle.org_id == caller.org_id, Bundle.name == name)
    )).scalars().first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not await _bundle_allowed(caller, bundle, db):
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the skill {name!r} in this team — an admin can grant it"))
    return await _bundle_view(bundle.id, db)


@app.get("/bundles/{bundle_id}")
async def get_bundle(
    bundle_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not await _bundle_allowed(caller, bundle, db):  # `treg skill install` uses this route too
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the skill {bundle.name!r} in this team — an admin can grant it"))
    return await _bundle_view(bundle_id, db)


@app.patch("/bundles/{bundle_id}")
async def update_bundle(
    bundle_id: int, body: BundleUpdate,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Edit a bundle's SKILL.md text. Only its creator or an admin may. (Execution config lives on
    the tool's cli profile, not here.)"""
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not _can_manage(caller, bundle):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this recipe")
    fields = body.model_dump(exclude_unset=True)  # exclude_unset so a field left out is untouched
    if fields.get("recipe") is not None:
        bundle.recipe = fields["recipe"]
    await db.commit()
    return await _bundle_view(bundle_id, db)


@app.delete("/bundles/{bundle_id}")
async def delete_bundle(
    bundle_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not _can_manage(caller, bundle):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this bundle")
    bundle_tools = (await db.execute(select(Tool).where(Tool.bundle_id == bundle_id))).scalars().all()
    bundle_tool_ids = {t.id for t in bundle_tools}
    bundle_secrets = (await db.execute(select(Secret).where(Secret.bundle_id == bundle_id))).scalars().all()
    # A bundle secret may be bound by a tool OUTSIDE the bundle (use-without-hold). Deleting it would
    # dangle that binding — the same invariant delete_secret guards with a 409, enforced here too.
    org_tools = (await db.execute(select(Tool).where(Tool.org_id == bundle.org_id))).scalars().all()
    outside = [t for t in org_tools if t.id not in bundle_tool_ids]
    # A bundle secret may be referenced by an outside tool's HTTP binding OR its local-run cli.inject —
    # guard BOTH (delete_secret does), else a local-run tool would dangle a missing secret_id.
    referenced = {b.get("secret_id") for t in outside for b in t.bindings}
    referenced |= {e.get("secret_id") for t in outside for e in ((t.cli or {}).get("inject") or [])}
    if any(s.id in referenced for s in bundle_secrets):
        raise HTTPException(status_code=409, detail="a bundle secret is referenced by a tool outside this bundle")
    for t in bundle_tools:
        await db.delete(t)
    for s in bundle_secrets:
        await db.delete(s)
    await db.delete(bundle)
    await db.commit()
    return {"deleted": bundle_id}


# ---- audit read ---------------------------------------------------------------------------
@app.get("/calls")
async def list_calls(
    limit: int = 50, days: int | None = None, before_id: int | None = None,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """This team's recent calls. `days` windows it and `before_id` pages backwards — a builder
    reconciling a month cannot do it through a newest-first limit alone.

    Analytics, NOT an invoice: these rows are written fire-and-forget and the queue sheds them under
    load. Money comes from `/orgs/{id}/usage/by-tag`, which reads the ledger.
    """
    limit = max(1, min(limit, 500))
    # The failure-evidence columns are not in the response below and are not fetched either: they are
    # the two wide columns on this table, this endpoint returns up to 500 rows, and a column nobody
    # reads should not cross the wire. Deferring also means adding them to the payload later has to be
    # a deliberate edit in two places, not an accident in one.
    q = (select(CallRecord)
         .options(defer(CallRecord.error_request), defer(CallRecord.error_response))
         .where(CallRecord.org_id == caller.org_id))
    if days is not None:
        q = q.where(CallRecord.created_at >= _day_start_utc() - timedelta(days=max(1, min(days, 365)) - 1))
    if before_id is not None:
        q = q.where(CallRecord.id < before_id)
    rows = (await db.execute(q.order_by(CallRecord.id.desc()).limit(limit))).scalars().all()
    return [
        {
            "id": c.id,
            "user_email": c.user_email,
            "tool_name": c.tool_name,
            "method": c.method,
            "path": c.path,
            "status_code": c.status_code,
            "kind": c.kind,
            "client": c.client,
            # Marketplace telemetry — all null for a plain tool call (see models.CallRecord). Kept in
            # the same row a caller already reads, so "what did this cost me" needs no second endpoint.
            "endpoint_id": c.endpoint_id,
            "provider": c.provider,
            "credential_tier": c.credential_tier,
            "cost_estimated_micro": c.cost_estimated_micro,
            "cost_observed_micro": c.cost_observed_micro,
            "cost_charged_micro": c.cost_charged_micro,
            "duration_ms": c.duration_ms,
            "response_bytes": c.response_bytes,
            "params_hash": c.params_hash,
            # non-null = treg said no before anything went upstream (see models.CallRecord) — the
            # one field that tells "the provider failed" apart from "we refused" in `treg audit`.
            "refused_by": c.refused_by,
            # The caller's own tags (X-Treg-Meta), for a builder reconciling this row against their
            # records. Money is NOT invoiced from here — see the ledger-backed usage endpoint.
            "call_ref": c.call_ref,
            "budget_dim": c.budget_dim,
            "budget_val": c.budget_val,
            "tags": c.tags,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


@app.get("/calls/{call_ref}")
async def get_call(
    call_ref: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """One call by the `X-Treg-Call-Id` it returned — the join key for a builder's own records.

    Also reports the LEDGER's view of the same reference, because that is the durable one: the audit
    row is fire-and-forget and may have been shed, while the money entries were written synchronously.
    A 404 here therefore means "no audit row", not "this call never happened" — check `ledger` in the
    body before concluding anything about money.
    """
    row = (await db.execute(select(CallRecord).where(
        CallRecord.org_id == caller.org_id, CallRecord.call_ref == call_ref))).scalars().first()
    entries = (await db.execute(select(LedgerEntry).where(
        LedgerEntry.org_id == caller.org_id, LedgerEntry.call_id == call_ref)
        .order_by(LedgerEntry.created_at))).scalars().all()
    if row is None and not entries:
        raise HTTPException(status_code=404, detail="no call with that id")
    view = None
    if row is not None:
        view = {"id": row.id, "call_ref": row.call_ref, "user_email": row.user_email,
                "tool_name": row.tool_name, "method": row.method, "path": row.path,
                "status_code": row.status_code, "kind": row.kind, "client": row.client,
                "endpoint_id": row.endpoint_id, "provider": row.provider,
                "credential_tier": row.credential_tier,
                "cost_estimated_micro": row.cost_estimated_micro,
                "cost_observed_micro": row.cost_observed_micro,
                "cost_charged_micro": row.cost_charged_micro,
                "duration_ms": row.duration_ms, "response_bytes": row.response_bytes,
                "refused_by": row.refused_by, "budget_dim": row.budget_dim,
                "budget_val": row.budget_val, "tags": row.tags,
                "created_at": row.created_at.isoformat() if row.created_at else None}
    return {
        "call": view,
        "ledger": [{"kind": e.kind, "amount_micro": e.amount_micro, "endpoint_id": e.endpoint_id,
                    "created_at": e.created_at.isoformat() if e.created_at else None}
                   for e in entries],
        "charged_micro": sum(-e.amount_micro for e in entries if e.kind == "settle"),
    }


@app.get("/runs")
async def list_runs(
    limit: int = 50, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Audit log for CLI executions (`treg run`, both tiers), scoped to the caller's org — each row
    tagged `where`: "server" (RunRecord) or "local" (a `local_run` GRANT on the member's machine).
    Local successes carry no exit code (only failures report back), so `exit_code` is null for them.
    Ids are prefixed (s/l) so the two sources never collide as list keys."""
    limit = max(1, min(limit, 500))
    server = (await db.execute(
        select(RunRecord).where(RunRecord.org_id == caller.org_id)
        .order_by(RunRecord.id.desc()).limit(limit)
    )).scalars().all()
    # A local run is audited as its GRANT (kind="local_run"); the redacted argv lives in `path`.
    local = (await db.execute(
        select(CallRecord).where(
            CallRecord.org_id == caller.org_id, CallRecord.kind == "local_run",
            CallRecord.method == "GRANT")
        .order_by(CallRecord.id.desc()).limit(limit)
    )).scalars().all()
    rows = [
        {"id": f"s{r.id}", "user_email": r.user_email, "tool": r.bundle_name,  # bundle_name = tool (historical)
         "argv": r.argv, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
         "where": "server", "client": r.client, "created_at": r.created_at.isoformat()}
        for r in server
    ] + [
        {"id": f"l{c.id}", "user_email": c.user_email, "tool": c.tool_name,
         "argv": (c.path or "").split(), "exit_code": None, "duration_ms": None,
         "where": "local", "client": c.client, "created_at": c.created_at.isoformat()}
        for c in local
    ]
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:limit]


# ---- OAuth connect flow (Phase C): mint the first token via browser consent --------------
async def _free_connection_name(base: str, org_id: int, db: AsyncSession) -> str:
    """First connection for a provider keeps the bare service name; later ones get -2, -3.

    The bare name matters: every skill and doc says `treg call google-search-console`, and a tool
    name is unique per org, so the first account must own it or all of that breaks. Suffixing only
    the extras means adding a second account can never change how the first one is called.
    """
    taken = set((await db.execute(
        select(Tool.name).where(Tool.org_id == org_id)
    )).scalars().all()) | set((await db.execute(
        select(Secret.name).where(Secret.org_id == org_id)
    )).scalars().all())
    if base not in taken:
        return base
    return next(f"{base}-{n}" for n in range(2, 1000) if f"{base}-{n}" not in taken)


def _provider_bindings(provider, secret: Secret) -> list[dict]:
    """The binding list that injects `secret` the way this registry provider authenticates.

    A pasted-secret provider's value is a plain string, not an oauth blob — injecting it with
    secret_field="access_token" would try to read a JSON field that isn't there. A key may ride in
    a header (default) or a query param (Semrush's ?key=…). A provider needing a second credential
    that TREG holds (Google Ads' developer token) gets it as a platform binding — read from settings
    at call time, never copied into the org's secrets."""
    if provider.uses_pasted_secret:
        if provider.token_location == "query":
            bindings = [{
                "secret_id": secret.id, "injector": "env", "location": "query",
                "name": provider.token_param, "format": provider.token_format,
            }]
        else:
            bindings = [{
                "secret_id": secret.id, "injector": "env", "location": "header",
                "name": provider.token_header, "format": provider.token_format,
            }]
    else:
        bindings = [{
            "secret_id": secret.id, "injector": "oauth", "location": "header",
            "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token",
        }]
    if provider.needs_extra_credential and provider.extra_credential_is_platform:
        bindings.append({
            "platform_setting": provider.extra_credential_setting, "injector": "env",
            "location": "header", "name": provider.extra_credential_header, "format": "{secret}",
        })
    return bindings


async def _autoprovision_provider_tool(
    provider, secret: Secret, pending: PendingOAuth, db: AsyncSession
) -> None:
    """Bind the freshly-connected credential to the provider's API as a callable tool.

    Named after the CONNECTION, not the provider — a tool name is unique per org, so two accounts
    on one provider need two tools. The first account's connection is named for the service, so it
    still gets the bare `google-search-console` every skill and doc refers to.

    Idempotent by (org, name): reconnecting rebinds the existing tool to the new credential rather
    than piling up duplicates."""
    tool_name = secret.name or provider.service
    existing = (
        await db.execute(
            select(Tool).where(Tool.org_id == secret.org_id, Tool.name == tool_name)
        )
    ).scalars().first()
    bindings = _provider_bindings(provider, secret)
    # A registry tool with a probe can self-validate on `health --run` instead of sitting at
    # "unchecked" until something happens to call it.
    health_check = (
        {"method": "GET", "path": provider.probe_path, "expect_status": 200}
        if provider.probe_path else None
    )
    examples = _provider_tool_examples(provider)
    if existing is not None:
        existing.bindings = bindings
        existing.base_url = provider.base_url
        existing.host = _host_of(provider.base_url)
        # Reconnecting is how an already-provisioned tool picks up a probe — or examples — added
        # to the registry since it was made.
        existing.health_check = health_check or existing.health_check
        if examples and not existing.examples:
            existing.examples = examples
    else:
        db.add(Tool(
            org_id=secret.org_id, name=tool_name, owner=pending.owner,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=bindings, health_check=health_check,
            examples=examples,
        ))
    await _upsert_provider_extra_tools(provider, secret, tool_name, pending.owner, db, bindings)


async def _upsert_provider_extra_tools(
    provider, secret: Secret, tool_name: str, owner: str, db: AsyncSession,
    bindings: list[dict] | None = None,
) -> int:
    """Upsert a split-host provider's companion tools; return the number newly created.

    Connect and startup backfill deliberately share this exact write path. A new provider registry
    `extra_tools` entry therefore heals old connections on their next boot without a one-off migration.
    """
    bindings = bindings or _provider_bindings(provider, secret)
    created = 0
    for extra in getattr(provider, "extra_tools", ()) or ():
        extra_name = f"{tool_name}-{extra['suffix']}"
        extra_probe = (
            {"method": "GET", "path": extra["probe_path"], "expect_status": 200}
            if extra.get("probe_path") else None
        )
        prior = (await db.execute(
            select(Tool).where(Tool.org_id == secret.org_id, Tool.name == extra_name)
        )).scalars().first()
        if prior is not None:
            prior.bindings = bindings
            prior.base_url = extra["base_url"]
            prior.host = _host_of(extra["base_url"])
            prior.health_check = extra_probe or prior.health_check
            if extra.get("examples") and not prior.examples:
                prior.examples = extra["examples"]
        else:
            db.add(Tool(
                org_id=secret.org_id, name=extra_name, owner=owner,
                base_url=extra["base_url"], host=_host_of(extra["base_url"]),
                bindings=bindings, health_check=extra_probe,
                examples=extra.get("examples") or [],
            ))
            created += 1
    return created


async def _backfill_provider_extra_tools() -> int:
    """Heal provider connections created before their registry entry gained companion tools.

    A connection qualifies only when its provider-attributed Secret still has the expected main
    Tool and that Tool is bound to the same Secret. This avoids creating orphan companions for a
    partially-deleted connection while keeping the scan generic across all future `extra_tools`.
    """
    async with session_maker() as db:
        provider_secrets = (await db.execute(
            select(Secret).where(Secret.provider != "")
        )).scalars().all()
        candidates = [
            (secret, provider)
            for secret in provider_secrets
            if (provider := oauth_providers.get(secret.provider)) is not None
            and (getattr(provider, "extra_tools", ()) or ())
        ]
        if not candidates:
            return 0

        org_ids = {secret.org_id for secret, _ in candidates}
        tools = (await db.execute(select(Tool).where(Tool.org_id.in_(org_ids)))).scalars().all()
        by_org_name = {(tool.org_id, tool.name): tool for tool in tools}
        created = 0
        for secret, provider in candidates:
            tool_name = secret.name or provider.service
            main = by_org_name.get((secret.org_id, tool_name))
            if main is None or not any(
                binding.get("secret_id") == secret.id for binding in (main.bindings or [])
            ):
                continue
            created += await _upsert_provider_extra_tools(
                provider, secret, tool_name, main.owner or secret.owner, db)
        await db.commit()
        if created:
            logging.getLogger("treg").info("backfilled %d provider companion tool(s)", created)
        return created


CATALOG_STAMP_CAP = 12  # a tool's examples are read by a human/agent scanning, not a full API doc


def _provider_tool_examples(provider) -> list[dict]:
    """The provisioned tool's `examples`: the registry's hand-written ones first, then the endpoint
    catalog's verified core operations for the same provider.

    This is what makes a fresh connection immediately useful — the agent gets real paths with the
    inputs they need instead of guessing them from the provider's docs and burning paid calls."""
    out = [dict(e) for e in provider.examples]
    seen = {(e.get("method", "").upper(), (e.get("path") or "").lstrip("/")) for e in out}
    for ex in catalog_store.tool_examples(provider.service):
        if len(out) >= CATALOG_STAMP_CAP:
            break
        key = (ex["method"], ex["path"].lstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


@app.get("/oauth/providers")
async def oauth_providers_list() -> list[dict]:
    """Providers treg holds its own approved app for. `configured` is false when this deployment
    hasn't set that provider's client credentials — listed, but its flow can't run here."""
    return oauth_providers.listing()


@app.post("/oauth/start")
async def oauth_start(
    body: OAuthStartIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    name, client_id, client_secret = body.name, body.client_id, body.client_secret
    auth_uri, token_uri, scopes = body.auth_uri, body.token_uri, list(body.scopes)
    code_verifier, auth_params, auth_method = "", "", "client_secret_post"
    cid_param, scope_sep = "client_id", " "
    long_lived = False

    if body.provider:  # REGISTRY mode — treg's own app supplies everything
        provider = oauth_providers.get(body.provider)
        if provider is None:
            known = ", ".join(sorted(oauth_providers.REGISTRY))
            raise HTTPException(status_code=404, detail=f"unknown provider {body.provider!r} (known: {known})")
        capability = body.capability or provider.default_capability
        try:
            scopes = provider.scopes_for(capability)
            client_id, client_secret = oauth_providers.credentials(provider)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        auth_uri, token_uri = provider.auth_uri, provider.token_uri
        name = name or provider.service
        auth_method = provider.token_endpoint_auth_method
        cid_param, scope_sep = provider.client_id_param, provider.scope_separator
        long_lived = provider.long_lived_exchange
        if provider.auth_params is not None:
            auth_params = json.dumps(provider.auth_params)
        if provider.pkce:
            code_verifier = crypto.new_token()
    elif not (client_id and client_secret):
        raise HTTPException(
            status_code=422,
            detail="supply `provider` for a registry connect, or client_id + client_secret to bring your own app",
        )
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    # Reconnecting targets ONE connection. Scoped to the caller's org so a guessed id can't aim a
    # consent at another org's credential, and matched to the provider so a Slack consent can't be
    # made to overwrite a Google one.
    replaces_id = None
    if body.connection_id is not None:
        target = (await db.execute(select(Secret).where(
            Secret.id == body.connection_id, Secret.org_id == caller.org_id
        ))).scalars().first()
        if target is None:
            raise HTTPException(status_code=404, detail="unknown connection")
        if body.provider and target.provider != body.provider:
            raise HTTPException(
                status_code=422,
                detail=f"connection {body.connection_id} is {target.provider or 'not a provider connection'}, not {body.provider}",
            )
        replaces_id = target.id
        name = target.name  # keep the account's name (and therefore its tool binding) stable

    state = crypto.new_token()
    treg_callback = f"{get_settings().public_url.rstrip('/')}/oauth/callback"
    # The code must come back to treg's OWN callback — a body-supplied redirect_uri pointing elsewhere
    # turns this into a consent-phishing URL builder (a legit provider link that routes the code away).
    if body.redirect_uri and body.redirect_uri.rstrip("/") != treg_callback:
        raise HTTPException(status_code=422, detail="redirect_uri must be treg's own /oauth/callback")
    redirect_uri = body.redirect_uri or treg_callback
    pending = PendingOAuth(
        org_id=caller.org_id, state=state, name=name, owner=caller.email,
        client_id=client_id, client_secret=crypto.encrypt(client_secret),
        auth_uri=auth_uri, token_uri=token_uri, scopes=scope_sep.join(scopes),
        redirect_uri=redirect_uri, provider=body.provider or "",
        code_verifier=code_verifier, auth_params=auth_params, token_endpoint_auth_method=auth_method,
        client_id_param=cid_param, scope_separator=scope_sep, long_lived_exchange=long_lived,
        replaces_secret_id=replaces_id,
    )
    db.add(pending)
    await db.commit()
    return {"state": state, "consent_url": oauth.consent_url(pending), "redirect_uri": redirect_uri}


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request, state: str = "", code: str = "", error: str = "",
    db: AsyncSession = Depends(get_session),
):
    # Hit by the BROWSER on redirect — no token; protected by the unguessable `state`.
    pending = (await db.execute(select(PendingOAuth).where(PendingOAuth.state == state))).scalar_one_or_none()
    if pending is None:
        return _auth_page("Connect failed", "Invalid or expired authorization link.", ok=False, status=404)
    if pending.status != "pending":
        # A browser re-load re-hits this URL with a now-spent code; re-exchanging would fail and
        # flip a successful connect's status to "error". Return the terminal result without redoing it.
        if pending.status == "done":
            return _auth_page("Connected", "You can close this tab.")
        return _auth_page("Connect failed", "This authorization already failed. Start the connect again.", ok=False, status=400)
    if _as_naive(pending.created_at) < _utcnow_naive() - timedelta(minutes=health.OAUTH_PENDING_TTL_MIN):
        pending.status, pending.detail = "error", "expired"  # an old state must not stay redeemable
        await db.commit()
        return _auth_page("Connect failed", "This authorization link expired. Start the connect again.", ok=False, status=400)
    if error or not code:
        pending.status, pending.detail = "error", (error or "no authorization code")[:200]
        await db.commit()
        return _auth_page("Connect failed", "Authorization failed. You can close this tab and try again.", ok=False, status=400)
    try:
        blob = await oauth.exchange_code(pending, code, request.app.state.http)
        provider = oauth_providers.get(pending.provider) if pending.provider else None
        # A consent either REPLACES one named connection or ADDS another. `replaces_secret_id` says
        # which, decided back at /oauth/start where the user's intent was known. This used to
        # blanket-replace by provider, which fixed the real bug — widening read→write silently made
        # a second google-search-console row — at the cost of banning a second account entirely.
        secret = None
        if pending.replaces_secret_id is not None:
            secret = (await db.execute(select(Secret).where(
                Secret.id == pending.replaces_secret_id, Secret.org_id == pending.org_id
            ))).scalars().first()
            # Deleted between consent and callback: fall through and add it back rather than 500.
        if secret is None:
            secret = Secret(org_id=pending.org_id,
                            name=await _free_connection_name(pending.name, pending.org_id, db),
                            owner=pending.owner,
                            kind="oauth", value=crypto.encrypt(json.dumps(blob)))
            db.add(secret)
        else:
            secret.value = crypto.encrypt(json.dumps(blob))
            secret.last_error = ""
        secret.provider = pending.provider or ""
        # granted_scopes stays canonically SPACE-joined whatever dialect went over the wire, so the
        # readers (satisfied_capabilities, the health payload) can keep using a plain .split().
        # TikTok comma-joins its consent scopes; without this normalisation a whole grant would
        # come back as one bogus scope string and every capability would read as unsatisfied.
        _sep = pending.scope_separator or " "
        secret.granted_scopes = " ".join(s for s in pending.scopes.split(_sep) if s)
        secret.expires_at = oauth.expiry_of(blob)
        await db.flush()
        # A connect that yields no callable tool is a dead end — the user consented and got
        # nothing. Auto-provision the provider's tool bound to this credential so the very next
        # thing they can do is make a real proxied call.
        if provider and provider.can_autoprovision:
            await _autoprovision_provider_tool(provider, secret, pending, db)
        if provider and provider.has_identity:
            await _record_connected_identity(provider, secret, blob, request.app.state.http)
        pending.status, pending.secret_id, pending.detail = "done", secret.id, "connected"
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth] token exchange failed for state {state}: {exc}")  # detail stays server-side
        pending.status, pending.detail = "error", "token exchange failed"
        await db.commit()
        return _auth_page("Connect failed", "Token exchange failed. You can close this tab and try again.", ok=False, status=502)
    return _auth_page("Connected", "You can close this tab and return to the terminal.")


@app.get("/connections")
async def list_connections(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every OAuth credential in the org, with health AND expiry. Metadata only — no token material."""
    rows = (
        await db.execute(
            select(Secret).where(
                Secret.org_id == caller.org_id,
                # A connection is "something a registry connect produced", NOT "an oauth blob".
                # Bring-your-own-token providers (Slack) store a plain string with kind "env", so a
                # kind=="oauth" filter created them successfully and then hid them from the list.
                or_(Secret.kind == "oauth", Secret.provider != ""),
            )
        )
    ).scalars().all()
    out = []
    for s in rows:
        view = oauth.connection_view(s)
        provider = oauth_providers.get(s.provider) if s.provider else None
        if provider is not None:
            granted = s.granted_scopes.split()
            have = provider.satisfied_capabilities(granted)
            view["capabilities"] = have
            # Providers don't backfill scopes onto an issued grant, so a capability the user never
            # consented to can only be added by re-consenting. Naming the gap here is what turns an
            # opaque upstream 403 into "reconnect to enable write".
            view["missing_capabilities"] = [c for c in provider.capabilities if c not in have]
            if not provider.extra_credential_is_platform:
                view["extra_credential_note"] = provider.extra_credential_note
            view["extra_credential_label"] = provider.extra_credential_label
            # Outstanding only while no tool exists for this provider — once one does, the second
            # credential has been supplied and the connection is callable.
            if provider.needs_extra_credential and not provider.extra_credential_is_platform:
                built = (await db.execute(
                    select(Tool).where(Tool.org_id == caller.org_id, Tool.name == provider.service)
                )).scalars().first()
                view["needs_extra_credential"] = built is None
        out.append(view)
    out.sort(key=lambda c: (c["provider"] or "~", c["name"]))
    return out


async def _owned_connection(secret_id: int, caller: Caller, db: AsyncSession) -> Secret:
    secret = (
        await db.execute(
            select(Secret).where(Secret.id == secret_id, Secret.org_id == caller.org_id)
        )
    ).scalars().first()
    if secret is None or (secret.kind != "oauth" and not secret.provider):
        raise HTTPException(status_code=404, detail="unknown connection")
    return secret


async def _record_connected_identity(provider, secret: Secret, blob: dict, client) -> None:
    """Ask the provider who just connected, and remember it.

    Providers with nothing to choose between (LinkedIn acts as the one member who consented) would
    otherwise show a connection with no indication of WHICH account it is. This also captures the
    id the API actually needs — LinkedIn's person URN — so the agent doesn't re-fetch it on every
    call. Best-effort: a failed lookup must never fail the connect."""
    try:
        resp = await client.get(
            f"{provider.base_url.rstrip('/')}{provider.identity_path}",
            headers={"Authorization": f"Bearer {blob.get('access_token')}"},
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        ident = _dig(data, provider.identity_id_path)
        if not ident:
            return
        secret.resource_ref = provider.identity_ref_format.format(id=ident)
        label = _dig(data, provider.identity_label_path) if provider.identity_label_path else None
        secret.resource_name = str(label) if label else str(ident)
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth] identity lookup failed for {provider.service}: {exc}")


def _dig(obj, dotted: str):
    """Walk a dotted path through dicts and list indices; None if any hop is missing."""
    for part in dotted.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


async def _enrich_resource_labels(provider, resources: list[dict], token: str, client) -> None:
    """Replace id-only labels with the upstream's human name, in place.

    Runs the lookups concurrently — six sequential round-trips to Google would make the picker feel
    broken. A row whose lookup fails keeps its id: a partial list beats an error, since the user may
    not have access to every account the listing returned."""
    async def one(row: dict) -> None:
        bare = str(row["id"]).rsplit("/", 1)[-1]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if provider.needs_extra_credential:
            headers[provider.extra_credential_header] = provider.platform_extra_credential
        if provider.enrich_header_name:
            headers[provider.enrich_header_name] = provider.enrich_header_value.format(id=bare)
        try:
            resp = await client.post(
                f"{provider.discovery_base.rstrip('/')}{provider.enrich_path.format(id=bare)}",
                headers=headers, json=provider.enrich_body or {},
            )
            if resp.status_code == 200:
                label = _dig(resp.json(), provider.enrich_label_path)
                if label:
                    row["label"] = str(label)
        except Exception:  # noqa: BLE001 — a naming lookup must never break the picker
            pass

    await asyncio.gather(*(one(r) for r in resources if r.get("id")))


@app.get("/connections/{secret_id}/resources")
async def connection_resources(
    secret_id: int, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """What this connection can act on — GSC sites, and later GA properties / Ads accounts.

    Live-fetched rather than stored: the answer changes when the user gains or loses access
    upstream, and a stale picker is worse than no picker."""
    secret = await _owned_connection(secret_id, caller, db)
    provider = oauth_providers.get(secret.provider)
    if provider is None or not provider.supports_discovery:
        raise HTTPException(
            status_code=422,
            detail=f"{secret.provider or 'this provider'} has nothing to choose between — it acts on your whole account",
        )
    await oauth.ensure_fresh(secret, db, request.app.state.http)  # no-op for a non-oauth secret
    # A pasted-secret (bot token / API key) secret is a PLAIN STRING, not an oauth blob — json.loads
    # on it throws. (Only header-auth pasted providers reach here; a query-key provider like Semrush
    # has nothing to discover, so supports_discovery is False and this endpoint 422s earlier.)
    raw = crypto.decrypt(secret.value)
    if provider.uses_pasted_secret:
        disc_headers = {provider.token_header: provider.token_format.format(secret=raw)}
    else:
        blob = json.loads(raw)
        token = blob.get("access_token") or blob.get("token")
        disc_headers = {"Authorization": f"Bearer {token}"}
    if provider.needs_extra_credential:  # Ads won't list accounts without the developer token
        disc_headers[provider.extra_credential_header] = provider.platform_extra_credential
    resp = await request.app.state.http.get(
        f"{provider.discovery_base.rstrip('/')}{provider.discover_path}",
        headers=disc_headers,
    )
    body = {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    # Slack answers 200 with {"ok": false, "error": "missing_scope"} — status alone would report an
    # empty picker instead of naming the scope the bot is missing.
    if resp.status_code >= 400 or body.get("ok") is False:
        upstream = ""
        err = body.get("error")
        if isinstance(err, dict):
            upstream = err.get("message", "")
        elif isinstance(err, str):
            upstream = err
        if not upstream:
            upstream = (resp.text or "")[:200]
        raise HTTPException(
            status_code=502,
            detail=f"could not list {provider.resource_plural} ({resp.status_code}): {upstream}".strip(),
        )
    # A successful discovery call is a real authenticated request to the upstream — the strongest
    # evidence we get that this credential works. Recording it turns the connection's health from
    # "unknown" into something earned, instead of waiting for the next health sweep.
    if secret.health_status != "ok":
        secret.health_status, secret.health_detail = "ok", "listed upstream resources"
        secret.health_checked_at = _utcnow_naive()
        await db.commit()
    rows = body.get(provider.discover_key) or []
    if provider.discover_nested_key:  # e.g. GA4 properties nested inside each account summary
        rows = [n for r in rows if isinstance(r, dict) for n in (r.get(provider.discover_nested_key) or [])]
    # Business-owned assets (Meta): a second listing whose rows hold nested lists of
    # primary-shaped rows — an agency member sees [] from /me/accounts yet manages everything
    # through their Business portfolio. Best-effort by design: the primary listing has already
    # answered, and a connection that consented before business_management existed in our scopes
    # gets a clean permission error here, which must read as "no extra assets", not a 502.
    if provider.discover_extra_path:
        try:
            extra = await request.app.state.http.get(
                f"{provider.discovery_base.rstrip('/')}{provider.discover_extra_path}",
                headers=disc_headers,
            )
            if extra.status_code < 400:
                for holder in (extra.json().get(provider.discover_key) or []):
                    for path in provider.discover_extra_list_paths:
                        rows.extend(n for n in (_dig(holder, path) or []) if isinstance(n, dict))
        except Exception:  # noqa: BLE001 — the extra listing must never break the picker
            pass
    label_field = provider.discover_label_field or provider.discover_id_field
    resources = [
        # A row is usually an object, but some providers return bare strings — Google Ads'
        # listAccessibleCustomers gives ["customers/6186675831", …]. Treat the string as both id
        # and label rather than silently dropping every row.
        {"id": r, "label": r.rsplit("/", 1)[-1], "raw": r} if isinstance(r, str)
        # _dig, not .get — YouTube's channel title is nested at snippet.title. A plain key is just
        # a one-hop path, so every existing provider walks the same code.
        else {"id": _dig(r, provider.discover_id_field), "label": _dig(r, label_field), "raw": r}
        for r in rows if isinstance(r, (dict, str))
    ]
    if provider.discover_extra_path:
        # A directly-managed Page is usually ALSO owned by a Business, so the two listings
        # overlap — keep the first sighting (the primary listing's). Id-less rows go too: a
        # Business-owned Page with no linked Instagram account digs to id None, and one None
        # would survive dedup as a phantom picker row.
        seen: set = set()
        resources = [x for x in resources if x["id"] and not (x["id"] in seen or seen.add(x["id"]))]
    if provider.supports_enrichment:
        await _enrich_resource_labels(provider, resources, token, request.app.state.http)
    # Self-heal a connection whose target was chosen before we stored labels (or via the API, which
    # has no label to give). We're already holding the upstream's own naming — resolving it here
    # spares the user a pointless re-pick just to make the row readable.
    if secret.resource_ref and not secret.resource_name:
        match = next((x for x in resources if x["id"] == secret.resource_ref), None)
        if match and match["label"]:
            secret.resource_name = match["label"]
            await db.commit()
    return {
        "provider": provider.service,
        "resource_label": provider.resource_label,
        "resource_plural": provider.resource_plural,
        "selected": secret.resource_ref,
        "resources": resources,
    }


class ResourceRefIn(BaseModel):
    resource_ref: str
    resource_name: str = ""  # the human label, so the UI never has to show "properties/384078430"


@app.post("/connections/{secret_id}/resource")
async def set_connection_resource(
    secret_id: int, body: ResourceRefIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    secret = await _owned_connection(secret_id, caller, db)
    secret.resource_ref = body.resource_ref
    secret.resource_name = body.resource_name
    # Picking a property/site/account is the moment we finally KNOW the id every real call needs —
    # so render it straight into the provisioned tool's examples as a ready-made call. Before this,
    # agents went hunting for the id through the vendor's admin API mid-task (GA4: 13 calls/7 orgs
    # dead-ended there). Re-picking replaces the stamped example rather than piling them up.
    provider = oauth_providers.get(secret.provider) if secret.provider else None
    tmpl = getattr(provider, "resource_example", None) if provider else None
    if tmpl and body.resource_ref:
        rendered = {
            k: v.replace("{resource}", body.resource_ref)
                .replace("{resource_name}", body.resource_name or body.resource_ref)
            if isinstance(v, str) else v
            for k, v in tmpl.items()
        }
        # The marker is what makes re-picking REPLACE: a stamp for property A and one for property B
        # share no path, so path-matching would let them pile up, one stale and confidently wrong.
        rendered["stamped"] = "resource"
        tool = (await db.execute(select(Tool).where(
            Tool.org_id == caller.org_id, Tool.name == (secret.name or provider.service)
        ))).scalars().first()
        if tool is not None:
            others = [e for e in (tool.examples or [])
                      if e.get("stamped") != "resource" and e.get("path") != tmpl["path"]]
            tool.examples = [rendered] + others
    await db.commit()
    await db.refresh(secret)
    return oauth.connection_view(secret)


class TokenConnectIn(BaseModel):
    provider: str
    token: str


@app.post("/connections/token")
async def connect_with_token(
    body: TokenConnectIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Connect a provider the user brings a pasted credential for — a bot token (Slack) or an API
    key (Apollo, TikHub, Semrush, …).

    The credential is VERIFIED against the provider's probe before anything is stored. Saving an
    unverified credential just moves the failure to the first real call, by which point the user
    has left the setup screen and has no idea which of the steps they got wrong."""
    _require_can_register(caller)
    provider = oauth_providers.get(body.provider)
    if provider is None or not provider.uses_pasted_secret:
        raise HTTPException(status_code=422, detail="this provider is connected by consent, not a token")
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail=f"{provider.token_label or 'Token'} is required")
    # HTTP Basic providers (DataForSEO, Moz) take a pasted `login:password`; store the Base64 blob so
    # `Basic {secret}` renders the same at connect and on every proxy call. Both dashboards ALSO hand
    # out a ready-made Base64 credential, and users paste that at least as often as the raw pair —
    # encoding it again produced a double-encoded blob the provider 401'd. So: if the paste already IS
    # Base64 of a printable `login:password`, keep it. A raw pair can never be mistaken for one (":"
    # is not in the Base64 alphabet, so strict decoding refuses it), and a Base64 blob can never be
    # a working raw pair (it has no ":"), so the branch is unambiguous either way.
    if provider.token_encode == "base64":
        import base64
        already = None
        try:
            decoded = base64.b64decode(token, validate=True).decode()
            if ":" in decoded and decoded.isprintable():
                already = token
        except Exception:  # noqa: BLE001 — not Base64, or not text: encode it below
            pass
        token = already or base64.b64encode(token.encode()).decode()

    # The credential rides in a header (default) or a query param (Semrush: ?key=…). The cheapest
    # check may also live on a different host than base_url, so honor an absolute probe_url override,
    # and a POST probe with a JSON body (Serpstat's JSON-RPC limits call).
    rendered = provider.token_format.format(secret=token)
    if provider.token_location == "query":
        headers, params = {}, {provider.token_param: rendered}
    else:
        headers, params = {provider.token_header: rendered}, {}
    probe_url = provider.probe_url or f"{provider.base_url.rstrip('/')}{provider.probe_path}"
    # httpx REPLACES a URL's own query string when `params=` is passed, so a probe_path like
    # `/autocomplete?field=title&text=data` (PDL, Akta, JustOneAPI, SpyFu) silently lost its required
    # params and the probe 400'd — rejecting a perfectly good key. Merge the path's query into params
    # ourselves (params, i.e. the credential for a query provider, wins on a key collision).
    split = urlsplit(probe_url)
    if split.query:
        params = {**dict(parse_qsl(split.query, keep_blank_values=True)), **params}
        probe_url = urlunsplit((split.scheme, split.netloc, split.path, "", split.fragment))
    try:
        resp = await request.app.state.http.request(
            provider.probe_method or "GET", probe_url,
            headers=headers, params=params, json=provider.probe_json,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not reach {provider.display_name}: {exc}") from None
    # Try to parse the body as JSON regardless of the content-type header: ScrapeCreators returns a
    # real JSON body labelled `text/plain`, and gating on `application/json` left its payload empty so
    # `token_verify_field` (creditCount) read as false and a valid key was rejected. The parse is
    # defensive — a genuinely non-JSON key check (Semrush's CSV/number balance) simply throws and
    # leaves payload empty, falling through to the `text_error` branch exactly as before.
    ctype = resp.headers.get("content-type", "")
    payload: dict = {}
    if resp.status_code < 500:
        try:
            parsed = resp.json()
            payload = parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            payload = {}
    # Some providers answer HTTP 200 even for a BAD key and signal validity only in the body: a JSON
    # field (Slack: "ok"; Apollo: "is_logged_in") or an "ERROR ..." text line (Semrush). An HTTP
    # status alone would happily accept a dead key, so check all three signals.
    field_bad = bool(provider.token_verify_field) and not payload.get(provider.token_verify_field)
    field_reject = bool(provider.token_reject_field) and bool(payload.get(provider.token_reject_field))
    equals_bad = bool(provider.token_ok_field) and str(payload.get(provider.token_ok_field)) != provider.token_ok_value
    # Usually any >=400 is a bad key; a provider with no free probe (Coresignal) POSTs an empty body so
    # a VALID key answers 400 — there only 401/403 mean the key itself is bad.
    status_reject = (
        resp.status_code in provider.probe_reject_statuses
        if provider.probe_reject_statuses else resp.status_code >= 400
    )
    text_error = (
        resp.status_code < 400
        and not ctype.startswith("application/json")
        and resp.text.lstrip().upper().startswith("ERROR")
    )
    if status_reject or field_bad or field_reject or equals_bad or text_error:
        why = (
            payload.get("error")
            or (payload.get("ErrorMessage") if equals_bad else None)
            or (f"{provider.token_verify_field}=false" if field_bad else None)
            or (resp.text.strip()[:80] if text_error else f"HTTP {resp.status_code}")
        )
        raise HTTPException(status_code=422, detail=f"{provider.display_name} rejected that token ({why})")

    secret = (await db.execute(
        select(Secret).where(Secret.org_id == caller.org_id, Secret.provider == provider.service)
    )).scalars().first()
    if secret is None:
        secret = Secret(org_id=caller.org_id, name=provider.service, owner=caller.email, kind="env",
                        value=crypto.encrypt(token), provider=provider.service)
        db.add(secret)
    else:
        secret.value = crypto.encrypt(token)
    # A token provider has no consent response to read scopes from, so take them from the probe's
    # response header — otherwise the connection reports "0 scopes" while holding a scoped token.
    if provider.token_scopes_header:
        granted = resp.headers.get(provider.token_scopes_header, "")
        if granted:
            secret.granted_scopes = " ".join(x.strip() for x in granted.split(",") if x.strip())
    secret.last_error = ""
    secret.health_status, secret.health_detail = "ok", "token verified at connect"
    secret.health_checked_at = _utcnow_naive()
    # The probe already told us who this is — no second call needed.
    if provider.has_identity:
        ident = _dig(payload, provider.identity_id_path)
        if ident:
            secret.resource_ref = provider.identity_ref_format.format(id=ident)
            label = _dig(payload, provider.identity_label_path) if provider.identity_label_path else None
            secret.resource_name = str(label) if label else str(ident)
    await db.flush()

    pending = PendingOAuth(org_id=caller.org_id, state="", name=provider.service, owner=caller.email,
                           client_id="", client_secret="", auth_uri="", token_uri="", redirect_uri="")
    await _autoprovision_provider_tool(provider, secret, pending, db)
    await db.commit()
    await db.refresh(secret)
    return oauth.connection_view(secret)


class ExtraCredentialIn(BaseModel):
    value: str


@app.post("/connections/{secret_id}/extra-credential")
async def set_extra_credential(
    secret_id: int, body: ExtraCredentialIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Supply the second credential a provider needs, and finish building its tool.

    Google Ads takes the user's OAuth bearer AND a developer-token header from an approved manager
    account. treg can't invent the latter, so the connect deliberately stops short of a tool. This
    is the other half: store the extra credential and provision the tool with BOTH bindings, so the
    connection goes from "connected but uncallable" to actually usable."""
    _require_can_register(caller)
    secret = await _owned_connection(secret_id, caller, db)
    provider = oauth_providers.get(secret.provider)
    if provider is None or not provider.needs_extra_credential:
        raise HTTPException(status_code=422, detail="this provider needs no extra credential")
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail=f"{provider.extra_credential_label} is required")

    name = f"{provider.service}-{provider.extra_credential_header}"
    extra = (await db.execute(
        select(Secret).where(Secret.org_id == caller.org_id, Secret.name == name)
    )).scalars().first()
    if extra is None:
        extra = Secret(org_id=caller.org_id, name=name, owner=caller.email, kind="env",
                       value=crypto.encrypt(value))
        db.add(extra)
        await db.flush()
    else:  # re-supplying replaces it — the usual reason is a rotated token
        extra.value = crypto.encrypt(value)

    # The primary binding must match how THIS provider authenticates — OAuth bearer for Google Ads,
    # but a pasted-key provider (Tomba's X-Tomba-Key + X-Tomba-Secret pair) injects a plain header.
    # Hardcoding the OAuth shape here gave a key provider a binding that JSON-parses a bare key and
    # fails on every call, so build the primary half with the same helper the connect flow uses.
    bindings = _provider_bindings(provider, secret) + [
        {"secret_id": extra.id, "injector": "env", "location": "header",
         "name": provider.extra_credential_header, "format": "{secret}"},
    ]
    tool = (await db.execute(
        select(Tool).where(Tool.org_id == caller.org_id, Tool.name == provider.service)
    )).scalars().first()
    if tool is None:
        tool = Tool(org_id=caller.org_id, name=provider.service, owner=caller.email,
                    base_url=provider.base_url, host=_host_of(provider.base_url), bindings=bindings)
        db.add(tool)
    else:
        tool.bindings = bindings
    await db.commit()
    await db.refresh(secret)
    return {**oauth.connection_view(secret), "tool": provider.service, "ready": True}


@app.delete("/connections/{secret_id}")
async def revoke_connection(
    secret_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Disconnect, and take the provider's own tool with it.

    A tool left bound to a deleted credential isn't "still configured" — it's broken, and it says so
    only at call time with "a bound secret is missing". We remove the tool treg auto-provisioned for
    this provider (that's treg's creation, not the user's) and drop the dead binding from any tool
    the user built themselves, leaving their other bindings intact."""
    _require_can_register(caller)
    secret = await _owned_connection(secret_id, caller, db)
    provider_service = secret.provider
    removed_tools: list[str] = []

    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    for tool in tools:
        bindings = [b for b in (tool.bindings or []) if b.get("secret_id") != secret_id]
        if len(bindings) == len(tool.bindings or []):
            continue  # this tool never used the credential
        if tool.name == provider_service or not bindings:
            await db.delete(tool)  # treg's own auto-provisioned tool, or nothing left to inject
            removed_tools.append(tool.name)
        else:
            tool.bindings = bindings  # a user-built tool keeps its other credentials

    await db.delete(secret)
    await db.commit()
    return {"deleted": secret_id, "removed_tools": removed_tools}


@app.get("/oauth/status/{state}")
async def oauth_status(
    state: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    pending = (
        await db.execute(
            select(PendingOAuth).where(PendingOAuth.state == state, PendingOAuth.org_id == caller.org_id)
        )
    ).scalar_one_or_none()
    if pending is None:
        raise HTTPException(status_code=404, detail="unknown oauth state")
    return {"status": pending.status, "secret_id": pending.secret_id, "detail": pending.detail, "name": pending.name}


# ---- credential health (Phase B): validate all creds + alert owners ----------------------
@app.post("/health/run")
async def run_health(
    request: Request, all_orgs: bool = False,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    # On-demand + Render-Cron trigger. Refreshes oauth tokens, probes tools, alerts owners.
    # Scoped to the caller's org so a member only ever probes/sees their own org's credentials —
    # EXCEPT a super-admin may pass ?all_orgs=1 to sweep EVERY org (so a single Render Cron token can
    # validate the whole platform, not just its own org).
    if all_orgs:
        if not caller.user.is_superadmin:
            raise HTTPException(status_code=403, detail="all_orgs requires super-admin")
        return await health.run_all(db, request.app.state.http, org_id=None)
    return await health.run_all(db, request.app.state.http, org_id=caller.org_id)


@app.get("/health")
async def get_health(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()
    visible = await _visible_secret_ids(caller, db)
    if visible is not None:  # same visibility rule as /secrets — health mustn't leak hidden keys
        rows = [s for s in rows if s.id in visible]
    # health.needs_reconnect rides along so a credential treg cannot renew announces itself BEFORE
    # it dies. Nothing else surfaces that: it probes green until the moment it stops working.
    return [{**health._view(s), "needs_reconnect": health.needs_reconnect(s)} for s in rows]


# ---- super-admin: cross-tenant read + control (env token OR is_superadmin user) -----------
class BoolIn(BaseModel):
    value: bool = True


def _tally(items) -> dict:
    d: dict[str, int] = {}
    for k in items:
        d[k] = d.get(k, 0) + 1
    return d


@app.get("/admin/stats")
async def admin_stats(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> dict:
    async def n(model) -> int:
        return (await db.execute(select(func.count()).select_from(model))).scalar() or 0

    tools = (await db.execute(select(Tool))).scalars().all()
    secrets = (await db.execute(select(Secret))).scalars().all()
    # THREE COLUMNS, not the whole row. This is an unbounded read of the largest table in the
    # database (tens of thousands of rows), and every one of them was being materialised as a full
    # ORM object to count three fields. It matters more now that `callrecord` carries the failure
    # evidence columns, which are wide and are read by nothing here.
    calls = (await db.execute(
        select(CallRecord.org_id, CallRecord.created_at, CallRecord.status_code))).all()
    users = (await db.execute(select(User))).scalars().all()
    orgs = (await db.execute(select(Org))).scalars().all()
    # Sandbox onboarding data isn't real platform usage — exclude the demo footprint from totals so
    # metrics stay honest (fake teammates, demo teams, and everything scoped to them).
    demo_org_ids = {o.id for o in orgs if o.demo}
    users = [u for u in users if not u.demo]
    orgs = [o for o in orgs if not o.demo]
    tools = [t for t in tools if t.org_id not in demo_org_ids]
    secrets = [s for s in secrets if s.org_id not in demo_org_ids]
    calls = [c for c in calls if c.org_id not in demo_org_ids]
    now = _utcnow_naive()

    def since(rows, days, pred=lambda r: True):
        cut = now - timedelta(days=days)
        return sum(1 for r in rows if _as_naive(r.created_at) >= cut and pred(r))

    ok = sum(1 for c in calls if c.status_code < 400)
    return {
        "totals": {
            "users": len(users), "orgs": len(orgs), "tools": len(tools),
            "secrets": len(secrets), "bundles": await n(Bundle), "calls": len(calls),
            "superadmins": sum(1 for u in users if u.is_superadmin),
            "suspended_orgs": sum(1 for o in orgs if o.suspended),
        },
        "tools_by_injector": _tally(b.get("injector", "?") for t in tools for b in t.bindings),
        "tools_by_host": _tally(t.host for t in tools),
        "credential_health": _tally(s.health_status for s in secrets),
        "calls": {
            "last_7d": since(calls, 7), "last_30d": since(calls, 30), "total": len(calls),
            "success_rate": round(ok / len(calls), 3) if calls else None,
        },
        "growth": {
            "new_users_7d": since(users, 7), "new_users_30d": since(users, 30),
            "new_orgs_7d": since(orgs, 7), "new_orgs_30d": since(orgs, 30),
        },
    }


@app.get("/admin/orgs")
async def admin_orgs(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    orgs = (await db.execute(select(Org))).scalars().all()

    async def _counts(model) -> dict[int, int]:  # one grouped COUNT instead of one-per-org (was O(orgs) queries)
        rows = await db.execute(select(model.org_id, func.count()).group_by(model.org_id))
        return {oid: n for oid, n in rows.all()}

    mems: dict[int, list] = {}
    for m in (await db.execute(select(Membership))).scalars().all():
        mems.setdefault(m.org_id, []).append(m)
    tool_n, secret_n, bundle_n = await _counts(Tool), await _counts(Secret), await _counts(Bundle)
    # Was 1 + 4N serial queries (401 at 100 orgs); now a constant ~4 regardless of tenant count.
    return [
        {
            "id": o.id, "slug": o.slug, "name": o.name, "suspended": o.suspended,
            "members": len(mems.get(o.id, [])), "roles": _tally(m.role for m in mems.get(o.id, [])),
            "tools": tool_n.get(o.id, 0), "secrets": secret_n.get(o.id, 0), "bundles": bundle_n.get(o.id, 0),
            "created_at": o.created_at.isoformat(),
        }
        for o in orgs
    ]


@app.get("/admin/orgs/{org_id}")
async def admin_org_detail(
    org_id: int, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    mem = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalars().all()
    umap = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_([m.user_id for m in mem]))
    )).scalars().all()}  # batched (was one db.get per member)
    members = [{"user_id": m.user_id, "email": umap[m.user_id].email if m.user_id in umap else None, "role": m.role}
               for m in mem]
    tools = (await db.execute(select(Tool).where(Tool.org_id == org_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(Secret.org_id == org_id))).scalars().all()
    recent = (
        await db.execute(select(CallRecord).where(CallRecord.org_id == org_id).order_by(CallRecord.id.desc()).limit(20))
    ).scalars().all()
    return {
        "id": org.id, "slug": org.slug, "name": org.name, "suspended": org.suspended,
        "members": members,
        "tools": [{"id": t.id, "name": t.name, "host": t.host, "owner": t.owner,
                   "injectors": [b.get("injector") for b in t.bindings]} for t in tools],
        "secrets": [{"id": s.id, "name": s.name, "kind": s.kind, "health": s.health_status, "owner": s.owner} for s in secrets],
        "recent_calls": [{"tool": c.tool_name, "method": c.method, "status": c.status_code,
                          "user": c.user_email, "at": c.created_at.isoformat()} for c in recent],
    }


@app.get("/admin/users")
async def admin_users(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    users = (await db.execute(select(User))).scalars().all()
    mems_by_user: dict[int, list] = {}  # all memberships in one query, grouped (was one query per user)
    for m in (await db.execute(select(Membership))).scalars().all():
        mems_by_user.setdefault(m.user_id, []).append(m)
    omap = {o.id: o for o in (await db.execute(  # all referenced orgs in one query (was one db.get per membership)
        select(Org).where(Org.id.in_({m.org_id for ms in mems_by_user.values() for m in ms}))
    )).scalars().all()}
    return [
        {
            "id": u.id, "email": u.email, "is_superadmin": u.is_superadmin, "suspended": u.suspended,
            "orgs": [{"slug": omap[m.org_id].slug if m.org_id in omap else None, "role": m.role}
                     for m in mems_by_user.get(u.id, [])],
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


@app.get("/admin/tools")
async def admin_tools(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    tools = (await db.execute(select(Tool))).scalars().all()
    omap = {o.id: o for o in (await db.execute(  # batched (was one db.get per tool)
        select(Org).where(Org.id.in_({t.org_id for t in tools}))
    )).scalars().all()}
    return [{"id": t.id, "name": t.name, "org": omap[t.org_id].slug if t.org_id in omap else None,
             "host": t.host, "owner": t.owner, "injectors": [b.get("injector") for b in t.bindings]} for t in tools]


@app.get("/admin/calls")
async def admin_calls(
    limit: int = 50, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    limit = max(1, min(limit, 1000))
    rows = (await db.execute(select(CallRecord).order_by(CallRecord.id.desc()).limit(limit))).scalars().all()
    return [{"id": c.id, "org_id": c.org_id, "user": c.user_email, "tool": c.tool_name,
             "method": c.method, "status": c.status_code, "at": c.created_at.isoformat()} for c in rows]


_ERROR_EVIDENCE_TTL_DAYS = 14
_ERROR_EVIDENCE_EXPIRED = "<expired>"


@app.get("/admin/errors")
async def admin_errors(
    days: int = 7, limit: int = 100, provider: str | None = None, status: int | None = None,
    tier: str | None = None,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session),
) -> dict:
    """Failed calls with the evidence to explain them — the caller's request and the provider's own
    answer (see models.CallRecord.error_request).

    Superadmin-only and deliberately not mirrored on `/calls`: the rows hold customers' request
    content, so v1 keeps them behind the same door as every other cross-tenant view.

    Ageing happens HERE rather than on the request path. There is no scheduler in this app by design
    (see the comment above `_claim_idempotent`), and the obvious lazy hook — a marker written on the
    request session — cannot work: `get_session` never commits, so the marker would roll back and the
    purge would then run on every single failed call. Doing it on this route costs one UPDATE to the
    person who came to read errors, which is exactly who wants the stale ones gone.
    """
    purged = await _purge_expired_error_evidence()
    since = _utcnow_naive() - timedelta(days=max(1, min(days, 90)))
    q = (select(CallRecord)
         .where(CallRecord.created_at >= since,
                or_(CallRecord.error_request.is_not(None), CallRecord.error_response.is_not(None)))
         .order_by(CallRecord.id.desc()).limit(max(1, min(limit, 500))))
    if provider:
        q = q.where(CallRecord.provider == provider)
    if status is not None:
        q = q.where(CallRecord.status_code == status)
    if tier is not None:
        q = q.where(CallRecord.credential_tier.is_(None) if tier == ""
                    else CallRecord.credential_tier == tier)
    rows = (await db.execute(q)).scalars().all()
    omap = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_({c.org_id for c in rows if c.org_id is not None})))).scalars().all()}
    return {
        "since": since.isoformat(), "days": days, "retention_days": _ERROR_EVIDENCE_TTL_DAYS,
        "expired_rows_purged": purged,
        "errors": [{
            "id": c.id, "call_ref": c.call_ref, "at": c.created_at.isoformat(),
            "org": omap[c.org_id].slug if c.org_id in omap else None,
            # `method` is already on the row and is the whole diagnosis for a real failure class:
            # 47 of apollo.people.enrich's failures were a GET at a POST endpoint. Omitting a field
            # we already store, in the view built to explain failures, was a free loss.
            "endpoint_id": c.endpoint_id, "provider": c.provider, "tier": c.credential_tier,
            "status": c.status_code,
            "method": c.method, "refused_by": c.refused_by, "duration_ms": c.duration_ms,
            # An aged-out row holds the sentinel, which is a STATE, not content. Returning it as the
            # request/response would have a reader treat the word `<expired>` as the provider's
            # answer; `expired` says the same thing without pretending to be evidence.
            "request": None if c.error_request == _ERROR_EVIDENCE_EXPIRED else c.error_request,
            "response": None if c.error_response == _ERROR_EVIDENCE_EXPIRED else c.error_response,
            "expired": c.error_response == _ERROR_EVIDENCE_EXPIRED,
        } for c in rows],
    }


async def _purge_expired_error_evidence() -> int:
    """Blank the evidence columns past the retention window; returns how many rows were cleared.

    An UPDATE, not a DELETE: `callrecord` is the audit trail and the rest of the row must survive.
    The sentinel rather than NULL keeps "captured, then aged out" distinguishable from "never
    captured" — without it an old failure and a successful call look identical. Runs on its own
    session because the request's session is not committed for us.
    """
    cutoff = _utcnow_naive() - timedelta(days=_ERROR_EVIDENCE_TTL_DAYS)
    try:
        async with session_maker() as db:
            result = await db.execute(
                update(CallRecord)
                # `coalesce`, not a bare `!=`: SQL three-valued logic makes `error_response !=
                # '<expired>'` UNKNOWN when that column is NULL, so a row carrying request-only
                # evidence would never age out — excluded by the very predicate meant only to skip
                # rows already purged.
                .where(CallRecord.created_at < cutoff,
                       or_(CallRecord.error_request.is_not(None),
                           CallRecord.error_response.is_not(None)),
                       or_(func.coalesce(CallRecord.error_request, "") != _ERROR_EVIDENCE_EXPIRED,
                           func.coalesce(CallRecord.error_response, "") != _ERROR_EVIDENCE_EXPIRED))
                .values(error_request=_ERROR_EVIDENCE_EXPIRED,
                        error_response=_ERROR_EVIDENCE_EXPIRED))
            await db.commit()
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 — retention housekeeping must not break the view
        logging.getLogger("treg").warning("error-evidence purge failed: %s", exc)
        return 0


@app.get("/admin/health")
async def admin_health(_: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.health_status != "ok"))).scalars().all()
    omap = {o.id: o for o in (await db.execute(  # batched (was one db.get per secret)
        select(Org).where(Org.id.in_({s.org_id for s in rows}))
    )).scalars().all()}
    out: list[dict] = []
    for s in rows:
        org = omap.get(s.org_id)
        out.append({"secret_id": s.id, "name": s.name, "org": org.slug if org else None,
                    "kind": s.kind, "status": s.health_status, "detail": s.health_detail})
    return out


@app.post("/admin/users/{user_id}/superadmin")
async def admin_set_superadmin(
    user_id: int, body: BoolIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Demoting the last active super-admin locks everyone out of /admin/* (the env token bypasses).
    if not body.value and principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot demote the last active super-admin")
    user.is_superadmin = body.value
    await db.commit()
    return {"user_id": user_id, "is_superadmin": user.is_superadmin}


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(
    user_id: int, body: BoolIn, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if body.value and principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot suspend the last active super-admin")
    user.suspended = body.value
    await db.commit()
    return {"user_id": user_id, "suspended": user.suspended}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int, principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if principal != "env-admin" and await _is_last_active_superadmin(db, user):
        raise HTTPException(status_code=409, detail="cannot delete the last active super-admin")
    mem = (await db.execute(select(Membership).where(Membership.user_id == user_id))).scalars().all()
    affected = {m.org_id for m in mem}
    for m in mem:
        await db.delete(m)
    await db.flush()
    emptied = []
    for oid in affected:
        survivors = (
            await db.execute(select(Membership).where(Membership.org_id == oid).order_by(Membership.id))
        ).scalars().all()
        if not survivors:  # an org left with zero members is dead — cascade it away
            org = await db.get(Org, oid)
            if org is not None:
                await _cascade_delete_org(org, db)
                emptied.append(oid)
        elif not any(m.role == "owner" for m in survivors):
            # Deleting the sole owner would leave an ungovernable org (no one can pass _require_owner_of).
            # Promote the earliest-joined survivor so ownership never evaporates.
            survivors[0].role = "owner"
    # The USER row is about to go, so member-scoped rules must go from EVERY org — `DenyRule.user_id`
    # is a foreign key, and a surviving row would dangle (a hard error on Postgres).
    await _drop_member_deny_rules(db, user_id)
    await db.delete(user)
    await db.commit()
    return {"deleted_user": user_id, "deleted_empty_orgs": emptied}


@app.post("/admin/orgs/{org_id}/suspend")
async def admin_suspend_org(
    org_id: int, body: BoolIn, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    org.suspended = body.value
    await db.commit()
    return {"org_id": org_id, "suspended": org.suspended}


@app.delete("/admin/orgs/{org_id}")
async def admin_delete_org(
    org_id: int, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session)
) -> dict:
    org = await db.get(Org, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="org not found")
    await _cascade_delete_org(org, db)
    await db.commit()
    return {"deleted_org": org_id}


# ---- reconciliation: is the money real? ----------------------------------------------------
# Cross-org aggregates over platform spend, so `require_superadmin` and nothing weaker — an org admin
# may see their own bill (`/orgs/{id}/balance`), never the platform's margin. See reconcile.py.
@app.get("/admin/reconcile/drift")
async def admin_reconcile_drift(
    since_days: int = 30, min_calls: int = 3,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session),
) -> dict:
    """Endpoints whose observed cost has wandered from the catalog's estimate. Only the providers that
    report their own charge in-band appear (see `reconcile.price_drift`)."""
    since = reconcile.window_start(since_days)
    rows = await reconcile.price_drift(db, since, max(1, min_calls))
    return {"since": since.isoformat(), "since_days": since_days, "min_calls": min_calls,
            "tolerance": reconcile.DRIFT_TOLERANCE,
            "flagged": [r for r in rows if r["flagged"]], "endpoints": rows}


@app.get("/admin/reconcile/spend")
async def admin_reconcile_spend(
    since_days: int = 30, _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session),
) -> dict:
    """Settled platform spend per provider — the number to hold next to the provider's own invoice."""
    since = reconcile.window_start(since_days)
    return {"since": since.isoformat(), "since_days": since_days,
            **await reconcile.provider_spend(db, since)}


@app.get("/admin/reconcile/repeats")
async def admin_reconcile_repeats(
    since_days: int = 30, top: int = 10,
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session),
) -> dict:
    """How much of the bill was the same query twice — the cache-worthiness measurement."""
    since = reconcile.window_start(since_days)
    return {"since": since.isoformat(), "since_days": since_days,
            **await reconcile.repeat_rate(db, since, top=top)}


@app.get("/admin/referrals")
async def admin_referrals(
    status: str = "", limit: int = Query(200, ge=1, le=1000),
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_session),
) -> dict:
    """Every referral, across every team — who invited whom, what it cost, and what is still owed.

    Cross-org, so `require_superadmin` and nothing weaker, exactly like the reconcile trio above.
    Read-only: it pays nothing and refuses nothing. `pending_payout_micro` is what the sweep will
    grant once holds elapse, and it is the number that says whether this program is affordable
    before the money leaves — the same job `provider_spend` does for provider bills.

    This is also the report the influencer tier will read when it lands: a cash payout run is this
    list, filtered to partners on a contract, exported.
    """
    s = get_settings()
    q = select(Referral).order_by(Referral.created_at.desc()).limit(limit)
    if status:
        q = q.where(Referral.status == status)
    rows = (await db.execute(q)).scalars().all()

    emails: dict[int, str] = {}
    for uid in {r.referrer_user_id for r in rows} | {r.referred_user_id for r in rows}:
        u = await db.get(User, uid)
        if u is not None:
            emails[uid] = u.email

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    pending = sum(int(s.referral_referrer_micro) + int(s.referral_referred_micro)
                  for r in rows if r.status == "qualified")
    paid = sum(r.referrer_reward_micro + r.referred_reward_micro for r in rows if r.status == "paid")
    return {
        "counts": by_status,
        "paid_micro": paid,
        "pending_payout_micro": pending,
        "referrals": [{
            "id": r.id, "code": r.code, "status": r.status, "reason": r.reject_reason,
            "referrer": emails.get(r.referrer_user_id, ""),
            "referred": emails.get(r.referred_user_id, ""),
            "referred_org_id": r.referred_org_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "qualified_at": r.qualified_at.isoformat() if r.qualified_at else None,
            "paid_at": r.paid_at.isoformat() if r.paid_at else None,
            "paid_micro": r.referrer_reward_micro + r.referred_reward_micro,
        } for r in rows],
    }


# ---- the proxy: call a tool without holding its credential --------------------------------
async def _resolve_call(rest: str, caller: Caller, db: AsyncSession) -> tuple[Tool, str]:
    """Resolve `/call/<rest>` to (tool, full upstream URL), scoped to the caller's org. Shapes:

    - URL-passthrough (agent-facing): rest is the real upstream URL. Resolve the tool by host
      (indexed) + longest base_url prefix — the caller types no treg vocabulary, just the API.
    - Named (CLI/legacy): rest = "<tool-name>/<upstream-path>".

    Both lookups are constrained to `org_id`, so two orgs resolve independently (and may reuse
    a tool name or an upstream host without colliding).

    Passthrough candidates are additionally filtered by the caller's ACL (project scope AND the
    per-tool list) *before* the longest-prefix tiebreak. That ordering matters: a same-host tool the
    caller cannot use must not be able to cause a 409 — or win the tiebreak — for someone who can't
    even see it in `list_tools`. This narrows the candidate set, so it can never grant access: whatever
    resolves still passes `_require_tool_use`. The named shape needs no filter (it resolves one tool).
    """
    org_id = caller.org_id
    norm = _normalize_scheme(rest)
    if norm.startswith("http://") or norm.startswith("https://"):
        try:
            host = urlsplit(norm).netloc.lower()
        except ValueError:  # malformed passthrough URL (e.g. unbalanced IPv6 brackets) → 400, not 500
            raise HTTPException(status_code=400, detail="malformed upstream URL")
        on_host = (await db.execute(
            select(Tool).where(Tool.host == host, Tool.org_id == org_id)
        )).scalars().all()
        candidates = [t for t in on_host if _tool_usable(caller, t)]  # can't use it → can't 409 on it
        # Match on a path-segment boundary, not a raw string prefix: base `.../v2` must NOT match
        # request `.../v20/...` (that would inject v2's credential onto an unregistered sibling path).
        def _prefix_match(base: str) -> bool:
            b = base.rstrip("/")
            return norm == b or norm.startswith(b + "/")

        matches = [t for t in candidates if _prefix_match(t.base_url)]
        if not matches:
            # Tell "no such tool" and "not yours to use" apart. If the ACL filter above is the ONLY
            # reason nothing matched, this is a 403 like the named shape would give — a 404 here
            # would send an admin hunting for a registration that already exists. The message names
            # the HOST the caller already typed, never the internal tool name the ACL hides.
            if any(_prefix_match(t.base_url) for t in on_host):
                raise HTTPException(status_code=403, detail=(
                    f"you don't have access to the registered tool for {host!r} in this team — an "
                    "admin can grant it (dashboard → Team, or `treg org access <you> …`)"))
            raise HTTPException(status_code=404, detail=f"no registered tool for upstream {host!r}")
        # Tiebreak on the NORMALIZED length so `.../v1` and `.../v1/` count equal (a real 409), not
        # one silently "longer" than the other.
        longest = max(len(t.base_url.rstrip("/")) for t in matches)
        top = [t for t in matches if len(t.base_url.rstrip("/")) == longest]
        if len(top) > 1:
            # A hand-registered tool for the same API (often predating the OAuth registry, and
            # frequently holding a stale credential) collides on host with the one connect
            # auto-provisioned. Both are real tools, so neither base_url is "longer" — but they are
            # not equally intended: the registry-provisioned one is the live connection the user
            # just authorised, and URL-passthrough is the AGENT-facing mode, so 409-ing here breaks
            # exactly the callers who never typed a tool name. Prefer the provider-backed tool.
            provider_owned = []
            for t in top:
                sids = {b.get("secret_id") for b in (t.bindings or []) if b.get("secret_id") is not None}
                for sid in sids:
                    s = await db.get(Secret, sid)
                    if s is not None and s.org_id == org_id and s.provider:
                        provider_owned.append(t)
                        break
            if len(provider_owned) == 1:
                return provider_owned[0], norm
            names = ", ".join(repr(t.name) for t in sorted(top, key=lambda t: t.name))
            raise HTTPException(status_code=409, detail=(
                f"ambiguous: multiple tools match {host!r}: {names}; call one by name as "
                "/call/<name>/<path>"))
        return top[0], norm

    name, _, path = rest.partition("/")
    tool = (
        await db.execute(select(Tool).where(Tool.name == name, Tool.org_id == org_id))
    ).scalar_one_or_none()
    if tool is None:
        cat = catalog_store.load()
        # A DOTTED name that reached here was meant to be a catalog endpoint id and missed — a
        # near-miss id, most often one segment off. Answering "no tool 'lusha.companies-signals' in
        # this org" describes the wrong half of treg and leaves the caller nothing to try; naming
        # the real id turns the dead end back into the next call.
        if (name not in cat.by_id and "." in name and not path
                and (near := catalog_store.near_ids(name, cat))):
            raise HTTPException(status_code=404, detail={
                "error": f"no endpoint {name!r} in the catalog",
                "hint": "did you mean " + ", ".join(near) + "?",
                "did_you_mean": near})
        detail = f"no tool {name!r} in this org"
        # A caller may have mistaken a catalog-looking operation for a path on the connected own
        # tool. Look only at callable tools inside this org and only on the error path; the first
        # dotted segment is the provider/tool convention (`google-analytics.report` →
        # `google-analytics`). Connection
        # suffixes also count, so an org whose surviving account is `google-analytics-2` still gets
        # an actionable route. Keep catalog_store.near_ids above provider-local and unchanged.
        own_tools = (await db.execute(
            select(Tool).where(Tool.org_id == org_id)
        )).scalars().all()
        first_segment = name.partition(".")[0]
        own_near = sorted({
            t.name for t in own_tools
            if _tool_usable(caller, t) and (
                name.startswith(t.name + ".")
                or t.name == first_segment
                or t.name.startswith(first_segment + "-")
            )
        }, key=lambda candidate: (-len(candidate), candidate))
        if own_near:
            suggested = own_near[0]
            raise HTTPException(status_code=404, detail={
                "error": detail,
                "hint": (f"your org has tool {suggested!r} — call "
                         f"/call/{suggested}/<path>"),
                "did_you_mean": own_near,
            })
        # A bare provider name (`treg call tikhub /path`) stays a miss, but points at the
        # marketplace form instead of dead-ending — its endpoints are callable without a tool.
        if oauth_providers.get(name) is not None or name in cat.provider_meta:
            detail += (f" — but {name!r} is a marketplace provider; call its endpoints directly: "
                       f"treg catalog search <what you need> → treg call <endpoint-id>")
        raise HTTPException(status_code=404, detail=detail)
    base = tool.base_url.rstrip("/")
    # No path → the base URL itself, WITHOUT a trailing slash: a base pinned to a full resource
    # (e.g. .../v1/charges) must relay as-is — Stripe 404s `/v1/charges/`.
    return tool, (f"{base}/{path.lstrip('/')}" if path else base)


# ---- direct marketplace calls: `treg call <catalog-endpoint-id>`, no tool registration ----------
# See docs/context/interface/cli-audit-2026-07-28.md (design section). The registry stays "our
# stuff"; the catalog is "everything callable". Credential ladder: (1) an org tool bound to the
# provider — resolved via the URL-passthrough shape, so ACLs and ambiguity handling are identical —
# then (2) an org credential matching the provider, injected via a VIRTUAL tool that is never
# persisted (no registry pollution), then (4) TREG'S OWN key for the provider, metered against the
# org's prepaid balance — the keyless first call — and only then (3) an actionable error naming the
# connect/secret fix.
#
# Tier 4 is the only rung that spends OUR money, so it is fenced on every side: the endpoint must be
# `platform_eligible` (priced, price-provenanced, live-verified, not the caller's own account's
# business — see catalog_store.platform_eligible), the provider must be allow-listed AND keyed
# (config.platform_key_for — the kill switch), the org must not be a demo, the estimated cost is
# RESERVED from the balance before the request leaves, and a per-org daily ceiling caps the damage a
# runaway agent can do. The key itself only ever exists as a `platform_setting` NAME in a virtual
# tool's bindings; `relay` reads the value from settings at call time, so no platform credential is
# stored, listable, or reachable from a local run.

def _catalog_endpoint_for(rest: str) -> dict | None:
    """The catalog endpoint `rest` names, or None. Only a dotted, slash-free rest can be an
    endpoint id, so tool names and URL/named shapes never reach the catalog lookup."""
    if "/" in rest or "." not in rest or rest.startswith("http"):
        return None
    return catalog_store.load().by_id.get(rest)


def _enforce_catalog_status(ep: dict) -> None:
    """Refuse a catalog id the provider has retired or broken, with its migration story.

    This runs only after `_resolve_call` has failed and `_catalog_endpoint_for` has identified a
    real catalog id. A team's own tool with the same name therefore still wins, and URL-passthrough
    calls never enter this path at all.
    """
    status = str(ep.get("status") or "").strip().lower()
    if not status:
        return
    detail = f"{ep['id']} is {status}"
    if note := str(ep.get("status_note") or "").strip():
        detail += f": {note}"
    if successor := str(ep.get("superseded_by") or "").strip():
        detail += f" Use {successor} instead."
    elif alternatives := _capability_alternatives(ep):
        # 41 of the 50 TikHub retirements have no same-provider successor, so `superseded_by` has
        # nothing to say for them. A cross-provider sibling is the only help left — and it is the
        # difference between a tombstone and a migration path.
        detail += " " + " ".join(alternatives)
    else:
        detail += " No replacement is currently catalogued."
    raise HTTPException(status_code=410, detail=detail)


async def _marketplace_secret(service: str, org_id: int, db: AsyncSession) -> Secret | None:
    """Tier 2's credential: an org secret tagged with this provider (registry connects), else one
    NAMED exactly for it (`treg secret add tikhub …`). Newest wins — a reconnect supersedes."""
    tagged = (await db.execute(
        select(Secret).where(Secret.org_id == org_id, Secret.provider == service)
        .order_by(Secret.id.desc())
    )).scalars().first()
    if tagged is not None:
        return tagged
    return (await db.execute(
        select(Secret).where(Secret.org_id == org_id, Secret.name == service)
        .order_by(Secret.id.desc())
    )).scalars().first()


@dataclass
class MarketplaceCall:
    """One resolved catalog-endpoint call: where it goes, who paid for the credential, and — when
    treg's own key is paying — what the ledger is holding for it. `call_tool` carries this from
    resolution through the relay to the settle and the telemetry row, so the endpoint id and the
    credential tier are recorded even when the call fails."""

    tool: Tool                      # real (tier 1) or virtual + never persisted (tiers 2/4)
    upstream: str
    consumed: set[str]              # query params eaten by `{placeholder}` path substitution
    endpoint_id: str
    provider: str
    tier: str                       # tool | credential | platform
    cost_type: str = ""             # cost.type — decides whether a 4xx is billable (per_call is)
    estimate_micro: int = 0         # RAW provider estimate; the ledger applies the margin
    params_hash: str = ""
    call_id: str | None = None      # the ledger hold, once reserved (metered calls only)
    # The call rides a REGISTRY OAUTH CONNECT of a provider that bills treg's app per use (X's
    # pay-per-use: the app owner pays whoever's token made the call). Orthogonal to `tier` — the
    # credential is genuinely the org's own (tier 1/2), but the upstream bill is ours, so the call
    # is metered anyway. Set by `_billed_marketplace` after the bound secrets are known.
    billed_oauth: bool = False
    unit_micro: int = 0             # RAW per-resource price for a per_result settle-by-count

    @property
    def metered(self) -> bool:
        """True when OUR money is at stake: treg's platform key (tier 4), or an org credential that
        rides treg's pay-per-use OAuth app (`billed_oauth`). Tiers 1/2 on a provider that bills the
        account owner stay unmetered — there the org's own account pays."""
        return self.tier == "platform" or self.billed_oauth


# A `per_result` price is per ROW, so an estimate needs a row count. The caller's own limit param is
# the best available signal; without one, assume a page. Capped, because `limit=100000` must not be
# able to reserve an org's whole balance for a single call — the settle corrects the estimate either way.
_PLATFORM_PAGE_DEFAULT = 20
_PLATFORM_PAGE_MAX = 100
_LIMIT_PARAMS = ("limit", "count", "depth", "page_size", "per_page", "num", "max_results", "size")


def _body_limit(body: bytes) -> int | None:
    """A row-count signal from a JSON body: an explicit limit key first (dataforseo takes
    `[{..., "limit": 3}]`, lusha `{"limit": 1}`), else the ARRAY LENGTH — providers that take a
    list of inputs (brightdata's urls, dataforseo's tasks) bill one result per item, so a 1-item
    body estimating at the 20-row default overstated 20x (seen live: $0.03 shown for a $0.0015
    call). Under-estimating is safe either way — the settle trues up, overruns included."""
    if not body:
        return None
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    items = None
    if isinstance(doc, list) and doc:
        items = len(doc)
        doc = doc[0]
    if not isinstance(doc, dict):
        return items
    for name in _LIMIT_PARAMS:
        val = doc.get(name)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    return items


def _platform_estimate_micro(cost: dict, query, body: bytes = b"") -> int:
    """What one call is expected to cost the platform, in RAW micro-USD (no margin — ledger.reserve
    applies that). Rounds UP: a fraction of a micro-dollar is not representable and must not round to
    free. Returns 0 for a genuinely free endpoint, which reserves nothing."""
    usd = cost.get("usd")
    if usd is None:
        return 0
    n = 1
    if cost.get("type") in ("per_result", "quota_rows"):
        asked = None
        for name in _LIMIT_PARAMS:
            raw = query.get(name)
            if raw is not None and str(raw).strip().isdigit():
                asked = int(str(raw).strip())
                break
        if asked is None:
            asked = _body_limit(body)  # POST providers put the row count in the body, not the query
        n = max(1, min(asked or _PLATFORM_PAGE_DEFAULT, _PLATFORM_PAGE_MAX))
    # Round to 9 dp BEFORE the ceil: float artifacts (0.0015 × 3 → 4500.000000001) must not
    # over-reserve a phantom micro-dollar.
    raw_micro = round(usd * n * 1_000_000, 9)
    whole = int(raw_micro)
    return whole + 1 if raw_micro > whole else whole


# ---- oauth-billed metering: providers whose upstream bill lands on treg's app -------------------
# X moved to pay-per-use (Feb 2026): the APP OWNER is billed per resource read / per post written,
# whoever's user token made the call. A registry connect rides treg's app, so those calls spend
# treg's prepaid credits and must be metered against the org's balance — the same reserve→settle
# path as tier 4. A BYO connect (/oauth/start with the caller's own client_id) stores
# `secret.provider == ""` and is therefore never flagged: its upstream bill is already the org's.

def _usd_to_micro(usd: float) -> int:
    """USD → RAW micro-USD, rounded UP like `_platform_estimate_micro` — a fraction of a
    micro-dollar must not round to free."""
    raw = round(usd * 1_000_000, 9)
    whole = int(raw)
    return whole + 1 if raw > whole else whole


def _oauth_billed_provider(secrets: dict[int, Secret]):
    """The flagged OAuthProvider whose registry connect this call's bindings ride, or None.
    Three gates: the secret is a REGISTRY connect (`secret.provider` is only ever set by the
    callback of a provider-mode /oauth/start — BYO connects carry ""), the registry entry says the
    upstream bills treg's app (`platform_billed`), and this deployment opted into charging
    (`TREG_OAUTH_BILLED_PROVIDERS`, the kill switch — empty keeps today's free behavior)."""
    billed = get_settings().oauth_billed_set
    if not billed:
        return None
    for s in secrets.values():
        if s.kind == "oauth" and s.provider and s.provider in billed:
            p = oauth_providers.get(s.provider)
            if p is not None and p.platform_billed:
                return p
    return None


def _billed_endpoint_match(service: str, method: str, path: str) -> dict | None:
    """The catalog endpoint a URL-passthrough call to `path` lands on, or None. Exact-path entries
    win over templated ones ({id} → one segment), so `/2/users/me` matches the own-account read and
    not `/2/users/{id}`. Purely for pricing + telemetry — never for routing."""
    best, best_placeholders = None, 99
    for ep in catalog_store.load().by_id.values():
        if ep.get("provider") != service or (ep.get("method") or "GET").upper() != method:
            continue
        template = ep.get("path") or "/"
        placeholders = template.count("{")
        if placeholders >= best_placeholders:
            continue
        pattern = re.sub(r"\{\w+\}", "[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}"))
        if re.fullmatch(pattern, path):
            best, best_placeholders = ep, placeholders
    return best


def _post_has_link(body: bytes) -> bool:
    """Whether a write body's `text` carries a URL — X prices those at `billed_write_link_usd`
    (13x a plain post). Sniffs only the text field, not the whole body, so a quote-post id or a
    docs URL in some other field can't inflate the price."""
    if not body:
        return False
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    text = doc.get("text") if isinstance(doc, dict) else None
    return bool(isinstance(text, str) and re.search(r"https?://|www\.", text))


def _oauth_billed_estimate(provider, ep: dict | None, method: str, query, body: bytes) -> tuple[int, str, int]:
    """What this oauth-billed call is expected to cost, RAW micro-USD (ledger applies the margin)
    → (estimate_micro, cost_type, unit_micro). A priced catalog entry wins (the curated x.yaml
    carries per-endpoint rates: own-account reads are 5x cheaper, user lookups 2x dearer than the
    default); the provider-level rates cover the extended/passthrough long tail. `unit_micro` is
    the per-resource price a `per_result` settle counts the response against."""
    cv = catalog_store.load().cost_view(ep.get("cost"), provider.service) if ep and ep.get("cost") else None
    # A ZERO price must fall through to the provider rate, not bill zero — on an oauth-billed
    # provider the upstream charges us whatever the catalog says, so `free` there is a catalog bug
    # (a stale ingest), never a fact. Spelled out because it used to ride on `0.0` being falsy:
    # the same expression read as "no price recorded" and "the price is nothing", and the catalog
    # could publish free while the balance was debited the fallback.
    if cv and cv.get("type") != "free" and cv.get("usd"):
        ctype = str(cv.get("type") or "per_call")
        est = _platform_estimate_micro(cv, query, body)
        if method != "GET" and provider.billed_write_link_usd and _post_has_link(body):
            est = max(est, _usd_to_micro(provider.billed_write_link_usd))
        return est, ctype, (_usd_to_micro(cv["usd"]) if ctype in ("per_result", "quota_rows") else 0)
    if method == "GET":
        rate = provider.billed_read_usd
        est = _platform_estimate_micro({"type": "per_result", "usd": rate}, query, body)
        return est, "per_result", _usd_to_micro(rate)
    if provider.billed_write_link_usd and _post_has_link(body):
        return _usd_to_micro(provider.billed_write_link_usd), "per_call", 0
    return _usd_to_micro(provider.billed_write_usd), "per_call", 0


async def _billed_marketplace(
    mk: MarketplaceCall | None, provider, tool: Tool, upstream_url: str, request: Request
) -> MarketplaceCall:
    """Flag (or, for a URL-passthrough call, build) the `MarketplaceCall` that meters an
    oauth-billed relay. The catalog id shape arrives with an `mk` (tier 1/2 — keep its endpoint id
    and telemetry identity); the passthrough shape gets one made here, priced off the catalog
    entry its path lands on so both shapes pay the same price for the same route."""
    body = await request.body() if _may_have_body(request) else b""
    method = request.method.upper()
    if mk is None:
        path = urlsplit(upstream_url).path or "/"
        ep = _billed_endpoint_match(provider.service, method, path)
        endpoint_id = ep["id"] if ep else f"{provider.service}.passthrough"
        mk = MarketplaceCall(
            tool=tool, upstream=upstream_url, consumed=set(), endpoint_id=endpoint_id,
            provider=provider.service, tier="tool",
            params_hash=_params_hash(endpoint_id, request.query_params.multi_items(), body))
    else:
        ep = catalog_store.load().by_id.get(mk.endpoint_id)
    est, ctype, unit = _oauth_billed_estimate(provider, ep, method, request.query_params, body)
    mk.billed_oauth, mk.estimate_micro, mk.cost_type, mk.unit_micro = True, est, ctype, unit
    return mk


def _params_hash(endpoint_id: str, query_items: list[tuple[str, str]], body: bytes) -> str:
    """An identity for "this exact call again": sha256 over the endpoint id, the ORDER-INDEPENDENT
    query, and a digest of the body. The body itself is never stored or logged — only its hash — so
    this is safe to keep forever and is the future cache key (plan phase 5, repeat-rate measurement)."""
    h = hashlib.sha256()
    h.update(endpoint_id.encode("utf-8", "replace"))
    for k, v in sorted(query_items):
        h.update(b"\x1f" + f"{k}={v}".encode("utf-8", "replace"))
    h.update(b"\x1e" + (hashlib.sha256(body).digest() if body else b""))
    return h.hexdigest()


def _platform_bindings(provider) -> list[dict]:
    """Tier 4's injection: the SAME header/param shape a pasted key of this provider gets
    (`_provider_bindings`), except the value is named rather than carried — `relay` reads
    `platform_setting` from settings at call time. That is the whole security model: treg's key is
    never written to a Secret row (unreadable by the tenant, unexportable by a local run, and
    `api.py`'s cross-org secret check would reject it anyway)."""
    setting = platform_setting_name(provider.service)
    if provider.token_location == "query":
        bindings = [{"platform_setting": setting, "injector": "env", "location": "query",
                     "name": provider.token_param, "format": provider.token_format}]
    else:
        bindings = [{"platform_setting": setting, "injector": "env", "location": "header",
                     "name": provider.token_header, "format": provider.token_format}]
    # A per-user credential PAIR (Tomba's key+secret headers) needs treg's own second half on
    # tier 4. platform_extra_setting is tier-4-only by design: extra_credential_setting would also
    # ride user connects, pairing a user's key with treg's secret — a pair the provider rejects.
    if provider.needs_extra_credential and provider.platform_extra_setting:
        bindings.append({"platform_setting": provider.platform_extra_setting, "injector": "env",
                         "location": "header", "name": provider.extra_credential_header,
                         "format": "{secret}"})
    return bindings


def _platform_offer(ep: dict, provider, org: Org) -> dict | None:
    """May tier 4 serve `ep` for this org, and at what price? The cost view when yes, None when no.

    Every clause is a refusal we WANT to be boring: an unpriced/unknown-confidence price
    (`platform_eligible`), a provider nobody enabled (`platform_key_for` — key AND allow-list), an
    OAuth provider (a platform key is meaningless for one: the credential is a user's own account),
    or a demo org (the sandbox and the public demo must never be able to spend real money — the
    landing page is reachable by anyone with the URL)."""
    if not provider.uses_pasted_secret:
        return None
    cat = catalog_store.load()
    if not cat.platform_eligible(ep):
        return None
    if not get_settings().platform_key_for(ep["provider"]):
        return None
    if demo_sandbox.is_sandbox(org) or org.public_demo:
        return None
    return cat.cost_view(ep.get("cost"), ep["provider"]) or None


def _capability_alternatives(ep: dict, *, limit: int = 3) -> list[str]:
    """Other providers' endpoints for the same capability, best first — derived, never hand-written.

    A dead end that names only the provider the caller asked for is the reason one org spent 268
    calls on `meta-ad-library.meta-ads.library.search` while `scrapecreators.…-search-ads` — the
    same `capability` string, on a key treg already holds — sat one row away answering 192 of 208
    calls for fourteen other teams. The refusal knew the capability the whole time.

    Read from `cat.endpoints`, which `_parse` has already stripped of marked rows, so a retirement
    stops being suggested the moment it is marked and no list here needs maintaining. This
    COMPARES, it does not route: treg never fails over on the caller's behalf (see the charter),
    so this names the options and their prices and leaves the choice where it belongs.

    Deliberately synchronous and I/O-free. Measured success would need `endpoint_stats.observed`
    and a DB round-trip on an error path — which is how a 404 turns into a 500 — and the caller's
    next step, `catalog get`, already ranks the same siblings by observed success.
    """
    capability = ep.get("capability")
    if not capability:  # only curated capabilities can find siblings; nothing is better than a guess
        return []
    cat = catalog_store.load()
    settings = get_settings()
    ranked = []
    for alt in cat.for_capability(capability):
        if alt["id"] == ep["id"]:
            continue
        cost = cat.cost_view(alt.get("cost"), alt["provider"])
        usd = cost.get("usd") if cost else None
        # "Servable" is the caller's real question: not "does another row exist" but "can treg
        # answer it for me right now". Both halves of tier 4, exactly as `_platform_offer` asks.
        servable = bool(cat.platform_eligible(alt) and settings.platform_key_for(alt["provider"]))
        ranked.append((not servable, usd if usd is not None else float("inf"), alt["id"], usd, servable))
    if not ranked:
        return []
    ranked.sort()
    lines = [f"another provider serves {capability}:"]
    for _, _, alt_id, usd, servable in ranked[:limit]:
        price = "price unknown" if usd is None else ("free" if usd == 0 else f"~${usd:g}/call")
        how = "callable now on treg's key" if servable else f"needs your own {alt_id.split('.')[0]} credential"
        lines.append(f"  {alt_id}  {price}  ({how})")
    return lines


def _marketplace_no_credential(service: str, ep_id: str, provider, ep: dict | None = None) -> HTTPException:
    """Tier 3: the actionable dead-end. Every line names a real command; a pasted-key provider
    gets the `secret add` route too (name it for the service so the ladder finds it)."""
    lines = [f"no {service} credential in this org — {ep_id} is a marketplace endpoint"]
    lines.append(f"  connect one:  treg connections connect --provider {service}")
    if provider.uses_pasted_secret:
        lines.append(f"  or add a key: treg secret add {service} --env-var {service.upper().replace('-', '_')}_API_KEY")
    lines.append(f"  or register the tool yourself: treg tool add {service} --base-url {provider.base_url} …")
    if ep is not None:
        lines.extend(_capability_alternatives(ep))
    return HTTPException(status_code=404, detail="\n".join(lines))


_VALID_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _marketplace_upstream(ep: dict, provider, query_params) -> tuple[str, set[str]]:
    """The full upstream URL for an endpoint-id call, with `{placeholder}` path params filled from
    the caller's query params (they are consumed — dropped from the relayed query). Missing
    required params fail HERE, before a credential is touched or money spent."""
    path, consumed = ep["path"] or "/", set()
    for name in re.findall(r"{(\w+)}", path):
        value = query_params.get(name)
        if value is None:
            raise HTTPException(status_code=400, detail=(
                f"{ep['id']} needs --query {name}=<value> (a path parameter of {ep['path']})"))
        # Agents often pass `siteUrl` straight from GSC's sites list, where it may already be
        # encoded. Preserve a value containing a real %HH escape; otherwise encode it exactly once.
        # A literal/invalid percent sequence has no valid escape and therefore becomes `%25`.
        rendered = value if _VALID_PERCENT_ESCAPE_RE.search(value) else quote(value, safe="")
        path = path.replace("{%s}" % name, rendered)
        consumed.add(name)
    inp = ep.get("input") or {}
    required = [k for k, v in (inp.get("queryParams") or {}).items()
                if isinstance(v, dict) and v.get("required") and query_params.get(k) is None]
    if required:
        raise HTTPException(status_code=400, detail=(
            f"{ep['id']} requires --query " + " --query ".join(f"{k}=<value>" for k in required)))
    return provider.base_url.rstrip("/") + "/" + path.lstrip("/"), consumed


async def _enforce_capability_pin(ep: dict, caller: Caller, db: AsyncSession) -> None:
    """Refuse a catalog call that goes around the team's pin for that capability.

    A pin is a decision the team already made ("for finding work emails we use Hunter"), so the
    answer names the endpoint they DO use — an agent that gets told "no" without being told "use
    this instead" will simply try the next provider and be refused again.

    Enforced here rather than in the client so it does not depend on the caller's goodwill, and
    before anything is reserved, so a refusal never has to un-hold money."""
    cap = ep.get("capability")
    if not cap or caller.org_id is None:
        return
    pin = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == caller.org_id,
        CapabilityPin.capability == cap).order_by(CapabilityPin.id))).scalars().first()
    if pin is None or pin.provider == ep["provider"]:
        return
    cat = catalog_store.load()
    # Suggest the OBVIOUS endpoint, not merely the first one in file order: `core` is the curated
    # route for a job, `extended` is the bulk-ingested long tail. Suggesting
    # `tikhub.x.tiktok-analytics-fetch-creator-info-and-milestones` when `tikhub.tiktok.user.profile`
    # exists reads as a broken suggestion and sends the caller somewhere they did not ask to go.
    mine = [e for e in cat.for_capability(cap) if e["provider"] == pin.provider]
    mine.sort(key=lambda e: ((e.get("tier") or "") != "core", not cat.platform_eligible(e), e["id"]))
    alt = mine[0]["id"] if mine else None
    raise HTTPException(status_code=403, detail={
        "error": "capability_pinned",
        "message": (f"this team uses {pin.provider!r} for {cap!r}"
                    + (f" — call {alt} instead" if alt else "")
                    + f". An admin can change it: treg org unpin {cap}"),
        "capability": cap, "pinned_provider": pin.provider, "use_endpoint": alt,
    })


IDEMPOTENCY_WINDOW_S = 24 * 3600   # retries happen in seconds; a day is generous and easy to reason about
IDEMPOTENCY_HEADER = "idempotency-key"
_IDEM_MAX_KEY = 200

# ---- caller tags (X-Treg-Meta) -----------------------------------------------------------------
# A builder reselling treg through one token stamps their OWN ids on each call —
# `X-Treg-Meta: customer=cust_8123, workspace=ws_9` — so they can attribute, budget and invoice their
# users. Deliberately a HEADER and not a tool argument: a model asked to pass an id drops it somewhere
# in a chain, and a figure you cannot reconcile is worse than no figure. The builder's backend already
# sets Authorization on this request; this is the same call site.
META_HEADER = "x-treg-meta"
_META_MAX_KEYS = 5
_META_MAX_HEADER = 512
_META_MAX_VALUE = 128
_META_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# Tag VALUES become storage keys (the idempotency scope, a TagBudget row, a TagSpend row), so the
# charset is an allowlist rather than a length check. See the collision note in `_parse_call_meta`.
_META_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,%d}$" % _META_MAX_VALUE)
# The dimension that scopes idempotency and defaults reports, for a team that never declared one.
DEFAULT_PRIMARY_DIM = "customer"
_MAX_BUDGET_DIMS = 3        # each declared dimension = one indexed lookup per call + a row per value
_MAX_TAG_VALUES = 10_000    # distinct values per dimension per org, bounded at WRITE (see _tag_budget)


@dataclass(frozen=True)
class CallMeta:
    """The parsed tag bag for one call. Built ONCE per request (see call_tool) and read by everyone —
    idempotency scope, budgets, the ledger and the audit row. A second parse site would be a second
    chance to disagree about who pays."""

    tags: dict[str, str]
    primary_dim: str = DEFAULT_PRIMARY_DIM

    @property
    def primary_val(self) -> str:
        return self.tags.get(self.primary_dim, "")


_NO_META = CallMeta(tags={})


def _tag_telemetry(meta: CallMeta) -> dict:
    """The tag columns of an audit row, built the one way — the refusal path and the success path
    both write them and had drifted apart once already.

    `budget_dim` stays blank unless the PRIMARY dimension actually carries a value: a call tagged
    only on some other key must not claim a primary it never had, or a report grouped by the indexed
    column would attribute it to the empty value.
    """
    return {"budget_dim": meta.primary_dim if meta.primary_val else "",
            "budget_val": meta.primary_val,
            "tags": dict(meta.tags) or None}


def _validate_tag_pair(key: str, value: str, *, where: str = "tag") -> tuple[str, str]:
    """One `dim=val` pair, validated the SAME way wherever it enters treg. THE only rule.

    Both doors have to agree, because a tag value becomes a storage key — the idempotency scope, a
    `TagBudget` row, a `TagSpend` row. `pinned_tags` arrives as JSON on the agent-mint endpoint and
    never passes the header parser, so validating only there would leave the identical hole open one
    route over (it did, until this function existed). `_parse_call_meta` therefore delegates here
    rather than repeating the checks: two copies of a storage-key rule is two chances to drift.

    `where` only names the source in the message ("X-Treg-Meta value" vs "tag value"); the rules
    themselves are identical by construction, which is the entire point.
    """
    key = (key or "").strip().lower()
    value = (value or "").strip()
    if not _META_KEY_RE.match(key):
        raise HTTPException(status_code=422, detail=(
            f"{key!r} is not a valid tag key — 1-32 chars of [a-z0-9_]"))
    if not value or len(value) > _META_MAX_VALUE:
        raise HTTPException(status_code=422, detail=(
            f"{where} value for {key!r} must be 1-{_META_MAX_VALUE} characters"))
    if "@" in value:
        # The ledger is append-only, so a tag written today cannot be erased later. An email here
        # is a permanent record of a person, which is not a thing we can undo on request.
        raise HTTPException(status_code=422, detail=(
            f"{where} value for {key!r} looks like an email — use an opaque id: these tags are "
            f"written to an append-only ledger and cannot be deleted afterwards"))
    if not _META_VALUE_RE.match(value):
        # An ALLOWLIST, not a blocklist, and the reason is `_scoped_idempotency_key`: the primary
        # value is joined to the caller's Idempotency-Key with \x1f, so a value permitted to
        # contain that separator lets `customer="A", key="B\x1fC"` collide with
        # `customer="A\x1fB", key="C"` — one of a builder's users reading another's cached
        # response. Do not narrow this to "reject \x1f": the header parser is not a security
        # boundary we control, and the next separator would reopen it.
        raise HTTPException(status_code=422, detail=(
            f"{where} value for {key!r} may only contain letters, digits and . _ - : "
            f"(these ids are used as storage keys)"))
    return key, value


def _parse_call_meta(request: Request, caller: Caller | None = None) -> CallMeta:
    """`X-Treg-Meta: k=v, k=v` → a validated bag. No header means today's behaviour exactly.

    REFUSES rather than repairs. A tag that is silently dropped or truncated is usage that leaves the
    builder's invoice without anyone noticing, and a truncated id can merge two of their users into one
    line — so an oversized value is a 422, never a `[:128]`.

    A PINNED token (Membership.pinned_tags) wins over the header for the dimensions it names: a token
    handed to one customer's machine must not be able to bill another customer. Naming a different
    value for a pinned dimension is a 403 rather than a silent override — a builder debugging their
    integration needs to see the disagreement, not discover it in a month of misattributed invoices.
    """
    pinned = (caller.membership.pinned_tags if caller is not None else None) or {}
    raw = (request.headers.get(META_HEADER) or "").strip()
    if not raw:
        # An unpinned caller with no header is untagged; a pinned one still attributes to its pin, so
        # a builder can hand out a scoped token and never touch the header at all.
        return CallMeta(tags=dict(pinned), primary_dim=_primary_dim_of(caller)) if pinned else _NO_META
    if len(raw.encode()) > _META_MAX_HEADER:
        raise HTTPException(status_code=422, detail=(
            f"X-Treg-Meta is limited to {_META_MAX_HEADER} bytes"))
    tags: dict[str, str] = {}
    for segment in raw.split(","):
        raw_key, sep, raw_value = segment.partition("=")
        if not sep or not _META_KEY_RE.match(raw_key.strip().lower()):
            # The SHAPE of the segment, which only this parser can report — everything past here is
            # the shared storage-key rule.
            raise HTTPException(status_code=422, detail=(
                f"X-Treg-Meta must be `key=value` pairs; keys are 1-32 chars of [a-z0-9_] "
                f"(got {segment.strip()!r})"))
        key, value = _validate_tag_pair(raw_key, raw_value, where="X-Treg-Meta")
        if key in tags:
            raise HTTPException(status_code=422, detail=f"X-Treg-Meta names {key!r} twice")
        tags[key] = value
    if len(tags) > _META_MAX_KEYS:
        raise HTTPException(status_code=422, detail=(
            f"X-Treg-Meta is limited to {_META_MAX_KEYS} keys (got {len(tags)})"))
    for dim, pinned_val in pinned.items():
        if tags.get(dim, pinned_val) != pinned_val:
            raise HTTPException(status_code=403, detail=(
                f"this token is pinned to {dim}={pinned_val!r} and cannot bill {tags[dim]!r}"))
        tags[dim] = pinned_val
    return CallMeta(tags=tags, primary_dim=_primary_dim_of(caller))


def _primary_dim_of(caller: Caller | None) -> str:
    """The tag key that scopes idempotency for this team. Per-org so a builder whose billing unit is a
    workspace is not forced to call it "customer"."""
    if caller is None:
        return DEFAULT_PRIMARY_DIM
    return (getattr(caller.org, "primary_dim", "") or DEFAULT_PRIMARY_DIM)


def _budget_dims_of(org: Org) -> list[str]:
    """The keys this team may set budgets on — declared, because each one costs an indexed lookup on
    every call and a row per value. Bounded at `_MAX_BUDGET_DIMS`."""
    declared = getattr(org, "budget_dims", None)
    if not declared:
        return [getattr(org, "primary_dim", "") or DEFAULT_PRIMARY_DIM]
    return [str(d) for d in declared][:_MAX_BUDGET_DIMS]


def _idempotency_key(request: Request) -> str:
    """The caller's label for this request, or "" when they sent none.

    Only ever the client's. A server-invented key — hashing the URL and body, say — would silently
    collapse two calls a caller genuinely MEANT to make twice, and "do this again" is a legitimate
    thing to ask of an API. No header means today's behaviour exactly: no lookup, no storage.
    """
    return (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()[:_IDEM_MAX_KEY]


_IDEM_SCOPE_SEP = "\x1f"


def _scoped_idempotency_key(key: str, meta: CallMeta) -> str:
    """The caller's label, PARTITIONED by the primary tag.

    A reselling builder runs every one of their users through one token, so two of them will both
    reach for `retry-1` — and `IdempotentCall` is unique on (membership_id, key), which would serve
    the second user the FIRST one's stored response body. That is the cross-tenant leak the table was
    built to prevent, reappearing one level down.

    Folding the value into the stored key partitions retries exactly as widening the unique constraint
    would, with no migration: `uq_idem_caller_key` is declared in `__table_args__`, so SQLAlchemy emits
    it as a table CONSTRAINT inside CREATE TABLE — Postgres could drop it, sqlite could not without
    rebuilding the table. Every access site keeps querying by (membership_id, key) and simply receives
    this value.

    Only the PRIMARY dimension partitions. Retry scoping cannot generalize the way budgets do: a call
    tagged `customer=a, workspace=b` has no principled answer for which of them owns the key.
    """
    if not key:
        return key
    return f"{meta.primary_val}{_IDEM_SCOPE_SEP}{key}" if meta.primary_val else key


def _idem_display(key: str) -> str:
    """The label as the CALLER wrote it — error messages must not echo our internal scoping."""
    return key.rsplit(_IDEM_SCOPE_SEP, 1)[-1]


def _request_fingerprint(method: str, rest: str, body: bytes, query: str = "") -> str:
    """What the label was used FOR, so reusing it on a different request can be caught.

    A client that reuses one label for two different requests has a bug. Quietly returning the first
    answer would hide it, and the caller would be left wondering why their second call returned
    somebody else's data. Refusing loudly is the useful behaviour, and it is what Stripe does.

    The QUERY STRING is part of the request. It was missing here at first, and since most catalog
    calls are GETs that carry all their arguments in the query, that made the check almost inert: two
    genuinely different lookups under one label matched, and the second was answered with the first
    one's data instead of the 422 this function exists to raise.
    """
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\0")
    h.update(rest.encode())
    h.update(b"\0")
    h.update((query or "").encode())
    h.update(b"\0")
    h.update(body or b"")
    return h.hexdigest()


async def _replay_idempotent(key: str, fingerprint: str, caller: Caller,
                             db: AsyncSession) -> Response | None:
    """The stored answer for this caller's label, or None if there is nothing to replay.

    Returns a real response, so the provider is never reached and no money moves. That is the whole
    point: merely skipping the second CHARGE would still make the second upstream call, which means
    still paying the provider and simply absorbing the double cost ourselves.
    """
    row = (await db.execute(select(IdempotentCall).where(
        IdempotentCall.membership_id == caller.membership.id,
        IdempotentCall.key == key))).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        # Past its window: the label is free again, and the call proceeds normally.
        await db.delete(row)
        await db.commit()
        return None
    if row.request_fingerprint and row.request_fingerprint != fingerprint:
        raise HTTPException(status_code=422, detail=(
            f"Idempotency-Key {_idem_display(key)!r} was already used for a different request. Use a new key, or "
            f"repeat the original request exactly."))
    if row.status != "done" or row.response_status is None:
        # Still in flight. The first call is talking to the provider right now; telling the caller to
        # retry is honest and cheap, and it is what stops the second one duplicating the spend.
        raise HTTPException(status_code=409, detail=(
            f"a call with Idempotency-Key {_idem_display(key)!r} is still in progress — retry shortly"))
    return Response(
        content=row.response_body or b"",
        status_code=row.response_status,
        media_type=row.response_media_type or "application/json",
        headers={"X-Treg-Idempotent-Replay": "true",
                 "X-Treg-Cost-Micro": str(row.charged_micro),
                 # The ORIGINAL call's id: a retry must resolve to the row that actually holds the
                 # money, not to a fresh reference for work that never happened.
                 **({"X-Treg-Call-Id": row.call_ref} if row.call_ref else {})},
    )


async def _release_idempotent_claim(request: Request) -> None:
    """Drop a claim this request took and never completed, so the label is usable again at once.

    Reads what the handler parked on `request.state`; does nothing when there is no claim, which is
    every request that sent no key. Never raises: this runs while an error is already being returned.
    """
    claim = getattr(request.state, "idem_claim", None)
    if not claim:
        return
    request.state.idem_claim = None
    membership_id, key = claim
    try:
        async with session_maker() as db:
            row = (await db.execute(select(IdempotentCall).where(
                IdempotentCall.membership_id == membership_id,
                IdempotentCall.key == key,
                IdempotentCall.status == "pending"))).scalar_one_or_none()
            if row is not None:
                await db.delete(row)
                await db.commit()
    except Exception as exc:  # noqa: BLE001 — an error is already on its way out
        logging.getLogger("treg.idempotency").error(
            "could not release idempotency claim %s: %s", key, exc, exc_info=True)


async def _claim_idempotent(key: str, fingerprint: str, rest: str, caller: Caller,
                            db: AsyncSession) -> bool:
    """Take the label for this caller, or report that somebody else already has it.

    The pending row IS the lock. It goes in before the upstream call, so a concurrent retry loses the
    insert on `(membership_id, key)` and is told to wait rather than duplicating the spend.
    """
    # Sweep this caller's expired labels first. LAZY and caller-scoped, matching the hold reaper in
    # ledger.py and for the same reasons: a background timer would need a scheduler and a leader
    # election on a multi-instance deploy, and would still only run on a timer. One indexed DELETE
    # paid by the caller who benefits from it, and a caller who never calls again leaves rows that
    # can no longer answer anything, because a replay checks the window before it serves.
    #
    # Freeing the label matters as much as reclaiming the space: without this, reusing a label a day
    # later would hit the old row's unique constraint and be refused rather than starting fresh.
    await db.execute(delete(IdempotentCall).where(
        IdempotentCall.membership_id == caller.membership.id,
        IdempotentCall.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)))

    row = IdempotentCall(
        org_id=caller.org_id, membership_id=caller.membership.id, key=key,
        request_fingerprint=fingerprint, endpoint_id=rest[:200], status="pending",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=IDEMPOTENCY_WINDOW_S))
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def _store_idempotent(key: str, caller: Caller, *, status_code: int, body: bytes,
                            media_type: str, charged_micro: int, metered: bool,
                            call_ref: str = "") -> None:
    """Remember a METERED success so a retry can be answered without paying twice.

    Metered only. A team calling on its OWN key is billed by the provider, not by us, so there is
    nothing to protect and no reason for treg to hold their response. Successes only, because a
    failure was never billed — replaying one would freeze an error the caller should be free to
    retry out of.

    Anything else drops the claim, which frees the label immediately rather than making the caller
    wait out the window before they can try again.

    Never raises: the caller already has their answer, and a bookkeeping failure must not turn a
    served call into a 500. Its own session, because the request's may be mid-rollback.
    """
    keep = metered and 200 <= status_code < 300
    try:
        async with session_maker() as db:
            row = (await db.execute(select(IdempotentCall).where(
                IdempotentCall.membership_id == caller.membership.id,
                IdempotentCall.key == key))).scalar_one_or_none()
            if row is None:
                return
            if not keep:
                await db.delete(row)
            else:
                row.status = "done"
                row.response_status = status_code
                row.response_body = body
                row.response_media_type = media_type or "application/json"
                row.charged_micro = charged_micro
                row.call_ref = call_ref
                db.add(row)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.idempotency").error(
            "could not record idempotency key %s: %s", key, exc, exc_info=True)


async def _resolve_marketplace_call(
    ep: dict, request: Request, caller: Caller, db: AsyncSession
) -> MarketplaceCall:
    """Walk the credential ladder for a catalog endpoint id → a `MarketplaceCall`.

    The tool is either the org's own registered tool for that provider (tier 1 — passthrough
    resolution, so ACL filtering and the provider-owned tiebreak apply unchanged) or a virtual,
    never-persisted Tool named after the ENDPOINT (tiers 2 and 4) — so the audit trail records the
    endpoint id, and a member's restricted tool list can never contain it (governance: restricted
    members get no direct marketplace calls; `_require_tool_use` enforces that downstream).

    NOTHING is reserved here. Resolution only PRICES the call; `call_tool` reserves after the deny
    rules and caps have had their say, so a refused call never has to un-hold money."""
    await _enforce_capability_pin(ep, caller, db)
    _enforce_catalog_status(ep)
    service = ep["provider"]
    provider = oauth_providers.get(service)
    if provider is None or not provider.base_url:
        raise HTTPException(status_code=502, detail=(
            f"{ep['id']} is cataloged but {service!r} isn't proxy-callable yet"))
    if request.method.upper() != (ep.get("method") or "GET").upper():
        raise HTTPException(status_code=400, detail=(
            f"{ep['id']} is {ep['method']} — add --method {ep['method']}"))
    upstream, consumed = _marketplace_upstream(ep, provider, request.query_params)
    # The telemetry identity of this call, computed once. The body is read here (Starlette caches it,
    # so the relay still streams the same bytes) only for its HASH — never stored, never logged.
    body = await request.body() if _may_have_body(request) else b""
    phash = _params_hash(ep["id"], request.query_params.multi_items(), body)
    # The catalog's estimate travels on EVERY tier — informational on tiers 1/2 (the provider bills
    # the org's own account; Activity shows "estimated") and the reserve amount on tier 4 only
    # (`metered` gates the ledger, so this never charges a balance for an own-key call).
    cv = catalog_store.load().cost_view(ep.get("cost"), service) if ep.get("cost") else None
    info_est = _platform_estimate_micro(cv, request.query_params, body) if cv else 0
    common = dict(upstream=upstream, consumed=consumed, endpoint_id=ep["id"], provider=service,
                  params_hash=phash, cost_type=str((ep.get("cost") or {}).get("type") or ""),
                  estimate_micro=info_est)
    try:  # tier 1 — the org registered this provider: their tool, their bindings, their ACLs
        tool, resolved = await _resolve_call(upstream, caller, db)
        return MarketplaceCall(tool=tool, tier="tool", **{**common, "upstream": resolved})
    except HTTPException as exc:
        if exc.status_code != 404:  # 403 (ACL) / 409 (ambiguous) are real answers, not fall-through
            raise
    secret = await _marketplace_secret(service, caller.org_id, db)  # tier 2 — credential, no tool
    if secret is not None:
        virtual = Tool(  # NEVER added to the session — no registry pollution, by design
            org_id=caller.org_id, name=ep["id"], owner=secret.owner,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_provider_bindings(provider, secret),
        )
        return MarketplaceCall(tool=virtual, tier="credential", **common)
    # tier 4 — treg's own key, metered against the org's balance. Shadowed by tiers 1 and 2 above:
    # an org that brought its own credential is billed by the provider, not by us, and must never be
    # silently switched onto our key (their quota, their rate limits, their data agreements).
    cost = _platform_offer(ep, provider, caller.org)
    if cost is not None:
        virtual = Tool(
            org_id=caller.org_id, name=ep["id"], owner=caller.email,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_platform_bindings(provider),
        )
        return MarketplaceCall(tool=virtual, tier="platform", **{
            **common, "cost_type": str(cost.get("type") or "per_call"),
            "estimate_micro": _platform_estimate_micro(cost, request.query_params, body)})
    raise _marketplace_no_credential(service, ep["id"], provider, ep)


def _may_have_body(request: Request) -> bool:
    """Whether this request could carry a body worth hashing. Mirrors proxy._has_body — a GET with no
    content-length must not be awaited for a body it never sends."""
    cl = request.headers.get("content-length")
    if cl is not None and cl != "0":
        return True
    return "chunked" in request.headers.get("transfer-encoding", "").lower()


@app.get("/catalog/endpoints/{endpoint_id}/access", include_in_schema=False)
async def catalog_endpoint_access(
    endpoint_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Authenticated dry-run of the marketplace credential ladder — which tier would serve YOU.
    Read by `treg catalog get` to print an honest access line under RUN IT (the open catalog
    endpoints stay unauthenticated; this one needs to know who is asking)."""
    ep = catalog_store.load().by_id.get(endpoint_id)
    if ep is None:
        raise HTTPException(status_code=404, detail=f"unknown endpoint {endpoint_id!r}")
    _enforce_catalog_status(ep)
    service = ep["provider"]
    provider = oauth_providers.get(service)
    if provider is None or not provider.base_url:
        return {"tier": "none", "detail": f"{service} isn't proxy-callable yet"}
    # An oauth-billed provider is metered even on the org's own connection (the upstream bills
    # treg's app, not the account) — the dry-run must say so, or the price is a surprise.
    billed_note = ""
    if provider.platform_billed and service in get_settings().oauth_billed_set:
        cv = catalog_store.load().cost_view(ep.get("cost"), service) if ep.get("cost") else None
        est = _platform_estimate_micro(cv, {}) if cv and cv.get("usd") else 0
        billed_note = (f" — metered from the team balance (~${ledger.usd(est):g}/call: "
                       f"{service} bills treg's app per use)") if est else \
                      f" — metered from the team balance ({service} bills treg's app per use)"
    probe = provider.base_url.rstrip("/") + "/" + (ep["path"] or "/").lstrip("/")
    try:
        tool, _ = await _resolve_call(probe, caller, db)
        return {"tier": "tool", "metered": bool(billed_note),
                "detail": f"will use this org's registered {tool.name!r} tool{billed_note}"}
    except HTTPException as exc:
        if exc.status_code == 403:
            return {"tier": "restricted", "detail": "a registered tool exists but your access is restricted — ask an admin"}
        if exc.status_code != 404:
            raise
    if await _marketplace_secret(service, caller.org_id, db) is not None:
        return {"tier": "credential", "metered": bool(billed_note),
                "detail": f"will use this org's {service} credential (no tool needed){billed_note}"}
    cost = _platform_offer(ep, provider, caller.org)
    if cost is not None:
        # The number is the honest per-call price at the DEFAULT page size — a `per_result` endpoint
        # costs more or less depending on how many rows the caller asks for, so it is "~".
        est = _platform_estimate_micro(cost, {})
        return {
            "tier": "platform",
            "detail": (f"no key needed — uses treg's {service} key, ~${ledger.usd(est):g}/call "
                       f"from your team balance (treg balance)"),
            "estimated_cost_micro": est,
            "estimated_cost_usd": ledger.usd(est),
        }
    hint = (f"connect with: treg connections connect --provider {service}"
            if not provider.uses_pasted_secret else
            f"connect with: treg connections connect --provider {service}, or treg secret add {service} …")
    return {"tier": "none", "detail": f"no {service} credential in this org yet — {hint}"}


# ---- tier-4 metering: reserve → relay → settle/release ------------------------------------------
def _effective_daily_cap(org: Org) -> int:
    """This team's ceiling on daily tier-4 spend: the LOWER of what they set and what we allow.

    Two masters, which is why it is two numbers. The team's own figure protects them from a runaway
    agent draining a balance that auto-top-up keeps refilling. The platform ceiling protects US from a
    catalog mispricing, and only we can raise it — so onboarding a high-volume builder is a
    conversation rather than an env-var edit that lifts the blast-radius rail for every team at once.

    0 means "never set one", which follows the deployment default rather than freezing the team at
    whatever that default happened to be the day they signed up.
    """
    ceiling = get_settings().platform_daily_cap_micro
    own = int(getattr(org, "daily_cap_micro", 0) or 0)
    return min(own, ceiling) if own > 0 else ceiling


async def _enforce_trial_allowance(caller: Caller, provider: str, db: AsyncSession) -> None:
    """Per-team, per-UTC-day call allowance for TRIAL-POOL providers (fx.yaml `kind: treg_trial`).

    A trial provider is served on treg's own FREE-tier key at a $0 price, so the price gives no
    brake at all — one looping agent would drain the shared vendor quota for every team at once.
    The allowance is the brake, and it lives in the same fx entry as the zero (catalog.trial_pools).

    Counted from audit rows: successful (2xx) calls only, because a failed call produced nothing —
    the same line billability draws. `tool_name` is the endpoint id, so the provider is its prefix.
    The audit is written fire-and-forget, so the count can lag a call or two under load; for a free
    trial that slack is acceptable and bounded. Own-key (tier 2) calls never reach this check — a
    team with its own key is never throttled by the trial it does not use.

    FAIL-CLOSED like the platform cap: the quota being protected is the shared vendor key, and
    serving blind when the count cannot be read is how the pool dies for everyone."""
    allowance = catalog_store.load().trial_pools.get(provider)
    if not allowance:
        return
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0,
                                                   tzinfo=None)
    try:
        used = (await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.org_id == caller.org_id,
                CallRecord.tool_name.like(f"{provider}.%"),  # type: ignore[union-attr]
                CallRecord.status_code >= 200, CallRecord.status_code < 300,
                CallRecord.created_at >= day_start))).scalar_one()
    except Exception as exc:  # noqa: BLE001 — cannot verify the pool ⇒ do not drain it
        logging.getLogger("treg.ledger").warning(
            "trial-allowance check failed for org %s / %s: %s", caller.org_id, provider, exc)
        raise HTTPException(status_code=429, detail=(
            f"cannot verify today's {provider} trial usage right now — retry shortly, or use "
            "your own key: treg connections connect"))
    if used >= allowance:
        raise HTTPException(status_code=429, detail={
            "error": "trial_allowance_reached", "provider": provider,
            "allowance_per_day": allowance, "used_today": int(used),
            "message": (f"this team has used its free {provider} trial for today "
                        f"({used}/{allowance} calls). It resets at 00:00 UTC — or connect your "
                        f"own {provider} key for unmetered calls at your plan's limits: "
                        "treg connections connect"),
        })


async def _enforce_platform_daily_cap(caller: Caller, add_micro: int, db: AsyncSession) -> None:
    """Per-org, per-UTC-day ceiling on tier-4 spend. FAIL-CLOSED, unlike `_enforce_daily_cap`: that one
    meters calls and may let a few extra through under load, this one meters OUR money, so a query that
    cannot answer refuses the call. The cap is the blast radius of a runaway agent (and of a pricing
    mistake in the catalog) — the balance alone is not enough, because auto-top-up can refill it."""
    cap = _effective_daily_cap(caller.org)
    try:
        spent = await ledger.spent_today(db, caller.org_id)
    except Exception as exc:  # noqa: BLE001 — cannot verify the ceiling ⇒ do not spend
        logging.getLogger("treg.ledger").warning(
            "platform daily-cap check failed for org %s: %s", caller.org_id, exc)
        raise HTTPException(status_code=429, detail=(
            "cannot verify today's platform spend right now — refusing to spend the team balance "
            "(retry shortly, or use your own key: treg connections connect)"))
    if spent + add_micro > cap:
        raise HTTPException(status_code=429, detail={
            "error": "platform_daily_cap_reached",
            "message": (f"this team has reached its daily limit for calls on treg's keys "
                        f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g} today). It resets at 00:00 UTC. "
                        f"To keep going now, connect your own key: "
                        f"treg connections connect --provider <provider>"),
            "spent_today_micro": spent, "daily_cap_micro": cap, "estimated_cost_micro": add_micro,
        })


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# The reserved value standing for "every value of this dimension". Safe forever because the tag
# value charset (`_META_VALUE_RE`) excludes `*`, so no caller can send a value that collides with it.
TAG_DEFAULT = "*"


async def _resolve_tag_budget(db: AsyncSession, org_id: int, dim: str, val: str) -> TagBudget | None:
    """The limit in force for one tag value: its own override, else the dimension's default, else
    none (unlimited — the shipped state, until a team sets a default).

    ONE indexed query for both, so adding defaults costs the call path nothing. A registry row
    (`auto`) is skipped: it exists to make the cardinality check cheap, and treating it as an override
    would mean the default never applied to anything that had ever been called.
    """
    rows = (await db.execute(select(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim,
        TagBudget.val.in_([val, TAG_DEFAULT])))).scalars().all()
    own = next((r for r in rows if r.val == val and not r.auto), None)
    return own or next((r for r in rows if r.val == TAG_DEFAULT), None)


async def _tag_budget(db: AsyncSession, org_id: int, dim: str, val: str,
                      create: bool = False) -> TagBudget | None:
    """This team's budget row for one tag value, creating it on first sighting when asked.

    Auto-created, so a builder never pre-registers a user before their first call can carry an id.
    The row also BOUNDS cardinality: the count runs only on the miss path, so steady state stays one
    indexed lookup. Bounding has to happen at the write — a limit checked when a report is run is
    checked after the rows already exist.
    """
    row = (await db.execute(select(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim, TagBudget.val == val))).scalar_one_or_none()
    if row is not None or not create:
        return row
    seen = (await db.execute(select(func.count()).select_from(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim))).scalar() or 0
    if seen >= _MAX_TAG_VALUES:
        raise HTTPException(status_code=429, detail={
            "error": "tag_cardinality_exceeded", "dim": dim,
            "message": (f"this team has already used {seen} distinct {dim!r} values, the limit. A tag "
                        f"that changes every call (a session or request id) is not a budget "
                        f"dimension — tag by the unit you bill."),
        })
    row = TagBudget(org_id=org_id, dim=dim, val=val, auto=True)
    db.add(row)
    await db.commit()
    return row


async def _enforce_tag_budgets(caller: Caller, meta: CallMeta, db: AsyncSession,
                               add_micro: int | None = None) -> None:
    """Refuse a call that breaches a builder-set limit on one of its tags.

    Two passes, called from two places. `add_micro is None` is the PRE-FLIGHT pass (blocked status and
    the daily call count), which runs before the idempotency replay so a blocked user can neither take
    a lock nor be served an answer cached before they were blocked. `add_micro` set is the SPEND pass,
    which needs the estimate and therefore runs inside `_platform_reserve`.

    Every declared dimension is evaluated and the FIRST breach in declaration order refuses, so the
    outcome is deterministic when budgets stack.

    THE CAPS ARE SOFT — advisory, not a gate. `ledger.reserve` is exact because the balance is a
    materialized column, so its check and its debit are one conditional UPDATE. A per-tag total is an
    aggregate over rows, so N concurrent calls can each read a compliant figure and together exceed
    the cap; the overshoot is bounded by concurrency × per-call estimate. That is acceptable ONLY
    because the hard gates sit behind this one: the org balance and the platform daily cap. Making it
    exact would need a second materialized authority on spend, reset daily, decremented on release and
    corrected on settle divergence — four new ways to disagree with ledger.py, which is the one module
    allowed to move money. Never document these caps to builders as hard limits.
    """
    if not meta.tags:
        return
    dims = _budget_dims_of(caller.org)
    for dim in dims:
        val = meta.tags.get(dim)
        if not val:
            continue
        try:
            # Registering the value (cardinality bound) happens on the pre-flight pass only; both
            # passes then resolve override → default.
            if add_micro is None:
                await _tag_budget(db, caller.org_id, dim, val, create=True)
            row = await _resolve_tag_budget(db, caller.org_id, dim, val)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — cannot verify a ceiling ⇒ do not spend
            logging.getLogger("treg.ledger").warning(
                "tag budget check failed for org %s (%s=%s): %s", caller.org_id, dim, val, exc)
            raise HTTPException(status_code=429, detail={
                "error": "tag_budget_unavailable", "dim": dim, "val": val,
                "message": "cannot verify this budget right now — retry shortly",
            })
        if row is None:
            continue
        if add_micro is None:
            if row.status == "blocked":
                raise HTTPException(status_code=403, detail={
                    "error": "tag_blocked", "dim": dim, "val": val,
                    "message": f"{dim} {val!r} is blocked",
                })
            if row.calls_per_day is not None and row.calls_per_day >= 0:
                # From the LEDGER's tag rows, never CallRecord: audit rows are shed under load, so a
                # count cap would let a burst through exactly when it matters — and CallRecord only
                # carries the PRIMARY dimension, so a cap on any other declared key matched nothing
                # and never fired at all.
                used = await ledger.tag_calls_since(
                    db, caller.org_id, dim, val, _day_start_utc())
                if used >= row.calls_per_day:
                    raise HTTPException(status_code=429, detail={
                        "error": "tag_call_cap_reached", "dim": dim, "val": val,
                        "used_today": int(used), "calls_per_day": row.calls_per_day,
                        "message": f"{dim} {val!r} has used its {row.calls_per_day} calls for today",
                    })
            continue
        for cap, since, period in ((row.daily_cap_micro, _day_start_utc(), "day"),
                                   (row.monthly_cap_micro, _month_start_utc(), "month")):
            if cap is None:
                continue
            spent = await ledger.tag_spent_since(db, caller.org_id, dim, val, since)
            if spent + add_micro > cap:
                # Deliberately NOT the org-level 402/429 shape: that one carries the team's balance and
                # a top-up link, and this response is the one a builder renders to their own end user.
                raise HTTPException(status_code=429, detail={
                    "error": "tag_spend_cap_reached", "dim": dim, "val": val,
                    "spent_micro": spent, "cap_micro": cap, "period": period,
                    "estimated_cost_micro": add_micro,
                    "message": (f"{dim} {val!r} has reached its spend limit for this {period} "
                                f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g})"),
                })


async def _platform_reserve(mk: MarketplaceCall, caller: Caller, db: AsyncSession,
                            meta: CallMeta = _NO_META,
                            call_ref: str | None = None) -> None:
    """Withhold this call's estimated cost BEFORE a byte goes upstream, and record the hold on `mk`.
    Insufficient balance is a 402 whose body an agent can act on without reading prose.

    `meta` is the caller's parsed X-Treg-Meta bag, passed explicitly rather than hung off `mk`:
    attribution decides who a reselling builder bills, and it belongs to the request, not to the
    endpoint match. The already-parsed object travels, never a bare dict — re-deriving the primary
    dimension here would be a second place that could disagree about who pays."""
    # The builder's own per-tag ceilings first: a refusal that belongs to ONE of their users must
    # not surface as the team-wide balance error, which names the builder's private numbers.
    await _enforce_tag_budgets(caller, meta, db, add_micro=mk.estimate_micro)
    await _enforce_platform_daily_cap(caller, mk.estimate_micro, db)
    await _enforce_trial_allowance(caller, mk.provider, db)
    try:
        mk.call_id = await ledger.reserve(
            db, caller.org_id, mk.endpoint_id, mk.estimate_micro,
            meta={"tier": "oauth" if mk.billed_oauth else "platform",
                  "provider": mk.provider, "cost_type": mk.cost_type},
            tags=meta.tags, call_id=call_ref)
        # reserve moves balance via a raw conditional UPDATE, so the ORM instance is stale — refresh
        # before the threshold check or a crossing goes unnoticed until some later request.
        await db.refresh(caller.org)
        billing.maybe_schedule_autotopup(caller.org)
    except ledger.InsufficientBalance as exc:
        wallet = f"treg's {mk.provider} " + ("app (pay-per-use)" if mk.billed_oauth else "key")
        # For a billed OAuth call "connect your own key" is not the fix — the connection already
        # exists; the way off the meter is bringing your OWN developer app to /oauth/start.
        alt = (f"  or bring your own {mk.provider} developer app (BYO OAuth) — those calls are never metered"
               if mk.billed_oauth else
               f"  or use your own key: treg connections connect --provider {mk.provider}")
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_balance",
            "message": (f"{mk.endpoint_id} would cost ~${ledger.usd(exc.required_micro):g} on {wallet} "
                        f"and this team's balance is ${ledger.usd(exc.balance_micro):g}.\n"
                        f"  add funds:      {get_settings().public_url}/app#billing\n"
                        + alt),
            "balance_micro": exc.balance_micro,
            "estimated_cost_micro": exc.required_micro,
            "topup_url": "/app#billing",
            "provider": mk.provider,
            "endpoint_id": mk.endpoint_id,
        })


# 4xx statuses that mean "the provider did not serve this, and it is NOT the caller's input" — our
# credential was rejected, exhausted, throttled, or the request timed out. The provider bills nothing
# for these, so neither may we: charging here would pass OUR expired or over-quota platform key on to
# a team as real spend, and for a builder reselling treg it would land on their end customers' bills.
# 403 is deliberately included even though some providers use it for a genuinely caller-driven
# "resource not accessible": when it is unclear whether the provider charged us, the safe direction
# is not to charge. Absorbing a rare few micro-USD is recoverable; over-billing out of an append-only
# ledger is not.
_NOT_THE_CALLERS_FAULT = frozenset({401, 402, 403, 405, 407, 408, 429})


def _platform_billable(status_code: int, cost_type: str) -> bool:
    """Does a response with this status cost us money? (plan §2.2)
      2xx                        → yes, the provider served it.
      4xx                        → only under `per_call`, and only when the rejection is about the
                                   CALLER'S INPUT (400/404/422 …): the provider charges for accepting
                                   such a request, so it is on the caller. A credential/quota refusal
                                   (`_NOT_THE_CALLERS_FAULT`) is on us and is never billed — a 405
                                   rejects the method OUR catalog selected, while a 429 on a
                                   SHARED-plan key is treg's own saturation. Billing either would
                                   charge teams for our metadata or congestion. Under
                                   `per_result`/`per_success` a rejected request produced nothing.
      5xx / 3xx / network error  → no. An upstream failure is never billed to the caller.
    """
    if 200 <= status_code < 300:
        return True
    if 400 <= status_code < 500:
        return cost_type == "per_call" and status_code not in _NOT_THE_CALLERS_FAULT
    return False


_PLATFORM_BODY_MAX = 8 * 1024 * 1024  # buffer ceiling for a metered response (API JSON, not downloads)

# ---- failure evidence: what a failed call is allowed to leave behind ----------------------------
# Sized to hold a real provider error whole — a typical 400 body is 80-300 characters and a verbose
# JSON one about 800 — while still capping a ~14KB CDN error page and a caller stuck in a retry loop.
_ERROR_RESPONSE_MAX = 2000
_ERROR_REQUEST_MAX = 1000
# Unmetered calls keep streaming unless the caller explicitly declared a small body. Starlette's
# request cache then lets relay replay those exact bytes without a second read from the socket.
_ERROR_CALLER_BODY_MAX = 64 * 1024
# Sliced off the FRONT before any decode, so an 8MB single-line HTML error page never gets decoded or
# regex-scanned on the request path. Every limit above is characters; this one is bytes.
_ERROR_BODY_SLICE = 8192
_ERROR_MASKING_FAILED = "<redacted: could not render credentials for masking>"

# Third-party secret shapes. `_EVIDENCE_SECRET_RE` below covers values that LOOK like a key; these
# two cover the places a value hides by its NAME instead — in a URL or a JSON body — which
# `_CRED_FLAG_EQ_RE` misses because it requires a leading dash (it was written for argv, where
# `--token=x` is the only shape).
_QUERY_CRED_RE = re.compile(
    r"(?i)((?:api[-_]?key|apikey|key|token|secret|password|passwd|pwd|auth|access[-_]?token"
    r"|sig|signature)\"?\s*[=:]\s*\"?)[^&\s\"',}]+")
_URL_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")

# `_ARGV_SECRET_RE`'s catch-all masks ANY 24+ run of [A-Za-z0-9_-], which is right for an argv log and
# wrong here: it deletes 100% of provider correlation identifiers — UUIDs, ULIDs, 32-char trace ids,
# request ids — which are exactly what you quote to a provider's support desk. Measured on real error
# bodies: the prose always survived, the correlation field never did. So for evidence we keep the
# TARGETED half (known key prefixes, JWTs) and drop the catch-all. Platform credentials do not depend
# on it — they have exact masking plus the fail-closed backstop below — and the owner has accepted
# that a third-party secret may occasionally survive here.
_EVIDENCE_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    # Anchored + possessive for the same reason as the argv rule above, and it matters MORE here:
    # this one runs on a PROVIDER's response body, which is uncontrolled input on the request path.
    r"|\beyJ[A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]{8,}")

# Response headers worth keeping on a FAILED platform call. An empty-bodied 401 or 429 is otherwise
# undiagnosable, and these say which of "bad credential" / "wrong scheme" / "quota gone" / "retry in
# N" it was. Allowlisted, never the whole bag: `authorization` and `set-cookie` live in there too.
_EVIDENCE_HEADERS = (
    "retry-after", "www-authenticate",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
    "x-request-id", "x-requestid", "request-id", "x-correlation-id", "x-amzn-requestid",
    "cf-ray", "x-trace-id",
)


_SENSITIVE_JSON_SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "token", "client_secret", "api_key", "secret",
    "password", "private_key",
}


def _secret_renderings(tool: Tool, secrets: dict[int, Secret]) -> list[str]:
    """Every spelling of every injected credential for this tool, longest first.

    This is the primary defence and the only deterministic one: platform credentials come from a
    named setting and org credentials from an encrypted Secret, so both can be matched exactly instead
    of guessed at. Providers routinely quote the offending request back inside a 400/401 body — the
    header they received, or the full URL including the query — and a key can survive
    `_EVIDENCE_SECRET_RE` by simply not looking like a known key shape. Exact substring masking is why
    the deterministic layer carries the weight here and the pattern layer is only a net.

    treg injects the value verbatim, but a PROVIDER may hand it back transformed, and a transform it
    can reverse is one we have to anticipate. Four families, all observed shapes rather than guesses:

    * the raw value, and the value after the binding's `format` (`Bearer {secret}`, `Basic {secret}`);
    * percent-encoded — twelve providers authenticate by query param, so the key comes back inside an
      echoed URL. Both cases: `quote()` emits UPPERCASE hex (`%2F`) and plenty of servers echo lower;
    * JSON-escaped, because a body quoting a URL usually writes `\\/` for `/`;
    * **the DECODED halves of a Basic credential.** `config.py` states that dataforseo's platform
      value is already the base64 of `login:password`. A provider that decodes Basic auth and reports
      `{"received_username": …, "received_password": …}` echoes treg's credential in a form where
      neither the base64 blob nor `Basic <blob>` appears. dataforseo is the largest provider by
      spend, so this is the opposite of theoretical.
    """
    out: set[str] = set()

    def add(value: str) -> None:
        """One secret and every spelling of it a provider might echo back."""
        if not value or len(value) < 4:
            return  # too short to mask without redacting half the message
        enc = quote(value, safe="")
        out.update({value, enc, enc.lower(), quote_plus(value), value.replace("/", "\\/"),
                    json.dumps(value, ensure_ascii=False)[1:-1]})

    def add_credential(value: str) -> None:
        add(value)
        for part in _basic_credential_parts(value):  # mask what a provider can DECODE
            add(part)

    for binding in tool.bindings or []:
        setting = binding.get("platform_setting")
        if setting:
            value = getattr(get_settings(), setting, None)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            add_credential(value)
            add(str(binding.get("format") or "{secret}").format(secret=value))
            continue

        sid = binding.get("secret_id")
        if sid is None:
            continue
        plain = crypto.decrypt(secrets[sid].value)
        add_credential(plain)
        injector = binding.get("injector", "env")
        if injector not in ("oauth", "secret_file"):
            add(str(binding.get("format") or "{secret}").format(secret=plain.strip()))
            continue

        field = str(binding.get("secret_field") or "access_token")
        token = injectors._token_from_json(plain, field)
        add_credential(token)
        add(f"Bearer {token}")
        add(str(binding.get("format") or "{secret}").format(secret=token.strip()))
        data = json.loads(plain)
        if not isinstance(data, dict):
            raise ValueError("JSON credential is not an object")
        sensitive = _SENSITIVE_JSON_SECRET_KEYS | {field.lower()}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in sensitive and isinstance(value, str):
                add_credential(value)
    # Longest first so `Bearer abc` is masked as a unit before the bare `abc` inside it turns the
    # line into `Bearer ***` — same result here, but the ordering stops a shorter secret that is a
    # substring of a longer one from fragmenting it into an unmatchable remainder.
    return sorted((s for s in out if len(s) >= 4), key=len, reverse=True)


def _safe_secret_renderings(tool: Tool, secrets: dict[int, Secret]) -> list[str] | None:
    """Render credentials for masking, or signal that evidence must be replaced wholesale."""
    try:
        return _secret_renderings(tool, secrets)
    except Exception as exc:  # noqa: BLE001 — malformed/encrypted credentials must fail closed
        logging.getLogger("treg").warning("could not render credentials for error masking: %s", exc)
        return None


def _basic_credential_parts(value: str) -> list[str]:
    """`login:password` and its two halves, when `value` is the base64 of a Basic credential.

    Returns [] for anything that is not — an ordinary API key rarely base64-decodes to printable text
    containing a colon, and a false positive here only costs an extra (harmless) mask.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 — not base64, or not text: simply not a Basic credential
        return []
    if ":" not in decoded or not decoded.isprintable():
        return []
    login, _, password = decoded.partition(":")
    return [p for p in (decoded, login, password) if p]


def _decode_error_body(raw: bytes, content_encoding: str = "", content_type: str = "") -> str:
    """Bytes off the wire → something a human can read, or an honest marker saying why not.

    `force_identity` asks the provider not to compress a metered response, but a CDN or WAF error page
    is generated at the edge and answers however it likes — and those 403s are exactly the responses
    this feature exists to explain. `relay` streams `aiter_raw()`, so nothing has decoded them.
    """
    if not raw:
        return ""
    enc = (content_encoding or "").strip().lower()
    if enc and enc != "identity":
        if enc not in ("gzip", "deflate"):  # br, zstd — no stdlib decoder we can rely on
            return f"<{enc}-encoded, {len(raw)} bytes, not decoded>"
        try:
            # INCREMENTAL and capped just past the evidence limit. `gzip.decompress` is unbounded, so
            # slicing the INPUT to 8KiB does not bound the OUTPUT: 20MB of one repeated byte
            # compresses to under 20KB, and a bomb would expand to megabytes that four regexes then
            # walk synchronously on the request path.
            d = zlib.decompressobj(16 + zlib.MAX_WBITS if enc == "gzip" else -zlib.MAX_WBITS)
            raw = d.decompress(raw, _ERROR_RESPONSE_MAX * 4)
        except Exception:  # noqa: BLE001 — a truncated slice of a gzip stream is expected to fail
            return f"<{enc}-encoded, {len(raw)} bytes, undecodable>"
    text = raw.decode("utf-8", "replace")
    # A binary payload decoded with errors="replace" is a wall of U+FFFD that says nothing. Report the
    # shape instead, keeping a short hex head so the content type is still identifiable.
    if text.count("�") > len(text) // 5 or ("\x00" in text[:512]):
        return f"<binary {content_type or 'response'}, {len(raw)} bytes, head={raw[:32].hex()}>"
    return text


def _caller_request_snippet(request: Request, tool: Tool, caller_body: bytes,
                            secrets: list[str]) -> str:
    """What the CALLER actually sent, redacted — the half of a failure treg otherwise forgets.

    `CallRecord.path` stores the catalog's upstream URL with only `{placeholder}` path params filled,
    so the caller's real query and body survive nowhere else (`params_hash` is one-way). Without this
    a 400 cannot be explained even when the provider says exactly what was wrong with it.

    Query params are read from the INBOUND request, which never carries an injected credential:
    injection builds a separate outbound list (see proxy.relay). The binding's own query names are
    dropped anyway, for the caller who passed a value into the slot the injector overwrites.
    """
    drop = {b.get("name", "Authorization") for b in (tool.bindings or [])
            if b.get("location", "header") == "query"}
    parts = []
    pairs = [f"{k}={v}" for k, v in request.query_params.multi_items() if k not in drop]
    if pairs:
        parts.append("?" + "&".join(pairs))
    if caller_body:
        parts.append(_decode_error_body(caller_body[:_ERROR_BODY_SLICE], "",
                                        request.headers.get("content-type", "")))
    return _redact_snippet(" ".join(parts), secrets, _ERROR_REQUEST_MAX)


def _redact_snippet(text: str, secrets: list[str], limit: int) -> str:
    """Mask, THEN truncate — never the other way round.

    Truncating first can cut a 40-character token down to a 12-character survivor that no longer
    matches the 24+ rule, which is how a "redacted" field ends up holding half a key. `_redact_argv`
    already gets this order right; this follows it.
    """
    if not text:
        return ""
    for secret in secrets:  # exact and deterministic, before any pattern guessing
        text = text.replace(secret, "***")
    text = _URL_USERINFO_RE.sub("://***:***@", text)
    text = _QUERY_CRED_RE.sub(r"\1***", text)
    text = _EVIDENCE_SECRET_RE.sub("***", text)
    text = " ".join(text.split())  # collapse newlines/indentation; these are read in a table
    # Fail closed. Everything above is a list of transforms we thought of; this asks whether a secret
    # survived one we did not, by re-checking a NORMALISED copy (percent-decoded, JSON-unescaped,
    # lowercased). If one is still there, drop the whole snippet: losing a debugging message is a bad
    # day, leaking the credential every tenant shares is a much worse one.
    if secrets:
        probe = unquote(text.replace("\\/", "/")).lower()
        if any(s.lower() in probe for s in secrets):
            return "<redacted: a credential survived masking>"
    if len(text) <= limit:
        return text
    # Truncation can expose a partial token at the seam that was safe only while whole.
    return re.sub(r"[A-Za-z0-9_\-+/=.]{8,}$", "***", text[:limit]) + "…"


def _observed_cost_micro(mk: MarketplaceCall, body: bytes) -> int | None:
    """The provider's OWN reported charge for this call, in micro-USD, or None when it doesn't say.

    For an oauth-billed `per_result` call (X reads), the response body IS the bill: X charges per
    resource returned, so counting `data` beats trusting the estimate — a timeline asked for 100
    posts that returned 7 settles at 7, and an empty page settles at zero. The count is capped at
    the reserved estimate's row assumption only implicitly (a bigger-than-asked response charges
    more, which `ledger.settle` handles as an overrun).

    Three providers volunteer the number, in two different denominations:
      - dataforseo: a top-level `cost` in USD — including 0 when it decided not to charge (a free
        route, or a request it rejected before metering). That zero is real information and settles the
        call at zero, which is why the test is `>= 0` and not truthiness.
      - scrapecreators (`credits_charged`), akta and leadmagic (`credits_consumed`): provider
        credits, converted through the provider's credit rate (fx.yaml) — the same conversion
        `cost_view` uses, so a settle can't disagree with the catalog's price. Akta is the one that
        NEEDS this: its enrich route is priced per SECTION requested and its news route adds a
        per-article rider, so the catalog's single estimate can only be an upper bound — the actual
        charge lives here. LeadMagic answers a miss with 2xx and `credits_consumed: 0` (observed at
        verify time), so honouring the field is what keeps a free miss from billing the estimate;
        it also reports fractions (email verify is 0.25).
      - lusha: `billing.creditsCharged`, one level down — the same reported-credits contract,
        including 0 on a 2xx miss (the captured people.enrich example IS one) and the 2-credit
        company enrich. Converted through the lusha rate like the others.
      - apollo: DERIVED, not reported. Apollo answers a miss with 2xx (`organization: null` on
        enrich, an empty `organizations` page on search) and charges nothing for it, so status-based
        billing alone would bill the caller for a response Apollo gave away. The body says whether
        the charged thing came back; when it didn't, the call settles at 0.
      - hunter (domain search): DERIVED too, and for the opposite reason — its price is not
        per row but one whole SEARCH credit per 10 emails returned, rounded up, with an empty
        domain free. `data.emails` is the only place that number exists.
      - hunter (email finder): DERIVED, the flat case — one whole SEARCH credit when an email is
        found, nothing on a miss ("a miss is free", per Hunter's own pricing), yet a miss still
        answers HTTP 200, so the estimate billed the full credit for a name Hunter had nothing on.
      - tikhub: REPORTED in prose rather than a number. Every envelope says whether the call is
        billed; only the explicit no-charge phrasing settles at zero, because TikHub really does
        charge for a 2xx whose payload is an embedded error (verified live 2026-07-30 — see
        docs/context/architecture/catalog.md, "the provider decides what counts as success").

    Everyone else settles at the estimate. This is the same signal the catalog's `observed_cost`
    harvests, which is what lets phase 5's drift detector compare the two numbers directly."""
    provider = mk.provider
    if not body:
        return None
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    if mk.billed_oauth and mk.cost_type == "per_result" and mk.unit_micro > 0:
        data = doc.get("data")
        n = len(data) if isinstance(data, list) else (1 if data else 0)
        return n * mk.unit_micro
    if provider == "dataforseo":
        cost = doc.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return int(cost * 1_000_000 + 0.5)
        return None
    if provider in ("scrapecreators", "akta", "leadmagic"):
        credits = doc.get("credits_charged" if provider == "scrapecreators" else "credits_consumed")
        rate = catalog_store.load().credit_rates.get(provider)
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "lusha":
        billing = doc.get("billing")
        credits = billing.get("creditsCharged") if isinstance(billing, dict) else None
        rate = catalog_store.load().credit_rates.get("lusha")
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "hunter" and mk.endpoint_id == "hunter.companies.emails":
        # DERIVED, like apollo. Hunter's domain search does not bill per row at all: it takes ONE
        # whole search credit per 10 emails RETURNED, rounded up, and a domain it knows nobody at is
        # free. Neither half of that rule survives being flattened into the catalog's per-row price
        # (1 credit ÷ 10 = $0.00245/result), so settling at the estimate is wrong in BOTH
        # directions — a search with no `limit` reserved the 20-row default page and settled a
        # ZERO-email answer at $0.0490, 20x the published per-result price for results nobody got,
        # while `limit=1` on a domain that did answer settled at $0.00245, a tenth of the credit
        # Hunter actually took. The returned list is the bill.
        data = doc.get("data")
        emails = data.get("emails") if isinstance(data, dict) else None
        rate = catalog_store.load().credit_rates.get("hunter")
        if isinstance(emails, list) and rate:
            credits = -(-len(emails) // 10)  # whole credits, rounded up; no emails = no charge
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "hunter" and mk.endpoint_id == "hunter.people.email.find":
        # DERIVED, the flat case of the same family: the finder takes ONE whole search credit when
        # it finds an email and nothing when it doesn't — the catalog note says "a miss is free" in
        # as many words, yet a miss still answers HTTP 200 with `email: null`, so settling at the
        # estimate billed the full credit ($0.0245) for a name Hunter had nothing on. A body
        # without the `email` key (an error shape) still falls back to the estimate.
        data = doc.get("data")
        rate = catalog_store.load().credit_rates.get("hunter")
        if isinstance(data, dict) and "email" in data and rate:
            return int(rate * 1_000_000 + 0.5) if data["email"] else 0
        return None
    if provider == "tikhub":
        # REPORTED in prose rather than a number: every TikHub envelope states whether the call is
        # billed. A 2xx whose payload is an embedded error still says "This request will incur a
        # charge." and TikHub really does charge us for it (verified live 2026-07-30 — see
        # docs/context/architecture/catalog.md, "the provider decides what counts as success"), so
        # a dead page settling at the estimate is faithful, not an over-charge. Only the explicit
        # no-charge phrasing settles at zero; anything else stays at the estimate.
        msg = doc.get("message")
        if isinstance(msg, str):
            low = msg.lower()
            if "won't be charged" in low or "will not be charged" in low or "not incur" in low:
                return 0
        return None
    if provider == "apollo":
        # Only the shapes whose billing rule is documented and body-decidable: company enrichment
        # (1 credit per organization returned, null on a miss) and company search (1 credit per
        # non-empty PAGE). A body carrying neither key — people enrichment's 1-9 credit range
        # included — falls through to the estimate rather than guessing.
        rate = catalog_store.load().credit_rates.get("apollo")
        if rate:
            for key in ("organization", "organizations"):
                if key in doc:
                    return int(rate * 1_000_000 + 0.5) if doc[key] else 0
        return None
    return None


async def _buffer_response(response: StreamingResponse) -> tuple[Response, bytes]:
    """Drain a relayed streaming response into memory and return an equivalent plain Response.

    Metered calls give up streaming on purpose: settling needs the provider's own reported cost (which
    lives in the body) and the telemetry row wants the response size, and neither can be known while
    the bytes are still in flight. These are JSON API answers — the same payloads the catalog stores as
    examples — so the memory cost is a few KB, and buffering happens BEFORE anything is sent to the
    caller, which is what lets a mid-stream upstream failure still become a clean 502 + release."""
    chunks, size = [], 0
    async for chunk in response.body_iterator:
        raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
        size += len(raw)
        if size <= _PLATFORM_BODY_MAX:
            chunks.append(raw)
    body = b"".join(chunks)
    if response.background is not None:  # the relay's upstream-close task — run it now, not later
        await response.background()
        response.background = None
    out = Response(content=body, status_code=response.status_code)
    # Carry the upstream's headers verbatim (the relay already dropped hop-by-hop + our own), with a
    # content-length that matches what we are actually about to send.
    out.raw_headers = [(k, v) for k, v in response.raw_headers if k.lower() != b"content-length"]
    out.raw_headers.append((b"content-length", str(len(body)).encode()))
    return out, body


async def _peek_stream_head(response: StreamingResponse, limit: int) -> tuple[StreamingResponse, bytes]:
    """Read at most ``limit`` response bytes for evidence, then replay every byte to the caller.

    Unmetered calls retain their streaming contract. The consumed chunks are yielded first by the
    replacement response, followed by the untouched iterator; the relay's upstream-close background
    task moves with it and therefore still runs after the caller finishes reading.
    """
    iterator = response.body_iterator.__aiter__()
    consumed: list[bytes] = []
    head = bytearray()
    while len(head) < limit:
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            break
        raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
        consumed.append(raw)
        head.extend(raw[:limit - len(head)])

    async def replay():
        for chunk in consumed:
            yield chunk
        async for chunk in iterator:
            yield chunk

    out = StreamingResponse(replay(), status_code=response.status_code,
                            background=response.background)
    response.background = None
    out.raw_headers = list(response.raw_headers)
    return out, bytes(head)


def _error_response_evidence(response: Response, body: bytes, secrets: list[str]) -> str:
    """Build the redacted provider half of a failed-call evidence row."""
    # Headers first: a 401 or 429 often has an empty or generic body, and `Retry-After` /
    # `WWW-Authenticate` / the rate-limit trio are then the entire diagnosis.
    hdrs = " ".join(f"{h}={response.headers[h]}" for h in _EVIDENCE_HEADERS
                    if response.headers.get(h))
    evidence = _redact_snippet(
        (f"[{hdrs}] " if hdrs else "") +
        _decode_error_body(body[:_ERROR_BODY_SLICE],
                           response.headers.get("content-encoding", ""),
                           response.headers.get("content-type", "")),
        secrets, _ERROR_RESPONSE_MAX)
    return evidence or "<no response body or headers>"


async def _platform_settle(
    mk: MarketplaceCall, status_code: int | None, body: bytes = b"", *, reason: str = ""
) -> tuple[int, int | None]:
    """Close the hold for a metered call → (charged_micro, observed_micro). `charged_micro` is what
    actually hit the org's balance (0 on a release) — the number the Activity feed must show, because
    the estimate alone over-reports a released call as spend.

    `status_code=None` means the provider never answered us (our own 4xx, an injection error, a network
    failure) — always a release, never a charge, whatever the endpoint's billing type says.

    Never raises: the caller already has their answer (or their error), and a ledger hiccup must not
    turn a served call into a 500. A hold that fails to close is not lost money either — the reaper
    releases it, which errs in the org's favour. Runs on its OWN session because the request's session
    may be mid-rollback from the very error we are releasing for."""
    if not mk.metered or not mk.call_id:
        return 0, None
    billable = status_code is not None and _platform_billable(status_code, mk.cost_type)
    observed = _observed_cost_micro(mk, body) if billable else None
    call_id, mk.call_id = mk.call_id, None  # closing is once-only, even if two paths try
    charged = 0
    try:
        async with session_maker() as db:
            if billable:
                charged = await ledger.settle(db, call_id, observed, meta={
                    "provider": mk.provider, "status_code": status_code, "cost_type": mk.cost_type,
                    "cost_source": "provider" if observed is not None else "estimate"})
            else:
                await ledger.release(db, call_id, reason=reason or f"not_billable_{status_code}",
                                     meta={"provider": mk.provider, "cost_type": mk.cost_type,
                                           "status_code": status_code})
    except Exception as exc:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.ledger").error(
            "settle/release failed for call %s (%s, status %s): %s",
            call_id, mk.endpoint_id, status_code, exc, exc_info=True)
    return charged, observed


async def _record_first_call(org_id: int) -> None:
    """Set Org.first_call_at once — the metric that decides whether a marketing channel is real (see
    marketing/landing/_measurement.md). A CONDITIONAL UPDATE, not read-then-write: concurrent first
    calls would both see NULL and both fire. Set for EVERY org (it is a product metric in its own
    right); adsconv.queue() itself no-ops for orgs with no ad_gclid, so the conversion side stays
    ad-attributed-only.

    Runs on its OWN session, same reason as _platform_settle: this fires after the response is built,
    while the request's `db` may still be mid-settlement (or mid-rollback from one), and a commit or
    rollback issued here would land on THAT transaction instead of this one. Never raises — a metric
    write must not turn a working proxied call into a 500."""
    try:
        async with session_maker() as db:
            result = await db.execute(
                update(Org)
                .where(Org.id == org_id, Org.first_call_at.is_(None))
                .values(first_call_at=_utcnow_naive())  # naive UTC — asyncpg rejects tz-aware here
            )
            if result.rowcount:
                org_row = await db.get(Org, org_id)
                if org_row is not None:
                    await adsconv.queue(db, org_row, adsconv.ACTION_FIRST_CALL)
                await db.commit()
    except Exception:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.adsconv").error(
            "first_call_at update/queue failed for org %s", org_id, exc_info=True)


async def _relay_live_demo(request: Request, upstream_url: str, key: str, visitor: str):
    """The sandbox's ONE real upstream call (the landing live wire). Deliberately narrower than
    relay(): form-encoded only, auth header built here from the env key (never from a sandbox
    secret), and `metadata[visitor]` is OVERRIDDEN server-side so the landing feed's name is
    always ours, whatever the caller put in the body."""
    from urllib.parse import parse_qsl, urlencode
    http: httpx.AsyncClient = request.app.state.http
    headers = {"Authorization": f"Bearer {key}"}
    content = None
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        pairs = [(k, v) for k, v in parse_qsl(body, keep_blank_values=True) if k != "metadata[visitor]"]
        pairs.append(("metadata[visitor]", visitor))
        content = urlencode(pairs)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = await http.request(request.method, upstream_url, params=request.query_params.multi_items(),
                           content=content, headers=headers)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@app.api_route(
    "/call/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def call_tool(
    rest: str,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
):
    # Identity for the refusal fallback in `_mark_treg_own_errors`: a raise anywhere below (unknown
    # tool, deny rule, daily cap) leaves this handler without an audit row, and the exception handler
    # is the one place every such refusal passes through — but it has no Caller of its own.
    request.state.call_identity = (caller.org_id, caller.email)
    # Faithful-relay: use the RAW request path, not Starlette's decoded path param. Decoding is
    # lossy — an encoded slash (`%2f`) in `rest` would become a real `/` and change the upstream
    # route (npm's scoped publish `PUT /@scope%2fname` 404s as `/@scope/name`). httpx preserves
    # valid percent-escapes, so the original bytes travel through to the upstream one-to-one.
    raw_path = request.scope.get("raw_path")
    if raw_path:
        _, sep, raw_rest = raw_path.decode("ascii", "replace").partition("/call/")
        if sep:
            rest = raw_rest
    # The caller's tags, parsed ONCE and read by everything below — the budgets, the ledger, the
    # idempotency scope and the audit row. Before the idempotency block on purpose: a malformed bag
    # must not burn the caller's label on its way to a 422.
    meta = _parse_call_meta(request, caller)
    # ONE id for this call, minted before anything can spend: it becomes the ledger's call_id on a
    # metered call, lands on the audit row, and goes back as X-Treg-Call-Id — so a builder can join
    # our records to theirs on a single value.
    call_ref = uuid.uuid4().hex
    # Blocked status and the per-tag call count, BEFORE the replay below: a blocked user must neither
    # take an idempotency lock nor be handed an answer this team cached before they were blocked.
    await _enforce_tag_budgets(caller, meta, db)
    # A retry the caller has labelled: answer it from what we already returned, before resolving
    # anything or reaching a provider. Nothing happens without the header, so a caller who sends none
    # sees exactly today's behaviour.
    # Scoped by the primary tag: two of a builder's users WILL both send `retry-1`, and without
    # this the second would be served the first's stored response.
    idem_key = _scoped_idempotency_key(_idempotency_key(request), meta)
    idem_fingerprint = ""
    if idem_key:
        idem_body = await request.body()
        idem_fingerprint = _request_fingerprint(
            request.method, rest, idem_body, request.url.query or "")
        replayed = await _replay_idempotent(idem_key, idem_fingerprint, caller, db)
        if replayed is not None:
            return replayed
        # Claim it now, before anything reaches a provider. Two retries can arrive together and both
        # miss the lookup above; the unique constraint is what makes the loser wait instead of making
        # a second upstream call. A check-then-act in Python would leave exactly the window this
        # feature exists to close — the same reasoning as the conditional UPDATE in ledger.reserve.
        if not await _claim_idempotent(idem_key, idem_fingerprint, rest, caller, db):
            raise HTTPException(status_code=409, detail=(
                f"a call with Idempotency-Key {_idem_display(idem_key)!r} is already in progress — retry shortly"))
        # Park it so a failure anywhere below can give the label back. Set AFTER the claim succeeds,
        # so losing the race above never releases the winner's row.
        request.state.idem_claim = (caller.membership.id, idem_key)

    drop_params: set[str] = set()
    mk: MarketplaceCall | None = None
    own_tool_miss: dict | None = None
    try:
        tool, upstream_url = await _resolve_call(rest, caller, db)
    except HTTPException as exc:
        # Not a tool → maybe a marketplace endpoint id (`treg call tikhub.tiktok.video.comments`).
        # Only the 404 falls through, so an org tool with the same name always wins.
        ep = _catalog_endpoint_for(rest) if exc.status_code == 404 else None
        if ep is None:
            raise
        if (isinstance(exc.detail, dict)
                and str(exc.detail.get("hint", "")).startswith("your org has tool ")):
            own_tool_miss = exc.detail
        try:
            mk = await _resolve_marketplace_call(ep, request, caller, db)
        except HTTPException as mkexc:
            # Catalog resolution is allowed to fall through from a named miss, but its own 404 must
            # not discard the useful fact discovered there: this org already has a nearby own tool.
            if mkexc.status_code == 404 and own_tool_miss is not None:
                mkexc.detail = {
                    "error": mkexc.detail,
                    "hint": own_tool_miss["hint"],
                    "did_you_mean": own_tool_miss["did_you_mean"],
                }
            # A malformed marketplace call (wrong method, missing param, no credential, 502) must
            # still leave a trace — it's exactly the row the caller will come asking about.
            request.state.call_audited = True
            audit.record_call(
                org_id=caller.org_id, user_email=caller.email, tool_name=ep["id"],
                method=request.method, path=rest, status_code=mkexc.status_code,
                client=_client_of(request), refused_by=_refusal_kind(mkexc.status_code),
                telemetry={"endpoint_id": ep["id"], "provider": ep.get("provider"),
                           **_tag_telemetry(meta)})
            analytics.capture(caller.email, "tool_called",
                {"tool_name": ep["id"], "status_code": mkexc.status_code,
                 "client": _client_of(request), "method": request.method,
                 "own_tool": False, "provider": ep.get("provider"), "endpoint_id": ep["id"]},
                groups={"team": caller.org.slug})
            raise
        tool, upstream_url, drop_params = mk.tool, mk.upstream, mk.consumed
    _require_tool_use(caller, tool)  # per-member tool + project ACL (NULL access = all; admins exempt)
    # Policy deny — evaluated on the RESOLVED upstream, so it sees the real host/path/method whichever
    # shape the caller used (named or URL-passthrough), and the relay never follows redirects, so a
    # blocked host can't be reached via a 3xx bounce.
    await _enforce_deny(caller, upstream_url, request.method, db, tool.project_id)
    await _enforce_daily_cap(caller, db)  # per-user daily cap (skips sandbox + unmetered members)
    if caller.org.public_demo and not _role_at_least(caller.role, "admin"):
        await _enforce_public_demo_ip_cap(request, db)  # shared token → meter by client IP, not user

    # The caller's own request bytes, read ONCE when it is safe to buffer them, so a failure can be
    # explained later (see models.CallRecord.error_request). Metered JSON calls already require full
    # buffering. Otherwise only a declared body at or below 64 KiB is cached; large and chunked uploads
    # keep streaming and still retain their query-param half if they fail. Starlette's request cache
    # lets relay stream the same bytes after this read.
    # Named `caller_body`: `body` in this function is the buffered RESPONSE, and confusing the two
    # would file the provider's answer as the caller's request.
    caller_body = b""
    content_length = request.headers.get("content-length")
    small_declared_body = False
    if content_length is not None:
        try:
            small_declared_body = 0 <= int(content_length) <= _ERROR_CALLER_BODY_MAX
        except ValueError:
            small_declared_body = False
    if _may_have_body(request) and ((mk is not None and mk.metered) or small_declared_body):
        try:
            caller_body = await request.body()
        except Exception:  # noqa: BLE001 — a caller that hung up must not become a 500 here
            caller_body = b""

    # Snapshot the audit identity NOW: a failed reserve rolls the session back, expiring the ORM
    # instances behind `caller` — reading them inside a later _audit would raise MissingGreenlet.
    audit_org_id, audit_email, audit_tool = caller.org_id, caller.email, tool.name
    audit_slug = caller.org.slug  # PostHog group key — must match the browser's posthog.group('team', slug)

    def _audit(status_code: int, *, observed_micro: int | None = None, charged_micro: int | None = None,
               duration_ms: int | None = None, response_bytes: int | None = None,
               refused_by: str | None = None,
               error_request: str | None = None, error_response: str | None = None) -> None:
        # Audit the attempt too — failures are results worth recording. A marketplace call additionally
        # carries its telemetry (which endpoint, which credential tier, what it cost): still
        # fire-and-forget, because the money itself already landed synchronously in the ledger.
        request.state.call_audited = True  # the refusal fallback in _mark_treg_own_errors stands down
        telemetry: dict = {"call_ref": call_ref}
        if meta.tags:
            # Own-tool calls carry tags too: a builder's usage report has to account for every call
            # their user made, not only the ones that spent treg's money.
            telemetry |= _tag_telemetry(meta)
        if mk is not None:
            telemetry |= {
                "endpoint_id": mk.endpoint_id, "provider": mk.provider, "credential_tier": mk.tier,
                # An org credential riding treg's pay-per-use OAuth app: tier stays tool/credential
                # (the credential IS theirs), this says who the upstream billed.
                **({"oauth_billed": True} if mk.billed_oauth else {}),
                "cost_estimated_micro": mk.estimate_micro or None,  # informational on tiers 1/2
                "cost_observed_micro": observed_micro,
                "cost_charged_micro": charged_micro,
                "duration_ms": duration_ms, "response_bytes": response_bytes,
                "params_hash": mk.params_hash,
            }
        # Sanctioned reversal of PR #139: failed own-key and own-tool calls now retain the same
        # redacted, admin-only, 14-day evidence as marketplace failures. Successes remain empty and
        # `/calls` still never exposes these columns.
        if error_request or error_response:
            telemetry |= {"error_request": error_request, "error_response": error_response}
        audit.record_call(
            org_id=audit_org_id, user_email=audit_email, tool_name=audit_tool,
            method=request.method, path=upstream_url, status_code=status_code,
            client=_client_of(request), refused_by=refused_by, telemetry=telemetry,
        )
        # Product analytics mirror of the row above. Deliberately excludes params, bodies, and the
        # full upstream URL (hostname only) — per-call detail beyond what a chart needs stays in the DB.
        props = {"tool_name": audit_tool, "status_code": status_code,
                 "client": _client_of(request), "method": request.method,
                 "own_tool": mk is None, "duration_ms": duration_ms}
        if mk is not None:
            props |= {"provider": mk.provider, "endpoint_id": mk.endpoint_id,
                      "tier": mk.tier, "metered": mk.metered, "cost_type": mk.cost_type,
                      "charged_micro": charged_micro, "observed_micro": observed_micro}
        else:
            props["provider"] = urlsplit(upstream_url).hostname or ""
        analytics.capture(audit_email, "tool_called", props, groups={"team": audit_slug})

    # Landing-page sandbox: never touch the network — EXCEPT the one live wire. A call to the
    # exact seeded stripe tool (fingerprint-matched; see sandbox.is_live_tool) relays to the real
    # Stripe test API with the env-held demo key. Any tampered/lookalike tool falls through to
    # synthesize below, so there is never a key to exfiltrate from a sandbox org.
    if demo_sandbox.is_sandbox(caller.org):
        live_key = get_settings().demo_stripe_key
        if live_key and demo_sandbox.is_live_tool(tool) and request.method in ("GET", "POST"):
            await _enforce_public_demo_ip_cap(request, db)  # one shared wire → meter by client IP
            try:
                response = await _relay_live_demo(
                    request, upstream_url, live_key, demo_sandbox.visitor_name(caller.org.slug))
            except httpx.RequestError as exc:
                _audit(502)
                raise HTTPException(status_code=502, detail=f"upstream request failed: {str(exc) or type(exc).__name__}")
            _audit(response.status_code)
            return response
        secrets = {}
        for sid in {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}:
            s = await db.get(Secret, sid)
            if s is not None and s.org_id == caller.org_id:
                secrets[sid] = s
        body = (await request.body()).decode("utf-8", "replace")
        result = demo_sandbox.synthesize(
            request.method, upstream_url, tool, secrets,
            query=request.query_params.multi_items(), body=body)
        _audit(200)
        return JSONResponse(result)

    # Load every secret the bindings need BEFORE the money gate (api does the DB work; proxy stays
    # I/O-free): whether this call is METERED can depend on the credential itself — a registry X
    # connect rides treg's pay-per-use app, so the org's "own" oauth secret is exactly what makes
    # the call billable. Nothing is reserved yet, so a load failure here leaves no hold behind.
    secrets: dict[int, Secret] = {}
    try:
        # A platform binding carries no secret_id — its value comes from settings at relay time.
        for sid in {b["secret_id"] for b in tool.bindings if b.get("secret_id") is not None}:
            secret = await db.get(Secret, sid)
            if secret is None or secret.org_id != caller.org_id:
                raise HTTPException(status_code=409, detail="a bound secret is missing")
            secrets[sid] = secret
    except HTTPException as exc:
        _audit(exc.status_code)  # record the failed attempt, same as a mid-relay refusal would
        raise
    billed_provider = _oauth_billed_provider(secrets)
    if billed_provider is not None:
        # The sandbox never reaches here (it returned above); the public demo could, and one shared
        # org must never be able to spend treg's upstream credits — refuse rather than relay free.
        if caller.org.public_demo:
            _audit(403)
            raise HTTPException(status_code=403, detail=(
                f"{billed_provider.display_name} calls are pay-per-use on treg's app and the "
                f"public demo can't spend — create your own team to use this"))
        mk = await _billed_marketplace(mk, billed_provider, tool, upstream_url, request)

    # Metered — treg's own money is about to be spent (tier 4's platform key, or a registry OAuth
    # connect on a pay-per-use app), so take the money FIRST. Deliberately the last gate before the
    # network: everything above (ACL, deny rules, caps) can still refuse the call, and a refused
    # call must not leave a hold behind for the reaper to clean up.
    if mk is not None and mk.metered:
        # Rendered BEFORE the reserve, while `tool` is still live. `ledger.reserve` calls
        # `db.rollback()` on InsufficientBalance, which EXPIRES every ORM object this session is
        # tracking — `tool` included — and reading an expired attribute outside an awaited call
        # raises MissingGreenlet. Doing it inside the handler below turned the one refusal an agent
        # is most likely to hit into a 500 with no `balance_micro` and no top-up URL. Same reasoning
        # as `block_id` in billing._credit, and the reason that capture is pinned by a test.
        refusal_secrets = _safe_secret_renderings(tool, secrets)
        try:
            await _platform_reserve(mk, caller, db, meta=meta, call_ref=call_ref)
        except HTTPException as exc:
            # A call refused for MONEY (402 empty balance / 429 daily cap) is the event the org will
            # ask about first — it must appear in the activity feed, charged 0.
            #
            # Keep the detail, because `cap` alone is not a diagnosis: every 429 maps to it, and that
            # covers a member call cap, a tag call or spend cap, the platform ceiling, a trial
            # allowance and a demo-IP limit. WHICH one is in `exc.detail` and was being discarded —
            # 878 refusals in a week that could not be told apart afterwards. This branch is inside
            # `mk.metered`, so it stays platform-only like every other capture site, and it runs
            # BEFORE relay, so no provider content can reach it.
            #
            # It is NOT free of caller data, though: a tag-cap detail carries the tag's `val` — an
            # end-customer id the builder supplied. That is the caller's own identifier, in the
            # caller's own row, and it is also the thing that makes the refusal diagnosable ("which
            # customer hit the cap"). It is strictly less than the request bodies this feature
            # already retains, and it is bounded by the same redaction and 14-day retention.
            _audit(exc.status_code, charged_micro=0,
                   refused_by="balance" if exc.status_code == 402 else "cap",
                   error_response=(
                       _ERROR_MASKING_FAILED if refusal_secrets is None else
                       _redact_snippet(f"treg: {exc.detail}", refusal_secrets,
                                       _ERROR_RESPONSE_MAX)))
            raise
    body = b""
    started = _now_ms()
    try:
        # treg keeps oauth tokens fresh: refresh in place if stale, before injecting. Inside the
        # try on purpose — a failed refresh after a reserve must release the hold (502 path below).
        for secret in secrets.values():
            try:
                await oauth.ensure_fresh(secret, db, request.app.state.http)
            except Exception as exc:  # noqa: BLE001 — surface a clear 502 instead of injecting a dead token
                raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
        try:
            response = await relay(request, upstream_url, tool, secrets, request.app.state.http,
                                   drop_params=drop_params or None,
                                   force_identity=mk is not None and mk.metered)
            if mk is not None and mk.metered:
                # Metered calls don't stream: settling needs the provider's own reported cost, which is
                # in the body (see _buffer_response). A failure while draining is still an upstream
                # failure, so it becomes a 502 and the hold goes back.
                response, body = await _buffer_response(response)
            elif response.status_code >= 400:
                # Preserve streaming for own-key and own-tool calls while retaining only the small
                # diagnostic head. The replacement response replays every consumed byte verbatim.
                response, body = await _peek_stream_head(response, _ERROR_BODY_SLICE)
        except ValueError as exc:  # a binding/injector mismatch (e.g. non-JSON secret on an oauth binding)
            raise HTTPException(status_code=502, detail=f"credential injection failed: {exc}")
        except httpx.RequestError as exc:  # upstream down/timeout is a gateway fault, not treg's 500
            raise HTTPException(status_code=502, detail=f"upstream request failed: {str(exc) or type(exc).__name__}")
    except HTTPException as exc:
        # The provider never produced a billable answer (our own error, a failed injection, an
        # unreachable upstream) → return the hold in full, regardless of the endpoint's billing type.
        metered = mk is not None and mk.metered
        if metered:
            await _platform_settle(mk, None, reason=f"call_failed_{exc.status_code}")
        # No provider body exists on this branch. treg's own detail is the explanation instead, and
        # it is the one worth keeping: this branch carries refresh, timeout, injection and SSRF 502s.
        _renderings = _safe_secret_renderings(tool, secrets)
        _audit(exc.status_code, charged_micro=0 if metered else None,
               duration_ms=_now_ms() - started,
               error_request=(
                   _ERROR_MASKING_FAILED if _renderings is None else
                   _caller_request_snippet(request, tool, caller_body, _renderings)),
               error_response=(
                   _ERROR_MASKING_FAILED if _renderings is None else
                   _redact_snippet(f"treg: {exc.detail}", _renderings, _ERROR_RESPONSE_MAX)))
        raise
    except Exception:  # noqa: BLE001 — an unexpected fault is still not the caller's bill
        # The reaper would eventually return this hold anyway; returning it now means a bug in the call
        # path can't make a funded org look broke for the next three minutes.
        if mk is not None and mk.metered:
            await _platform_settle(mk, None, reason="call_crashed")
        raise
    duration_ms = _now_ms() - started
    # First successful call. The common case — an org that already has one — is an in-memory check
    # against `caller.org` (freshly loaded this request by require_member): zero DB cost on a path
    # that runs on every proxied call. Only an org's actual first call touches the database, and it
    # does so via _record_first_call's own session, never the request's `db` (which _platform_settle,
    # right below, is about to settle/release — see its docstring for why that session is off-limits).
    if 200 <= response.status_code < 400 and caller.org_id and caller.org.first_call_at is None:
        await _record_first_call(caller.org_id)
    if mk is not None and mk.metered:
        charged, observed = await _platform_settle(mk, response.status_code, body)
        # A relayed non-2xx arrives HERE, as a Response — the vendor's own status is never raised
        # (see _refusal_kind). So this is where the provider's own explanation is captured, and the
        # only place it exists: nothing downstream keeps the body.
        err_request = err_response = None
        if response.status_code >= 400:
            _renderings = _safe_secret_renderings(tool, secrets)
            if _renderings is None:
                err_request = err_response = _ERROR_MASKING_FAILED
            else:
                err_request = _caller_request_snippet(request, tool, caller_body, _renderings)
                err_response = _error_response_evidence(response, body, _renderings)
        _audit(response.status_code, observed_micro=observed, charged_micro=charged,
               duration_ms=duration_ms, response_bytes=len(body),
               error_request=err_request, error_response=err_response)
        if idem_key:
            # Here, and not earlier: this is the first point where BOTH the response and what it
            # actually cost are known, and a replay has to hand back the real charge rather than the
            # estimate that was reserved.
            request.state.idem_claim = None      # dealt with; nothing left to release
            await _store_idempotent(idem_key, caller, status_code=response.status_code, body=body,
                                    media_type=response.headers.get("content-type", ""),
                                    charged_micro=charged, metered=True, call_ref=call_ref)
        # Tell the caller what the call actually cost. Both llms.txt and skill.md instruct an agent to
        # report the price it spent, and until now the only way to find out was to read the balance
        # before and after — which races with any other call and cannot attribute a figure to a
        # request. The header is set only on a METERED call: a team's own key is never charged, and a
        # `0` there would read as "free" rather than "not applicable".
        response.headers["X-Treg-Cost-Micro"] = str(charged)
        response.headers["X-Treg-Call-Id"] = call_ref
        return response
    # Fire-and-forget audit — does not block the streaming response (rule #2). A failed unmetered
    # call has already yielded just enough response bytes to retain redacted evidence; successes
    # still take the untouched streaming path.
    err_request = err_response = None
    if response.status_code >= 400:
        _renderings = _safe_secret_renderings(tool, secrets)
        if _renderings is None:
            err_request = err_response = _ERROR_MASKING_FAILED
        else:
            err_request = _caller_request_snippet(request, tool, caller_body, _renderings)
            err_response = _error_response_evidence(response, body, _renderings)
    _audit(response.status_code, duration_ms=duration_ms,
           error_request=err_request, error_response=err_response)
    if idem_key:
        # Unmetered: nothing was billed, so there is nothing to protect. Dropping the claim frees the
        # label at once instead of making the caller wait out the window to reuse it.
        request.state.idem_claim = None
        await _store_idempotent(idem_key, caller, status_code=response.status_code, body=b"",
                                media_type="", charged_micro=0, metered=False)
    response.headers["X-Treg-Call-Id"] = call_ref
    return response


# ---- server-side CLI execution (Tier 0 `treg run`) ---------------------------------------
class RunIn(BaseModel):
    tool: str             # the tool name in the caller's org (its `cli` profile drives execution)
    args: list[str] = []  # argv passed to the CLI (secrets are injected via env, never here)
    timeout_s: int | None = None


@app.post("/run")
async def run_tool_server(
    body: RunIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Run a tool's CLI **on the treg server**, with its `cli.inject` secrets injected into the
    child process — the caller never holds the key. Both run tiers read the same `Tool.cli`
    profile; any tool WITH a profile is server-runnable (no per-tool opt-in — unlike the local
    tier, the key never reaches the member, and the bin allow-list still gates what executes).
    See docs/CLI-RUN-PLAN.md.

    member+ (executing argv server-side is a register-tier capability, not a read); the sandbox is
    excluded (it never touches the real world). A non-zero CLI exit is a normal 200 result with
    `exit_code` set; only a failure to *start* (not enabled / CLI absent) is a 4xx."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="CLI run is disabled in the sandbox")
    tool = (
        await db.execute(select(Tool).where(Tool.name == body.tool, Tool.org_id == caller.org_id))
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no tool {body.tool!r} in this org")
    _require_tool_use(caller, tool)  # per-member tool + project ACL
    # A run executes a CLI, so there is no request path to match — evaluate the tool's own upstream
    # host, which is what a host-level rule ("nobody may reach api.stripe.com") is really saying.
    await _enforce_deny(caller, tool.base_url, "", db, tool.project_id)
    await _enforce_daily_cap(caller, db)  # a server run counts toward the per-user daily cap
    try:
        exec_bin = runner.resolve_exec_bin(tool)  # the SAME resolution run_tool execs — never diverges
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if exec_bin not in _allowed_server_bins():
        raise HTTPException(status_code=422, detail=(
            f"{exec_bin!r} is not approved for server runs — only catalog-known CLIs may run on the "
            "server (an admin can allow more via TREG_RUN_ALLOWED_BINS). Use `treg run --local` instead."))
    timeout = max(1, min(body.timeout_s or runner.DEFAULT_TIMEOUT_S, 600))
    try:
        async with runner.run_slot(caller.email):  # cap concurrent server runs (global + per-user)
            result = await runner.run_tool(tool, list(body.args), db, timeout_s=timeout)
    except runner.RunBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record_run(
        org_id=caller.org_id, user_email=caller.email, bundle_name=tool.name,
        argv=_redact_argv_list(list(body.args)),  # redact any credential typed inline before it's stored
        exit_code=result.exit_code, duration_ms=result.duration_ms, client=_client_of(request),
    )
    return {
        "tool": tool.name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }


# ---- view helpers (never leak secret values) ----------------------------------------------
def _secret_view(s: Secret) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "owner": s.owner, "bundle_id": s.bundle_id}


def _tool_view(t: Tool) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "owner": t.owner,
        "base_url": t.base_url,
        "host": t.host,
        "bindings": t.bindings,
        "health_check": t.health_check,
        "examples": t.examples or [],
        "cli": t.cli,
        # Server-computed so the dashboard never guesses: a run needs a cli profile, an allow-listed bin
        # (server config the client can't see), AND a server-injectable auth mechanism — a config_file /
        # device CLI authenticates from the member's own machine, so it's local-only (default "env" keeps
        # every pre-auth_mechanism tool server-runnable as before).
        "server_runnable": (bool(t.cli) and (t.cli.get("bin") or t.name) in _allowed_server_bins()
                            and (t.cli.get("auth_mechanism") or "env") in ("env", "argv")),
        "project_id": t.project_id,  # None = org-wide
        "bundle_id": t.bundle_id,
    }


async def _bundle_view(bundle_id: int, db: AsyncSession) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    tools = (await db.execute(select(Tool).where(Tool.bundle_id == bundle_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(Secret.bundle_id == bundle_id))).scalars().all()
    return {
        "id": bundle.id,
        "name": bundle.name,
        "owner": bundle.owner,
        "recipe": bundle.recipe,
        "files": bundle.files or {},   # companion files {relpath: content} — `skill install` writes these
        "tools": [_tool_view(t) for t in tools],
        "secrets": [_secret_view(s) for s in secrets],
    }


# ---------------------------------------------------------------------------------------------
# HEAD, everywhere GET is answered
# ---------------------------------------------------------------------------------------------
# FastAPI's APIRoute pins `methods` to {"GET"} and, unlike Starlette's plain Route, never adds HEAD
# (fastapi/routing.py: `if methods is None: methods = ["GET"]`). So every page on the site answered
# 405 to the HEAD probe that crawlers, link unfurlers and uptime checks send first — including `/`.
# Widened once, here, rather than by editing ~120 decorators: this runs after every route is
# registered, and only touches routes that are GET-only (a POST route keeps refusing HEAD, rightly).
#
# Sending the body is not a concern: ASGI servers drop it for HEAD per RFC 9110, and Starlette's
# FileResponse already checks the scope method and sends headers only.
_HEAD_WIDENED: list[APIRoute] = []
for _route in app.routes:
    if isinstance(_route, APIRoute) and _route.methods == {"GET"}:
        _route.methods = {"GET", "HEAD"}
        _HEAD_WIDENED.append(_route)


_fastapi_openapi = app.openapi


def _openapi_without_head():
    """The generated schema, minus the HEAD the loop above just added to every GET route.

    Without this the widening leaks into a PUBLIC artifact: FastAPI derives one operation per
    (path, method), so /openapi.json grew 58 duplicate HEAD entries — each warning "Duplicate
    Operation ID" at generation and each doubling its operation on the /docs page. HEAD is a
    transport detail that HTTP already implies wherever GET is answered; it is not an API operation
    anyone reads about. So the routes are narrowed for the duration of generation and put back.
    Routes that declared HEAD themselves (the /call proxy) are untouched and still documented.
    """
    if app.openapi_schema:
        return app.openapi_schema
    for r in _HEAD_WIDENED:
        r.methods = {"GET"}
    try:
        app.openapi_schema = _fastapi_openapi()
    finally:
        for r in _HEAD_WIDENED:
            r.methods = {"GET", "HEAD"}
    return app.openapi_schema


app.openapi = _openapi_without_head


# ---------------------------------------------------------------------------------------------
# The MCP front door
# ---------------------------------------------------------------------------------------------
# Mounted LAST so it cannot shadow a route, and guarded so a deploy without the `[server]` extra's
# `mcp` dependency still serves everything else. Its lifespan is entered in `lifespan` above — a
# mounted app's own lifespan never runs, and this one builds the transport's task group there.
if _mcp is not None:
    app.mount("/mcp", _mcp.mcp_app)
