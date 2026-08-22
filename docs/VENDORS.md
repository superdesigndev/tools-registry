# List your API on treg

treg is the tool catalog for AI agents: ~2,600 endpoints across ~40 providers, discoverable by
*capability* ("get a domain's backlink summary") rather than by vendor name. Agents search the
catalog, compare providers side by side on price / measured success rate / speed, and call your
API through treg's proxy with the credential injected server-side.

**What listing gets you:**

- **Discovery** — every agent connected to a treg deployment can find your endpoints when they
  search for the job your API does, next to (and compared against) your competitors.
- **Zero-friction customers** — teams that connect their own key to your API call you directly;
  you keep the billing relationship. treg never sits between you and your customer's account.
- **Metered platform traffic (optional)** — if your pricing is published and machine-checkable,
  treg can serve your endpoints on *its own* key, metered per call from teams' prepaid balances.
  That means paid usage from teams that never signed up with you. See "Platform-eligible" below.

## Eligibility

You need ALL of these. They exist because every listing is *live-tested*, not transcribed:

| Requirement | Why |
|---|---|
| **Self-serve API keys** — sign up, get a key, no sales call | The whole listing pipeline runs in one session; sales-gated APIs can't be verified |
| **Key rides in a header or query param** | treg injects credentials as a header (preferred) or query param. Keys embedded in the URL path are not supported |
| **A free (or near-free) probe endpoint** that returns 2xx for a valid key and a distinguishable failure for an invalid one | Connect-time verification. We will literally send your API a garbage key and watch it get rejected — **an API that accepts any key cannot be listed** |
| **Published pricing** with a stable URL | Costs in the catalog carry provenance (`source_url`, `checked`, `confidence`); "contact sales" prices can't be listed as numbers |
| **Accurate docs** for your core endpoints: param names, types, required flags, and example *values* | Every listed endpoint gets a real test call; docs without example values can't be verified |

Strongly preferred (each one directly improves your listing):

- **An OpenAPI spec at a stable URL** — lets us machine-generate your *full* endpoint surface as
  an "extended tier" (hundreds of routes), not just the hand-curated core 8–15.
- **A machine-readable rate card endpoint** (an API route that returns your current per-endpoint
  prices) — the strongest cost provenance we have, and the fastest route to platform eligibility.
- **Per-success billing** (errors free) — makes verification and retries cheap, and means broken
  calls never bill treg users.
- **Documented rate limits**, including per-route limits, not just the account-wide one.

## What to send us

