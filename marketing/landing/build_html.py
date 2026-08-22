#!/usr/bin/env python3
"""Render the built markdown pages into the site's HTML, in the landing-page design system.

    python3 build.py && python3 build_html.py

Reads   marketing/landing/dist/*.md   (produced by build.py — shared blocks already expanded)
Writes  src/treg/web/usecase-<key>.html

The markdown stays the single source of truth. NEVER hand-edit the generated HTML; it is
overwritten on every run.

Safety rule that matters: the ad/creator kit and the fact-key table are INTERNAL. They carry
bid keywords, negative keywords and our own conversion hypotheses. This script cuts the
document at the ad-kit heading and refuses to emit anything after it.

Needs `markdown` (pip install markdown) — a build-time dependency only, never imported by
the shipped package.
"""
import re, sys, pathlib, html
import markdown as md

HERE = pathlib.Path(__file__).parent
DIST = HERE / "dist"
WEB = HERE.parent.parent / "src" / "treg" / "web"

# page id -> url slug + the file key we write. Kept explicit rather than derived from the
# filename so a renamed source file can never silently change a live URL.
PAGES = {
    "p1": ("seo-data-for-ai-agents", "seo"),
    "p2": ("lead-enrichment-for-ai-agents", "enrichment"),
    "p3": ("social-trend-research-for-ai-agents", "social"),
    "p4": ("competitor-ad-research-for-ai-agents", "ads"),
    "p5": ("company-research-for-ai-agents", "company"),
}
CUT_AT = "# Ad and creator kit"


INTERNAL_LABEL = re.compile(r"\(Vertical note:\s*(.)")


def strip_editor_labels(body):
    """`*(Vertical note: worth doing here…)*` is guidance for whoever edits the copy; the sentence
    after the colon is genuinely useful to a reader, the label is not. Ship the aside, drop the
    label."""
    return INTERNAL_LABEL.sub(lambda m: "(" + m.group(1).upper(), body)


# Tokens that must NEVER reach a public page. The ad-kit cutoff catches the big block; this catches
# the annotations that live ABOVE it, which is how "Vertical note" shipped on all five pages.
LEAK_TOKENS = ("Vertical note", "ad_keywords", "Negative keywords", "Responsive search ad",
               "Measurable hypothesis", "hypothesis:", "TO BE POPULATED", "verify_after",
               "Numbers used on this page")


def assert_no_leaks(name, markup):
    hits = [tok for tok in LEAK_TOKENS if tok.lower() in markup.lower()]
    if hits:
        raise SystemExit(f"{name}: INTERNAL CONTENT WOULD BE PUBLISHED -> {hits}")


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit("no front-matter")
    fm, body = {}, text[m.end():]
    for line in m.group(1).splitlines():
        k = re.match(r"^(\w+):\s*(.*)$", line)
        if k and k.group(2).strip():
            v = k.group(2).strip()
            v = re.sub(r"\s+#\s*\d+.*$", "", v)          # strip the char-count comment
            fm[k.group(1)] = v.strip('"')
    return fm, body


def inline(text):
    """Markdown → HTML for a fragment. Tables included; no wrapping <p> stripped."""
    return md.markdown(text.strip(), extensions=["tables"])


def blocks(text):
    """Split a section body into blank-line-separated blocks, keeping fenced code intact."""
    out, buf, fence = [], [], False
    for line in text.splitlines():
        if line.startswith("```"):
            fence = not fence
            buf.append(line)
            if not fence:
                out.append("\n".join(buf)); buf = []
            continue
        if not fence and not line.strip():
            if buf: out.append("\n".join(buf)); buf = []
        else:
            buf.append(line)
    if buf: out.append("\n".join(buf))
    return [b for b in out if b.strip()]


def is_cta(b):
    return b.strip().startswith("**[") and b.strip().endswith("]**")


