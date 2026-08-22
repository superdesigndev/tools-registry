"""Structural checks on the single-file dashboard.

index.html is a 3k-line Vue template with no build step and no component boundaries, so nothing
catches a block ending up in the wrong place. These assert the few structural rules that, when
broken, produce bugs that look like dead buttons rather than errors.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from treg import api

INDEX = (Path(api.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
TUTORIAL = (Path(api.__file__).parent / "web" / "tutorial.html").read_text(encoding="utf-8")

# Dialogs reachable from more than one view. Each is opened by a button that exists on both the
# marketplace list and an integration page.
SHARED_DIALOGS = ["tokenAsk", "capAsk", "resPick"]


# Every <template> open/close, because only a balanced count locates the view boundaries: the file
# nests plain `v-if`/`v-for` templates inside views, and each of those closes with a `</template>`
# that would otherwise be read as closing the view. (Corollary: don't write a literal `<template`
# inside an HTML comment in index.html — this counts it as a real open tag.)
_TPL = re.compile(r"<template\b(?P<attrs>[^>]*)>|</template>")
_VIEW_ATTR = re.compile(r"""v-if="view==='(\w+)'""")


def _enclosing_views(needle: str) -> list[str]:
    """Which `<template v-if="view===...">` blocks, if any, contain `needle`."""
    at = INDEX.index(needle)
    stack: list[str | None] = []
    for m in _TPL.finditer(INDEX[:at]):
        if m.group(0).startswith("</"):
            if stack:
                stack.pop()
        else:
            v = _VIEW_ATTR.search(m.group("attrs"))
            stack.append(v.group(1) if v else None)
    return [v for v in stack if v]


@pytest.mark.parametrize("dialog", SHARED_DIALOGS)
def test_shared_dialogs_are_not_trapped_inside_one_view(dialog: str):
    """A dialog nested in `view==='connections'` does not render on the integration page — the
    Connect button looks dead, and the modal appears on the list view once you navigate back.
    Vue reports nothing, because a v-if that never matches is not an error."""
    enclosing = _enclosing_views(f'v-if="{dialog}"')
    assert not enclosing, f"{dialog} dialog is trapped inside view(s) {enclosing}; move it to root"


def test_the_parser_can_actually_see_a_trapped_dialog():
    """Guard the guard: a check that can't detect the bug it exists for is worse than none."""
    assert _enclosing_views('v-for="p in g.items"') == ["connections"]


# --- endpoint catalog: the marketplace's platform axis (view==='platform') --------------------


def test_the_platform_header_stacks_instead_of_putting_providers_in_a_column():
    """`.tut-head` is two columns, and a platform can be served by a dozen providers — as a
    right-hand column that chip list steals half the width and wraps the title into a ribbon
    ("People & / contact / data"). Title full width, intro under it, providers as their own row."""
    at = INDEX.index('<div class="plat-head">')
    head = INDEX[at : INDEX.index('v-if="platLoading"', at)]
    assert head.index('class="plat-title"') < head.index('plat-intro"') < head.index('class="plat-provs"')
    assert 'v-for="s in platProviders"' in head
    assert "tut-actions" not in head  # the two-column header is gone from this view
    assert ".plat-provs{display:flex;flex-wrap:wrap" in INDEX  # ...and the chips wrap rather than scroll
    assert ".plat-intro{margin:10px 0 0;max-width:74ch}" in INDEX  # readable measure, full-width column


def test_platform_view_is_a_top_level_view():
    """`view==='platform'` nested inside another view would render nowhere — the platform cards
    would look like dead buttons, exactly the failure the dialog checks above exist for."""
    assert _enclosing_views("<template v-if=\"view==='platform'\">") == []


def test_platform_shelf_lives_on_the_marketplace_list():
    """The only way into a platform page is the tile grid on the marketplace list view."""
    assert _enclosing_views("openPlatform(pl.slug)") == ["connections"]


def test_platform_shelf_disappears_without_the_catalog():
    """/catalog is served by a newer build than some deployments run: the tab bar must be v-if'd on
    having platforms, so a 404 leaves the marketplace exactly as it was rather than showing an
    empty section or an error."""
    tabs = INDEX.index('<div class="mk-tabs-wrap"')
    assert 'v-if="plats.list.length"' in INDEX[tabs : tabs + 200]
    # ...and with no tiles to show, the active tab resolves to the original provider shelves.
    assert "mkTabActive(){ return this.platCategories.length ? this.mkTab : 'platform'; }" in INDEX


# --- marketplace tab bar: capability-first tiles, with the original shelves on the last tab ------


def test_the_tab_bar_is_all_plus_the_catalog_categories_plus_platform():
    """The founder's bar reads All | <categories> | Platform. The middle is derived from the
    catalog's own `category` field rather than hard-coded, so a new category can't go missing."""
    assert 'v-for="t in mkTabs"' in INDEX
    tabs = INDEX[INDEX.index("mkTabs(){") : INDEX.index("mkTabs(){") + 420]
    assert "key:'all'" in tabs
    assert "for(const g of this.platCategories)" in tabs
    assert "key:'platform'" in tabs
    # The canonical reading order, straight from the founder's list. It is only an ORDER: the
    # categories themselves are whatever the catalog rows carry, so this list may not gate them.
    order = INDEX[INDEX.index("platCategories(){") :][:900]
    assert (
        "['SEO/AEO','Social','Advertising','Enrichment','E-commerce','Reviews & Apps',"
        "'Community']" in order
    )


def test_the_category_list_comes_from_the_data_not_the_order_list():
    """Categories keep changing ("Social media" became "Social", "China Social" is new). A category
    the catalog invents must still get a shelf and a tab — sorted to the end, never dropped."""
    body = INDEX[INDEX.index("platCategories(){") :][:2000]
    assert "for(const pl of this.plats.list)" in body  # the categories are read off the rows...
    assert "Object.keys(by).filter(c=>!order.includes(c)).sort()" in body  # ...and unknown ones survive
    # The old catalog names, gone from the data. The answer engines were their own shelf twice over
    # ("AI Search", then "AEO / GEO"); they are unfeatured SEO platforms now, so they surface inside
    # that shelf's overflow row instead. (`providerGroups` has its own, unrelated category set from
    # /oauth/providers — "Social media" there is an integration shelf, not a platform one.)
    for gone in ("'Social media'", "'AI Search'", "'AEO / GEO'"):
        assert gone not in body, f"{gone} is no longer a catalog category — remove the reference"


