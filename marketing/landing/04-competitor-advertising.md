---
page_id: p4
slug: /use-cases/competitor-ad-research-for-ai-agents/
seo_title: "Competitor Ad Research for AI Agents — Per Call | treg.to"        # 57
meta_description: "Let your agent pull competitors' live ads from the Meta, Google, TikTok and LinkedIn ad libraries through one key. Calls from $0.00188. $1.00 free."  # 147
h1: "See What Your Competitors Are Advertising"
hub_title: "Competitor ads"
hub_blurb: "Competitors' live ads out of the Meta, Google, TikTok and LinkedIn ad libraries."
price_old: "$447/mo"
price_old_label: "Optmyzr + Revealbot + Adalysis"
price_new: "$0.0075"
price_new_label: "what our run actually cost"
seo_terms:
  primary: "competitor ad research api"
  secondary:
    - "meta ad library api pay per call"
    - "google ads transparency api"
    - "ad creative research for agents"
    - "tiktok ad library api"
ad_keywords:
  - "ad spy tool"
  - "meta ad library api"
  - "competitor ad research"
  - "facebook ads scraper api"
  - "google ads transparency center api"
capabilities: [meta-ads.library.search, google.ads.transparency, tiktok-ads.library.search, linkedin.ads.search]
facts_used: [F-01, F-02, F-03, F-04, F-05, F-06, F-07, F-08, F-09, F-10, F-11, F-13, F-23, F-60, F-80, F-91]
hypothesis: "Narrowest audience, sharpest pain. Predict the highest Copy-Prompt-to-first-call rate of the five, on the lowest volume. NARROWNESS NOW CONFIRMED by telemetry — the Meta ad library is 303 calls / 1.2% of all traffic, the smallest job of the five. Spend last, and only if the Copy-Prompt-to-first-call rate justifies the CPC."
verify_after: 2026-08-31
status: proof populated from real runs 2026-08-17 ($0.00752) · 4 of 4 platforms · ready for build · revised against 30-day telemetry 2026-08-17
---

# Page 4 — Competitor advertising

---

## Hero

### See What Your Competitors Are Advertising

Name five competitors and your agent pulls their live ads out of the Meta, Google, TikTok and LinkedIn ad
libraries, then groups them by offer, hook and format — for a fraction of a cent per call.

**[ Run This Workflow Free ]**   **[ See the Example ]**

`S-TRUST-HERO`

*Sub-line:* Ten ways into the four major ad libraries, from $0.0006 a call. The agent picks; you see the
price first.

---

## The old way vs. the treg.to way

| | The old way | With treg |
|---|---|---|
| **What you pay for** | An ad-spy subscription, or a media-buying suite at $99–$249/mo, for research you do before each launch | One prepaid balance. A Meta ad library search is $0.00188 |
| **Keys** | Meta's Ad Library API needs identity verification; the rest have no official access at all | One treg token. Every tool in the catalog answers to it |
| **Picking a provider** | Whichever spy tool you subscribed to, whatever it happens to cover | `catalog get` lists every provider for that library with price, measured success rate and median speed |
| **Commitment** | A monthly tool for research that happens in bursts around launches | No subscription. Research a launch, stop paying until the next one |
| **The workflow** | Scroll four ad libraries by hand, screenshot into a slide deck, lose it before the next launch | One agent run across four platforms, grouped by offer and hook |

---

## A real workflow

### Copy this into Claude Code, Cursor, Codex or opencode

```text
Find active advertisements from these five competitors across Meta, Google,
TikTok and LinkedIn. Group them by offer, hook and creative format, then
identify the three most common strategies.
```

**[ Copy Prompt ]**

### What happens when you run it

**The agent picks a way into each library.** Ten endpoints cover the four platforms, at prices that are
not close to each other:

| Library | Provider | Cost | Success rate | Median |
|---|---|---|---|---|
| **Meta** (Facebook + Instagram) | ScrapeCreators | $0.00188 / call | 100% (166 calls) | 3.1 s |
| | Apify | $0.005 / ad returned | 100% (16 calls) | 12.9 s |
| | Meta, own key | free, after identity verification | not yet measured | — |
| **Google** Ads Transparency | DataForSEO | $0.0006 (async batch) | not yet measured | — |
| | ScrapeCreators | $0.00188 | not yet measured | — |
| | SerpApi | $0.015 | not yet measured | — |
| **TikTok** ad library | ScrapeCreators | $0.00188 | 100% (16 calls) | 3.5 s |
| | Apify | $0.003 | not yet measured | — |
| | tikhub | $0.001 | 7 calls, none recorded successful | — |
| **LinkedIn** ads | ScrapeCreators | $0.00188 | not yet measured | — |
| | tikhub | $0.004 | not yet measured | — |

