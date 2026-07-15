"""Tier 0 server-side CLI execution (`treg run --server`).

Runs a real CLI (`sh`) on the server with the tool's `cli.inject` secrets injected via env, never
onto the caller. The tool-side unification: one `Tool.cli` profile drives BOTH run tiers —
`cli.bin` is the entrypoint, `cli.inject` names the env vars; any tool with a profile is
server-runnable (no opt-in — the key never reaches the member; the bin allow-list gates). Covers:
env injection + output redaction, a scrubbed env (server secrets excluded), exit-code propagation,
the no-profile / missing-CLI / unknown-tool guards, timeout, audit, cross-org isolation, and the legacy bundle→tool migration fold. See docs/CLI-RUN-PLAN.md.
"""

from __future__ import annotations

from httpx import AsyncClient

from treg import audit


async def _register_runnable(c: AsyncClient, *, name="sh-skill", value="s3cr3t-value", bin="sh"):
    """A skill whose tool carries the full cli profile — the canonical runnable shape."""
    payload = {
        "name": name,
        "recipe": f"# {name}\n",
        "secrets": [{"local_name": "my-secret", "kind": "env", "value": value}],
        "tools": [{
            "name": name,
            "base_url": "https://api.example.com",
            "cli": {
                "bin": bin,
                "inject": [{"secret": "my-secret", "via": "env", "name": "MY_SECRET"}],
                "enabled": True,
            },
        }],
    }
    r = await c.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


async def test_run_rejects_a_command_not_on_the_allow_list(clients: AsyncClient):
    # `bash` is not a catalog CLI and not in the test allow-list → refused (no arbitrary code as the server)
    await _register_runnable(clients, name="bash-skill", bin="bash")
    r = await clients.post("/run", json={"tool": "bash-skill", "args": ["-c", "echo hi"]})
    assert r.status_code == 422 and "not approved for server runs" in r.text


async def test_server_run_applies_resource_limits(clients: AsyncClient):
    # The DoS half of the server-run sandbox: the child gets POSIX rlimits. Core dumps are OFF (a core
    # would spill the injected secret to disk) and the file-size limit is finite (disk-fill guard). We
    # read them back with the shell's own `ulimit`, which reports the limits the child was started with.
    await _register_runnable(clients, name="rl-skill")
    core = await clients.post("/run", json={"tool": "rl-skill", "args": ["-c", "ulimit -c"]})
    assert core.status_code == 200, core.text
    assert core.json()["stdout"].strip() == "0"  # RLIMIT_CORE = 0

    fsize = await clients.post("/run", json={"tool": "rl-skill", "args": ["-c", "ulimit -f"]})
    assert fsize.json()["stdout"].strip().isdigit()  # a finite block count, not "unlimited"


def test_spawn_preexec_toggles_with_the_setting(monkeypatch):
    # The rlimit preexec is applied only when TREG_RUN_RLIMITS is on (default). Off → no preexec.
    from treg import runner
    from treg.config import get_settings

    monkeypatch.setenv("TREG_RUN_RLIMITS", "false")
    get_settings.cache_clear()
    assert runner._spawn_preexec() is None
    monkeypatch.setenv("TREG_RUN_RLIMITS", "true")
    get_settings.cache_clear()
    assert callable(runner._spawn_preexec())
    get_settings.cache_clear()  # restore for the next test (env reverts via monkeypatch)


