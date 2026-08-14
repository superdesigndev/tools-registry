#!/usr/bin/env python3
"""Validate the endpoint catalog (src/treg/catalog/*.yaml) — schema shape + referential integrity.

Run from the repo root: `uv run python scripts/catalog_validate.py [service ...]`
Exit 0 = valid. Every violation prints one line: `<file>: <problem>`.

Checks (the success criteria from docs/context/architecture/catalog.md):
  - provider file's `provider` matches its filename and exists in treg.oauth_providers.REGISTRY
  - endpoint ids unique across the WHOLE catalog; id convention `<provider>.<capability>`
  - `capability` exists in capabilities.yaml OR the file's own proposed_capabilities
  - `platform` equals the capability's first segment and exists in capabilities.yaml platforms
  - required fields present; enums valid (scope, method, cost.type/currency/unit/source/confidence)
  - a `cost` block is BILLABLE, not decorative: a null `value` and `confidence: unknown` appear
    together or not at all; a verified/documented price names its `source_url`; every priced entry
    carries `checked` (WARN past 90 days); free is spelled exactly one way
  - a `verified` endpoint must have an existing example_response file
  - an extended endpoint a verification run has touched claims exactly one non-empty state
    (verified | unverified | untestable | skipped), and an `untestable` one carries no
    test_request for a re-verify run to call it with anyway
  - no obvious credential leak (Authorization/token values) in any catalog file

Two tiers, two rule sets. `<provider>.yaml` holds hand-curated `tier: core` entries and every rule
above applies. `<provider>.extended.yaml` holds the machine-generated coverage tier written by
scripts/catalog_ingest.py: those entries need only id/platform/method/path/summary, and carry no
capability (nothing has been mapped to the taxonomy yet). Id-prefix, id-uniqueness, platform
integrity and the leak scan apply to BOTH — an extended entry that does declare a capability is
held to the core referential rules.

The evidence rule is deliberately tier-blind, and needed no loosening when the extended tier gained
generated `test_request`s and live verification (scripts/catalog_verify_extended.py): `verified`
means the same thing in both files, so in both it must be backed by an `example_response` file that
exists and a `test_request` to re-check it with. A tier decides how much is EXPECTED of an entry,
never what a claim on it is worth.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"
sys.path.insert(0, str(ROOT / "src"))
# The enums the SERVER reads the same files with. Imported, never re-typed: a validator that
# accepts a unit `cost_view` cannot price is worse than no validator.
from treg.catalog_store import COST_SOURCES as _SOURCES  # noqa: E402
from treg.catalog_store import COST_UNITS as _UNITS  # noqa: E402
from treg.catalog_store import CONFIDENCES as _CONFIDENCES  # noqa: E402

SCOPES = {"any_account", "own_account"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
COST_TYPES = {"per_call", "per_result", "per_success", "free", "quota_rows"}
# What the price is denominated in. `credit` and `unit` are PROVIDER-scoped meters, not currencies,
# and convert via fx.yaml's `credit_rates_usd` / `unit_rates_usd` (see catalog_store.cost_view).
CURRENCIES = {"USD", "CNY", "credit", "unit"}
COST_UNITS, CONFIDENCES, COST_SOURCES = set(_UNITS), set(_CONFIDENCES), set(_SOURCES)
# A price is a perishable fact. Older than this and CI says so — a warning, not an error: a stale
# price is still the best number we have, and failing the build over the calendar would only teach
# people to bump the date without re-checking.
STALE_DAYS = 90
# The one spelling of free. It was written three incompatible ways across 661 endpoints (`value: 0`,
# `value: 0.0`, and no value at all), which made "is this free?" a per-entry judgement call and left
# `cost.usd` null on a third of them — indistinguishable, downstream, from "price unknown".
CANONICAL_FREE = {"value": 0, "currency": "USD", "unit": "call"}
TIERS = {"core", "extended"}
# what an endpoint IS (marketplace browse surface vs. plumbing). Optional — absent reads as "data" —
# but a stated one must be from this set. See docs/context/architecture/catalog.md.
KINDS = {"data", "action", "account", "utility"}
# the section heading an endpoint files under on its platform page — one lowercase word
DOMAIN = re.compile(r"[a-z][a-z0-9_]*")
REQUIRED = {
    "core": ("id", "capability", "platform", "method", "path", "summary"),
    "extended": ("id", "platform", "method", "path", "summary"),
}
# The four outcomes an extended endpoint can have once a verification run has touched it. They are
# mutually exclusive by construction, and an entry that has been through the pipeline must claim
# exactly one — "no state" reads as "never attempted", which is a different fact and a lie once a
# run has been over it. This caught a real regression: re-running an endpoint overwrote its result
# record, dropped the reason string, and stamped an EMPTY state that nothing else noticed.
STATES = ("verified", "unverified", "untestable", "skipped")
# a long token-looking literal anywhere in a catalog file is a leak until proven otherwise
LEAK = re.compile(r"(Bearer\s+[A-Za-z0-9+/_=-]{16,}|[A-Za-z0-9+/]{40,}={0,2})")
# ...but URL and API paths are also long runs of [A-Za-z0-9/], and the extended tier is thousands
# of them. Two things separate them from a credential: a path is built of short slash-separated
# segments, and those segments spell words ("dataforseo", "kolContentTags"). A base64 secret hits
# a `/` only about once per 64 characters, so at least one of its segments stays long and wordless.
WORDY = re.compile(r"[a-z]{8,}")
# ...and a Solana public key (mint/wallet address) is a THIRD shape that pattern-matches as a
# secret under the same test a base64 credential does: 32-44 wordless alphanumeric characters, no
# slash to break it into shorter segments (risk-data-api.yaml, added with the Crypto/Safety
# category). It differs from an actual credential in the one way that matters: it is base58, not
# base64/hex — Bitcoin's base58 alphabet (which Solana also uses) drops 0/O/I/l specifically so an
# address can be read aloud and copied without the visual-ambiguity bugs that plague raw base64/hex
# secrets. That is also exactly why it is safe to except: a base58 pubkey is meant to be public by
# design (an address is what you SHARE to receive funds; the private key never appears in an API
# example), so excepting the shape does not risk waving through an actual leaked credential — those
# still contain 0/O/I/l often enough that requiring their absence stays a meaningful filter.
BASE58_PUBKEY = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def looks_like_secret(match: str) -> bool:
    if match.lower().startswith("bearer"):
        return True
    return any(
        len(seg) >= 24
        and not WORDY.search(seg)
        and any(c.isdigit() for c in seg)
        and not BASE58_PUBKEY.match(seg)
        for seg in match.split("/")
    )



def fail(errors: list[str], where: str, msg: str) -> None:
    errors.append(f"{where}: {msg}")


def _as_date(value) -> dt.date | None:
    """`checked: 2026-07-28` parses as a date; quoted, it stays a string. Accept both, reject prose."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def check_cost(cost: dict, where: str, errors: list[str], warnings: list[str]) -> None:
    """The price block's own rules — the ones that make a figure BILLABLE rather than decorative.

    A platform key spends treg's money on a caller's behalf, so every number here has to answer
    "how much, per what, says who, checked when". The rules below are what makes each of those four
    unskippable; `catalog_store.Catalog.platform_eligible` is what refuses the ones that fail.
    """
    if cost.get("type") not in COST_TYPES:
        fail(errors, where, f"cost.type missing or not one of {sorted(COST_TYPES)}")
    value, conf = cost.get("value"), cost.get("confidence")
    if conf is not None and conf not in CONFIDENCES:
        fail(errors, where, f"cost.confidence '{conf}' not one of {sorted(CONFIDENCES)}")
    if (src := cost.get("source")) is not None and src not in COST_SOURCES:
        fail(errors, where, f"cost.source '{src}' not one of {sorted(COST_SOURCES)}")
    if (unit := cost.get("unit")) is not None and unit not in COST_UNITS:
        fail(errors, where, f"cost.unit '{unit}' not one of {sorted(COST_UNITS)}")
    if (cur := cost.get("currency")) is not None and cur not in CURRENCIES:
        fail(errors, where, f"cost.currency '{cur}' not one of {sorted(CURRENCIES)}")
    per = cost.get("per")
    if per is not None and (not isinstance(per, int) or isinstance(per, bool) or per < 1):
        fail(errors, where, f"cost.per '{per}' must be a positive integer (the quantity `value` covers)")
    # `currency: unit` means "the provider's own meter" — which meter is the only thing that makes
    # the number convertible, and it is `unit` that names it.
    if cur == "unit" and unit not in COST_UNITS:
        fail(errors, where, "cost.currency 'unit' needs cost.unit to name the provider's meter "
                            "(fx.yaml unit_rates_usd is keyed by it)")

    if cost.get("type") == "free":
        wrong = {k: cost.get(k) for k, want in CANONICAL_FREE.items() if cost.get(k) != want}
        if wrong or (per not in (None, 1)):
            fail(errors, where, f"free must be spelled {CANONICAL_FREE} (got {wrong or {'per': per}})")
        return

    # An unknown price and a null figure are the SAME fact, so they must always be written together:
    # a null value with a confident-looking block is how a guess ships, and `confidence: unknown`
    # over a real number is how a real number gets ignored.
    if (value is None) != (conf == "unknown"):
        fail(errors, where, f"cost.value {value!r} vs confidence {conf!r} — a null value requires "
                            "confidence: unknown and vice versa")
    if value is None:
        if not str(cost.get("note") or "").strip():
            fail(errors, where, "unknown price needs a cost.note saying why no figure is recorded")
        return

    # Priced from here on. Provenance is not paperwork: without it there is no way to tell a figure
    # read off a rate card from one somebody remembered.
    if conf in ("verified", "documented") and not str(cost.get("source_url") or "").strip():
        # `source: observed` is the exception — its evidence is the captured example response and
        # the provider's own reported charge, not a page that may have moved since.
        if cost.get("source") != "observed":
            fail(errors, where, f"confidence '{conf}' requires a cost.source_url naming the rate card")
    checked = cost.get("checked")
    if not checked:
        fail(errors, where, "priced entry needs cost.checked (the date the PRICE was confirmed — "
                            "distinct from `verified`, which is the date the ROUTE was called)")
    elif (day := _as_date(checked)) is None:
        fail(errors, where, f"cost.checked '{checked}' is not an ISO date (YYYY-MM-DD)")
    elif (age := (dt.date.today() - day).days) > STALE_DAYS:
        warnings.append(f"{where}: cost.checked is {age} days old (> {STALE_DAYS}) — re-check the price")


