"""The CLI is a thin client — unit-test parsing + the identity-config round-trip (no network)."""

from __future__ import annotations

import json

import json

import pytest

from treg import cli


@pytest.fixture(autouse=True)
def _isolate_cli_config(tmp_path, monkeypatch):
    """Never let a CLI test touch the real ~/.treg/config.json. Some commands persist config
    (e.g. _clear_active_if_targeted -> _save_config), so an un-isolated test would wipe the
    developer's own login mid-suite. Redirect CONFIG_PATH to a tmp file for every test here."""
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")


def test_parser_dispatches_core():
    p = cli.build_parser()
    assert p.parse_args(["login"]).fn is cli.cmd_login
    assert p.parse_args(["login", "--token", "T"]).token == "T"
    assert p.parse_args(["logout"]).fn is cli.cmd_logout
    assert p.parse_args(["secret", "add", "k", "--value", "v"]).fn is cli.cmd_secret_add
    assert p.parse_args(["tool", "add", "t", "--base-url", "http://x", "--secret", "1"]).fn is cli.cmd_tool_add
    assert p.parse_args(["call", "echo", "get", "--query", "a=1"]).fn is cli.cmd_call


def test_call_named_and_single_url():
    p = cli.build_parser()
    a = p.parse_args(["call", "echo", "v1/x", "--method", "POST"])
    assert a.target == "echo" and a.path == "v1/x" and a.method == "POST"
    b = p.parse_args(["call", "https://api.intercom.io/me"])
    assert b.target == "https://api.intercom.io/me" and b.path == ""


def test_org_parsers():
    p = cli.build_parser()
    assert p.parse_args(["org", "ls"]).fn is cli.cmd_org_ls
    assert p.parse_args(["org", "use", "team-a"]).slug == "team-a"
    assert p.parse_args(["org", "create", "Team A"]).fn is cli.cmd_org_create
    assert p.parse_args(["org", "invite", "b@x.dev", "--role", "viewer"]).role == "viewer"
    assert p.parse_args(["org", "set-role", "7", "admin"]).user_id == 7
    assert p.parse_args(["org", "invites"]).fn is cli.cmd_org_invites
    assert p.parse_args(["org", "revoke", "9"]).invite_id == 9
    d = p.parse_args(["org", "delete", "team-a"]); assert d.fn is cli.cmd_org_delete and d.slug == "team-a"
    assert p.parse_args(["org", "join", "inv_x", "--email", "b@x.dev"]).code == "inv_x"


def test_admin_and_skill_parsers():
    p = cli.build_parser()
    assert p.parse_args(["admin", "stats"]).fn is cli.cmd_admin_stats
    assert p.parse_args(["admin", "grant", "5"]).user_id == 5
    assert p.parse_args(["skill", "init", "--dir", "/s"]).fn is cli.cmd_skill_init
    assert p.parse_args(["skill", "add", "--dir", "/s"]).fn is cli.cmd_skill_add


def test_config_v2_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    cli._save_config({"base_url": "https://treg.to", "token": "T", "email": "me@x.dev",
                      "active_org": "team-a", "identity": True})
    cfg = cli._load_config()
    assert cfg["token"] == "T" and cfg["active_org"] == "team-a" and cfg["identity"] is True


def test_legacy_multiorg_config_migrates(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": "https://treg.to", "active_org": "team-a",
        "orgs": {"team-a": {"token": "OLD", "org_id": 3}}}))
    cfg = cli._load_config()
    assert cfg["token"] == "OLD" and cfg["active_org"] == "team-a" and cfg["identity"] is False


def test_load_config_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "nope.json")
    cfg = cli._load_config()
    assert cfg["token"] is None and cfg["active_org"] is None and cfg["base_url"].startswith("http")


def test_token_org_claim_reads_a_team_pinned_token():
    """`_token_org_claim` decodes the org slug a team-pinned identity token carries — without the
    signature check (the server still authorizes), and returning None on anything else."""
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": 7, "exp": 9999999999, "tv": 0, "org": "team-b"}).encode()
    ).decode().rstrip("=")
    assert cli._token_org_claim(f"{payload}.not-a-real-signature") == "team-b"
    assert cli._token_org_claim("tok-opaque-per-org") is None      # opaque membership token
    assert cli._token_org_claim(None) is None
    assert cli._token_org_claim("") is None


def test_pick_active_org_prefers_the_tokens_baked_org(monkeypatch):
    """Against an older server that marks nothing active for a team-pinned token, `_pick_active_org`
    must land on the token's own org — not the first membership, which for a multi-team user is an
    arbitrary other team (`treg login --token <superdesign key>` used to activate `oauth-test`)."""
    import base64

    class _Resp:
        status_code = 200
        def json(self):
            return [{"slug": "first-team", "active": False}, {"slug": "second-team", "active": False}]

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, path): return _Resp()

    monkeypatch.setattr(cli, "_client", lambda cfg: _Client())
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": 7, "exp": 9999999999, "tv": 0, "org": "second-team"}).encode()
    ).decode().rstrip("=")
    cfg = {"base_url": "http://x", "token": f"{payload}.sig", "active_org": None}
    cli._pick_active_org(cfg)
    assert cfg["active_org"] == "second-team"
    # an opaque token (no claim) still falls back to the first membership
    cfg = {"base_url": "http://x", "token": "tok-opaque", "active_org": None}
    cli._pick_active_org(cfg)
    assert cfg["active_org"] == "first-team"


