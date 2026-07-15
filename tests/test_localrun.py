"""`treg run` — grants for local CLI execution (docs/CLI-RUN-PLAN.md).

Three layers, mirroring the plan's §12: pure unit tests on `localrun` (profile merge, deny
semantics, validation), endpoint tests on /grant + /run-report (custody, opt-in, deny, audit,
health feedback, params, cross-org), and client tests on `cmd_run` (env composition, exit codes,
verdict reporting) with subprocess/network mocked.
"""

from __future__ import annotations

import json
import os

import pytest
from httpx import AsyncClient

from treg import cli as _cli
from treg import localrun
from treg.models import Tool


@pytest.fixture(autouse=True)
def _no_installed_runner(monkeypatch):
    """Unit tests must not depend on whether THIS machine has `treg setup-local-run` installed. Force the
    isolated-runner path OFF (as if not installed) so `cmd_run` exercises the in-process helper instead
    of doing a real `sudo` hand-off. A test that wants the hand-off patches os.path.exists itself (that
    runs after this fixture, so it wins)."""
    real = os.path.exists
    monkeypatch.setattr(_cli.os.path, "exists", lambda p: False if p == _cli._RUNNER_PATH else real(p))

LIVE = r"(^|\s)--live(\s|$)"


def _tool(cli=None, bindings=None, name="stripe"):
    return Tool(org_id=1, name=name, owner="o", base_url="http://x", host="x",
                bindings=bindings or [], cli=cli)


def _env(grant: dict) -> dict:
    """The env vars a grant would deliver — flattened from the delivery-tagged `inject` list."""
    return {i["name"]: i["value"] for i in grant.get("inject", []) if i.get("via") == "env"}


def _argv(grant: dict) -> list:
    out: list = []
    for i in grant.get("inject", []):
        if i.get("via") == "argv":
            out += i.get("argv") or []
    return out


# ---- unit: profile merge -------------------------------------------------------------------
def test_effective_profile_contract_over_catalog_and_deny_union():
    catalog = {"bin": "stripe", "install": "brew install stripe", "deny": [LIVE],
               "inject": [{"via": "env", "name": "STRIPE_API_KEY"}]}
    eff = localrun.effective_profile(_tool(cli={"enabled": True, "deny": ["refunds"]}), catalog)
    assert eff["enabled"] is True
    assert eff["bin"] == "stripe" and eff["install"]                 # catalog fills what mine omits
    assert eff["deny"] == ["refunds", LIVE]                          # union, creator's first
    assert eff["inject"] == [{"via": "env", "name": "STRIPE_API_KEY"}]


def test_effective_profile_catalog_never_enables():
    eff = localrun.effective_profile(_tool(cli=None), {"bin": "stripe", "inject": []})
    assert eff is not None and eff["enabled"] is False               # owner must opt in


def test_effective_profile_deny_defaults_false_drops_catalog_denies_only():
    catalog = {"bin": "stripe", "deny": [LIVE]}
    eff = localrun.effective_profile(
        _tool(cli={"enabled": True, "deny": ["refunds"], "deny_defaults": False}), catalog)
    assert eff["deny"] == ["refunds"]                                # kept mine, dropped the default


def test_effective_profile_none_when_nobody_knows_the_cli():
    assert localrun.effective_profile(_tool(cli=None), None) is None


# ---- unit: deny semantics ------------------------------------------------------------------
def test_check_deny_word_boundary_and_source():
    prof = localrun.effective_profile(_tool(cli={"enabled": True, "deny": ["refunds create"]}),
                                      {"deny": [LIVE]})
    hit = localrun.check_deny(prof, ["get", "/v1/charges", "--live"])
    assert hit == (LIVE, "the treg catalog defaults")
    hit = localrun.check_deny(prof, ["refunds", "create"])
    assert hit is not None and hit[1] == "this skill's treg.json"
    assert localrun.check_deny(prof, ["get", "--liveness"]) is None  # no substring false-positive
    assert localrun.check_deny(prof, ["get", "/v1/balance"]) is None


def test_check_deny_malformed_legacy_pattern_never_blocks():
    prof = {"deny": ["("], "_own_deny": ["("]}
    assert localrun.check_deny(prof, ["anything"]) is None           # skipped, not a crash


# ---- unit: profile validation --------------------------------------------------------------
def test_validate_cli_profile_accepts_a_full_profile():
    localrun.validate_cli_profile({
        "enabled": True, "bin": "stripe", "install": "brew install stripe",
        "inject": [{"via": "env", "name": "STRIPE_API_KEY"},
                   {"via": "argv", "argv": ["--token", "{secret}"]},
                   {"via": "env", "name": "X", "secret": "local-name", "secret_field": "token"}],
        "deny": [LIVE], "deny_defaults": False, "noninteractive": ["--yes"],
        "errors": [{"pattern": "401", "verdict": "credential_invalid", "message": "dead key"}],
    })


@pytest.mark.parametrize("bad,msg", [
    ({"deny": ["("]}, "regex"),
    ({"inject": [{"via": "env"}]}, "env var 'name'"),
    ({"inject": [{"via": "argv"}]}, "argv"),
    ({"inject": [{"via": "env", "name": "OK", "format": "no placeholder"}]}, "{secret}"),
    ({"inject": [{"via": "env", "name": "9BAD"}]}, "env var"),
    ({"errors": [{"pattern": "x", "verdict": "nonsense"}]}, "verdict"),
    ({"enabled": "yes"}, "boolean"),
    ({"inject": "not-a-list"}, "list"),
    # BUG #1: code-execution env names must be rejected
    ({"inject": [{"via": "env", "name": "LD_PRELOAD"}]}, "not allowed"),
    ({"inject": [{"via": "env", "name": "DYLD_INSERT_LIBRARIES"}]}, "not allowed"),
    ({"inject": [{"via": "env", "name": "PATH"}]}, "not allowed"),
    ({"inject": [{"via": "env", "name": "NODE_OPTIONS"}]}, "not allowed"),
    # BUG #3: bin must be a bare command name (no path / shell tokens)
    ({"bin": "/tmp/evil"}, "plain command name"),
    ({"bin": "sh -c evil"}, "plain command name"),
    # BUG #20: size caps
    ({"inject": [{"via": "env", "name": f"V{i}"} for i in range(40)]}, "too many"),
    ({"deny": [f"p{i}" for i in range(70)]}, "too many"),
])
def test_validate_cli_profile_rejects(bad, msg):
    with pytest.raises(ValueError, match=msg.replace("(", r"\(").replace(")", r"\)").replace("{", r"\{")):
        localrun.validate_cli_profile(bad)


