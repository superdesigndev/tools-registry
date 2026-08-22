---
name: treg-integration
description: Integrate treg into your own product — give your users ~2,600 external API tools without owning the keys, and bill each of your customers for what they used. Covers MCP, CLI and HTTP, per-customer attribution, spend limits, and invoicing.
---

# Integrating treg into your product

You are a coding agent. A human has pointed you at this file because they want **treg** inside their
product. Read this whole file before writing code — the billing section changes how you write the
plumbing, so writing the plumbing first means rewriting it.

**What treg is:** one base URL and one token that reach ~2,600 external API endpoints (SEO, SERP,
backlinks, social, enrichment, ads, scraping) across ~40 providers, plus whatever the team registered
themselves. treg holds the provider credentials and injects them server-side; your caller makes the
real upstream request and gets the provider's real response back. Calls served on treg's own key are
metered per call from a prepaid balance at cost — **0% markup**.

**What this file is for:** the case where *your product* is the customer of treg, and *your users* are
customers of you. You pay treg; you bill them.

---

## 1. Pick an integration method

Three doors, same token, same behaviour. Pick by where your code already lives.

| Method | Use when | Shape |
|---|---|---|
| **HTTP** `POST {BASE}/call/<endpoint-id>` | your backend already makes HTTP calls — **the default for a product** | you build the request, treg injects the credential and relays |
| **MCP** `{BASE}/mcp/` | your agent runtime speaks MCP and you want tool discovery for free | `catalog_search` → `catalog_get` → `call` |
| **CLI** `treg` | scripts, CI, a devbox — not usually a product integration | thin client over the same API |

### HTTP — the one you probably want

```bash
curl --get "{BASE}/call/scrapecreators.x.v1-facebook-group" \
  --data-urlencode "url=https://www.facebook.com/groups/366190054572553/about" \
  -H "X-Treg-Token: $TREG_TOKEN" \
  -H "X-Treg-Meta: customer=cust_8123"
```

Find endpoint ids and prices first — no auth needed:

```
GET {BASE}/catalog/search?q=facebook+group     # what exists, and what each costs
GET {BASE}/catalog/endpoints/<id>              # params, method, example
```

Two response headers matter on every call:

- **`X-Treg-Cost-Micro`** — what this call cost, in integer micro-USD (1e-6 USD). Present only on a
  metered call; absent means it ran on the team's own key and was not billed.
- **`X-Treg-Call-Id`** — a stable id for this call. **Store it on your side.** It is how you join
  treg's records to yours later, and it resolves via `GET {BASE}/calls/<id>`.

### MCP

Point your client at `{BASE}/mcp/` with `Authorization: Bearer <token>`. Tools: `catalog_search`,
`catalog_get`, `call`, `balance`, `my_tools`. The `call` result carries `cost_usd`.

### Auth, and the one trap

Tokens come in two kinds and the difference will cost you an afternoon:

- **Org-scoped** (`treg org agent-new <name>`) — belongs to one team, works as a bare bearer.
  **Use this in a product.**
- **Identity** (`treg login`) — belongs to a *person*, who may be in several teams. Recent versions
  pin it to the active team so it also works bare; an older or unpinned one answers
  `400 choose an org (send X-Treg-Org)`. If you see that, either send `X-Treg-Org: <slug>` or mint an
  org-scoped token instead.

---

## 2. Tag every call with your customer — this is the whole billing model

**Send `X-Treg-Meta` from your backend on every call.** Up to 5 `key=value` pairs:

```
X-Treg-Meta: customer=cust_8123, workspace=ws_9, feature=lead-enrichment
```

Values may contain letters, digits and `. _ - :` only, ≤128 chars, and must not look like an email —
these become storage keys in an append-only ledger, so treg refuses anything it cannot safely keep.
A malformed bag is a `422` **before** anything is relayed, so it costs nothing.

### Set it in your code, never in a prompt

Do **not** expose the tag as an argument your LLM fills in. A model will omit it somewhere in a long
chain, and a number you cannot reconcile is worse than no number. Your backend already knows which
user a request belongs to and already sets the `Authorization` header — set the tag at that same call
site:

```ts
// the ONE place your product talks to treg
async function tregCall(path: string, ctx: { customerId: string; workspaceId?: string }) {
  const tags = [`customer=${ctx.customerId}`];
  if (ctx.workspaceId) tags.push(`workspace=${ctx.workspaceId}`);

  const r = await fetch(`${TREG_BASE}/call/${path}`, {
    headers: {
      "X-Treg-Token": process.env.TREG_TOKEN!,
      "X-Treg-Meta": tags.join(", "),
    },
  });

  // Record what it cost against YOUR customer, keyed by the id treg returns.
  await db.usage.insert({
    customerId: ctx.customerId,
    tregCallId: r.headers.get("X-Treg-Call-Id"),
    costMicro: Number(r.headers.get("X-Treg-Cost-Micro") ?? 0),
  });
  return r;
}
```

