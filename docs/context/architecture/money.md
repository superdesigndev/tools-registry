---
title: Money — prepaid balance, the ledger, Stripe, and the reports that check it
status: shipped
sources:
  - src/treg/ledger.py
  - src/treg/billing.py
  - src/treg/reconcile.py
  - src/treg/api.py
related:
  - architecture/catalog.md
  - architecture/proxy-model.md
  - architecture/data-model.md
---

# Money

A catalogued endpoint can be served on **treg's own key** — no provider signup for the caller — which
means treg pays the provider and bills the team. That needs a balance, a way to top it up, and a way
to prove afterwards that the numbers were real. Three modules, one job each:

| Module | Job | May it write money? |
|---|---|---|
| `ledger.py` | the only code path that moves money | **yes — exclusively** |
| `billing.py` | the only code path that talks to Stripe | no (it calls `ledger.topup`) |
| `reconcile.py` | read-only reports that check the ledger against the world | no |

The seam between the first two is one function: `ledger.topup(org, amount_micro, payment_ref)`.
Stripe authorizes a payment on one side, the ledger credits balance on the other, and neither reaches
into the other's job.

## Units: integer micro-USD, everywhere

1 micro = 1e-6 USD. A catalog call costs ~600 micro ($0.0006), so **cents cannot represent one call**
and floats cannot be summed for a year without drifting. The only float is the margin *rate*, turned
into an integer immediately (`with_margin`). Stripe speaks integer **cents**, so 1 cent = 10,000
micro and every crossing goes through `micro_to_cents` / `cents_to_micro` in `billing.py` — the one
file where two unit systems meet. Whole dollars appear only in settings and in what a human types.
Every `*_micro` value has a display-only `*_usd` twin: **never compute against the USD field.**

## The four tables and the invariant

`Org.balance_micro` (materialized) · `CreditBlock` (one funding event, and what is left of it) ·
`Hold` (an open reservation) · `LedgerEntry` (append-only journal).

    balance_micro == sum(block.remaining_micro) - sum(open hold.amount_micro)

The balance is a column rather than a query because `reserve` has to be one conditional UPDATE (see
below). Every operation writes its `LedgerEntry` **in the same transaction, synchronously,
in-request**. Never route a ledger write through `audit.py`: it drops rows past its queue bound and
swallows exceptions, which is right for analytics and fatal for money.

## The five operations (`ledger.py`)

| Op | Effect |
|---|---|
| `grant` | new promotional block, balance up (org creation) |
| `topup` | new purchased block, balance up (after Stripe authorized) |
| `reserve` | balance down by the estimate, `Hold` opened — the hot-path spend gate |
| `settle` | blocks down by the observed cost, hold closed, difference refunded |
| `release` | hold closed, balance refunded in full (upstream failure — not billable) |

**The gate is one statement**, which is the heart of the design:

```sql
UPDATE org SET balance_micro = balance_micro - :est WHERE id = :org AND balance_micro >= :est
```

The WHERE is the check and the SET is the debit, so the *database* decides who gets the last cent.
`rowcount 0` means insufficient funds → `InsufficientBalance` → a 402 the agent can act on. No
SELECT-then-UPDATE, no application lock, same behaviour on SQLite and Postgres: N concurrent callers
against a balance that affords K get exactly K successes.

**Block consumption order** is promotional-first, then oldest-purchased-first. Promo credit is a
marketing expense and never refundable; purchased credit is a deferred-revenue liability and *is*
refundable and disputable — so spending promo first keeps the refundable pool as small as possible
for as long as possible.

**Margin is applied inside the module** (`with_margin`), at reserve AND settle, and the rate in force
is recorded on every entry — so a rate change cannot retroactively rewrite what a call cost, and two
call sites cannot disagree.

**The hold reaper is lazy**, at the top of `reserve`, scoped to the calling org. A crash between relay
and settle would otherwise strand that money forever. A background timer would need a scheduler and
leader election on a multi-instance deploy, and would still only run on a timer; sweeping one org's
stale holds is paid by the caller who benefits from it, and an org that never calls again has no
balance to strand.

**Idempotency on `topup` is enforced by the database.** `stripe_payment_intent` is UNIQUE, and `topup`
FLUSHES immediately after adding the block, before the balance moves: the loser of a race rolls back
and returns the winner's block, giving the same answer as the sequential path. The application-level
SELECT is an optimisation, not the guarantee — two concurrent deliveries of one PaymentIntent both
miss it. (Fixed in #45; the migration is `db.py` A28, placed above the `(B)` legacy block because that
block returns early on a fresh database — precisely the one that needs it.)

