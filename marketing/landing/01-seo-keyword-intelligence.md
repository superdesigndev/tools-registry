---
page_id: p1
slug: /use-cases/seo-data-for-ai-agents/
seo_title: "Real SEO Data for AI Agents — One Key, Per Call | treg.to"        # 57 chars
meta_description: "Give your agent live Google results, keyword volume and backlink data through one key. Pay per call, not per seat. $1.00 free to start."  # 137
h1: "Give Your Agent Real SEO Data — Without a Semrush Subscription"
hub_title: "SEO & search results"
hub_blurb: "Live Google results, keyword volume, difficulty and backlinks — priced per call."
price_old: "$214/mo"
price_old_label: "Semrush + SerpApi, at list"
price_new: "$0.012"
price_new_label: "what our run actually cost"
seo_terms:
  primary: "seo data for ai agents"
  secondary:
    - "serp api for claude code"
    - "google search results api pay per call"
    - "rank tracking api without a subscription"
    - "keyword volume api pay per call"
    - "backlink data api per call"
ad_keywords:                       # bid here; do NOT optimize the page for these
  - "semrush api alternative"
  - "cheapest serp api"
  - "keyword research api"
  - "seo api for developers"
  - "dataforseo alternative"
capabilities: [google.keywords.volume, google.keywords.ideas, google.serp.organic, web.backlinks.summary]
facts_used: [F-01, F-02, F-03, F-04, F-05, F-06, F-07, F-08, F-09, F-10, F-11, F-13, F-20, F-30, F-31, F-32, F-90, F-92]
hypothesis: "Highest traffic, worst cost per first successful call — SEO buyers already own a tool. Predict this page is the one we stop paying for. RE-POINTED 2026-08-17: telemetry shows keyword research is ~1.3% of real usage while SERP scraping is ~22%, so the second workflow now leads on recurring result monitoring."
verify_after: 2026-08-31
status: proof populated from a real run 2026-08-17 ($0.012) · ready for build · revised against 30-day telemetry 2026-08-17
---

# Page 1 — SEO and keyword intelligence

---

## Hero

### Give Your Agent Real SEO Data — Without a Semrush Subscription

Your coding agent can pull live Google results, keyword volume, difficulty and backlink data through one
key, and you pay for the calls it makes instead of a seat you use twice a month.

**[ Run This Workflow Free ]**   **[ See the Example ]**

`S-TRUST-HERO`

*Sub-line:* Live results — organic, news, maps — plus keyword data, backlinks, Search Console and GA4, all
answering the same token. The organic results endpoint has served **2,075 calls at a 100% success rate**.

---

## The old way vs. the treg.to way

| | The old way | With treg |
|---|---|---|
| **What you pay for** | Semrush $139/mo + SerpApi $75/mo + Moz $99/mo, running whether the agent does or not | One prepaid balance. A keyword lookup is a fraction of a cent |
| **Keys** | One account, one login and one API key per provider, spread across machines and `.env` files | One treg token. Every tool in the catalog answers to it |
| **Picking a provider** | You guess, or you use the one you already pay for | `catalog get` lists every provider for that job with price, measured success rate, median speed and when it last answered |
| **Commitment** | Annual plans and seat minimums to answer one research question | No subscription. Stop calling and you stop paying |
| **The workflow** | Export a CSV from the keyword tool, another from the rank tracker, join them by hand | One agent run: keywords, difficulty, live results and competitors in a single pass |

---

## A real workflow

### Copy this into Claude Code, Cursor, Codex or opencode

```text
Research the market for [product]. Find 50 relevant keywords with real search
volume, difficulty and search intent. Identify the 10 best opportunities and
show which competitors currently rank.
```

**[ Copy Prompt ]**

### What happens when you run it

**The agent searches by the job, not the vendor.** `catalog_search "keyword search volume"` returns the
endpoints that do it, each with a price and whether treg.to can serve it without a key of yours.

**It compares, then picks.** Five providers answer this one capability, and the spread is not small:

| Provider | Cost | Success rate | Median | Notes |
|---|---|---|---|---|
| **Serpstat** | $0.0005 per keyword returned | 100% (77 calls) | 1.2 s | up to 1,000 keywords per call |
| **SE Ranking** | $0.00179 per keyword returned | 92.3% (20 calls) | 1.7 s | up to 5,000 per call |
| **DataForSEO** | $0.09 flat per call | 98.8% (165 calls) | 3.9 s | same price for 1 keyword or 1,000 — batch it |
| **Google Ads** | free with a connected account | not yet measured | — | your own OAuth |
| **Semrush** | 10 API units per row | not yet measured | — | own key only |

Those numbers are what treg.to has actually observed across real calls, sample size included — a 100%
over 8 calls is weaker evidence than a 99% over 121, and the catalog shows you both so you can tell.

