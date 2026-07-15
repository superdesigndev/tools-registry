# tools-registry (treg) - quick start

Share your team's **keys and skills**, without sharing your keys. One vault holds every secret, one
registry holds every skill. Your Claude Code, your teammates, and your agents call through a token you
can revoke - no key ever lands on a machine, and there's one library to maintain, not one per person.

Base URL: {BASE}

---

## 1. Install the CLI

```sh
curl -fsSL {BASE}/install.sh | sh
```

Installs the `treg` command (Python 3.12+) and points it at this registry. Later, upgrade in place with
`treg update`.

## 2. Sign in

First sign-in also registers you and gives you a personal team.

```sh
treg login                                # GitHub (opens a browser)
treg login --email you@company.com        # or an emailed 6-digit code
```

Agents/CI use a per-org token instead: `treg login --token <token>`.

---

## 3. Share a **skill** - the heart of treg

A **skill** is a whole capability your agent loads: a `SKILL.md` recipe + the secrets it needs + the
tool(s) it calls. Registering it once means **everyone on the team runs the exact same skill, the same
way, maintained in one place** - nobody re-writes the script or copies the key.

There are three shapes of skill, and treg handles all of them:

- **A skill that calls an API** (needs a credential) - e.g. `google-ads`, `intercom`, `gsc`.
- **A skill that just carries know-how** (no credential) - e.g. an SEO writer, a triage playbook.
- **A folder you already have** - treg builds the contract for you.

### Register a folder of skills in one pass

Point treg at your skills directory and it registers every one - building a `treg.json` for any that
lacks one (guessing the base URL, finding the secret it reads):

```sh
treg upload skills --dir ~/.claude/skills --all
```

```
Scanned ~/.claude/skills: 5 API-tool skill(s), 23 recipe-only.
  ✓ google-ads      (tool)
  ✓ intercom        (tool)
  ✓ seo-blog-writer (recipe)
  …
Imported 27/28 skills.
```

### A teammate installs any shared skill with one command

```sh
treg skill install seo-blog-writer        # or --all for the whole library
```

It writes the recipe into their `./.claude/skills/` - the API skills call through treg with their
token, so the key stays in the vault, never in the skill.

### Register one skill by hand

```sh
treg skill init --dir ./my-skill          # drafts treg.json (base_url + secrets)
treg skill add  --dir ./my-skill          # uploads recipe + secrets + tool, atomically
```

---

## 4. Or register a single **API endpoint**

When you just want one URL callable with a stored key:

```sh
treg secret add STRIPE_KEY --value sk_live_123
treg add stripe --base-url https://api.stripe.com --secret STRIPE_KEY
treg call https://api.stripe.com/v1/charges          # the key is injected server-side
```

`--secret` takes the secret's **name** or its id. Other auth shapes (an `x-api-key` header, a
`?apiKey=` query param, Basic auth, or OAuth) are supported per binding - see `treg tool add -h`.

### Bulk-register the keys already in your `.env`

```sh
treg upload env --select openai,stripe,resend
```

treg matches each variable against ~80 known providers and registers the ones you pick. Config vars
(`*_HOST`, `*_MODEL`) and your app's own secrets (`SECRET_KEY`, `DATABASE_URL`, `*_WEBHOOK_SECRET`) are
excluded automatically. A `CLIENT_ID`+`CLIENT_SECRET` pair is detected as OAuth and offered a guided
connect.

> Preview first with `treg scan` (read-only). Bare `treg upload` (no `env`/`skills`) does **both** for the current directory. It's idempotent -
> re-run any time, or `--replace` to update.

---

## 5. Point your agent at it

An agent needn't learn treg. Point Claude Code (or Codex/Gemini) at **{BASE}/llms.txt** and it's fluent
in the whole registry - or it just prefixes any upstream URL with the proxy and sends its token:

```sh
curl {BASE}/call/https://api.stripe.com/v1/charges \
  -H "X-Treg-Token: $TREG_TOKEN"
```

No key in the command. Revoke the token anytime - your key never moves.

---

## Handy commands

```sh
treg tool ls | secret ls | skill ls       # what's registered
treg calls                                # the audit log: who called what
treg health --run                         # validate every secret against its tool
treg org invite bob@company.com --role member
treg --help                               # every command has -h with examples
```

Full interactive walkthrough: {BASE}/tutorial
