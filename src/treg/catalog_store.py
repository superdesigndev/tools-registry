"""Reader for the endpoint catalog (`src/treg/catalog/*.yaml`) — the operations layer.

The marketplace registry (`oauth_providers.py`) catalogs CREDENTIALS: how to connect a provider.
This reads the other half: what you can DO once connected — which endpoints exist per platform,
what they cost, and which ones were actually verified against the live API. See
`docs/context/architecture/catalog.md` for the data's schema and curation process.

SERVER-SIDE ONLY. The CLI must never import this (it pulls in pyyaml, which is a `[server]` extra);
`treg catalog` is a thin client over the /catalog/* endpoints like every other CLI command.

The data is read-only and changes only on deploy, so it is parsed once and cached in the process.
A missing or half-written catalog dir degrades to an empty catalog rather than failing startup —
serving no endpoints is a worse product, not a broken server.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATALOG_DIR = Path(__file__).parent / "catalog"
EXAMPLES_DIRNAME = "examples"
CAPABILITIES_FILE = "capabilities.yaml"
FX_FILE = "fx.yaml"
ALIASES_FILE = "aliases.yaml"

# What an endpoint IS, so the marketplace can browse the useful surface and tuck the plumbing away:
#   data    — fetch/scrape/enrich a resource (the DEFAULT when `kind:` is absent).
#   action  — a meaningful WRITE on the connected user's own account (post, reply, update a budget).
#   account — the provider's own bookkeeping CRUD (create/delete a lead-list, manage webhooks).
#   utility — helpers with no data of their own (token generators, enum/location lookups, decrypt).
# `data`+`action` are the browse surface; `account`+`utility` are "management endpoints" — served in
# the endpoint list with `kind` set, but kept OUT of the census counts and the default platform view.
KINDS = ("data", "action", "account", "utility")
DEFAULT_KIND = "data"
HIDDEN_KINDS = frozenset({"account", "utility"})  # served, but never inflate the browse counts

# How much the recorded PRICE is worth as evidence (cost.confidence). It is a claim about the
# price, not about the endpoint: `verified: 2026-07-28` says the route answered, `confidence:
# verified` says the money figure was confirmed against something re-checkable.
#   verified   — observed in real billing, or read from the provider's own live rate card
#   documented — transcribed from the provider's docs / pricing page (docs lie, and prices move)
#   inferred   — derived from an adjacent price (a sibling route, a plan average)
#   unknown    — no figure at all; `value` must be null and `note` must say why
CONFIDENCES = ("verified", "documented", "inferred", "unknown")
# Where the figure came from. `observed` is evidenced by the captured example + the provider's own
# reported charge rather than a URL; every other source names one.
COST_SOURCES = ("rate_card_api", "docs", "observed", "vendor_email", "inferred")
# What `per` counts: "value currency per <per> <unit>". Under `currency: unit` the provider bills in
# its own meter and `unit` names that meter (see `Catalog.cost_view`).
COST_UNITS = ("call", "result", "row", "record", "keyword", "page", "character", "review",
              "section", "employee", "GB", "ad", "month", "line", "target", "domain", "item",
              "post", "user",
              "api_unit", "analysis_unit", "retrieval_unit", "index_item_unit", "quota_row",
              "verifier_credit")
# A platform key spends OUR money on a caller's behalf, so it is allowed only where the price is
# machine-computable and provenanced. `account`-kind routes are the provider's own bookkeeping —
# never worth spending on — and `own_account` scope needs the caller's own credential by definition.
PLATFORM_INELIGIBLE_KINDS = frozenset({"account"})


@dataclass(frozen=True)
class Catalog:
    fx: dict[str, float] = field(default_factory=dict)           # currency -> USD rate (fx.yaml)
    # provider service -> USD per credit, or None when the provider publishes no per-credit price.
    # Credits are PROVIDER-scoped, never a currency: one scrapecreators credit and one lusha credit
    # are unrelated, so this cannot live in `fx` alongside CNY.
    credit_rates: dict[str, float | None] = field(default_factory=dict)
    # service -> {"usd", "fee_usd_month"} for rates TREG SET (fx.yaml `kind: treg_shared_plan`).
    # Kept separately because two consumers need the distinction: the reconcile recovery report
    # computes fee-vs-collected from it, and any surface explaining provenance must say whose price
    # this is. The rate itself still lives in `credit_rates` like any other — pricing machinery
    # neither knows nor cares that the rate is ours.
    shared_plans: dict[str, dict] = field(default_factory=dict)
    # service -> calls/team/day for rates treg set to ZERO (fx.yaml `kind: treg_trial`): a capped
    # free taste served on treg's own free-tier key. The allowance lives beside the zero because at
    # $0 the price gives no brake — the cap is the only congestion control, so an entry without one
    # is invalid (check_fx). api._enforce_trial_allowance reads this.
    trial_pools: dict[str, int] = field(default_factory=dict)
    # provider service -> meter name -> USD per one unit of that meter (fx.yaml `unit_rates_usd`).
    # Providers that bill in a proprietary meter (Semrush API units, Majestic's three pools, Moz's
    # row quota) get one rate per meter — a provider can have several, and they do not convert
    # into each other any more than two providers' credits do.
    unit_rates: dict[str, dict[str, float | None]] = field(default_factory=dict)
    platforms: dict[str, dict] = field(default_factory=dict)     # slug -> {label, category}
    capabilities: dict[str, str] = field(default_factory=dict)   # id -> description
    endpoints: list[dict] = field(default_factory=list)          # normalized endpoint dicts
    # query word -> catalog words that mean the same thing (aliases.yaml) — search-time only
    aliases: dict[str, list[str]] = field(default_factory=dict)
    # lazy per-instance search index (see _search_fields) — never part of identity or repr
    _search_fields: list | None = field(default=None, init=False, repr=False, compare=False)
    by_id: dict[str, dict] = field(default_factory=dict)
    provider_meta: dict[str, dict] = field(default_factory=dict)  # service -> {limits, pricing_url, docs}

    def for_capability(self, capability: str) -> list[dict]:
        return [e for e in self.endpoints if capability and e["capability"] == capability]

    def for_platform(self, slug: str) -> list[dict]:
        return [e for e in self.endpoints if e["platform"] == slug]

    def for_provider(self, service: str) -> list[dict]:
        return [e for e in self.endpoints if e["provider"] == service]

    def cost_view(self, cost, provider: str | None = None) -> dict | None:
        """A cost dict with a computed `usd` field — USD for ONE chargeable event (one call, or one
        result, per `cost.type`). Source of truth stays in the provider's BILLING currency; USD is
        derived at serve time from fx.yaml so a rate refresh re-prices the whole catalog at once.
        `usd` is None when the value or the rate is unknown — never 0, because "we don't know" and
        "it's free" are different answers and only one of them may be billed against.

        Three kinds of denomination convert, and they convert differently:
          - a real currency (USD, CNY) via fx.yaml `rates_to_usd`, keyed by currency;
          - `currency: credit` — NOT a currency but a PROVIDER-scoped unit (one scrapecreators
            credit and one lusha credit are unrelated), via `credit_rates_usd`, keyed by service;
          - `currency: unit` — the provider's own meter, via `unit_rates_usd[provider][unit]`.
        Hence `provider`. A null rate keeps `usd` None and the client displays the native amount.

        `per` (default 1) is the quantity the price covers, so a $2.00-per-1,000-rows endpoint
        records `value: 2.0, per: 1000, unit: row` and prices out at $0.002 per row."""
        if not isinstance(cost, dict):
            return None
        value, cur = cost.get("value"), cost.get("currency", "USD")
        per = cost.get("per") or 1
        if cur == "credit":
            rate = self.credit_rates.get(provider)
        elif cur == "unit":
            rate = self.unit_rates.get(provider or "", {}).get(str(cost.get("unit") or ""))
        else:
            rate = self.fx.get(cur)
        # 9 decimals, not 6: a per-row price can be a ten-thousandth of a cent and rounding it to
        # zero would turn a real charge into "free" for anything reading `usd` as money.
        usd = (round(value * rate / per, 9)
               if isinstance(value, (int, float)) and rate is not None and per > 0 else None)
        out = {**cost, "usd": usd}
        # A $0 trial price travels with its allowance, so every surface showing the price can also
        # say how much of it a team gets — a bare $0.00 would read as unlimited.
        if provider in self.trial_pools and usd == 0:
            out["trial_calls_per_team_day"] = self.trial_pools[provider]
        return out

    def platform_eligible(self, endpoint: dict) -> bool:
        """May treg serve this endpoint with a PLATFORM key, billed to the caller's balance?

        One predicate, so the API, the validator and the proxy cannot drift apart. A missing or
        unknown price must read as "refuse", never as free (docs/context/architecture/catalog.md,
        Cost). Three things must hold: the USD cost is computable, the price provenance is at least
        `documented` (2026-07-31 policy: a provider-published rate is billable — `verified` remains
        the gold standard the drift reports police; `inferred`/`unknown` stay refused), and the
        route is not the caller's own account's business (`own_account` scope / `account` kind).
        The live-verified stamp is deliberately NOT required anymore: a broken route fails
        unbilled under per_success/per_result, and the fail-closed daily cap bounds the rest —
        coverage beats caution now that the ladder and settle logic are proven.
        """
        # A marked row is retained only to explain a cached id and point at its replacement. It is
        # never an offer treg may spend against, even if its historical price remains complete.
        if endpoint.get("status"):
            return False
        # Blocked on treg's own plan: the route works, the price is real, and the shared key
        # still cannot serve it — a documented upstream "your subscription does not include this
        # endpoint". Discovery keeps the row (a caller's own key may serve it); the offer doesn't.
        if endpoint.get("platform_blocked"):
            return False
        cost = self.cost_view(endpoint.get("cost"), endpoint.get("provider"))
        if not cost or cost.get("usd") is None:
            return False
        # A FREE route needs no price provenance: `confidence` exists to say how much we trust a
        # NUMBER we are about to charge, and there is no number here. Requiring it anyway refused 61
        # endpoints across 8 providers — 28 of Hunter's 35 — as though "costs nothing" were the same
        # answer as "we don't know", which is the exact distinction the rest of this file is careful
        # to keep apart.
        priced_or_free = (cost.get("type") == "free" and not cost.get("usd")) \
            or cost.get("confidence") in ("verified", "documented")
        return bool(priced_or_free
                    and endpoint.get("scope") != "own_account"
                    and (endpoint.get("kind") or DEFAULT_KIND) not in PLATFORM_INELIGIBLE_KINDS)


_CACHE: Catalog | None = None


def load(*, refresh: bool = False, directory: Path | None = None) -> Catalog:
    """The parsed catalog, cached for the life of the process. `refresh`/`directory` exist for tests."""
    global _CACHE
    if directory is not None:
        return _parse(Path(directory))
    if _CACHE is None or refresh:
        _CACHE = _parse(CATALOG_DIR)
    return _CACHE


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse(directory: Path) -> Catalog:
    if not directory.is_dir():
        return Catalog()
    taxonomy = _read_yaml(directory / CAPABILITIES_FILE)
    # Platform values are {label, category} mappings (category = the marketplace tab the platform
    # browses under); a bare-string value is accepted as a label-only shorthand.
    platforms = {
        slug: (v if isinstance(v, dict) else {"label": str(v)}) | {"category": (v.get("category") if isinstance(v, dict) else None) or "Other"}
        for slug, v in (taxonomy.get("platforms") or {}).items()
    }
    capabilities = dict(taxonomy.get("capabilities") or {})

    endpoints: list[dict] = []
    by_id: dict[str, dict] = {}
    provider_meta: dict[str, dict] = {}
    # Sorted so a rebuild is deterministic; `<service>.extended.yaml` sorts right after
    # `<service>.yaml`, and both land under the same provider (curation splits a provider's
    # core operations from the long tail across two files).
    for path in sorted(directory.glob("*.yaml")):
        if path.name in (CAPABILITIES_FILE, FX_FILE, ALIASES_FILE):
            continue
        doc = _read_yaml(path)
        provider = doc.get("provider") or path.name.split(".")[0]
        # A capability a provider file proposes is not yet in the shared taxonomy, but it is
        # already the join key of its endpoints — carry its description so the API can label it.
        for cap, desc in (doc.get("proposed_capabilities") or {}).items():
            capabilities.setdefault(cap, desc)
        # Provider-wide facts live once at the top of the file, not on every endpoint; the detail
        # view reunites them with the endpoint. `<service>.extended.yaml` fills only what its core
        # file left blank, so the curated file stays authoritative.
        meta = provider_meta.setdefault(provider, {})
        for key, value in (("limits", doc.get("limits")),
                           ("pricing_url", doc.get("pricing_url")),
                           ("docs", (doc.get("source") or {}).get("docs"))):
            if value and not meta.get(key):
                meta[key] = str(value)
        for raw in doc.get("endpoints") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            ep = _normalize(raw, provider, directory)
            if ep["id"] in by_id:  # first file wins; ids are unique by validator contract
                continue
            by_id[ep["id"]] = ep
            # Marked endpoints stay addressable by id so a caller holding yesterday's id gets the
            # provider's retirement story and replacement. Browse/search/census all read
            # `endpoints`, so omitting the row here removes it from every discovery surface without
            # turning a known retirement into an unexplained 404.
            if not ep["status"]:
                endpoints.append(ep)

    for ep in endpoints:  # a platform seen only in provider files still deserves a label
        platforms.setdefault(ep["platform"], {"label": ep["platform"], "category": "Other"})
    aliases = {
        str(k).lower(): [str(v).lower() for v in (vals if isinstance(vals, list) else [vals])]
        for k, vals in (_read_yaml(directory / ALIASES_FILE).get("aliases") or {}).items()
    }
    fx_doc = _read_yaml(directory / FX_FILE) or {}
    fx = dict(fx_doc.get("rates_to_usd") or {"USD": 1.0})
    # Each entry is a {usd, basis, source, checked} block; only the rate is served, and `usd: null`
    # (no published per-credit price) is kept as an explicit None so display stays native.
    credit_rates = {
        str(service): (v.get("usd") if isinstance(v, dict) else v)
        for service, v in (fx_doc.get("credit_rates_usd") or {}).items()
    }
    shared_plans = {
        str(service): {"usd": v.get("usd"), "fee_usd_month": v.get("fee_usd_month")}
        for service, v in (fx_doc.get("credit_rates_usd") or {}).items()
        if isinstance(v, dict) and v.get("kind") == "treg_shared_plan"
    }
    trial_pools = {
        str(service): int(v.get("trial_calls_per_team_day") or 0)
        for service, v in (fx_doc.get("credit_rates_usd") or {}).items()
        if isinstance(v, dict) and v.get("kind") == "treg_trial"
    }
    # Same shape one level deeper: service -> meter -> {usd, basis, source, checked}. A provider
    # bills in several meters at once (Majestic has three), so the meter name is part of the key.
    unit_rates = {
        str(service): {str(meter): (v.get("usd") if isinstance(v, dict) else v)
                       for meter, v in (meters or {}).items()}
        for service, meters in (fx_doc.get("unit_rates_usd") or {}).items()
    }
    return Catalog(fx=fx, credit_rates=credit_rates, unit_rates=unit_rates,
                   shared_plans=shared_plans, trial_pools=trial_pools, platforms=platforms,
                   capabilities=capabilities, endpoints=endpoints, by_id=by_id,
                   provider_meta=provider_meta, aliases=aliases)


# The subject an endpoint is ABOUT, within its platform — the section a platform page files it
# under. Order matters: the first hit wins, so the specific keys ("showcase", "hashtag") sit ahead of
# the general ones they contain a word of. Deliberately shared across platforms: a douyin "comment"
# route and a tiktok one belong under the same heading, and one table beats sixty bespoke ones.
DOMAIN_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("backlink", "backlinks"), ("llm_mention", "mentions"), ("llm_response", "ai answers"),
    ("llm_scraper", "ai answers"), ("llm", "ai answers"), ("keyword", "keywords"), ("serp", "serp"),
    ("shop", "shop"), ("showcase", "shop"), ("product", "shop"),
    ("ads", "ads"), ("ad_", "ads"),
    ("analytics", "analytics"), ("creator", "creator"), ("search", "search"),
    ("hashtag", "hashtag"), ("challenge", "hashtag"),
    ("comment", "video"), ("video", "video"), ("post", "video"), ("aweme", "video"),
    ("music", "music"), ("sound", "music"), ("live", "live"),
    ("user", "user"), ("profile", "user"), ("follow", "user"),
    ("feed", "feed"), ("publish", "publishing"),
)
DOMAIN_OTHER = "other"
# Path segments that say how a route is CALLED, not what it is about. DataForSEO ends every
# synchronous route in `/live`, which read as a subject and filed 33 unrelated SEO endpoints under a
# "live" heading — the same trap for a version prefix or a `/json` suffix.
DOMAIN_NOISE = {"live", "task_post", "task_get", "task_ready", "tasks_ready", "tasks_fixed",
                "json", "html", "xml", "api", "rest", "open", "web", "app", "mobile", "desktop",
                # provider BRAND/family segments: how the vendor organises its API, not what the
                # route is about — reading them as a subject put a "dataforseo_labs" heading on the
                # Google page. Filtered here so such routes classify by their function keywords.
                # `web_vN` is the "web" delivery marker with a version glued on (TikHub's
                # `/xiaohongshu/web_v3/…`); left in, it grew a "web_v3"/"web_v2" section per platform.
                "dataforseo_labs", "appendix", "ai_optimization", "web_v2", "web_v3", "web_v4"}
_VERSION_SEG = re.compile(r"v\d+(?:\.\d+)*")
_WORD_SEG = re.compile(r"[a-z][a-z0-9_]*")
# The WHOLE words of a path segment — split on non-letters AND at camelCase humps, so a keyword
# matches a word boundary, never a fragment: `ads` must not fire inside `leads`/`threads`, `user`
# not inside `abuser`, while `searchAnalytics` still yields `search` + `analytics`.
_WORDS = re.compile(r"[A-Z]?[a-z]+")


def _domain(raw: dict, capability: str, platform: str) -> str:
    """Which section of its platform's page this endpoint belongs to.

    Four sources, best first: the yaml's own `domain:` (curation always wins), the capability id's
    middle segment (`tiktok.video.comments` → video — the taxonomy already encodes the subject), a
    keyword read off the path and summary, and failing that the path's own leading subject segment
    (`/v3/backlinks/anchors/live` → backlinks), which is usually how a provider organises its API.
    """
    explicit = str(raw.get("domain") or "").strip().lower()
    if explicit:
        return explicit
    parts = capability.split(".")
    if len(parts) >= 3 and parts[1]:
        return parts[1]
    segments = [s for s in str(raw.get("path") or "").split("/")
                if s and s.lower() not in DOMAIN_NOISE and not _VERSION_SEG.fullmatch(s.lower())]
    # The PATH only, never the summary. Prose says "the Live SERP API…" and "your own post…" about
    # endpoints that are neither, and one false heading is worse than a row in `other`: a section a
    # visitor doesn't trust is a section they stop reading. Match keywords at WORD boundaries, not by
    # raw substring: substring put an "ads" section on the People page (it lives inside `leads`) and a
    # "user" one on abuse reports (inside `abuser`). A stem key (`keyword` → `keywords`) still matches
    # by prefix; a short key (`ads`, `llm`) must hit a whole word, so `ads` ≠ `leads`/`adset`.
    words = [w.lower() for seg in segments for w in _WORDS.findall(seg)]
    for key, domain in DOMAIN_KEYWORDS:
        stem = key.rstrip("_")  # `ad_` → `ad`: the singular still matches the `ad`/`adGroup` word
        for w in words:
            if w.startswith(stem) and (len(stem) > 3 or w == stem):
                return domain
    # Only a GROUPING segment counts — one with an operation name after it. The last segment IS the
    # operation ("generate_xbogus"), and a section per operation is not a section at all.
    seg_lc = [s.lower() for s in segments]
    candidates = [s for s in seg_lc if s != platform and _WORD_SEG.fullmatch(s)]
    return candidates[0] if len(candidates) > 1 else DOMAIN_OTHER


def _effective_cost(raw: dict):
    """The price to display: the declared cost block, else the price the verifier OBSERVED.

    Providers that price per API family (DataForSEO) ship no per-route cost block, which left every
    row reading "—" even after live verification had recorded the provider's own stated charge.
    An observed price is real money that was actually billed — better display data than silence.
    Zero observed cost means the call was genuinely free (some routes bill nothing).

    An observed price is also the STRONGEST provenance the catalog has: the figure is what the
    provider itself reported charging for that exact call, on the date in `verified`. So the
    synthesized block carries `source: observed, confidence: verified` and needs no `source_url` —
    its evidence is the captured example response, not a page that might have moved since. Without
    a `verified` date there is no date to stand behind, and the price stays unprovenanced."""
    cost = raw.get("cost")
    if isinstance(cost, dict) and (cost.get("value") is not None or cost.get("type") == "free"):
        return cost
    oc = raw.get("observed_cost")
    if oc is None:
        return cost or None
    when = str(raw.get("verified") or "")
    seen = {"source": "observed", "confidence": "verified", "checked": when} if when else {}
    if oc == 0:
        return {"type": "free", "value": 0, "currency": "USD", "per": 1, "unit": "call", **seen,
                "note": "no charge observed at verification"}
    return {"type": "per_call", "value": oc, "currency": "USD", "per": 1, "unit": "call", **seen,
            "note": "price observed at live verification (provider-reported charge)"}


def _normalize(raw: dict, provider: str, directory: Path) -> dict:
    capability = raw.get("capability") or ""
    platform = raw.get("platform") or (capability.split(".")[0] if capability else "other")
    verified = raw.get("verified")
    return {
        "id": str(raw["id"]),
        "provider": provider,
        "capability": capability,
        "platform": platform,
        "domain": _domain(raw, capability, platform),
        "scope": raw.get("scope") or "",
        # what the endpoint IS (see KINDS): default `data`, so every un-annotated route stays on the
        # browse surface and only the deliberately-marked plumbing (account/utility) is tucked away.
        "kind": str(raw.get("kind") or DEFAULT_KIND).strip().lower() or DEFAULT_KIND,
        "method": (raw.get("method") or "GET").upper(),
        "path": raw.get("path") or "",
        # optional short display title; `summary` stays the provider's own description, verbatim
        "name": str(raw.get("name") or "").strip(),
        "summary": raw.get("summary") or "",
        "input": raw.get("input") or {},
        # the request the verifier actually made — a proven set of values, which is what makes
        # `call_template` a paste-ready line rather than a shape with placeholders in it
        "test_request": raw.get("test_request") or {},
        "cost": _effective_cost(raw),
        # Absent `tier` means core: the curated first wave predates the split, and treating an
        # unmarked endpoint as extended would hide it from the platform view entirely.
        "tier": raw.get("tier") or "core",
        "verified": str(verified) if verified else None,
        # {status, means} — a status the provider uses for "asked and answered: no result" (PDL
        # 404s a person it has no record of). Only endpoints with evidenced miss semantics carry
        # it; for everything else an error status means what it says.
        "miss": raw.get("miss") or None,
        # A provider can retire/move a route after an agent has cached its id. Keep that id in
        # `by_id`, but remove it from discovery and return the migration story on direct lookup.
        "status": str(raw.get("status") or "").strip().lower(),
        "status_note": str(raw.get("status_note") or "").strip(),
        # A route that WORKS upstream but that treg's own plan/subscription cannot serve (a
        # provider tier the shared key lacks). Unlike `status` it stays in discovery — a team's
        # own key may well serve it — but it is never a platform offer, and the reason rides on
        # the row so an agent learns "bring your own key" BEFORE the 403, not from it.
        "platform_blocked": str(raw.get("platform_blocked") or "").strip(),
        "superseded_by": str(raw.get("superseded_by") or "").strip(),
        "docs_url": raw.get("docs_url") or "",
        "example_file": _example_file(raw, directory),
    }


def _example_file(raw: dict, directory: Path) -> str | None:
    """The captured example response's filename, or None when it isn't on disk.

    Only the BASENAME of the declared path is honoured — examples live in one flat directory, and a
    data file must never be able to point the server at an arbitrary path.
    """
    declared = raw.get("example_response") or f"{EXAMPLES_DIRNAME}/{raw['id']}.json"
    name = Path(str(declared)).name
    if not name.endswith(".json"):
        return None
    return name if (directory / EXAMPLES_DIRNAME / name).is_file() else None


def example_path(endpoint_id: str) -> Path | None:
    """Filesystem path of a KNOWN endpoint's example response, or None.

    The id is resolved through the loaded catalog first, so nothing a caller types is ever joined
    into a path — traversal attempts simply miss the lookup.
    """
    ep = load().by_id.get(endpoint_id)
    if ep is None or not ep["example_file"]:
        return None
    return CATALOG_DIR / EXAMPLES_DIRNAME / ep["example_file"]


# ---- shaping ---------------------------------------------------------------------------------

def endpoint_view(ep: dict, provider_display: str, cat: Catalog | None = None) -> dict:
    """The public JSON shape of one endpoint (the dashboard + CLI render this)."""
    return {
        "id": ep["id"],
        "provider": ep["provider"],
        "provider_display": provider_display,
        # short display title (may be ""); clients fall back to `summary` when absent
        "name": ep.get("name") or "",
        "summary": ep["summary"],
        "method": ep["method"],
        "path": ep["path"],
        "scope": ep["scope"],
        "tier": ep["tier"],
        # data | action | account | utility — the front-end hides account/utility behind an expander
        "kind": ep.get("kind") or DEFAULT_KIND,
        # the section of its platform page this row files under (see `_domain`)
        "domain": ep.get("domain") or DOMAIN_OTHER,
        # the whole point of a row is the call it stands for, so the line that makes it is part of
        # the row — not a second request away
        "call_template": call_template(ep),
        "cost": cat.cost_view(ep["cost"], ep["provider"]) if cat else ep["cost"],
        # whether treg may serve this route with its OWN key, billed to the caller's balance —
        # a fact about the row that decides whether the caller needs a credential at all, so it
        # rides on the row rather than being re-derived per client (see `Catalog.platform_eligible`)
        "platform_eligible": cat.platform_eligible(ep) if cat else None,
        # WHY the platform can't serve an otherwise-working route (plan/tier gap on treg's own
        # subscription) — so "bring your own key" is said up front instead of discovered via a 403.
        "platform_blocked": ep.get("platform_blocked") or None,
        # "no match" semantics, when the endpoint has them — an agent that reads `miss` stops
        # treating an expected empty answer as a failed call (and stops retrying it).
        "miss": ep.get("miss"),
        # Only direct-id lookups can return a marked row; discovery surfaces never include one.
        "status": ep.get("status") or None,
        "status_note": ep.get("status_note") or None,
        "superseded_by": ep.get("superseded_by") or None,
        "verified": ep["verified"],
        "docs_url": ep["docs_url"],
        "has_example": bool(ep["example_file"]),
        # the request schema, split by location (pathParams/queryParams/body + notes) — without it
        # the dashboard can show what comes BACK (example_response) but not what to SEND
        "input": ep.get("input") or None,
        # the exact request that live-verified this endpoint — the Try-it drawer prefills from it
        # verbatim (it also carries the ground truth the input spec can't express: whether the
        # body is a bare object or an ARRAY of tasks, which dataforseo requires)
        "test_request": ep.get("test_request") or None,
    }


def endpoint_context(ep: dict, cat: Catalog) -> dict:
    """The join keys an endpoint row needs to stand ALONE (search results, the detail view) — inside
    a platform page these are implied by the page itself, so `endpoint_view` leaves them out."""
    return {
        "capability": ep["capability"],
        "capability_description": cat.capabilities.get(ep["capability"], ""),
        "platform": ep["platform"],
        "platform_label": cat.platforms.get(ep["platform"], {}).get("label", ep["platform"]),
    }


def domain_rows(pairs: list[tuple[dict, dict]], capabilities: dict[str, str]) -> list[dict]:
    """One platform's `(endpoint, view)` pairs as the LEDGER the platform page renders: domain
    sections, each holding merged rows (a job several providers do) before single rows.

    The ordering is decided here rather than in the browser so every client — and every screenshot —
    agrees on it: `other` last because it is the junk drawer, the rest busiest-first because the
    biggest section is the one a visitor came for, and merged before single because a comparable job
    is worth more than a lone route.
    """
    by_cap: dict[str, list[dict]] = {}
    rows: list[dict] = []
    for ep, view in pairs:
        if ep["capability"]:
            by_cap.setdefault(ep["capability"], []).append(view)
        else:
            # `name` is the curated short title; `summary` is documentation prose and can run to a
            # paragraph, so it leads a row only until a name exists for that endpoint.
            rows.append({"kind": "single", "capability": None,
                         "description": view["name"] or view["summary"],
                         "domain": view["domain"], "endpoints": [view]})

    def _ep_rank(v: dict):
        # Within one capability: cheapest first (free counts as cheapest; unpriced last — an
        # unknown price must not lead a comparison), then the curated core route before ingested
        # ones, then live-verified before unproven, then stable alpha. Before this existed the
        # order was an accident of filename sorting ("tikhub.extended.yaml" < "tikhub.yaml"), which
        # put the canonical core route at the BOTTOM of its own row.
        usd = (v.get("cost") or {}).get("usd")
        return (usd is None, usd if usd is not None else 0.0,
                v.get("tier") != "core", not v.get("verified"),
                v.get("provider") or "", v["id"])

    for cap, eps in by_cap.items():
        eps.sort(key=_ep_rank)
        title = capabilities.get(cap, "") or cap
        # A capability only ONE provider implements is not a comparison, so it does not merge: it
        # becomes a row per endpoint, each led by its own summary, same as an unmapped route. Folding
        # a provider's two takes on one job into a single row would show one route and the OTHER's
        # price. The capability id stays on every row either way; it is a join key, never a heading.
        if len({e["provider"] for e in eps}) > 1:
            rows.append({"kind": "merged", "capability": cap, "description": title,
                         "domain": eps[0]["domain"], "endpoints": eps})
            continue
        rows += [{"kind": "single", "capability": cap, "description": e["name"] or e["summary"] or title,
                  "domain": e["domain"], "endpoints": [e]} for e in eps]

    sections: dict[str, list[dict]] = {}
    for row in rows:
        sections.setdefault(row["domain"], []).append(row)
    for section in sections.values():
        # merged first (a comparable job beats a lone route), then the widely-supported jobs — the
        # ones the most providers implement, i.e. the popular ones — before the niche, then alpha.
        section.sort(key=lambda r: (r["kind"] != "merged", -len(r["endpoints"]), r["description"].lower()))
    order = sorted(sections, key=lambda d: (d == DOMAIN_OTHER, -len(sections[d]), d))
    return [{"domain": d, "rows": sections[d]} for d in order]


# ---- near misses -----------------------------------------------------------------------------
def _id_shape(endpoint_id: str) -> tuple[str, list[str]]:
    """`(provider, comparable tokens)` for an endpoint id.

    The provider is the FIRST DOT-DELIMITED SEGMENT, kept whole — not the first word of a
    tokenisation. Ten providers are hyphenated (`google-ads`, `meta-ad-library`,
    `google-search-console`, …), and splitting on non-alphanumerics turned `google-ads` into
    `google`, which matches no provider, so every near miss under those ten silently returned
    nothing at all.

    The `x` tier marker is then stripped only where it can BE a tier marker — the second segment —
    instead of everywhere. Dropping the token `x` globally erased the name of the provider that is
    literally called `x` (X/Twitter, 173 endpoints), so even a correct `x.*` id resolved to nothing.
    Both bugs made the report-#1 fix quietly inert across ~17% of the catalog while its test, which
    only used single-word providers, went on passing.
    """
    head, _, rest = endpoint_id.strip().lower().partition(".")
    if rest.startswith("x."):
        rest = rest[2:]
    return head, [t for t in _SPLIT.split(rest) if t]


def near_ids(endpoint_id: str, cat: Catalog, limit: int = 3) -> list[str]:
    """Real ids close to one that doesn't exist — for the answer to an unknown id.

    An id is not free text: an agent that has one and is told only "no endpoint 'x'" has hit a dead
    end mid-plan, and the usual next move is to invent another id and fail again. Almost every real
    miss is one of three shapes — a dropped tier segment (`lusha.companies-signals` for
    `lusha.x.companies-signals`, which is what a model does when it relays an id through a summary),
    a singular/plural slip, or a stale id we retired — and all three are one comparison away from
    the right answer.

    Deliberately cheap and predictable, in this order: same provider and the same remaining segments
    ignoring the tier marker, then a shared-token overlap ranked by how much of the id matched. No
    edit-distance library, no fuzzy threshold to tune.
    """
    provider, want = _id_shape(endpoint_id)
    if not want:
        return []
    want_set = set(want)
    # SAME PROVIDER ONLY. Two reasons, and the second is the load-bearing one. A suggestion is a
    # claim that the caller mistyped, and `apollo.people.email.find` is not a typo for
    # `hunter.people.email.find` — it is a different vendor, a different price and a different
    # credential. On a path that spends money, "did you mean <competitor>?" is provider routing
    # wearing a spellcheck's clothes, which is exactly what the charter says treg does not do:
    # treg compares providers side by side and the CALLER chooses. (The first cut of this scored
    # cross-provider matches and merely preferred the same provider — via `r[2] != provider`, which
    # compared a full id against a bare provider token and was therefore true every time, so even
    # that preference never applied. `fake.companies-signals` confidently answered
    # `lusha.x.companies-signals`.)
    if provider not in cat.provider_meta:
        return []
    exact, scored = [], []
    for eid, ep in cat.by_id.items():
        if ep.get("provider") != provider:
            continue
        _, have = _id_shape(eid)
        if not have:
            continue
        if have == want:      # the same id but for a tier marker — collect ALL, don't return the
            exact.append(eid)  # first: ids that normalise identically are equally good answers
            continue
        overlap = len(want_set & set(have))
        if not overlap or overlap < len(want_set) - 1:
            continue
        scored.append((-overlap, len(set(have) - want_set), eid))
    if exact:
        return sorted(exact)[:limit]
    scored.sort()
    return [eid for _, _, eid in scored[:limit]]


def unknown_id_hint(endpoint_id: str, cat: Catalog) -> str:
    """The one-line "so what do I do now" for an id that isn't in the catalog."""
    near = near_ids(endpoint_id, cat)
    if near:
        return "did you mean " + ", ".join(near) + "?"
    tail = endpoint_id.split(".")[-1].replace("-", " ").replace("_", " ")
    return f"search for it instead: treg catalog search {tail!r}"


