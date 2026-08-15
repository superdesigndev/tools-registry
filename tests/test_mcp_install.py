"""`treg mcp install` — registering the treg MCP server into agents, header-authed, user-global.

The header path is deliberate: treg 200s a valid Authorization header, so a client never falls back
to OAuth discovery (verified against Claude Code). These tests lock the config each agent actually
writes and the user-global scope — a project-scoped MCP entry is a per-repo surprise, not a setup.
"""
from __future__ import annotations

import json

from treg import mcp_install


def test_json_agents_write_user_global_header_config(tmp_path, monkeypatch):
    """Cursor + opencode: a header-authed entry in the per-USER config file (not a project ./ file),
    merged so anything already there survives, and idempotent on re-run."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # pre-existing unrelated servers must be preserved
    cur = tmp_path / ".cursor"; cur.mkdir()
    (cur / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"url": "http://x"}}}))
    (tmp_path / ".config" / "opencode").mkdir(parents=True)

    out = mcp_install.install_mcp(base_url="https://treg.to", token="TESTKEY",
                                  only=["cursor", "opencode"])
    got = {d: (s, detail) for d, s, detail in out["results"]}
    assert got["Cursor"][0] == "ok" and got["opencode"][0] == "ok", out

    cursor = json.loads((cur / "mcp.json").read_text())
    assert cursor["mcpServers"]["other"] == {"url": "http://x"}          # untouched
    treg = cursor["mcpServers"]["treg"]
    assert treg["url"] == "https://treg.to/mcp/"
    assert treg["headers"]["Authorization"] == "Bearer TESTKEY"

    oc = json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())
    assert oc["mcp"]["treg"]["type"] == "remote" and oc["mcp"]["treg"]["enabled"] is True
    assert oc["mcp"]["treg"]["headers"]["Authorization"] == "Bearer TESTKEY"

    # idempotent: a second run leaves exactly one entry, still correct
    mcp_install.install_mcp(base_url="https://treg.to", token="TESTKEY", only=["cursor"])
    again = json.loads((cur / "mcp.json").read_text())
    assert list(again["mcpServers"].keys()) == ["other", "treg"]


def test_uninstalled_agents_are_skipped_not_written(tmp_path, monkeypatch):
    """No marker dir → the agent isn't touched (no stray config created for something not installed)."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    out = mcp_install.install_mcp(base_url="https://treg.to", token="K",
                                  only=["cursor", "opencode"])
    assert out["results"] == []                       # nothing installed → nothing written
    assert not (tmp_path / ".cursor").exists()


def test_the_mcp_url_carries_the_trailing_slash(tmp_path, monkeypatch):
    """The resource identifier is `/mcp/` — the transport is served there, and a client that resolves
    metadata uses that exact form.

    HOME is isolated even though `only=[]` writes nothing: this exact test once ran against the
    REAL home (empty list read as "no filter" by the old `if only and …` guard), so every suite run
    silently rewrote the developer's actual Claude/Cursor/opencode configs with `Bearer K` — and
    every MCP call on the machine failed days later with "invalid token". A test must never be one
    guard away from writing outside its sandbox."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    out = mcp_install.install_mcp(base_url="https://treg.to/", token="K", only=[])
    assert out["mcp_url"] == "https://treg.to/mcp/"


def test_only_empty_means_NONE_not_all(tmp_path, monkeypatch):
    """`only=[]` writes nothing anywhere — an empty restriction is "no agents", not "no filter".
    The old falsy check made [] behave like None, which is how the suite's dummy token reached
    real configs (see test above)."""
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".cursor").mkdir()                       # cursor IS "installed" in this home…
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    out = mcp_install.install_mcp(base_url="https://treg.to", token="K", only=[])
    assert out["results"] == [] and out["manual"] == []  # …and still nothing is written
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()


def test_cmd_mcp_install_REFUSES_a_bad_token_before_writing(tmp_path, monkeypatch):
    """The command validates the token against /auth/me before touching any agent config — the same
    check `treg login --token` runs. Without it a garbage token (stale TREG_TOKEN, mangled paste)
    fans out silently into every agent on the machine and surfaces days later as per-provider
    "invalid token" errors inside whichever agent tries a call."""
    from types import SimpleNamespace

    import httpx
    import pytest

    from treg import cli

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            assert url == "/auth/me"
            return httpx.Response(401, json={"detail": "invalid token"},
                                  request=httpx.Request("GET", "http://t/auth/me"))

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(mcp_install, "HOME", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".config" / "opencode").mkdir(parents=True)

    with pytest.raises(SystemExit) as e:
        cli.cmd_mcp_install(SimpleNamespace(name=None),
                            {"token": "K", "base_url": "https://treg.to"})
    assert "rejected" in str(e.value)
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()  # nothing written
