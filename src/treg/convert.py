"""Skill -> bundle scaffolding. Deterministic discovery only.

Walking a skill directory tells us its recipe (SKILL.md) and its credentials (.secret/* files).
It CANNOT know the upstream base_url or the exact binding placement — that lives in the skill's
script and needs reading/inference. So scaffold emits a manifest with secrets + a tool stub, and
leaves `base_url` and binding details for the agent (or a human) to complete before `skill push`.
This split keeps the deterministic part dumb and the inference part with whoever understands the API.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

FILL = "FILL_ME"
_SECRET_DIRS = (".secret", ".secrets")
_RECIPE_FILES = ("SKILL.md", "skill.md", "README.md")


def resolve_secret_path(base: Path, rel: str) -> Path:
    """Resolve a contract `file:` path tolerantly. The `.secret`/`.secrets` spelling drifts between
    machines (both are gitignored, so each person creates their own) — so a treg.json written against
    `.secret/token.json` must still find `.secrets/token.json` and vice versa. The exact path wins;
    otherwise swap the leading secret-dir segment. Returns the exact (maybe-missing) path if nothing
    matches, so callers' error messages stay meaningful."""
    exact = base / rel
    if exact.exists():
        return exact
    parts = Path(rel).parts
    if parts and parts[0] in _SECRET_DIRS:
        for sd in _SECRET_DIRS:
            alt = base.joinpath(sd, *parts[1:])
            if alt.exists():
                return alt
    return exact
CONTRACT_FILE = "treg.json"  # the sidecar contract a skill can carry so treg auto-registers it

# Hosts that are documentation, not the upstream API — never a base_url.
_DOC_HOST_HINTS = ("developer", "docs.", "api-docs.", "reference", "github.com", "readme.io")
_URL_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=\-]*)?")


def _guess_kind(filename: str, text: str) -> str:
    """env (plain string) | secret_file (JSON token file) | oauth (JSON with refresh_token)."""
    if filename.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "secret_file"
        if isinstance(data, dict) and "refresh_token" in data:
            return "oauth"
        return "secret_file"
    return "env"


def _matches_kind(f: Path, kind: str) -> bool:
    """Does a `.secret/` file look like the requested kind?
    oauth/secret_file → a JSON token blob (has token/access_token/refresh_token);
    env/cli_auth → a plain-text (non-JSON) file. This excludes client_secret.json from oauth."""
    text = f.read_text(errors="replace")
    if f.name.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        is_token = isinstance(data, dict) and any(k in data for k in ("access_token", "token", "refresh_token"))
        return is_token if kind in ("oauth", "secret_file") else False
    return kind in ("env", "cli_auth")


