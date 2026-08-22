#!/usr/bin/env python3
"""Generate every plugin's SKILL.md from the one source: `src/treg/web/skill.md`.

Four shop windows, three bootstraps, one source:

- **codex**  -> `plugin/skills/treg/SKILL.md` — ships an MCP connector, so its bootstrap points at
  the five tools and tells the reader NOT to reach for a terminal.
- **claude** -> `skills/treg/SKILL.md` — skills-only by design (nothing about it waits on a
  directory review), so its bootstrap says the opposite: there are no tools, install the CLI. It
  also gets a `version:` stamped into its frontmatter, which is what ClawHub requires and Claude
  Code ignores.
- **cursor** -> `plugins/treg/skills/treg/SKILL.md` — same CLI bootstrap as claude; Cursor
  prescribes its own layout, where the plugin root is never the repo root.
- **dsh**    -> `dsh/skills/treg/SKILL.md` — DeepSeek Harness, the only surface that ships the
  connector AND the CLI path in one install: the MCP row is disabled until `TREG_TOKEN` exists, so
  its bootstrap has to cover both states and steer away from `treg mcp install`, which cannot write
  a dsh profile. See docs/DSH-PLUGIN.md.

The Claude variant sits at the REPO ROOT rather than under `plugin/` because that one path is
simultaneously what Claude Code's plugin loader auto-discovers, what `npx skills add` resolves, and
what `clawhub skill publish` takes — one generated tree, four distribution channels.

Each plugin is a shop window, and what it ships is the SAME skill `treg skill bootstrap` already
writes into every agent's skills dir. Copying that file by hand would be a second source of truth for
the product's most-read page, and it would rot: the served copy changes whenever the product does,
and nothing would notice a plugin drifting behind it. So all of them are generated, and
`tests/test_plugin.py` fails if any checked-in copy is stale.

Three transformations, each for a reason the served file does not have:

1. `{BASE}` is a placeholder the SERVER substitutes per request (`api.py` `/skill.md`). Nothing
   substitutes it inside an installed plugin, so a raw copy would ship the literal string `{BASE}`
   and every URL in it would be broken. It is baked to the public deployment here.

2. A short section is prepended, and it is the ONLY thing that differs between the two variants —
   because they land in opposite worlds. Codex arrives with a connector, so its bootstrap maps the
   page onto the five tools and tells the reader not to reach for a terminal. Claude Code arrives
   with neither the CLI nor the tools, so its bootstrap walks the setup that produces both:
   `install.sh` -> `treg login` -> `treg mcp install`, in that order (step 3 exits without writing
   if it runs before there is a token). Without either, a first run is an agent dutifully invoking
   something that does not exist — the listing's whole conversion funnel spent on an error message.

3. The Claude variant gets `version:` stamped into its frontmatter. ClawHub requires it and Claude
   Code ignores it, which is what lets one generated file satisfy both registries.

Usage:  python3 scripts/build_plugin.py [--check]
        --check exits 1 if either generated file differs from what is checked in (used by the test).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "treg" / "web" / "skill.md"
PUBLIC_BASE = "https://treg.to"

CODEX_TARGET = ROOT / "plugin" / "skills" / "treg" / "SKILL.md"
CLAUDE_TARGET = ROOT / "skills" / "treg" / "SKILL.md"
# Cursor's layout is prescribed by github.com/cursor/plugin-template: a marketplace manifest at the
# repo root, and every plugin under `plugins/<name>/` with its OWN `.cursor-plugin/plugin.json`.
# Unlike Claude Code, the plugin root is never the repo root — `source` must be `./plugins/treg`.
CURSOR_TARGET = ROOT / "plugins" / "treg" / "skills" / "treg" / "SKILL.md"
# DeepSeek Harness installs an npm package, not a plugin manifest: `package.json` declares
# `dsh.bundle`, and `dsh/cordis.patch.yml` mounts `dsh/index.js` as a `ctx.skills` provider over
# this tree. See docs/DSH-PLUGIN.md.
DSH_TARGET = ROOT / "dsh" / "skills" / "treg" / "SKILL.md"

CODEX_BOOTSTRAP = """
## First, check which treg you have

