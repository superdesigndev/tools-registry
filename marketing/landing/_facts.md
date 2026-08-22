# Facts registry — every number allowed on a landing page

**Rule: if a figure is not in this table, it may not appear on a page.** Each page ends with a
*Numbers used on this page* table naming its fact keys, so re-verification is mechanical.

Verified **2026-08-17** unless stated. Re-verify with:

```bash
curl -s https://treg.to/catalog/platforms | python3 -c "..."   # counts
treg catalog get <endpoint_id>                                  # price + measured stats
```

---

## Product and pricing

| Key | Value | Source | Verified |
|---|---|---|---|
| `F-01` | **2,600+ tools · 40+ providers** | treg.to hero (2,617 / 42); rounded down so it holds under every count | 2026-08-17 |
| `F-02` | $1.00 free credit on every new team | treg.to; `llms.txt`; `config.py promo_grant_micro = 1_000_000` | 2026-08-17 |
| `F-03` | 0% additional fee — treg.to does not mark up provider call prices | treg.to ("0% additional fee — we earn on volume pricing with vendors, not on you") | 2026-08-17 |
| `F-04` | Credentials injected server-side; response relayed verbatim; every call audited | `llms.txt` | 2026-08-17 |
| `F-05` | Your own key always wins over treg's, and those calls are never metered | `llms.txt`; treg.to | 2026-08-17 |
| `F-06` | treg.to does **not** choose a provider or fail over for you, by design | `llms.txt` line 182 | 2026-08-17 |
| `F-07` | 5xx, timeouts and network errors are never billed; `Idempotency-Key` prevents paying twice for a retry | `llms.txt` | 2026-08-17 |
| `F-08` | Sign-in doors: GitHub, Google, email one-time code. No card required to create a team or spend the grant | treg.to sign-in modal; `ledger.grant` fires at org creation | 2026-08-17 |
| `F-09` | Installer registers the MCP server into Claude Code, Cursor and opencode; other MCP clients connect via OAuth; anything that runs a shell can use the CLI | `llms.txt` | 2026-08-17 |
| `F-10` | 100% open source, AGPL — github.com/superdesigndev/treg | treg.to footer | 2026-08-17 |
| `F-11` | Price is visible before the call (`catalog get` / `estimated_cost_usd`) | `llms.txt` | 2026-08-17 |
| `F-12` | Out of balance returns HTTP 402 with `balance_micro`, `estimated_cost_micro` and a top-up URL | `llms.txt` | 2026-08-17 |
| `F-13` | A team can pin a capability to one provider: `treg org pin <capability> --provider <p>` | `llms.txt` CLI reference | 2026-08-17 |

> **Why `F-01` is a rounded claim, not an exact one.** Three surfaces currently disagree: treg.to's hero
> says 2,617 endpoints / 42 providers, `llms.txt` says "~2,600 endpoints across ~48 providers", and
> `GET /catalog/platforms` sums to 2,363 endpoints across 47 providers over 80 platforms. **2,600+ tools /
> 40+ providers** is Jason's call (2026-08-17) and is safe on the provider half under every count.
>
> One loose end on the tool half: `/catalog/platforms` is the only surface below 2,600, so if anyone
> audits the claim that endpoint is what they will hit. Worth checking whether it under-reports (it may
> exclude own-key or unpriced endpoints) before this number goes into a Google Ads asset — a claim in an
> ad has to be defensible on the page it points at.
>
> Prefer **"tools"** over "endpoints" in all public copy: per `CLAUDE.md`, a tool is what an agent calls,
> and it is the word a visitor understands without knowing what an API endpoint is.

## Subscription list prices (the "instead of" anchors, all from treg.to's catalog grid)