def test_resolve_secret_id_ambiguous_multi_secret_returns_none():
    # BUG #5/#10: an unmapped inject on a multi-credential tool must NOT silently pick the first
    t = _tool(bindings=[{"secret_id": 1, "name": "Authorization"}, {"secret_id": 2, "name": "developer-token"}])
    assert localrun._resolve_secret_id({"via": "env", "name": "X"}, t) is None       # ambiguous → None
    assert localrun._resolve_secret_id({"via": "env", "name": "X", "secret_id": 2}, t) == 2
    assert localrun._resolve_secret_id({"from_binding": "developer-token"}, t) == 2
    one = _tool(bindings=[{"secret_id": 5, "name": "Authorization"}])
    assert localrun._resolve_secret_id({"via": "env", "name": "X"}, one) == 5          # sole secret → fine


def test_catalog_cli_recipe_becomes_runnable_tool(tmp_path):
    # The bridge: a secret-less recipe skill the catalog knows as a CLI (stripe-cli) becomes a runnable
    # cli tool, sourcing its credential from an env var — a gap when absent, ready when present.
    from treg import skills as sk, providers as prov
    d = tmp_path / "stripe-cli"; d.mkdir()
    (d / "SKILL.md").write_text("# use the stripe CLI to do things")
    det = sk._classify(d, prov.CATALOG, set())                       # STRIPE_API_KEY not in env
    assert det.kind == "generated" and det.cli and det.cli["enabled"] is True
    assert det.cli["bin"] == "stripe" and det.base_url == "https://api.stripe.com/v1"
    assert det.secrets and det.secrets[0]["env"] == "STRIPE_API_KEY" and det.secrets[0]["source"] == "env"
    assert any("STRIPE_API_KEY" in g for g in det.gaps) and not det.ready
    assert det.cli["inject"][0]["secret"] == det.secrets[0]["name"]  # inject references the credential
    det2 = sk._classify(d, prov.CATALOG, {"STRIPE_API_KEY"})         # credential present on the machine
    assert det2.gaps == [] and det2.ready


def test_deny_matches_flag_equals_value():
    # BUG #4: the shipped Stripe deny must catch --live AND --live=true, but not --livemode
    from treg import providers as prov
    pat = next(p for p in prov.CATALOG if p["provider"] == "Stripe")["cli"]["deny"][0]
    prof = {"deny": [pat], "_own_deny": []}
    assert localrun.check_deny(prof, ["get", "--live"]) is not None
    assert localrun.check_deny(prof, ["get", "--live=true"]) is not None
    assert localrun.check_deny(prof, ["get", "--livemode"]) is None


# ---- endpoint helpers ----------------------------------------------------------------------
async def _mk_tool(c: AsyncClient, name="stripe", cli=None, kind="env", value="sk_test_123") -> dict:
    sid = (await c.post("/secrets", json={"name": f"{name}-key", "kind": kind, "value": value})).json()["id"]
    body = {"name": name, "base_url": "http://upstream", "secret_id": sid}
    if kind == "oauth":
        body["injector"] = "oauth"
    if cli is not None:
        body["cli"] = cli
    r = await c.post("/tools", json=body)
    assert r.status_code == 200, r.text
    return {"tool": r.json(), "secret_id": sid}


_ENV_CLI = {"enabled": True, "bin": "stripe", "inject": [{"via": "env", "name": "STRIPE_API_KEY"}]}


# ---- endpoint: grant happy path + audit ----------------------------------------------------
async def test_config_file_cli_is_local_only_not_server_runnable(clients: AsyncClient):
    """A config_file/device CLI authenticates from the member's own machine, so it must NOT report as
    server_runnable (the auto-import routes it local). An env CLI stays server_runnable."""
    await _mk_tool(clients, name="pscale", cli={"enabled": True, "bin": "stripe", "auth_mechanism": "config_file"})
    await _mk_tool(clients, name="strp", cli={"enabled": True, "bin": "stripe", "auth_mechanism": "env"})
    tools = {t["name"]: t for t in (await clients.get("/tools")).json()}
    assert tools["pscale"]["server_runnable"] is False   # config_file → local only
    assert tools["strp"]["server_runnable"] is True       # env → server-injectable


async def test_grant_local_tier_cli_injects_nothing(clients: AsyncClient):
    """Auto-import 'local tier': a self-authenticating CLI (its own login) stores an EXPLICIT empty
    inject. Two regressions guarded: (1) the grant-time catalog merge must NOT re-add the catalog's
    inject — `gh`'s catalog profile carries `inject:[GH_TOKEN]`, which previously leaked back and failed
    to resolve; an empty inject overrides it. (2) an empty inject is a VALID grant (bin + audit, no
    secret), not a 'profile has no inject entries' error — so `treg run gh` just execs gh."""
    r = await clients.post("/tools", json={"name": "gh", "base_url": "http://upstream",
                                           "cli": {"enabled": True, "bin": "gh", "inject": []}})
    assert r.status_code == 200, r.text
    g = await clients.post("/tools/gh/grant", json={"argv": ["auth", "status"]})
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["bin"] == "gh"
    assert body["inject"] == []  # nothing injected — the CLI authenticates via its own login
    assert isinstance(body["audit_id"], int)


def test_catalog_deny_blocks_token_and_codeexec_subcommands():
    """The deny-rule audit: catalog CLIs must refuse features that print the injected key or run member
    code as the runner — while leaving normal subcommands alone. Enforced server-side at grant."""
    from treg import providers as prov

    def prof(bin_):
        e = next(e for e in prov.CATALOG if (e.get("cli") or {}).get("bin") == bin_)
        return {"deny": e["cli"].get("deny") or [], "_own_deny": []}

    assert localrun.check_deny(prof("gh"), ["auth", "token"])           # prints the token
    assert localrun.check_deny(prof("gh"), ["extension", "install", "x"])  # arbitrary code as the runner
    assert localrun.check_deny(prof("gh"), ["alias", "set", "co", "!sh"])
    assert localrun.check_deny(prof("gh"), ["repo", "list"]) is None    # normal use is fine
    assert localrun.check_deny(prof("gh"), ["auth", "login"]) is None
    assert localrun.check_deny(prof("flyctl"), ["auth", "token"])
    assert localrun.check_deny(prof("doppler"), ["run", "--", "printenv"])  # runs a child with the token in env
    assert localrun.check_deny(prof("doppler"), ["secrets", "get", "X"]) is None
    assert localrun.check_deny(prof("infisical"), ["run", "--", "env"])


