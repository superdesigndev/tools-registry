---
name: write-provider-skill
description: Build a treg provider skill — the endpoint map + mistake map that lets an agent do real work on a platform API through treg's proxy. Use when adding a skill for a connected provider (Google Ads, LinkedIn, Meta Ads, TikTok, X, Instagram, Search Console), or when an existing provider skill needs verifying or extending.
---

# Writing a provider skill

A provider skill is **an MCP server made of documentation**. An MCP server gives a model a tool
list and typed parameters. We can't ship a server per platform, so the skill carries the same
payload as prose: *which endpoints matter, what the parameters really are, and where the API
lies to you.*

The third part is the one MCP can't give you, and it's where nearly all the value is.

## The rule that decides what goes in

> **Document what produces a wrong answer without producing an error.**

Ranked by value to an agent:

| Class | Why it matters | Example |
|---|---|---|
| **Silent wrong answers** | Agent reports confidently false results. Unrecoverable alone. | GA4: incompatible dimension+metric returns `"0"` per row, not an error — reads as "no revenue" |
| **Wrong-shaped errors** | Error doesn't describe the real cause, so retries go sideways | GA4: bad path returns a Google **HTML 404 page**, not JSON — a JSON parse throws something unrelated |
| **Unguessable identifiers** | Agent invents an ID and gets 403/404 forever | GA4 needs `properties/123456789`; GSC needs `sc-domain:x.com` vs `https://x.com/` |
| **Host splits** | Endpoint exists but not on the bound host | GA4 Admin API is `analyticsadmin`, tool is bound to `analyticsdata` → 404 |
| **Capability limits** | Agent attempts work the scope can't do | GA4 connection is `read` only |
| Endpoint list | Findable in official docs | — |

Anything in the bottom row: **link to the docs, don't restate them.** A skill that paraphrases
the API reference is worse than a URL — it goes stale and it's longer.

## Verification protocol — non-negotiable

**Every endpoint and every example body in the skill must have been executed.** Not adapted from
docs, not plausible — run.

```bash
scripts/dev-local.sh up
scripts/dev-local.sh cli connections ls          # confirm health + resource_ref
scripts/dev-local.sh cli call <provider> <path>  # always via /call/, never direct
```

Call through `/call/` rather than curl-with-a-token: that exercises binding injection, token
refresh, and the audit trail, which is where treg bugs surface.

Mark every endpoint **✅ verified** or **⚠️ unverified**, and date the skill. An unverified
endpoint is allowed — an *unmarked* one is not.

**Read the audit log when something 404s.** It records the exact upstream URL, which catches
client-side path mangling that the terminal output hides:

```bash
sqlite3 treg-dev.db "select method,path,status_code from callrecord order by id desc limit 5"
```

That's how the zsh `:r` bug below was found: the terminal showed a plausible Google error, the
audit log showed the request had gone to `...123456789unRealtimeReport`.

## Write-side testing

Read endpoints: call freely. **Write endpoints publish real things to real accounts and are not
reversible.** Before any POST/PUT/DELETE that creates content or spends money:

1. Get an explicit designated target from the human (throwaway account, sandbox ad account)
2. Get explicit per-provider go-ahead
3. If neither exists, mark the endpoint ⚠️ unverified and document it from the API reference

Never publish to a production account to satisfy a checkbox.

## Skill structure

Follow this order — it matches how an agent actually reads under time pressure:

```
frontmatter: name + description (description = the trigger; list the words a user would say)
# <Platform> via treg
  one line: you call X through treg's proxy, you never hold a credential
  "All examples verified live on <date>"
## Setup (once per team)      — connect command, what capability you get
## Which <resource> to use    — how to READ the id, never guess it
## The endpoints              — table: path | method | purpose | verified?
## Common jobs                — 4-6 real tasks, full runnable bodies
## Reading the response       — shape gotchas (positional arrays, strings-not-numbers, timezone)
## Pitfalls                   — the silent-wrong-answer list. The most valuable section.
## Full documentation         — links out
```

## Pitfall classes that recur across providers

Check each one explicitly when building a new skill. Confirmed on GA4; **expected but
unconfirmed** elsewhere until tested.

- **Host split** — is there an admin/management API on a different hostname than the data API?
  The tool's `base_url` binds one host; the other is unreachable via `/call/`.
  *Expect this on Google Ads (googleads vs googleadsapi surfaces) and Meta (graph vs business).*
- **Resource id format** — exact prefix, exact encoding. Read it from `treg connections ls`
  → `resource_ref`. If empty, `treg connections resources <id>` then `treg connections use`.
- **Positional response arrays** — headers and values matched by index, not name.
- **Numbers as strings** — cast before arithmetic.
- **Timezone/currency** — reports render in the *property's* timezone. Read it from the response
  before interpreting "today".
- **Silent truncation** — default row limits, `(other)` buckets for high-cardinality dimensions,
  pagination tokens that look like completion.
- **Shell quoting** — in zsh, `$VAR:runReport` triggers the `:r` history modifier and silently
  corrupts the path. Use `${VAR}:runReport`. Applies to any API with `:action` path suffixes.
- **Scope vs capability** — what the connection actually granted (`treg connections ls` →
  `scopes`, `capabilities`, `missing_capabilities`), not what the platform offers.

## Also record treg friction

This exercise doubles as a treg test. When an agent would plausibly guess wrong about *treg's own
interface*, note it — those are product bugs, not skill content. Found so far:

- `treg connections list` is invalid; the subcommand is `ls`
- `treg connections resources` takes a numeric id, not a provider name

## Checklist

- [ ] Connected via real OAuth; `connections ls` shows `health: ok`
- [ ] Every endpoint executed through `/call/`; each marked verified/unverified
- [ ] Deliberately probed for silent-wrong-answers (bad field names, incompatible combos)
- [ ] Confirmed which capability the connection actually has
- [ ] Checked for a host split
- [ ] Write endpoints either authorized + tested, or marked unverified
- [ ] Links out instead of restating the reference
- [ ] treg friction recorded separately from platform friction
