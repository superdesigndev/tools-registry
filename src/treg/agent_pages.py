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
    "youtube": "youtube.com",
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


USE_CASE_PAGES[("youtube-video", "get-a-video-s-transcript")] = {
    "label": "Get a video's transcript",
    "sentence": "YouTube transcript API: a video's captions as plain text",
    "title": "YouTube transcript API: {n} providers compared | treg.to",
    "lede": (
        "Give your agent a YouTube URL and get the spoken words back as text, ready to summarise, "
        "search or quote. {n} providers do this through one treg.to key, from {cheapest} a video. "
        "The official YouTube Data API cannot do it for a video you do not own, which is the whole "
        "reason this job has a price at all."),
    "prompt": "Using treg, get the transcript of https://www.youtube.com/watch?v=dQw4w9WgXcQ in "
              "English. Show me the price first, then summarise it into five bullet points.",
    "prompt_why": [
        ("Give it the URL", "Both providers take a watch or Shorts URL. No video id lookup first."),
        ("Name the language", "Ask for a language you know the video carries, or you get an empty result."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what it will spend."),
        ("Say what to do with it", "Transcript plus instruction in one turn beats fetching then pasting."),
    ],
    "result_noun": "transcript",
    "result_image": None,
    "voices_intro": (
        "This job has a large and unusually honest literature, because everyone starts by doing it "
        "themselves. From ~180 Reddit and X posts in August 2026, after excluding thirteen vendor "
        "and self-promotion clusters that were roughly half the corpus, these five recur."),
    "voices": [
        ("It works on a laptop and stops working on a server",
         "When I try to run it in the cloud, YouTube seems to block the IP.",
         "r/SaaS, 5 points", "https://www.reddit.com/r/SaaS/comments/1fgjjd1/looking_for_a_saas_tool_to_fetch_youtube_video/",
         "This is the single most repeated failure in the research, and it is structural: the "
         "unofficial route reads from your address, and datacentre ranges get blocked. A call "
         "through treg.to leaves the provider's infrastructure instead. What no table can tell you "
         "is whether a given provider's pool is clear today at your volume, which is why the "
         "observed success rates on this page are measured rather than promised."),
        ("Nobody can promise it still works next month",
         "it still feels like the whole feature could break the moment something changes on YouTube's end",
         "r/sideprojects, 5 points", "https://www.reddit.com/r/sideprojects/comments/1v4zk6t/is_there_a_stable_way_to_pull_youtube_transcripts/",
         "Correct, and no comparison table can tell you otherwise. The honest difference is who owns "
         "the repair: on the unofficial route it is you, at the moment it breaks, and here it is the "
         "provider, with the failure showing up as a billing line rather than an outage."),
        ("The proxy is the real cost of doing it yourself",
         "it needs ip address rotations (becuase youtube blocks transcript scrapers), so I set up a webshare proxy (costs like $3)",
         "r/n8n, 285 points", "https://www.reddit.com/r/n8n/comments/1pd5gbx/turn_any_youtuber_into_an_ai_agent_001run_using/",
         "A fair benchmark, and worth doing the arithmetic against: a proxy is a monthly floor you "
         "pay whether or not you pull a transcript, plus the maintenance. The prices here are per "
         "video with no floor, which wins at low volume and loses at very high volume."),
        ("An agent cannot watch a video, so the transcript is the adapter",
         "What's the best way to have an agent via API in my app auto-generate a transcript from a YouTube video",
         "@waynesutton on X", "https://x.com/i/status/2087026783615168992",
         "This framing was the largest single theme in the research, twelve posts. It is one call "
         "here: the agent turns a URL into text and then does the actual work in the same turn, "
         "which is what the prompt at the top of this page does."),
        ("The DIY integrations people build for this keep not sticking",
         "I built a couple of MCPs using APIs etc, but they didn't work out so well for pulling transcripts.",
         "r/OpenAI, 25 points", "https://www.reddit.com/r/OpenAI/comments/1uq73nr/youtube_transcript_getter_extension_for_obsidian/",
         "Worth being precise about why, because it is not the MCP part. It is that the transcript "
         "source underneath was unofficial. Nothing here changes that for the two paid providers; "
         "what changes is that maintaining it is their job, and you can compare what they charge "
         "for the video where it fails."),
    ],
    "q_cheapest": "Which YouTube transcript API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What is a YouTube transcript API?",
    "what_is": (
        "It returns the caption track YouTube already holds for a video: the auto-generated one the "
        "speech recogniser produced, or the human-uploaded one if the channel added it. You get the "
        "text, usually with timestamps, in SRT, plain text or JSON. It is not transcription; nobody "
        "here is running speech recognition on the audio, so a video with no caption track has "
        "nothing to return."),
    "notes": [
        "Google's own Data API is missing from this comparison on purpose. Its captions.download "
        "method only works on videos the connected account owns, so it cannot read anyone else's, "
        "and that is why a job the platform does for free in the player costs money here.",
        "ScrapeCreators takes a cache_max_age parameter: if a cached copy is newer than the number "
        "of days you pass, it returns that for 0 credits instead of scraping again. On a rerun over "
        "the same videos, this is the difference between paying and not.",
        "Ask for a language the video actually has. ScrapeCreators returns transcript: null when "
        "your two-letter code is missing rather than falling back; TikHub returns the list of "
        "available caption tracks if you send no language code at all, which is the safer first call.",
    ],
    "faq": [
        ("How much does a YouTube transcript cost?",
         "A fraction of a cent per video at the provider's own rate, with $0.000 added by treg.to. "
         "The live prices are in the comparison above, and one provider bills only on success while "
         "the other bills the attempt."),
        ("Why not use youtube-transcript-api myself?",
         "You can, and it works until it does not: the library reads an undocumented endpoint from "
         "your IP, and datacentre ranges get blocked, which is the failure everybody hits at the "
         "point they move off a laptop. These providers run their own IP pools, and the failure "
         "becomes a billing line instead of an outage."),
        ("Does this work on Shorts and live streams?",
         "Shorts, yes; both providers accept a Shorts URL. A live stream only has captions once the "
         "recording is processed, and a video whose channel disabled captions has no track to "
         "return at any price."),
        ("Can I get transcripts in bulk?",
         "Yes, it is one call per video and you tell your agent the list. There is no batch "
         "endpoint, so the cost is linear, and the billing unit in the comparison tells you which "
         "of the two charges for a video that turns out to have no captions."),
    ],
    "related": ("Video details, views and stats", "A video's comments",
                "Transcripts of X and Facebook video posts", "Search videos and channels by keyword"),
}

USE_CASE_PAGES[("youtube-video", "video-details-views-and-stats")] = {
    "label": "Video details, views and stats",
    "sentence": "YouTube video statistics: views, likes and metadata by video id",
    "title": "YouTube video statistics API: {n} providers | treg.to",
    "lede": (
        "Views, likes, comment count, duration, title, description, tags and publish date, for any "
        "public video. {n} providers do this through one treg.to key, and one of them is Google's "
        "own API on the account you already have, which is free but rationed."),
    "prompt": "Using treg, get the view count, like count and publish date for these 30 YouTube "
              "video ids and put them in a table sorted by views. Show me the price first.",
    "prompt_why": [
        ("Give it video ids", "The v= parameter of the watch URL. Most providers take the id, not the URL."),
        ("Ask for a batch", "Google's API takes 50 ids in one call for the price of one."),
        ("Name the fields you want", "Views, likes and duration live in different parts of the response."),
        ("Say which account to use", "Your own Google connection is free; the paid providers need no account."),
    ],
    "result_noun": "video",
    "result_image": None,
    "voices_intro": (
        "Almost nobody complains about the price of this call. They complain about the daily cap, "
        "and about whether the number they got is today's number. From ~180 Reddit and X posts in "
        "August 2026, with the vendor clusters excluded, these five recur."),
    "voices": [
        ("The free quota runs out in the middle of something",
         "i exceeded youtubes API quota. womp womp. Anyway, it should be back and updating properly at midnight pacific time.",
         "r/Destiny, 575 points", "https://www.reddit.com/r/Destiny/comments/1vafc5k/dave_comment_tracker_i_made/",
         "The daily budget resets at midnight Pacific, so a bug in the morning costs the rest of the "
         "day. Reading a video costs 1 unit of 10,000 and batches 50 ids into that one unit, so most "
         "people who hit the wall were spending it on search. The paid rows have no daily ceiling; "
         "treg.to shows you both and you pick, it does not switch over on its own."),
        ("You cannot tell whether you ran out or something else broke",
         "The strange thing is, my daily usage counter (which I'm tracking in the script) shows that I'm nowhere near the daily quota limit.",
         "r/pythontips, 2 points", "https://www.reddit.com/r/pythontips/comments/1epf9dk/youtube_api_quota_issue_despite_not_reaching_the/",
         "The recurring complaint is not running out, it is not knowing why. We cannot fix Google's "
         "quota accounting. What a metered call gives you instead is a definite price before it goes "
         "and a definite outcome after, so the ambiguous middle disappears."),
        ("Leaving your laptop changes the failure mode",
         "Apparently that IP address is for AWS.",
         "r/MailChimp, 6 points", "https://www.reddit.com/r/MailChimp/comments/1g9ft0z/youtube_api_error/",
         "The error underneath that thread is API_KEY_IP_ADDRESS_BLOCKED, and it is the same story as "
         "the transcript job: it worked until it was deployed. A call through treg.to leaves the "
         "provider's infrastructure, not your host's. Whether your own host is blocked is a property "
         "of your host, and no comparison table has that column."),
        ("The counts themselves are not stable at fine granularity",
         "during certain intervals, views increase without likes/comments scaling proportionally, while at the hourly aggregate level everything looks perfectly normal",
         "r/HiddenTrueCrimeChat, 23 points", "https://www.reddit.com/r/HiddenTrueCrimeChat/comments/1prow46/ive_been_logging_htc_youtube_views_likes_and/",
         "This is the honest limit of every row on this page. View counts are aggregated and "
         "sometimes revised downward by YouTube itself, and every provider here is downstream of "
         "that. A table can compare price and measured success rate; it cannot tell you whose number "
         "is right, because on some hours no number is."),
        ("The folk remedy is more keys, which is not a plan",
         "Google youtube API has a quota limits, so I added over 45 API",
         "r/MetaRayBanDisplay, 4 points", "https://www.reddit.com/r/MetaRayBanDisplay/comments/1tyuxrx/viewtube_v20_question_for_all_google_youtube_api/",
         "Forty-five keys is a lot of Google projects to own, and it is the shape people reach for "
         "when the only lever is a daily cap. The alternative here is a per-call price with no cap "
         "and no rotation to maintain. Spend the free quota first if you have it."),
    ],
    "q_cheapest": "Which YouTube video stats API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What does a YouTube video stats API return?",
    "what_is": (
        "One video's public record: the snippet (title, description, tags, channel, publish date), "
        "the statistics (view count, like count, comment count) and the content details (duration, "
        "definition, caption availability). It is the same data the watch page shows, as JSON. "
        "Watch time, impressions, click-through rate and audience retention are not here; those are "
        "YouTube Analytics, and only the channel owner can read them."),
    "notes": [
        "Google's Data API charges 1 quota unit whether you pass one video id or fifty, out of a "
        "default 10,000 units a day. Batching is not an optimisation here, it is a fifty-fold "
        "difference in what a day's quota buys you.",
        "The billing units are not comparable. Bright Data bills per record delivered, TikHub and "
        "Just One API bill per successful call, ScrapeCreators bills the call whether or not it "
        "found the video. A run over a list with dead ids costs differently on each.",
        "Like counts are returned as the channel chose to expose them, and a channel that hides its "
        "like count returns no field rather than a zero. Treat a missing field and a zero as "
        "different answers when you aggregate.",
    ],
    "faq": [
        ("Is the YouTube Data API free?",
         "Yes, on your own Google account, and treg.to never meters a call on your own key. What it "
         "is not is unlimited: 10,000 quota units a day by default, which this method spends 1 at a "
         "time. The paid providers exist for when that runs out."),
        ("Can I get watch time or retention?",
         "No. Those are YouTube Analytics numbers and only the channel's owner can read them, "
         "through a connected account. Everything on this page is the public record of the video."),
        ("Do I need a Google account for this?",
         "Only for the free row. The other providers are called on treg.to's keys and billed per "
         "call from your prepaid balance, so you can read a video's stats with no Google project at all."),
        ("How current are the view counts?",
         "They are what the platform is publishing at the moment of the call. YouTube itself updates "
         "public view counts on its own schedule, so two providers reading a minute apart can "
         "legitimately disagree on a fast-moving video."),
    ],
    "related": ("Get a video's transcript", "A video's comments",
                "A channel's profile and lifetime stats", "Search videos and channels by keyword"),
}

USE_CASE_PAGES[("youtube-video", "a-channel-s-profile-and-lifetime-stats")] = {
    "label": "A channel's profile and lifetime stats",
    "sentence": "YouTube channel stats API: subscribers, total views and profile",
    "title": "YouTube channel stats API: {n} providers | treg.to",
    "lede": (
        "Subscriber count, lifetime views, video count, description, country and links, for any "
        "public channel. {n} providers do this through one treg.to key, including Google's own API "
        "on your account, which resolves an @handle without you having to find the UC id first."),
    "prompt": "Using treg, get the subscriber count, total views and video count for @MrBeast and "
              "@mkbhd, and tell me which has more views per video. Show me the price first.",
    "prompt_why": [
        ("A handle is enough", "Google's API resolves @handle directly. The scrapers vary; some need the UC id."),
        ("Ask for the pair you need", "Profile and statistics are separate parts of the response. Name both."),
        ("Ask it to do the arithmetic", "Views per video and subscribers per video are one line, not a spreadsheet."),
        ("Say whose key to use", "Your own Google connection is free and never metered."),
    ],
    "result_noun": "channel",
    "result_image": None,
    "voices_intro": (
        "Two things stop people here, and neither is the price: how fresh the numbers are, and "
        "whether touching the API can hurt the channel they already run. From ~180 Reddit and X "
        "posts in August 2026, with the vendor clusters excluded."),
    "voices": [
        ("One response, and its fields disagree about how current they are",
         "So one field in the response was stale while the other two were fresh.",
         "r/googlecloud, 1 point", "https://www.reddit.com/r/googlecloud/comments/1tjofun/youtube_data_api_v3_channelstatisticsviewcount/",
         "Documented day by day in that thread: lifetime viewCount frozen for over a day while "
         "subscriberCount and videoCount kept moving, inside Google's own API. Every provider on "
         "this page reads from the same well, so no comparison table can rank them on whether a "
         "number is today's. Record when you pulled and treat a flat total as suspect, not as news."),
        ("People are afraid the API itself will hurt their channel",
         "Is there any risk that my main channel could be shadowbanned, restricted, or lose its algorithmic reach just by using the official API",
         "r/youtube, 3 points", "https://www.reddit.com/r/youtube/comments/1vlva1i/question_regarding_youtube_data_api_v3_readonly/",
         "A fair question that almost nobody answers in writing. Reading public channel data through "
         "one of the paid providers here happens server side on a credential that has nothing to do "
         "with your creator account, so there is no account of yours in the request at all. If you "
         "use the free Google row instead, that is your connected account, by design."),
        ("Quota is why people stop tracking channels, not why they start",
         "spending more api quota to start tracking a third channel from scratch",
         "r/HiddenTrueCrimeChat, 20 points", "https://www.reddit.com/r/HiddenTrueCrimeChat/comments/1q1eryk/are_htcs_views_real_what_im_measuring_with_public/",
         "A researcher dropped a control channel because a third one cost more quota than they had. "
         "A channel read is 1 unit, so the free cap is roughly 10,000 snapshots a day; what actually "
         "burns it is polling often. Paid rows have no daily ceiling, which is the whole difference "
         "for anything long running."),
        ("Whether you may keep and show the data is a separate question",
         "Can I legally use YouTube channel profile pictures and public stats from the YouTube API in a collectible card game?",
         "r/legaladvice", "https://www.reddit.com/r/legaladvice/comments/1v6c5ni/can_i_legally_use_youtube_channel_profile/",
         "No comparison table can answer this and we are not going to pretend otherwise. Public and "
         "reusable are different things; storing, republishing and putting someone's avatar on a "
         "product are governed by YouTube's terms and by the law where you are. Read the terms, "
         "and take advice if money is involved."),
        ("Nobody wants a channel endpoint, they want the channel",
         "I want to archive everything from the videos, to the likes, view and comments on it, lives and the same for it",
         "r/DataHoarder, 20 points", "https://www.reddit.com/r/DataHoarder/comments/1s426uf/need_help_with_youtube_channel_archivescraping/",
         "That is four jobs chained: the profile, the uploads list, per video stats, then comments. "
         "Asking Google for part=contentDetails hands you the uploads playlist id, which is the "
         "cheap way into the catalogue without spending a search. Your agent runs the chain; "
         "treg.to prices each step."),
    ],
    "q_cheapest": "Which YouTube channel stats API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What counts as a channel's lifetime stats?",
    "what_is": (
        "The three totals a channel publishes about itself: subscribers, views across every video, "
        "and how many public videos it has. Alongside them come the profile fields: title, "
        "description, custom URL, country, keywords, banner and thumbnail. These are cumulative "
        "totals, not a time series, so measuring growth means reading them repeatedly and keeping "
        "your own history."),
    "notes": [
        "The three providers disagree about what identifies a channel. Google's API takes exactly "
        "one of mine, id or forHandle, so an @handle resolves in a single call; ScrapeCreators "
        "accepts a channel id, a handle or a URL; TikHub needs the UC channel id, so a handle costs "
        "you a lookup first.",
        "Subscriber counts come back as YouTube publishes them, which is rounded once a channel is "
        "past a thousand. Two providers reading the same channel will agree on the rounded "
        "figure and neither has the exact one, because the platform does not expose it.",
        "Asking Google for part=contentDetails also returns the uploads playlist id, which is how "
        "you list a channel's videos without spending a search call. It is the cheapest path from a "
        "channel to its catalogue by a wide margin.",
    ],
    "faq": [
        ("Can I get a channel's subscriber history?",
         "Not from any of these. They return the current totals, because that is all YouTube "
         "publishes. A growth curve means calling on a schedule and storing what you get."),
        ("How do I find a channel by name?",
         "Search for it first, then read the profile. Channel search is a separate job on this menu "
         "and returns the ids these endpoints take."),
        ("Is Google's API really free here?",
         "Yes. It runs on the Google account you connect, so treg.to relays the call and meters "
         "nothing. Only calls on treg.to's own provider keys are billed."),
        ("Can I read another channel's analytics?",
         "No, and no provider can. Watch time, traffic sources and audience data are visible only to "
         "the channel's owner through a connected account. Everything here is public."),
    ],
    "related": ("Video details, views and stats", "Search videos and channels by keyword",
                "A creator's profile and stats", "Get a video's transcript"),
}

USE_CASE_PAGES[("youtube-video", "search-videos-and-channels-by-keyword")] = {
    "label": "Search videos and channels by keyword",
    "sentence": "YouTube search API: find videos and channels by keyword",
    "title": "YouTube search API: {n} providers compared | treg.to",
    "lede": (
        "Run a YouTube search from your agent and get the results as data: titles, video ids, "
        "channels, publish dates and thumbnails, with the filters the site itself offers. {n} "
        "providers do this through one treg.to key. Google's own API is free on your account and "
        "the single most quota-expensive call it has, which is why the others are here."),
    "prompt": "Using treg, search YouTube for videos about home espresso uploaded in the last month, "
              "sorted by view count. Show me the price first, then give me the top 20 with links.",
    "prompt_why": [
        ("Say what to search for", "A keyword, the way you would type it into the site's own box."),
        ("Name the filters", "Upload date, duration, type and sort order are parameters on every provider."),
        ("Ask for videos or channels", "The same query returns either. Say which, or you get a mix."),
        ("Ask for stats separately", "Search results carry no view counts. The agent fetches those next."),
    ],
    "result_noun": "result",
    "result_image": None,
    "voices_intro": (
        "This is the job where the free route runs out first, and the people who have hit it are "
        "unusually precise about why. From ~180 Reddit and X posts in August 2026, with the vendor "
        "clusters excluded."),
    "voices": [
        ("One number decides this whole page",
         "The official Data API v3 search.list costs 100 units/call against a 10k/day quota, which dies almost immediately once you're polling multiple keyword combos",
         "r/webscraping, 7 points", "https://www.reddit.com/r/webscraping/comments/1uaq3lk/keywordsearching_youtube_at_scale_official_api_vs/",
         "Exactly right, and worth stating as arithmetic: 100 units a search against a 10,000 unit "
         "day is about 100 searches for the entire project, before you spend anything hydrating the "
         "results with view counts. Every other call on this page costs 1 unit. That gap is the "
         "reason the paid rows on this page exist."),
        ("Trading a hard cap for an unknown one",
         "Roughly what request rate gets you rate-limited / soft-banned on the InnerTube route?",
         "r/webscraping, 7 points", "https://www.reddit.com/r/webscraping/comments/1uaq3lk/keywordsearching_youtube_at_scale_official_api_vs/",
         "Nobody in the research knew, including the person who asked, and we do not either. What we "
         "can say is that the request leaves the provider's infrastructure rather than yours, and "
         "that the measured success rates shown here are live traffic rather than a claim. That is the "
         "honest version of an answer to this question."),
        ("Search results are not what a person sees",
         "I want an API or scraper which helps me replicate the same results when I do a search on mobile. Is there a way?",
         "r/n8n, 1 point", "https://www.reddit.com/r/n8n/comments/1rkmsj4/youtube_scraper/",
         "No, not exactly, from anyone. On the site the ranking is personalised, regional and "
         "signed in; these calls are none of those. Pass a region and language to get nearer a "
         "market. No comparison table can tell you whose ordering matches your users, so run your "
         "own query through two providers for a cent and diff the two lists."),
        ("An agent will write the expensive version by default",
         "The problem is my playlist contains 1.2k songs but I can only transfer around 60 songs and after that its an error",
         "r/learnpython", "https://www.reddit.com/r/learnpython/comments/1hk199a/help_needed_exceeded_youtube_api_quota_while/",
         "ChatGPT wrote them a loop that runs one search per track, which at 100 units each is a dead "
         "project at song 100. This is the failure this page is really about: the code is fine and "
         "the quota model is what nobody read. Tell your agent the price first and it can tell you "
         "the run is unaffordable before it starts."),
        ("The actual job is a sweep, not a search",
         "So I built a Python script to scrape YouTube Shorts from a specific niche (AI kids bedtime stories), pull stats, transcripts",
         "r/shortsAlgorithm, 78 points", "https://www.reddit.com/r/shortsAlgorithm/comments/1rh2bek/i_scraped_53_youtube_shorts_from_a_single_niche/",
         "Search, then details, then transcripts, over a niche, on a schedule. That is three jobs on "
         "this menu chained, and it is what almost everyone doing this actually wants. Per call "
         "pricing makes the sweep something you can size in advance instead of a quota you discover "
         "the edge of."),
    ],
    "q_cheapest": "Which YouTube search API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What does a YouTube search API return?",
    "what_is": (
        "A page of search results as structured records: video id, title, description snippet, "
        "channel name and id, publish date and thumbnails, plus a token to fetch the next page. It "
        "is the site's own ranking, not a fresh index, so results move as YouTube's does and two "
        "calls minutes apart can legitimately differ."),
    "notes": [
        "Search results carry no statistics anywhere. To rank what you found by views you take the "
        "video ids and call the video details job, which on Google's own API costs 1 quota unit for "
        "50 ids. Budget for two steps, not one.",
        "Google's search method costs 100 quota units against a default 10,000 a day, so about 100 "
        "searches exhausts a day for the whole project. Every other call on this page costs 1. That "
        "single number is why paid search providers exist at all.",
        "SerpApi's parameter is search_query, not q. Sending q to its YouTube engine returns nothing "
        "rather than an error, which is the kind of failure that looks like an empty result set.",
    ],
    "faq": [
        ("Why does YouTube search cost so much quota?",
         "Google prices the search method at 100 units against a 10,000 unit daily default, roughly "
         "100 searches a day for an entire project. Reading a video, a channel or a comment thread "
         "costs 1. A quota increase means an application and a review."),
        ("Can I search a single channel's videos?",
         "Yes. Google's search takes a channelId, and one TikHub endpoint searches within a channel "
         "by id. The comparison above names which endpoint does which."),
        ("Do search results include view counts?",
         "No, on any provider. That is a second call to the video details job, which is cheap and "
         "batches, so ask your agent for both and it will chain them."),
        ("Is this the same ranking a user sees?",
         "Close, but not guaranteed. Results are personalised and regional on the site, and these "
         "calls are not signed in as you. Pass a region and language to get nearer a given market."),
    ],
    "related": ("Video details, views and stats", "A channel's profile and lifetime stats",
                "Trending videos", "Find creators by keyword"),
}

USE_CASE_PAGES[("youtube-video", "a-video-s-comments")] = {
    "label": "A video's comments",
    "sentence": "YouTube comment scraper: every comment on a video, as data",
    "title": "YouTube comment scraper API: {n} providers | treg.to",
    "lede": (
        "Pull a video's comments with authors, like counts, timestamps and replies, so your agent "
        "can read the audience instead of you scrolling. {n} providers do this through one treg.to "
        "key, and Google's own API is free on the account you already have."),
    "prompt": "Using treg, get the comments on https://www.youtube.com/watch?v=dQw4w9WgXcQ sorted by "
              "relevance. Show me the price first, then group them into the five things people complain about.",
    "prompt_why": [
        ("Give it the video", "A URL or a video id, depending on the provider. Both are one field."),
        ("Choose the sort", "Relevance surfaces the comments people upvoted. Time gets you the newest."),
        ("Ask for the analysis, not the dump", "A thousand comments is not an answer. Ask for the themes."),
        ("Say how deep to go", "Replies are nested and paginated. Top-level threads are usually enough."),
    ],
    "result_noun": "comment",
    "result_image": None,
    "voices_intro": (
        "The people doing this job are not chasing volume, they are chasing the forty comments that "
        "matter. From ~180 Reddit and X posts in August 2026, with the vendor clusters excluded, "
        "these five are what they actually complain about."),
    "voices": [
        ("The data is easy, the filtering is the job",
         "a video with 3,000 comments might have 40 that are actually useful to me and the rest is noise",
         "r/claude, 3 points", "https://www.reddit.com/r/claude/comments/1ragfjd/im_building_a_youtube_comment_filtering_tool_with/",
         "The most useful document in the whole research pass. They are on version six of that "
         "filter and get about 50% agreement with their own judgment. No provider on this page fixes that, "
         "and any that claims to is selling you something: getting the comments is the cheap half."),
        ("YouTube gives you no way to search or export a comment section",
         "I find myself wanting to search youtube comments all the time. Because youtube lacks this feature (for some reason)",
         "r/SideProject, 2 points", "https://www.reddit.com/r/SideProject/comments/1uzurwc/made_a_free_chrome_extension_that_lets_you_search/",
         "True, and it is the whole reason this job exists. Every provider here hands back the "
         "comments as records with author, likes and timestamps, so searching, sorting and grouping "
         "them becomes something your agent does rather than something the site has to offer."),
        ("Quota is what stops a long-running tracker",
         "it would also mean spending more api quota to start tracking a third channel from scratch",
         "r/HiddenTrueCrimeChat, 20 points", "https://www.reddit.com/r/HiddenTrueCrimeChat/comments/1q1eryk/are_htcs_views_real_what_im_measuring_with_public/",
         "A researcher dropping a control channel because a third one costs more quota than they have "
         "is the clearest argument on this page. Google's route is free and rationed; the paid rows "
         "have no daily ceiling, and you can mix them, spending quota first and paying past it."),
        ("What comes back is not stable between two pulls",
         "How has youtube given me the wrong comments section on a video bro 💀",
         "r/Quadeca, 19 points", "https://www.reddit.com/r/Quadeca/comments/1mhjev8/how_has_youtube_given_me_the_wrong_comments/",
         "Comment sections move: held-for-review, author-deleted and creator-hidden comments differ "
         "between two calls minutes apart, and the sort you ask for changes which arrive first. No "
         "provider here, and no comparison table, can tell you that you got all of them. Pin the "
         "sort order and record when you pulled."),
        ("People want the whole channel, not one video",
         "I want to archive everything from the videos, to the likes, view and comments on it, lives and the same for it",
         "r/DataHoarder, 20 points", "https://www.reddit.com/r/DataHoarder/comments/1s426uf/need_help_with_youtube_channel_archivescraping/",
         "That is several jobs chained, not one call: list the channel's videos, then pull comments "
         "per video, then paginate each. Your agent can run that loop and treg.to prices every "
         "step, but it does not run the loop for you, and on a large channel the bill is the sum of "
         "the parts."),
    ],
    "q_cheapest": "Which YouTube comment API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What does a YouTube comment scraper return?",
    "what_is": (
        "The public comment threads on a video: each comment's text, author name and channel, like "
        "count, published and updated times, and the replies underneath it. Comments are paginated, "
        "so a video with tens of thousands of them is many calls, and the order you ask for changes "
        "which ones arrive first."),
    "notes": [
        "Google's commentThreads method returns top-level threads with up to 5 replies inlined. "
        "Past that you fetch the rest by parent id, so a thread with hundreds of replies is a "
        "second and third call rather than a deeper response.",
        "A video with comments turned off answers 403 commentsDisabled on Google's API rather than "
        "an empty list. Treat that as a distinct outcome; the scraping providers signal it "
        "differently, and on a per-call biller you have still paid for the attempt.",
        "Bright Data bills per comment delivered, not per video. On a heavily commented video that "
        "is a materially different bill from a per-call provider, in either direction "
        "depending on how many you actually pull.",
    ],
    "faq": [
        ("Can I download all the comments on a video?",
         "Yes, by paginating. It is one call per page on every provider here, so a heavily "
         "commented video costs proportionally more. The comparison shows the per-call and "
         "per-record rates side by side."),
        ("Does this include replies?",
         "Top-level threads come back with their first replies attached. Deeper replies are a "
         "further call per thread, which is worth knowing before you ask an agent for everything."),
        ("Is scraping YouTube comments allowed?",
         "The comments are public and Google's own API serves them, which is the route this page "
         "recommends first. The third-party providers read the public page; you are responsible for "
         "what you do with the data, including the privacy law that applies to you."),
        ("Can I get comments from a whole channel?",
         "Not in one call. List the channel's videos first, then fetch comments per video. Your "
         "agent can chain that; treg.to prices each step but does not run the loop for you."),
    ],
    "related": ("Get a video's transcript", "Video details, views and stats",
                "Mine the comments", "A channel's profile and lifetime stats"),
}