def test_the_tab_strip_scrolls_without_showing_a_scrollbar():
    """Ten tabs overflow a narrow window, but the scrollbar under them reads as a rendering fault.
    The strip still scrolls; a right-edge fade is what hints there is more."""
    css = INDEX[INDEX.index(".mk-tabs{") :][:600]
    assert "overflow-x:auto" in css
    assert "scrollbar-width:none" in css
    assert ".mk-tabs::-webkit-scrollbar{display:none}" in INDEX
    assert "mask-image:linear-gradient(90deg,#000 calc(100% - 26px),transparent)" in css
    # The rule under the tabs must NOT be on the masked scroller, or it fades short of the edge.
    assert "border-bottom" not in css[: css.index("}")]
    assert ".mk-tabs-wrap{border-bottom:1px solid var(--line)" in INDEX


def test_tiles_are_grouped_by_category_on_every_capability_tab():
    """"All" is not a flat wall of tiles: it keeps the category headings, and a category tab is the
    same grouping filtered to one — so the page never loses its place."""
    assert 'v-for="g in platCatGroups"' in INDEX
    assert _enclosing_views('v-for="g in platCatGroups"') == ["connections"]
    grp = INDEX[INDEX.index("platCatGroups(){") :][:260]
    assert "this.mkTabActive==='all' ? this.platCategories" in grp
    assert "filter(g=>g.category===this.mkTabActive)" in grp


def test_platforms_categorised_as_other_get_no_tile():
    """`account` and friends are capabilities of a platform page, not destinations — a tile for
    "Other" would be a dead end."""
    cats = INDEX[INDEX.index("platCategories(){") :][:2000]
    assert "if(c==='Other') continue;" in cats


# --- featured shelves: a long category shows its ranked tiles, then one "more" row ---------------


def test_a_long_category_shows_only_its_featured_tiles():
    """China Social carries 14 platforms. A wall of 14 tiles is scrolled past, not read, so past the
    threshold only the catalog's `featured` ranks get a full tile."""
    grp = INDEX[INDEX.index("platCatGroups(){") :][:1400]
    assert "items.filter(p=>p.featured!=null)" in grp
    assert "items.length<=8" in grp
    assert "this.platShelfOpen[g.category]" in grp  # ...until the row is clicked


def test_featured_tiles_are_ordered_by_rank_then_size():
    """`featured` is a rank, so 1 comes first; the unranked tail has only endpoint count left to
    sort by. An unranked platform must sort AFTER every ranked one, not before it."""
    grp = INDEX[INDEX.index("platCatGroups(){") :][:1400]
    assert "(a.featured==null?1e9:a.featured)-(b.featured==null?1e9:b.featured)" in grp
    assert "(b.endpoints||0)-(a.endpoints||0)" in grp


def test_a_category_with_no_featured_ranks_still_shows_its_tiles():
    """A shelf whose rows are all unranked would collapse to an empty grid over a "more" row —
    the whole category hidden behind a click, which is worse than a long shelf."""
    assert "|| !feat.length ||" in INDEX[INDEX.index("platCatGroups(){") :][:1400]


def test_the_shelf_count_is_the_whole_category_not_the_visible_tiles():
    """A header reading "SEO 5" under a tab reading "SEO 10" looks like tiles went missing."""
    assert 'class="sec-n">{{g.total}}' in INDEX
    assert "total:items.length" in INDEX


def test_a_category_heading_is_a_real_heading():
    """The page is browsed by category, so the category has to be the loudest thing on it after the
    page title — a small muted caption made the whole marketplace read as undifferentiated."""
    head = INDEX[INDEX.index('<div class="sec-head">') :][:500]
    assert '<h2 class="sec-h"><b>{{g.category}}</b>' in head
    assert "{{g.hint}}" in head  # the one-line explainer stays, under the heading
    css = INDEX[INDEX.index(".sec-h b{") :][:120]
    assert "font-size:19px" in css and "font-weight:600" in css


def test_the_more_row_names_the_platforms_it_is_hiding():
    """"See Zhihu, WeChat Channels, and 6 more" — a stack of the marks plus two names, so the row
    says what KIND of thing is hidden rather than only how much of it."""
    at = INDEX.index('class="pt-more"')
    row = INDEX[at : at + 1200]
    assert "platShelfOpen[g.category]=true" in row
    assert 'v-for="pl in g.rest.slice(0,7)"' in row  # the little logo stack
    assert "'/logos/platforms/'+pl.slug+'.svg'" in row
    assert "moreLabel(g.rest)" in row
    lbl = INDEX[INDEX.index("moreLabel(rest){") :][:400]
    assert "rest.slice(0,2)" in lbl
    assert "(rest.length-2)+' more'" in lbl


def test_the_more_row_is_a_quiet_row_with_an_arrow_not_a_dashed_box():
    """A dashed border reads as a drop zone or a placeholder, and it drew more attention than the
    cards it sits under. The row is plain; an arrow (which slides on hover) is the affordance."""
    at = INDEX.index('class="pt-more"')
    assert '<span class="pt-more-a" aria-hidden="true">→</span>' in INDEX[at : at + 1400]
    css = INDEX[INDEX.index(".pt-more{") :][:900]
    assert "dashed" not in css and "border:0" in css
    assert ".pt-more:hover .pt-more-a{transform:translateX(2px)" in INDEX
    assert ".pt-more:hover{color:var(--ink);background:var(--hover)}" in INDEX  # subtle tint, no border


# --- the platform card: enough to choose a platform without opening it --------------------------


def _card() -> str:
    at = INDEX.index('class="pt-card"')
    return INDEX[at : INDEX.index("</button>", at)]


def test_a_card_leads_with_the_platforms_own_mark_and_name():
    """TikTok's card wears TikTok's mark, with the category as the subtitle — the headline answers
    "which data", which is the question the whole browse surface is organised around."""
    card = _card()
    assert "'/logos/platforms/'+pl.slug+'.svg'" in card
    assert 'class="pt-name">{{platShort(pl.label)}}' in card
    assert 'class="pt-cat">{{pl.category}}' in card


def test_a_card_corner_answers_whether_you_can_call_it_today():
    """The corner is the one browse-time fact that changes what you do next: call it, or go and
    sign up first. WHICH provider serves it is a decision for the platform page, so the provider
    logo stack that used to live here is gone."""
    card = _card()
    assert 'v-if="platConnected(pl)" class="chip go"' in card
    assert ">Connected</span>" in card
    assert 'v-else class="pt-quiet"' in card and ">not connected<" in card
    assert "'/logos/'+s+'.svg'" not in card  # the provider stack is gone
    fn = INDEX[INDEX.index("platConnected(pl){") :][:220]
    assert "some(s=>this.catConnected(s))" in fn  # ANY serving provider being connected is enough


