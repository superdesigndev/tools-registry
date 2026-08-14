"""The endpoint catalog served over HTTP (`/catalog/*`) + stamped onto provisioned tools.

The data lives in src/treg/catalog/*.yaml (see docs/context/architecture/catalog.md). These tests
assert the API CONTRACT the dashboard and `treg catalog` are written against, plus the two things
the loader has to get right on its own: merging a provider's `<service>.extended.yaml` into the same
provider, and never letting a caller-supplied id reach the filesystem.
"""

from __future__ import annotations

import dataclasses
import json

from httpx import AsyncClient

from treg import catalog_store as cs
from treg import oauth_providers as P


# ---- platform listing --------------------------------------------------------------------
async def test_platforms_lists_the_curated_shelves_busiest_first(clients: AsyncClient):
    r = await clients.get("/catalog/platforms")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated_from"] == "catalog"
    rows = {p["slug"]: p for p in body["platforms"]}
    assert {"tiktok", "web"} <= set(rows), rows.keys()

    tiktok = rows["tiktok"]
    assert tiktok["label"] == "TikTok"
    assert tiktok["endpoints"] >= 5
    assert 0 < tiktok["capabilities"] <= tiktok["endpoints"]
    assert 0 < tiktok["verified"] <= tiktok["endpoints"]
    assert {"tikhub", "justoneapi"} <= set(tiktok["providers"]), "the social overlap pair is the point"
    assert {"dataforseo", "moz", "linkup"} <= set(rows["web"]["providers"])

    counts = [p["endpoints"] for p in body["platforms"]]
    assert counts == sorted(counts, reverse=True)


async def test_platform_listing_hides_taxonomy_entries_nobody_implements(clients: AsyncClient):
    rows = (await clients.get("/catalog/platforms")).json()["platforms"]
    assert all(p["endpoints"] > 0 for p in rows)


async def test_linkup_exposes_search_answer_structured_and_fetch(clients: AsyncClient):
    body = (await clients.get("/catalog/platforms/web")).json()
    caps = {c["id"]: c for c in body["capabilities"]}
    assert {
        "web.search.results", "web.search.answer", "web.search.structured", "web.page.fetch",
    } <= set(caps)
    endpoints = {
        e["id"]: e for capability in caps.values() for e in capability["endpoints"]
        if e["provider"] == "linkup"
    }
    assert set(endpoints) == {
        "linkup.web.search.results",
        "linkup.web.search.answer",
        "linkup.web.search.structured",
        "linkup.web.page.fetch",
    }
    assert all(e["platform_eligible"] is False for e in endpoints.values())


# ---- platform detail ---------------------------------------------------------------------
async def test_platform_detail_groups_the_same_job_across_providers(clients: AsyncClient):
    """The capability is the join key: one row, both providers — that's what makes the catalog
    comparable rather than four separate provider docs."""
    r = await clients.get("/catalog/platforms/tiktok")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == {"slug": "tiktok", "label": "TikTok", "category": "Social"}

    caps = {c["id"]: c for c in body["capabilities"]}
    assert [c["id"] for c in body["capabilities"]] == sorted(caps), "capabilities are sorted by id"
    profile = caps["tiktok.user.profile"]
    assert profile["description"]
    assert {e["provider"] for e in profile["endpoints"]} >= {"tikhub", "justoneapi"}

    ep = next(e for e in profile["endpoints"] if e["provider"] == "tikhub")
    assert set(ep) == {"id", "provider", "provider_display", "name", "summary", "method", "path",
                       "scope", "tier", "kind", "domain", "call_template", "cost", "verified", "docs_url",
                       "has_example", "input", "platform_eligible", "test_request", "miss"}
    assert ep["kind"] == "data", "an endpoint with no explicit kind is data (the browse surface)"
    assert ep["provider_display"] == P.get("tikhub").display_name
    assert ep["method"] == "GET" and ep["path"].startswith("/")
    assert ep["tier"] == "core", "an endpoint with no explicit tier is core, not hidden"
    assert ep["verified"] and ep["has_example"] is True
    assert ep["cost"]["type"] == "per_success"


async def test_every_listed_endpoint_carries_its_platform(clients: AsyncClient):
    for slug in ("web", "google", "tiktok"):
        body = (await clients.get(f"/catalog/platforms/{slug}")).json()
        ids = {e["id"] for c in body["capabilities"] for e in c["endpoints"]}
        ids |= {e["id"] for e in body["extended"]}
        # the default view is the BROWSE surface — account/utility plumbing is served only under
        # ?include_hidden=1, so the listing equals the platform's data + action endpoints
        assert ids == {e["id"] for e in cs.load().for_platform(slug)
                       if e["kind"] not in cs.HIDDEN_KINDS}


