---
title: Endpoint catalog — what you can DO with a connected key, and which provider should do it
status: shipped
sources:
  - src/treg/catalog_store.py
  - src/treg/endpoint_stats.py
related:
  - architecture/money.md
  - architecture/proxy-model.md
  - interface/cli.md
---

# Endpoint catalog — platform-grouped operations per provider

## Why

The marketplace registry (`oauth_providers.py`) catalogs *credentials*: how to connect a provider.
It says nothing about what you can DO once connected — which endpoints exist, what they cost, what
they return. Agents guess paths from external docs and burn paid calls. This layer answers that,
and it runs through the team's OWN keys: every call is proxied, governed and audited.

The catalog adds that operations layer:

- **platform** (tiktok, instagram, google, web, …) → the marketplace grouping axis: click TikTok,
  see every provider + endpoint that serves TikTok data.
- **capability** (`tiktok.user.profile`) → the same operation across providers, so a user can
  compare TikHub vs JustOneAPI for one job, and a future router can fail over between them.
- **verified example responses** → captured during live testing, because docs show request params
  but choosing an API comes down to what actually comes back.

## Where things live

```
src/treg/catalog/
  capabilities.yaml        # the shared capability taxonomy (the cross-provider join key)
  fx.yaml                  # currency -> USD rates + per-PROVIDER credit rates (see "Cost" below)
  <service>.yaml           # CORE tier — hand-curated; <service> = OAuthProvider.service
  <service>.extended.yaml  # EXTENDED tier — machine-generated full endpoint surface
  examples/<endpoint-id>.json  # truncated, scrubbed real responses captured at verify time
scripts/
  catalog_validate.py         # schema + referential checks (run in CI / after any edit)
  catalog_verify.py           # live-tests CORE endpoints with a real credential; writes examples/
  catalog_verify_extended.py  # the same for the extended tier, in bulk, under a spend cap
  catalog_ingest.py           # bulk-generates the extended tier from provider specs
  catalog_cost_provenance.py  # backfills cost units + provenance; re-run after any re-ingest
```

