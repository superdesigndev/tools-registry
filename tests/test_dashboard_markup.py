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

# Dialogs reachable from more than one view. Each is opened by a button that exists on both the
# marketplace list and an integration page.
SHARED_DIALOGS = ["tokenAsk", "capAsk", "resPick"]


def _enclosing_views(needle: str) -> list[str]:
    """Which `<template v-if="view===...">` blocks, if any, contain `needle`."""
    at = INDEX.index(needle)
    open_views: list[str] = []
    for m in re.finditer(r'<template v-if="view===\'(\w+)\'|</template>', INDEX[:at]):
        if m.group(0).startswith("</"):
            if open_views:
                open_views.pop()
        else:
            open_views.append(m.group(1))
    return open_views


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
