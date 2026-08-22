# Demo shooting script — five screen recordings

One per landing page. Each is a **single unbroken terminal take**, 45–70 seconds, no editing beyond
top-and-tail. Every command here was run for real on 2026-08-17, so the outputs below are what you should
actually see — if a take doesn't match, something changed and the page copy needs re-checking too.

Total cost to shoot all five, including one rehearsal pass: **well under $0.50**. Balance was $4.78 on the
`superdesign` team at time of writing.

---

## Before you hit record

**Use the CLI, not an agent's MCP connection.** The MCP grant on this machine is pinned to the
`superdesign-7` team ($0.02 left), not `superdesign` ($4.78). Calls through an agent will bill the wrong
balance and may 402 mid-take. Confirm with:

```bash
treg org ls          # expect: * superdesign (active)
treg balance         # expect: ~$4.78
```

**Terminal setup**
- Window ~110 columns × 30 rows. Wider than that and the catalog tables wrap; narrower and they truncate.
- Font 15–16pt. The persuasive content is a *table of small numbers* — if it isn't legible on a phone,
  the demo does nothing.
- Light theme if you have one, to sit with the page. Dark is fine, the page's prompt block is dark too.
- Clear scrollback (`clear`) immediately before each take.

**Two hard stops**
1. **Never show the token.** Don't run `treg login`, `env`, `cat ~/.treg/*`, or anything that prints
   `trg_live_…` on screen. `treg balance` and `treg call` are safe.
2. **Demo 2 returns a real person's real work email.** Do not publish that frame. The script below uses
   your own domain so the address is yours. If you'd rather show a recognisable company, blur the address
   in post — do not skip this.

**`jq` is required.** Every command is piped through it. `brew install jq` if `jq --version` fails.

**Rehearse first.** Run the whole block below once before recording. It costs about $0.15 and it is the
difference between a clean take and discovering on camera that a provider changed its shape.

```bash
treg balance
treg catalog get serpstat.google.keywords.volume            # free
treg catalog get apollo.companies.search                    # free
treg catalog search "ad library"                            # free
# then one paid call from each demo below
```

Every jq pipeline in this file was tested against the real 2026-08-17 responses, including the rows where
a field comes back null. If one errors, the provider changed its response shape — which also means the
matching page needs re-checking, not just the demo.

**If a call fails on camera, keep rolling.** A 4xx costs nothing and treg says so. Recovering live — reading
the error, switching provider — is a better demo than a clean run. That is literally the product.

---

## Demo 1 — SEO · for `/use-cases/seo-data-for-ai-agents/`

**The one beat:** five providers sell the same keyword data at a 180× spread, and the cheapest is also the
best-measured.
**Length:** ~60s.

| # | Type this | What lands on screen | Hold |
|---|---|---|---|
| 1 | `treg catalog search "keyword search volume"` | The endpoint list with a COST column | 3s |
| 2 | `treg catalog get serpstat.google.keywords.volume` | **The money frame.** The sibling table: serpstat $0.0005/kw · 100% (77) · 1.2s, seranking $0.00179 · 92% (20), dataforseo $0.09/call · 99% (165) · 3.9s | **6s — this is the shot** |
| 3 | The call below | 13 keywords with real volume, difficulty and intent | 5s |
| 4 | `treg balance` | The charge: $0.010 | 4s |

```bash
treg call serpstat.google.keywords.volume --method POST --data '{"id":"1","method":"SerpstatKeywordProcedure.getKeywordsInfo","params":{"keywords":["mcp server","serp api","agent skills","reddit api","tiktok api","web scraping api","rank tracking api","backlink api","keyword research api","email finder api"],"se":"g_us","withIntents":true}}' \
  | jq -r '.result.data[] | [.keyword, .region_queries_count, .difficulty, (.intents[0] // "-")] | @tsv' \
  | sort -t$'\t' -k2 -rn | column -t -s $'\t'
```

The `-s $'\t'` matters: without it `column` splits on spaces too and `mcp server` breaks across two
columns on screen. All four pipe stages are load-bearing — copy the line whole.

Expect `mcp server 60500 8 informational` at the top, `serp api 12100 17`, `agent skills 8100 5`.
Cost $0.005–0.010 depending on how many return data.