async def test_grant_owned_key_is_not_redacted(clients: AsyncClient):
    # An owned key (the caller registered it) needs no output redaction — you may see your own value.
    await _mk_tool(clients, cli=_ENV_CLI)
    g = (await clients.post("/tools/stripe/grant", json={"argv": ["x"]})).json()
    assert g["redact_output"] is False


async def test_grant_renders_env_and_audits(clients: AsyncClient):
    await _mk_tool(clients, cli=_ENV_CLI)
    r = await clients.post("/tools/stripe/grant", json={"argv": ["get", "/v1/balance"]})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["bin"] == "stripe" and _env(g) == {"STRIPE_API_KEY": "sk_test_123"}
    assert _argv(g) == [] and isinstance(g["audit_id"], int)
    calls = (await clients.get("/calls")).json()
    grants = [x for x in calls if x["method"] == "GRANT"]
    assert grants and grants[0]["tool_name"] == "stripe" and "get /v1/balance" in grants[0]["path"]


async def test_grant_argv_injection(clients: AsyncClient):
    cli = {"enabled": True, "bin": "vercel",
           "inject": [{"via": "argv", "argv": ["--token", "{secret}"]}]}
    await _mk_tool(clients, name="vc", cli=cli, value="vc_tok")
    g = (await clients.post("/tools/vc/grant", json={"argv": ["whoami"]})).json()
    assert _argv(g) == ["--token", "vc_tok"] and _env(g) == {}


# ---- endpoint: THE custody test ------------------------------------------------------------
async def test_grant_oauth_releases_only_the_leaf(clients: AsyncClient):
    blob = json.dumps({"access_token": "OLD", "refresh_token": "RT-SECRET", "client_id": "cid",
                       "client_secret": "CSEC-SECRET", "token_uri": "http://upstream/token",
                       "expires_at": 0})  # stale → grant must refresh first
    await _mk_tool(clients, name="gsc", cli={"enabled": True, "bin": "gsc",
                   "inject": [{"via": "env", "name": "GSC_TOKEN"}]}, kind="oauth", value=blob)
    r = await clients.post("/tools/gsc/grant", json={"argv": []})
    assert r.status_code == 200, r.text
    text = r.text
    assert _env(r.json())["GSC_TOKEN"] == "REFRESHED"       # the fresh leaf, not the stale one
    for forbidden in ("RT-SECRET", "CSEC-SECRET", "refresh_token", "client_secret"):
        assert forbidden not in text                          # the root NEVER leaves the server
    assert r.json()["ttl_seconds"] and r.json()["ttl_seconds"] > 3000  # conftest /token: expires_in 3600
    assert any("expires in" in w for w in r.json()["warnings"])


async def test_grant_oauth_leaf_field_fallback(clients: AsyncClient):
    # google-style token.json pre-refresh: the access token lives under `token`, and there's
    # nothing to refresh with — the grant must still find the leaf via the sibling key.
    blob = json.dumps({"token": "GOOGLE-LEAF"})
    await _mk_tool(clients, name="gads", cli={"enabled": True, "bin": "gads",
                   "inject": [{"via": "env", "name": "T"}]}, kind="oauth", value=blob)
    g = (await clients.post("/tools/gads/grant", json={"argv": []})).json()
    assert _env(g)["T"] == "GOOGLE-LEAF"


# ---- endpoint: opt-in + catalog live-merge -------------------------------------------------
async def test_grant_catalog_profile_needs_enable_then_works(clients: AsyncClient):
    # "stripe" matches the real catalog: profile exists (bin, inject, deny) but is DISABLED.
    made = await _mk_tool(clients, cli=None)
    r = await clients.post("/tools/stripe/grant", json={"argv": ["get", "/v1/balance"]})
    assert r.status_code == 403 and "enable" in r.json()["detail"]
    # the owner flips the toggle — grant then renders from the LIVE catalog profile
    r = await clients.patch(f"/tools/{made['tool']['id']}", json={"cli": {"enabled": True}})
    assert r.status_code == 200, r.text
    g = (await clients.post("/tools/stripe/grant", json={"argv": ["get", "/v1/balance"]})).json()
    assert _env(g) == {"STRIPE_API_KEY": "sk_test_123"} and g["bin"] == "stripe"


async def test_grant_denied_by_catalog_defaults_and_audited(clients: AsyncClient):
    made = await _mk_tool(clients, cli=None)
    await clients.patch(f"/tools/{made['tool']['id']}", json={"cli": {"enabled": True}})
    r = await clients.post("/tools/stripe/grant", json={"argv": ["get", "/v1/charges", "--live"]})
    assert r.status_code == 403
    assert "catalog defaults" in r.json()["detail"] and "--live" in r.json()["detail"]
    denies = [x for x in (await clients.get("/calls")).json() if x["method"] == "DENY"]
    assert denies and "--live" in denies[0]["path"]


async def test_grant_creator_deny_and_deny_defaults_opt_out(clients: AsyncClient):
    cli = {"enabled": True, "bin": "stripe", "deny": ["refunds"], "deny_defaults": False,
           "inject": [{"via": "env", "name": "STRIPE_API_KEY"}]}
    await _mk_tool(clients, cli=cli)
    ok = await clients.post("/tools/stripe/grant", json={"argv": ["get", "/v1/charges", "--live"]})
    assert ok.status_code == 200                                   # catalog --live default opted out
    denied = await clients.post("/tools/stripe/grant", json={"argv": ["refunds", "create"]})
    assert denied.status_code == 403 and "treg.json" in denied.json()["detail"]


