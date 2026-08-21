# Shipping the rest: agent pages, use-case pages, workflows

Written 2026-08-21, after `/agents/chatgpt` and `/use-cases/data-enrichment-sales/find-professional-emails`
shipped. The templates exist and are tested; what remains is content, and content is the slow part.
Plan, spec and review: `pseo-catalog-plan.md`, `pseo-build-spec.md`, `_review-codex-seo-plan.md`.

## What a page costs now

| Page | Machine work | Hand-written work |
|---|---|---|
| Agent page | none, the template renders it | 1 dict entry: title, definition, 4 install steps, 4 FAQ answers, 1 screenshot. **~45 min** |
| Use-case page | none | 1 dict entry: sentence, title, lede, prompt, 3 "why it works" lines, 3 "what differs" notes, "what is X", 4 FAQ, related. **~60–90 min**, because the notes require actually reading the providers' docs |
| Workflow page | new template (below) | the workflow itself, run once end to end |

So the constraint is writing and verifying, not code. Everything below is sequenced around that.

## Wave 1 — finish the cluster that is already half-built (this week)

The Codex review's core point: ship one complete cluster, submit it, then expand. The cluster is
**data enrichment**, because it has the most providers, the clearest buyer, and its first page exists.

1. `/agents/claude` — Claude.ai's Connectors flow, screenshot of the connector dialog. Same prompts.
2. `/agents/claude-code` — `treg mcp install`, screenshot of the terminal after install.
3. `/agents/cursor` — same command, Cursor's MCP settings screenshot.
4. Four more enrichment use cases, in demand order:
   - `verify-an-email-before-you-send` (5 providers) — pairs with the finder; the obvious next click
   - `enrich-a-person-from-an-email-or-linkedin-url` (13)
   - `find-people-by-role-company-or-location` (15)
   - `enrich-a-company-from-its-domain` (19)
5. **The workflow page** for the cluster (see below).
6. Submit to the directories: mcpservers.org, PulseMCP, Glama, Smithery, Docker MCP hub, Anthropic's
   connector directory, and the ChatGPT plugin listing's own description linking `/agents/chatgpt`.

**Gate at day 14:** are all seven pages indexed, and does GSC show impressions for
`email finder api`, `linkedin email finder`, `chatgpt plugin`, `claude connectors`? If nothing is
indexed, stop and fix crawling before writing more.

## Wave 2 — the two clusters with measured search demand (weeks 2–4)

Only if wave 1 indexes. Each is one pricing/comparison page plus its three or four use cases.

- **YouTube & video.** Transcripts were the single most-asked job in the X/Reddit research (16
  posts) and people repeatedly fail to self-host it. Pages: `get-a-video-s-transcript`,
  `a-channel-s-profile-and-lifetime-stats`, `search-videos-and-channels-by-keyword`,
  `a-video-s-comments`.
- **Connect your own accounts.** The biggest demand cluster overall (~32 posts) and every row is
  free, which is the strongest hook we have. Pages: `search-console-queries`,
  `google-analytics-reports`, `google-ads-search-terms`, `google-business-reviews-and-replies`.
  These are single-provider pages, so they follow the **short form**: prompt, why it works, connect
  steps, params, one call, FAQ. No comparison section.

### Wave 2 progress

