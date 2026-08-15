---
title: The shippable tools-registry skill (3 personas)
status: shipped
sources:
  - src/treg/web/skill.md
  - scripts/build_plugin.py
  - .claude-plugin/plugin.json
  - .claude-plugin/marketplace.json
  - plugin/.codex-plugin/plugin.json
related:
  - interface/cli.md
  - interface/api.md
---

# The `tools-registry` skill

`src/treg/web/skill.md` is the **product** skill that ships to consumers — the agent's whole interface to the
registry (distinct from `.claude/skills/tools-registry-context/`, which maintains *these* design docs).
Its frontmatter `name: treg` + `description` make it loadable by a coding agent.

One skill, three personas:
- **consumer** — discover + call tools with no credentials locally. Teaches the agent-native
  **URL-passthrough** first: take the real upstream URL and prefix it with `{BASE}/call/`
  + the `X-Treg-Token` header; `treg call <tool> <path>` is the CLI shorthand.
- **creator** — turn a local skill into a shared tool: `treg secret add`, `treg tool add` (single-key or
  `--bind` multi-credential), the `treg skill scaffold → push` bundle flow, and `treg oauth connect` for
  browser-consent tokens. Documents the two OAuth modes (auto-refresh vs manual) and the four auth shapes.
- **admin** — inventory + monitor: `treg tool/secret/skill ls`, `treg calls`, and `treg health [--run]`
  (with the per-tool `health_check` probe).

**Distribution:** the file is `{BASE}`-templated and served at **`GET /skill.md`** (`skill_md` in
`api.py`, via `_serve_md`), and `install.sh` best-effort drops it into
`~/.claude/skills/treg/SKILL.md` right after installing the CLI — so `curl {BASE}/install.sh | sh`
gives a machine both the `treg` command AND the skill that teaches an agent to use it. It restates the
invariants (secrets are write-only, use-without-hold, the proxy relays the upstream's truth) and links
`{BASE}/llms.txt` + `{BASE}/tutorial`. It mirrors the surfaces in [api.md](api.md) + [cli.md](cli.md);
keep the three in sync when the API/CLI change.

## Four doors, one source

The same file reaches agents four ways. Only the first is hand-written; the rest are **generated or
served**, because a second copy of the product's most-read page is a copy that rots.

| door | artifact | who reaches it |
|---|---|---|
| the installer | `install.sh` → `treg skill bootstrap` → every detected agent's skills dir | people who ran the curl one-liner |
| Claude Code plugin | `.claude-plugin/` + generated `skills/treg/SKILL.md` (repo root) | `/plugin marketplace add superdesigndev/treg` |
| Codex/ChatGPT plugin | `plugin/.codex-plugin/` + generated `plugin/skills/treg/SKILL.md` | the directory ChatGPT and Codex share |
| the domain itself | `GET /.well-known/skills/index.json` + `/.well-known/skills/treg/SKILL.md` | anything speaking the agentskills.io convention (Hermes reads this directly) |

`scripts/build_plugin.py` renders both plugin copies from the one source and `--check` fails if
either is stale (`tests/test_plugin.py`). The two variants differ **only** in their prepended
bootstrap, because they arrive in opposite worlds: the Codex plugin ships an MCP connector, so its
bootstrap says *use the tools, not the terminal*; the Claude plugin declares **no connector in its
manifest** — so it installs with no token and nothing waits on a directory review — and its bootstrap
does the opposite, walking the agent through `install.sh` → `treg login` → `treg mcp install` so the
first run ends with the CLI *and* the tools. Skills-only is a property of the manifest, not of the
end state; the order in that bootstrap is load-bearing, because `treg mcp install` exits without
writing when it runs before there is a token. The Claude copy also gets a `version:` stamped into its
frontmatter, which ClawHub requires and Claude Code ignores; that stamp is what lets one file satisfy
both registries.

The Claude variant sits at the **repo root**, not under `plugin/`, because that single path is
simultaneously what Claude Code's loader auto-discovers, what `npx skills add` resolves, and what
`clawhub skill publish` takes. See [docs/CLAUDE-PLUGIN.md](../../CLAUDE-PLUGIN.md) for the
per-registry submission runbook.
