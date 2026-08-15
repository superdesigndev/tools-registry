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
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATALOG_DIR = Path(__file__).parent / "catalog"
EXAMPLES_DIRNAME = "examples"
CAPABILITIES_FILE = "capabilities.yaml"
FX_FILE = "fx.yaml"

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
              "api_unit", "analysis_unit", "retrieval_unit", "index_item_unit", "quota_row")
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
    # provider service -> meter name -> USD per one unit of that meter (fx.yaml `unit_rates_usd`).
    # Providers that bill in a proprietary meter (Semrush API units, Majestic's three pools, Moz's
    # row quota) get one rate per meter — a provider can have several, and they do not convert
    # into each other any more than two providers' credits do.
    unit_rates: dict[str, dict[str, float | None]] = field(default_factory=dict)
    platforms: dict[str, dict] = field(default_factory=dict)     # slug -> {label, category}
    capabilities: dict[str, str] = field(default_factory=dict)   # id -> description
    endpoints: list[dict] = field(default_factory=list)          # normalized endpoint dicts
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
        return {**cost, "usd": usd}

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
        if path.name in (CAPABILITIES_FILE, FX_FILE):
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
            endpoints.append(ep)

    for ep in endpoints:  # a platform seen only in provider files still deserves a label
        platforms.setdefault(ep["platform"], {"label": ep["platform"], "category": "Other"})
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
    # Same shape one level deeper: service -> meter -> {usd, basis, source, checked}. A provider
    # bills in several meters at once (Majestic has three), so the meter name is part of the key.
    unit_rates = {
        str(service): {str(meter): (v.get("usd") if isinstance(v, dict) else v)
                       for meter, v in (meters or {}).items()}
        for service, meters in (fx_doc.get("unit_rates_usd") or {}).items()
    }
    return Catalog(fx=fx, credit_rates=credit_rates, unit_rates=unit_rates,
                   shared_plans=shared_plans, platforms=platforms,
                   capabilities=capabilities, endpoints=endpoints, by_id=by_id,
                   provider_meta=provider_meta)


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
        # "no match" semantics, when the endpoint has them — an agent that reads `miss` stops
        # treating an expected empty answer as a failed call (and stops retrying it).
        "miss": ep.get("miss"),
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


def _haystacks(ep: dict, cat: Catalog) -> list[tuple[int, str]]:
    plat = cat.platforms.get(ep["platform"], {})
    return [
        (W_CAPABILITY, " ".join((ep["capability"], cat.capabilities.get(ep["capability"], ""),
                                 plat.get("label", ""), ep["platform"])).lower()),
        (W_SUMMARY, ep["summary"].lower()),
        (W_PATH, " ".join((ep["id"], ep["path"], ep["provider"])).lower()),
    ]


def search(query: str, cat: Catalog, limit: int = 25) -> tuple[list[tuple[dict, int]], int]:
    """`(ranked [(endpoint, score)], total_matches)` for a free-text query.

    EVERY token must match somewhere (AND, not OR): a query is a refinement, so "tiktok comments"
    must not return every tiktok endpoint. Score sums each token's best field weight; ties break to
    core-before-extended, then verified-before-not, then id — so the ordering is total and stable.
    """
    tokens = _tokens(query)
    if not tokens:
        return [], 0
    scored: list[tuple[dict, int]] = []
    for ep in cat.endpoints:
        fields = _haystacks(ep, cat)
        score = 0
        for tok in tokens:
            best = max((w for w, text in fields if tok in text), default=0)
            if not best:
                break
            score += best
        else:
            scored.append((ep, score))
    scored.sort(key=lambda row: (-row[1], row[0]["tier"] != "core", not row[0]["verified"], row[0]["id"]))
    return scored[:max(limit, 0)], len(scored)


# ---- call template ---------------------------------------------------------------------------
def _placeholder(spec: dict) -> str:
    example = spec.get("example")
    if example not in (None, ""):
        return str(example)
    return f"<{spec.get('type') or 'value'}>"


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
        parts += ["--query", f"{name}={value}"]
    for name, value in {**_required_examples(inp.get("queryParams")), **(test.get("queryParams") or {})}.items():
        parts += ["--query", f"{name}={value}"]

    body = test.get("body") if test.get("body") is not None else (_required_examples(inp.get("body")) or None)
    if body:
        # single-quoted so the JSON's own quotes survive a paste into any POSIX shell
        parts += ["--data", "'" + json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "'"]
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
