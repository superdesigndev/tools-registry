"""Skill-directory scanner for `treg upload` (skill mode) — the skill analogue of providers.scan_env.

Point it at a directory of skills (each a subdir with a SKILL.md) and it classifies every one and
resolves what it takes to register it. Two outcomes matter:

- **api_tool** — a skill that calls an authed external API. Either it already carries a `treg.json`
  contract (use it), or we generate one: base_url from the script's `API = "…"`, the credential from a
  local `.secret*` file OR the auth env var the script reads (cross-referenced against the provider
  CATALOG for base_url + auth shape). Registers as a secret + proxied tool + the SKILL.md recipe.
- **recipe_only** — a knowledge/workflow skill (SKILL.md, no external authed API). Published as a
  recipe-only bundle (the SKILL.md text) so the whole team library lives in one installable place.

Detection reads scripts + SKILL.md + secret files; VALUES are only read at push time (skills.build_payload).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import convert, providers

# `API = "https://…"` / `BASE_URL = "…"` declared at a script's top — the most reliable base_url signal.
_BASE_RE = re.compile(r'(?:API|BASE|BASE_URL|API_URL|ENDPOINT|HOST)\s*=\s*["\'](https?://[^"\']+)["\']', re.I)
# env vars a script reads — Python (os.environ["X"]) OR shell ($X / ${X}).
_ENV_RE = re.compile(r'os\.environ(?:\.get)?\(\s*["\']([A-Z][A-Z0-9_]+)["\']|\$\{?([A-Z][A-Z0-9_]+)\}?')
_AUTHY = ("KEY", "TOKEN", "SECRET", "AUTH", "PAT")
_SCRIPT_GLOBS = ("*.py", "*.sh", "*.js", "*.ts", "*.mjs")


@dataclass
class SkillDetection:
    name: str
    path: str
    kind: str                                   # contract | generated | recipe_only | skip
    base_url: str | None = None
    secrets: list = field(default_factory=list)  # [{source:"file"|"env", file?/env?, name, kind}]
    bindings: list = field(default_factory=list)
    health: dict | None = None                   # optional {path:...} health check (from a treg.json)
    examples: list = field(default_factory=list)  # optional [{method,path,note}] for the dashboard
    gaps: list = field(default_factory=list)     # blockers to a clean register (missing base_url / key)
    note: str | None = None
    # Local-run profile from the contract's `cli` block (creator-declared → ships enabled). Catalog
    # CLIs store NOTHING — the grant merges the live catalog at run time; owner enables via PATCH.
    cli: dict | None = None

    @property
    def ready(self) -> bool:
        """True if it can register with no manual gap-filling (tool skills need base_url + a resolvable secret)."""
        return self.kind in ("contract", "recipe_only") or (self.kind == "generated" and not self.gaps)


def _read_scripts(p: Path) -> str:
    text, seen = "", set()
    for pat in _SCRIPT_GLOBS:
        for f in sorted(p.rglob(pat)):           # any depth, not just top-level + one level
            if f in seen:
                continue
            seen.add(f)
            try:
                text += f.read_text(errors="replace") + "\n"
            except OSError:
                pass
    return text


# A skill is marked by a SKILL.md (the Claude Code convention). README.md alone is too loose for
# DISCOVERY (a docs/ subdir would false-match), though _read_recipe still falls back to it for content.
_SKILL_MARKERS = ("SKILL.md", "skill.md")


def is_skill_dir(p: Path) -> bool:
    return any((p / f).exists() for f in _SKILL_MARKERS)


def scan_skills(skills_dir: str, catalog: list[dict] | None = None,
                env_names: set[str] | None = None) -> list[SkillDetection]:
    """Classify every skill under `skills_dir` — each subdir with a SKILL.md, OR the dir itself if IT is
    a single skill (so `treg upload skills` works from inside one skill). `env_names` = vars available."""
    catalog = catalog if catalog is not None else providers.CATALOG
    env_names = env_names or set()
    root = Path(skills_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    subdirs = [p for p in sorted(root.iterdir())
               if p.is_dir() and not p.name.startswith(".") and is_skill_dir(p)]
    if not subdirs and is_skill_dir(root):
        return [_classify(root, catalog, env_names)]      # the target dir IS a single skill
    return [_classify(d, catalog, env_names) for d in subdirs]


def _catalog_for_env(var: str, catalog: list[dict]) -> dict | None:
    comps = set(var.upper().split("_"))
    return providers._match_provider(comps, catalog)


def _contract_cli(contract: dict) -> dict | None:
    """The contract's `cli` block, defaulted to enabled — writing the block IS the creator's opt-in
    (a catalog-only CLI stays disabled until an owner flips it)."""
    cli = contract.get("cli")
    if not isinstance(cli, dict):
        return None
    out = dict(cli)
    out.setdefault("enabled", True)
    return out


def _catalog_cli_detection(name: str, d: Path, prov_entry: dict, env_names: set[str]) -> SkillDetection:
    """A recipe-only skill the catalog recognizes as a CLI → a RUNNABLE cli tool. Attach the catalog cli
    profile (enabled) and turn each injectable env credential into an env-sourced secret, discovered from
    the machine env at import (or asked for). Executed via `treg run`, not the proxy. `env_from` on an
    argv inject names the env var to source the value from (e.g. Vercel's --token)."""
    cli = {k: v for k, v in prov_entry["cli"].items() if k != "verified"}
    cli["enabled"] = True
    inject = [dict(e) for e in cli.get("inject") or []]
    secrets: list[dict] = []
    gaps: list[str] = []
    seen: set[str] = set()
    for e in inject:
        env_var = e.get("name") if e.get("via", "env") == "env" else e.get("env_from")
        if not env_var:
            continue  # can't discover this one from an env var; a hand-written treg.json can supply it
        local = env_var.lower().replace("_", "-")
        while local in seen:
            local = f"{local}-x"
        seen.add(local)
        e["secret"] = local  # the register path resolves this local_name → secret_id (stored in tool.cli)
        secrets.append({"source": "env", "env": env_var, "name": local, "kind": "env"})
        if env_var not in env_names:
            gaps.append(f"needs credential {env_var} (not found in your environment)")
    cli["inject"] = inject
    return SkillDetection(name=name, path=str(d), kind="generated", base_url=prov_entry.get("base_url", ""),
                          secrets=secrets, bindings=[], cli=cli, gaps=gaps, note="catalog CLI skill")


def cli_preview(det: SkillDetection, catalog: list[dict] | None = None) -> dict | None:
    """What the analyze preview should say about local runs for this skill: contract-declared,
    catalog-known (available once enabled), or explicitly unsupported (with the reason)."""
    if det.cli:
        src = "catalog" if det.note == "catalog CLI skill" else "contract"
        return {"source": src, "bin": det.cli.get("bin") or det.name,
                "enabled": bool(det.cli.get("enabled")), "needs_credential": bool(det.gaps)}
    prov_cli = (providers.match_skill(det.name, catalog) or {}).get("cli")
    if not prov_cli:
        return None
    if prov_cli.get("unsupported"):
        return {"source": "unsupported", "bin": prov_cli.get("bin") or det.name, "reason": prov_cli.get("reason", "")}
    return {"source": "catalog", "bin": prov_cli.get("bin") or det.name,
            "verified": bool(prov_cli.get("verified")), "enabled": False}


def _classify(d: Path, catalog: list[dict], env_names: set[str]) -> SkillDetection:
    name = d.name
    # (1) an existing, complete treg.json wins — use it verbatim (its secrets are local files).
    try:
        contract = convert.load_contract(d)
    except ValueError as exc:
        return SkillDetection(name=name, path=str(d), kind="skip", note=f"bad treg.json: {exc}")
    if contract and contract.get("base_url"):
        secrets = [{"source": "file", **s} for s in contract.get("secrets", [])]
        det = SkillDetection(name=name, path=str(d), kind="contract", base_url=contract["base_url"],
                             secrets=secrets, bindings=contract.get("bindings", []),
                             health=contract.get("health"), examples=contract.get("examples") or [],
                             note="has treg.json", cli=_contract_cli(contract))
        # Validate readiness (a contract is not inherently trustworthy — a missing file or an env var
        # absent on THIS machine must surface as a gap, so `ready` doesn't lie across machines).
        for s in contract.get("secrets", []):
            if s.get("file") and not convert.resolve_secret_path(d, s["file"]).exists():
                det.gaps.append(f"treg.json secret file missing: {s['file']}")
            elif s.get("env") and s["env"] not in env_names:
                det.gaps.append(f"needs env var {s['env']} — not found in the env")
        if not contract.get("bindings") and not contract.get("cli"):
            # a CLI tool legitimately has no HTTP bindings — its auth is the cli.inject, run via `treg run`
            det.gaps.append("treg.json has no bindings (tool would have no auth)")
        _flag_header_collisions(det)
        return det

    scripts = _read_scripts(d)
    base_m = _BASE_RE.search(scripts)
    secret_files = convert._secret_files(d)
    # Match auth env vars by NAME COMPONENT, not substring — else "PAT" in "PATH" flags os.environ["PATH"]
    # (ubiquitous) as a credential. So OAUTH_TOKEN/API_KEY match; PATH/PATTERN/AUTHOR don't. _ENV_RE has
    # two capture groups (python + shell) → flatten the tuples.
    found = [g for pair in _ENV_RE.findall(scripts) for g in pair if g]
    auth_envs = [e for e in dict.fromkeys(found) if set(e.split("_")) & set(_AUTHY)]

    # (2) no local credential + no API host in the scripts:
    if not (base_m or secret_files or auth_envs):
        # (2a) …but the CATALOG knows this skill as a CLI (e.g. stripe-cli) → make it a RUNNABLE cli tool:
        # attach the catalog's cli profile and source its credential from an env var (found on the machine
        # at import, or asked for). `treg run <name>` then works — no proxy involved.
        prov_entry = providers.match_skill(name, catalog)
        if prov_entry and prov_entry.get("cli") and not prov_entry["cli"].get("unsupported"):
            return _catalog_cli_detection(name, d, prov_entry, env_names)
        # (2b) otherwise it is a knowledge/workflow skill → publish the recipe only.
        return SkillDetection(name=name, path=str(d), kind="recipe_only", note="knowledge skill — recipe only")

    # (3) generate a contract. base_url: script decl → heuristic guess → catalog-by-name.
    det = SkillDetection(name=name, path=str(d), kind="generated")
    # A script's `API = "https://docs.…"` is documentation, not the API — reject doc hosts (like
    # convert._guess_base_url does) and fall through to the heuristic guess.
    base_url = base_m.group(1) if base_m else None
    if base_url and any(h in base_url.lower() for h in convert._DOC_HOST_HINTS):
        base_url = None
    if not base_url:
        base_url, _ = convert._guess_base_url(d)
    # A catalog match on the skill NAME is curated (a real host + auth), so it beats a heuristic guess
    # — e.g. google-ads/gsc, whose scripts only mention a generic googleapis.com / a docs host.
    skill_prov = providers.match_skill(name, catalog)
    if skill_prov:
        base_url = skill_prov["base_url"]
    secrets, bindings = [], []
    if secret_files:                      # prefer a credential the skill ships as a local file
        # Shared with the CLI's generate_contract: primary token -> Authorization, each additional
        # credential -> its own filename-derived header (so multi-credential skills don't collide).
        gen_secrets, bindings = convert.auto_bindings(d, secret_files)
        secrets = [{"source": "file", **s} for s in gen_secrets]
        # Even with a file credential, the catalog can still supply a base_url we couldn't find.
        if not base_url and auth_envs:
            prov = _catalog_for_env(auth_envs[0], catalog)
            if prov:
                base_url = prov["base_url"]
    elif auth_envs:                       # else the credential is an env var (from the user's .env)
        var = auth_envs[0]
        prov = _catalog_for_env(var, catalog)
        if prov and not base_url:
            base_url = prov["base_url"]
        binding = providers.build_binding(prov["auth"]) if prov else None
        binding = binding or {"injector": "env", "location": "header", "name": "Authorization", "format": "Bearer {secret}"}
        secrets.append({"source": "env", "env": var, "name": name, "kind": "env"})
        bindings.append({"secret": name, **binding})
        if var not in env_names:
            det.gaps.append(f"needs env var {var} — not found in the env")
        if len(auth_envs) > 1:   # a second credential the script reads — bind only the first, flag the rest
            det.note = f"reads {len(auth_envs)} auth vars ({', '.join(auth_envs)}); only {var} bound — verify"
    det.base_url = base_url
    det.secrets, det.bindings = secrets, bindings
    if not base_url:
        det.gaps.append("base_url not found — set it manually or use --llm")
    _flag_header_collisions(det)
    return det


def _flag_header_collisions(det: SkillDetection) -> None:
    """Multiple bindings targeting the SAME header (e.g. a skill shipping several credential files that
    all default to `Authorization`) silently overwrite each other and are rejected at registration.
    Surface it as an actionable gap so the analyze preview shows WHY — not a valid-looking skill that
    fails to register (google-ads/gsc ship client_secret + token + developer_token this way)."""
    hdr = [(b.get("name") or "Authorization").lower() for b in det.bindings if b.get("location", "header") == "header"]
    dup = sorted({h for h in hdr if hdr.count(h) > 1})
    if dup:
        det.gaps.append(f"ships multiple credentials that all map to one header ({', '.join(dup)}) — add a "
                        "treg.json mapping each credential to its own header (e.g. Authorization + developer-token)")


# --- contract + payload (increment 2) ------------------------------------------------------------
def to_contract(det: SkillDetection) -> dict:
    """The treg.json dict for a generated/contract skill (drops the internal `source` marker; keeps
    `file` for local-file secrets and `env` for env-sourced ones)."""
    secrets = [{k: v for k, v in s.items() if k != "source"} for s in det.secrets]
    c = {"name": det.name, "base_url": det.base_url or "", "secrets": secrets,
         "bindings": det.bindings, "examples": det.examples or []}
    if det.health:
        c["health"] = det.health
    if det.cli:  # a CLI tool's auth lives here (not in bindings) — persist it so re-upload stays runnable
        c["cli"] = det.cli
    return c


def write_contract(det: SkillDetection, force: bool = False) -> str | None:
    """Persist treg.json into the skill dir so the skill carries its own contract. Skips a skill that
    already has one (unless force). Returns the path written, or None if skipped. recipe_only/skip: no-op."""
    if det.kind not in ("generated",):
        return None                       # contract skills already have one; recipe_only has no tool
    import json
    path = Path(det.path) / convert.CONTRACT_FILE
    if path.exists() and not force:
        return None
    path.write_text(json.dumps(to_contract(det), indent=2))
    return str(path)


def env_needs(dets: list[SkillDetection]) -> list[str]:
    """Env var names the selected skills need for their credentials (to load values at push time)."""
    names = [s["env"] for d in dets for s in d.secrets if s.get("env")]
    return list(dict.fromkeys(names))     # de-dup while preserving order (two skills, same var)


# Companion-file collection so a WHOLE skill folder travels (not just SKILL.md). Skip the recipe
# itself, secret dirs (never ship a credential in the file blob), VCS/build/OS junk, and binaries.
_SKIP_DIRS = {".secret", ".secrets", ".git", "__pycache__", "node_modules", ".venv", ".idea"}
_SKIP_NAMES = {"SKILL.md", "treg.json", ".DS_Store"}
_MAX_FILE_BYTES = 512 * 1024      # a skill folder is assumed small; skip anything oversized
_MAX_TOTAL_BYTES = 4 * 1024 * 1024


def collect_files(skill_dir: str | Path) -> dict[str, str]:
    """Every text file under a skill dir as {relpath: content}, nested paths preserved — minus the
    recipe, secrets, junk, and binaries. `skill install` reconstructs the tree from this."""
    root = Path(skill_dir)
    out: dict[str, str] = {}
    total = 0
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts) or f.name in _SKIP_NAMES or f.name.endswith(".pyc"):
            continue
        try:
            if f.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding="utf-8")   # UnicodeDecodeError → binary → skip
        except (OSError, UnicodeDecodeError):
            continue
        total += len(text.encode("utf-8"))
        if total > _MAX_TOTAL_BYTES:
            break
        out[rel.as_posix()] = text
    return out


