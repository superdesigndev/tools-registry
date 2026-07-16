"""The CLI is a thin client — unit-test parsing + the identity-config round-trip (no network)."""

from __future__ import annotations

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
    cli._save_config({"base_url": "https://treg.superdesign.dev", "token": "T", "email": "me@x.dev",
                      "active_org": "team-a", "identity": True})
    cfg = cli._load_config()
    assert cfg["token"] == "T" and cfg["active_org"] == "team-a" and cfg["identity"] is True


def test_legacy_multiorg_config_migrates(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": "https://treg.superdesign.dev", "active_org": "team-a",
        "orgs": {"team-a": {"token": "OLD", "org_id": 3}}}))
    cfg = cli._load_config()
    assert cfg["token"] == "OLD" and cfg["active_org"] == "team-a" and cfg["identity"] is False


def test_load_config_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "nope.json")
    cfg = cli._load_config()
    assert cfg["token"] is None and cfg["active_org"] is None and cfg["base_url"].startswith("http")


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
    args = type("A", (), {"client_secret": "/nonexistent/x.json", "name": "g", "scopes": []})()
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