def test_a_card_is_a_name_and_two_facts_with_no_description_paragraph():
    """Size and starting price are what turn a wall of logos into a comparison, and both come from
    the catalog row (`price_from`) — no detail call needed. The summary paragraph does NOT: it
    repeated what the name and category already said and made every card tall enough that a shelf
    of twelve became a scroll. It survives as the card's hover title, nowhere else."""
    card = _card()
    assert "pt-desc" not in card and "pt-desc" not in INDEX, "the paragraph and its CSS are both gone"
    assert "{{pl.endpoints}} endpoint" in card
    assert 'v-if="platPrice(pl)"' in card
    assert ":title=\"pl.summary ? pl.label+' — '+pl.summary : pl.label\"" in card


def test_a_long_platform_name_wraps_instead_of_ellipsizing():
    """"Google Search Con…" is a card that cannot say what it is. With the description gone there is
    room for the whole name on two lines."""
    css = INDEX[INDEX.index(".pt-name{") :][:300]
    assert "-webkit-line-clamp:2" in css
    assert "white-space:nowrap" not in css


def test_the_card_price_is_the_servers_computed_usd():
    """Every price the marketplace shows is in one unit, so two numbers on the same screen can be
    compared. The conversion is the server's (`usd`), not the dashboard's — a local FX constant
    would drift from the CLI the moment the rate table changed."""
    fn = INDEX[INDEX.index("platPrice(pl){") :][:1400]
    assert "typeof pf.usd==='number'" in fn
    assert "'$'+this.usdNum(pf.usd)+' / '+this.priceUnit(pf.type)" in fn
    assert "return null; }" in fn  # priced but unpublished → say nothing, not "from —"
    # "from" is a floor: an OAuth provider among the platform's providers makes the floor $0, even
    # when metered providers publish a rate — that rate demotes to the tooltip.
    assert fn.index("if(hasOauth) return {free:true") < fn.index("if(paid) return {free:false")


def test_an_empty_price_from_object_reads_as_no_price_at_all():
    """`price_from` arrives as null OR as `{}`. A truthy empty object short-circuits the OAuth-free
    branch below it, silently costing an OAuth-only platform its "free with your account"."""
    fn = INDEX[INDEX.index("platPrice(pl){") :][:1400]
    assert "(raw && Object.keys(raw).length) ? raw : null" in fn


def test_a_converted_price_still_shows_the_providers_own_figure():
    """Nobody should have to wonder whether we invented the number: the native amount rides along
    wherever the provider bills in something other than USD. On the platform card it rides in the
    hover title, not the footer — "(1 credit)" after an already-long price wrapped the card."""
    assert ".cost-nat{" in INDEX
    fn = INDEX[INDEX.index("nativeAmount(c){") :][:500]
    assert "c.currency==='USD'" in fn  # ...and is omitted when there is nothing to convert
    assert "n.toFixed(2)" in fn  # money keeps its cents: ¥0.10, not ¥0.1
    # the two roomy price surfaces carry it inline; the platform card only in its tooltip
    assert "platPrice(pl).native" not in INDEX
    assert "'billed as '+p.native" in INDEX
    assert "r.priceNative" in INDEX
    assert 'v-if="costNative(e.cost)"' in INDEX


def test_sub_cent_prices_get_two_significant_figures():
    """$0.01476 as "$0.01" is wrong and as "$0.014760" is noise. Two significant figures reads as a
    price at every magnitude the catalog carries ($0.00015 through $0.044)."""
    fn = INDEX[INDEX.index("usdNum(n){") :][:500]
    assert "n.toPrecision(2)" in fn
    assert "n.toFixed(2)" in fn  # ...but a dollar or more is just cents
    assert "indexOf('e')" in fn  # toPrecision goes exponential below 1e-6


def test_an_oauth_only_platform_reads_as_free_with_your_account():
    """`price_from` is null both when a rate is unpublished and when there is nothing to charge for.
    Every-provider-is-OAuth separates them: the account you connect IS the licence."""
    fn = INDEX[INDEX.index("platPrice(pl){") :][:1200]
    assert "p.auth_kind==='oauth'" in fn
    assert "free with your account" in fn


def test_an_undrawn_platform_falls_back_to_a_generated_initial_tile():
    """60 platforms and counting: a missing SVG must degrade to a coloured initial, not a broken
    image. The colour is derived from the slug so it is stable across reloads."""
    card = _card()
    assert '@error="platLogoBad[pl.slug]=true"' in card
    assert 'v-else class="pt-i"' in card
    assert "platTileBg(slug){" in INDEX


def test_the_platform_tab_is_the_original_provider_marketplace():
    """The last tab is the pre-existing integration shelves, unchanged — connect flows all live
    there, so nothing may be lost behind the new default view."""
    at = INDEX.index("""<template v-if="mkTabActive==='platform'">""")
    block = INDEX[at : INDEX.index('v-if="view===\'provider\' && mkProvider"')]
    assert 'v-for="g in shownGroups"' in block  # the category shelves
    assert 'v-for="p in g.items"' in block  # the provider cards
    assert 'openProvider(p.service)' in block  # ...still opening the integration page


# --- platform detail: ONE ledger, sectioned by domain -------------------------------------------


def _ledger() -> str:
    """The platform view's markup, from the filter bar to the end of the table."""
    view = INDEX[INDEX.index("<template v-if=\"view==='platform'\">") :]
    return view[: view.index("<template v-if=\"view==='tools'\">")]


def test_the_platform_is_one_table_not_a_stack_of_capability_cards():
    """A platform page is a ledger you scan top to bottom: one table, section headings inside it.
    The old per-capability card stack made the shape of the platform unreadable — every job looked
    the same size, and nothing could be compared without opening two cards."""
    block = _ledger()
    assert '<table class="ledger">' in block
    assert 'v-for="sec in platLedger"' in block
    assert '<tr class="lsec"' in block
    assert "cap-card" not in INDEX, "the capability card is gone, and so is its CSS"


def test_sections_are_domains_with_other_pinned_last():
    """`other` is the junk drawer: it is the one section whose position carries meaning, and it
    can't be allowed to outrank a real subject just because it is large. The order is decided
    server-side so the API, the CLI and this page can't disagree about it."""
    src = (Path(api.__file__).parent / "catalog_store.py").read_text(encoding="utf-8")
    fn = src[src.index("def domain_rows("):]
    assert "key=lambda d: (d == DOMAIN_OTHER, -len(sections[d]), d)" in fn
    # ...and the page renders that order rather than re-sorting it.
    led = INDEX[INDEX.index("platLedger(){") :][:900]
    assert "this.platData.domains||[]" in led


def test_merged_rows_come_before_single_rows_in_a_section():
    """A job several providers do is the comparison the catalog exists to make, so it leads its
    section; the endpoints only one provider offers follow."""
    src = (Path(api.__file__).parent / "catalog_store.py").read_text(encoding="utf-8")
    fn = src[src.index("def domain_rows("):]
    assert 'section.sort(key=lambda r: (r["kind"] != "merged"' in fn


