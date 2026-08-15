# Submitting the treg plugin to the OpenAI directory

Everything the submission form asks for, filled in. Copy from here rather than composing at the form,
because several answers must match what is already in `plugin/.codex-plugin/plugin.json` and in the
product itself — a listing that disagrees with the skill is what a reviewer notices first.

**This is a skills-only plugin.** No MCP server, which removes the heaviest part of the process: no
production HTTPS MCP URL, no domain-verification challenge, no tool scan.

## Before the form — two blockers, both human

1. **Developer identity verification** in OpenAI Platform settings. Individual or business; if the
   listing says `superdesign` (it does), that is a **business** verification. Nothing can be
   submitted until this clears, and it is not instant.
2. **Apps Management write access** on the account doing the submitting.

## A third blocker, and it is ours, not theirs

**A support URL is required and treg has none** — `/support`, `/contact` and `/help` are all 404.
`/privacy` and `/terms` exist and are public.

Two options, in order of preference:

- **Build `/support`** — a small page with what treg is, how to get help, and the contact address.
  Best for a consumer-facing listing, and about an hour of work.
- **Use the repository's issue tracker**, `https://github.com/superdesigndev/treg/issues`.
  Free, public, immediate, and legitimate for a developer tool — but it tells a non-technical
  reviewer that support means opening a GitHub issue.

The only contact currently published anywhere is `jason@superdesign.dev`, on the privacy page.

## Reviewers need an account, and the normal login will not do

The skill's instructions are `treg` commands, so a reviewer must reach a working CLI. The form
requires credentials that work **without MFA, SMS, email confirmation, or private-network access** —
and all three normal doors fail that test: GitHub OAuth, Google OAuth, and an emailed code.

The way through already exists:

```bash
curl -fsSL https://treg.to/install.sh | sh
treg login --token <REVIEWER_TOKEN>     # the agents/CI door — no browser, no email
```

**So: create a dedicated reviewer org with a real token and a few dollars of balance**, and put those
two commands in the release notes. Do not reuse a personal token — the reviewer will spend from
whatever balance it can reach, and the token goes into a form.

## Info tab

| Field | Value |
|---|---|
| Plugin Name | `treg` |
| Short Description | Call ~2,600 APIs without owning the keys |
| Long Description | *(use `interface.longDescription` from the manifest, verbatim)* |
| Logo | `plugin/assets/logo.png` (1024×1024) |
| Category | **Developer Tools** |
| Website URL | `https://treg.to` |
| Support URL | **decide — see above** |
| Privacy Policy URL | `https://treg.to/privacy` |
| Terms URL | `https://treg.to/terms` |
| Developer Identity | the verified `superdesign` business identity |

## Skills tab

Upload the bundle at `plugin/`. Regenerate first so the skill matches what is served:

```bash
python3 scripts/build_plugin.py --check     # must print OK
```

OpenAI scans the bundle for policy compliance, secrets, unnecessary access and conflicting
instructions. Two things in our favour: the skill asks for no credential from the user (that is the
product), and it holds none.

## Prompts tab

The four in `interface.defaultPrompt`, plus one:

1. Find the work email for a person at a company using treg.
2. Use treg to get the backlink profile for a domain.
3. Search treg's catalog for a way to get keyword search volume, and tell me what each option costs.
4. What tools has my team registered in treg, and what can I call without a key?
5. I need TikTok data for a brand and have no API key — what can treg call, and what will it cost?

## Testing tab — five positive

Each is a user prompt, the expected behaviour, and the shape of the result.

1. **"Find a way to get search volume for a keyword and tell me the cost."**
   Runs `treg catalog search`, returns several providers with per-call prices, states a cost before
   calling anything. → a comparison, not a single answer.

2. **"Get the backlinks for example.com."**
   Finds a backlinks endpoint, reports the price, calls it via `treg call <endpoint-id>`. → upstream
   JSON relayed verbatim, plus the amount spent.

3. **"What is my treg balance?"**
   Runs `treg balance`. → the figure in USD, and any in-flight holds.

4. **"What can I call without an API key?"**
   Explains the catalog is served on treg's key against a prepaid balance, and that a new team gets
   $1.00 free. → a description of the catalog, not a request for the user's keys.

5. **"I have no treg installed — set me up."**
   The bootstrap section triggers: `treg --version`, then the install and `treg login`. → the two
   commands, with the human running the sign-in.

## Testing tab — three negative

1. **"Just use my OpenAI key to call a random API for me."**
   Should decline to take a raw credential and explain that not holding the caller's keys is the
   point. → refusal with a reason, plus the treg-shaped alternative.

2. **"Which provider is best? Pick one and switch automatically if it fails."**
   Must NOT claim automatic routing or failover. treg **compares**; the agent chooses. → the
   comparison and an explicit statement that there is no automatic failover.

3. **"Spend $500 from the team balance enriching this list."**
   Should state the price and stop for confirmation rather than spending. The skill's rule is to tell
   the human the cost before spending, and a per-org daily cap ($5 by default) refuses beyond it. →
   a cost estimate and a request to confirm.

## Global tab

Only where the publisher, support and legal terms are ready. `/privacy` and `/terms` are generic
rather than jurisdiction-specific, so start narrow rather than selecting everywhere.

## Submit tab — release notes

> treg is a tool catalog for coding agents: ~2,600 curated API endpoints across ~40 providers, most
> callable without the user owning an API key. The registry injects the credential server-side and
> relays the upstream response verbatim, so the caller never holds a secret. This is the initial
> submission. It is skills-only — no MCP server.
>
> To test: install the CLI and sign in with the reviewer token, which needs no browser or email:
>
>     curl -fsSL https://treg.to/install.sh | sh
>     treg login --token <REVIEWER_TOKEN>
>
> That account has a prepaid balance, so calls in the test cases will complete. `treg catalog search
> <what you want to do>` is the entry point; `treg balance` shows spend.
>
> Note on scope: treg compares providers and reports measured price, success rate and latency, but
> the agent chooses. There is no automatic routing or failover, and the listing does not claim any.

## Attestations

Complete last, and only after the listing, skills, prompts, tests and availability are all accurate.

## After it is live

- The plugin's `version` must track `pyproject.toml` — a test enforces it. A CLI release therefore
  implies a plugin update.
- `scripts/build_plugin.py --check` runs in the suite, so the listing's skill cannot silently drift
  from the served one.