| Key | Value |
|---|---|
| `F-20` | Semrush $139/mo · Serpstat $69/mo · SpyFu $39/mo · SerpApi $75/mo · SE Ranking $65/mo · Moz $99/mo · Majestic $50/mo · SEOTesting $40/mo · Supermetrics $69/mo |
| `F-21` | Hunter $34/mo · Lusha $49/mo · Apollo $59/seat · Crunchbase $99/mo · Diffbot $299/mo |
| `F-22` | X API $200/mo · Postiz $29/mo |
| `F-23` | Optmyzr $249/mo · Revealbot $99/mo · Madgicx $55/mo · Adalysis $99/mo · Tailwind $25/mo · BrightLocal $39/mo |
| `F-24` | treg.to's own summed example: "$371/mo of seats → cents per call" |

## Measured endpoint data — `treg catalog get`, 2026-08-17

Columns: **cost** · **success rate (sample size)** · **median latency**. `—` means nobody has called it
enough to measure, which is itself worth showing.

### Keyword volume — capability `google.keywords.volume`, 5 providers (`F-30`)

| Provider | Cost | Success (n) | Median |
|---|---|---|---|
| Serpstat | $0.0005 / keyword returned | 100% (77) | 1.2 s |
| SE Ranking | $0.00179 / keyword returned | 92.3% (20) | 1.7 s |
| DataForSEO | $0.09 flat / call, 1–1,000 keywords | 98.8% (165) | 3.9 s |
| Google Ads | free with a connected account | — (8) | — |
| Semrush | 10 API units / row; own key only | — (0) | — |

### Google organic results — capability `google.serp.organic` (`F-31`)

Serpstat $0.0005 · DataForSEO $0.002 · ScrapeCreators $0.00188 · SerpApi $0.015 · Semrush own-key only.

### Other SEO endpoints (`F-32`)

Keyword ideas: DataForSEO $0.012 · SE Ranking $0.00179 · Serpstat $0.0005.
Bulk keyword difficulty: DataForSEO $0.01236.
Backlinks / authority: Majestic $0.0008 (own key) · SE Ranking $0.002685 · Moz $0.006667 · DataForSEO
$0.024. Backlinks capability has 76 matching endpoints.

### Find a work email — capability `people.email.find` (`F-40`)

| Provider | Cost | Success (n) | Median |
|---|---|---|---|
| Hunter | $0.0245, **charged only when an email is found** | 100% (582) | 1.4 s |
| LeadMagic | $0.025, free on a miss | 100% (86) | 8.3 s |
| LeadMagic (from a profile URL) | $0.125, free on a miss | 100% (15) | 1.6 s |

### Verify an email (`F-41`)

LeadMagic $0.00625 — **100% over 963 calls, 6.1 s median; the most-called enrichment endpoint on the
platform.** Hunter $0.01225.

### Every email at a company (`F-43`)

`hunter.companies.emails` — 100% over 597 calls, 0.4 s median. Returns the known addresses at a domain in
one call. Not previously represented on any page; added to p2 on 2026-08-17.

### Enrich a person (`F-42`)

LeadMagic $0.025 · Lusha $0.2496 · Coresignal $0.392.

### Search companies — capability `companies.search`, 11 providers (`F-50`)

| Provider | Cost | Success (n) | Median |
|---|---|---|---|
| Akta | free | 100% (198) | 3.2 s |
| Coresignal | free (ids only; the cost lands on collect) | — (1) | — |
| Hunter Discover | free | 100% (24) | 1.7 s |
| The Companies API | $0.0019 / company returned; free with `simplified=true` | 100% (338) | 0.3 s |
| Lusha | $0.004992 / up to 25 results | — (0) | — |
| LeadMagic | $0.025 / company returned | 70% (10) | 3.2 s |
| Apollo | $0.026 / page returned | — (0) | — |
| Diffbot | $0.0299 / record returned | 100% (5) | 0.7 s |
| PDL | $0.38 / record returned | — (0) | — |
| Crunchbase | own key only, no per-call price | — (2) | — |

**200× spread for the same job, and the cheapest is free.** This table is the single most defensible
asset in this whole cluster.

### Company signals and funding (`F-51`)