SHARED_PLAN_KIND = "treg_shared_plan"


def check_fx(errors: list[str]) -> None:
    """The fx entries that carry a rate TREG SET, not one the vendor published.

    A `kind: treg_shared_plan` entry is the catalog asserting its own price for a flat-fee provider
    (see docs/SHARED-PLAN-PRICING-PLAN.md). Everything downstream shows the basis string as
    provenance, so the honesty lives in that string — these checks make it impossible to add a
    treg-set rate that does not say whose price it is, what the vendor fee was, and what volume it
    breaks even at. A treg rate card with none of that printed is exactly the dishonesty the design
    exists to avoid.
    """
    fx = yaml.safe_load((CATALOG / "fx.yaml").read_text()) or {}
    for service, entry in (fx.get("credit_rates_usd") or {}).items():
        if not isinstance(entry, dict):
            continue
        where = f"fx.yaml credit_rates_usd.{service}"
        kind = entry.get("kind")
        basis = str(entry.get("basis") or "")
        if kind is None:
            # a vendor-published rate; but prose claiming a treg rate without the marker is drift
            # between what the machine knows and what the human reads
            if "treg shared-plan" in basis.lower():
                fail(errors, where, "basis claims a treg shared-plan rate but has no "
                                    f"`kind: {SHARED_PLAN_KIND}` marker")
            continue
        if kind != SHARED_PLAN_KIND:
            fail(errors, where, f"unknown kind {kind!r} (only {SHARED_PLAN_KIND!r} exists)")
            continue
        if not isinstance(entry.get("usd"), (int, float)) or entry["usd"] <= 0:
            fail(errors, where, "a treg_shared_plan rate must carry a positive usd — null means "
                                "'unpriced', which needs no marker")
        if not basis.startswith("treg shared-plan rate"):
            fail(errors, where, "basis must START with 'treg shared-plan rate' so every surface "
                                "showing provenance says whose price this is")
        if not re.search(r"\$[\d,]+(?:\.\d+)?\s*/\s*(?:mo|yr)", basis):
            fail(errors, where, "basis must name the vendor fee (e.g. '$49.99/mo')")
        if not re.search(r"break-even at [\d,]+ calls/mo", basis):
            fail(errors, where, "basis must state 'break-even at N calls/mo'")
        if not entry.get("source") or not entry.get("checked"):
            fail(errors, where, "source and checked are required on a treg-set rate")
        fee = entry.get("fee_usd_month")
        if not isinstance(fee, (int, float)) or fee <= 0:
            fail(errors, where, "fee_usd_month is required on a treg_shared_plan entry — the "
                                "recovery report computes fee vs collected from it, and a fee that "
                                "lives only in prose cannot be computed against")