def test_a_collapsed_merged_row_prices_its_providers_in_short_pills():
    """The pills are the comparison the merge exists to make, so they stay — but each one is a name,
    a price only when the price is a REAL number, and a ✓. The long filler ("per result · price in
    provider dashboard") is what wrapped a six-provider row over four lines, and it appears on no
    collapsed surface. Short pills may wrap to a second line; prose may not."""
    block = _ledger()
    assert 'v-for="pill in r.pills"' in block and 'class="pchip"' in block
    assert "{{pill.name}}" in block and 'v-if="pill.price"' in block
    assert "costLabel" not in block, "the long form is a sentence — it belongs in the facts list"
    price = INDEX[INDEX.index("pillPrice(c){") :][:500]
    assert "if(c.type==='free') return 'free';" in price, "free is a real price and worth showing"
    assert "typeof c.usd==='number' ? this.costLabel(c) : ''" in price
    assert "l.length<=8 ? l : ''" in price, "a quota label only if it fits — '2 rows' yes, longer no"
    # The cheapest of them still stands in the price column, so the row answers "what does it cost".
    rows = INDEX[INDEX.index("platRowsAll(){") :][:3600]
    assert "const cheapest=this.capCheapest(eps);" in rows
    assert "cheapest.n>0?'from ':''" in rows, '"from free" hedges the one price that needs none'


def test_the_pill_strip_never_wraps_three_pills_then_a_count():
    """The hard rule: a collapsed merged row is EXACTLY one line. Wrapped chips gave the shelf
    ragged row heights and left the title cell's border ending mid-row. Three pills and a `+N`,
    on a strip that cannot wrap — the rest of the providers are one click down, on the sub-rows."""
    block = _ledger()
    assert 'v-if="r.pillsMore" class="pchip more"' in block and "+{{r.pillsMore}}" in block
    assert ':title="r.pillsMoreTitle"' in block, "the hidden names stay reachable"
    assert ".lprovs{display:flex;flex-wrap:nowrap;overflow:hidden" in INDEX
    rows = INDEX[INDEX.index("platRowsAll(){") :][:3600]
    assert "pills:pills.slice(0,3), pillsMore:Math.max(0, pills.length-3)" in rows


def test_the_three_visible_pills_are_the_ones_worth_seeing():
    """Only three survive, so they are sorted cheapest-first then verified — and the providers with
    no price to show, whose pill would be a bare name, sort to the tail."""
    fn = INDEX[INDEX.index("provPills(eps){") :][:900]
    assert ".sort((a,b)=>(a.n==null)-(b.n==null) || (a.n-b.n) || (b.verified-a.verified))" in fn


def test_a_pill_is_per_provider_not_per_endpoint():
    """TikHub offers the same job four ways; four identical TikHub pills is noise, not a comparison.
    One pill per provider, carrying that provider's cheapest priced endpoint."""
    fn = INDEX[INDEX.index("provPills(eps){") :][:700]
    assert "by.set(e.provider" in fn
    assert "if(n!=null && (cur.n==null || n<cur.n))" in fn  # ...its cheapest
    assert "cur.verified = cur.verified || !!e.verified" in fn  # ...verified if ANY of them is


def test_a_merged_row_counts_providers_and_endpoints_separately():
    """One provider can offer the same job three ways, so the two numbers differ — and the endpoint
    count standing in for the provider count read "6 providers" on a 3-provider row."""
    rows = INDEX[INDEX.index("platRowsAll(){") :][:2900]
    assert "provN:provs.length" in rows
    assert "' provider'+(provs.length===1?'':'s')" in rows
    assert "' endpoint'+(eps.length===1?'':'s')" in rows
    # ...in the cell's tooltip, not on a second line — a second line is what made these rows look
    # broken next to the single ones.
    assert ':title="r.provTitle"' in _ledger()


def test_a_single_row_is_led_by_a_short_title_never_a_paragraph():
    """"Get Showcase Product List" says more than `tiktok.shop.showcase` ever could, so a row only
    one provider serves is led by the endpoint's curated `name`, falling back to its `summary`. But
    a summary is documentation prose — DataForSEO's run to a paragraph — so the row CLIPS it and the
    two-line clamp catches whatever still overflows. The full text is never lost: the expansion
    shows it whole, and the clipped row keeps it in a title attribute."""
    block = _ledger()
    assert "{{r.title}}" in block
    assert 'v-if="r.kind===\'merged\'"' in block
    rows = INDEX[INDEX.index("platRowsAll(){") :][:2400]
    assert "title:this.clip(r.description, 90)" in rows
    clip = INDEX[INDEX.index("clip(text, n){") :][:400]
    assert "lastIndexOf(' ')" in clip, "clip at a word boundary, not mid-token"
    assert ".lsum b{display:-webkit-box;-webkit-line-clamp:2" in INDEX
    # ...and the server picks name-over-summary before it ever reaches the row.
    src = (Path(api.__file__).parent / "catalog_store.py").read_text(encoding="utf-8")
    fn = src[src.index("def domain_rows("):]
    assert '"description": e["name"] or e["summary"] or title' in fn
    assert '"description": view["name"] or view["summary"]' in fn


def test_the_expansion_still_carries_the_full_summary():
    """The row is a title; the prose the row clipped is the expansion's description. Losing it to
    the clip would make the ledger less informative than the page it replaced."""
    block = _ledger()
    assert '<p class="cat-sum">{{e.summary||' in block
    assert block.index('<div class="lep"') < block.index('<p class="cat-sum">')


def test_every_row_expands_not_just_the_merged_ones():
    """The row says what it does; the expansion says how to call it. Which of the two a visitor
    needs is not something the row's shape can decide for them."""
    block = _ledger()
    assert '@click="toggleRow(r)"' in block
    assert ':aria-expanded="!!platOpen[r.key]"' in block
    assert '<tr v-if="platOpen[r.key]"' in block
    tog = INDEX[INDEX.index("toggleRow(r){") :][:200]
    assert "this.platOpen[r.key] = !this.platOpen[r.key]" in tog


def test_a_merged_row_expands_to_collapsed_provider_sub_rows():
    """Level two. Dropping six full parameter tables on one click buried the comparison the merge
    exists to make, so a merged row opens to its providers — one line each: name, short price,
    verification, route — and a provider opens to its own instruction."""
    block = _ledger()
    sub = block[block.index('<button v-if="r.kind===\'merged\'" class="lsub"') :]
    assert '@click.stop="toggleEp(e)"' in sub
    assert ':aria-expanded="!!epOpen[e.id]"' in sub
    assert "{{costShort(e.cost)}}" in sub
    assert 'class="lsub-path mono"' in sub
    assert '<div class="lep" v-if="r.kind!==\'merged\' || epOpen[e.id]">' in block
    tog = INDEX[INDEX.index("toggleEp(e){") :][:120]
    assert "this.epOpen[e.id] = !this.epOpen[e.id]" in tog