Lusha company signals $0.1248 · Lusha signal types (list) free · LeadMagic company funding $0.10 ·
LeadMagic company search $0.025 · Apollo company enrich $0.026 · Coresignal company enrich $0.392 ·
ScrapeCreators LinkedIn company page $0.00188.

### Ad libraries (`F-60`)

| Job | Provider | Cost | Success (n) | Median |
|---|---|---|---|---|
| Meta ad library search | ScrapeCreators | $0.00188 / call | 100% (166) | 3.1 s |
| Meta ad library search | Apify | $0.005 / ad returned | 100% (16) | 12.9 s |
| Meta ad library search | Meta (own key) | free; identity verification required | — (1) | — |
| Google Ads Transparency | DataForSEO | $0.0006 (async batch) | — | — |
| Google Ads Transparency | ScrapeCreators | $0.00188 | — | — |
| Google Ads Transparency | SerpApi | $0.015 | — | — |
| TikTok ad library | ScrapeCreators | $0.00188 / call | 100% (16) | 3.5 s |
| TikTok ad library | Apify | $0.003 / result | — | — |
| TikTok ad library | tikhub | $0.001 | none of 7 recorded successful — returns 405 on its documented shape | — |
| LinkedIn ads search | ScrapeCreators | $0.00188 | — | — |
| LinkedIn ads search | tikhub | $0.004 | — | — |

### Social platforms — endpoints per platform, `GET /catalog/platforms` (`F-70`)

TikTok 163 · Instagram 150 · YouTube 148 · X 147 · LinkedIn 72 · Facebook 58 · Reddit 35 · Threads 17 ·
Pinterest 5. (Douyin 291, Weibo 75, Zhihu 56, Bilibili 49, Kuaishou 46, Xiaohongshu 40 are also live and
are the strongest coverage in the catalog — worth a China-facing variant later, not on these pages.)

### Social endpoint prices (`F-71`)

Reddit search: tikhub $0.001 · ScrapeCreators $0.00188 · JustOneAPI $0.01476.
Most tikhub social endpoints are $0.001 per call, including a TikTok profile.

### Category totals (`F-80`)

Social 1,501 endpoints · SEO/AEO 367 · Advertising 205 · Enrichment 88 (companies 47, people 41) ·
E-commerce 81 · Reviews & Apps 55 · Market data 40 · Community 16 · Developer 10.

---

## Production telemetry — 30 days to 2026-08-17 (`F-90`)

Swept from the live registry's public endpoint view, which attaches cross-tenant `observed` stats. Source:
the "What Agents Call treg For" artifact. **24,921 catalog calls across every team.**

**Publishable vs internal — read this before using anything here.**

- ✅ **Per-endpoint `calls served`, `ok` rate and `p50`** are fine to publish. `catalog_get` already shows
  these cross-tenant numbers to any user, so they are public information, and they are the strongest
  reliability evidence we have.
- ❌ **The aggregates are internal**: total platform volume (24,921), the 82%-of-catalog-dark figure, the
  provider revenue split, the never-used counts. Those describe our business, not a caller's experience.
  Keep them in this file and out of the pages.

### Where the traffic actually goes (`F-91` — internal, for targeting only)

| Job | Calls | Share |
|---|---|---|
| Google SERP (maps + organic + news + trends) | 7,559 | 30.3% |
| X / Twitter | 4,351 | 17.5% |
| People enrichment | 3,657 | 14.7% |
| TikTok | 1,849 | 7.4% |
| Company data | 1,164 | 4.7% |
| LinkedIn | 1,077 | 4.3% |
| Instagram | 1,060 | 4.3% |
| Web scraping | 961 | 3.9% |
| Reddit | 886 | 3.6% |
| AI answer engines | 376 | 1.5% |
| **Meta ad library** | **303** | **1.2%** |
| YouTube | 162 | 0.7% |

Two findings that change page targeting:
- **Keyword research is not what people do.** No keyword endpoint appears in the top 20. Serpstat totals
  334 calls (1.3%). The real "SEO" job is **scraping result pages** — Maps alone is 2,646.