# ---- search ----------------------------------------------------------------------------------
# Deliberately plain: token containment over a few weighted fields. The catalog is ~2k endpoints of
# curated English, so an embedding index would add a dependency and a build step to beat "tiktok
# comments" by nothing. Ranking has to be PREDICTABLE above all — an agent that can't guess why a row
# won can't refine its query.
_SPLIT = re.compile(r"[^a-z0-9]+")

# what a token hit is worth, by where it landed
W_CAPABILITY = 3   # capability id/description + platform label/slug — the curated vocabulary
W_SUMMARY = 2      # the endpoint's own one-line description
W_PATH = 1         # id, path, provider — a hit here is incidental as often as it is meaningful


def _tokens(text: str) -> list[str]:
    return [t for t in _SPLIT.split(text.lower()) if t]


# Pure function words. They carry no meaning about WHICH endpoint, but each one inflates the query's
# token count — and with it the number of real words a row must match to be admitted. "trending
# repositories on github this week" is a 3-word query wearing 6 tokens; dropping these makes the
# miss allowance apply to the words that matter. Never words that select ("open", "search", "list").
_STOPWORDS = frozenset("""a an and are as at be by can do does for from how i in is it its me my
    of on or that the this to was what when where which who will with you your""".split())


def _haystacks(ep: dict, cat: Catalog) -> list[tuple[int, str]]:
    plat = cat.platforms.get(ep["platform"], {})
    return [
        (W_CAPABILITY, " ".join((ep["capability"], cat.capabilities.get(ep["capability"], ""),
                                 plat.get("label", ""), ep["platform"])).lower()),
        # `name` is OURS to word (summary stays the provider's, verbatim) — it is the one
        # per-endpoint field curation may write search vocabulary into, so it must be searched
        (W_SUMMARY, " ".join((ep.get("name") or "", ep["summary"])).lower()),
        (W_PATH, " ".join((ep["id"], ep["path"], ep["provider"])).lower()),
    ]