**If you narrate, one line:** *"Five providers sell this. The cheapest is a hundred and eighty times cheaper
than the most expensive, and it also has the best measured success rate. My agent can see that before it
calls."*

---

## Demo 2 — Enrichment · for `/use-cases/lead-enrichment-for-ai-agents/`

**The one beat:** discovery is free, and a lookup that finds nothing costs nothing. You only pay for answers.
**Length:** ~55s.

⚠️ **Privacy:** step 3 returns a real work email. Use your own domain, or blur it.

| # | Type this | What lands on screen | Hold |
|---|---|---|---|
| 1 | Discovery call below | 9 funded companies **and `cost_usd: 0`** | 5s |
| 2 | `treg catalog get hunter.people.email.find` | COST line: *1 credit, charged only when an email is found; a miss is free* | 5s |
| 3 | Email call below | A found + verified address, `"status": "valid"` | 4s |
| 4 | Deliberate miss below | `"email": null` — and the cost line reads **$0.00** | **6s — this is the shot** |
| 5 | `treg balance` | One charge, not two | 4s |

```bash
# 1 — free discovery
treg call hunter.x.discover-companies --method POST \
  --data '{"query":"AI infrastructure companies that recently raised funding, 20-200 employees","limit":10}' \
  | jq '{companies: [.data[].organization], charged: 0}'

# 3 — a real find (use YOUR domain and name)
treg call hunter.people.email.find --query domain=YOURDOMAIN.com --query full_name="Your Name" \
  | jq '{email: .data.email, score: .data.score, verified: .data.verification.status}'

# 4 — the miss. This is the beat. Nothing is charged.
treg call hunter.people.email.find --query domain=stripe.com --query full_name="Nonexistent Personname" \
  | jq '{email: .data.email}'
```

**Narration:** *"Finding the companies was free. Finding an email that doesn't exist was also free. I pay two
and a half cents when it actually finds someone."*

---

## Demo 3 — Social · for `/use-cases/social-trend-research-for-ai-agents/`