Data files are YAML (curation-friendly); nothing in the app imports them yet. Serving them
(`GET /catalog/platforms/…`), stamping provisioned tools' `examples`, the dashboard view, and the
capability router are later phases (see the plan in this doc's history / PR description).

## Two tiers

Curation and coverage pull in opposite directions: an agent needs to know that TikHub *can* read
Zhihu answers (breadth), and separately needs one endpoint per job that is known to work and known
to cost $0.001 (depth). The catalog carries both, in separate files, distinguished by `tier`.

| | `tier: core` — `<service>.yaml` | `tier: extended` — `<service>.extended.yaml` |
|---|---|---|
| written by | a human, one endpoint at a time | `scripts/catalog_ingest.py`, from the provider's spec |
| size | ~10–15 per provider | every route the provider exposes (hundreds to ~1400) |
| `capability` | required — the cross-provider join key | absent; nothing is mapped to the taxonomy yet |
| `input` / `test_request` | required, hand-written | generated, when the provider documents parameters |
| `verified` + `example_response` | expected | where the generated `test_request` passed a live call |
| example size | ~10 KB, arrays → 2 items | ~2 KB, arrays → 1 item (shape, not fidelity) |
| `cost` | required | present when the provider publishes per-route prices |

The extended tier originally carried no `input`, `test_request` or verification at all — the tiers
split on *curated vs. generated*, and it read as if it also split on *tested vs. untested*. It does
not, and it should not: a provider that documents its parameters (with example values, as TikHub
does) gives us everything needed to generate a test request and make the call. What stays exclusive
to core is the part a machine cannot do — mapping the endpoint to a capability, choosing the test
target deliberately, and a full-fidelity example. See "bulk-verifying the extended tier" below.

Core wins on collision: `catalog_ingest.py` drops any `(method, path)` the provider's core file
already curates, so an endpoint appears exactly once across both tiers. Promoting an extended entry
means moving it into the core file and completing it (steps 3–8 below) — not editing it in place;
extended files are regenerated wholesale and hand edits are lost.

## Schema

### capabilities.yaml

```yaml
capabilities:
  tiktok.user.profile: "Public profile of a TikTok user (followers, bio, stats)"
  web.backlinks.summary: "Aggregate backlink profile of a domain or URL"
platforms:
  tiktok: "TikTok"
  web: "The web at large (backlinks, authority, traffic)"
```

Rules:
- A capability id is dot-delimited, lowercase; the FIRST segment is its platform slug.
- Ids name the *job*, not the provider's endpoint ("get user profile", not "fetch_user_profile_v2").
- Adding a capability = adding it here. Provider files may carry `proposed_capabilities:` (same
  mapping shape) when curation discovers a job the taxonomy lacks; the reviewer merges them into
  this file. The validator accepts a capability that is either global or proposed in the same file.

### `<service>.yaml`

```yaml
provider: tikhub                # must equal OAuthProvider.service in oauth_providers.py
source:
  docs: https://docs.tikhub.io/
  openapi: https://api.tikhub.io/openapi.json   # null when the provider has no spec
  curated: 2026-07-28
limits: "10 requests/second per key"   # optional, provider-level: the rate/quota model in one line
pricing_url: https://…                 # optional: where CURRENT prices live (values in cost blocks age)
endpoints:
  - id: tikhub.tiktok.user.profile   # unique; convention: <provider>.<capability>
    capability: tiktok.user.profile  # must exist in capabilities.yaml (or proposed_capabilities)
    platform: tiktok                 # must equal the capability's first segment
    domain: user                     # optional: the platform page's section. One lowercase word.
                                     #   Omit it and the loader derives one — capability's middle
                                     #   segment, else a path keyword, else the path's grouping
                                     #   segment, else "other". Set it only to override a bad guess.
    scope: any_account               # any_account (scrapers) | own_account (first-party OAuth)
    kind: data                       # optional; data (DEFAULT) | action | account | utility.
                                     #   what the endpoint IS — see "Kind" below. Absent ⇒ data.
    method: GET
    path: /api/v1/tiktok/web/fetch_user_profile   # relative to the provider's base_url
    name: "Get user profile"        # optional short DISPLAY title (≤60 chars). Set it when the
                                    #   summary is doc-prose too long for a row heading; clients
                                    #   fall back to `summary` when absent.
    summary: "Public TikTok profile by username"  # the provider's own description, kept VERBATIM —
                                    #   `name` is ours to word, `summary` is theirs
    input:                           # split by location — mirrors treg's binding model
      queryParams:
        uniqueId: {type: string, required: false, note: "username from the profile URL", example: "tiktok"}
        secUid:   {type: string, required: false}
      note: "one of uniqueId | secUid; uniqueId preferred"
      # also allowed: pathParams, body, bodyType (json|form)
    test_request:                    # EXACT params catalog_verify.py sends — must be cheap
      queryParams: {uniqueId: "tiktok"}
    expect:                          # optional; default is "HTTP 2xx"
      json_path: code                # dotted path into the response JSON
      equals: 200                    #   (for providers that answer HTTP 200 even on failure)
    cost:
      type: per_success              # per_call | per_result | per_success | free | quota_rows
      value: 0.0015
      currency: USD
      note: "charged on 2xx only; errors free"
    verified: 2026-07-28             # date of the last PASSING catalog_verify.py run; absent = unverified
    example_response: examples/tikhub.tiktok.user.profile.json   # written by catalog_verify.py
    docs_url: https://docs.tikhub.io/…
```

### `<service>.extended.yaml`

Generated — never hand-edited. Re-run `uv run python scripts/catalog_ingest.py <service>` instead.

```yaml
provider: tikhub                  # same rule as core: equals OAuthProvider.service
source:
  method: openapi + provider rate card   # how the entries below were derived
  ingested: 2026-07-28                   # date of the generating run
  spec_urls:                             # every upstream the run read, so it is reproducible
    - https://api.tikhub.io/openapi.json
endpoints:
  - id: tikhub.x.zhihu-web-fetch-answer-comments   # <service>.x.<path-slugified>; the `.x.`
    tier: extended                                 #   infix keeps extended ids out of the
    platform: zhihu                                #   `<provider>.<capability>` namespace
    method: GET
    path: /api/v1/zhihu/web/fetch_answer_comments
    name: "Zhihu answer comments"   # optional, same meaning as core; the ingesters harvest it
                                    #   where the spec offers a human title distinct from the
                                    #   description (TikHub's Apifox op names, Just One API's
                                    #   per-op summary / info.title, DataForSEO's operationId).
                                    #   Carried across re-ingests by id, like `capability`.
    summary: "Get comments of a Zhihu answer"
    kind: data                      # optional; data (DEFAULT) | action | account | utility (see "Kind")
    cost: {type: per_success, value: 0.001, currency: USD}   # optional
    docs_url: https://docs.…                                 # optional
    input:                          # generated from the provider's parameter docs
      queryParams:
        answer_id: {type: string, required: true, note: "Answer id", example: "1913...”}
    test_request:                   # generated: documented example values, page sizes clamped
      queryParams: {answer_id: "1913…", limit: 5}
    verified: 2026-07-28                                     # a real call passed
    example_response: examples/tikhub.x.zhihu-web-fetch-answer-comments.json
```

Rules:
- Required: `id`, `platform`, `method`, `path`, `summary`. `platform` must exist in
  `capabilities.yaml` — that is what puts the endpoint on a marketplace shelf.
- `capability` is normally ABSENT (extended entries are unmapped). If one is present the validator
  holds it to the full core rules, so promotion by hand cannot silently drift.
- `cost` is optional, because several providers price per API family rather than per route. When
  present it must still be a real cost model (`cost.type` from the same enum as core).
- `input` / `test_request` appear when the provider publishes enough parameter documentation to
  generate them; both are machine-written and are rewritten on the next ingest.
- `verified` + `example_response` mean a live call was made and passed, and carry exactly the same
  weight as in core — the validator applies one rule to both tiers: verified ⇒ a `test_request` to
  re-verify with and an `example_response` file that exists.
- Exactly one of `verified`, `unverified`, `untestable` or `skipped` should be present on an entry
  that has been through the pipeline:
  - `untestable: <reason>` — set at INGEST: no test request could be generated (route absent from
    the provider's docs, or a required parameter only the caller can supply, e.g. their own
    platform cookie). No call was made and none is possible with a bare key.
  - `unverified: http 404 …` — set at VERIFY: the call was made and failed, with the status code
    and the provider's message. This is a finding, not a gap: `402` means the route needs a paid
    plan tier, a family-wide run of `404`s means the provider's upstream scraper is broken.
  - `skipped: <reason>` — set at VERIFY: a usable test request exists and the call was deliberately
    not made, to conserve a paid balance. The reason names the sibling endpoint that WAS verified,
    or why one call costs too much. See "the fourth state" below — these clear with money, not
    investigation, which is what separates them from the two above.

  `catalog_validate.py` ENFORCES this: an extended entry that has been through the pipeline (it
  has a `test_request`, or any of the four keys) must claim exactly one of them, non-empty. Two at
  once is a contradiction; a key present but empty is the failure that motivated the rule — a
  re-run overwrote an endpoint's result record, dropped the reason string, and stamped an empty
  state, which every other check happily passed. A never-verified entry straight out of ingest has
  neither a request nor a state key and is left alone.
- Ids are unique across the WHOLE catalog, both tiers, all providers.
- Two optional fields exist only in this tier, both added for the first-party OAuth providers:
  - `host: <fqdn>` — this route is NOT on the provider's `base_url`, and its `path` is relative to
    the named host instead. Google splits one product across sibling `*.googleapis.com` services
    (GA4 reporting vs GA4 admin; six separate My Business services) while an `OAuthProvider` names
    one host. The same OAuth token calls them all, so the endpoints are real and worth listing —
    but the auto-provisioned tool is bound to `base_url`, so calling one needs a second tool bound
    to that host. Absence of `host` means "callable through the provisioned tool".
  - `scope_gap: <one line>` — the credential treg's OAuth app obtains CANNOT call this, and this is
    the scope that is missing. These are listed rather than dropped on purpose: the set of gaps is
    the answer to "which scopes should we add to the registered app", and it is only visible if the
    endpoints stay in the file. `scope_gap` present ⇒ expect 403 until the app is widened.

### Kind — the browse surface vs. the plumbing

`kind` says what an endpoint IS, so the marketplace can lead with the useful surface and tuck the
provider's own machinery out of the way. It is optional in BOTH tiers; absent reads as `data`.

| `kind` | what it is | examples | browse |
|---|---|---|---|
| `data` (default) | fetch / scrape / enrich a resource | get user profile, backlink summary, SERP | shown |
| `action` | a meaningful WRITE on the connected user's OWN account | post a video, reply, update an ad budget, upload | shown |
| `account` | the provider's own list/webhook/saved-search/credit CRUD | create/delete a lead-list, manage webhooks | hidden |
| `utility` | helpers with no data of their own | token/x-bogus generators, enum & location listings, decrypt/encrypt, device register | hidden |

`data` + `action` are the **browse surface**; `account` + `utility` are **management endpoints**.
Three things follow, and they are the whole point of the field:

- **The platform census counts data + action only.** `GET /catalog/platforms` reports each shelf's
  `endpoints` / `capabilities` / `verified` and its "from …" price over the browse surface — a
  management endpoint is real inventory but it is not what a tile advertises, so it never inflates
  those numbers (nor the marketplace tile counts the dashboard renders from them).
- **The default platform view drops them.** `GET /catalog/platforms/<slug>` returns only the
  browse surface in `capabilities` / `extended` / `domains`, plus a `hidden_count`. Pass
  `?include_hidden=1` to get the WHOLE surface back — every endpoint carries `kind`, so a client
  can fold the plumbing behind its own control. The dashboard does exactly this: it requests
  `include_hidden`, renders data/action in the ledger, and files account/utility behind a small
  per-section "N management endpoints" expander (the same show-more gesture as the platform tiles).
- **`kind` is a reviewed judgement, carried across re-ingests.** Like `capability` and `name`, an
  extended entry's `kind` is set by review, not derived from the spec, so `catalog_ingest.py`'s
  `carry_verification` re-attaches it by id — regenerating the file must not reset it to `data`.

`catalog_validate.py` only checks the value when present: a stated `kind` must be one of the four.

### Cost — the file keeps the billing unit, the server computes USD

A `cost` block stays in whatever unit the PROVIDER bills in; that is the number that stays correct
when a rate moves. `cost.usd` is added at SERVE time by `Catalog.cost_view` from `fx.yaml`, so a
rate refresh re-prices the whole catalog without touching a provider file. Clients (dashboard cards,
`treg catalog search`, `treg catalog get`) lead with `usd` because a column is only comparable in
one unit, and fall back to the native amount when `usd` is null.

The full block:

```yaml
cost:
  type: per_result        # per_call | per_result | per_success | free | quota_rows
  value: 2.00             # non-null unless confidence: unknown
  currency: USD           # USD | CNY | credit | unit
  per: 1000               # the quantity `value` covers (default 1)
  unit: row               # what `per` counts — or, under `currency: unit`, the provider's meter
  source: docs            # rate_card_api | docs | observed | vendor_email | inferred
  source_url: https://…   # the exact rate card / pricing page (or rate-card endpoint)
  checked: 2026-07-28     # when the PRICE was confirmed — not when the route was called
  confidence: documented  # verified | documented | inferred | unknown
  note: "…"               # free text: the half of the charge the schema cannot hold, caveats, traps
```

`value` + `currency` + `per` answer *how much*; `type` + `unit` answer *per what*; `source` +
`source_url` + `checked` + `confidence` answer *says who, and how sure*. All four questions have to
have an answer before treg will spend its OWN money on an endpoint (see "platform-eligible" below),
which is the whole reason the provenance keys exist.

**`per` and `unit`.** Read a block as "`value` `currency` per `per` `unit`". SpyFu bills a CPM, so
`value: 2.00, per: 1000, unit: row` — and `cost_view` divides, serving `usd: 0.002` per row. Hunter
charges 1 credit per 10 emails (`per: 10, unit: record`), Akta 1.5 credits per 50 reviews. Without
`per`, every one of those had to be either wrong or rounded into prose.

**Three kinds of denomination convert, and they convert differently:**

- **A real currency** (`currency: USD`, `CNY`) uses `fx.yaml`'s `rates_to_usd`, keyed by currency.
- **`currency: credit`** is NOT a currency. A credit is a PROVIDER-SCOPED unit — one scrapecreators
  credit and one lusha credit have nothing to do with each other — so it converts with the rate for
  the endpoint's provider from `fx.yaml`'s `credit_rates_usd` block, keyed by service. That is why
  `cost_view(cost, provider)` takes the provider: the same `value: 1, currency: credit` is worth
  $0.00188 on scrapecreators and $0.1248 on lusha.
- **`currency: unit`** is the provider's own METER: Semrush's "API units", Majestic's three
  independent allowances, Moz's row quota. `unit` names which meter, and the rate comes from
  `fx.yaml`'s `unit_rates_usd[provider][unit]`. A provider can spend several meters at once —
  Majestic's analysis / retrieval / index-item units no more convert into each other than two
  providers' credits do, so each gets its own row. Before this existed, Moz's `quota_rows` blocks
  carried no `currency` at all, defaulted to USD, and served every Moz route as costing $1.00.

A `credit_rates_usd` entry may carry **`kind: treg_shared_plan`**: a rate TREG SET for a flat-fee
provider (a subscription with a rate limit or unlimited calls), where no per-call vendor price can
exist. The credit is then "one call on treg's shared plan" and the machinery is unchanged — the
honesty lives in the entry: the basis must start with "treg shared-plan rate", name the vendor fee,
and state the break-even volume, and `fee_usd_month` must be present as data (the validator's
`check_fx` enforces all of it). The rate is reviewed monthly against `reconcile.shared_plan_recovery`
and edited by hand. The full ladder: docs/SHARED-PLAN-PRICING-PLAN.md; the billing side (429 never
billable, the recovery report): architecture/money.md.

Each `credit_rates_usd` / `unit_rates_usd` entry carries `usd` plus the `basis`/`source`/`checked` that justify it —
the cheapest PUBLICLY listed tier (plan price ÷ credits included), so the served figure is an upper
bound on real spend, never an under-estimate. `usd: null` is a deliberate state, not a gap: the
provider publishes no per-credit price (sales-negotiated like Crunchbase, or not
credit-priced at all like BrightData). Those endpoints keep `cost.usd = null` and display natively
("3 credits/success"), because a guessed dollar figure is worse than an honest credit count. Both
blocks are hand-maintained and must stay ABOVE `rates_to_usd:` — `catalog_fx_update.py` rewrites the
file from the text before that key and discards anything below it.

#### Provenance — `confidence` is a claim about the PRICE, not about the route

`verified: 2026-07-28` on an endpoint says the route answered. `cost.confidence: verified` says the
money figure was confirmed. They are independent, and conflating them is how a guess gets spent:

| `confidence` | what earns it |
|---|---|
| `verified` | observed being billed on a real call (`source: observed`), or read from the provider's own live rate card (`source: rate_card_api` — TikHub's `get_all_endpoints_info`, DataForSEO's `/appendix/user_data`, ScrapeCreators' `credits_charged` in its OpenAPI) |
| `documented` | transcribed from the provider's docs or pricing page |
| `inferred` | the figure is a floor or the top of a published range — a base fee with a per-row half on top ("1 credit base + 1 per ad"), a spread ("1–9 credits", "$0.50–$5.00 per 1,000"). The recorded number is not the whole charge, and the note says what else applies |
| `unknown` | no figure is published anywhere citable. `value` MUST be null and `note` MUST say why |

