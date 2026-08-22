# The DeepSeek Harness bundle

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) is a coding agent built
on "everything is a plugin". It is the fourth shop window for the same skill, and the only one that
can ship the connector **and** the CLI path in a single zero-config install.

## Install

```bash
dsh plugin --profile <name> add github:superdesigndev/treg
dsh --profile <name>
```

## Why there is no manifest

dsh reads no `plugin.json`. It installs an **npm package whose `package.json` declares
`dsh.bundle`**, pointing at a `cordis.patch.yml` config layer, and appends that bundle to the
profile's `dsh.profile.bundles` list. Layers compose in list order, so a user can override any row
we ship from their own profile patch without touching this package
([publish.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md)).

```
package.json                  the bundle manifest — `dsh.bundle.patch`
dsh/cordis.patch.yml          the layer: the skill row + the MCP row
dsh/index.js                  a ctx.skills provider over the tree below
dsh/skills/treg/SKILL.md      GENERATED — never edit by hand
```

## The MCP row is disabled until there is a token

The Claude Code plugin declares no connector at all, because anything declared in a manifest is
registered at install time, before any human has signed in: five always-on tools that 401 on every
call ([CLAUDE-PLUGIN.md](CLAUDE-PLUGIN.md)). dsh can express the better version of that trade,
because a row carries a `disabled` expression evaluated at boot:

```yaml
- id: treg-mcp
  name: '@deepseek-ai/dsh-mcp-client'
  disabled: !!js !process.env.TREG_TOKEN
```

So a user with `TREG_TOKEN` in their environment gets `mcp__treg__catalog_search` and friends at
boot, and everyone else gets a clean install and a skill that walks them through `treg login`. The
row is evaluated **at boot**, so a token exported afterwards needs a `dsh` restart — the bootstrap
says so, because a human waiting for tools that will not appear this session is the failure mode.

`treg mcp install` is deliberately *not* part of that path: it writes Claude Code, Cursor and
opencode configs (`mcp_install.py`), and a dsh profile is none of those. `treg mcp install` prints
that instruction when it detects `~/.dsh`, and the dsh bootstrap steers away from it.

## Two constraints on `dsh/index.js`

**No build step.** `dsh plugin add github:...` fetches sources, not build output. A TypeScript
package would need a `prepare` script, and pnpm ≥10 refuses to run one from a git dependency until
the user adds an `allowBuilds` entry to their profile's `pnpm-workspace.yaml` — i.e. the documented
install command fails on first run for everyone. Plain hand-written ESM sidesteps it.

**No `@deepseek-ai/*` dependency.** The obvious implementation mounts the in-box
`@deepseek-ai/dsh-skill-filesystem` with `customSkillDirs`, but an out-of-tree bundle depending on
an in-box package installs a *second* copy that drifts from the host's. The provider's `list`/`get`
contract is the stable seam, so `index.js` implements it directly — the shape `dsh-skill-badge` uses
for its own bundled skill. `tests/test_plugin.py` pins both constraints.

The skill's `description` is parsed out of `SKILL.md` at runtime rather than restated in JS, so
there is no second copy to rot.

## Discovery

Plugins are found through the [`dsh-plugin`](https://github.com/topics/dsh-plugin) GitHub topic,
which is what the ecosystem catalogs (awesome-dsh-plugin, dsh.tools) sync from. The topic is set on
this repo; a listing PR to those catalogs is the manual half.
