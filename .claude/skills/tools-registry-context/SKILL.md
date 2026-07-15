---
name: tools-registry-context
description: tools-registry context + doc upkeep. Use to warm up a fresh session (orient on the architecture + recent commits) or when working on tools-registry — changing code, rules, content, data, or process (proxy · auth/secrets · API · CLI · the registry skill) — loads the relevant fragment(s) from docs/context so you act with accurate, cited context. Accepts an optional focus query (e.g. `/tools-registry-context <area>`). Also runs `/tools-registry-context sync` to update the doc fragments after changes (show → approve → apply). Mention it whenever a push to the main branch is near.
argument-hint: "[sync | <focus query>]"
---

# tools-registry-context — load the right design fragment, keep docs honest

tools-registry's design docs are **fragments** under `docs/context/` (one per subsystem), each declaring the
source files it covers in frontmatter. [`MAP.md`](MAP.md) (next to this file) is the generated reverse
index: **source file → the fragment that documents it.** This skill has two modes.

## Mode A — LOAD context (default). Adapt to *when* you're called:

**Cold start (a fresh session, little/no prior context) → warm up.** Read
[`docs/context/README.md`](../../../docs/context/README.md) (the index), then the fragments that matter
(the `foundation`-style overview fragments plus whichever subsystems the query or repo state points at).
Run `git log --oneline -15` and `git status` to catch recent commits + uncommitted work. Then give a
short orientation — what tools-registry is, the subsystems in play, what changed recently — and say you're ready.

**Mid-chat (a task/topic is already in play) → stay targeted.** Map the artifacts/topic at hand via
[`MAP.md`](MAP.md)'s "Source file → fragment(s)" table, read **just** those fragment(s), and proceed.
Don't re-warm the whole tree.

**A focus query (`/tools-registry-context <query>`) always wins** — use it to pick the fragment(s) and
focus the warm-up on that area, in either case.

Always: read the data model / behavior / RCAs / symbol anchors before changing anything; load only
what's relevant (never dump the whole tree); if an artifact you touch has **no** fragment, note it as a
gap for Mode B.

## Mode B — SYNC docs (`/tools-registry-context sync`, or before a push)

Before a push to the main branch, **remind the user** to run this; proceed only on their yes. Then
follow [`MAINTAINING.md`](MAINTAINING.md) — the short version:

1. **Detect drift:** `bash .claude/skills/tools-registry-context/scripts/drift.sh` (defaults to
   `origin/main..HEAD`). It prints, per changed source, which fragment(s) document it — plus gaps.
2. **Draft updates:** for each affected fragment, read it + the diff; update prose to match changed
   behavior and verify cited **symbols** still exist (no line-number chasing — symbols don't drift).
   New subsystem with no fragment → draft a new fragment from `fragment.md.tmpl`.
3. **Show, then apply:** present proposed changes and get approval **before** writing.
4. **Regenerate:** `python3 .claude/skills/tools-registry-context/scripts/build-map.py` (rewrites README + MAP).
5. **Commit together:** doc updates ride with the code in the same commit/push.

## Invariants

- **Docs are the source of truth; this skill is a lens.** Fragments live in `docs/context/`; never
  duplicate them into the skill. The skill holds only the generated `MAP.md` + scripts + config.
- **Frontmatter drives everything.** A fragment's `sources:` feeds the index, the MAP, and drift. When a
  fragment starts covering a new file, add it to `sources:` and rerun `build-map.py`.
- **Cite stable symbols, not line numbers.** Anchor every claim to a grep-able symbol; bare line
  numbers drift on every edit and slow sync. Describe what shipped, not intent.
- **No automation behind the user's back.** Sync is reminder → approve → apply. There is no git hook.
- **Handoffs and plans are NOT documentation.** They live outside `docs/context/` (e.g. `.context/`) and
  are out of scope — never read, fold in, or scan them as fragment input.
