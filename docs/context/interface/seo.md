---
title: Search surfaces — robots, sitemap, the crawlable catalog, and the social card
status: shipped
sources:
  - src/treg/api.py
  - src/treg/agent_pages.py
  - src/treg/web/robots.txt
  - src/treg/web/catalog.css
  - src/treg/web/index.html
  - src/treg/web/landing.html
  - src/treg/web/support.html
  - assets/brand/og-card.html
related:
  - interface/api.md
  - interface/dashboard.md
  - architecture/catalog.md
---

# Search surfaces

Everything a crawler, a link unfurler or an AI answer engine sees. It is one subsystem because the
pieces only work together: a sitemap is worthless without pages to list, and pages are worthless if
`HEAD` 405s before the crawl starts.

## The problem this fixed

The catalog — ~2,630 endpoints across 80 platform shelves, the entire substance of the product — had
**no URLs**. The dashboard browses platforms through hash routes (`/app#platform/<slug>`) behind a
login, and individual endpoints were expandable rows with no address at all. A crawler could reach
six thin marketing pages and nothing else. On top of that: no `robots.txt`, no `sitemap.xml`, `HEAD`
answering 405 everywhere, no `og:`/`twitter:` tags or image, no structured data, and `/docs` serving
FastAPI's stock Swagger shell — a kilobyte of JavaScript to anything that does not run scripts.

## The pieces

| Path | What it is |
|---|---|
| `/robots.txt` | Bundled file, `{BASE}`-templated. Disallows `/app`, `/login`, auth and OAuth flows, `/call/`, `/mcp`, `/admin`, `/docs/api`. Names the sitemap. |
| `/sitemap.xml` | **Generated**, not bundled — 80 of its URLs come from the catalog. Static pages take `lastmod` from their file's mtime, shelves from the newest mtime under `src/treg/catalog/`. |
| `/resources` + `/use-cases/<slug>` | The outcome pages and their hub. Their sitemap rows are spread from `_USE_CASES` rather than listed by hand, so routing a new page lists it — see below. |
| `/catalog` | The dashboard SPA, in public mode — the marketplace's Catalog view on an indexable URL. |
| `/catalog/<slug>` | The same SPA, on the platform view for one shelf. |
| `/docs` | Server-rendered API reference built from `app.openapi()`. |
| `/docs/api` | FastAPI's Swagger UI, moved here and `Disallow`ed. ReDoc is off. |
| `/media/og.png` | The 1200×630 social card, served by the pre-existing `/media` mount. |
| `/catalog.css` | Skin for `/docs`. The catalog URLs need none — they ship the dashboard's own stylesheet, because they ship the dashboard. |

`_page()` in `api.py` is the shell for the standalone server-rendered pages (`/docs`) — it owns
`<title>`, the meta description, the canonical, the og/twitter card and the JSON-LD, so a new page
cannot ship missing them. That omission is exactly what left the landing bare.

## The public catalog is the marketplace, not a copy of it

The first cut of this hand-built `/catalog` pages in Python string templates. They shared the API
functions but not the UI, and it showed immediately: the app renders **one row per job with
competing providers merged onto it** (Majestic $0.0008 · Serpstat $0.0025 · SE Ranking $0.018) —
the comparison *is* the product — while the hand-built page listed each endpoint separately. Same
data, different axis, two things to maintain.

So `/catalog` and `/catalog/<slug>` now serve **`index.html`**, and the Vue app renders the same
platform views a member sees. This works because the catalog API is unauthenticated; `publicCatalog`
in `index.html` is the flag, set from `catalogFromPath()` before the `/auth/me` check so the first
paint is already in public mode.

What public mode changes, and why each one:

| Hidden / swapped | Because |
|---|---|
| The app top bar (global tool search) and the whole sidebar → a marketing-style `.pubnav` | a catalog visitor is reading a **website**, not operating an app; the workspace chrome is furniture for a job they have not started. `.layout.solo` gives the main column the full width |
| Org switcher, Getting started, Your vault, Activity, Team | every one needs a session |
| The "not connected" badge on all 80 tiles | connection state is a member fact; publicly it is 80 red herrings |
| Try-it, Connect, BYOK, the provider chips, "Start free" | all open the **sign-in modal in place** |
| Modal subtitle | the default is the sandbox's ("bring it into a real account") — nonsense to someone who arrived from a search result |

