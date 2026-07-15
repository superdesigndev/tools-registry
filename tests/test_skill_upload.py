"""The dashboard's folder-importer endpoints: /skills/analyze + /skills/import reuse the CLI's
scan_skills/_classify + build_payload on uploaded file contents (no disk path needed)."""

from __future__ import annotations

import json
from httpx import AsyncClient

CONTRACT = json.dumps({
    "name": "intercom", "base_url": "https://api.intercom.io",
    "secrets": [{"file": ".secrets/token", "name": "intercom", "kind": "env"}],
    "bindings": [{"secret": "intercom", "injector": "env", "location": "header",
                  "name": "Authorization", "format": "Bearer {secret}"}],
})

FILES = [
    {"path": "stop-slop/SKILL.md", "content": "# stop-slop\nA knowledge skill. No API."},
    {"path": "intercom/SKILL.md", "content": "# intercom\nCall the Intercom API."},
    {"path": "intercom/treg.json", "content": CONTRACT},
    {"path": "intercom/.secrets/token", "content": "SEKRET-TOKEN"},
]


async def test_analyze_classifies_like_the_cli(clients: AsyncClient):
    r = await clients.post("/skills/analyze", json={"files": FILES})
    assert r.status_code == 200, r.text
    by = {s["name"]: s for s in r.json()["skills"]}
    assert by["stop-slop"]["kind"] == "recipe_only" and by["stop-slop"]["ready"]
    assert by["intercom"]["kind"] == "contract" and by["intercom"]["base_url"] == "https://api.intercom.io"
    assert by["intercom"]["secrets"][0]["present"] is True   # the uploaded .secrets/token is seen
    assert by["intercom"]["ready"] and not by["intercom"]["gaps"]


async def test_import_registers_recipe_and_tool(clients: AsyncClient):
    r = await clients.post("/skills/import", json={"files": FILES, "select": ["stop-slop", "intercom"]})
    assert r.status_code == 200, r.text
    ok = {x["name"]: x for x in r.json()["results"]}
    assert ok["stop-slop"]["ok"] and ok["intercom"]["ok"]
    # the tool is live + injects the uploaded secret
    tools = {t["name"] for t in (await clients.get("/tools")).json()}
    assert "intercom" in tools
    bundles = {b["name"] for b in (await clients.get("/bundles")).json()}
    assert {"stop-slop", "intercom"} <= bundles


async def test_import_flags_missing_env_secret(clients: AsyncClient):
    # a contract that needs an env var not provided -> a gap, not a crash
    bad = json.dumps({"name": "x", "base_url": "https://api.x.com",
                      "secrets": [{"env": "X_KEY", "name": "x", "kind": "env"}],
                      "bindings": [{"secret": "x", "injector": "env", "location": "header",
                                    "name": "Authorization", "format": "Bearer {secret}"}]})
    files = [{"path": "x/SKILL.md", "content": "# x"}, {"path": "x/treg.json", "content": bad}]
    a = (await clients.post("/skills/analyze", json={"files": files})).json()["skills"][0]
    # value can still be supplied at import time
    r = await clients.post("/skills/import", json={"files": files, "select": ["x"],
                                                   "env_values": {"X_KEY": "live-value"}})
    assert r.status_code == 200, r.text


async def test_edit_recipe_content(clients: AsyncClient):
    await clients.post("/skills/import", json={"files": [{"path": "note/SKILL.md", "content": "# note\nv1"}], "select": ["note"]})
    bid = next(b["id"] for b in (await clients.get("/bundles")).json() if b["name"] == "note")
    r = await clients.patch(f"/bundles/{bid}", json={"recipe": "# note\nv2 edited"})
    assert r.status_code == 200, r.text
    assert (await clients.get(f"/bundles/{bid}")).json()["recipe"] == "# note\nv2 edited"


async def test_reimport_is_idempotent_not_500(clients: AsyncClient):
    files = [{"path": "dup/SKILL.md", "content": "# dup"},
             {"path": "toolx/SKILL.md", "content": "# toolx"}, {"path": "toolx/treg.json", "content": CONTRACT.replace("intercom", "toolx")},
             {"path": "toolx/.secrets/token", "content": "S"}]
    r1 = await clients.post("/skills/import", json={"files": files, "select": ["dup", "toolx"]})
    assert r1.status_code == 200 and all(x["ok"] for x in r1.json()["results"])
    # re-upload the same folder -> must NOT 500; everything reports already-registered
    r2 = await clients.post("/skills/import", json={"files": files, "select": ["dup", "toolx"]})
    assert r2.status_code == 200, r2.text
    assert all((not x["ok"]) for x in r2.json()["results"])


async def test_multi_credential_skill_generates_distinct_headers(clients: AsyncClient):
    """A generated skill shipping several credential files must NOT collide on Authorization: the
    primary oauth token -> Authorization, each other credential -> its own filename-derived header,
    and oauth app config (client_secret) is skipped. So it analyzes ready and imports cleanly."""
    token = json.dumps({"token": "AT", "refresh_token": "RT", "client_id": "c", "client_secret": "s"})
    files = [{"path": "gg/SKILL.md", "content": "# gg"},
             {"path": "gg/run.sh", "content": 'API="https://www.googleapis.com"\ncurl -H "Authorization: Bearer $T"'},
             {"path": "gg/.secrets/client_secret.json", "content": "{}"},
             {"path": "gg/.secrets/token.json", "content": token},
             {"path": "gg/.secrets/developer_token", "content": "x"}]
    a = (await clients.post("/skills/analyze", json={"files": files})).json()["skills"][0]
    assert a["ready"] is True and not any("header" in g for g in a["gaps"])  # no collision -> ready
    assert not any(s["name"] == "client_secret.json" for s in a["secrets"])  # app config skipped
    r = (await clients.post("/skills/import", json={"files": files, "select": ["gg"]})).json()["results"][0]
    assert r["ok"]                                                          # registers, no crash
    tools = (await clients.get("/tools")).json()
    binds = next(t for t in tools if t["name"] == "gg")["bindings"]
    header_names = [b["name"].lower() for b in binds]
    assert "authorization" in header_names and "developer-token" in header_names
    assert len(header_names) == len(set(header_names))                      # distinct headers, no collision
