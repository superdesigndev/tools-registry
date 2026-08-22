"""End-to-end verification of failure capture against a REAL provider.

Deliberately hits real upstreams on treg's real platform keys, because the whole point is that the
provider's own error body survives redaction and lands in the column. Spend is a few tenths of a
cent. The platform credential is asserted against but NEVER printed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18795"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e.db")
ADMIN = "E2E-ADMIN-TOKEN"

OK, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"  {OK if ok else FAIL} {label}" + (f"\n      {detail}" if detail else ""))
    return ok


def req(method: str, path: str, token: str | None = None, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, method=method, data=data)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Treg-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def newest_row(endpoint_id: str | None = None, tries: int = 40) -> dict:
    """Read the audit row straight from the DB — /calls deliberately does not expose these columns.

    Polls, because audit writes are fire-and-forget: the HTTP response returns before the row is
    committed, so reading immediately races and can see the PREVIOUS call. Waiting for the expected
    endpoint_id is what makes each assertion actually about the call just made.
    """
    for _ in range(tries):
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute(
                "select endpoint_id, credential_tier, status_code, refused_by, error_request,"
                " error_response from callrecord order by id desc limit 1").fetchone()
        finally:
            con.close()
        if r and (endpoint_id is None or r["endpoint_id"] == endpoint_id):
            return dict(r)
        time.sleep(0.25)
    return dict(r) if r else {}


def platform_keys() -> list[str]:
    """Every real platform key, read from .env — for ABSENCE assertions only. Never printed."""
    out = []
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, ".env")) as fh:
        for line in fh:
            if line.startswith("TREG_PLATFORM_KEY_") and "=" in line:
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 8:
                    out.append(v)
    return out


def main() -> int:
    keys = platform_keys()
    print(f"\n\033[1me2e: failure capture\033[0m  ({len(keys)} real platform keys loaded for leak checks)\n")

    # A fresh identity per run: the database persists between runs, and a re-registration 409s.
    email = f"e2e-{os.urandom(4).hex()}@treg.local"
    status, body = req("POST", "/users", body={"email": email})
    tok = json.loads(body).get("token") if status == 200 else None
    if not tok:
        print("could not create user:", status, body[:300])
        return 1
    org = json.loads(req("GET", "/orgs", tok)[1])[0]
    bal = json.loads(req("GET", f"/orgs/{org['org_id']}/balance", tok)[1])["balance_micro"]
    print(f"  org {org['slug']!r}  balance ${bal/1e6:.2f}\n")
    if bal <= 0:
        print("  no balance — tier 4 cannot be reached; aborting")
        return 1

    # ---- 1. a SUCCESS must store nothing --------------------------------------------------------
    # hunter, not tikhub: tikhub sits behind Cloudflare, which 403s this machine's IP. That block is
    # a genuine provider failure and is used as such in step 2 — it just cannot serve as the success.
    print("\033[1m1. success stores no evidence\033[0m")
    s, _ = req("GET", "/call/hunter.companies.enrich?domain=stripe.com", tok)
    row = newest_row("hunter.companies.enrich")
    check(s == 200, f"upstream returned 200 (got {s})")
    check(row.get("credential_tier") == "platform", f"served on the platform key (got {row.get('credential_tier')})")
    check(row.get("error_request") is None and row.get("error_response") is None,
          "both evidence columns are NULL on a success")

    # ---- 2. a real PROVIDER FAILURE must be explained -------------------------------------------
    print("\n\033[1m2. a real provider failure keeps its message\033[0m")
    attempts = [
        ("spyfu.google.domain.ranked_keywords", "?domain=", "spyfu, empty required param"),
        ("serpapi.x.bing-search", "?q=", "serpapi, empty query"),
        ("tikhub.x.twitter-web-fetch-search-timeline", "?keyword=", "tikhub, empty keyword"),
    ]
    captured = None
    for ep, qs, label in attempts:
        s, _ = req("GET", f"/call/{ep}{qs}", tok)
        row = newest_row(ep)
        if s >= 400 and row.get("error_response") and row.get("refused_by") is None:
            captured = (label, s, row)
            break
        print(f"      (tried {label}: status {s}, refused_by={row.get('refused_by')})")
    if captured:
        label, s, row = captured
        check(True, f"provider failure captured via {label} (status {s})")
        check(len(row["error_response"]) <= 2001,
              f"truncated to the cap ({len(row['error_response'])} chars, limit 2000 + marker)")
        check("�" not in row["error_response"],
              "decoded cleanly — no replacement characters")
        print(f"      \033[2msaid:\033[0m {row['error_response'][:150]}")
        print(f"      \033[2msent:\033[0m {row['error_request'][:120]}")
    else:
        check(False, "no provider failure could be produced", "every attempt was refused by treg first")

    # ---- 3. the platform credential must appear nowhere -----------------------------------------
    print("\n\033[1m3. no platform credential in any captured row\033[0m")
    con = sqlite3.connect(DB)
    try:
        blob = "\n".join(
            (a or "") + "\n" + (b or "") for a, b in
            con.execute("select error_request, error_response from callrecord").fetchall())
    finally:
        con.close()
    leaked = [k for k in keys if k in blob]
    check(not leaked, f"none of the {len(keys)} real platform keys appear in the evidence columns",
          "" if not leaked else f"LEAKED {len(leaked)} key(s) — do not print, inspect e2e.db")
    check(len(blob) > 0, "there was captured evidence to check (not a vacuous pass)")

    # ---- 4. the admin route ---------------------------------------------------------------------
    print("\n\033[1m4. /admin/errors\033[0m")
    s, body = req("GET", "/admin/errors", ADMIN)
    d = json.loads(body) if s == 200 else {}
    check(s == 200, f"route answers (got {s})")
    check(d.get("retention_days") == 14, f"retention window reported ({d.get('retention_days')}d)")
    errs = d.get("errors", [])
    check(all(e["status"] >= 400 for e in errs) if errs else False,
          f"lists only failures ({len(errs)} rows)")

    # ---- 5. the team's own feed must NOT carry it -----------------------------------------------
    print("\n\033[1m5. the customer feed is unchanged\033[0m")
    _, calls = req("GET", "/calls", tok)
    check("error_response" not in calls, "/calls does not expose the evidence columns")

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n\033[1m{passed}/{len(results)} checks passed\033[0m\n")
    return 0 if passed == len(results) else 1


sys.exit(main())
