---
name: vendor-listing
description: >
  Onboard a vendor who wants their API listed in the treg catalog. Use whenever someone asks
  "how do we get listed on treg", a vendor sends their API details, or a listing PR/issue needs
  review. Walks the whole pipeline: eligibility gate → registry entry → logo → tests → LIVE
  bogus-key test → core catalog YAML → verify → scrub → validate. The vendor-facing doc this
  skill implements is docs/VENDORS.md.
---

# Vendor listing — add a provider to the catalog

A listing has **two halves**, and both must ship:

1. **Registry entry** (`src/treg/oauth_providers.py`) — how a team connects a credential for the
   provider, and how treg verifies that credential is real.
2. **Core catalog file** (`src/treg/catalog/<service>.yaml`) — what an agent can *do*: 8–15
   curated endpoints with capability mapping, inputs, cost + provenance, and verified examples.

Deep references (read before non-trivial work; do not duplicate them here):
- `docs/context/guides/expanding-a-category.md` — the add-a-provider playbook, verify toolbox, traps
- `docs/context/architecture/catalog.md` — catalog schema, cost provenance, verify pipeline, PII rules
- `docs/VENDORS.md` — what we told the vendor to prepare (their checklist)
- `src/treg/web/vendor-listing.md` — the HOSTED instructions (served at `/vendor-listing`) that a
  vendor's own coding agent follows to raise a listing PR; the dashboard's "List as vendor" modal
  (connections view, `vendorAsk` in `index.html`) hands vendors a prompt pointing at it. Keep the
  three vendor-facing surfaces (doc, hosted page, modal prompt) telling one story.

## Step 0 — intake: collect the vendor facts

Before touching code, you need ALL of these. If the vendor's submission is missing any, ask —
do not guess ("unconfirmed" beats a wrong path shipped):

- **A contact email for the vendor's team** — required in the PR/issue description. It is how a
  test credential gets arranged for live verification; without it the listing stalls at step 7.
- `service` id (lowercase slug), display name, one-line summary (what an agent can DO)
- `base_url` (exact API root)
- Auth: where the key rides (header name + format, or query param name). Key **in the URL path is
  not supported** — decline or defer.
- A **free or near-free probe endpoint** where a valid key returns 2xx and an invalid key does NOT
  — plus the exact bad-key behavior (status code, or the JSON field that signals invalid)
- Pricing page URL, per-endpoint prices, and the billing model (`per_call` / `per_success` /
  `per_result` / credits / quota). Machine-readable rate-card endpoint if they have one.
- Docs URL; OpenAPI spec URL if published
- The 8–15 endpoints they consider their core surface, with example parameter *values*
- A test credential (or credits grant) for verification — read it from env only, never write it
  into any file

## Step 1 — eligibility gate

Reject decisively, with a recorded reason, when:
- The key **cannot be validated** (API returns success for garbage keys) — e.g. ScrapeCreators
- Key rides in the **URL path** (`/v3/{key}/…`) — injectors do header/query only
- **Sales-gated** signup (no self-serve key breaks the fast path)
- Legal/shutdown risk, or deprecated/absorbed products

## Step 2 — registry entry

Add an `OAuthProvider(auth_kind="key", …)` in `oauth_providers.py` and append it to `REGISTRY`.
Model it on `HUNTER` (a clean key provider). Pick the verify fields from the toolbox table in
`expanding-a-category.md` (`token_header`/`token_format`, `token_location="query"`+`token_param`,
`probe_url`, `probe_method`+`probe_json`, `token_verify_field`, `token_ok_field`+`token_ok_value`,
`token_reject_field`, `probe_reject_statuses`, …). Prefer a header over a query key so the secret
never lands in a logged URL. Set `category` (add to `CATEGORY_ORDER` only if genuinely new),
`summary`, `base_url`, `docs_url`, `probe_path`, and `setup_url`/`setup_steps` so a user can find
their key.

## Step 3 — logo

`src/treg/web/logos/<service>.svg` — a **neutral lettermark**, not the real brand mark.
`test_every_provider_has_a_logo` fails without it.

## Step 4 — tests

- Add the id to `test_every_provider_is_registered` (test_oauth_providers_m3)
- Add it to the offerable loop in `test_key_providers`

## Step 5 — LIVE bogus-key test (load-bearing; never skip)

Start the server, `POST /connections/token` with a **garbage key** against the real API:
- `422 "rejected …"` → correct. Ship it.
- `200` → the probe does not validate the key → fix the verify fields or drop the provider.
- `404`/`502` in the reason → wrong probe path/host → fix `base_url`/`probe_path`.

**Never ship a key provider you haven't watched reject a bogus key.** Use a throwaway org
(`e2e-…@treg.local`) and delete it after. Watch for the known traps: trailing-slash 307 (put the
slash in `probe_path`), 200-with-error-body (read a body field), CSV/text responses.

