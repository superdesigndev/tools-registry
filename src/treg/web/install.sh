#!/bin/sh
# treg CLI installer  -  curl -fsSL {BASE}/install.sh | sh
# Installs the `treg` command and points it at this server ({BASE}).
set -e

BASE="{BASE}"
# Install from PyPI (fast, public, no git clone). The base package is the light CLI; the FastAPI/DB
# server stack is the `tools-registry[server]` extra, which people who self-host install separately.
SRC="tools-registry"

printf '\n\033[38;5;173m▚ tools-registry\033[0m - installing the treg CLI…\n\n'

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$SRC"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC"
elif command -v pip3 >/dev/null 2>&1; then
  pip3 install --user --upgrade "$SRC"
else
  echo "Need Python 3.12+ and one of: uv (recommended), pipx, or pip3." >&2
  echo "Install uv:  https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# point the CLI at this server (falls back silently on older CLIs)
treg config --base-url "$BASE" >/dev/null 2>&1 || true

# install the official tools-registry skill into every detected agent so it knows how to use treg.
# `treg skill bootstrap` fans out across all supported agents (Claude Code, Cursor, Codex, Gemini,
# Copilot, OpenCode, Windsurf, …). Fall back to the Claude-only drop for older CLIs without it.
if treg skill bootstrap 2>/dev/null; then
  :
else
  SKILL_DIR="$HOME/.claude/skills/tools-registry"
  if mkdir -p "$SKILL_DIR" 2>/dev/null && curl -fsSL "$BASE/skill.md" -o "$SKILL_DIR/SKILL.md" 2>/dev/null; then
    printf '\033[32m✓\033[0m Installed the \033[1mtools-registry\033[0m skill for Claude Code (%s)\n' "$SKILL_DIR"
  fi
fi

printf '\n\033[32m✓\033[0m Installed \033[1mtreg\033[0m. Next:\n'
printf '    \033[38;5;173mtreg login\033[0m      # sign in (GitHub or email) - first login registers you\n'
printf '    \033[38;5;173mtreg onboard\033[0m    # optional guided walkthrough\n'
printf '\nDocs & interactive tutorial:  %s/tutorial\n\n' "$BASE"
