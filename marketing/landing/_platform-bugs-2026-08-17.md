# Message to send to the treg.to developer

Copy from the line below. Findings are from ~20 real calls on 2026-08-17 across two teams
(`superdesign` and `superdesign-7`) while building landing-page proof sections. Everything here was
reproduced at least twice unless noted.

---

Hey — I ran about 20 real calls through treg.to today (SEO, enrichment, social, ads, company data) and hit
six things worth fixing. Roughly in order of how much they cost me.

**1. `catalog_search` over MCP returns an endpoint id that doesn't exist.**

Searching `"hiring headcount"` via the MCP `catalog_search` tool returns:

```json
{"endpoint_id": "lusha.companies-signals", "usd_per_call": 0.1248, "no_key_needed": true}
```

But that id doesn't resolve:

```
$ treg catalog get lusha.companies-signals
no endpoint 'lusha.companies-signals' in the catalog
```

The real ids are `lusha.x.company-signal-types`, `lusha.x.company-signal-filters`,
`lusha.x.companies-website-visits`. This breaks the documented search → get → call flow at the first step,
and it does it *with a confident price attached*, so an agent will plan around a call it can never make.
This is the one I'd fix first. It blocked a whole workflow for me.

**2. `tikhub.x.tiktok-ads-search-ads` has the wrong method recorded, and can't be called at all.**

```
POST → treg says: "tikhub.x.tiktok-ads-search-ads is GET — add --method GET"
GET  → provider returns 405 Method Not Allowed
```

So treg enforces GET and the upstream rejects GET. Catalog metadata looks wrong. Its stats row also reads
`— (7)` for WORKS with `LAST OK: today`, which I can't interpret — 7 samples, no success rate, but it
answered today? If those 7 are all failures, the row should say so, because it currently reads as "fine,
just new."

`scrapecreators.x.v1-tiktok-ad-library-search` works perfectly for the same job at $0.00188.

**3. Boolean query params get serialized wrong over MCP.**

Calling `thecompaniesapi.companies.search` with `query: {"simplified": true}` returns:

```json
{"details": [{"message": "The value must be a boolean", "rule": "boolean", "field": "simplified"}]}
```

Looks like a Python `True` reaching the wire as `"True"` instead of `"true"`. Worth fixing generally, but
it stings on this endpoint specifically: `simplified=true` is the **free** mode. The bug silently pushes
callers onto the paid path for a query they could have previewed for nothing.

**4. `catalog_search` ranking doesn't break ties on price or measured reliability.**

`catalog_search "ad library"` with `limit=15` returned seven tikhub tiktok-ads endpoints — including the
broken one in #2 — and **omitted** `scrapecreators.x.v1-tiktok-ad-library-search`, which is $0.00188 with
100% success over 16 measured calls. Everything scored 6, so the cut was effectively arbitrary.

The default MCP limit is 8, so in practice an agent asking for ad-library tools gets shown unmeasured and
broken options and not the best-measured one. You already collect WORKS/SPEED/COST — using them as the
tiebreaker inside an equal relevance score would fix this and would make the "your agent picks on evidence"
story actually true at the search step, not just at `catalog_get`.

**5. The MCP OAuth grant is pinned to a team that `treg org ls` doesn't list, with no way to see or change it.**

MCP `balance` reported team `superdesign-7`. My CLI shows only:

```
* superdesign   superdesign   admin (active)
  ai-jason      AI Jason      admin
```

I spent ~$0.05 out of `superdesign-7` before anyone noticed, because from inside the agent there is no
signal that it's the wrong team — `balance` returns a slug I had no way to recognise as unexpected.

Two asks: (a) make `balance` / `my_tools` return something a human can sanity-check (team display name +
which identity the grant belongs to), and (b) give the agent or the user a way to switch the MCP grant's
team without re-doing OAuth. Right now the consent-screen choice is invisible and permanent, and the
failure mode is silent spend on the wrong balance.

**6. Endpoint and provider counts disagree across three surfaces.**

- treg.to hero: **2,617 endpoints / 42 providers**
- `llms.txt`: **~2,600 endpoints / ~48 providers**
- `GET /catalog/platforms`, summed: **2,363 endpoints / 47 providers / 80 platforms**

Not a functional bug, but I'm about to put a number in Google Ads copy and it has to be defensible on the
page it points at. Whichever is right, the other two should be computed from it. `/catalog/platforms` being
the low one is the awkward part, since that's the surface anyone auditing would hit.

---

**Two provider-side things, not treg bugs, but maybe worth a catalog note:**

- `leadmagic.x.company-funding` found nothing for `runpod.io` or `daloopa.com` (both free misses — the
  free-on-miss pricing is great, by the way) but returned a full history for `stripe.com`. Coverage seems
  thin on small recently-funded startups, which is the main use case people will bring to it. A coverage
  hint on the endpoint would save people the discovery.
- `scrapecreators.reddit.search.posts` on `"ai agents"` returned an unrelated r/lol post as a top result.
  Site-wide relevance is weak; subreddit-scoped search is fine.

**What worked really well, since it's all complaints above:** failed calls genuinely cost nothing — I had
five failures including a treg-side refusal that never reached the provider, and all were $0.00. Every
`cost_usd` matched what `catalog_get` quoted beforehand, exactly. And `catalog_get`'s sibling table with
COST/WORKS/SPEED/sample size is the best thing in the product — it's the reason I could pick providers at
all. Item 4 is really just "please use that data one step earlier."
