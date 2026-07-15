"""Local CLI runs (`treg run`) — the server-side grant logic.

A grant renders a tool's local-run profile into concrete process material (env vars / argv
fragments) for ONE run on a member's machine. It is the single, sanctioned, audited exception to
"secret values are never returned" — gated per tool by `cli.enabled` (owner opt-in). OAuth secrets
are refreshed first and release ONLY the expiring leaf (the access token); the blob
(refresh_token / client_secret) never leaves the server. Deny patterns are checked HERE, where the
secret lives, not on the client. See docs/CLI-RUN-PLAN.md.

Profile shape (tool.cli, mirrored by the catalog `cli` blocks):
  {enabled, bin, install?, inject: [{secret_id | from_binding?, via: "env"|"argv",
   name? (env var), argv? (template list), format?, secret_field?}],
   deny: [regex…], deny_defaults?, noninteractive?, warnings?, errors: [{pattern, verdict, message}]}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from . import crypto, oauth
from .injectors import _token_from_json
from .models import Secret, Tool

# OAuth grants may release ONLY the short-lived leaf (access token). Releasing refresh_token /
# client_secret would hand the whole re-mintable identity to the member — never allowed.
_OAUTH_RELEASABLE_FIELDS = ("access_token", "token")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")  # a bare command name: no path sep, no spaces/shell
VERDICTS = ("ok", "credential_invalid", "unknown_error")
# How a CLI authenticates — drives auto-import routing (docs/CLI-AUTOIMPORT-PLAN.md):
#   env / argv  → server-injectable (treg holds + injects the secret; either run tier works)
#   config_file → local-only (the credential lives in the CLI's own config on the member's machine)
#   device      → report-only (browser/device login, no token override — e.g. `az`)
AUTH_MECHANISMS = ("env", "argv", "config_file", "device")

# Env vars that alter how a process (or its children) loads code — injecting a secret VALUE into any of
# these is arbitrary code execution on the member's machine, so a cli.inject may never target them.
_DANGEROUS_ENV_EXACT = {
    "PATH", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONEXECUTABLE", "NODE_OPTIONS",
    "BASH_ENV", "ENV", "IFS", "SHELL", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF",
    "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB", "GEM_PATH", "CLASSPATH", "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS", "PS4", "PROMPT_COMMAND", "GLOBIGNORE", "CDPATH", "FPATH", "ZDOTDIR",
}
_DANGEROUS_ENV_PREFIX = ("LD_", "DYLD_", "BASH_FUNC_")
_MAX_INJECT = 32
_MAX_LIST = 64


def _is_dangerous_env(name: str) -> bool:
    n = (name or "").upper()
    return n in _DANGEROUS_ENV_EXACT or n.startswith(_DANGEROUS_ENV_PREFIX)


def effective_profile(tool: Tool, catalog_cli: dict | None) -> dict | None:
    """The creator's `tool.cli` merged over the catalog profile. The catalog NEVER enables a tool
    (enabled comes only from tool.cli — owner opt-in); deny lists are unioned unless the creator
    set `deny_defaults: false`. Returns None when neither side knows this CLI."""
    mine = tool.cli or {}
    if not mine and not catalog_cli:
        return None
    # Copy the catalog entry (and its inner list objects) — a returned profile must never alias the
    # module-level CATALOG, or a future in-place edit would corrupt it process-wide.
    eff = {k: (list(v) if isinstance(v, list) else v) for k, v in (catalog_cli or {}).items()}
    eff.update({k: v for k, v in mine.items() if k not in ("deny", "deny_defaults")})
    deny = list(mine.get("deny") or [])
    if mine.get("deny_defaults", True):
        deny += [p for p in (catalog_cli or {}).get("deny") or [] if p not in deny]
    eff["deny"] = deny
    eff["_own_deny"] = list(mine.get("deny") or [])  # so a refusal can name its source
    eff["enabled"] = bool(mine.get("enabled", False))
    return eff


def check_deny(profile: dict, argv: list[str]) -> tuple[str, str] | None:
    """First deny pattern matching the joined argv → (pattern, source). Malformed stored patterns
    never block a grant (they are rejected at write time by validate_cli_profile; a legacy bad one
    is skipped rather than 500ing every run)."""
    joined = " ".join(argv)
    own = set(profile.get("_own_deny") or [])
    for pat in profile.get("deny") or []:
        try:
            # Match the joined argv AND each argument on its own, so an anchored pattern (e.g. ^--live$)
            # a creator writes still catches the flag — it would otherwise fail open on the joined string.
            if re.search(pat, joined) or any(re.search(pat, a) for a in argv):
                return pat, ("this skill's treg.json" if pat in own else "the treg catalog defaults")
        except re.error:
            continue
    return None


def validate_cli_profile(cli: dict) -> None:
    """Reject a malformed profile at write time with a clear message (never at grant time)."""
    if not isinstance(cli, dict):
        raise ValueError("cli must be an object")
    for key in ("enabled", "deny_defaults", "beta"):  # enabled gates local runs; beta = unverified catalog entry
        if key in cli and not isinstance(cli[key], bool):
            raise ValueError(f"cli.{key} must be a boolean")
    if "auth_mechanism" in cli and cli["auth_mechanism"] not in AUTH_MECHANISMS:
        raise ValueError(f"cli.auth_mechanism must be one of {AUTH_MECHANISMS}")
    det = cli.get("detect")  # optional login-state hint: {config_paths: [str]} (presence ⇒ logged in)
    if det is not None:
        if not isinstance(det, dict):
            raise ValueError("cli.detect must be an object")
        paths = det.get("config_paths")
        if paths is not None and not (isinstance(paths, list) and all(isinstance(p, str) and p for p in paths)):
            raise ValueError("cli.detect.config_paths must be a list of non-empty strings")
    for key in ("bin", "install", "package", "runtime"):  # package/runtime are advisory install metadata
        if key in cli and not (isinstance(cli[key], str) and cli[key]):
            raise ValueError(f"cli.{key} must be a non-empty string")
    if cli.get("bin") and not _BIN_RE.match(cli["bin"]):  # a bare command name — no path/shell tokens
        raise ValueError(f"cli.bin {cli['bin']!r} must be a plain command name (no path separators or spaces)")
    for key in ("deny", "noninteractive", "warnings"):
        vals = cli.get(key)
        if vals is None:
            continue
        if not (isinstance(vals, list) and all(isinstance(v, str) for v in vals)):
            raise ValueError(f"cli.{key} must be a list of strings")
        if len(vals) > _MAX_LIST:
            raise ValueError(f"cli.{key} has too many entries (max {_MAX_LIST})")
    for pat in cli.get("deny") or []:
        try:
            re.compile(pat)
        except re.error as exc:
            raise ValueError(f"cli.deny pattern {pat!r} is not a valid regex: {exc}")
    for e in cli.get("errors") or []:
        if not isinstance(e, dict) or not isinstance(e.get("pattern"), str):
            raise ValueError("cli.errors entries must be objects with a 'pattern'")
        try:
            re.compile(e["pattern"])
        except re.error as exc:
            raise ValueError(f"cli.errors pattern {e['pattern']!r} is not a valid regex: {exc}")
        if e.get("verdict") is not None and e["verdict"] not in VERDICTS:
            raise ValueError(f"cli.errors verdict must be one of {VERDICTS}")
    inject = cli.get("inject")
    if inject is None:
        return
    if not isinstance(inject, list):
        raise ValueError("cli.inject must be a list")
    if len(inject) > _MAX_INJECT:
        raise ValueError(f"cli.inject has too many entries (max {_MAX_INJECT})")
    for e in inject:
        if not isinstance(e, dict):
            raise ValueError("cli.inject entries must be objects")
        via = e.get("via", "env")
        if via not in ("env", "argv"):
            raise ValueError("cli.inject via must be 'env' or 'argv'")
        if via == "env":
            if not (isinstance(e.get("name"), str) and _ENV_NAME_RE.match(e["name"] or "")):
                raise ValueError(f"cli.inject env entry needs a valid env var 'name' (got {e.get('name')!r})")
            if _is_dangerous_env(e["name"]):  # LD_PRELOAD / PATH / NODE_OPTIONS … = code-execution vectors
                raise ValueError(f"cli.inject env name {e['name']!r} is not allowed — it can hijack code execution")
        else:
            argv_t = e.get("argv")
            if not (isinstance(argv_t, list) and argv_t and all(isinstance(a, str) for a in argv_t)):
                raise ValueError("cli.inject argv entry needs a non-empty 'argv' template list")
        fmt = e.get("format", "{secret}")
        if not isinstance(fmt, str) or "{secret}" not in fmt:
            raise ValueError(f"cli.inject format must be a string containing {{secret}} (got {fmt!r})")
        for key in ("from_binding", "secret_field", "secret"):
            if key in e and not (isinstance(e[key], str) and e[key]):
                raise ValueError(f"cli.inject {key} must be a non-empty string")
        if "secret_id" in e and not isinstance(e["secret_id"], int):
            raise ValueError("cli.inject secret_id must be an integer")


def _resolve_secret_id(entry: dict, tool: Tool) -> int | None:
    """Which secret an inject entry means: explicit `secret_id` → the HTTP binding named by
    `from_binding` → default: the tool's SOLE bound secret. Returns None when it can't decide —
    including the AMBIGUOUS case of a multi-credential tool with no explicit mapping (so grant fails
    loudly instead of silently injecting the wrong credential)."""
    if entry.get("secret_id") is not None:
        return entry["secret_id"]
    if entry.get("from_binding"):
        want = entry["from_binding"].lower()
        for b in tool.bindings:
            if (b.get("name") or "").lower() == want:
                return b.get("secret_id")
        return None
    distinct = {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _leaf_ttl(blob: dict) -> int | None:
    """Seconds until the oauth leaf expires (None if the blob doesn't say)."""
    raw = blob.get("expires_at")  # explicit None check: an epoch of 0 (already expired) is falsy
    if raw is None:
        raw = blob.get("expiry")
    try:
        if isinstance(raw, (int, float)):
            return max(0, int(raw - datetime.now(timezone.utc).timestamp()))
        if isinstance(raw, str):
            exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, OverflowError):
        return None
    return None