# ---- endpoint: refusals --------------------------------------------------------------------
async def test_grant_unknown_tool_404(clients: AsyncClient):
    r = await clients.post("/tools/nope/grant", json={"argv": []})
    assert r.status_code == 404


async def test_grant_no_profile_409_with_template(clients: AsyncClient):
    await _mk_tool(clients, name="zzz-internal", cli=None)  # no catalog match, no contract cli
    r = await clients.post("/tools/zzz-internal/grant", json={"argv": []})
    assert r.status_code == 409 and '"cli"' in r.json()["detail"]  # the treg.json template


async def test_grant_unsupported_cli_409_with_reason(clients: AsyncClient):
    await _mk_tool(clients, name="az", cli=None)  # catalog marks az unsupported
    r = await clients.post("/tools/az/grant", json={"argv": ["vm", "list"]})
    assert r.status_code == 409 and "service principal" in r.json()["detail"]


async def test_grant_strips_trailing_newline(clients: AsyncClient):
    # BUG #9: a file-sourced secret with a stray newline must not become an illegal env value
    await _mk_tool(clients, cli=_ENV_CLI, value="sk_test_ABC\n")
    g = (await clients.post("/tools/stripe/grant", json={"argv": []})).json()
    assert _env(g)["STRIPE_API_KEY"] == "sk_test_ABC"           # stripped, like build_payload does


async def test_grant_ambiguous_multi_secret_is_409_not_wrong_key(clients: AsyncClient):
    # BUG #5/#10: unmapped inject on a 2-credential tool → clear 409, never a silently-wrong secret
    a = (await clients.post("/secrets", json={"name": "a", "value": "AAA"})).json()["id"]
    d = (await clients.post("/secrets", json={"name": "d", "value": "DDD"})).json()["id"]
    r = await clients.post("/tools", json={"name": "ads", "base_url": "http://upstream",
        "bindings": [{"secret_id": a, "injector": "env", "location": "header", "name": "Authorization", "format": "Bearer {secret}"},
                     {"secret_id": d, "injector": "env", "location": "header", "name": "developer-token", "format": "{secret}"}],
        "cli": {"enabled": True, "bin": "x", "inject": [{"via": "env", "name": "DEV"}]}})
    assert r.status_code == 200, r.text
    g = await clients.post("/tools/ads/grant", json={"argv": []})
    assert g.status_code == 409 and "secret_id" in g.json()["detail"]  # refuses to guess


async def test_grant_refused_for_viewer_who_may_still_call(clients: AsyncClient):
    # BUG #2 (tightened): /call injects server-side (a viewer may call, no value leaks); /grant HANDS the
    # credential value to the caller's machine, so it needs member+ — else the lowest-trust role can
    # exfiltrate every enabled key. Prove the viewer CAN call but CANNOT grant.
    team = (await clients.post("/orgs", json={"name": "RunTeam"})).json()
    otok, org_id = team["token"], team["org_id"]
    h = {"X-Treg-Token": otok}
    sid = (await clients.post("/secrets", json={"name": "stripe-key", "value": "sk_test_1"}, headers=h)).json()["id"]
    await clients.post("/tools", json={"name": "stripe", "base_url": "http://upstream", "secret_id": sid,
                                       "cli": {"enabled": True}}, headers=h)
    code = (await clients.post(f"/orgs/{org_id}/invites", json={"email": "vi@x.dev", "role": "viewer"}, headers=h)).json()["code"]
    vtok = (await clients.post("/invites/accept", json={"code": code, "email": "vi@x.dev"})).json()["token"]
    vh = {"X-Treg-Token": vtok}
    assert (await clients.get("/call/stripe/anything", headers=vh)).status_code == 200   # viewer may call
    r = await clients.post("/tools/stripe/grant", json={"argv": []}, headers=vh)
    assert r.status_code == 403 and "viewer" in r.json()["detail"].lower()               # but not extract


async def test_call_and_grant_are_tagged_by_kind(clients: AsyncClient):
    """The usage breakdown must tell a proxy call from a local-run grant: /call records kind='call',
    /tools/{name}/grant records kind='local_run' (both land in CallRecord)."""
    from treg import audit
    await _mk_tool(clients, cli=_ENV_CLI)
    await clients.get("/call/stripe/anything")                     # proxy call → kind 'call'
    await clients.post("/tools/stripe/grant", json={"argv": []})   # local-run grant → kind 'local_run'
    await audit.drain()  # /call's CallRecord is written fire-and-forget
    calls = (await clients.get("/calls")).json()
    by_method = {c["method"]: c["kind"] for c in calls}
    assert by_method.get("GRANT") == "local_run"
    assert any(c["kind"] == "call" for c in calls if c["method"] != "GRANT")


async def test_grant_argv_secrets_redacted_in_audit(clients: AsyncClient):
    # BUG #6: a secret typed inline must not be persisted verbatim in the audit log
    await _mk_tool(clients, cli=_ENV_CLI)
    await clients.post("/tools/stripe/grant", json={"argv": ["post", "--key", "sk_live_ABCDEFGHIJKLMNOP1234"]})
    grants = [x for x in (await clients.get("/calls")).json() if x["method"] == "GRANT"]
    assert grants and "sk_live_ABCDEFGHIJKLMNOP1234" not in grants[0]["path"] and "***" in grants[0]["path"]


async def test_delete_secret_blocked_when_used_by_cli_inject(clients: AsyncClient):
    # BUG #14: a secret referenced only by a local-run inject (not a binding) must not be deletable
    sid = (await clients.post("/secrets", json={"name": "clionly", "value": "v"})).json()["id"]
    await clients.post("/tools", json={"name": "fly", "base_url": "http://upstream", "bindings": [],
        "cli": {"enabled": True, "bin": "flyctl", "inject": [{"via": "env", "name": "FLY_API_TOKEN", "secret_id": sid}]}})
    r = await clients.delete(f"/secrets/{sid}")
    assert r.status_code == 409 and "local-run" in r.json()["detail"]


async def test_grant_cross_org_isolation(clients: AsyncClient):
    await _mk_tool(clients, cli=_ENV_CLI)
    other = (await clients.post("/users", json={"email": "eve@other.dev"})).json()["token"]
    r = await clients.post("/tools/stripe/grant", json={"argv": []},
                           headers={"X-Treg-Token": other})
    assert r.status_code == 404                                     # her org has no such tool


