#!/usr/bin/env python3
"""Relevance benchmark for catalog search — the labeled replay behind the 2026-08-20 scoring change.

    uv run --frozen python scripts/search_bench.py

Thirty real-shaped queries, each labeled with the endpoint ids that genuinely answer it (any hit
counts — most jobs have several correct providers, and the bench must not punish picking a valid
one). Two styles on purpose:

  - SENTENCES — how agents actually query (the two 2026-08-19 SearchMiss rows are the first two,
    verbatim). This is the class the old all-words-must-match rule zeroed.
  - SHORT — the 2–3 word refinements the old rule was designed for. These are the regression guard:
    the new scoring must not lose what the old one did well.

Compares the shipped `catalog_store.search` against the pre-change algorithm (reimplemented inline,
frozen at its 2026-08-19 shape) on zero-result rate, hit@1, hit@8 and MRR@8. Pure relevance only:
the evidence rerank (`rerank`) sits above both engines unchanged and is out of scope here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from treg import catalog_store as cs  # noqa: E402


# ---- the frozen pre-change engine (every token must match; score = sum of field weights) --------
def search_legacy(query: str, cat, limit: int = 25):
    tokens = cs._tokens(query)
    if not tokens:
        return [], 0
    scored = []
    for ep in cat.endpoints:
        fields = cs._haystacks(ep, cat)
        score = 0
        for tok in tokens:
            best = max((w for w, text in fields if tok in text), default=0)
            if not best:
                break
            score += best
        else:
            scored.append((ep, score))
    scored.sort(key=lambda row: (-row[1], row[0]["tier"] != "core", not row[0]["verified"], row[0]["id"]))
    return scored[:max(limit, 0)], len(scored)


# ---- labeled queries -----------------------------------------------------------------------------
# (style, query, truth ids). A `family:` truth expands at runtime to every id containing ALL its
# words — used where a job is served by a whole family and pinning one id would be arbitrary.
CASES = [
    # -- sentences: agent voice (first two are the real SearchMiss rows, verbatim) --
    ("sentence", "company job postings hiring open jobs linkedin",
     {"apollo.companies.jobs", "apify.linkedin.search.jobs", "leadmagic.x.jobs-search-v3"}),
    ("sentence", "company search filter by location industry headcount growth",
     {"apollo.companies.search", "pdl.companies.search", "crunchbase.companies.search",
      "thecompaniesapi.companies.search", "pdl.x.company-search-post"}),
    ("sentence", "find work email address from person name and company domain",
     {"hunter.people.email.find", "leadmagic.people.email.find"}),
    ("sentence", "verify if an email address is valid and deliverable",
     {"hunter.people.email.verify", "leadmagic.people.email.verify"}),
    ("sentence", "get tiktok user profile with followers and bio",
     {"tikhub.tiktok.user.profile", "justoneapi.tiktok.user.profile",
      "scrapecreators.tiktok.user.profile", "tiktok.tiktok.user.profile"}),
    ("sentence", "backlinks profile summary for a competitor domain",
     "family:web.backlinks.summary"),
    ("sentence", "google search engine results for a keyword",
     {"dataforseo.x.serp-google-organic-live-advanced", "serpapi.x.google-search"}),
    ("sentence", "monthly search volume and cpc for a list of keywords",
     "family:google.keywords.volume"),
    ("sentence", "scrape the comments on an instagram post",
     {"instagram.instagram.post.comments", "scrapecreators.instagram.post.comments"}),
    ("sentence", "search the facebook ad library for a competitor's ads",
     {"scrapecreators.x.v1-facebook-adlibrary-search-ads", "meta-ad-library.meta-ads.library.search",
      "scrapecreators.x.v1-facebook-adlibrary-company-ads"}),
    ("sentence", "what technologies and software does a website use",
     {"dataforseo.x.domain-analytics-technologies-domain-technologies-live"}),
    ("sentence", "estimate monthly website traffic visitors for a domain",
     {"akta.x.website-traffic", "dataforseo.x.dataforseo-labs-google-bulk-traffic-estimation-live"}),
    ("sentence", "enrich a person profile from their linkedin url",
     "family:people.enrich"),
    ("sentence", "download the transcript or captions of a youtube video",
     {"justoneapi.x.youtube-get-video-captions-v1", "tikhub.x.youtube-web-v2-get-video-captions",
      "youtube.x.youtube-captions-download", "dataforseo.x.serp-youtube-video-subtitles-live-advanced"}),
    ("sentence", "current market price of a cryptocurrency like bitcoin",
     {"coingecko.simple.price", "coingecko.coins.markets", "tiingo.crypto.prices"}),
    ("sentence", "real time stock quote for a ticker symbol",
     {"finnhub.quote", "twelvedata.quote"}),
    ("sentence", "search reddit posts inside a subreddit",
     {"justoneapi.x.reddit-search-v1", "brightdata.x.reddit-posts",
      "scrapecreators.x.v1-reddit-subreddit-search"}),
    ("sentence", "trustpilot reviews of a company",
     {"brightdata.x.trustpilot-reviews", "dataforseo.x.business-data-trustpilot-reviews-task-post"}),
    ("sentence", "google maps local business listings search",
     {"dataforseo.x.serp-google-maps-live-advanced", "serpapi.x.google-maps",
      "dataforseo.x.business-data-business-listings-search-live", "serpapi.x.google-local"}),
    ("sentence", "list the employees of a company on linkedin",
     {"justoneapi.x.linkedin-get-company-employees-v1"}),
    ("sentence", "amazon product details by asin number",
     {"dataforseo.x.merchant-amazon-asin-live-advanced", "brightdata.x.amazon-products",
      "justoneapi.x.amazon-get-product-detail-v1"}),
    ("sentence", "trending repositories on github this week",
     {"scrapecreators.x.v1-github-trending-repositories"}),
    ("sentence", "law firm job openings hiring signal",
     {"apollo.companies.jobs", "leadmagic.x.jobs-search-v3", "apify.linkedin.search.jobs",
      "predictleads.companies.job_openings", "predictleads.jobs.discover"}),
    ("sentence", "K&L Gates company lookup", "family:companies.enrich"),
    ("sentence", "resolve company name to linkedin slug",
     {"scrapecreators.x.v1-linkedin-company", "apollo.companies.enrich",
      "leadsforge.people.identity.resolve.bulk", "fiber-ai.people.identity.resolve",
      "icypeas.people.identity.resolve"}),
    # -- short: the refinement style the old rule served well (regression guard) --
    ("short", "tiktok comments", "family:tiktok comment"),
    ("short", "ad library",
     {"scrapecreators.x.v1-tiktok-ad-library-search", "meta-ad-library.meta-ads.library.search",
      "scrapecreators.x.v1-facebook-adlibrary-search-ads"}),
    ("short", "email finder", {"hunter.people.email.find", "leadmagic.people.email.find"}),
    ("short", "backlinks", "family:web.backlinks"),
    ("short", "google serp", {"dataforseo.x.serp-google-organic-live-advanced", "serpapi.x.google-search"}),
    ("short", "instagram trending reels", {"scrapecreators.x.v1-instagram-reels-trending"}),
    ("short", "company enrich", "family:companies.enrich"),
    ("short", "youtube video comments",
     {"dataforseo.x.serp-youtube-video-comments-live-advanced", "brightdata.x.youtube-comments"}),
]

K = 8


def expand(truth, cat) -> set[str]:
    if isinstance(truth, set):
        return truth
    words = truth.removeprefix("family:").split()
    return {i for i in cat.by_id if all(w in i for w in words)}


def run(engine, cat):
    per_case, zero = [], 0
    for style, q, truth in CASES:
        want = expand(truth, cat)
        rows, total = engine(q, cat, K)
        ids = [ep["id"] for ep, _ in rows]
        rank = next((i + 1 for i, x in enumerate(ids) if x in want), None)
        if total == 0:
            zero += 1
        per_case.append((style, q, rank, total))
    return per_case, zero


def summarize(name, per_case, zero, style=None):
    rows = [r for r in per_case if style in (None, r[0])]
    n = len(rows)
    hit1 = sum(1 for _, _, rank, _ in rows if rank == 1)
    hitk = sum(1 for _, _, rank, _ in rows if rank)
    mrr = sum(1 / rank for _, _, rank, _ in rows if rank) / n
    print(f"  {name:8} n={n:2}  zero-results={sum(1 for *_, t in rows if t == 0):2}  "
          f"hit@1={hit1/n:5.0%}  hit@{K}={hitk/n:5.0%}  MRR@{K}={mrr:.3f}")


def main() -> None:
    cat = cs.load()
    bad = [q for _, q, t in CASES if not expand(t, cat)]
    if bad:
        sys.exit(f"labels name no existing endpoint: {bad}")

    engines = [("legacy", search_legacy), ("current", cs.search)]
    results = {name: run(fn, cat) for name, fn in engines}

    for style in ("sentence", "short", None):
        print(f"\n== {style or 'ALL'} ==")
        for name, (per_case, zero) in results.items():
            summarize(name, per_case, zero, style)

    print("\nper-query rank of first correct id (None = not in top 8; total=0 marked ∅):")
    legacy_rows = results["legacy"][0]
    current_rows = results["current"][0]
    for (style, q, r_old, t_old), (_, _, r_new, t_new) in zip(legacy_rows, current_rows):
        mark = lambda r, t: "∅" if t == 0 else (str(r) if r else "—")
        flag = "  <-- fixed" if (t_old == 0 or not r_old) and r_new else \
               ("  <-- REGRESSED" if r_old and not r_new else "")
        print(f"  [{style[:4]}] {q[:58]:58} legacy={mark(r_old, t_old):2} current={mark(r_new, t_new):2}{flag}")


if __name__ == "__main__":
    main()