def test_client_sends_token_and_active_org(monkeypatch):
    monkeypatch.setattr(cli, "_ORG_OVERRIDE", None)
    cfg = {"base_url": "http://x", "token": "TK", "active_org": "team-a"}
    with cli._client(cfg) as c:
        assert c.headers["X-Treg-Token"] == "TK" and c.headers["X-Treg-Org"] == "team-a"


def test_org_override_beats_active(monkeypatch):
    monkeypatch.setattr(cli, "_ORG_OVERRIDE", "team-b")
    cfg = {"base_url": "http://x", "token": "TK", "active_org": "team-a"}
    assert cli._effective_org(cfg) == "team-b"
    with cli._client(cfg) as c:
        assert c.headers["X-Treg-Org"] == "team-b"


def test_pop_org_flag():
    a = ["tool", "ls", "--org", "team-b"]; assert cli._pop_org_flag(a) == "team-b" and a == ["tool", "ls"]
    b = ["tool", "ls", "--org=team-c"]; assert cli._pop_org_flag(b) == "team-c" and b == ["tool", "ls"]
    assert cli._pop_org_flag(["x"]) is None


def test_admin_client_prefers_admin_token():
    cfg = {"base_url": "http://x", "admin_token": "ENV", "token": "USER"}
    with cli._admin_client(cfg) as c:
        assert c.headers["X-Treg-Token"] == "ENV"
    del cfg["admin_token"]
    with cli._admin_client(cfg) as c:
        assert c.headers["X-Treg-Token"] == "USER"


def test_org_delete_requires_matching_slug():
    cfg = {"base_url": "http://x", "token": "T", "active_org": "team-a"}
    args = type("A", (), {"slug": "wrong"})()
    with pytest.raises(SystemExit):
        cli.cmd_org_delete(args, cfg)


