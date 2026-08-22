---
name: treg-page
description: Write a treg.to agent page (/agents/<client>) or use-case page (/use-cases/<category>/<job>). Researches the real problem on Reddit and X with agent-reach BEFORE writing, so the page targets the words buyers actually use and quotes their own questions. Use when adding a page from marketing/pseo-ship-plan.md, or when asked to "write the <job> page" / "add the <agent> page".
argument-hint: "[use-case <category>/<job> | agent <slug>]"
---

# treg-page — research first, then fill in the spec

Both page types are **templates fed by one dict entry**. No HTML is written by hand: the route in
`api.py` renders titles, prices, provider rows, reliability and schema from `catalog_store` at
request time. Your job is the copy, and the copy is only as good as the research behind it.

Read `marketing/pseo-ship-plan.md` for what to write next and `docs/context/interface/seo.md` for
how the pages work. Never invent a number: if it is not in the catalog, it does not go on the page.

## The order matters. Do not skip step 1.

### 1. Find the words buyers use (measure, never guess)

The catalog's vocabulary is not the buyer's. "People enrichment" is our word; "email finder" is
theirs. Measure before titling anything:

```bash
uv run --frozen treg call google-ads "v22/customers/2277522568:generateKeywordHistoricalMetrics" \
  --method POST --data '{"keywords":["email finder","email finder api","linkedin email finder"],
  "geoTargetConstants":["geoTargetConstants/2840"],"language":"languageConstants/1000",
  "keywordPlanNetwork":"GOOGLE_SEARCH"}'
```

Google Ads is an **own-key tool, so this costs nothing**. Use the tool-scoped form (`treg call
google-ads <path>`); the bare endpoint id is ambiguous across three registered accounts. Try 12 to 20
phrasings: the job in our words, the job in the buyer's words, `<thing> api`, `<thing> pricing`,
`<platform> scraper`, and the agent-prefixed forms. Keep whatever clears ~100/month; a term with 0
volume must not become an H1.

Known from earlier passes: buyers type **scraper**, **`<platform> api pricing`** and **`<x> mcp
server`**. They do not type "api alternative", and they do not type "how to <job> with chatgpt"
(measured 0 to 20/month across 30+ phrasings). Do not target those.

### 2. Find the real difficulty (agent-reach on Reddit and X)

This is what makes the page worth linking to. Use the `agent-reach` skill; both platforms go through
OpenCLI (twitter-cli's search endpoint 404s regularly):

```bash
agent-reach doctor --json          # confirm reddit + twitter backends
mkdir -p /tmp/ar-<job> && cd /tmp/ar-<job>
for q in "<job in buyer words>" "<job> accuracy" "<job> not working" "<provider a> vs <provider b>" \
         "how do you <job>" "<job> cost at scale"; do
  opencli reddit  search "$q" -f yaml > "r-$(echo $q | tr ' /' '__').yaml"
  opencli twitter search "$q" -f yaml > "x-$(echo $q | tr ' /' '__').yaml"
done
```

Six to eight queries gives ~180 posts. **Delegate the reading to a subagent** so the raw dump never
enters the main context; ask it for:

- the 8 to 12 most quotable questions or complaints, **verbatim and under 25 words**, each with
  platform, score and URL;
- the distinct pain themes with a post count each;
- for each theme, what a page can honestly say (and "no good answer" where it cannot);
- any post where someone tried to do this with ChatGPT, Claude or an agent.

**Filter vendor astroturf.** Posts that arc from "burning credits" to "my boss is asking questions"
to a named tool, identical posts across subreddits, and posts in a vendor's own sub are marketing.
The last research pass found four such clusters. Exclude them; quote organic posts only.

Then map each theme to a `voices` entry: `(heading, verbatim quote, who, url, what this page can
honestly do about it)`. Where the honest answer is "no comparison table can tell you this", say so
and offer the cheap experiment instead. That candour is the page's credibility.

### 3. Check what the catalog can actually do

```bash
uv run --frozen treg catalog search "<the job in plain words>"
uv run --frozen treg catalog get <endpoint-id>          # params, cost, verified, siblings
```

Every capability id you name must exist (`tests/test_agent_pages.py` enforces it). **A job the
catalog cannot do does not get a page.** If the research surfaced a job we cannot serve, add it to
the gap list in `marketing/pseo-ship-plan.md` instead of writing around it.

### 4. Write the entry

**Use-case page** — add to `USE_CASE_PAGES` in `src/treg/agent_pages.py`, keyed by
`(category slug, job slug)`. The route picks the FORM from the data, so you do not:

| Form | When | What renders |
|---|---|---|
| `short` | one provider | prompt, why, "How it works", one call. No comparison |
| `platforms` | the job spans several platforms | providers grouped per platform; cheapest claimed per platform |
| `compare` | several providers, one platform | the full comparison |

Fields: `label` (must match its row in `USE_CASES` exactly), `sentence` (the H1: the buyer's term
from step 1, not ours), `title` (`{n}`, `{cheapest}`, `{agent}` interpolate), `lede`, `prompt` (one,
starting "Using treg, …"), `prompt_why` (4 × `(lead, one line)`), `result_noun`, `what_is_heading`,
`what_is`, `notes` (3, from provider docs and catalog notes, the part nobody can regenerate),
`voices` from step 2, `faq` (4), `related` (labels that exist on the menu).

**Agent page** — add to `AGENTS`. Everything but ChatGPT uses the universal install: paste
`set up treg — https://treg.to/llms.txt` into that client's chat, and hand it the team token
(header `X-Treg-Token`) only if it asks. That is what the dashboard's own onboarding shows, so the
steps must match it. Screenshot optional; ship without the slot rather than with a placeholder.

### 5. Verify

```bash
uv run --frozen python -m pytest -q tests/test_agent_pages.py
uv run --frozen python -m treg          # then open the page and READ it in a browser
```

Check by eye: the H1 carries a measured term · the first sentence of every section answers its
heading · prices and dates come from the catalog · no empty section · the quotes link out.

## House rules

- **No em-dashes** in page copy. A test fails on them. The setup line is the one exception.
- `treg.to`, never bare `treg`, in titles, H1s and anchors.
- Never claim treg.to chooses or fails over between providers. It compares; the agent picks, or the
  reader tells it how.
- Observed reliability stats may be published per vendor (Jason, 2026-08-21) with the "live traffic,
  not a controlled benchmark" caveat. The section hides itself when there is no traffic.
- Say the price is the provider's own rate with **$0.000 markup**, and mark own-account jobs FREE.
- Update `docs/context/interface/seo.md` in the same commit as the page.
