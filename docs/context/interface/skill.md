---
title: The shippable tools-registry skill (3 personas)
status: shipped
sources:
  - src/treg/web/skill.md
related:
  - interface/cli.md
  - interface/api.md
---

# The `tools-registry` skill

`src/treg/web/skill.md` is the **product** skill that ships to consumers — the agent's whole interface to the
registry (distinct from `.claude/skills/tools-registry-context/`, which maintains *these* design docs).
Its frontmatter `name: tools-registry` + `description` make it loadable by a coding agent.

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
`~/.claude/skills/tools-registry/SKILL.md` right after installing the CLI — so `curl {BASE}/install.sh | sh`
gives a machine both the `treg` command AND the skill that teaches an agent to use it. It restates the
invariants (secrets are write-only, use-without-hold, the proxy relays the upstream's truth) and links
`{BASE}/llms.txt` + `{BASE}/tutorial`. It mirrors the surfaces in [api.md](api.md) + [cli.md](cli.md);
keep the three in sync when the API/CLI change.
