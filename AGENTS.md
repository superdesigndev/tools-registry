<!-- DRAFT SKELETON — for AI collaborators (Claude Code / Codex / etc.). You plan to author the full
version; this frames what such a file usually carries. Note: the private CLAUDE.md from the internal repo
did NOT come across — this is the public, contributor-facing equivalent, with no internal infra details. -->

# AGENTS.md — guide for AI coding agents

This file orients an AI agent (Claude Code, Codex, Cursor, …) working in this repository.

## Read first

- **Design docs are the source of truth.** `docs/context/` holds one fragment per subsystem, each citing
  the source files it covers. Before changing code, load the fragment for that area. The
  `tools-registry-context` skill (`.claude/skills/`) maps a source file → its fragment.
- **The charter:** tools-registry is a registry that turns a team's skills into shareable, callable tools;
  the core mechanic is a proxy that injects credentials server-side so a consumer never holds the secret.
  See `README.md`.

## Working agreement

- Run `uv run pytest -q` before and after changes; keep it green (add tests for new behavior).
- Keep changes minimal and scoped; match the surrounding style.
- When you change a subsystem, update its `docs/context/` fragment in the same change.
- _TODO: commit conventions, PR expectations, any do-not-touch areas._

## Security awareness

- Never commit real secrets. Placeholder/demo values are obviously fake (see `.gitleaks.toml`); CI scans
  every PR. Credentials belong in `.env` (gitignored), never in code, tests, or docs.
- Read **[SECURITY.md](SECURITY.md)** for the security model and the known limitations before touching the
  proxy, the runners, auth, or secret handling.

## Local setup

See **[CONTRIBUTING.md](CONTRIBUTING.md)**.

_TODO: expand with anything you want every agent to know before it edits._