## Stripe (`billing.py`)

**Credit happens on the WEBHOOK, never on the browser's return from Checkout.** The success redirect
is a URL the payer controls; treating it as proof of payment would let anyone mint balance by typing
it. The one exception is the off-session auto-top-up charge, where the server itself holds the
PaymentIntent's confirmed status — nothing attacker-supplied is involved — so it credits immediately
and the webhook redelivery lands as a no-op.

The webhook lives at `POST /billing/stripe/webhook`, **deliberately separate from the landing demo's
`/stripe/webhook`** and signed by a different secret: they are different Stripe accounts' events with
different consequences, and sharing a path would let one secret authorize the other's effects. It
**404s when unconfigured**, so a deploy without the secret exposes no unauthenticated POST surface.
`verify_event` uses the SDK's `verify_header` (timestamp tolerance = replay protection, and it handles
the several-signatures case during rotation) rather than `construct_event`, so a genuine event of a
type this SDK version predates is accepted and then ignored, not rejected as forged. A handler failure
returns 500 **on purpose**: that is how Stripe is told to retry.

The Stripe SDK is synchronous, so every call goes through `_sdk()` onto a worker thread — a blocking
network call on the event loop would stall every in-flight request, including the proxy's hot path.

`_credit` also emits the `topup_completed` product-analytics event (`analytics.capture`, PostHog),
riding the same `fresh` flag as the receipt email so a redelivery re-emits nothing. `capture` is
synchronous and swallowing by construction — analytics is the one side effect in the webhook that is
allowed to fail, and it must fail silently, because a raise here would 500 the handler and make Stripe
retry a payment that already credited. Amounts travel as canonical integer `amount_micro`; the
`amount_usd` on the event is display-only.

**Invoices exist on the manual path only.** The top-up Checkout sets `invoice_creation`, so a
one-off purchase produces a real Stripe Invoice — number, PDF, billing address, tax ID — which is the
document a finance team accepts; Stripe's card receipt is not. Auto-top-up charges a bare off-session
PaymentIntent and therefore has **no** invoice, only a receipt: attaching one would mean rebuilding
the automatic charge as InvoiceItem + Invoice paid off-session, rewriting the money path and its
idempotency guarantees for the minority of payments. Say "invoice" only about the manual path.

Turning `invoice_creation` on makes Stripe emit `invoice.created` / `invoice.paid` for every top-up.
`handle_webhook_event` drops them, deliberately: crediting on an invoice event as well as on the
PaymentIntent would be a second door onto the same money. The invoice is a document; the
PaymentIntent is the payment. Note also that `invoice_creation` on one-time Checkout is **priced
separately** by Stripe, and invoice emails only go out with Customer emails → Successful payments
enabled in the dashboard.

**`list_payments` reads rows from us and documents from Stripe.** The payment list is built from our
own `CreditBlock` rows — the same table the balance is computed from, so the history can never show a
payment the balance disagrees with, and amounts and dates need no network call. Stripe is asked only
for the links, in two list calls (`Charge.list` + `Invoice.list`, joined in memory) rather than two
per row; a failure degrades to rows without links and reports `stripe_ok: false`, because a Stripe
hiccup should cost the payer a download button, not their payment history. Both Stripe windows cap at
100 payments, so a very old top-up on a busy account comes back link-less — the portal is the
unbounded archive.

**`create_portal_session` is the self-serve surface** for card, billing address, tax ID and the full
invoice archive: hosted, because every one of those is a form we would otherwise own and the tax-ID
rules go stale per country. It requires a portal configuration saved in the Stripe dashboard, and it
refuses an org with no `stripe_customer_id` rather than minting one — a customer exists once someone
has paid, and an empty portal has nothing to show. `billing_state.portal` is the flag the UI hides
the button on, so a new team never sees a button that would 422.

**Auto-top-up is guarded in depth**, because it is the part that can go wrong expensively: recorded
consent (the PSD2/SCA mandate, a compliance requirement rather than a checkbox), a monthly cap, a
cooldown stamped in the DB *before* the charge so a second web worker sees it, a consecutive-failure
limit, and an idempotency key derived from the threshold crossing — so a burst of concurrent calls
that all notice the low balance produces exactly ONE charge.