This plugin can arrive two ways, and they need opposite first moves. **Look at your tools before you
do anything else.**

**If you can see `catalog_search`, `catalog_get`, `call`, `balance` and `my_tools`** — treg is
connected. Use those; there is nothing to install. The rest of this page is written around the `treg`
command line, so read `treg catalog search` as `catalog_search`, `treg call` as `call`, and so on.

**If you cannot see them**, this copy was installed skills-only — the directory's upload path strips
connector config. Everything on this page then runs through the CLI, which you set up once:

```bash
curl -fsSL {BASE}/install.sh | sh   # the CLI, pointed at treg
treg login                                   # sign in; first login registers you
```

A new team starts with **$1.00 of free balance**, so there is nothing to pay before the first call.
If sign-in is needed, say so plainly and stop — never ask the human for a provider's API key, which
is the thing treg exists to avoid. (Registering treg's MCP server into Codex is manual: it needs a
`~/.codex/config.toml` entry with an env-var indirection. `{BASE}/llms.txt` has the shape.)

---

## When the tools ARE there

| tool | use it for |
|---|---|
| `catalog_search` | find an endpoint by WHAT YOU WANT TO DO — "work email", "backlinks", "tiktok comments" |
| `catalog_get` | one endpoint's parameters and its exact price, **before** you spend |
| `call` | make the call; treg injects the credential and relays the answer |
| `balance` | the team's prepaid balance |
| `my_tools` | what this team registered and you can call without holding the key |

If the connector is present but the tools error, it has no token yet: the human sets `TREG_TOKEN`
(from {BASE} → sign in → copy token) for this plugin.

Either way, the rest of this page is the part that matters — **when** treg is the right move, and
**how to choose** between providers.

---
"""

# The Claude Code plugin's MANIFEST declares no connector — anything it declared would be registered
# at install time, before a human has signed in, i.e. five always-on tools that 401 on every call.
# Keeping it out is what makes `/plugin install` zero-config. The SKILL then finishes the job at
# first run, when a human IS present to sign in, and the intended end state is CLI + skill + tools.
#
# Hence the ordered three-step block below. The order is load-bearing, not stylistic: `cmd_mcp_install`
# reads the token from config and sys.exits before writing anything when there is none, so running it
# ahead of `treg login` is a silent no-op an agent would move straight past.
CLI_BOOTSTRAP = """
## First run: finish the setup

This plugin ships the skill, so you have this page — but not yet the `treg` command, and not yet its
tools. Set both up **once**, in this order:

```bash
curl -fsSL {BASE}/install.sh | sh   # 1. the CLI (skip if `treg --version` already works)
treg login                                   # 2. sign in — opens a browser
treg mcp install                             # 3. register treg's tools into this agent
```

Step 3 writes the token from step 2, so the order matters — run out of order it exits without
writing anything. The tools (`catalog_search`, `catalog_get`, `call`, `balance`, `my_tools`) appear
after the agent restarts; until then everything on this page works through the CLI.

Step 1 also installs this same skill into the agent's own skills directory, which duplicates what
the plugin already gives you — worth mentioning to the human, who can remove it.

---
"""

DSH_BOOTSTRAP = """
## First, check which treg you have

This bundle ships two things: the page you are reading, and a treg MCP connector that stays
**disabled until `TREG_TOKEN` is in dsh's environment** — a connector registered without a token is
five tools that 401 on every call. So the first move depends on which one you got.

**If you can see `mcp__treg__catalog_search`, `mcp__treg__catalog_get`, `mcp__treg__call`,
`mcp__treg__balance` and `mcp__treg__my_tools`** — the token was there at boot and there is nothing
to install. The rest of this page is written around the `treg` command line, so read
`treg catalog search` as `mcp__treg__catalog_search`, `treg call` as `mcp__treg__call`, and so on.

