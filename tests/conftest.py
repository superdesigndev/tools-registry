"""Shared test fixtures.

The "upstream" is a tiny in-process ASGI echo app. The registry's shared httpx client is
pointed at it via ASGITransport, so the relay path runs for real, just without a socket.
The `clients` fixture also registers a user and authes the client by default.
"""

from __future__ import annotations

import os

# Isolate the test DB from any .env / running dev server BEFORE importing treg (the engine is
# built at import time). A real env var overrides the .env file in pydantic-settings.
os.environ["TREG_DATABASE_URL"] = "sqlite+aiosqlite:///./treg-test.db"
os.environ["TREG_EMAIL_DEV_MODE"] = "true"  # tests need the returned OTP code (prod default is now False)
os.environ["TREG_RESEND_API_KEY"] = ""  # never fire a real Resend send from the test suite (send_otp/send_invite skip when empty)
os.environ["TREG_RUN_ALLOWED_BINS"] = "sh,echo,true,false,cat,sleep,treg-nonexistent-bin-xyz"  # allow the test CLIs for --server run tests
os.environ["TREG_PROXY_SSRF_CHECK"] = "false"  # the test upstream is an in-process ASGI transport, not real DNS

import pytest  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from treg.api import app  # noqa: E402
from treg.db import reset_db  # noqa: E402


# The OTP-start + sandbox throttles (and the OTP codes) now live in the DB's `ephemeral` table, not in
# process-global dicts — so `reset_db()` (called by every client fixture) already clears them between
# tests. No separate rate-limit reset fixture is needed.


def make_upstream(hook_hits: list | None = None) -> FastAPI:
    up = FastAPI()

    @up.post("/token")
    async def token() -> dict:
        # stand-in OAuth token endpoint: serves both refresh + authorization_code exchanges.
        return {"access_token": "REFRESHED", "refresh_token": "NEW-RT", "expires_in": 3600}

    @up.post("/hook")
    async def hook(request: Request) -> dict:
        # records health webhook POSTs so alerting tests can assert the webhook actually fired.
        if hook_hits is not None:
            hook_hits.append(await request.json())
        return {"ok": True}

    @up.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def echo(request: Request) -> dict:
        body = (await request.body()).decode()
        return {
            "auth": request.headers.get("authorization"),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "query": dict(request.query_params),
            "query_multi": request.query_params.multi_items(),  # preserves duplicate keys
            "body": body,
            "raw_path": request.scope.get("raw_path", b"").decode(),  # pre-decode bytes, for %2f fidelity asserts
        }

    return up


@pytest.fixture
async def clients():
    await reset_db()
    app.state.hook_hits = []  # webhook POSTs the upstream received (for alerting assertions)
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream(app.state.hook_hits)), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.post("/users", json={"email": "tim@superdesign.dev"})  # open registration
        assert r.status_code == 200, r.text
        c.headers["X-Treg-Token"] = r.json()["token"]  # authed by default from here on
        yield c
    await app.state.http.aclose()