def _search_fields(cat: Catalog) -> list[tuple[dict, list[tuple[int, str]]]]:
    """Every endpoint's weighted haystacks, built once per Catalog instance — search runs two
    passes (document frequency, then scoring) and rebuilding the joined strings per query per
    pass was the only real cost in either."""
    cached = cat._search_fields
    if cached is None:
        cached = [(ep, _haystacks(ep, cat)) for ep in cat.endpoints]
        object.__setattr__(cat, "_search_fields", cached)  # frozen dataclass, deliberate
    return cached


# A token matching more than this share of the catalog selects nothing — it may still add score
# where it matches, but a row is never punished for missing it. Statistical stopwords: "data" (33%),
# "api" (50%), "get" (40%) are corpus filler no hand list would keep up with, while "search" (22%)
# and "tiktok" (19%) stay required. Measured 2026-08-20 over 2,791 endpoints.
SOFT_DF_SHARE = 0.25


def _match(query: str, cat: Catalog):
    """The shared matching pass behind `search` and `near_misses`: tokens, per-row best-field
    weights, idf, and the admission gate (which tokens are required, and how many must hit)."""
    raw = _tokens(query)
    # single letters can never select ("K&L" tokenizes to k + l, df 2,039 and 2,512) — they only
    # inflated the number of matches a row needed elsewhere
    tokens = [t for t in raw if t not in _STOPWORDS and len(t) > 1] or raw
    if not tokens:
        return None
    variants = [[tok, *cat.aliases.get(tok, ())] for tok in tokens]
    # A token that IS a platform slug ("tiktok", "linkedin") is the caller's hard filter, but idf
    # prices it low — half the catalog serves the big platforms — so rows matching a rarer facet
    # word ("followers") outranked rows matching the asked-for platform. Double the weight where a
    # platform token matches: same-pattern rows still sum identical floats, ties survive.
    boost = [2 if tok in cat.platforms else 1 for tok in tokens]
    rows = _search_fields(cat)
    total = len(rows)
    best: list[list[int]] = []
    df = [0] * len(tokens)
    for _, fields in rows:
        per_tok = [b * max((w for w, text in fields if any(v in text for v in vs)), default=0)
                   for vs, b in zip(variants, boost)]
        best.append(per_tok)
        for i, w in enumerate(per_tok):
            if w:
                df[i] += 1
    idf = [math.log(1 + (total - d + 0.5) / (d + 0.5)) for d in df]
    required = [i for i, d in enumerate(df) if d <= SOFT_DF_SHARE * total] \
        or list(range(len(tokens)))
    need = len(required) - len(required) // 3
    return tokens, rows, best, idf, required, need