**The one beat:** four platforms — including ones whose official APIs you cannot buy — in one run, under a cent.
**Length:** ~70s (it's four calls; keep each tight).

| # | Type this | What lands on screen | Hold |
|---|---|---|---|
| 1 | TikTok call | 10 videos with play counts | 4s |
| 2 | YouTube call | Videos with view counts, one published hours ago | 4s |
| 3 | X call | A live search timeline | 3s |
| 4 | Reddit call | Posts with scores | 3s |
| 5 | `treg balance` | **Four platforms, ~$0.006 total** | **6s — this is the shot** |

```bash
treg call tikhub.tiktok.search.videos --query keyword="ai agents" --query count=10 \
  | jq -r '.data.search_item_list[]?.aweme_info | "\(.statistics.play_count)\t\(.desc[0:60])"' | head -5

treg call tikhub.youtube.search.videos --query keyword="ai agents" --query language_code=en --query country_code=us \
  | jq -r '.data.videos[] | "\(.view_count)\t\(.published_time)\t\(.title[0:50])"' | head -5

treg call tikhub.x.twitter-web-fetch-search-timeline --query keyword="ai agents" \
  | jq '{ok: .code, at: .time}'

treg call scrapecreators.reddit.search.posts --query query="ai agents" --query sort=top --query timeframe=week \
  | jq -r '.posts[] | "\(.score)\t\(.subreddit)\t\(.title[0:50])"' | head -5
```

**Honesty note, and I'd keep it in:** the Reddit results are noisy — site-wide search returns off-topic hits.
If you want the take clean, add `--query subreddit=LocalLLaMA`. If you want it credible, leave it and say
*"that one's a bad result — ranking is the provider's, and I'd pin a different one for real work."*

**Narration:** *"TikTok's API is invite-only. X wants two hundred a month. That was four platforms for
six tenths of a cent."*

---

## Demo 4 — Ads · for `/use-cases/competitor-ad-research-for-ai-agents/`

**The one beat:** the Google Ads Transparency call reveals the same advertiser split across three registered
entities — search one name and you see a third of the activity. Nobody expects this.
**Length:** ~60s.

| # | Type this | What lands on screen | Hold |
|---|---|---|---|
| 1 | Meta call | Ad count + live creative with body copy | 4s |
| 2 | Google call | **Semrush INC ~20,000 · Semrush Inc ~9,000 · Semrush Inc. ~500** | **7s — this is the shot** |
| 3 | LinkedIn call | 2,107 ads, full post copy | 4s |
| 4 | TikTok call | Running TikTok ads with dates | 3s |
| 5 | `treg balance` | Four ad libraries, ~$0.0075 | 4s |

```bash
treg call scrapecreators.x.v1-facebook-adlibrary-search-ads --query query="seo tool" --query country=US --query status=ACTIVE --query trim=true \
  | jq '{matching_ads: .searchResultsCount, sample: [.searchResults[0].snapshot | {page_name, cta_text, body: .body.text[0:80]}]}'

treg call scrapecreators.x.v1-google-adlibrary-advertisers-search --query query=semrush \
  | jq '.advertisers'

treg call scrapecreators.x.v1-linkedin-ads-search --query keyword="seo software" \
  | jq '{total: .totalAds, sample: [.ads[0] | {poster, promotedBy}]}'

treg call scrapecreators.x.v1-tiktok-ad-library-search --query query="Semrush" \
  | jq '{source, ads: [.ads[0] | {name, first_shown_date}]}'
```

**Narration for the money frame:** *"Same company, three registered advertiser entities. If you searched one
name you'd see a third of what they're actually running."*

---

## Demo 5 — Company data · for `/use-cases/company-research-for-ai-agents/`

**The one beat:** eleven providers answer one job, from free to $0.38 a record. A 200× spread on the same
question.
**Length:** ~55s.

| # | Type this | What lands on screen | Hold |
|---|---|---|---|
| 1 | `treg catalog get apollo.companies.search` | **The money frame.** Eleven siblings with COST / WORKS / SPEED — akta free, thecompaniesapi $0.0019, apollo $0.026, pdl $0.38 | **8s — hold longest of any shot** |
| 2 | Free search below | Real companies, `cost_usd: 0` | 4s |
| 3 | Funding call below | Stripe: $9.8B raised, last round, named investors — $0.10 | 5s |
| 4 | The miss below | *"Company funding data not found"*, `credits_consumed: 0` | 5s |

```bash
treg call hunter.x.discover-companies --method POST \
  --data '{"query":"AI infrastructure companies that recently raised funding, 20-200 employees","limit":10}' \
  | jq '{found: [.data[].organization] | length, charged: 0}'

treg call leadmagic.x.company-funding --method POST --data '{"company_domain":"stripe.com"}' \
  | jq '{company: .basicInfo.companyName, raised: .financialInfo.formattedFunding, last: .financialInfo.lastFundingRound.round}'

# the honest beat — coverage is thin on small startups, and the miss is free
treg call leadmagic.x.company-funding --method POST --data '{"company_domain":"daloopa.com"}'
```

**Narration:** *"Eleven providers answer that one question. Three of them are free, one charges thirty-eight
cents a record. Same job. Most people just buy the first one they've heard of."*

---

## What to do with the five files

- **On the pages:** place below the copy-prompt block, never above it. The conversion is the first call, and a
  satisfying video above the fold is a reason to close the tab. The pages already emit `lp_copy_prompt`, so
  ship the demo to two of the five first and compare — don't assume it helps.
- **Stamp every one** "recorded 17 Aug 2026". Catalog prices are re-checked over time and a demo hardcodes a
  moment, unlike everything else on these pages which routes through `_facts.md`.
- **Reuse:** demo 4's three-entity reveal and demo 5's eleven-provider table are the two that work as
  standalone social clips. Demo 2's free-miss is the best 15-second cut.

## Where the take can go wrong

| Symptom | Cause | Do this |
|---|---|---|
| 402 mid-take | Billing the `superdesign-7` team | `treg org use superdesign`, check `treg balance` |
| Terminal floods with JSON | Dropped the `jq` pipe | Re-run with the pipe as written |
| `405` on a TikTok ads call | You used `tikhub.x.tiktok-ads-search-ads` — it's broken | Use `scrapecreators.x.v1-tiktok-ad-library-search` as scripted |
| `no endpoint '…' in the catalog` | Bad id | `treg catalog search "<the job>"` and read the real id |
| Reddit results look irrelevant | Provider ranking, not your query | Add `--query subreddit=…`, or leave it and say so |
