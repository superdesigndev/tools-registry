#!/usr/bin/env python3
"""Bulk-ingest a provider's FULL endpoint surface into `src/treg/catalog/<provider>.extended.yaml`.

    uv run python scripts/catalog_ingest.py tikhub
    uv run python scripts/catalog_ingest.py dataforseo justoneapi
    uv run python scripts/catalog_ingest.py all --refresh

The hand-curated `<provider>.yaml` (tier: core) stays the quality tier: capability-mapped, live
verified, with captured example responses. This script produces the *coverage* tier — every route
the provider exposes, machine-generated, unmapped and unverified — so an agent browsing the
marketplace sees the real breadth of what a key unlocks.

Properties this script guarantees (they are why it exists instead of a one-off scrape):
  - **re-runnable**: same upstream in, byte-identical file out. Endpoints sort by (platform, path);
    nothing carries a timestamp except `source.ingested`, which only moves when the data does.
  - **core wins**: any (method, path) already in the provider's core yaml is skipped, never duplicated.
  - **cached**: downloads land in ~/.cache/treg-catalog-ingest (override TREG_INGEST_CACHE);
    `--refresh` re-fetches. Nothing is cached inside the repo.
  - **credential-free**: no source below needs auth, so no secret can reach a committed file.

See docs/context/architecture/catalog.md for the extended-entry schema.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"
CACHE = Path(os.environ.get("TREG_INGEST_CACHE") or (Path.home() / ".cache" / "treg-catalog-ingest"))

CJK = re.compile("[　-鿿＀-￯]")


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that survives real-world specs — DataForSEO's has a bare `=` enum value, which
    YAML 1.1 reserves as the 'default value' tag and SafeLoader refuses to construct."""


_TolerantLoader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: node.value)


# ---------------------------------------------------------------------------------------------
# plumbing


def fetch(url: str, key: str, *, refresh: bool = False, headers: dict[str, str] | None = None) -> bytes:
    """GET `url` through curl, cached on disk by `key`. Tolerates a truncated body (see tikhub)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    blob = CACHE / key
    if blob.is_file() and not refresh:
        return blob.read_bytes()
    cmd = ["curl", "-sSL", "--max-time", "300", "-o", str(blob)]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True)
    if not blob.is_file() or blob.stat().st_size == 0:
        raise SystemExit(f"fetch failed: {url}\n{proc.stderr.decode()[:400]}")
    return blob.read_bytes()


def salvage_json_map(text: str, at_key: str) -> dict:
    """Parse the `at_key` object of a JSON document that may be TRUNCATED mid-stream.

    api.tikhub.io/openapi.json is cut off by the origin at ~840 KB (reproducible across encodings
    and clients), so a plain json.loads yields nothing at all. Decoding entry-by-entry keeps every
    complete member and drops only the one that was cut.
    """
    dec = json.JSONDecoder()
    start = text.index(f'"{at_key}"')
    pos = text.index("{", start) + 1
    out: dict = {}
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != '"':
            break
        try:
            key, pos = dec.raw_decode(text, pos)
            pos = text.index(":", pos) + 1
            while text[pos] in " \t\r\n":
                pos += 1
            val, pos = dec.raw_decode(text, pos)
        except (ValueError, IndexError):
            break
        out[key] = val
    return out


ZERO_WIDTH = re.compile("[​-‏‪-‮﻿]")


def clean(s: str) -> str:
    """Collapse whitespace and drop the zero-width joiners providers sprinkle through their docs."""
    return " ".join(ZERO_WIDTH.sub("", s or "").split()).strip()


def english(s: str) -> str:
    """TikHub summaries are '中文描述/English description' — keep the English half."""
    s = clean(s)
    parts = s.split("/")
    for i in range(len(parts)):
        cand = "/".join(parts[i:]).strip()
        if cand and not CJK.search(cand):
            return cand
    return s


def titlecase(slug: str) -> str:
    words = re.split(r"[_\-]+", slug)
    return " ".join(words).strip().capitalize()


def short_name(candidate: str, summary: str) -> str:
    """`candidate` as an endpoint's short display `name`, or "" when it adds nothing.

    A name is only worth carrying when the upstream spec offers a real human title that is
    DISTINCT from the description we keep verbatim in `summary`: non-empty after cleaning, no
    CJK left, fits a row heading (≤60 chars — the validator's cap), and not just the summary
    (or its first 60 chars) restated.
    """
    cand = english(candidate or "")
    if not cand or CJK.search(cand) or len(cand) > 60:
        return ""
    if cand.lower() in (clean(summary or "").lower(), clean(summary or "")[:60].lower()):
        return ""
    return cand


def slug_id(provider: str, path: str) -> str:
    body = re.sub(r"[^a-z0-9]+", "-", path.lower().lstrip("/")).strip("-")
    return f"{provider}.x.{body}"


def core_routes(provider: str) -> set[tuple[str, str]]:
    """(METHOD, path) pairs already curated in the provider's core yaml — those win."""
    f = CATALOG / f"{provider}.yaml"
    if not f.is_file():
        return set()
    data = yaml.safe_load(f.read_text()) or {}
    return {
        (str(ep.get("method", "")).upper(), str(ep.get("path", "")))
        for ep in (data.get("endpoints") or [])
    }


def carry_verification(provider: str, endpoints: list[dict]) -> int:
    """Re-attach `verified` / `example_response` / `unverified` / `capability` from the file being replaced.

    Those fields are the only ones NOT derived from upstream: verification stamps are the result
    of an actual paid call made by scripts/catalog_verify_extended.py, and `capability` is a
    reviewed mapping into the capabilities.yaml taxonomy (2026-07-28 batch onwards). `name` (the
    short display title) and `kind` (data | action | account | utility — a reviewed judgement the
    ingest cannot re-derive) are carried on the same guard: ingest generates a title where the spec
    offers one, but a reviewed/hand-set title or kind must survive a re-ingest like a capability.
    Regenerating the file must not silently discard them, so they are carried across by id, as long as the
    route itself (method + path) still matches — a route that moved is a different endpoint and
    its old result means nothing.

    A carried `capability` brings its `platform` with it: the validator requires platform ==
    the capability's first segment, and review sometimes refines the ingest guess (e.g. douyin ->
    douyin-xingtu, tiktok -> tiktok-shop), so regenerating the guess over the reviewed platform
    would break validation on the next run.

    A stamp carries its `test_request` with it. The generated request is our best guess from the
    docs; a request stamped `verified` is one we watched succeed, including the case where the
    verifier had to repair it (a page-size knob the endpoint rejected). Regenerating the guess over
    the top of a proven request would drop the stamp on the next ingest and re-bill the call to
    learn what the file already knew. `--refresh` re-fetches the docs; re-verifying is what
    replaces a proven request, not re-ingesting.
    """
    old_file = CATALOG / f"{provider}.extended.yaml"
    if not old_file.is_file():
        return 0
    old = {ep["id"]: ep for ep in (yaml.safe_load(old_file.read_text()) or {}).get("endpoints") or []}
    kept = 0
    for ep in endpoints:
        prev = old.get(ep["id"])
        if not prev:
            continue
        if prev.get("method") != ep.get("method") or prev.get("path") != ep.get("path"):
            continue
        for field in ("verified", "example_response", "unverified", "capability", "name", "kind"):
            if prev.get(field) is not None:
                ep[field] = prev[field]
        if prev.get("capability") is not None and prev.get("platform"):
            # the reviewed platform (capability's first segment) wins over the ingest guess
            ep["platform"] = prev["platform"]
        if prev.get("verified"):
            kept += 1
            if prev.get("test_request") is not None:
                ep["test_request"] = prev["test_request"]
                ep.pop("untestable", None)
    return kept


def write_extended(provider: str, source: dict, endpoints: list[dict], notes: list[str]) -> Path:
    carried = carry_verification(provider, endpoints)
    if carried:
        print(f"  carried {carried} verification stamp(s) forward", file=sys.stderr)
    endpoints = sorted(endpoints, key=lambda e: (e["platform"], e["path"], e["method"]))
    seen: set[str] = set()
    for ep in endpoints:
        if ep["id"] in seen:
            raise SystemExit(f"{provider}: duplicate generated id {ep['id']}")
        seen.add(ep["id"])
    out = CATALOG / f"{provider}.extended.yaml"
    header = [
        f"# {provider} — EXTENDED tier: the provider's full endpoint surface, machine-generated by",
        "# scripts/catalog_ingest.py. Do not hand-edit; re-run the script instead. Entries start with",
        "# no capability, no verification and no example response — verification stamps and reviewed",
        "# `capability` mappings (with their platform correction) are added later and carried across",
        f"# re-ingests by id via carry_verification. Routes curated in core {provider}.yaml are excluded here.",
    ]
    header += [f"# {n}" for n in notes]
    body = yaml.safe_dump(
        {"provider": provider, "source": source, "endpoints": endpoints},
        sort_keys=False,
        allow_unicode=True,
        width=4096,
        default_flow_style=False,
    )
    out.write_text("\n".join(header) + "\n\n" + body)
    return out


# ---------------------------------------------------------------------------------------------
# tikhub

# path segment after /api/v1/ → platform slug. Absent = use the segment as-is.
TIKHUB_PLATFORM = {"twitter": "x", "net_ease_cloud_music": "netease-music", "hybrid": "web"}
TIKHUB_PLATFORM.update({k: k.replace("_", "-") for k in ("wechat_channels", "wechat_mp", "wechat_search")})
# utility families, not data: solved captchas, throwaway inboxes, the account's own meter, the
# iOS-shortcut installer, the MCP bridge and the Sora2 video generator.
TIKHUB_SKIP = {"captcha", "temp_mail", "ios_shortcut", "mcp", "sora2", "tikhub", "health", "downloader"}


# --- TikHub parameter documentation -----------------------------------------------------------
#
# api.tikhub.io/openapi.json is served truncated at 842001 bytes (see the note in ingest_tikhub),
# which costs us the parameter block of ~78% of the routes. TikHub's published docs are an Apifox
# site, and Apifox serves the same project as JSON through the API its own front-end calls — no
# auth, no truncation, and richer than the OpenAPI: every parameter carries a `sampleValue`, the
# working example value TikHub themselves demo the endpoint with. That is what makes a generated
# `test_request` a real call rather than a guess.
#
#   /api/v1/published-projects/domains/<domain>   → the project + branch ids
#   /api/v1/published-projects/<id>/http-api-tree → every documented operation, with its api id
#   /api/v1/published-projects/<id>/http-apis/<n> → one operation: parameters, requestBody, samples

TIKHUB_DOCS = "https://docs.tikhub.io"
_JSON_HDR = {"accept": "application/json"}

# Parameters that name the CALLER's own infrastructure or credentials. A test request must never
# invent a value for these — an endpoint that truly needs one cannot be verified with a bare key.
TIKHUB_PARAM_SKIP = {
    "cookie", "cookies", "proxy", "proxy_url", "token", "api_key", "apikey", "authorization",
    "x-api-key", "session", "sessionid", "auth", "access_token", "refresh_token",
}
# Page-size knobs. Clamped to the smallest useful page: it keeps the captured example small and,
# on the per-result endpoints, keeps the call cheap.
TIKHUB_COUNT_PARAMS = {
    "count", "limit", "size", "num", "number", "page_size", "pagesize", "per_page", "perpage",
    "page_count", "pagecount", "page_size_", "result_count",
}
TIKHUB_TEST_COUNT = 5


def tikhub_docs_apis(refresh: bool) -> dict[str, dict]:
    """`path` → the Apifox operation object (parameters, requestBody, sampleValues)."""
    proj = json.loads(fetch(
        f"{TIKHUB_DOCS}/api/v1/published-projects/domains/docs.tikhub.io",
        "tikhub_apifox_project.json", refresh=refresh, headers=_JSON_HDR,
    ))["data"]
    pid = proj["id"]
    tree = json.loads(fetch(
        f"{TIKHUB_DOCS}/api/v1/published-projects/{pid}/http-api-tree?locale=en-US",
        "tikhub_apifox_tree.json", refresh=refresh, headers=_JSON_HDR,
    ))["data"]

    ids: list[int] = []

    def walk(nodes: list) -> None:
        for n in nodes:
            api = n.get("api") or {}
            if api.get("id") and api.get("path"):
                ids.append(api["id"])
            walk(n.get("children") or [])

    walk(tree)
    todo = [i for i in ids if refresh or not (CACHE / "tikhub_apifox" / f"{i}.json").is_file()]
    if todo:
        print(f"  fetching {len(todo)} documented operation(s) from the Apifox docs API…", file=sys.stderr)

        def one(api_id: int) -> None:
            fetch(f"{TIKHUB_DOCS}/api/v1/published-projects/{pid}/http-apis/{api_id}?locale=en-US",
                  f"tikhub_apifox/{api_id}.json", refresh=refresh, headers=_JSON_HDR)

        (CACHE / "tikhub_apifox").mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one, todo))

    out: dict[str, dict] = {}
    for api_id in ids:
        blob = CACHE / "tikhub_apifox" / f"{api_id}.json"
        if not blob.is_file():
            continue
        try:
            data = json.loads(blob.read_text())["data"]
        except (ValueError, KeyError):
            continue
        if data.get("path"):
            out[data["path"]] = data
    return out