def search(query: str, cat: Catalog, limit: int = 25) -> tuple[list[tuple[dict, float]], int]:
    """`(ranked [(endpoint, score)], total_matches)` for a free-text query.

    MOST tokens must match — a query is a refinement, so "tiktok comments" must not return every
    tiktok endpoint, but agents write sentences, and demanding every word zeroed real queries on
    their filler: "company job postings hiring open jobs linkedin" found nothing while three
    endpoints matched 6 of its 7 words (see models.SearchMiss — the log this rule was read from).
    A query may miss one token in every three (2 words → both required, unchanged; 7 → 5 must hit).

    Each matched token scores its best field weight times its BM25 idf, so a word that matches half
    the catalog ("by": 558 endpoints) is worth almost nothing and a rare one ("postings": 4) decides
    the order — which is also what keeps the miss allowance safe: dropping a rare token costs more
    score than dropping filler, so a 7-of-7 fluff match cannot outrank a 6-of-7 match on substance.
    Rows matching the same tokens in the same fields still tie EXACTLY (same floats from the same
    sums), which `rank_band`'s tie sweep and the evidence rerank both depend on. Remaining ties
    break to core-before-extended, then verified-before-not, then id — total and stable.
    """
    m = _match(query, cat)
    if m is None:
        return [], 0
    tokens, rows, best, idf, required, need = m
    scored: list[tuple[dict, float]] = []
    for (ep, _), per_tok in zip(rows, best):
        if sum(1 for i in required if per_tok[i]) < need:
            continue
        scored.append((ep, round(sum(w * idf[i] for i, w in enumerate(per_tok)), 4)))
    scored.sort(key=lambda row: (-row[1], row[0]["tier"] != "core", not row[0]["verified"], row[0]["id"]))
    return scored[:max(limit, 0)], len(scored)


