"""Shareable detail pages: name-keyed lookups (/bundles/by-name, /tools/by-name) and the
SPA deep-link routes (/app/skills/<name>, /app/tools/<name>) with per-resource og meta.

These back the share flow: every CLI registration prints the page URL; the page previews
the resource and carries the agent install prompt.
"""

from __future__ import annotations

from httpx import AsyncClient


SKILL = {
    "name": "intercom",
    "recipe": "# intercom\nTalk to the Intercom API.\n",
    "files": {"reference/fields.md": "# fields\n", "scripts/run.py": "print('hi')\n"},
    "secrets": [{"local_name": "key", "kind": "env", "value": "SECRET-123"}],
    "tools": [
        {
            "name": "intercom",
            "base_url": "http://upstream",
            "bindings": [{"secret": "key", "injector": "env", "location": "header",
                          "name": "Authorization", "format": "Bearer {secret}"}],
        }
    ],
}


async def test_bundle_by_name_returns_full_view(clients: AsyncClient):
    await clients.post("/skills", json=SKILL)
    r = await clients.get("/bundles/by-name/intercom")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["name"] == "intercom"
    assert b["recipe"].startswith("# intercom")
    assert set(b["files"]) == {"reference/fields.md", "scripts/run.py"}  # nested paths preserved
    assert b["tools"][0]["name"] == "intercom"
    assert "SECRET-123" not in r.text  # secret VALUES never appear in a bundle view


async def test_tool_by_name_returns_tool_view(clients: AsyncClient):
    await clients.post("/skills", json=SKILL)
    r = await clients.get("/tools/by-name/intercom")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["base_url"] == "http://upstream"
    assert t["bundle_id"] is not None  # links back to its parent skill for the detail page


async def test_by_name_404s(clients: AsyncClient):
    assert (await clients.get("/bundles/by-name/ghost")).status_code == 404
    assert (await clients.get("/tools/by-name/ghost")).status_code == 404


async def test_by_name_is_org_scoped(clients: AsyncClient):
    await clients.post("/skills", json=SKILL)
    r = await clients.post("/users", json={"email": "stranger@elsewhere.dev"})
    other = {"X-Treg-Token": r.json()["token"]}
    assert (await clients.get("/bundles/by-name/intercom", headers=other)).status_code == 404
    assert (await clients.get("/tools/by-name/intercom", headers=other)).status_code == 404


async def test_detail_page_serves_spa_with_og_meta(clients: AsyncClient):
    r = await clients.get("/app/skills/intercom")  # unauthenticated is fine — the page itself gates on login
    assert r.status_code == 200
    assert 'og:title' in r.text and "intercom" in r.text
    r = await clients.get("/app/tools/stripe")
    assert r.status_code == 200
    assert 'og:title' in r.text and "shared tool" in r.text


async def test_detail_page_escapes_name(clients: AsyncClient):
    r = await clients.get('/app/skills/%3Cscript%3Ealert(1)%3C-x')
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text  # the echoed name is HTML-escaped
