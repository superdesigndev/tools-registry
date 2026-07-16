# AGENTS.md — guide for AI coding agents

This file orients an AI agent (Claude Code, Codex, Cursor, …) working in this repository.

## Read first

- **Design docs are the source of truth.** `docs/context/` holds one fragment per subsystem, each citing
  the source files it covers in its frontmatter (`sources:`). Before changing code, load the fragment for
  that area; `docs/context/README.md` is the generated index (source file → fragment).
- **The charter:** tools-registry is a registry that turns a team's skills into shareable, callable tools;
  the core mechanic is a proxy that injects credentials server-side so a consumer never holds the secret.
  See `README.md`.

## Working agreement

- Run `uv run pytest -q` before and after changes; keep it green (add tests for new behavior).
- Keep changes minimal and scoped; match the surrounding style.
- When you change a subsystem, update its `docs/context/` fragment in the same change.
- Commits follow Conventional Commits (`feat(scope): …`, `fix: …`, `docs: …`); one logical change per
  commit. PRs should say what changed and why, and note which fragments were updated.

## Do not touch (without reading the fragment first)

- **The faithful-relay contract** (`src/treg/proxy.py`): the proxy alters only hop-by-hop headers, treg's
  own control headers, and the injected credential — never add upstream-specific modeling or buffering.
- **Security guards that look redundant on purpose**: the `expose_dev_code` double-guard (dev OTP only on
  local sqlite), the call-time SSRF check, the fail-loud missing-Fernet-key startup check, and the
  `treg run` allow-list/rlimits. Read `docs/context/architecture/` before changing any of them.

## Security awareness

- Never commit real secrets. Placeholder/demo values are obviously fake (see `.gitleaks.toml`); CI scans
  every PR. Credentials belong in `.env` (gitignored), never in code, tests, or docs.
- Read **[SECURITY.md](SECURITY.md)** for the security model and the known limitations before touching the
  proxy, the runners, auth, or secret handling.

## Local setup

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. Quick version: `uv sync && uv run pytest -q`; the live dev
stack is `scripts/dev-local.sh up` (server on `:18790`, hot-reload, own sqlite DB, email OTP shown
on-page) with a sandboxed CLI via `scripts/dev-local.sh cli <args>`.

## Things every agent should know before editing

- The API (`src/treg/api.py`) is the only brain — the CLI and the dashboard are thin clients over it.
  Put logic in the API, not in `cli.py` or the web layer.
- The dashboard (`src/treg/web/index.html`) is a single-file Vue app with **no build step** — edit the
  HTML directly; there is nothing to compile.
- Migrations run on every startup and must stay idempotent **and** portable across SQLite + Postgres
  (see `docs/context/ops/deploy.md` for the SQL rules).
- One fetch teaches you the product itself: `src/treg/web/llms.txt` (served at `/llms.txt`).
