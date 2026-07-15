"""Provider catalog + env scanner (`treg upload`, Phase 1 detect + Phase 2 plan).

Pure logic — no DB, no network. Covers: bucket classification (matched / oauth_pair / basic_pair /
app_internal / unknown_secret / config), pair grouping (+ incomplete half), provider-wins-over-internal,
longest-token match, binding construction per auth shape, plan_actions supported/deferred, and that
env_values reads ONLY requested names (values never leak into detection).
"""
from __future__ import annotations

import pytest

from treg import providers as prov


def _write_env(tmp_path, body: str) -> str:
    p = tmp_path / ".env"
    p.write_text(body)
    return str(p)


def _by_var(dets):
    """Map a single-var detection by its var name -> Detection (skips multi-var pairs)."""
    return {d.vars[0]: d for d in dets if len(d.vars) == 1}


def test_bucketing_covers_every_kind(tmp_path):
    env = _write_env(tmp_path, "\n".join([
        "OPENAI_API_KEY=x",                 # matched bearer
        "ANTHROPIC_API_KEY=x",              # matched api_key_header
        "GITHUB_CLIENT_ID=x",               # oauth pair (with secret below)
        "GITHUB_CLIENT_SECRET=x",
        "TWILIO_ACCOUNT_SID=x",             # basic pair
        "TWILIO_AUTH_TOKEN=x",
        "ACME_API_KEY=x",                   # unknown secret
        "SESSION_SECRET=x",                 # app internal
        "LOG_LEVEL=info",                   # config
    ]))
    dets = prov.scan_env(env)
    kinds = sorted({d.kind for d in dets})
    assert kinds == ["app_internal", "basic_pair", "config", "matched", "oauth_pair", "unknown_secret"]


def test_bearer_vs_header_auth_shapes(tmp_path):
    env = _write_env(tmp_path, "OPENAI_API_KEY=x\nANTHROPIC_API_KEY=x\n")
    d = _by_var(prov.scan_env(env))
    assert d["OPENAI_API_KEY"].provider == "OpenAI"
    assert d["OPENAI_API_KEY"].auth["shape"] == "bearer"
    assert d["ANTHROPIC_API_KEY"].auth == {"shape": "api_key_header", "header": "x-api-key"}


def test_app_prefix_is_transparent(tmp_path):
    # A TREG_/APP_ prefix must not hide the provider token.
    env = _write_env(tmp_path, "TREG_RESEND_API_KEY=x\n")
    d = _by_var(prov.scan_env(env))
    assert d["TREG_RESEND_API_KEY"].provider == "Resend"


def test_oauth_pair_is_grouped(tmp_path):
    env = _write_env(tmp_path, "GITHUB_CLIENT_ID=x\nGITHUB_CLIENT_SECRET=x\n")
    dets = prov.scan_env(env)
    pairs = [d for d in dets if d.kind == "oauth_pair"]
    assert len(pairs) == 1
    assert set(pairs[0].vars) == {"GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"}
    assert pairs[0].provider == "GitHub" and pairs[0].auth["shape"] == "oauth2"


def test_incomplete_oauth_pair_flagged(tmp_path):
    env = _write_env(tmp_path, "NOTION_CLIENT_ID=x\n")
    [d] = prov.scan_env(env)
    assert d.kind == "oauth_pair" and d.vars == ["NOTION_CLIENT_ID"]
    assert "incomplete" in (d.note or "")


def test_basic_pair_grouped(tmp_path):
    env = _write_env(tmp_path, "TWILIO_ACCOUNT_SID=x\nTWILIO_AUTH_TOKEN=x\n")
    dets = prov.scan_env(env)
    pairs = [d for d in dets if d.kind == "basic_pair"]
    assert len(pairs) == 1 and pairs[0].provider == "Twilio"


def test_provider_beats_app_internal(tmp_path):
    # OPENAI_SECRET_KEY has the SECRET_KEY internal pattern but is a real OpenAI key — provider wins.
    env = _write_env(tmp_path, "OPENAI_SECRET_KEY=x\nSECRET_KEY=x\n")
    d = _by_var(prov.scan_env(env))
    assert d["OPENAI_SECRET_KEY"].kind == "matched" and d["OPENAI_SECRET_KEY"].provider == "OpenAI"
    assert d["SECRET_KEY"].kind == "app_internal"


def test_app_internal_and_config_excluded_from_plan(tmp_path):
    env = _write_env(tmp_path, "SECRET_KEY=x\nDATABASE_URL=x\nLOG_LEVEL=info\nPORT=8080\n")
    actions = prov.plan_actions(prov.scan_env(env))
    assert actions == []  # nothing offerable


