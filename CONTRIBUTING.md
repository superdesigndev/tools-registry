<!-- DRAFT SKELETON — you plan to author the full guide. This is a starting frame; fill / rewrite freely. -->

# Contributing to tools-registry

Thanks for your interest! This is a starting frame — sections marked _TODO_ are for you to expand.

## Getting set up

```bash
git clone https://github.com/<org>/tools-registry
cd tools-registry
uv sync                     # install deps (uv — https://docs.astral.sh/uv/)
cp .env.example .env        # then: uv run python -m treg keygen  → set TREG_SECRET_KEY
uv run pytest -q            # the full suite should pass before you start
```

A one-command local stack is in `scripts/dev-local.sh` (also the `dev-local` skill under `.claude/skills`).

## Project layout

- `src/treg/` — the app (FastAPI API, proxy, CLI, runners). The **design docs** in `docs/context/` explain
  each subsystem and cite the source files they cover — read the relevant fragment before changing code.
- `tests/` — the test suite (pytest). New behavior needs a test.
- `docs/context/` — per-subsystem design fragments (the source of truth for "how it works").

## Making a change

1. Branch off `main`.
2. _TODO: coding conventions — style, typing, commit message format._
3. Add or update tests; run `uv run pytest -q` (all green).
4. If you changed a subsystem, update its fragment in `docs/context/` in the same PR.
5. Open a PR. CI runs the tests + a secret scan; a maintainer reviews.

## What to work on

_TODO: good-first-issue labels, the roadmap, areas that need help._

## Reporting bugs / security issues

Normal bugs → a GitHub issue. **Security vulnerabilities → see [SECURITY.md](SECURITY.md)** (report
privately, never in a public issue).

## Code of conduct

_TODO: link a CODE_OF_CONDUCT.md (e.g. Contributor Covenant)._
