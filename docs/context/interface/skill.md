---
title: The shippable tools-registry skill (3 personas)
status: shipped
sources:
  - src/treg/web/skill.md
  - src/treg/mcp_install.py
  - scripts/build_plugin.py
  - .claude-plugin/plugin.json
  - .claude-plugin/marketplace.json
  - plugin/.codex-plugin/plugin.json
  - plugins/treg/.cursor-plugin/plugin.json
  - package.json
  - dsh/cordis.patch.yml
  - dsh/index.js
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

The same file reaches agents six ways. Only the first is hand-written; the rest are **generated or
served**, because a second copy of the product's most-read page is a copy that rots.

| door | artifact | who reaches it |
|---|---|---|
| the installer | `install.sh` → `treg skill bootstrap` → every detected agent's skills dir | people who ran the curl one-liner |
| Claude Code plugin | `.claude-plugin/` + generated `skills/treg/SKILL.md` (repo root) | `/plugin marketplace add superdesigndev/treg` |
| Codex/ChatGPT plugin | `plugin/.codex-plugin/` + generated `plugin/skills/treg/SKILL.md` | the directory ChatGPT and Codex share |
| Cursor plugin | `.cursor-plugin/marketplace.json` + generated `plugins/treg/skills/treg/SKILL.md` | the Cursor marketplace (plugin root is never the repo root) |
| DeepSeek Harness bundle | root `package.json` (`dsh.bundle`) + `dsh/cordis.patch.yml` + generated `dsh/skills/treg/SKILL.md` | `dsh plugin --profile <name> add github:superdesigndev/treg` |
| the domain itself | `GET /.well-known/skills/index.json` + `/.well-known/skills/treg/SKILL.md` | anything speaking the agentskills.io convention (Hermes reads this directly) |

`scripts/build_plugin.py` renders every plugin copy from the one source and `--check` fails if any is
stale (`tests/test_plugin.py`). The variants differ **only** in their prepended bootstrap, because they arrive in opposite worlds: the Codex plugin ships an MCP connector, so its
bootstrap says *use the tools, not the terminal*; the Claude plugin declares **no connector in its
manifest** — so it installs with no token and nothing waits on a directory review — and its bootstrap
does the opposite, walking the agent through `install.sh` → `treg login` → `treg mcp install` so the
first run ends with the CLI *and* the tools. Skills-only is a property of the manifest, not of the
end state; the order in that bootstrap is load-bearing, because `treg mcp install` exits without
writing when it runs before there is a token. The Claude copy also gets a `version:` stamped into its
frontmatter, which ClawHub requires and Claude Code ignores; that stamp is what lets one file satisfy
both registries.

**DeepSeek Harness** is the odd one out, and the only door that ships the connector *and* the CLI
path in one zero-config install. dsh reads no manifest: it installs an npm package whose
`package.json` declares `dsh.bundle`, pointing at a config layer that composes into the user's
profile. That layer carries a treg MCP row whose `disabled` expression is evaluated at boot, so it
stays off until `TREG_TOKEN` is in the environment — the same "no always-on tools that 401" stance as
the Claude manifest, but expressible as a row rather than an omission. Its bootstrap is its own for
two reasons the others do not have: the tools are namespaced (`mcp__treg__call`, not `call`), and
`treg mcp install` cannot help here (it writes Claude Code / Cursor / opencode configs, never a dsh
profile), so `mcp_install.py` reports dsh as a MANUAL agent pointing at the bundle. See
[docs/DSH-PLUGIN.md](../../DSH-PLUGIN.md).

The Claude variant sits at the **repo root**, not under `plugin/`, because that single path is
simultaneously what Claude Code's loader auto-discovers, what `npx skills add` resolves, and what
`clawhub skill publish` takes. See [docs/CLAUDE-PLUGIN.md](../../CLAUDE-PLUGIN.md) for the
per-registry submission runbook.
## `/integrate.md` — the BUILDER skill

A second, separate skill for the other side of the relationship. `skill.md` teaches an agent to **use**
treg; `integrate.md` is pasted into a builder's own repo and pointed at their coding agent so they can
**embed** treg and bill their own customers for it.

It leads with the per-customer billing model rather than the call syntax, deliberately: tagging has to
happen at the one place your backend already sets `Authorization`, and a builder who writes the
plumbing first writes it in a shape that has to be torn out.

The things it insists on, each because getting them wrong is expensive and silent:

- **Tag from the backend, never as a model-supplied argument** — a model omits it mid-chain and the
  spend leaves the invoice.
- **Invoice from `usage/by-tag` (the ledger), never from `/calls`** — audit rows are shed under load.
- **Assert `attributed + unattributed == total`**, and drive `unattributed_micro` to zero; anything
  left is a call site that forgot to tag.
- **Branch on `X-Treg-Error`, not the status code** — a provider's own 4xx is relayed verbatim.
- **Never forward a team-level 402/429 to an end user** — those carry the builder's balance and a
  top-up link. The tag-scoped refusals are safe to surface and carry nothing about the team.
- **Per-customer caps are advisory**, so they must not be sold to end users as exact.

`tests/test_tag_billing.py` pins the header and route names the skill teaches, so a rename that would
silently turn it into wrong instructions fails the suite instead.
