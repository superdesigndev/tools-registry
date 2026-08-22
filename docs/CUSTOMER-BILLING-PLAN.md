# Customer-level billing — builders reselling treg to their own customers

**Status:** planned, awaiting review. Nothing built. Written to be handed to a maintainer cold; no
context from the design conversation is assumed.

## Who is asking, and for what

Builders are integrating treg into their own agent products. Their end-customers use the product;
the product calls treg underneath. The builder needs to know and limit what each of THEIR customers
consumed, and to bill those customers themselves.

## The boundary decision everything follows from

**treg meters and enforces; the builder monetizes.** treg never bills a builder's end-customer, never
holds their card, never sets their price. What treg owes the builder is exactly three things:

1. **Attribution** — what did customer X consume this month
2. **Enforcement** — refuse customer X at the limit the builder set
3. **Export** — numbers clean enough to invoice from

Everything below is those three things and nothing else. Margin, pricing to the end-customer, and
invoicing are the builder's own Stripe and their own business.

## Two models, one already shipped

| Model | Who pays treg | Status |
|---|---|---|
| **Customer-pays** | each end-customer, their own team + balance | **Shipped.** The builder's product connects through treg's OAuth server (consent screen, team picker). Built for ChatGPT, deliberately client-agnostic |
| **Builder-pays** | the builder, one org, one balance | **This plan** |

Customer-pays suits developer-tool products whose users would want their own treg anyway. Builder-pays
is what most builders mean, and is the gap.

## Builder-pays: the design

### The metadata bag, and the reserved `customer` key

ONE mechanism, deliberately — not a dedicated header per special concept. Every call may carry a
small tag bag, and `customer` is a reserved key inside it:

    POST /call/hunter.people.email.find
    X-Treg-Token: <builder org token>
    X-Treg-Meta: customer=cust_8123, feature=email-finder, env=prod

Over MCP, the same thing as an optional `meta` dict argument on the `call` tool.

The split of powers:

- **Any key** is recorded and can be grouped in the usage report (`group_by=feature` works the same
  as `group_by=customer`). This is the AWS cost-allocation-tags model; Anthropic's API does the same
  (free metadata, one blessed `user_id`).
- **Only `customer`** carries enforcement: budgets, `blocked`, idempotency scoping, and the
  token-per-customer upgrade path. Two reasons this must not generalize: a budget check against
  arbitrary tag combinations is N lookups on the hot path instead of one, and retry scoping needs
  exactly ONE partitioning dimension (a call tagged `customer=a, feature=b` has no right answer for
  which one partitions its idempotency keys).

Future special keys get semantics by reserving another key in the bag, never by adding a header.

Bag rules: max 5 keys per call; key and value are opaque strings, max 32/128 chars, stored as given.
Docs must say "use your internal ids, not emails" — tags land in audit rows and usage exports, so
PII does not belong in them. No bag means today's behaviour exactly.

Spoofing is a non-issue by construction: the only party who could mis-tag is the builder, and the
only budgets and reports a tag touches are the builder's own.

### Piece 1 — record the bag (attribution)

Audit rows already carry a `telemetry` dict and ledger settles already carry a `meta` dict. The
bag's keys merge into each. No schema change; attribution becomes a query.

### Piece 2 — `CustomerBudget` (enforcement)

New table, org-scoped (goes into `_ORG_SCOPED_MODELS` — the delete-cascade test will catch it if
forgotten, it has before):

    CustomerBudget: org_id, customer (string), monthly_cap_micro | daily_cap_micro (nullable),
                    calls_per_day (nullable), status (active|blocked), created/updated

CRUD under `/orgs/{id}/customers`. Enforcement sits at the same insertion point as
`_enforce_daily_cap` in the `/call/` path and reads the trailing spend for (org, customer) the same
way the daily cap reads per-user spend.

The refusal must be its own error, `customer_budget_exceeded`, with the cap and the period in the
body. The builder's product needs to show THEIR user a clean "you have reached your plan limit"
message, not a treg error about someone else's balance. Never include the org's overall balance in
this response: that is the builder's private number, and the response may be shown to their customer.

A `status: blocked` customer is refused outright — the soft version of revocation, no token
management needed.

### Piece 3 — the usage endpoint (export)

    GET /orgs/{id}/usage?group_by=customer&from=...&to=...
    -> [{customer, calls, charged_micro, by_provider: {...}}, ...]

