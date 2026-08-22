---
title: Expanding a marketplace category — the add-a-provider playbook
status: guide
sources:
  - src/treg/oauth_providers.py
  - src/treg/api.py
  - src/treg/config.py
related:
  - architecture/auth-secrets.md
  - interface/api.md
---

# Expanding a marketplace category (adding providers)

How we grew **SEO**, **Enrichment** and **Advertising** from a handful of entries to ten each — and then
Enrichment again by eight providers in one pass (2026-08-20: companyenrich, oceanio, tomba, predictleads,
findymail, branddev, icypeas, leadsforge). This is the repeatable process — follow it whenever a
category needs more providers. Creator/influencer data (influencers.club, 2026-08-21) was the first
provider added under the vendor-listing skill end to end: registry + 15-endpoint catalog, every price
reconciled against the provider's own credit meter.

Everything lives in **`oauth_providers.py`** (the `REGISTRY` of `OAuthProvider` entries). Connecting,
verifying and auto-provisioning a pasted-key provider is **`connect_with_token`** (`POST /connections/token`)
in `api.py`. Both are documented in [auth-secrets](../architecture/auth-secrets.md) + [api](../interface/api.md);
this fragment is the *process*, not the mechanics reference.

## The two kinds of provider
- **API-key** (`auth_kind="key"`) — the user pastes a key; self-serve; **the fast path** (research → implement
  → live-test in one session). This is the workhorse and where almost all growth happens.
- **OAuth** (`auth_kind="oauth"`) — treg holds its own registered app; **heavy** (needs a dev-app registration
  + client credentials). Add as **unconfigured** entries (they list `configured:false` until this deployment
  sets the credentials).
- **token** (Slack) — bring-your-own bot token; shares the pasted-secret path via `uses_pasted_secret`.

## The fast path — adding an API-key provider

1. **Curate.** Pick providers that fill a *gap* vs what the category already has (not near-duplicates). Use
   the selection heuristics below. A research subagent is the right tool: give it the pool + criteria and have
   it recommend + reject with reasons.
2. **Get EXACT specs** per provider — `base_url`, auth (location / name / format), a **cheap or FREE probe
   endpoint** (a valid key returns 200, an invalid key does NOT), and the **bad-key behavior** (status, or the
   JSON field that signals invalid). Accuracy of `base_url` + probe path + bad-key behavior matters most,
   because you validate them **live**. Have the research agent say "unconfirmed" rather than guess a path.
3. **Add the registry entry** — an `OAuthProvider(auth_kind="key", …)` in `oauth_providers.py`; add it to the
   `REGISTRY` tuple; add the category to `CATEGORY_ORDER` if it's new. Key providers have `scopes={}` (no
   consent screen); the marketplace card leans on `summary`.
4. **Placeholder logo** — `web/logos/<service>.svg` (a lettermark; swap for the official brand SVG later). The
   guard test `test_every_provider_has_a_logo` fails without one. Do NOT reproduce a real brand mark — a neutral
   lettermark is the placeholder.
5. **Tests** — add the id to `test_every_provider_is_registered` (test_oauth_providers_m3) and the offerable
   loop in test_key_providers.
6. **LIVE bogus-key test — the load-bearing step.** Start the server and `POST /connections/token` with a
   *garbage* key against the **real** API:
   - `422` "rejected …" → correct (bad key rejected). Ship it.
   - `200` → the probe does **not** validate the key → fix the verify (see the toolbox), or drop the provider.
   - `404`/`502` in the reason → wrong probe path / host → fix `base_url`/`probe_path`.
   This one step caught Apollo (`is_logged_in`), Akta (trailing slash), Majestic (`Code`), and ScrapeCreators
   (accepts any key). **Never ship a key provider you haven't watched reject a bogus key.** Run in a throwaway
   org and delete it after (test users are `e2e-…@treg.local`).
7. **Run the suite, [sync docs](../../../.claude/skills/tools-registry-context/MAINTAINING.md), commit + push.**

## The verify toolbox — which `OAuthProvider` field for which bad-key behavior

