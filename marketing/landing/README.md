# treg.to outcome landing pages

Five vertical landing pages that serve two jobs at once: **Google Ads destinations** (the primary job —
they exist to find out which vertical is worth spending on) and **organic `/use-cases/` pages** (the
secondary job).

```
marketing/landing/
  README.md          you are here — the rules, and how to change things safely
  _facts.md          every number used on any page, with source + date. ONE edit point.
  _shared.md         blocks that are identical across all five pages
  _measurement.md    the funnel, the per-page hypotheses, what must be live before spend
  01-seo-keyword-intelligence.md
  02-lead-enrichment.md
  03-social-creator-trends.md
  04-competitor-advertising.md
  05-company-buying-signals.md
```

---

## How this is built to survive SEO changes

The pages are **data + template**, not five hand-written documents. Four separations do the work:

**1. Numbers live in one file.** Every figure on every page comes from `_facts.md`, which records the
value, where it was verified, and when. Each page ends with a *Numbers used on this page* table naming
its fact keys. Catalog prices move; re-verifying is then one script and one file, and `grep` tells you
which pages are stale. **No page may introduce a number that is not in `_facts.md`.**

**2. Shared copy lives in one file.** The CTAs, the trust line, the free-credit statement and six of the
seven objection answers are identical across all five pages and live in `_shared.md`. A product change —
the router shipping, the grant amount changing, a new agent being supported — is one edit that
propagates. Pages only carry the vertical-specific override.

**3. Search terms and ad keywords are separate lists.** Front-matter carries `seo_terms` and
`ad_keywords` as different fields, because they answer to different constraints. Google Ads happily
takes `semrush api alternative`; organic must not target it (see the rules below). An SEO strategy
change edits `seo_terms`. An ads-policy or CPC change edits `ad_keywords`. Neither touches the copy.

**4. Front-matter is machine-readable.** Slug, title, meta, terms, capability ids, hypothesis and the
`verify_after` date are structured. These files can be compiled into HTML, Next.js or Sanity later
without anyone rewriting a sentence.

### Building the hand-off copy

The numbered source files carry `S-XXX` references instead of the shared copy, so a page reads as a
skeleton until it is built. To produce standalone pages a designer or CMS can take:

```bash
python3 build.py           # → dist/*.md, shared blocks expanded, editor annotations stripped
python3 build.py --check   # verify only; non-zero exit on an unresolved ref or an empty proof field
```

`dist/` is generated and overwritten on every run — **never edit it**. Edit the numbered source files or
`_shared.md`, then rebuild. `--check` is the pre-publish gate: it fails if any `S-` ref is unresolved or
any `[ TO BE POPULATED ]` proof field is still empty.

The build also strips `F-NN` fact keys, which are provenance for editors and not reader-facing.

### Building the live pages

```bash
python3 build.py && python3 build_html.py    # → src/treg/web/usecase-*.html
```

Served at `/use-cases/<slug>` off the `_USE_CASES` map in `src/treg/api.py`, with `usecase.css` as the
shared skin — the same split the legal pages use. The skin's tokens are lifted verbatim from
`landing.html` so an ad click lands on something recognisably the same product.

**The generated HTML is never hand-edited** — `build_html.py` overwrites it. Two safety properties worth
knowing before you touch that script:

- It **cuts the document at the ad-kit heading** and refuses to build if that heading is missing. Bid
  keywords, negative keywords and our conversion hypotheses cannot reach a public page by accident.
- An unknown slug **404s** rather than falling through to the SPA, so a typo in a live ad is visible.

Requires `pip install markdown` — a build-time dependency only. It is never imported by the shipped
package, so it does not touch the light-CLI install weight.

### When something changes, edit here

| What changed | Edit |
|---|---|
| A catalog price, a provider count, the free grant | `_facts.md` only |
| An objection answer, a CTA, the trust line | `_shared.md` only |
| Which terms we optimize for | `seo_terms` in front-matter |
| Which terms we bid on | `ad_keywords` in front-matter |
| The product itself (e.g. a router ships) | `_shared.md`, then grep pages for the claim |
| A page's verdict is due | `_measurement.md` + that page's `verify_after` |

