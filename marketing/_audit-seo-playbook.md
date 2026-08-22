# Risk audit of the programmatic pages — `/seo-playbook`, 2026-08-21

Run against the kill list and extraction rules in `.claude/skills/seo-playbook` (Lily Ray on what
Google punishes, Kevin Indig on how LLMs select, Fishkin/Natividad on the zero-click response).
Subject: 4 agent pages, 7 use-case pages, 1 hub, plus the 66-job menu they are the first slice of.

## Kill list: no hits, and two near-misses worth naming

| # | Tactic | Verdict |
|---|---|---|
| 1 | Self-promotional "best X" ranking our own brand first | **Clear.** The comparison ranks *providers*; treg.to is the access layer and is not one of the ranked items. This also sidesteps Ray's law 5 (69% of self-promotional listicle citations ended with the citing brand excluded from the recommendation) |
| 5 | Comparison / "X alternatives" at scale | **Near-miss, structurally avoided.** The banned shape is one page per competitor pairing. Ours is one page per *job* with every provider on it, so nine providers produce one page, not 36 pairings. Keep it that way |
| 6 | FAQ farms | **Clear.** Four questions inside a page, never one question per URL |
| 3 / 7 | Templated scaling with minimal uniqueness | **Near-miss, and the real risk for waves 2 and 3.** 12 pages with hand-written notes and quoted research is fine; 66 generated from the same shell would be the fingerprint Ray describes. The `treg-page` skill exists to keep the per-page research mandatory |
| 4 | Artificial freshness | **Clear.** Dates come from the catalog's own verification stamp, so nothing bumps without a real re-check |
| 10 | Schema misuse | **Was a hit, now fixed.** `SoftwareApplication` carried `Offer price: "0"`, which reads as "the product is free" while the page says installing is free and calls are metered. The offer now says exactly that |
| 8, 9, 11 | Hidden instructions, bought consensus, one page per query | **Clear** |

## Extraction rules (Indig)

- **Answer in the first 30%** — passes. The provider count, the from-price, the $0.000 markup and the
  named cheapest provider all appear before the 30% mark; the economics block sits above the fold on
  desktop.
- **15+ unique data points** — passes comfortably (per-provider price, billing unit, accepted inputs,
  verified date, success rate and latency where measured). The bar Indig cites is that the top ten
  average only about four.
- **Methodology boxed** — **was missing, now added.** Every comparison page carries a four-box block:
  how prices are derived, what the success rate counts (2xx vs 5xx, 4xx excluded), what it is not
  (not a controlled benchmark), and what "verified" means.
- **Visible date** — **was missing, now added** to the hero subline ("last checked 2026-08-20").
- **Named author** — **still missing.** Indig's data says named authors outperform brand bylines.
  This needs a person's name and is Jason's call, not something to invent.
- **Focused beats ultimate guide, but cover the intents inside the page** — passes: one job per page,
  with the prompt, the comparison, the caveats and the FAQ all inside it.

## Where the pages are weakest, in order

1. **No off-site presence.** Nothing in this repo fixes it. Indig and Ray converge here: authority is
   per-topic, three placements in sources the models already cite beat a dozen scattered mentions, and
   nofollow counts for AI mentions. The directory submissions in `pseo-ship-plan.md` (mcpservers.org,
   PulseMCP, Glama, Smithery, Docker MCP, Anthropic's connector directory) are the highest-leverage
   remaining work, and they are Jason's to send.
2. **No AI-visibility measurement.** Indig's method is polling, not rank tracking: ~40 seed prompts,
   five runs per platform per week, per-platform scores with confidence intervals, never one blended
   score. Nothing like this exists yet. Ray's caveat applies: treat it as directional.
3. **Scaled-content risk lives in waves 2 and 3, not in what shipped.** The guard is the research
   step, and it is only a guard while someone actually runs it.

## Kept deliberately, against a rule

- **Traffic is not the KPI.** Fishkin's decoupling argument and the wiki agree: the verdict metric is
  repeated successful calls per landing page, which still needs `first_call_at` attribution.
- **Comparison pages at all.** Ray's caution is about scale and self-promotion. These are neither, and
  the first-party price and reliability data is the thing no competitor can regenerate.
