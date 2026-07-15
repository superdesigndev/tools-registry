"""The skill scaffolder: deterministic discovery of recipe + secrets from a skill dir."""

from __future__ import annotations

import json

import pytest

from treg.convert import (
    FILL,
    contract_to_skill_payload,
    find_secret_file,
    generate_contract,
    load_contract,
    resolve_secret_path,
    scaffold_skill,
)


def test_resolve_secret_path_tolerates_secret_dir_spelling(tmp_path):
    # contract written against `.secret/`, file actually lives under `.secrets/` (and vice versa)
    d = tmp_path / "skill"
    (d / ".secrets").mkdir(parents=True)
    (d / ".secrets" / "token.json").write_text("{}")
    assert resolve_secret_path(d, ".secret/token.json") == d / ".secrets" / "token.json"
    assert resolve_secret_path(d, ".secrets/token.json") == d / ".secrets" / "token.json"
    # exact match wins when present; a genuinely missing file returns the exact (missing) path
    assert resolve_secret_path(d, ".secret/nope.json") == d / ".secret" / "nope.json"


def _make_skill(tmp_path):
    d = tmp_path / "google-ads"
    (d / ".secret").mkdir(parents=True)
    (d / "SKILL.md").write_text("# google-ads\nrun GAQL\n")
    (d / ".secret" / "developer_token").write_text("DEV123")
    (d / ".secret" / "token.json").write_text(json.dumps({"access_token": "AT", "refresh_token": "RT"}))
    (d / ".secret" / "client_secret.json").write_text(json.dumps({"installed": {"client_id": "x"}}))
    return d


def test_scaffold_discovers_recipe_and_secrets(tmp_path):
    m = scaffold_skill(_make_skill(tmp_path))
    assert m["name"] == "google-ads"
    assert m["recipe"].startswith("# google-ads")

    by_name = {s["local_name"]: s for s in m["secrets"]}
    assert by_name["developer_token"]["kind"] == "env"          # plain string
    assert by_name["token.json"]["kind"] == "oauth"             # JSON w/ refresh_token
    assert by_name["client_secret.json"]["kind"] == "secret_file"  # JSON w/o refresh_token
    assert by_name["developer_token"]["value"] == "DEV123"      # real contents captured


def test_scaffold_leaves_base_url_and_extra_bindings_for_the_agent(tmp_path):
    m = scaffold_skill(_make_skill(tmp_path))
    tool = m["tools"][0]
    assert tool["base_url"].startswith(FILL)          # agent must set the upstream
    assert tool["bindings"][0]["name"] == "Authorization"  # first binding is a sensible default
    assert any(b["name"] == FILL for b in tool["bindings"][1:])  # others flagged for completion


def test_find_secret_file_by_kind(tmp_path):
    d = _make_skill(tmp_path)
    assert find_secret_file(d, "oauth").name == "token.json"       # the token blob, not client_secret
    assert find_secret_file(d, "secret_file").name == "token.json"
    assert find_secret_file(d, "env").name == "developer_token"    # the plain-text file


def test_find_secret_file_no_match(tmp_path):
    d = tmp_path / "envonly"
    (d / ".secret").mkdir(parents=True)
    (d / ".secret" / "key").write_text("PLAINKEY")
    assert find_secret_file(d, "env").name == "key"
    with pytest.raises(FileNotFoundError):
        find_secret_file(d, "oauth")   # no JSON token blob present


def test_find_secret_file_ambiguous(tmp_path):
    d = tmp_path / "twoplain"
    (d / ".secret").mkdir(parents=True)
    (d / ".secret" / "a").write_text("A")
    (d / ".secret" / "b").write_text("B")
    with pytest.raises(ValueError, match="ambiguous"):
        find_secret_file(d, "env")     # two plain-text files -> must pass --file


# ---- treg.json contract -------------------------------------------------------------------
def test_generate_contract_single_secret_guesses_base_url(tmp_path):
    d = tmp_path / "helpdesk"  # a name NOT in the catalog, so the base_url guess heuristic still runs
    (d / ".secrets").mkdir(parents=True)
    (d / "SKILL.md").write_text("Intercom REST. See https://api.intercom.io/conversations and docs at https://developers.intercom.com/x")
    (d / ".secrets" / "token").write_text("TOK")
    c = generate_contract(d)
    assert c["base_url"] == "https://api.intercom.io"          # api host, not the docs host
    assert c["secrets"] == [{"file": ".secrets/token", "name": "helpdesk", "kind": "env"}]
    assert c["bindings"][0]["secret"] == "helpdesk" and c["bindings"][0]["format"] == "Bearer {secret}"
    assert any("base_url" in note for note in c["_fill"])      # heuristic -> flagged to verify


