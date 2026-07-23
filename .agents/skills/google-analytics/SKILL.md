---
name: google-analytics
description: Traps to avoid when querying Google Analytics 4 through treg — cases where the GA4 Data API returns a confident wrong answer instead of an error. Use whenever answering questions about site traffic, visitors, pageviews, channels, conversions, or revenue from GA4.
---

# GA4 via treg — what will silently mislead you

You already know the GA4 Data API. This file is **only** the things that return a plausible wrong
answer with no error. Everything here was verified live on 2026-07-22, and each trap was
confirmed by watching three independent agents walk into it.

Property id: `treg connections ls` → the `google-analytics` row's `resource_ref`.

## 1. `endDate:"today"` silently includes a partial day

**3 of 3 agents did this.** It inflates every number and you will not notice.

```
28daysAgo → today      /top-page = 45,120   ← includes a partial day
28daysAgo → yesterday  /top-page = 44,300
```

Use `yesterday` unless the user explicitly asks about today. If you must use `today`, say the
last day is partial.

## 2. Zero does not mean zero — check whether it's even tracked

**3 of 3 agents reported "$0 revenue and 0 conversions" for a business with real revenue.** Two
rated it high confidence; one argued the zero was trustworthy because `eventCount` was large.

`keyEvents`, `conversions`, and `totalRevenue` all return `0` when **nothing is instrumented**,
which is indistinguishable from genuinely zero. Before reporting any zero:

```bash
# does this property have ANY key events configured?
--data '{"dateRanges":[...],"metrics":[{"name":"keyEvents"}]}'
```

Don't request `keyEvents` and `conversions` in the same call — they're aliases and GA4 rejects
it with `400 Found duplicate metrics: conversions`. Pick one.

If `keyEvents` is 0 across a long window on a site with traffic, the correct answer is
**"conversions are not instrumented in GA4; this question can't be answered from this source"** —
never "revenue is $0". Revenue almost certainly lives in Stripe/PostHog instead.

## 3. A zero-row response has NO `rows` key at all

Not `rows: []` — the key is absent, and so is `rowCount`. The shape varies by query:

| Query | Top-level keys returned |
|---|---|
| grouped by a dimension | `['dimensionHeaders','metricHeaders','metadata','kind']` |
| metrics only (e.g. `keyEvents`) | `['metadata','kind']` — headers gone too |

A naive `d['rows']` raises `KeyError`, which reads as a malformed request. All three unskilled
agents were confused by this. Use `d.get('rows', [])`, treat empty as "no data", then apply
trap #2.

## 3b. Don't list `dateRange` as a dimension

Comparing two periods is a standard ask, and the obvious move errors:

```
400: "Field dateRange is not a dimension. This field can be used in a Pivot or
      OrderBy like a dimension, but does not need to be listed in the Dimensions."
```

Pass 2+ entries in `dateRanges` and the API **auto-appends** the range as an extra dimension
value. Give the ranges `name`s to label them in the output.

## 4. Dimension sums do NOT equal totals — they overcount

Measured against a dimensionless true total (illustrative, ratios are the point):

| Grouped by | Sum | vs true total |
|---|---|---|
| `sessionDefaultChannelGroup` | | **~104%** |
| `country` | | **~104%** |
| `pagePath` | | **~189%** |

Sessions span multiple pages, so per-page sums double-count badly. **Never sum a dimension to
report a total** — run a separate dimensionless query.

## 5. `limit` defaults to 10,000 and truncates silently

An unlimited `pagePath` query returned exactly 10,000 rows with `"rowCount": 42613` — 32,613 rows
dropped, no warning. **`rowCount` is the truth; `len(rows)` is not.** Compare them before saying
"all" or "every".

## 6. The Admin API is a different host and is unreachable via `/call/`

The tool is bound to `analyticsdata.googleapis.com`. Anything on
`analyticsadmin.googleapis.com` — `accountSummaries`, property metadata — returns a **Google HTML
404**, not JSON. Two of three agents burned several attempts here, including trying to pass an
absolute URL (which gets concatenated into garbage).

To list properties, use treg, not the proxy:

```bash
treg connections resources <connection-id>   # hits the Admin API server-side
```

## Everything else

Standard GA4 Data API. `runReport`, `runRealtimeReport`, `batchRunReports`, `runPivotReport`,
`checkCompatibility`, `/metadata` all verified working through `/call/`. Two minor notes:
metric values are **strings** (cast before arithmetic), and `metadata.timeZone` governs what
"today" means (a property reports in its own timezone, e.g. `Australia/Sydney`).

⚠️ *Reported but not observed here:* high-cardinality dimensions can collapse into an `(other)` row.

- API schema: https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
- runReport: https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport
- Quotas: https://developers.google.com/analytics/devguides/reporting/data/v1/quotas
