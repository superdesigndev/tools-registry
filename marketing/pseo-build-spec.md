# treg.to programmatic pages — build spec

Companion to `pseo-catalog-plan.md` (the why). This is the what: every page type, its URL, what is
on it, where each block's data comes from, how it links, and when it is indexed. Written 2026-08-21.

Rules that apply to every page below:

- **Server-rendered through `_page()` in `api.py`** (title, description, canonical, og/twitter,
  JSON-LD all owned by the shell). No SPA, no `#prerender` trick — GPTBot/ClaudeBot/PerplexityBot
  do not run JavaScript, and agent retrieval is half the point.
- **Every number and name comes from `catalog_store.load()` at request time.** No page may carry a
  count, price or provider name that is not in the catalog. Counts that appear in copy ("81
  platforms", "2,800 endpoints") are computed, never typed.
- **`treg.to` in every title, H1 and anchor.** Never bare `treg`.
- **Observed stats** (success rate, p50) appear only with the one-line caveat "measured on treg.to
  traffic; inputs and sample sizes differ by provider — not a controlled benchmark", and only if
  Jason approves per-vendor publication. Prices appear always.
- **Routing and sitemap from one map**, as `_USE_CASES` / `_SITEMAP_PAGES` do today: a page cannot
  be routed and forgotten by the sitemap. `tests/test_seo.py` walks every sitemap URL for a 200.
- **No trailing slashes; canonical = bare path.** `?v=<mtime>` on any new stylesheet.
- **Every row is a CTA.** The visitor's agent is remembered in `localStorage` (first visit: picker);
  "Use in ChatGPT / Claude / Claude Code / Cursor" buttons on every row open the install path for
  that agent, never `/app`.
- **Sitewide footer** on all of these: the 12 most-called platforms (from `observed` call counts)
  and the four agent pages. That is where authority pools.

---

## Page type A — agent hub · `/apps/<agent>` · 10 pages

Revised 2026-08-21: the onboarding (`welcomeAgents` / `welcomeMoreAgents` in `index.html`) already
ships a setup path for **OpenClaw, Hermes Agent, Claude.ai, Claude Code, Codex, opencode, pi,
Cursor, Gemini CLI**, and ChatGPT has the Plugins listing. One page each. The list must move to one
shared source (a small JSON the SPA and the server pages both read) so a page and the dropdown can
never disagree.

The install is mostly **agent-agnostic** — the onboarding's "set up treg — https://treg.to/skill.md …"
line pasted into any agent's chat (`buildAgentPrompt`). What differs per agent, and is the
per-page content: *where* you paste it (screenshot), and the native path where one exists —
ChatGPT: Plugins → Install; Claude.ai: Connectors → `https://treg.to/mcp/` (OAuth); Claude Code /
Cursor / opencode: `treg mcp install`. Hand-written above the fold, generated below.

| Element | Content | Source |
|---|---|---|
| Title | `treg.to for ChatGPT — 2,800 tools your ChatGPT can call, no API keys` | count computed |
| Meta | `Install treg.to from ChatGPT's Plugins directory and ChatGPT can call {N} APIs across {P} platforms — SEO, LinkedIn, Reddit, people search — priced per call, no provider signup.` | computed |
| H1 | `What do you want ChatGPT to do?` | fixed per agent |
| Install block | per agent: **ChatGPT** — "Plugins → search *treg* → Install", with Jason's screenshot; **Claude** — "Settings → Connectors → Add → `https://treg.to/mcp/`" (OAuth); **Claude Code / Cursor** — `curl -fsSL https://treg.to/install.sh \| sh` then `treg mcp install`. A button that starts the flow where a deep link exists. | `llms.txt` §MCP, `skill.md` |
| Five prompts | five pasteable prompts that each complete a real job end to end, e.g. "Find the work email of the VP Marketing at stripe.com and tell me what it cost" / "Pull the top 20 Google results for 'serp api' with each page's backlink count" / "Give me the last 30 days of Reddit posts mentioning 'mcp server' with upvotes". Each prompt names the capability it will hit and its price. | hand-written once, prices computed |
| Sections by category | **Sales & people** · **SEO & search** · **Social: LinkedIn, Instagram, TikTok, X, Reddit, YouTube** · **Companies & market** · **Ads** · **Web & scraping** — each section = rows of use cases (type D) with price range and provider count; the section heading links to its category hub (type C) | catalog categories → `_USE_CASES` |
| Platform grid | all platforms with ≥2 providers, linking to `/mcp/<platform>` (type B), with endpoint count and lowest price | catalog |
| How billing works | $1.00 free per new team, per-call metering, own keys never metered | `_facts.md` keys, not typed |
| JSON-LD | `SoftwareApplication` (name treg.to, `applicationCategory` DeveloperApplication, `offers` $0 with the free grant — matches visible text) + `BreadcrumbList` + `FAQPage` (4 Qs that appear verbatim in an FAQ section: is it free, do I need API keys, which agents, what does a call cost) | — |
| Links out | 4 agent siblings, 5 category hubs, ~25 platform hubs, ~30 use-case rows | — |
| Index | yes, day one | — |

Add `windsurf`, `antigravity`, `perplexity` etc. only when the onboarding dropdown gains them —
the page list is derived from that list, never maintained separately.

## Page type B — `/mcp/<platform>` · 3 pages, provisional

Revised 2026-08-21 after Jason asked "why B?". With E (pricing/comparison) and D (use cases) in
place, B is redundant as a hub — all three list the same providers and jobs. Its only distinct job
is carrying the exact phrase `<platform> mcp server`, which is worth a page only where people type
it. Measured: `linkedin mcp server` 480 · `reddit mcp server` 210 · `youtube mcp server` 140; the
SERPs are GitHub repos and small directories — the most winnable terms in the study. No other
platform showed volume.

So: **three pages**, built like Pipedream's winning `mcp.pipedream.com/app/<app>` pages — short,
exact-match title, install steps for every agent, then links down to `/pricing/<platform>-api` and
the spokes. If none has moved by day 30, drop B and put the MCP install block on E instead. The
platform-level hub role belongs to E, which has the editorial content to deserve it. The section
table below describes the full version; the three pages ship the short one.

| Element | Content | Source |
|---|---|---|
| Title | `LinkedIn MCP server — 8 providers, 43 endpoints, from $0.002/call \| treg.to` | catalog |
| Meta | `Give ChatGPT, Claude, Claude Code or Cursor a LinkedIn MCP server with {N} endpoints from {providers}: profiles, company pages, posts, jobs. Priced per call, compared side by side, no provider signup.` | catalog |
| H1 | `LinkedIn MCP server for ChatGPT, Claude and Cursor` | — |
| Lede | one generated paragraph: what the agent can now do on this platform, in jobs not endpoints ("look up a member's profile, a company page, a post and its comments, search jobs") — composed from the capability descriptions | `capability.description` |
| Install tabs | the same four install blocks as type A, collapsed; the `claude-code`/`cursor` tab includes `treg catalog search "linkedin profile"` as the first command | — |
| **Jobs on this platform** | one row per capability: plain-words name, provider count, price range, verified count, link to its use-case page (type D). Single-provider capabilities are listed but link to `/catalog/<slug>#<cap>` instead of a spoke | catalog |
| **Providers compared** | table: provider · endpoints on this platform · cheapest price · verified · (observed ok-rate / p50 if approved) · "own key?" — the comparison is the product | catalog + `observed` |
| One runnable call | `treg call <cheapest verified endpoint> …` with its `test_request`, and the MCP equivalent ("ask your agent: …") | `test_request` |
| Who owns the key | the honest paragraph: treg.to's key, metered per call; or the team's own key, never metered; treg compares, **does not route** | `CLAUDE.md` rule |
| JSON-LD | `ItemList` of capabilities (as `/catalog/<slug>` does now) + `BreadcrumbList` + `FAQPage` (3 Qs visible on page: what does a LinkedIn profile lookup cost, does it need a LinkedIn login, which agents) | — |
| Links | up: `/apps/<agent>` ×4, `/catalog/<slug>`; down: every use case on this platform; across: `/integrations/<provider>` for each provider | — |
| Index | yes | — |

## Page type C — category hub · `/use-cases/<category>` · 5 pages (exist)

The five landing pages (`seo-data-for-ai-agents`, `lead-enrichment-for-ai-agents`,
`social-trend-research-for-ai-agents`, `competitor-ad-research-for-ai-agents`,
`company-research-for-ai-agents`) stay as the ad destinations they are. Two additions, appended
below the existing copy so `build_html.py` keeps owning the top:

- **a generated "every job in this category" list** — rows linking to type D pages, with price
  ranges; this is what makes them hubs;
- the four agent install buttons.

Their ad-kit cut-off rule stays; the generated block is injected by the route, not by the builder.

## Page type D — use case (the spoke) · `/use-cases/<category>/<job>` · ~201 generated, ~100–150 indexed

One per capability with ≥2 providers. Slug = the job in words, not the dotted id:
`people.email.find` → `/use-cases/lead-enrichment/find-work-email`. Slugs are a hand-kept map
(`capability id → slug, category, job sentence`) committed next to the catalog; a capability
without a slug renders nothing, so a typo cannot mint a page.

| Element | Content | Source |
|---|---|---|
| Title | `Find someone's work email from a name and company — 9 providers, from $0.009/success \| treg.to` | slug map + catalog |
| Meta | `{job sentence}. Compare {providers} on price, verified status and measured success, then call the one you pick from ChatGPT, Claude, Claude Code or Cursor — no provider account.` | — |
| H1 | the job sentence | slug map |
| Agent tabs | ChatGPT / Claude / Claude Code / Cursor — each tab shows *the prompt to type* for this job in that client, and the install link if not yet installed. One URL; the tab is client-side state | — |
| **Providers for this job** | table: provider · endpoint id · price (unit: /call, /result, /success) · verified date · (observed) · input it needs (email? LinkedIn URL? name + domain?) — the "pick the one whose inputs match what you HAVE" guidance from `catalog get` | catalog |
| Parameters | union of the inputs, marked required/optional per provider | `input` |
| One runnable call | cheapest verified endpoint's `test_request` as `treg call …` plus the MCP phrasing | `test_request` |
| Example response | first 25 lines of the `example_file`, if present | `example_file` |
| Same job, other platforms | e.g. "Find a person by email" → its siblings in `people.*` | capability family |
| Related jobs on these platforms | links to type B hubs and sibling spokes | — |
| JSON-LD | `BreadcrumbList` + `ItemList` of the provider endpoints (`name`, `url` = `/catalog/endpoints/<id>` JSON is *not* a page — point to the spoke's own `#<provider>` anchor) | — |
| Links | up: category hub, platform hub(s), agent hubs; across: siblings | — |
| **Index rule** | `index` when (≥2 providers) **and** (≥1 verified endpoint) **and** (observed calls in the last 30 days ≥ 1) — else `noindex,follow` and not in the sitemap. Recomputed on each sitemap render, so a job that starts getting used starts getting indexed | `observed` |

First batch (all ≥5 providers, mostly verified, already used in production):

| Slug | Capability | Providers |
|---|---|---|
| `/use-cases/lead-enrichment/find-work-email` | `people.email.find` | 9 |
| `/use-cases/lead-enrichment/enrich-a-person` | `people.enrich` | 13 |
| `/use-cases/lead-enrichment/search-people-by-title-and-company` | `people.search` | 14 |
| `/use-cases/lead-enrichment/verify-an-email` | `people.email.verify` | 5 |
| `/use-cases/lead-enrichment/find-a-phone-number` | `people.phone.find` | 5 |
| `/use-cases/company-research/enrich-a-company` | `companies.enrich` | 19 |
| `/use-cases/company-research/search-companies` | `companies.search` | 17 |
| `/use-cases/seo/google-search-results` | `google.serp.organic` | 5 |
| `/use-cases/seo/keyword-search-volume` | `google.keywords.volume` | 5 |
| `/use-cases/seo/keyword-ideas` | `google.keywords.ideas` | 6 |
| `/use-cases/seo/keywords-a-domain-ranks-for` | `google.domain.ranked_keywords` | 5 |
| `/use-cases/seo/backlink-profile` | `web.backlinks.summary` | 6 |
| `/use-cases/seo/list-backlinks` | `web.backlinks.list` | 6 |
| `/use-cases/seo/referring-domains` | `web.linking_domains.list` | 6 |
| `/use-cases/social/linkedin-profile` | `linkedin.user.profile` | 6 |
| `/use-cases/social/linkedin-company-page` | `linkedin.company.profile` | 4 |
| `/use-cases/social/instagram-profile` | `instagram.user.profile` | 6 |
| `/use-cases/social/instagram-post-comments` | `instagram.post.comments` | 5 |
| `/use-cases/social/tiktok-profile` | `tiktok.user.profile` | 5 |
| `/use-cases/social/tiktok-video` | `tiktok.video.detail` | 4 |
| `/use-cases/social/x-profile` | `x.user.profile` | 5 |
| `/use-cases/social/youtube-video` | `youtube.video.detail` | 5 |
| `/use-cases/social/youtube-search` | `youtube.search.videos` | 5 |
| `/use-cases/social/youtube-comments` | `youtube.video.comments` | 5 |

(`account.usage`, 21 providers, is excluded — it is plumbing, not a job anyone searches.)

Not built, on purpose: a page per endpoint (2,791); "find influencers" (no capability yet);
anything whose only provider is one vendor (their own site owns that query).

## Page type E — API pricing & comparison · `/pricing/<platform>-api` · 47 pages

Jason's call (2026-08-21): a real comparison page, not a retitled shelf. `/catalog/<slug>` stays the
app's comparison UI and gets a link to this page; this page is server-rendered, editorial, and is
the one page only treg.to can write — we have run every provider.

Demand (measured): `twitter api pricing` 720 · `reddit api pricing` 480 at $0 CPC · `linkedin api
pricing` (the shelf already gets impressions for it at pos 86) · `youtube api key` 1,900 ·
`google trends api` 1,600 · `instagram scraper` 1,000 · `linkedin scraper` 1,000. Blotato's
`/blog/<platform>-api-pricing` posts are the precedent in category.

| Element | Content | Source |
|---|---|---|
| Title | `LinkedIn API pricing (2026) — 8 providers compared per call, plus the official API \| treg.to` | catalog; year computed |
| Meta | `What a LinkedIn profile, company page or post lookup actually costs across {providers}, normalised to per-result, with what each one is good at and which inputs it needs. Verified {latest verified date}.` | catalog |
| H1 | `LinkedIn API pricing: 8 providers compared` | — |
| **The answer first** | three computed sentences: cheapest per result for the most-used job; the official API's real cost (free, but needs an approved app / own account — e.g. LinkedIn official endpoints are own-key only); the best-value pick given measured success (if approved) | catalog |
| **Price table, normalised** | provider × job (capability) grid; every cell shows the list unit (`$0.0008/result`, `$0.025/success`, `$0.09/call`) **and** the normalised cost for a standard task ("100 profiles" / "1,000 keywords"). Unit normalisation is the editorial value — per-call with 1,000 keywords in the body is not comparable to per-result until someone does the arithmetic | `cost` + a per-capability "standard task" constant in the slug map |
| **Which one for what** | one row per provider, generated from facts: inputs accepted (URL / email / name + company), which jobs it covers on this platform, verified date, rate limits, whether it is the official API, and — if approved — observed success and p50 with the caveat. Then one hand-written sentence per provider per platform, kept in the slug map, reviewed when the catalog changes ("TikHub is the only one returning post reactions; Bright Data is slowest but the only one that takes a Sales Navigator URL"). The hand-written sentence is the part Ray's clone test cares about | catalog + hand-kept notes |
| **The official API** | its real terms: free tier, approval process, what it will not give you (LinkedIn: nothing about other members), and how it runs on treg with the team's own key, unmetered | catalog `tier`/`scope` + hand-kept |
| **Hidden costs** | what the list price omits: minimums, monthly plans, failed-call billing (per-success vs per-call), rate limits — each as a fact per provider from the catalog `status_note`/limits | catalog |
| One call each way | the cheapest verified endpoint and the official one, as `treg call …` | `test_request` |
| FAQ (visible + `FAQPage`) | "How much does the LinkedIn API cost?" · "Is there a free LinkedIn API?" · "What is the cheapest way to get LinkedIn profile data?" · "Do I need a LinkedIn developer account?" — answered from the numbers on the page | — |
| JSON-LD | `FAQPage` + `BreadcrumbList` + `Table`-free `ItemList` of providers (`Offer` only where a single price is unambiguous — otherwise no `Offer`, because schema must match visible text) | — |
| Links | up: `/mcp/<platform>`, `/catalog/<slug>`, agent hubs; down: every spoke on this platform; across: `/integrations/<provider>` | — |
| Freshness | "Prices verified {date}" from the newest `verified` on the page; the sitemap `lastmod` follows the catalog mtime. Never date-bumped | catalog |
| Index | yes, all 47 platforms with ≥2 providers; single-provider platforms get no page (the vendor owns that query) | — |

Honesty constraints (wiki §8): name the cheapest **with caveats by input, quality, latency and
volume**; observed stats are "measured on treg.to traffic, not a controlled benchmark"; treg
compares and does not route — "choose well, then switch without a new account" is the CTA. A
controlled benchmark with published methodology is a separate later page (`/benchmarks/…`), not
this one.

First five: `linkedin-api`, `reddit-api`, `twitter-api` (platform `x`), `youtube-api`,
`instagram-api` — the measured terms. Then `google-serp-api` (`serp api` 12,100/mo is brand-owned
by SerpApi, but `cheapest serp api` 390/mo is beatable per the wiki), `tiktok-api`, `people-data-api`,
`company-data-api`, `backlink-api`.

## Page type F — provider · `/integrations/<provider>` · ~47 pages

Per wiki §8. Activation pages; their job is "does treg.to work with DataForSEO, and how", not rank.

Title `DataForSEO on treg.to — 61 endpoints, call them with no DataForSEO account`; sections:
what's covered (capabilities by platform), prices as treg charges them, the own-key path
(`treg connections connect --provider dataforseo`, never metered), limits from the catalog, one
runnable call, link to the vendor's own docs. `index` yes — we are not competing for the vendor's
name, we are answering "does it work with".

---

## Build order

| Step | Pages | Work | Verdict checked |
|---|---|---|---|
| 1 | `/apps/chatgpt`, `/apps/claude`, then the other 8 from the shared agent list | new route `apps_page()`, shared agent JSON, install blocks, five prompts, screenshots; sitemap rows; tests | indexed in 14 days; `chatgpt`/`claude` + "treg.to" in GSC |
| 2 | `/mcp/linkedin`, `/mcp/reddit`, `/mcp/youtube` | new route `mcp_page()`, short form; shared providers/jobs block | day 30: impressions for `<platform> mcp server`; if none, drop B |
| 3 | first 24 spokes (type D) | route `use_case_job_page()`, slug map, index-rule function, sitemap conditional | day 30: which spokes get impressions at all |
| 4 | `/pricing/linkedin-api`, `/pricing/reddit-api`, `/pricing/twitter-api` (E); append generated lists to the 5 category hubs (C) | route `pricing_page()`, unit-normalisation constants, hand-written provider sentences for 3 platforms | day 30: impressions for `<platform> api pricing`; `/catalog/linkedin` (linked from it) moves from pos 74 |
| 5 | submit to mcpservers.org, PulseMCP, Glama, Smithery, Docker MCP, Anthropic directory; point each listing and each catalog YAML at its page | outside the repo | referring domains in GSC |
| 6 | remaining spokes and pricing pages, `/integrations/<provider>` | only if steps 3–4 show movement | — |

Prerequisites before step 1 ships: Jason's call on publishing observed stats per vendor; top up the
team balance for the measurement passes; `first_call_at` attribution so step 5 and the day-60 verdict
can be read on first calls rather than signups.

## Files touched

- `src/treg/api.py` — routes `apps_page`, `mcp_page`, `use_case_job_page`, `integration_page`; a
  shared `_providers_block(platform|capability)` used by B, D, E; index-rule helper; `_SITEMAP_PAGES`
  spreading all four maps.
- `src/treg/catalog/usecase_slugs.yaml` (new) — `capability → slug, category, sentence`.
- `src/treg/web/pseo.css` (new) — one skin for A/B/D/F, tokens lifted from `landing.html`.
- `src/treg/web/media/install/chatgpt-plugins.png` etc. — the screenshots.
- `tests/test_seo.py` — every sitemap URL 200s; every FAQ question appears in the body; no page
  carries a number absent from the catalog; noindex spokes are absent from the sitemap.
- `docs/context/interface/seo.md` — updated in the same commit.
- `src/treg/web/llms.txt`, `skill.md` — link the agent pages.

---

## v3 — decisions after independent review (Codex, 2026-08-21; full text in `_review-codex-seo-plan.md`)

The entity hierarchy stands. The launch inventory, the index gate, the URL namespace and the
verdict metric change. Footprint before review: 10 + 5 + 47 + ~150 + 3 + ~47, on top of 81 indexed
shelves = **343** indexable entity pages. Footprint after: **~45 new indexed pages, one complete
cluster first.**

| Decision | Was | Now | Why |
|---|---|---|---|
| Agent hubs (A) | 10 indexed | **4 indexed** (ChatGPT, Claude.ai, Claude Code, Cursor — each has a distinct native install path or measured adjacent demand) + a crawlable `/apps` directory listing all ten + 6 `noindex` setup pages | ten pages whose main instruction is the same setup line are not ten search intents |
| MCP pages (B) | `/mcp/<platform>` ×3 | **`/mcp-servers/<platform>`** ×3 | `/mcp` is the mounted MCP transport and `robots.txt` Disallows it; never share the protocol namespace |
| Category hubs (C) | spokes nest under `/use-cases/seo` etc. | **one shared route map**; the five ad slugs (`/use-cases/seo-data-for-ai-agents`) stay canonical, spokes nest under them; no parent URL that does not exist | the parents the spec pointed at were never routed |
| Spokes (D) | ~150 indexed automatically; gate = ≥2 providers + verified + ≥1 observed call in 30d, recomputed per sitemap render | **first 24, human-reviewed**; gate = substitutable providers + verified + complete price/input/test/provenance + (external demand evidence **or** repeated successful use across >1 customer) + **approval into a deploy-time index manifest**; usage *nominates*, never flips index state; hysteresis, no rolling-window churn | one call is a test call; a sitemap must not need live `CallRecord` aggregates (`endpoint_stats.observed()` is designed for small sibling sets) |
| Pricing (E) | 47 | **5** (linkedin, reddit, twitter, youtube, instagram); the rest stay on `/catalog/<slug>` | the hand-written provider judgments are a research program, not a template field |
| E content | price grid + sentences | add: **published normalisation methodology** (batch size, pagination, caps, minimums, prepaid/overage, retries, failed-call billing), **per-fact provenance + change log**, **three task sizes** not one constant, comparability beyond price (inputs, returned fields, freshness, geo, limits, access constraints), disclosure of hosted-vs-own-key charging, a named maintenance owner | without these E is a catalog table with prose, not a decision tool |
| Catalog shelves | untouched | where a pricing page exists, **retitle the shelf away from pricing intent** and link to it; explicit canonical policy across `/catalog/<p>`, `/pricing/<p>-api`, `/mcp-servers/<p>` | cannibalisation |
| Provider pages (F) | 47 indexed | index only those with a verified integration, real setup/limits content and a runnable call | activation docs, not a rollout |
| Parent directories | sitemap only | server-rendered `/apps`, `/pricing`, `/integrations`, `/use-cases` index pages | a sitemap is not a crawl path |
| `_page()` | as is | gains a `robots` argument (`noindex,follow`), a stylesheet argument, and context-preserving install CTAs instead of "Start free → /app" | the shell cannot currently render a noindex spoke |
| Self-hosting | "treg.to", the ChatGPT plugin, hosted keys and the $1 grant hardcoded | **pSEO routes gated to the hosted deployment** (or host-derived copy) | every one of those statements is false on a self-hosted registry |
| Where copy lives | `api.py` | routing and data authority in `api.py`; editorial copy, slug map, provider sentences and the index manifest as declarative data files | `api.py` is already 10k+ lines |
| Tests | "walks every sitemap URL" | `test_seo.py` samples 5 shelves today; new families need their own classification (walk all reviewed spokes, sample nothing) | the spec overstated the existing test |
| `llms.txt` / `skill.md` | link the agent pages | link only after the routes ship | do not document what is not built |

### Build order, v3 — one complete cluster, then a control

1. **Foundations:** canonical/category URL map + redirects; `_page()` robots/stylesheet args; the
   index manifest format; **organic landing-page → first call → week-two reuse attribution**
   (`first_call_at` exists; the page attribution does not).
2. **The LinkedIn cluster, complete:** `/apps/chatgpt` + `/apps/claude` (+ `/apps` directory),
   `/mcp-servers/linkedin`, `/pricing/linkedin-api` with the full methodology, the 3–5 strongest
   LinkedIn spokes (profile, company page, post + comments, jobs search), and only the complete
   provider pages for its 8 providers.
3. **Submit the live cluster to directories the same week** (mcpservers.org, PulseMCP, Glama,
   Smithery, Docker MCP, Anthropic) — not after four internal steps.
4. **One non-social control cluster** — people/email (`/pricing/people-data-api`, the
   lead-enrichment spokes) — so the verdict is not confounded by LinkedIn alone.
5. Expand **the page type that proves both query match and repeated calls**. Not the others.

### Verdicts, v3

- **Day 14:** crawl and index coverage.
- **Day 30:** qualified impressions for the intended query families, impression trajectory vs the
  existing shelf, install/connect starts, first calls. **Not position.** Pause expansion on zero
  qualified impressions across the test set; do not delete B at day 30.
- **Day 60–90:** position and clicks, with external links in place.
- **Day 60/120:** repeated successful calls by landing page — the business verdict.

## v4 — reader-first simplification (Jason, 2026-08-21)

Two questions settled:

**Agent pages list every use case, categorized.** That is the page's value: "I use ChatGPT — what
can I now do?" answered with the whole list (Sales & people · SEO & search · LinkedIn / Instagram /
TikTok / X / Reddit / YouTube · Companies · Ads · Web), each row a job with its price and the prompt
to paste into *that* client. The four pages share the list; they differ in install and in how a row
is run there. A long, accurate, actionable list is not thin content.

**Provider pages (F) are dropped.** Provider detail lives as an expandable row on every pricing page
the provider appears on — inputs accepted, returned fields, limits, verified date, own-key path, docs
link. "Does treg.to work with DataForSEO" is answered by the catalog's provider view; the vendor's
name SERP stays the vendor's. One page type fewer, nothing a reader loses.

The plan in reader terms — three questions, three page types, plus one experiment:

| Reader asks | Page |
|---|---|
| I use ChatGPT / Claude / Claude Code / Cursor — what can I do now? | `/apps/<agent>` ×4 |
| What does LinkedIn / Reddit / X / YouTube / Instagram data cost, which provider, why? | `/pricing/<platform>-api` ×5 |
| How do I do this one job? | `/use-cases/<category>/<job>` ×24 reviewed |
| (test) `<platform> mcp server` | `/mcp-servers/<platform>` ×3 |