def test_a_single_row_still_opens_straight_to_its_detail():
    """One provider is nothing to compare, so a single row skips the middle level — and it renders
    the SAME detail block, so the two paths can't present the instruction differently."""
    block = _ledger()
    assert '<div class="lgrp" v-for="e in r.endpoints"' in block
    assert block.count('<div class="lep"') == 1, "one copy of the detail markup, shared by both paths"
    assert '<button v-if="r.kind===\'merged\'" class="lsub"' in block  # ...the sub-row is the only extra


def test_the_long_metered_phrasing_never_reaches_a_collapsed_line():
    """"per success · price in provider dashboard" is true and belongs in the expanded detail; on a
    collapsed line it wraps the row onto three lines. Collapsed surfaces say "credit-priced"."""
    short = INDEX[INDEX.index("costShort(c){") :][:400]
    assert "return 'credit-priced'" in short
    assert "if(c.value==null) return 'credit-priced'" in short
    assert "return this.costLabel(c)" in short  # ...a published number is short already
    block = _ledger()
    assert "costLabel" not in block, "the long form is a sentence, not a label — it belongs in the facts"
    facts = INDEX[INDEX.index("epFacts(e){") :][:900]
    assert "does not publish the rate" in facts
    assert "this.priceUnit(c.type)" in facts, "...naming the unit it IS billed by"


def test_a_domain_section_needs_a_visible_row_to_exist():
    """A section filed the account/utility plumbing under whatever capability id it happened to
    carry, which conjured sections that had nothing else in them — CAMPAIGNS 0, LOCATION 0, PERSON
    0, SCHOOL 0, TITLE 0 on the People page. A domain renders only if a browse row lands in it."""
    led = INDEX[INDEX.index("platLedger(){") :][:1200]
    assert "if(r.mgmt || (this.platDomain && r.domain!==this.platDomain)) continue;" in led
    assert ".filter(s=>vis[s.domain])" in led, "no visible row, no section"


def test_management_endpoints_collapse_into_one_actions_section_at_the_bottom():
    """Account/utility routes are the provider's own plumbing (webhooks, saved lists, token
    helpers). ONE collapsed section for the whole platform, at the foot of the ledger, counted in
    its heading — not one expander per domain, and not filed by a domain that means nothing here."""
    block = _ledger()
    assert 'v-if="sec.actions"' in block and ">Actions" in block
    assert '@click="platActionsOpen=!platActionsOpen"' in block
    assert "{{sec.count}}" in block
    assert "lmore" not in INDEX, "the per-section management expander and its CSS are gone"
    led = INDEX[INDEX.index("platLedger(){") :][:1200]
    assert "out.push({domain:'Actions', actions:true" in led
    assert "rows:this.platActionsOpen?acts:[]" in led, "collapsed by default, and it renders no rows"
    # ...and it is platform-wide, so a domain chip hides it rather than filtering it
    fn = INDEX[INDEX.index("platActionRows(){") :][:220]
    assert "this.platDomain ? [] : this.platRowsPreDomain.filter(r=>r.mgmt)" in fn
    # ...and "All" counts what the domain chips add up to, not the plumbing behind them
    assert "{{platBrowseCount}}" in block
    assert "platBrowseCount(){ return this.platRowsPreDomain.filter(r=>!r.mgmt).length; }" in INDEX
    # ...and an Actions row says which kind of plumbing it is — the only thing telling them apart
    assert 'v-if="r.mgmt" class="chip lkind"' in block
    assert "{{r.endpoints[0].kind}}" in block


def test_the_filter_bar_narrows_by_domain_text_and_verification():
    block = _ledger()
    assert 'v-for="d in platDomainTabs"' in block
    assert 'v-model="platQ"' in block and 'v-model="platVerifiedOnly"' in block
    assert "{{platStats.rows}} row" in block and "{{platStats.eps}} endpoint" in block
    pre = INDEX[INDEX.index("platRowsPreDomain(){") :][:400]
    assert "!this.platVerifiedOnly||r.verified" in pre
    assert "!q||r.hay.includes(q)" in pre


def test_a_chip_counts_what_it_would_actually_show():
    """The chip counts are taken AFTER the text/verified filters and before the domain one, so a
    chip never promises rows that the other filters have already removed."""
    tabs = INDEX[INDEX.index("platDomainTabs(){") :][:600]
    assert "for(const r of this.platRowsPreDomain)" in tabs
    assert "this.platData.domains||[]" in tabs, "the server's order, so the bar can't reshuffle as you type"


def test_an_empty_filter_result_says_so_instead_of_showing_a_bare_table():
    block = _ledger()
    assert 'v-if="platLedger.length"' in block
    assert "Nothing on this platform matches that filter" in block
    assert '@click="platClearFilters"' in block


def test_the_filter_bar_and_section_headings_are_both_sticky():
    """Two sticky layers under the 57px top bar. The bar's chips SCROLL rather than wrap, because a
    bar that grows a second row as you filter would push the headings out from under it."""
    assert ".lbar{position:sticky;top:var(--lbar-top)" in INDEX
    assert ".lsec td{position:sticky;top:var(--lsec-top)" in INDEX
    assert ".lchips{display:flex;gap:6px;min-width:0;overflow-x:auto" in INDEX
    # An `overflow:hidden` ancestor is a scroll container, and sticky inside one never escapes it.
    assert ".lwrap{overflow:visible" in INDEX and ".ledger{overflow:visible" in INDEX


# --- "you can call this right now" has to survive being skimmed ---------------------------------


def test_the_ready_state_has_a_green_of_its_own():
    """`.chip.ok` is not styled anywhere, so it renders muted grey — a ready capability looked
    exactly like an unavailable one. `.chip.go` is the marketplace's single "runnable" green."""
    css = INDEX[INDEX.index(".chip.go{") :][:400]
    assert "color:var(--green)" in css
    assert "font-weight:600" in css
    assert ".godot{" in INDEX  # ...led by a dot, so it reads as a status and not a label


def test_a_callable_row_is_marked_down_its_leading_edge():
    """Picking the option you can already call is the reason to read the table, so a row with a
    connected provider carries a rule down its edge — findable without reading."""
    block = _ledger()
    assert ':class="{open:platOpen[r.key], go:r.ready}"' in block
    assert ".lrow.go td:first-child{box-shadow:inset 3px 0 0" in INDEX
    rows = INDEX[INDEX.index("platRowsAll(){") :][:3400]
    assert "ready: eps.some(e=>this.catConnected(e.provider))" in rows