The aggregation pattern already exists in `reconcile.provider_spend` (audit rows, 30-day window);
this is the same query with a different group-by and a date range. JSON now; CSV later if builders
ask. This output is what the builder invoices from.

### Isolation mode: a token per customer (already exists)

The tag counts; a token controls. For customers needing real control, the builder mints them a
dedicated agent token: `POST /orgs/{id}/agents` — which already exists end to end.

| Need | Tag enough? | Token needed? |
|---|---|---|
| usage counting, budgets, invoicing | yes | no |
| cut off ONE customer instantly | `status: blocked` works | revocation also works |
| different tool access per customer (premium vs free tier) | no | yes — per-member `tool_access` exists |
| token runs on the CUSTOMER's device (leak containment) | no | yes |

Rule for the docs: **tag for counting, token for control.** Start everyone on tags; upgrade the few.

One glue task: a call made with a per-customer token should attribute to that customer WITHOUT the
bag — set `meta.customer` from a `customer` field on the membership (nullable column on Membership, set
when the agent is minted with `--customer cust_8123`). Then both modes produce one consistent usage
report.

## Three traps, found at design time — do not skip these

1. **The per-member daily cap throttles the whole customer base.** All tagged traffic rides one
   membership, so one member's $5/day cap becomes the product-wide ceiling. Builder orgs need that
   cap raisable (per-member override already exists in spirit via unmetered members; the clean fix is
   a per-membership `daily_cap_micro` override column). Without this, builder-pays does not survive
   contact with its tenth customer.
2. **Idempotency must scope by tag.** Two of the builder's customers WILL both send `retry-1` under
   the shared token. `IdempotentCall` is unique on `(membership_id, key)` today; it must become
   `(membership_id, customer, key)` with `customer` defaulting to "". NOTE: this is an ALTER of an
   existing unique constraint — `create_all` will not do it; prod is Postgres and needs a real
   migration step in the deploy.
3. **Why not one org per customer** (the tempting shortcut): the builder's balance fragments into N
   pots topped up separately, their secrets would need copying into every org, and the $1 promo grant
   multiplies by N — a free-credit printing machine. Rejected; do not revisit without solving all
   three.

## Order, with a stop after each step

1. The bag: accept the `X-Treg-Meta` header + the MCP `meta` arg, record into audit telemetry +
   ledger meta. Attribution only, nothing enforced. Tests: tagged call lands in both records with all
   keys; untagged call byte-identical to today; a 6th key refused with a clear error.
2. The usage endpoint. Tests: grouping, date range, tags with zero spend absent, and org isolation
   (org A cannot read org B's usage — the multi-tenancy test pattern).
3. `CustomerBudget` + enforcement + `blocked`. Tests: cap refusal shape, the error never leaks org
   balance, blocked refused, capless customer unaffected.
4. Idempotency scoping + the Postgres migration for the constraint.
5. The membership `customer` field + `--customer` on agent-new (isolation glue).
6. The per-membership cap override.
7. Docs: llms.txt (a "for builders" section), `docs/context/architecture/multi-tenancy.md` and
   `money.md` fragments, MAP.md entries, skill regeneration if skill.md changes.

Steps 1–3 are the product; 4–6 are correctness and scale; each is shippable alone.

## Reuse scoreboard

Reused: orgs, ledger reserve/settle, audit, the daily-cap insertion point, the OAuth server
(customer-pays model), the public-demo precedent (it already meters many anonymous users on one
shared token, by IP — this is the same mechanism with the grain changed to a customer id), agent
tokens, per-member ACLs.

Genuinely new: one table, one header (the meta bag), one endpoint, two columns, one migration.

## What could go wrong, where to look first

- **The budget check racing itself** — two concurrent calls both under the cap, together over it.
  Same class as every reserve race; acceptable for v1 (caps are soft business limits, not ledger
  invariants), but say so in the code comment rather than let it be discovered as a bug.
- **Usage endpoint performance** — audit rows per org can be large; the query needs the existing
  org+created index plus a telemetry-tag strategy (worst case, a small denormalised
  `customer_spend` daily rollup later; do not build it until it hurts).
- **Tag cardinality abuse** — a runaway integration sending a fresh UUID per call (as any key's
  value) bloats reports. Cap distinct values per key per org (say 10k) with a clear error, counted
  cheaply at budget-create and report time.