- **Local/Maps is the biggest uncovered job**, and no page in this cluster addresses it.

### Measured endpoint stats — use these, they beat the old sample sizes (`F-92`)

| Endpoint | Calls | ok | p50 |
|---|---|---|---|
| `tikhub.x.twitter-web-fetch-search-timeline` | 3,311 | 1.00 | 2.6 s |
| `dataforseo.x.serp-google-maps-live-advanced` | 2,646 | 1.00 | 1.9 s |
| `dataforseo.google.serp.organic` | 2,075 | 1.00 | 5.5 s |
| `leadmagic.people.email.verify` | 963 | 1.00 | 6.1 s |
| `dataforseo.x.serp-google-news-live-advanced` | 690 | 1.00 | 9.0 s |
| `hunter.companies.emails` | 597 | 1.00 | 0.4 s |
| `hunter.people.email.find` | 582 | 1.00 | 1.4 s |
| Google Trends (dataforseo) | 462 | 0.98 | 6.7 s |
| `tikhub.x.tiktok-app-v3-fetch-hashtag-search` | 436 | 1.00 | 1.3 s |
| `tikhub.tiktok.search.videos` | 389 | 1.00 | 3.3 s |
| `tikhub.x.reddit-app-fetch-dynamic-search` | 384 | 1.00 | 1.0 s |
| `tikhub.tiktok.user.profile` | 365 | 1.00 | 0.4 s |
| `hunter.people.enrich` | 356 | 1.00 | 0.3 s |
| `scrapecreators.x.v1-google-search` | 343 | 1.00 | 1.6 s |
| `thecompaniesapi.companies.search` | 339 | 1.00 | 0.3 s |
| `scrapecreators.x.v2-instagram-reels-search` | 300 | 1.00 | 3.9 s |
| `scrapecreators.reddit.search.posts` | 283 | 0.98 | 8.5 s |
| `tikhub.tiktok.user.videos` | 267 | 1.00 | 0.5 s |
| `apollo.people.enrich` | 263 | 1.00 | 0.3 s |

### Endpoints that FAIL — never send a reader to one of these (`F-93`)

| Endpoint | Calls | State |
|---|---|---|
| `tikhub.x.linkedin-web-search-people` | 313 | **every call 4xx** |
| `diffbot extract-analyze` | 73 | every call 4xx |
| `tikhub.x.linkedin-web-search-jobs` | 26 | every call 4xx |
| `tiktok-ads trends-hashtag` | 22 | every call 4xx |
| `lusha decision-makers` | 13 | every call 4xx |
| `x.x.search-posts-recent` (official X API) | 208 | **ok 0.62** — fails 2 calls in 5 |
| `x.x.post.create` (official X API) | 38 | ok 0.64 |
| `spyfu.google.domain.competitors` | 36 | ok 0.81 |

> **`x.x.search-posts-recent` at ok 0.62 against `tikhub` at 1.00 over 3,311 calls is the single best
> proof point in this dataset** — the official API is the unreliable option, and we measured it.

### The caveat that limits all of the above

The stats pool every org deliberately: **there is no per-tenant breakdown, so 24,921 calls could be forty
teams or four.** Our own `superdesign` org contributes under 40 of them, so the demand is genuinely
external — but its concentration is unknown. Treat this as directional for targeting, not as proof of a
market. The team count needs `TREG_ADMIN_TOKEN` and one `/admin/*` call.

---

## Claims that are NOT verified — do not use

- Any customer name, quote, logo or case study. None exist.
- Any security certification (SOC 2, ISO, GDPR compliance statement). None claimed anywhere.
- Any uptime, latency or accuracy *guarantee*. The catalog reports what has been measured; that is an
  observation, not an SLA.
- "Access to every provider / every platform." Coverage is specific and varies by platform.
- Automatic routing, fallback or failover. See `F-06` — it does not exist and must not be implied.
- Anything about how many people or teams use treg.to. Not instrumented.