## Step 6 — core catalog YAML

`src/treg/catalog/<service>.yaml`, following the schema in `catalog.md`. In order:

1. **Ingest** from OpenAPI if published (never hand-transcribe paths); else from docs with
   `source.openapi: null`.
2. **Select** ~8–15 endpoints; ALWAYS include ones matching capabilities other providers already
   implement (overlap enables comparison).
3. **Map** each to a capability from `capabilities.yaml`; missing jobs go under
   `proposed_capabilities:` in the provider file, not straight into the shared taxonomy.
4. **Describe** `input` (param names, types, required, location; constraints into `note`).
5. **Cost** with full provenance: `type/value/currency/per/unit` + `source/source_url/checked/
   confidence`. Unknown price → `value: null` + `confidence: unknown` + a note. Prefer a
   rate-card endpoint (`source: rate_card_api`) over a pricing page.
6. **test_request** per endpoint — CHEAP: smallest limit, one item, public well-known target.
   ⚠️ Never probe with empty params "expecting a validation error": a no-required-params endpoint
   returns its full default result set and bills for it (the Moz quota trap).

## Step 7 — verify, scrub, validate

```bash
TREG_CATALOG_CRED='<secret>' uv run --frozen python scripts/catalog_verify.py <service>.yaml
uv run --frozen python scripts/catalog_validate.py    # must exit 0
uv run --frozen python -m pytest -q
```

- Stamp `verified:` only on endpoints that PASSED. Docs lie; documented ≠ verified.
- **Scrub every captured example** (this repo is public): no named private individuals
  (contact-lookup routes get `untestable:` + no test_request + no example), no third-party
  emails/phones riding along, no first-party account identity.
- No credential value anywhere in the diff.

## Step 8 — optional extended tier

If the vendor publishes a stable OpenAPI spec with example parameter values, add an
`ingest_<service>()` to `scripts/catalog_ingest.py`, register it in `INGESTERS`, and generate
`<service>.extended.yaml`. Rules: never probe with a real call; platform = what the data is
ABOUT; normalise platform slugs across providers. Bulk-verify with
`catalog_verify_extended.py --dry-run` first, then with an explicit `--budget`.

## Reviewing a vendor-RAISED PR (they wrote the files; you verify)

The same pipeline, entered from the other end. Every vendor claim is **untrusted input** — one
vendor PR was outright malicious (#92), and an honest one shipped a docs-transcribed price 5×
under the real charge (#141, GitHub→LinkedIn: claimed 1 credit, metered 5). The order:

1. **Gate on the required PR evidence** (per `docs/VENDORS.md` items 8–9): the self-verification
   ledger (per-endpoint status + claimed vs metered cost, dated) and the full-surface map.
   Missing → ask for it before spending review time. A ledger that contradicts its own YAML
   bounces the PR unreviewed.
2. **Diff hygiene first**: expected files only (registry entry, catalog YAML, logo, two test
   lists, fx row), data-only changes, no credential values, no `verified:` stamps or committed
   examples (those are yours to add).
3. **Merge it onto current main locally** before verifying — catalog PRs staleness-conflict in
   the shared test lists and REGISTRY tuple within days.
4. **Independently verify with a key YOU control** (steps 5–7 above): watch the bogus-key
   rejection yourself and quote the real wire body, run `catalog_verify.py` over every
   test_request, and reconcile every cost block against the meter (charge field / rate-card
   endpoint / balance delta) — the vendor's ledger is a cross-check, never the source of truth.
   Where a price disagrees, fix it from the observed charge (`source: observed`,
   `confidence: verified`) and tell the vendor their docs are stale.
5. **Audit the curation against their surface map**: are the free count/pre-flight routes and
   cheapest operation tiers in? Deliberate-miss test_requests labeled, with the hit price
   observed once? per_success semantics actually observed (a miss settling at 0)?
6. **Finish the maintainer half they can't**: front-door counts (llms.txt, skill.md, README) +
   `scripts/build_plugin.py`, docs drift, and — if their prices are verified/documented — the
   optional tier-4 key slot. Land your verified version (a maintainer branch superseding their
   PR is fine); close their PR with credit and the findings.

## Step 9 — done means

- Validator exits 0; suite green; bogus-key rejection observed live
- The PR/issue carries the vendor's contact email (and no credential value anywhere)
- Every endpoint carries `verified:` + example, or an explicit reason it couldn't be live-tested
- Docs synced: run `bash .claude/skills/tools-registry-context/scripts/drift.sh`, update touched
  fragments in the same commit
- If pricing has `confidence: verified|documented` with a computable USD figure and the endpoints
  aren't `own_account`/`kind: account`, the vendor is **platform-eligible** — tell them treg can
  additionally serve their endpoints on its own key once the provider is keyed and allow-listed
  (`platform_key_for`), which is a separate ops decision, not automatic.
