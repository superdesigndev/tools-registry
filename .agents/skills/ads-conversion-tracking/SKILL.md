---
name: ads-conversion-tracking
description: Use when setting up, changing or debugging treg's own conversion tracking — the ad-click capture, the AdConversion outbox, or the Data Manager uploader — and whenever asked whether conversions are "working", "live" or "verified". Also use before claiming any part of the pipeline is proven.
---

# treg's ad conversion tracking — what breaks, and what "verified" actually means

treg records three conversions against the ad click that produced them: **signup**, **first
successful call**, **first top-up**. A click id lands in a first-party cookie, is persisted on the
`Org`, three server chokepoints write rows into the `AdConversion` outbox, and a background worker
uploads them to Google's Data Manager API.

Every failure this system has had was invisible to the obvious check. Green tests, HTTP 200s and
dry runs were all reported while nothing worked. **The rule below is the whole skill.**

## The rule

> A layer is verified when you have observed **real data at that layer in production**.
> Not a mock, not a dry run, not a status code, not an agent's report.

## The verification ladder

Climb it in order. Each rung has failed in production at least once.

| # | Layer | Verified by | What lies to you |
|---|---|---|---|
| 1 | Script is served | `curl -s https://treg.to/adtrack.js \| wc -c` | HTTP 200 with a **0-byte body** — the route serves an empty script when `adsconv.enabled()` is false |
| 2 | Every ad destination loads it | grep each page for `adtrack.js` | `/` serves `landing.html`, NOT `index.html` |
| 3 | Click reaches the DB | `select ad_gclid, ad_click_id_type, ad_landing from org where id=…` | cookie format changes silently (`kind\|id\|landing`) |
| 4 | Chokepoint fires | a row in `adconversion` for that org | unit tests pass with a fake client |
| 5 | Upload accepted | `attempts=1, uploaded_at` set, `error` empty | **`validateOnly` returns 200 for payloads a real ingest rejects** |
| 6 | Google processed it | `requestStatus:retrieve` with the request's id | `uploaded_at` is set on the immediate 200 only |
| 7 | Attributed to a real click | a conversion in the Ads UI | nothing synthetic can prove this — a fabricated `gclid` is accepted and discarded |

Rungs 1–6 are provable for free. **Rung 7 needs one real ad click** and nothing substitutes for it.

## Checks that cost nothing and catch the most

```bash
# 1. is capture actually on?  0 = the feature gate is off, not a deploy failure
curl -s https://treg.to/adtrack.js | wc -c

# 2. does every destination load it?
for p in / /resources /use-cases/seo-data-for-ai-agents; do
  printf '%-42s %s\n' "$p" "$(curl -s "https://treg.to$p" | grep -c adtrack.js)"; done

# 3-5. the whole chain, in production, without spending anything
curl -s -X POST https://treg.to/users -H 'Content-Type: application/json' \
  -H 'Cookie: treg_ad=gclid%7CPROBE1%7Cp2' -d '{"email":"you+probe@example.com"}'
# then: select ad_gclid, ad_landing from org where id=<new>;
#       select action, attempts, uploaded_at, error from adconversion where org_id=<new>;
```

A `/call/` as that team then fires `first_call`. A real top-up fires `paid` — there is no free
substitute for that one.

## Traps, each of which shipped

**`validateOnly` does not apply every rule.** A dry run returned 200 for a payload a real ingest
rejects with `400 INVALID_ARGUMENT`. Never sign off on a dry run. Send one real event.

**A numeric `transactionId` is rejected.** `"2"` fails, `"treg-2"` succeeds, every other field
identical. Prefix it. This alone would have made every upload fail.

**Naive UTC everywhere.** Columns are `TIMESTAMP WITHOUT TIME ZONE`; asyncpg rejects tz-aware values,
SQLite accepts them. Use `models._now` / `api._utcnow_naive`. And `.astimezone()` on a naive value
reads it as **local** time — on a Sydney server that shifts every conversion 10-11 hours, producing
wrong data rather than an error.

**The scope belongs to treg, not to customers.** The uploader authenticates with a platform refresh
token in settings (`ads_conv_refresh_token`). Do NOT add `auth/datamanager` to `GOOGLE_ADS` in
`oauth_providers.py` — `listing()` shows every provider, so it would appear on every customer's
consent screen. The existing `adwords` scope already covers audience/customer-match writes (verified
by a `validateOnly` `userLists:mutate`).

**Never route conversions through `audit.py` or `analytics.py`.** Both shed rows under load by
design — they would undercount precisely when traffic peaks.

**Nothing may touch the request's transaction.** An earlier version committed on the request session
mid-settlement and broke 8 billing tests while slowing `/call/` 6.6x. `_record_first_call` runs on
its own session for this reason.

**A first-call is HTTP-status-based.** Any 2xx/3xx relay counts, including an upstream error body.
It also misses `treg cli run` / `treg with`, which bypass `/call/` entirely.

## Changing the uploader

1. Read `docs/context/architecture/ads-conversions.md` first.
2. Change `src/treg/adsconv.py` only — the outbox, chokepoints, dedupe and money rules stay put.
3. Money stays integer micro-USD. Exactly one float, at the JSON boundary, because
   `conversionValue` is a wire double. `aud_micro = usd_micro * 10 // 7`.
4. Add a test that fails when the feature is removed — comment the code out and watch it go red.
   A test that cannot fail is worse than no test.
5. Then climb the ladder. Do not report success from a rung you did not observe.

## Red flags — you are about to report something false

- "The dry run passed" · "validateOnly returned 200"
- "HTTP 200, so it worked" — check the **body**
- "The tests pass" — they use a fake client
- "It failed once then succeeded, probably transient" — on a first real call that IS the bug
- "The agent reported DONE" — read the diff and the database
- "It's deployed" — check the deploy actually finished

**Each of these was said in the session that produced this skill, and each was wrong.**
