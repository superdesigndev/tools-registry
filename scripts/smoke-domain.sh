#!/usr/bin/env bash
# smoke-domain.sh — live verification of the treg.superdesign.dev → treg.to cutover, phase-aware.
#
#   scripts/smoke-domain.sh pre     # after the INERT deploy (env still on the old domain)
#   scripts/smoke-domain.sh post    # after flipping TREG_PUBLIC_URL to https://treg.to
#
# Reviews reason about code; this checks the actual deployment, Render's edge included. Every
# check prints PASS/FAIL and the script exits non-zero if anything failed. No tokens needed —
# everything here is either public or expected to answer 401 (which proves the route is alive).
set -u

OLD="https://treg.superdesign.dev"
NEW="https://treg.to"
PHASE="${1:-}"
[ "$PHASE" = "pre" ] || [ "$PHASE" = "post" ] || { echo "usage: $0 pre|post" >&2; exit 2; }

fail=0
# NB: must end returning 0 — callers use `check && note PASS … || note FAIL …`, and a non-zero
# return from the PASS branch would run the FAIL branch too.
note() { printf '%-4s %s\n' "$1" "$2"; if [ "$1" = FAIL ]; then fail=1; fi; }

# ---- helpers -----------------------------------------------------------------------------------
# code <url> [curl args…] → HTTP status, never following redirects (redirect behavior IS the test).
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }
loc()  { curl -s -o /dev/null -w '%{redirect_url}' --max-time 15 "$@"; }

# ---- both phases: the API must serve IN PLACE on BOTH hosts -------------------------------------
for base in "$OLD" "$NEW"; do
  for path in /meta /llms.txt /install.sh /selfhost.sh /skill.md /vendor-listing; do
    c=$(code "$base$path")
    case "$c" in
      200) note PASS "$base$path → 200" ;;
      30*) note FAIL "$base$path → $c (API/agent surface must never redirect)" ;;
      *)   note FAIL "$base$path → $c" ;;
    esac
  done
  # /mcp/: 421 = the Host allow-list rejected this domain (the exact pre-cutover breakage class).
  # A bare 401 proves the transport answered but NOT host acceptance (auth challenges first) — set
  # TREG_SMOKE_TOKEN to a real token to make this check authoritative (expects 200).
  auth_args=()
  [ -n "${TREG_SMOKE_TOKEN:-}" ] && auth_args=(-H "Authorization: Bearer $TREG_SMOKE_TOKEN")
  c=$(code -X POST "$base/mcp/" -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' "${auth_args[@]}" \
        -H "Origin: $base" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')
  if [ -n "${TREG_SMOKE_TOKEN:-}" ]; then
    [ "$c" = 200 ] && note PASS "$base/mcp/ authed initialize → 200 (host+origin accepted)" \
                   || note FAIL "$base/mcp/ authed initialize → $c (expected 200)"
  else
    case "$c" in
      200|401) note PASS "$base/mcp/ initialize → $c (transport alive; set TREG_SMOKE_TOKEN for full proof)" ;;
      421)     note FAIL "$base/mcp/ → 421 Misdirected (host allow-list rejected this domain)" ;;
      *)       note FAIL "$base/mcp/ → $c" ;;
    esac
  fi
done

# ---- marketing pages: behavior differs by phase --------------------------------------------------
for path in / /terms /privacy /support /tutorial; do
  c=$(code "$OLD$path")
  if [ "$PHASE" = pre ]; then
    [ "$c" = 200 ] && note PASS "pre: $OLD$path served in place (200)" \
                   || note FAIL "pre: $OLD$path → $c (inert deploy must not redirect anything)"
  else
    if [ "$c" = 301 ]; then
      l=$(loc "$OLD$path")
      [ "$l" = "$NEW$path" ] && note PASS "post: $OLD$path → 301 $l" \
                             || note FAIL "post: $OLD$path 301 to unexpected: $l"
    else
      note FAIL "post: $OLD$path → $c (expected 301 to $NEW$path)"
    fi
  fi
done

# treg.to marketing pages serve in place in EVERY phase (never a redirect source: loop safety).
for path in / /terms; do
  c=$(code "$NEW$path")
  [ "$c" = 200 ] && note PASS "$NEW$path → 200" || note FAIL "$NEW$path → $c"
done

# ---- auth entries -------------------------------------------------------------------------------
for path in /auth/github /auth/google /oauth/authorize; do
  c=$(code "$OLD$path")
  if [ "$PHASE" = pre ]; then
    # Inert: served in place (503 = provider unconfigured is fine; 30x cross-host is not).
    case "$c" in 30*) l=$(loc "$OLD$path"); case "$l" in "$NEW"*) note FAIL "pre: $OLD$path → $c $l";; *) note PASS "pre: $OLD$path → $c (same-host)";; esac ;;
                 *) note PASS "pre: $OLD$path → $c (served in place)" ;; esac
  else
    if [ "$c" = 302 ]; then
      l=$(loc "$OLD$path")
      case "$l" in "$NEW$path"*) note PASS "post: $OLD$path → 302 $l";;
                   *)            note FAIL "post: $OLD$path 302 to unexpected: $l";; esac
    else
      note FAIL "post: $OLD$path → $c (expected 302 to $NEW$path)"
    fi
  fi
done

# ---- webhooks stay put --------------------------------------------------------------------------
c=$(code -X POST "$OLD/billing/stripe/webhook" -d '{}')
case "$c" in 30*) note FAIL "$OLD/billing/stripe/webhook → $c (webhook must never redirect)";;
             000) note FAIL "$OLD/billing/stripe/webhook → no connection";;
             *)   note PASS "$OLD/billing/stripe/webhook answers in place ($c)";; esac

echo
[ "$fail" = 0 ] && echo "SMOKE: ALL PASS ($PHASE)" || echo "SMOKE: FAILURES ($PHASE) — do not proceed"
exit $fail