def test_cheapest_orders_on_the_servers_usd_not_a_local_fx_constant():
    """A ¥0.10 endpoint and a $0.001 one have to be orderable. The catalog owns the FX table so one
    rate refresh re-prices every screen at once and the dashboard cannot drift from the CLI."""
    usd = INDEX[INDEX.index("costUsd(c){") :][:500]
    assert "typeof c.usd==='number' ? c.usd : null" in usd
    assert "0.14" not in usd  # no hard-coded rate survives here
    cheap = INDEX[INDEX.index("capCheapest(eps){") :][:600]
    assert "this.nativeAmount(e.cost)" in cheap  # ...with the provider's own figure alongside


def test_an_unpriced_endpoint_can_never_be_the_cheapest_option():
    """"from —" is worse than naming the cheapest price we do know. A row quota is not a price
    either, so it cannot win against a per-call rate — and the server would happily convert its
    row COUNT into dollars, which is why it is excluded before `usd` is read."""
    usd = INDEX[INDEX.index("costUsd(c){") :][:500]
    assert "if(c.type==='quota_rows') return null;" in usd
    assert usd.index("quota_rows") < usd.index("c.usd")


def test_credit_metered_providers_are_not_reported_as_unpriced():
    """Apollo, PDL, Hunter and friends bill in their own credits: no dollar figure, but the price IS
    documented, in the note on every row. An em-dash would be flatly wrong there, and it is the
    entire People/Company half of the catalog."""
    rows = INDEX[INDEX.index("platRowsAll(){") :][:3400]
    assert "eps.some(e=>e.cost&&e.cost.note) ? 'see provider' : '—'" in rows


def test_a_connected_oauth_endpoint_counts_as_the_free_path():
    """The account you already connected is the licence — calling it costs nothing more, which is
    usually the right answer and must beat any metered scraper."""
    free = INDEX[INDEX.index("capFree(e){") :][:400]
    assert "this.catConnected(e.provider)" in free
    assert "e.scope==='own_account'" in free
    assert "e.cost.type==='free'" in free


# --- the expansion: the whole instruction, without leaving the page -----------------------------


def test_the_expansion_is_two_tabs_request_first():
    """What you SEND and what comes BACK are two documents; stacked, the expansion was a page you
    scrolled rather than read. Request leads — it is the half that was missing, and the half that
    tells you whether the endpoint is callable at all."""
    block = _ledger()
    bar = block[block.index('<div class="ltabs">') : block.index('</div>', block.index('<div class="ltabs">'))]
    assert bar.index(">Request<") < bar.index(">Example response<")
    assert "epTabOf(e)===\'req\'" in block and "epTabOf(e)===\'res\'" in block


def test_an_endpoint_with_no_example_has_no_second_tab_at_all():
    """Not a greyed-out tab, not a placeholder: a disabled tab is a promise the catalog can't keep,
    and it draws the eye to the one thing that isn't there. Such an endpoint has one tab, which
    reads as a label for the pane under it."""
    block = _ledger()
    assert '<button v-if="e.has_example" class="ltab"' in block
    assert "disabled" not in block, "no faded tab, and no placeholder to go with it"
    assert "No captured example" not in INDEX and "No example was captured for this" in INDEX  # ...only as a load error
    # ...and a stale 'res' tab state can't survive on an endpoint that has no example
    fn = INDEX[INDEX.index("epTabOf(e){") :][:200]
    assert "(t==='res' && !e.has_example) ? 'req'" in fn
    assert "if(tab==='res' && !e.has_example) return;" in INDEX[INDEX.index("setEpTab(e, tab){") :][:300]


def test_an_expanded_endpoint_lists_its_parameters():
    """The captured response answered "what comes back" while this half was missing, so every
    endpoint looked uncallable until you left for the provider's docs."""
    block = _ledger()
    assert '<div class="prm"' in block
    assert "{{p.name}}" in block and "{{p.type||'—'}}" in block
    assert ">required</span>" in block and ">optional</span>" in block
    assert "e.g. {{fmtExample(p.example)}}" in block
    assert "v-if=\"e.input && e.input.note\"" in block  # the top-level hint line


def test_parameters_are_grouped_by_where_they_go():
    """A name alone doesn't tell you whether it belongs in the query string, the path or the body.
    Query first, then path, then body — the order you fill them in for the common GET."""
    fn = INDEX[INDEX.index("paramSections(e){") :][:700]
    assert fn.index("add('query'") < fn.index("add('path'") < fn.index("add('body'")
    assert "i.bodyType" in fn  # the body section says which encoding it wants


def test_an_endpoint_with_no_catalogued_params_says_so_rather_than_showing_an_empty_table():
    """Every one of the ~1850 extended endpoints carries `input: null`, so this is the common case,
    not an edge one."""
    block = _ledger()
    assert 'v-else class="prm-none"' in block
    assert "provider's docs have them" in block


def test_the_params_table_drops_the_global_table_chrome():
    """The global `table` rule paints a panel, a border and a radius and `th` a filled bar — inside
    the .prm box that reads as a stray highlight, and it clips the first column."""
    css = INDEX[INDEX.index(".prm-t{") :][:400]
    assert "background:none;border:0" in css
    assert ".prm-t th{background:none" in INDEX


def test_the_expansion_carries_a_paste_ready_call_line():
    """Finding the endpoint was never the goal — running it is. The `treg call` line is served on
    the row itself (`call_template`), so the instruction is complete without a second request."""
    block = _ledger()
    assert '<div class="lcall" v-if="e.call_template">' in block
    assert "{{e.call_template}}" in block
    assert '@click.stop="copyCall(e)"' in block


def test_the_expansion_carries_the_provider_wide_facts():
    """Limits and the rate card live once per provider at the top of its yaml, and an expanded row
    is exactly where they are needed — so the platform response ships them alongside the rows."""
    block = _ledger()
    assert 'v-if="epFacts(e).length"' in block
    facts = INDEX[INDEX.index("epFacts(e){") :][:900]
    assert "c.note" in facts and "'limits'" in facts and "'pricing_url'" in facts
    assert "this.platData.providers" in INDEX[INDEX.index("provFact(service, key){") :][:260]


def test_own_account_rows_are_labelled_and_distinct_from_scraper_rows():
    """An OAuth endpoint reads the account you connected; a scraper endpoint reads any handle. That
    is the difference between two different products, so it gets a badge and a colour."""
    assert """<span v-if="e.scope==='own_account'" class="chip own\"""" in INDEX
    assert ">your account</span>" in INDEX
    assert "Reads the account YOU connect via OAuth, not arbitrary public accounts" in INDEX
    assert """v-else-if="e.scope==='any_account'" class="chip any\"""" in INDEX
    assert ".chip.own{" in INDEX


