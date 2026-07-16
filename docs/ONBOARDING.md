# tools-registry — onboarding

The bootstrap an agent (or human) follows to start calling shared tools and to share its own.
The CLI is a thin client over the API at `https://treg.superdesign.dev`; the API is the only brain.

## 1. Install the CLI

```bash
curl -fsSL https://treg.superdesign.dev/install.sh | sh   # installs `treg`, points it at the registry
```

(Working from a clone instead? `uv sync && uv run treg --help`.)

## 2. Sign in

Three doors, one identity (your email):

```bash
treg login                                  # GitHub OAuth (opens the browser)
treg login --email you@example.com          # email one-time code
treg login --token <TOKEN>                  # agents/CI: a per-org token from a team owner
```

Your token identifies you on every call (`X-Treg-Token`). New here? `treg onboard` walks you
through everything with a disposable demo team.

## 3a. Use a tool someone already shared (consumer)

```bash
treg tool ls                                 # what's available
treg call posthog query/events --query limit=5
treg run gh -- pr list                       # vendor CLIs work too
```

You hold **no upstream key** — the registry injects it server-side.

## 3b. Share your own (creator)

Point treg at a project — it finds the provider keys in the `.env`, the skill folders, and the
installed catalog CLIs, and registers what you pick:

```bash
treg scan          # read-only preview; nothing leaves the machine
treg upload        # register (encrypted server-side); idempotent, --replace to update
```

For one skill or a tricky tool (multi-credential, OAuth), see the manual flow in
[`USAGE.md`](../USAGE.md) — `treg skill init` / `skill add`, `treg tool add --bind`,
`treg oauth connect`.

## 4. Verify + observe

```bash
treg call <tool> <upstream-path>     # smoke-test
treg calls                           # audit: who called which tool, when, status
treg health                          # credential health across the org
```

## Notes

- Secrets are write-only; the API never returns a stored value.
- A tool can reference a secret another member uploaded (use-without-hold).
- Everything is org-scoped: `treg org ls` / `treg org use <slug>` picks the active team.