**Never point a public CTA at `/app`.** A logged-out visit to `/app` hits
`location.replace('/')` in the boot and lands on the marketing landing with no modal open — so the
CTA loses the page the visitor was reading and offers them nothing. That was the first cut and it
was a dead end at every one of six call sites. They call `openSignin()` instead.

For that to work the sign-in modal had to move: it lived **inside** the logged-out landing branch,
which public mode does not render. It is now a sibling of both branches, near the end of `#app`.
`.lc-scrim` is `position:fixed` and unscoped, so the move needed no CSS change.

`platProviders`, `provName` and `mkKnown` fall back to the open catalog response's own `providers`
map, since their normal source (`/connections`) needs a session. Without the fallback every provider
on a public shelf renders as its bare slug and the whole action chain collapses to nothing.
`mkOauth` has no public fallback — the open response carries no `auth_kind` — so the public branch
offers BYOK, which is true for every provider, rather than guessing Connect.

**Each action is ONE button whose handler forks on `publicCatalog`**, not a duplicated public
template. `tests/test_dashboard_markup.py` asserts the member chain's exact shape
(`v-else-if="mkOauth(e.provider)" class="btn sm primary"`, `openProvider(e.provider)`, …), and a
fork keeps those substrings intact where a parallel branch drifts. That test reads a fixed-size
window of the markup and has already been outgrown once by these forks.

### The no-JS fallback

Vue compiles `#app`'s own innerHTML as its template, so prerendered markup **cannot go inside it**.
`#prerender` is a sibling, removed by the app on boot.

It is deliberately plainer than the Vue view. The ledger's row-merging is a chain of client-side
computeds (`platRowsAll` → `platRowsPreDomain` → `platLedger`), and reproducing that server-side
would recreate exactly the duplicate implementation this whole design removes. The fallback carries
the **text** — names, summaries, providers, prices — which is what a crawler that runs no scripts is
here for. Google executes JS and sees the real view; the ones that don't still get the content.

`/catalog/<slug>` asks the API for `include_hidden=1`, matching what the SPA asks for in
`loadPlatform`. Requesting a different population than the view about to replace it would put two
different endpoint counts on one URL.

## Adding a platform, changing the UI

**New catalog data appears on both sides with no code change.** Everything — the app's tile grid,
the shelf pages, the `#prerender` fallback and the sitemap — reads one `catalog_store.load()`. Drop
the YAML in, restart (the catalog is parsed once per process and changes only on deploy), and the
new shelf is live and indexable.

One gate: give the platform a `platforms:` entry in `capabilities.yaml`. Without one,
`catalog_store` auto-registers it as `category: "Other"`, and the dashboard's `platCategories`
skips `Other` outright — so the sitemap would publish `/catalog/<slug>` while the app's own grid
links to nothing. `test_no_shelf_is_published_that_the_app_grid_hides` fails the build if that
happens.

**UI changes to the shared views reach the public pages automatically** — it is the same
`index.html`. Three things do NOT follow along:

1. **Anything reading member-only state.** `providers`, `connCount`, `billing` and `sessionMode` are
   all empty without a session, so a new element built on them renders blank publicly. Three helpers
   already needed public fallbacks for exactly this (`platProviders`, `provName`, `mkKnown`).
2. **New member-only actions leak.** Public mode hides things by naming them; a new button is
   visible to signed-out visitors until it is gated on `authed` or forked on `publicCatalog`.
3. **The `#prerender` fallback and the SEO head are server-built** (`_spa_catalog_page`), so a
   change to what the page *says* needs the fallback and the meta description updated too.

## Things that will bite you

**`{BASE}`, never a hardcoded `treg.to`.** Every page is also served by self-hosted registries. A
hardcoded canonical tells their crawler the real page lives on someone else's domain. `landing()`,
`_legal_page()` and `tutorial_page()` all read-and-substitute for this reason — they were plain
`FileResponse`s before. `tests/test_seo.py` asserts no response body leaks a literal `{BASE}`, and
none leaks a hardcoded host when `public_url` is overridden.