def main(argv: list[str]) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    check_fx(errors)
    tax = yaml.safe_load((CATALOG / "capabilities.yaml").read_text())
    platforms = set(tax.get("platforms") or {})
    capabilities = set(tax.get("capabilities") or {})

    from treg.oauth_providers import REGISTRY  # noqa: E402

    only = set(argv)
    files = sorted(p for p in CATALOG.glob("*.yaml") if p.name not in ("capabilities.yaml", "fx.yaml"))
    # "tikhub" selects tikhub.yaml AND tikhub.extended.yaml — a service is both its tiers
    service_of = {p: p.stem.removesuffix(".extended") for p in files}
    if only:
        files = [p for p in files if service_of[p] in only]
        missing = only - {service_of[p] for p in files}
        for m in missing:
            fail(errors, m, "no such catalog file")

    seen_ids: dict[str, str] = {}
    # (service, method, path) -> {tier: [ids]} — to catch the same operation shipping in BOTH tiers.
    # Some providers multiplex one URL across many jobs (SerpApi's `engine`, BrightData's
    # `dataset_id`, GAQL's query body), so a core/extended path collision is legitimate THERE and
    # only there. Everywhere else it is the ingest-dedup failure that shipped 27 duplicate rows
    # (dataforseo via the /v3 prefix mismatch, scrapecreators via core being curated after ingest).
    PARAM_MULTIPLEXED = {"serpapi", "brightdata", "google-ads"}
    route_tiers: dict[tuple, dict[str, list[str]]] = {}
    for path in files:
        name = path.name
        text = path.read_text()
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            fail(errors, name, "not a mapping")
            continue
        extended_file = path.name.endswith(".extended.yaml")
        service = data.get("provider")
        if service != service_of[path]:
            fail(errors, name, f"provider '{service}' != filename stem '{service_of[path]}'")
        if service not in REGISTRY:
            fail(errors, name, f"provider '{service}' not in oauth_providers.REGISTRY")
        src = data.get("source")
        # core files cite the docs they were curated from; extended files cite the specs they were
        # generated from, so that a re-run is reproducible from the file alone
        need = "spec_urls" if extended_file else "docs"
        if not isinstance(src, dict) or not src.get(need):
            fail(errors, name, f"source.{need} missing")
        proposed = set(data.get("proposed_capabilities") or {})
        known = capabilities | proposed

        eps = data.get("endpoints")
        if not isinstance(eps, list) or not eps:
            fail(errors, name, "endpoints missing or empty")
            continue
        for ep in eps:
            eid = ep.get("id", "<no id>")
            where = f"{name}:{eid}"
            tier = ep.get("tier", "extended" if extended_file else "core")
            if tier not in TIERS:
                fail(errors, where, f"bad tier '{tier}'")
                tier = "extended" if extended_file else "core"
            if extended_file != (tier == "extended"):
                fail(errors, where, f"tier '{tier}' does not belong in {name}")
            route_tiers.setdefault((service, ep.get("method"), ep.get("path")), {}) \
                .setdefault(tier, []).append(eid)
            for f in REQUIRED[tier]:
                if not ep.get(f):
                    fail(errors, where, f"missing required field '{f}'")
            if eid in seen_ids:
                fail(errors, where, f"duplicate id (also in {seen_ids[eid]})")
            seen_ids[eid] = name
            if service and not eid.startswith(f"{service}."):
                fail(errors, where, f"id must start with '{service}.'")
            # extended entries are unmapped by design; one that DOES claim a capability is held to
            # the same referential rules as core, so a hand-promoted entry can't drift
            cap = ep.get("capability", "")
            if cap or tier == "core":
                if cap not in known:
                    fail(errors, where, f"capability '{cap}' not in capabilities.yaml or proposed_capabilities")
            plat = ep.get("platform", "")
            if plat not in platforms:
                fail(errors, where, f"platform '{plat}' not in capabilities.yaml platforms")
            if cap and plat and cap.split(".")[0] != plat:
                fail(errors, where, f"platform '{plat}' != capability's first segment '{cap.split('.')[0]}'")
            # `domain` is optional — the loader derives one from the capability id or the path when
            # it is absent. Declaring one overrides that, so it has to be the same SHAPE the derived
            # ones are: one lowercase word, or the platform page grows a section of one.
            # `name` is an optional short DISPLAY title; `summary` stays the provider's own
            # description. Light check only: present ⇒ a non-empty string that fits a row heading.
            nm = ep.get("name")
            if nm is not None and (not isinstance(nm, str) or not nm.strip() or len(nm) > 60):
                fail(errors, where, "name must be a non-empty string of at most 60 chars")
            dom = ep.get("domain")
            if dom is not None and not DOMAIN.fullmatch(str(dom)):
                fail(errors, where, f"domain '{dom}' must be a single lowercase word (a-z0-9_)")
            # `kind` is optional (absent ⇒ data); a stated one must be a known kind
            if ep.get("kind") is not None and ep.get("kind") not in KINDS:
                fail(errors, where, f"kind '{ep.get('kind')}' not one of {sorted(KINDS)}")
            if ep.get("scope", "any_account") not in SCOPES:
                fail(errors, where, f"bad scope '{ep.get('scope')}'")
            if ep.get("method") not in METHODS:
                fail(errors, where, f"bad method '{ep.get('method')}'")
            cost = ep.get("cost")
            if cost is not None or tier == "core":
                # cost is optional in the extended tier — several providers publish prices per API
                # family rather than per route — but a stated cost must still be a real cost model.
                # An ABSENT one reads as "price unknown", never as free (catalog.md, Cost), which is
                # why nothing here has to be written out for it to be refused a platform key.
                if not isinstance(cost, dict):
                    fail(errors, where, f"cost.type missing or not one of {sorted(COST_TYPES)}")
                else:
                    check_cost(cost, where, errors, warnings)
            if ep.get("verified"):
                ex = ep.get("example_response")
                if not ex:
                    fail(errors, where, "verified but no example_response")
                elif not (CATALOG / ex).is_file():
                    fail(errors, where, f"example_response '{ex}' does not exist")
                if not ep.get("test_request"):
                    fail(errors, where, "verified but no test_request (nothing to re-verify with)")
            if tier == "extended":
                # "been through the pipeline" = a run either built it a request or recorded an
                # outcome for it. A freshly ingested entry that has never been verified has
                # neither, and is left alone.
                claimed = [s for s in STATES if ep.get(s)]
                touched = ep.get("test_request") or any(s in ep for s in STATES)
                if len(claimed) > 1:
                    fail(errors, where, f"claims {len(claimed)} states at once: {claimed} — "
                                        "verified/unverified/untestable/skipped are exclusive")
                elif touched and not claimed:
                    empty = [s for s in STATES if s in ep]
                    fail(errors, where, f"no endpoint state: {sorted(empty)} present but empty"
                         if empty else "has a test_request but no verified/unverified/untestable/"
                                       "skipped saying what happened when it was called")
                if ep.get("untestable") and ep.get("test_request"):
                    # `untestable` means no call is possible — but catalog_verify.py --extended
                    # replays anything that HAS a test_request, so the pair is not just a
                    # contradiction on paper: it gets the endpoint called, and billed, by a
                    # re-verification run that was told it was uncallable.
                    fail(errors, where, "untestable but carries a test_request — a re-verify run "
                                        "would call it anyway; drop one of the two")

        if extended_file:
            # extended files are machine-generated from PUBLIC specs and public target ids (TikTok
            # secUids, WeChat export ids, CDN URIs, pagination cursors) — long opaque strings that
            # pattern-match as secrets endlessly. Credentials only ever travel via TREG_CATALOG_CRED
            # env in the verify scripts, so here only the unambiguous leak shape is flagged.
            for m in re.finditer(r"Bearer\s+[A-Za-z0-9+/_=-]{16,}", text):
                fail(errors, name, f"credential literal in file: '{m.group(0)[:24]}…'")
            continue
        for m in LEAK.finditer(text):
            if looks_like_secret(m.group(0)):
                fail(errors, name, f"possible credential literal in file: '{m.group(0)[:24]}…'")

    # The same operation must not ship in both tiers — core wins and the extended copy is a bug,
    # except for the param-multiplexed providers where one URL is legitimately many jobs.
    for (svc, method, path), tiers in route_tiers.items():
        if svc in PARAM_MULTIPLEXED or not (tiers.get("core") and tiers.get("extended")):
            continue
        fail(errors, f"{svc} {method} {path}",
             f"shipped in BOTH tiers — core {tiers['core']} duplicates extended {tiers['extended']}; "
             f"drop the extended copy (core wins) or fix the ingester's core_routes dedup")

    for e in errors:
        print(e)
    for w in warnings:
        print(f"WARN {w}")
    print(f"{'FAIL' if errors else 'OK'} — {len(files)} provider file(s), {len(seen_ids)} endpoint(s), "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
