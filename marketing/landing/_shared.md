# Shared blocks — identical on all five pages

Edit here once; it propagates to all five. Pages reference these by id. Where a page needs a different
answer, it overrides the block and says so inline.

---

## `S-CTA-PRIMARY`
**Run This Workflow Free**

## `S-CTA-SECONDARY`
**See the Example**

## `S-TRUST-HERO`
$1.00 of free credit on every new team. No provider signup, no credit card. `F-02` `F-08`

## `S-SCALE-LINE`
2,600+ tools across 40+ providers, all answering one token. `F-01`

## `S-COMPARISON-TABLE`
The five rows of *the old way vs. the treg.to way*. Row 1 and row 5 are rewritten per page with that
vertical's providers and job; rows 2–4 are fixed.

| | The old way | With treg.to |
|---|---|---|
| **What you pay for** | *(per page: the specific subscriptions)* | One prepaid balance. Per call, in fractions of a cent |
| **Keys** | One account, one login and one API key per provider, spread across machines and `.env` files | One treg.to token. Every tool in the catalog answers to it |
| **Picking a provider** | You guess, or you use the one you already pay for | `catalog get` lists every provider for that job with price, the success rate treg.to has measured, median speed and when it last answered |
| **Commitment** | Annual plans and seat minimums to answer one question | No subscription. Stop calling and you stop paying |
| **The workflow** | *(per page: the specific manual stitching)* | One agent run, one report, one bill |

## `S-PROOF-TABLE`
Every page carries this table. **Fields must be filled from a real treg.to run and are not publishable
with estimates in them.**

| Field | Value |
|---|---|
| Providers considered for this job | `[ TO BE POPULATED — count from catalog_get ]` |
| Provider the agent selected | `[ TO BE POPULATED — endpoint id ]` |
| Why it was selected | `[ TO BE POPULATED — price / measured success rate / median speed ]` |
| Total cost of the run | `[ TO BE POPULATED — from the Activity page ]` |
| Subscription cost avoided | `[ TO BE POPULATED — the seats this replaces, at list price ]` |
| Time to completion | `[ TO BE POPULATED — wall clock, first call to final report ]` |
| Data freshness | `[ TO BE POPULATED — the date the provider's data reflects ]` |

## `S-OBJ-CREDENTIALS` — How are credentials handled?
The credential is injected on the server. Your agent makes the real upstream request through treg.to's
`/call/` endpoint; treg.to adds the key and relays the provider's answer back verbatim. No provider key
is ever written to your machine, your repo or your agent's context. Every call is recorded and attributed
to the token that made it. `F-04`

## `S-OBJ-CHOOSE` — Can I choose a specific provider?
Yes. Every endpoint has an id, and calling it by id calls that provider. If you want the choice made once
for the whole team, `treg org pin <capability> --provider <provider>` refuses calls to any other provider
of that capability. `F-13`

## `S-OBJ-OWN-KEY` — Can I use my existing provider key?
Yes, and it takes precedence. Register the key once and every call to that provider routes through it —
those calls are never metered against your treg.to balance. Your key always wins over treg.to's. `F-05`

## `S-OBJ-FAILURE` — What happens if a provider fails?
treg.to does not silently reroute you. That is deliberate: only you know which inputs you actually hold,
so treg.to relays your request rather than rewriting it. What your agent gets instead is the information
to recover — it already knows the alternatives and their parameters, so on a 429, a 5xx or a timeout it
can try the next provider and tell you which one it switched to. Failed calls are not billed. If a call
succeeded upstream but the answer was lost coming back, an `Idempotency-Key` returns the stored result
without paying twice. `F-06` `F-07`

## `S-OBJ-AGENTS` — Which agents does it work with?
The installer registers treg.to's MCP server into Claude Code, Cursor and opencode automatically. Any MCP
client that supports the authorization spec can connect with OAuth. Anything that can run a shell command —
Codex included — can use the `treg` CLI or plain HTTP. `F-09`

## `S-OBJ-DIRECT` — Why not call the providers directly?
*(Per page: the vertical's own version of the arithmetic. The fixed part below closes it.)*

You can, and if you already pay for a provider you should — connect that key and those calls route
through it, unmetered. What treg.to removes is the rest: an account, a contract and a key for every
provider you might need once, plus learning each one's API shape. It is closer to OpenRouter for agent
tools than to a data vendor: one base URL, one token, many providers behind it. `F-05`

## `S-FINAL-CTA-TRUST`
$1.00 of free credit on every new team. No credit card, no provider signup. treg.to is open source
(AGPL). `F-02` `F-08` `F-10`

## `S-INSTALL`
```text
set up treg — https://treg.to/llms.txt
```
Paste that into your agent, or run `curl -fsSL https://treg.to/install.sh | sh`.

---

## Wording rules for anyone editing these pages

- **`treg.to`, never bare `treg`,** in every title, meta description, heading and link anchor. Inside body
  copy, `treg.to` on first mention and wherever it could be read as the immunology term.
- One concept, one word: **a tool** (what an agent calls) · **the catalog** (the public half) · **your own
  tools** (the team's keys and skills). Never *vault*, *marketplace*, or *the registry*.
- "OpenRouter for agent tools" — **at most once per page**, and only as a supporting explanation.
- Say what the agent can now **do**, never what we store.
- Do not imply routing, fallback or provider selection on our side. `F-06`
- No hype: not *revolutionary*, *game-changing*, *unlock*, *supercharge*, *transform*, *all-in-one*.
- No customer names, quotes, logos or certifications. None exist.