def cta_html(b, page_id, prompt_sel="#prompt"):
    """The bracketed placeholders become the real buttons. Secondary scrolls to the workflow.
    `prompt_sel` is the prompt this button copies — the most recent one rendered, so a page with
    more than one workflow copies the right block rather than always the first."""
    labels = re.findall(r"\*\*\[\s*([^\]]+?)\s*\]\*\*", b)
    parts = []
    for lb in labels:
        if "Copy Prompt" in lb:
            parts.append(f'<button class="candy" data-copy="{prompt_sel}" '
                         f'data-ev="lp_copy_prompt" data-page="{page_id}">{html.escape(lb)}</button>')
        elif "See the Example" in lb:
            parts.append(f'<a class="ghostbtn" href="#workflow" data-ev="lp_cta_secondary" '
                         f'data-page="{page_id}">{html.escape(lb)}</a>')
        else:
            parts.append(f'<a class="candy" href="/app?ref={page_id}" data-ev="lp_cta_primary" '
                         f'data-page="{page_id}">{html.escape(lb)}</a>')
    return '<div class="ctas">' + "".join(parts) + "</div>"


def code_text(b):
    return "\n".join(b.splitlines()[1:-1])


# Display name → the slug treg already serves at /logos/<slug>.svg. The provider tables ARE the
# evidence on these pages; a wall of names reads as assertion, the same table with real brand marks
# reads as a record. Longest keys first so "Hunter Discover" never matches as "Hunter".
LOGOS = {
    "The Companies API": "thecompaniesapi", "Google Ads Transparency": "google-ads",
    "Hunter Discover": "hunter", "Meta, own key": "meta-ad-library", "ScrapeCreators": "scrapecreators",
    "SE Ranking": "seranking", "DataForSEO": "dataforseo", "Crunchbase": "crunchbase",
    "Coresignal": "coresignal", "JustOneAPI": "justoneapi", "LeadMagic": "leadmagic",
    "Serpstat": "serpstat", "Majestic": "majestic", "Semrush": "semrush", "SerpApi": "serpapi",
    "Diffbot": "diffbot", "Google Ads": "google-ads", "Apollo": "apollo", "Hunter": "hunter",
    "tikhub": "tikhub", "Lusha": "lusha", "Apify": "apify", "Akta": "akta", "Moz": "moz",
    "PDL": "pdl", "Meta": "meta-ad-library",
}
_LOGO_KEYS = sorted(LOGOS, key=len, reverse=True)
PROVIDERS_SEEN = set()


# The page said "copy this into Claude Code" while never telling a first-time visitor how treg gets
# INTO Claude Code. Pasting the workflow with no account and no MCP server does nothing, so the
# advertised conversion — a first successful call — was unreachable from the page. This box is
# rendered immediately before the first prompt on every page.
SETUP_BOX = """<div class="steplabel"><span class="n">1</span><b>Set treg up in your agent</b>
  <span class="once">first time only</span></div>
<p class="stephint">Paste this into the same agent. It installs the CLI, signs you in and registers the
tools. One line, once &mdash; already set up? Skip to step 2.</p>
<div class="promptbox">
  <div class="ph"><span>setup</span>
    <button class="copybtn" data-copy="#setup" data-ev="lp_copy_setup" data-page="{page_id}">copy</button>
  </div><pre id="setup">set up treg &mdash; https://treg.to/llms.txt</pre>
</div>
<div class="steplabel"><span class="n">2</span><b>Run the workflow</b></div>"""


PRICEWALL = """<div class="pricewall">
  <div class="pw old"><span class="k">Instead of</span><span class="v">{price_old}</span>
    <span class="s">{price_old_label}</span></div>
  <div class="pw arrow" aria-hidden="true">&rarr;</div>
  <div class="pw new"><span class="k">You paid</span><span class="v">{price_new}</span>
    <span class="s">{price_new_label}</span></div>
</div>"""


def logo_tile(slug, name):
    """A bare inline <img> followed by the name — NOT an inline-flex wrapper.

    Wrapping them in `display:inline-flex` gave the box its own baseline, so any text that followed
    in the same cell ("tikhub" + "search timeline") sat on two different baselines.
    """
    return (f'<img class="plogo" src="/logos/{slug}.svg" alt="" loading="lazy" '
            f'width="18" height="18">{html.escape(name)}')


