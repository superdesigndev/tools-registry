"""The per-agent pages (`/apps/<agent>`): "I use ChatGPT — what can my agent do now?"

Everything on them is a projection of the catalog plus one hand-written install block, so the tests
assert the projection (counts, categories, rows) against `catalog_store.load()` rather than against
literals, and assert the crawler plumbing the shell is supposed to guarantee — canonical, robots
reachability, sitemap membership, FAQ schema that matches the visible page.
"""

from __future__ import annotations

import html as html_mod
import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from treg import agent_pages, catalog_store
from treg.api import app
from treg.config import get_settings


def _base() -> str:
    return get_settings().public_url.rstrip("/")


def _ld(html: str) -> list[dict]:
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


async def test_chatgpt_page_is_served_with_the_crawler_essentials(clients: AsyncClient):
    r = await clients.get("/agents/chatgpt")
    assert r.status_code == 200, r.text[:300]
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert f'<link rel="canonical" href="{_base()}/agents/chatgpt"/>' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "ChatGPT" in title and "treg.to" in title
    assert "noindex" not in html


async def test_chatgpt_page_counts_come_from_the_catalog(clients: AsyncClient):
    """The title's tool count is computed, never typed — the landing, llms.txt and the schema had
    drifted to three different numbers before this rule existed."""
    cat = catalog_store.load()
    n = sum(1 for e in cat.endpoints if e["kind"] not in catalog_store.HIDDEN_KINDS)
    html = (await clients.get("/agents/chatgpt")).text
    assert f"{n:,}" in re.search(r"<title>(.*?)</title>", html).group(1)


async def test_chatgpt_page_hero_rotates_through_the_roles(clients: AsyncClient):
    """"ChatGPT for SEO experts / social media managers / SDRs …" — the first role is in the
    server-rendered H1 so a crawler reads a complete sentence; the rest ride along for the JS."""
    html = (await clients.get("/agents/chatgpt")).text
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
    assert "ChatGPT plugin for" in h1 and html_mod.escape(agent_pages.ROLES[0]) in h1
    # only ONE role in the H1 itself — the rest are appended by JS from data-more
    assert html_mod.escape(agent_pages.ROLES[1]) not in h1
    more = json.loads(re.search(r'<script type="application/json" id="roles-more">(.*?)</script>', html, re.S).group(1))
    assert tuple(more) == agent_pages.ROLES[1:]


async def test_chatgpt_page_lists_the_curated_use_cases_by_category(clients: AsyncClient):
    """The page's value is a buyer's menu: plain-words jobs under buyer categories, each backed
    by the capabilities that do it — never a row per endpoint."""
    html = (await clients.get("/agents/chatgpt")).text
    for category, jobs in agent_pages.USE_CASES:
        assert f'<section id="{re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")}"' in html, category
        for label, caps in jobs:
            assert html_mod.escape(label, quote=True) in html, label
            for cid in caps:
                assert f'data-cap="{cid}"' in html, cid
    assert 'data-endpoint="' not in html


def test_every_use_case_capability_exists_in_the_catalog():
    """A job the catalog cannot do must not be advertised, and a renamed capability must fail here
    rather than silently drop a row from the page."""
    cat = catalog_store.load()
    missing = [cid for _, jobs in agent_pages.USE_CASES for _, caps in jobs for cid in caps
               if not cat.for_capability(cid)]
    assert not missing, missing


async def test_chatgpt_page_install_block_names_the_plugins_directory(clients: AsyncClient):
    html = (await clients.get("/agents/chatgpt")).text
    assert "Plugins" in html and "Install" in html
    # never a CTA into the authenticated app: a logged-out /app visit bounces to the landing
    body = html.split("<body>", 1)[1].split("<footer>", 1)[0]
    assert 'href="/app"' not in body.replace('href="/agents/', "")


async def test_chatgpt_page_faq_schema_matches_the_visible_page(clients: AsyncClient):
    """Google treats schema that claims something the page does not say as a violation."""
    html = (await clients.get("/agents/chatgpt")).text
    faqs = [b for b in _ld(html) if b.get("@type") == "FAQPage"]
    assert len(faqs) == 1
    qs = faqs[0]["mainEntity"]
    assert len(qs) >= 3
    for q in qs:
        assert q["name"] in html, q["name"]
    types = {b.get("@type") for b in _ld(html)}
    assert {"SoftwareApplication", "BreadcrumbList"} <= types