The blank cells are honest: those endpoints are catalogued and priced but have not been called often
enough for treg.to to have measured them. The catalog shows you that rather than a rounded number with no
sample behind it.

**It pulls the creative, not just the count.** Ad copy, headlines, calls to action, format, the landing
URL and how long each ad has been running.

**It groups and reports.** By offer, by hook, by format — and how long each cluster has been live,
which is the closest public signal to what is working.

> **Read this before you build on it.** These are public ad libraries, so coverage is whatever each
> platform chooses to publish, and it differs by platform and country. Meta's library drops commercial ads
> some time after they stop running, so it is a live-ish view, not an archive. Spend figures are published
> only for political and issue ads.

### What comes back

```text
5 competitors · 4 platforms · active ads · pulled [date]

BY OFFER
  free trial          31 ads   <competitor>, <competitor>, <competitor>
  demo request        18 ads   <competitor>, <competitor>
  discount            9 ads    <competitor>

BY HOOK
  problem-first       27 ads   longest-running: 94 days
  social proof        22 ads   longest-running: 61 days
  ...

BY FORMAT           video 44 · image 31 · carousel 12

THE THREE STRATEGIES
 1. <strategy> — <n> ads, running longest, used by <n> of 5 competitors
 2. <strategy> — ...
 3. <strategy> — ...

COST   $[from your run]
```

*Structure is illustrative. Ad content comes from the platforms' own public libraries, relayed unchanged.*

**[ Copy Prompt ]**

---

## Proof — from one real run

*Run on treg.to, 17 Aug 2026, against the SEO-software category. Every figure is from the Activity log.*

| Field | Value |
|---|---|
| Providers considered | 3 for Meta, 3 for Google Ads Transparency, 2 each for LinkedIn and TikTok |
| Providers selected | `scrapecreators` on all four libraries |
| Why | Cheapest per call at $0.00188, and the only Meta option with a real measured record — **166 calls, 100% success, 3.1 s median**. Apify's Meta endpoint charges per ad returned ($0.005 each), which is the wrong shape for a broad sweep |
| Total cost of the run | **$0.00752** — four calls |
| Subscription cost avoided | **$447/mo** at list — Optmyzr $249 + Revealbot $99 + Adalysis $99 |
| Time to completion | Around 10 seconds across all four |
| Data freshness | Live at call time; ads carried their own delivery start and end dates |
| Platforms covered | **4 of 4** — Meta, Google, LinkedIn, TikTok |
| Ads retrieved | 914 Meta matches (29 returned in full) · 2,107 LinkedIn ads (24 returned in full) · ~29,500 Google ads estimated across 3 advertiser entities |
| Cost per 100 ads | roughly **$0.01** at the page sizes returned |

**What came back per platform.** Meta returned full creative — body copy, CTA text, display format, landing
domain, page name and delivery dates. LinkedIn returned the whole post text, the person who posted it and
the company promoting it. Google Ads Transparency returned something the other two cannot: **the same
advertiser split across three registered entities** — "Semrush INC" (~20,000 ads), "Semrush Inc" (~9,000)
and "Semrush Inc." (~500). Search one name and you see a third of the real activity.

**Two calls in this run failed and cost nothing.** One sent the wrong parameter name and was rejected by
the provider (HTTP 400, `$0.00`). One omitted a required parameter and was refused by treg.to *before it
reached the provider at all*. Neither was billed.

**TikTok took two providers to get right, and that is the point.** The first TikTok ad-library endpoint
tried returned a 405 and was not billed. The second — ScrapeCreators at $0.00188, with a measured 100%
over 16 calls — returned Semrush's running TikTok ads with video URLs, first and last shown dates and
estimated audience bands. That is the objection section playing out live: treg.to does not fail over for
you, it shows you the alternatives and their measured records, and you switch.

> **The honest read of this run:** all four libraries returned data, but they do not return the *same*
> data. Meta gives you creative and delivery dates; Google gives you advertiser-level ad counts; LinkedIn
> gives you full post copy; TikTok gives you video files and audience bands but the `spent` field came back
> empty. Do not promise a clean four-platform comparison table — promise four libraries in one run, which
> is what actually happened.

---

## Three things you can do the day you sign up