Authorization splits by WHAT, not by who. `_billing_org` (the `/billing/*` routes — cards, top-ups,
auto-top-up policy, payment history, the portal) requires **admin or owner**: a card, a spend policy
and an invoice archive are the org's money, not a member's preference.

`GET /orgs/{id}/balance` is different, and deliberately so. Any **member** sees the figure and the
in-flight holds; the **funding detail** (credit blocks, the ledger) stays admin+. It used to be
admin-only, which meant a machine identity could not read the balance it was spending — while every
402 already hands the caller `balance_micro`, and both `llms.txt` and `skill.md` tell an agent to run
`treg balance` after a call. Refusing the number there while shipping it in an error was incoherent.
(Reported by Jason, 2026-08-07.)

## The spend ceiling (`api.py`)

`_enforce_platform_daily_cap` is a per-org, per-UTC-day ceiling on platform spend, and it is
**fail-closed** — unlike the per-user call cap, which may let a few extra through under load. A query
that cannot answer refuses the call, because this one meters *our* money. The balance alone is not
enough: auto-top-up refills it, so the cap is the blast radius of both a runaway agent and a pricing
mistake in the catalog.

An endpoint whose price is unknown never reaches this path at all: `catalog_store.platform_eligible`
requires `cost_view(...)["usd"] is not None`, so "we don't know" is refused rather than served free —
see [catalog](catalog.md).

## Checking the work (`reconcile.py`)

Read-only, query-time, no scheduler. Three questions, each needing its own source of truth:

- **`price_drift`** — did the catalog's price stay true? Compares, per endpoint, the estimate
  RESERVED against the cost the provider REPORTED, both on the same `CallRecord` row. Providers
  re-price whenever they like; a silent 10% climb turns a positive margin negative with nothing on
  fire, and this report is the only thing that notices.
- **`provider_spend`** — reads the **ledger**, not the audit table, because it is the number a human
  holds next to an invoice. Audit rows are fire-and-forget and may be missing; ledger rows may not.
- **`repeat_rate`** — measurement only: how much of the bill was the same query twice. Answering it
  first is what makes a cache a decision rather than a guess.

Two aggregations happen in Python rather than SQL on purpose — the ledger's provenance lives in a JSON
`meta` column (portable JSON extraction across SQLite and Postgres is not worth a report), and these
are admin-scale windows over a bounded number of metered calls, the same tradeoff `admin_stats` makes.

## Where a call's money actually moves

    resolve → _platform_offer (priced + eligible?) → _enforce_platform_daily_cap (fail-closed)
            → ledger.reserve (the UPDATE gate; 402 if short)
            → relay upstream
            → settle at the observed cost when the provider reports one
              (dataforseo `cost`, scrapecreators `credits_charged`, akta `credits_consumed` —
              the last is what makes akta's per-section enrich billable: the estimate is an
              upper bound, the settle is the real charge), else at the estimate;
              release instead when the call was not billable

Closing the hold runs on its **own session** (the request's may be mid-rollback from the very error
being released for) and **never raises** — the caller already has their answer, and a ledger hiccup
must not turn a served call into a 500. A hold that fails to close is not lost money either: the
reaper releases it, which errs in the org's favour.

## Shared-plan pricing: flat-fee providers, and the rate treg sets

A flat-fee provider (a monthly subscription with a rate limit or unlimited calls) has no per-call
vendor price, which kept every one of them out of the catalog. The ladder that admits them:

| The provider sells | The price of one call |
|---|---|
| real credits | vendor price ÷ credits (the normal fx entry) |
| a monthly request cap | fee ÷ cap — same arithmetic |
| a rate limit only | fee ÷ theoretical max is the FLOOR; the rate sits above it at a stated break-even |
| unlimited | a treg-set rate with the break-even printed |

The honesty rule that makes the last two rungs defensible: **we never claim these are vendor
prices.** What treg sells there is its own service — subscription custody, the key, a share of the
rate limit — at a published rate whose fee and break-even are printed beside it (fx.yaml
`kind: treg_shared_plan`; `check_fx` makes the marker impossible to carry dishonestly). The price is
also congestion control: at $0, one looping agent exhausts a shared rate limit for every team at
once.

Mechanically a shared-plan provider is just a credit provider whose credit is "one call on treg's
shared plan" — `cost_view`, holds, caps and settlement needed zero changes. What is new:

