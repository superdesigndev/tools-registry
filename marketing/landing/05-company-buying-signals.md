---
page_id: p5
slug: /use-cases/company-research-for-ai-agents/
seo_title: "Company Data and Funding for AI Agents | treg.to"           # 54
meta_description: "Let your agent search companies and pull funding, headcount and leadership through one key. Eleven providers, from free to $0.38 a record."  # 145
h1: "Research Any Company List Without Buying a Database"
hub_title: "Company data & funding"
hub_blurb: "Search companies, then pull funding, headcount and leadership. Eleven providers, free to $0.38 a record."
price_old: "$398/mo"
price_old_label: "Crunchbase + Diffbot, at list"
price_new: "$0.102"
price_new_label: "what our run actually cost"
seo_terms:
  primary: "company data api for ai agents"
  secondary:
    - "company search api pay per record"
    - "funding data api without subscription"
    - "company funding data api"
    - "firmographic api per call"
ad_keywords:
  - "crunchbase api alternative"
  - "company data api"
  - "funding data api"
  - "firmographic data api"
  - "buying intent data api"
capabilities: [companies.search, companies.enrich, people.enrich]
facts_used: [F-01, F-02, F-03, F-04, F-05, F-06, F-07, F-08, F-09, F-10, F-11, F-13, F-21, F-42, F-50, F-51, F-80, F-92]
hypothesis: "Sits on the catalog's strongest data — 11 providers, 200x spread. Predict the highest week-2 repeat-call rate of the five."
verify_after: 2026-08-31
status: proof populated from real runs 2026-08-17 ($0.10188) · funding proven, activity signals blocked by a platform bug · revised against 30-day telemetry 2026-08-17
---

# Page 5 — Company and buying-signal intelligence

---

## Hero

### Research Any Company List Without Buying a Database

Your agent can find companies by size, industry, tech or funding, then pull funding history, headcount
and leadership across eleven providers — paying per record instead of buying a database.

**[ Run This Workflow Free ]**   **[ See the Example ]**

`S-TRUST-HERO`

*Sub-line:* Company search alone has eleven providers behind it, from free to $0.38 a record. Your agent
sees that before it calls.

---

## The old way vs. the treg.to way

| | The old way | With treg |
|---|---|---|
| **What you pay for** | Crunchbase $99/mo + Diffbot $299/mo, whether you run a search this month or not | One prepaid balance. Company search starts at free and tops out at $0.38 a record |
| **Keys** | An account and a contract per data vendor, most of them annual | One treg token. Every tool in the catalog answers to it |
| **Picking a provider** | You buy one and find out afterwards whether it covers your market | `catalog get` puts all eleven side by side with price, measured success rate and median speed |
| **Commitment** | Annual data contracts to answer a question that changes every quarter | No subscription. Test coverage for cents before committing to anything |
| **The workflow** | Funding in one tool, headcount in another, people in a third, joined in a spreadsheet | One agent run: companies, their funding and the people attached to them, in one pass |

---

## A real workflow

### Copy this into Claude Code, Cursor, Codex or opencode

```text
Find AI infrastructure companies with 20-200 employees. For each one, pull its
funding history, headcount and leadership, and flag the ones that raised most
recently.
```

**[ Copy Prompt ]**

### What happens when you run it

**The agent finds the companies.** Eleven providers answer company search, and the spread is the widest
in the catalog:

| Provider | Cost per company | Success rate | Median |
|---|---|---|---|
| **Akta** | free | 100% (248 calls) | 3.2 s |
| **Coresignal** | free — returns ids; cost lands on the read | not yet measured | — |
| **Hunter Discover** | free | 100% (24 calls) | 1.7 s |
| **The Companies API** | $0.0019, or free with `simplified=true` | 100% (339 calls) | 0.3 s |
| **Lusha** | $0.004992 per 25 results | not yet measured | — |
| **LeadMagic** | $0.025 | 70% (10 calls) | 3.2 s |
| **Apollo** | $0.026 per page | not yet measured | — |
| **Diffbot** | $0.0299 | 100% (5 calls) | 0.7 s |
| **PDL** | $0.38 | not yet measured | — |
| **Crunchbase** | own key only, no per-call price | not yet measured | — |

**A 200× spread for the same job, and three of the eleven are free.** That table does not exist anywhere
else, because building it means holding accounts with all eleven.