**HEAD is widened after registration, and must not leak into the schema.** FastAPI's `APIRoute` pins
`methods` to `{"GET"}` and never adds HEAD (unlike Starlette's plain `Route`), so every page 405'd on
the probe crawlers send first. One loop at the bottom of `api.py` widens every GET-only route. But
FastAPI derives one operation per (path, method), so that widening put **58 duplicate HEAD entries
into `/openapi.json`**, each with a duplicate operation id. `_openapi_without_head()` narrows the
widened routes for the duration of schema generation and puts them back. Only `/call/{rest}`, which
declares HEAD itself, is documented with one.

**`/catalog/<slug>` sits in front of the JSON routes.** `/catalog/platforms`, `/catalog/search`,
`/catalog/endpoints/…` and `/catalog/examples/…` keep matching only because they are registered
first. `_CATALOG_RESERVED` refuses those names explicitly as a second guard, and the tests assert
the JSON routes still answer `application/json` — if the page route ever swallows one, the dashboard
and every installed CLI break at once.

**Structured data must match the visible page.** Google treats schema claiming something the page
does not say as a violation, not a shortcut. The landing's `Offer` figures ($1.00 free, 0% markup)
are asserted against the rendered HTML, and every FAQ question in `support.html`'s schema is
asserted to appear in its body. Edit one, edit the other, same commit.

**The catalog page and the app must ask for the same population.** See `include_hidden` above.

**A promo banner on `index.html` is a catalog-page edit.** `/catalog` and `/catalog/<slug>` render
from `index.html`, so anything added to that file lands on all ~80 crawlable shelves unless it is
gated. The one banner this app has carried — the Product Hunt launch strip, since removed — sat
inside the Vue app behind `v-if="…&& !publicCatalog"` for exactly this reason; anything similar needs
the same gate, plus a test that the catalog's `#prerender` block never carries it. Note also that
`landing.html` **is** `{BASE}`-substituted
and `index.html` **is not** (`dashboard()` returns a plain `FileResponse`), so a placeholder that is
safe in one half ships literally in the other — hardcode absolute URLs on the app side.

**Prices need `_usd_short`, not `%g`.** `%g` flips to scientific notation below `1e-4`, and a shelf
advertising "from $1.2e-07 per call" reads as a bug. Anything under a hundredth of a cent renders as
`<$0.0001` — which then has to be HTML-escaped at every use site, because that `<` is real markup.

## The outcome pages are listed from the route map, not by hand

`_SITEMAP_PAGES` spreads `_USE_CASES` rather than repeating its five slugs, which is why `_USE_CASES`
is defined **above** the sitemap block instead of next to its route — a page cannot be routed and
then forgotten by the sitemap. Two details are load-bearing:

- **No trailing slash.** `/use-cases/<slug>/` 307s to the bare form, and the comment above
  `_SITEMAP_PAGES` is explicit that listing a redirect is worse than listing nothing. The pages'
  `<link rel="canonical">` matches the bare form for the same reason.
- **The hub is what makes them crawlable at all.** Before `/resources` existed, nothing on the site
  linked to them; the sitemap alone would have been the only path in.

**The sitemap is walked, not spot-checked.** `test_every_sitemap_url_answers_200` fetches what it
publishes. Rename a route and the sitemap silently starts serving 404s to Google with nothing else
failing.

**`catalog.css` is stamped with its mtime.** It is served with a real `max-age`, so without
`?v=<mtime>` an edited skin keeps rendering from the browser's cache — the same trap `/tutorial.js`
already guards.

## The social card

`assets/brand/og-card.html` is the **source**; `src/treg/web/media/og.png` is the render. Open the
HTML at exactly 1200×630 in a headless browser and screenshot it. The provider favicons are fetched
at render time and baked into the PNG, so the shipped card has no runtime network dependency.

Every brand on the card is a real provider — checked against `catalog_store.load()`, after an early
draft showed Ahrefs, which treg does not carry. LinkedIn's mark is inlined because its Google s2
favicon only resolves at 16px and falls back to a generic globe at 64.

Per-platform cards (`/media/og/<slug>.png`) are a deliberate follow-up. Until then every catalog page
points at the shared one.

## The agent pages — `/agents/<agent>`

"I use ChatGPT — what can it do now?" answered on one server-rendered URL per client. The first is
`/agents/chatgpt`; the set is the keys of `agent_pages.AGENTS`, and nothing else (an unknown agent
404s). They came out of the programmatic-SEO plan in `marketing/pseo-build-spec.md`: the measured
demand is for the *agent* ("chatgpt connectors") and the *platform* ("linkedin api pricing"), never
for "how to <job> in chatgpt", so the job list lives on the agent page as rows, not as URLs.

**One skin for both page types.** The agent and use-case pages render with `usecase.css`, the
landing-page skin the five outcome pages already use, passed to `_page(css=...)`: centered hero with
a kicker pill and CTAs, `seclab` labels above each `h2`, real `<table>` comparisons in a `tablewrap`,
the dark `promptbox` with a copy button, `steplabel` numbered steps, `cards` grids, `pricewall` for
the money, and the dark `final` band. The stack of `.ep` rows the first cut used is gone: a job menu
and a provider comparison are tables, and the endpoint inventory belongs on the catalog shelf, which
the pages link to rather than reprint.

**The economics block** (`pricewall`) anchors a per-call price against a subscription: "instead of
$34/mo (Hunter, at list) → you pay $0.89 for 100". Plan prices come only from
`agent_pages.PLAN_PRICES`, which mirrors `marketing/landing/_facts.md` F-20..F-23 and records where
each figure was sourced; a page names a provider's plan price only if it is listed there. With none
listed the anchor falls back to the catalog's own spread (dearest vs cheapest for the same 100
calls), because an invented subscription number is worse than no anchor.

