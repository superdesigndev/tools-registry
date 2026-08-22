---
page_id: p2
slug: /use-cases/lead-enrichment-for-ai-agents/
seo_title: "Find and Verify Work Emails from One Agent Prompt | treg.to"      # 59
meta_description: "Let your agent find companies, identify decision-makers and verify work emails through one key. Pay per lookup, not per seat. $1.00 free to start."  # 146
h1: "Find and Verify Decision-Makers from One Agent Prompt"
hub_title: "Lead enrichment"
hub_blurb: "Find companies, identify the decision-maker, verify the work email. Misses are free."
price_old: "$142/mo"
price_old_label: "Apollo + Hunter + Lusha, at list"
price_new: "$0.0245"
price_new_label: "what our run actually cost"
seo_terms:
  primary: "lead enrichment for ai agents"
  secondary:
    - "work email finder api pay per call"
    - "company search api per record"
    - "email verification api without subscription"
    - "prospecting api for developers"
ad_keywords:
  - "apollo api alternative"
  - "hunter io api pricing"
  - "email finder api"
  - "b2b data enrichment api"
  - "clearbit alternative api"
capabilities: [people.email.find, people.email.verify, people.enrich, companies.search, companies.enrich]
facts_used: [F-01, F-02, F-03, F-04, F-05, F-06, F-07, F-08, F-09, F-10, F-11, F-13, F-21, F-40, F-41, F-42, F-50, F-80, F-90, F-92, F-43]
hypothesis: "Highest commercial intent of the five. Predict the lowest cost per first successful call."
verify_after: 2026-08-31
status: proof populated from a real run 2026-08-17 ($0.0245) · ready for build · revised against 30-day telemetry 2026-08-17
---

# Page 2 — Lead enrichment

---

## Hero

### Find and Verify Decision-Makers from One Agent Prompt

Describe the companies you want and who you need inside them; your agent finds them, enriches the
profiles and verifies the work emails, paying per lookup instead of per seat.

**[ Run This Workflow Free ]**   **[ See the Example ]**

`S-TRUST-HERO`

*Sub-line:* Eleven providers answer company search alone, and your agent sees what each one costs before it
calls. Email verification has run **963 times at a 100% success rate**.

---

## The old way vs. the treg.to way

| | The old way | With treg |
|---|---|---|
| **What you pay for** | Apollo $59/seat + Hunter $34/mo + Lusha $49/mo, and a credit pool that expires monthly | One prepaid balance. A found email is $0.0245, and a miss costs nothing |
| **Keys** | One account, one login and one API key per provider, spread across machines and `.env` files | One treg token. Every tool in the catalog answers to it |
| **Picking a provider** | You use whichever tool the team bought last year | `catalog get` lists all eleven company-search providers with price, measured success rate and median speed |
| **Commitment** | Seat minimums and annual contracts before you know whether the data covers your market | No subscription. Test the coverage for cents, then decide |
| **The workflow** | Export from the prospecting tool, paste into the enrichment tool, upload to the verifier, reconcile three CSVs | One agent run: find, enrich, verify, deduplicate — in one pass |

---

## A real workflow

### Copy this into Claude Code, Cursor, Codex or opencode

```text
Find 50 recently funded AI companies with 20–200 employees. Identify their
head of growth or VP of marketing and verify their work email.
```

**[ Copy Prompt ]**

### What happens when you run it

**The agent finds the companies.** `catalog_search "search companies by size and funding"` returns
eleven providers for that one job — and this is the sharpest price spread in the whole catalog:

| Provider | Cost per company | Success rate | Median |
|---|---|---|---|
| **Akta** | free | 100% (248 calls) | 3.2 s |
| **Hunter Discover** | free | 100% (24 calls) | 1.7 s |
| **The Companies API** | $0.0019 | 100% (339 calls) | 0.3 s |
| **Lusha** | $0.004992 per 25 results | not yet measured | — |
| **LeadMagic** | $0.025 | 70% (10 calls) | 3.2 s |
| **Apollo** | $0.026 per page | not yet measured | — |
| **Diffbot** | $0.0299 | 100% (5 calls) | 0.7 s |
| **PDL** | $0.38 | not yet measured | — |
| **Crunchbase** | own key only | — | — |