---

## The rules these pages obey

From `wiki/topics/treg.to SEO.md` and the `/seo-growth` + `/seo-playbook` method. Each cost something
to learn; do not quietly drop one.

**Always write `treg.to`, never bare `treg`, in every title, meta description, heading and backlink
anchor.** `treg` is regulatory T cells — an immunology term that won a Nobel Prize, with Wikipedia, NIH
and Nature owning the SERP. The bare word is not winnable and never will be. The name must be spoken and
written as a URL.

**Job, not persona, in the URL.** Nobody searches "tool platform for media buyers." The slug namespace is
`/use-cases/<job>/`, kept clear of `/tools/<capability>/` and `/compare/<capability>/`, which are reserved
for the catalog pages in the site plan. These five pages will not have to move when those ship.

**Never target `<vendor> mcp` or `<vendor> alternative` organically.** The vendor is the primary source
for its own name and always wins it — six of the top ten for `semrush mcp` are Semrush's own domains. And
the alternative family is in decline across the board (`semrush alternative` -73%, `clearbit alternative`
-48%). These terms are fine to *bid* on. They are a waste of an organic page.

**No "best X" or "X vs Y" listicle shape.** Highest competition, lowest CTR, and the shape can exclude
you from AI recommendations — 69% of self-promotional listicle citations ended with the citing brand
left out of the recommendation. These pages are workflow pages that name real prices, not rankings.

**Do crown a cheapest answer where one exists, with caveats.** This reverses an earlier draft of the site
plan. treg.to does **not** route or fail over — `llms.txt` says so explicitly — so "picking is the problem
and we remove it" contradicts the product. The honest frame is: *choose well, then switch freely without
opening a new account*, with price and measured success visible before the call.

**The CTA test is a hard gate.** What does the reader DO after this page? Here: copy the prompt, run the
call. If a future edit leaves the honest answer as "close the tab," the edit is wrong.

**No page per endpoint.** 2,600+ tools as 2,600+ thin pages is the most obvious idea available and the
one most likely to earn a site-wide penalty. Five pages carrying real measured data is the opposite shape.

**Uniqueness is the measured-comparison table.** Each page carries a provider comparison with real cost,
success rate, sample size and median latency pulled from the live catalog. That table exists nowhere else
on the internet and cannot be regenerated from a prompt — reproducing it requires holding 42 provider
accounts. It is the reason five structurally similar pages are not a template farm.

---

## Before any of this earns a verdict

`_measurement.md` has the detail. The short version: the wiki's standing position is **no paid spend for
treg.to yet**, because install → auth → first call → week-two reuse is not instrumented, and spending
against an uninstrumented funnel buys a false negative on the only question worth asking.

The user's goal here — *find out which vertical deserves the effort* — is exactly what paid traffic is
good at, so the answer is not "don't run ads." It is **instrument first-call attribution before or
alongside the first dollar**, because otherwise the experiment returns account signups, and account
signups are not the conversion these pages exist to produce.

---

## Status

Copy is written and every number is verified against treg.to and the live catalog as of **2026-08-17**.

Scale is stated as **2,600+ tools across 40+ providers** (`F-01`) — rounded down deliberately, and using
"tools" rather than "endpoints" because that is the word in `CLAUDE.md` and the one a visitor understands.

**Proof sections are populated from a real run** on 2026-08-17 — 14 calls, $0.0489 total. The full audit
is in `_run-log.md`, including the five calls that failed and cost nothing.

**Not yet done, and blocking publication:**
- **p5 must not claim hiring or activity signals.** Funding is proven; the signals endpoint is blocked by a
  platform bug — see `_platform-bugs-2026-08-17.md`, item 1.
- `/catalog/platforms` reports fewer tools than the other two surfaces (see the note under `F-01`). Worth
  resolving before the number goes into a Google Ads asset.

Pages 1–4 are proof-complete and ready to build.