def build_payload(det: SkillDetection, values: dict[str, str]) -> dict:
    """POST /skills body. File secrets are read from disk; env secrets pull from `values`; recipe_only
    ships just the recipe (no tool/secret). Companion files (the rest of the folder) always ride along.
    Raises ValueError on an unresolved env value."""
    p = Path(det.path)
    recipe = convert._read_recipe(p)
    files = collect_files(p)
    if det.kind == "recipe_only":
        return {"name": det.name, "recipe": recipe, "files": files, "secrets": [], "tools": []}
    secrets = []
    for s in det.secrets:
        if s.get("file"):
            # strip trailing newline/space — a stray "\n" in a token becomes an illegal header value
            val = convert.resolve_secret_path(p, s["file"]).read_text().strip()
        elif s.get("env"):
            val = values.get(s["env"])
            if val is None:
                raise ValueError(f"missing env value for {s['env']}")
        else:
            raise ValueError(f"secret {s.get('name')!r} has neither a file nor an env source")
        secrets.append({"local_name": s["name"], "value": val, "kind": s.get("kind", "env")})
    tool = {"name": det.name, "base_url": det.base_url, "bindings": det.bindings}
    if det.health:
        tool["health_check"] = det.health
    if det.examples:
        tool["examples"] = det.examples
    if det.cli:
        tool["cli"] = det.cli  # inject entries may reference local names; the server resolves them
    return {"name": det.name, "recipe": recipe, "files": files, "secrets": secrets, "tools": [tool]}