def test_a_metered_endpoint_with_no_published_rate_says_where_the_price_lives():
    """A bare "per success" reads as free, and an em-dash hides that we do know how it is billed."""
    cost = INDEX[INDEX.index("costLabel(c){") :][:700]
    assert "per '+per+' · price in provider dashboard'" in cost


def test_the_ledger_rows_and_their_detail_live_in_the_platform_view():
    assert _enclosing_views('v-for="r in sec.rows"') == ["platform"]
    assert _enclosing_views('<tr v-if="platOpen[r.key]"') == ["platform"]


def test_no_ledger_cell_is_taken_out_of_the_table_layout():
    """A `<td>` with `display:flex` stops being a table-cell: the browser wraps it in an anonymous
    cell that stretches to the row height while the flex box sizes to its content and keeps the
    border. The separator under column one then lands a pixel above the one under column two — a
    visible seam down the table, with the hover background split along it. Flex goes on a wrapper
    INSIDE the cell, never on the cell."""
    block = _ledger()
    assert '<td class="lsum">' in block and '<div class="lsum-i">' in block
    css = INDEX[INDEX.index(".lsum-i{") :][:200]
    assert "display:flex" in css
    # ...and no rule anywhere gives a ledger cell its own display.
    for rule in ("td.lsum{", ".lsum{display", ".lprice{display", ".ledger td{display"):
        assert rule not in INDEX, f"{rule} takes a ledger cell out of the table layout"


def test_the_row_pair_rides_a_template_inside_the_table():
    """A row and its expanded detail are two <tr>s, so they must be wrapped in a <template>, not a
    <tbody> — a per-row <tbody> makes `tr:last-child` strip every row separator."""
    block = _ledger()
    table = block.index('<table class="ledger">')
    assert block.index('<template v-for="r in sec.rows"') > table
    assert "<tbody" not in block


def test_example_responses_are_fetched_only_when_their_tab_is_opened():
    """A platform can carry hundreds of endpoints; fetching every captured response with the page
    would make the view cost more than the calls it describes. Opening the tab is the trigger, and
    it fires once — a second visit to the tab must not re-fetch."""
    fn = INDEX[INDEX.index("async loadExample(e){") :][:700]
    assert "if(this.platEx[e.id]) return;" in fn, "loaded once, not on every tab switch"
    assert "'/catalog/examples/'" in fn
    assert "if(tab==='res') this.loadExample(e);" in INDEX[INDEX.index("setEpTab(e, tab){") :][:300]


def test_both_panes_are_capped_and_scroll_rather_than_growing_the_page():
    """A DataForSEO body carries thirty parameters. Uncapped, one expansion pushes every row below
    it off the screen — so both panes are the same bounded box that scrolls inside itself."""
    assert ".prm{max-height:320px;overflow-y:auto}" in INDEX
    assert ".cat-ex pre{max-height:320px}" in INDEX


def test_the_run_actions_lead_the_expansion_tab_bar():
    """The tab bar carries docs + the two run actions, visible the moment the expansion opens.
    Try-it is the primary (ink-fill) CTA for a key/token provider — trying on treg's key is what most
    visitors want — with the own-key path demoted to a secondary "Bring your own key". For an OAuth
    provider treg can't serve on its own key, so Connect is the primary instead and Try-it is
    secondary. The green Connected state replaces the connect button once an account exists.

    Each action is ONE button whose handler forks on `publicCatalog` — signed out, all three open
    the sign-in modal instead, because calling needs a team. The member half of every fork is what
    is asserted below; it must keep its shape when someone edits the public half."""
    block = _ledger()
    # Sized to the whole action group, not a round number — the window has been outgrown once
    # already (by the publicCatalog forks) and silently cut the last assertions out of range.
    bar = block[block.index('<span class="ltabs-r">') :][:3400]
    # Try-it: primary UNLESS the provider is OAuth
    assert 'openEpTry(e)' in bar and ':class="{primary:!mkOauth(e.provider)}"' in bar
    # OAuth → Connect is the ink-fill primary
    assert 'v-else-if="mkOauth(e.provider)" class="btn sm primary"' in bar and "openProvider(e.provider)" in bar
    assert ">Connect {{e.provider_display||e.provider}}</button>" in bar
    # key/token → own key is the secondary
    assert 'v-else-if="mkKnown(e.provider)" class="btn sm"' in bar and ">Bring your own key</button>" in bar
    assert 'v-if="catConnected(e.provider)" class="chip go"' in bar and ">Connected</span>" in bar
    assert 'v-if="e.docs_url"' in bar  # ...docs sit up here too, not below the fold
    assert ".ltabs-r .btn.primary{background:var(--inverse)" in INDEX


def test_provider_page_links_into_the_catalog():
    """Navigation runs both ways: an integration page names the platforms its catalog covers."""
    assert _enclosing_views('v-for="pl in mkPlatforms"') == ["provider"]


# --- code surfaces follow the theme -------------------------------------------------------------


def test_the_syntax_ramp_is_themed_not_hard_coded_for_dark():
    """Light mode painted the code BLOCK light (the generic `[data-theme=light] pre` rule outranks
    `.lc-codewrap pre`) while the ramp stayed drawn for a dark one — a near-white command on a
    near-white background. One ramp, as variables, with a light set that clears 4.5:1."""
    for page in (INDEX, TUTORIAL):
        for var in ("--sx-cmd", "--sx-var", "--sx-str", "--sx-flag", "--sx-cmt"):
            assert page.count(var) >= 3, f"{var} needs a dark value, a light value and a use"
    # No syntax rule may carry a hex literal: a literal is one theme's value, and three earlier
    # generations of this ramp each left one behind (#5f9ea0, #19D0E8, #e0703f, #f0f0ee…).
    rule = re.compile(r"(?m)^\s*(\.(?:hl|s)-[\w-]+[^{}\n]*)\{([^}]*)\}")
    for page, name in ((INDEX, "index.html"), (TUTORIAL, "tutorial.html")):
        for sel, body in rule.findall(page):
            assert "#" not in body, f"{name}: {sel.strip()} hard-codes {body} — use the --sx-* ramp"
    assert ".hl-cmd,.hl-kw,.s-kw{color:var(--sx-cmd)" in INDEX
    assert "--sx-cmd:#1a1a1a" in INDEX and "--sx-cmd:#f0f0ee" in INDEX