# ---- platform detail: the domain ledger the dashboard renders -----------------------------
async def test_the_ledger_files_every_row_under_a_domain_with_other_last(clients: AsyncClient):
    """A platform page is ONE table sectioned by subject. `other` is the junk drawer, so it is the
    one section whose position carries meaning — it can never outrank a real subject, however big
    it gets. The rest run busiest-first, because the biggest section is what a visitor came for."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    doms = body["domains"]
    assert [d["domain"] for d in doms].count("other") == 1
    assert doms[-1]["domain"] == "other", "the junk drawer sorts last no matter its size"
    sizes = [len(d["rows"]) for d in doms[:-1]]
    assert sizes == sorted(sizes, reverse=True)

    # every browse-surface endpoint appears exactly once, in exactly one section (the default
    # ledger drops account/utility plumbing — see the hidden-count test below)
    ids = [e["id"] for d in doms for r in d["rows"] for e in r["endpoints"]]
    assert sorted(ids) == sorted(e["id"] for e in cs.load().for_platform("tiktok")
                                 if e["kind"] not in cs.HIDDEN_KINDS)
    assert len(ids) == len(set(ids))


async def test_a_section_leads_with_the_jobs_several_providers_do(clients: AsyncClient):
    """Merged rows first: a comparable job is worth more than a lone route, and burying the
    comparison under fifty single endpoints is how the old page hid it."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    for section in body["domains"]:
        kinds = [r["kind"] for r in section["rows"]]
        assert kinds == sorted(kinds, key=lambda k: k != "merged"), section["domain"]

    user = next(d for d in body["domains"] if d["domain"] == "user")
    merged = next(r for r in user["rows"] if r["capability"] == "tiktok.user.profile")
    assert merged["kind"] == "merged"
    assert merged["description"] == "Get a user's public profile", "the capability's plain-English job"
    assert len({e["provider"] for e in merged["endpoints"]}) > 1, "one provider is not a comparison"


async def test_a_single_row_is_led_by_the_endpoints_own_name(clients: AsyncClient):
    """A row only one provider serves is led by the endpoint's curated `name` ("Get Showcase Product
    List" says more than `tiktok.shop.showcase` ever could), falling back to its `summary` until a
    name is written. `summary` is documentation prose — some of it runs to a paragraph — so it is a
    fallback for the row and the description in the expansion, never the preferred title."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    rows = [r for d in body["domains"] for r in d["rows"] if r["kind"] == "single"]
    assert rows
    for row in rows:
        # exactly one endpoint: folding a provider's two takes on one job into a single row would
        # show one route next to the OTHER one's price
        assert len(row["endpoints"]) == 1, row["capability"]
        ep = row["endpoints"][0]
        assert row["description"] == (ep["name"] or ep["summary"] or row["capability"])


def test_a_curated_name_beats_the_summary_on_a_row(tmp_path):
    """The founder's fix for DataForSEO rows rendering doc paragraphs as titles: where a `name:` is
    written, it wins, and the prose stays in the expansion."""
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "dataforseo.extended.yaml").write_text(
        "provider: dataforseo\nendpoints:\n"
        "  - id: dataforseo.x.named\n    platform: web\n    tier: extended\n    method: POST\n"
        "    path: /v3/backlinks/anchors/live\n    name: Anchor text overview\n"
        "    summary: This endpoint will provide you with a detailed overview of anchors used when "
        "linking to the specified website with relevant backlink data for each of them.\n"
        "  - id: dataforseo.x.unnamed\n    platform: web\n    tier: extended\n    method: POST\n"
        "    path: /v3/backlinks/domain_pages/live\n    summary: Domain pages with backlink data\n")
    cat = cs.load(directory=tmp_path)
    pairs = [(e, cs.endpoint_view(e, e["provider"], cat)) for e in cat.endpoints]
    rows = {r["endpoints"][0]["id"]: r for s in cs.domain_rows(pairs, cat.capabilities) for r in s["rows"]}
    assert rows["dataforseo.x.named"]["description"] == "Anchor text overview"
    assert rows["dataforseo.x.unnamed"]["description"] == "Domain pages with backlink data"


async def test_every_endpoint_carries_a_domain_and_a_call_line(clients: AsyncClient):
    """The domain decides which section a row files under, and the `treg call` line is the reason
    the row exists — both ride on the row rather than costing a second request."""
    for slug in ("tiktok", "web"):
        body = (await clients.get(f"/catalog/platforms/{slug}")).json()
        eps = [e for c in body["capabilities"] for e in c["endpoints"]] + body["extended"]
        assert eps
        for e in eps:
            assert e["domain"] and e["domain"] == e["domain"].lower().strip()
            # The endpoint-ID form: callable with no registered tool (marketplace credential ladder).
            assert e["call_template"].startswith(f"treg call {e['id']}")


async def test_the_ledger_ships_the_provider_wide_facts_once(clients: AsyncClient):
    """Limits and the rate card live once at the top of a provider's yaml. An expanded row needs
    them, and copying them onto 2,000 endpoint rows to deliver them would be absurd."""
    body = (await clients.get("/catalog/platforms/tiktok")).json()
    assert set(body["providers"]) == {e["provider"] for e in cs.load().for_platform("tiktok")}
    tikhub = body["providers"]["tikhub"]
    assert tikhub["display_name"] == P.get("tikhub").display_name
    assert tikhub["docs"]
    assert body["providers"]["justoneapi"]["pricing_url"]


def test_the_domain_is_curation_first_then_the_taxonomy_then_a_guess(tmp_path):
    """Three sources, best first. An explicit `domain:` always wins; otherwise the capability id's
    middle segment, which the taxonomy already encodes; and only then a keyword read off the path,
    which is all an unmapped extended endpoint has to offer."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {tiktok: TikTok}\ncapabilities: {tiktok.video.comments: Comments}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\nendpoints:\n"
        "  - id: tikhub.tiktok.video.comments\n    capability: tiktok.video.comments\n"
        "    platform: tiktok\n    method: GET\n    path: /a\n"
        "  - id: tikhub.tiktok.override\n    platform: tiktok\n    domain: shop\n"
        "    method: GET\n    path: /api/v1/tiktok/web/fetch_user_profile\n"
        "  - id: tikhub.tiktok.guessed\n    platform: tiktok\n    method: GET\n"
        "    path: /api/v1/tiktok/shop/web/fetch_product_detail\n    summary: Product detail\n"
        "  - id: tikhub.tiktok.nothing\n    platform: tiktok\n    method: GET\n    path: /x\n")
    by_id = cs.load(directory=tmp_path).by_id
    assert by_id["tikhub.tiktok.video.comments"]["domain"] == "video"
    assert by_id["tikhub.tiktok.override"]["domain"] == "shop", "curation beats the keyword guess"
    assert by_id["tikhub.tiktok.guessed"]["domain"] == "shop"
    assert by_id["tikhub.tiktok.nothing"]["domain"] == "other"