def label_cells(tbl_html):
    """Stamp each <td> with its column name so the mobile stacked layout can show it.

    Without this the phone breakpoint renders a column of bare values with no idea which is price
    and which is success rate — worse than the horizontal scroller it replaces.
    """
    heads = [re.sub(r"<[^>]+>", "", h).strip()
             for h in re.findall(r"<th[^>]*>(.*?)</th>", tbl_html, re.S)]
    if not heads:
        return tbl_html

    def row(m):
        cells = re.findall(r"<td[^>]*>.*?</td>", m.group(1), re.S)
        if not cells:                    # the <th> header row — nothing to label
            return m.group(0)
        out = []
        for i, c in enumerate(cells):
            label = heads[i] if i < len(heads) else ""
            # A blank header (the compare table's first column) or a blank cell (a grouped
            # continuation row) gets no label rather than an empty one.
            if label and re.sub(r"<[^>]+>", "", c).strip():
                c = c.replace("<td", f'<td data-label="{html.escape(label)}"', 1)
            out.append(c)
        return "<tr>" + "".join(out) + "</tr>"

    # One lazy group, no nested quantifier. The previous `(?:<td.*?</td>\s*)+` put a lazy wildcard
    # inside a repeated group, which backtracks exponentially on a long row (CodeQL py/redos).
    return re.sub(r"<tr>(.*?)</tr>", row, tbl_html, flags=re.S)


def add_logos(tbl_html):
    """Put the brand mark beside the provider name in the first column of a table."""
    used = set()

    def cell(m):
        open_tag, inner, close = m.group(1), m.group(2), m.group(3)
        text = re.sub(r"<[^>]+>", "", inner).strip()
        for key in _LOGO_KEYS:
            if text == key or text.startswith(key + " "):
                used.add(LOGOS[key])
                marked = inner.replace(key, logo_tile(LOGOS[key], key), 1)
                return open_tag + marked + close
        return m.group(0)

    # Every cell, not just the first: some tables group by platform in column 1 and name the
    # provider in column 2 (p3, p4), so a first-cell-only rule marked nothing on those pages.
    # Matching is still whole-cell, so a provider named inside prose is never touched.
    out = re.sub(r"(<td[^>]*>)(.*?)(</td>)", cell, tbl_html, flags=re.S)
    return out, used


# ---------------------------------------------------------------- sections

def render_hero(body, page_id):
    bs = blocks(body)
    head = lede = trust = sub = ""
    ctas = ""
    for b in bs:
        s = b.strip()
        if s.startswith("### "):
            head = s[4:].strip()
        elif is_cta(s):
            ctas = cta_html(s, page_id)
        elif s.startswith("*Sub-line:*"):
            sub = inline(s.replace("*Sub-line:*", "").strip())
        elif s.startswith("$1.00"):
            trust = html.escape(s)
        elif not lede:
            lede = inline(s)
    return f"""<header class="hero"><div class="wrap">
  <div class="kicker">2,600+ tools · 40+ providers · one key</div>
  <h1>{html.escape(head)}</h1>
  <div class="lede">{lede}</div>
  {ctas}
  <div class="trust">{trust}</div>
  <div class="subline">{sub}</div>
  <div class="provstrip" id="provstrip"></div>
</div></header>"""


