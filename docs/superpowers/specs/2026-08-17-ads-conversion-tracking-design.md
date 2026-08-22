# Google Ads conversion tracking for treg — design

**Date:** 2026-08-17
**Branch:** `feat/ads-conversion-tracking`
**Ads account:** `5149790776` ("Treg"), AUD, `Australia/Sydney`, client of the Shirley Lou MCC (`3519125194`)

## The problem

treg has no analytics instrumentation of any kind — no `gtag`, GTM, GA, PostHog or Plausible, in the
repo or on the live site. The Ads account's only conversion action (`Purchase`, `7722657530`) is a
`WEBPAGE` action created by the signup wizard; with no tag on the site it has never fired and never
could.

`marketing/landing/_measurement.md` states the consequence: *"A vertical whose visitors sign up and
never call looks identical to a vertical whose visitors sign up and call daily."* Spending against
this returns a verdict on the wrong metric.

## Goal

Record three conversions against the ad click that produced them: **signup**, **first successful
call**, and **first top-up**.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Conversion set | signup + first call + first top-up | `_measurement.md` names first call as the metric that decides whether a vertical is real |
| Mechanism | server-side upload (`uploadClickConversions`) | a first call happens in a CLI/MCP client with no browser; a tag physically cannot see it |
| Top-up counting | once per org, not per payment | optimises for acquiring payers; value-based bidding needs volume treg does not have yet |
| Currency | fixed rate, 1 AUD = 0.70 USD | stable reported value; `aud_micro = usd_micro * 10 // 7`, integer only |
| Ad landing | five usecase pages for those verticals, homepage otherwise | landing-page relevance feeds Quality Score, which sets CPC |

### Decisions recorded against a documented position

- **`_measurement.md` treats signup as a weak metric** ("signups measure curiosity"). Signup is
  therefore created as a **secondary** action — tracked, but not something Google bids toward.
- **`wiki/topics/treg.to SEO.md` §9 (2026-08-17) states "No ads for treg"**, on the grounds that the
  preconditions — a working funnel, ~50 tracked conversions, call instrumentation — are unmet. This
  work *builds* the missing precondition; it does not authorise spend. The wiki's inherited
  thresholds (~50 conversions to learn, 2–3 weeks to read, 3 months to judge) still apply before any
  campaign verdict is trusted.
- **Competitor-alternative keywords**: §3 finds that family declining across the board
  (`semrush alternative` −73%, `ahrefs alternative` −52%, `clearbit alternative` −48%,
  `bright data alternative` −64%) and "the highest-competition, lowest-CTR shape". Deliberately left
  open — nothing in this design depends on keyword choice.

## Architecture

```
ad click ──► landing page ──► first-party cookie ──► signup ──► Org row
                                                                  │
        signup ─┐                                                 │ ad_gclid
     first call ─┼──► AdConversion outbox (uploaded_at NULL) ◄─────┘
       first top-up ─┘            │
                                  ▼
                    background drain ──► Ads API uploadClickConversions
```

### 1. Capture (client)

Ten lines of first-party JS on the homepage and, when they ship, the five usecase pages. Reads
`gclid`, `gbraid`, `wbraid` (Google substitutes the latter two on iOS traffic; omitting them silently
drops mobile conversions) and `utm_content` (carries the page id `p1`…`p5` per `_measurement.md`).
Writes a `SameSite=Lax` cookie with a 90-day life, matching Google's click-conversion window. No
Google script, no third-party request.

### 2. Store — new columns on `Org`

| column | purpose |
|---|---|
| `ad_gclid` | the click id, kept for the life of the team |
| `ad_click_at` | uploads must postdate the click and fall inside 90 days |
| `ad_landing` | which page, for the per-page hypotheses |
| `first_call_at` | the decisive metric; also the guard that makes first-call fire once |

New table `AdConversion(org_id, action, dedupe_key, uploaded_at, error, attempts)`, unique on
`(org_id, action)`. Migrations follow the existing `create_all` + guarded `ALTER` pattern in `db.py`;
the project does not use Alembic.

### 3. Fire — three existing chokepoints

- **Signup** → `_grant_signup_promo()` (`api.py:3748`), called from exactly two doors,
  `register_user()` (3814) and `create_org()` (3877), already idempotent per `(org, kind)`.