async def test_unknown_agent_404s(clients: AsyncClient):
    assert (await clients.get("/agents/clippy")).status_code == 404


async def test_agent_page_is_in_the_sitemap_and_reachable_by_robots(clients: AsyncClient):
    """`Disallow: /app` is a prefix rule: /agents/… must not sit under it (that is why the pages are
    not at /apps/…), and nothing else in robots.txt may block them."""
    sitemap = (await clients.get("/sitemap.xml")).text
    assert f"{_base()}/agents/chatgpt" in sitemap
    robots = (await clients.get("/robots.txt")).text
    assert not any(line.strip() in ("Disallow: /agents", "Disallow: /agents/", "Disallow: /use-cases")
                   for line in robots.splitlines())
    assert (await clients.head("/agents/chatgpt")).status_code == 200


async def test_agent_pages_are_hosted_only(monkeypatch):
    """The install copy says "search treg in ChatGPT's Plugins directory" — true of treg.to, false of
    every self-hosted registry. So off the reference hosts the page 404s and leaves the sitemap."""
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get("/agents/chatgpt")).status_code == 404
            assert "/agents/chatgpt" not in (await c.get("/sitemap.xml")).text
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ use-case pages (the spokes)

USECASE = "/use-cases/data-enrichment-sales/find-professional-emails"


async def test_use_case_page_is_served_with_the_crawler_essentials(clients: AsyncClient):
    r = await clients.get(USECASE)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    assert f'<link rel="canonical" href="{_base()}{USECASE}"/>' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "treg.to" in title and "providers" in title and "API" in title
    assert "noindex" not in html


async def test_use_case_page_answers_the_four_questions_in_order(clients: AsyncClient):
    """The reader does one thing (the prompt); everything else is what the agent sees before it
    calls. Each part is headed as the question people ask, answered in its first sentence."""
    html = (await clients.get(USECASE)).text
    order = ["best way to ask", "Why go through treg.to",
             "Which email finder API is cheapest", "How do the providers compare"]
    idx = [html.find(html_mod.escape(q, quote=False)) for q in order]
    assert all(i > 0 for i in idx), list(zip(order, idx))
    assert idx == sorted(idx), "sections out of order"
    # the honest product claim, and the reader's lever
    assert "does not choose for you" in html or "doesn't choose for you" in html
    assert "tell it how" in html


async def test_use_case_page_compares_one_row_per_provider_with_every_endpoint_collapsed(clients: AsyncClient):
    cat = catalog_store.load()
    eps = [e for e in cat.for_capability("people.email.find")
           if e["kind"] not in catalog_store.HIDDEN_KINDS]
    provs = {e["provider"] for e in eps}
    assert len(provs) >= 2
    html = (await clients.get(USECASE)).text
    for p in provs:
        assert f'data-provider="{p}"' in html, p
    # the page decides between PROVIDERS; the endpoint list belongs on the catalog shelf, which the
    # page links to. One runnable call stays as proof.
    assert "treg call " in html
    assert 'href="/catalog/' in html
    assert 'href="/agents/chatgpt#' in html   # up to the agent page's category anchor


async def test_reliability_section_appears_only_with_traffic(clients: AsyncClient):
    """With no call history the section is omitted (an empty promise is worse than none). The
    copy that would print per-vendor rates lives behind that check."""
    html = (await clients.get(USECASE)).text
    assert "Which one is the most reliable" not in html
    import inspect
    from treg import api
    src = inspect.getsource(api.use_case_job_page)
    assert "not a controlled benchmark" in src


async def test_use_case_page_faq_matches_the_visible_page(clients: AsyncClient):
    html = (await clients.get(USECASE)).text
    faqs = [b for b in _ld(html) if b.get("@type") == "FAQPage"]
    assert len(faqs) == 1
    for q in faqs[0]["mainEntity"]:
        assert html_mod.escape(q["name"], quote=True) in html, q["name"]
    assert "BreadcrumbList" in {b.get("@type") for b in _ld(html)}