# --- dev/test entrypoint: `python -m treg.skills --dir DIR [--env-dir ENVDIR]` --------------------
if __name__ == "__main__":
    import argparse
    import os
    ap = argparse.ArgumentParser(description="treg skill-dir scan (classification only)")
    ap.add_argument("--dir", required=True, help="a directory of skills (each a subdir with SKILL.md)")
    ap.add_argument("--env-dir", help="dir holding a .env whose keys satisfy env-sourced skill creds")
    args = ap.parse_args()

    names: set[str] = set()
    if args.env_dir and os.path.isfile(os.path.join(args.env_dir, ".env")):
        names = set(providers.var_names(os.path.join(args.env_dir, ".env")))
    dets = scan_skills(args.dir, env_names=names)
    order = {"contract": 0, "generated": 1, "recipe_only": 2, "skip": 3}
    labels = {"contract": "● API tool (has treg.json)", "generated": "● API tool (generated contract)",
              "recipe_only": "○ recipe-only (knowledge skill)", "skip": "· skip"}
    for k in sorted({d.kind for d in dets}, key=lambda x: order.get(x, 9)):
        group = [d for d in dets if d.kind == k]
        print(f"\n{labels.get(k, k)}  ({len(group)})")
        for d in group:
            if d.kind in ("contract", "generated"):
                src = ", ".join((s.get("env") or s.get("file")) for s in d.secrets) or "(no secret)"
                print(f"    {d.name:<28} {d.base_url or '(no base_url)'}")
                print(f"        secret: {src}" + (f"   ⚠ {'; '.join(d.gaps)}" if d.gaps else ""))
            else:
                print(f"    {d.name}")
    print(f"\n{len(dets)} skills: "
          + ", ".join(f"{sum(1 for d in dets if d.kind==k)} {k}" for k in ('contract', 'generated', 'recipe_only', 'skip')))