def test_longest_token_wins(tmp_path):
    # HUGGINGFACE and HF both map to HuggingFace; the var HUGGINGFACE_API_KEY must resolve cleanly.
    env = _write_env(tmp_path, "HUGGINGFACE_API_KEY=x\n")
    d = _by_var(prov.scan_env(env))
    assert d["HUGGINGFACE_API_KEY"].provider == "HuggingFace"


def test_build_binding_shapes():
    assert prov.build_binding({"shape": "bearer"})["format"] == "Bearer {secret}"
    b = prov.build_binding({"shape": "api_key_header", "header": "x-api-key"})
    assert b["name"] == "x-api-key" and b["format"] == "{secret}"
    assert prov.build_binding({"shape": "oauth2"}) is None   # oauth2 uses the connect flow, not a binding


def test_plan_actions_supported_and_deferred(tmp_path):
    env = _write_env(tmp_path, "\n".join([
        "OPENAI_API_KEY=x", "GITHUB_CLIENT_ID=x", "GITHUB_CLIENT_SECRET=x", "ACME_API_KEY=x",
    ]))
    actions = prov.plan_actions(prov.scan_env(env))
    supported = [a for a in actions if a.supported]
    assert len(supported) == 1 and supported[0].tool_name == "openai"
    assert supported[0].binding["secret_id"] if "secret_id" in supported[0].binding else True  # template has no id yet
    assert "secret_id" not in supported[0].binding  # filled at register time, not in the plan
    deferred = {tuple(a.detection.vars): a.reason for a in actions if not a.supported}
    assert ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET") in deferred
    assert ("ACME_API_KEY",) in deferred


def test_slug_multiword_provider(tmp_path):
    env = _write_env(tmp_path, "GEMINI_API_KEY=x\n")   # provider "Google AI"
    [a] = prov.plan_actions(prov.scan_env(env))
    assert a.tool_name == "google-ai"


def test_oauth_pair_carries_connect_endpoints(tmp_path):
    env = _write_env(tmp_path, "GITHUB_CLIENT_ID=x\nGITHUB_CLIENT_SECRET=x\n")
    [d] = prov.scan_env(env)
    assert d.auth["auth_uri"].startswith("https://github.com/login/oauth")
    assert d.auth["token_uri"] and "scopes" in d.auth


def test_oauth_parts_identifies_id_and_secret():
    cid, csec = prov.oauth_parts(["GITHUB_CLIENT_SECRET", "GITHUB_CLIENT_ID"])
    assert cid == "GITHUB_CLIENT_ID" and csec == "GITHUB_CLIENT_SECRET"


def test_oauth_ready_true_only_for_complete_pair_with_endpoints(tmp_path):
    # GitHub: complete pair + catalog endpoints → ready.
    env = _write_env(tmp_path, "GITHUB_CLIENT_ID=x\nGITHUB_CLIENT_SECRET=x\n")
    [gh] = prov.scan_env(env)
    assert prov.oauth_ready(gh) is True
    # Stripe: a client pair but NO oauth block in the catalog → not ready.
    p2 = tmp_path / "stripe"; p2.mkdir(); (p2 / ".env").write_text("STRIPE_CLIENT_ID=x\nSTRIPE_CLIENT_SECRET=x\n")
    [st] = prov.scan_env(str(p2 / ".env"))
    assert prov.oauth_ready(st) is False
    # Incomplete pair (only the id half) → not ready.
    p3 = tmp_path / "half"; p3.mkdir(); (p3 / ".env").write_text("GITHUB_CLIENT_ID=x\n")
    [half] = prov.scan_env(str(p3 / ".env"))
    assert prov.oauth_ready(half) is False


def test_build_binding_query_and_basic():
    q = prov.build_binding({"shape": "query", "param": "api_key"})
    assert q["location"] == "query" and q["name"] == "api_key"
    b = prov.build_binding({"shape": "basic"})
    assert b["location"] == "header" and b["format"] == "Basic {secret}"


def test_basic_parts_twilio():
    u, pw = prov.basic_parts(["TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID"])
    assert u == "TWILIO_ACCOUNT_SID" and pw == "TWILIO_AUTH_TOKEN"


def test_basic_pair_is_supported_with_combine(tmp_path):
    env = _write_env(tmp_path, "TWILIO_ACCOUNT_SID=x\nTWILIO_AUTH_TOKEN=y\n")
    [a] = prov.plan_actions(prov.scan_env(env))
    assert a.supported and a.combine == ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
    assert a.binding["format"] == "Basic {secret}" and a.tool_name == "twilio"