def tikhub_docs_schemas(refresh: bool) -> dict[str, dict]:
    """`#/definitions/<id>` targets — the shared request/response models a requestBody may $ref."""
    proj = json.loads(fetch(
        f"{TIKHUB_DOCS}/api/v1/published-projects/domains/docs.tikhub.io",
        "tikhub_apifox_project.json", refresh=refresh, headers=_JSON_HDR,
    ))["data"]
    raw = json.loads(fetch(
        f"{TIKHUB_DOCS}/api/v1/published-projects/{proj['id']}/data-schemas",
        "tikhub_apifox_schemas.json", refresh=refresh, headers=_JSON_HDR,
    ))["data"]
    return {str(s["id"]): (s.get("jsonSchema") or {}) for s in raw if s.get("id")}


def _schema_example(schema: dict, defs: dict, depth: int = 0):
    """A concrete value for `schema`, built only from values the docs actually state.

    Returns None when the docs state none — a request body we invented would test our imagination,
    not the endpoint. Arrays are built with a SINGLE item: these are batch routes, and on a
    per-result price a 10-id example body costs 10x for no extra information.
    """
    if depth > 4 or not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if ref:
        return _schema_example(defs.get(str(ref).rsplit("/", 1)[-1]) or {}, defs, depth + 1)
    for key in ("default", "example"):
        if schema.get(key) not in (None, "", [], {}):
            return schema[key]
    examples = schema.get("examples") or []
    if examples and examples[0] not in (None, ""):
        return examples[0]
    stype = schema.get("type")
    if stype == "array":
        item = _schema_example(schema.get("items") or {}, defs, depth + 1)
        return None if item is None else [item]
    if stype == "object":
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        out = {}
        for name, sub in props.items():
            val = _schema_example(sub if isinstance(sub, dict) else {}, defs, depth + 1)
            if val is not None:
                out[name] = val
            elif name in required:
                return None  # a required field we cannot fill makes the whole body a guess
        return out
    return None


def _body_example(body_spec: dict, defs: dict):
    """The request body a test call should send, or None if the docs give us nothing usable."""
    for ex in body_spec.get("examples") or []:
        value = ex.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if value in (None, ""):
            continue
        return value[:1] if isinstance(value, list) else value
    built = _schema_example(body_spec.get("jsonSchema") or {}, defs)
    if built is None and not body_spec.get("required"):
        return {}
    return built


def _param_value(p: dict):
    """The value a test request should send for parameter `p`, or None if the docs give us none.

    Order: TikHub's own demo value → the schema default → the first enum member. Anything else
    would be us inventing an id, and an invented id verifies nothing.
    """
    schema = p.get("schema") or {}
    for cand in (p.get("sampleValue"), schema.get("default")):
        if cand not in (None, "", [], {}):
            return cand
    enum = schema.get("enum") or []
    if enum:
        return enum[0]
    return None


def _coerce(value, ptype: str):
    if ptype == "integer" and isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    if ptype == "boolean" and isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return value


def tikhub_input_and_test(op: dict, defs: dict, *, method: str = "GET",
                          spec_op: dict | None = None) -> tuple[dict, dict, str]:
    """Build an endpoint's `input` schema and `test_request` from one Apifox operation.

    Returns (input, test_request, reason). `test_request` is empty when the operation cannot be
    called blind — every such case gets a `reason` that ends up in the file as an `unverified:`
    note, so a human can see WHY rather than just that it is missing.

    Which parameters the test sends:
      - every REQUIRED one (if any of them has no documented value, there is no test request —
        guessing a user id produces a 404 that says nothing about the endpoint);
      - every optional one that carries a `sampleValue`, because TikHub marks the real selector
        optional all the time (fetch_user_profile's `uniqueId` is "optional" but the call is
        meaningless without it) — a plain schema default is left off instead, since the server
        applies it anyway;
      - page-size knobs clamped to TIKHUB_TEST_COUNT.
    """
    params = (op.get("parameters") or {}).get("query") or []
    body_spec = op.get("requestBody") or {}
    inp: dict = {}
    qs: dict = {}
    test_q: dict = {}
    unresolved: list[str] = []
    skipped: list[str] = []

    for p in params:
        name = p.get("name")
        if not name or p.get("enable") is False:
            continue
        schema = p.get("schema") or {}
        ptype = p.get("type") or schema.get("type") or "string"
        entry: dict = {"type": ptype, "required": bool(p.get("required"))}
        note = english(p.get("description") or "")
        if note:
            entry["note"] = note
        value = _param_value(p)
        if value not in (None, "", [], {}):
            entry["example"] = _coerce(value, ptype)
        if schema.get("enum"):
            entry["enum"] = schema["enum"]
        qs[name] = entry

        if name.lower() in TIKHUB_PARAM_SKIP:
            if p.get("required"):
                skipped.append(name)
            continue
        if name.lower() in TIKHUB_COUNT_PARAMS and ptype in ("integer", "number"):
            test_q[name] = TIKHUB_TEST_COUNT
            continue
        if value in (None, "", [], {}):
            if p.get("required"):
                unresolved.append(name)
            continue
        if p.get("required") or p.get("sampleValue") not in (None, "", [], {}):
            test_q[name] = _coerce(value, ptype)

    if qs:
        inp["queryParams"] = qs

    body = None
    if (body_spec.get("type") or "none") == "application/json":
        js = body_spec.get("jsonSchema") or {}
        if js.get("$ref"):
            js = defs.get(str(js["$ref"]).rsplit("/", 1)[-1]) or js
        inp["bodyType"] = "json"
        inp["body"] = {"type": js.get("type", "object"), "required": bool(body_spec.get("required"))}
        note = english(js.get("description") or "")
        if note:
            inp["body"]["note"] = note
        props = js.get("properties") or {}
        if props:
            inp["body"]["properties"] = {
                k: {"type": v.get("type", "string"),
                    **({"note": english(v["description"])} if isinstance(v, dict) and v.get("description") else {}),
                    **({"required": True} if k in set(js.get("required") or []) else {})}
                for k, v in props.items() if isinstance(v, dict)
            }
        body = _body_example(body_spec, defs)
        if body is None:
            unresolved.append("<request body>")
        # the same rule as for query params: a body field naming the caller's own cookie/token
        # cannot be filled by us, and the docs' placeholder ("your_vip_bilibili_cookie") would send
        # a call that fails for a reason the failure text would not explain
        elif isinstance(body, dict):
            own = sorted(k for k in body if k.lower() in TIKHUB_PARAM_SKIP)
            if own:
                skipped.extend(own)

    if skipped:
        return inp, {}, f"needs the caller's own {', '.join(skipped)}"
    if unresolved:
        return inp, {}, f"no documented value for required {', '.join(unresolved)}"

    test: dict = {}
    if test_q:
        test["queryParams"] = test_q
    if body is not None:
        test["body"] = body

    # The verb and the parameter POSITION are one decision, and they were being made from two
    # different documents. Apifox lists every TikTok-Ads parameter under `parameters.query` while
    # the OpenAPI declares the same route POST-with-a-JSON-body, so the generator emitted a POST
    # whose arguments sat in the query string — still uncallable, just for a new reason, and a
    # plain re-ingest would silently undo the hand correction of 2026-08-17. When the spec says
    # this method carries a JSON body and Apifox gave us no body of its own, the documented
    # "query" parameters ARE that body's fields.
    # `not _spec_declares_query`: relocate only where the spec puts EVERYTHING in the body. A route
    # that genuinely takes both — TikHub's zhihu scholar search is the one live example — keeps its
    # query string, and moving those params into the body would break a currently-correct endpoint.
    # Latent rather than live today (that route's docs supply a body, so `body is None` already
    # blocked it), which is exactly why it is worth closing before it isn't.
    if (method not in ("GET", "HEAD", "DELETE") and body is None
            and _spec_wants_json_body(spec_op, method)
            and not _spec_declares_query(spec_op, method) and qs):
        inp.pop("queryParams", None)
        inp["bodyType"] = "json"
        inp["body"] = qs
        if "queryParams" in test:
            test["body"] = test.pop("queryParams")

    if not test:
        # no parameters at all: the route is callable bare, and an empty test_request is the
        # honest description of that call
        test = {"body": {}} if inp.get("bodyType") == "json" or (
            method not in ("GET", "HEAD", "DELETE") and _spec_wants_json_body(spec_op, method)
        ) else {"queryParams": {}}
    return inp, test, ""


def resolve_method(*, spec_op: dict | None, probed: str | None, documented: str = "") -> str:
    """Which HTTP verb this route takes, from the three sources that claim to know.

    Order matters and it was wrong. The provider's OWN OpenAPI wins whenever it names exactly one
    method for the route; only then do we fall back to the OPTIONS probe, then the docs portal, then
    the spec's first method, then GET. The probe infers a verb from a preflight, and a route that
    answers with a method LIST walks the probe's preference order and comes out `GET` no matter what
    the handler takes — so ranking it above the contract lets a guess overrule a published fact.

    (This is a hardening, not the cause of the 2026-08-17 TikTok-Ads breakage — see the note at the
    call site. Those routes really were GET when ingested; TikHub moved them afterwards.)
    """
    op = spec_op or {}
    spec_methods = [m.upper() for m in ("get", "post", "put", "patch", "delete") if m in op]
    if len(spec_methods) == 1:
        return spec_methods[0]
    return (probed or documented.upper() or (spec_methods[0] if spec_methods else "GET"))


def _spec_wants_json_body(spec_op: dict | None, method: str) -> bool:
    """Does the provider's OWN OpenAPI say this method carries a JSON request body?"""
    op = (spec_op or {}).get(method.lower()) or {}
    content = ((op.get("requestBody") or {}).get("content") or {})
    return any(str(k).startswith("application/json") for k in content)


def _spec_declares_query(spec_op: dict | None, method: str) -> bool:
    """…and does it ALSO declare query parameters for that method?"""
    op = (spec_op or {}).get(method.lower()) or {}
    return any(p.get("in") == "query" for p in (op.get("parameters") or []) if isinstance(p, dict))