- **First call** → guarded `UPDATE ... WHERE first_call_at IS NULL` in the `/call/{rest:path}`
  handler (`api.py:9098`) on a successful upstream response. **Not** `ledger.reserve` — a team using
  its own key is never metered and never touches the ledger, so a ledger hook would miss exactly
  those teams.
- **First top-up** → `_credit()` in `billing.py`, on the existing branch that distinguishes "this
  delivery moved money" from a webhook redelivery.

Signup and first-call write their outbox row **in the same transaction as the event**, so the event
and its pending conversion commit or fail together.

**The paid path is the exception, and it is not atomic.** `ledger.topup()` commits internally
(`ledger.py:36`) before `_credit()` reaches the conversion code, so the credit and the conversion are
two separate commits. If the process dies between them the conversion is lost permanently — a Stripe
redelivery finds the payment already credited, `fresh` is False, and the fire site never runs again.
No error is raised and nothing detects it.

This is accepted, deliberately (2026-08-17). The window is sub-millisecond, the blast radius is one
missing conversion rather than lost money or a failed webhook, and closing it properly would mean
restructuring `ledger.py` — the only code path that moves money — to serve a marketing feature. A
reconciliation sweep (find orgs with a credited payment and a `gclid` but no `paid` row, backfill it)
is the cheap fix if this ever proves to matter in practice.

### 4. Upload — a durable outbox, never fire-and-forget

A background worker drains rows with `uploaded_at IS NULL` and POSTs to
`v22/customers/5149790776/conversionUploads:uploadClickConversions` with `partialFailure: true`,
recording each row's result individually. The request path never waits on Google.

This also solves a timing problem that otherwise reads as random data loss: **a `gclid` is not valid
for upload immediately.** Google needs several hours after the click before it will accept a
conversion against it, so anything uploaded at signup time is rejected. Retry-with-backoff turns
"rejected, gone" into "sent a few hours later". Rows failing permanently (expired click, malformed
id) are marked with the error code rather than retried forever.

**Nothing routes through `audit.py`.** It sheds rows past `_MAX_PENDING = 5000` — correct for
analytics, and it would undercount conversions exactly when traffic peaks (`CLAUDE.md`, money-code
rule).

## Done already (2026-08-17)

Created on the live account via the Ads API, `validateOnly` first:

| ID | Name | Type | Category | Primary |
|---|---|---|---|---|
| `7723667014` | treg Signup | `UPLOAD_CLICKS` | `SIGNUP` | no |
| `7723667017` | treg First successful call | `UPLOAD_CLICKS` | `QUALIFIED_LEAD` | **yes** |
| `7723667020` | treg First top-up | `UPLOAD_CLICKS` | `PURCHASE` | **yes** |

`Purchase` (`7722657530`) demoted from primary — a `WEBPAGE` action cannot receive uploads, and
leaving it primary meant Google bidding toward a goal that would always read zero.

**API version is `v22`.** `v21` — the version pinned in `.agents/skills/google-ads/SKILL.md` — now
returns `UNSUPPORTED_VERSION`. That file needs updating.

## Deferred

The five usecase landing pages (`usecase-*.html`, `usecase.css`, `resources.html`) are untracked and
therefore undeployed, so their routes (`api.py:2315`) 404. The capture snippet is written as one
shared include so adding them later is a one-line change per page.

## Verification

No test `gclid` exists, so the chain cannot be proven without a real click:

1. One Search campaign, minimum budget, tight geo.
2. A genuine click on your own ad.
3. Confirm `gclid` → cookie → `Org.ad_gclid`.
4. Walk the funnel: sign up, install agent, one call, minimum top-up.
5. Wait — uploads cannot succeed for several hours after the click, and conversions take up to 24h
   to surface in the UI. Do not conclude failure before then.
6. Assert three conversions, and that a US$20 top-up reads **A$28.57**. A$20.00 or A$14.00 means the
   currency math is wrong.

## Risks

- **Fixed FX drifts.** Named constant with the date it was set, not a magic number.
- **Privacy policy.** `privacy.html` needs a line on advertising measurement; a first-party cookie is
  still advertising data.
- **PMax campaign `24150011650`** sits paused at A$44.81/day on a signup-wizard default. It should be
  restructured or deleted, not unpaused as-is.
