# The Claude Code plugin, and where it gets listed

treg's Codex/ChatGPT plugin is built around the **MCP connector**, and that listing sits in a review
queue. The skill does not need MCP: `src/treg/web/skill.md` already teaches an agent to install the
CLI, sign in, and call the catalog. So it also ships as a **skills-only Claude Code plugin**, served
from this repo as its own marketplace — live the moment it merges, with no reviewer in the loop.

## Install

```
/plugin marketplace add superdesigndev/treg
/plugin install treg@treg
```

The skill loads as `treg:treg`, and the install itself needs **no token and no configuration** —
that is the point of shipping a skills-only manifest.

### Skills-only manifest, full setup at first run

These are two different questions, and conflating them is the easy mistake here.

The **manifest** declares no MCP server, because anything it declared would be registered at install
time, before any human has signed in — five always-on tools that 401 on every call. Keeping it out is
what makes `/plugin install` a zero-config action.

The **skill** then completes the setup on first use, when a human *is* present to sign in. Its
bootstrap walks three steps in a fixed order:

```bash
curl -fsSL https://treg.to/install.sh | sh   # 1. the CLI (skipped if treg --version works)
treg login                                   # 2. sign in — the token
treg mcp install                             # 3. the tools, header-authed with that token
```

So the intended end state is CLI **and** skill **and** MCP tools — reached by the skill, not by the
manifest.

**The order is load-bearing.** `cmd_mcp_install` (`src/treg/cli.py`) reads the token from config and
`sys.exit`s *before writing anything* when there is none — and again on a 401, with "nothing was
written". Run step 3 before step 2 and it is a no-op, quiet enough that an agent moves on believing
the tools exist. `test_the_claude_bootstrap_sets_up_cli_login_and_mcp_in_order` pins the sequence,
and pins the restart note too: `claude mcp add` does not take effect until the agent restarts.

Two consequences worth remembering:

- **`install.sh` always runs `treg skill bootstrap`** — there is no flag to suppress it — so step 1
  drops a *second* copy of this same skill into `~/.claude/skills/treg/`. Harmless, but redundant
  with the plugin. The bootstrap tells the agent to mention it to the human rather than delete it
  silently; deleting files under `$HOME` is not a skill's call to make.
- **The token branch of `install.sh` does all three steps at once**
  (`… | sh -s -- --token <key>`). That is the right path for CI or a machine handed a team token —
  but not for a plugin user, who has no token until step 2.

## What is where

```
.claude-plugin/plugin.json        the manifest Claude Code reads
.claude-plugin/marketplace.json   makes this repo its own single-plugin marketplace
skills/treg/SKILL.md              GENERATED — never edit by hand
skills.sh.json                    category grouping for skills.sh / Hermes' Skills Hub
```

`source: "./"` in `marketplace.json` makes the **plugin root the repo root**, which is why
`skills/treg/` is at the top level rather than under `plugin/`. That one path is also what
`npx skills add` resolves and what `clawhub skill publish` takes — one generated tree, four channels.

A consequence worth remembering: anything Claude Code auto-discovers at the repo root ships to users.
Only `skills/` is intended; a root-level `commands/`, `agents/`, or `hooks/` directory would be
published too. `tests/test_plugin.py` asserts none appears.

**Never edit `skills/treg/SKILL.md`.** Change `src/treg/web/skill.md` and regenerate:

```bash
python3 scripts/build_plugin.py            # regenerate BOTH plugins
python3 scripts/build_plugin.py --check    # fail if either is stale (also a test)
```

## Listing runbook

Ordered so that everything self-serve ships before anything with a queue.

### 1. Own marketplace — live on merge

Nothing to submit. Merging to `main` publishes it; the install lines above start working.

Bump `version` in `pyproject.toml` and both manifests together — `test_plugin_version_tracks_the_package`
pins all of them to one value.

### 2. ClawHub (clawhub.ai) — ✅ LIVE, self-serve, CLI-based

