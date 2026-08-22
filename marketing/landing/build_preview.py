#!/usr/bin/env python3
"""Turn one built page into a standalone, artifact-ready preview.

    python3 build_preview.py seo [outfile]

Takes src/treg/web/usecase-<key>.html and makes it self-contained:
  - inlines usecase.css (the artifact host has no /usecase.css to fetch)
  - drops the Google Fonts <link> (the host's CSP blocks font CDNs, so it would fail silently —
    the page falls back through treg's own stack: Georgia / system-ui / ui-monospace)
  - rewrites root-relative hrefs to https://treg.to/... so every link in the preview goes somewhere real
  - strips the doctype/html/head/body wrapper, which the artifact host supplies itself

What survives is the real page: same markup, same tokens, same copy, working copy-to-clipboard.
"""
import re, sys, pathlib, base64

HERE = pathlib.Path(__file__).parent
WEB = HERE.parent.parent / "src" / "treg" / "web"


def inline_logos(markup):
    """Embed the provider marks as data: URIs.

    The artifact host's CSP blocks every external request, images included — so a src pointing at
    treg.to renders an empty tile, which is worse than no tile. These are small SVGs, so inlining
    them costs a few KB and makes the preview self-contained.
    """
    def sub(m):
        f = WEB / "logos" / (m.group(1) + ".svg")
        if not f.exists():
            return m.group(0)
        b64 = base64.b64encode(f.read_bytes()).decode()
        return f'src="data:image/svg+xml;base64,{b64}"'
    return re.sub(r'src="(?:https://treg\.to)?/logos/([a-z0-9-]+)\.svg"', sub, markup)


def build(key, dest):
    src = (WEB / f"usecase-{key}.html").read_text()
    css = (WEB / "usecase.css").read_text()

    title = re.search(r"<title>(.*?)</title>", src, re.S).group(1)
    body = re.search(r"<body[^>]*>(.*)</body>", src, re.S).group(1)

    # every root-relative link should resolve to the real site from inside the preview
    body = re.sub(r'(href|src)="/(?!/)', r'\1="https://treg.to/', body)
    body = inline_logos(body)          # …except images, which the CSP would block

    # a banner so nobody mistakes a preview for the live page
    banner = (
        '<div style="background:#1a1a1a;color:#f8f8f7;font:12px/1.5 ui-monospace,Menlo,monospace;'
        'letter-spacing:.1em;text-transform:uppercase;text-align:center;padding:9px 20px">'
        'Preview &middot; treg.to/use-cases/seo-data-for-ai-agents/ &middot; not yet deployed</div>'
    )
    # the fixed nav would sit under the banner; make it flow instead
    fix = ("\n.navwrap{position:static;padding-top:14px}\n"
           ".hero{padding-top:56px}\nbody{background:var(--bg)}\n")

    doc = f'<meta charset="utf-8">\n<title>{title}</title>\n<style>{css}{fix}</style>\n{banner}\n{body}'
    dest.write_text(doc)
    return dest, len(doc)


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "seo"
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / f"preview-{key}.html"
    d, n = build(key, out)
    print(f"wrote {d} ({n:,} bytes)")
