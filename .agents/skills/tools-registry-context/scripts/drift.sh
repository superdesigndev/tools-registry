#!/usr/bin/env bash
# drift.sh [git-range] — map changed source files to the fragment(s) that document them.
# Project-agnostic: source pathspecs + extensions come from fragments.config. Reads the generated MAP.md.
# Default range: origin/main..HEAD (everything unpushed).
set -euo pipefail

RANGE="${1:-origin/main..HEAD}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
MAP="$SKILL_DIR/MAP.md"
CONFIG="$SKILL_DIR/fragments.config"

[ -f "$MAP" ] || { echo "MAP.md not found — run build-map.py first" >&2; exit 1; }

# Pull source pathspecs + optional extension filter from the JSON config (python3 is a dependency).
globs=$(python3 -c "import json;print(' '.join(json.load(open('$CONFIG')).get('source_globs',[])))" 2>/dev/null || true)
extre=$(python3 -c "import json;e=json.load(open('$CONFIG')).get('source_exts',[]);print('\\.('+'|'.join(e)+')\$' if e else '')" 2>/dev/null || true)

cd "$ROOT"
changed=$(git diff --name-only "$RANGE" -- $globs 2>/dev/null || true)
[ -n "$extre" ] && changed=$(printf '%s\n' "$changed" | grep -E "$extre" || true)

if [ -z "$changed" ]; then
  echo "No documented-source changes in $RANGE."
  exit 0
fi

echo "Changed sources in $RANGE → fragments to review:"
gap=()
while IFS= read -r f; do
  [ -z "$f" ] && continue
  line=$(grep -F "\`$f\`" "$MAP" | grep ' | ' | head -1 || true)
  if [ -n "$line" ]; then
    docs=$(printf '%s' "$line" | sed -E 's/^\| `[^`]+` \| (.*) \|$/\1/')
    printf '  %-48s → %s\n' "$f" "$docs"
  else
    gap+=("$f")
  fi
done <<< "$changed"

if [ ${#gap[@]} -gt 0 ]; then
  echo ""
  echo "⚠ changed sources with NO fragment (possible doc gap — fold in or write a new fragment):"
  printf '  %s\n' "${gap[@]}"
fi
