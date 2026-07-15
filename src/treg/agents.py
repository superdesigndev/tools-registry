"""Where each coding agent looks for skills.

A "skill" is the same everywhere — a `SKILL.md` (YAML frontmatter + markdown) in its own folder.
Only the *directory* differs per agent, so supporting "any agent" is just a path table plus a probe
for which agents are actually installed on this machine.

Two conventions dominate the ecosystem:
  - `.agents/skills/`  — the de-facto shared standard (Cursor, Codex, Gemini CLI, Copilot, OpenCode,
    Cline, Amp, Warp, Zed, …). One write covers most agents.
  - `.claude/skills/`  — Claude Code keeps its own dir.
Everything else is a bespoke `.<agent>/skills/` folder. This mirrors vercel-labs/skills' registry,
scoped to the high-coverage subset (expand `AGENTS` to add more).

Paths honor the same env overrides the agents themselves use (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`XDG_CONFIG_HOME`).
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg) if xdg else HOME / ".config"


def _claude_home() -> Path:
    v = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(v) if v else HOME / ".claude"


def _codex_home() -> Path:
    v = os.environ.get("CODEX_HOME", "").strip()
    return Path(v) if v else HOME / ".codex"


# The shared project convention. Writing skills here reaches every agent that reads `.agents/skills`.
STANDARD_PROJECT_DIR = ".agents/skills"

# name -> (display, project skills dir (relative to a repo root), global skills dir, install marker).
# `marker` is a path whose existence means "this agent is installed for this user".
# `project` / `global_` / `marker` are callables so env overrides are read at call time (testable).
AGENTS: dict[str, dict] = {
    "claude-code": {
        "display": "Claude Code",
        "project": ".claude/skills",
        "global_": lambda: _claude_home() / "skills",
        "marker": lambda: _claude_home(),
    },
    "cursor": {
        "display": "Cursor",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: HOME / ".cursor" / "skills",
        "marker": lambda: HOME / ".cursor",
    },
    "codex": {
        "display": "Codex",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: _codex_home() / "skills",
        "marker": lambda: _codex_home(),
    },
    "gemini-cli": {
        "display": "Gemini CLI",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: HOME / ".gemini" / "skills",
        "marker": lambda: HOME / ".gemini",
    },
    "github-copilot": {
        "display": "GitHub Copilot",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: HOME / ".copilot" / "skills",
        "marker": lambda: HOME / ".copilot",
    },
    "opencode": {
        "display": "OpenCode",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: _config_home() / "opencode" / "skills",
        "marker": lambda: _config_home() / "opencode",
    },
    "amp": {
        "display": "Amp",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: _config_home() / "agents" / "skills",
        "marker": lambda: _config_home() / "amp",
    },
    "cline": {
        "display": "Cline",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: HOME / ".agents" / "skills",
        "marker": lambda: HOME / ".cline",
    },
    "warp": {
        "display": "Warp",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: HOME / ".agents" / "skills",
        "marker": lambda: HOME / ".warp",
    },
    "zed": {
        "display": "Zed",
        "project": STANDARD_PROJECT_DIR,
        "global_": lambda: HOME / ".agents" / "skills",
        "marker": lambda: _config_home() / "zed",
    },
    "windsurf": {
        "display": "Windsurf",
        "project": ".windsurf/skills",
        "global_": lambda: HOME / ".codeium" / "windsurf" / "skills",
        "marker": lambda: HOME / ".codeium" / "windsurf",
    },
    "roo": {
        "display": "Roo Code",
        "project": ".roo/skills",
        "global_": lambda: HOME / ".roo" / "skills",
        "marker": lambda: HOME / ".roo",
    },
    "continue": {
        "display": "Continue",
        "project": ".continue/skills",
        "global_": lambda: HOME / ".continue" / "skills",
        "marker": lambda: HOME / ".continue",
    },
    "qwen-code": {
        "display": "Qwen Code",
        "project": ".qwen/skills",
        "global_": lambda: HOME / ".qwen" / "skills",
        "marker": lambda: HOME / ".qwen",
    },
    "kilo": {
        "display": "Kilo Code",
        "project": ".kilocode/skills",
        "global_": lambda: HOME / ".kilocode" / "skills",
        "marker": lambda: HOME / ".kilocode",
    },
    "goose": {
        "display": "Goose",
        "project": ".goose/skills",
        "global_": lambda: _config_home() / "goose" / "skills",
        "marker": lambda: _config_home() / "goose",
    },
}

# Default project fan-out when the user names no agent: the two highest-coverage buckets.
# `.agents/skills` (shared standard) + `.claude/skills` (Claude Code) ≈ ~90% of agents in one shot.
DEFAULT_PROJECT_DIRS = [STANDARD_PROJECT_DIR, ".claude/skills"]


def project_dir(agent: str) -> str:
    """The repo-relative skills dir for one agent (KeyError if unknown)."""
    return AGENTS[agent]["project"]


def global_dir(agent: str) -> Path:
    return AGENTS[agent]["global_"]()


def is_installed(agent: str) -> bool:
    try:
        return AGENTS[agent]["marker"]().exists()
    except OSError:
        return False


def detect_installed() -> list[str]:
    """Agent names whose install marker exists on this machine (order = registry order)."""
    return [name for name in AGENTS if is_installed(name)]


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def resolve_targets(
    *,
    explicit_dir: str | None = None,
    agent: str | None = None,
    scope_global: bool = False,
    all_agents: bool = False,
) -> list[Path]:
    """The list of base dirs to write skills into, deduped.

    Precedence:
      explicit_dir  -> exactly that dir (back-compat with the old --dir behavior).
      agent         -> that one agent (global dir if scope_global, else its project dir).
      all_agents    -> every registered agent (global dirs if scope_global, else project dirs).
      scope_global  -> global dirs of *detected-installed* agents; falls back to the default set.
      (default)     -> DEFAULT_PROJECT_DIRS (.agents/skills + .claude/skills).
    """
    if explicit_dir:
        return [Path(explicit_dir)]
    if agent:
        if agent not in AGENTS:
            raise KeyError(agent)
        return [global_dir(agent) if scope_global else Path(project_dir(agent))]
    if all_agents:
        if scope_global:
            return _dedupe([global_dir(a) for a in AGENTS])
        return _dedupe([Path(AGENTS[a]["project"]) for a in AGENTS])
    if scope_global:
        detected = detect_installed()
        if detected:
            return _dedupe([global_dir(a) for a in detected])
        # nothing detected — still land somewhere useful
        return _dedupe([global_dir("claude-code"), HOME / STANDARD_PROJECT_DIR])
    return _dedupe([Path(p) for p in DEFAULT_PROJECT_DIRS])