def render_generic(title, body, page_id, anchor=None, label=None):
    """Prose + tables + code, in document order. Used for compare / workflow / proof."""
    out = []
    bs = blocks(body)
    n_prompt = 0
    current_prompt = "#prompt"          # what a "Copy Prompt" button copies right now
    just_emitted_prompt = False         # see the CTA branch: kills the duplicate copy button
    for i, b in enumerate(bs):
        s = b.strip()
        if s.startswith("```"):
            txt = html.escape(code_text(b))
            # A fence is a PROMPT if a "Copy Prompt" affordance follows it, otherwise it is sample
            # output. Counting fences instead broke as soon as a page carried a second prompt.
            nxt = bs[i + 1].strip() if i + 1 < len(bs) else ""
            if is_cta(nxt) and "Copy Prompt" in nxt:
                n_prompt += 1
                if n_prompt == 1:
                    out.append(SETUP_BOX.format(page_id=page_id))
                pid = "prompt" if n_prompt == 1 else f"prompt-{n_prompt}"
                current_prompt = "#" + pid
                out.append(f'''<div class="promptbox">
  <div class="ph"><span>the prompt</span>
    <button class="copybtn" data-copy="#{pid}" data-ev="lp_copy_prompt" data-page="{page_id}">copy</button>
  </div><pre id="{pid}">{txt}</pre>
</div>''')
                just_emitted_prompt = True
            else:
                out.append(f'<div class="sample"><div class="sbar">what comes back</div>'
                           f'<pre>{txt}</pre></div>')
        elif is_cta(s):
            # Every prompt block already carries its own `copy` button. A big "Copy Prompt" placed
            # immediately under it is the same action twice, 40px apart — so drop that one and keep
            # the copies that follow the sample output, where the reader has seen the payoff.
            only_copy = all("Copy Prompt" in lb
                            for lb in re.findall(r"\*\*\[\s*([^\]]+?)\s*\]\*\*", s))
            if just_emitted_prompt and only_copy:
                just_emitted_prompt = False
                continue
            out.append(cta_html(s, page_id, current_prompt))
        elif s.startswith("|"):
            cls = "compare" if "The old way" in s else "data"
            tbl = inline(s).replace("<table>", '<table class="%s">' % cls)
            tbl = label_cells(tbl)
            if cls == "data":
                tbl, used = add_logos(tbl)
                PROVIDERS_SEEN.update(used)
            out.append('<div class="tablewrap">%s</div>' % tbl)
        else:
            out.append(inline(s))
        if not s.startswith("```"):
            just_emitted_prompt = False
    a = f' id="{anchor}"' if anchor else ""
    lab = f'<div class="seclab">{label}</div>' if label else ""
    return (f'<section{a}><div class="wrap">{lab}<h2>{html.escape(title)}</h2>'
            + "".join(out) + "</div></section>")


def render_cards(title, body, label=None):
    cards = []
    cur = None
    for b in blocks(body):
        s = b.strip()
        m = re.match(r"^\*\*(.+?)\*\*\s*$", s)
        if m:
            cur = {"h": m.group(1), "p": []}
            cards.append(cur)
        elif cur is not None:
            cur["p"].append(s)
        # a heading-and-body in ONE block (title line then prose) is the common shape
        if cur is not None and not m and s.startswith("**") and "**\n" in s:
            pass
    # the source writes "**Title.**\nbody" inside one block — handle that shape too
    if not cards:
        for b in blocks(body):
            s = b.strip()
            m = re.match(r"^\*\*(.+?)\*\*\n(.+)$", s, re.S)
            if m:
                cards.append({"h": m.group(1), "p": [m.group(2)]})
    inner = "".join(
        f'<div class="card"><h4>{inline(c["h"])[3:-4]}</h4>{inline(" ".join(c["p"]))}</div>'
        for c in cards)
    lab = f'<div class="seclab">{label}</div>' if label else ""
    return (f'<section><div class="wrap">{lab}<h2>{html.escape(title)}</h2>'
            f'<div class="cards">{inner}</div></div></section>')


def render_who(title, body, label=None):
    # Isolate the bullet list first. Bounding the item regex with \Z let the final item swallow
    # whatever followed the list, so take only the contiguous list block: lines starting with
    # "- " plus their indented continuation lines.
    lines, keep = body.splitlines(), []
    for ln in lines:
        if ln.startswith("- "):
            keep.append(ln)
        elif keep and (ln.startswith("  ") and ln.strip()):
            keep.append(ln)
        elif keep:
            break
    listing = "\n".join(keep)

    items = []
    for m in re.finditer(r"^- \*\*(.+?)\*\*(.*?)(?=^- |\Z)", listing, re.S | re.M):
        rest = " ".join(m.group(2).split())          # collapse the wrapped source lines
        body_html = inline(rest)
        body_html = re.sub(r"^<p>|</p>$", "", body_html.strip())
        items.append(f'<div><b>{html.escape(m.group(1))}</b> {body_html}</div>')
    lab = f'<div class="seclab">{label}</div>' if label else ""
    return (f'<section><div class="wrap">{lab}<h2>{html.escape(title)}</h2>'
            f'<div class="who">{"".join(items)}</div></div></section>')