def ingest_tikhub(refresh: bool) -> tuple[Path, dict]:
    card = json.loads(fetch(
        "https://api.tikhub.io/api/v1/tikhub/user/get_all_endpoints_info",
        "tikhub_rate_card.json", refresh=refresh,
    ))["data"]
    spec_text = fetch("https://api.tikhub.io/openapi.json", "tikhub_openapi.json", refresh=refresh)
    spec_paths = salvage_json_map(spec_text.decode("utf-8", "replace"), "paths")

    routes: dict[str, dict] = {}
    for row in card:
        # the rate card carries a few malformed uris — a stray \r\n, one missing its leading slash
        uri = row["endpoint_uri"].strip()
        if not uri.startswith("/"):
            uri = "/" + uri
        seg = uri.strip("/").split("/")
        if len(seg) < 4 or seg[0] != "api" or seg[1] != "v1" or seg[2] in TIKHUB_SKIP:
            continue
        routes.setdefault(uri, row)

    methods = tikhub_methods(sorted(routes), refresh=refresh)
    docs_apis = tikhub_docs_apis(refresh)
    docs_defs = tikhub_docs_schemas(refresh)
    skip = core_routes("tikhub")
    endpoints, from_spec, with_test, documented, dropped_undocumented = [], 0, 0, 0, 0
    for uri, row in routes.items():
        family = uri.strip("/").split("/")[2]
        platform = TIKHUB_PLATFORM.get(family, family.replace("_", "-"))
        op = spec_paths.get(uri) or {}
        doc_op = docs_apis.get(uri)
        # The SPEC decides whenever it declares exactly one method for the route — it is the
        # provider's own published contract, and it was wrong to rank it below an OPTIONS probe
        # that infers a verb from a preflight (a route answering with a list walks the preference
        # order below and comes out `GET`, whatever the handler actually takes).
        #
        # This is a hardening, NOT the cause of the 2026-08-17 TikTok-Ads breakage — the first
        # write-up of that fix said otherwise and was wrong. Those routes were GET when ingested:
        # the July spec says `get`, and the captured example_response is a real billed 200 from a
        # GET on 2026-07-27. TikHub MOVED them to POST afterwards. The catalog did not mis-read the
        # provider; it went stale, and nothing re-checks a provider's spec for drift. Preferring the
        # spec narrows the window (a re-ingest now inherits the change) but does not close it —
        # only re-ingesting does, and only if the cached spec is refreshed.
        method = resolve_method(spec_op=op, probed=methods.get(uri),
                                documented=(doc_op or {}).get("method", ""))
        if (method, uri) in skip:
            continue
        label = titlecase(family)
        spec_summary = english((op.get(method.lower()) or {}).get("summary") or "")
        if CJK.search(spec_summary):
            spec_summary = ""  # a handful of routes are documented in Chinese only
        if spec_summary:
            from_spec += 1
            # TikHub's terse ones ("Home Feed", "Search user") lose their subject once the
            # endpoint is listed next to 22 other platforms — give it back
            summary = spec_summary if len(spec_summary) >= 16 else f"{label}: {spec_summary}"
        else:
            tail = uri.strip("/").split("/")[3:]
            where = "/".join(tail[:-1])
            summary = f"{label}: {titlecase(tail[-1]).lower()}" + (f" ({where})" if where else "")
        cost = float(row.get("endpoint_cost") or 0)
        entry = {
            "id": slug_id("tikhub", uri.replace("/api/v1/", "")),
            "tier": "extended",
            "platform": platform,
            "method": method,
            "path": uri,
            "summary": summary,
        }
        # Apifox gives every documented op a human title ("中文/Get TikHub user info") distinct
        # from the openapi summary — the English half is the display `name`.
        title = short_name((doc_op or {}).get("name") or "", summary)
        if title:
            entry["name"] = title
        entry["cost"] = (
            {"type": "free", "value": 0.0, "currency": "USD"} if cost == 0
            else {"type": "per_success", "value": cost, "currency": "USD"}
        )
        if doc_op is not None:
            documented += 1
            inp, test, reason = tikhub_input_and_test(doc_op, docs_defs, method=method, spec_op=op)
            if inp:
                entry["input"] = inp
            if test:
                entry["test_request"] = test
                with_test += 1
            elif reason:
                entry["untestable"] = reason
        else:
            # A route the rate card lists but the docs never describe is not usable inventory:
            # no parameter spec exists anywhere TikHub publishes, and spot checks 404 ("ghost
            # routes" the server enumerates but no longer serves). Dropped rather than catalogued —
            # 399 such routes on 2026-07-28; they return automatically if TikHub ever documents them.
            dropped_undocumented += 1
            continue
        endpoints.append(entry)

    source = {
        "method": "openapi + provider rate card",
        "ingested": str(date.today()),
        "spec_urls": [
            "https://api.tikhub.io/openapi.json",
            "https://api.tikhub.io/api/v1/tikhub/user/get_all_endpoints_info",
            "https://docs.tikhub.io/api/v1/published-projects/{id}/http-api-tree",
        ],
    }
    notes = [
        "",
        "Route list + per-endpoint USD price come from TikHub's own free rate card endpoint",
        "(get_all_endpoints_info), which is authoritative and complete. openapi.json is served",
        f"TRUNCATED by the origin (~840 KB cap), so it supplied summaries for only {from_spec} routes;",
        "the rest have a summary derived from the route itself. A single-method OpenAPI declaration",
        "is method ground truth; OPTIONS is the fallback when the spec is absent or ambiguous.",
        "Skipped families: captcha, temp_mail, ios_shortcut, sora2, tikhub (own account), health.",
        "",
        f"`input` and `test_request` come from TikHub's Apifox docs API, which documents {documented}",
        "of these routes in full — parameter types, which are required, and a `sampleValue` per",
        f"parameter that is TikHub's own working demo value. {with_test} routes got a test request;",
        "the rest carry `untestable:` saying what is missing (a parameter only the caller can",
        f"supply, such as their own platform cookie). {dropped_undocumented} rate-card routes with no",
        "documentation anywhere were DROPPED entirely (ghost routes; several 404 live). `verified` +",
        "`example_response` are stamped later by scripts/catalog_verify_extended.py and are carried",
        "across re-ingests by id, so regenerating this file does not throw away live test results.",
    ]
    return write_extended("tikhub", source, endpoints, notes), {"from_spec": from_spec}


def tikhub_methods(paths: list[str], *, refresh: bool) -> dict[str, str]:
    """Ground-truth HTTP method per route, via an OPTIONS probe (405 + `allow:` header).

    Starlette answers a non-matching method with 405 before the handler runs, so the probe is free
    — critical here, because a bare GET against a route whose params are all optional would execute
    and bill (the Moz quota trap in catalog.md, at 1400x).

    A probe that comes back without an `allow:` header (transient 5xx, connection reset — TikHub
    does both under load) is a MISS, not an answer: it is retried in-process and never written to
    the cache. Caching misses is how the first run of this script left 1361 of 1404 routes falling
    back to the "GET" default, 128 of them wrongly (they are POST-only).
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    blob = CACHE / "tikhub_methods.json"
    known: dict[str, str] = {}
    if blob.is_file() and not refresh:
        known = {k: v for k, v in json.loads(blob.read_text()).items() if v}
    todo = [p for p in paths if p not in known]
    if todo:
        print(f"  probing {len(todo)} route method(s) via OPTIONS…", file=sys.stderr)

        def probe(path: str) -> tuple[str, str]:
            for attempt in range(3):
                out = subprocess.run(
                    ["curl", "-sS", "-X", "OPTIONS", "-D", "-", "-o", "/dev/null",
                     "--max-time", "30", f"https://api.tikhub.io{path}"],
                    capture_output=True,
                ).stdout.decode("utf-8", "replace")
                m = re.search(r"^allow:\s*(.+)$", out, re.I | re.M)
                allowed = [a.strip().upper() for a in (m.group(1) if m else "").split(",") if a.strip()]
                for pref in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    if pref in allowed:
                        return path, pref
                time.sleep(1 + attempt)
            return path, ""

        with ThreadPoolExecutor(max_workers=8) as pool:
            for path, method in pool.map(probe, todo):
                if method:
                    known[path] = method
        blob.write_text(json.dumps(known, indent=0, sort_keys=True))
    return {k: v for k, v in known.items() if v}


# ---------------------------------------------------------------------------------------------
# dataforseo

# /v3/<family>/<sub>/… → the platform the data is ABOUT (not the API family it lives under).
DFS_PLATFORM = {
    ("serp", "google"): "google", ("serp", "bing"): "bing", ("serp", "yahoo"): "yahoo",
    ("serp", "baidu"): "baidu", ("serp", "naver"): "naver", ("serp", "seznam"): "seznam",
    ("serp", "youtube"): "youtube", ("serp", "ai_summary"): "google", ("serp", "screenshot"): "google",
    ("keywords_data", "google_ads"): "google", ("keywords_data", "google_trends"): "google",
    ("keywords_data", "bing"): "bing",
    ("dataforseo_labs", "google"): "google", ("dataforseo_labs", "bing"): "bing",
    ("dataforseo_labs", "amazon"): "amazon", ("dataforseo_labs", "apple"): "app-store",
    ("business_data", "google"): "google", ("business_data", "tripadvisor"): "tripadvisor",
    ("business_data", "trustpilot"): "trustpilot",
    ("merchant", "amazon"): "amazon", ("merchant", "google"): "google",
    ("app_data", "google"): "google-play", ("app_data", "apple"): "app-store",
    ("ai_optimization", "chat_gpt"): "chatgpt", ("ai_optimization", "gemini"): "gemini",
    ("ai_optimization", "claude"): "claude", ("ai_optimization", "perplexity"): "perplexity",
    ("ai_optimization", "llm_mentions"): "ai-search",
    ("ai_optimization", "ai_keyword_data"): "ai-search",
}
# result readers and task bookkeeping — not a data call an agent would choose
DFS_PLUMBING = {
    "tasks_ready", "tasks_fixed", "id_list", "errors", "webhook_resend", "status",
    "available_filters", "locations", "languages", "locations_and_languages", "categories",
    "models", "versions", "user_data", "force_stop", "task_get",
}
DFS_DOCS = re.compile(r"https://docs\.dataforseo\.com/[^'\s\"]+")


def ingest_dataforseo(refresh: bool) -> tuple[Path, dict]:
    raw = fetch(
        "https://raw.githubusercontent.com/dataforseo/OpenApiDocumentation/master/openapi_specification.yaml",
        "dataforseo_openapi.yaml", refresh=refresh,
    )
    spec = yaml.load(raw, Loader=_TolerantLoader)
    ops: dict[str, tuple[str, dict]] = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                ops[path] = (method.upper(), op)
                break

    # one canonical call per operation: the sync `live` form when it exists, else `task_post`.
    families: dict[str, list[str]] = {}
    for path in ops:
        seg = path.strip("/").split("/")
        if seg[-1] in DFS_PLUMBING or "task_get" in seg or path.endswith("/html"):
            continue
        stem = "/".join(seg[: seg.index("live")]) if "live" in seg else (
            "/".join(seg[:-1]) if seg[-1] == "task_post" else path)
        families.setdefault(stem, []).append(path)
    chosen: list[str] = []
    for paths in families.values():
        live = [p for p in paths if "/live" in p]
        chosen += live or paths

    skip = core_routes("dataforseo")
    endpoints = []
    for path in chosen:
        method, op = ops[path]
        if (method, path) in skip:
            continue
        seg = path.strip("/").split("/")
        platform = DFS_PLATFORM.get((seg[1], seg[2] if len(seg) > 2 else ""), "web")
        desc = clean(op.get("description") or "")
        docs = DFS_DOCS.search(desc)
        desc = re.split(r"\s*for more info please visit", desc)[0]
        summary = (re.split(r"(?<=[.!?])\s+", desc)[0] or op.get("operationId") or seg[-1]).strip()
        if not summary:
            summary = titlecase(seg[-1])
        entry = {
            "id": slug_id("dataforseo", path.replace("/v3/", "")),
            "tier": "extended",
            "platform": platform,
            "method": method,
            # base_url already ends in /v3 — a verbatim spec path would relay to /v3/v3/… (404 on
            # all 216 extended routes; found live 2026-07-30). Core files store /v3-less paths too.
            "path": path.removeprefix("/v3"),
            "summary": summary[:400],
        }
        # DataForSEO's spec has NO per-op summary/title field (checked 2026-07-28: 0 of 570 ops)
        # — the only short handle is the CamelCase operationId ("GoogleOrganicLiveRegular"),
        # which de-camels into a serviceable display title.
        title = short_name(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", op.get("operationId") or ""), summary)
        if title:
            entry["name"] = title
        if docs:
            entry["docs_url"] = docs.group(0).rstrip("',.").split("?")[0]
        endpoints.append(entry)

    source = {
        "method": "openapi",
        "ingested": str(date.today()),
        "spec_urls": [
            "https://github.com/dataforseo/OpenApiDocumentation/blob/master/openapi_specification.yaml"
        ],
    }
    notes = [
        "",
        "Selection rule: one canonical call per operation. DataForSEO exposes most jobs three ways",
        "(sync `/live/...`, async `/task_post` + `/task_get`, plus a `/live/html` raw-HTML twin);",
        "the sync `live` form is kept when it exists, `task_post` only when it does not. Task-",
        "bookkeeping routes (tasks_ready, tasks_fixed, id_list, errors, task_get) and reference",
        "lookups (locations, languages, categories, available_filters) are omitted.",
        "`platform` is the system the data DESCRIBES (google, amazon, app-store, chatgpt…), not the",
        "API family; anything not tied to one engine is `web`. No per-endpoint price: DataForSEO's",
        "pricing is published per API family on a separate page, not in the spec.",
    ]
    return write_extended("dataforseo", source, endpoints, notes), {}


# ---------------------------------------------------------------------------------------------
# justoneapi

# Just One API's own x-platform-id values are marketing names ("douyin-tiktok-china"); normalise
# them onto the slugs the rest of the catalog already uses so a platform page shows every provider.
JOA_PLATFORM = {
    "douban-movie": "douban",
    "dewu-poizon": "dewu",
    "douyin-tiktok-china": "douyin",
    "douyin-e-commerce": "douyin-shop",
    "douyin-creator-marketplace-xingtu": "douyin-xingtu",
    "xiaohongshu-rednote": "xiaohongshu",
    "xiaohongshu-creator-marketplace-pugongying": "xiaohongshu-pugongying",
    "qq-huxuan-creator-marketplace": "qq-huxuan",
    "wechat-official-accounts": "wechat-mp",
    "taobao-and-tmall": "taobao",
    "xianyu-goofish": "xianyu",
    "jdcom": "jd",
    "twitter": "x",
    "social-media": "web",   # one cross-platform Chinese-web search endpoint
    "llm": "doubao",         # the family holds a single endpoint, Doubao's web answer
}


def ingest_justoneapi(refresh: bool) -> tuple[Path, dict]:
    sitemap = fetch("https://docs.justoneapi.com/en/sitemap.xml", "justoneapi_sitemap.xml",
                    refresh=refresh).decode()
    pages = sorted({
        m.group(1) for m in re.finditer(r"<loc>https://docs\.justoneapi\.com/en/api/([a-z0-9\-]+/[a-z0-9\-]+)</loc>", sitemap)
    })

    def load(rel: str) -> tuple[str, dict | None]:
        platform, endpoint = rel.split("/")
        try:
            blob = fetch(
                f"https://docs.justoneapi.com/openapi/{platform}/{endpoint}-en.json",
                f"justoneapi_{platform}__{endpoint}.json", refresh=refresh,
            )
            return rel, json.loads(blob)
        except Exception:
            return rel, None

    todo = [p for p in pages if not p.endswith("/")]
    print(f"  fetching {len(todo)} per-endpoint spec(s)…", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=8) as pool:
        specs = dict(pool.map(load, todo))

    skip = core_routes("justoneapi")
    # Prices are dashboard-only (no public price API); scripts/data/justoneapi_prices.json is a
    # hand-exported snapshot of the dashboard's Pricing page — re-export + rerun to refresh.
    prices_file = Path(__file__).parent / "data" / "justoneapi_prices.json"
    joa_prices: dict[str, float] = {}
    if prices_file.is_file():
        joa_prices = json.loads(prices_file.read_text()).get("prices", {})
    endpoints, missing = [], []
    for rel, spec in sorted(specs.items()):
        if not spec or not spec.get("paths"):
            missing.append(rel)
            continue
        family = rel.split("/")[0]
        tag_platform = next(
            (t.get("x-platform-id") for t in (spec.get("tags") or []) if t.get("x-platform-id")), None)
        raw = (tag_platform or family).replace("_", "-")
        platform = JOA_PLATFORM.get(family) or JOA_PLATFORM.get(raw) or raw
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                method = method.upper()
                if (method, path) in skip:
                    continue
                desc = clean(op.get("description") or op.get("summary")
                             or spec["info"].get("description") or "")
                summary = re.split(r"(?<=[.!?])\s+", desc)[0] if desc else spec["info"].get("title", rel)
                entry = {
                    "id": slug_id("justoneapi", path.replace("/api/", "")),
                    "tier": "extended",
                    "platform": platform,
                    "method": method,
                    "path": path,
                    "summary": summary[:400],
                    "docs_url": f"https://docs.justoneapi.com/en/api/{rel}",
                }
                # per-op `summary` is Just One API's short label ("Best Sellers"); the per-file
                # info.title ("Amazon Best Sellers API (V1)") backs it up
                title = short_name(clean(op.get("summary") or "") or spec["info"].get("title", ""),
                                   summary)
                if title:
                    entry["name"] = title
                price = joa_prices.get(path)
                if price is not None:
                    entry["cost"] = {"type": "per_success", "value": price, "currency": "CNY",
                                     "note": "billed only on success (code 0); errors free"}
                endpoints.append(entry)

    source = {
        "method": "per-endpoint openapi files linked from the docs sitemap",
        "ingested": str(date.today()),
        "spec_urls": [
            "https://docs.justoneapi.com/en/sitemap.xml",
            "https://docs.justoneapi.com/openapi/<platform>/<endpoint>-en.json",
        ],
    }
    notes = [
        "",
        "Just One API publishes no combined spec; every documented endpoint has its own OpenAPI file",
        "at /openapi/<platform>/<endpoint>-en.json, enumerated here from the English sitemap.",
        "`platform` comes from the spec's own x-platform-id tag, falling back to the docs URL segment.",
        "Endpoints whose docs page carries a `-deprecated` slug are kept — the provider still serves",
        "them — and are visible as such in the id. Prices come from scripts/data/justoneapi_prices.json,",
        "a hand-exported snapshot of the logged-in dashboard's Pricing page (CNY per successful request);",
        "an endpoint without a cost was absent from that export (e.g. not activated for the account).",
    ]
    if missing:
        notes.append(f"{len(missing)} doc page(s) had no fetchable spec and were skipped.")
    return write_extended("justoneapi", source, endpoints, notes), {"missing": len(missing)}


INGESTERS = {"tikhub": ingest_tikhub, "dataforseo": ingest_dataforseo, "justoneapi": ingest_justoneapi}


# =============================================================================================
# FIRST-PARTY OAUTH PROVIDERS
#
# The scraper providers above sell breadth, and their extended tier reads as a menu. These nine
# are the opposite: one connected account, and the question the founder asked is "what can this
# credential actually DO?". Three things are therefore different here and are worth stating once:
#
#   - **Scope gaps are data, not a filter.** A method whose Google/X scopes are NOT covered by what
#     treg's OAuth apps request is still listed, carrying `scope_gap:` naming the missing scope.
#     Dropping them would hide exactly the list someone needs in order to decide which scopes to
#     add to the registered apps. Nothing generated here is callable-by-default in the way a
#     scraper route is, so "listed" never implies "works today".
#   - **Some methods live on a SIBLING HOST.** Google splits one product across several
#     `*.googleapis.com` services (GA4 reporting vs GA4 admin; four separate My Business services)
#     while `OAuthProvider.base_url` names exactly one. Those entries carry `host:` and their
#     `path` is relative to THAT host, not to base_url. This is the same class of bug as the
#     DataForSEO `/v3` note in docs/context/architecture/catalog.md — recorded explicitly instead
#     of silently producing paths that 404 against the provisioned tool.
#   - **No test_request, no verification.** These call a real business's own account; a generated
#     test request would need a property id, a customer id or a Page id that no spec can supply.
#     A later wave replays them through `--via-treg` with a live connection.
#
# Core-wins dedup compares paths with their placeholders NORMALISED ({property} == {property_id}),
# because a hand-curated core file and a machine spec never agree on placeholder spelling — the
# naive (method, path) comparison is what let every DataForSEO core route reappear in extended.


def _route_key(method: str, path: str) -> tuple[str, str]:
    return str(method).upper(), re.sub(r"\{[^}]*\}", "{}", str(path))


def core_route_keys(provider: str) -> set[tuple[str, str]]:
    return {_route_key(m, p) for m, p in core_routes(provider)}


def _oauth_source(method: str, urls: list[str]) -> dict:
    return {"method": method, "ingested": str(date.today()), "spec_urls": urls}


# --- Google discovery documents ----------------------------------------------------------------
# Every Google REST API publishes a machine-readable Discovery document describing every method:
# httpMethod, flatPath (the URL relative to the service host), a description, the full parameter
# list with types/required flags/enums, and the OAuth scopes the method accepts.

_DISCOVERY = "https://{svc}.googleapis.com/$discovery/rest?version={ver}"


def google_discovery(service: str, version: str, refresh: bool) -> dict:
    return json.loads(fetch(
        _DISCOVERY.format(svc=service, ver=version),
        f"google_discovery_{service}_{version}.json", refresh=refresh, headers=_JSON_HDR,
    ))


def google_methods(doc: dict) -> list[dict]:
    """Every method in a discovery document, flattened out of its nested resource tree."""
    out: list[dict] = []

    def walk(res: dict) -> None:
        for _, r in sorted((res.get("resources") or {}).items()):
            for _, m in sorted((r.get("methods") or {}).items()):
                out.append(m)
            walk(r)

    walk(doc)
    return out


def _google_input(m: dict) -> dict:
    """`input` from the discovery method's parameter block — names, types, required, enums."""
    inp: dict = {}
    path_p: dict = {}
    query_p: dict = {}
    for name, p in sorted((m.get("parameters") or {}).items()):
        entry: dict = {"type": p.get("type", "string"), "required": bool(p.get("required"))}
        note = clean(p.get("description") or "")
        if note:
            entry["note"] = note[:300]
        if p.get("enum"):
            entry["enum"] = p["enum"]
        (path_p if p.get("location") == "path" else query_p)[name] = entry
    if path_p:
        inp["pathParams"] = path_p
    if query_p:
        inp["queryParams"] = query_p
    if m.get("request"):
        ref = str((m["request"] or {}).get("$ref") or "object")
        inp["bodyType"] = "json"
        inp["body"] = {
            "type": "object", "required": True,
            "note": f"JSON body — the {ref} resource; see the method's API reference for its fields",
        }
    return inp