def test_query_provider_auto_registers(tmp_path):
    env = _write_env(tmp_path, "SERPAPI_API_KEY=x\n")   # SerpAPI uses ?api_key=
    [a] = prov.plan_actions(prov.scan_env(env))
    assert a.supported and a.binding["location"] == "query" and a.binding["name"] == "api_key"


def test_catalog_grew_and_versioned():
    assert prov.CATALOG_VERSION >= 2 and len(prov.CATALOG) >= 80
    # tokens stay distinct enough: no duplicate provider names
    names = [p["provider"] for p in prov.CATALOG]
    assert len(names) == len(set(names))


def test_env_values_reads_only_requested(tmp_path):
    env = _write_env(tmp_path, 'A_KEY="v1"\nexport B_KEY=v2\nC_KEY=v3\n# comment\nD_KEY=\n')
    vals = prov.env_values(env, ["A_KEY", "B_KEY"])
    assert vals == {"A_KEY": "v1", "B_KEY": "v2"}   # quotes stripped, export handled, C/D not returned


def test_var_names_discards_values(tmp_path):
    env = _write_env(tmp_path, "SECRET=supersecretvalue\n")
    assert prov.var_names(env) == ["SECRET"]   # only the name is ever surfaced


def test_llm_prompt_lists_names():
    system, user = prov.llm_prompt(["ACME_API_KEY", "FOO_TOKEN"])
    assert "ACME_API_KEY" in user and "FOO_TOKEN" in user and "JSON" in user
    assert "secret" in system.lower()


def test_llm_parse_tolerates_prose_and_filters_bad_entries():
    text = ('Sure:\n{"resolved":[{"var":"ACME_API_KEY","provider":"Acme",'
            '"base_url":"https://api.acme.com","auth":{"shape":"bearer"}},'
            '{"var":"BAD","provider":"x"}]}\nhope that helps')
    out = prov.llm_parse(text)
    assert len(out) == 1 and out[0]["var"] == "ACME_API_KEY"   # the incomplete "BAD" entry is dropped


def test_llm_parse_handles_garbage():
    assert prov.llm_parse("no json at all") == []
    assert prov.llm_parse('{"resolved":[]}') == []


def test_provider_config_var_is_not_a_credential(tmp_path):
    # BUG: POSTHOG_HOST / POSTHOG_PROJECT_ID matched the PostHog token and were registered as tools.
    env = _write_env(tmp_path, "POSTHOG_HOST=https://eu.posthog.com\nPOSTHOG_PROJECT_ID=123\nPOSTHOG_API_KEY=phc_x\n")
    d = _by_var(prov.scan_env(env))
    assert d["POSTHOG_HOST"].kind == "config"
    assert d["POSTHOG_PROJECT_ID"].kind == "config"
    assert d["POSTHOG_API_KEY"].kind == "matched"     # a real key still matches


def test_render_api_still_matches_without_secret_hint(tmp_path):
    # RENDER_API has no KEY/TOKEN nor a config hint → must still match (regression guard for the fix above)
    env = _write_env(tmp_path, "RENDER_API=rnd_x\n")
    assert _by_var(prov.scan_env(env))["RENDER_API"].kind == "matched"


def test_duplicate_provider_vars_get_unique_tool_names(tmp_path):
    env = _write_env(tmp_path, "GITHUB_TOKEN=a\nGH_TOKEN=b\n")   # both → provider GitHub
    actions = [a for a in prov.plan_actions(prov.scan_env(env)) if a.supported]
    names = [a.tool_name for a in actions]
    assert len(names) == 2 and len(set(names)) == 2   # no collision at register


def test_independent_oauth_apps_not_merged(tmp_path):
    env = _write_env(tmp_path, "\n".join([
        "GITHUB_A_CLIENT_ID=1", "GITHUB_A_CLIENT_SECRET=2",
        "GITHUB_B_CLIENT_ID=3", "GITHUB_B_CLIENT_SECRET=4"]))
    pairs = [d for d in prov.scan_env(env) if d.kind == "oauth_pair"]
    assert len(pairs) == 2                             # two apps, not one 4-var blob
    assert all(len(p.vars) == 2 for p in pairs)


def test_llm_parse_survives_top_level_array():
    assert prov.llm_parse('[{"var":"X","base_url":"y","auth":{"shape":"bearer"}}]') == []   # no crash


def test_query_without_param_is_unsupported():
    assert prov.build_binding({"shape": "query"}) is None            # Telegram-style, no param
    assert prov.build_binding({"shape": "query", "param": "api_key"}) is not None