**YouTube & video shipped 2026-08-21** (5 pages, not the 4 planned: `video-details-views-and-stats`
came along because the quota argument is the same argument). Measured US volume behind the titles,
via the free own-key Google Ads endpoint: `youtube transcript` 110,000 · `get youtube transcript`
2,400 · `youtube channel stats` 1,300 · `youtube data api` 1,000 · `youtube transcript api` 880 ·
`youtube video statistics` 590 · `youtube keyword search tool` 480 · `youtube scraper` 390 ·
`youtube comment scraper` 210 · `youtube search api` 170 · `youtube mcp server` 140. Dead, do not
target: `youtube video stats api` (0), `youtube channel data api` (0), `youtube subscriber count
api` (10), `youtube video details api` (10), `youtube metadata api` (10), `youtube channel api` (30).
The pattern from the enrichment pass holds: buyers type the **thing plus a verb** ("get youtube
transcript") or **scraper**, not `<thing> api`, except where the thing is a named product
(`youtube data api`).

Two remain in the cluster for the next run: `trending-videos` and `transcripts-of-x-and-facebook-video-posts`.

**Catalog gap found, needs a human.** `tikhub.x.youtube-web-search-channel` is mapped to capability
`youtube.search.channels`, but it searches *within* one channel by id ("Search within a YouTube
channel by keyword"), which is a different job from finding channels by keyword. It renders as a row
on `/use-cases/youtube-video/search-videos-and-channels-by-keyword`, where the page's FAQ now names
the distinction rather than papering over it. The real cross-channel search is
`tikhub.x.youtube-web-v2-search-channels`. Either remap the endpoint or give within-channel search
its own capability.

## Wave 3 — the rest, ordered by demand, not by catalog size (weeks 5+)

SEO (8 jobs) · Social (9) · Local businesses (4) · Finance (7) · E-commerce (4) · Advertising (3) ·
Market research (3). Write them in the order the measured terms justify; stop adding pages to a
cluster the moment its first pages stop earning impressions.

Remaining agent pages (`opencode`, `codex`, `gemini-cli`, `openclaw`, `hermes`, `pi`) ship as one
batch **noindex** behind an `/agents` directory page, and get indexed individually only when one has
a distinct install path worth its own page. Ten near-identical pages is the thin-content risk Codex
flagged.

## The new thing: workflow pages

`/workflows/<slug>`, the "copy the whole thing" page. A use-case page answers one job; a workflow
page is the sequence a real person runs, with the prompt for each step, what it costs at the end,
and what to do with the output. This is the shape that earns links, because it is genuinely useful
on its own and cannot be regenerated from a vendor's docs.

First one: **`/workflows/find-and-verify-a-lead-list`** (data enrichment).

    Step 1  Build the list      companies.search      "50 Series-A SaaS companies in the US, 50-200 staff"
    Step 2  Find the people     people.search         "the VP of Marketing or Head of Growth at each"
    Step 3  Find the emails     people.email.find     "their work emails, cheapest verified provider"
    Step 4  Verify              people.email.verify   "drop anything not deliverable"
    Step 5  Enrich for the copy companies.news        "any funding or product news in the last 90 days"
    Output  a CSV, and the total cost printed by the agent

Every step links to its use-case page, so the workflow is the hub those spokes needed. The page
carries: the one paste-in prompt that runs the whole thing, the step table with per-step prices, a
worked example with real numbers (run it once on 50 rows, publish what it cost), the failure modes
(catch-all domains, no result for tiny companies), and the CSV of the run.

Later workflows, one per cluster: `/workflows/weekly-seo-report` (Search Console + keyword volume +
SERP + backlinks), `/workflows/find-creators-to-sponsor` (search, profile, engagement, contact),
`/workflows/watch-a-competitor` (ads library + posts + news + hiring).

**Cost:** a workflow page needs a real run, so it needs balance. The lead-list run above is roughly
50 company lookups + 50 people searches + 50 email finds + 50 verifies, call it $3–6.

## Standing rules for every page shipped

- Nothing on a page that the catalog does not produce, except the hand-written notes, which name a
  provider fact and get re-checked when `verified` changes.
- No em-dashes; the test enforces it.
- Every use-case page: the prompt first, the comparison behind "what the agent sees".
- Per-vendor observed stats are approved (Jason, 2026-08-21) with the "live traffic, not a
  controlled benchmark" caveat.
- `.md` mirror ships automatically; `llms.txt` links only `/agents/chatgpt`.
- A page is not done until its screenshot exists. A page with an empty image slot ships without the
  slot, never with a placeholder.

## What would make me stop and re-plan

- Day 14: nothing indexed → crawling problem, not a content problem.
- Day 30: indexed but zero qualified impressions across all seven wave-1 pages → the terms are wrong,
  and more pages will not fix it. Go back to keyword measurement.
- Day 30: impressions but no install starts → the pages attract readers who do not want a plugin;
  rewrite the top of the page, do not add more pages.