def near_misses(query: str, cat: Catalog, limit: int = 3) -> list[dict]:
    """The rows just under the admission gate, and WHICH words they miss — for zero-result answers.

    A zero-result search already computed which endpoints matched everything but one or two words;
    throwing that away and answering with prose was the least useful thing the data allowed. The
    caller is usually an LLM: told "apollo.companies.jobs matches job, hiring, signal; misses law,
    firm", it re-queries correctly on the next call. Only meaningful when `search` returned 0."""
    m = _match(query, cat)
    if m is None:
        return []
    tokens, rows, best, idf, required, need = m
    cand = []
    for (ep, _), per_tok in zip(rows, best):
        got = sum(1 for i in required if per_tok[i])
        if 0 < got < need:
            mass = sum(idf[i] * w for i, w in enumerate(per_tok))
            cand.append((mass, ep, per_tok))
    cand.sort(key=lambda c: (-c[0], c[1]["tier"] != "core", c[1]["id"]))
    return [{
        "endpoint_id": ep["id"],
        "matches": [tokens[i] for i, w in enumerate(per_tok) if w],
        "missing": [tokens[i] for i in required if not per_tok[i]],
    } for _, ep, per_tok in cand[:max(limit, 0)]]


# The ceiling on how many equal-scoring rows the evidence sort may consider. Scoring is token
# containment over three fields, so ties are the NORM, not the exception: "ad library" scores every
# one of its 24 matches a 6, and the answer to "which 8 do I show?" was then file order.
#
# Sized against the real catalog rather than guessed. The tie group straddling a default limit of 8
# is 17 rows for "ad library", 24 for "email", 88 for "company", 127 for "backlinks" — but 523 for
# the bare word "tiktok". Taking the whole group unconditionally would put a 523-id `IN` clause
# behind every search on an OPEN, unauthenticated route, so the group is taken whole up to this
# ceiling and the caller is TOLD when it was not (see `rank_band`) — a bounded cut that announces
# itself, rather than the silent one this fix set out to remove.
RERANK_BAND = 250