def render_faq(title, body, label=None):
    """'**Question?**\\nanswer' pairs → <details>. First one open, so the section reads as content."""
    # A question is a line that is ENTIRELY a bold question — nothing before it, nothing after it
    # on that line. The previous pattern let the capture start at any bold run, so an answer that
    # merely opened with bold (p3's "**62%** of calls…") swallowed the following question and
    # produced a 433-character <summary>. It rendered on a live page and every test still passed.
    lines = body.splitlines()
    starts = [i for i, ln in enumerate(lines)
              if re.fullmatch(r"\*\*[^*].*\?\*\*", ln.strip())]
    qs = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        q = lines[i].strip()[2:-2]
        qs.append((q, "\n".join(lines[i + 1:end]).strip()))
    inner = "".join(
        f'<details{" open" if i == 0 else ""} data-ev="lp_objection_open" data-q="{html.escape(q)}">'
        f'<summary>{html.escape(q)}</summary><div class="body">{inline(a)}</div></details>'
        for i, (q, a) in enumerate(qs))
    lab = f'<div class="seclab">{label}</div>' if label else ""
    return (f'<section><div class="wrap">{lab}<h2>{html.escape(title)}</h2>{inner}</div></section>')


def render_final(body, page_id):
    bs = blocks(body)
    head = trust = ""
    ctas = ""
    for b in bs:
        s = b.strip()
        if s.startswith("### "):
            head = s[4:].strip()
        elif is_cta(s):
            ctas = cta_html(s, page_id)
        elif s.startswith("$1.00"):
            trust = inline(s)
    return f"""<section class="final"><div class="wrap">
  <h2>{html.escape(head)}</h2>
  {ctas}
  <div class="trust">{trust}</div>
</div></section>"""


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<meta name="description" content="{desc}"/>
<link rel="canonical" href="https://treg.to/use-cases/{slug}"/>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:url" content="https://treg.to/use-cases/{slug}"/>
<meta property="og:type" content="website"/>
<meta name="twitter:card" content="summary_large_image"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=Inter:wght@400;450;500;600;650;700&family=DM+Mono:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/usecase.css"/>
</head>
<body data-page="{page_id}">

<div class="navwrap"><nav class="nav">
  <a class="brand" href="/"><span class="glyph">&#9626;</span> treg</a>
  <div class="links">
    <a class="hidem" href="/tutorial">docs</a>
    <a class="hidem" href="https://github.com/superdesigndev/treg" target="_blank" rel="noopener">repo</a>
    <a class="candy sm" href="/app?ref={page_id}" data-ev="lp_cta_primary" data-page="{page_id}">Start free</a>
  </div>
</nav></div>

{content}

<footer>
  <div class="foot-in">
    <a class="brand" href="/"><span class="glyph">&#9626;</span> treg</a>
    <span class="note">&mdash; 100% open source</span>
    <span class="sp"></span>
    <a href="/resources">resources</a><a href="/tutorial">docs</a><a href="/llms.txt">llms.txt</a>
    <a href="https://github.com/superdesigndev/treg" target="_blank" rel="noopener">github &#8599;</a>
    <a href="/terms">terms</a><a href="/privacy">privacy</a>
  </div>
</footer>

<script>
/* Landing-page instrumentation. The conversion that matters is the FIRST SUCCESSFUL CALL,
   not the signup — see marketing/landing/_measurement.md. `lp_copy_prompt` is the leading
   indicator we score pages on, so it fires from every copy affordance.
   INTEGRATION POINT: this forwards to window.posthog if the page ever loads it. Until then
   events are queued on window.tregEvents so nothing is silently lost. */