def test_env_values_strips_one_quote_pair(tmp_path):
    env = _write_env(tmp_path, "A=\"pa'ss\"\nB='v2'\nC=plain\n")
    v = prov.env_values(env, ["A", "B", "C"])
    assert v == {"A": "pa'ss", "B": "v2", "C": "plain"}   # inner quote preserved, one pair stripped


def test_sentry_auth_token_is_bearer_not_incomplete_basic(tmp_path):
    # BUG: "AUTH_TOKEN" substring made SENTRY_AUTH_TOKEN look like a Twilio-style Basic half → unsupported.
    env = _write_env(tmp_path, "SENTRY_AUTH_TOKEN=x\n")
    d = _by_var(prov.scan_env(env))["SENTRY_AUTH_TOKEN"]
    assert d.kind == "matched" and d.provider == "Sentry"
    assert prov.plan_actions([d])[0].supported


def test_provider_webhook_secret_is_app_internal(tmp_path):
    env = _write_env(tmp_path, "STRIPE_WEBHOOK_SECRET=whsec_x\nSTRIPE_SECRET_KEY=sk_x\n")
    d = _by_var(prov.scan_env(env))
    assert d["STRIPE_WEBHOOK_SECRET"].kind == "app_internal"   # a signing secret, not a callable key
    assert d["STRIPE_SECRET_KEY"].kind == "matched"            # a real key still matches


def test_provider_model_and_dsn_are_config(tmp_path):
    env = _write_env(tmp_path, "OPENAI_MODEL=gpt-4\nOPENAI_API_BASE=https://x\nSENTRY_DSN=https://k@o.ingest.sentry.io/1\n")
    d = _by_var(prov.scan_env(env))
    assert d["OPENAI_MODEL"].kind == "config"
    assert d["OPENAI_API_BASE"].kind == "config"
    assert d["SENTRY_DSN"].kind == "config"


def test_linear_and_calcom_auth_shapes():
    lin = next(p for p in prov.CATALOG if p["provider"] == "Linear")
    assert lin["auth"]["shape"] == "api_key_header" and lin["auth"]["format"] == "{secret}"
    cal = next(p for p in prov.CATALOG if p["provider"] == "Cal.com")
    assert cal["auth"]["shape"] == "query" and cal["auth"]["param"] == "apiKey"


def test_llm_parse_rejects_unsafe_base_url():
    good = '{"resolved":[{"var":"X","base_url":"https://api.acme.com","auth":{"shape":"bearer"}}]}'
    assert len(prov.llm_parse(good)) == 1
    for bad in ("http://api.acme.com", "https://localhost/x", "https://127.0.0.1/x",
                "https://x.internal/y", "https://169.254.169.254/latest"):
        payload = '{"resolved":[{"var":"X","base_url":"%s","auth":{"shape":"bearer"}}]}' % bad
        assert prov.llm_parse(payload) == [], bad


def test_llm_parse_dedupes_by_var():
    payload = ('{"resolved":[{"var":"X","base_url":"https://a.com","auth":{"shape":"bearer"}},'
               '{"var":"X","base_url":"https://b.com","auth":{"shape":"bearer"}}]}')
    assert len(prov.llm_parse(payload)) == 1


def test_scan_env_respects_passed_catalog(tmp_path):
    env = _write_env(tmp_path, "FOO_API_KEY=x\n")
    custom = [{"provider": "Foo", "tokens": ["FOO"], "base_url": "https://foo.test", "auth": {"shape": "bearer"}}]
    assert _by_var(prov.scan_env(env, catalog=custom))["FOO_API_KEY"].provider == "Foo"
    # empty catalog → no provider match → falls through to unknown_secret
    assert _by_var(prov.scan_env(env, catalog=[]))["FOO_API_KEY"].kind == "unknown_secret"