Rules the validator enforces: `value: null` and `confidence: unknown` appear together or not at all;
a `verified`/`documented` price names its `source_url` (`source: observed` is exempt — its evidence
is the captured example response, not a page that may have moved); every priced entry carries
`checked`, and CI WARNS past 90 days. A file whose header says `UNVERIFIED` caps its prices at
`documented`: nothing in it has been called, so no price in it can have been seen being charged.

Free is spelled exactly one way — `type: free, value: 0, currency: USD, unit: call` — and needs no
provenance, because 0 does not move and there is nothing to re-check. It was previously written
three incompatible ways across 661 endpoints, which left `cost.usd` null on most of them:
indistinguishable, downstream, from "price unknown".

`scripts/catalog_cost_provenance.py` owns the mapping from what the repo knows about a provider's
pricing to these keys, and is re-runnable — the extended tier is regenerated wholesale, so
provenance typed by hand into a generated file would not survive the next `catalog_ingest.py`.

#### Platform-eligible — when treg may spend its OWN key on a call

`Catalog.platform_eligible(endpoint)` is the single predicate behind prepaid/platform-key access
(tier 4 of the credential ladder in `api.py`). One implementation, so the API, the validator and
the proxy cannot drift. It requires ALL of:

- `cost_view(...)["usd"]` is not None — the charge is machine-computable;
- `cost.confidence` is `verified` OR `documented` (policy widened 2026-07-31: a rate the provider
  itself publishes is billable; `verified` stays the gold standard the drift reports police, and
  `inferred`/`unknown` stay refused — a guess is not a rate);
