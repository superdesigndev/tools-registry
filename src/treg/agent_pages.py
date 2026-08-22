"""The hand-written half of the per-agent pages (`/agents/<agent>`).

Everything else on those pages is projected from the catalog at request time; this module holds the
only copy a person writes — who the page is for (the rotating roles), how to install treg in that
client, and the use-case menu: plain-words jobs under buyer categories, each mapped to the catalog
capabilities that do it. Kept out of `api.py` so the editorial text can be reviewed without reading
routing code, and kept free of heavy imports so nothing here costs the light CLI install anything.

Two rules the tests enforce:
  - every capability id named in USE_CASES must exist in the catalog — a job the catalog cannot do
    must not be advertised ("do not document what is not built"), and a renamed capability must
    fail the build rather than silently drop a row;
  - the install copy describes the HOSTED treg.to (the ChatGPT Plugins listing, the $1.00 grant),
    so `api.py` serves these pages only on the reference hosts (`PUBLIC_HOST_ALIASES`).
"""

from __future__ import annotations

# The rotating word in the hero: "ChatGPT for <role>". The first is what crawlers and no-JS
# readers see, so it is the broadest.
# One line per category for the overview cards on the agent page. "{agent}" is the client's name.
CATEGORY_BLURBS: dict[str, str] = {
    "Connect your own accounts": "Search Console, GA4, Google Ads, Meta Ads, Business Profile, Slack. Your data, read by {agent}, free.",
    "Data enrichment & sales": "Work emails, phone numbers, people search, company lists and signals for outbound.",
    "Social": "Creators, trends, posts, hashtags and comments across LinkedIn, Instagram, TikTok, X, Reddit.",
    "YouTube & video": "Transcripts, channel stats, search, trending and comments.",
    "Finance & markets": "Quotes, price history, fundamentals, dividends and crypto.",
    "Local businesses & reviews": "Find businesses by keyword and location, read their reviews.",
    "SEO": "Keyword volume, SERPs, rankings, backlinks, audits and AI-answer visibility.",
    "E-commerce": "Amazon, TikTok Shop and app-store product data and reviews.",
    "Advertising": "What competitors are running, what a domain bids on, your own campaign numbers.",
    "Market research": "Job postings, employee reviews, GitHub trends.",
}

CATEGORY_PROMPTS: dict[str, str] = {
    "Connect your own accounts": "Using treg, which queries is treg.to ranking 8–15 for in Search Console, and did traffic drop this week in GA4?",
    "YouTube & video": "Using treg, get the transcript of this YouTube video and pull the 10 most-liked comments.",
    "Finance & markets": "Using treg, get the current price of AAPL, its last 30 days of closes, and any dividends this year.",
    "Local businesses & reviews": "Using treg, find 20 plumbers in Austin on Yelp with rating and review count, and pull the latest reviews for the top 3.",
    "Data enrichment & sales": "Using treg, find the work email of the VP of Marketing at stripe.com and tell me what the call cost.",
    "Social": "Using treg, find 20 TikTok creators posting about home espresso with 50k–500k followers.",
    "SEO": "Using treg, how many people search “reddit api pricing” per month in the US, and who ranks top 10?",
    "E-commerce": "Using treg, pull the top 10 Amazon best sellers in espresso machines with price and rating.",
    "Advertising": "Using treg, show me every ad Notion is running on Meta right now.",
    "Market research": "Using treg, list companies hiring a Head of SEO this month, with headcount trend.",
}

ROLES: tuple[str, ...] = (
    "SEO experts",
    "social media managers",
    "SDRs",
    "YouTubers & creators",
    "indie hackers",
    "growth marketers",
    "e-commerce sellers",
    "market researchers",
    "media buyers",
)

# category → [(job in the buyer's words, [capability ids that do it])]. Order is the page order.
# A job may span platforms (one id per platform); the page shows the union of providers and the
# lowest price across them, and links each platform. Each category carries one example prompt —
# what asking looks like; the per-job prompts live on the use-case pages.
USE_CASES: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    # The biggest demand cluster in the X/Reddit research (2026-08-21): people want their agent on
    # THEIR data. All own-key, so never metered.
    ("Connect your own accounts", (
        ("Search Console: clicks, impressions and top queries", ("search-console.performance",)),
        ("Is this page indexed, and why not", ("search-console.url_inspection",)),
        ("Google Analytics: traffic and behaviour reports", ("google-analytics.report",)),
        ("Realtime visitors on your site", ("google-analytics.realtime",)),
        ("Google Ads: the search terms triggering your ads", ("google-ads.search_terms",)),
        ("Google Ads and Meta Ads campaign performance", ("google-ads.campaigns.performance",
                                                          "meta-ads.insights")),
        ("Your Google Business Profile reviews, and reply to them", ("google-business.reviews",
                                                                     "google-business.review.reply")),
        ("Search terms that surfaced your listing on Maps", ("google-business.insights.keywords",)),
        ("Your Instagram and Facebook page insights", ("instagram.account.insights",
                                                       "facebook.page.insights")),
        ("Read and post in your Slack channels", ("slack.messages.history", "slack.message.send")),
    )),
    ("Data enrichment & sales", (
        ("Find professional emails", ("people.email.find",)),
        ("Verify an email before you send", ("people.email.verify",)),
        ("Find people by role, company or location", ("people.search", "linkedin.search.people")),
        ("Get a LinkedIn profile", ("linkedin.user.profile",)),
        ("Enrich a person from an email or LinkedIn URL", ("people.enrich",)),
        ("Find phone numbers", ("people.phone.find",)),
        ("Enrich a company from its domain", ("companies.enrich",)),
        ("Build a company list by industry, size or tech", ("companies.search",)),
        ("A company's email format", ("companies.email_pattern",)),
        ("Hiring, headcount and news signals", ("companies.jobs", "companies.headcount_trend",
                                                "companies.news")),
        ("A company's funding rounds", ("companies.funding",)),
    )),
    ("Social", (
        ("Find creators by keyword", ("instagram.search.users", "tiktok.search.users",
                                     "youtube.search.channels", "x.search.users")),
        ("A creator's profile and stats", ("instagram.user.profile", "tiktok.user.profile",
                                           "youtube.channel.profile", "x.user.profile")),
        ("What's trending right now", ("x.trending.topics", "tiktok.trends.searches",
                                       "youtube.trending.videos", "reddit.search.trending")),
        ("Search posts by keyword", ("x.search.posts", "reddit.search.posts",
                                     "linkedin.search.posts", "tiktok.search.videos")),
        ("Posts under a hashtag", ("instagram.hashtag.posts", "tiktok.hashtag.videos")),
        ("Mine the comments", ("instagram.post.comments", "youtube.video.comments",
                               "reddit.post.comments")),
        ("A competitor's recent posts", ("x.user.posts", "linkedin.company.posts",
                                         "threads.user.posts")),
        ("Publish to your own accounts", ("instagram.post.create", "linkedin.user.post.create",
                                          "x.post.create", "tiktok.video.publish",
                                          "youtube.video.upload")),
        ("Podcast episodes and shows", ("spotify.search", "spotify.podcast.episodes")),
    )),
    # The single most-asked job in the research was YouTube transcripts (16 posts), and people
    # keep failing to self-host it.
    ("YouTube & video", (
        ("Get a video's transcript", ("youtube.video.captions",)),
        ("Video details, views and stats", ("youtube.video.detail",)),
        ("A channel's profile and lifetime stats", ("youtube.channel.profile",)),
        ("Search videos and channels by keyword", ("youtube.search.videos", "youtube.search.channels")),
        ("Trending videos", ("youtube.trending.videos",)),
        ("A video's comments", ("youtube.video.comments",)),
        ("Transcripts of X and Facebook video posts", ("x.post.transcript", "facebook.post.transcript")),
    )),
    ("Finance & markets", (
        ("Current quote for a ticker", ("stocks.quote.live",)),
        ("Daily price history", ("stocks.eod.history",)),
        ("Company profile and fundamentals behind a ticker", ("stocks.company.profile",
                                                              "stocks.fundamentals.metrics")),
        ("Dividends and splits", ("stocks.actions.dividends", "stocks.actions.splits")),
        ("News for a ticker", ("stocks.company.news",)),
        ("Live crypto prices and history", ("crypto.price.current", "crypto.price.history")),
        ("Coins trending right now", ("crypto.market.trending",)),
    )),
    ("Local businesses & reviews", (
        ("Find local businesses by keyword and location", ("yelp.business.search",
                                                           "tripadvisor.search.businesses")),
        ("A business's reviews", ("yelp.business.reviews", "tripadvisor.business.reviews",
                                  "trustpilot.business.reviews")),
        ("Hotel listing details", ("tripadvisor.hotel.detail",)),
        ("Product reviews", ("walmart.product.reviews", "tiktok-shop.product.reviews")),
    )),
    ("SEO", (
        ("Keyword volume, CPC and competition", ("google.keywords.volume",)),
        ("Keyword ideas from a seed", ("google.keywords.ideas",)),
        ("Google results for a keyword", ("google.serp.organic",)),
        ("Keywords a domain ranks for", ("google.domain.ranked_keywords",)),
        ("Backlink profile of a domain", ("web.backlinks.summary",)),
        ("List backlinks and find link gaps", ("web.backlinks.list", "web.backlinks.intersect")),
        ("On-page audit of a URL", ("web.page.audit",)),
        ("How AI answers mention your brand", ("ai-search.mentions.summary",
                                                "ai-search.chatgpt.answer",
                                                "ai-search.perplexity.answer")),
    )),
    ("E-commerce", (
        ("Amazon product detail by ASIN", ("amazon.product.detail",)),
        ("Amazon search and best sellers", ("amazon.search.products", "amazon.bestsellers.list")),
        ("TikTok Shop products and reviews", ("tiktok-shop.search.products",
                                              "tiktok-shop.product.reviews")),
        ("App store search", ("app-store.search.apps", "google-play.search.apps")),
    )),
    ("Advertising", (
        ("Ads a competitor is running now", ("meta-ads.library.search", "meta-ads.library.advertiser",
                                             "google.ads.transparency", "linkedin.search.ads")),
        ("Keywords a domain bids on", ("google.domain.paid_keywords",)),
        ("Your own campaign performance", ("google-ads.campaigns.performance", "meta-ads.insights")),
    )),
    ("Market research", (
        ("Job postings across companies", ("companies.jobs.search", "linkedin.search.jobs")),
        ("Employee reviews of a company", ("companies.reviews",)),
        ("GitHub: trending repositories and a repo's profile", ("github.trending.repositories",
                                                                 "github.repo.profile")),
    )),
)

