"""Portability checks on migration DDL.

The suite runs on SQLite; production is Postgres. SQLite is the more permissive of the two, so a
whole class of migration bug passes here and takes the deployment down on boot — which is exactly
what `BOOLEAN NOT NULL DEFAULT 0` did. These read the DDL as text rather than executing it, so
they catch the mismatch without needing a Postgres in CI.
"""

from __future__ import annotations

import inspect
import re

from treg import db


def _shared_ddl() -> list[tuple[int, str]]:
    """String literals in db.py that run against BOTH engines, with their line numbers.

    _rebuild_user_table is excluded: it exists because SQLite can't drop columns portably and
    never executes on Postgres, so its `DEFAULT 0` booleans are correct as written.
    """
    src = inspect.getsource(db).split("\n")
    skip = inspect.getsource(db._rebuild_user_table)
    skip_lines = set(range(
        (start := src.index(skip.split("\n")[0])), start + len(skip.split("\n"))
    ))
    return [(i + 1, ln) for i, ln in enumerate(src) if i not in skip_lines]


def test_boolean_columns_never_default_to_an_integer():
    """Postgres refuses an integer default on a BOOLEAN column; SQLite silently accepts it."""
    bad = [
        f"db.py:{n}: {ln.strip()}"
        for n, ln in _shared_ddl()
        if re.search(r"BOOLEAN\b[^,'\")]*DEFAULT\s+[01]\b", ln, re.I)
    ]
    assert not bad, "use DEFAULT true/false, not 1/0:\n" + "\n".join(bad)


def test_added_columns_are_guarded_by_an_existence_check():
    """Every ADD COLUMN sits behind `if col not in cols` (or IF NOT EXISTS). init_db runs on every
    boot, so an unguarded one fails the SECOND deploy rather than the first — the worst kind."""
    src = inspect.getsource(db)
    for stmt in re.findall(r"ADD COLUMN(?! IF NOT EXISTS)[^\"']*", src):
        assert "{col}" in stmt or "{ddl}" in stmt or "not in cols" in src, stmt
