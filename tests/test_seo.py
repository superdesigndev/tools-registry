"""Crawler-facing surfaces: robots.txt, sitemap.xml, HEAD, canonicals, and structured data.

These are easy to break silently — nothing in the app fails when a canonical goes stale or a
sitemap starts listing a renamed route, and nobody notices until traffic does. So the sitemap test
walks every URL it publishes rather than spot-checking, and the host tests assert on
`public_url` rather than on the literal treg.to: a self-hosted registry must advertise itself.
"""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree as ET

import pytest
from httpx import AsyncClient

from treg.config import get_settings


SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _base() -> str:
    return get_settings().public_url.rstrip("/")


def _locs(xml: str) -> list[str]:
    return [e.text or "" for e in ET.fromstring(xml).iter(f"{SITEMAP_NS}loc")]


# --------------------------------------------------------------------------------------- robots

async def test_robots_txt_is_served_and_names_the_sitemap(clients: AsyncClient):
    r = await clients.get("/robots.txt")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert f"Sitemap: {_base()}/sitemap.xml" in r.text
    assert "User-agent: *" in r.text


async def test_robots_txt_keeps_crawlers_out_of_what_costs_or_gates(clients: AsyncClient):
    """The metered proxy and the authenticated app are the two that actually matter: one bills per
    request, the other has nothing to show a crawler."""
    body = (await clients.get("/robots.txt")).text
    for path in ("/app", "/call/", "/login", "/oauth/", "/docs/api"):
        assert f"Disallow: {path}" in body, path
    assert "Disallow: /catalog" not in body   # the catalog is the whole point of indexing us


async def test_robots_txt_has_no_unsubstituted_template(clients: AsyncClient):
    assert "{BASE}" not in (await clients.get("/robots.txt")).text


# -------------------------------------------------------------------------------------- sitemap

async def test_sitemap_is_valid_xml_on_the_public_host(clients: AsyncClient):
    r = await clients.get("/sitemap.xml")
    assert r.status_code == 200, r.text
    assert "xml" in r.headers["content-type"]
    locs = _locs(r.text)
    assert len(locs) > 50, "the catalog shelves should dominate the sitemap"
    assert all(u.startswith(_base() + "/") or u == _base() + "/" for u in locs), locs[:3]


async def test_sitemap_lists_the_catalog_shelves(clients: AsyncClient):
    locs = _locs((await clients.get("/sitemap.xml")).text)
    assert f"{_base()}/" in locs
    assert f"{_base()}/catalog" in locs
    assert any(u.startswith(f"{_base()}/catalog/") for u in locs)


async def test_sitemap_omits_pages_that_would_not_answer_a_crawler(clients: AsyncClient):
    """Each of these fails a crawl differently: /login redirects, /tool-requests is POST-only,
    /app needs a session, and /contact + /vendor-listing.md are duplicate URLs for pages already
    listed under their canonical name."""
    locs = set(_locs((await clients.get("/sitemap.xml")).text))
    for path in ("/login", "/tool-requests", "/app", "/contact", "/help",
                 "/vendor-listing.md", "/connect-demo", "/install.sh", "/selfhost.sh"):
        assert f"{_base()}{path}" not in locs, path


async def test_every_sitemap_url_answers_200(clients: AsyncClient):
    """The test that earns its keep: rename a route and the sitemap starts publishing 404s, with
    nothing else in the suite noticing. Walks a sample of the catalog pages plus every static one,
    since 88 full renders would dominate the suite's runtime."""
    locs = _locs((await clients.get("/sitemap.xml")).text)
    static = [u for u in locs if not u.startswith(f"{_base()}/catalog/")]
    shelves = [u for u in locs if u.startswith(f"{_base()}/catalog/")][:5]
    for url in static + shelves:
        path = url[len(_base()):] or "/"
        r = await clients.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code} (listed in sitemap.xml)"


async def test_sitemap_redirects_from_a_legacy_host(clients: AsyncClient):
    """A sitemap served on treg.superdesign.dev but full of treg.to URLs is a cross-submission a
    crawler may discard wholesale. Send it to the canonical copy instead."""
    for path in ("/robots.txt", "/sitemap.xml"):
        r = await clients.get(path, headers={"Host": "treg.superdesign.dev"},
                              follow_redirects=False)
        assert r.status_code == 301, path
        assert r.headers["location"] == f"{_base()}{path}"


# ------------------------------------------------------------------------------------------ HEAD

@pytest.mark.parametrize("path", ["/", "/tutorial", "/llms.txt", "/favicon.svg", "/support",
                                  "/robots.txt", "/sitemap.xml", "/catalog", "/meta"])