| Provider behavior | Field(s) to set | Real example |
|---|---|---|
| Key in a header | `token_header` + `token_format` (`Bearer {secret}` / `{secret}` / `Token {secret}`) | TikHub, Apollo, SE Ranking |
| Key in the query string | `token_location="query"` + `token_param` | Semrush, Diffbot, SpyFu |
| HTTP Basic from `login:password` | `token_format="Basic {secret}"` + `token_encode="base64"` | DataForSEO, Moz |
| HTTP Basic with a RAW token after `Basic ` | `token_format="Basic {secret}"`, **no** `token_encode` | The Companies API |
| Cheapest check on a DIFFERENT host | `probe_url` (absolute) | Semrush (balance host), Diffbot (account host) |
| Probe needs a POST body | `probe_method="POST"` + `probe_json` | Serpstat, Moz, Coresignal |
| 200 on a bad key; a truthy field = valid | `token_verify_field` | Slack `ok`, Apollo `is_logged_in` |
| 200 on a bad key; a field == a value = valid | `token_ok_field` + `token_ok_value` | Majestic `Code=="OK"` |
| 200 on a bad key; an error object present = invalid | `token_reject_field` | Serpstat `error` |
| 200 on a bad key; an `ERROR …` text body | handled automatically (text-error guard) | Semrush |
| No free probe; valid key 400s on empty body, invalid 401s | `probe_reject_statuses=(401,403)` | Coresignal |
| The provider's OWN "test my auth" endpoint answers 200 with prose for a bad key | probe a DATA endpoint instead | Tiingo `/api/test` (2026-08-14; `/tiingo/daily/aapl` 403s cleanly) |
| Bad key 302-redirects to an HTML login page (a Laravel app that only speaks JSON when asked) | `probe_reject_statuses=(302, 401, 403)` **plus** `token_verify_field` on a body field the redirect lacks | Findymail `/credits` → `email` (2026-08-20) |
| TWO header credentials, both per-user, and a free probe answers the key alone | `extra_credential_label`/`extra_credential_header` with `extra_credential_setting` EMPTY (user binds the second half via `POST /connections/{id}/extra-credential`); probe the key-only route | Tomba `X-Tomba-Key` + `X-Tomba-Secret`, probe `/v1/usage` (2026-08-20) |
| TWO credentials but the API also takes standard HTTP Basic `a:b` | one pasted `key:token` pair, `token_format="Basic {secret}"` + `token_encode="base64"` — no second slot needed | PredictLeads `api_key:api_token` (2026-08-20) |
| A second host that answers 200 to anything (demo/free tier) | pin the host that rejects | CoinGecko demo host (2026-08-14) |
| Accepts ANY key on every endpoint, even premium ones | DROP — cannot ship | Alpha Vantage (2026-08-14: `apikey=bogus123` returned real quote data) |
| Ongoing tool health check | `probe_path` (a cheap GET on `base_url`) | most |