**Brief a creative round from what is actually running.**
Pull every live ad from five competitors and group them by hook. What has been running for ninety days is
a stronger brief than what a team guessed in a workshop.

**Check a positioning claim before you commit to it.**
Search the ad libraries for the phrasing you are about to build a campaign on. If four competitors are
already running it, you have learned that for less than a cent.

**Watch a launch as it happens.**
Run the same competitor pull weekly. New ads appearing, and old ones stopping, is the earliest public
signal that someone changed strategy. A quiet week costs nothing.

---

## Who this is for

- **Performance marketers** who research creative before each launch and cannot justify a year-round spy
  tool for it.
- **Media buyers** managing several accounts, who need the same sweep across four platforms without four
  logins.
- **Creative strategists** who want ad copy and format grouped by pattern, not a folder of screenshots.
- **Founders** doing their own ads, who want to see what the category is running before spending anything.

---

## Before you sign up

**Why not just call the providers directly?**
Meta's Ad Library API requires identity verification and returns only what you ask for in a fields
parameter; the other three platforms have no comparable official developer access. So "directly" in
practice means one verified Meta app plus three manual browser workflows — for research that happens in
bursts. treg.to is closer to OpenRouter for agent tools than to a data vendor: one base URL, one token,
many providers behind it.

**How are credentials handled?** — `S-OBJ-CREDENTIALS`

**Can I choose a specific provider?** — `S-OBJ-CHOOSE`
*(Vertical note: matters here. Providers into the same library return different fields — one gives you EU
reach and per-ad detail, another gives you a cheap count. Read the endpoint before you pin one.)*

**Can I use my existing provider key?** — `S-OBJ-OWN-KEY`
*(If you have a verified Meta app, connect it: Meta's own Ad Library API costs nothing per call, and those
calls will not touch your balance.)*

**What happens if a provider fails?** — `S-OBJ-FAILURE`

**How much does a call cost?**
A Meta ad library search is $0.00188 with ScrapeCreators, or $0.005 per ad returned with Apify. Google Ads
Transparency ranges from $0.0006 to $0.015 depending on provider. The exact price is shown before the
call, and treg.to adds no markup. New teams start with $1.00 of free credit — enough for several hundred
library searches.

**Which agents does it work with?** — `S-OBJ-AGENTS`

---

## Final section

### Find out what the category is running, before your next launch

**[ Run This Workflow Free ]**

`S-FINAL-CTA-TRUST`

---
---

# Ad and creator kit

### Responsive search ad headlines
| Headline | Chars |
|---|---|
| `See Competitor Ads Live` | 23 |
| `Ad Library Data Per Call` | 24 |
| `No Ad Spy Subscription` | 22 |

### Search ad descriptions
| Description | Chars |
|---|---|
| `Pull competitors' live ads across four platforms. One key, from $0.00188 a call.` | 80 |
| `Group ads by offer, hook and format in one agent run. $1.00 free, no subscription.` | 82 |

### Creator video hooks
1. "I pulled every ad five competitors are running, across four platforms, for under a dollar."
2. "Ad spy tools cost $99 a month. Here's the same research as one prompt."
3. "The ad that's been running 94 days is the brief. Here's how to find it in a minute."

### X post hook
`Ad spy tools charge monthly for research you do four times a year. Here's the same sweep — Meta, Google, TikTok, LinkedIn ad libraries — as one agent prompt, priced per call.`

### High-intent keyword phrases
`meta ad library api` · `google ads transparency api` · `competitor ad research api` ·
`tiktok ad library api access` · `ad creative research for agents`

### Negative keywords
`free ad spy` · `jobs` · `course` · `how to advertise` · `ad blocker` · `report an ad` · `ad revenue` ·
`google ads certification` · `ad manager login` · `create an ad`

### Demonstration a creator can reproduce
Pick five well-known competitors in a category the audience knows, run the prompt, and show the grouped
output next to the cost. Land on the longest-running ad — "this one has been live 94 days, that's the
one that's working" — then show the balance.

### Measurable hypothesis
Narrowest audience of the five and the sharpest pain, so predict the **highest Copy-Prompt-to-first-call
rate on the lowest traffic**. If that holds, this vertical is worth spending on even at a high CPC, and
the constraint is reach rather than conversion — which points at creator distribution rather than more
search budget.

---

## Numbers used on this page

`F-01` `F-02` `F-03` `F-04` `F-05` `F-06` `F-07` `F-08` `F-09` `F-10` `F-11` `F-13` `F-23` `F-60` `F-80`
— defined in `_facts.md`, verified 2026-08-17.