# ---- bug-hunt regressions -----------------------------------------------------------------
def test_corrupt_config_does_not_brick_the_cli(tmp_path, monkeypatch):
    """A half-written / hand-broken config must load as empty, not JSONDecodeError on every run."""
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{ this is not valid json")
    cfg = cli._load_config()  # must not raise
    assert cfg["token"] is None and cfg["base_url"].startswith("http")


def test_save_config_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    cli._save_config({"base_url": "http://x", "token": "T"})
    assert not (tmp_path / "config.json.tmp").exists()  # temp renamed away, no litter
    assert cli._load_config()["token"] == "T"


class _FakeResp:
    status_code = 200
    def json(self): return {}
    text = "{}"


class _FakeClient:
    def __init__(self): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def request(self, method, url, params=None, content=None, headers=None):
        self.calls.append((method, url, params, content, headers)); return _FakeResp()


def test_call_preserves_duplicate_query_keys(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    monkeypatch.setattr(cli, "_show", lambda r: None)
    args = cli.build_parser().parse_args(["call", "echo", "--query", "tag=a", "--query", "tag=b"])
    cli.cmd_call(args, {"base_url": "http://x"})
    _, _, params, _, _ = fake.calls[0]
    assert params == [("tag", "a"), ("tag", "b")]  # both survive; a dict would drop tag=a


def test_call_query_without_equals_exits_cleanly(monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda cfg: _FakeClient())
    args = cli.build_parser().parse_args(["call", "echo", "--query", "flag"])
    with pytest.raises(SystemExit):
        cli.cmd_call(args, {"base_url": "http://x"})


# ---- cycle-2 CLI regressions --------------------------------------------------------------
def test_parse_bind_non_int_secret_exits():
    with pytest.raises(SystemExit):
        cli._parse_bind("secret=abc")


def test_load_json_arg_bad_json_exits():
    with pytest.raises(SystemExit):
        cli._load_json_arg("{bad", "binding")


def test_pop_org_flag_missing_value_exits():
    with pytest.raises(SystemExit):
        cli._pop_org_flag(["tool", "ls", "--org"])


def test_oauth_connect_missing_file_exits():
    args = type("A", (), {"client_secret": "/nonexistent/x.json", "name": "g", "scopes": [],
                          "provider": None, "capability": None})()
    with pytest.raises(SystemExit):
        cli.cmd_oauth_connect(args, {"base_url": "http://x"})


def test_oauth_connect_without_provider_or_client_secret_exits():
    """Registry mode needs --provider; BYO needs --client-secret. Neither is a usage error."""
    args = type("A", (), {"client_secret": None, "name": None, "scopes": [],
                          "provider": None, "capability": None})()
    with pytest.raises(SystemExit):
        cli.cmd_oauth_connect(args, {"base_url": "http://x"})


def test_skill_push_missing_file_exits():
    args = type("A", (), {"file": "/nonexistent/skill.json"})()
    with pytest.raises(SystemExit):
        cli.cmd_skill_push(args, {"base_url": "http://x"})


def test_clear_active_only_when_targeted(monkeypatch):
    # a one-shot --org override on a DIFFERENT org must not wipe the stored active org
    monkeypatch.setattr(cli, "_ORG_OVERRIDE", "beta")
    cfg = {"active_org": "alpha"}
    cli._clear_active_if_targeted(cfg)
    assert cfg["active_org"] == "alpha"  # untouched
    # acting on the stored active org clears it
    monkeypatch.setattr(cli, "_ORG_OVERRIDE", None)
    cli._clear_active_if_targeted(cfg)
    assert cfg["active_org"] is None


def test_find_env_upwards_locates_project_env(tmp_path):
    """A skills dir sits UNDER a project whose .env is at the root — the walk-up must find it so
    env-credentialed skills (render/vercel) aren't gapped 'needs env var … not found'."""
    (tmp_path / ".env").write_text("RENDER_API_KEY=x\n")
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    found = cli._find_env_upwards(str(skills))
    assert found == str(tmp_path / ".env")
    # a path nested deeper under the project still resolves to the same root .env
    assert cli._find_env_upwards(str(skills / "render")) == str(tmp_path / ".env")


def test_secret_add_env_var_parses_and_strips_quotes(tmp_path, monkeypatch):
    """`secret add --env-var` reads ONE var from an .env via treg's parser — a quoted value
    (AGENTMAIL_API_KEY="am_…") is stored WITHOUT the quotes (the bug agents hit hand-extracting)."""
    (tmp_path / ".env").write_text('AGENTMAIL_API_KEY="am_us_pod_QUOTED"\nOTHER=nope\n')
    posted = {}

    class _FakeResp:
        status_code = 200
        def json(self): return {"id": 1}

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, path, json): posted.update(json); return _FakeResp()

    monkeypatch.setattr(cli, "_client", lambda cfg: _FakeClient())
    args = type("A", (), {"name": "agentmail-key", "env_var": "AGENTMAIL_API_KEY",
                          "env_file": str(tmp_path / ".env"), "dir": None, "file": None,
                          "value": None, "kind": "env"})()
    cli.cmd_secret_add(args, {"base_url": "http://x", "token": "t"})
    assert posted["value"] == "am_us_pod_QUOTED"  # no surrounding quotes
    assert posted["name"] == "agentmail-key" and posted["kind"] == "env"


def test_onboard_setup_import_args_are_complete():
    """`_run_setup` builds upload args from build_parser() so it can't drift out of sync with new
    upload flags (regression: a hand-built Namespace missing `no_oauth` crashed onboarding Set up)."""
    a = cli.build_parser().parse_args(["upload"])
    for attr in ("no_oauth", "llm", "llm_token", "llm_model", "llm_base_url",
                 "dry_run", "all", "select", "replace", "env_file", "skills_dir", "mode"):
        assert hasattr(a, attr), f"upload args missing {attr}"


def test_scan_upload_import_verbs():
    """`treg scan` is the read-only preview (forced dry_run, no prompts); `treg upload` is the real
    thing; `treg import` stays a working alias of upload (old docs/scripts must not break)."""
    p = cli.build_parser()
    s = p.parse_args(["scan"])
    assert s.dry_run and s.as_scan and s.all and s.no_oauth and s.fn is cli.cmd_import
    u = p.parse_args(["upload"])
    assert not u.dry_run and not u.as_scan and u.fn is cli.cmd_import
    i = p.parse_args(["import", "--dry-run"])   # alias keeps full flag surface
    assert i.dry_run and not i.as_scan and i.cmd == "import" and i.fn is cli.cmd_import


def test_onboard_setup_source_picks_scan_dirs(tmp_path, monkeypatch):
    """`treg onboard --path setup --source global` scans the machine-wide agent skill folders
    (~/.claude/skills, …); `--source local` keeps the project-only scan; `--source both` unions them."""
    gdir = tmp_path / "home" / ".claude" / "skills"
    (gdir / "globskill").mkdir(parents=True)
    (gdir / "globskill" / "SKILL.md").write_text("---\nname: globskill\n---\nhi")
    proj = tmp_path / "proj"
    local = proj / ".claude" / "skills" / "localskill"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: localskill\n---\nhi")
    monkeypatch.chdir(proj)

    from treg import agents as ag
    monkeypatch.setattr(ag, "detect_installed", lambda: ["claude-code"])
    monkeypatch.setattr(ag, "global_dir", lambda a: gdir)
    monkeypatch.setattr(cli, "_onboard_active_org", lambda cfg: {"name": "T", "slug": "t", "role": "admin"})
    scanned: list[list[str]] = []
    monkeypatch.setattr(cli, "_import_skills", lambda args, cfg, dirs, env: scanned.append([str(d) for d in dirs]))
    monkeypatch.setattr(cli, "_client", lambda cfg: (_ for _ in ()).throw(RuntimeError("no server")))

    def run(source):
        scanned.clear()
        args = cli.build_parser().parse_args(["onboard", "--path", "setup", "--source", source])
        cli._run_setup({"base_url": "http://x", "token": "t"}, args)
        return scanned[0] if scanned else []

    assert run("global") == [str(gdir)]
    got_local = run("local")
    assert str(gdir) not in got_local and any(d.endswith("skills") for d in got_local)
    got_both = run("both")
    assert str(gdir) in got_both and got_both != [str(gdir)]


def test_only_resolvable_gaps():
    mk = lambda gaps: type("D", (), {"gaps": gaps})()
    assert cli._only_resolvable_gaps(mk([]))                                  # no gaps → checkable
    assert cli._only_resolvable_gaps(mk(["needs env var STRIPE_KEY — not found in the env"]))  # env-var → fixable
    assert not cli._only_resolvable_gaps(mk(["treg.json secret file missing: token.json"]))    # file gap → not


def test_prompt_missing_skill_creds_fills_values_and_clears_gaps(monkeypatch):
    """A skill needing an env var absent from .env prompts for it; on a value, the var lands in
    `values` and the gap clears so the skill registers. Blank input leaves it skipped."""
    answers = iter(["dev-token-123", ""])  # first var answered, second left blank
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    good = type("D", (), {"gaps": ["needs env var GOOGLE_ADS_DEVELOPER_TOKEN — not found in the env"]})()
    skip = type("D", (), {"gaps": ["needs env var INTERCOM_TOKEN — not found in the env"]})()
    values = {}
    cli._prompt_missing_skill_creds([good, skip], values)
    assert values == {"GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token-123"}  # only the answered one
    assert good.gaps == []                                            # resolved → will register
    assert skip.gaps and "INTERCOM_TOKEN" in skip.gaps[0]            # blank → still gapped, still skipped


def test_load_catalog_prefers_newer_bundled_over_older_server(tmp_path, monkeypatch):
    """A CLI updated ahead of its server must NOT regress to the server's older catalog (which lacks new
    CLIs + auth_mechanism/detect). _load_catalog uses whichever is newer by CATALOG_VERSION."""
    from treg import providers as prov
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")

    class _Resp:
        status_code = 200
        text = '{"version": 1, "providers": [{"provider": "OldOnly"}]}'
        def json(self): return {"version": 1, "providers": [{"provider": "OldOnly"}]}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, path): return _Resp()

    monkeypatch.setattr(cli, "_client", lambda cfg, auth=False: _C())
    cat = cli._load_catalog({"base_url": "http://old-server"})
    assert cat is prov.CATALOG  # bundled (v8) wins over the server's v1

    # …but a server that's newer/equal wins (it can grow without a CLI release)
    class _RespNew(_Resp):
        text = '{"version": 999, "providers": [{"provider": "NewServer"}]}'
        def json(self): return {"version": 999, "providers": [{"provider": "NewServer"}]}
    monkeypatch.setattr(cli, "_client", lambda cfg, auth=False: type("X", (_C,), {"get": lambda s, p: _RespNew()})())
    cat2 = cli._load_catalog({"base_url": "http://new-server"})
    assert cat2 == [{"provider": "NewServer"}]


