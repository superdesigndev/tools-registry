# Measurement — how these five pages answer the actual question

The question behind this whole exercise is **which vertical deserves the effort**. Five pages plus paid
traffic is a good way to answer it. It only works if the thing being counted is the right thing.

---

## The conversion is the first successful call, not the signup

An account is free and costs a visitor nothing to create, so signups measure curiosity. The funnel that
decides whether a vertical is real is:

```
page view → Copy Prompt → account created → agent installed → FIRST SUCCESSFUL CALL → a second call in week 2
```

**None of the steps after "account created" are currently instrumented.** This is the standing blocker in
`wiki/topics/treg.to SEO.md` §8–9, and it is the reason the wiki's position has been *no paid spend yet*:
spend against an uninstrumented funnel returns a verdict on the wrong metric. A vertical whose visitors
sign up and never call looks identical to a vertical whose visitors sign up and call daily.

**This does not mean don't run ads. It means instrument first-call attribution before or alongside the
first dollar.** Minimum viable version, in rough order of cost:

1. A `?utm_source/medium/campaign/content` scheme where `utm_content` carries the page id (`p1`…`p5`),
   persisted to the account record at signup.
2. A `first_call_at` timestamp on the team, and the endpoint id of that first call.
3. A weekly join: page → signups → first calls → teams with a call in days 8–14.

Until (2) exists, every number below is a leading indicator, not a verdict.

---

## Two clocks, and the pages are on the slow one

The day-7 gate that makes cheap keyword bets affordable is the **wrong clock for these pages**. It is
built for emerging-term content bets; an evergreen utility or commercial page earns nothing for months and
then compounds. Pointing the day-7 gate at this cluster would kill it just before it started working.

| Channel | Clock | Verdict on |
|---|---|---|
| **Google Ads** | day 7 and day 14 | cost per first successful call, by page |
| **Organic** | day 14/30 (indexation, qualified impressions), then day 60/120 | assisted signups and first calls |

Score the **daily series**, not a trailing average — a 7-day mean printed "scale, confirmed" on another
property while the daily numbers fell for seven consecutive days. And end every window **3 days back**;
Search Console lags 2–3 days and ending on yesterday silently ends every window on preliminary data.

---

## Per-page hypotheses

Each page states one measurable hypothesis in its front-matter. Collected here so they can be scored
together.

| Page | Hypothesis | Scored on |
|---|---|---|
| 01 SEO | Highest volume, lowest intent — SEO people already own a tool. Predict the **most clicks and the worst cost per first call** of the five. | CPFC vs. the other four at day 14 |
| 02 Lead enrichment | Highest commercial intent. Predict the **lowest cost per first successful call**. | CPFC, day 14 |
| 03 Social trends | Widest audience, weakest developer overlap. Predict high Copy Prompt rate and low install-completion. | Copy Prompt → account, then account → first call |
| 04 Competitor ads | Narrowest audience, sharpest pain. Predict the **highest Copy-Prompt-to-first-call rate**, on low volume. | Copy Prompt → first call |
| 05 Company signals | Closest to the catalog's strongest data (11 providers, 200× spread). Predict the **highest week-2 repeat-call rate**. | teams calling again in days 8–14 |

### What the 30-day telemetry already settled (2026-08-17)

Production usage answers part of this before a dollar is spent. It measures **existing** users, not the
ones these pages are meant to acquire, so it constrains rather than decides — but it is real demand and it
moves two of the five.

- **p4's narrowness is confirmed.** The Meta ad library is 303 calls, **1.2% of all traffic** — the
  smallest job of the five by a wide margin. The "sharp pain" half of the hypothesis is still open. Fund
  it last, and only if Copy-Prompt-to-first-call clears the others by enough to justify the CPC.
- **p1 was aimed at the wrong slice and has been re-pointed.** Keyword research is ~1.3% of usage and no
  keyword endpoint appears in the top 20; **scraping result pages is ~22%**. The page now leads its second
  workflow with recurring SERP monitoring. Watch whether the two prompts split the Copy-Prompt event — if
  the SERP one wins decisively, the hero should follow it.
- **p3's premise strengthened.** X alone is 17.5% of traffic and its search endpoint is the single
  most-called tool on the platform. The page was written as a four-platform sweep; if the data holds, an
  X-specific page is the better ad destination and this one becomes the organic hub.

**The limit on all three.** The telemetry cannot say how many teams produced it — 24,921 calls could be
forty teams or four. Do not treat a large share as a large market until the team count is known
(`TREG_ADMIN_TOKEN`, one `/admin/*` call). A 17.5% share driven by one integration is a different fact
from the same share across thirty customers.

Two of these are deliberately predictions of *failure*. A test where every arm is expected to win teaches
nothing, and the cheapest outcome here is finding out early which two verticals to stop paying for.

**The cross-page hypothesis:** Copy Prompt clicks predict first-call conversion better than account
creations do. If that holds, Copy Prompt becomes the optimization target for the ad accounts and the
primary on-page event, which is a cheaper signal to collect than the full funnel.

---

## What each page must emit as events

Same names on all five, so the pages are comparable:

| Event | Fires on |
|---|---|
| `lp_view` | page load, with `page_id` |
| `lp_copy_prompt` | any Copy Prompt affordance, with `page_id` **and `prompt`** — the id of the block copied (`#prompt`, `#prompt-2`). p1 carries two workflows and which one people take is the question |
| `lp_cta_primary` | Run This Workflow Free |
| `lp_cta_secondary` | See the Example |
| `lp_scroll_proof` | proof section enters the viewport (does the evidence get read?) |
| `lp_objection_open` | an objection expands, with which one — **this is the highest-value diagnostic on the page**; the objection people open most is the one the hero failed to answer |

---

## Verdict dates

Set on ship. Each page's front-matter carries `verify_after`. A page with a passed `verify_after` and no
recorded verdict is an open bet nobody scored, which is the failure mode that made the scorecard loop a
separate role from the engine loop in the first place.
