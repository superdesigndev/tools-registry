"""The per-agent skills-path registry + target resolution (`treg agents`, skill fan-out).

Pure logic over the registry — no DB, no network. Covers: the two-dir default fan-out, single-agent
and all-agents targeting, explicit-dir back-compat, global-scope resolution, install detection, and
that env overrides (CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_CONFIG_HOME) are honored at call time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from treg import agents as ag


def test_default_fanout_is_agents_plus_claude():
    bases = ag.resolve_targets()
    assert bases == [Path(".agents/skills"), Path(".claude/skills")]


def test_explicit_dir_wins_and_is_singular():
    assert ag.resolve_targets(explicit_dir="custom/x") == [Path("custom/x")]


def test_single_agent_project_dir():
    assert ag.resolve_targets(agent="cursor") == [Path(".agents/skills")]
    assert ag.resolve_targets(agent="claude-code") == [Path(".claude/skills")]


def test_unknown_agent_raises():
    with pytest.raises(KeyError):
        ag.resolve_targets(agent="nonesuch")


def test_all_agents_dedupes_shared_bucket():
    dirs = ag.resolve_targets(all_agents=True)
    # every .agents/skills agent collapses to a single entry
    assert dirs.count(Path(".agents/skills")) == 1
    assert Path(".claude/skills") in dirs
    assert Path(".windsurf/skills") in dirs
    # far fewer distinct dirs than agents
    assert len(dirs) < len(ag.AGENTS)


def test_single_agent_global_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert ag.resolve_targets(agent="codex", scope_global=True) == [tmp_path / "cx" / "skills"]


def test_global_scope_uses_detected_or_falls_back(monkeypatch, tmp_path):
    # point every marker into an empty home so nothing is "installed"
    monkeypatch.setattr(ag, "HOME", tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert ag.detect_installed() == []
    bases = ag.resolve_targets(scope_global=True)
    # fallback: claude global + the standard ~/.agents/skills
    assert (tmp_path / ".claude" / "skills") in bases
    assert (tmp_path / ag.STANDARD_PROJECT_DIR) in bases


def test_detect_installed_reads_markers(monkeypatch, tmp_path):
    monkeypatch.setattr(ag, "HOME", tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".claude").mkdir()
    detected = ag.detect_installed()
    assert "cursor" in detected and "claude-code" in detected
    assert "codex" not in detected  # ~/.codex absent


def test_claude_config_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert ag.global_dir("claude-code") == tmp_path / "cc" / "skills"
