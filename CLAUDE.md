# tools-registry (`treg`)

**The tool catalog for your agent.** Point an agent at one base URL with one token and it can do the
job — without owning the API keys.

Two halves answer the same token, through the same `/call/`:

- **The catalog** — ~2,850 curated external endpoints across ~57 providers (SEO and backlinks, social
  and trends, people and company enrichment, ads, scraping). treg can serve eligible ones **on its own
  key**, metered per call from the team's prepaid balance ($1.00 free per new team). No provider signup.
- **Your own tools** — what a teammate registered: a paid API account, an OAuth connection, a vendor
  CLI, a `SKILL.md`. **A team's own key always wins over treg's, and those calls are never metered.**

The load-bearing mechanic: the caller makes the **real upstream request**, the proxy **injects the
credential server-side**, and relays the answer verbatim. We never model an upstream API, so we
survive its changes and the caller never holds a secret.

## Words to use, and words not to

One concept, one word. This was settled deliberately — mixed vocabulary is how the old framing keeps
coming back.

| Thing | Word |
|---|---|
| what an agent calls | **a tool** |
| the public half | **the catalog** |
| the team's half | **your own tools** (your keys & skills) |
| the server / deployment | **registry** — only here |

**Do not** call either half a *vault*, a *marketplace*, or *the registry*. "Vault" means safe storage,
which was the older security-led pitch; "marketplace" implies buying and sellers; "registry" is
infrastructure language that says nothing about what a user can now do.

Phrase everything as **what the agent can now do**, not what we store. The test: *"your team's shared
vault of skills and secrets"* fails it; *"2,800 tools your agent can call, plus your own"* passes.

## Do not document what is not built

An agent that believes a feature exists fails in a way nobody can debug. In particular: treg
**compares** providers of the same capability (`treg catalog search` shows them side by side with
prices) but does **not** route automatically or fail over. Choosing is the caller's. Say so wherever
the subject comes up, and change it only when the router actually ships.

## Repo conventions

**Docs are fragments.** Per-subsystem design docs live in `docs/context/`, one fragment per subsystem,
each naming its `src/treg/*` sources in frontmatter. Load one with the `tools-registry-context` skill.

**Before pushing:** run `bash .agents/skills/tools-registry-context/scripts/drift.sh`, map changed
sources → fragments, update them, and commit the docs **in the same commit as the code**.

**Three files move together** or they drift: `src/treg/web/tutorial.js` (the only interactive source)
and its two hand-kept prose mirrors, `src/treg/web/tutorial.md` and `docs/TUTORIAL.md`.

**Agent-facing files are the product's front door**, not documentation: `src/treg/web/llms.txt` and
`src/treg/web/skill.md` (the latter is installed into every agent by `install.sh`). Any change to how
treg works should ask whether these need to change too.

## Development

```bash
uv run --frozen python -m pytest -q     # the whole suite
uv run --frozen treg --help             # the CLI from this checkout
uv run --frozen python -m treg          # the server
```

- **Always `--frozen`.** Running `uv lock` / `uv sync` on an older uv rewrites `uv.lock` into an older
  format — a ~650-line diff that changes no versions. Hand-add new dependencies to the lock instead.
- **The package is split.** The base install is the **light CLI** (`httpx` + `questionary`); the
  FastAPI/DB stack is the `[server]` extra and the certificate authority is `[proxy]`. Never import a
  heavy dependency at the top of a CLI-path module (`cli.py`, `convert.py`, `skills.py`, `providers.py`,
  `localrun.py`, `shell.py`, `agents.py`, `egress.py`, `fsjail.py`) — it would re-bloat `pip install`.
- **Verify UI work in a browser.** Markup that reads correctly still breaks: a headline whose CSS is
  hand-tuned to its character count, a Vue `@click` naming a view that does not exist (fails silently),
  a cached asset that never reaches anyone.

## Money code

`ledger.py` is the **only** code path that moves money; `billing.py` is the only one that talks to
Stripe; `reconcile.py` is read-only. Everything is **integer micro-USD** — never floats, never cents.
Never route a ledger write through `audit.py`: it drops rows past its queue bound, which is right for
analytics and fatal for money. See `docs/context/architecture/money.md`.

## Pointers

- `README.md` — overview and quickstart · `USAGE.md` — the full CLI reference
- `/llms.txt` — the agent-onboarding file · `/tutorial` — the interactive walkthrough
- `docs/context/` — per-subsystem design fragments (start at `foundation/charter.md`)
