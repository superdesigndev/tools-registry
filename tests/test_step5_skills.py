"""Step 5: the bundle (skill) composer + multi-binding tools.

A skill registers atomically as a bundle = recipe + secrets + tool(s). Bindings reference
secrets by local_name. The google-ads shape (one request needing OAuth bearer + a
developer-token header) is the motivating multi-binding case.
"""

from __future__ import annotations

from httpx import AsyncClient


GOOGLE_ADS_SKILL = {
    "name": "google-ads",
    "recipe": "# google-ads\nRun GAQL against the Ads API.\n",
    "secrets": [
        {"local_name": "oauth", "kind": "oauth", "value": '{"access_token": "OAUTH-AT"}'},
        {"local_name": "dev", "kind": "env", "value": "DEV-TOKEN-123"},
    ],
    "tools": [
        {
            "name": "google-ads",
            "base_url": "http://upstream",
            "bindings": [
                {"secret": "oauth", "injector": "oauth", "location": "header", "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token"},
                {"secret": "dev", "injector": "env", "location": "header", "name": "developer-token", "format": "{secret}"},
            ],
        }
    ],
}


async def test_register_skill_creates_bundle_secrets_tools(clients: AsyncClient):
    r = await clients.post("/skills", json=GOOGLE_ADS_SKILL)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["name"] == "google-ads"
    assert b["recipe"].startswith("# google-ads")
    assert {s["name"] for s in b["secrets"]} == {"oauth", "dev"}
    assert len(b["tools"]) == 1
    assert len(b["tools"][0]["bindings"]) == 2
    # secrets are scoped to the bundle
    assert all(s["bundle_id"] == b["id"] for s in b["secrets"])


async def test_multi_binding_call_injects_all_credentials(clients: AsyncClient):
    await clients.post("/skills", json=GOOGLE_ADS_SKILL)
    r = await clients.get("/call/google-ads/echo")
    assert r.status_code == 200, r.text
    h = r.json()["headers"]
    assert h["authorization"] == "Bearer OAUTH-AT"   # binding 1 (OAuth token extracted from JSON)
    assert h["developer-token"] == "DEV-TOKEN-123"   # binding 2 (plain header) — both on ONE request


async def test_skill_unknown_secret_ref_rejected(clients: AsyncClient):
    bad = {
        "name": "broken",
        "secrets": [{"local_name": "a", "value": "x"}],
        "tools": [{"name": "broken-t", "base_url": "http://upstream", "bindings": [{"secret": "ghost", "injector": "env"}]}],
    }
    r = await clients.post("/skills", json=bad)
    assert r.status_code == 422


async def test_bundle_get_and_delete_cascades(clients: AsyncClient):
    bid = (await clients.post("/skills", json=GOOGLE_ADS_SKILL)).json()["id"]
    assert (await clients.get(f"/bundles/{bid}")).status_code == 200

    d = await clients.delete(f"/bundles/{bid}")
    assert d.status_code == 200
    assert (await clients.get(f"/bundles/{bid}")).status_code == 404
    # its tool + secrets are gone
    assert all(t["name"] != "google-ads" for t in (await clients.get("/tools")).json())
    assert all(s["name"] not in {"oauth", "dev"} for s in (await clients.get("/secrets")).json())


async def test_recipe_only_bundle_round_trips(clients: AsyncClient):
    """A recipe-only skill (no secrets/tools) registers as a bundle and its recipe reads back exactly
    — the basis for `treg skill install` distributing knowledge skills."""
    recipe = "---\nname: seo-writer\n---\n# SEO writer\nWrite great blogs.\n"
    r = await clients.post("/skills", json={"name": "seo-writer", "recipe": recipe})
    assert r.status_code == 200, r.text
    bundle_id = r.json()["id"]
    assert r.json()["tools"] == [] and r.json()["secrets"] == []
    got = await clients.get(f"/bundles/{bundle_id}")
    assert got.status_code == 200
    assert got.json()["recipe"] == recipe   # byte-exact round-trip (what install writes to SKILL.md)


async def test_skill_tool_health_check_is_persisted_and_returned(clients: AsyncClient):
    """A skill tool's health_check must round-trip through POST /skills → GET /bundles/{id} (it was
    stored but omitted from _tool_view, so it read back as None)."""
    skill = {"name": "hc", "recipe": "# hc",
             "secrets": [{"local_name": "k", "kind": "env", "value": "V"}],
             "tools": [{"name": "hc", "base_url": "http://up",
                        "bindings": [{"secret": "k", "injector": "env", "location": "header",
                                      "name": "Authorization", "format": "Bearer {secret}"}],
                        "health_check": {"path": "ping"}}]}
    r = await clients.post("/skills", json=skill)
    assert r.status_code == 200, r.text
    bundle = await clients.get(f"/bundles/{r.json()['id']}")
    assert bundle.json()["tools"][0]["health_check"] == {"path": "ping"}


# ---- companion files: a whole skill folder travels, not just SKILL.md ----------------------
async def test_skill_files_roundtrip_and_sanitize(clients: AsyncClient):
    skill = {**GOOGLE_ADS_SKILL, "files": {
        "gaql.py": "print('run')\n",                 # a script
        "reference/fields.md": "# Fields\n",          # a NESTED reference doc
        "SKILL.md": "should be dropped (that's recipe)",
        ".secrets/token.json": "SECRET",              # must NEVER be stored
        "../escape.txt": "traversal",                 # path-traversal → dropped
    }}
    bid = (await clients.post("/skills", json=skill)).json()["id"]
    files = (await clients.get(f"/bundles/{bid}")).json()["files"]
    assert files == {"gaql.py": "print('run')\n", "reference/fields.md": "# Fields\n"}  # only the safe companions


def test_collect_files_excludes_secrets_junk_and_recipe(tmp_path):
    from treg import skills as sk
    (tmp_path / "SKILL.md").write_text("# recipe")
    (tmp_path / "gaql.py").write_text("code")
    (tmp_path / "reference").mkdir(); (tmp_path / "reference" / "f.md").write_text("doc")
    (tmp_path / ".secrets").mkdir(); (tmp_path / ".secrets" / "token.json").write_text("SECRET")
    (tmp_path / "treg.json").write_text("{}")
    (tmp_path / ".DS_Store").write_text("junk")
    got = sk.collect_files(tmp_path)
    assert got == {"gaql.py": "code", "reference/f.md": "doc"}  # no SKILL.md/secrets/treg.json/junk


def test_write_bundle_files_rejects_escaping_paths(tmp_path):
    from treg import cli
    dest = tmp_path / "skills" / "myskill"
    dest.mkdir(parents=True)
    n = cli._write_bundle_files(dest, {
        "ref/a.md": "ok",
        "../../evil.txt": "nope",
        "/etc/passwd": "nope",
    })
    assert n == 1 and (dest / "ref" / "a.md").read_text() == "ok"
    assert not (tmp_path / "evil.txt").exists()
