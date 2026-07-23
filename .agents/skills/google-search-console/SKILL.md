---
name: google-search-console
description: Traps to avoid when querying Google Search Console through treg — cases where the API returns a confident wrong answer instead of an error, especially around totals and recent-date trends. Use whenever answering questions about organic search clicks, impressions, rankings, or index status.
---

# Search Console via treg — what will silently mislead you

You already know the Search Console API. This file is **only** the things that return a plausible
wrong answer with no error. Verified live on 2026-07-22 against `sc-domain:example.com`, with
each trap confirmed by watching three independent agents.

Site id: `treg connections ls` → `resource_ref`. If it's empty, **no site is pinned** — list them
with `treg connections resources <id>` and say which one you chose and why. Don't present an
inferred site as configured, and don't report the blank as a fault — it isn't one.

When nothing is pinned and the question doesn't name a site, **pick the site whose domain matches
the question and state that you inferred it.** Note that subdomains are separate properties, so
`sc-domain:example.com` and `sc-domain:app.example.com` are different answers — say which you used.

## 1. Recent days are incomplete — do not report them as a trend

**3 of 3 agents concluded "organic traffic is down ~47%".** It wasn't.

GSC lags ~2–3 days and simply omits missing days — a 14-day request returns 11–12 rows with no
warning. Worse, the newest present days read low. The tail looked like this:

```
07-15: 640   07-16: 610   07-17: 500   07-18: 520   07-19: 430   ← last 3 days incomplete
```

Cross-checked against GA4 organic sessions for the same dates, the decline **does not exist** —
and GA4 shows 07-20 and 07-21 recovering to 746 and 849, days GSC hasn't reported at all.

**Two rules:**
- End ranges ≥3 days before today, and never start a window on a local peak.
- Before calling any GSC trend real, **cross-check GA4 organic sessions for the same dates.**
  Two independent sources disagreeing means the GSC tail is an artifact.

## 2. Dimension sums do NOT equal site totals

Illustrative (the point is that dimension sums miss the true total, not the absolutes):

| Grouped by | Rows | Impressions | vs true total |
|---|---|---|---|
| `query` | 4,219 | | **~31%** — loses ~69% |
| `page` | 1,161 | | **~151%** — overcounts ~51% |
| `date` | 28 | (true total) | 100% ✅ |
| *(none)* | 1 | (true total) | 100% ✅ |

`query` undercounts because Google drops anonymised rare queries; `page` overcounts because one
SERP impression showing several of your pages counts once per page. *(Numbers measured;
mechanisms are the standard explanation, not measured.)*

**Never sum a dimension to report a total** — especially for "how much traffic do my top queries
drive?", where summing understates organic traffic by ~69% with total confidence.

## 3. `searchAppearance` cannot be combined with any other dimension

Fails loudly with a 400: `"Cannot group by search appearance dimension together with another
dimension."` Query it alone — and note that a sparse result there is a correct answer, not a bug.

## 4. Two API surfaces on one host

- `webmasters/v3/...` — search analytics, sitemaps
- `v1/urlInspection/index:inspect` — URL Inspection

Guessing `webmasters/v3/urlInspection/...` returns a **Google HTML 404**, not a JSON error. Two of
three agents hit this. The HTML-vs-JSON tell is worth knowing generally: HTML means the path never
routed.

## Everything else

Standard API. `ctr` is a fraction (0.0882 = 8.82%); `position` is an average, so don't average it
across rows. `rowLimit` accepts 25000 (default reported as 1000 — unverified). Sitemap
`contents[].indexed` returned `"0"` for a fully-indexed site with 1,079 URLs — use URL Inspection
for real index status. Subdomains are separate properties.

Both `sc-domain:example.com` and `sc-domain%3Aexample.com` work in the path — verified via the
audit log. Encoding is optional; don't debug a 403 by adding it.

⚠️ *Unverified:* URL-prefix properties (`https://example.com/`, trailing slash) — none available
to test. A 403 is reported to usually mean the wrong property form rather than missing permission.

**Writes** (`PUT`/`DELETE` on sitemaps) change what Google crawls. Untested by design — get an
explicit target and human go-ahead first.

- Search Analytics query: https://developers.google.com/webmaster-tools/v1/searchanalytics/query
- URL Inspection: https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect
- Freshness & anonymisation: https://support.google.com/webmasters/answer/7576553