**It pulls the detail.** Funding history at $0.10 a company with LeadMagic; company enrichment at
$0.026 with Apollo or $0.392 with Coresignal; the LinkedIn company page — headcount, leadership, location —
at $0.00188.

**It finds the people attached to the signal.** Person enrichment from $0.025, so the output is a
company, a reason to talk to them, and who to talk to.

> **Read this before you build on it.** Coverage differs sharply by provider and by company size — see the
> funding results in the evidence below, where two small recently-funded startups returned nothing and
> Stripe returned a full history. Test coverage on your own list first; the misses are free.

### What comes back

```text
AI infrastructure · last 90 days · pulled [date]

COMPANY        HEADCOUNT   SIGNAL                              STRENGTH   DECISION-MAKER
<company>      140 (+22)   <round> raised <date>               strong     <name>, <title>
<company>      68 (+15)    hiring in <function>, <n> roles     medium     <name>, <title>
<company>      310 (+4)    <signal>                            weak       <name>, <title>
...

RANKED SIGNALS
 1. <signal type> — <n> companies, most recent <date>
 2. <signal type> — <n> companies
 ...

PROVIDERS USED   <provider> (search) · <provider> (funding) · <provider> (people)
COST             $[from your run]
```

*Structure is illustrative. Values come from the providers, relayed unchanged.*

**[ Copy Prompt ]**

---

## Proof — from one real run

*Run on treg.to, 17 Aug 2026. Every figure is from the Activity log of that run.*

| Field | Value |
|---|---|
| Providers considered | **11** for company search |
| Providers selected | `hunter.x.discover-companies` (free) · `scrapecreators.x.v1-linkedin-company` |
| Why | Hunter Discover is free, takes a plain-English brief and resolved it into explicit funding and headcount filters. LinkedIn company data at $0.00188 was the cheapest way to add headcount and leadership |
| Total cost of the run | **$0.10188** — discovery free, one enrichment, one funding lookup (two further funding lookups missed and cost nothing) |
| Subscription cost avoided | **$398/mo** at list — Crunchbase $99 + Diffbot $299 |
| Time to completion | Under 5 seconds |
| Data freshness | Live at call time |
| Companies returned | **9**, filtered to recently funded AI infrastructure at 20–200 employees |
| Cost per company researched | **$0.00** for discovery; $0.00188 per company enriched |

**What the free call returned.** Nine companies with domains and contactable-address counts — Daloopa,
ZincFive, Ethernovia, RunPod, AttoTude, Netris, Bobyard, Normal Computing, Arycs Technologies. Hunter
translated the brief into filters and showed its working: headcount `20-50` and `51-200`, funding series
pre-seed through series C+.

> **The honest read of this run, and it is the most useful thing in it.** The LinkedIn enrichment returned
> **the wrong company**. Asked for `linkedin.com/company/runpod`, it correctly returned a 2-person retail
> partnership in Sligo, Ireland — because that is what lives at that URL. The AI infrastructure company is
> at `/company/runpod-io`. The provider answered exactly what was asked, and a confident wrong answer cost
> $0.00188.
>
> This is the catalog's first selection rule in practice: **match the inputs you actually hold**, ahead of
> price. It is also why treg.to relays your request rather than rewriting it — a system that silently
> "corrected" that URL would have guessed, and guessed inside your research.

**The funding leg, run separately.** LeadMagic's funding endpoint was tried on three companies:

| Company | Result | Charged |
|---|---|---|
| runpod.io | no funding data | **$0.00** |
| daloopa.com | no funding data | **$0.00** |
| stripe.com | full history — $9.8B total raised, revenue, last round, named investors | $0.10 |

> **This is the finding that should change how you use this page.** The endpoint works, and works well —
> Stripe came back with founding year, headquarters, revenue, total funding, the most recent round and the
> investor list. But **it found nothing for either small recently-funded startup**, which is precisely the
> segment the example prompt targets. Coverage is strongest where public reporting is strongest.
>
> The cost structure absorbs this: both misses were free, so testing coverage on your own list costs
> nothing until it works. Test before you build a workflow on it.

**What this page does not cover.** Activity and hiring signals are a different job from the one above,
and this workflow does not do them — everything shown here is company search, funding, headcount and
leadership. If your work depends on hiring or intent signals, check the catalog for what serves that
capability before you build on it.

---

## Three things you can do the day you sign up

