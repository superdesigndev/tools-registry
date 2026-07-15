"""Filesystem-jail seatbelt profile builder (the write-confinement half of the local-run sandbox). Pure
tests; enforcement is verified live on macOS in the run path. See src/treg/fsjail.py."""

from __future__ import annotations

from treg import fsjail


def test_macos_profile_denies_writes_except_scratch_and_dev():
    p = fsjail.macos_profile("/tmp/treg-fsjail-abc")
    assert p.startswith("(version 1)")
    assert "(allow default)" in p              # reads/exec/net stay open
    assert "(deny file-write*)" in p
    assert '(allow file-write* (subpath "/tmp/treg-fsjail-abc"))' in p   # the private scratch
    assert '(allow file-write* (regex #"^/dev/"))' in p                  # devices


def test_macos_profile_quotes_paths_safely():
    p = fsjail.macos_profile('/tmp/a"b\\c')
    assert '/tmp/a\\"b\\\\c' in p   # both the quote and the backslash are escaped for the sb literal


def test_wrap_macos_prefixes_sandbox_exec():
    assert fsjail.wrap_macos(["/bin/id", "-u"], "/x/prof.sb") == \
        ["sandbox-exec", "-f", "/x/prof.sb", "/bin/id", "-u"]