**If you cannot see them**, this profile booted without a token. Everything here still works through
the CLI, which you set up once:

```bash
curl -fsSL {BASE}/install.sh | sh   # the CLI, pointed at treg
treg login                                   # sign in; first login registers you
```

A new team starts with **$1.00 of free balance**, so there is nothing to pay before the first call.
If sign-in is needed, say so plainly and stop — never ask the human for a provider's API key, which
is the thing treg exists to avoid.

To get the tools as well, the human exports that token in the environment dsh starts in and
**restarts dsh** — the row is then enabled automatically:

```bash
export TREG_TOKEN=<token from {BASE} → sign in → copy token>
```

Do **not** run `treg mcp install` for this: it writes configs for Claude Code, Cursor and opencode,
and a dsh profile is neither. This bundle already carries the dsh row.

One more thing worth mentioning to the human rather than silently fixing: `install.sh` always runs
`treg skill bootstrap`, which drops a second copy of this same page into `~/.agents/skills/treg/` —
a directory dsh also scans. Harmless, but redundant with this bundle.

---
"""

# variant -> (target, bootstrap, stamp_version). One source, four shop windows.
#
# Claude Code and Cursor share CLI_BOOTSTRAP: both give the agent a terminal, neither ships a
# connector, and `treg mcp install` writes a verified config for both (mcp_install.py). Keeping one
# string rather than two near-identical ones is what stops them drifting.
#
# dsh gets its OWN bootstrap rather than sharing either: it is the only surface that ships the
# connector and the CLI path in one install (the row is disabled until there is a token), and it is
# the only one where `treg mcp install` is the wrong move — it writes configs for other agents, not
# for a dsh profile.
VARIANTS = {
    "codex": (CODEX_TARGET, CODEX_BOOTSTRAP, False),
    "claude": (CLAUDE_TARGET, CLI_BOOTSTRAP, True),
    "cursor": (CURSOR_TARGET, CLI_BOOTSTRAP, True),
    "dsh": (DSH_TARGET, DSH_BOOTSTRAP, False),
}


def package_version() -> str:
    """The one version, read from pyproject — the same value `tests/test_plugin.py` pins both
    manifests to. Read rather than duplicated so a release bump cannot leave the skill behind."""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit("pyproject.toml has no `version = \"…\"` line")


def render(variant: str) -> str:
    target, bootstrap, stamp_version = VARIANTS[variant]
    text = SOURCE.read_text(encoding="utf-8")
    if "---" not in text:
        raise SystemExit(f"{SOURCE} has no frontmatter — refusing to guess where it ends")
    # Keep the frontmatter exactly as served (name + description drive discovery in BOTH surfaces),
    # and insert the bootstrap immediately after it, before the skill's own opening.
    _, fm, body = text.split("---", 2)
    if stamp_version:
        # ClawHub REQUIRES a semver `version` in frontmatter (and requires `name` to match the
        # parent directory, which `treg` already does). Claude Code ignores the extra key, so
        # stamping it here is what lets one generated file satisfy both registries.
        fm = f"{fm.rstrip()}\nversion: {package_version()}\n"
    out = f"---{fm}---\n{bootstrap}\n{body.lstrip(chr(10))}"
    return out.replace("{BASE}", PUBLIC_BASE)


def main() -> int:
    check = "--check" in sys.argv
    stale = False

    for variant, (target, _, _) in VARIANTS.items():
        generated = render(variant)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        rel = target.relative_to(ROOT)

        if check:
            if current == generated:
                print(f"OK — {rel} matches {SOURCE.relative_to(ROOT)}")
            else:
                print(f"STALE — {rel} does not match {SOURCE.relative_to(ROOT)}",
                      file=sys.stderr)
                stale = True
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated, encoding="utf-8")
        print(f"wrote {rel}  ({len(generated.splitlines())} lines)")

    if stale:
        print("  regenerate with: python3 scripts/build_plugin.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