def rank_band(query: str, cat: Catalog, limit: int) -> tuple[list[tuple[dict, float]], int, bool]:
    """`(rows, total_matches, tie_truncated)` — the candidates the evidence sort gets to reorder.

    Takes `limit` rows, then keeps taking while the score stays equal to the last one kept: a cut
    made *inside* a group of equally relevant rows is the arbitrary cut, and reranking a slice that
    already dropped the best-measured row cannot put it back. `tie_truncated` is true when the group
    ran past `RERANK_BAND` and the evidence therefore did not get to see all of it.
    """
    rows, total = search(query, cat, max(limit, 0))
    if not rows or len(rows) >= total:
        return rows, total, False
    # One row PAST the ceiling, so "was the group actually cut?" is observed rather than inferred.
    # Inferring it from `len(kept) >= RERANK_BAND` cried truncation whenever the group happened to
    # fill the ceiling exactly — telling the caller to narrow a query that had in fact been ranked
    # in full.
    # The ceiling bounds the EXTRA rows the tie sweep pulls in; it must never return fewer than the
    # caller asked for. (Shipped surfaces clamp to 100 and 25, so this was unreachable in
    # production — but a helper that silently under-delivers is a trap for the next call site.)
    ceiling = max(limit, RERANK_BAND)
    wider, _ = search(query, cat, ceiling + 1)
    edge = rows[-1][1]
    group = [r for r in wider if r[1] >= edge]
    kept = group[:ceiling]
    return kept, total, len(group) > len(kept)