**Two halves, one rule each.** The hand-written half lives in `src/treg/agent_pages.py` — a module
with no heavy imports so it costs the light CLI nothing and can be reviewed without reading routing
code. It holds `ROLES` (the rotating "ChatGPT for *SEO experts / social media managers / SDRs*…"
hero; the first role is server-rendered in the H1 so a crawler reads a full sentence), the install
steps and screenshot, one example prompt per category, the FAQ, and `USE_CASES`: the buyer's menu —
plain-words jobs ("Find professional emails", "Find creators by keyword") under buyer categories
(Data enrichment & sales, Social, SEO, E-commerce, Advertising, Market research), each mapped to
the capability ids that do it. That taxonomy is the map of the whole site: the use-case pages will
hang from the same categories, and a row links to its page once one exists. The route projects
the rest from `catalog_store.load()` per request — the union of providers, the lowest USD price
via `cost_view`, verified counts, one chip per platform — and the counts in the title are computed.
`tests/test_agent_pages.py` asserts every capability id in `USE_CASES` exists in the catalog, so a
job the catalog cannot do cannot be advertised and a renamed capability fails the suite instead of
silently dropping a row. Never a row per endpoint — that is the banned page-per-endpoint in list form.

**Hosted only.** The copy describes treg.to's own listings — the ChatGPT Plugins entry, the $1.00
grant — none of which is true of a self-hosted registry. `_hosted()` checks `public_url` against
`PUBLIC_HOST_ALIASES`; elsewhere the route 404s and the sitemap omits the rows, rather than lie.

**`Disallow: /app` is a prefix rule** and would have blocked `/agents/…` too. `robots.txt` carries an
explicit `Allow: /apps/`; the longer match wins. The test asserts the Allow line exists.

**The shell's CTA now carries `?ref=<page>`.** `_page()`'s "Start free" used to link bare `/app`,
which bounces a logged-out visitor to the landing with nothing open — the dead end this fragment
already documents for the catalog pages. The app boot treats `ref` as a use-case CTA (no bounce,
sign-in opens in place), so every server-rendered page now gets that behaviour and the page that
produced the signup is recorded. Schema on the page: `SoftwareApplication`, `BreadcrumbList`, and
a `FAQPage` whose questions are asserted to appear verbatim in the body.

## The use-case pages — `/use-cases/<category>/<job>`, and the hub at `/use-cases`

The spokes. **The reader does one thing, the prompt; everything else is what the agent sees before
it calls.** Above the fold: the setup line, one prompt with a copy button, four "why this prompt
works" cards, an optional screenshot. Then "Why go through treg.to" (`WHY_TREG`, six cards).