# ---- shared-key output redaction (the streaming scrubber) ---------------------------------
def test_stream_redactor_scrubs_across_chunk_boundary():
    r = cli._StreamRedactor([b"SEKRET"])
    out = r.feed(b"before SEK") + r.feed(b"RET after") + r.flush()
    assert out == b"before *** after" and b"SEKRET" not in out


def test_stream_redactor_passthrough_when_no_secret():
    r = cli._StreamRedactor([b"KEY"])
    assert r.feed(b"hello world") + r.flush() == b"hello world"


def test_stream_redactor_empty_secrets_is_passthrough():
    r = cli._StreamRedactor([])
    assert r.feed(b"anything at all") + r.flush() == b"anything at all"


def test_traversable_by_others_root_yes_missing_no():
    # world-traversable → True; a missing path (OSError) → False (conservative)
    assert cli._traversable_by_others("/") is True
    assert cli._traversable_by_others("/no/such/path/zzz") is False


def test_org_access_and_invite_access_parsers():
    p = cli.build_parser()
    a = p.parse_args(["org", "access", "5", "--tools", "stripe,gh", "--local-run", "off"])
    assert a.fn is cli.cmd_org_access and a.user_id == 5 and a.tools == "stripe,gh" and a.local_run == "off"
    b = p.parse_args(["org", "invite", "x@y.z", "--all-tools", "--local-run", "off"])
    assert b.fn is cli.cmd_org_invite and b.all_tools is True and b.local_run == "off"