async def test_providers_json_endpoint_serves_catalog():
    from httpx import ASGITransport, AsyncClient

    from treg.api import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/providers.json")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == prov.CATALOG_VERSION
    assert any(p["provider"] == "OpenAI" for p in body["providers"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_match_skill_resolves_file_credential_providers():
    # skill folders (OAuth token files, no env var) resolve to a curated host by name / alias
    assert prov.match_skill("google-ads")["base_url"] == "https://googleads.googleapis.com"
    assert prov.match_skill("gsc")["base_url"] == "https://searchconsole.googleapis.com"
    assert prov.match_skill("Google Search Console")["base_url"] == "https://searchconsole.googleapis.com"
    assert prov.match_skill("google_ads")["provider"] == "Google Ads"   # punctuation-insensitive
    assert prov.match_skill("totally-unknown-skill") is None
    assert prov.match_skill("") is None


def test_google_providers_have_no_env_tokens():
    # these OAuth providers must NOT be detected from a .env (their auth is OAuth + extra headers,
    # not a simple bearer key) — so their catalog tokens are empty and the env scanner skips them
    for name in ("Google Ads", "Google Search Console"):
        entry = next(p for p in prov.CATALOG if p["provider"] == name)
        assert entry["tokens"] == []


# ---- catalog CLI metadata (auto-import Phase 1: auth_mechanism + detect) --------------------
def test_every_catalog_cli_profile_is_valid_and_typed():
    """Every catalog `cli` block must pass validate_cli_profile AND declare a known auth_mechanism —
    the field the auto-importer routes on (env/argv → server-injectable, config_file → local, device →
    report-only)."""
    from treg.localrun import AUTH_MECHANISMS, validate_cli_profile
    clis = [(e["provider"], e["cli"]) for e in prov.CATALOG if "cli" in e]
    assert clis, "catalog has no cli entries"
    for provider, cli in clis:
        validate_cli_profile(cli)  # raises on a malformed profile
        assert cli.get("auth_mechanism") in AUTH_MECHANISMS, f"{provider}: bad/missing auth_mechanism"
        # env/argv entries carry an inject; device/config_file need not
        if cli["auth_mechanism"] in ("env", "argv") and not cli.get("unsupported"):
            assert cli.get("inject"), f"{provider}: env/argv mechanism must have an inject"


def test_catalog_version_bumped_for_the_new_fields():
    assert prov.CATALOG_VERSION >= 8


def test_validate_cli_profile_accepts_and_rejects_the_new_fields():
    from treg.localrun import validate_cli_profile
    validate_cli_profile({"bin": "x", "auth_mechanism": "env", "beta": True,
                          "detect": {"config_paths": ["~/.config/x"]}})
    for bad in (
        {"bin": "x", "auth_mechanism": "nonsense"},
        {"bin": "x", "beta": "yes"},
        {"bin": "x", "detect": ["not-an-object"]},
        {"bin": "x", "detect": {"config_paths": [""]}},
    ):
        with pytest.raises(ValueError):
            validate_cli_profile(bad)


# ---- CLI auto-import classifier (Phase 2) --------------------------------------------------
_ENV_CLI = {"provider": "Stripe", "cli": {"bin": "stripe", "auth_mechanism": "env",
            "inject": [{"via": "env", "name": "STRIPE_API_KEY"}], "install": "brew install stripe"}}
_ARGV_CLI = {"provider": "Vercel", "cli": {"bin": "vercel", "auth_mechanism": "argv",
             "inject": [{"via": "argv", "argv": ["--token", "{secret}"], "env_from": "VERCEL_TOKEN"}]}}
_CONFIG_CLI = {"provider": "PlanetScale", "cli": {"bin": "pscale", "auth_mechanism": "config_file",
               "detect": {"config_paths": ["~/.config/planetscale/x"]}}}
_DEVICE_CLI = {"provider": "Azure", "cli": {"bin": "az", "auth_mechanism": "device", "unsupported": True,
               "reason": "device login only"}}


def test_cli_env_var_extraction():
    assert prov.cli_env_var(_ENV_CLI["cli"]) == "STRIPE_API_KEY"
    assert prov.cli_env_var(_ARGV_CLI["cli"]) == "VERCEL_TOKEN"       # argv → env_from
    assert prov.cli_env_var(_CONFIG_CLI["cli"]) is None               # nothing to inject


def test_classify_env_cli_all_states():
    c = lambda **kw: prov.classify_cli(_ENV_CLI, **kw)
    assert c(installed=True, secret_present=True, logged_in=False) == {"status": "ready", "tier": "server"}
    assert c(installed=True, secret_present=False, logged_in=True) == {"status": "ready", "tier": "local"}
    assert c(installed=True, secret_present=False, logged_in=False) == {"status": "needs_key", "env": "STRIPE_API_KEY"}
    assert c(installed=False, secret_present=True, logged_in=True)["status"] == "not_installed"


def test_classify_config_file_cli():
    c = lambda **kw: prov.classify_cli(_CONFIG_CLI, **kw)
    assert c(installed=True, secret_present=False, logged_in=True) == {"status": "ready", "tier": "local"}
    got = c(installed=True, secret_present=False, logged_in=False)
    assert got["status"] == "needs_login" and "pscale login" in got["action"]


def test_classify_device_cli_is_unsupported_even_if_installed():
    d = prov.classify_cli(_DEVICE_CLI, installed=True, secret_present=True, logged_in=True)
    assert d["status"] == "unsupported" and "device" in d["reason"]