- `scope != own_account` and `kind != account` — the provider's own bookkeeping is never worth
  spending on, and an own-account route needs the caller's own credential by definition.

The live-called `verified:` stamp is no longer required (same 2026-07-31 change): a broken route
fails unbilled under `per_success`/`per_result` billing, providers that report in-band settle at 0,
and the fail-closed daily platform cap bounds whatever remains — coverage beats caution now that
the reserve/settle machinery is proven. Eligibility alone still spends nothing: the provider must
ALSO be keyed and allow-listed (`platform_key_for`).

The doctrine is asymmetric on purpose: **a missing or unknown price reads as "refuse", never as
free.** An endpoint with no `cost` block at all is therefore not platform-eligible without anything
having to be written out for it, which is why the extended tier's unpriced routes need no
annotation. Where an endpoint carries only `observed_cost` (DataForSEO prices per API family, not
per route), `_effective_cost` synthesizes the block with `source: observed, confidence: verified`
and `checked` = the verify date: a figure the provider itself reported charging is the strongest
provenance the catalog has.

### Core-wins dedup compares NORMALISED paths — except on Graph

A hand-curated core file and a machine-readable spec never agree on placeholder spelling: core says
`/v1beta/properties/{property_id}:runReport`, Google's discovery document says `{property}`. A naive
`(method, path)` comparison therefore misses, and the endpoint ships in both tiers — that is the
DataForSEO `/v3` bug below, in its other form. The Google and X ingesters compare with every
`{...}` collapsed to `{}`, so the two spellings match.

Meta is the exception and uses exact comparison, because on the Graph API the node id IS the first
path segment: `/{post_id}/insights` and `/{page_id}/insights` differ only by the placeholder name
and are genuinely different endpoints. Normalising there would silently drop post insights because
the core file curates page insights.

## Process — adding / curating a provider

Do these steps in order; each has a hard success criterion.

