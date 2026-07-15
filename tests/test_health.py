"""Phase B: credential health — probe each tool, refresh-test each oauth secret, alert owners.

The conftest upstream serves any path (200) and has /status/{code} via the echo catch-all only
returning 200, so we drive invalid cases through a real failure (a probe to a dead host) and
through an unrefreshable oauth secret.
"""

from __future__ import annotations

import json

from httpx import AsyncClient


async def test_probe_marks_ok(clients: AsyncClient):
    sid = (await clients.post("/secrets", json={"name": "k", "value": "V"})).json()["id"]
    await clients.post(
        "/tools",
        json={"name": "ok-tool", "base_url": "http://upstream", "secret_id": sid, "health_check": {"path": "ping"}},
    )
    report = (await clients.post("/health/run")).json()
    assert report["checked"] == 1
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["k"]["status"] == "ok"
    assert "HTTP 200" in health["k"]["detail"]


async def test_probe_failure_marks_invalid(clients: AsyncClient):
    sid = (await clients.post("/secrets", json={"name": "bad", "value": "V"})).json()["id"]
    # Force a failing verdict via an expect_status mismatch: the echo upstream answers 200, we
    # require 503, so _probe's expect-status branch returns "invalid".
    await clients.post(
        "/tools",
        json={"name": "bad-tool", "base_url": "http://upstream", "secret_id": sid,
              "health_check": {"path": "ping", "expect_status": 503}},  # echo returns 200 != 503
    )
    await clients.post("/health/run")
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["bad"]["status"] == "invalid"  # expected 503, got 200
    # persistence: a fresh read (new request/session) still shows the stored verdict
    health2 = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health2["bad"]["status"] == "invalid"


async def test_unrefreshable_oauth_secret_flagged_invalid(clients: AsyncClient):
    # oauth blob with refresh fields but a token_uri that 404s -> refresh fails -> invalid.
    blob = json.dumps({
        "access_token": "X", "refresh_token": "RT", "client_id": "c", "client_secret": "s",
        "token_uri": "http://upstream/nonexistent-token-endpoint", "expires_at": 0,
    })
    sid = (await clients.post("/secrets", json={"name": "oa", "kind": "oauth", "value": blob})).json()["id"]
    await clients.post("/tools", json={"name": "oa-tool", "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    await clients.post("/health/run")
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["oa"]["status"] == "invalid"
    assert "refresh failed" in health["oa"]["detail"]


async def test_oauth_refresh_ok_marks_ok(clients: AsyncClient):
    blob = json.dumps({
        "access_token": "X", "refresh_token": "RT", "client_id": "c", "client_secret": "s",
        "token_uri": "http://upstream/token", "expires_at": 0,  # /token returns a fresh token
    })
    sid = (await clients.post("/secrets", json={"name": "oa2", "kind": "oauth", "value": blob})).json()["id"]
    await clients.post("/tools", json={"name": "oa2-tool", "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    await clients.post("/health/run")
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["oa2"]["status"] == "ok"


async def test_owner_webhook_alerted_on_invalid(clients: AsyncClient):
    # register a second user WITH a webhook that the upstream RECORDS; their bad cred must trigger a
    # real POST to that webhook (not merely land in the invalid list).
    from treg.api import app
    r = await clients.post("/users", json={"email": "owner2@x.dev", "webhook_url": "http://upstream/hook"})
    tok = r.json()["token"]
    h2 = {"X-Treg-Token": tok}
    blob = json.dumps({"access_token": "X", "refresh_token": "RT", "client_id": "c", "client_secret": "s",
                       "token_uri": "http://upstream/nope", "expires_at": 0})
    sid = (await clients.post("/secrets", headers=h2, json={"name": "o2", "kind": "oauth", "value": blob})).json()["id"]
    await clients.post("/tools", headers=h2, json={"name": "o2-tool", "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    report = (await clients.post("/health/run", headers=h2)).json()
    assert any(i["name"] == "o2" and i["owner"] == "owner2@x.dev" for i in report["invalid"])
    # the webhook actually fired (this is what the test exists to prove)
    assert any(hit.get("owner") == "owner2@x.dev" for hit in app.state.hook_hits)


async def test_team_org_invalid_credential_still_alerts(clients: AsyncClient):
    # A team org's memberships carry no webhook_url (only the personal org gets one at registration);
    # an invalid team credential must still alert via the org owner's webhook fallback.
    from treg.api import app
    owner = await clients.post("/users", json={"email": "team-owner@x.dev", "webhook_url": "http://upstream/hook"})
    ot = owner.json()["token"]
    team = (await clients.post("/orgs", headers={"X-Treg-Token": ot}, json={"name": "Team"})).json()
    th = {"X-Treg-Token": team["token"]}  # a team-org token — its membership has NO webhook_url
    blob = json.dumps({"access_token": "X", "refresh_token": "RT", "client_id": "c", "client_secret": "s",
                       "token_uri": "http://upstream/nope", "expires_at": 0})
    sid = (await clients.post("/secrets", headers=th, json={"name": "tc", "kind": "oauth", "value": blob})).json()["id"]
    await clients.post("/tools", headers=th, json={"name": "tc-tool", "base_url": "http://upstream", "secret_id": sid, "injector": "oauth"})
    await clients.post("/health/run", headers=th)
    assert any(hit.get("owner") == "team-owner@x.dev" for hit in app.state.hook_hits)  # fell back to the org owner


async def test_malformed_health_check_does_not_500_the_batch(clients: AsyncClient):
    """A single tool in a bad state (here: a health_check stored as a string, not a dict) must NOT
    crash the whole batch — run_all isolates each tool and marks it 'unknown'. Regression: prod
    `treg health --run` 500'd batch-wide on one weird tool."""
    from sqlmodel import update
    from treg.db import session_maker
    from treg.models import Tool
    good = (await clients.post("/secrets", json={"name": "good", "value": "V"})).json()["id"]
    bad = (await clients.post("/secrets", json={"name": "weird", "value": "V"})).json()["id"]
    await clients.post("/tools", json={"name": "healthy", "base_url": "http://upstream",
                                       "secret_id": good, "health_check": {"path": "ping"}})
    await clients.post("/tools", json={"name": "broken", "base_url": "http://upstream", "secret_id": bad})
    async with session_maker() as s:  # poke a malformed (string) health_check straight into the row
        await s.execute(update(Tool).where(Tool.name == "broken").values(health_check="not-a-dict"))
        await s.commit()
    r = await clients.post("/health/run")
    assert r.status_code == 200  # was 500 — the batch now survives the bad tool
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["good"]["status"] == "ok"       # the healthy tool still validated in the same run
    assert health["weird"]["status"] == "unknown"  # the bad tool contained, not crashed