The connect verify already parses JSON only when the response *is* JSON (so a CSV/text body doesn't error), and
rejects on HTTP status by default.

## Common traps (RCAs from real providers)
- **200 for a bad key.** The status lies; read a body field (`token_verify_field` / `token_ok_field`). Apollo,
  Majestic.
- **Trailing-slash 307 redirect.** We don't follow redirects with the key attached, so we only see the 307. Put
  the trailing slash **in `probe_path`** so it hits the clean 401. Akta.
- **CSV / text response.** Don't JSON-parse; the verify guards on content-type. Semrush.
- **Key in the URL PATH** (`/v3/{key}/…`). Our injectors only do header/query — **not supported**. Skip the
  provider (or add a path injector). Adbeat.
- **No free probe.** POST an empty body; a valid key 400s, an invalid 401s → `probe_reject_statuses=(401,403)`.
  Coresignal.
- **API accepts ANY key** (returns success for garbage). You cannot validate at connect — **drop it**; don't
  ship a provider whose key can't be checked. ScrapeCreators.
- **Cheapest check is off-host** → `probe_url`. Semrush, Diffbot.
- **A gateway 504 that still bills.** influencers.club (2026-08-21) fronts slow enrichment with an nginx
  60s cutoff: the first call for a handle took 54–61s, several came back as a 504 HTML page, and the
  backend finished anyway — two of those 504s were charged, one was not. Record it in the catalog
  file's header (retry once, warm answers in 2–5s) rather than stamping the route `unverified`; and
  give `catalog_verify.py` a second pass for the routes that timed out instead of widening its timeout.
- **Django trailing slash.** influencers.club's slash-less paths 301 with the body dropped; the Akta
  fix (slash IN `probe_path`) applies to every catalog `path` too.
- **A cross-platform provider needs its own platform slug.** influencers.club enriches a handle on any
  of 11 networks through a `platform` body field, so no single social slug is honest; it got a
  `creators` platform on the Enrichment shelf (2026-08-21). Platforms cannot be proposed from a
  provider file — add the slug to `capabilities.yaml` and propose only the capabilities.

## Selection heuristics (what makes a provider worth adding)
- **Self-serve API-key first** — sign up, get a key, no sales call. That is the entire speed advantage; anything
  sales-gated breaks the fast path.
- **Distinct value** — fill a gap the category lacks (phones, bulk data, knowledge-graph, tech-stack, GDPR
  email…). Skip near-duplicates.
- **Reliability / legal.** Reject, decisively and *with a recorded reason*: legal/shutdown risk (Proxycurl —
  shut down by LinkedIn's suit, Jul 2025), deprecated/absorbed (Clearbit → HubSpot-gated), UI-only with no API
  (BigSpy/AdSpy), enterprise-sales-gated (SimilarWeb, SensorTower, MediaRadar).
- **The self-serve field is often THIN** (Ads especially — only ~4 real self-serve ad-intel APIs). When N clean
  picks don't exist, **say so** and fill the count with an adjacent-but-clean pick (SerpApi for ads) or an
  unconfigured OAuth platform — don't force a bad-fit provider.

## The heavy path — adding an OAuth provider
1. treg must register its **own** dev app on the network → `client_id`/`client_secret` → add the two settings
   to `config.py` (`Settings`) so they load from env; the registry entry names them via
   `client_id_setting`/`client_secret_setting`.
2. Add the `OAuthProvider` with `auth_uri`/`token_uri`/`scopes`/`base_url`/`token_endpoint_auth_method`, a
   **`SCOPE_LABELS` entry for every scope** (guarded by `test_every_requested_scope_has_a_label`), and a logo.
   `write` must be a superset of `read` (`test_default_capability_is_the_broadest`) — or use a single capability.
3. Unset credentials → it lists `configured:false`, so it is **safe to add UNCONFIGURED** to reach a target
   count; it activates when the credentials are set.
4. **Quirks** (all snapshotted onto `PendingOAuth`): `extra_credential_*` for a second header (Google Ads /
   Microsoft Ads `DeveloperToken`), `auth_params={}` for non-Google providers that reject `access_type`,
   `client_id_param` (TikTok's `app_id`/`client_key`), `scope_separator`, `pkce`, `long_lived_exchange`,
   `token_endpoint_auth_method="client_secret_basic"` (X, Pinterest — ALSO persisted into the token blob
   so refresh speaks the same dialect), `extra_tools` for a vendor that splits one product across hosts
   (GA4's admin/data split — each extra host provisions a companion Tool on the same secret; the
   generic `_backfill_provider_extra_tools` startup pass gives existing connections newly-added
   companions automatically), and
   `resource_example` to stamp a ready-made call onto the tool once the user picks their resource.
5. **NON-STANDARD OAuth is not free.** TikTok Ads (`app_id`/`auth_code`, JSON-body token exchange, `code==0`
   envelope, `Access-Token` header instead of `Authorization: Bearer`) does NOT work with the standard
   `oauth.py` flow or the Bearer auto-provision binding. Add it as a **flagged placeholder** — it needs real
   `oauth.py` + binding work before it can run.

## The tests that gate every new provider
- `test_every_provider_is_registered` — add the id.
- `test_every_provider_has_a_logo` — add `web/logos/<id>.svg`.
- `test_default_capability_is_the_broadest` — OAuth: `write ⊇ read` (or one capability).
- `test_every_requested_scope_has_a_label` — a `SCOPE_LABELS` entry per OAuth scope (key providers have none).
- `test_key_providers` — the offerable + connect-flow coverage for `auth_kind="key"`.