The fastest route: paste the prompt from the **"List as vendor"** button on the dashboard's
Catalog page into your coding agent — it follows the hosted instructions at
[`/vendor-listing`](https://treg.to/vendor-listing) and opens the PR for you.
Or open an issue or PR on this repo yourself, with:

0. **A contact email in the PR/issue description** (e.g. `Contact: partnerships@yourapi.com`) —
   we use it to arrange a test credential for live verification; a PR without one cannot be
   verified or merged

1. `service` slug (lowercase), display name, and a one-line summary of what an agent can DO
2. `base_url`, auth mechanics (header name + format, or query param name)
3. Probe endpoint path + exact bad-key behavior (status code, or the JSON field that flips)
4. Pricing page URL + billing model; rate-card endpoint if you have one
5. Docs URL; OpenAPI spec URL if published
6. Your 8–15 core endpoints, each with example parameter values and a cheap test target
7. A test credential (or a small credits grant) sent privately once we reach out — **never in
   the issue/PR** — so we can run live verification
8. **A self-verification ledger**: before submitting, run every `test_request` live against your
   own account and record, per endpoint, the HTTP status, the cost your YAML claims, and the cost
   your meter actually charged (charge field, rate-card endpoint, or balance delta), dated. Docs
   drift; meters don't — a real submission's docs-transcribed price was 5× under what the meter
   charged. Deliberate-miss test targets must be labeled as such, with the hit price observed
   once on a real target.
9. **A full-surface map**: every documented operation, marked catalogued or excluded-with-reason —
   including the free count/preview routes and your cheapest operation tiers, which are the ones
   agents (and reviewers) look for first.

The example below is the complete shape of a listing.

## Example implementation — "Acme SEO"

A fictional vendor: Acme SEO sells backlink data at `https://api.acmeseo.example/v1`, key in an
`X-Api-Key` header, free `/account` route, $0.002 per successful request, pricing at
`https://acmeseo.example/pricing`.

### 1. Registry entry — `src/treg/oauth_providers.py`

How a team connects a key, and how treg verifies it is real:

```python
ACMESEO = OAuthProvider(
    service="acmeseo",
    display_name="Acme SEO",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Acme SEO API key",
    token_header="X-Api-Key",
    token_format="{secret}",
    setup_url="https://acmeseo.example/dashboard/api-keys",
    setup_action_label="Get your Acme SEO API key",
    setup_steps=(
        "Sign in to Acme SEO and open Settings → API Keys.",
        "Copy your API key.",
    ),
    setup_note="Backlink queries are billed per successful request; the account check is free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Backlink profiles, referring domains and anchor texts for any domain or URL.",
    base_url="https://api.acmeseo.example/v1",
    docs_url="https://docs.acmeseo.example",
    probe_path="/account",  # free — a bad key gets a 401 here
)
```

…added to the `REGISTRY` tuple, plus a neutral lettermark at `src/treg/web/logos/acmeseo.svg`
and the two test-list additions (`test_every_provider_is_registered`, `test_key_providers`).

If your API answers HTTP 200 even for a bad key, the entry instead names the body field that
distinguishes them (`token_verify_field`, `token_ok_field`/`token_ok_value`, or
`token_reject_field`) — tell us the exact behavior and we pick the right one.

### 2. Core catalog file — `src/treg/catalog/acmeseo.yaml`

What an agent can do, priced and verifiable:

```yaml
provider: acmeseo                  # must equal the registry entry's `service`
source:
  docs: https://docs.acmeseo.example
  openapi: https://api.acmeseo.example/v1/openapi.json
  curated: 2026-08-12
limits: "60 requests/minute per key"
pricing_url: https://acmeseo.example/pricing
endpoints:
  - id: acmeseo.web.backlinks.summary
    capability: web.backlinks.summary     # the cross-provider join key, from capabilities.yaml
    platform: web
    scope: any_account                     # data about anything, not the caller's own account
    method: GET
    path: /backlinks/summary
    name: "Backlink summary"
    summary: "Aggregate backlink profile of a domain or URL"   # vendor's wording, verbatim
    input:
      queryParams:
        target: {type: string, required: true, note: "domain or full URL", example: "moz.com"}
        mode:   {type: string, required: false, note: "one of domain | url", example: "domain"}
    test_request:                          # the CHEAP call verification replays
      queryParams: {target: "moz.com", mode: "domain"}
    cost:
      type: per_success                    # errors free
      value: 0.002
      currency: USD
      source: docs
      source_url: https://acmeseo.example/pricing
      checked: 2026-08-12
      confidence: documented
    docs_url: https://docs.acmeseo.example/backlinks-summary
  # …7–14 more endpoints, same shape
```

The `capability` field is what puts you on the comparison shelf: an agent asking for
`web.backlinks.summary` sees every provider that implements it, with prices side by side. If
your API does a job the taxonomy lacks, the file proposes it under `proposed_capabilities:`.

### 3. What we run before it merges

Your self-verification ledger speeds this up but never replaces it: every claim in a vendor PR —
prices, probe behavior, evidence — is re-checked against an **independent** live run with a
credential we control. `verified:` stamps and example responses are ours to add, on what we
watched succeed. A ledger that disagrees with its own YAML bounces the PR; a price that disagrees
with our meter gets corrected from the observed charge.

```bash
# 1. Live bogus-key test: a garbage key POSTed to /connections/token must come back rejected
# 2. Live verification of every endpoint's test_request (with the test credential, from env)
TREG_CATALOG_CRED='<key>' uv run python scripts/catalog_verify.py acmeseo.yaml
# 3. Schema + referential integrity
uv run python scripts/catalog_validate.py     # must exit 0
# 4. The full test suite
uv run --frozen python -m pytest -q
```

Endpoints that pass get a `verified:` date and a truncated, PII-scrubbed real example response
committed to `src/treg/catalog/examples/` — that example is what convinces an agent choosing
between providers, so passing verification is the listing's main asset.

## Platform-eligible — getting paid calls from treg's own key

Beyond discovery, treg can serve your endpoints on its **own** key, charging the calling team's
prepaid balance per call. An endpoint qualifies when:

- its cost is machine-computable in USD (a concrete `value`, or a convertible credit/unit rate),
- the price's `confidence` is `verified` (observed billed, or read from your live rate card) or
  `documented` (transcribed from your published pricing), and
- it isn't an own-account or account-management route.

Eligibility is necessary but not sufficient — treg also has to hold a funded account with you
and allow-list your service, which is an ops decision made per provider. The fastest way to get
there: publish a rate-card endpoint, bill per success, and give us a verification credit grant.

## What gets declined

Recorded reasons from past reviews, so nobody wastes a cycle:

- API accepts any key (no way to validate a connection)
- Key embedded in the URL path
- Sales-gated / enterprise-only access
- Legal or shutdown risk; deprecated or absorbed products
- UI-only products with no API

## Keeping your listing healthy

- **Prices moved?** Tell us or PR the `cost` blocks — stale prices are flagged after 90 days.
- **Routes changed?** If you publish OpenAPI, a re-ingest regenerates your extended tier from
  the spec; the curated core file is updated by hand, so breaking changes there need a heads-up.
- **Success rates are public.** treg aggregates observed success rate and latency per endpoint
  across all callers and shows them next to your price. Reliability is your ranking.