async def test_bad_cli_profile_rejected_at_registration(clients: AsyncClient):
    sid = (await clients.post("/secrets", json={"name": "k", "value": "v"})).json()["id"]
    r = await clients.post("/tools", json={"name": "t1", "base_url": "http://upstream",
                                           "secret_id": sid, "cli": {"deny": ["("]}})
    assert r.status_code == 422 and "regex" in r.json()["detail"]
    r = await clients.post("/tools", json={"name": "t2", "base_url": "http://upstream",
                                           "secret_id": sid, "cli": {"inject": [{"via": "env"}]}})
    assert r.status_code == 422


# ---- endpoint: run-report → health ---------------------------------------------------------
async def test_run_report_marks_credential_invalid(clients: AsyncClient):
    await _mk_tool(clients, cli=_ENV_CLI)
    g = (await clients.post("/tools/stripe/grant", json={"argv": ["get", "/v1/balance"]})).json()
    r = await clients.post("/tools/stripe/run-report",
                           json={"audit_id": g["audit_id"], "exit_code": 1, "verdict": "credential_invalid"})
    assert r.status_code == 200 and r.json()["marked_invalid"] == ["stripe-key"]
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["stripe-key"]["status"] == "invalid"
    assert "local run" in health["stripe-key"]["detail"]
    reports = [x for x in (await clients.get("/calls")).json() if x["method"] == "REPORT"]
    assert reports and "credential_invalid" in reports[0]["path"]


async def test_run_report_does_not_mark_params_or_unrelated_secrets(clients: AsyncClient):
    # BUG #12/#13: a credential_invalid report marks only the injected CREDENTIAL, never a param
    pid = (await clients.post("/secrets", json={"name": "proj", "kind": "param", "value": "p-1"})).json()["id"]
    sid = (await clients.post("/secrets", json={"name": "gc-key", "value": "tok"})).json()["id"]
    cli = {"enabled": True, "bin": "gcloud",
           "inject": [{"via": "env", "name": "TOKEN", "secret_id": sid},
                      {"via": "env", "name": "PROJECT", "secret_id": pid}]}
    await clients.post("/tools", json={"name": "gcl", "base_url": "http://upstream", "secret_id": sid, "cli": cli})
    g = (await clients.post("/tools/gcl/grant", json={"argv": ["x"]})).json()
    r = await clients.post("/tools/gcl/run-report",
                           json={"audit_id": g["audit_id"], "exit_code": 1, "verdict": "credential_invalid"})
    assert r.status_code == 200 and "proj" not in r.json()["marked_invalid"] and "gc-key" in r.json()["marked_invalid"]
    health = {h["name"]: h["status"] for h in (await clients.get("/health")).json()}
    assert health["proj"] == "unknown" and health["gc-key"] == "invalid"


async def test_run_report_needs_a_real_grant(clients: AsyncClient):
    await _mk_tool(clients, cli=_ENV_CLI)
    r = await clients.post("/tools/stripe/run-report",
                           json={"audit_id": 99999, "exit_code": 1, "verdict": "credential_invalid"})
    assert r.status_code == 404
    g = (await clients.post("/tools/stripe/grant", json={"argv": []})).json()
    r = await clients.post("/tools/stripe/run-report",
                           json={"audit_id": g["audit_id"], "exit_code": 1, "verdict": "nonsense"})
    assert r.status_code == 422
    # a grant for stripe can't be replayed against a different tool
    await _mk_tool(clients, name="other", cli=_ENV_CLI)
    r = await clients.post("/tools/other/run-report",
                           json={"audit_id": g["audit_id"], "exit_code": 1, "verdict": "ok"})
    assert r.status_code == 404


# ---- endpoint: params (kind "param") -------------------------------------------------------
async def test_param_kind_injects_into_grant_and_http_binding(clients: AsyncClient):
    pid = (await clients.post("/secrets", json={"name": "gcp-project", "kind": "param",
                                                "value": "proj-123"})).json()["id"]
    sid = (await clients.post("/secrets", json={"name": "gc-key", "value": "tok"})).json()["id"]
    cli = {"enabled": True, "bin": "gcloud",
           "inject": [{"via": "env", "name": "CLOUDSDK_AUTH_ACCESS_TOKEN"},
                      {"via": "env", "name": "CLOUDSDK_CORE_PROJECT", "secret_id": pid}]}
    r = await clients.post("/tools", json={"name": "gcl", "base_url": "http://upstream",
                                           "secret_id": sid, "cli": cli})
    assert r.status_code == 200, r.text
    g = (await clients.post("/tools/gcl/grant", json={"argv": ["projects", "list"]})).json()
    assert _env(g) == {"CLOUDSDK_AUTH_ACCESS_TOKEN": "tok", "CLOUDSDK_CORE_PROJECT": "proj-123"}
    # the same param works in an HTTP query binding — the free upgrade for HTTP tools
    r = await clients.post("/tools", json={
        "name": "httpq", "base_url": "http://upstream",
        "bindings": [{"secret_id": sid, "injector": "env", "location": "header",
                      "name": "Authorization", "format": "Bearer {secret}"},
                     {"secret_id": pid, "injector": "env", "location": "query",
                      "name": "project", "format": "{secret}"}]})
    assert r.status_code == 200, r.text
    echo = (await clients.get("/call/httpq/echo")).json()
    assert echo["query"]["project"] == "proj-123"
    # health never verdicts a param — it's config, not a credential
    await clients.post("/health/run")
    health = {h["name"]: h for h in (await clients.get("/health")).json()}
    assert health["gcp-project"]["status"] == "unknown"


# ---- endpoint: the skill door (contract cli → registered tool → grant) ----------------------
async def test_skill_contract_cli_registers_and_grants(clients: AsyncClient):
    payload = {
        "name": "stripe-cli", "recipe": "# stripe\nuse `treg run stripe-cli`",
        "secrets": [{"local_name": "stripe-key", "value": "sk_test_9", "kind": "env"}],
        "tools": [{"name": "stripe-cli", "base_url": "https://api.stripe.com/v1",
                   "bindings": [{"secret": "stripe-key", "injector": "env", "location": "header",
                                 "name": "Authorization", "format": "Bearer {secret}"}],
                   "cli": {"enabled": True, "bin": "stripe",
                           "inject": [{"secret": "stripe-key", "via": "env", "name": "STRIPE_API_KEY"}]}}],
    }
    r = await clients.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    g = (await clients.post("/tools/stripe-cli/grant", json={"argv": ["get", "/v1/balance"]})).json()
    assert _env(g) == {"STRIPE_API_KEY": "sk_test_9"}
    # …and the catalog's --live default still guards it (deny union with the Stripe entry)
    r = await clients.post("/tools/stripe-cli/grant", json={"argv": ["charges", "create", "--live"]})
    assert r.status_code == 403


