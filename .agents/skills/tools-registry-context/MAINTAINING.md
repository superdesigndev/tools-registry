# MAINTAINING — keep the tools-registry fragment set (and this skill) current

The "sub-skill": the protocol for updating `docs/context/` fragments after code changes. Read it when
running `/tools-registry-context sync`. (This system was scaffolded by the global `codemap` skill.)

## Scope boundary (read first)

This skill documents **only `docs/context/`**. Session **handoffs and plans live elsewhere** (e.g.
`.context/`) and are explicitly **not** part of the fragment set — never read them as input, fold them
into a fragment, or sweep them into the index. `build-map.py` walks `docs/context/` only, by design.

## The mental model

```
docs/context/**/*.md   ← the fragments (SOURCE OF TRUTH; human-browsable, versioned)
   frontmatter:  title / status / sources: [...] / related: [...]
        │  scripts/build-map.py reads all frontmatter + fragments.config →
        ▼
docs/context/README.md                     (human index, grouped by category)
.claude/skills/tools-registry-context/MAP.md  (source file → fragment, for routing + drift)
```

Edit **fragments**, never the generated files (they carry a "GENERATED" banner and are overwritten).

## Sync workflow

1. **Scope:** default range `origin/main..HEAD`. Override with an explicit range if needed.
2. **Detect drift:** `bash .claude/skills/tools-registry-context/scripts/drift.sh [range]` → (a) changed
   source → documenting fragment(s); (b) changed source with **no** fragment.
3. **For each affected fragment:** read it + `git diff <range> -- <file>`; update prose; re-grep moved
   cited symbols still exist (grep them); add new files to `sources:`. Citations are **symbols, not
   line numbers**, so an unrelated insert elsewhere does NOT make this fragment drift — only a genuine
   behavior/rename change does. (This is why sync stays fast.)
4. **For gaps:** fold into an existing fragment (+ `sources:`), or write a new fragment from
   `fragment.md.tmpl` in the right category folder (~70–130 lines, cited, present-tense).
5. **Show before applying.** Get user approval; apply only what's approved.
6. **Regenerate:** `python3 .claude/skills/tools-registry-context/scripts/build-map.py` — warns on any
   fragment missing frontmatter (exit 1). Fix before committing.
7. **Commit with the code.** `docs(context):` scope; follow the repo's commit conventions.

## Fragment conventions (must hold for the tooling)

- **Frontmatter required** on every fragment: `title`, `status`
  (shipped | reference | foundational | living | archived | backlog), `sources:` (repo-relative; `[]`
  for narrative/reference), `related:` (other fragment paths).
- **Categories = subfolders** under `docs/context/`, ordered/labeled in `fragments.config`.
- **One subsystem per fragment.** Split anything past ~150 lines.

## Evolving this skill / config

- New category → add `{dir,label}` to `fragments.config` `categories` (controls index order + heading).
- New source area → add a pathspec to `source_globs` (and extension to `source_exts`) in the config.
- Pull upstream script improvements → re-run `codemap` in `refresh` mode (re-copies `build-map.py` /
  `drift.sh` from the global skill without touching your fragments or config).
- Keep `SKILL.md` thin (a router). Detail belongs here or in fragments.
