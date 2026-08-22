#!/usr/bin/env python3
"""Compare catalogued path+method pairs with providers' public OpenAPI documents.

No credential is sent and no provider endpoint is called: this reads documentation only. Marked
retirements remain in YAML by design, so an absent marked route is acknowledged; an unmarked absent
route, a method change, or a marked route that has reappeared makes the command fail.

Usage:
    uv run --frozen python scripts/catalog_drift.py
    uv run --frozen python scripts/catalog_drift.py tikhub
    uv run --frozen python scripts/catalog_drift.py tikhub --spec-file /tmp/openapi.json
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "treg" / "catalog"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

# TikHub's origin has historically truncated openapi.json mid-document. Its `paths` map precedes
# the truncation, and the ingester's shared salvage routine is the one parser already maintained for
# that shape. Import it rather than growing a second almost-identical recovery algorithm.
try:  # Running as a script puts `scripts/` on sys.path; importing in tests puts the repo root there.
    from catalog_ingest import salvage_json_map  # type: ignore[import-not-found]  # noqa: E402
except ModuleNotFoundError:
    from scripts.catalog_ingest import salvage_json_map  # noqa: E402


class UnsupportedSpec(ValueError):
    """The linked document is a different API-description format (for example Google Discovery)."""


@dataclass
class Drift:
    provider: str
    catalogued: int
    spec_paths: int
    ok: list[dict] = field(default_factory=list)
    dead_path: list[dict] = field(default_factory=list)
    method_rot: list[tuple[dict, list[str]]] = field(default_factory=list)
    acknowledged: list[dict] = field(default_factory=list)
    restored: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.dead_path or self.method_rot or self.restored)


def _github_raw(url: str) -> str:
    """Turn a GitHub blob citation into the document it cites."""
    prefix = "https://github.com/"
    if url.startswith(prefix) and "/blob/" in url:
        repo_path, _, file_path = url[len(prefix):].partition("/blob/")
        branch, _, file_path = file_path.partition("/")
        return f"https://raw.githubusercontent.com/{repo_path}/{branch}/{file_path}"
    return url


def discover_specs(catalog_dir: Path) -> dict[str, str]:
    """Find one public spec per provider from catalog source provenance."""
    found: dict[str, str] = {}
    files = sorted(p for p in catalog_dir.glob("*.yaml")
                   if p.name not in {"capabilities.yaml", "fx.yaml"})
    # Core files sort before `.extended` and their explicit `source.openapi` is the curated choice.
    for path in files:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict) or not data.get("provider"):
            continue
        provider = str(data["provider"])
        source = data.get("source") or {}
        explicit = source.get("openapi") if isinstance(source, dict) else None
        candidates = [explicit] if isinstance(explicit, str) and explicit.strip() else []
        if not candidates and isinstance(source, dict):
            candidates = [u for u in source.get("spec_urls") or []
                          if isinstance(u, str) and "openapi" in u.lower()
                          and not any(mark in u for mark in ("<", ">", "{", "}"))]
        if candidates:
            found.setdefault(provider, _github_raw(candidates[0]))
    return found


def provider_endpoints(provider: str, catalog_dir: Path = CATALOG) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(catalog_dir.glob(f"{provider}*.yaml")):
        if path.stem.removesuffix(".extended") != provider:
            continue
        data = yaml.safe_load(path.read_text()) or {}
        if isinstance(data, dict) and data.get("provider") == provider:
            rows.extend(ep for ep in data.get("endpoints") or [] if isinstance(ep, dict))
    return rows


def fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "treg-catalog-drift/1", "Accept": "application/json, text/yaml, */*"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def parse_spec(text: str) -> tuple[dict, str]:
    """Return an OpenAPI paths map and the parser mode used."""
    document = None
    try:
        document = json.loads(text)
        mode = "json"
    except json.JSONDecodeError as json_error:
        try:
            paths = salvage_json_map(text, "paths")
            return paths, f"salvaged-json ({json_error.msg} at byte {json_error.pos})"
        except (ValueError, json.JSONDecodeError):
            try:
                document = yaml.safe_load(text)
                mode = "yaml"
            except yaml.YAMLError as yaml_error:
                raise ValueError(f"not JSON or YAML: {yaml_error}") from yaml_error
    if not isinstance(document, dict):
        raise ValueError("spec root is not a mapping")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        if "resources" in document and "rootUrl" in document:
            raise UnsupportedSpec("Google Discovery document (not OpenAPI paths)")
        raise ValueError("spec has no paths mapping")
    return paths, mode


def compare(provider: str, endpoints: list[dict], paths: dict) -> Drift:
    result = Drift(provider=provider, catalogued=len(endpoints), spec_paths=len(paths))
    for ep in endpoints:
        item = paths.get(ep.get("path"))
        marked = bool(str(ep.get("status") or "").strip())
        if not isinstance(item, dict):
            (result.acknowledged if marked else result.dead_path).append(ep)
            continue
        method = str(ep.get("method") or "GET").lower()
        available = sorted(k.upper() for k in item if k.lower() in HTTP_METHODS)
        if method not in {str(k).lower() for k in item}:
            if marked:
                result.acknowledged.append(ep)
            else:
                result.method_rot.append((ep, available))
        elif marked:
            result.restored.append(ep)
        else:
            result.ok.append(ep)
    return result


def _print_result(result: Drift, source: str, mode: str) -> None:
    print(f"{result.provider}: {result.catalogued} catalogued, {len(result.ok)} ok, "
          f"{len(result.dead_path)} dead-path, {len(result.method_rot)} method-rot, "
          f"{len(result.acknowledged)} acknowledged, {len(result.restored)} restored "
          f"(spec paths={result.spec_paths}, parser={mode}, source={source})")
    for ep in result.dead_path:
        print(f"  DEAD {ep.get('method')} {ep.get('path')}  {ep.get('id')}")
    for ep, methods in result.method_rot:
        print(f"  METHOD {ep.get('method')} -> {'/'.join(methods) or 'none'} "
              f"{ep.get('path')}  {ep.get('id')}")
    for ep in result.restored:
        print(f"  RESTORED {ep.get('method')} {ep.get('path')}  {ep.get('id')} "
              f"(catalog status={ep.get('status')})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("providers", nargs="*", help="provider service(s); default: every discovered spec")
    parser.add_argument("--spec-file", type=Path,
                        help="read this local spec (requires exactly one provider; useful offline)")
    parser.add_argument("--catalog-dir", type=Path, default=CATALOG)
    args = parser.parse_args(argv)

    specs = discover_specs(args.catalog_dir)
    selected = args.providers or sorted(specs)
    if args.spec_file and len(selected) != 1:
        parser.error("--spec-file requires exactly one provider")
    unknown = [provider for provider in selected if provider not in specs]
    if unknown:
        parser.error("no public OpenAPI source for: " + ", ".join(unknown))

    failed = False
    errors = False
    checked = 0
    for provider in selected:
        source = str(args.spec_file) if args.spec_file else specs[provider]
        try:
            text = args.spec_file.read_text() if args.spec_file else fetch(source)
            paths, mode = parse_spec(text)
        except UnsupportedSpec as exc:
            print(f"SKIP {provider}: {exc} ({source})")
            continue
        except (OSError, ValueError) as exc:
            print(f"ERROR {provider}: {exc} ({source})", file=sys.stderr)
            errors = True
            continue
        result = compare(provider, provider_endpoints(provider, args.catalog_dir), paths)
        _print_result(result, source, mode)
        failed = failed or result.failed
        checked += 1
    if errors:
        return 2
    if not checked:
        print("ERROR no OpenAPI providers were checked", file=sys.stderr)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