async def test_skill_cli_inject_unknown_local_name_422(clients: AsyncClient):
    payload = {"name": "s", "recipe": "r", "secrets": [],
               "tools": [{"name": "s", "base_url": "http://upstream", "bindings": [],
                          "cli": {"enabled": True, "inject": [{"secret": "ghost", "via": "env", "name": "X"}]}}]}
    r = await clients.post("/skills", json=payload)
    assert r.status_code == 422 and "ghost" in r.json()["detail"]


# ---- endpoint: analyze preview -------------------------------------------------------------
async def test_analyze_reports_cli_support(clients: AsyncClient):
    files = [{"path": "stripe-cli/SKILL.md", "content": "# stripe cli"},
             {"path": "az/SKILL.md", "content": "# azure"},
             {"path": "plain/SKILL.md", "content": "# notes"}]
    skills = (await clients.post("/skills/analyze", json={"files": files})).json()["skills"]
    kinds = {s["name"]: s["kind"] for s in skills}
    by = {s["name"]: s.get("cli") for s in skills}
    # stripe-cli is now turned INTO a runnable cli tool (catalog-derived), enabled, needing its credential
    assert kinds["stripe-cli"] == "generated"
    assert by["stripe-cli"]["source"] == "catalog" and by["stripe-cli"]["bin"] == "stripe"
    assert by["stripe-cli"]["enabled"] is True and by["stripe-cli"]["needs_credential"] is True
    # az stays a recipe (unsupported for local runs), plain stays a plain recipe
    assert kinds["az"] == "recipe_only" and by["az"]["source"] == "unsupported" and "service principal" in by["az"]["reason"]
    assert by["plain"] is None


# ---- client: cmd_run (subprocess + network mocked) ------------------------------------------
class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code, self._body = status_code, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, responses: dict):
        self.responses, self.posts = responses, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, path, json=None, **kw):
        self.posts.append((path, json))
        for frag, resp in self.responses.items():
            if frag in path:
                return resp
        raise AssertionError(f"unexpected POST {path}")


class _FakeProc:
    def __init__(self, rc: int, stderr_bytes: bytes):
        self.returncode, self._rc = rc, rc
        import io
        self.stderr = io.BytesIO(stderr_bytes)

    def wait(self, timeout=None):
        return self._rc

    def poll(self):
        return self._rc          # already exited → cmd_run's cleanup is a no-op

    def send_signal(self, signum):
        pass


def _run_args(tool: str, *cli_args: str):
    from treg import cli as cli_mod
    return cli_mod.build_parser().parse_args(["run", tool, "--", *cli_args])


def test_cmd_run_env_composition_and_exit_code(monkeypatch):
    from treg import cli as cli_mod
    grant = _FakeResp(200, {"bin": "stripe", "inject": [{"via": "env", "name": "STRIPE_API_KEY", "value": "sk_INJECTED"},
                                         {"via": "argv", "argv": ["--extra"]}], "audit_id": 7, "warnings": [], "errors": []})
    fake = _FakeClient({"/grant": grant})
    seen: dict = {}

    def fake_popen(cmd, env=None, **kw):
        seen["cmd"], seen["env"] = cmd, env
        return _FakeProc(0, b"")

    monkeypatch.setattr(cli_mod, "_client", lambda cfg: fake)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli_mod.shutil, "which", lambda b: f"/bin/{b}")
    monkeypatch.setenv("STRIPE_API_KEY", "sk_stale_local")
    monkeypatch.setenv("HOME", "/home/u")
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(_run_args("stripe", "get", "/v1/balance"), {"base_url": "http://x"})
    assert e.value.code == 0
    assert seen["cmd"] == ["/bin/stripe", "--extra", "get", "/v1/balance"]  # argv_extra leads (global flags first)
    assert seen["env"]["STRIPE_API_KEY"] == "sk_INJECTED"        # the grant beats the stale local key
    assert seen["env"]["HOME"] == "/home/u"                      # the user's env is inherited, not replaced
    assert fake.posts[0] == ("/tools/stripe/grant", {"argv": ["get", "/v1/balance"]})


def test_cmd_run_macos_best_effort_without_setup(monkeypatch, capsys):
    # macOS now supports the same dedicated-user isolation as Linux. WITHOUT setup (no runner), it runs
    # best-effort as the member and hints how to enable isolation — parity with Linux.
    from treg import cli as cli_mod
    grant = _FakeResp(200, {"bin": "gh", "inject": [{"via": "env", "name": "GH_TOKEN", "value": "t"}],
                            "audit_id": 1, "warnings": [], "errors": []})
    monkeypatch.setattr(cli_mod.sys, "platform", "darwin")
    monkeypatch.setattr(cli_mod.os.path, "exists", lambda p: False)  # runner not installed yet
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: _FakeClient({"/grant": grant}))
    monkeypatch.setattr(cli_mod.shutil, "which", lambda b: f"/bin/{b}")
    seen = {}
    monkeypatch.setattr(cli_mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: (seen.update(env=env) or _FakeProc(0, b"")))
    with pytest.raises(SystemExit):
        cli_mod.cmd_run(_run_args("gh", "api", "user"), {"base_url": "http://x"})
    assert "best-effort" in capsys.readouterr().err.lower()     # hints at `setup-local-run`
    assert seen["env"]["GH_TOKEN"] == "t"                       # still injects the credential