def rerank(rows: list[tuple[dict, float]], stats: dict[str, dict],
           cat: Catalog | None = None) -> list[tuple[dict, float]]:
    """Re-order equal-scoring rows by what treg has MEASURED, then by price.

    Relevance still decides first — a cheap reliable endpoint that doesn't do the job is not the
    answer. But below that, treg is the only party that can rank on evidence, and until this existed
    it collected WORKS/SPEED/COST and then spent none of it at the step where the agent actually
    chooses. `catalog_search "ad library"` returned seven tikhub rows — one of them uncallable — and
    dropped `scrapecreators…ad-library-search`, which is cheaper and had answered 16 of 16.

    Buckets, not a formula: an ok_rate and a price are different units, and a weighted sum of the two
    is a number nobody can predict or argue with. Unmeasured sits ABOVE measured-poor and below
    measured-good — a new endpoint is an unknown, not a suspect.
    """
    def bucket(ep: dict) -> int:
        rate = (stats.get(ep["id"]) or {}).get("ok_rate")
        if rate is None:
            return 1        # no evidence either way (or too few samples to publish one)
        # `ok_rate` is computed from DECIDED samples only — 2xx against 5xx, with 4xx excluded as
        # the caller's fault — so a 0 here means the provider failed every call that was its to
        # answer. Reading "never worked" off anything wider (an earlier revision used a
        # sample-count-based `any_ok`) demotes a healthy endpoint the moment one agent sends bad
        # parameters, which is the failure `endpoint_stats`' 4xx rule exists to prevent.
        if rate == 0:
            return 3
        return 0 if rate >= 0.9 else 2

    def price(ep: dict) -> float:
        cost = (cat.cost_view(ep.get("cost"), ep.get("provider")) if cat else ep.get("cost")) or {}
        usd = cost.get("usd")
        return float(usd) if isinstance(usd, (int, float)) else float("inf")

    # Evidence outranks curation — a core row that has never once answered is not the better
    # suggestion — but curation outranks PRICE. `core` is the hand-picked route for a job and
    # `extended` the bulk-ingested long tail, so letting a tenth of a cent promote the long tail
    # over the curated answer trades relevance for change: "tiktok comments" would lead with
    # douyin danmaku because it is cheap.
    return sorted(rows, key=lambda row: (
        -row[1], bucket(row[0]), row[0]["tier"] != "core", price(row[0]),
        not row[0]["verified"], row[0]["id"]))