async def test_head_is_answered_wherever_get_is(clients: AsyncClient, path: str):
    """FastAPI's APIRoute never adds HEAD to a GET route, so every page 405'd on the probe crawlers
    and link unfurlers send first. api.py widens them all in one pass after registration."""
    r = await clients.head(path)
    assert r.status_code == 200, f"HEAD {path} -> {r.status_code}"
    assert r.content == b""


async def test_head_is_still_refused_where_there_is_no_get(clients: AsyncClient):
    """The widening must be surgical: a POST-only route keeps refusing HEAD."""
    assert (await clients.head("/tool-requests")).status_code == 405


# ------------------------------------------------------------------------------------- canonical

async def test_the_three_support_urls_share_one_canonical(clients: AsyncClient):
    """/support, /contact and /help are one file (people guess differently). Without a canonical
    they are three URLs competing for the same page."""
    for path in ("/support", "/contact", "/help"):
        r = await clients.get(path)
        assert r.status_code == 200
        assert f'<link rel="canonical" href="{_base()}/support"/>' in r.text, path


@pytest.mark.parametrize("path,canon", [("/terms", "/terms"), ("/privacy", "/privacy"),
                                        ("/tutorial", "/tutorial")])
async def test_pages_declare_their_canonical(clients: AsyncClient, path: str, canon: str):
    r = await clients.get(path)
    assert f'<link rel="canonical" href="{_base()}{canon}"/>' in r.text


async def test_the_dashboard_is_noindex(clients: AsyncClient):
    r = await clients.get("/app")
    assert re.search(r'<meta name="robots" content="noindex', r.text)


async def test_the_markdown_twin_of_vendor_listing_is_noindex(clients: AsyncClient):
    """Two URLs, one document, and text/plain cannot carry a canonical — so the header does it."""
    assert (await clients.get("/vendor-listing.md")).headers.get("X-Robots-Tag") == "noindex"
    assert "X-Robots-Tag" not in (await clients.get("/vendor-listing")).headers


# ------------------------------------------------------------------- catalog pages & structured data

async def test_catalog_urls_serve_the_dashboard_spa(clients: AsyncClient):
    """The public catalog is not a second implementation — it IS the marketplace. /catalog hands
    back index.html so Vue renders the same platform views a member sees; if this ever stops being
    true, the two UIs have forked and will drift."""
    for path in ("/catalog", "/catalog/google"):
        body = (await clients.get(path)).text
        assert '<div id="app"' in body, path
        assert "vue" in body.lower(), path


async def test_the_catalog_index_lists_shelves_without_javascript(clients: AsyncClient):
    """The Vue app is the UI; #prerender is what a crawler that runs no scripts reads. It has to
    carry the text — 80 shelves that previously existed only as hash routes behind a login."""
    r = await clients.get("/catalog")
    assert r.status_code == 200
    pre = re.search(r'<div id="prerender">(.*?)</div>\s*<div id="app"', r.text, re.S)
    assert pre, "no server-rendered fallback"
    assert pre.group(1).count('href="/catalog/') > 50
    assert 'href="/catalog/google"' in pre.group(1)


@pytest.mark.parametrize("slug", ["google", "web", "tiktok"])
async def test_a_platform_page_renders_its_endpoints_as_text(clients: AsyncClient, slug: str):
    r = await clients.get(f"/catalog/{slug}")
    assert r.status_code == 200
    pre = re.search(r'<div id="prerender">(.*?)</div>\s*<div id="app"', r.text, re.S)
    assert pre, slug
    assert pre.group(1).count("<li>") > 5, "endpoint names should be in the fallback"


async def test_the_prerender_is_a_sibling_of_the_vue_root(clients: AsyncClient):
    """Vue compiles #app's own innerHTML as its template, so prerendered markup inside it would be
    parsed as a template (and blow up on the first stray moustache). It must sit outside."""
    body = (await clients.get("/catalog")).text
    assert body.index('id="prerender"') < body.index('id="app"')
    app_html = body[body.index('<div id="app"'):]
    assert 'id="prerender"' not in app_html


async def test_catalog_urls_are_indexable_despite_the_spa_default(clients: AsyncClient):
    """index.html carries `robots: noindex` for the authenticated app. These URLs are public, and
    shipping both tags would leave a crawler obeying the wrong one."""
    body = (await clients.get("/catalog/google")).text
    assert "content=\"noindex" not in body
    assert 'content="index, follow"' in body
    assert (await clients.get("/app")).text.count('content="noindex') == 1