def test_a_delivery_mode_in_the_path_is_not_a_subject(tmp_path):
    """DataForSEO ends every synchronous route in `/live`, which read as a subject and filed 33
    unrelated SEO endpoints under a "live" heading. A mode, a version and a format segment all say
    how a route is CALLED — the grouping segment before them is what it is about."""
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "dataforseo.extended.yaml").write_text(
        "provider: dataforseo\nendpoints:\n"
        "  - id: dataforseo.x.anchors\n    platform: web\n    tier: extended\n    method: POST\n"
        "    path: /v3/backlinks/anchors/live\n    summary: Live anchors overview\n"
        "  - id: dataforseo.x.lonely\n    platform: web\n    tier: extended\n    method: GET\n"
        "    path: /v3/status\n    summary: Live API status\n")
    by_id = cs.load(directory=tmp_path).by_id
    assert by_id["dataforseo.x.anchors"]["domain"] == "backlinks"
    # ...and a path with no grouping segment left is honest about it: the last segment is the
    # OPERATION, and a section per operation is not a section at all.
    assert by_id["dataforseo.x.lonely"]["domain"] == "other"


async def test_unknown_platform_is_404(clients: AsyncClient):
    r = await clients.get("/catalog/platforms/myspace")
    assert r.status_code == 404
    assert "myspace" in r.text


