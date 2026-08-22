#!/usr/bin/env python3
"""Expand the shared blocks into standalone, hand-off-ready pages.

    python3 build.py            # writes ./dist/*.md
    python3 build.py --check    # verify only: non-zero exit if any ref is unresolved

Source files stay the single point of edit. `dist/` is generated — never edit it, it is
overwritten on every run.
"""
import re, sys, pathlib

HERE = pathlib.Path(__file__).parent
DIST = HERE / "dist"

def load_blocks():
    """Parse _shared.md into {id: body}. A block starts at '## `S-...`' and runs to the next '## '."""
    text = (HERE / "_shared.md").read_text()
    blocks, current, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^## `(S-[A-Z0-9-]+)`(.*)$", line)
        if m:
            if current:
                blocks[current] = "\n".join(buf).strip()
            # trailing text on the heading (e.g. "— How are credentials handled?") is a label for
            # editors, not body copy — the page already writes the question. Drop it.
            current, buf = m.group(1), []
        elif line.startswith("## "):
            if current:
                blocks[current] = "\n".join(buf).strip()
            current, buf = None, []
        elif current is not None:
            buf.append(line)
    if current:
        blocks[current] = "\n".join(buf).strip()
    return blocks

def expand(text, blocks):
    """Replace `S-XXX` refs with their block body. Returns (text, unresolved_ids)."""
    unresolved = []
    def sub(m):
        key = m.group(1)
        if key in blocks:
            return blocks[key]
        unresolved.append(key)
        return m.group(0)
    # a page writes "**Question?** — `S-OBJ-X`"; the em-dash is editor shorthand for "answer lives
    # in _shared". In the build the answer must start on its own line, matching the page-specific
    # answers around it.
    text = re.sub(r"\*\*[ \t]*[—-][ \t]*(?=`S-[A-Z0-9-]+`)", "**\n", text)
    # only the backticked form is a reference; bare S-FOO inside prose is left alone
    text = re.sub(r"`(S-[A-Z0-9-]+)`", sub, text)
    # `F-NN` fact keys are provenance for editors, not reader-facing copy — strip from the build,
    # including any whitespace left behind at end of line
    text = re.sub(r"[ \t]*`F-\d+`", "", text)
    return text, unresolved

def main():
    check_only = "--check" in sys.argv
    blocks = load_blocks()
    pages = sorted(HERE.glob("0*.md"))
    if not check_only:
        DIST.mkdir(exist_ok=True)
    failed = False
    for p in pages:
        out, unresolved = expand(p.read_text(), blocks)
        if unresolved:
            failed = True
            print(f"  {p.name}: UNRESOLVED {sorted(set(unresolved))}")
        remaining = re.findall(r"\[ TO BE POPULATED[^\]]*\]", out)
        if remaining:
            failed = True
            print(f"  {p.name}: {len(remaining)} unpopulated proof field(s)")
        if not check_only:
            (DIST / p.name).write_text(out)
    if check_only:
        print("FAIL — see above" if failed else f"OK — {len(pages)} pages, {len(blocks)} shared blocks, no unresolved refs")
        sys.exit(1 if failed else 0)
    print(f"wrote {len(pages)} pages to {DIST}/ ({len(blocks)} shared blocks expanded)")
    if failed:
        print("WARNING: issues above — do not publish")
        sys.exit(1)

if __name__ == "__main__":
    main()
