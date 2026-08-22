#!/usr/bin/env python3
"""Bundle the five built pages into ONE review artifact.

    python3 build.py && python3 build_review.py [outfile]

Same content, same skin as the live pages — it imports build_html's renderers rather than
re-implementing them, so what you review is what ships. Adds a review bar the live pages do
not have: page switcher, per-page status, and the blockers.

Fonts differ from production by necessity: the artifact host blocks font CDNs, so the page
falls back through treg's own stack (Georgia / system-ui / ui-monospace) instead of
Geist Pixel / Inter / DM Mono. Everything else is identical.
"""
import re, sys, pathlib, html
import build_html as B
from build_preview import inline_logos

HERE = pathlib.Path(__file__).parent
CSS = (HERE.parent.parent / "src" / "treg" / "web" / "usecase.css").read_text()

TAB = [("p1", "SEO"), ("p2", "Enrichment"), ("p3", "Social"), ("p4", "Ads"), ("p5", "Company")]


def page_parts(path):
    text = path.read_text()
    text = text.split(B.CUT_AT)[0]
    fm, body = B.frontmatter(text)
    pid = fm["page_id"]
    slug, _ = B.PAGES[pid]
    parts = re.split(r"^## (.+)$", body, flags=re.M)
    chunks = [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    out = []
    for title, sec in chunks:
        t = title.lower()
        if t == "hero":
            out.append(B.render_hero(sec, pid))
        elif t.startswith("the old way"):
            out.append(B.render_generic(title, sec, pid, label="The economics"))
        elif t.startswith("a real workflow"):
            out.append(B.render_generic(title, sec, pid, anchor=None, label="Try it"))
        elif t.startswith("proof"):
            out.append(B.render_generic(title, sec, pid, anchor=None, label="Evidence"))
        elif t.startswith("three things"):
            out.append(B.render_cards(title, sec, label="Outcomes"))
        elif t.startswith("who this is for"):
            out.append(B.render_who(title, sec, label="Fit"))
        elif t.startswith("before you sign up"):
            out.append(B.render_faq(title, sec, label="Questions"))
        elif t.startswith("final section"):
            out.append(B.render_final(sec, pid))
    return pid, slug, fm, "\n".join(out)


REVIEW_CSS = """
/* ---- review chrome: deliberately outside the brand so it never reads as page content ---- */
body { padding-top: 0; }
.rbar { position: sticky; top: 0; z-index: 200; background: var(--inverse); color: var(--inverse-ink);
  border-bottom: 1px solid #ffffff1f; }
.rbar .in { max-width: 1160px; margin: 0 auto; padding: 10px 24px; display: flex; align-items: center;
  gap: 18px; flex-wrap: wrap; }
.rbar .who { font-family: var(--mono); font-size: 11.5px; letter-spacing: .12em; text-transform: uppercase;
  color: #8a8a86; }
.rtabs { display: flex; gap: 6px; margin-left: auto; flex-wrap: wrap; }
.rtab { font-family: var(--sans); font-size: 13px; font-weight: 550; color: #b9b9b5; background: transparent;
  border: 1px solid #ffffff26; border-radius: 999px; padding: 6px 15px; cursor: pointer;
  transition: color .15s, background-color .15s, border-color .15s; }
.rtab:hover { color: #fff; border-color: #ffffff55; }
.rtab[aria-selected="true"] { background: var(--surface); color: var(--ink); border-color: var(--surface); }
.rtab .n { font-family: var(--mono); font-size: 10.5px; color: currentColor; opacity: .6; margin-right: 6px; }

.rmeta { background: var(--panel2); border-bottom: 1px solid var(--line); }
.rmeta .in { max-width: 860px; margin: 0 auto; padding: 16px 28px; display: flex; flex-direction: column; gap: 8px; }
.rmeta .slug { font-family: var(--mono); font-size: 12.5px; color: var(--ink); }
.rmeta .st { font-size: 13px; color: var(--muted); line-height: 1.55; }
.rmeta .flag { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; color: var(--ink);
  background: #ba660314; border: 1px solid #ba660340; border-radius: 8px; padding: 8px 13px; align-self: flex-start; }
.rmeta .flag b { font-weight: 650; }
.rmeta .ok { background: #11845310; border-color: #11845338; }

.panel { display: none; }
.panel.on { display: block; }
/* the nav pill is part of the real page; in review it would collide with the sticky bar */
.panel .navwrap { display: none; }
.panel .hero { padding-top: 44px; }
"""


def build(dest):
    panels, tabs, metas = [], [], []
    for p in sorted(B.DIST.glob("0*.md")):
        pid, slug, fm, content = page_parts(p)
        label = dict(TAB)[pid]
        status = fm.get("status", "")
        blocked = "blocked" in status or "hold" in status or "unproven" in status
        flag = ""
        if pid == "p5":
            flag = ('<div class="flag"><b>Blocked:</b> must not claim hiring or activity signals — '
                    'the signals endpoint id returned by catalog search does not resolve. Funding is proven.</div>')
        elif pid == "p4":
            flag = ('<div class="flag"><b>Fund last:</b> telemetry confirms this is the narrowest job of '
                    'the five — the Meta ad library is 1.2% of all traffic.</div>')
        elif pid == "p1":
            flag = ('<div class="flag"><b>Re-pointed:</b> keyword research is ~1.3% of real usage, SERP '
                    'scraping ~22%. A second workflow was added; the data decides which becomes the hero.</div>')
        else:
            flag = '<div class="flag ok"><b>Ready to build.</b> Proof populated from a real run.</div>'

        metas.append(f'''<div class="rmeta" data-for="{pid}"><div class="in">
          <div class="slug">treg.to/use-cases/{html.escape(slug)}</div>
          <div class="st">{html.escape(status)}</div>
          {flag}
        </div></div>''')
        # CSP blocks external images on the artifact host — embed the marks or the tiles come up blank
        content = inline_logos(content)
        panels.append(f'<div class="panel" data-panel="{pid}">{metas[-1]}{content}</div>')
        tabs.append(f'<button class="rtab" role="tab" data-tab="{pid}" aria-selected="false">'
                    f'<span class="n">{pid.upper()}</span>{label}</button>')

    # The artifact host supplies its own charset; this repeats it so the file also renders correctly
    # when opened straight off disk or a bare static server during review.
    doc = f"""<meta charset="utf-8">
<title>treg.to Use-Case Pages</title>
<style>{CSS}{REVIEW_CSS}</style>

<div class="rbar"><div class="in">
  <span class="who">treg.to · use-case pages · review build 17 Aug 2026</span>
  <div class="rtabs" role="tablist">{''.join(tabs)}</div>
</div></div>

{''.join(panels)}

<script>
(function () {{
  var tabs = [].slice.call(document.querySelectorAll('.rtab'));
  var panels = [].slice.call(document.querySelectorAll('.panel'));
  function show(id) {{
    tabs.forEach(function (t) {{ t.setAttribute('aria-selected', String(t.dataset.tab === id)); }});
    panels.forEach(function (p) {{ p.classList.toggle('on', p.dataset.panel === id); }});
    window.scrollTo({{ top: 0, behavior: 'instant' }});
  }}
  tabs.forEach(function (t) {{ t.addEventListener('click', function () {{ show(t.dataset.tab); }}); }});
  show('p1');

  /* the copy buttons are real, so the prompts can be lifted straight out of the review */
  document.addEventListener('click', function (e) {{
    var b = e.target.closest('[data-copy]');
    if (!b) return;
    var src = document.querySelector('.panel.on ' + b.dataset.copy) || document.querySelector(b.dataset.copy);
    if (!src) return;
    var t = src.textContent.trim();
    if (navigator.clipboard) navigator.clipboard.writeText(t).catch(function () {{}});
    var old = b.textContent;
    b.textContent = b.classList.contains('copybtn') ? 'copied' : 'Copied \\u2713';
    b.classList.add('done');
    setTimeout(function () {{ b.textContent = old; b.classList.remove('done'); }}, 1600);
  }});
}})();
</script>
"""
    dest.write_text(doc)
    return dest, len(doc)


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "review.html"
    d, n = build(out)
    print(f"wrote {d} ({n:,} bytes)")