def test_cmd_run_linux_hands_off_to_treg_run_user(monkeypatch):
    # On Linux with local-run set up, the member never fetches the credential: it execs
    # `sudo -u treg-run <runner>` and passes its own token through the environment.
    from treg import cli as cli_mod
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    monkeypatch.setattr(cli_mod.os.path, "exists", lambda p: p == cli_mod._RUNNER_PATH)
    seen = {}

    def fake_execvpe(file, argv, env):
        seen["file"], seen["argv"], seen["env"] = file, argv, env
        raise OSError("stop here")  # normally replaces the process; abort so the test can assert
    monkeypatch.setattr(cli_mod.os, "execvpe", fake_execvpe)
    with pytest.raises(SystemExit):
        cli_mod.cmd_run(_run_args("stripe", "get", "/v1/balance"),
                        {"token": "MEMBER_TOK", "base_url": "http://x", "active_org": "acme"})
    assert seen["file"] == "sudo"
    assert seen["argv"][:4] == ["sudo", "-u", "treg-run", "--"]
    assert cli_mod._RUNNER_PATH in seen["argv"] and "stripe" in seen["argv"]
    assert seen["env"]["TREG_RUN_TOKEN"] == "MEMBER_TOK" and seen["env"]["TREG_RUN_ORG"] == "acme"


def test_cmd_run_helper_uses_env_context(monkeypatch):
    # The treg-run-side helper rebuilds its config from the env the member passed through sudo.
    from treg import cli as cli_mod
    grant = _FakeResp(200, {"bin": "gh", "inject": [{"via": "env", "name": "GH_TOKEN", "value": "t"}],
                            "audit_id": 9, "warnings": [], "errors": []})
    seen = {}
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: seen.update(cfg=cfg) or _FakeClient({"/grant": grant}))
    monkeypatch.setattr(cli_mod.shutil, "which", lambda b: f"/bin/{b}")
    monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda cmd, env=None, **kw: _FakeProc(0, b""))
    monkeypatch.setenv("TREG_RUN_TOKEN", "T"); monkeypatch.setenv("TREG_RUN_BASE", "http://x")
    monkeypatch.setenv("TREG_RUN_ORG", "acme")
    with pytest.raises(SystemExit):
        cli_mod.cmd_run_helper(_run_args("gh", "api", "user"), {})
    assert seen["cfg"]["token"] == "T" and seen["cfg"]["active_org"] == "acme"


def test_cmd_run_refusal_prints_server_detail(monkeypatch, capsys):
    from treg import cli as cli_mod
    fake = _FakeClient({"/grant": _FakeResp(403, {"detail": "denied by the treg catalog defaults: pattern '--live'"})})
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: fake)
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(_run_args("stripe", "get", "--live"), {"base_url": "http://x"})
    assert "denied by the treg catalog defaults" in str(e.value.code)


def test_cmd_run_reports_credential_invalid(monkeypatch, capsys):
    from treg import cli as cli_mod
    grant = _FakeResp(200, {"bin": "doctl", "inject": [{"via": "env", "name": "T", "value": "v"}], "audit_id": 11,
                            "warnings": [],
                            "errors": [{"pattern": "(?i)401|unauthorized", "verdict": "credential_invalid",
                                        "message": "the org's credential is invalid or expired"}]})
    fake = _FakeClient({"/grant": grant, "/run-report": _FakeResp(200, {"ok": True})})
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: fake)
    monkeypatch.setattr(cli_mod.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakeProc(1, b"Error: GET .../account: 401 Unauthorized\n"))
    monkeypatch.setattr(cli_mod.shutil, "which", lambda b: f"/bin/{b}")
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(_run_args("doctl", "account", "get"), {"base_url": "http://x"})
    assert e.value.code == 1                                     # the CLI's own exit code passes through
    report = next(p for p in fake.posts if "/run-report" in p[0])
    assert report[1] == {"audit_id": 11, "exit_code": 1, "verdict": "credential_invalid"}
    assert "invalid or expired" in capsys.readouterr().err


def test_cmd_run_missing_binary_shows_install_hint(monkeypatch):
    from treg import cli as cli_mod
    grant = _FakeResp(200, {"bin": "doctl", "inject": [], "audit_id": 1,
                            "install": "brew install doctl", "warnings": [], "errors": []})
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: _FakeClient({"/grant": grant}))
    monkeypatch.setattr(cli_mod.shutil, "which", lambda b: None)
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(_run_args("doctl"), {"base_url": "http://x"})
    assert "brew install doctl" in str(e.value.code)


def _args(**kw):
    return type("A", (), kw)()


def test_setup_local_run_guards(monkeypatch):
    from treg import cli as cli_mod
    # an unsupported OS is rejected
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_setup_local_run(_args(member=None), {})
    assert "Linux and macOS" in str(e.value.code)
    # macOS is now supported → it passes the OS gate and hits the root check
    monkeypatch.setattr(cli_mod.sys, "platform", "darwin")
    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as e2:
        cli_mod.cmd_setup_local_run(_args(member=None), {})
    assert "sudo" in str(e2.value.code)                            # needs root, on macOS too


def test_parser_dispatches_run_helper_and_setup():
    from treg import cli as cli_mod
    p = cli_mod.build_parser()
    assert p.parse_args(["setup-local-run"]).fn is cli_mod.cmd_setup_local_run
    assert p.parse_args(["__run-helper", "gh", "--", "api", "user"]).fn is cli_mod.cmd_run_helper


def test_run_parser_remainder():
    from treg import cli as cli_mod
    # argparse consumes the first `--` itself; flags after it survive verbatim into the remainder
    a = cli_mod.build_parser().parse_args(["run", "stripe", "--", "get", "/v1/balance", "--live"])
    assert a.fn is cli_mod.cmd_run and a.tool == "stripe"
    assert a.args == ["get", "/v1/balance", "--live"]
    # without the separator, args that don't collide with treg flags also pass through
    b = cli_mod.build_parser().parse_args(["run", "gh", "pr", "list"])
    assert b.args == ["pr", "list"]


