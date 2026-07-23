---
name: google-ads
description: Traps when running Google Ads through treg — the API requirements and cleanup semantics that cost round-trips or money. Use whenever asked to analyse ad performance, audit spend, create or change campaigns, adjust budgets or bids, or do media buying.
---

# Google Ads via treg — the traps

Verified live on 2026-07-22 against a live account, via `treg call google-ads`.
Each trap is tagged with how many independent agents hit it when working **without** this file, so
you know which are real and which are here for insurance.

Account id: `treg connections ls` → the `google-ads` row; `treg connections resources <id>` to list.
Never guess an account id — you may be spending someone else's money.

## 1. Pin the API version — there is no discovery  ⚠️ 2/2 agents hit this

Paths are versioned and a wrong guess returns a **Google HTML 404**, not JSON. Nothing in the API
or in `treg tool ls` reports the current version — one agent burned **five** calls walking v14→v18
before recovering the answer from the `treg calls` audit log.

**Use `v21`** (verified 2026-07-22). If it 404s, check the release notes; do not guess downward.

## 2. `contains_eu_political_advertising` is required on campaign create  ⚠️ 3/3 hit this

Absent from essentially every code sample. Campaign create fails `REQUIRED` without it:

```json
"containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
```

## 3. Bidding strategy must be the sub-message, not the enum  ⚠️ 1/2 hit this

`"biddingStrategyType":"TARGET_SPEND"` alone fails `REQUIRED` on `campaign_bidding_strategy`. It's
a protobuf oneof — send the field itself: `"targetSpend":{}`, `"manualCpc":{}`,
`"maximizeConversions":{}`. The enum is output-only.

## 4. Removing a parent orphans children — and their status lies  ⚠️ 2/2 confused by this

Remove a campaign and its ad groups/criteria/ads become immutable but **keep reporting their old
status** — a removed campaign's ad group still reads `ENABLED`. Mutating one returns
`OPERATION_NOT_PERMITTED_FOR_REMOVED_RESOURCE`.

**Verify teardown on `campaign.status`, never on a child's.** Checking the child looks like cleanup
silently failed. Remove campaign first, then its budget — the budget is not removed for you, and an
orphaned budget is easy to leave behind.

Nothing is ever hard-deleted: `REMOVED` is terminal. Filter with `WHERE campaign.status != 'REMOVED'`
or your campaign list fills with corpses.

## 5. Money is in micros  ✅ 0/2 got this wrong — kept for asymmetry

`amountMicros: 3000000` = **$3.00/day**. Both agents handled this correctly, so it is not a common
failure — but a 10⁶ slip creates a $3,000/day budget that the API accepts **without any error**, and
it is the only mistake here that spends money silently. Compute `dollars * 1_000_000` explicitly and
re-read it before sending.

Currency is **not** always USD — the account may bill in a non-USD currency (e.g. AUD):

```
SELECT customer.currency_code, customer.time_zone FROM customer
```

## 6. `updateMask` controls what changes

```json
{"operations":[{"updateMask":"amount_micros",
  "update":{"resourceName":"customers/<CID>/campaignBudgets/<BID>","amountMicros":3000000}}]}
```

Only listed fields change; a field you set but don't list is ignored, one you list but don't set is
**cleared**. Both `amount_micros` and `amountMicros` are accepted (verified).

## 7. `login-customer-id` failures look like auth failures

Acting on a client account under a manager needs the header:

```bash
treg call google-ads "<path>" --method POST --header 'login-customer-id: 9876543210' --data '...'
```

A wrong or unauthorised value returns **`401 UNAUTHENTICATED`**, not a targeting error. Check the
header before debugging OAuth.

## `validateOnly` — free insurance on any mutation

```json
{"validateOnly":true,"operations":[...]}   // returns {} on success, changes nothing
```

Verified: a `validateOnly` budget change returned `{}` and left the value untouched. Neither
unskilled agent used it and neither needed it — their mistakes were rejected by the API anyway. Its
real value is the case they never hit: a mutation that is **valid but wrong** (a mistyped budget),
which no error will catch. Use it whenever the operation spends money or you can't cheaply undo it.

## Reading performance

```bash
treg call google-ads "v21/customers/<CID>/googleAds:search" --method POST --data '{
  "query":"SELECT campaign.name, campaign.status, campaign_budget.amount_micros,
           metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
           FROM campaign WHERE segments.date DURING LAST_30_DAYS
           ORDER BY metrics.cost_micros DESC"}'
```

Metrics need a date condition (`DURING LAST_30_DAYS`, or `BETWEEN '2026-06-01' AND '2026-06-30'`) —
without one you get lifetime totals. Use `googleAds:searchStream` for large unpaginated pulls.
Keyword-level detail lives in `keyword_view`.

**On zero conversions** (✅ 2/2 agents already handle this): an audited account showed ~$300 spent, ~170 clicks, 0 conversions because nothing is instrumented. Both unskilled agents correctly said they
couldn't distinguish "no tracking" from "no results" — keep doing that, and check before treating
zero as a performance verdict.

## Creating a campaign — minimum that works

Budget first, then campaign. Verified end-to-end.

```json
// campaignBudgets:mutate
{"operations":[{"create":{"name":"...","amountMicros":3000000,
  "deliveryMethod":"STANDARD","explicitlyShared":false}}]}

// campaigns:mutate
{"operations":[{"create":{
  "name":"...", "status":"PAUSED",
  "advertisingChannelType":"SEARCH",
  "campaignBudget":"customers/<CID>/campaignBudgets/<BID>",
  "manualCpc":{},
  "containsEuPoliticalAdvertising":"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
  "networkSettings":{"targetGoogleSearch":true,"targetSearchNetwork":false,"targetContentNetwork":false}
}}]}
```

Then `adGroups:mutate` → `adGroupCriteria:mutate` (keywords) → `adGroupAds:mutate` (responsive
search ad). A campaign with no ad group, keywords or ads **cannot serve**, whatever its status.

✅ *Both unskilled agents defaulted to `PAUSED` on a real-money account without being told, and
explained why.* Keep that default; enabling is a one-field update, an unwanted live campaign is a
refund request.

## Test accounts solve less than you'd hope

Created under a **test manager** — a separate Google account; a production manager cannot host them.
They have **no serving data** and **cannot test conversion uploads**, so they only prove mutation
mechanics. Performance and conversion work must happen on a live account.

- Field reference: https://developers.google.com/google-ads/api/fields/v21/overview
- GAQL grammar: https://developers.google.com/google-ads/api/docs/query/grammar
- Test accounts: https://developers.google.com/google-ads/api/docs/best-practices/test-accounts
