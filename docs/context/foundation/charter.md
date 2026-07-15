---
title: Tools Registry — charter (what it is, why, the proxy model)
status: foundational
sources:
  - external:meetings/2026-06-30-jason-tools-registry.md
  - README.md
related:
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
  - interface/api.md
---

# Tools Registry — charter

> **Status:** the MVP described below is **shipped and live** (API + CLI + skill, proxy, 4 auth shapes,
> OAuth refresh/connect, health, 5 tools on `treg.ngrok.app`). This fragment records the vision + the
> "why"; the shipped detail lives in the architecture/interface fragments. Not yet: Render deploy, org
> isolation.

A remote **registry that turns the team's skills into shareable, callable tools.** Any team member's
**agent** (Claude Code / Codex / Gemini) — or a human via CLI/API — can call a tool **without owning its
credentials or rebuilding it**. (Internally we think of it as a *skill registry*: skills + data actions;
"tool registry" is the working name.)

> **Load-bearing idea — the proxy.** The consumer makes the **real upstream API call** (it already knows
> the API, e.g. PostHog) with its own params; the call is **routed through our proxy domain**, which
> **injects the auth** server-side. The consumer never holds the secret. This is the whole point — and it
> survives upstream API version changes, because we don't model the API, we relay it.

## Why
- **Easy sharing / onboarding** — give a teammate *one endpoint* for everything; add a new system without
  re-wiring each person. Example: a new marketing manager calls the PostHog tool with **no API key**.
- **Consumer is often an agent, not a human.** Start with the team; later anyone on Claude Code/Codex/etc.
- Later: **permission levels + audit log** (who used what).

## What a "tool" is
A **skill + its auth**. Auth shapes to support (from the real `superdesign-agi/.claude/skills/`): plain
**ENV vars**, **`.secret/` OAuth token files** (e.g. GCP, Google Ads), **CLI-auth** services, and full
**OAuth** connect flows — not just API keys. MCP is **optional, later**.

## Shape (MVP)
- **API + CLI + one registry skill.** The single `tools-registry` skill is how consumers *call* tools and
  how creators/admins *create / push / list / update / delete* them (full **CRUD**, via both API and CLI).
- **Onboarding flow:** an agent reads an install doc → installs the CLI → user auths → the agent
  identifies the local skills + env/secrets, **uploads/registers** them → returns an endpoint + a
  share-instruction for the team. (Web dashboard = phase 2; minimal, GitHub/Vercel-secrets-style.)
- **Auth from day one** (to store per-user/org secrets). **Allow everyone** to register for now;
  authorization tiers later.

## Security (MVP vs later)
- **MVP:** rely on **TLS/HTTPS** (like pasting a secret into GitHub/Vercel). The proxy already delivers
  "use a tool without the key locally" (the key lives in the registry; a member can build/register tools
  against a secret someone else uploaded — a placeholder).
- **Later (extreme):** operator registration that **mints a local private key** and signs credentials so
  only the server can decrypt them (banking-style) — interception-proof. Not MVP.

## Deploy & scale
Test server first → **Render** (easier to debug). The proxy is **thin** (IO-bound, low CPU/memory) → cheap
to scale; per-tenant cheap machine vs shared = **undecided**, built thin so either works.

## Boundaries / relationships
- **Standalone for now**, separate from SuperDesign. May **later merge with Loopni** (loops + context +
  tool access in one place); Loopni could itself become a tool/runtime in the registry.
- **Distinct from Intel Factory** (`intel-factory/`, which builds knowledge *lakes* per business). This
  shares *tools*, not knowledge. Likely a **community PR-back** flow for new connectors (Intel-Factory style).

## First proving ground
Auto-convert **every skill in `superdesign-agi/.claude/skills/`** into a tool and test each (PostHog, GSC,
…) — proving the *machine that makes any tool*, matching every auth shape. New tools after that = human-added.