Same job. **A 200× spread, and the cheapest option is free.** Coverage differs, which is exactly why the
price is not the only column — but nobody should pay $0.38 a record without knowing $0.0019 was on the
table.

**It finds the people.** Two routes, and the cheaper one is usually the right one. If you want everyone
at a company, `hunter.companies.emails` returns the known addresses at a domain in a single call —
**100% success across 597 calls, 0.4 s median**, the fastest endpoint in this whole workflow. If you want
one named person, `hunter.people.email.find` costs $0.0245 and **only charges when it finds someone**
(100% across 582 calls, 1.4 s). Enrichment fills thin profiles: Hunter $0.0245 (356 calls, 0.3 s),
Apollo $0.026 (263 calls, 0.3 s), LeadMagic $0.025, Coresignal $0.392.

**It verifies before you send — and this is the step that matters most.** Verification is the single
most-called enrichment endpoint on treg.to: **963 calls at 100% success**. It runs at $0.00625 with
LeadMagic or $0.01225 with Hunter. Finding an address is cheap; sending to a dead one costs you a domain
reputation, which is why the teams already doing this at volume verify more than they search.

### What comes back

```text
50 companies · 20–200 employees · funded in the window · pulled [date]

COMPANY          SIZE   LAST ROUND      CONTACT              TITLE              EMAIL          STATUS
<company>        84     <round, date>   <name>               VP Marketing       <email>        verified
<company>        142    <round, date>   <name>               Head of Growth     <email>        verified
<company>        37     <round, date>   <name>               Head of Growth     —              no match
...

SUMMARY   50 companies · 47 contacts identified · 41 emails verified · 6 no match
COST      $[from your run]   (misses were not charged)
```

*Structure is illustrative. Values come from the providers, relayed unchanged.*

**[ Copy Prompt ]**

---

## Proof — from one real run

*Run on treg.to, 17 Aug 2026. Every figure below is from the Activity log of that run.*

| Field | Value |
|---|---|
| Providers considered | **11** for company search, **3** for finding a work email |
| Providers selected | `hunter.x.discover-companies` (free) · `hunter.people.email.find` |
| Why | Discovery is free on Hunter and takes a plain-English brief. For the email, Hunter's finder carries one of the longest records in the catalog — **582 calls, 100% success, 1.4 s median** — and it only charges when it finds one |
| Total cost of the run | **$0.0245** — company discovery was free, one email was found and charged |
| Subscription cost avoided | **$142/mo** at list — Apollo $59/seat + Hunter $34 + Lusha $49 |
| Time to completion | Under 5 seconds across both calls |
| Data freshness | The address came back **verified `valid` on 2026-08-17**, sourced from a page last seen 2026-07-30 |
| Companies returned | 9, all matching "AI infrastructure, recently funded, 20–200 employees" |
| Emails found and verified | 1 of 1 attempted, confidence score 80 |

**What the discovery call returned, for $0.00:** nine companies with their domains and a count of known
addresses at each — Daloopa (223), ZincFive (64), Ethernovia (51), RunPod (33), AttoTude (19), Netris (17),
Bobyard (16), Normal Computing (9), Arycs Technologies (4). Hunter resolved the plain-English brief into
explicit filters and returned them: headcount bands `20-50` and `51-200`, funding series pre-seed through
series C+.

**One call in this run failed and cost nothing.** A company-search request was rejected for sending a
boolean as a string — HTTP 400, `$0.00` charged. Your parameter mistakes are free; only answers are billed.

> **The honest read of this run:** one verified email is a demonstration, not a benchmark. Hunter's 100%
> over 582 calls is the measured **success rate of the endpoint**, not a promise that it finds every
> person you ask for. The number that matters for your list is the hit rate on *your* market, which is
> what the free tier is for — and misses cost nothing either way.

---

## Three things you can do the day you sign up

**Build a list for a campaign you have not committed to yet.**
Test whether your ICP even exists at the size you assumed. Company search starts free, so you can check
the market before anyone signs a contract for the data.

**Enrich the leads you already have, without a new seat.**
Point the agent at your existing CSV of names and domains. It fills in titles, company size and verified
emails, and marks the rows it could not resolve rather than guessing.

**Stop paying for bounces.**
Verify every address before it enters the sequence. At $0.00625 a check, verifying a 2,000-row list costs
about $12.50 — and Hunter's finder does not charge you for the ones it cannot find.