async def test_run_injects_secret_and_redacts(clients: AsyncClient):
    await _register_runnable(clients)
    r = await clients.post("/run", json={"tool": "sh-skill", "args": ["-c", "echo tok=$MY_SECRET"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["exit_code"] == 0
    # the secret WAS injected (echo printed it) but is REDACTED before leaving the server
    assert "tok=***" in d["stdout"]
    assert "s3cr3t-value" not in d["stdout"]


async def test_run_injects_under_the_cli_inject_name(clients: AsyncClient):
    # The importer stores slugified secret names (`stripe-api-key`); the inject entry carries the
    # env var the CLI actually reads (STRIPE_API_KEY). The runner must inject under the mapped name.
    payload = {
        "name": "slug-skill",
        "recipe": "# slug-skill\n",
        "secrets": [{"local_name": "slug-api-key", "kind": "env", "value": "slug-v4lue"}],
        "tools": [{
            "name": "slug-skill",
            "base_url": "https://api.example.com",
            "cli": {
                "bin": "sh",
                "inject": [{"secret": "slug-api-key", "via": "env", "name": "REAL_ENV_NAME"}],
                "enabled": True,
            },
        }],
    }
    r = await clients.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    r = await clients.post(
        "/run",
        json={"tool": "slug-skill", "args": ["-c", "echo mapped=${REAL_ENV_NAME:+yes}"]},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["exit_code"] == 0
    assert "mapped=yes" in d["stdout"]  # injected under the cli.inject name, not the stored slug


async def test_run_resolves_from_binding_and_sole_secret_injects(clients: AsyncClient):
    # An inject entry with NO explicit secret_id (the sole-bound-secret default, or from_binding)
    # must inject on --server exactly like the local grant path resolves it — one profile, two tiers.
    payload = {
        "name": "implicit",
        "recipe": "x",
        "secrets": [{"local_name": "only-key", "kind": "env", "value": "implicit-v4l"}],
        "tools": [{
            "name": "implicit",
            "base_url": "https://api.example.com",
            "bindings": [{"secret": "only-key", "name": "Authorization", "format": "Bearer {secret}"}],
            "cli": {"bin": "sh", "inject": [{"via": "env", "name": "ONLY_KEY"}], "enabled": True},
        }],
    }
    r = await clients.post("/skills", json=payload)
    assert r.status_code == 200, r.text
    r = await clients.post("/run", json={"tool": "implicit", "args": ["-c", "echo got=${ONLY_KEY:+yes}"]})
    assert r.status_code == 200, r.text
    assert "got=yes" in r.json()["stdout"]


async def test_run_scrubs_server_env(clients: AsyncClient, monkeypatch):
    # A var set on the treg process must NOT reach the child (the env is built fresh, not copied).
    monkeypatch.setenv("SERVER_ONLY_ENV", "leaky")
    await _register_runnable(clients)
    r = await clients.post("/run", json={"tool": "sh-skill", "args": ["-c", "echo v=${SERVER_ONLY_ENV:-absent}"]})
    assert r.status_code == 200, r.text
    assert r.json()["stdout"].strip() == "v=absent"


async def test_run_exit_code_propagates(clients: AsyncClient):
    await _register_runnable(clients)
    r = await clients.post("/run", json={"tool": "sh-skill", "args": ["-c", "exit 3"]})
    assert r.status_code == 200, r.text  # a non-zero CLI exit is a normal result, not an HTTP error
    assert r.json()["exit_code"] == 3


async def test_run_plain_http_tool_rejected(clients: AsyncClient):
    # an ordinary HTTP tool (no cli profile at all) can never be executed
    r = await clients.post("/tools", json={"name": "plainhttp", "base_url": "https://api.example.com"})
    assert r.status_code == 200, r.text
    r = await clients.post("/run", json={"tool": "plainhttp", "args": ["-c", "echo hi"]})
    assert r.status_code == 422
    assert "no CLI profile" in r.json()["detail"]


async def test_run_missing_cli_rejected(clients: AsyncClient):
    await _register_runnable(clients, name="ghost", bin="treg-nonexistent-bin-xyz")
    r = await clients.post("/run", json={"tool": "ghost", "args": []})
    assert r.status_code == 422
    assert "not installed" in r.json()["detail"]


async def test_run_unknown_tool_404(clients: AsyncClient):
    r = await clients.post("/run", json={"tool": "nope", "args": []})
    assert r.status_code == 404


async def test_run_timeout(clients: AsyncClient):
    await _register_runnable(clients)
    r = await clients.post("/run", json={"tool": "sh-skill", "args": ["-c", "sleep 5"], "timeout_s": 1})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timed_out"] is True
    assert d["exit_code"] != 0


async def test_run_is_audited(clients: AsyncClient):
    await _register_runnable(clients)
    await clients.post("/run", json={"tool": "sh-skill", "args": ["-c", "echo hi"]})
    await audit.drain()  # audit is fire-and-forget — flush before asserting
    r = await clients.get("/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["tool"] == "sh-skill"
    assert runs[0]["exit_code"] == 0
    assert runs[0]["argv"] == ["-c", "echo hi"]
    assert runs[0]["where"] == "server"  # a /run is a server-tier execution


async def test_runs_log_includes_local_runs_tagged_where(clients: AsyncClient):
    """The run audit unifies both tiers: a server /run AND a local grant both appear in /runs, each
    tagged `where`. Local successes carry no exit code (only failures report back) → exit_code null."""
    await _register_runnable(clients)  # sh-skill, server tier
    await clients.post("/run", json={"tool": "sh-skill", "args": ["ok"]})
    # a local run is audited as its grant (kind="local_run")
    g = await clients.post("/tools/sh-skill/grant", json={"argv": ["local", "args"]})
    assert g.status_code == 200, g.text
    await audit.drain()
    runs = (await clients.get("/runs")).json()
    where = {r["where"] for r in runs}
    assert where == {"server", "local"}
    local = next(r for r in runs if r["where"] == "local")
    assert local["tool"] == "sh-skill" and local["exit_code"] is None
    assert local["argv"] == ["local", "args"]


async def test_cli_profile_is_server_runnable_without_optin(clients: AsyncClient):
    # server runs need no per-tool flag: the key never reaches the member, and the bin
    # allow-list gates what may execute — a tool WITH a cli profile just runs.
    payload = {
        "name": "flip",
        "recipe": "x",
        "secrets": [{"local_name": "k", "kind": "env", "value": "vvv"}],
        "tools": [{
            "name": "flip",
            "base_url": "https://api.example.com",
            "cli": {"bin": "sh", "inject": [{"secret": "k", "via": "env", "name": "K"}], "enabled": False},
        }],
    }
    b = await clients.post("/skills", json=payload)
    assert b.status_code == 200, b.text
    r = await clients.post("/run", json={"tool": "flip", "args": ["-c", "echo hi"]})
    assert r.status_code == 200 and r.json()["exit_code"] == 0  # even with LOCAL runs off


async def test_run_cross_org_isolation(clients: AsyncClient):
    await _register_runnable(clients, name="sh-skill")
    # a second user (their own personal org) cannot see or run org A's tool
    r = await clients.post("/users", json={"email": "other@superdesign.dev"})
    tok2 = r.json()["token"]
    r = await clients.post("/run", json={"tool": "sh-skill", "args": []}, headers={"X-Treg-Token": tok2})
    assert r.status_code == 404