**It calls, then joins the results.** Volume and difficulty for the list, live Google results for the
shortlist, and authority metrics for whoever ranks (SE Ranking $0.002685, Moz $0.006667). One report,
one bill.

The results endpoints are the most-used tools in the whole catalog, so their records are the longest:

| Result type | Provider | Cost | Success rate | Median |
|---|---|---|---|---|
| **Organic** | DataForSEO | $0.002 | **100% (2,075 calls)** | 5.5 s |
| | ScrapeCreators | $0.00188 | 100% (343 calls) | 1.6 s |
| | Serpstat | $0.0005 | 100% (77 calls) | 1.2 s |
| | SerpApi | $0.015 | — | — |
| **Maps / local** | DataForSEO | $0.002 | **100% (2,646 calls)** | 1.9 s |
| **News** | DataForSEO | $0.002 | 100% (690 calls) | 9.0 s |
| **Trends** | DataForSEO | — | 98% (462 calls) | 6.7 s |

### What comes back

```text
50 keywords · US · Google · pulled [date]

KEYWORD                          VOL/MO   KD   INTENT         TOP 3 RANKING
<keyword>                         8,100    61  commercial     <domain> · <domain> · <domain>
<keyword>                         2,900    48  commercial     <domain> · <domain> · <domain>
<keyword>                         1,300    34  informational  <domain> · <domain> · <domain>
...

THE 10 OPPORTUNITIES   (volume weighted by difficulty and intent)
 1. <keyword>   1,900/mo · KD 22 · commercial · weakest ranker DA 31
 2. <keyword>     880/mo · KD 18 · commercial · nothing targets this exactly
 ...

PROVIDERS USED   serpstat (volume + difficulty) · dataforseo (results)
COST             $[from your run]
```

*Structure is illustrative. The values are the providers' own, relayed unchanged.*

**[ Copy Prompt ]**

### The one teams actually run most

Keyword research is a job you do once. Watching results is a job you do every week, and it is what the
agents on treg.to overwhelmingly spend their calls on — the organic, maps and news result endpoints
together serve more traffic than every other SEO tool in the catalog combined.

```text
Every Monday, check where we and our top 3 competitors rank for these 20
keywords — organic, news and maps. Report only what moved since last week,
and for anything that dropped, show which page overtook us.
```

**[ Copy Prompt ]**

Twenty keywords across three result types is 60 calls, about **$0.12** at DataForSEO's $0.002. Run weekly
for a year and it costs roughly $6 — against a rank tracker at $65/mo. And the week you skip it, it costs
nothing, which is the part a subscription can never do.

---

## Proof — from one real run

*Run on treg.to, 17 Aug 2026. Every figure below is from the Activity log of that run.*

| Field | Value |
|---|---|
| Providers considered | **5** for keyword volume, **5** for Google results |
| Providers selected | `serpstat.google.keywords.volume` · `dataforseo.google.serp.organic` |
| Why | Serpstat was the cheapest per keyword ($0.0005) **and** carried the best measured record of the five — 100% success over 77 calls, 1.2 s median. DataForSEO took the results call at $0.002 |
| Total cost of the run | **$0.012** — 2 calls |
| Subscription cost avoided | **$214/mo** at list — Semrush $139 + SerpApi $75 |
| Time to completion | Under 15 seconds. The results call reported 10.4 s server-side |
| Data freshness | Google results timestamped **2026-08-17 02:06:40 UTC** — live at call time. Keyword volume is the provider's monthly index |

**What actually came back.** 20 keywords submitted, **13 returned with data**; seven had none, which is a
real answer and worth more than an invented number.

| Keyword | Volume/mo | Difficulty | Intent |
|---|---|---|---|
| mcp server | 60,500 | 8 | informational |
| serp api | 12,100 | 17 | informational |
| agent skills | 8,100 | 5 | informational |
| reddit api | 5,400 | 9.65 | informational |
| tiktok api | 2,400 | 9 | informational |
| web scraping api | 1,900 | 23 | navigational |
| rank tracking api | 1,000 | 6 | informational |
| social media api | 320 | 8 | informational |
| backlink api | 210 | 3 | informational |
| keyword research api | 140 | 3 | informational |
| data enrichment api | 110 | 2 | informational |
| company data api | 70 | 5 | navigational |
| email finder api | 40 | 1 | informational |

And the live top 7 for `serp api`: serpapi.com, dataforseo.com, scrapfly.io, github.com, brightdata.com,
searchapi.io, you.com.

> **The honest read of this run:** `mcp server` at 60,500/mo with difficulty 8 is the kind of finding the
> workflow exists to surface — but difficulty scores are a provider's model, not a fact. Treat the volume
> as data and the difficulty as an opinion you paid $0.0005 for.

---

## Three things you can do the day you sign up