# The clients the onboarding supports, with their icon (lobehub static set, as the landing and the
# dashboard already use). An entry links to `/agents/<id>` only once that page exists in AGENTS.
AGENT_ICONS: tuple[tuple[str, str, str], ...] = (
    ("chatgpt", "ChatGPT", "openai"),
    ("claude", "Claude", "claude-color"),
    ("claude-code", "Claude Code", "claudecode-color"),
    ("codex", "Codex", "codex-color"),
    ("cursor", "Cursor", "cursor"),
    ("gemini-cli", "Gemini CLI", "gemini-color"),
    ("openclaw", "OpenClaw", "openclaw-color"),
    ("hermes", "Hermes Agent", "hermesagent"),
    ("opencode", "opencode", "opencode"),
    ("pi", "pi", "pi"),
)

# Logo domains for the favicon service the landing already uses
# (https://www.google.com/s2/favicons?domain=…). Hand-kept; a platform or provider not listed gets
# the treg glyph instead of a wrong logo. Abstract shelves (people, companies, web, stocks) have no
# single brand and are deliberately absent.
PLATFORM_DOMAINS: dict[str, str] = {
    "search-console": "search.google.com", "google-analytics": "analytics.google.com",
    "google-ads": "ads.google.com", "google-business": "business.google.com", "google": "google.com",
    "meta-ads": "facebook.com", "facebook": "facebook.com", "instagram": "instagram.com",
    "linkedin": "linkedin.com", "x": "x.com", "reddit": "reddit.com", "youtube": "youtube.com",
    "tiktok": "tiktok.com", "tiktok-shop": "tiktok.com", "threads": "threads.net",
    "slack": "slack.com", "telegram": "telegram.org", "github": "github.com", "spotify": "spotify.com",
    "amazon": "amazon.com", "app-store": "apple.com", "google-play": "play.google.com",
    "yelp": "yelp.com", "tripadvisor": "tripadvisor.com", "trustpilot": "trustpilot.com",
    "walmart": "walmart.com", "ai-search": "chatgpt.com", "crypto": "coingecko.com",
    "stocks": "finance.yahoo.com", "web": "cloudflare.com",
}
PROVIDER_DOMAINS: dict[str, str] = {
    "hunter": "hunter.io", "tomba": "tomba.io", "leadmagic": "leadmagic.io", "icypeas": "icypeas.com",
    "fiber-ai": "fiber.ai", "findymail": "findymail.com", "leadsforge": "leadsforge.ai",
    "oceanio": "ocean.io", "companyenrich": "companyenrich.com", "apollo": "apollo.io",
    "dataforseo": "dataforseo.com", "serpapi": "serpapi.com", "semrush": "semrush.com", "moz": "moz.com",
    "majestic": "majestic.com", "seranking": "seranking.com", "serpstat": "serpstat.com",
    "spyfu": "spyfu.com", "apify": "apify.com", "brightdata": "brightdata.com",
    "scrapecreators": "scrapecreators.com", "tikhub": "tikhub.io", "justoneapi": "justoneapi.com",
    "predictleads": "predictleads.com", "finnhub": "finnhub.io", "twelvedata": "twelvedata.com",
    "eodhd": "eodhd.com", "lusha": "lusha.com", "crunchbase": "crunchbase.com",
}

# Why go through treg.to at all: (lead, one short line). Same on every use-case page.
WHY_TREG: tuple[tuple[str, str], ...] = (
    ("One key, not 9 accounts", "treg.to holds the provider keys. Neither you nor the agent sees them."),
    ("Price before the call", "The provider's own rate, $0.000 markup, from a prepaid balance."),
    ("No subscription, no seats", "Charged per call. $1.00 free per new team, no card to start."),
    ("Your own keys are free", "Already pay Hunter? Register it and those calls are never metered."),
    ("Switch by changing a word", "Another provider is a different word in the prompt, not a new integration."),
    ("Nothing to integrate", "No SDK, no OAuth dance per vendor, no seats."),
)