def test_generate_contract_skips_oauth_app_config(tmp_path):
    d = tmp_path / "gsc"
    (d / ".secrets").mkdir(parents=True)
    (d / "SKILL.md").write_text("no url here")
    # google-style token.json: access token under `token`
    (d / ".secrets" / "token.json").write_text(json.dumps({"token": "AT", "refresh_token": "RT"}))
    (d / ".secrets" / "client_secret.json").write_text(json.dumps({"web": {"client_id": "x"}}))
    c = generate_contract(d)
    assert c["base_url"] == "https://searchconsole.googleapis.com"  # curated catalog host (skill name "gsc")
    assert not any("NOT FOUND" in n for n in c["_fill"])            # catalog supplied the host
    # client_secret.json is oauth *app* config, never a request credential -> skipped entirely
    assert [s["name"] for s in c["secrets"]] == ["gsc"]
    assert len(c["bindings"]) == 1
    oauth_b = c["bindings"][0]
    assert oauth_b["injector"] == "oauth" and oauth_b["name"] == "Authorization"
    assert oauth_b["secret_field"] == "token"                   # detected Google shape
    assert not any("multiple credentials" in n for n in c["_fill"])  # only one cred after the skip


def test_generate_contract_multi_credential_distinct_headers(tmp_path):
    # google-ads shape: an oauth token + a developer token + oauth app config
    d = _make_skill(tmp_path)  # dir "google-ads"; .secret/{developer_token, token.json, client_secret.json}
    c = generate_contract(d)
    assert c["base_url"] == "https://googleads.googleapis.com"    # curated catalog host, not a guess
    names = {s["name"] for s in c["secrets"]}                    # secret name = file stem
    assert names == {"developer_token", "token"}                 # client_secret.json (app config) skipped
    by_secret = {b["secret"]: b for b in c["bindings"]}
    # primary oauth token -> Authorization: Bearer; the developer token -> its OWN header (no collision)
    assert by_secret["token"]["name"] == "Authorization"
    assert by_secret["token"]["injector"] == "oauth"
    assert by_secret["developer_token"]["name"] == "developer-token"
    assert by_secret["developer_token"]["format"] == "{secret}"
    header_names = [b["name"].lower() for b in c["bindings"]]
    assert len(header_names) == len(set(header_names))           # NO duplicate header -> no collision


def test_contract_roundtrip_to_payload(tmp_path):
    d = tmp_path / "svc"
    (d / ".secrets").mkdir(parents=True)
    (d / "SKILL.md").write_text("recipe body")
    (d / ".secrets" / "token").write_text("SEKRET")
    c = generate_contract(d)
    c["base_url"] = "https://api.svc.com"      # user fills the heuristic
    c["health"] = {"path": "me"}
    (d / "treg.json").write_text(json.dumps(c))
    loaded = load_contract(d)
    payload = contract_to_skill_payload(d, loaded)
    assert payload["name"] == "svc" and payload["recipe"] == "recipe body"
    assert payload["secrets"][0] == {"local_name": "svc", "value": "SEKRET", "kind": "env"}
    tool = payload["tools"][0]
    assert tool["base_url"] == "https://api.svc.com" and tool["health_check"] == {"path": "me"}


# ---- bug-hunt regressions: a stale/hand-edited treg.json fails clearly, not with a traceback ---
def test_malformed_treg_json_raises_clear_value_error(tmp_path):
    d = tmp_path / "svc"
    d.mkdir()
    (d / "treg.json").write_text("{ not valid json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_contract(d)


def test_contract_missing_secret_file_named_in_error(tmp_path):
    d = tmp_path / "svc"
    d.mkdir()
    contract = {"name": "svc", "base_url": "https://api.svc.com",
                "secrets": [{"name": "svc", "file": ".secret/gone", "kind": "env"}], "bindings": []}
    with pytest.raises(FileNotFoundError, match="gone"):
        contract_to_skill_payload(d, contract)


def test_contract_secret_entry_missing_keys_raises(tmp_path):
    d = tmp_path / "svc"
    d.mkdir()
    # no name → clear error
    with pytest.raises(ValueError, match="name"):
        contract_to_skill_payload(d, {"name": "svc", "base_url": "https://api.svc.com",
                                      "secrets": [{"kind": "env"}], "bindings": []})
    # has a name but neither a file nor an env source → clear error
    with pytest.raises(ValueError, match="file.*env|env.*file|source"):
        contract_to_skill_payload(d, {"name": "svc", "base_url": "https://api.svc.com",
                                      "secrets": [{"name": "x", "kind": "env"}], "bindings": []})


def test_contract_env_sourced_secret_reads_from_environment(tmp_path, monkeypatch):
    """A treg-import contract with an env-sourced secret (no file) must be readable by
    contract_to_skill_payload — it pulls the value from the environment."""
    d = tmp_path / "render"; d.mkdir()
    monkeypatch.setenv("RENDER_API_KEY", "rnd-secret")
    contract = {"name": "render", "base_url": "https://api.render.com/v1",
                "secrets": [{"env": "RENDER_API_KEY", "name": "render", "kind": "env"}],
                "bindings": [{"secret": "render", "injector": "env"}]}
    payload = contract_to_skill_payload(d, contract)
    assert payload["secrets"][0]["value"] == "rnd-secret"