**Price a content plan before you commit writers to it.**
Hand the agent 200 candidate titles. It returns real monthly volume, difficulty and intent for each, and
flags where the current top three are weaker than you are. At $0.0005 a keyword that is a few cents, not a
month of Semrush.

**Find out who actually earned the ranking.**
For any keyword, the agent pulls the live results, then pulls authority and referring-domain data for each
page that ranks. You learn whether the leader is winning on links or on the page — which decides whether
you write or go get links.

**Watch a competitor's index without buying a rank tracker.**
Run the same keyword set weekly. The agent keeps last week's positions and reports only what moved. You
pay per run, so a paused project costs nothing while it is paused.

---

## Who this is for

- **In-house SEOs** running more research than one seat can justify, who want the data inside the agent
  that is already writing the brief.
- **Content marketers** who need volume and intent for a plan this week and cannot get a tool approved by
  then.
- **Growth teams** doing keyword work in bursts — a launch, a new market — where a monthly subscription
  sits idle between them.
- **Developers building SEO agents or dashboards** who want one HTTP surface and one bill instead of five
  vendor SDKs and five renewal dates.

---

## Before you sign up

**Why not just call the providers directly?**
For one provider you already pay for, do — connect that key and those calls route through it, unmetered.
The arithmetic changes at the second provider. This workflow touches keyword data, live results and
backlinks; buying that separately is three accounts, three contracts and three API shapes to learn, for
research you might run twice a month. treg.to is closer to OpenRouter for agent tools than to a data
vendor: one base URL, one token, many providers behind it.

**How are credentials handled?** — `S-OBJ-CREDENTIALS`

**Can I choose a specific provider?** — `S-OBJ-CHOOSE`
*(Vertical note: worth doing here. Keyword databases disagree, so if you have been reporting Semrush
volumes to a client for a year, pin Semrush and keep the series consistent.)*

**Can I use my existing provider key?** — `S-OBJ-OWN-KEY`

**What happens if a provider fails?** — `S-OBJ-FAILURE`

**How much does a call cost?**
Fractions of a cent for most SEO endpoints — Serpstat keyword volume at $0.0005 per keyword, DataForSEO
organic results at $0.002 per call, Moz URL metrics at $0.006667, at the time of writing. The exact price
is shown before the call, and treg.to adds no markup to what the provider charges. New teams start with
$1.00 of free credit.

**Which agents does it work with?** — `S-OBJ-AGENTS`

---

## Final section

### Your next keyword plan can be built by the agent that is already open

**[ Run This Workflow Free ]**

`S-FINAL-CTA-TRUST`

---
---

# Ad and creator kit

### Responsive search ad headlines
| Headline | Chars |
|---|---|
| `Real SEO Data for Agents` | 24 |
| `Keyword Volume, Per Call` | 24 |
| `No Semrush Seat Required` | 24 |

### Search ad descriptions
| Description | Chars |
|---|---|
| `Keyword volume, difficulty, results and backlinks through one key. $1.00 free to start.` | 87 |
| `Pay per call, not per seat. Price shown before your agent calls. No provider signup.` | 84 |

### Creator video hooks
1. "I asked Claude Code for 50 keywords with real search volume. Watch what the run cost."
2. "Five providers sell the same keyword data. One charges 180 times what another does."
3. "Your agent can't do SEO because it can't see the data. Two-minute fix."

### X post hook
`Your agent writes confident SEO briefs off keyword data it invented. Here's how to give it the real numbers — volume, difficulty, live SERPs — for a fraction of a cent per call.`

### High-intent keyword phrases
`keyword volume api pay per call` · `serp api for claude code` · `seo data for ai agents` ·
`keyword research api without subscription` · `backlink api per call`

### Negative keywords
`free` · `jobs` · `salary` · `course` · `certification` · `wordpress plugin` · `chrome extension` ·
`agency near me` · `meaning` · `what is seo`

### Demonstration a creator can reproduce
Paste `set up treg — https://treg.to/llms.txt` into Claude Code, then paste the workflow prompt. Film the
terminal end to end, and run `treg balance` before and after so the audience sees the actual cost of the
run rather than a claim about it.

### Measurable hypothesis
Highest traffic of the five, worst cost per first successful call — SEO buyers usually already own a tool,
so the pitch is a saving rather than a new capability. If day-14 CPFC is the worst of the five, stop
paying for this page and move the budget to page 2 or 4. Secondary: Copy Prompt clicks predict first-call
conversion better than account creations do.

---

## Numbers used on this page

`F-01` `F-02` `F-03` `F-04` `F-05` `F-06` `F-07` `F-08` `F-09` `F-10` `F-11` `F-13` `F-20` `F-30` `F-31`
`F-32` — all defined in `_facts.md`, verified 2026-08-17. Re-verify with
`treg catalog get <endpoint_id>` before publishing.