**Test whether your market is the size you think it is.**
Company search starts free. Before anyone signs a data contract, run the filters that define your ICP and
count what comes back.

**Build a research brief on a company in one prompt.**
Funding history, headcount, leadership and the LinkedIn page, pulled together — for well
under a cent when the cheap providers cover it.

**Watch a list for changes rather than re-researching it.**
Run the same company set monthly and have the agent report only what moved: new funding, headcount jumps,
new leadership. A quiet month costs almost nothing.

---

## Who this is for

- **Founders** sizing a market or a partner list without buying a database to find out.
- **Investors** tracking a sector's funding and hiring without a per-seat data platform.
- **Sales teams** who want a reason to reach out attached to every account, not just a name.
- **Market researchers and developers building research agents** who want one surface across eleven
  company data providers and one bill.

---

## Before you sign up

**Why not just call the providers directly?**
Because the answer to "which company data provider should I buy" is genuinely unknown until you test it
against your own market — coverage differs far more than price does, and the price differs by 200×. Buying
one to find out is the expensive way. Testing all eleven for a few cents is not. If you already pay for
one, connect it and those calls route through your key, unmetered. treg.to is closer to OpenRouter for
agent tools than to a data vendor: one base URL, one token, many providers behind it.

**How are credentials handled?** — `S-OBJ-CREDENTIALS`

**Can I choose a specific provider?** — `S-OBJ-CHOOSE`
*(Vertical note: the point of this page. Test broadly first, then pin the one that covered your market, so
the whole team's numbers stay consistent.)*

**Can I use my existing provider key?** — `S-OBJ-OWN-KEY`
*(Crunchbase is own-key only here — if your team has a licence, connect it and those calls cost nothing on
treg.to.)*

**What happens if a provider fails?** — `S-OBJ-FAILURE`

**How much does a call cost?**
Company search is free with three of the eleven providers and $0.38 a record at the top end. Funding
history is $0.10 a company; person enrichment from $0.025. The exact price is
shown before the call, and treg.to adds no markup. New teams start with $1.00 of free credit.

**Which agents does it work with?** — `S-OBJ-AGENTS`

---

## Final section

### Research a market this afternoon, without buying a database

**[ Run This Workflow Free ]**

`S-FINAL-CTA-TRUST`

---
---

# Ad and creator kit

### Responsive search ad headlines
| Headline | Chars |
|---|---|
| `Company Data, Per Record` | 24 |
| `11 Providers, One Key` | 21 |
| `Funding + Hiring Signals` | 24 |

### Search ad descriptions
| Description | Chars |
|---|---|
| `Funding, hiring, headcount and leadership through one key. From free to $0.38 a record.` | 87 |
| `Compare 11 company data providers before you buy one. $1.00 free, no subscription.` | 82 |

### Creator video hooks
1. "Eleven providers sell company data. Three of them are free. Here's the price table nobody publishes."
2. "Before you pay $99 a month for company data, test whether it covers your market for eight cents."
3. "I asked my agent which AI companies raised or hired this quarter, and who to talk to."

### X post hook
`"Search companies by size and funding" is one job with 11 providers behind it — free, $0.0019, $0.026, $0.38 a record. Same job, 200x spread. Most people buy the first one they hear of.`

### High-intent keyword phrases
`company data api pay per record` · `funding data api` · `firmographic api for developers` ·
`crunchbase api alternative pricing`

### Negative keywords
`free company lookup` · `companies house` · `register a company` · `jobs` · `stock` · `ticker` ·
`annual report` · `credit score` · `whois` · `business plan`

### Demonstration a creator can reproduce
Run `treg catalog get apollo.companies.search` on camera and let the sibling table render — eleven
providers, prices, success rates, sample sizes. That single screen is the most persuasive thing treg.to
has, and no competitor can film it.

### Measurable hypothesis
This vertical sits on the catalog's strongest and best-measured data, and company research is recurring
work rather than a one-off. Predict the **highest week-2 repeat-call rate** of the five. Repeat calls
matter more than first calls here: a page that produces one call and silence is worse than a page with
half the signups and a second call in week two.

---

## Numbers used on this page

`F-01` `F-02` `F-03` `F-04` `F-05` `F-06` `F-07` `F-08` `F-09` `F-10` `F-11` `F-13` `F-21` `F-42` `F-50`
`F-51` `F-80` — defined in `_facts.md`, verified 2026-08-17.