async def test_a_shelf_page_counts_the_same_endpoints_the_app_does(clients: AsyncClient):
    """The SPA asks for ?include_hidden=1. The page must ask for the same population, or the two
    numbers on one URL — the fallback's and the app's — disagree in front of the reader."""
    full = (await clients.get("/catalog/platforms/web?include_hidden=1")).json()
    total = sum(len(c["endpoints"]) for c in full["capabilities"]) + len(full["extended"])
    assert f"{total} endpoints" in (await clients.get("/catalog/web")).text


async def test_the_json_catalog_routes_still_answer_json(clients: AsyncClient):
    """`/catalog/<slug>` sits in front of these. Registration order keeps them matching first, and
    if that ever changes the dashboard and every CLI break at once."""
    for path in ("/catalog/platforms", "/catalog/platforms/google", "/catalog/search?q=backlinks",
                 "/catalog/endpoints/moz.web.url.metrics"):
        r = await clients.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("application/json"), path


@pytest.mark.parametrize("slug", ["endpoints", "examples", "not-a-platform"])
async def test_unknown_slugs_404(clients: AsyncClient, slug: str):
    """`endpoints` and `examples` reach the page route (their JSON siblings need a trailing id), so
    only the reserved-word guard stops them rendering a nonsense shelf."""
    assert (await clients.get(f"/catalog/{slug}")).status_code == 404


@pytest.mark.parametrize("slug", ["platforms", "search"])
async def test_reserved_slugs_never_render_the_html_page(clients: AsyncClient, slug: str):
    """These two ARE valid URLs — the JSON routes registered before `/catalog/{slug}` claim them.
    What must never happen is the page route swallowing one and serving HTML to the dashboard."""
    r = await clients.get(f"/catalog/{slug}")
    assert r.headers["content-type"].startswith("application/json"), slug


async def test_prices_never_render_in_scientific_notation(clients: AsyncClient):
    """`%g` flips to exponent below 1e-4 and a shelf advertised "from $1.2e-07 per call", which
    reads as a bug rather than a price."""
    for slug in ("web", "google", "people"):
        assert not re.search(r"\$\d+(\.\d+)?e-\d+", (await clients.get(f"/catalog/{slug}")).text), slug


@pytest.mark.parametrize("path,types", [
    ("/", {"SoftwareApplication", "Organization"}),
    ("/catalog", {"ItemList", "BreadcrumbList"}),
    ("/catalog/google", {"ItemList", "BreadcrumbList"}),
    ("/support", {"FAQPage"}),
])
async def test_structured_data_parses_and_says_what_it_should(clients: AsyncClient, path, types):
    r = await clients.get(path)
    found = {json.loads(b)["@type"]
             for b in re.findall(r'application/ld\+json">(.*?)</script>', r.text, re.S)}
    assert types <= found, f"{path}: got {found}"


async def test_the_landing_offer_matches_the_page(clients: AsyncClient):
    """Schema that claims a price the page does not show is a structured-data violation, not a
    shortcut — so assert the free-credit figure appears in both."""
    r = await clients.get("/")
    ld = next(json.loads(b) for b in re.findall(r'application/ld\+json">(.*?)</script>', r.text, re.S)
              if json.loads(b)["@type"] == "SoftwareApplication")
    assert "$1.00 of free credit" in ld["offers"]["description"]
    assert "$1.00 free to start" in r.text          # benefit 03, the visible claim
    assert "0%" in ld["offers"]["description"] and "0%" in r.text


async def test_faq_schema_matches_the_visible_questions(clients: AsyncClient):
    r = await clients.get("/support")
    ld = next(json.loads(b) for b in re.findall(r'application/ld\+json">(.*?)</script>', r.text, re.S))
    for q in ld["mainEntity"]:
        assert f"<b>{q['name']}</b>" in r.text, f"schema asks {q['name']!r}, the page does not"


async def test_every_page_carries_a_social_card(clients: AsyncClient):
    for path in ("/", "/catalog", "/catalog/google", "/docs"):
        body = (await clients.get(path)).text
        assert 'property="og:image"' in body and "/media/og.png" in body, path
        assert 'name="twitter:card" content="summary_large_image"' in body, path