async def test_use_case_page_prices_come_from_the_catalog(clients: AsyncClient):
    """No number on the page that the catalog did not produce: the lowest price in the title is the
    cheapest `cost_view` USD across the job's endpoints."""
    cat = catalog_store.load()
    eps = [e for e in cat.for_capability("people.email.find")
           if e["kind"] not in catalog_store.HIDDEN_KINDS]
    lowest = min(c["usd"] for e in eps
                 if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"])
    from treg.api import _usd_short
    html = (await clients.get(USECASE)).text
    # the price sits in the hero kicker and the economics block, not the title: a title that fits a
    # search result has no room for it
    assert _usd_short(lowest) in html


async def test_unknown_use_case_404s(clients: AsyncClient):
    assert (await clients.get("/use-cases/data-enrichment-sales/teleport")).status_code == 404
    assert (await clients.get("/use-cases/nope/find-professional-emails")).status_code == 404


async def test_use_case_page_is_in_the_sitemap_and_linked_from_the_agent_page(clients: AsyncClient):
    assert f"{_base()}{USECASE}" in (await clients.get("/sitemap.xml")).text
    assert f'href="{USECASE}"' in (await clients.get("/agents/chatgpt")).text
    assert (await clients.head(USECASE)).status_code == 200


async def test_legacy_flat_use_case_pages_still_answer(clients: AsyncClient):
    """The five ad landing pages keep their flat URLs; nesting must not shadow them."""
    assert (await clients.get("/use-cases/lead-enrichment-for-ai-agents")).status_code == 200


def test_every_use_case_page_is_a_row_on_the_menu():
    """A spoke's label must match a job in USE_CASES exactly, or the agent page cannot link to it
    and the page has no capabilities to render."""
    menu = {(agent_pages.category_slug(c), lbl) for c, jobs in agent_pages.USE_CASES for lbl, _ in jobs}
    for (c, _), spec in agent_pages.USE_CASE_PAGES.items():
        assert (c, spec["label"]) in menu, (c, spec["label"])


# ------------------------------------------------------------------------------- markdown mirrors

async def test_pages_have_markdown_mirrors_for_agents_and_answer_engines(clients: AsyncClient):
    """`.md` serves the same page as plain Markdown, and the HTML declares it as an alternate."""
    for path in ("/agents/chatgpt", USECASE):
        html = (await clients.get(path)).text
        assert f'<link rel="alternate" type="text/markdown" href="{_base()}{path}.md"/>' in html
        r = await clients.get(path + ".md")
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/markdown")
        assert r.text.startswith("# ")
        assert "<div" not in r.text
    # the markdown lists the same jobs as the HTML menu
    md = (await clients.get("/agents/chatgpt.md")).text
    for _, jobs in agent_pages.USE_CASES:
        for label, _ in jobs:
            assert label in md, label


async def test_agent_page_rows_carry_logos_and_free_badges(clients: AsyncClient):
    html = (await clients.get("/agents/chatgpt")).text
    assert "favicons?domain=linkedin.com" in html
    assert "free, your account" in html               # Search Console etc. run on the team's own key
    assert "$0.000" in html                            # the no-markup promise, stated


async def test_no_em_dashes_in_the_hand_written_copy():
    """House style: no em-dashes in page copy. The setup line is the product's literal command and
    is the one exception."""
    import inspect
    src = inspect.getsource(agent_pages)
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#") and "SETUP_LINE" not in l)
    # docstrings are not page copy; strip the module docstring
    body = body.split('"""', 2)[-1]
    assert "—" not in body, [l for l in body.splitlines() if "—" in l][:3]


# --------------------------------------------------- template fitness for the other 65 jobs

SINGLE = "/use-cases/connect-your-own-accounts/search-console-queries"
MULTI = "/use-cases/social/find-creators-by-keyword"


async def test_cheapest_is_only_claimed_within_one_billing_unit(clients: AsyncClient):
    """38 of the 66 jobs mix per-call, per-result and per-success endpoints. Sorting those by USD
    per chargeable event names the wrong winner (a $0.09 call returning 1,000 rows is not dearer
    than $0.0005 per row), so the page states a cheapest PER UNIT and says the units differ."""
    html = (await clients.get(USECASE)).text
    assert "per found" in html
    # the claim carries its unit, never a bare superlative
    assert re.search(r"[Cc]heapest per (found|call|result)", html), "no unit-scoped cheapest claim"


async def test_single_provider_job_uses_the_short_form(clients: AsyncClient):
    """One provider means there is nothing to compare: the three comparison questions must not
    render as three sections with one row each."""
    r = await clients.get(SINGLE)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    assert "cheapest" not in html.lower()
    assert "How do the providers compare" not in html
    assert "Behind the scenes" not in html
    assert "How it works" in html                 # the short form's own section
    assert "your own account" in html.lower()     # and it says the job runs on the reader's key
    assert "treg call " in html


async def test_multi_platform_job_groups_by_platform_not_by_price(clients: AsyncClient):
    """19 jobs span several platforms. Instagram search and YouTube search are not alternatives to
    each other, so the page must not rank them against one another as if they were."""
    r = await clients.get(MULTI)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    for label in ("Instagram", "TikTok", "YouTube"):
        assert f'data-platform-group="{label}"' in html or f'>{label}</h4>' in html, label
    assert not re.search(r"[Cc]heapest overall", html)


async def test_reliability_section_is_absent_when_there_is_no_traffic(clients: AsyncClient):
    """Most endpoints see no calls in a 30-day window. An empty promise is worse than no section."""
    html = (await clients.get(USECASE)).text
    assert "Which one is the most reliable" not in html   # no CallRecords in the test database


async def test_no_agent_or_job_specific_string_is_hardcoded_in_the_route():
    """Everything job-specific comes from the page spec, and the example agent from one constant,
    so writing page 2 is data entry."""
    import inspect
    from treg import api
    src = inspect.getsource(api.use_case_job_page)
    for bad in ("email finder", "found addresses", "an address is found", "email address"):
        assert bad not in src.lower(), bad
    assert src.count("ChatGPT") == 0, "the example agent must come from DEFAULT_AGENT"


async def test_use_cases_hub_lists_every_written_page(clients: AsyncClient):
    """The breadcrumb pointed at an agent page because no hub existed. A sitemap is not a crawl path."""
    r = await clients.get("/use-cases")
    assert r.status_code == 200, r.text[:200]
    html = r.text
    for (c, j), spec in agent_pages.USE_CASE_PAGES.items():
        assert f'href="/use-cases/{c}/{j}"' in html, (c, j)
        assert html_mod.escape(spec["sentence"], quote=True) in html or spec["label"] in html
    assert f"{_base()}/use-cases" in (await clients.get("/sitemap.xml")).text
    assert '<a href="/use-cases">' in (await clients.get(USECASE)).text   # breadcrumb points here


@pytest.mark.parametrize("path", ["/agents/chatgpt", "/agents/claude", "/agents/claude-code",
                                  "/agents/cursor", "/use-cases", USECASE])
async def test_titles_and_descriptions_fit_a_search_result(clients: AsyncClient, path: str):
    """Google prints roughly 60 characters of a title and 155 of a description; past that it cuts
    mid-word, and the cut usually lands on the part that would have made someone click."""
    html = (await clients.get(path)).text
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    desc = re.search(r'name="description" content="(.*?)"', html, re.S).group(1)
    assert len(title) <= 65, f"{path}: title {len(title)} chars: {title}"
    assert len(desc) <= 160, f"{path}: description {len(desc)} chars"
    assert desc.rstrip().endswith((".", "?", "!")), f"{path}: description cut mid-sentence: …{desc[-40:]}"


async def test_non_canonical_casing_redirects_to_the_one_spelling(clients: AsyncClient):
    """Lookups are case-insensitive, but the request's own bytes must never be rendered into the
    canonical / alternate / breadcrumb (CodeQL py/reflective-xss) — and `/agents/ChatGPT` serving a
    200 with a canonical to itself is a duplicate page. One 301 to the lowercase slug instead."""
    r = await clients.get("/agents/ChatGPT", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/agents/chatgpt"
    r = await clients.get("/agents/ChatGPT.md", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/agents/chatgpt.md"
    cat, job = next(iter(agent_pages.USE_CASE_PAGES))
    r = await clients.get(f"/use-cases/{cat.upper()}/{job}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/use-cases/{cat}/{job}"
    assert (await clients.get("/agents/<script>")).status_code == 404