async def test_management_endpoints_are_hidden_from_counts_and_the_default_view(
        clients: AsyncClient, monkeypatch, tmp_path):
    """`kind: account`/`utility` are real inventory but PLUMBING (webhooks, saved lists, token
    helpers). They must never inflate the census counts, and the default platform view leaves them
    out — the browse surface is data + action. `?include_hidden=1` returns the whole surface, each
    endpoint carrying its `kind` so a client can file the plumbing behind its own expander."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {web: Web}\ncapabilities: {web.backlinks.summary: Backlinks}\n")
    (tmp_path / "moz.yaml").write_text(
        "provider: moz\nsource: {docs: https://moz.com}\nendpoints:\n"
        "  - id: moz.web.backlinks.summary\n    capability: web.backlinks.summary\n"
        "    platform: web\n    method: POST\n    path: /d\n    kind: data\n"
        "  - id: moz.web.post\n    platform: web\n    method: POST\n    path: /a\n    kind: action\n"
        "  - id: moz.web.list.create\n    platform: web\n    method: POST\n    path: /acc\n    kind: account\n"
        "  - id: moz.web.token\n    platform: web\n    method: GET\n    path: /util\n    kind: utility\n")
    cat = cs.load(directory=tmp_path)
    monkeypatch.setattr(cs, "load", lambda *a, **k: cat)

    # census: only data + action count — never the account/utility plumbing
    census = {p["slug"]: p for p in (await clients.get("/catalog/platforms")).json()["platforms"]}
    assert census["web"]["endpoints"] == 2, "management endpoints do not inflate the tile count"

    # default platform detail: management endpoints are absent from every shape it returns
    body = (await clients.get("/catalog/platforms/web")).json()
    ids = ({e["id"] for c in body["capabilities"] for e in c["endpoints"]}
           | {e["id"] for e in body["extended"]}
           | {e["id"] for d in body["domains"] for r in d["rows"] for e in r["endpoints"]})
    assert ids == {"moz.web.backlinks.summary", "moz.web.post"}
    assert body["hidden_count"] == 2, "but the page is told how many were set aside"

    # ?include_hidden=1: the whole surface comes back, each endpoint carrying its kind
    full = (await clients.get("/catalog/platforms/web?include_hidden=1")).json()
    shown = {e["id"]: e for d in full["domains"] for r in d["rows"] for e in r["endpoints"]}
    assert set(shown) == {"moz.web.backlinks.summary", "moz.web.post",
                          "moz.web.list.create", "moz.web.token"}
    assert shown["moz.web.list.create"]["kind"] == "account"
    assert shown["moz.web.token"]["kind"] == "utility"
    assert full["hidden_count"] == 2


# ---- search ------------------------------------------------------------------------------
async def test_search_finds_the_job_across_providers_best_first(clients: AsyncClient):
    """The discover half of the loop: an agent knows the JOB ("tiktok comments"), not the shelf it
    sits on. Both providers implementing it must come back, core+verified ahead of the long tail."""
    r = await clients.get("/catalog/search", params={"q": "tiktok comments"})
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["results"]
    assert body["count"] == len(rows) <= body["total"]

    top = [e["id"] for e in rows[:3]]
    assert set(top) == {
        "justoneapi.tiktok.video.comments",
        "tikhub.tiktok.video.comments",
        "scrapecreators.tiktok.video.comments",
    }
    assert all(e["tier"] == "core" and e["verified"] for e in rows[:3])
    # rank is total and stable: score desc, then core before extended, then verified before not
    scores = [e["score"] for e in rows]
    assert scores == sorted(scores, reverse=True)
    assert [e["tier"] for e in rows] == sorted((e["tier"] for e in rows), key=lambda t: t != "core")

    first = rows[0]
    assert first["capability"] == "tiktok.video.comments" and first["capability_description"]
    assert first["platform"] == "tiktok" and first["platform_label"] == "TikTok"
    assert first["provider_display"] == P.get(first["provider"]).display_name
    assert first["cost"]["usd"] is not None, "a search row prices in one currency or comparison is fiction"
    assert any(h.startswith(f"treg catalog get {first['id']}") for h in body["hints"])


async def test_search_requires_every_token_to_match(clients: AsyncClient):
    """AND, not OR — a second word is a refinement, so it must be able to shrink the result set."""
    broad = (await clients.get("/catalog/search", params={"q": "tiktok", "limit": 100})).json()
    narrow = (await clients.get("/catalog/search", params={"q": "tiktok comments", "limit": 100})).json()
    assert 0 < narrow["total"] < broad["total"]
    for e in narrow["results"]:  # the second token really is a filter, not a scoring nudge
        haystack = " ".join((e["id"], e["summary"], e["capability"], e["capability_description"])).lower()
        assert "comment" in haystack, e["id"]

    empty = (await clients.get("/catalog/search", params={"q": "tiktok flibbertigibbet"})).json()
    assert empty["total"] == 0 and empty["results"] == []
    assert empty["hints"], "a dead end still has to say what to try next"


async def test_search_respects_limit_and_an_empty_query(clients: AsyncClient):
    body = (await clients.get("/catalog/search", params={"q": "tiktok", "limit": 3})).json()
    assert len(body["results"]) == 3 and body["total"] > 3
    assert any("more matches" in h for h in body["hints"])
    assert (await clients.get("/catalog/search", params={"q": "  "})).json()["results"] == []


# ---- endpoint detail -----------------------------------------------------------------------
async def test_endpoint_detail_answers_everything_in_one_call(clients: AsyncClient):
    """The inspect half: params, price, the captured response and a paste-ready command — plus the
    OTHER providers doing the same job, so comparing costs never needs a second request."""
    r = await clients.get("/catalog/endpoints/justoneapi.tiktok.video.comments")
    assert r.status_code == 200, r.text
    body = r.json()
    e = body["endpoint"]
    assert e["id"] == "justoneapi.tiktok.video.comments"
    assert e["capability"] == "tiktok.video.comments" and e["platform_label"] == "TikTok"
    assert e["input"]["queryParams"], "the detail view's whole job is showing what to SEND"

    assert body["provider"]["service"] == "justoneapi"
    assert body["provider"]["pricing_url"], "provider-wide facts live once at the top of the yaml file"

    sibs = {s["id"] for s in body["siblings"]}
    assert "tikhub.tiktok.video.comments" in sibs
    assert e["id"] not in sibs, "an endpoint is not its own sibling"

    tmpl = body["call_template"]
    assert tmpl.startswith("treg call justoneapi.tiktok.video.comments")
    assert "--query awemeId=" in tmpl, "the verifier's proven request is what makes it paste-ready"
    assert isinstance(body["example_response"], (dict, list)), "inline, so parsing needs no second call"


async def test_call_template_carries_method_and_body_for_a_post(clients: AsyncClient):
    body = (await clients.get("/catalog/endpoints/dataforseo.web.backlinks.summary")).json()
    tmpl = body["call_template"]
    assert tmpl.startswith("treg call dataforseo.web.backlinks.summary --method POST")
    assert "--data '[{\"target\":\"moz.com\"" in tmpl, "single-quoted JSON survives a shell paste"


async def test_a_credit_price_is_served_in_usd_per_provider(clients: AsyncClient):
    """A "credit" is a provider-scoped unit, not a currency: each provider converts at its OWN
    fx.yaml rate ($0.00188 on scrapecreators, $0.026 on apollo), and a provider with no rate
    stays native — "we don't know" must never surface as a dollar figure."""
    priced = (await clients.get("/catalog/endpoints/scrapecreators.x.v1-amazon-shop")).json()["endpoint"]["cost"]
    assert priced["currency"] == "credit" and priced["value"]
    assert priced["usd"] == round(priced["value"] * cs.load().credit_rates["scrapecreators"], 6)
    assert priced["usd"] > 0, "a credit rate exists, so the dashboard gets a comparable number"

    apollo = (await clients.get("/catalog/endpoints/apollo.people.enrich")).json()["endpoint"]["cost"]
    assert apollo["currency"] == "credit" and apollo["value"]
    assert apollo["usd"] == round(apollo["value"] * cs.load().credit_rates["apollo"], 6)
    assert apollo["usd"] != priced["usd"] or apollo["value"] != priced["value"], \
        "two providers' credits are unrelated units, priced by their own rates"

    unrated = cs.load().cost_view({"type": "per_call", "value": 3, "currency": "credit"}, "no-such-provider")
    assert unrated["usd"] is None, "no published rate: display credits, never a guessed dollar"


# ---- cost: units, provenance and platform eligibility ----------------------------------------
async def test_per_divides_so_a_cpm_price_serves_per_row():
    """A provider that quotes dollars per 1,000 rows must not serve as dollars per row. `per` is what
    makes "$2.00 per 1,000" and "$0.002 each" the same fact instead of a 1,000x error."""
    cat = cs.load()
    ep = cat.by_id["spyfu.google.domain.paid_keywords"]
    cost = cat.cost_view(ep["cost"], ep["provider"])
    assert (cost["value"], cost["per"], cost["unit"]) == (2.0, 1000, "row")
    assert cost["usd"] == 0.002, "usd is the price of ONE chargeable event, whatever the quote unit"


async def test_a_provider_meter_converts_per_meter_not_per_provider():
    """`currency: unit` is the provider's own meter, and a provider can spend several at once.
    Majestic's analysis units and index-item units are as unrelated as two providers' credits."""
    cat = cs.load()
    assert set(cat.unit_rates["majestic"]) == {"analysis_unit", "retrieval_unit", "index_item_unit"}
    listing = cat.by_id["majestic.web.backlinks.list"]["cost"]
    assert listing["currency"] == "unit" and listing["unit"] == "analysis_unit"
    assert listing["value"] == 5000
    # Each pool converts at ITS OWN researched rate (fx.yaml, 2026-07-31): 5,000 analysis units at
    # $0.000004 — not at the retrieval or index-item rate, which differ by orders of magnitude.
    assert cat.cost_view(listing, "majestic")["usd"] == 0.02  # 5,000 × $0.000004, rounded at 9 dp

    # Moz's row quota is a meter too. It used to carry no `currency` at all, defaulted to USD, and
    # served every Moz route at $1.00 per row — the exact failure `currency: unit` prevents.
    moz = cat.by_id["moz.web.url.metrics"]["cost"]
    assert moz["currency"] == "unit" and moz["unit"] == "quota_row"
    assert cat.cost_view(moz, "moz")["usd"] == 0.006667  # 1 row × the researched $0.006667/row
    # Semrush is the one that stays native: package prices are sales-gated, so no rate is derivable
    # and its `usd` must remain None rather than a guess.
    assert cat.unit_rates["semrush"]["api_unit"] is None


async def test_free_is_spelled_one_way_and_prices_at_zero():
    """661 endpoints wrote free three ways, which left `usd` null on most of them — downstream,
    indistinguishable from "price unknown", which is the one thing free must never look like."""
    cat = cs.load()
    frees = [e for e in cat.endpoints if (e["cost"] or {}).get("type") == "free"]
    assert len(frees) > 500
    for ep in frees:
        cost = cat.cost_view(ep["cost"], ep["provider"])
        assert (cost["value"], cost["currency"], cost["unit"]) == (0, "USD", "call"), ep["id"]
        assert cost["usd"] == 0, ep["id"]


async def test_an_observed_price_is_provenanced_without_a_url():
    """DataForSEO prices per API family, not per route, so its extended entries carry only the
    charge the provider REPORTED at verification. That is the strongest provenance there is — it is
    what was actually billed — and its evidence is the captured response, not a pricing page."""
    cat = cs.load()
    ep = cat.by_id["dataforseo.x.ai-optimization-llm-mentions-historical-live"]
    cost = cat.cost_view(ep["cost"], ep["provider"])
    assert cost["source"] == "observed" and cost["confidence"] == "verified"
    assert cost["checked"] == ep["verified"], "the price was confirmed the day the call was made"
    assert "source_url" not in cost and cost["usd"] == cost["value"]


async def test_platform_eligibility_refuses_everything_it_cannot_prove():
    """The predicate behind spending treg's own key. Asymmetric on purpose: an unknown price must
    read as "refuse", never as free — so every axis is checked independently."""
    cat = cs.load()
    ok = cat.by_id["tikhub.tiktok.user.profile"]
    assert cat.platform_eligible(ok), "priced from the provider's live rate card, and live-called"

    def with_cost(**changes):
        return {**ok, "cost": {**ok["cost"], **changes}}

    assert cat.platform_eligible(with_cost(confidence="documented")), \
        "2026-07-31 policy: a provider-published rate is billable"
    assert not cat.platform_eligible(with_cost(confidence="inferred")), "a guess is not a rate"
    assert not cat.platform_eligible(with_cost(value=None, confidence="unknown"))
    assert not cat.platform_eligible(with_cost(currency="credit")), "tikhub has no credit rate"
    assert not cat.platform_eligible({**ok, "scope": "own_account"})
    assert not cat.platform_eligible({**ok, "kind": "account"})
    assert cat.platform_eligible({**ok, "verified": None}), \
        "2026-07-31 policy: the live-called stamp is no longer required — a broken route fails unbilled"
    assert not cat.platform_eligible({**ok, "cost": None}), "no price block ⇒ refuse, not free"


async def test_eligibility_rides_on_the_served_row(clients: AsyncClient):
    """A client deciding whether a call needs a credential must not have to re-derive the rule."""
    cat = cs.load()
    body = (await clients.get("/catalog/endpoints/tikhub.tiktok.user.profile")).json()
    assert body["endpoint"]["platform_eligible"] is True
    # Every launch provider has to have a usable surface, or tier 4 ships with nothing behind it.
    eligible = [e for e in cat.endpoints if cat.platform_eligible(e)]
    for provider in ("tikhub", "dataforseo", "scrapecreators"):
        assert sum(1 for e in eligible if e["provider"] == provider) > 20, provider


async def test_unknown_endpoint_is_404(clients: AsyncClient):
    r = await clients.get("/catalog/endpoints/tikhub.tiktok.nope")
    assert r.status_code == 404 and "tikhub.tiktok.nope" in r.text


def test_call_template_falls_back_to_documented_examples(tmp_path):
    """No test_request (an unverified endpoint) still yields a usable line: required params only,
    valued by their documented example, or a typed placeholder when even that is missing."""
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "moz.yaml").write_text(
        "provider: moz\nendpoints:\n"
        "  - id: moz.web.thing\n    platform: web\n    method: GET\n    path: /v1/{site}/links\n"
        "    input:\n"
        "      pathParams: {site: {type: string, required: true, example: moz.com}}\n"
        "      queryParams:\n"
        "        limit: {type: integer, required: true}\n"
        "        offset: {type: integer, required: false}\n")
    ep = cs.load(directory=tmp_path).by_id["moz.web.thing"]
    # Path params ride as --query (the server folds them into the path), required query after.
    assert cs.call_template(ep) == "treg call moz.web.thing --query site=moz.com --query limit=<integer>"


# ---- example responses -------------------------------------------------------------------
async def test_example_route_serves_a_captured_response(clients: AsyncClient):
    r = await clients.get("/catalog/examples/tikhub.tiktok.user.profile")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert isinstance(r.json(), (dict, list))


async def test_example_route_404s_an_unknown_endpoint(clients: AsyncClient):
    assert (await clients.get("/catalog/examples/tikhub.tiktok.nope")).status_code == 404


async def test_example_route_refuses_path_traversal(clients: AsyncClient):
    """The id is resolved through the catalog before any path is built, so a traversal attempt is
    just a miss — it must never read a file, encoded or not."""
    for probe in ("../../api.py", "..%2f..%2fapi.py", "%2e%2e%2f%2e%2e%2fconfig.py",
                  "/etc/passwd", "tikhub.tiktok.user.profile/../../api.py"):
        r = await clients.get(f"/catalog/examples/{probe}")
        assert r.status_code == 404, (probe, r.status_code)
        assert "def " not in r.text, probe


# ---- loader ------------------------------------------------------------------------------
def test_an_extended_file_merges_into_the_same_provider(tmp_path):
    """Curation splits a provider across `<service>.yaml` and `<service>.extended.yaml`; both land
    under one provider, and the extended tier is what keeps the long tail out of the capability view."""
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {tiktok: TikTok}\ncapabilities: {tiktok.user.profile: A profile}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\nendpoints:\n"
        "  - id: tikhub.tiktok.user.profile\n"
        "    capability: tiktok.user.profile\n"
        "    platform: tiktok\n    method: GET\n    path: /a\n")
    (tmp_path / "tikhub.extended.yaml").write_text(
        "provider: tikhub\nendpoints:\n"
        "  - id: tikhub.tiktok.user.mix\n"
        "    platform: tiktok\n    tier: extended\n    method: GET\n    path: /b\n"
        "  - id: tikhub.tiktok.user.stats\n"
        "    capability: tiktok.user.profile\n"
        "    platform: tiktok\n    tier: extended\n    method: GET\n    path: /c\n")

    cat = cs.load(directory=tmp_path)
    assert {e["id"] for e in cat.for_provider("tikhub")} == {
        "tikhub.tiktok.user.profile", "tikhub.tiktok.user.mix", "tikhub.tiktok.user.stats"}
    tiers = {e["id"]: e["tier"] for e in cat.endpoints}
    assert tiers["tikhub.tiktok.user.profile"] == "core"
    assert tiers["tikhub.tiktok.user.mix"] == "extended"


def test_a_missing_catalog_directory_is_an_empty_catalog_not_a_crash(tmp_path):
    cat = cs.load(directory=tmp_path / "nope")
    assert cat.endpoints == [] and cat.platforms == {}
    # a half-written dir (no taxonomy, malformed provider file) must degrade the same way
    (tmp_path / "capabilities.yaml").write_text("platforms: {tiktok: TikTok}\n")
    (tmp_path / "broken.yaml").write_text("provider: broken\nendpoints: [{no_id: true}]\n")
    assert cs.load(directory=tmp_path).endpoints == []


def test_a_proposed_capability_still_gets_a_description(tmp_path):
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "moz.yaml").write_text(
        "provider: moz\nproposed_capabilities: {web.thing.new: A new job}\nendpoints:\n"
        "  - id: moz.web.thing.new\n    capability: web.thing.new\n    platform: web\n"
        "    method: POST\n    path: /x\n")
    assert cs.load(directory=tmp_path).capabilities["web.thing.new"] == "A new job"


# ---- stamping ----------------------------------------------------------------------------
async def test_connecting_a_key_stamps_the_catalogs_verified_endpoints(clients: AsyncClient, monkeypatch):
    """A fresh connection should arrive knowing what it can call — the catalog's verified core
    endpoints ride onto the provisioned tool's examples, capped so the list stays scannable."""
    monkeypatch.setitem(P.REGISTRY, "tikhub", dataclasses.replace(
        P.REGISTRY["tikhub"], base_url="http://upstream", probe_url="", probe_path="/whoami",
        token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "tikhub", "token": "tk-key"})
    assert r.status_code == 200, r.text

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "tikhub")
    examples = tool["examples"]
    assert 0 < len(examples) <= 12
    assert all(set(e) == {"method", "path", "note"} for e in examples)
    assert len({(e["method"], e["path"]) for e in examples}) == len(examples), "no duplicate paths"

    profile = next(e for e in examples if e["path"] == "/api/v1/tiktok/web/fetch_user_profile")
    assert "tiktok.user.profile" in profile["note"], "the capability travels with the example"
    assert "uniqueId" in profile["note"], "the input hint is the part method+path can't show"

    stamped = {(e["method"], e["path"]) for e in examples}
    unverified = [e for e in cs.load().for_provider("tikhub") if not e["verified"]]
    assert unverified, "fixture assumption: tikhub has at least one unverified endpoint"
    assert not stamped & {(e["method"], e["path"]) for e in unverified}, "documented is not verified"