async def test_the_og_image_is_actually_served_at_the_right_size(clients: AsyncClient):
    """Tags pointing at a 404 mean every shared link unfurls blank."""
    r = await clients.get("/media/og.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # PNG header: width and height are big-endian uint32 at bytes 16..24
    width = int.from_bytes(r.content[16:20], "big")
    height = int.from_bytes(r.content[20:24], "big")
    assert (width, height) == (1200, 630), f"og.png is {width}x{height}, must be 1200x630"


# ------------------------------------------------------------------------------------------ docs

async def test_docs_is_server_rendered_and_swagger_moved(clients: AsyncClient):
    """/docs was a Swagger script shell — nothing for a crawler, and the landing linked to it."""
    r = await clients.get("/docs")
    assert r.status_code == 200
    assert "/call/{rest}" in r.text and "/catalog/search" in r.text
    assert "SwaggerUIBundle" not in r.text
    assert (await clients.get("/docs/api")).status_code == 200
    assert (await clients.get("/openapi.json")).status_code == 200


async def test_docs_does_not_advertise_the_admin_api(clients: AsyncClient):
    assert "/admin/orgs" not in (await clients.get("/docs")).text


async def test_widening_head_did_not_leak_into_the_public_schema(clients: AsyncClient):
    """Adding HEAD to every GET route gave FastAPI a second operation per path — 58 duplicate
    entries in openapi.json, each with a duplicate operation id. Only the /call proxy, which
    declares HEAD itself, should have one."""
    paths = (await clients.get("/openapi.json")).json()["paths"]
    with_head = [p for p, ops in paths.items() if "head" in ops]
    assert with_head == ["/call/{rest}"], with_head


def test_no_shelf_is_published_that_the_app_grid_hides():
    """Adding a platform is a data-only change — drop the YAML in and it appears on both sides. The
    one way that breaks: `catalog_store` auto-registers a platform with no `platforms:` entry in
    capabilities.yaml as `category: "Other"` (catalog_store.py, `platforms.setdefault`), and the
    dashboard's `platCategories` skips `Other` outright (`if(c==='Other') continue`). The shelf page
    would still render and the sitemap would still publish it — but nothing in the app's own grid
    would link to it. Give the new platform a label and category in capabilities.yaml.
    """
    from treg.api import _platform_rows
    orphans = [r["slug"] for r in _platform_rows() if r["category"] == "Other"]
    assert not orphans, (
        f"{orphans} have endpoints but no capabilities.yaml `platforms:` entry — the sitemap will "
        "publish /catalog/<slug> for each while the app's tile grid hides them")


# -------------------------------------------------------------------- public-mode chrome & CTAs
# Markup assertions rather than behaviour: these live in index.html's Vue template, which the test
# suite reads as text (see tests/test_dashboard_markup.py). Both were reported from the browser.

def _spa() -> str:
    from treg.api import _WEB_DIR
    return (_WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_public_catalog_drops_the_workspace_chrome():
    """A catalog visitor is reading a website, not operating an app. The org switcher, the global
    tool search and the member nav are furniture for a job they have not started."""
    spa = _spa()
    assert '<div class="pubnav" v-if="publicCatalog">' in spa      # marketing nav instead
    assert '<div class="top" role="banner" v-else>' in spa          # app bar only for members
    assert '<nav class="side" v-if="!publicCatalog">' in spa        # no sidebar in public mode
    assert '.layout.solo{grid-template-columns:minmax(0,1fr)}' in spa   # main spans the full width


def test_no_public_cta_navigates_to_a_page_that_bounces():
    """/app sends a logged-out visitor straight back to the landing (`location.replace('/')`), so a
    CTA pointing there is a dead end that loses the page they were reading. Every one of them opens
    the sign-in modal in place instead."""
    spa = _spa()
    assert "location.href='/app'" not in spa
    assert spa.count("publicCatalog ? openSignin()") >= 5   # try-it, connect, byok ×3, chips


def test_the_signin_modal_is_reachable_from_public_mode():
    """It used to live inside the logged-out landing branch, which public mode does not render —
    so there was nothing for a CTA to open."""
    spa = _spa()
    lp = spa.index('class="lp"')
    modal = spa.index('<div class="lc-scrim"')
    shell = spa.index("<template v-else>")
    assert not (lp < modal < shell), "the modal is trapped inside the logged-out landing branch"


def test_the_modal_does_not_talk_about_a_sandbox_on_the_catalog():
    """Default copy is the sandbox's ('bring it into a real account') — nonsense to someone who
    arrived from a search result."""
    spa = _spa()
    assert 'v-else-if="publicCatalog" class="sub">Create a free team' in spa


async def test_no_page_ships_an_unsubstituted_base(clients: AsyncClient):
    """`{BASE}` reaching a browser means a canonical or og:url is pointing at nothing."""
    for path in ("/", "/support", "/terms", "/privacy", "/tutorial", "/catalog"):
        assert "{BASE}" not in (await clients.get(path)).text, path