(function(){{
  window.tregEvents = window.tregEvents || [];
  function ev(name, props){{
    var p = Object.assign({{page: document.body.dataset.page}}, props || {{}});
    window.tregEvents.push([name, p, Date.now()]);
    if (window.posthog && window.posthog.capture) window.posthog.capture(name, p);
  }}
  ev('lp_view');

  /* copy-to-clipboard, same interaction as the landing page's give-box */
  document.addEventListener('click', function(e){{
    var b = e.target.closest('[data-copy]');
    if (b) {{
      var src = document.querySelector(b.dataset.copy);
      if (src) {{
        var t = src.textContent.trim();
        (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).catch(function(){{}});
        var old = b.textContent;
        b.textContent = b.classList.contains('copybtn') ? 'copied' : 'Copied \\u2713';
        b.classList.add('done');
        setTimeout(function(){{ b.textContent = old; b.classList.remove('done'); }}, 1800);
      }}
      /* which prompt was copied matters: a page can carry more than one workflow, and knowing
         which one people take is the whole reason to run two. */
      ev(b.dataset.ev || 'lp_copy_prompt', {{prompt: b.dataset.copy}});
      return;
    }}
    var a = e.target.closest('[data-ev]');
    if (a && a.tagName !== 'DETAILS') ev(a.dataset.ev);
  }});

  /* which objection people open is the best diagnostic on the page: it names what the hero failed to answer.
     The first one ships open, and Chrome fires `toggle` for that initial state — counting it would score a
     false open on every page view, so only user-driven changes count. */
  document.querySelectorAll('details[data-ev]').forEach(function(d){{
    var initial = d.open;
    d.addEventListener('toggle', function(){{
      if (d.open && !initial) ev('lp_objection_open', {{q: d.dataset.q}});
      initial = false;
    }});
  }});

  /* did the evidence actually get read? */
  var proof = document.getElementById('proof');
  if (proof && 'IntersectionObserver' in window) {{
    var io = new IntersectionObserver(function(es){{
      es.forEach(function(en){{ if (en.isIntersecting) {{ ev('lp_scroll_proof'); io.disconnect(); }} }});
    }}, {{threshold: 0.3}});
    io.observe(proof);
  }}
}})();
</script>
<script src="/adtrack.js"></script>
</body>
</html>
"""


def build_page(path):
    text = path.read_text()
    if CUT_AT in text:
        text = text.split(CUT_AT)[0]
    else:
        raise SystemExit(f"{path.name}: ad-kit marker not found — refusing to build "
                         f"rather than risk publishing internal ad strategy")
    fm, body = frontmatter(text)
    body = strip_editor_labels(body)
    PROVIDERS_SEEN.clear()          # the strip is per page, not cumulative
    page_id = fm["page_id"]
    slug, key = PAGES[page_id]

    parts = re.split(r"^## (.+)$", body, flags=re.M)
    chunks = [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]

    out = []
    for title, sec in chunks:
        t = title.lower()
        if t == "hero":
            out.append(render_hero(sec, page_id))
        elif t.startswith("the old way"):
            block = render_generic(title, sec, page_id, label="The economics")
            if fm.get("price_old") and fm.get("price_new"):
                wall = PRICEWALL.format(
                    price_old=html.escape(fm["price_old"]),
                    price_old_label=html.escape(fm.get("price_old_label", "")),
                    price_new=html.escape(fm["price_new"]),
                    price_new_label=html.escape(fm.get("price_new_label", "")))
                block = block.replace("</div></section>", wall + "</div></section>")
            out.append(block)
        elif t.startswith("a real workflow"):
            out.append(render_generic(title, sec, page_id, anchor="workflow", label="Try it"))
        elif t.startswith("proof"):
            out.append(render_generic(title, sec, page_id, anchor="proof", label="Evidence"))
        elif t.startswith("three things"):
            out.append(render_cards(title, sec, label="Outcomes"))
        elif t.startswith("who this is for"):
            out.append(render_who(title, sec, label="Fit"))
        elif t.startswith("before you sign up"):
            out.append(render_faq(title, sec, label="Questions"))
        elif t.startswith("final section"):
            out.append(render_final(sec, page_id))
        # any other h2 (e.g. "Numbers used on this page") is internal and dropped by design

    body_html = "\n".join(out)
    # The hero strip is DERIVED from the marks the page's own tables cite — it can never claim a
    # provider the page does not actually use.
    if PROVIDERS_SEEN:
        strip = "".join(
            f'<span class="ptile"><img src="/logos/{s}.svg" alt="" loading="lazy" '
            f'width="22" height="22"></span>'
            for s in sorted(PROVIDERS_SEEN))
        strip = ('<div class="provstrip"><span class="pl">compared on this page</span>'
                 f'<span class="ptiles">{strip}</span></div>')
    else:
        strip = ""
    body_html = body_html.replace('<div class="provstrip" id="provstrip"></div>', strip)

    page = TEMPLATE.format(
        title=html.escape(fm["seo_title"]),
        desc=html.escape(fm["meta_description"]),
        slug=slug, page_id=page_id, content=body_html,
    )
    assert_no_leaks(f"usecase-{key}.html", page)
    dest = WEB / f"usecase-{key}.html"
    dest.write_text(page)
    return dest, len(page)


HUB = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Resources — what your agent can do with treg.to</title>
<meta name="description" content="Worked workflows for treg.to: SEO and search results, lead enrichment, social research, competitor ads and company data. Each one is a prompt you can copy and run."/>
<link rel="canonical" href="https://treg.to/resources"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=Inter:wght@400;450;500;600;650;700&family=DM+Mono:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/usecase.css"/>
</head>
<body data-page="hub">

<div class="navwrap"><nav class="nav">
  <a class="brand" href="/"><span class="glyph">&#9626;</span> treg</a>
  <div class="links">
    <a class="hidem" href="/tutorial">docs</a>
    <a class="hidem" href="https://github.com/superdesigndev/treg" target="_blank" rel="noopener">repo</a>
    <a class="candy sm" href="/app?ref=hub">Start free</a>
  </div>
</nav></div>

<header class="hero"><div class="wrap">
  <div class="kicker">2,600+ tools &middot; 40+ providers &middot; one key</div>
  <h1>What your agent can do with treg.to</h1>
  <div class="lede"><p>Each of these is a job an agent can finish, a prompt you can copy, and what the
  run actually cost when we made it. Pick the one that matches your work.</p></div>
</div></header>

<section><div class="wrap">
  <div class="seclab">Workflows</div>
  <div class="cards">
{cards}
  </div>
</div></section>

<section><div class="wrap">
  <div class="seclab">Start anywhere</div>
  <h2>Every one of them runs on the same key</h2>
  <p>One token, one prepaid balance, no provider signup. New teams get $1.00 of free credit &mdash; enough
  to finish most of the workflows above and still have change.</p>
  <div class="ctas" style="justify-content:flex-start">
    <a class="candy" href="/app?ref=hub">Start free</a>
    <a class="ghostbtn" href="/llms.txt">Point an agent at it</a>
  </div>
</div></section>

<footer>
  <div class="foot-in">
    <a class="brand" href="/"><span class="glyph">&#9626;</span> treg</a>
    <span class="note">&mdash; 100% open source</span>
    <span class="sp"></span>
    <a href="/resources">resources</a><a href="/tutorial">docs</a><a href="/llms.txt">llms.txt</a>
    <a href="https://github.com/superdesigndev/treg" target="_blank" rel="noopener">github &#8599;</a>
    <a href="/terms">terms</a><a href="/privacy">privacy</a>
  </div>
</footer>

<script src="/adtrack.js"></script>
</body>
</html>
"""


def build_hub(rows):
    cards = "\n".join(
        f'    <a class="card" href="/use-cases/{slug}"><h4>{html.escape(h1)}</h4>'
        f'<p>{html.escape(blurb)}</p></a>'
        for slug, h1, blurb in rows)
    dest = WEB / "resources.html"
    page = HUB.replace("{cards}", cards)
    dest.write_text(page)
    return dest, len(page)


def main():
    if not DIST.exists():
        raise SystemExit("run `python3 build.py` first")
    total, rows = 0, []
    for p in sorted(DIST.glob("0*.md")):
        dest, n = build_page(p)
        total += 1
        print(f"  {dest.relative_to(WEB.parent.parent.parent)}  {n:,} bytes")
        fm, _ = frontmatter(p.read_text().split(CUT_AT)[0])
        rows.append((PAGES[fm["page_id"]][0], fm.get("hub_title") or fm["h1"], fm.get("hub_blurb", "")))
    d, n = build_hub(rows)
    print(f"  {d.relative_to(WEB.parent.parent.parent)}  {n:,} bytes  (hub)")
    print(f"built {total} pages + hub")


if __name__ == "__main__":
    main()