---

## Who this is for

- **Founders** doing their own outbound, who need 50 good contacts this week and cannot justify a seat.
- **SDR teams** whose credits run out mid-month, and who want per-lookup pricing that matches actual usage.
- **Growth teams** testing a new segment where they do not yet know which provider has coverage.
- **Developers building sales agents** who want one HTTP surface across finders, verifiers and company
  data instead of four vendor SDKs.

---

## Before you sign up

**Why not just call the providers directly?**
Because this workflow is not one provider. Finding companies, identifying people and verifying email are
three different products, and the vendor that is best at one is rarely best at the others — the table
above is 200× wide for a single job. Holding accounts with all of them to find out is the expensive way
to learn it. If you already pay for one, connect it and those calls route through your key, unmetered.
treg.to is closer to OpenRouter for agent tools than to a data vendor: one base URL, one token, many
providers behind it.

**How are credentials handled?** — `S-OBJ-CREDENTIALS`

**Can I choose a specific provider?** — `S-OBJ-CHOOSE`
*(Vertical note: often the right call here. Coverage varies by region and company size far more than it
varies by price, so once you find the provider that covers your market, pin it.)*

**Can I use my existing provider key?** — `S-OBJ-OWN-KEY`
*(If your team already pays for Apollo or Hunter, this is the first thing to do. Those calls stop costing
you anything on treg.to.)*

**What happens if a provider fails?** — `S-OBJ-FAILURE`

**How much does a call cost?**
A found work email is $0.0245 with Hunter and a miss is free. Verification is $0.00625. Company search
ranges from free to $0.38 a record depending on provider. The exact price is shown before the call, and
treg.to adds no markup to what the provider charges. New teams start with $1.00 of free credit — enough
for roughly 40 verified email lookups before you spend anything.

**Which agents does it work with?** — `S-OBJ-AGENTS`

---

## Final section

### Your next list can be built, enriched and verified in one prompt

**[ Run This Workflow Free ]**

`S-FINAL-CTA-TRUST`

---
---

# Ad and creator kit

### Responsive search ad headlines
| Headline | Chars |
|---|---|
| `Verified Emails, Per Call` | 25 |
| `Find Buyers From One Key` | 24 |
| `No Seat. No Credit Pool.` | 24 |

### Search ad descriptions
| Description | Chars |
|---|---|
| `Find companies, identify buyers, verify emails. One key, pay per lookup. $1.00 free.` | 84 |
| `A found email is $0.0245. A miss costs nothing. No seat, no monthly credit pool.` | 80 |

### Creator video hooks
1. "Eleven providers sell company data. One charges 200 times what another charges for the same row."
2. "I built a verified 50-lead list in one prompt. Here's the actual bill."
3. "Stop paying for leads you never contact. Pay for the lookups that found someone."

### X post hook
`Same job — "find companies matching this profile" — priced across 11 providers: free, $0.0019, $0.025, $0.38 a record. Your agent can see that table before it calls. Most sales tools can't.`

### High-intent keyword phrases
`work email finder api` · `company search api pay per record` · `email verification api pricing` ·
`lead enrichment api for developers` · `b2b prospecting api no subscription`

### Negative keywords
`free email finder` · `gmail` · `personal email lookup` · `jobs` · `resume` · `linkedin scraper free` ·
`email marketing software` · `newsletter` · `spam` · `phone number lookup`

### Demonstration a creator can reproduce
Run the workflow prompt on a real ICP, then open the Activity page and show the per-call costs next to the
list. The strongest beat is the miss: show a lookup that found nothing and cost $0.00.

### Measurable hypothesis
Highest commercial intent of the five, so this page should produce the **lowest cost per first successful
call** at day 14. If it does, it is the vertical to concentrate spend on. Watch the second-order signal
too: enrichment buyers are the most likely to connect their own existing key, which lowers revenue per
account while raising retention — worth knowing before scaling.

---

## Numbers used on this page

`F-01` `F-02` `F-03` `F-04` `F-05` `F-06` `F-07` `F-08` `F-09` `F-10` `F-11` `F-13` `F-21` `F-40` `F-41`
`F-42` `F-50` `F-80` — defined in `_facts.md`, verified 2026-08-17.