1. **Ingest.** If the provider publishes OpenAPI (`/openapi.json`), fetch it and list candidate
   operations from there — do not hand-transcribe paths (that is how typos ship). Otherwise work
   from the official docs and record `source.openapi: null`.
2. **Select.** Curate, don't mirror: pick the ~8–15 endpoints an agent would actually reach for,
   and ALWAYS include the endpoints matching capabilities other providers already implement —
   overlap is the point (comparison + failover). Skip exotic ops.
3. **Map.** Assign each endpoint a capability from `capabilities.yaml`. Missing job → add it under
   `proposed_capabilities:` in your provider file, don't edit the shared taxonomy in parallel work.
4. **Describe.** Fill `input` from the spec/docs: param names, types, which are required, where
   they ride (path/query/body). Copy real constraints ("one of A|B") into `note`.
5. **Cost.** Record the provider's price model per endpoint from their pricing page — with its
   provenance (`source`, `source_url`, `checked`, `confidence`) and its unit (`per`, `unit`), per
   "Cost" above. `quota_rows` is for row-quota APIs (Moz). Unknown exact value → `value: null` +
   `confidence: unknown` + a `note` saying why. If the provider exposes its rate card as an
   endpoint, prefer it over the pricing page and record it as `source: rate_card_api`: it is
   re-checkable, which is what lets treg serve the route on its own key.
6. **Test-request.** Give every endpoint a `test_request` that is CHEAP (smallest limit, one item,
   public well-known target — e.g. user "tiktok", domain "moz.com"). This is what verification and
   future health checks replay, so it must not burn meaningful credits.
   ⚠️ Quota trap (learned live, Moz 2026-07-28): never probe an endpoint with an empty body/params
   "expecting a free validation error" — an endpoint with NO required params answers with its FULL
   default result set and bills for it (Moz's global_top_* ate an entire 50-row period quota in two
   calls). Always pass an explicit smallest limit, and on row-quota APIs check the usage endpoint
   before and after the first call.
7. **Verify + capture.** Run `scripts/catalog_verify.py <service>.yaml` with the credential in the
   `TREG_CATALOG_CRED` env var. It calls every endpoint's `test_request`, checks `expect`, writes
   the truncated example response to `examples/`, and prints PASS/FAIL per endpoint. Stamp
   `verified: <today>` ONLY on endpoints that passed — documented ≠ verified; docs lie.
8. **Scrub.** Read every captured example: replace anything personal that is not the public test
   target's own public data. The account-info endpoints of YOUR OWN key (quota, balance) must have
   emails/ids masked before commit.
9. **Validate.** `scripts/catalog_validate.py` must exit 0: schema shape, unique ids, capability
   and platform referential integrity, example files exist for verified endpoints, provider exists
   in `oauth_providers.py`.

Success criteria for a provider PR: validator exits 0; every endpoint either carries a `verified`
date + example file or an explicit comment why it could not be live-tested; no credential value
appears anywhere in the diff.

Linkup is the first agentic open-web provider curated here: its core catalog keeps Search's raw,
sourced-answer and structured response shapes separate, plus Fetch. Their documented prices vary
with body parameters, so the static test-request price is marked `inferred` and is not eligible for
treg's platform key until reservation can price `depth`, `outputType` and `renderJs`.

## Process — bulk-ingesting the extended tier

```
uv run python scripts/catalog_ingest.py tikhub          # one provider
uv run python scripts/catalog_ingest.py all --refresh   # every provider, re-downloading the specs
uv run python scripts/catalog_validate.py               # must exit 0
```

The script owns `<service>.extended.yaml` end to end: it fetches the provider's spec, maps every
route to a platform, drops what the core file already covers, and rewrites the file. Downloads are
cached under `~/.cache/treg-catalog-ingest` (override `TREG_INGEST_CACHE`); `--refresh` re-fetches.
Output is deterministic — a re-run with unchanged upstreams produces a byte-identical file, so a
diff always means the provider changed.

Adding a provider means adding an `ingest_<service>()` function and registering it in `INGESTERS`.
Three rules it must honour:

- **Never probe with a real call.** Discovering an HTTP method by sending a GET is how you get
  billed 1400 times (see the quota trap above). TikHub's methods come from an `OPTIONS` request,
  which Starlette answers `405 + allow:` before the handler — and therefore the meter — runs.
- **Platform is the system the data is ABOUT**, not the API family it lives under: DataForSEO's
  `/v3/merchant/amazon/products/live/advanced` is `amazon`, not `merchant`. Anything not tied to
  one system is `web`. Every new slug goes into `capabilities.yaml`'s `platforms` in the same
  change — the script exits non-zero if a generated platform is unknown, which is the guard.
- **Normalise slugs across providers.** Just One API calls it `douyin-tiktok-china` and TikHub
  calls it `douyin`; if both don't land on `douyin`, the marketplace shelf splits in two and the
  cross-provider comparison the catalog exists for silently stops working.

### The first-party OAuth wave (2026-07-28)

The scraper providers sell breadth and their extended tier reads as a menu. The nine providers
where treg owns the OAuth app are the opposite question — *what can this one connected account
actually do?* — and their sources differ per provider:

| service | source | entries | scope gaps |
|---|---|---|---|
| google-search-console | searchconsole v1 discovery | 7 | 0 |
| google-analytics | analyticsdata + analyticsadmin v1beta discovery | 63 (55 on the admin host) | 32 |
| google-business-profile | six My Business discovery docs + 7 hand-listed legacy v4 routes | 60 (45 off-host) | n/a |
| youtube | youtube v3 discovery + the published quota-cost table | 76 | 2 |
| google-ads | the GAQL resource reference — one entry per queryable resource | 42 | 0 |
| x | X's own v2 OpenAPI | 168 | 91 |
| facebook / instagram / meta-ads | hand-curated from the Graph HTML reference | 26 / 23 / 34 | 6 / 7 / 8 |

