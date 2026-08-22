# treg.to programmatic pages — the idea behind Pipedream, measured, and treg's version

Written 2026-08-21. Sources: DataForSEO ranked-keyword pull for pipedream.com (4,000 US keywords,
four calls), live SERPs for treg's target terms (ScrapeCreators, own key), blotato.com's sitemap,
Jason's wiki (`wiki/topics/treg.to SEO.md` §8 is the operative position, `Blotato growth
teardown.md`, `SEO.md`, `Distribution.md`), treg.to Search Console 2026-07-21→08-18, and the
catalog itself (`catalog_store.load()`).

---

## 1. What Pipedream's pages actually earn (US, DataForSEO ETV, Aug 2026)

| URL pattern | est. visits/mo | share | keywords ranked | in top 10 |
|---|---|---|---|---|
| `/` (brand: "pipedream", "pipedream meaning", "pipe dream") | 15,700 | 67% | 146 | 21 |
| `mcp.pipedream.com/app/<app>` | 3,980 (≈580 without one outlier*) | 17% | 345 | **84** |
| `/apps/<a>/integrations/<b>` | 850 | 3.6% | **1,619** | 49 |
| `/apps/<a>` | 630 | 2.7% | 589 | 53 |
| `/community` | 610 | 2.6% | 596 | 103 |
| `/blog` | 580 | 2.5% | 68 | 31 |
| `/apps/<a>/(triggers\|actions)/<x>` | 370 | 1.6% | 398 | 48 |
| `/vs/<competitor>` | 15 | 0.1% | 2 | 1 |

\* "e conomic" (a Danish accounting brand, 301k/mo, position 10) alone is 3,400 of it.

What this says, and it is not what the page *looks* like it says:

1. **"Lots of traffic" is mostly brand.** 68% of Pipedream's US organic is people typing its name.
   Non-brand organic is ~4,000 visits/month for a company with 3,000 apps and a million users.
2. **The app×app pages are a breadth machine, not a volume machine.** 2,600 ranked keywords across
   the three `/apps` patterns, ~1,850 visits. Under one visit per keyword per month. The value is the
   long tail *and the internal-link graph that gets the app hubs crawled*, not any page's traffic.
3. **The newest, thinnest surface earns the most non-brand traffic.** `mcp.pipedream.com/app/linear`
   is a title, an H1 ("Linear MCP Server"), two install steps, no H2s, no internal links, no schema —
   and it holds top-5 for "quickbooks mcp", "plaid mcp server", "pipedrive mcp", "sendgrid mcp",
   "typeform mcp", 84 top-10 positions in all. That is the first law of the seo-growth playbook
   in one table: **a low-effort page wins while the term is young and the SERP is thin**. The
   integrations pages had years and authority and rank 11–50.
4. **Comparison pages are nothing.** `/vs/zapier` = 15 visits. The footer column in the screenshot
   is there for crawl paths and positioning, not traffic.
5. **What each row on their page is, is a CTA.** The product surface is the SEO surface — that is
   the part worth copying, and it is a product decision, not a content one.

## 1b. The mechanism behind Pipedream's pages (full-site crawl, Aug 2026)

Counts come from their open-source registry (`PipedreamHQ/pipedream/components/`: 3,397 apps, 4,626
triggers, 11,681 actions), because **`/sitemap.xml` lists only 11 hub URLs** — none of the
programmatic pages are in any sitemap. The query-intent ladder:

| Level | URL | Title = the query | Count |
|---|---|---|---|
| app | `/apps/notion` | "Notion API Integrations" | ~3,000 |
| pair | `/apps/notion/integrations/slack-v2` | "Integrate the Notion API with the Slack API" | any ordered pair, generated on request (≈9M possible) |
| component | `/apps/notion/triggers/new-page` | component `name` verbatim | ~16,300 |
| workflow | `…/create-a-page-with-notion-api-on-new-gist-from-github-api-int_…` | "{action} with {B} API on {trigger} from {A} API" | every trigger×action (≈54M possible) |
| hand-written | `/vs/zapier` ×5, `/templates` ×40 | — | already stale ("2,200+ apps" vs "3,000+" on generated pages) |

The eight principles, and treg's counterpart to each:

1. **The data is the content; the page is a view.** Every page is a function of the registry
   (name, description, version, README, OAuth config). Contributors write code; pages appear.
   *treg:* the catalog YAML is that registry. `/catalog/<slug>` already renders from it; the new
   pages must too — no page may carry a number or a name the catalog doesn't.
2. **Titles are literal queries, composed from entity names.** Each level targets a different query
   shape. *treg:* `<platform> api pricing` (shelf) · `<platform> mcp server` (mcp) · `<agent>` +
   "connectors"/"mcp" long tail (agent) · the job in plain words (capability, noindex until proven).
3. **Unbounded tail, concentrated head.** Pairs exist for any combination, but every page links the
   same usage-ranked top-24 apps and the footer's 8 money apps. *treg:* we have the usage data —
   top 10 endpoints = 49% of 25k monthly calls (Google SERP 30%, X 17%, people enrichment 15%).
   Put the top-used platforms in every page's footer; that is where authority should pool.
4. **Two-hop crawl path to every entity** via an unpaginated A–Z directory. *treg:* `/catalog`
   already lists all 81 shelves; keep it unpaginated and server-rendered.
5. **Every page is a funnel.** "TRY IT" / "USE THIS ACTION" deep-links into the builder with the
   component preselected. *treg:* every row opens connect/install for the visitor's agent; the
   `/apps/<agent>` page starts the OAuth or Plugins install from a button.
6. **Thin-page risk is offset by unique machine data** — full source, version, key, doc link per
   component; changes with every PR. *treg:* price per call, params, `test_request`, verified date,
   observed success/latency. More unique data per page than Pipedream has, not less.
7. **The open-source registry is the content pipeline and the backlink engine.** Every npm package
   has `"homepage": "https://pipedream.com/apps/<app>"`; every page says "View on GitHub"; 5.7k forks.
   *treg:* the repo is public (423 stars). Each catalog YAML and each MCP-directory / ChatGPT-plugin
   listing should point at its page; the directories that *are* the SERP for `<platform> mcp server`
   double as the inbound links.
8. **Structure beats polish.** No canonicals, raw markdown in meta descriptions, self-pairs,
   JS-only "Load more", a Vue SPA that only Googlebot renders — and it still works. What does not
   transfer: AI crawlers don't render JS, so treg's pages must be server-rendered (wiki §8 agrees).

The one lesson *not* to take: Pipedream's pair and workflow pages are indexed far past any demand
("create sitemap with WebScraper.IO on new transaction from YNAB"). They can afford it on their
authority; a 2026 domain with 41 clicks cannot. Generate the graph, index the measured nodes.

## 2. The precedent in treg's own category: blotato.com

The wiki's teardown already covers why it works (561 referring domains beating Buffer's 99k on the
queries that matter; `/tools/*` out-earning 162 blog posts; 81 third-party n8n templates as the
real wedge). The sitemap, pulled today, is the structure Jason sketched, already shipped:

```
/ai-agent/<agent>        16: chatgpt, claude, claude-code, codex, cursor, gemini, windsurf, perplexity, …
/mcp/<platform>           9: instagram, linkedin, tiktok, x, youtube, threads, facebook, bluesky, pinterest
/tools/<free-tool>       12: social-media-api-cost-calculator, best-time-to-post, …
/blog/how-to-post-to-<platform>-with-claude        (one per platform — the "how to" lives in blog)
/blog/<platform>-api-pricing                        (one per platform — the buyer's own words)
/blotato-alternatives                                (one hub, 18-tool table, "Verified August 2026")
```

Three levels, not four. The job ("post") is fixed by the product, so the how-to is `platform ×
agent`, never `agent × category × job`. The pricing posts are the *same* platforms again under the
other phrasing buyers use.

## 3. What treg.to's own data adds

- **Search Console (28d):** 41 clicks, 35 brand. 24 `/catalog/<slug>` shelves get impressions at
  0 clicks, already matched to `linkedin api pricing` (pos 86), `serpapi`, `/catalog/linkedin` pos 74.
  Indexed, aimed right, too thin. Cold start (Playbook A) on every cluster.
- **Demand measured 2026-08-21 (US/mo):** `claude connectors` 2,900 · `claude mcp servers` 1,600 ·
  `firecrawl mcp` 1,300 · `chatgpt connectors` 1,000 · `chatgpt mcp` 1,000 · `claude code mcp
  servers` 880 · `cursor mcp servers` 720 · `linkedin mcp server` 480 · `apify mcp` 480 · `chatgpt
  for seo` 480 · `ahrefs mcp` / `semrush mcp` 390 · `dataforseo mcp` 210 · `reddit mcp server` 210 ·
  `youtube mcp server` 140 · `how to find instagram influencers` 170.
  **Dead (0–20/mo, 30+ phrasings tried):** every "how to <job> with chatgpt/claude" — sdr, find
  email, lead list, schedule instagram, instagram analytics, rank tracking, serp analysis, keyword
  research. People don't search how to do a job *in* ChatGPT; they open ChatGPT. They search for the
  **connector by name**, or the **job by its own name**.
- **Live SERPs:** `linkedin mcp server` / `reddit mcp server` → GitHub repos, mcpservers.org,
  PulseMCP, Docker Hub, Apify. Thin, winnable, and those directories are themselves places treg
  should be listed. `dataforseo mcp` → 6 of 8 are dataforseo.com (vendor owns its name; wiki §3
  already says don't fight it). `claude connectors` / `chatgpt connectors` → Anthropic/OpenAI docs
  plus big listicles (Composio, Forte Labs). The hub term is not winnable head-on; the long tail
  under it is.
- **Catalog shape:** 81 platforms, 47 of them with ≥2 providers (linkedin 8, google 8, youtube 7,
  instagram 6, tiktok 5, x 5, reddit 4). 1,034 capabilities, 201 with ≥2 providers, 72 with ≥3.
- **Product facts that are now true:** treg is a public plugin in ChatGPT's directory (search
  "treg" → Install, verified by Jason 2026-08-20); OAuth MCP connector for Claude; `treg mcp
  install` for Claude Code / Cursor / opencode.

## 4. What the wiki already decided (do not relitigate)

- `treg.to` in every public title/heading/anchor, never bare `treg` (immunology SERP).
- **No programmatic page per endpoint** — "the single most obvious idea here and the one most
  likely to earn a site-wide penalty" (`treg.to SEO.md`, What not to do).
- No "best X" / "X alternatives" listicles; no blog cadence.
- §8 operative URL unit: `/integrations/<provider>/` first, `/use-cases/<job>/` second; capability
  URLs **noindex** until demand, comparability and differentiated content are all proven.
- Vendor-name SERPs (`semrush mcp`) are lost, but an integration page with install steps, runnable
  calls, pricing and limits "serves activation and agent retrieval, which is not the job the SERP
  was being asked to do" — judge it on first calls, not rank.
- Score evergreen pages on day-14/30 indexation and impressions, then day-60/120 on assisted
  signups; never the day-7 gate. The primary growth unit is **successful repeated calls**; the
  install → auth → first call → week-two funnel is still uninstrumented.
- Any claim of a number comes from the catalog at build time; observed success/latency stats carry
  the "not a fair benchmark" caveat; printing reliability numbers for named vendors is Jason's call.
- Pages must be server-rendered: GPTBot/ClaudeBot/PerplexityBot do not run the Vue SPA.

## 5. The structure

The principle, stated once: **treg's entity graph is agent × platform × provider, and a page exists
for a node only where someone types that node's name.** Nobody types the job with an agent prefix;
everybody types the platform with "mcp", "api pricing" or "scraper", and the agent with
"connectors" / "mcp servers". So:

```
/apps/<agent>                         4 pages    chatgpt · claude · claude-code · cursor
/mcp/<platform>                      ~25 pages   every platform with ≥2 providers AND a measured "<platform> mcp" term
/integrations/<provider>             ~47 pages   per wiki §8 — activation pages, rank is not their job
/catalog/<slug>                       81 pages   exists; becomes the "<platform> api pricing" page
/use-cases/<job>                     ~201        the spoke layer: capability as a job sentence, providers compared, agent tabs; indexed when ≥2 providers AND observed calls
/tools/<free-tool>                    a few      api-cost-calculator style, per Blotato's /tools/* result
```

### `/apps/<agent>` — "treg.to in ChatGPT / Claude / Claude Code / Cursor"
Targets `chatgpt connectors`-family long tail ("chatgpt connectors for seo", "claude mcp server for
linkedin") rather than the head term. Content that is genuinely per-agent: the install path for that
client (ChatGPT: Plugins → search treg → Install, with the screenshot; Claude: OAuth connect; Claude
Code/Cursor: one command), five pasteable prompts that each do a real job end to end, and the list
of platforms as rows linking to `/mcp/<platform>`. The page **starts** the connect flow from a
button — the Pipedream lesson that the product surface is the SEO surface. Four pages, hand-written
above the fold, templated below it.

### `/mcp/<platform>` — "LinkedIn MCP server — 8 providers, from $0.002/call, works in ChatGPT, Claude, Cursor"
The leaf that matches the measured demand and the thin SERPs. Per page: what the agent can now do on
that platform (the capability list in plain words — "find influencers" appears *here*, as a row,
linked to the job search in the product), the provider comparison (price, verified, observed
success/latency with caveat), install tabs per agent, one runnable call. Title carries the
platform's *own* name — `linkedin mcp server`, not "treg.to linkedin". Build only for platforms with
a measured term; the remaining shelves stay on `/catalog/<slug>`.

### `/catalog/<slug>` — becomes the "api pricing" page
Already indexed, already matched to `linkedin api pricing`. Retitle to the buyer's phrasing
("LinkedIn API pricing — 8 providers compared, per-call"), move the prerender from a list to a
comparison table, add the `treg call` sample. No new URLs; this is the metadata-only fix the wiki
calls the fastest ROI on an existing site.

### `/use-cases/<job>` — the spoke layer (revised 2026-08-21 after Jason's push-back)
Pipedream's zero-traffic pages are not waste: they are spokes. Each one carries the hub's name in
its breadcrumb, H1 and parent link (anchor text and entity coverage for `/apps/<app>`, which holds
53 top-10 positions), each is an entry point for an external link (a GitHub issue or forum answer
links to the specific action page, not the hub), and their long tail sums (1,619 keywords → 850
visits). Their best-ranking leaf is the action page whose H1 is the job in plain words
("find social media accounts by email" → the Proxycurl action page).

treg's equivalent is the **capability worded as a job**: 1,034 capabilities, 201 with ≥2 providers.
"Find someone's work email from a LinkedIn URL — 3 providers, from $0.009/success." The provider
comparison is the unique content an endpoint page would never have. Per-agent install is **tabs on
the page**, one URL — never `/apps/<agent>/<job>` ×4.

Jason's categories (sales, instagram, seo…) are **section headings on the hubs**, not a URL level —
as Pipedream uses "Popular triggers / Popular actions" rather than `/apps/notion/triggers/` as a page.
The how-to is the use-case page; the agent is a tab on it.

**Index gate — usage, not search demand.** Wiki §8 says capability URLs stay noindex until search
demand is proven; a noindex page eventually stops passing link signals, so it is not a spoke. Change
the test: a use case with **≥2 providers and real observed calls** (the `observed` block; 485
endpoints had calls last month) is indexed. The rest render for agents and the internal graph and
stay out of the index until they earn it. Expected indexed set: ~100–150 use-case pages, each with
something a competitor cannot regenerate — not 2,791 that don't. Generate the whole graph, index
the used nodes, let the hubs collect the anchor text.

The one measured how-to the catalog cannot serve today: `how to find instagram influencers`
(170/mo) — influencer discovery by niche is not in the catalog; that page waits on the addition.

### What is deliberately absent
Per-endpoint pages (2,791 — banned by the wiki and by the penalty data). Provider×provider or
agent×category×job trees. `/vs/` and "alternatives" (15 visits/mo for Pipedream; declining terms;
excludes the citing brand from AI recommendations). Hub terms `claude connectors` / `chatgpt
connectors` as page titles — owned by Anthropic/OpenAI.

## 6. Order and gates

1. **`/apps/chatgpt` + `/apps/claude`** (product-led; the plugin listing is new and real) and
   **`/mcp/linkedin`** (480/mo, SERP of GitHub repos, shelf already has impressions). Three pages.
2. Retitle the 81 `/catalog/<slug>` pages to the pricing phrasing — one template edit.
3. Submit treg to the directories that *are* the SERP: mcpservers.org, PulseMCP, Glama, Smithery,
   Docker MCP hub, Anthropic's connector directory. Blotato's lesson: the win is being listed where
   buyers already look, and those listings are also the backlinks the pages need.
4. Day 14/30: indexation and impressions per page; did `/mcp/linkedin` enter the top 30 for
   `linkedin mcp server`, did `/catalog/linkedin` move from 74. If yes, roll `/mcp/<platform>` to
   the other ~24 measured platforms in one release. If no, more pages will not fix it.
5. Day 60/120: assisted signups and first calls by landing page — which needs `first_call_at`
   attribution, the same blocker as `marketing/landing/_measurement.md`.

## 7. Open questions for Jason

- Render: server-side `_page()` for all of these (the wiki's AI-crawler point makes this close to
  mandatory), with the comparison UI embedded rather than the SPA.
- Whether observed success/latency may appear on public per-vendor pages with the caveat, or only
  prices. Wiki says this is Jason's call.
- Whether the catalog should add influencer discovery (the one how-to with measured demand that treg
  cannot honestly serve today).
- `treg` team balance is negative (−$0.11) after today's DataForSEO pulls; top up before the next
  measurement pass.