def test_call_content_type_flag_and_json_sniff(monkeypatch):
    """`call` sends a Content-Type: explicit --content-type wins; else a JSON body sniffs to
    application/json (npm publish et al. reject an untyped body); a non-JSON body sends none."""
    p = cli.build_parser()
    assert p.parse_args(["call", "t", "p", "--content-type", "text/plain"]).content_type == "text/plain"

    fake = _FakeClient()
    monkeypatch.setattr(cli, "_client", lambda cfg: fake)
    monkeypatch.setattr(cli, "_show", lambda r: None)
    cfg = {"base_url": "http://x", "token": "T"}

    def sent_headers(*extra) -> dict:
        cli.cmd_call(p.parse_args(["call", "t", "p", "--method", "PUT", *extra]), cfg)
        return fake.calls[-1][4]

    assert sent_headers("--data", '{"ok":1}') == {"content-type": "application/json"}  # sniffed from JSON body
    assert sent_headers("--data", "plain text") == {}  # non-JSON body: no guess
    assert sent_headers("--data", "plain", "--content-type", "text/csv") == {"content-type": "text/csv"}  # flag wins


# ---- command consolidation: grouped help + hidden back-compat aliases ----------------------
def test_new_command_map_routes():
    """The consolidated IA: `cli` wraps the run/shell tier, `connections` absorbed `oauth`,
    `audit` unifies the two audit logs."""
    p = cli.build_parser()
    assert p.parse_args(["cli", "run", "stripe", "--", "get", "/v1/balance"]).fn is cli.cmd_run
    assert p.parse_args(["cli", "runs"]).fn is cli.cmd_runs
    assert p.parse_args(["cli", "shell", "start"]).fn is cli.cmd_shell_start
    assert p.parse_args(["cli", "shell", "stop"]).fn is cli.cmd_shell_stop
    assert p.parse_args(["cli", "setup"]).fn is cli.cmd_setup_local_run
    assert p.parse_args(["connections"]).fn is cli.cmd_connections_ls        # bare = list
    assert p.parse_args(["connections", "ls"]).fn is cli.cmd_connections_ls
    assert p.parse_args(["connections", "connect", "--provider", "gsc"]).fn is cli.cmd_oauth_connect
    assert p.parse_args(["connections", "providers"]).fn is cli.cmd_oauth_providers
    a = p.parse_args(["audit", "--limit", "20"])
    assert a.fn is cli.cmd_audit and a.limit == 20 and a.calls is False and a.runs is False


def test_hidden_aliases_still_parse_and_route():
    """Every removed/renamed top-level command keeps working for existing scripts + agent
    instructions — it is only absent from --help."""
    p = cli.build_parser()
    for argv, fn in [
        (["add", "stripe", "--base-url", "https://api.stripe.com", "--secret", "STRIPE_KEY"], cli.cmd_add),
        (["oauth", "providers"], cli.cmd_oauth_providers),
        (["oauth", "connect", "--provider", "gsc"], cli.cmd_oauth_connect),
        (["run", "stripe", "--", "get", "/v1/balance"], cli.cmd_run),
        (["runs"], cli.cmd_runs),
        (["calls"], cli.cmd_calls),
        (["shell", "start"], cli.cmd_shell_start),
        (["shell", "stop"], cli.cmd_shell_stop),
        (["setup-local-run"], cli.cmd_setup_local_run),
        (["import"], cli.cmd_import),
    ]:
        assert p.parse_args(argv).fn is fn, argv


def test_alias_flags_match_their_canonical_command():
    """An alias is the SAME parser, not a stub: its flags must still parse."""
    p = cli.build_parser()
    assert p.parse_args(["run", "--server", "sk", "--", "x"]).server is True
    assert p.parse_args(["cli", "run", "--server", "sk", "--", "x"]).server is True
    assert p.parse_args(["shell", "start", "--ttl", "60"]).ttl == 60
    assert p.parse_args(["cli", "shell", "start", "--ttl", "60"]).ttl == 60
    assert p.parse_args(["setup-local-run", "--run-proof", "P"]).run_proof == "P"
    assert p.parse_args(["cli", "setup", "--run-proof", "P"]).run_proof == "P"
    assert p.parse_args(["runs", "--limit", "5"]).limit == 5
    assert p.parse_args(["calls", "--limit", "5"]).limit == 5