# ---- call template ---------------------------------------------------------------------------
def _placeholder(spec: dict):
    example = spec.get("example")
    if example not in (None, ""):
        # Keep structured examples structured until the wire encoder sees them. Stringifying a
        # list here produced Python repr (`['US']`) before the JSON/comma metadata had any chance
        # to choose its representation; booleans likewise became capitalized Python `True`.
        return example
    return f"<{spec.get('type') or 'value'}>"


def wire_value(value) -> str:
    """One scalar query value in its canonical HTTP spelling, shared by MCP and command hints."""
    if isinstance(value, bool):      # before int — bool IS an int in Python
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def query_values(ep: dict | None, name: str, value) -> list[str]:
    """Encode one structured query value according to the endpoint's declared wire contract.

    A Python/MCP list does not identify an HTTP representation: Meta expects one JSON array, while
    ordinary repeated keys remain useful for team tools and catalog endpoints. An endpoint may
    declare one exceptional encoding when all its array parameters share that contract; absence
    preserves the established repeated-key behavior.
    """
    if not isinstance(value, (list, tuple)):
        return [wire_value(value)]
    inp = (ep or {}).get("input") or {}
    encoding = inp.get("queryArrayEncoding") or "repeated"
    if encoding == "json":
        return [wire_value(value)]
    if encoding == "comma":
        return [",".join(wire_value(item) for item in value if item is not None)]
    return [wire_value(item) for item in value if item is not None]


def _required_examples(params) -> dict:
    """Required params only, valued by their documented example (else a typed placeholder). Optional
    params are omitted — the template is the SHORTEST call that works, not a parameter reference."""
    if not isinstance(params, dict):
        return {}
    return {k: _placeholder(v) for k, v in params.items()
            if isinstance(v, dict) and v.get("required")}


def call_template(ep: dict) -> str:
    """A paste-ready `treg call …` line for this endpoint.

    Values come from `test_request` first — that request was actually run against the live API on the
    verification date, so the line is known-good rather than merely well-shaped.

    The target is the ENDPOINT ID, not `<provider> <path>`: the id form works with no registered
    tool (the server walks the marketplace credential ladder), and path `{placeholders}` ride as
    `--query` params the server substitutes — so the line is copy-paste-true for everyone.
    """
    inp = ep.get("input") or {}
    test = ep.get("test_request") or {}

    parts = ["treg call", ep["id"]]
    if ep["method"] != "GET":
        parts += ["--method", ep["method"]]
    # Path params first (the server folds them into the path), then query params proper.
    for name, value in {**_required_examples(inp.get("pathParams")), **(test.get("pathParams") or {})}.items():
        parts += ["--query", shlex.quote(f"{name}={wire_value(value)}")]
    for name, value in {**_required_examples(inp.get("queryParams")), **(test.get("queryParams") or {})}.items():
        for encoded in query_values(ep, name, value):
            # Quote the COMPLETE argv token, not just its value. `name=['US']` lost its inner
            # quotes after shell parsing and became either an unmatched glob (zsh) or `[US]`;
            # `name=two words` split into two arguments. shlex.quote makes the printed command the
            # same request the structured MCP path sends.
            parts += ["--query", shlex.quote(f"{name}={encoded}")]

    body = test.get("body") if test.get("body") is not None else (_required_examples(inp.get("body")) or None)
    # `is not None`, not truthiness: `body: {}` is a real stored test request — a POST that takes no
    # arguments but still requires a JSON body — and dropping `--data '{}'` from the line hands the
    # reader a command that differs from the one that was tested, on handlers that reject an empty
    # body outright.
    if body is not None and (body or ep["method"] != "GET"):
        parts += ["--data", shlex.quote(json.dumps(
            body, separators=(",", ":"), ensure_ascii=False))]
    return " ".join(parts)


def _input_hint(ep: dict) -> str:
    """The one thing a stamped example can't show from method+path: what to send."""
    inp = ep.get("input") or {}
    note = (inp.get("note") or "").strip()
    required: list[str] = []
    for where in ("pathParams", "queryParams", "body"):
        params = inp.get(where)
        if isinstance(params, dict):
            required += [k for k, v in params.items() if isinstance(v, dict) and v.get("required")]
    bits = []
    if required:
        bits.append("required: " + ", ".join(required))
    if note:
        bits.append(note)
    return "; ".join(bits)


def tool_examples(service: str) -> list[dict]:
    """Catalog knowledge in the shape `Tool.examples` already uses — `{method, path, note}`.

    Only VERIFIED core endpoints: a tool's examples are a promise that the call works, and the
    catalog's `verified` date is the only evidence we have that it does.
    """
    out = []
    for ep in load().for_provider(service):
        if ep["tier"] != "core" or not ep["verified"] or not ep["path"]:
            continue
        note = ep["summary"] or ep["id"]
        hint = _input_hint(ep)
        if hint:
            note = f"{note}. {hint}"
        if ep["capability"]:
            note = f"{note} [capability: {ep['capability']}]"
        out.append({"method": ep["method"], "path": ep["path"], "note": note})
    return out