def _scope_gap(needed: list[str], granted: set[str]) -> str:
    """Empty when this credential can call the method; otherwise what is missing, in one line.

    Google's discovery lists ALTERNATIVE scopes (holding any one of them suffices), so the test is
    an intersection, not a subset.
    """
    if not needed or set(needed) & granted:
        return ""
    short = [s.rsplit("/", 1)[-1] for s in needed]
    return ("treg's OAuth app does not request this; needs one of: " + ", ".join(sorted(short)))


def google_entry(
    provider: str, m: dict, *, platform: str, granted: set[str],
    host: str = "", scope: str = "own_account", cost: dict | None = None, docs_url: str = "",
) -> dict:
    """One extended entry from one discovery method.

    `path` prefers the media-upload URL when the method has one — an upload goes to
    /upload/<service>/… and posting the metadata URL instead is a silent 400.
    """
    upload = (((m.get("mediaUpload") or {}).get("protocols") or {}).get("simple") or {}).get("path")
    path = upload or ("/" + str(m.get("flatPath") or m.get("path") or "").lstrip("/"))
    mid = str(m["id"])
    desc = clean(m.get("description") or "")
    tail = ".".join(mid.split(".")[-2:])
    entry: dict = {
        "id": f"{provider}.x." + re.sub(r"[^a-z0-9]+", "-", mid.lower()).strip("-"),
        "tier": "extended",
        "platform": platform,
        "method": str(m["httpMethod"]).upper(),
        "path": path,
        "summary": (f"{tail} — {desc}" if desc else tail)[:400],
        "scope": scope,
    }
    if host:
        entry["host"] = host
    if cost:
        entry["cost"] = cost
    if docs_url:
        entry["docs_url"] = docs_url
    gap = _scope_gap(m.get("scopes") or [], granted)
    if gap:
        entry["scope_gap"] = gap
    inp = _google_input(m)
    if inp:
        entry["input"] = inp
    return entry


FREE_QUOTA = {"type": "free", "value": 0.0, "currency": "USD",
              "note": "no per-call charge; billed against the API's daily quota"}


# --- google-search-console ---------------------------------------------------------------------

def ingest_google_search_console(refresh: bool) -> tuple[Path, dict]:
    provider = "google-search-console"
    doc = google_discovery("searchconsole", "v1", refresh)
    granted = {
        "https://www.googleapis.com/auth/webmasters",
        "https://www.googleapis.com/auth/webmasters.readonly",
    }
    skip = core_route_keys(provider)
    endpoints, gaps = [], 0
    for m in google_methods(doc):
        # the Mobile-Friendly Test declares no scope at all: it tests any public URL and merely
        # happens to be hosted here, so it is not an own-account read
        public = not (m.get("scopes") or [])
        e = google_entry(provider, m, platform="search-console", granted=granted, cost=FREE_QUOTA,
                         scope="any_account" if public else "own_account",
                         docs_url="https://developers.google.com/webmaster-tools/v1/api_reference_index")
        if _route_key(e["method"], e["path"]) in skip:
            continue
        gaps += bool(e.get("scope_gap"))
        endpoints.append(e)
    notes = [
        "",
        "Source: the searchconsole v1 Discovery document, which covers BOTH surfaces of the product —",
        "the legacy `webmasters/v3/…` resources (sites, sitemaps, searchanalytics) and the newer",
        "`v1/…` ones (URL Inspection, the Mobile-Friendly Test). Both are served by",
        "searchconsole.googleapis.com, so every path here is relative to the provider's base_url.",
        "",
        "treg requests webmasters.readonly by default and webmasters for the `write` capability, so",
        "every method is reachable — sites.add/delete and sitemaps.submit/delete need `write`.",
        "urlTestingTools.mobileFriendlyTest.run declares NO scope: it is a public tool that happens to",
        "live on this host, so it is marked scope any_account.",
    ]
    return write_extended(provider, _oauth_source(
        "google discovery document",
        ["https://searchconsole.googleapis.com/$discovery/rest?version=v1"],
    ), endpoints, notes), {"scope_gaps": gaps}


# --- google-analytics --------------------------------------------------------------------------