def test_help_is_grouped_and_hides_aliases():
    help_ = cli.build_parser().format_help()
    # The order IS the pitch: what you can do with no setup (the catalog) comes before what you
    # have to register yourself. It is the approved IA, not argparse's registration order.
    headers = ("THE CATALOG", "YOUR OWN TOOLS", "ON YOUR MACHINE", "BULK UPLOAD",
               "TEAM MANAGEMENT", "CONFIG")
    for header in headers:
        assert f"\n{header}" in help_, header
    positions = [help_.index(h) for h in headers]
    assert positions == sorted(positions)
    # and `catalog` is the first command a reader meets
    assert help_.index("    catalog") < help_.index("    tool")
    listed = {ln.split()[0] for ln in help_.splitlines() if ln.startswith("    ") and ln.strip()}
    for gone in ("add", "oauth", "setup-local-run", "run", "runs", "calls", "shell", "import"):
        assert gone not in listed, gone
    for shown in ("catalog", "tool", "connections", "cli", "audit", "org", "config", "version"):
        assert shown in listed, shown


def test_help_groups_only_name_real_commands():
    """HELP_GROUPS is hand-written copy; keep it from drifting off the real subparsers."""
    p = cli.build_parser()
    choices = next(a.choices for a in p._subparsers._group_actions if a.choices)
    for _title, rows in cli.HELP_GROUPS:
        for name, desc in rows:
            assert name in choices, name
            assert desc.endswith("."), name


def test_audit_merges_calls_and_runs(monkeypatch, capsys):
    """The default `audit` view interleaves both logs by time, and drops the `local_run`
    CallRecords that /runs already reports as its local rows (else each shows up twice)."""
    class _Resp:
        status_code = 200
        def __init__(self, body): self._b = body
        def json(self): return self._b

    bodies = {
        "/calls": [
            {"id": 2, "user_email": "a@x.dev", "tool_name": "stripe", "method": "GET",
             "path": "v1/charges", "status_code": 200, "kind": "call", "created_at": "2026-07-28T10:00:00"},
            {"id": 1, "user_email": "a@x.dev", "tool_name": "gh", "method": "GRANT",
             "path": "pr list", "status_code": 200, "kind": "local_run", "created_at": "2026-07-28T09:00:00"},
        ],
        "/runs": [
            {"id": "l1", "user_email": "a@x.dev", "tool": "gh", "argv": ["pr", "list"],
             "exit_code": None, "where": "local", "created_at": "2026-07-28T09:00:00"},
        ],
    }

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None): return _Resp(bodies[url])

    monkeypatch.setattr(cli, "_client", lambda cfg: _C())
    args = cli.build_parser().parse_args(["audit"])
    cli.cmd_audit(args, {"base_url": "http://x"})
    rows = json.loads(capsys.readouterr().out)
    assert [r["kind"] for r in rows] == ["call", "run"]          # newest first
    assert rows[0]["detail"] == "GET v1/charges" and rows[0]["where"] == "proxy"
    assert rows[1]["detail"] == "pr list" and rows[1]["where"] == "local"


def test_audit_filters_delegate_to_the_single_source_views(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_calls", lambda a, c: seen.append("calls"))
    monkeypatch.setattr(cli, "cmd_runs", lambda a, c: seen.append("runs"))
    p = cli.build_parser()
    cli.cmd_audit(p.parse_args(["audit", "--calls"]), {})
    cli.cmd_audit(p.parse_args(["audit", "--runs"]), {})
    assert seen == ["calls", "runs"]
    with pytest.raises(SystemExit):        # the two filters are mutually exclusive
        p.parse_args(["audit", "--calls", "--runs"])


def test_subcommand_help_is_not_the_grouped_top_level_page():
    """add_subparsers clones the PARENT's class by default, which would make every `treg X -h`
    print the top-level grouped page. The subparsers must stay plain ArgumentParsers."""
    p = cli.build_parser()
    choices = next(a.choices for a in p._subparsers._group_actions if a.choices)
    for name in ("cli", "call", "connections", "audit", "tool"):
        sub_help = choices[name].format_help()
        assert "MARKETPLACE" not in sub_help, name
        assert sub_help.startswith(f"usage: treg {name}"), name
    assert "run" in choices["cli"].format_help()      # its OWN subcommands, though


# ---- catalog search / get (the discover -> inspect pair) ----------------------------------
def test_catalog_verbs_parse_without_displacing_a_platform_slug():
    """`search`/`get` are positional verbs, not subparsers — so `treg catalog tiktok` still browses
    a shelf, and the multi-word query needs no quoting."""
    p = cli.build_parser()
    a = p.parse_args(["catalog", "search", "tiktok", "comments", "--limit", "5"])
    assert a.fn is cli.cmd_catalog and a.platform == "search" and a.rest == ["tiktok", "comments"] and a.limit == 5
    g = p.parse_args(["catalog", "get", "tikhub.tiktok.video.comments"])
    assert g.platform == "get" and g.rest == ["tikhub.tiktok.video.comments"]
    assert p.parse_args(["catalog", "tiktok"]).rest == []
    assert p.parse_args(["catalog"]).platform is None


class _CatalogResp:
    def __init__(self, body, status=200): self._b, self.status_code = body, status
    def json(self): return self._b


def _stub_catalog_client(monkeypatch, routes: dict):
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None): return routes[url]
    monkeypatch.setattr(cli, "_client", lambda cfg, auth=True: _C())