def find_secret_file(skill_dir: str | Path, kind: str) -> Path:
    """Find THE secret file of a given kind in a skill dir's .secret/.secrets. Exactly one match
    or raise: none → FileNotFoundError, several → ValueError (pass --file to disambiguate)."""
    p = Path(skill_dir)
    candidates = [
        f for sd in _SECRET_DIRS if (p / sd).is_dir() for f in sorted((p / sd).iterdir()) if f.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(f"no .secret/.secrets files under {p}")
    matches = [f for f in candidates if _matches_kind(f, kind)]
    if not matches:
        raise FileNotFoundError(f"no {kind!r} secret in {p} (found: {[f.name for f in candidates]})")
    if len(matches) > 1:
        raise ValueError(f"ambiguous {kind!r} secret in {p}: {[f.name for f in matches]} — use --file")
    return matches[0]


def scaffold_skill(skill_dir: str | Path) -> dict:
    """Discover a skill directory into a /skills manifest stub (bindings need completing)."""
    p = Path(skill_dir)
    if not p.is_dir():
        raise NotADirectoryError(f"{p} is not a directory")

    recipe = ""
    for cand in _RECIPE_FILES:
        f = p / cand
        if f.exists():
            recipe = f.read_text(errors="replace")
            break

    secrets: list[dict] = []
    for sd in _SECRET_DIRS:
        d = p / sd
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file():
                    text = f.read_text(errors="replace")
                    secrets.append({"local_name": f.name, "value": text, "kind": _guess_kind(f.name, text)})

    # One tool stub bound to every discovered secret. The agent fixes base_url + per-binding
    # placement (e.g. a developer-token header vs an Authorization bearer).
    bindings = [
        {
            "secret": s["local_name"],
            "injector": s["kind"],
            "location": "header",
            "name": "Authorization" if i == 0 else FILL,
            "format": "Bearer {secret}" if i == 0 else "{secret}",
            "secret_field": "access_token",
        }
        for i, s in enumerate(secrets)
    ]
    tools = [{"name": p.name, "base_url": f"{FILL}://upstream", "bindings": bindings}]
    return {"name": p.name, "recipe": recipe, "secrets": secrets, "tools": tools}


# ---- treg.json contract: heuristic generation + loading -----------------------------------
def _read_recipe(p: Path) -> str:
    for cand in _RECIPE_FILES:
        f = p / cand
        if f.exists():
            return f.read_text(errors="replace")
    return ""


def _guess_base_url(p: Path) -> tuple[str | None, list[str]]:
    """Scan SKILL.md + top-level *.py for an upstream API host. Returns (base_url|None, notes).
    Heuristic + honest: we return scheme://host of the best candidate and always ask the user to
    verify (a real base often carries a version path like /v1 we can't infer reliably)."""
    text = _read_recipe(p)
    for py in sorted(p.glob("*.py")):
        text += "\n" + py.read_text(errors="replace")
    hosts: list[str] = []
    for m in _URL_RE.finditer(text):
        u = urlsplit(m.group(0))
        host = u.netloc.lower()
        if not host or any(h in host for h in _DOC_HOST_HINTS):
            continue
        base = f"{u.scheme}://{host}"
        if base not in hosts:
            hosts.append(base)
    if not hosts:
        return None, ["base_url — NOT FOUND in SKILL.md/*.py; set it manually"]
    # prefer an api.* host, else the first seen
    hosts.sort(key=lambda h: (0 if "//api." in h else 1))
    note = "base_url — heuristic guess, verify (may need a version path e.g. /v1)"
    if len(hosts) > 1:
        note += f"; other candidates: {', '.join(hosts[1:])}"
    return hosts[0], [note]


def _oauth_secret_field(f: Path) -> str:
    """Google-style token.json stores the access token under `token`; most others `access_token`."""
    try:
        data = json.loads(f.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return "access_token"
    if isinstance(data, dict) and "access_token" not in data and "token" in data:
        return "token"
    return "access_token"


def _default_binding(name: str, kind: str, f: Path) -> dict:
    b = {"secret": name, "injector": kind, "location": "header", "name": "Authorization",
         "format": "Bearer {secret}", "secret_field": "access_token"}
    if kind == "oauth":
        b["secret_field"] = _oauth_secret_field(f)
    return b


_APP_CONFIG_RE = re.compile(r"client[_-]?secret|(^|[_-])credentials\.json$", re.I)


def _is_app_config(f: Path) -> bool:
    """OAuth *app* config (client_secret.json / credentials.json) mints & refreshes tokens — its
    client_id/secret also live inside the token blob — so it is never sent as a request credential.
    The auto-generator neither stores nor binds it (a hand-written treg.json still may)."""
    return bool(_APP_CONFIG_RE.search(f.name))


def _header_from_stem(stem: str) -> str:
    """A request-header name from a credential's file stem: `developer_token` -> `developer-token`."""
    return re.sub(r"[_\s]+", "-", stem).strip("-").lower() or "authorization"


def auto_bindings(base: Path, files: list[Path]) -> tuple[list[dict], list[dict]]:
    """Secrets + NON-COLLIDING bindings for a skill's credential files (shared by the CLI's
    generate_contract and the dashboard's _classify, so both behave identically). The primary token
    (the oauth/bearer credential, else the first file) is injected as `Authorization: Bearer {secret}`;
    every ADDITIONAL credential gets its own header derived from its filename (`developer_token` ->
    `developer-token`) instead of all colliding on Authorization. OAuth app config is skipped."""
    creds = [f for f in files if not _is_app_config(f)]
    if not creds:
        return [], []
    kinds = {f: _guess_kind(f.name, f.read_text(errors="replace")) for f in creds}
    primary = next((f for f in creds if kinds[f] == "oauth"), creds[0])
    single = len(creds) == 1
    secrets: list[dict] = []
    bindings: list[dict] = []
    seen: set[str] = set()
    for f in creds:
        kind = kinds[f]
        name = base.name if single else f.stem
        while name in seen:  # two files with the same stem must not collide into one local_name
            name = f"{name}-{len(seen)}"
        seen.add(name)
        secrets.append({"file": str(f.relative_to(base)), "name": name, "kind": kind})
        if f is primary:
            bindings.append(_default_binding(name, kind, f))  # Authorization: Bearer {secret}
        else:  # a secondary credential -> its own header, value injected as-is (no Bearer)
            bindings.append({"secret": name, "injector": kind, "location": "header",
                             "name": _header_from_stem(f.stem), "format": "{secret}",
                             "secret_field": "access_token"})
    return secrets, bindings


def _secret_files(p: Path) -> list[Path]:
    return [f for sd in _SECRET_DIRS if (p / sd).is_dir()
            for f in sorted((p / sd).iterdir()) if f.is_file()]


def generate_contract(skill_dir: str | Path) -> dict:
    """Heuristically build a treg.json for a skill dir: auto-discover secrets (from .secret/.secrets),
    guess base_url + a default binding per secret, and list whatever is left in `_fill` for the user."""
    p = Path(skill_dir)
    if not p.is_dir():
        raise NotADirectoryError(f"{p} is not a directory")
    files = _secret_files(p)
    secrets, bindings = auto_bindings(p, files)

    base_url, fill = _guess_base_url(p)
    from . import providers  # local import: providers is dependency-light; avoids an import cycle
    skill_prov = providers.match_skill(p.name)
    if skill_prov:  # a curated catalog host beats the heuristic guess (e.g. google-ads, gsc)
        base_url = skill_prov["base_url"]
        fill = [n for n in fill if "NOT FOUND" not in n and "base_url" not in n.lower()]
    cli_profile = (skill_prov or {}).get("cli")
    if cli_profile and not cli_profile.get("unsupported"):
        # Emit the catalog's local-run profile into the contract (minus catalog metadata). Committing
        # the file is the creator's opt-in, so it ships enabled; they review deny/inject before pushing.
        contract_cli = {k: v for k, v in cli_profile.items() if k != "verified"}
        contract_cli["enabled"] = True
        fill.append(f"cli — local runs: members can `treg run {p.name}`; review the deny list + inject")
    else:
        contract_cli = None
    if len(secrets) > 1:
        fill.append("bindings — multiple credentials: the primary token is bound to Authorization and each "
                    "other to a header from its filename; verify each placement")
    if not files:
        fill.append("secrets — none found under .secret/.secrets; add any the API needs")
    fill.append("health — optional: add {\"path\": \"...\"} to enable health checks")

    fill.append("examples — optional: add [{\"method\":\"GET\",\"path\":\"...\",\"note\":\"...\"}] to power the dashboard")
    contract = {"name": p.name, "base_url": base_url or "", "secrets": secrets, "bindings": bindings, "examples": []}
    if contract_cli:
        contract["cli"] = contract_cli
    contract["_fill"] = fill
    return contract


def load_contract(skill_dir: str | Path) -> dict | None:
    """Return the parsed treg.json in a skill dir, or None if absent. A hand-edited, malformed file
    raises a clear ValueError rather than a bare JSONDecodeError traceback."""
    f = Path(skill_dir) / CONTRACT_FILE
    if not f.exists():
        return None
    try:
        contract = json.loads(f.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CONTRACT_FILE} is not valid JSON: {exc}") from exc
    if contract.get("cli") is not None:
        from .localrun import validate_cli_profile  # local import — keeps convert dependency-light
        try:
            validate_cli_profile(contract["cli"])
        except ValueError as exc:
            raise ValueError(f"{CONTRACT_FILE} cli block: {exc}") from exc
    return contract


def contract_to_skill_payload(skill_dir: str | Path, contract: dict) -> dict:
    """Turn a treg.json + its on-disk secret files into a POST /skills body (values loaded from disk).
    A stale/hand-edited contract (missing keys, a secret file that was moved) raises a clear error
    naming the offending entry instead of a bare KeyError/FileNotFoundError."""
    p = Path(skill_dir)
    name = contract.get("name") or p.name
    secrets = []
    for s in contract.get("secrets", []):
        if "name" not in s:
            raise ValueError(f"{CONTRACT_FILE} secret entry needs a 'name': {s!r}")
        if s.get("file"):
            f = resolve_secret_path(p, s["file"])
            if not f.exists():
                raise FileNotFoundError(f"{CONTRACT_FILE} references a missing secret file: {f}")
            value = f.read_text().strip()  # a trailing newline becomes an illegal header value (as build_payload does)
        elif s.get("env"):                     # env-sourced (a treg-import contract) → read from the environment
            import os
            value = os.environ.get(s["env"])
            if value is None:
                raise ValueError(f"{CONTRACT_FILE} secret {s['name']!r} needs env var {s['env']} (not set)")
        else:
            raise ValueError(f"{CONTRACT_FILE} secret entry needs a 'file' or 'env' source: {s!r}")
        secrets.append({"local_name": s["name"], "value": value, "kind": s.get("kind", "env")})
    if not contract.get("base_url"):
        raise ValueError(f"{CONTRACT_FILE} has no base_url")
    tool: dict = {"name": name, "base_url": contract["base_url"], "bindings": contract.get("bindings", [])}
    if contract.get("health"):
        tool["health_check"] = contract["health"]
    if contract.get("examples"):
        tool["examples"] = contract["examples"]
    if contract.get("cli"):
        cli = dict(contract["cli"])
        cli.setdefault("enabled", True)  # writing the block IS the creator's local-run opt-in
        tool["cli"] = cli
    from . import skills as _sk  # lazy: convert is imported early, skills pulls providers/catalog
    return {"name": name, "recipe": _read_recipe(p), "files": _sk.collect_files(p),
            "secrets": secrets, "tools": [tool]}