def ingest_google_analytics(refresh: bool) -> tuple[Path, dict]:
    provider = "google-analytics"
    granted = {"https://www.googleapis.com/auth/analytics.readonly"}
    skip = core_route_keys(provider)
    endpoints, gaps, admin = [], 0, 0
    for m in google_methods(google_discovery("analyticsdata", "v1beta", refresh)):
        e = google_entry(provider, m, platform="google-analytics", granted=granted, cost=FREE_QUOTA,
                         docs_url="https://developers.google.com/analytics/devguides/reporting/data/v1")
        if _route_key(e["method"], e["path"]) in skip:
            continue
        gaps += bool(e.get("scope_gap"))
        endpoints.append(e)
    for m in google_methods(google_discovery("analyticsadmin", "v1beta", refresh)):
        e = google_entry(provider, m, platform="google-analytics", granted=granted, cost=FREE_QUOTA,
                         host="analyticsadmin.googleapis.com",
                         docs_url="https://developers.google.com/analytics/devguides/config/admin/v1")
        if _route_key(e["method"], e["path"]) in skip:
            continue
        gaps += bool(e.get("scope_gap"))
        admin += 1
        endpoints.append(e)
    notes = [
        "",
        "TWO HOSTS. Google splits GA4 in half: reporting (runReport, runRealtimeReport, funnels,",
        "pivots) is analyticsdata.googleapis.com — the provider's base_url — while everything that",
        "DESCRIBES the account (properties, data streams, custom dimensions, audiences, links to",
        "Ads/BigQuery/Search Console) is analyticsadmin.googleapis.com.",
        "",
        f"The {admin} Admin API entries below carry `host: analyticsadmin.googleapis.com` and their",
        "`path` is relative to THAT host. They are listed rather than dropped because the same OAuth",
        "token calls both and they are most of what a connected GA4 account can do — but the tool",
        "treg auto-provisions is bound to analyticsdata, so calling one needs a second tool bound to",
        "the admin host (or `treg connections resources`, which already does the property listing).",
        "Every entry WITHOUT a `host:` is callable through the provisioned google-analytics tool.",
        "",
        "treg requests analytics.readonly only. Every Admin write method (create/update/delete/",
        "archive) therefore carries `scope_gap:` naming analytics.edit — that list is the answer to",
        "'which scope would make GA4 writable'.",
    ]
    return write_extended(provider, _oauth_source(
        "google discovery documents (analyticsdata + analyticsadmin)",
        ["https://analyticsdata.googleapis.com/$discovery/rest?version=v1beta",
         "https://analyticsadmin.googleapis.com/$discovery/rest?version=v1beta"],
    ), endpoints, notes), {"scope_gaps": gaps, "admin": admin}


# --- google-business-profile -------------------------------------------------------------------
# Google retired the single "My Business API v4" into SIX narrow services, each on its own host.
# base_url is the account-management one, so only that family is callable through the provisioned
# tool; the rest carry `host:`. Reviews never made the migration and still live on the legacy v4
# host, which publishes NO discovery document — those few routes are hand-listed and flagged.

GBP_SERVICES = [
    ("mybusinessaccountmanagement", "v1", ""),  # == base_url: no host override needed
    ("mybusinessbusinessinformation", "v1", "mybusinessbusinessinformation.googleapis.com"),
    ("businessprofileperformance", "v1", "businessprofileperformance.googleapis.com"),
    ("mybusinessqanda", "v1", "mybusinessqanda.googleapis.com"),
    ("mybusinessplaceactions", "v1", "mybusinessplaceactions.googleapis.com"),
    ("mybusinessnotifications", "v1", "mybusinessnotifications.googleapis.com"),
    ("mybusinessverifications", "v1", "mybusinessverifications.googleapis.com"),
]

# The legacy v4 surface, which has no discovery document (https://mybusiness.googleapis.com/
# $discovery/rest?version=v4 answers 404). Reviews are the reason anyone connects this provider,
# so the handful that matter are transcribed from the published v4 reference. Kept deliberately
# short — hand-transcribed paths are how typos ship (catalog.md, step 1).
GBP_V4 = [
    ("reviews-get", "GET", "/v4/accounts/{account_id}/locations/{location_id}/reviews/{review_id}",
     "reviews.get — Read a single review left on one of your listings"),
    ("reviews-deletereply", "DELETE",
     "/v4/accounts/{account_id}/locations/{location_id}/reviews/{review_id}/reply",
     "reviews.deleteReply — Delete the business's public reply to a review"),
    ("reviews-batchget", "GET", "/v4/accounts/{account_id}/locations:batchGetReviews",
     "reviews.batchGetReviews — Read reviews across several of your listings in one call"),
    ("media-list", "GET", "/v4/accounts/{account_id}/locations/{location_id}/media",
     "media.list — List the photos and videos on one of your listings"),
    ("media-create", "POST", "/v4/accounts/{account_id}/locations/{location_id}/media",
     "media.create — Add a photo or video to one of your listings"),
    ("localposts-list", "GET", "/v4/accounts/{account_id}/locations/{location_id}/localPosts",
     "localPosts.list — List the local posts (offers, updates, events) on one of your listings"),
    ("localposts-create", "POST", "/v4/accounts/{account_id}/locations/{location_id}/localPosts",
     "localPosts.create — Publish a local post (offer, update or event) to one of your listings"),
]


def ingest_google_business_profile(refresh: bool) -> tuple[Path, dict]:
    provider = "google-business-profile"
    granted = {"https://www.googleapis.com/auth/business.manage"}
    skip = core_route_keys(provider)
    endpoints, off_host = [], 0
    for service, version, host in GBP_SERVICES:
        for m in google_methods(google_discovery(service, version, refresh)):
            e = google_entry(provider, m, platform="google-business", granted=granted,
                             host=host, cost=FREE_QUOTA,
                             docs_url=f"https://developers.google.com/my-business/reference/{service}/rest")
            if _route_key(e["method"], e["path"]) in skip:
                continue
            # discovery ids collide across the six services (accounts.list exists in three of them)
            e["id"] = f"{provider}.x.{service.removeprefix('mybusiness')}-" + e["id"].split(".x.", 1)[1]
            off_host += bool(host)
            endpoints.append(e)
    for slug, method, path, summary in GBP_V4:
        if _route_key(method, path) in skip:
            continue
        endpoints.append({
            "id": f"{provider}.x.v4-{slug}", "tier": "extended", "platform": "google-business",
            "method": method, "path": path, "summary": summary, "scope": "own_account",
            "host": "mybusiness.googleapis.com", "cost": FREE_QUOTA,
            "docs_url": "https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews",
        })
    notes = [
        "",
        "SIX HOSTS, plus a legacy one. Google broke the old My Business API into narrow services, one",
        "host each. base_url is mybusinessaccountmanagement.googleapis.com, so only that family is",
        "callable through the provisioned tool; every other entry carries `host:` and its `path` is",
        "relative to THAT host. Services covered: accountmanagement (accounts, admins, invitations),",
        "businessinformation (locations, attributes, categories, chains), businessprofileperformance",
        "(daily metrics and search keywords), qanda (questions and answers), placeactions, ",
        "notifications, verifications.",
        "",
        "Reviews never migrated: they are still on mybusiness.googleapis.com/v4, which publishes NO",
        "discovery document. The 7 v4 entries here are hand-transcribed from the published reference",
        "and are the only ones in this file not machine-derived — treat their paths with more",
        "suspicion than the rest. (The core file's two review endpoints have the same origin.)",
        "",
        "No scope_gap anywhere: the My Business discovery documents declare no scopes at all, so",
        "coverage cannot be computed from the spec. business.manage — what treg requests — is in",
        "practice the single scope the whole family uses. The real gate on this provider is not the",
        "scope but Google's separate Business Profile API quota grant, which starts every project at",
        "zero requests per day.",
    ]
    return write_extended(provider, _oauth_source(
        "google discovery documents (six My Business services) + hand-listed legacy v4 reviews",
        [_DISCOVERY.format(svc=s, ver=v) for s, v, _ in GBP_SERVICES]
        + ["https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews"],
    ), endpoints, notes), {"off_host": off_host}


# --- youtube -----------------------------------------------------------------------------------
# Quota, not money, is what runs out on the YouTube Data API: a project gets 10,000 units a day and
# a single search.list costs 100 of them. The per-method costs below are the published quota table
# (developers.google.com/youtube/v3/determine_quota_cost) — the discovery document does not carry
# them, and an agent that does not know search.list is 100x a videos.list will exhaust a day's
# quota in 100 calls.
YT_QUOTA = {
    "youtube.search.list": 100,
    "youtube.captions.list": 50, "youtube.captions.insert": 400, "youtube.captions.update": 450,
    "youtube.captions.download": 200, "youtube.captions.delete": 50,
    "youtube.videos.insert": 1600, "youtube.videos.getRating": 1,
}
# Reads that return the same data for anybody — the connected account is just how we authenticate.
YT_PUBLIC = {
    "youtube.videos.list", "youtube.search.list", "youtube.channels.list",
    "youtube.commentThreads.list", "youtube.comments.list", "youtube.playlists.list",
    "youtube.playlistItems.list", "youtube.channelSections.list", "youtube.activities.list",
    "youtube.videoCategories.list", "youtube.guideCategories.list", "youtube.i18nLanguages.list",
    "youtube.i18nRegions.list", "youtube.playlistImages.list", "youtube.members.list",
}


def _yt_quota(mid: str) -> int:
    if mid in YT_QUOTA:
        return YT_QUOTA[mid]
    return 1 if mid.endswith(".list") else 50


def ingest_youtube(refresh: bool) -> tuple[Path, dict]:
    provider = "youtube"
    doc = google_discovery("youtube", "v3", refresh)
    granted = {
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    }
    skip = core_route_keys(provider)
    endpoints, gaps, units = [], 0, 0
    for m in google_methods(doc):
        mid = str(m["id"])
        q = _yt_quota(mid)
        cost = {"type": "free", "value": 0.0, "currency": "USD",
                "note": f"{q} quota unit{'s' if q != 1 else ''} per call "
                        f"(default project allowance: 10,000 units/day)"}
        e = google_entry(provider, m, platform="youtube", granted=granted, cost=cost,
                         scope="any_account" if mid in YT_PUBLIC else "own_account",
                         docs_url="https://developers.google.com/youtube/v3/docs")
        if _route_key(e["method"], e["path"]) in skip:
            continue
        gaps += bool(e.get("scope_gap"))
        units += q
        endpoints.append(e)
    notes = [
        "",
        "Source: the youtube v3 Discovery document — the complete Data API. Paths are relative to",
        "base_url (youtube.googleapis.com); the three upload methods correctly point at the",
        "/upload/… URL rather than the metadata one, which is a 400 if you post a body to it.",
        "",
        "COST IS QUOTA, NOT MONEY. Nothing here is billed, but a project gets 10,000 units a day and",
        "the methods are not equal: a list is 1 unit, a write is 50, search.list is 100, captions",
        "cost 200-450 and a video UPLOAD is 1600 — six uploads is more than half a day's allowance.",
        "Each entry's cost.note carries its published unit price (from YouTube's quota-cost table;",
        "the discovery document does not include it).",
        "",
        "scope: `any_account` marks the reads that return the same public data for anyone (videos,",
        "search, channels, comments, playlists) — the connection is just how the call authenticates.",
        "Everything else acts on the connected channel.",
        "",
        "Only 2 scope gaps, both channel memberships (members.list, membershipsLevels.list), which",
        "need youtube.channel-memberships.creator. The `manage` capability's four scopes cover the",
        "entire rest of the Data API — including the content-owner methods, which accept plain",
        "`youtube` as an alternative to youtubepartner and so are NOT gaps, though they are only",
        "meaningful to a CMS partner account.",
    ]
    return write_extended(provider, _oauth_source(
        "google discovery document",
        ["https://youtube.googleapis.com/$discovery/rest?version=v3",
         "https://developers.google.com/youtube/v3/determine_quota_cost"],
    ), endpoints, notes), {"scope_gaps": gaps, "quota_units": units}


