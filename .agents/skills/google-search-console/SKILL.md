---
name: google-search-console
description: Query Google Search Console through treg — search performance by query/page/country/device, indexing status, and sitemaps. Credentials are injected server-side; you never hold a token. Use when asked about organic search traffic, impressions, clicks, CTR, average position, which queries or pages drive traffic, or whether a URL is indexed.
---

# Google Search Console via treg

You call Search Console through treg's proxy. **You never see or handle a credential** — treg
injects the OAuth token server-side on every request.

## Setup (once per team)

```bash
treg oauth providers                                  # confirm it's available
treg oauth connect --provider google-search-console   # opens browser consent
treg connections ls                                   # check health + expiry
```

Connecting auto-creates a `google-search-console` tool, so a call works immediately after consent.

**Pick which site to query** (a Google account often has many):

```bash
treg connections resources <id>                       # list verified sites
treg connections use <id> sc-domain:example.com       # remember the choice
```

## Site URL format — the most common mistake

Search Console identifies a property in one of two forms, and they are **not** interchangeable:

| Property type | `siteUrl` value |
|---|---|
| Domain property | `sc-domain:example.com` |
| URL-prefix property | `https://example.com/` (**trailing slash required**) |

Always URL-encode it in the path: `sc-domain%3Aexample.com`, `https%3A%2F%2Fexample.com%2F`.
If you get a 403, the property form is wrong more often than the permission is.

## Calls

**List the sites this connection can read**

```bash
treg call google-search-console webmasters/v3/sites
```

**Search performance** — the main one. POST with a JSON body:

```bash
treg call google-search-console \
  "webmasters/v3/sites/sc-domain%3Aexample.com/searchAnalytics/query" \
  --method POST --data '{
    "startDate": "2026-06-01",
    "endDate": "2026-06-30",
    "dimensions": ["query"],
    "rowLimit": 25
  }'
```

Returns `rows[]`, each with `keys` (one per dimension, in order) plus `clicks`, `impressions`,
`ctr`, `position`.

**Useful dimension combinations**

| Question | `dimensions` |
|---|---|
| Which queries drive traffic? | `["query"]` |
| Which pages? | `["page"]` |
| Which queries for a given page? | `["page","query"]` |
| Trend over time | `["date"]` |
| Where geographically? | `["country"]` |
| Mobile vs desktop | `["device"]` |

**Filter to a subset**

```json
{"dimensionFilterGroups": [{"filters": [
  {"dimension": "page", "operator": "contains", "expression": "/blog/"}
]}]}
```

**Is a URL indexed?**

```bash
treg call google-search-console urlInspection/index:inspect --method POST \
  --data '{"inspectionUrl":"https://example.com/page","siteUrl":"sc-domain:example.com"}'
```

**Sitemaps**

```bash
treg call google-search-console "webmasters/v3/sites/sc-domain%3Aexample.com/sitemaps"
```

## Gotchas that will bite you

- **Query params need `--query`, not `?` in the path.** `treg call ... "path?x=1"` silently drops
  the query string and you get a plausible-looking response with the wrong data. Use
  `--query x=1`.
- **Data lags ~2–3 days.** An `endDate` of today returns little or nothing. Default to ending
  3 days ago unless asked otherwise.
- **`rowLimit` maxes at 25,000**; page with `startRow` beyond that.
- **`position` is an average and lower is better** — don't describe a drop from 3 to 8 as an
  improvement.
- **`ctr` is a fraction** (0.042), not a percentage. Multiply by 100 before showing it.
- Date range is **inclusive** of both ends, and dates are `YYYY-MM-DD` in the property's timezone.

## Reading the numbers honestly

- Impressions up with clicks flat is a **ranking or snippet** problem, not a traffic win.
- Compare like-for-like periods (same weekday count) — week-over-week beats month-to-date.
- A single query's `position` averaged across countries/devices can hide a large regional split;
  add `["query","country"]` before concluding anything about a ranking change.