- **A 429 is never billable**, under any cost type. Capacity refusing a request is not the caller's
  bad input, and on a shared key it is treg's own saturation. This also fixed a pre-existing wrong:
  `per_call` used to bill upstream 429s, and no vendor bills a request it refused to accept.
- **`shared_plan_recovery`** (`GET /admin/reconcile/shared-plans`): fee versus collected per
  treg-set rate, with `suggested_usd = fee ÷ measured calls` and an action at ±50% thresholds. It
  REPORTS; a human edits fx.yaml monthly. An auto-adjusting price would move under an agent's feet,
  and a rate card that moves on its own is not a rate card. The fee is scaled to the report's
  window, so a 7-day report compares against a quarter of the fee.
- **`price_drift` never sees these providers** — drift compares our estimate against the provider's
  own reported charge, and a flat-fee provider never reports one. Pinned by a test that fires if an
  observed-cost parser is ever added for one, because at that point the drift report would be
  policing a price treg itself set.

## Retries: a call must not be paid for twice

Prompted by a public question — *"how does result pricing handle retries, agents need idempotent
billing before this works"* — and it matters more here than for a human-facing API, because agents
retry far more than people do.

**Most retries were already free**, which is what makes the real gap narrow. `_platform_billable`
never bills a 5xx, a 3xx, a timeout or a network error, and bills a 4xx only under `per_call` where
the provider charges for accepting the request at all. Result pricing settles on the provider's own
reported number (`_observed_cost_micro`), so a `per_success` lookup that finds nothing costs nothing.

The gap is one case: treg reached the provider, the provider succeeded **and charged us**, and the
response was lost on the way back. The agent retries and we pay twice.

### Why remembering the charge is not enough

The cheap fix is to note that a key was already billed and skip the second charge. It does not work:
treg would still make the second upstream call, so we would still pay the provider and would simply
move the double cost onto ourselves. The second request has to not reach the provider at all, which
means storing the first response and replaying it.

### The surface

`Idempotency-Key: <label>` on `/call/`, or the `idempotency_key` argument to the MCP `call` tool. A
replay answers with `X-Treg-Idempotent-Replay: true` and `X-Treg-Cost-Micro` set to what the FIRST
call cost, so a caller can report the charge honestly rather than implying a second one. Over MCP the
result carries `replayed: true`.

Nothing happens without it. A caller who sends no label sees byte-identical behaviour to before the
feature existed, which is what made it safe to ship.

### The key belongs to the caller

`IdempotentCall` is keyed on `(membership_id, key)`. Every door — a personal token, an agent token,
an OAuth grant — resolves to one `Membership`, so a single rule covers a human, an agent, and two
agents in the same team: **the label belongs to whoever called**.

Not `key` alone: clients choose their own labels, the same string will be picked twice, and that
collision would serve one team's stored response to another. It is the only failure in this feature
that leaks data rather than money. Not per-org either — two lazily written agents in one team both
reach for `retry-1` and would collide for no reason.

The key is never derived from the request. That was proposed and rejected: two identical searches an
hour apart are new work, and treg cannot tell that from a retry. A server-invented key would turn a
correctness feature into a 24-hour cache that quietly serves stale data.

### What is stored, and for how long

Metered successes only, for 24 hours. A team calling on its **own** key is billed by the provider, so
there is nothing to protect and no reason to hold their response. A failure is never billed, and
replaying one would freeze an error the caller should be free to retry out of — so a failed call
frees its label immediately.

That is also what bounds storage: bodies are kept only for calls that actually cost money, for a day.

### Concurrency, and giving the label back

A `pending` row is written **before** the upstream call, and that row is the lock: two retries
arriving together race on the unique constraint, and the loser is told to wait (409) instead of
duplicating the spend. Same reasoning as the conditional UPDATE in `ledger.reserve` — where two paths
can read before either writes, the database has to arbitrate.

A request that dies after claiming must give the label back, or a single bad parameter would hold it
for the full window and answer every retry with 409 — worse than the problem this solves. The release
happens in the `StarletteHTTPException` handler, the one place every refusal passes through; the call
handler has a dozen raise points and releasing at each would be a dozen chances to miss one.

Expired rows are swept lazily at claim time, scoped to the calling caller, exactly like the hold
reaper: no scheduler, no leader election on a multi-instance deploy.
