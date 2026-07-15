---
title: Glossary
status: reference
sources:
  - external:meetings/2026-06-30-jason-tools-registry.md
related:
  - foundation/charter.md
  - architecture/data-model.md
---

# Glossary

- **Tool** — a registered, callable capability = an upstream `base_url` + a list of credential
  **bindings**. Resolved by name or by host (URL-passthrough).
- **Binding** — one credential injection on a tool: `{secret_id, injector, location, name, format,
  secret_field}`. A tool applies all of its bindings to every call.
- **Secret** — a stored credential (kind `env` | `secret_file` | `oauth` | `cli_auth`), Fernet-encrypted
  at rest, never returned to clients.
- **Injector** — the code that places a secret into a request (a header or query param) per its binding;
  string-shapes (`env`, `cli_auth`) vs JSON-blob shapes (`secret_file`, `oauth`).
- **Bundle (skill)** — a named group = recipe (SKILL.md) + its secrets + its tool(s); registered
  atomically via `/skills`.
- **Proxy / relay** — the `/call` endpoint that forwards a consumer's real upstream request and injects
  auth server-side, so the consumer never holds the key.
- **URL-passthrough** — the agent-native call form: prefix the real upstream URL with `…/call/` instead
  of naming a tool + path.
- **Use-without-hold** — calling/building a tool whose secret is already in the registry, with no key
  locally.
- **Consumer / creator / admin** — the three personas of the shippable skill: who calls tools · who
  registers them · who manages + monitors.
- **Auto-refresh vs manual (OAuth)** — a refreshable token blob (has refresh_token + client creds) is
  kept fresh by treg; a bare uploaded token is injected as-is and re-uploaded on expiry.