Three things generalise from it:

- **Google publishes a Discovery document for every API** at
  `https://<service>.googleapis.com/$discovery/rest?version=<v>` — httpMethod, flatPath, a
  description, the full typed parameter list with required flags, and the OAuth scopes each method
  accepts. It is the same class of source as an OpenAPI spec and should always be preferred to the
  HTML reference. Scopes are ALTERNATIVES (holding any one suffices), so coverage is an
  intersection, not a subset. The My Business documents are the exception that declares no scopes
  at all, which is why that provider has no computable gaps.
- **Google Ads is a resource list, not a route list.** One endpoint (`googleAds:searchStream`)
  answers every read and what varies is the GAQL `FROM` clause, so the unit of coverage is the
  queryable resource. Forty entries share a path and differ in `input.note` and `docs_url`.
- **No test_request anywhere in this wave.** Every route needs a property id, a customer id or a
  Page id that belongs to the connected business and that no spec can supply. They are verified by
  replay against a live connection (`--via-treg`), not by a generated blind call.

## Process — bulk-verifying the extended tier

```
TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub --dry-run
TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub --budget 1.80
uv run python scripts/catalog_validate.py            # must exit 0
```

`--dry-run` prints the queue and what it would cost at list price; nothing is called. The real run
goes CHEAPEST FIRST and stops before any call that would push the run past `--budget`, so a
half-finished run has verified the cheap majority rather than an arbitrary slice. Results are
written back into the yaml after every run and a re-run skips what already carries `verified`,
which makes an interrupted run resumable instead of a repeat bill.

Three things to know before pointing it at a new provider:

- **A missing `cost` reads as free, and silently disables `--budget`.** DataForSEO publishes prices
  per API family, so not one of its 216 extended entries carries a `cost` block — which made the
  spend cap inert: a run queued the whole platform at an estimated $0.000 and still spent real
  money, with only the after-the-fact balance readback noticing. The fix is `observed_cost`: the
  charge the provider states in its own response (`tasks.0.cost`), written onto the endpoint at
  verify time and used to budget the next run. It is the better number regardless — measured, not
  transcribed from a price list — and summing it gives a defensible run total, which balance
  arithmetic cannot because it cannot separate our calls from anything else using the same key.
  DataForSEO's full sweep, summed this way: $4.85521 across 177 endpoints.
- **`observed_time` is measured, not read.** The wall-clock seconds WE waited for the response,
  recorded on the endpoint next to `observed_cost`. Two reasons it is not lifted out of the body:
  only DataForSEO reports its own duration, and TikHub's `time` field is a TIMESTAMP
  ("2026-07-27 23:27:48"), so an extractor trusting the field name would write a date into a
  numeric column. It is also the number that matters — `CALL_TIMEOUT` applies to OUR client. Worth
  having because a timeout is recorded as the endpoint's verdict, and the same DataForSEO route can
  swing wildly: `merchant/amazon/sellers/live/advanced` answered in 9s and 105s on two identical
  calls, `products/live/advanced` in 26s and 55s. Under the old 60s ceiling both were coin flips
  that would have written "unverified" onto a healthy route on some runs and not others. Elapsed
  time predicts nothing about price, either: a 0.04s call cost 4x a 26s one.
- **Cost accounting assumes the provider bills per success.** The run's spend is the sum of the
  prices of the calls that returned 2xx. If a provider bills per *call*, that is wrong in the
  optimistic direction — check the balance delta the script prints against its own estimate before
  trusting a large run. It reads the balance before and after for exactly this reason.
- **The parameter source has to give example VALUES, not just names.** A generated test request
  that invents an id verifies nothing: it produces a 404 that looks like a broken endpoint. If the
  provider documents parameters without examples, the honest output is `untestable`, not a guess.
  (For TikHub, the values come from `sampleValue` in their Apifox docs API — their own demo values.)
- **Examples are trimmed to ~2 KB, arrays to one item.** At 1385 endpoints, core's 10 KB cap would
  add ~14 MB of JSON. An extended example is there to show the response SHAPE.
- **Check for a PER-ROUTE rate limit, not just the account-wide one.** TikHub allows 10 req/s on
  the account but only 1 req/s on any single route. A global pacer does nothing about that — it
  spaces consecutive requests across *different* routes — while a retry by definition hits the same
  route again. Retrying after 0.5s therefore guarantees a 429, and the 429 lands in the file as
  though the endpoint had failed: 66 endpoints on the first full run carried a rate-limit verdict
  that said nothing about the endpoint. Any same-route retry has to wait out that window
  (`PER_ROUTE_GAP`), and 429 must count as retryable rather than as an answer.

⚠️ Read the recorded failures before believing them. A `unverified:` line is evidence about one
call at one moment, and the failure modes that look identical in a summary count are not: a 400
that repeats is a verdict, a 400 that passes on the third try is a flaky upstream (TikHub's
LinkedIn family), and a 429 is usually our own fault. Grouping the failures by status code and by
platform family, then re-running one family, is what separates them — pass rates per platform in
the same run ranged from 8% to 100%, and the low ones were mostly not the provider's fault.

**A fix landing mid-sweep leaves the un-noticed batches wrong.** DataForSEO's 8-batch sweep ran
across the moment the `/v3/v3` URL bug (see below) was fixed. The `web` batch failed loudly at 100%
and was re-run after the fix; the `amazon` batch had failed the same way, nobody re-ran it, and its
pre-fix results merged into the file as 7 endpoints marked `unverified: http 404` — which then read
as a retired Amazon route family. All 7 passed on a re-run, first try, for $0.075. Nothing was ever
wrong with them.

Two signals identified it, and both are worth checking before believing any block of failures:
- **The failures aligned exactly with a batch boundary.** `amazon` was the only platform in the
  file with a single `unverified`, and it held 100% of what that batch touched. Endpoint problems
  do not respect our batching; tooling problems do.
