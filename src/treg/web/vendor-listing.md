# List your API in the treg catalog — instructions for a coding agent

You are helping an API vendor get listed in treg's tool catalog ({BASE}). The deliverable is a
**pull request to https://github.com/superdesigndev/treg** containing the listing, plus a way for
the maintainers to reach your team.

## Before you write anything

Clone the repo and read these two files — they are the authority, this page is only the summary:

- `docs/VENDORS.md` — eligibility rules, the vendor checklist, and a complete worked example
  (a fictional provider "Acme SEO" showing every file a listing needs).
- `.claude/skills/vendor-listing/SKILL.md` — the exact review pipeline your PR will be run
  through, including the live bogus-key test.

Confirm eligibility first. Hard requirements: self-serve API keys (no sales call), the key rides
in a **header or query param** (never the URL path), a free or near-free probe endpoint that
**rejects an invalid key** with a distinguishable response, a published pricing page, and docs
with example parameter values. If any of these fail, stop and tell your user which one — a PR
that fails them will be declined.

## What the PR contains

1. **Registry entry** — an `OAuthProvider(auth_kind="key", …)` in `src/treg/oauth_providers.py`,
   appended to `REGISTRY`: service slug, display name, `base_url`, auth mechanics
   (`token_header`/`token_format` or `token_location="query"`+`token_param`), `category`,
   one-line `summary`, `docs_url`, `probe_path`, and `setup_url`/`setup_steps` so users can find
   their key. Copy the shape of the `HUNTER` entry.
2. **Logo** — `src/treg/web/logos/<service>.svg`, a **neutral lettermark** (do not reproduce the
   real brand mark).
3. **Tests** — add the service id to `test_every_provider_is_registered`
   (tests/test_oauth_providers_m3.py) and the offerable loop in tests/test_key_providers.py.
4. **Core catalog file** — `src/treg/catalog/<service>.yaml` with the 8–15 endpoints an agent
   would actually reach for, each with: a `capability` from `src/treg/catalog/capabilities.yaml`
   (propose missing ones under `proposed_capabilities:`), typed `input` params, a **cheap**
   `test_request` (smallest limit, public well-known target), and a `cost` block with provenance
   (`value`, `currency`, `type`, `source`, `source_url`, `checked`, `confidence`). Follow the
   schema in the worked example.

## Map the FULL surface before selecting

List every documented operation your API exposes, then choose the 8–15 you catalog. The PR
description must carry that map as a short "catalogued / excluded, because…" list — reviewers
check curation against it, and "we didn't know it existed" is the gap this prevents. Two rules of
thumb from listings that went well: include the **free count / preview / pre-flight routes** that
let an agent size a query before paying for rows, and include your **cheapest tier** of an
operation, not only the default one.

## Self-verify with your OWN key before opening the PR

Docs drift; meters don't. Before the PR goes up, run **every** `test_request` live against your
own account and reconcile each `cost` block against what the meter actually charged (a per-call
charge field in the response, a rate-card endpoint, or the balance delta). A price transcribed
from a docs page has been wrong by 5× in a real submission — the live meter is the authority, and
a mismatch you find now is a one-line fix instead of a review round-trip.

While you're there:
- Quote the probe's bad-key behavior **from the wire** (run it with a garbage key; record the
  exact status and body, with the date) — not from memory or docs.
- If a `test_request` is a **deliberate miss** (a target chosen so a pay-on-success endpoint
  charges nothing), label it as such in the endpoint note — and observe the HIT price once on a
  real target so the cost block's number is metered, not assumed.

```bash
uv run --frozen python scripts/catalog_validate.py   # must exit 0
uv run --frozen python -m pytest -q                  # must pass
```

Rebase on the latest `main` right before opening — catalog files move fast and a stale branch
conflicts in the shared test lists.

Do NOT stamp `verified:` on any endpoint and do NOT commit example responses — those marks mean
"the maintainers watched it", and they run their own verification with an independent credential
regardless of your evidence. And **never put a credential value anywhere in the diff**: no API
keys in the YAML, the tests, the PR description, or commit messages.

## The PR description — required

- **A contact email** for your team, stated plainly (e.g. `Contact: partnerships@yourapi.com`).
  The maintainers reach out on this address to arrange a test credential for live verification —
  a PR without it cannot be verified or merged.
- **The self-verification ledger**: one line per endpoint — HTTP status of your live
  `test_request`, the cost the YAML claims, and the cost the meter reported, with the date. This
  is review evidence, not a substitute for the maintainers' own run — but a PR that arrives with
  it merges much faster, and a PR whose ledger disagrees with its own YAML will be bounced.
- **The full-surface map** — every documented operation, marked catalogued or excluded-because.
- Your probe endpoint's exact bad-key behavior (status + body, observed on a dated live call).
- Your pricing page URL and billing model; a machine-readable rate-card endpoint if you have one
  (that is the fastest path to treg serving your endpoints on its own key, metered).
- Whether you publish an OpenAPI spec at a stable URL (enables a machine-generated "extended
  tier" listing of your full endpoint surface).

## What happens next

Maintainers run the live checks: a garbage key POSTed at your probe must come back rejected, and
every endpoint's `test_request` is called with the test credential you arrange over email.
Endpoints that pass get `verified:` stamps and captured example responses; then the PR merges and
your API is discoverable by every agent using the catalog — compared side by side with other
providers on price, measured success rate, and speed.