**Published 2026-08-14: [clawhub.ai/superdesigndev](https://clawhub.ai/superdesigndev) · `treg@0.11.0`
· owner `@superdesigndev` · MIT-0.** Verified by installing it back out of the registry into a scratch
dir — the delivered `SKILL.md` is byte-identical to `skills/treg/SKILL.md` in this repo.

The publisher org was created with
`clawhub publisher create superdesigndev --display-name "Superdesign dev, Inc."`. Publishing under a
personal handle would also have worked — `--owner <handle> --migrate-owner` moves a skill later — but
a company handle reads as a product listing rather than a side project.


`https://clawhub.ai/submit` **404s** (Hermes' CLI still points there; its `--to clawhub` is a stub).
The live path is the npm CLI:

```bash
npm i -g clawhub
clawhub login                     # device flow; opens a browser (--no-browser prints a URL + code)
clawhub whoami                    # confirm before publishing

clawhub skill publish "$PWD/skills/treg" \
  --slug treg --name "treg" --version 0.11.0 \
  --changelog "..." \
  --topics "api,seo,serp,backlinks,enrichment,scraping,agent-tools,web-search" \
  --source-repo superdesigndev/treg \
  --source-commit "$(git rev-parse HEAD)" \
  --source-ref main --source-path skills/treg \
  --dry-run                       # drop --dry-run to publish for real

# Publishing is TWO PHASES. The command above only SUBMITS — the version sits at
# `pending.publication`, invisible on every public surface, until a scan is attached to it:
clawhub scan --slug treg --version 0.11.0 --update    # took ~37 minutes; let it run
```

**Do not skip that second command, and do not conclude the publish failed while it runs.** This is
the single least obvious thing about ClawHub and it is not in their docs. `clawhub skill publish`
reports `"status": "pending-publication"` and exits; nothing else happens on its own. `scan --update`
("write published scan results back to the selected version") is not a convenience — it is the step
that completes publication. Watch the moderation record flip:

```bash
curl -s -H "Authorization: Bearer $(clawhub token)" \
  https://clawhub.ai/api/v1/skills/treg/moderation
```

| | before `scan --update` | after |
|---|---|---|
| `legacyReason` | `pending.publication` | `scanner.llm.review` |
| `reasonCodes` | `[]` | `["review.llm_review"]` |
| public `GET /api/v1/skills/treg` | 403, hidden | returns the skill |

Note `package publish` has a `--wait` flag that polls to a definitive state; **`skill publish` does
not**, and `/api/v1/publish/attempts/<attemptId>` 404s for skill attempts. So for skills there is no
progress indicator at all — run `scan --update` and wait it out.

Verified against clawhub CLI **v0.23.3** — the published docs are thinner than the real `--help`,
so read the CLI:

- **Pass an ABSOLUTE path.** A relative `./skills/treg` fails with `Path must be a folder`, because
  paths resolve against `--workdir` + `--dir` (which defaults to `skills`), so it looks for
  `skills/skills/treg`. `"$PWD/skills/treg"` is the reliable form.
- **The `--source-*` flags are worth filling in.** They link the listing back to a GitHub repo,
  commit and path. That is the one provenance signal available on a registry whose own vetting
  history is the reason Hermes marks every ClawHub skill `community` trust.
- **`--categories` takes slugs the public API does not enumerate** (`/api/v1/categories` 404s and
  listings come back with empty `categories`). Do not invent one — use `--topics`, which is
  free-form, until a real category slug can be read off a live listing.
- `--dry-run --json` prints the resolved slug, version, file count and fingerprint without
  publishing. `latestVersion: null` means the slug is unclaimed.
- Publishing is not irreversible: `clawhub delete` soft-deletes a skill or withdraws one version,
  and `clawhub hide` / `undelete` exist too. Ownership can move later with
  `--owner <handle> --migrate-owner`, so publishing under a personal handle now does not lock the
  skill out of a `superdesigndev` org later.

Requirements the generated skill already satisfies: frontmatter `name` + `description` + semver
`version`, with `name` matching the parent directory (`treg`). A security scan runs post-publish;
flagged releases are hidden from the public catalog but stay in your dashboard.

⚠️ **ClawHub forces MIT-0 on every skill, with no per-skill override.** This repo is Apache-2.0 plus
a hosted-service restriction. `SKILL.md` is prose — it documents a hosted service rather than
implementing one — so licensing that one file permissively is consistent with wanting it copied
everywhere. It is a deliberate choice, not an oversight; do not let it drift into a claim about the
server code.

Listing here also feeds Hermes' Skills Hub automatically. Note Hermes hard-codes every ClawHub skill
to `community` trust after the Feb 2026 "ClawHavoc" incident (341 malicious skills), so this is a
discovery channel, not a trust signal.

#### Two verdicts, and only one of them gates visibility

Do not confuse these — one costs a day if you do:

- **Moderation** (`/api/v1/skills/treg/moderation`) is what hides or shows a skill. Ours is
  `verdict: "clean"`, `isSuspicious: false`, `isMalwareBlocked: false`, empty `reasonCodes`. Reading
  the CLI source: `isSuspicious` only prints an install-time warning that `--force` bypasses; only
  `isMalwareBlocked` hard-blocks.
- **`clawscan`** (`clawhub scan`) is an LLM review published as an **advisory on the listing**. It
  rated treg `suspicious` — and that changed nothing about visibility.

The v0.11.0 advisory raised four findings against `SKILL.md`. Three restate deliberate product
decisions; **one is worth weighing on its own merits, independent of ClawHub**:

| id | sev | what it says |
|---|---|---|
| `PE3` | **HIGH** | `cli_auth` consumes material from a CLI's keychain and relays it — "expands the agent's credential-access surface … without a strong, explicit consent boundary". Remediation suggested: per-tool opt-in before using keychain-derived credentials, and prefer scoped service tokens. |
| `SQP-1` | MEDIUM | the frontmatter `description` is broad enough to get the skill selected for loosely-related tasks |
| `SQP-2` | MEDIUM | `curl … \| sh` executes remote code with no checksum or signature |
| `EA2` | MEDIUM | the "ask once, not per command" guidance discourages per-command approval |

`EA2` and `SQP-2` are the trade-offs treg made knowingly. **`PE3` is a real description of a real
feature** and deserves a decision rather than a dismissal — see `docs/context/architecture/` for how
`cli_auth` binds. Static analysis and VirusTotal both came back clean.

### 3. Skill Store (aiskillstore/marketplace) + skills.sh

**Do NOT open a pull request against `aiskillstore/marketplace`.** Its README is explicit — *"PRs
adding skills will be closed"*; the repo is written to by the platform's own review pipeline, not by
contributors. Submit through the form instead:

> <https://skillstore.io/submit> — one required field, and it accepts a **directory**, so give it:
> `https://github.com/superdesigndev/treg/tree/main/skills/treg`

GitHub auth is optional (it only adds review notifications); anonymous submissions are allowed. The
audit there is **report-only** — a risk finding does not block publication. This is still worth doing
because `aiskillstore/marketplace` is one of only two repos in Hermes' `KNOWN_MARKETPLACES`, so it is
the cheapest route into Hermes' `claude-marketplace` source. Note `npx skillstore` is install-only —
there is no CLI submit path.

**skills.sh / `npx skills` — pass `-s treg`, always:**

```bash
npx skills add superdesigndev/treg -s treg     # ✅ just the treg skill
npx skills add superdesigndev/treg             # ❌ installs NINE skills
```

Verified on a scratch machine: the bare form scans the whole repo, finds `.agents/skills/*` as well
as `skills/treg/`, and installs this repo's **internal development skills** (`dev-local`,
`tools-registry-context`, `write-provider-skill`, `add-oauth-provider`, `vendor-listing`, the three
Google-provider skills) into the user's agent alongside treg. That is our layout leaking into a
product install. `skills.sh.json` does not prevent it — it only supplies category labels.

`-s treg` scopes it to one skill and is what every install line we publish must carry. With it, that
one command still covers 70+ coding agents beyond Claude Code.

### 4. anthropics/claude-plugins-official — the queue

Submit at <https://claude.ai/settings/plugins/submit>. Highest-trust placement, and it also feeds
Hermes. It is a review queue — the same "takes too long" risk as the MCP track — which is exactly why
it is last: everything above is already shipping by the time it is filed.

Entries in that directory use `git-subdir` with a pinned `ref` **and** `sha`, so being inside this
monorepo is not a blocker. **Tag a release and give them the tag.**

## The fourth door: treg.to itself

`GET /.well-known/skills/index.json` + `/.well-known/skills/treg/SKILL.md` (in `src/treg/api.py`)
make this host a first-class skill source under the agentskills.io convention — no registry, no
review, no third party. Hermes reads it directly. Because it goes through the same `_serve_md` as
`/skill.md`, `{BASE}` is templated to the **serving** host, so a self-hosted registry advertises
itself rather than treg.to.

## Before you submit anywhere

- `uv run --frozen python -m pytest tests/test_plugin.py tests/test_skill_md.py -q`
- `claude plugin validate .` — validates the marketplace manifest the way Claude Code itself parses
  it, which catches shape errors the JSON schema does not.
- Install it for real from a scratch clone, because markup that reads correctly still breaks:

  ```bash
  git clone --depth 1 https://github.com/superdesigndev/treg /tmp/treg-check
  claude plugin marketplace add /tmp/treg-check
  claude plugin install treg@treg
  claude plugin details treg@treg     # then: uninstall + marketplace remove
  ```

  `details` is the real check. It must report **Skills (1) treg** and **MCP servers (0)** — a zero
  skill count is the silent failure this whole layout risks, and any non-zero agents/hooks count
  means something at the repo root leaked into the plugin.
- `claude plugin tag` creates the `treg--v{version}` release tag and validates that `plugin.json`
  and the enclosing marketplace entry agree — worth running before handing a tag to Anthropic.
- Confirm the copy still says treg **compares** providers and the caller chooses. treg does not route
  and does not fail over; the landing page had to be corrected for that claim once already, and a
  store listing is much harder to correct. `test_the_listing_does_not_promise_routing` guards it.
- Confirm the one-line positioning is identical in all three places (`test_one_product_one_sentence`).