def test_a_code_block_and_its_ink_come_from_the_same_theme():
    """The surface is a token too, so a block and the text on it can never again be specified by
    different generations of the sheet. The inner <pre> must out-specify BOTH generic `pre` rules —
    that mismatch is what put a light panel inside a dark wrapper."""
    assert "--code-bg:#161518" in INDEX and "--code-bg:#f4f4f1" in INDEX
    css = INDEX[INDEX.index("RESTYLE — 3.8 code surfaces") :]
    assert ".term,.lc-codewrap," in css and "background:var(--code-bg);color:var(--code-ink)" in css
    assert ":root[data-theme=light] .lc-codewrap pre" in css
    assert ":root:not([data-theme=light]) .lc-codewrap pre" in css
    # ...and the copy button is not a dark pill on a light block
    assert ".lc-cp{background:var(--code-btn);color:var(--code-ink)" in css


def test_muted_output_panes_use_the_ramps_comment_grey():
    """`--muted` is 3.7:1 on the light code surface — fine for a caption, not for the pane that
    shows what a command printed."""
    assert "pre.term.out{color:var(--sx-cmt)}" in INDEX
    assert "pre.out{color:var(--sx-cmt)}" in TUTORIAL


def test_dashboard_delete_org_sends_the_confirmation_slug():
    """DELETE /orgs/{id} refuses without `?confirm=<slug>` (it is irreversible and reachable by path
    normalization from any deeper org route). The dashboard already makes the human type the slug —
    if this ever stops being sent, Delete org silently 422s for every dashboard user."""
    assert "'/orgs/'+this.activeOrgId+'?confirm='" in INDEX


# --- referrals (view==='referrals') -----------------------------------------------------------


def test_referrals_is_a_top_level_view():
    """Nested inside another view it would render nowhere, and the nav button would look dead —
    the exact silent failure the dialog checks above exist for."""
    assert _enclosing_views("<template v-if=\"view==='referrals'\">") == []


def test_referrals_is_registered_in_BOTH_view_whitelists():
    """`go('referrals')` works on click regardless; these two lists are what make it survive a
    RELOAD and a BACK button. One is `viewFromHash` (fresh load / deep link), the other is the
    popstate handler. Missing either is invisible until someone refreshes the page."""
    assert INDEX.count("'connections','referrals'") == 2


def test_referral_page_shows_why_a_referral_paid_nothing():
    """"I referred someone and got nothing" is the ticket this program generates. `capped` and
    `rejected` must render a reason on the page rather than an empty cell."""
    assert "refStatus(r)" in INDEX
    assert "Over your limit" in INDEX and "Not eligible" in INDEX


def test_the_referral_link_is_not_gated_behind_paying_us():
    """Every signed-in person gets a link, free tier included — they are most of the userbase on a
    product pitched as "$1.00 free, no card", and the likeliest to tell a friend. The `!eligible`
    branch survives only for the degenerate case of having no team to pay a reward into, so it must
    NOT ask anyone to add funds."""
    assert 'v-if="!ref.eligible"' in INDEX
    assert "Create a team to unlock your link" in INDEX
    assert "Add funds once to unlock your link" not in INDEX


def _referrals_view() -> str:
    """The Referrals view block ONLY. Anchored on the template tag, not the bare `view==='referrals'`
    string — the nav button matches that first and would slice from the sidebar."""
    start = INDEX.index("<template v-if=\"view==='referrals'\">")
    end = INDEX.index("<template v-if=\"view==='help'\">", start)
    return INDEX[start:end]


def test_referral_stat_tiles_use_the_sheets_own_class_names():
    """`.stat` styles `.n` (the figure, 26px accent) and `.l` (the uppercase caption) — and nothing
    else. A hand-rolled `.k`/`.v` pair renders as two lines of unstyled body text that still *look*
    like content, so nothing errors and the bug ships. Order matters too: figure first."""
    block = _referrals_view()
    assert 'class="statgrid"' in block
    assert block.count('<div class="n">') == 3 and block.count('<div class="l">') == 3
    assert 'class="k"' not in block and 'class="v"' not in block
    assert block.index('<div class="n">') < block.index('<div class="l">')


def test_referral_stat_tiles_fill_the_column():
    """The sheet's `.statgrid` is auto-FILL, which at this width lays out four 150px tracks and
    leaves the fourth empty — three tiles then stop short of the right edge the card above and the
    table below both reach. auto-fit collapses the empty track."""
    assert "grid-template-columns:repeat(auto-fit,minmax(150px,1fr))" in _referrals_view()


def test_referral_table_keeps_the_sheets_cell_padding():
    """A bare `<table>` IS a card here (background, border, radius) and `th,td` already carry
    10px/13px. Overriding that to `padding:8px 0` — copied from a table that lives INSIDE a card —
    puts the right-aligned amount flush against the card's own edge, which reads as clipped."""
    block = _referrals_view()
    table = block[block.index("<table"):block.index("</table>")]
    assert "padding:" not in table, "let th,td carry the padding"
    assert "<th>" in table, "house tables have a header row"


def test_the_referral_column_is_one_width():
    """Card, tiles and table sit in a SINGLE max-width container. Three separate max-widths is what
    made them start and end at three different x-positions."""
    assert _referrals_view().count("max-width:") == 1


def test_the_billing_page_names_the_bonus_on_the_qualifying_presets():
    """A referred team decides HOW MUCH to add on this screen, and the first preset ($5) is below the
    minimum. A note alone is not enough — the amount is chosen at the buttons, so the qualifying ones
    have to say so themselves."""
    at = INDEX.index('<div class="fundgrid"')
    grid = INDEX[at : INDEX.index("</div>", INDEX.index("</button>", at))]
    assert "refPresetBonus(p)" in grid, "presets must mark which ones earn the bonus"
    assert "billing.referral_offer" in INDEX


def test_the_referral_offer_is_hidden_from_teams_that_were_not_referred():
    """`v-if` on the offer object, so a team that arrived on its own sees the billing page exactly as
    it was before this shipped."""
    assert 'v-if="billing.referral_offer"' in INDEX


def test_the_billing_prompt_renders_the_masked_referrer_never_a_raw_one():
    """The template must read `referrer_masked`. Reaching for any full-address field here would
    publish an influencer's email to every stranger who signs up through their public link."""
    at = INDEX.index("You were invited")
    block = INDEX[at : INDEX.index("</div>", at)]
    assert "billing.referral_offer.referrer_masked" in block
    assert "referrer_email" not in INDEX


def test_the_referral_preset_helper_null_guards_before_reading_the_offer():
    """`referral_offer` is null for every team that was NOT referred — most of them. Reading through
    it throws inside a render, and a render error in Vue blanks the entire dashboard, not just the
    row. This shipped once, as a blank billing page, when a guard clause was folded into a larger
    expression and then edited away."""
    at = INDEX.index("refPresetBonus(usd)")
    body = INDEX[at : INDEX.index("},", at)]
    assert "if(!o) return 0;" in body
    assert body.index("if(!o) return 0;") < body.index("o.remaining_micro")