The page then takes one of **three forms, chosen from the catalog rather than by hand** — this is
what makes it a template rather than one page's prose:

| Form | Condition | Renders |
|---|---|---|
| `short` | one provider | "How it works": the one call, on the reader's own account. No comparison |
| `platforms` | the job spans several platforms | providers grouped per platform, cheapest claimed per platform |
| `compare` | several providers, one platform | the full comparison |

Of the 66 jobs on the menu, 19 are single-provider and 19 span several platforms, so two thirds of
the eventual pages are not the plain comparison the first page was built for.

**Cheapest is claimed per billing unit, never overall.** 38 of the 66 jobs mix per-call, per-result
and per-success endpoints, and ranking those by USD per chargeable event names the wrong winner: one
call returning a thousand rows is not dearer than one row. The page prints "cheapest per found",
"cheapest per call" and so on, and says the units are not interchangeable when more than one appears.

**Providers are keyed by (provider, platform), not provider.** ScrapeCreators serves Instagram and
YouTube for the same job; collapsing on provider alone silently dropped a whole platform from a
multi-platform page.

**The reliability section renders only when there is traffic.** `endpoint_stats.observed` is empty
for most endpoints, and an empty promise is worse than no section. When it does render it names
per-vendor success rate, median latency and sample size (publication approved by Jason 2026-08-21)
with the "live traffic, not a controlled benchmark" caveat.

**Nothing job-specific or agent-specific is in the route.** The example client is
`agent_pages.DEFAULT_AGENT`; the job's own words, result noun, "what is X" heading, notes, FAQ and
`voices` all come from `USE_CASE_PAGES`, keyed by `(category slug, job slug)`. A spec's `label` must
match a row of `USE_CASES` exactly (tested), which is how the agent page knows to link the row to
its page. `tests/test_agent_pages.py` asserts the route source contains no job-specific string.

**`voices`** is the section that cannot be regenerated: real questions from Reddit and X, quoted
verbatim with a link, each followed by what the page can honestly do about it (including "no
comparison table can answer this"). The `.agents/skills/treg-page` skill runs that research with
`agent-reach` before any page is written, and documents how to spot the vendor astroturf that
dominates these searches. It is not decoration: on the YouTube pass roughly half the corpus was
vendor-written, thirteen distinguishable clusters, one posting the same body to three subreddits
seven seconds apart. `voices` renders in HTML and in the `.md` mirror, and is optional in the spec
(two of the first seven pages ship without it), so `test_no_use_case_page_ships_with_an_empty_section`
requires `voices` and `voices_intro` together rather than requiring either.

**The section order is comparison, then voices, then notes, then FAQ.** Copy inside `voices`,
`notes` and `faq` that says "the comparison below" is pointing backwards; the first written pages
say it anyway. Write position-neutral ("the comparison above", "the prices here") or the sentence
is wrong for every reader who scrolls.

**Written so far: 12 of the 66 jobs.** The YouTube & video cluster (transcript, video stats, channel
stats, search, comments) landed 2026-08-21 and is the first `compare`-form cluster where one row is
free: the official Data API on the reader's own connected Google account, at $0.00 with a 10,000
unit daily quota. The free row is deliberately excluded from the "cheapest per unit" claim, because
`_uc_providers` only ranks rows with a truthy USD price, and a free-but-rationed row is not a
cheaper version of a metered one. Those pages carry the quota arithmetic instead, which is what the
research said people actually get stuck on.

Nested under the category on purpose: the five flat ad pages keep their URLs and `build_html.py`
ownership, and `test_legacy_flat_use_case_pages_still_answer` proves the nested route cannot shadow
them. `/use-cases` is the crawlable hub they hang from; before it existed the only link into a spoke
was one row on one agent page. All hosted-only, sitemapped and `.md`-mirrored like the agent pages.

## Counts

`2,630 endpoints / 47 providers / 80 platforms`, from `catalog_store.load()`. The landing, `llms.txt`
and the schema all state them and had drifted apart (2,617/42 and ~2,600/~48). Note the catalog index
shows the **whole** catalog, not the sum of its tiles: a tile counts only its browse surface, so the
account/utility endpoints — real inventory, listed on each shelf page — are excluded from tile counts
by `catalog_store.HIDDEN_KINDS`.
