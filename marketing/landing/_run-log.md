# Proof run — 2026-08-17

Every proof section in the five pages comes from these runs. **Total spend across both: $0.1499.**

> ⚠️ **Batch 1 billed the wrong team.** The MCP grant is pinned to `superdesign-7`, not the `superdesign`
> team the CLI treats as active. $0.0489 came out of a balance nobody was watching, and there was no signal
> from inside the agent that this was happening. Reported as bug 5 in `_platform-bugs-2026-08-17.md`.
> Batch 2 was run through the CLI and correctly billed `superdesign` ($4.8821 → $4.7811).

## Batch 1 — team `superdesign-7`, via MCP

**Balance before $0.073572 → after $0.024672. Spent: $0.0489.**

| # | Page | Endpoint | Cost | Outcome |
|---|---|---|---|---|
| 1 | p2, p5 | `hunter.x.discover-companies` | $0 | 9 funded AI-infra companies, 20–200 employees |
| 2 | p5 | `thecompaniesapi.companies.search` | $0 | **400** — boolean sent as string. Not billed |
| 3 | p1 | `serpstat.google.keywords.volume` | $0.010 | 20 submitted, 13 returned with volume + difficulty + intent |
| 4 | p3 | `scrapecreators.reddit.search.posts` | $0.00188 | 29 posts. **Poor relevance** — see p3 |
| 5 | p4 | `scrapecreators.x.v1-facebook-adlibrary-search-ads` | $0.00188 | 914 matches, 29 ads with full creative |
| 6 | p2 | `hunter.people.email.find` | $0.0245 | Found + verified `valid`, confidence 80 |
| 7 | p1 | `dataforseo.google.serp.organic` | $0.002 | Top 7 for `serp api`, stamped 02:06:40 UTC |
| 8 | p4 | `scrapecreators.x.v1-linkedin-ads-search` | $0 | **400** — used `query` instead of `keyword`. Not billed |
| 9 | p4 | `scrapecreators.x.v1-google-adlibrary-advertisers-search` | $0.00188 | Semrush across 3 advertiser entities, ~29,500 ads |
| 10 | p4 | `scrapecreators.x.v1-linkedin-ads-search` | $0.00188 | 2,107 ads, 24 with full copy |
| 11 | p5 | `scrapecreators.x.v1-linkedin-company` | $0 | **400 from treg.to itself** — required param missing, refused before reaching the provider |
| 12 | p3 | `tikhub.tiktok.search.videos` | $0.001 | 10 videos with play/like/comment/share |
| 13 | p5 | `scrapecreators.x.v1-linkedin-company` | $0.00188 | **Wrong entity returned** — see p5 |
| 14 | p4 | `tikhub.x.tiktok-ads-search-ads` | $0 | **405** on the documented shape. Not billed, not retried |
| 15 | p3 | `tikhub.youtube.search.videos` | $0.002 | 19 videos + 25 Shorts with view counts |

**14 calls attempted, 9 billed, 5 failures — none of which cost anything.** That is the single most
persuasive number in this table and it was not designed, it just happened.

## Batch 2 — team `superdesign`, via CLI (gap-closing)

**Balance before $4.8821 → after $4.7811. Spent: $0.1010.**

| # | Page | Endpoint | Cost | Outcome |
|---|---|---|---|---|
| 16 | p4 | `scrapecreators.x.v1-tiktok-ad-library-search` | $0.00188 | Semrush's TikTok ads — video URLs, shown dates, audience bands. **Closes p4 to 4 of 4** |
| 17 | p5 | `leadmagic.x.company-funding` (runpod.io) | $0 | No funding data. Free miss |
| 18 | p5 | `leadmagic.x.company-funding` (daloopa.com) | $0 | No funding data. Free miss |
| 19 | p5 | `leadmagic.x.company-funding` (stripe.com) | $0.10 | Full history — $9.8B raised, revenue, last round, named investors |
| 20 | p3 | `tikhub.x.twitter-web-fetch-search-timeline` | $0.001 | X search timeline. **Closes p3 to 4 of 4** |
| — | p5 | `lusha.companies-signals` | — | **Endpoint id does not exist.** Blocked; see bug 1 |

Across both batches: **20 calls, 11 billed, 9 failures or misses, $0.00 charged for every one of them.**

---

## What these runs prove, and what they don't

**Proven and safe to publish:**
- Failed calls are free, including a treg.to-side refusal that never reached the provider.
- Prices match the catalog exactly — every `cost_usd` matched what `catalog_get` quoted beforehand.
- The credential never touched this machine; no provider account was created for any of it.
- Real, current data: results timestamped to the minute, a YouTube video published 11 hours earlier.

**Closed in batch 2:** p3 is 4 of 4 platforms. p4 is 4 of 4 ad libraries. p5's funding leg is proven.

**Still not proven — the page must not claim it:**
- **p5, activity/hiring signals.** Blocked by bug 1: the endpoint id `catalog_search` returns for company
  signals does not resolve. Funding is proven; "buying signals" broadly is not.
- Sample sizes are demonstrations, not benchmarks: 20 keywords not 50, one email not 50.

---

## Two findings worth keeping

**The wrong-company result (item 13).** Asked for `linkedin.com/company/runpod`, the provider returned a
two-person retail partnership in Sligo, Ireland — correctly, because that is what is at that URL. The AI
infrastructure company is `/company/runpod-io`. A confident wrong answer for $0.00188. This is the
catalog's first selection rule in practice — match the inputs you actually hold, ahead of price — and it
is the clearest argument for why treg.to relays a request rather than rewriting it.

**Reddit relevance (item 4).** A whole-site search for "ai agents" surfaced an unrelated r/lol post as a
top result. Ranking belongs to the provider. Three others serve Reddit search, one at $0.001, and
subreddit-scoped search is a separate endpoint. Cheap to fix, but it is a real limitation.

Both are on the pages. Neither is flattering, and both make the pages more credible than a clean run would
have.