async def test_stamping_keeps_the_registrys_own_examples_first(clients: AsyncClient, monkeypatch):
    """The hand-written registry examples are the curated ones; the catalog appends, never displaces."""
    provider = dataclasses.replace(
        P.REGISTRY["tikhub"], base_url="http://upstream", probe_url="", probe_path="/whoami",
        token_verify_field="",
        examples=({"method": "GET", "path": "/hand/written", "note": "from the registry"},))
    monkeypatch.setitem(P.REGISTRY, "tikhub", provider)
    assert (await clients.post("/connections/token",
                               json={"provider": "tikhub", "token": "tk-key"})).status_code == 200

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "tikhub")
    assert tool["examples"][0]["path"] == "/hand/written"
    assert len(tool["examples"]) == 12


def test_a_provider_with_no_catalog_entry_stamps_nothing():
    assert cs.tool_examples("slack") == []
    assert json.dumps(cs.tool_examples("tikhub")), "the shape must be JSON-serializable for the Tool column"


# ---- "free" is a price, not a missing one ----------------------------------------------------
def test_a_free_route_needs_no_price_provenance():
    """`confidence` says how much we trust a NUMBER we are about to charge. A free route has no
    number, so demanding provenance refused 61 endpoints across 8 providers — 28 of Hunter's 35 —
    treating "costs nothing" as if it meant "we don't know", which is the one distinction the cost
    model is otherwise careful to keep apart."""
    from treg import catalog_store as cs
    cat = cs.load()
    free = {"type": "free", "value": 0, "currency": "USD", "unit": "call"}
    ep = {"cost": free, "provider": "hunter", "scope": "any_account", "kind": "data"}
    assert cat.platform_eligible(ep)


