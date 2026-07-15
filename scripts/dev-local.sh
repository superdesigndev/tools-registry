#!/usr/bin/env bash
#
# dev-local.sh — bring the treg dev stack up in one command.
#
# One service: the FastAPI server (`python -m treg`, port 18790) in a tmux window,
# started with TREG_EMAIL_DEV_MODE=true (the OTP shows in the login page / API
# response — no mail sender needed) and its own sqlite DB (treg-dev.db), so your
# real data and .env stay untouched.
#
# The `cli` subcommand runs the WORKING-TREE treg CLI in a sandboxed HOME
# (scripts/.dev-home), pre-pointed at localhost — it never touches
# ~/.treg/config.json, which usually points at production.
#
# Usage:
#   scripts/dev-local.sh up            # start the server (idempotent)
#   scripts/dev-local.sh down          # stop it
#   scripts/dev-local.sh status        # window list + port check
#   scripts/dev-local.sh logs          # tail the server window
#   scripts/dev-local.sh restart       # restart the server window
#   scripts/dev-local.sh attach        # attach to the tmux session
#   scripts/dev-local.sh cli <args…>   # sandboxed dev CLI, e.g.: cli login
#   scripts/dev-local.sh reset         # down + wipe the dev DB and CLI sandbox
#
set -euo pipefail

SESSION="treg-dev"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=18790
DEV_DB="$ROOT/treg-dev.db"                 # *.db is gitignored
DEV_HOME="$ROOT/scripts/.dev-home"         # sandbox HOME for the dev CLI

DEV_KEYS="$DEV_HOME/dev-keys.env"           # stable dev-only Fernet + session keys, minted once —
                                            # without them every --reload restart drops sessions/secrets
SERVER_ENV="TREG_EMAIL_DEV_MODE=true TREG_DATABASE_URL=sqlite+aiosqlite:///$DEV_DB"
SERVER_CMD="cd $ROOT && set -a && . $DEV_KEYS && set +a && env $SERVER_ENV uv run python -m treg --reload"

ensure_dev_keys() {
  [ -f "$DEV_KEYS" ] && return
  mkdir -p "$DEV_HOME"
  info "minting dev keys (once) → $DEV_KEYS"
  {
    printf 'TREG_SECRET_KEY=%s\n' "$(cd "$ROOT" && uv run python -m treg keygen)"
    printf 'TREG_SESSION_SECRET=%s\n' "$(openssl rand -hex 24)"
  } > "$DEV_KEYS"
}

c_reset=$'\033[0m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_cyn=$'\033[36m'
info() { printf "${c_cyn}▸ %s${c_reset}\n" "$*"; }
ok()   { printf "${c_grn}✓ %s${c_reset}\n" "$*"; }
warn() { printf "${c_ylw}! %s${c_reset}\n" "$*"; }
die()  { printf "${c_red}✗ %s${c_reset}\n" "$*" >&2; exit 1; }
port_up() { lsof -ti :"$1" -sTCP:LISTEN >/dev/null 2>&1; }

preflight() {
  command -v tmux >/dev/null 2>&1 || die "tmux not found. Install: brew install tmux"
  command -v uv   >/dev/null 2>&1 || die "uv not found. Install: https://docs.astral.sh/uv/"
  [ -d "$ROOT/.venv" ] || { info "no .venv — running uv sync"; (cd "$ROOT" && uv sync); }
}

up() {
  preflight
  ensure_dev_keys
  if port_up "$PORT" && ! tmux has-session -t "$SESSION" 2>/dev/null; then
    die "port $PORT is already in use by something outside this script (lsof -i :$PORT)"
  fi
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    warn "session '$SESSION' already running"
  else
    tmux new-session -d -s "$SESSION" -n server "$SERVER_CMD"
    info "waiting for the server…"
    for _ in $(seq 1 30); do port_up "$PORT" && break; sleep 0.5; done
    port_up "$PORT" || { tmux capture-pane -pt "$SESSION:server" | tail -20; die "server didn't come up — window output above"; }
  fi
  ok "server up  →  http://localhost:$PORT   (dashboard · /docs · /login)"
  echo "  email OTP dev mode is ON: codes appear on the page / in the API response"
  echo "  manual login test:  scripts/dev-local.sh cli login"
}

down() {
  tmux kill-session -t "$SESSION" 2>/dev/null && ok "session stopped" || warn "no session running"
}

status() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux list-windows -t "$SESSION" -F "  window: #{window_name}  (#{pane_current_command})"
  else
    warn "no tmux session '$SESSION'"
  fi
  if port_up "$PORT"; then ok "port $PORT listening"; else warn "port $PORT not listening"; fi
}

logs()    { tmux capture-pane -pt "$SESSION:server" -S -200; }
restart() { ensure_dev_keys; tmux respawn-window -k -t "$SESSION:server" "$SERVER_CMD" && ok "server restarted"; }
attach()  { tmux attach -t "$SESSION"; }

cli() {
  # Working-tree CLI, sandboxed: HOME is swapped so ~/.treg/config.json (prod) is never touched.
  mkdir -p "$DEV_HOME/.treg"
  [ -f "$DEV_HOME/.treg/config.json" ] || printf '{"base_url":"http://localhost:%s"}\n' "$PORT" > "$DEV_HOME/.treg/config.json"
  (cd "$ROOT" && HOME="$DEV_HOME" uv run treg "$@")
}

reset() {
  down
  rm -f "$DEV_DB" && rm -rf "$DEV_HOME"
  ok "dev DB and CLI sandbox wiped"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  logs) logs ;;
  restart) restart ;;
  attach) attach ;;
  cli) shift; cli "$@" ;;
  reset) reset ;;
  *) sed -n '3,24p' "$0"; exit 1 ;;
esac