async def test_owner_only_binding_and_runner_proof(clients: AsyncClient, monkeypatch):
    """A member can't bind/extract a teammate's secret (Bug 2), and can't read a shared key via a direct
    /grant without the isolated-runner proof (Bug 1)."""
    from treg.config import get_settings
    victim = (await clients.post("/secrets", json={"name": "admin-key", "kind": "env", "value": "ADMIN-SECRET"})).json()["id"]
    await clients.post("/tools", json={"name": "shared", "base_url": "http://upstream", "secret_id": victim,
                                       "cli": {"enabled": True, "bin": "printenv", "inject": [{"via": "env", "name": "X"}]}})
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    inv = (await clients.post(f"/orgs/{org_id}/invites", json={"email": "memberb@x.dev", "role": "member"})).json()
    tokB = (await clients.post("/invites/accept", json={"code": inv["code"], "email": "memberb@x.dev"})).json()["token"]
    hb = {"X-Treg-Token": tokB}

    # A1 — member B cannot bind the owner's secret to a tool B controls
    r = await clients.post("/tools", json={"name": "steal", "base_url": "http://upstream", "secret_id": victim}, headers=hb)
    assert r.status_code == 403 and "own" in r.text.lower()

    # A2 — B granting the owner's SHARED tool with no proof configured → refused
    r = await clients.post("/tools/shared/grant", json={"argv": []}, headers=hb)
    assert r.status_code == 403 and "treg-run runner" in r.text

    # A2 — with the server proof configured AND presented by the runner → allowed
    monkeypatch.setattr(get_settings(), "run_proof", "PROOF123")
    r = await clients.post("/tools/shared/grant", json={"argv": []}, headers={**hb, "X-Treg-Run-Proof": "PROOF123"})
    assert r.status_code == 200, r.text
    # wrong proof → still refused
    r = await clients.post("/tools/shared/grant", json={"argv": []}, headers={**hb, "X-Treg-Run-Proof": "nope"})
    assert r.status_code == 403
    # the OWNER never needs a proof (they hold the key already)
    r = await clients.post("/tools/shared/grant", json={"argv": []})
    assert r.status_code == 200, r.text


# ---- regression tests for the bug-hunt fixes ------------------------------------------------
async def test_grant_refuses_oauth_non_leaf_field(clients: AsyncClient):
    """A cli.inject may not release refresh_token / client_secret — only the access-token leaf."""
    blob = json.dumps({"access_token": "OK", "refresh_token": "RT", "expires_at": 0})
    await _mk_tool(clients, name="gx", kind="oauth", value=blob, cli={"enabled": True, "bin": "gx",
                   "inject": [{"via": "env", "name": "T", "secret_field": "refresh_token"}]})
    r = await clients.post("/tools/gx/grant", json={"argv": []})
    assert r.status_code >= 400 and "RT" not in r.text            # the refresh token never leaves


async def test_cli_bin_rejects_paths_and_shell(clients: AsyncClient):
    """cli.bin is exec'd (server-side and locally), so an absolute path / shell command is rejected
    at write time by validate_cli_profile."""
    t = (await clients.post("/tools", json={"name": "sk1", "base_url": "https://api.example.com"})).json()
    for bad in ("/bin/sh", "a; rm -rf /", "../evil"):
        r = await clients.patch(f"/tools/{t['id']}", json={"cli": {"bin": bad}})
        assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"
    ok = await clients.patch(f"/tools/{t['id']}", json={"cli": {"bin": "agentmail"}})
    assert ok.status_code == 200                                   # a plain command name is fine


def test_redact_argv_masks_short_flag_values_and_jwts():
    from treg.api import _redact_argv
    assert "Hunter2Hunter2" not in _redact_argv(["db", "--password", "Hunter2Hunter2"])
    assert "s3cret" not in _redact_argv(["x", "--token=s3cret"])
    jwt = "eyJhbGciOi.eyJzdWIiOiIxMjM0NTY3ODkw.SflKxwRJSMeKKF2QT4"
    assert jwt not in _redact_argv(["call", jwt])
    assert "get" in _redact_argv(["get", "/v1/balance"])           # ordinary args survive


def test_check_deny_matches_per_argument_anchored_pattern():
    # an anchored pattern a creator writes must still catch the flag as its own argument
    assert localrun.check_deny({"deny": [r"^--live$"]}, ["stripe", "--live"]) is not None
    assert localrun.check_deny({"deny": [r"^--live$"]}, ["stripe", "get"]) is None


def test_cmd_run_rejects_a_tier_flag_after_the_tool(monkeypatch):
    from treg import cli as cli_mod
    # a tier flag typed AFTER the tool (no `--`) → wrong tier silently, so treg errors
    monkeypatch.setattr(cli_mod.sys, "argv", ["treg", "run", "stripe", "--server", "list"])
    args = cli_mod.build_parser().parse_args(["run", "stripe", "--server", "list"])
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(args, {})
    assert "before the tool name" in str(e.value.code).lower()


def test_cmd_run_allows_vendor_flags_after_the_separator(monkeypatch):
    from treg import cli as cli_mod
    # `--timeout`/`--server` AFTER `--` legitimately belong to the vendor CLI — must NOT be blocked
    monkeypatch.setattr(cli_mod.sys, "argv", ["treg", "run", "db", "--", "fetch", "--timeout", "30"])
    args = cli_mod.build_parser().parse_args(["run", "db", "--", "fetch", "--timeout", "30"])
    fake = _FakeClient({"/grant": _FakeResp(500, {"detail": "stop-here"})})   # stop before real work
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: fake)
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(args, {"base_url": "http://x", "token": "t"})
    assert "before the tool name" not in str(e.value.code).lower()          # guard did NOT trip


async def test_run_slot_caps_concurrent_runs_per_user():
    from treg import runner
    import contextlib
    async with contextlib.AsyncExitStack() as stack:
        for _ in range(runner.MAX_CONCURRENT_RUNS_PER_USER):        # fill this user's slots
            await stack.enter_async_context(runner.run_slot("u@x"))
        with pytest.raises(runner.RunBusy):                          # one more → busy
            await stack.enter_async_context(runner.run_slot("u@x"))
    # slots released on exit → a fresh run works again
    async with runner.run_slot("u@x"):
        pass


def test_run_server_timeout_exits_nonzero(monkeypatch):
    from treg import cli as cli_mod
    fake = _FakeClient({"/run": _FakeResp(200, {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": True})})
    monkeypatch.setattr(cli_mod, "_client", lambda cfg: fake)
    args = cli_mod.build_parser().parse_args(["run", "--server", "sk", "--", "x"])
    with pytest.raises(SystemExit) as e:
        cli_mod.cmd_run(args, {"base_url": "http://x", "token": "t"})
    assert e.value.code == 1                                        # a timeout is never a success