# --- google-ads --------------------------------------------------------------------------------
# The Google Ads API has no discovery document or OpenAPI spec worth ingesting: apart from a
# handful of mutate services, ONE endpoint (googleAds:searchStream) answers everything, and what
# varies is the GAQL `FROM` clause. So the unit of coverage here is the RESOURCE, not the route —
# forty entries pointing at the same path, each documenting one thing you can query. That is what
# an agent actually needs: it already knows how to POST a query, it does not know that
# `search_term_view` is where the actual user searches live.
GADS_VERSION = "v25"  # must track OAuthProvider.examples / core google-ads.yaml
GADS_RESOURCES: list[tuple[str, str]] = [
    ("campaign", "Campaigns — name, status, budget, bidding strategy, start/end dates and all campaign-level metrics"),
    ("ad_group", "Ad groups — name, status, type, CPC bid and ad-group-level metrics"),
    ("ad_group_ad", "The individual ads — headlines, descriptions, final URLs, approval status and per-ad metrics"),
    ("ad_group_criterion", "Everything targeted or excluded in an ad group: keywords, audiences, placements, with bids"),
    ("keyword_view", "Per-keyword performance — the metrics view over ad_group_criterion keywords"),
    ("search_term_view", "The actual queries people typed that triggered your ads, and what each cost"),
    ("campaign_budget", "Budgets — daily amount, delivery method, and which campaigns share them"),
    ("campaign_criterion", "Campaign-level targeting and exclusions: locations, languages, devices, negative keywords"),
    ("customer", "The account itself — name, currency, timezone, auto-tagging, and account-level metrics"),
    ("customer_client", "Every account under a manager (MCC), including the hierarchy level and whether it is a manager"),
    ("conversion_action", "Conversion actions — what counts as a conversion, its category, value and counting rules"),
    ("change_event", "Who changed what in the account in the last 30 days, old value and new value"),
    ("change_status", "Which resources changed since a timestamp — the cheap way to poll for edits"),
    ("bidding_strategy", "Portfolio bidding strategies and their targets (target CPA, target ROAS, maximise conversions)"),
    ("billing_setup", "Billing setups and payment accounts attached to the customer"),
    ("account_budget", "Account-level budgets and their approved spending limits (invoiced accounts)"),
    ("asset", "Assets — images, videos, text, sitelinks, callouts — and where each is used"),
    ("asset_group", "Asset groups (Performance Max) with their status and per-group metrics"),
    ("campaign_asset", "Which assets are attached to which campaign, and in what field type"),
    ("customer_asset", "Assets attached at the account level"),
    ("ad_group_ad_asset_view", "Per-asset performance inside a specific ad — which headline actually pulled"),
    ("audience", "Audiences defined in the account and their segments"),
    ("user_list", "Remarketing and customer-match lists, their size and eligibility per network"),
    ("user_interest", "The affinity and in-market interest taxonomy available for targeting"),
    ("campaign_audience_view", "Audience performance at campaign level"),
    ("ad_group_audience_view", "Audience performance at ad-group level"),
    ("age_range_view", "Performance split by the viewer's age bracket"),
    ("gender_view", "Performance split by the viewer's gender"),
    ("parental_status_view", "Performance split by parental status"),
    ("income_range_view", "Performance split by household income bracket"),
    ("geographic_view", "Performance by the location that triggered the ad (physical or interest)"),
    ("user_location_view", "Performance by where the user actually was, ignoring location-of-interest"),
    ("location_view", "Performance per targeted location criterion"),
    ("ad_schedule_view", "Performance by day of week and hour of day"),
    ("detail_placement_view", "Exact URLs and apps your Display/Video ads ran on"),
    ("managed_placement_view", "Performance of placements you chose yourself"),
    ("topic_view", "Performance of Display topic targeting"),
    ("landing_page_view", "Performance grouped by the unexpanded final URL"),
    ("shopping_performance_view", "Shopping metrics broken down by product attributes (brand, category, item id)"),
    ("recommendation", "Google's own optimisation suggestions for the account, with their estimated impact"),
    ("paid_organic_search_term_view", "Paid and organic side by side per query — needs Search Console linked"),
    ("click_view", "Individual clicks with GCLID and location, for the last 90 days"),
]


def ingest_google_ads(refresh: bool) -> tuple[Path, dict]:
    provider = "google-ads"
    skip = core_route_keys(provider)
    path = f"/{GADS_VERSION}/customers/{{customer_id}}/googleAds:searchStream"
    endpoints = []
    for resource, summary in GADS_RESOURCES:
        if _route_key("POST", path) in skip:
            break
        endpoints.append({
            "id": f"{provider}.x.gaql-{resource.replace('_', '-')}",
            "tier": "extended",
            "platform": "google-ads",
            "method": "POST",
            "path": path,
            "summary": f"GAQL FROM {resource} — {summary}",
            "scope": "own_account",
            "cost": {"type": "free", "value": 0.0, "currency": "USD",
                     "note": "no per-call charge; counts against the developer token's daily "
                             "operation limit (Basic access: 15,000 operations/day)"},
            "input": {
                "pathParams": {"customer_id": {
                    "type": "string", "required": True,
                    "note": "the 10-digit Google Ads customer id, no dashes; from listAccessibleCustomers"}},
                "bodyType": "json",
                "body": {"type": "object", "required": True,
                         "note": f'{{"query": "SELECT <fields> FROM {resource} '
                                 f'WHERE segments.date DURING LAST_30_DAYS"}}'},
                "note": f"Resource: {resource}. Its selectable fields, segments and metrics are listed at "
                        f"https://developers.google.com/google-ads/api/fields/{GADS_VERSION}/{resource}. "
                        f"searchStream returns the whole result set in one streamed response and takes no "
                        f"page size; googleAds:search is the paginated twin (curated in the core file).",
            },
            "docs_url": f"https://developers.google.com/google-ads/api/fields/{GADS_VERSION}/{resource}",
        })
    notes = [
        "",
        "NOT A ROUTE LIST — A RESOURCE LIST. The Google Ads API publishes no discovery document or",
        "OpenAPI spec, and it would not help if it did: one endpoint, googleAds:searchStream, answers",
        "every read, and what actually varies is the GAQL `FROM` clause. So each entry below is one",
        f"QUERYABLE RESOURCE pointed at that single path, capped at the {len(GADS_RESOURCES)} an agent would realistically",
        "reach for out of ~180 the API defines. `input.note` names the resource and links its field",
        "reference; `docs_url` goes straight to the selectable fields, segments and metrics.",
        "",
        f"API version {GADS_VERSION}, matching the core file and OAuthProvider.examples. A wrong version 404s as",
        "HTML rather than JSON, so this must be bumped in all three places together.",
        "",
        "Every call needs BOTH the OAuth bearer and a developer-token header from an approved manager",
        "(MCC) account — see OAuthProvider.extra_credential_note. The adwords scope treg requests",
        "covers the whole API, so there are no scope gaps here; the gate is the developer token's",
        "access level (Basic caps the account at 15,000 operations/day).",
    ]
    return write_extended(provider, _oauth_source(
        "official GAQL resource reference (one entry per queryable resource)",
        [f"https://developers.google.com/google-ads/api/fields/{GADS_VERSION}/overview",
         f"https://developers.google.com/google-ads/api/reference/rpc/{GADS_VERSION}/overview"],
    ), endpoints, notes), {}


# --- x ------------------------------------------------------------------------------------------

X_OPENAPI = "https://api.twitter.com/2/openapi.json"
# treg's X app requests these across its read + write capabilities. Anything else is a scope gap.
X_GRANTED = {"tweet.read", "tweet.write", "users.read", "offline.access"}
# Path fragments that make a route act on the CONNECTED account rather than read public data.
X_OWN = re.compile(
    r"/(me|dm_conversations|dm_events|bookmarks|blocking|muting|compliance|usage"
    r"|account_activity|webhooks|reverse_chronological)\b")


# X PRICING IS PAY-PER-USE AND THE BILL LANDS ON TREG's APP, so an extended entry that says "free"
# is not a cosmetic slip — it is a price we published and then charged against.
#
# X publishes a RATE CARD PER RESOURCE TYPE, not one read price and one write price
# (docs.x.com/x-api/getting-started/pricing). Flattening it is how "create a list" ends up billed at
# the post-creation rate — 50% over the real $0.010. The card is transcribed below verbatim so the
# mapping can be audited against it line by line, and every route names the row it was priced from.
#
# OWNED READS ($0.001/resource) DO NOT APPLY TO US. The discount needs the authenticated user to be
# "the owner of the developer app" — on a registry connect the app is treg's and the user is a
# stranger to it, so a connected member reading their own timeline pays the ordinary Posts rate.
# Pricing those routes at $0.001 would have under-billed the calls treg is charged the most for.
X_PRICING_URL = "https://docs.x.com/x-api/getting-started/pricing"
X_PRICE_CHECKED = "2026-08-18"

# (value, cost type, unit) exactly as the card states it. Reads are per RESOURCE RETURNED; the
# entries marked per_call are the card's own per-REQUEST rows (counts and trends sit in the write
# table despite being GETs).
X_RATES = {
    "posts":       (0.005, "per_result", "post"),     # Posts: Read
    "user":        (0.010, "per_result", "user"),     # User: Read / Following-Followers: Read
    "dm_event":    (0.010, "per_result", "result"),   # DM Event: Read
    "list":        (0.005, "per_result", "result"),   # List: Read
    "space":       (0.005, "per_result", "result"),   # Space: Read
    "community":   (0.005, "per_result", "result"),   # Community: Read
    "note":        (0.005, "per_result", "result"),   # Note: Read
    "like":        (0.001, "per_result", "result"),   # Like: Read
    "mute":        (0.001, "per_result", "result"),   # Mute: Read
    "block":       (0.001, "per_result", "result"),   # Block: Read
    "counts_recent": (0.005, "per_call", "call"),     # Counts: Recent
    "counts_all":  (0.010, "per_call", "call"),       # Counts: All
    "trends":      (0.010, "per_call", "call"),       # Trends
    "post_create": (0.015, "per_call", "call"),       # Post: Create ($0.200 with a URL)
    "dm_create":   (0.015, "per_call", "call"),       # DM Interaction: Create
    "interaction": (0.015, "per_call", "call"),       # User Interaction: Create
    "int_delete":  (0.010, "per_call", "call"),       # Interaction: Delete
    "content":     (0.005, "per_call", "call"),       # Content: Manage
    "list_create": (0.010, "per_call", "call"),       # List: Create
    "list_manage": (0.005, "per_call", "call"),       # List: Manage
    "bookmark":    (0.005, "per_call", "call"),       # Bookmark
    "media_meta":  (0.005, "per_call", "call"),       # Media Metadata
    "privacy":     (0.010, "per_call", "call"),       # Privacy: Update
    "mute_delete": (0.005, "per_call", "call"),       # Mute: Delete
}
# The rate a route earns when the card names no row for its resource (webhooks, subscriptions,
# connections, stream rules, media upload, compliance jobs, analytics). It is the figure
# `api._oauth_billed_estimate` falls back to, so the published price still equals the metered one —
# `inferred` says we are quoting treg's fallback, not a rate X published.
X_FALLBACK_READ, X_FALLBACK_WRITE = "posts", "post_create"

# (methods, path regex, rate key, confidence). FIRST MATCH WINS, so the specific rows precede the
# families they sit inside. `documented` means the card names this resource; `inferred` means the
# route's resource type is a judgement call (a route returning the users who liked a post could be
# read as User: Read or Like: Read — it takes the dearer reading, since treg pays the difference).
X_ROUTE_RATES: list[tuple[str, str, str, str]] = [
    # --- writes: the specific actions first -----------------------------------------------------
    ("POST",            r"^/2/lists$",                              "list_create", "documented"),
    ("PUT|DELETE",      r"^/2/lists/\{[^}]+\}$",                    "list_manage", "documented"),
    ("POST|DELETE",     r"^/2/lists/\{[^}]+\}/members",             "list_manage", "documented"),
    ("POST|DELETE",     r"/(followed_lists|pinned_lists)(/|$)",     "list_manage", "documented"),
    ("POST|DELETE",     r"/bookmarks(/|$)",                         "bookmark",    "documented"),
    ("POST|DELETE",     r"^/2/media/(metadata|subtitles)$",          "media_meta",  "documented"),
    ("DELETE",          r"/muting/",                                "mute_delete", "documented"),
    ("DELETE",          r"/(likes|retweets|following)/",            "int_delete",  "documented"),
    ("POST",            r"/(likes|retweets|following|muting)$",     "interaction", "documented"),
    ("POST",            r"^/2/users/\{[^}]+\}/dm/(un)?block$",       "privacy",     "inferred"),
    ("POST",            r"^/2/dm_conversations(/|$)|^/2/chat/conversations/\{[^}]+\}/messages",
                                                                    "dm_create",   "documented"),
    ("POST",            r"^/2/chat/conversations",                  "dm_create",   "inferred"),
    ("DELETE",          r"^/2/dm_events/",                          "int_delete",  "inferred"),
    ("DELETE",          r"^/2/tweets/\{[^}]+\}$",                   "int_delete",  "inferred"),
    ("PUT",             r"^/2/tweets/\{[^}]+\}/hidden$",            "content",     "documented"),
    ("POST|DELETE",     r"^/2/(notes|articles|broadcasts)",          "content",     "inferred"),
    # --- reads: per-resource rows ---------------------------------------------------------------
    ("GET",             r"^/2/tweets/counts/recent$",               "counts_recent", "documented"),
    ("GET",             r"^/2/tweets/counts/all$",                  "counts_all",  "documented"),
    ("GET",             r"^/2/trends/",                             "trends",      "documented"),
    ("GET",             r"^/2/users/personalized_trends$",          "trends",      "inferred"),
    ("GET",             r"^/2/likes/",                              "like",        "documented"),
    ("GET",             r"/muting$",                                "mute",        "documented"),
    ("GET",             r"/blocking$",                              "block",       "documented"),
    ("GET",             r"^/2/(dm_events|dm_conversations)|^/2/chat/conversations/\{[^}]+\}/events$",
                                                                    "dm_event",    "documented"),
    ("GET",             r"^/2/communities/",                        "community",   "documented"),
    ("GET",             r"^/2/spaces(/|$)(?!.*\{[^}]+\}/(tweets|buyers))", "space", "documented"),
    ("GET",             r"^/2/notes/search/",                       "note",        "documented"),
    ("GET",             r"^/2/lists/\{[^}]+\}$",                    "list",        "documented"),
    ("GET",             r"/(owned_lists|followed_lists|list_memberships|pinned_lists)$",
                                                                    "list",        "documented"),
    ("GET",             r"^/2/lists/\{[^}]+\}/(followers|members)$",  "user",        "inferred"),
    ("GET",             r"/(followers|following)$",                 "user",        "documented"),
    ("GET",             r"/(liking_users|retweeted_by|buyers|members|affiliates)$",
                                                                    "user",        "inferred"),
    ("GET",             r"^/2/users(/by|/search)?$|^/2/users/\{[^}]+\}$", "user",   "documented"),
    ("GET",             r"^/2/tweets(/|$)(?!.*\b(analytics|label|compliance|webhooks|rules)\b)",
                                                                    "posts",       "documented"),
    ("GET",             r"/(liked_tweets|mentions|bookmarks|quote_tweets|retweets|reposts_of_me"
                        r"|notes_written|posts_eligible_for_notes|reverse_chronological)$",
                                                                    "posts",       "documented"),
    ("GET",             r"^/2/(lists|spaces)/\{[^}]+\}/tweets$",     "posts",       "documented"),
    ("GET",             r"^/2/news/",                               "note",        "inferred"),
]
_X_COMPILED = [(set(ms.split("|")), re.compile(rx), key, conf) for ms, rx, key, conf in X_ROUTE_RATES]