def test_a_PAID_route_still_needs_provenance():
    """The relaxation must not leak: a route with a real price and no provenance stays refused, or
    we would bill a team using a number nobody checked."""
    from treg import catalog_store as cs
    cat = cs.load()
    unchecked = {"type": "per_call", "value": 0.05, "currency": "USD", "unit": "call"}
    ep = {"cost": unchecked, "provider": "hunter", "scope": "any_account", "kind": "data"}
    assert not cat.platform_eligible(ep)

    documented = dict(unchecked, confidence="documented")
    assert cat.platform_eligible({**ep, "cost": documented})


def test_an_unpriced_route_is_still_refused():
    """The original rule, unchanged: no usd → refuse, so treg never pays a provider and charges $0."""
    from treg import catalog_store as cs
    cat = cs.load()
    ep = {"cost": {"type": "per_call", "currency": "credit", "unit": "credit"},
          "provider": "pdl", "scope": "any_account", "kind": "data"}
    assert not cat.platform_eligible(ep)


# ---- shared-plan rates: a price treg SET must say so, everywhere ----------------------------

def _load_validator():
    """The actual validator module, so these tests exercise the real check rather than a copy that
    can drift from it."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "catalog_validate", Path(__file__).parent.parent / "scripts" / "catalog_validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_shared_plan_rate_prints_its_fee_and_break_even():
    """A `kind: treg_shared_plan` entry is the catalog asserting OUR OWN price for a flat-fee
    provider. The honesty of that lives in the basis string, because the basis is what every surface
    shows as provenance: it must say whose price it is, the vendor fee, and the break-even volume.
    This runs the validator's own check against the real fx.yaml."""
    cv = _load_validator()
    errors: list[str] = []
    cv.check_fx(errors)
    assert errors == [], "\n".join(errors)