Route every treg call through one function like this. If two call sites can disagree about who gets
tagged, eventually they will.

---

## 3. Limit what a customer can spend

`PUT {BASE}/orgs/<org_id>/budgets/<dim>/<value>` — admin token required.

```bash
# cap one customer at $5/day
curl -X PUT "{BASE}/orgs/1/budgets/customer/cust_8123" \
  -H "X-Treg-Token: $TREG_TOKEN" -H 'content-type: application/json' \
  -d '{"daily_cap_micro": 5000000}'

# a DEFAULT for every customer without an override (omit the value)
curl -X PUT "{BASE}/orgs/1/budgets/customer" ... -d '{"daily_cap_micro": 1000000}'

# cut someone off; their caps survive the block
curl -X PUT "{BASE}/orgs/1/budgets/customer/cust_8123" ... -d '{"status":"blocked"}'
```

Unlimited until you set something. Setting the first limit on a key also **declares** it as a budget
dimension — up to 3, because each one is checked on every call.

**Limits stack.** A `workspace` ceiling and a `customer` ceiling both apply to a call carrying both,
and the refusal names which one hit.

### The refusals your users may see

These carry nothing about your treg account — no balance, no top-up link — so they are safe to surface:

```json
403 {"error":"tag_blocked","dim":"customer","val":"cust_8123","message":"…"}
429 {"error":"tag_spend_cap_reached","dim":"customer","val":"cust_8123",
     "spent_micro":5000000,"cap_micro":5000000,"period":"day","message":"…"}
```

**Never proxy a treg error body to your end user blindly.** The team-level ones (`402
insufficient_balance`, `429 platform_daily_cap_reached`) contain *your* balance and a top-up URL.
Handle those yourself.

> **Per-customer caps are advisory, not exact.** They are checked against an aggregate, so concurrent
> calls can overshoot by roughly one call's estimate per in-flight request. Your prepaid balance is
> the hard limit. Do not sell these to your users as a precise cap.

---

## 4. Invoice your customers

```
GET {BASE}/orgs/<org_id>/usage/by-tag?key=customer&days=30
```

```json
{"key":"customer","days":30,
 "rows":[{"value":"cust_8123","charged_micro":41234,"charged_usd":0.041234,"calls":22}],
 "attributed_micro":41234, "unattributed_micro":1880, "total_micro":43114}
```

- **Money comes from the ledger**, not from the call log. Audit rows are fire-and-forget and are shed
  under load — exactly the traffic a growing product generates — so anything you bill from must come
  from here. `GET /calls` is for debugging, never for invoices.
- **`attributed + unattributed == total`**, for every key. If it doesn't, stop and investigate rather
  than shipping the invoice.
- **`unattributed_micro` is spend you could not attribute** — calls that carried no value for that
  key. Reconcile it to zero; anything left is a call site that forgot to tag.

A call is credited **in full** to each of its tags, so grouping by `customer` and by `workspace` each
reconcile to the same total on their own. Never add two groupings together.

---

## 5. Isolation, when a tag isn't enough

A tag is a label your backend asserts — fine when the only budgets and reports it touches are yours.
If a credential will run on *your customer's own machine*, mint a token pinned to them instead:

```bash
treg org agent-new cust-8123-bot --pin customer=cust_8123
```

That token can only ever bill `cust_8123`; naming another customer is a `403`. **Tag for counting,
token for control.** Start everyone on tags and upgrade the few who need real separation.

---

## 6. Before you ship

- [ ] One function wraps every treg call, and it sets `X-Treg-Meta` from request context.
- [ ] `X-Treg-Call-Id` is stored on your usage rows.
- [ ] Your invoice reads `usage/by-tag`, and you assert `attributed + unattributed == total`.
- [ ] `unattributed_micro` is zero — or you know which call site isn't tagging.
- [ ] Team-level `402`/`429` bodies are handled by you, never forwarded to a user.
- [ ] Auto top-up is on, or you monitor `GET /orgs/<id>/balance` — a flat balance fails every call.
- [ ] Retries send `Idempotency-Key`; a replay returns the stored answer and charges nothing.

**A provider's own error is relayed to you verbatim**, so a `4xx`/`5xx` may be theirs, not treg's.
treg stamps **`X-Treg-Error: 1`** on its own refusals — branch on that header, not on the status code.
Failed upstream calls are not billed; a `per_call`-priced endpoint does bill a genuine `4xx` caused by
your own bad input, but never one caused by a credential or quota problem.