_SEARCH_BODY = {
    "query": "tiktok comments", "count": 2, "total": 9,
    "results": [
        {"id": "justoneapi.tiktok.video.comments", "provider": "justoneapi", "provider_display": "JustOneAPI",
         "summary": "A post's comments, cursor-paginated", "method": "GET", "path": "/api/x", "scope": "any_account",
         "tier": "core", "cost": {"type": "per_success", "value": 0.1, "currency": "CNY", "usd": 0.014},
         "verified": "2026-07-28", "docs_url": "", "has_example": True, "input": None,
         "capability": "tiktok.video.comments", "capability_description": "List a video's comments",
         "platform": "tiktok", "platform_label": "TikTok", "score": 6},
        {"id": "tikhub.x.douyin-web-fetch-video-comments", "provider": "tikhub", "provider_display": "TikHub",
         "summary": "Douyin comments", "method": "GET", "path": "/api/y", "scope": "any_account",
         "tier": "extended", "cost": None, "verified": None, "docs_url": "", "has_example": False, "input": None,
         "capability": "", "capability_description": "", "platform": "douyin", "platform_label": "Douyin",
         "score": 5},
    ],
    "hints": ["treg catalog get justoneapi.tiktok.video.comments   # params, cost, example response"],
}


def test_catalog_search_table_prices_in_usd_and_points_at_get(monkeypatch, capsys):
    """The table has to be comparable down the COST column — the yaml's own CNY/USD mix isn't."""
    _stub_catalog_client(monkeypatch, {"/catalog/search": _CatalogResp(_SEARCH_BODY)})
    args = cli.build_parser().parse_args(["catalog", "search", "tiktok", "comments"])
    cli.cmd_catalog(args, {"base_url": "http://x"})
    out = capsys.readouterr().out
    assert "9 matches" in out and "showing 2" in out
    assert "justoneapi.tiktok.video.comments" in out and "$0.014/success" in out
    assert "●" in out, "connected state is the actionable glyph (verified/tier are maintenance metadata)"
    assert "treg catalog get justoneapi.tiktok.video.comments" in out


def test_catalog_search_says_what_to_try_when_nothing_matches(monkeypatch, capsys):
    _stub_catalog_client(monkeypatch, {"/catalog/search": _CatalogResp(
        {"query": "zzz", "count": 0, "total": 0, "results": [], "hints": []})})
    cli.cmd_catalog(cli.build_parser().parse_args(["catalog", "search", "zzz"]), {"base_url": "http://x"})
    out = capsys.readouterr().out
    assert "nothing matches" in out and "different task words" in out


def test_catalog_get_renders_params_siblings_and_the_command(monkeypatch, capsys):
    body = {
        "endpoint": {
            "id": "justoneapi.tiktok.video.comments", "provider": "justoneapi", "provider_display": "JustOneAPI",
            "summary": "A post's comments", "method": "GET", "path": "/api/tiktok/get-post-comment/v1",
            "scope": "any_account", "tier": "core",
            "cost": {"type": "per_success", "value": 0.1, "currency": "CNY", "usd": 0.014, "note": "billed on success"},
            "verified": "2026-07-28", "docs_url": "https://docs.example/comments", "has_example": True,
            "input": {"queryParams": {
                "awemeId": {"type": "string", "required": True, "note": "the post id", "example": "76662"},
                "cursor": {"type": "string", "required": False, "note": "'0' for the first page"}}},
            "capability": "tiktok.video.comments", "capability_description": "List a video's comments",
            "platform": "tiktok", "platform_label": "TikTok"},
        "provider": {"service": "justoneapi", "display_name": "JustOneAPI",
                     "pricing_url": "https://justoneapi.com/", "limits": "60 req/min"},
        "siblings": [{"id": "tikhub.tiktok.video.comments", "provider": "tikhub", "provider_display": "TikHub",
                      "summary": "s", "method": "GET", "path": "/p", "scope": "any_account", "tier": "core",
                      "cost": {"type": "per_call", "value": 0.001, "currency": "USD", "usd": 0.001},
                      "verified": "2026-07-28", "docs_url": "", "has_example": True, "input": None}],
        "call_template": "treg call justoneapi /api/tiktok/get-post-comment/v1 --query awemeId=76662",
        "example_response": {"code": 0, "data": {"comments": [{"text": "hi"}]}},
        "hints": [],
    }
    _stub_catalog_client(monkeypatch, {"/catalog/endpoints/justoneapi.tiktok.video.comments": _CatalogResp(body)})
    cli.cmd_catalog(cli.build_parser().parse_args(
        ["catalog", "get", "justoneapi.tiktok.video.comments"]), {"base_url": "http://x"})
    out = capsys.readouterr().out
    assert "$0.014/success" in out and "(CNY 0.1)" in out          # usd to compare, original to verify
    provider = body["provider"]
    assert provider["limits"] in out and provider["pricing_url"] in out
    assert "tikhub.tiktok.video.comments" in out                    # the sibling, for comparison
    assert "awemeId" in out and "the post id" in out and "e.g. 76662" in out
    assert "treg call justoneapi /api/tiktok/get-post-comment/v1 --query awemeId=76662" in out
    assert '"comments"' in out                                      # the example response, inline