def _x_cost(method: str, path: str, scope: str = "") -> dict:
    """The rate `api._oauth_billed_estimate` will actually charge this route, written down.

    Never returns `free`: nothing on X's v2 API is free to treg any more, and a `free` block here
    both misleads the catalog reader and is skipped by the meter (its `usd` is 0, which is falsy),
    so the two numbers silently disagree — the display says $0 and the balance says otherwise."""
    key = conf = None
    for methods, rx, k, c in _X_COMPILED:
        if method in methods and rx.search(path):
            key, conf = k, c
            break
    if key is None:
        key = X_FALLBACK_READ if method == "GET" else X_FALLBACK_WRITE
        conf = "inferred"
    value, ctype, unit = X_RATES[key]
    note = (f"X's rate card, {key.replace('_', ' ')}: ${value:g} "
            f"{'per resource returned' if ctype == 'per_result' else 'per request'}")
    if conf == "inferred":
        note = (f"X publishes no rate for this route's resource, so treg meters it at ${value:g} "
                f"{'per resource' if ctype == 'per_result' else 'per request'} — the same figure "
                f"the proxy falls back to") if key in (X_FALLBACK_READ, X_FALLBACK_WRITE) else \
               (f"{note}; which row applies is a judgement call, and this takes the dearer reading")
    if key == "post_create" and conf == "documented":
        note += " — $0.200 when the text carries a URL, which the proxy prices at the higher rate"
    return {"type": ctype, "value": value, "currency": "USD", "per": 1, "unit": unit,
            "source": "docs", "source_url": X_PRICING_URL, "checked": X_PRICE_CHECKED,
            "confidence": conf, "note": note}


def _x_scopes(op: dict) -> list[str]:
    for sec in op.get("security") or []:
        for name, scopes in sec.items():
            if name.startswith("OAuth2User") and scopes:
                return list(scopes)
    return []


def ingest_x(refresh: bool) -> tuple[Path, dict]:
    provider = "x"
    spec = json.loads(fetch(X_OPENAPI, "x_openapi.json", refresh=refresh, headers=_JSON_HDR))
    skip = core_route_keys(provider)
    endpoints, gaps = [], 0
    for path, item in sorted((spec.get("paths") or {}).items()):
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            method = method.upper()
            if _route_key(method, path) in skip:
                continue
            opid = str(op.get("operationId") or "")
            slug = re.sub(r"(?<!^)(?=[A-Z])", "-", opid).lower() if opid else \
                re.sub(r"[^a-z0-9]+", "-", f"{method}-{path}".lower()).strip("-")
            summary = clean(op.get("summary") or "") or clean(op.get("description") or "") or opid
            scope = "own_account" if (method != "GET" or X_OWN.search(path)) else "any_account"
            entry: dict = {
                "id": f"{provider}.x.{re.sub(r'[^a-z0-9]+', '-', slug).strip('-')}",
                "tier": "extended",
                "platform": "x",
                "method": method,
                "path": path,
                "summary": summary[:400],
                "scope": scope,
                "cost": _x_cost(method, path, scope),
                "docs_url": "https://docs.x.com/x-api",
            }
            needed = _x_scopes(op)
            if needed and not set(needed) <= X_GRANTED:
                gaps += 1
                entry["scope_gap"] = ("treg's OAuth app does not request this; needs: "
                                      + ", ".join(sorted(set(needed) - X_GRANTED)))
            params = {}
            for p in op.get("parameters") or []:
                name = p.get("name")
                if not name:
                    continue
                e: dict = {"type": (p.get("schema") or {}).get("type", "string"),
                           "required": bool(p.get("required"))}
                note = clean(p.get("description") or "")
                if note:
                    e["note"] = note[:300]
                params.setdefault("path" if p.get("in") == "path" else "query", {})[name] = e
            inp: dict = {}
            if params.get("path"):
                inp["pathParams"] = params["path"]
            if params.get("query"):
                inp["queryParams"] = params["query"]
            if op.get("requestBody"):
                inp["bodyType"] = "json"
                inp["body"] = {"type": "object", "required": bool((op["requestBody"] or {}).get("required")),
                               "note": "JSON body; see the operation's reference for its fields"}
            if inp:
                entry["input"] = inp
            endpoints.append(entry)
    notes = [
        "",
        "Source: X's own OpenAPI document for the v2 API (api.twitter.com/2/openapi.json — the",
        "api.x.com host serves the same API). Its declared server is https://api.x.com, which is",
        "exactly OAuthProvider.base_url, so every path here is already relative to it.",
        "",
        f"SCOPE GAPS ARE THE STORY. treg's X app requests only {', '.join(sorted(X_GRANTED))};",
        f"{gaps} of these routes need something else — likes, follows, lists, spaces, bookmarks, mutes,",
        "blocks, DMs and media upload each have their own scope. They are listed with `scope_gap:`",
        "rather than omitted, because that list IS the decision of which scopes to add to the app.",
        "",
        "Beyond scopes, a route may still answer 403 on a project whose access the spec cannot",
        "express — the OpenAPI document lists the whole surface, not what one app may reach.",
        "",
        "EVERY ENTRY IS PRICED, AND NONE OF THEM ARE FREE. X bills the APP OWNER per use (prepaid",
        "credits, no plan tiers since Feb 2026), so a call on a registry connect spends treg's money",
        "and is metered from the team balance. Reads are per resource RETURNED — posts $0.005, users",
        "$0.010 — an own-account read is $0.001, and a write is $0.015 per request ($0.20 when a",
        "post's text carries a URL). The rates are documented; which one a route earns is read off",
        "the resource it returns, so routes returning a resource type X publishes no rate for are",
        "marked `confidence: inferred` at the post-read rate — the same figure the proxy's fallback",
        "charges, so the published price and the metered price cannot drift apart.",
        "",
        "scope: GET routes that read public data are `any_account`; writes and anything touching",
        "/me, DMs, bookmarks, blocks, mutes, compliance or usage are `own_account`.",
    ]
    return write_extended(provider, _oauth_source(
        "official openapi", [X_OPENAPI],
    ), endpoints, notes), {"scope_gaps": gaps}


# --- Meta Graph API: facebook / instagram / meta-ads --------------------------------------------
# Meta publishes NO machine-readable spec for the Graph API — no OpenAPI, no discovery document,
# nothing versioned we could diff. The only source is the HTML reference index, one page per node
# type. Transcribing hundreds of routes from HTML is precisely what catalog.md step 1 warns
# against ("that is how typos ship"), so these three files are deliberately MODEST and
# hand-curated: the edges of the node types treg's scopes can actually reach, each one read off
# the official reference page for that node. Accuracy over volume.
#
# Every path is relative to base_url, which already ends in /v25.0 — do NOT prefix the version.
# Tuple: (slug, method, path, summary, scope_gap or "")

META_GRAPH_DOCS = "https://developers.facebook.com/docs/graph-api/reference"

FACEBOOK_EDGES: list[tuple[str, str, str, str, str]] = [
    ("page", "GET", "/{page_id}",
     "The Page itself — name, category, about, fan_count, link, verification status, hours", ""),
    ("page-feed", "GET", "/{page_id}/feed",
     "The Page's feed: its own posts plus visitor posts, with message, created_time and attachments", ""),
    ("page-published-posts", "GET", "/{page_id}/published_posts",
     "Only the posts the Page itself published, including ones published via the API", ""),
    ("page-tagged", "GET", "/{page_id}/tagged",
     "Posts by other people that tagged this Page", ""),
    ("page-photos", "GET", "/{page_id}/photos",
     "Photos uploaded to the Page, with images, alt text and created_time", ""),
    ("page-albums", "GET", "/{page_id}/albums",
     "The Page's photo albums and their counts", ""),
    ("page-videos", "GET", "/{page_id}/videos",
     "Videos on the Page, with title, description, length and permalink", ""),
    ("page-live-videos", "GET", "/{page_id}/live_videos",
     "Live broadcasts on the Page, past and current, with status and viewer counts", ""),
    ("page-events", "GET", "/{page_id}/events",
     "Events the Page has created, with time, place and attendance counts", ""),
    ("page-ratings", "GET", "/{page_id}/ratings",
     "Recommendations and reviews left on the Page, with rating, review text and reviewer", ""),
    ("page-settings", "GET", "/{page_id}/settings",
     "The Page's own settings — messaging, tagging and visitor-post permissions", ""),
    ("page-roles", "GET", "/{page_id}/roles",
     "Who administers the Page and at what role level", "needs pages_manage_metadata"),
    ("page-conversations", "GET", "/{page_id}/conversations",
     "Messenger threads with the Page, with participants and updated_time",
     "needs pages_messaging and pages_read_user_content"),
    ("page-leadgen-forms", "GET", "/{page_id}/leadgen_forms",
     "Lead-ad forms attached to the Page, and their fields", "needs leads_retrieval"),
    ("page-instagram-accounts", "GET", "/{page_id}?fields=instagram_business_account",
     "The Instagram professional account linked to this Page — the id every Instagram call needs", ""),
    ("post", "GET", "/{post_id}",
     "A single post — message, created_time, permalink_url, attachments and privacy", ""),
    ("post-comments", "GET", "/{post_id}/comments",
     "Comments on a post, with message, from, like_count and comment_count", ""),
    ("post-likes", "GET", "/{post_id}/likes",
     "Who liked a post (summary=true gives just the total)", ""),
    ("post-reactions", "GET", "/{post_id}/reactions",
     "Reactions on a post broken down by type (LIKE, LOVE, WOW, ANGRY…)", ""),
    ("post-sharedposts", "GET", "/{post_id}/sharedposts",
     "The posts that shared this post", ""),
    ("post-insights", "GET", "/{post_id}/insights",
     "Per-post metrics — impressions, reach, engaged users, clicks, negative feedback", ""),
    ("post-update", "POST", "/{post_id}",
     "Edit a published post's message", ""),
    ("post-delete", "DELETE", "/{post_id}",
     "Delete one of the Page's posts", ""),
    ("comment-reply", "POST", "/{comment_id}/comments",
     "Reply to a comment as the Page", "needs pages_manage_engagement"),
    ("comment-delete", "DELETE", "/{comment_id}",
     "Delete a comment on the Page's content", "needs pages_manage_engagement"),
    ("comment-hide", "POST", "/{comment_id}?is_hidden=true",
     "Hide a comment on the Page's content", "needs pages_manage_engagement"),
]