- **Siblings verified by a DIFFERENT code path passed.** `dataforseo_labs/amazon/ranked_keywords`
  and `merchant/amazon/asin` were green in the same two families, verified earlier by
  `catalog_verify.py` rather than the bulk runner. A family cannot be both retired and working, so
  the disagreement was between our two callers, not about the endpoints.

The general rule: after fixing a bug that could have produced failures, re-run **every** batch that
ran before the fix, not just the one whose failure you noticed. The loud batch is the one you
already know about; the quiet ones are what ship a false verdict into the catalog.

**How many passes, and when to stop.** On TikHub, LinkedIn went 8% → 27% → 67% → 90% verified over
four passes with no change other than being asked again — 43 of 48 endpoints that a single pass
called broken. Conversion per pass is the stopping signal, not a pass count: 672, +30, +16, +14,
+2. A pass that converts ~2 is convergence, and what remains after it is genuinely broken (for
tikhub, 107 of the final 115 failures are the provider's own "Request failed. Please retry." after
six attempts each). Raising `--retry-attempts` is the cheapest lever available on a flaky provider
and costs nothing but wall-clock under per-success billing.

Just One API shows the same curve from its far end, and what a *confirmed* verdict costs to
establish. Its 13 failures were one uniform error, `code 301 COLLECT FAILED`, clustered in whole
families (Kuaishou, Taobao, JD) — the exact shape that ought to mean "our fault". They survived 3
retries inside a call, then 4 runs, then a serial pass hours later, then a sixth with retry depth
raised 3 → 6: the last two passes converted one endpoint each, for ¥0.35. Same decay, further
along, so its 11 survivors are evidenced verdicts rather than impatience. The rule is therefore not
"retry until it works" but **retry until the result stops changing**.

Two things generalise from that. The one endpoint that flipped was LinkedIn — the family that is
also flaky through TikHub, a different vendor entirely. That is the scraped platform defending
itself, not the API vendor, so expect it from anyone scraping LinkedIn, and treat two LinkedIn
scrapers as one point of failure rather than a redundant pair. And retrying is only free under
`per_success` billing (both social providers); on a `per_call` provider like DataForSEO each retry
and each extra pass is a purchase, so that budget belongs in the plan rather than in a loop.

One caveat on reading `per_success` as "bad input is free": the provider decides what counts as
success. TikHub answers some invalid inputs (a bogus channel id) with HTTP **200**, the error nested
in the body, and "this request will incur a charge" — so the platform meter bills it, faithfully to
what TikHub charges us. When TikHub uses a real 4xx it says "You won't be charged" and the meter
releases the hold. Verified live 2026-07-30.

Then read a sample of the captured examples for PII before committing, as with core — bulk capture
does not remove the scrub step, it just means sampling per platform family rather than reading all
of them.

### `skipped:` — the fourth state, for a call that was affordable but not made

`verified` / `unverified` / `untestable` above cover *passed*, *called and failed*, and *no call is
possible*. Verifying two paid providers against nearly-empty accounts surfaced a fourth case they
cannot express: the test request exists, the call would very likely pass, and it was deliberately
NOT made because the balance was needed elsewhere. Calling that `untestable` is a lie about the
endpoint, and `unverified` is a lie about the provider — it invents a failure that never happened.

```yaml
  skipped: family verified via dataforseo.x.backlinks-summary-live; the DataForSEO account held
    $0.739 on 2026-07-28 and $0.58 of it was spent verifying one endpoint per API family
```

The reason must say what to do about it, which in practice is one of: **the sibling that WAS
verified** (whole-family skips — 155 of DataForSEO's 216, 6 of Just One API's WeChat endpoints at
¥1.0–1.5/call), or **why one call is too expensive to justify** (DataForSEO's `llm_responses`
routes exceed the $0.15/call ceiling). A `skipped` entry needs no re-investigation — only money —
so a top-up plus a re-run clears them in bulk, while `unverified` and `untestable` need a human.

The state distribution is itself the report. Just One API: 227 verified, 21 unverified (11 of them
the provider's own `code 301 COLLECT FAILED` after six passes, 5 `NO PERMISSION` — an account fact,
not an endpoint fact), 6 untestable, 6 skipped. DataForSEO after its top-up and full sweep: 177
verified, 0 unverified, 39 untestable, 0 skipped — nothing in its extended surface is broken, and
every remaining gap is structural (23 routes whose spec ships no example body, 16 on_page routes
needing an async crawl id).

### Two provider-specific traps

- **Chained ids.** Most detail endpoints need an id no spec can supply. The working order is:
  call the search/list endpoints first, harvest ids out of their responses by normalised field
  name (`aweme_id` fills `awemeId`), then call the detail endpoints, then repeat — Just One API's
  WeChat Channels comments need an `objectId` that only exists after search → `convert-export-id`.
  35 of 260 endpoints were verified only because of that second and third pass.
- **A query token is not always a token.** Just One API's POST routes take
  `application/x-www-form-urlencoded` and read the credential from the FORM BODY; leaving it in
  the query string as well makes them fail with a misleading `TOKEN INVALID/UNACTIVATE`.
  `catalog_verify.py --extended` moves it for `bodyType: form` entries.

### DataForSEO paths carry `/v3`, its base_url ends in `/v3`

`catalog_ingest.py` writes DataForSEO's extended paths exactly as the spec spells them
(`/v3/serp/google/organic/live/regular`) while `OAuthProvider.base_url` already ends in `/v3` and
the core file's paths are relative to it (`/serp/google/organic/live/regular`). Two consequences:
`catalog_verify.py --extended` strips a leading duplicate of the base_url path before calling, and
**the ingester's core-wins dedup silently misses**, because it compares `(method, path)` across the
two spellings — every DataForSEO route curated in core is also present in the extended file under
a different id. Fixing that belongs in `catalog_ingest.py` and needs a regeneration.