# The client used as the example throughout the use-case pages. One constant, so the pages are a
# template rather than ChatGPT-specific prose.
DEFAULT_AGENT = "chatgpt"

# Subscription list prices, for the "instead of" anchor on a use-case page. Sourced from
# marketing/landing/_facts.md F-20..F-23, which record where each figure came from and when. A page
# names only providers whose plan price is listed here; anything else shows the catalog spread
# instead, because an invented anchor is worse than no anchor.
PLAN_PRICES: dict[str, int] = {
    "hunter": 34, "lusha": 49, "apollo": 59, "crunchbase": 99, "diffbot": 299,
    "semrush": 139, "serpstat": 69, "spyfu": 39, "serpapi": 75, "seranking": 65,
    "moz": 99, "majestic": 50,
}

# The universal setup line: paste it into any agent's chat and it reads llms.txt and sets itself up.
SETUP_LINE = "set up treg — {base}/llms.txt"

AGENTS: dict[str, dict] = {
    "chatgpt": {
        "name": "ChatGPT",
        "title": "ChatGPT plugin: call {n} APIs without keys | treg.to",
        "description": (
            "treg.to is a ChatGPT plugin that lets ChatGPT call {n} APIs across {p} platforms: "
            "find work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor "
            "ads. Priced per call at the provider's own rate, with no markup and no provider signup."),
        # The one quotable sentence an answer engine should lift. Server-rendered first, under the H1.
        "definition": (
            "treg.to is a ChatGPT plugin (and MCP server) that gives ChatGPT {n} ready-to-call APIs "
            "across {p} platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, "
            "ads and e-commerce. Calls run on treg.to's own keys and are metered from a prepaid "
            "balance at the provider's rate with $0.000 markup. Every new team starts with $1.00 "
            "free, and there are no provider accounts to open."),
        # Steps shown as numbered HTML list items. Plain text; escaped by the route.
        "install_steps": [
            "In ChatGPT, open <b>Plugins</b> in the left sidebar.",
            "Search for <b>treg</b> and click <b>Install</b>. It is listed publicly as "
            "“treg: Call 2,600 APIs without keys”.",
            "Sign in when ChatGPT asks; your first team starts with $1.00 of free calls.",
            "Ask for what you want done. ChatGPT searches the catalog, tells you the price, and "
            "calls the endpoint. You never hold a provider key.",
        ],
        "install_image": "/media/install/chatgpt-plugins.png",
        "install_image_alt": "ChatGPT's Plugins directory with “treg” searched and its Install button",
        "install_image_bar": "chatgpt.com  ·  Plugins",
        "install_image_caption": "Steps 1 and 2: Plugins in the sidebar, search treg, Install.",
        "faq": [
            ("Is treg.to free to use in ChatGPT?",
             "Installing is free and every new team starts with $1.00 of calls. After that, each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup "
             "and no subscription. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer. If your "
             "team already has a key for a provider, you can register it and those calls are never "
             "metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup "
             "to a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate. Every row on this page shows the lowest price for that job, and ChatGPT tells "
             "you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do the same job they are shown side by side with prices "
             "and measured reliability, and ChatGPT (or you) chooses. treg.to does not route or "
             "fail over between them automatically."),
        ],
    },
}


