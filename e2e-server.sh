#!/usr/bin/env bash
# Start an ISOLATED e2e server for this worktree. Deliberately not `treg-dev-server`: that one
# pkills every `python -m treg`, and another session already has one running against the main
# checkout. Own port, own database, nothing shared.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-18795}"
LOG=/tmp/treg-e2e.log

case "${1:-start}" in
  # Kill by PORT, not by a pattern: TREG_E2E_MARKER is an environment variable and never appears in
  # argv, so `pkill -f` silently matched nothing and left an orphan holding the database open — which
  # then showed up as "attempt to write a readonly database" after the file was replaced underneath it.
  stop)
    pids="$(lsof -ti :"$PORT" 2>/dev/null || true)"
    if [ -n "$pids" ]; then kill $pids 2>/dev/null || true; sleep 2; echo "stopped ($pids)"; else echo "wasn't running"; fi
    exit 0 ;;
  logs) exec tail -f "$LOG"; ;;
esac

# Real provider keys, so a real upstream answers with a real error body — the whole point of e2e.
set -a; . "$HERE/.env"; set +a

# A FRESH Fernet key per run, generated here rather than written into the file. `treg-dev-server`
# hardcodes one because its database persists and a new key would orphan every stored secret; this
# harness deletes its database between runs, so it has no such need — and a committed key is a
# committed key, whatever it unlocks. (gitleaks flagged the hardcoded one, correctly.)
SECRET_KEY="$(uv run --frozen --directory "$HERE" python -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

PORT="$PORT" \
TREG_E2E_MARKER=1 \
TREG_DATABASE_URL="sqlite+aiosqlite:///$HERE/e2e.db" \
TREG_SECRET_KEY="$SECRET_KEY" \
TREG_ADMIN_TOKEN="E2E-ADMIN-TOKEN" \
  nohup uv run --frozen --directory "$HERE" python -m treg > "$LOG" 2>&1 &

for _ in $(seq 1 60); do
  if grep -q "Application startup complete" "$LOG" 2>/dev/null; then
    echo "e2e server up on http://127.0.0.1:$PORT (db $HERE/e2e.db, log $LOG)"
    exit 0
  fi
  sleep 1
done
echo "did not start within 60s" >&2; tail -20 "$LOG" >&2; exit 1