def test_a_credit_price_reads_as_dollars_with_the_credits_behind_it():
    """A bare credit count is not a price — a reader cannot compare "1 credit" to "$0.001". When
    the server priced the credit, dollars lead; only an unpriced credit shows alone."""
    priced = {"type": "per_call", "value": 1, "currency": "credit", "usd": 0.00188}
    assert cli._cost_label(priced) == "$0.00188/call (1 credit)"
    assert cli._cost_usd(priced) == "$0.00188/call"                 # narrow comparison column

    unpriced = {"type": "per_success", "value": 3, "currency": "credit", "usd": None}
    assert cli._cost_label(unpriced) == "3 credits/success"         # native, labelled as credits
    assert cli._cost_usd(unpriced) == "3 credits/success"
    assert "$" not in cli._cost_usd(unpriced), "no rate, no invented dollar figure"


def test_catalog_get_needs_an_id_and_404s_helpfully(monkeypatch, capsys):
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        cli.cmd_catalog(p.parse_args(["catalog", "get"]), {"base_url": "http://x"})
    _stub_catalog_client(monkeypatch, {"/catalog/endpoints/nope.x": _CatalogResp({}, status=404)})
    with pytest.raises(SystemExit):
        cli.cmd_catalog(p.parse_args(["catalog", "get", "nope.x"]), {"base_url": "http://x"})
    assert "find one with: treg catalog search" in capsys.readouterr().out


# ---- onboarding path 1: the catalog ---------------------------------------------------------
class _CatResp:
    def __init__(self, payload, status=200, text=None):
        self._payload, self.status_code = payload, status
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class _CatClient:
    """Stands in for `_client(cfg)`: answers the three endpoints the catalog path uses."""

    def __init__(self, access: dict, call_status: int = 200):
        self.access, self.call_status = access, call_status
        self.seen: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, path, **kw):
        self.seen.append(path)
        if path.startswith("/catalog/search"):
            return _CatResp({"results": [{"id": "tikhub.tiktok.user.profile", "provider": "tikhub",
                                           "summary": "Public TikTok profile"}]})
        if path.endswith("/access"):
            return _CatResp(self.access)
        if path.endswith("/balance"):
            return _CatResp({"balance_micro": 999_000, "blocks": [], "entries": []})
        return _CatResp({}, 404)

    def request(self, method, path, **kw):
        self.seen.append(f"{method} {path}")
        return _CatResp({"ok": True}, self.call_status, text='{"ok": true}')


def _onboard_catalog(monkeypatch, access, call_status=200):
    client = _CatClient(access, call_status)
    monkeypatch.setattr(cli, "_client", lambda cfg: client)
    monkeypatch.setattr(cli, "_active_org_id", lambda cfg, c: 1)
    args = cli.build_parser().parse_args(["onboard", "--path", "catalog", "--yes"])
    cli._run_catalog({"base_url": "http://x", "token": "t"}, args)
    return client


def test_onboard_catalog_calls_when_treg_serves_it(monkeypatch, capsys):
    """The happy path: treg's own key covers it, so the price is stated and the call is made."""
    c = _onboard_catalog(monkeypatch, {"tier": "platform", "estimated_cost_usd": 0.001})
    out = capsys.readouterr().out
    assert "on treg's key" in out and "$0.001" in out
    assert "GET /call/tikhub.tiktok.user.profile" in c.seen      # it really called
    assert "treg balance" in out                                  # and showed what it cost


def test_onboard_catalog_says_not_metered_for_your_own_key(monkeypatch, capsys):
    c = _onboard_catalog(monkeypatch, {"tier": "credential"})
    out = capsys.readouterr().out
    assert "OWN credential" in out and "not metered" in out
    assert any(s.startswith("GET /call/") for s in c.seen)
    assert "treg balance" not in out          # nothing was spent, so don't show a balance


def test_onboard_catalog_dead_end_names_the_one_command_that_fixes_it(monkeypatch, capsys):
    """Before platform providers are switched on (TREG_PLATFORM_PROVIDERS empty) this is what every
    new user sees — so it must not read like a failure, and must never pretend to call."""
    c = _onboard_catalog(monkeypatch, {"tier": "none", "detail": "no tikhub credential in this org yet"})
    out = capsys.readouterr().out
    assert "treg connections connect --provider tikhub" in out
    assert not any(s.startswith("GET /call/") for s in c.seen)   # nothing was called