## Choosing between providers (`endpoint_stats.py`)

307 capabilities are served by more than one provider, and prices inside one capability differ by up
to **261×**. So "which provider" is a real decision, made on every call.

**The agent makes it, not treg** — see `docs/CAPABILITY-CHOICE-PLAN.md` for the measurement behind
that. Two reasons, and the second is the load-bearing one. Providers of the same capability take
*different requests* (only 5 of 171 match exactly), so a router would need a canonical schema treg
does not have; and they sometimes ask a different QUESTION entirely — `hunter.people.email.find`
wants a domain and a name, `leadmagic.x.b2b-profile-email` wants a LinkedIn URL. **Only the caller
knows which inputs it holds.** A router picking on price would choose the second for someone holding
a name, and fail. Routing would also have been the first feature to break the founding rule that treg
relays rather than models.

What treg owes instead is the half only treg can supply, because only treg sees every call from every
tenant: `endpoint_stats.observed()` aggregates **success rate, p50/p95 latency, last-answered and
sample size** per endpoint from `CallRecord` — which has recorded `endpoint_id`, `status_code` and
`duration_ms` since the marketplace shipped and was never read. It rides on
`/catalog/endpoints/{id}`, attached to the endpoint **and every sibling**, because the choice is made
on that page and an agent will not make a second round-trip to compare reliability.

Five rules worth keeping:

- **A 4xx never counts against the provider.** It usually means the caller sent bad parameters;
  counting it would let one agent's mistake make a healthy endpoint look broken to everyone. Only
  2xx versus 5xx decides the rate.
- **A treg refusal is not evidence about the endpoint.** Rows with `refused_by` set (a paywall 402,
  a daily-cap 429 — see the data-model fragment) never reached the provider; they are excluded even
  from `samples`, or a burst of refused calls dresses itself up as traffic. The 2026-08-12 Hunter
  incident — 309 refusals next to 488 real calls — is why.
- **`miss` semantics ride on the endpoint.** Some providers answer "asked and answered: no result"
  with an error status (PDL 404s a person it has no record of; Hunter's combined-find does the
  same). Endpoints with evidenced miss behaviour carry a `miss: {status, means}` block in their
  YAML, surfaced through `endpoint_view` — so an agent reads "404 = no match, don't retry" instead
  of treating an expected empty answer as a failure. Only annotate what the wire has demonstrated.
- **Below `MIN_SAMPLES` we publish the count and nothing else.** "100% from two calls" is noise
  dressed as evidence, and on a quiet endpoint a rate could expose one org's activity.
- **Sample size is always visible**, so `100% (8)` cannot beat `99% (121)` by looking rounder.
- **"Free" is a price, not a missing one.** `platform_eligible` used to demand
  `confidence in (verified, documented)` for every route, but `confidence` says how much we trust a
  NUMBER we are about to charge — and a free route has no number. Requiring it anyway refused 61
  endpoints across 8 providers (28 of Hunter's 35) as though "costs nothing" meant "we don't know",
  which is the one distinction this file otherwise keeps apart. A `type: free` route is now eligible
  without provenance; a PAID route without provenance is still refused.
- **A claim never wears a measurement's badge.** The `LAST OK` column prints a bare age when a real
  call produced it and a **`✓` age** when it came from the catalog's `verified:` stamp — the same
  discipline `confidence:` already applies to price. The stamp is the cold-start answer: it covers
  1,380 of 1,810 eligible endpoints for free, which is why the column is useful on day one.

Team policy sits on top: `CapabilityPin` (see [data-model](data-model.md)) lets an org fix a
capability to one provider, enforced in `_resolve_marketplace_call` before anything is reserved.

Its boundary, verified rather than assumed: a pin gates the **catalog id**, which is the only route
to treg's own key — so it cannot be side-stepped to spend our money (a URL-passthrough call resolves
against the org's OWN tools and 404s without one). A team holding its own key for another provider
can still call that provider by URL; that is their credential and their bill, and `DenyRule` —
host-scoped, applied to every shape of call — is the tool for blocking it.

## Security

PII IS THE HARD RULE. This repo is public, and every captured example ships in it. Three checks
before any example is committed, all learned the hard way:

1. **No named private individuals.** Contact-lookup routes (LinkedIn contact info, people-enrichment
   by email) return a real person's name, personal email and phone. Such an endpoint stays in the
   catalog — the route is real and useful — but it is marked `untestable:` with the reason, carries
   NO `test_request` (so a re-verify cannot silently re-capture it), and no example is stored.
2. **No third-party PII riding along.** Emails and phones turn up inside unrelated payloads — a
   YouTube description, a review body. Sweep every captured example for address-shaped strings and
   mask anything that isn't a business contact.
3. **No first-party identity.** Own-account verification (`mine=true`, your own site in Search
   Console) captures YOUR channel, sitemap and metrics. Point test requests at neutral public
   targets instead, and scrub what you already captured.


Credentials are NEVER written into catalog files, examples, scripts, or docs — the verifier reads
`TREG_CATALOG_CRED` from the environment only. Captured examples are truncated (arrays → 2 items,
long strings clipped, ~10 KB cap) by the verifier, then human-reviewed for PII before commit.

## Pilot providers (first wave)

| service | platform focus | auth (from oauth_providers.py) | overlap group |
|---|---|---|---|
| dataforseo | google, web | Basic (login:password base64) | SEO: web.backlinks.*, web.url.metrics |
| moz | web | Basic (AccessID:SecretKey base64), POST JSON API | SEO: web.backlinks.*, web.url.metrics |
| tikhub | tiktok (+instagram, youtube, x) | Bearer key | Social: tiktok.* |
| justoneapi | tiktok (+instagram, xiaohongshu, weibo) | `?token=` query param | Social: tiktok.* |

The SEO pair and the social pair each implement the same capabilities on purpose — they are the
first real test that the capability taxonomy supports cross-provider comparison.