async def render_grant(tool: Tool, profile: dict, db: AsyncSession, http) -> dict:
    """Render the profile's inject entries into a delivery-agnostic list:
    `{"items": [{via, ...}], "ttl_seconds": int|None}`. Each item is one credential to deliver, tagged
    by method — `{via:"env", name, value}` or `{via:"argv", argv:[…]}` (a `broker` method is added later).
    The CLIENT decides HOW to hand each item to the CLI (an env var, a flag, or a private socket); the
    server only resolves + renders the value. Raises LookupError (a referenced secret is missing) or lets
    an oauth refresh error propagate — the endpoint maps both to the same statuses the /call path uses."""
    inject = profile.get("inject") or []
    if not inject:
        # A self-authenticating CLI (auto-import "local tier": gh, gcloud, vercel — the credential lives
        # in the CLI's own config) injects nothing. The grant is still valid: it returns the bin + audit
        # so `treg run` just execs the CLI. (Empty is DISTINCT from a broken profile, which is rejected
        # at write time by validate_cli_profile.)
        return {"items": [], "ttl_seconds": None}
    # A grant may only release a secret that BELONGS to this tool — one of its HTTP bindings, a secret in
    # the tool's own bundle (how skill/CLI tools attach theirs), or a secret OWNED by the tool's owner
    # (e.g. a param the owner attached). Without this, a member could point an inject at ANOTHER user's
    # secret id and extract its value (values are otherwise never returned).
    allowed_sids = {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}
    if tool.bundle_id is not None:
        rows = (await db.execute(select(Secret.id).where(Secret.bundle_id == tool.bundle_id))).all()
        allowed_sids |= {r[0] for r in rows}
    items: list[dict] = []
    ttl: int | None = None
    for entry in inject:
        sid = _resolve_secret_id(entry, tool)
        secret = await db.get(Secret, sid) if sid is not None else None
        if (secret is None or secret.org_id != tool.org_id
                or (sid not in allowed_sids and secret.owner != tool.owner)):
            target = entry.get("name") or entry.get("argv")
            raise LookupError(
                f"inject entry {target} has no resolvable secret — add an explicit \"secret_id\" or "
                '"from_binding" (the tool has multiple credentials, so treg won\'t guess which one)'
                if len({b.get("secret_id") for b in tool.bindings}) > 1
                else f"inject entry {target} has no resolvable secret")
        if secret.kind == "oauth":
            await oauth.ensure_fresh(secret, db, http)  # refresh in place; raises on a dead root
            raw = crypto.decrypt(secret.value)
            blob = json.loads(raw)
            field = entry.get("secret_field", "access_token")
            if field not in _OAUTH_RELEASABLE_FIELDS:  # never hand out refresh_token / client_secret
                raise LookupError(
                    f"oauth inject may only release the access token, not {field!r} "
                    f"(allowed: {', '.join(_OAUTH_RELEASABLE_FIELDS)})")
            try:
                value = _token_from_json(raw, field)
            except ValueError:
                # Google-style blobs carry `token` before treg's first refresh and `access_token`
                # after (refresh() writes both) — fall back to the sibling key so a grant right
                # after upload doesn't fail on a field-name technicality.
                sibling = "token" if field == "access_token" else "access_token"
                value = _token_from_json(raw, sibling)
            t = _leaf_ttl(blob)
            ttl = t if ttl is None else min(ttl, t if t is not None else ttl)
        else:  # env | param | cli_auth | secret_file — the stored string
            value = crypto.decrypt(secret.value)
        # A stray trailing newline (common in file-sourced secrets) becomes an illegal env/header value
        # downstream — strip it here exactly as build_payload does on the register path.
        value = value.strip()
        rendered = entry.get("format", "{secret}").replace("{secret}", value)
        via = entry.get("via", "env")
        if via == "env":
            items.append({"via": "env", "name": entry["name"], "value": rendered})
        else:  # argv
            items.append({"via": "argv", "argv": [a.replace("{secret}", value) for a in entry["argv"]]})
    return {"items": items, "ttl_seconds": ttl}