INSTAGRAM_EDGES: list[tuple[str, str, str, str, str]] = [
    ("user-stories", "GET", "/{ig_user_id}/stories",
     "The account's stories from the last 24 hours", ""),
    ("user-live-media", "GET", "/{ig_user_id}/live_media",
     "The account's currently running live broadcasts", ""),
    ("user-tags", "GET", "/{ig_user_id}/tags",
     "Media where another account tagged this one in the photo", ""),
    ("user-mentioned-media", "GET", "/{ig_user_id}/mentioned_media",
     "Media whose caption @-mentions this account", ""),
    ("user-mentioned-comment", "GET", "/{ig_user_id}/mentioned_comment",
     "A comment that @-mentions this account, with its thread", ""),
    ("user-content-publishing-limit", "GET", "/{ig_user_id}/content_publishing_limit",
     "How many of the 24-hour posting quota (50 posts) this account has already used", ""),
    ("user-business-discovery", "GET", "/{ig_user_id}?fields=business_discovery.username({username})",
     "Read ANOTHER public professional account's followers, media count and recent posts", ""),
    ("user-recently-searched-hashtags", "GET", "/{ig_user_id}/recently_searched_hashtags",
     "The hashtags this account has looked up recently (the 30-per-week search quota)", ""),
    ("hashtag-search", "GET", "/ig_hashtag_search",
     "Resolve a hashtag name to its id — the first call of any hashtag lookup", ""),
    ("hashtag", "GET", "/{ig_hashtag_id}",
     "A hashtag's id and name", ""),
    ("hashtag-top-media", "GET", "/{ig_hashtag_id}/top_media",
     "The most popular public posts carrying a hashtag", ""),
    ("hashtag-recent-media", "GET", "/{ig_hashtag_id}/recent_media",
     "The most recent public posts carrying a hashtag", ""),
    ("media", "GET", "/{ig_media_id}",
     "A single post — caption, media_type, media_url, permalink, timestamp, like and comment counts", ""),
    ("media-children", "GET", "/{ig_media_id}/children",
     "The individual images or videos inside a carousel post", ""),
    ("comment", "GET", "/{ig_comment_id}",
     "A single comment — text, username, timestamp and like count", ""),
    ("comment-replies", "GET", "/{ig_comment_id}/replies",
     "The replies under a comment", ""),
    ("comment-reply-create", "POST", "/{ig_comment_id}/replies",
     "Reply to a comment as the account", "needs instagram_manage_comments"),
    ("comment-hide", "POST", "/{ig_comment_id}?hide=true",
     "Hide a comment on the account's own media", "needs instagram_manage_comments"),
    ("comment-delete", "DELETE", "/{ig_comment_id}",
     "Delete a comment on the account's own media", "needs instagram_manage_comments"),
    ("media-comment-create", "POST", "/{ig_media_id}/comments",
     "Comment on the account's own media", "needs instagram_manage_comments"),
    ("user-messages", "GET", "/{ig_user_id}/conversations",
     "Instagram Direct threads with the account", "needs instagram_manage_messages"),
    ("user-product-appeal", "GET", "/{ig_user_id}/product_appeal",
     "The status of appeals against rejected shopping products", "needs instagram_shopping_tag_products"),
    ("catalog-product-search", "GET", "/{ig_user_id}/catalog_product_search",
     "Search the linked commerce catalog for products to tag in a post",
     "needs instagram_shopping_tag_products"),
]

META_ADS_EDGES: list[tuple[str, str, str, str, str]] = [
    ("me-businesses", "GET", "/me/businesses",
     "The Business Manager accounts this login belongs to", ""),
    ("business-owned-ad-accounts", "GET", "/{business_id}/owned_ad_accounts",
     "The ad accounts a Business Manager owns", ""),
    ("account", "GET", "/{ad_account_id}",
     "The ad account itself — name, currency, timezone, spend cap, amount_spent, account_status", ""),
    ("account-adsets", "GET", "/{ad_account_id}/adsets",
     "Every ad set in the account with budget, schedule, optimisation goal and targeting", ""),
    ("account-ads", "GET", "/{ad_account_id}/ads",
     "Every ad in the account with its creative, status and effective review status", ""),
    ("account-adcreatives", "GET", "/{ad_account_id}/adcreatives",
     "The creatives — body, title, image/video, link and call-to-action", ""),
    ("account-adimages", "GET", "/{ad_account_id}/adimages",
     "Images uploaded to the ad account, with hash, dimensions and permalink", ""),
    ("account-advideos", "GET", "/{ad_account_id}/advideos",
     "Videos uploaded to the ad account", ""),
    ("account-customaudiences", "GET", "/{ad_account_id}/customaudiences",
     "Custom and lookalike audiences with approximate size and delivery status", ""),
    ("account-customconversions", "GET", "/{ad_account_id}/customconversions",
     "Custom conversions defined on the account and their rules", ""),
    ("account-adspixels", "GET", "/{ad_account_id}/adspixels",
     "Meta pixels on the account and when each last fired", ""),
    ("account-adlabels", "GET", "/{ad_account_id}/adlabels",
     "Labels used to group campaigns, ad sets and ads", ""),
    ("account-adrules-library", "GET", "/{ad_account_id}/adrules_library",
     "Automated rules on the account — their conditions and actions", ""),
    ("account-activities", "GET", "/{ad_account_id}/activities",
     "The change log: who changed what on the account and when", ""),
    ("account-delivery-estimate", "GET", "/{ad_account_id}/delivery_estimate",
     "Estimated daily reach and bid range for a targeting spec, before you spend anything", ""),
    ("account-reachestimate", "GET", "/{ad_account_id}/reachestimate",
     "Estimated audience size for a targeting spec", ""),
    ("account-targetingsearch", "GET", "/{ad_account_id}/targetingsearch",
     "Search Meta's targeting taxonomy (interests, behaviours, demographics) by keyword", ""),
    ("account-targetingbrowse", "GET", "/{ad_account_id}/targetingbrowse",
     "Browse the targeting taxonomy as a tree", ""),
    ("account-targetingsuggestions", "GET", "/{ad_account_id}/targetingsuggestions",
     "Interests related to ones you already target", ""),
    ("campaign", "GET", "/{campaign_id}",
     "A single campaign — objective, status, budget, bid strategy and spend cap", ""),
    ("campaign-adsets", "GET", "/{campaign_id}/adsets",
     "The ad sets inside one campaign", ""),
    ("campaign-ads", "GET", "/{campaign_id}/ads",
     "The ads inside one campaign", ""),
    ("adset", "GET", "/{adset_id}",
     "A single ad set — targeting, schedule, budget, optimisation goal and billing event", ""),
    ("adset-ads", "GET", "/{adset_id}/ads",
     "The ads inside one ad set", ""),
    ("ad", "GET", "/{ad_id}",
     "A single ad — its creative, status, tracking specs and review status", ""),
    ("ad-previews", "GET", "/{ad_id}/previews",
     "A rendered preview of an ad in a given placement, as an iframe", ""),
    ("ad-leads", "GET", "/{ad_id}/leads",
     "The leads a lead ad has collected", "needs leads_retrieval"),
    ("campaign-create", "POST", "/{ad_account_id}/campaigns",
     "Create a campaign (objective, status, special ad categories)", "needs ads_management"),
    ("adset-create", "POST", "/{ad_account_id}/adsets",
     "Create an ad set (targeting, budget, schedule, optimisation goal)", "needs ads_management"),
    ("ad-create", "POST", "/{ad_account_id}/ads",
     "Create an ad from a creative inside an ad set", "needs ads_management"),
    ("adcreative-create", "POST", "/{ad_account_id}/adcreatives",
     "Create an ad creative", "needs ads_management"),
    ("campaign-update", "POST", "/{campaign_id}",
     "Update a campaign — pause it, rename it, change its budget", "needs ads_management"),
    ("adset-update", "POST", "/{adset_id}",
     "Update an ad set — pause it, change budget, schedule or targeting", "needs ads_management"),
    ("ad-update", "POST", "/{ad_id}",
     "Update an ad — pause it or swap its creative", "needs ads_management"),
]

META_PROVIDERS = {
    "facebook": ("facebook", FACEBOOK_EDGES, "pages_show_list, pages_read_engagement, read_insights"
                                             " (+ pages_manage_posts for the `post` capability)",
                 f"{META_GRAPH_DOCS}/page/"),
    "instagram": ("instagram", INSTAGRAM_EDGES,
                  "instagram_basic, instagram_manage_insights, pages_show_list, pages_read_engagement"
                  " (+ instagram_content_publish for the `post` capability)",
                  "https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference"),
    "meta-ads": ("meta-ads", META_ADS_EDGES, "ads_read, business_management"
                                             " (+ ads_management for the `manage` capability)",
                 "https://developers.facebook.com/docs/marketing-api/reference"),
}


def ingest_meta(service: str, refresh: bool) -> tuple[Path, dict]:
    platform, edges, granted, docs = META_PROVIDERS[service]
    # EXACT (method, path) dedup here, not the normalised one the Google ingesters use. On Graph the
    # node id IS the first path segment, so `/{post_id}/insights` and `/{page_id}/insights` differ
    # only by the placeholder NAME while being genuinely different endpoints — normalising would
    # silently drop post insights because the core file curates page insights. The paths below are
    # hand-written against the core files, so exact matching is enough.
    skip = core_routes(service)
    endpoints, gaps = [], 0
    for slug, method, path, summary, gap in edges:
        if _route_key(method, path) in skip:
            continue
        entry: dict = {
            "id": f"{service}.x.{slug}", "tier": "extended", "platform": platform,
            "method": method, "path": path, "summary": summary, "scope": "own_account",
            "cost": {"type": "free", "value": 0.0, "currency": "USD",
                     "note": "no per-call charge; counted against the app's Graph API rate limit"},
            "docs_url": docs,
        }
        if gap:
            gaps += 1
            entry["scope_gap"] = f"treg's OAuth app does not request this; {gap}"
        endpoints.append(entry)
    notes = [
        "",
        "META PUBLISHES NO SPEC. There is no OpenAPI document, no discovery document and no",
        "machine-readable index for the Graph API — the only source is the HTML reference, one page",
        "per node type. Hand-transcribing hundreds of routes out of HTML is how typos ship, so this",
        f"file is deliberately SHORT: {len(endpoints)} entries, each an edge of a node type this provider's scopes",
        "can actually reach, read off the official reference page linked in every docs_url. Accuracy",
        "over volume — assume the Graph surface is much larger than what is listed here.",
        "",
        "Paths are relative to base_url, which ALREADY ENDS IN /v25.0 — no entry repeats the version.",
        "Bumping the Graph version is a base_url change in oauth_providers.py, not an edit here.",
        "",
        f"treg's app requests {granted}.",
        f"{gaps} entr{'ies' if gaps != 1 else 'y'} need a scope beyond that and carry `scope_gap:`. Meta gates most of those behind",
        "App Review with a screencast per permission, so a scope gap here is a product decision, not",
        "a config change.",
        "",
        "Graph ids are opaque and account-specific ({page_id}, {ad_account_id} — the latter includes",
        "its `act_` prefix), so nothing here carries a test_request; `treg connections resources`",
        "supplies the id for the connected account.",
    ]
    return write_extended(service, _oauth_source(
        "hand-curated from the official Graph API HTML reference (Meta publishes no machine-readable spec)",
        [docs, META_GRAPH_DOCS],
    ), endpoints, notes), {"scope_gaps": gaps}


INGESTERS.update({
    "google-search-console": ingest_google_search_console,
    "google-analytics": ingest_google_analytics,
    "google-business-profile": ingest_google_business_profile,
    "youtube": ingest_youtube,
    "google-ads": ingest_google_ads,
    "x": ingest_x,
    "facebook": lambda refresh: ingest_meta("facebook", refresh),
    "instagram": lambda refresh: ingest_meta("instagram", refresh),
    "meta-ads": lambda refresh: ingest_meta("meta-ads", refresh),
})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("providers", nargs="+", choices=[*INGESTERS, "all"])
    ap.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")
    args = ap.parse_args(argv)

    names = list(INGESTERS) if "all" in args.providers else args.providers
    platforms = set(yaml.safe_load((CATALOG / "capabilities.yaml").read_text())["platforms"])
    rc = 0
    for name in names:
        print(f"{name}:", file=sys.stderr)
        path, _ = INGESTERS[name](args.refresh)
        data = yaml.safe_load(path.read_text())
        used = {}
        for ep in data["endpoints"]:
            used[ep["platform"]] = used.get(ep["platform"], 0) + 1
        unknown = sorted(set(used) - platforms)
        print(f"  {len(data['endpoints'])} endpoint(s) → {path.relative_to(ROOT)}")
        print("  platforms: " + ", ".join(f"{k} ({v})" for k, v in sorted(used.items(), key=lambda kv: -kv[1])))
        if unknown:
            rc = 1
            print(f"  MISSING from capabilities.yaml platforms: {', '.join(unknown)}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
