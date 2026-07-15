# tools-registry — onboarding

The bootstrap an agent (or human) follows to start calling shared tools and to share its own.
The CLI is a thin client over the API at `https://treg.ngrok.app`; the API is the only brain.

## 1. Install the CLI
```bash
# from the tools-registry repo
uv sync
uv run treg --help                  # or `pip install -e .` to get a global `treg`
```

## 2. Point it at the registry + authenticate
```bash
treg config --base-url https://treg.ngrok.app
treg register --email you@kidocode.com      # one-time; token saved to ~/.treg/config.json
#   already have a token from elsewhere?  ->  treg login --token <TOKEN>
```
The token is shown **once** at registration. It identifies you on every call (`X-Treg-Token`).

## 3a. Use a tool someone already shared (consumer)
```bash
treg tool ls                                 # what's available
treg call posthog query/events --query limit=5
```
You hold **no upstream key** — the registry injects it server-side.

## 3b. Share your local skills (creator)
An agent does this by reading each local skill and registering it:

1. **Identify** the local skills + their credentials. Skills live in `.claude/skills/<name>/` —
   a `SKILL.md` (the recipe), a script, and a `.secret/` or `.secrets/` dir with key files.
2. **Scaffold** a manifest (deterministic discovery of recipe + secrets):
   ```bash
   treg skill scaffold ~/.claude/skills/posthog --out posthog.json
   ```
3. **Complete** the manifest — the scaffolder can't read the script, so YOU set:
   - `tools[].base_url` — the upstream (e.g. `https://us.posthog.com`).
   - each `bindings[]` — where the credential goes: `location` (header|query), `name`,
     `format` (template with `{secret}`), `injector` (`env`/`secret_file`/`oauth`/`cli_auth`),
     and `secret_field` for JSON-blob secrets. A request may need several bindings
     (e.g. google-ads: an OAuth bearer **and** a `developer-token` header).
4. **Register** it (creates the bundle + secrets + tool(s) atomically):
   ```bash
   treg skill push posthog.json
   ```
5. **Share** — hand teammates the endpoint + the tool name. Their agent calls it with no key.

## 4. Verify + observe
```bash
treg call <tool> <upstream-path>     # smoke-test
treg calls                           # audit: who called which tool, when, status
```

## Notes
- Secrets are write-only; the API never returns a stored value.
- A tool can reference a secret another member uploaded (use-without-hold).
- Security MVP = TLS only (like pasting a secret into GitHub/Vercel). End-to-end local-key
  encryption is a later hardening, not required to start.
