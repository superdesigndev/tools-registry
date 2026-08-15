# Shared-plan pricing — the ladder that lets flat-fee providers into the catalog

**Status:** planned, awaiting approval. Nothing built.

## The ladder (agreed 2026-08-13)

| The provider sells | How we price a call |
|---|---|
| Real credits (Hunter: $49 / 2,000) | vendor price ÷ credits. Exists today, unchanged |
| A monthly request cap ($10 / 10,000) | fee ÷ cap. Same arithmetic, also effectively exists |
| A rate limit only ($49.99 at 75 req/min) | fee ÷ theoretical max is the FLOOR; the real price sits above it at a stated break-even |
| Unlimited | no anchor, so a treg-set price with the break-even printed next to it |
| All flat-fee rows | reviewed monthly against our own call counts |

The honesty rule that makes the last two rungs defensible: we never claim these are vendor prices.
The vendor has no per-call price. What treg sells on those rungs is its own service — holding the
subscription, custody of the key, sharing the rate limit — at a **published** rate whose fee and
break-even volume are printed beside it. And the price is not only cost recovery: on a shared key it
is congestion control. At $0, one looping agent exhausts the rate limit for every team at once.

## The build is smaller than the discussion was

The first design had a new cost type, new `cost_view` logic, and a new fx.yaml section feeding it.
Reading the code first killed most of that: **a flat-fee provider can be modelled as a credit
provider whose credit is "one call on treg's shared plan."**

- fx.yaml already re-prices a whole provider from one rate line
- endpoints already carry `{type, value, currency: credit}` costs
- `cost_view` already multiplies the two — it needs **zero changes**
- billing, holds, caps, `platform_eligible` all just work, because the endpoint has a USD price

So Alpha Vantage becomes: every endpoint costs 1 credit, and fx.yaml says the credit is worth
$0.001, with a basis line reading like the PDL one does today:

    alphavantage: {usd: 0.001, kind: treg_shared_plan,
      basis: "treg shared-plan rate. Vendor sells flat $49.99/mo at 75 req/min (= 3.24M calls/mo
              capacity; floor $0.0000154/call). Rate set for break-even at 50k calls/mo (1.5%
              utilization); reviewed monthly against audited volume",
      source: "https://www.alphavantage.co/premium/", checked: "2026-08-13"}

What actually changes:

1. **A `kind: treg_shared_plan` marker** on such fx.yaml entries, and the block documented. It is
   what separates "the vendor's rate card" from "treg's rate card" everywhere downstream.
2. **The validator** learns the marker and requires a fee + break-even in the basis of any entry
   carrying it. A treg-set price with no printed break-even is exactly the dishonesty this design
   exists to avoid.
3. **`price_drift` must skip these providers.** The drift detector compares our estimate against the
   provider's observed charge; a flat-fee provider reports no charge, and treating "no observed
   cost" as drift would page us weekly about a number that cannot drift.
4. **429 is never billable.** Today `_platform_billable` bills any 4xx under `per_call`. On a shared
   key a 429 is pool congestion — our capacity problem, not the caller's mistake — and billing it
   would charge teams for our own saturation. This fix is global and correct for credit providers
   too: no vendor charges for a request it rate-limited away.
5. **A recovery report in `reconcile`** beside `provider_spend`, which already aggregates the audit
   rows: per shared-plan provider, fee vs (calls × rate) for the month. Output is a report for a
   human; the price change is a hand edit to fx.yaml, so prices stay stable and deliberate, never
   auto-adjusted under an agent's feet.

## The monthly review rule

The recovery report says what happened; this is what the reviewer does with it. The rule is the same
arithmetic that set the price, re-run with a measurement in place of the guess:

    new price = fee ÷ measured monthly calls        (round to a clean number)

Worked both ways:

- **Heavy month.** 500,000 calls at $0.001 collected $500 against a $49.99 fee. New price
  $49.99 ÷ 500,000 ≈ $0.0001. Heavy usage makes the endpoint cheaper for everyone, which is the
  right direction: a fixed fee spread over more calls.
- **Quiet month.** 5,000 calls collected $5 against $49.99. Either the price rises toward $0.01, or
  the honest conclusion is that the provider does not justify a subscription, and it demotes to
  own-key-only.

Three outcomes, one decision each:

| Report shows | The move |
|---|---|
| Over-recovered (collected well above the fee) | lower the price toward fee ÷ measured volume |
| Under-recovered | raise it, or demote the provider to own-key-only |
| Healthy volume but pressing the rate limit | keep the price; buy the vendor's bigger tier — the limit paying for its own upgrade is the model working |

Two stabilisers, both deliberate:

1. **A human edits the number, once a month, from the report.** Never auto-adjusted: agents must see
   a stable price inside a month, and a rate card that moves under a caller's feet is not a rate
   card.
2. **Volume smoothing over the trailing month, not the loudest week.** One viral day must not
   reprice the provider; the window is the same 30-day one `provider_spend` already uses.

## What this does and does not do for the 55 unpriced

It unlocks the flat-fee-shaped ones — apollo (4, seat-priced) and crunchbase (4, sales-quoted, if we
hold a plan) — and every NEW flat-fee provider, which is the market-data category's whole blocker.

It does **not** price the rest: semrush (11), lusha (10), justoneapi (7), pdl (7), diffbot (6) are
credit providers whose per-endpoint credit counts are simply unrecorded. That is docs-reading work,
one endpoint at a time, and no pricing model removes it.

## Order, with a stop after each

1. fx.yaml `treg_shared_plan` convention + validator rules + ONE pilot entry (apollo or a market-data
   provider), no endpoints repriced yet. **Stop for review.**
2. The 429 billability fix, with tests that bill a 402→ no, 429 → no, 400 per_call → yes. **Stop.**
3. The recovery report in reconcile, with a test on synthetic audit rows. **Stop.**
4. Doc sync: money.md (the ladder + the honesty rule), llms.txt if pricing language there needs it,
   and the fx.yaml header comment.
5. Then the market-data category starts on top of this, per `MARKET-DATA-CATEGORY-RESEARCH.md`.

## What could go wrong, and where I would look first

- **The marker not propagating.** If nothing downstream can tell a treg rate from a vendor rate, some
  future surface will present our price as the vendor's. The validator rule and the basis wording are
  the guard; a test should assert every `treg_shared_plan` entry names a fee and a break-even.
- **Double-charging a team on its own key.** Tier 2 is already unmetered, so a team bringing its own
  Alpha Vantage key must never see our shared-plan rate. Existing behaviour, but it gets a test,
  because this is the first time a provider has a treg price AND an obvious own-key path.
- **The recovery loop becoming automatic pricing.** It stays a report. An auto-adjusting price would
  surprise agents mid-session and turn a published rate card into a moving target.
