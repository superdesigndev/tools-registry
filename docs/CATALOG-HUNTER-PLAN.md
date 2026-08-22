# The catalog hunter — a weekly agent loop that finds, triages and drafts new providers

**Status:** planned, awaiting review. Nothing built.

## What it is

A scheduled agent run, weekly, that keeps the catalog growing without a human doing the hunting:

1. **Collect** candidates from three sources
2. **Triage** them against the selection rules we already wrote down
3. **Draft** a full dossier per surviving candidate
4. **Report** to Unclecode for approval
5. On approval, **implement** everything up to the one step that needs a human

The playbook already exists: `docs/context/guides/expanding-a-category.md` was written as a
repeatable process with selection heuristics and recorded rejection reasons. The loop is that guide,
executed on a schedule, with a human gate where the guide itself demands one.

## The three inputs

- **The hunt.** Web research per category, including the new market-data one
  (`docs/MARKET-DATA-CATEGORY-RESEARCH.md` is the standing shortlist to draw from). Also: gaps the
  usage data shows — a capability teams keep searching for and not finding is a stronger signal than
  any listicle.
- **Community issues** on the repo asking for providers or categories (the market-data request came
  in exactly this way).
- **Provider PRs.** Right now there are SEVEN open external submissions (#92 risk-data, #94
  Parallel, #95 Linkup, #96 LemMeBuyIt, #97 Goldsky, #119 Virlo). These are the highest-value input
  and the least served today: a contributor did the drafting, and nobody has systematically checked
  their work against our rules.

## Triage: the rules are already written

From the guide, applied mechanically before any human sees the list:

- self-serve API key, no sales call (this is the entire fast path)
- distinct value against what the category already has, not a near-duplicate
- reject with a recorded reason: legal risk, deprecated, UI-only, enterprise-gated
- when N clean picks do not exist, say so rather than pad with a bad fit

Plus the new rung the pricing ladder added: a flat-fee provider is no longer auto-rejected — the
dossier states which rung it prices on and, for shared-plan candidates, the proposed rate with fee
and break-even.

## The dossier (one per candidate, the approval artifact)

Exactly the facts the guide says matter, with "unconfirmed" allowed and guessing forbidden:

    provider, category, what gap it fills
    pricing rung: credits / cap / rate-limit / unlimited — with the numbers and source URL
    base_url, auth location + format
    a cheap or FREE probe endpoint, and the documented bad-key behaviour
    ToS flags: redistribution, non-commercial free tier, data-licensing (market data especially)
    for provider PRs: does the submitted YAML match reality, and what the diff review found
    verdict: recommend / reject (with the recorded reason)

## The human seam, stated plainly

**The live bogus-key test cannot be automated away.** It needs a real signup and a real key, and the
guide is explicit: never ship a key provider you have not watched reject a bogus key. The loop
therefore ends its automated run at "dossier ready, tested except keys", and the approval email
lists exactly which signups are needed. After Unclecode (or Jason) supplies keys, the
implementation half runs: registry entry, logo placeholder, tests, the live bogus-key check, docs.

The second human gate is approval itself: nothing is added, and no PR is opened, without Unclecode
seeing the dossier list. The loop drafts; it does not ship.

## Mechanics

- **Schedule:** weekly, as a scheduled agent session in this repo. The run's output is one markdown
  report (dossiers + rejections + the keys-needed list) committed to a branch and linked in the
  notification, so review happens in VS Code like everything else.
- **State:** a small `docs/hunter/seen.yaml` of already-evaluated candidates with their verdicts, so
  a rejected provider is not re-litigated weekly and a "not yet, thin value" can be revisited when
  something changes. The recorded-rejection habit the guide started becomes the loop's memory.
- **Provider PRs:** the loop comments its dossier findings on the PR itself (facts, not decisions),
  which is also the polite thing for the contributor waiting on #92–#119.
- **Cost:** the hunt is web research plus catalog reads; the only spend is probe calls where a free
  tier exists. No platform-key spending without approval, ever.

## Order

1. The dossier format + `seen.yaml`, and one manual run END TO END on the seven open PRs — the
   highest-value pile, and the run that proves the format before any scheduling exists. Reviewed
   like any work.
2. The hunt half (web research per category), second manual run.
3. Only then the schedule, once two manual runs have shown the report is worth waking up to.
4. The implementation half (post-approval automation) last — it is the guide's steps 3–7, and they
   are already well-specified.

Deliberately: scheduling is step 3, not step 1. An unattended loop earns its cron slot by being
useful twice while watched.

## What could go wrong

- **Dossier rot.** A provider verified in week 1 and added in week 6 may have changed pricing. The
  dossier carries its `checked` dates; the implementation half re-verifies pricing before writing
  fx entries rather than trusting a stale dossier.
- **PR flattery.** A contributor's YAML can look complete and be wrong (the b2b-ads-search stamp
  this week came from OUR OWN verified work, and it was still incomplete). The dossier for a PR is
  built from the provider's docs independently, then diffed against the submission — never the
  submission taken as the source.
- **Category padding.** The guide's rule stands: when clean picks run out, the report says so. A
  weekly loop with a quota would invent providers to meet it; this one has no quota.
