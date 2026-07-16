# Contributing to tools-registry

Thanks for your interest!

## Getting set up

```bash
git clone https://github.com/superdesigndev/tools-registry
cd tools-registry
uv sync                     # install deps (uv — https://docs.astral.sh/uv/)
uv run pytest -q            # the full suite should pass before you start
```

No `.env` needed for dev — every setting has a working default (ephemeral encryption key, local
sqlite). The `TREG_*` knobs for persistence / a real deployment are documented in the README's
**Configuration** section (and `docs/context/ops/deploy.md`).

A one-command local stack is in `scripts/dev-local.sh` (also the `dev-local` skill under `.claude/skills`).

## Project layout

- `src/treg/` — the app (FastAPI API, proxy, CLI, runners). The **design docs** in `docs/context/` explain
  each subsystem and cite the source files they cover — read the relevant fragment before changing code.
- `tests/` — the test suite (pytest). New behavior needs a test.
- `docs/context/` — per-subsystem design fragments (the source of truth for "how it works").

## Making a change

1. Branch off `main`.
2. Match the surrounding style — plain Python with type hints, no new dependencies without a reason.
   Commit messages follow Conventional Commits (`feat(scope): …`, `fix: …`, `docs: …`).
3. Add or update tests; run `uv run pytest -q` (all green).
4. If you changed a subsystem, update its fragment in `docs/context/` in the same PR.
5. Open a PR. CI runs the tests + a secret scan; a maintainer reviews.

## What to work on

The roadmap lives at the end of [`README.md`](README.md) (MCP support, finer permission tiers,
key-management hardening). Bug reports and small fixes are welcome anytime; for a larger feature,
open an issue first so we can agree on the approach before you invest in it.

## Reporting bugs / security issues

Normal bugs → a GitHub issue. **Security vulnerabilities → see [SECURITY.md](SECURITY.md)** (report
privately, never in a public issue).

## Code of conduct

Be kind and constructive. Harassment or personal attacks are not tolerated; maintainers may remove
comments or contributors that cross that line.
