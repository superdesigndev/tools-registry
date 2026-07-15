"""Filesystem jail for isolated local runs (`treg run --fs-jail`) — the last channel of the sandbox.

Even with uid isolation (the member can't read treg-run's memory) and egress control (treg-run can't send
the key over the network), a CLI feature that runs member code could still WRITE the injected key to a file
the member then reads. This confines the run's writes to a private per-run scratch dir (0700, owned by
treg-run, so the member can't traverse into it); everything else on disk is read-only to the process.

It is OPT-IN, because it also stops a CLI from writing output files where the member wants them (e.g.
`aws s3 cp … ./out`). macOS enforces it with a seatbelt (`sandbox-exec`) profile — tested on macOS 15:
writes outside the scratch are denied, inside are allowed. Linux would use Landlock / a mount namespace
(the builder is here; wiring is a follow-up).
"""

from __future__ import annotations


def _q(path: str) -> str:
    """Quote a path as a seatbelt string literal (escape backslash + double-quote)."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(scratch: str) -> str:
    """A seatbelt profile: reads / exec / network stay UNrestricted (we're not sandboxing those — uid
    isolation + egress already do), but WRITES are denied everywhere except the private scratch dir and
    device files. So the CLI can run and use its own HOME (pointed at the scratch), but can't drop the
    key in any member-readable location."""
    return (
        "(version 1)\n"
        "(allow default)\n"                                # only restrict writes; reads/exec/net allowed
        "(deny file-write*)\n"
        f"(allow file-write* (subpath {_q(scratch)}))\n"   # the private, member-unreadable scratch
        '(allow file-write* (regex #"^/dev/"))\n'          # /dev/null, /dev/tty, /dev/stdout, …
    )


def wrap_macos(cmd: list[str], profile_path: str) -> list[str]:
    """Wrap a command to exec under a seatbelt profile file (`sandbox-exec -f <profile> <cmd…>`)."""
    return ["sandbox-exec", "-f", profile_path, *cmd]