def category_slug(category: str) -> str:
    """`Data enrichment & sales` → `data-enrichment-sales`. The URL segment for a category, and the
    anchor id of its section on the agent pages: one function so they can never disagree."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


# The use-case pages (the spokes): one per job from USE_CASES that has been reviewed and written.
# Keyed by (category slug, job slug). A job without an entry here has no page — the agent page
# links it into the catalog instead — so a page cannot exist without a person having written its
# sentence and prompt. `label` must match the row in USE_CASES exactly; a test enforces it.
USE_CASE_PAGES: dict[tuple[str, str], dict] = {
    ("data-enrichment-sales", "find-professional-emails"): {
        "label": "Find professional emails",
        # The H1, in the buyer's words; the title is built from it plus live catalog numbers.
        # H1 and title carry the words people type ("email finder", "linkedin email finder", "api");
        # the buyer's-words label stays on the menu.
        "sentence": "Email finder API: a work email from a name, company or LinkedIn URL",
        "title": "Email finder API: {n} providers compared | treg.to",
        "lede": (
            "Give your agent a name and a company domain, or a LinkedIn URL, and get back a "
            "verified work address. {n} providers do this job. They differ in what they need as "
            "input, what they charge for a miss, and how they bill. Every row below is callable "
            "right now through one treg.to key, at the provider's rate with no markup."),
        # What to type, per client. One URL, tabs on the page.
        # One prompt, the same in every client. Copy button on the page.
        "prompt": "Using treg, find the work email of the VP of Marketing at stripe.com. Show me "
                  "the price first, then call the cheapest verified provider and tell me the confidence.",
        # Why the prompt works: what to give the agent and what to ask it for. Two or three points.
        # (lead, one short line). Rendered as small cards, not a bullet wall.
        "prompt_why": [
            ("Give it what you have",
             "Name plus company domain works everywhere. A LinkedIn URL works with 3 of the 9."),
            ("Ask for the price first",
             "treg.to returns the cost before the call, so ChatGPT can say what it will spend."),
            ("Name your preference, the agent picks",
             "“cheapest” · “most reliable” · “only bill me when you find one” · “takes a LinkedIn URL”."),
            ("Say what to do on a miss",
             "“If the first returns nothing, try the next cheapest.” That is a waterfall in one line."),
        ],
        # Real questions from Reddit and X, quoted verbatim with a link. Gathered 2026-08-21 via
        # agent-reach over ~180 posts; vendor-written posts were excluded.
        "voices_intro": (
            "The hard part is not finding an address, it is finding one that will not bounce. From "
            "180 posts on Reddit and X in August 2026, these five come up more than anything else."),
        "voices": [
            ("A bounce hurts the sending domain, not just the campaign",
             "A 3% bounce rate from Apollo can torch your sending domain and trigger weeks of warmup.",
             "@dan__rosenthal on X", "https://x.com/i/status/2053188346629820721",
             "Verify before you send. The verification job is one more call at $0.0019 and returns "
             "deliverability plus the mail provider, so you can drop the risky rows instead of "
             "gambling the domain."),
            ("Find rate and validity are different numbers, and vendors advertise the first",
             "low coverage means most of the list never even gets contacted. high bounces hurt deliverability.",
             "r/GrowthHacking", "https://www.reddit.com/r/GrowthHacking/comments/1rle23d/which_email_finder_actually_scales/",
             "This page does not reprint anyone's accuracy claim. It shows the price, what the "
             "provider bills for a miss, and the success rate treg.to measured on live calls."),
            ("Nobody trusts one provider, so everyone builds a waterfall",
             "Most agencies use one email finder, get 45-50% coverage, and immediately lose half their list before even emailing.",
             "@itsalexvacca on X", "https://x.com/i/status/1976301889634566420",
             "All nine are callable through one key, so a miss costs one more call to try the next "
             "one. Tell the agent the order you want; treg.to compares, it does not fail over on "
             "its own."),
            ("The lead is a LinkedIn URL and nothing else",
             "I tried GetProspect and Apollo.io to extract the email address but most of them were wrong.",
             "r/businessemail", "https://www.reddit.com/r/businessemail/comments/1ttn2fd/how_to_search_an_email_address_on_linkedin_easily/",
             "Three providers here take a LinkedIn URL directly (Tomba, Fiber AI, LeadMagic) and "
             "skip name matching entirely. The comparison below marks what each one accepts."),
            ("Coverage claims are indistinguishable, especially outside the US",
             "everyone's marketing site claims continental coverage so i can't tell which one holds at the smaller-domain end",
             "r/SalesOperations", "https://www.reddit.com/r/SalesOperations/comments/1uen0g6/best_email_finder_for_smaller_eu_companies/",
             "No comparison table can answer this honestly. Per-call pricing makes the real test "
             "cheap: run 100 of your own contacts through three providers for a few dollars and "
             "keep the one that holds up."),
        ],
        # Optional: a screenshot of the real answer in the client. Omitted until one exists.
        "result_image": None,
        # The keyword phrasing of the three behind-the-scenes questions, in the page's own words.
        "q_cheapest": "Which email finder API is cheapest?",
        "q_reliable": "Which one is the most reliable?",
        "q_compare": "How do the providers compare?",
        "what_is": (
            "An email finder API takes a person's name and their company's domain (or a LinkedIn "
            "URL) and returns the work email address, usually with a confidence score and the "
            "sources it was seen on. Providers differ in coverage, in what they accept as input, "
            "and in whether they bill per attempt or only per address found."),
        # Hand-written: what actually differs between the providers for THIS job. Reviewed when the
        # catalog changes; kept short on purpose.
        "notes": [
            "Per-success pricing (Hunter, LeadMagic, Tomba, CompanyEnrich) bills only when an address "
            "is found; per-call pricing bills the attempt. On lists with many misses that difference "
            "dominates the bill.",
            "Inputs differ: most take name + company domain. Tomba and Fiber AI also resolve from a "
            "LinkedIn URL, and CompanyEnrich needs its own person id from a prior search, so that "
            "one is a two-step flow.",
            "Confidence scores are not comparable across providers. Treat a 'verified' grade from one "
            "and a 95 from another as different scales, and verify before you send "
            "(see the email verification job).",
        ],
        "faq": [
            ("How much does it cost to find a work email?",
             "Between a fraction of a cent and a few cents per found address, depending on the "
             "provider. The lowest live price is shown at the top of this page, and treg.to adds "
             "no markup. Most providers here charge only on success, so a miss costs nothing."),
            ("Do I need a Hunter or Apollo account?",
             "No. treg.to calls the provider on its own key and bills your team's prepaid balance per "
             "call. If you already have a key for one of them, register it and those calls are "
             "never metered."),
            ("Which provider should I use?",
             "It depends on what you have. Name plus domain: start with the cheapest verified "
             "per-success provider. A LinkedIn URL: use one that accepts it directly. treg.to shows "
             "them side by side but does not choose or fail over for you."),
            ("Is the data legal to use?",
             "These providers return business contact data under their own terms; you are "
             "responsible for how you use it, including consent and anti-spam law in your "
             "jurisdiction. treg.to relays the provider's answer and stores no copy."),
        ],
        "related": ("Verify an email before you send", "Enrich a person from an email or LinkedIn URL",
                    "Find people by role, company or location", "A company's email format"),
    },
}

USE_CASE_PAGES[("connect-your-own-accounts", "search-console-queries")] = {
    "label": "Search Console: clicks, impressions and top queries",
    "sentence": "Google Search Console API: clicks, impressions and top queries, read by your agent",
    "title": "Search Console API for {agent}: queries and clicks | treg.to",
    "lede": (
        "Connect the Search Console property you already own and your agent can read the same "
        "numbers the UI shows: every query, its clicks, impressions, CTR and average position, for "
        "any date range. It runs on your own Google account, so treg.to never meters it."),
    "prompt": "Using treg, show me the queries treg.to ranked 8 to 20 for in the last 28 days with "
              "more than 50 impressions, sorted by impressions, and tell me which ones have the worst CTR.",
    "prompt_why": [
        ("Connect once", "One OAuth click for the property you own. treg.to holds the token, not you."),
        ("Ask in plain words", "Date ranges, filters and sorting are the agent's job, not yours."),
        ("End the window 3 days back", "Search Console lags 2 to 3 days; yesterday is preliminary data."),
        ("It costs nothing", "Your own account, so the call is never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is the Search Console API?",
    "what_is": (
        "The Search Console API returns your site's Google Search performance as data: clicks, "
        "impressions, CTR and average position, broken down by query, page, country, device and "
        "date. It is the same data as the Performance report, without the UI's row limits, and it "
        "is the only first-party source for what people actually searched before they reached you."),
    "notes": [
        "Ending a window on yesterday silently ends it on preliminary data. Score a 28-day window "
        "that ends 3 days back.",
        "The UI caps the query table at 1,000 rows; the API paginates past that, which is where the "
        "long tail lives.",
        "Queries with very low volume are withheld for privacy, so the totals in a query breakdown "
        "will not add up to the site total. That gap is expected.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. Search Console runs on your own Google account, so treg.to relays the call and meters "
         "nothing. Only calls on treg.to's own provider keys are billed."),
        ("What do I have to connect?",
         "The Google account that already has access to the property, once, through an OAuth "
         "screen. treg.to stores the token server side; your agent never sees it."),
        ("Can my agent see other people's sites?",
         "No. The API returns only the properties your connected account can access."),
        ("Which agents can do this?",
         "Any client that can reach treg.to: ChatGPT, Claude, Claude Code, Cursor and the rest of "
         "the supported list."),
    ],
    "related": ("Is this page indexed, and why not", "Google Analytics: traffic and behaviour reports",
                "Keyword volume, CPC and competition", "Google results for a keyword"),
}

USE_CASE_PAGES[("social", "find-creators-by-keyword")] = {
    "label": "Find creators by keyword",
    "sentence": "Find creators by keyword on Instagram, TikTok, YouTube and X",
    "title": "Creator search API: 4 platforms compared | treg.to",
    "lede": (
        "Search each platform's own user index by keyword and get back profiles with follower "
        "counts, bios and links, so your agent can shortlist creators instead of you scrolling. "
        "Each platform is served by its own providers; the comparison below is per platform, "
        "because an Instagram search and a YouTube search are different jobs, not alternatives."),
    "prompt": "Using treg, find 20 TikTok creators posting about home espresso with between 50k and "
              "500k followers. Show me the price first, then give me handles, follower counts and bios.",
    "prompt_why": [
        ("Name the platform", "Each platform has its own index. \"Creators\" alone makes the agent guess."),
        ("Give a keyword, not a topic", "These are keyword searches over profiles, not semantic search."),
        ("Ask for the fields you want", "Follower count, bio and link come back; say so and you get a table."),
        ("Filter after, not during", "Follower ranges are your filter on the results, not a search parameter."),
    ],
    "result_noun": "profile",
    "result_image": None,
    "what_is_heading": "What does a creator search API return?",
    "what_is": (
        "A creator search endpoint queries a platform's own user directory for a keyword and "
        "returns matching profiles: handle, display name, follower count, bio, verification and "
        "profile link. It is the discovery half of influencer research. Engagement rates, contact "
        "details and audience demographics are separate jobs on separate endpoints."),
    "notes": [
        "Follower ranges are not a search filter on any of these platforms. Providers return "
        "keyword matches and your agent filters them, so ask for more results than you need.",
        "A keyword search matches handles, display names and bios, not video or caption text. To "
        "find creators by what they posted, search posts or hashtags instead and take the authors.",
        "Coverage differs per platform, not per provider: TikTok and Instagram indexes are deep, "
        "X's user search is shallow, and YouTube's channel search is keyword-literal.",
    ],
    "faq": [
        ("Can I find creators by follower count?",
         "Not directly. These endpoints search by keyword; ask your agent for a larger result set "
         "and let it filter on the follower counts that come back."),
        ("Does this give me their email?",
         "No. Contact details are a separate job. Some creator bios carry a business email, and the "
         "people enrichment endpoints can resolve a work address from a name and company."),
        ("Which platform should I search?",
         "The one your audience uses. The comparison below shows who serves each platform and what "
         "a search costs there; treg.to does not pick a platform for you."),
        ("Is this the official API?",
         "For most of these platforms it is a data provider reading the public index, not the "
         "platform's own API. Each row names the provider and links its documentation."),
    ],
    "related": ("A creator's profile and stats", "Search posts by keyword", "Posts under a hashtag",
                "Mine the comments"),
}
AGENTS["claude"] = {
    "name": "Claude",
    "title": "Claude MCP server: {n} APIs without keys | treg.to",
    "description": (
        "treg.to gives Claude {n} ready-to-call APIs across {p} platforms: work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced per call at the provider's own rate with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Claude that gives it {n} ready-to-call APIs across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "Calls run on treg.to's own keys and are metered from a prepaid balance at the provider's "
        "rate with $0.000 markup. Every new team starts with $1.00 free, and there are no provider "
        "accounts to open."),
    "install_steps": [
        "In Claude, send this in the chat: <code>set up treg &mdash; https://treg.to/llms.txt</code>",
        "It reads that page and sets itself up. If it asks for a key, give it your team token from "
        "the treg.to dashboard (header <code>X-Treg-Token</code>).",
        "Your first team starts with $1.00 of free calls. No card, no subscription, no seats.",
        "Ask for what you want done. Claude searches the catalog, tells you the price, and calls the "
        "endpoint. You never hold a provider key.",
    ],
    "install_image": None,
        "faq": [
            ("Is treg.to free to use in Claude?",
             "Installing is free and every new team starts with $1.00 of calls. After that each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup, "
             "no subscription and no seats. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer, so Claude "
             "never holds a provider credential. If your team already pays for a provider, register "
             "that key and those calls are never metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup to "
             "a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate, and Claude tells you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do one job they are shown side by side with prices and "
             "measured reliability, and Claude picks, or you tell it how. treg.to does not route or "
             "fail over between providers automatically."),
        ],
}

AGENTS["claude-code"] = {
    "name": "Claude Code",
    "title": "Claude Code MCP server: {n} APIs, no keys | treg.to",
    "description": (
        "treg.to gives Claude Code {n} ready-to-call APIs across {p} platforms: work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced per call at the provider's own rate with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Claude Code that gives it {n} ready-to-call APIs across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "One command registers it, calls run on treg.to's keys at the provider's rate with $0.000 "
        "markup, and your own keys are never metered."),
    "install_steps": [
        "In Claude Code, send this in the chat: <code>set up treg &mdash; https://treg.to/llms.txt</code>",
        "It reads that page and registers treg.to as an MCP server for you. Prefer to do it "
        "yourself? <code>curl -fsSL https://treg.to/install.sh | sh</code> then "
        "<code>treg login</code> and <code>treg mcp install</code>.",
        "Your first team starts with $1.00 of free calls. No card, no subscription, no seats.",
        "Ask for what you want done, or call an endpoint directly with "
        "<code>treg call &lt;endpoint-id&gt;</code>. The price comes back before the spend.",
    ],
    "install_image": None,
        "faq": [
            ("Is treg.to free to use in Claude Code?",
             "Installing is free and every new team starts with $1.00 of calls. After that each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup, "
             "no subscription and no seats. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer, so Claude Code "
             "never holds a provider credential. If your team already pays for a provider, register "
             "that key and those calls are never metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup to "
             "a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate, and Claude Code tells you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do one job they are shown side by side with prices and "
             "measured reliability, and Claude Code picks, or you tell it how. treg.to does not route or "
             "fail over between providers automatically."),
        ],
}

AGENTS["cursor"] = {
    "name": "Cursor",
    "title": "Cursor MCP server: {n} APIs, no keys | treg.to",
    "description": (
        "treg.to gives Cursor {n} ready-to-call APIs across {p} platforms: work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced per call at the provider's own rate with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Cursor that gives its agent {n} ready-to-call APIs across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "Calls run on treg.to's own keys at the provider's rate with $0.000 markup, from a prepaid "
        "balance that starts with $1.00 free."),
    "install_steps": [
        "In Cursor's agent chat, send: <code>set up treg &mdash; https://treg.to/llms.txt</code>",
        "It reads that page and sets itself up. If it asks for a key, give it your team token from "
        "the treg.to dashboard (header <code>X-Treg-Token</code>).",
        "Your first team starts with $1.00 of free calls. No card, no subscription, no seats.",
        "Ask for what you want done. Cursor searches the catalog, tells you the price, and calls the "
        "endpoint. You never hold a provider key.",
    ],
    "install_image": None,
        "faq": [
            ("Is treg.to free to use in Cursor?",
             "Installing is free and every new team starts with $1.00 of calls. After that each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup, "
             "no subscription and no seats. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer, so Cursor "
             "never holds a provider credential. If your team already pays for a provider, register "
             "that key and those calls are never metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup to "
             "a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate, and Cursor tells you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do one job they are shown side by side with prices and "
             "measured reliability, and Cursor picks, or you tell it how. treg.to does not route or "
             "fail over between providers automatically."),
        ],
}

USE_CASE_PAGES[("data-enrichment-sales", "verify-an-email")] = {
    "label": "Verify an email before you send",
    "sentence": "Email verification API: is this address deliverable, before you send",
    "title": "Email verification API: {n} verifiers compared | treg.to",
    "lede": (
        "Hand your agent an address and get back a verdict: deliverable, undeliverable, or the "
        "third bucket every verifier has and each one names differently. {n} providers do this "
        "through one treg.to key, and what separates them is not accuracy claims. It is what they "
        "charge for an answer of “unknown”, which on a real B2B list is about a fifth of it."),
    "prompt": "Using treg, verify these 40 addresses before I send. Show me the price first, then "
              "give me three lists: safe to send, do not send, and unknown with the reason.",
    "prompt_why": [
        ("Verify in one batch", "One list in, one table out. Cheaper to reason about than 40 calls."),
        ("Ask for three buckets", "Valid and invalid are easy. The third bucket is the decision you have to make."),
        ("Ask what a miss costs", "One provider charges nothing for “unknown”. The others bill it as a check."),
        ("Verify close to the send", "Data decays. A check from three weeks ago is not a check."),
    ],
    "result_noun": "check",
    "result_image": None,
    "what_is_heading": "What does an email verification API actually do?",
    "what_is": (
        "It resolves the domain's MX records and opens an SMTP conversation with the receiving "
        "server to ask whether the mailbox exists, without delivering a message. Three answers come "
        "back: the server confirms, the server denies, or the server accepts everything and tells "
        "you nothing. That last case is a catch-all domain, and no provider can resolve it, because "
        "the information does not exist on the wire."),
    "notes": [
        "Only LeadMagic bills nothing for an inconclusive result: it charges 0.25 credits per "
        "definitive verdict and lets “unknown” through free. Icypeas charges per address "
        "tested whether or not the answer is useful. On a list that is a fifth catch-all, that gap "
        "is the whole price difference.",
        "Every provider names the third bucket differently. Hunter returns accept_all, webmail, "
        "disposable and unknown as separate statuses with a score; LeadMagic returns a plain "
        "is_domain_catch_all flag plus the MX provider; Icypeas returns a certainty level. Compare "
        "the buckets, not the headline accuracy number.",
        "Icypeas is asynchronous: you submit the address and read the verdict from a second call. "
        "The other four answer in the same request, which matters when an agent is verifying a "
        "list interactively.",
    ],
    "voices_intro": (
        "Verification is the step everybody agrees on and nobody is happy with. From ~150 Reddit "
        "and X posts in August 2026, after excluding four separate vendor-astroturf clusters, "
        "these are the complaints that recur."),
    "voices": [
        ("The price looks high for what the operation is",
         "zerobounce wants $65 per 10k emails. neverbounce wants $80. hunter wants $100. i built the same thing in n8n for $0.",
         "r/n8n, 214 points", "https://www.reddit.com/r/n8n/comments/1ra50to/zerobounce_wants_65_per_10k_emails_neverbounce/",
         "Fair complaint, and the reason we publish per-check prices side by side rather than per "
         "10k tiers. Through treg.to the same checks run from a fraction of a cent, and you can "
         "compare what each one charges for an inconclusive answer."),
        ("Catch-all domains are a fifth of the list and nobody knows what to do with them",
         "Catch-all domains are about 20% of any B2B list. Most operators throw them away because the bounce risk is real",
         "@DeanFiacco on X", "https://x.com/i/status/2088600390509937006",
         "No provider resolves a true catch-all. What differs is what each one hands back: a "
         "distinct status, a probability, or a shrug. The comparison below names each provider's "
         "third bucket so you can decide once instead of per list."),
        ("An unknown result is the server refusing to answer, not the tool failing",
         "So the verifier returns unknown, or accept-all, or risky depending on the wording. That is not the tool failing.",
         "r/ColdEmailAndSales", "https://www.reddit.com/r/ColdEmailAndSales/comments/1vnjwj3/email_verification_what_catchall_domains_hide/",
         "Exactly right, and it is why “which verifier is most accurate” is the wrong "
         "question. Ask instead who charges you for the shrug: one of these five does not."),
        ("Role addresses and catch-alls get misclassified",
         "also curious if anyone has had issues with verification tools missing role accounts or catch alls. thats been my biggest frustration.",
         "r/Coldemailing", "https://www.reddit.com/r/Coldemailing/comments/1trpu7m/what_email_verifier_do_you_guys_actually_use_for/",
         "Hunter reports role and disposable addresses as their own statuses; the others fold them "
         "in. Whether a role address is worth keeping is genuinely unsettled, so the honest answer "
         "is to keep them separate and decide per campaign."),
        ("Doing it yourself gets your IP blocked",
         "SMTP probing gets your IP blocklisted and the big providers accept everything anyway.",
         "@kumard_3 on X", "https://x.com/i/status/2089979688395624832",
         "Both halves are true. Cloud providers block port 25 and repeated probes from one address "
         "get you listed, which is what you are paying a provider's IP pool for. It also explains "
         "why the unknown bucket exists at all."),
    ],
    "faq": [
        ("Does verification stop bounces?",
         "It removes the addresses that are provably dead, which is most of the risk. It cannot "
         "catch an address that goes stale between the check and the send, or one that a security "
         "gateway rejects at delivery time. Verify close to the send, not weeks before."),
        ("What is a catch-all or accept-all domain?",
         "A domain whose server accepts mail for every address without saying whether the mailbox "
         "exists. No verifier can resolve it. Expect roughly a fifth of a B2B list to land there."),
        ("How much does verification cost here?",
         "A fraction of a cent per check at the provider's own rate, with $0.000 added by treg.to. "
         "The prices and how each provider bills an inconclusive result are in the comparison below."),
        ("Can I run several verifiers over the same list?",
         "Yes, they are all callable through one key, and heavy senders do exactly that because "
         "verifiers disagree on the ambiguous rows. Your agent chains them; treg.to compares the "
         "options but does not route or fail over on its own."),
    ],
    "related": ("Find professional emails", "Enrich a person from an email or LinkedIn URL",
                "A company's email format", "Find people by role, company or location"),
}

USE_CASE_PAGES[("data-enrichment-sales", "enrich-a-person")] = {
    "label": "Enrich a person from an email or LinkedIn URL",
    "sentence": "Person enrichment API: a full profile from an email or LinkedIn URL",
    "title": "Person enrichment API: {n} providers compared | treg.to",
    "lede": (
        "Give your agent an email address or a LinkedIn URL and get back the person: current title, "
        "employer, seniority, location, work history. {n} providers do this through one treg.to "
        "key, and they are not close on price. The same match costs {cheapest} at one and about "
        "eighty times that at another, so what you are really choosing is how much a miss costs you."),
    "prompt": "Using treg, enrich these 20 LinkedIn URLs into a table: name, current title, company, "
              "seniority, location. Show me the price first, and skip anyone whose profile does not resolve.",
    "prompt_why": [
        ("Lead with your strongest key", "Email beats LinkedIn URL beats name plus company. Give the best one you have."),
        ("Ask for the fields, get a table", "Say which columns you want and the agent shapes the response."),
        ("Ask what a miss costs", "Several providers bill nothing when the person does not resolve."),
        ("Re-run the doubtful rows", "A second provider on the same person is one more call, not a second contract."),
    ],
    "result_noun": "match",
    "result_image": None,
    "what_is_heading": "What is a person enrichment API?",
    "what_is": (
        "It takes one identifier you already have, usually a work email or a LinkedIn profile URL, "
        "and returns the structured record behind it: name, current job title, employer, seniority, "
        "department, location and often the full work history. It is the step between knowing "
        "someone exists and knowing whether they are worth contacting."),
    "notes": [
        "Hunter bills conditionally: 0.2 credits only when the email, full name and position all "
        "come back, so a 404 or a partial record is free. LeadMagic and Icypeas are free on a "
        "no-match too. Apollo, by contrast, charges 8 extra credits the moment a mobile number is "
        "revealed, so a default enrichment and a phone-revealing one are different products.",
        "The price spread is the story: the same job runs from a fraction of a cent to about 38 "
        "cents a record. The dear end buys either phone numbers (Lusha's direct dials) or breadth "
        "of coverage (People Data Labs), not better titles. Decide which you are paying for.",
        "Several providers offer a bulk route at a different rate: Ocean.io's bulk lookup is half "
        "the price of its single enrichment and answers synchronously, while its batch enrichment "
        "is asynchronous and returns to a webhook. For fewer than a thousand people the cheap "
        "synchronous route is usually the right one.",
    ],
    "voices_intro": (
        "Enrichment is bought on accuracy claims and judged on what happens six weeks later. From "
        "~180 Reddit and X posts in August 2026, with two large vendor-astroturf rings excluded, "
        "these are the complaints that recur."),
    "voices": [
        ("The record is right when you buy it and wrong when you use it",
         "within weeks everything starts falling apart, emails bounce, titles are wrong, half the people moved companies",
         "r/SalesOperations, 17 points", "https://www.reddit.com/r/SalesOperations/comments/1pph89g/crm_data_enrichment_was_60_garbage_after_3_months/",
         "Nothing here stops decay. What per-call pricing changes is that re-checking a doubtful "
         "row costs a fraction of a cent instead of another annual contract, so you can enrich "
         "close to the moment you act instead of once a quarter."),
        ("Verified does not mean deliverable",
         "data accuracy is all over the place. we're seeing like 20-30% bounce rates even with their “verified” emails",
         "r/CRM", "https://www.reddit.com/r/CRM/comments/1svj17o/zoominfo_vs_cognism_vs_apollo_which_one_and_why/",
         "This page prints no vendor's accuracy claim, because none of them are measured the same "
         "way. Treat enrichment and verification as two steps: enrich, then run the address "
         "through a verifier before you send."),
        ("You pay for the blanks",
         "You pay for the data you ASK for, and a big chunk comes back empty or already dead.",
         "r/b2b_sales", "https://www.reddit.com/r/b2b_sales/comments/1uhqtas/i_compared_how_data_enrichment_tools_actually/",
         "Worth checking per provider, because they differ: Hunter, LeadMagic and Icypeas charge "
         "nothing when nothing resolves. The comparison below shows how each one bills a miss."),
        ("Nobody can check an accuracy claim before signing",
         "Every provider resolves between 94.7% and 100% of domains. That spread is not a buying signal.",
         "r/gtmengineering", "https://www.reddit.com/r/gtmengineering/comments/1vl0kht/independent_open_source_benchmark_of_company/",
         "Agreed, which is why the honest test is your own list. Running 200 of your real contacts "
         "through three providers costs a few dollars here and answers the question for your data "
         "rather than someone's benchmark."),
    ],
    "faq": [
        ("What can I use as input?",
         "A work email or a LinkedIn profile URL works everywhere. Some providers also take a name "
         "plus a company domain, and People Data Labs will accept a name with a location. The "
         "comparison below lists what each one accepts."),
        ("Do I pay when the person is not found?",
         "It depends on the provider, and it is the most useful thing on this page. Hunter charges "
         "only when a complete record comes back; LeadMagic and Icypeas are free on a no-match; "
         "others bill the attempt."),
        ("Why is one provider eighty times the price of another?",
         "The expensive end sells either phone numbers or coverage breadth. If you do not need "
         "direct dials, the cheap end returns the same title and employer."),
        ("Is enrichment the same as finding an email?",
         "No. Enrichment starts from an identifier you already have and fills in the profile; "
         "finding an email starts from a name and a company and resolves the address. They are "
         "separate jobs and separate prices."),
    ],
    "related": ("Find professional emails", "Verify an email before you send",
                "Find people by role, company or location", "Enrich a company from its domain"),
}

USE_CASE_PAGES[("data-enrichment-sales", "people-search")] = {
    "label": "Find people by role, company or location",
    "sentence": "People search API: find people by job title, company or location",
    "title": "People search API: {n} providers compared | treg.to",
    "lede": (
        "Search across companies for the people who match a role, a seniority, a location or a tech "
        "stack, and get back a list your agent can work with. {n} providers do this through one "
        "treg.to key. The trap is the billing unit: some charge per row returned, so an unbounded "
        "search is an unbounded bill."),
    "prompt": "Using treg, find 25 heads of growth at US SaaS companies with 50 to 200 employees. "
              "Show me the price first, keep the result set small, and give me name, title, company and LinkedIn URL.",
    "prompt_why": [
        ("Always cap the result set", "Several providers bill per row. “Find everyone” is a bill, not a query."),
        ("Search first, reveal second", "Some searches are free and only the contact details cost."),
        ("Filter on their fields", "Seniority, department, headcount and location are provider filters, not your post-processing."),
        ("Name the join key", "Company domain resolves cleanly. A company name does not, and returns the wrong people."),
    ],
    "result_noun": "row",
    "result_image": None,
    "what_is_heading": "What is a people search API?",
    "what_is": (
        "It queries a provider's index of working professionals by attributes rather than by name: "
        "job title, seniority, department, company, headcount, industry, location. It answers "
        "“who are the people like this”, where an enrichment API answers “who is this "
        "person”. Most of these return the person without contact details, which you then "
        "resolve separately."),
    "notes": [
        "Search and reveal are priced separately almost everywhere. Apollo's people search is free "
        "precisely because it returns no emails or phone numbers, Hunter's multi-domain search is "
        "free until you unlock an address, and Lusha's search returns masked previews. Budget for "
        "the reveal, not the search.",
        "Per-row billing is where the money goes. People Data Labs charges one credit for every "
        "record in the response, so a size=1000 call is a thousand credits; Icypeas charges 0.02 "
        "credits a row. Both are legitimate, but only one survives an agent that forgets to set a "
        "limit. Cap the result set in the prompt.",
        "The join key decides whether the results are right. LeadMagic and CompanyEnrich want a "
        "company domain; a bare company name is ambiguous and quietly returns people from the wrong "
        "company. Give the domain wherever you have it.",
    ],
    "voices_intro": (
        "The complaints here are less about accuracy than about access: the search that does not "
        "exist, the bill that scales with rows, and the seat licence. From ~180 Reddit and X posts "
        "in August 2026, vendor rings excluded."),
    "voices": [
        ("The primitive people actually want",
         "I need an open search option where I can find people across companies by job title (e.g., 'CTO' or 'Product Manager') and other filters.",
         "Reddit", "https://www.reddit.com/r/u_Icy_Data8505/comments/1n85jhw/people_search_tool_similar_to_clays_people_finder/",
         "That is this job. The comparison below marks which providers support unbounded search by "
         "title and location, and which can only list people at a company you already named."),
        ("Seat pricing locks small teams out",
         "zoominfo has the best mobile numbers by far, but at like 15k/seat minimum they're pricing out anyone who isn't enterprise",
         "r/CRM", "https://www.reddit.com/r/CRM/comments/1svj17o/zoominfo_vs_cognism_vs_apollo_which_one_and_why/",
         "There are no seats here and no minimum. Calls are metered per row or per call from a "
         "shared team balance that starts with $1.00 free, so a hundred-row test costs cents."),
        ("Doing it yourself gets the account banned",
         "scraping twitter directly got my accounts banned pretty fast. linkedin is even worse, flags you almost immediately.",
         "r/openclaw, 72 points", "https://www.reddit.com/r/openclaw/comments/1sft22e/kept_getting_my_accounts_banned_trying_to_get/",
         "The providers here run their own infrastructure, so your accounts are not in the loop. "
         "That is an operational answer, not a legal one: check each provider's terms for your use, "
         "because treg.to relays their answer and makes nothing lawful that was not."),
        ("Coverage outside the US and outside tech is a guess",
         "I've heard of ZoomInfo, Apollo, LeadSquared, IndiaMART but no idea about quality for Indian market specifically.",
         "r/b2bmarketing", "https://www.reddit.com/r/b2bmarketing/comments/1r08jcy/need_recommendations_for_b2b_contact_data/",
         "No comparison table can answer this, and anyone claiming otherwise is guessing. Per-row "
         "pricing makes the real test cheap: run the same 100-row query of your actual market "
         "through three providers and keep the one that holds up."),
    ],
    "faq": [
        ("Do these return email addresses?",
         "Usually not, and that is why the search is cheap or free. You get the person and then "
         "resolve the address with a separate call, which is a separate price."),
        ("How do I stop a search costing more than I expect?",
         "Set a limit. Providers that bill per row charge for every record they return, so a "
         "thousand-row response is a thousand charges. Ask the agent for a small result set first."),
        ("Can I search across all companies, or only within one?",
         "Both exist here, and they are different products. The comparison marks which providers "
         "support open search by title and location and which need a company first."),
        ("What is the cheapest way to build a list?",
         "Search on a free or per-row-cheap provider, filter down to the people you actually want, "
         "then spend on contact details only for those. The two-step flow is why search and reveal "
         "are priced separately."),
    ],
    "related": ("Find professional emails", "Enrich a person from an email or LinkedIn URL",
                "Build a company list by industry, size or tech", "Get a LinkedIn profile"),
}

USE_CASE_PAGES[("data-enrichment-sales", "enrich-a-company")] = {
    "label": "Enrich a company from its domain",
    "sentence": "Company enrichment API: firmographics from a domain",
    "title": "Company enrichment API: {n} providers compared | treg.to",
    "lede": (
        "Give your agent a domain and get the company behind it: industry, headcount, location, "
        "founding year, tech stack, funding, sometimes revenue. {n} providers do this through one "
        "treg.to key. Resolution is a solved problem, so the useful comparison is which fields come "
        "back filled, what a miss costs, and how fast."),
    "prompt": "Using treg, enrich these 30 domains into a table: company name, industry, headcount, "
              "country, founded year and tech stack. Show me the price first, and mark any field that came back empty.",
    "prompt_why": [
        ("Give the domain, not the name", "A domain maps to one company. A name is ambiguous and matches the wrong one."),
        ("Ask for the fields you need", "Some providers price per section requested. Asking for everything costs more."),
        ("Ask it to mark the blanks", "An empty field is information. A quietly guessed one is not."),
        ("Batch when you can", "Several providers have a bulk route at half the single-call price."),
    ],
    "result_noun": "company",
    "result_image": None,
    "what_is_heading": "What is a company enrichment API?",
    "what_is": (
        "It resolves a domain, company name or LinkedIn URL to a structured company record: legal "
        "name, industry, employee count, headquarters, founding year, and depending on the provider "
        "the technology stack, funding history, web traffic and social profiles. It is what turns a "
        "signup email domain into a qualified account."),
    "notes": [
        "Headcount, revenue and industry are inferred by most providers, not observed, and almost "
        "none of them mark which is which. A confidently wrong headcount does more damage in a "
        "scoring model than a blank one, so ask your agent to keep the empties visible.",
        "The billing units are genuinely different products. Akta prices per section of the record "
        "you request; CompanyEnrich charges one credit per call and five more for the workforce "
        "expansion; Hunter charges only when name, size and location all come back; Ocean.io's bulk "
        "lookup is half the price of its single enrichment. The word credit means something "
        "different at every vendor, which is why this page prices everything in dollars per call.",
        "Use the deterministic route when you have a domain. CompanyEnrich's by-domain lookup maps "
        "one domain to exactly one company, while its by-properties route is a fuzzy match at the "
        "same price. The Companies API returns an empty object and charges nothing when a domain "
        "has no company behind it.",
    ],
    "voices_intro": (
        "Everybody wants the same thing here, and the arguments are about what comes back and what "
        "it costs. From ~180 Reddit and X posts in August 2026, with the vendor rings removed."),
    "voices": [
        ("The ask, in the buyer's own words",
         "I'll give it a company name/website and it will return company size, industry, founded, market cap, maybe leadership info, etc.",
         "r/CRM, 17 points", "https://www.reddit.com/r/CRM/comments/1si1jbw/crm_enrichment_apis/",
         "That is exactly this job, and 19 providers do it. The comparison below is about which "
         "fields each one actually fills, because the ask is identical everywhere."),
        ("Resolution rate is not a buying signal",
         "Every provider resolves between 94.7% and 100% of domains. That spread is not a buying signal.",
         "r/gtmengineering", "https://www.reddit.com/r/gtmengineering/comments/1vl0kht/independent_open_source_benchmark_of_company/",
         "Right, so this page does not rank on it. Compare on what a filled record contains, what "
         "an empty one costs you, and the measured latency, which is where the real spread is."),
        ("A guessed field is worse than a blank one",
         "A pipeline that appends fields without validating them just produces confident-looking wrong data, which is worse than an empty field",
         "r/Data_Enrichment", "https://www.reddit.com/r/Data_Enrichment/comments/1vqoj48/what_is_data_enrichment/",
         "Most providers do not distinguish observed from inferred fields, and we will not pretend "
         "otherwise. Ask the agent to keep blanks blank, and treat headcount and revenue as "
         "estimates unless the provider says otherwise."),
        ("Credits are not comparable between vendors",
         "Every vendor on this list uses the word “credit,” and none of them mean the same thing",
         "r/Data_Enrichment", "https://www.reddit.com/r/Data_Enrichment/comments/1vrl2q4/data_enrichment_pricing_2026_august_update/",
         "Which is why every price on this page is in dollars per call, converted from each "
         "provider's own unit at their published rate, with the date we last verified it."),
    ],
    "faq": [
        ("What do I send in?",
         "A domain gives the highest match rate and is deterministic. Most providers also accept a "
         "company name, a LinkedIn URL or a work email, and a few take a stock ticker."),
        ("Which fields can I count on?",
         "Name, domain, industry, location and an employee range come back almost everywhere. Tech "
         "stack, funding, revenue and web traffic are provider-specific, and revenue in particular "
         "is usually modelled."),
        ("What happens when a domain has no company?",
         "It varies, and it is worth knowing before a batch: some return a 404, some an empty "
         "object, and the free-mail domains are refused outright. Several providers do not bill "
         "for a miss."),
        ("Is this how I qualify signups?",
         "Yes, that is the common use: turn the new user's work email domain into firmographics and "
         "route the account. One provider has a route that takes the email address directly."),
    ],
    "related": ("Build a company list by industry, size or tech", "Hiring, headcount and news signals",
                "Find people by role, company or location", "A company's funding rounds"),
}