def test_the_shared_plan_check_actually_BITES(tmp_path):
    """Each dishonest shape must produce an error — checked by running the real check against bad
    entries, not by trusting that it would. (This session alone produced two tests that passed with
    their subject deleted; the lesson stuck.)"""
    cv = _load_validator()
    bad = """
credit_rates_usd:
  no_price:   {usd: null, kind: treg_shared_plan, basis: "treg shared-plan rate. $10/mo. break-even at 1,000 calls/mo", source: "x", checked: "2026-08-14"}
  no_fee:     {usd: 0.001, kind: treg_shared_plan, basis: "treg shared-plan rate. break-even at 1,000 calls/mo", source: "x", checked: "2026-08-14"}
  no_breakeven: {usd: 0.001, kind: treg_shared_plan, basis: "treg shared-plan rate. Vendor sells flat $10/mo", source: "x", checked: "2026-08-14"}
  wrong_start: {usd: 0.001, kind: treg_shared_plan, basis: "Vendor sells flat $10/mo; break-even at 1,000 calls/mo", source: "x", checked: "2026-08-14"}
  typo_kind:  {usd: 0.001, kind: treg_shared_pla, basis: "whatever", source: "x", checked: "2026-08-14"}
  prose_only: {usd: 0.001, basis: "treg shared-plan rate without the marker", source: "x", checked: "2026-08-14"}
  fine_vendor: {usd: 0.02, basis: "Starter $49/mo / 2,000 credits", source: "x", checked: "2026-08-14"}
"""
    (tmp_path / "fx.yaml").write_text(bad)
    original = cv.CATALOG
    try:
        cv.CATALOG = tmp_path
        errors: list[str] = []
        cv.check_fx(errors)
    finally:
        cv.CATALOG = original
    blob = "\n".join(errors)
    for name in ("no_price", "no_fee", "no_breakeven", "wrong_start", "typo_kind", "prose_only"):
        assert name in blob, f"{name} should have been refused:\n{blob}"
    assert "fine_vendor" not in blob, "an ordinary vendor rate must pass untouched"


def test_a_shared_plan_rate_converts_like_any_credit():
    """The whole design: a flat-fee provider is modelled as a credit provider whose credit is one
    call on treg's shared plan. cost_view needs ZERO changes — this asserts the pilot entry flows
    through the existing conversion."""
    from treg import catalog_store as cs

    c = cs.load()
    out = c.cost_view({"type": "per_call", "value": 1, "currency": "credit"}, "alphavantage")
    assert out["usd"] == 0.001
