#!/usr/bin/env python3
"""What every org actually called, what it cost, and what failed — straight from the prod DB.

    uv run --frozen python scripts/usage_report.py [--days 1] [--top 25] [--html PATH] [--json]

Run it daily. It prints a summary to stdout and writes a self-contained HTML dashboard.

WHY THIS READS THE DATABASE AND NOT THE ADMIN API
-------------------------------------------------
`/admin/calls` caps at 1,000 rows with no paging — about five hours at current volume — so a daily
job against it would silently miss most of the day. The three `/admin/reconcile/*` reports aggregate
over 30 days server-side but only answer their own three questions, and none of them can group by
endpoint for a provider that doesn't report its own cost. Two columns this report is built on,
`credential_tier` and `refused_by`, are exposed by NO admin route at all: `/orgs/{id}/calls` carries
them but is scoped to the caller's own org. So the database is the only source that can answer
"what is everyone doing", and this script is the sanctioned way to ask.

WHAT IT DOES TO PRODUCTION
--------------------------
The prod Postgres keeps an EMPTY `ipAllowList`, which drops every external connection. This script
opens a hole for this machine's /32, reads, and closes it again in a `finally` — then re-reads the
resource to PROVE it closed. If you see "allowlist NOT closed" in the output, close it by hand
(Render dashboard -> treg-db -> Access Control) before doing anything else. Nothing here writes:
every statement is a SELECT, and the connection is opened read-only.

It deliberately uses RAW SQL rather than the ORM. Importing `treg.models` binds the query to
whatever branch is checked out, and a branch carrying an unmigrated column makes every SELECT fail
against prod with `UndefinedColumnError` — a trap that has cost real time before. Raw SQL against
named columns is immune, so this script runs correctly from any branch.

DEFINITIONS (they are not interchangeable, and conflating them is how "our error rate" gets misread)
----------------------------------------------------------------------------------------------------
  refused    `refused_by IS NOT NULL` — TREG said no before a byte went upstream: no balance, a daily
             cap, a bad token, an unknown endpoint, a malformed request. Costs nothing and is not a
             provider failure. `balance` refusals are the paywall, i.e. demand we didn't serve.
  failed     `status_code >= 400 AND refused_by IS NULL` — the call went upstream and the PROVIDER
             answered badly. This is the number that means something is broken.
  ok         everything else.
  spend      `SUM(cost_charged_micro)` — what actually hit the org's balance. NOT the estimate:
             a released or refunded call still carries an estimate and would over-report spend.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_ID = os.environ.get("TREG_PROD_DB_ID", "dpg-d94fp3d7vvec73di5rqg-a")
RENDER_API = "https://api.render.com/v1"


# ---- environment -------------------------------------------------------------------------------
def env(key: str) -> str:
    """Read `key` from the process env, falling back to the repo's .env (same file the deploy uses)."""
    if os.environ.get(key):
        return os.environ[key]
    envfile = REPO / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{key} is not set (looked in the environment and {envfile})")


def render_api(method: str, path: str, body: dict | None = None):
    """One Render API call. PATCH, never PUT — PUT answers with a non-JSON body on this resource."""
    req = urllib.request.Request(
        RENDER_API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + env("RENDER_API_KEY"),
                 "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else None


def my_ip() -> str:
    return urllib.request.urlopen("https://api.ipify.org", timeout=20).read().decode().strip()


# ---- the queries -------------------------------------------------------------------------------
# One CTE defines the window and the three outcome classes once, so every panel below counts the
# same way. `endpoint_id` is NULL for a call to a team's own registered tool; `tool_name` names it,
# so the report coalesces to keep own-tool traffic visible instead of bucketing it all as "unknown".
BASE = """
with c as (
  select
    coalesce(endpoint_id, tool_name)              as ep,
    coalesce(provider, split_part(tool_name,'.',1)) as prov,
    -- The split that decides which of the two endpoint tables a row lands in. A non-NULL
    -- endpoint_id means the call RESOLVED to a catalog endpoint; NULL means it was either a tool
    -- the team registered themselves or a shape treg could not resolve at all.
    (endpoint_id is not null)                     as cataloged,
    id, method, org_id, user_email, status_code, refused_by, credential_tier, path,
    {evidence_cols}
    nullif(client,'')                             as client,
    coalesce(cost_charged_micro,0)                as spend,
    duration_ms, created_at,
    (refused_by is not null)                                  as refused,
    (status_code >= 400 and refused_by is null)               as failed,
    (status_code < 400)                                       as ok
  from callrecord
  where created_at >= $1
)
"""

QUERIES: dict[str, str] = {
    "summary": BASE + """
      select count(*) calls, count(distinct org_id) orgs, count(distinct ep) endpoints,
             count(distinct prov) providers, sum(spend) spend,
             sum(case when ok then 1 else 0 end) ok,
             sum(case when failed then 1 else 0 end) failed,
             sum(case when refused then 1 else 0 end) refused,
             percentile_disc(0.5) within group (order by duration_ms) p50_ms,
             percentile_disc(0.95) within group (order by duration_ms) p95_ms
      from c""",
    # {unit} is filled from a two-value whitelist in `collect` — a short window wants the day's
    # hourly shape, a long one wants days. Never interpolate anything user-supplied here.
    "daily": BASE + """
      select date_trunc('{unit}', created_at) d, count(*) calls,
             sum(case when ok then 1 else 0 end) ok,
             sum(case when failed then 1 else 0 end) failed,
             sum(case when refused then 1 else 0 end) refused,
             sum(spend) spend, count(distinct org_id) orgs
      from c group by 1 order by 1""",
    # Every endpoint, with each failure class broken out as its own column so a row says WHERE to
    # improve without opening anything: `req` is a request treg rejected, `res` is something it
    # could not find or serve, `failed` is the provider answering badly. Three different fixes.
    "endpoints": BASE + """
      select ep, prov, cataloged, count(*) calls, count(distinct org_id) orgs, sum(spend) spend,
             sum(case when ok then 1 else 0 end) ok,
             sum(case when failed then 1 else 0 end) failed,
             sum(case when refused_by = 'request'    then 1 else 0 end) req,
             sum(case when refused_by = 'resolution' then 1 else 0 end) res,
             sum(case when refused_by = 'balance'    then 1 else 0 end) bal,
             sum(case when refused_by = 'cap'        then 1 else 0 end) cap,
             sum(case when refused_by = 'auth'       then 1 else 0 end) auth,
             sum(case when refused_by = 'policy'     then 1 else 0 end) pol,
             percentile_disc(0.5) within group (order by duration_ms) p50_ms
      from c group by 1,2,3 order by count(*) desc""",
    # The per-endpoint drill-down behind each expandable row: one line per (reason, status), with a
    # sample path so a 400 can be reproduced. Only non-2xx rows, so it stays small.
    # Keyed by (ep, cataloged), NOT by ep alone: the same name can appear in BOTH endpoint tables —
    # e.g. a tikhub id that resolved 3,428 times and was also sent 159 times in an unresolvable
    # shape. Keying on the name alone would show each row the other one's errors.
    # `evidence` and `sent` are the provider's own error message and the caller's request, captured
    # for failed PLATFORM calls only (see models.CallRecord.error_response). They are NULL for an
    # own-key call by design, and for anything that failed before the capture shipped — so a blank
    # column here means "not captured", never "no error".
    #
    # Both come from ONE row, via the same ORDER BY inside array_agg. They used to be independent
    # `max()`es, which silently paired one call's request with a DIFFERENT call's response — a
    # plausible, readable, wrong diagnosis, which is worse than showing nothing. Ordering prefers a
    # row that actually has evidence, then the newest, and `id` makes it a total order so the two
    # aggregates cannot disagree.
    #
    # `reason` distinguishes three owners, not two: treg refused it, treg could not reach the
    # provider (a relay 502 — the provider never answered, and calling that "provider answered" is
    # the same false-diagnosis class), or the provider answered badly.
    "errdetail": BASE + """
      select ep, cataloged,
             -- Four cases, not three. Deriving "who failed" from the evidence TEXT is only sound
             -- while the text is there: before migration (A35) deploys it is NULL, and after 14 days
             -- it is '<expired>'. Both used to fall through to "provider answered", which quietly
             -- re-attributed treg's own 502s to the provider — the same false-diagnosis class this
             -- report was just fixed for, reappearing whenever evidence is absent. When we cannot
             -- tell, say so.
             case when refused_by is not null then refused_by
                  when error_response like 'treg:%' then '(treg never reached it)'
                  when error_response is null or error_response = '<expired>'
                       then '(evidence not captured)'
                  else '(provider answered)' end reason,
             status_code, count(*) n, count(distinct org_id) orgs,
             left(min(path), 170) sample,
             string_agg(distinct method, '/') methods,
             {evidence_agg}
      from c where status_code >= 400
      group by 1,2,3,4 order by 1, 5 desc""",
    "providers": BASE + """
      select prov, count(*) calls, count(distinct org_id) orgs, sum(spend) spend,
             sum(case when failed then 1 else 0 end) failed,
             sum(case when refused then 1 else 0 end) refused
      from c group by 1 order by count(*) desc""",
    "statuses": BASE + """
      select status_code, count(*) n, sum(case when refused then 1 else 0 end) refused
      from c where status_code >= 400 group by 1 order by 2 desc""",
    "refusals": BASE + """
      select refused_by, count(*) n, count(distinct org_id) orgs
      from c where refused_by is not null group by 1 order by 2 desc""",
    "failures": BASE + """
      select ep, prov, status_code, count(*) n, count(distinct org_id) orgs
      from c where failed group by 1,2,3 order by 4 desc limit 40""",
    # credential_tier is the rung of the ladder that served the call (api.py `_marketplace_call`).
    # NULL is NOT a fourth rung — it means the call was never a catalog endpoint at all, i.e. a plain
    # proxy call to a tool the team registered themselves. Labelling that "(own tool)" next to the
    # real "tool" tier is how you end up with two buckets that read identically and mean different
    # things, so each row is spelled out here instead.
    "tiers": BASE + """
      select case credential_tier
               when 'platform'   then 'platform — treg''s key, metered'
               when 'tool'       then 'tool — org''s own key, catalog endpoint'
               when 'credential' then 'credential — org''s own secret, no tool registered'
               else 'not a catalog call — org''s own registered tool'
             end tier,
             count(*) n, sum(spend) spend, count(distinct org_id) orgs
      from c group by 1 order by 2 desc""",
    "clients": BASE + """
      select coalesce(client,'(unreported)') client, count(*) n, count(distinct org_id) orgs
      from c group by 1 order by 2 desc""",
    # "The catalog doesn't have X" — filed from the web, the CLI, or by an agent over MCP.
    # NOT windowed like everything else, and deliberately: a request is a BACKLOG ITEM, not a time
    # series. A `--days 1` run that showed only today's would report an empty queue while a month of
    # unanswered asks sat in the table. So every open row is listed, and `in_window` marks the new
    # ones. `$1` is referenced only for that flag — collect() passes `since` to every query, and a
    # statement that ignores its parameter is a protocol error, not a no-op.
    "requests": """
      select id, org_id, user_email, capability, query, note, contact, source, status, created_at,
             (created_at >= $1) as in_window
      from toolrequest
      where status = 'open' or created_at >= $1
      order by created_at desc limit 300""",
    # Catalog searches that matched NOTHING, grouped by query — the demand signal one step before a
    # tool request (most agents that miss never file one; the query text is all they leave behind).
    # Windowed like the call panels: a miss is a time-series event, not a backlog item — the same
    # query missing last month and this month should read as two spikes, not one eternal row.
    "search_misses": """
      select query, count(*) n, string_agg(distinct source, '/') sources,
             min(created_at) first_seen, max(created_at) last_seen
      from searchmiss
      where created_at >= $1
      group by 1 order by count(*) desc, max(created_at) desc limit 200""",
    "orgs": BASE + """
      select c.org_id, o.slug, count(*) calls, sum(c.spend) spend,
             sum(case when c.failed then 1 else 0 end) failed,
             sum(case when c.refused then 1 else 0 end) refused,
             count(distinct c.ep) endpoints, max(c.created_at) last_seen
      from c left join org o on o.id = c.org_id
      group by 1,2 order by count(*) desc""",
}


async def collect(since: dt.datetime, unit: str = "day") -> dict:
    """Open prod to this IP, run every query, close prod again. Closing is not optional."""
    import asyncpg

    if unit not in ("hour", "day"):  # the only values that ever reach the SQL string
        raise ValueError(unit)

    ip = my_ip()
    print(f"opening prod allowlist for {ip}/32 ...", file=sys.stderr)
    render_api("PATCH", f"/postgres/{DB_ID}",
               {"ipAllowList": [{"cidrBlock": f"{ip}/32", "description": "usage_report.py"}]})
    try:
        dsn = render_api("GET", f"/postgres/{DB_ID}/connection-info")["externalConnectionString"]
        # Retry the connect. Two transient failures are common here and neither means anything is
        # wrong: the allowlist PATCH takes a moment to take effect, and Render's Postgres hostname
        # intermittently SERVFAILs (observed 2026-08-17). An unattended daily run must ride both out
        # rather than exit — the `finally` below still closes the allowlist if every attempt fails.
        conn = None
        for attempt, pause in enumerate((2, 5, 10, 0), start=1):
            try:
                conn = await asyncpg.connect(dsn, ssl="require", timeout=45,
                                             server_settings={"default_transaction_read_only": "on"})
                break
            except (OSError, asyncpg.PostgresError) as exc:
                if not pause:
                    raise
                print(f"  connect attempt {attempt} failed ({type(exc).__name__}: {exc}); "
                      f"retrying in {pause}s", file=sys.stderr)
                await asyncio.sleep(pause)
        try:
            # The failure-evidence columns arrive with migration (A35). Until that deploys, prod does
            # not have them — so probe rather than assume, or this script dies on
            # UndefinedColumnError against exactly the database it exists to report on. Degrading to
            # "no evidence available" keeps every other panel working through the deploy window.
            has_evidence = bool(await conn.fetchval(
                "select 1 from information_schema.columns"
                " where table_name = 'callrecord' and column_name = 'error_response'"))
            if not has_evidence:
                print("  note: callrecord has no error_request/error_response yet — evidence panels "
                      "will be empty until migration (A35) deploys", file=sys.stderr)
            ev_cols = ("error_request, error_response," if has_evidence
                       else "null::text error_request, null::text error_response,")
            # `nullif(..., '<expired>')` so an aged-out row reads as "no evidence" rather than as
            # evidence whose content is the word `<expired>` — otherwise the drawer prints
            # "said <expired>" and the exemplar picker prefers an expired row over a newer real one.
            ev_agg = ("""left((array_agg(nullif(error_response, '<expired>')
                            order by (nullif(error_response, '<expired>') is not null) desc,
                                     id desc))[1], 400) evidence,
                         left((array_agg(nullif(error_request, '<expired>')
                            order by (nullif(error_response, '<expired>') is not null) desc,
                                     id desc))[1], 220) sent"""
                      if has_evidence else "null::text evidence, null::text sent")

            # Same probe-don't-assume dance as the evidence columns: `searchmiss` arrives with the
            # zero-result search logging, and until that deploys the table does not exist on prod —
            # this script must keep working from any branch against exactly that database.
            has_misses = bool(await conn.fetchval(
                "select 1 from information_schema.tables where table_name = 'searchmiss'"))
            if not has_misses:
                print("  note: no searchmiss table yet — the empty-search panel will be empty "
                      "until the search-miss logging deploys", file=sys.stderr)

            out = {}
            for name, sql in QUERIES.items():
                if name == "search_misses" and not has_misses:
                    out[name] = []
                    continue
                sql = (sql.replace("{unit}", unit)
                          .replace("{evidence_cols}", ev_cols)
                          .replace("{evidence_agg}", ev_agg))
                rows = [dict(r) for r in await conn.fetch(sql, since)]
                out[name] = rows
                print(f"  {name}: {len(rows)} rows", file=sys.stderr)
            return out
        finally:
            await conn.close()
    finally:
        # Runs on success, on error, and on Ctrl-C. The verify is the point: a PATCH that 200s but
        # leaves the list populated would quietly expose prod until someone noticed.
        try:
            render_api("PATCH", f"/postgres/{DB_ID}", {"ipAllowList": []})
            still = render_api("GET", f"/postgres/{DB_ID}").get("ipAllowList")
            if still:
                print(f"!! allowlist NOT closed — still {still}. Close it by hand NOW.", file=sys.stderr)
            else:
                print("prod allowlist closed and verified.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — never let a reporting bug leave prod open silently
            print(f"!! could not close the allowlist ({exc}). Close it by hand NOW.", file=sys.stderr)


# ---- formatting helpers ------------------------------------------------------------------------
def usd(micro) -> str:
    return f"${(micro or 0) / 1_000_000:,.2f}"


def pct(n, d) -> str:
    return f"{(n / d * 100):.1f}%" if d else "—"


def short(s: str, n: int) -> str:
    s = s or "—"
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- terminal ------------------------------------------------------------------------------------
def print_summary(data: dict, days: int, top: int) -> None:
    s = data["summary"][0]
    calls = s["calls"] or 0
    print(f"\n\033[1mtreg usage — last {days}d\033[0m   ({calls:,} calls · {s['orgs']} orgs · "
          f"{s['endpoints']} endpoints · {s['providers']} providers)")
    print(f"  ok {s['ok']:,} ({pct(s['ok'], calls)})   "
          f"provider-failed {s['failed']:,} ({pct(s['failed'], calls)})   "
          f"treg-refused {s['refused']:,} ({pct(s['refused'], calls)})")
    print(f"  spend {usd(s['spend'])}   latency p50 {s['p50_ms'] or 0}ms / p95 {s['p95_ms'] or 0}ms")

    if data["refusals"]:
        print("\n\033[1mwhat treg stopped before sending\033[0m  (costs nothing)")
        for r in data["refusals"]:
            # An `auth` refusal has no org — the token never resolved to one — so 0 here is correct.
            where = f"across {r['orgs']} orgs" if r["orgs"] else "no org resolved"
            plain, gloss = PLAIN.get(r["refused_by"], (r["refused_by"], ""))
            print(f"  {r['n']:>6}  {plain:<14} {where:<20} {gloss}")

    cat = [e for e in data["endpoints"] if e["cataloged"]]
    non = [e for e in data["endpoints"] if not e["cataloged"]]
    print(f"\n\033[1mtop {top} endpoints\033[0m  ({len(cat)} catalog + {len(non)} non-catalog; "
          f"the HTML lists every one)")
    print(f"  {'calls':>7} {'prov':>5} {'req':>5} {'reso':>5} {'spend':>9} {'orgs':>5}  endpoint")
    for e in data["endpoints"][:top]:
        okrate = (e["ok"] or 0) / e["calls"] if e["calls"] else 1
        flag = "\033[31m!\033[0m" if e["calls"] >= 20 and okrate < 0.85 else " "
        print(f"  {e['calls']:>7} {e['failed'] or '':>5} {e['req'] or '':>5} {e['res'] or '':>5} "
              f"{usd(e['spend']):>9} {e['orgs']:>5} {flag}{short(e['ep'], 54)}")

    fails = data["failures"]
    if fails:
        print("\n\033[1mtop provider failures\033[0m  (the provider answered badly — treg's refusals are excluded)")
        for f in fails[:12]:
            print(f"  {f['n']:>5}  {f['status_code']}  {short(f['ep'], 62)}  ({f['orgs']} orgs)")

    reqs = data.get("requests") or []
    if reqs:
        new = sum(1 for r in reqs if r["in_window"])
        # A row with neither an email nor a contact is unanswerable — the request endpoint is
        # deliberately anonymous-friendly, so this is the cost of that choice, stated out loud.
        anon = sum(1 for r in reqs if not (r["user_email"] or r["contact"]))
        print(f"\n\033[1mtool requests\033[0m  ({len(reqs)} open · {new} filed in this window · "
              f"{anon} with no way to reply)")
        for r in reqs[:12]:
            who = r["user_email"] or r["contact"] or "anonymous"
            print(f"  {r['created_at']:%m-%d %H:%M}  {r['source']:<4} {short(who, 24):<24} "
                  f"{short(r['capability'], 58)}")
        if len(reqs) > 12:
            print(f"  … {len(reqs) - 12} more (the HTML lists every one)")

    misses = data.get("search_misses") or []
    if misses:
        hits = sum(m["n"] for m in misses)
        print(f"\n\033[1msearches that matched nothing\033[0m  ({len(misses)} distinct queries · "
              f"{hits} misses in this window)")
        for m in misses[:12]:
            print(f"  {m['n']:>5}  {m['sources']:<8} {short(m['query'], 64)}")
        if len(misses) > 12:
            print(f"  … {len(misses) - 12} more (the HTML lists every one)")

    print("\n\033[1mtop orgs\033[0m")
    for o in data["orgs"][:10]:
        print(f"  {o['calls']:>6} calls  {usd(o['spend']):>9}  {(o['slug'] or '?'):<24} "
              f"{o['endpoints']} endpoints, {o['failed']} failed, {o['refused']} refused")


# ---- HTML ----------------------------------------------------------------------------------------
CSS = """
:root{
  --bg:#eaeeee; --surface:#fff; --surface2:#f4f7f7; --ink:#121a1a; --muted:#5c6d6d;
  --line:#d2dcdc; --line-soft:#e3eaea; --accent:#0d6e68; --accent-soft:#0d6e6822;
  --ok:#2c7a52; --warn:#a86a14; --crit:#ab332c; --shadow:0 1px 2px #0f26261a;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0d1313; --surface:#141d1d; --surface2:#101818; --ink:#e4eded; --muted:#8fa3a3;
    --line:#24312f; --line-soft:#1c2726; --accent:#4ec9be; --accent-soft:#4ec9be22;
    --ok:#48a877; --warn:#cf9440; --crit:#d9645c; --shadow:0 1px 2px #0006;
  }
}
:root[data-theme="dark"]{
  --bg:#0d1313; --surface:#141d1d; --surface2:#101818; --ink:#e4eded; --muted:#8fa3a3;
  --line:#24312f; --line-soft:#1c2726; --accent:#4ec9be; --accent-soft:#4ec9be22;
  --ok:#48a877; --warn:#cf9440; --crit:#d9645c; --shadow:0 1px 2px #0006;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px;display:flex;flex-direction:column;gap:24px}
.num{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:12px;
  border-bottom:1px solid var(--line);padding-bottom:16px}
h1{margin:0;font-size:20px;font-weight:650;letter-spacing:-.01em}
h2{margin:0 0 12px;font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}
.sub{color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:14px 16px;
  box-shadow:var(--shadow);display:flex;flex-direction:column;gap:4px}
.kpi .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.kpi .v{font-size:24px;font-weight:600;letter-spacing:-.02em}
.kpi .d{font-size:12px;color:var(--muted)}
.kpi.ok .v{color:var(--ok)} .kpi.warn .v{color:var(--warn)} .kpi.crit .v{color:var(--crit)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:18px;
  box-shadow:var(--shadow);overflow:hidden}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}
@media (max-width:820px){.cols{grid-template-columns:1fr}}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
  font-weight:600;padding:0 8px 7px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--line-soft);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right}
td.name{white-space:normal;word-break:break-word;min-width:220px}
/* A request filed inside the report window. The left rule carries the meaning; the tint is only a
   hint, so this still reads on a monochrome print and in both themes. */
tr.fresh td{background:var(--accent-soft)}
tr.fresh td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
.bar{position:relative;display:block;height:3px;background:var(--accent-soft);border-radius:2px;margin-top:4px}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:2px}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;
  border:1px solid currentColor}
.pill.ok{color:var(--ok)} .pill.warn{color:var(--warn)} .pill.crit{color:var(--crit)}
.dim{color:var(--muted)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:10px}
.legend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px;line-height:1.7}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  background:var(--surface2);padding:1px 5px;border-radius:3px}
/* --- the two big expandable endpoint tables ------------------------------------------------ */
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:0 0 14px}
.toolbar input[type=search]{flex:1 1 220px;min-width:180px;padding:6px 10px;font:inherit;
  color:var(--ink);background:var(--surface2);border:1px solid var(--line);border-radius:4px}
.toolbar label{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted);
  cursor:pointer;user-select:none}
.toolbar .count{font-size:12px;color:var(--muted)}
:is(input,button,tr).exp:focus-visible,tr.exp:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
table.big{min-width:900px}
table.big thead th{position:sticky;top:0;z-index:1;background:var(--surface)}
tr.exp{cursor:pointer}
tr.exp:hover td{background:var(--surface2)}
.caret{display:inline-block;width:13px;color:var(--accent);font-size:10px;
  transition:transform .12s ease;transform-origin:center}
tr.exp[aria-expanded="true"] .caret{transform:rotate(90deg)}
@media (prefers-reduced-motion:reduce){.caret{transition:none}}
tr.detail>td{background:var(--surface2);padding:0 8px 10px 26px}
table.inner{width:auto;min-width:520px;font-size:12px;margin-top:2px}
table.inner th{padding-top:8px;border-bottom:1px solid var(--line)}
table.inner td{border-bottom:1px solid var(--line-soft);padding:4px 10px 4px 0}
table.inner code{font-size:11px;background:transparent;padding:0}
/* The captured evidence: what the provider said, and what the caller sent. Wraps, because these are
   sentences — a message clipped to a column width is the problem this feature exists to fix. */
tr.evrow td{padding:0 10px 8px 0;border-bottom:1px solid var(--line-soft)}
.ev{display:flex;gap:8px;align-items:baseline;margin-top:2px;font-size:12px}
.ev b{flex:0 0 auto;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.ev code{white-space:pre-wrap;word-break:break-word;color:var(--ink);font-size:11.5px}
"""

# Row expansion + filtering. Vanilla, inline, no network: the page must work from a file:// URL.
JS = """
for (const t of document.querySelectorAll('table.big')) {
  t.addEventListener('click', e => {
    const tr = e.target.closest('tr.exp'); if (!tr || !t.contains(tr)) return;
    const d = tr.nextElementSibling;
    if (!d || !d.classList.contains('detail')) return;
    const open = d.hidden; d.hidden = !open; tr.setAttribute('aria-expanded', open);
  });
  t.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const tr = e.target.closest('tr.exp'); if (!tr) return;
    e.preventDefault(); tr.click();
  });
}
for (const box of document.querySelectorAll('[data-filters]')) {
  const table = document.querySelector('#' + box.dataset.filters);
  const q = box.querySelector('input[type=search]');
  const badOnly = box.querySelector('input[type=checkbox]');
  const out = box.querySelector('.count');
  const apply = () => {
    const needle = q.value.trim().toLowerCase();
    let shown = 0, total = 0;
    for (const tr of table.tBodies[0].rows) {
      if (tr.classList.contains('detail')) continue;
      total++;
      const hit = (!needle || tr.dataset.ep.toLowerCase().includes(needle))
               && (!badOnly.checked || tr.dataset.bad === '1');
      tr.hidden = !hit; if (hit) shown++;
      const d = tr.nextElementSibling;
      if (d && d.classList.contains('detail') && !hit) { d.hidden = true; tr.removeAttribute('aria-expanded'); }
    }
    out.textContent = shown === total ? total + ' rows' : shown + ' of ' + total + ' rows';
  };
  q.addEventListener('input', apply); badOnly.addEventListener('change', apply); apply();
}
"""


def svg_stacked(daily: list[dict], unit: str = "day") -> str:
    """Volume as ok / provider-failed / treg-refused. Hand-built bars — no library, no CDN."""
    if not daily:
        return '<p class="dim">No calls in this window.</p>'
    fmt = "%Hh" if unit == "hour" else "%d"
    W, H, PAD = 1100, 190, 26
    peak = max(d["calls"] for d in daily) or 1
    n = len(daily)
    slot = (W - PAD * 2) / n
    bw = max(3.0, min(slot * 0.66, 46))
    parts = []
    for i, d in enumerate(daily):
        x = PAD + slot * i + (slot - bw) / 2
        y = H - PAD
        for key, var in (("refused", "var(--warn)"), ("failed", "var(--crit)"), ("ok", "var(--ok)")):
            v = d[key] or 0
            if not v:
                continue
            h = (v / peak) * (H - PAD * 2)
            y -= h
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{var}"/>')
        every = max(1, n // 14)  # thin the axis labels so hourly buckets don't collide
        if i % every == 0:
            parts.append(
                f'<text x="{x + bw / 2:.1f}" y="{H - PAD + 14}" text-anchor="middle" font-size="10" '
                f'fill="var(--muted)">{d["d"].strftime(fmt)}</text>')
        parts.append(f'<title>{d["d"]}: {d["calls"]:,} calls, {d["failed"]} failed, {d["refused"]} refused</title>')
    grid = "".join(
        f'<line x1="{PAD}" x2="{W - PAD}" y1="{H - PAD - f * (H - PAD * 2):.1f}" '
        f'y2="{H - PAD - f * (H - PAD * 2):.1f}" stroke="var(--line-soft)"/>' for f in (0.5, 1.0))
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" role="img" '
            f'aria-label="Daily call volume">{grid}{"".join(parts)}'
            f'<line x1="{PAD}" x2="{W - PAD}" y1="{H - PAD}" y2="{H - PAD}" stroke="var(--line)"/></svg>'
            '<div class="legend">'
            '<span><b style="background:var(--ok)"></b>ok</span>'
            '<span><b style="background:var(--crit)"></b>provider failed</span>'
            '<span><b style="background:var(--warn)"></b>treg refused</span></div>')


# The database stores terse gate names. Nobody should have to learn them to read this report, so
# every surface prints plain English and keeps the raw value alongside for grepping the DB.
PLAIN = {
    "request":    ("bad request", "wrong method, or a missing/invalid parameter"),
    "resolution": ("not found", "no such endpoint, or treg has no key to call it with"),
    "balance":    ("out of credit", "the org's prepaid balance could not cover it"),
    # NOT "the org hit its own spending cap": every 429 maps to `cap`, and that covers a member
    # call-count cap, a tag call or spend cap, the platform ceiling, a trial allowance and a demo-IP
    # limit. Naming one of them was a confident wrong answer for the other five.
    "cap":        ("hit a limit", "some cap or quota — member, tag, org, platform or trial"),
    "auth":       ("bad token", "the caller's token was missing, wrong or expired"),
    "policy":     ("blocked", "an ACL, deny rule or suspension refused it"),
    "(treg never reached it)": ("treg could not reach the provider",
                                "timeout, reset, failed injection or SSRF refusal — no answer came"),
}


def _detail_rows(detail: list[dict], cols: int) -> str:
    """The drill-down revealed when a row is expanded.

    The first column answers WHO produced the status, which is the whole point of the drawer: the
    identical 400 can be the provider rejecting a relayed call or treg refusing to relay it at all,
    and those have different owners. An earlier version put both in one column headed "Refused by",
    which made every provider error read as "refused by: the provider answered" — a contradiction.
    """
    rows = []
    for d in detail:
        upstream = d["reason"] == "(provider answered)"
        who = ('<span class="pill crit">provider</span>' if upstream
               else '<span class="pill warn">treg</span>')
        plain, gloss = PLAIN.get(d["reason"], (d["reason"], ""))
        why = ('<span class="dim">the provider rejected it</span>' if upstream
               else f'<b>{html.escape(plain)}</b> <span class="dim">— {html.escape(gloss)}</span>')
        # The method is already on every row and costs nothing to show. It IS the diagnosis for a
        # whole failure class: 47 of apollo.people.enrich's failures were a GET at a POST endpoint,
        # and without this column the drawer only ever said "bad request".
        meth = html.escape(d.get("methods") or "")
        rows.append(
            f'<tr><td>{who}</td><td>{why}</td><td class="num">{meth} {d["status_code"]}</td>'
            f'<td class="r num">{d["n"]:,}</td><td class="r num">{d["orgs"]}</td>'
            f'<td class="name dim"><code>{html.escape(d["sample"] or "")}</code></td></tr>')
        # The captured evidence, when there is any. Its own full-width row rather than another
        # column: a provider's message is a sentence, and squeezing it into a table cell is how it
        # ends up truncated to uselessness — which was the whole problem this feature solves.
        said, sent = d.get("evidence"), d.get("sent")
        if said or sent:
            bits = []
            if said:
                bits.append(f'<div class="ev"><b>said</b> <code>{html.escape(said)}</code></div>')
            if sent:
                bits.append(f'<div class="ev"><b>sent</b> <code>{html.escape(sent)}</code></div>')
            rows.append(f'<tr class="evrow"><td></td><td colspan="5">{"".join(bits)}</td></tr>')
        elif not d.get("cataloged"):
            # Say WHY there is nothing, rather than letting a blank read as "no detail exists".
            # Evidence is captured for platform calls only, so a team's own registered tool — which
            # is the single largest failure group, google-ads at 283 — never has any here.
            rows.append('<tr class="evrow"><td></td><td colspan="5"><div class="ev dim">'
                        'not captured — own registered tool, so the request and the provider\'s '
                        'answer are the team\'s to inspect, not treg\'s</div></td></tr>')
    return (f'<tr class="detail" hidden><td colspan="{cols}">'
            f'<div class="scroll"><table class="inner"><thead><tr>'
            f'<th>Stopped by</th><th>Reason</th><th>Status</th>'
            f'<th class="r">Calls</th><th class="r">Orgs</th>'
            f'<th>Example request <span class="dim">— a full URL went upstream; '
            f'a bare id never left treg</span></th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></td></tr>')


def endpoint_table(eps: list[dict], details: dict[tuple[str, bool], list[dict]], *, first_col: str,
                   table_id: str) -> str:
    """Every endpoint, one row each, expandable into its error breakdown. No truncation."""
    if not eps:
        return '<p class="dim">Nothing in this bucket.</p>'
    peak = max(e["calls"] for e in eps) or 1
    head = (f'<th>{first_col}</th><th class="r">Calls</th><th class="r">Orgs</th><th class="r">OK</th>'
            '<th class="r">Provider<br/>said no</th><th class="r">Bad<br/>request</th>'
            '<th class="r">Not<br/>found</th><th class="r">Out of credit<br/>/ capped</th>'
            '<th class="r">Spend</th><th class="r">p50</th>')
    cols = 10
    out = []
    for e in eps:
        calls, ep = e["calls"], e["ep"] or "—"
        other = (e["bal"] or 0) + (e["cap"] or 0) + (e["auth"] or 0) + (e["pol"] or 0)
        bad = (e["failed"] or 0) + (e["req"] or 0) + (e["res"] or 0) + other
        okrate = (e["ok"] or 0) / calls if calls else 0
        cls = "crit" if okrate < 0.6 else "warn" if okrate < 0.95 else "ok"
        det = details.get((ep, e["cataloged"]))
        # Only a row that HAS failures is expandable — a clickable row with an empty drawer is a lie.
        out.append(
            f'<tr class="{"exp" if det else ""}" data-ep="{html.escape(ep)}" '
            f'data-bad="{1 if bad else 0}" tabindex="{0 if det else -1}">'
            f'<td class="name">{"<span class=\'caret\'>▸</span>" if det else "<span class=\'caret dim\'>·</span>"}'
            f'{html.escape(ep)}'
            f'<span class="bar"><i style="width:{calls / peak * 100:.1f}%"></i></span></td>'
            f'<td class="r num">{calls:,}</td><td class="r num dim">{e["orgs"]}</td>'
            f'<td class="r"><span class="pill {cls}">{okrate * 100:.0f}%</span></td>'
            f'<td class="r num">{e["failed"] or ""}</td><td class="r num">{e["req"] or ""}</td>'
            f'<td class="r num">{e["res"] or ""}</td><td class="r num dim">{other or ""}</td>'
            f'<td class="r num">{usd(e["spend"])}</td>'
            f'<td class="r num dim">{e["p50_ms"] or 0}ms</td></tr>')
        if det:
            out.append(_detail_rows(det, cols))
    return (f'<div class="scroll"><table class="big" id="{table_id}"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(out)}</tbody></table></div>')


def build_html(data: dict, days: int, top: int, since: dt.datetime, unit: str = "day") -> str:
    s = data["summary"][0]
    calls = s["calls"] or 0
    fail_rate = (s["failed"] or 0) / calls if calls else 0
    kpi_cls = "crit" if fail_rate > 0.10 else "warn" if fail_rate > 0.05 else "ok"
    bal = next((r["n"] for r in data["refusals"] if r["refused_by"] == "balance"), 0)

    kpis = [
        ("Calls", f"{calls:,}", f"{s['orgs']} orgs · {s['endpoints']} endpoints", ""),
        ("Spend", usd(s["spend"]), f"{usd((s['spend'] or 0) / max(days, 1))}/day", ""),
        ("Provider failures", f"{pct(s['failed'], calls)}", f"{s['failed']:,} calls", kpi_cls),
        ("Refused by treg", f"{pct(s['refused'], calls)}", f"{s['refused']:,} calls", "warn" if s["refused"] else ""),
        ("Hit the paywall", f"{bal:,}", "balance refusals", "warn" if bal else ""),
        ("Latency p95", f"{s['p95_ms'] or 0}ms", f"p50 {s['p50_ms'] or 0}ms", ""),
    ]
    kpi_html = "".join(
        f'<div class="kpi {c}"><span class="k">{k}</span><span class="v num">{v}</span>'
        f'<span class="d num">{d}</span></div>' for k, v, d, c in kpis)

    def table(head: str, body: str) -> str:
        return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    refusals = table(
        '<th>Reason</th><th class="r">Calls</th><th class="r">Orgs</th>',
        "".join(f'<tr><td><code>{html.escape(r["refused_by"])}</code></td>'
                f'<td class="r num">{r["n"]:,}</td><td class="r num">{r["orgs"]}</td></tr>'
                for r in data["refusals"]) or '<tr><td colspan="3" class="dim">Nothing refused.</td></tr>')

    failures = table(
        '<th>Endpoint</th><th class="r">Status</th><th class="r">Calls</th>',
        "".join(f'<tr><td class="name">{html.escape(f["ep"] or "—")}</td>'
                f'<td class="r num">{f["status_code"]}</td><td class="r num">{f["n"]:,}</td></tr>'
                for f in data["failures"][:14]) or '<tr><td colspan="3" class="dim">No provider failures.</td></tr>')

    providers = table(
        '<th>Provider</th><th class="r">Calls</th><th class="r">Orgs</th><th class="r">Failed</th>'
        '<th class="r">Refused</th><th class="r">Spend</th>',
        "".join(f'<tr><td>{html.escape(p["prov"] or "—")}</td><td class="r num">{p["calls"]:,}</td>'
                f'<td class="r num">{p["orgs"]}</td><td class="r num">{p["failed"] or ""}</td>'
                f'<td class="r num">{p["refused"] or ""}</td><td class="r num">{usd(p["spend"])}</td></tr>'
                for p in data["providers"][:20]))

    orgs = table(
        '<th>Org</th><th class="r">Calls</th><th class="r">Endpoints</th><th class="r">Failed</th>'
        '<th class="r">Refused</th><th class="r">Spend</th>',
        "".join(f'<tr><td>{html.escape(o["slug"] or ("org " + str(o["org_id"])))}</td>'
                f'<td class="r num">{o["calls"]:,}</td><td class="r num">{o["endpoints"]}</td>'
                f'<td class="r num">{o["failed"] or ""}</td><td class="r num">{o["refused"] or ""}</td>'
                f'<td class="r num">{usd(o["spend"])}</td></tr>'
                for o in data["orgs"][:15]))

    tiers = table(
        '<th>Credential</th><th class="r">Calls</th><th class="r">Orgs</th><th class="r">Spend</th>',
        "".join(f'<tr><td><code>{html.escape(t["tier"])}</code></td><td class="r num">{t["n"]:,}</td>'
                f'<td class="r num">{t["orgs"]}</td><td class="r num">{usd(t["spend"])}</td></tr>'
                for t in data["tiers"]))

    clients = table(
        '<th>Agent</th><th class="r">Calls</th><th class="r">Orgs</th>',
        "".join(f'<tr><td>{html.escape(c["client"])}</td><td class="r num">{c["n"]:,}</td>'
                f'<td class="r num">{c["orgs"]}</td></tr>' for c in data["clients"][:10]))

    reqs = data.get("requests") or []
    req_new = sum(1 for r in reqs if r["in_window"])
    req_new_label = f" · {req_new} new" if req_new else ""
    req_anon = sum(1 for r in reqs if not (r["user_email"] or r["contact"]))
    def request_row(r: dict) -> str:
        who = r["user_email"] or r["contact"]
        who_cell = html.escape(who) if who else '<span class="dim">anonymous</span>'
        detail = r["note"] or r["query"]
        detail_html = f'<div class="dim">{html.escape(short(detail, 260))}</div>' if detail else ""
        return (f'<tr class="{"fresh" if r["in_window"] else ""}">'
                f'<td class="num">{r["created_at"]:%Y-%m-%d %H:%M}</td>'
                f'<td><code>{html.escape(r["source"] or "?")}</code></td>'
                f'<td>{who_cell}</td>'
                f'<td class="name">{html.escape(r["capability"] or "—")}{detail_html}</td></tr>')

    requests_html = table(
        '<th>Filed</th><th>Source</th><th>Who</th><th>Asked for</th>',
        "".join(request_row(r) for r in reqs)
        or '<tr><td colspan="4" class="dim">No open requests.</td></tr>')

    misses = data.get("search_misses") or []
    miss_total = sum(m["n"] for m in misses)
    misses_html = table(
        '<th class="r">Misses</th><th>Source</th><th>Query</th><th class="r">Last seen</th>',
        "".join(f'<tr><td class="r num">{m["n"]:,}</td><td><code>{html.escape(m["sources"])}</code></td>'
                f'<td class="name">{html.escape(m["query"])}</td>'
                f'<td class="r num dim">{m["last_seen"]:%Y-%m-%d %H:%M}</td></tr>'
                for m in misses)
        or '<tr><td colspan="4" class="dim">No empty searches in this window '
           '(or the search-miss logging has not deployed yet).</td></tr>')

    details: dict[tuple[str, bool], list[dict]] = {}
    for d in data["errdetail"]:
        details.setdefault((d["ep"], d["cataloged"]), []).append(d)

    cataloged = [e for e in data["endpoints"] if e["cataloged"]]
    other = [e for e in data["endpoints"] if not e["cataloged"]]
    endpoints = endpoint_table(cataloged, details, first_col="Endpoint", table_id="t-cat")
    noncatalog = endpoint_table(other, details, table_id="t-non",
                                first_col="Tool name / what the caller sent")

    def toolbar(target: str, n: int) -> str:
        return (f'<div class="toolbar" data-filters="{target}">'
                f'<input type="search" placeholder="Filter {n} rows by name…" aria-label="Filter rows"/>'
                f'<label><input type="checkbox"/> only rows with errors</label>'
                f'<span class="count"></span></div>')

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    # The DOCTYPE is load-bearing, not boilerplate: without it Chrome renders in QUIRKS MODE, where
    # <table> does not inherit `color` from its ancestors. Every table cell then falls back to the
    # light-theme ink and the whole report is invisible dark-on-dark for a dark-mode reader. Verified
    # in a browser — the bug is silent in light mode, so it survives any amount of source review.
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>treg Call Ledger</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">
  <header>
    <div><h1>treg call ledger</h1>
      <div class="sub num">{since:%Y-%m-%d %H:%M} → now · {days} day{"s" if days != 1 else ""}</div></div>
    <div class="sub num">generated {generated}</div>
  </header>

  <div class="kpis">{kpi_html}</div>

  <div class="card"><h2>Volume by {unit}</h2>{svg_stacked(data["daily"], unit)}</div>

  <div class="card"><h2>Tool requests — all {len(reqs)} open{req_new_label}</h2>
    <p class="sub" style="margin:-4px 0 12px">"The catalog doesn't have X", filed from the web page,
    the CLI, or by an agent mid-search over MCP. <b>New rows in this window are highlighted.</b>
    Every row here stays until someone flips its <code>status</code> by hand — this is a queue, not a
    feed, so it is listed in full regardless of the report window.<br/>
    <b>Read them against the catalog before building anything.</b> A request for something treg
    already serves is a <i>discovery</i> failure wearing a coverage failure's clothes, and shipping
    the endpoint again would not have helped the person who asked.<br/>
    {req_anon} of {len(reqs)} carry neither an email nor a contact — filing is deliberately
    anonymous-friendly, so those asks cannot be answered even when we build the thing.</p>
    {requests_html}</div>

  <div class="card"><h2>Searches that matched nothing — {len(misses)} distinct
    <span class="dim">· {miss_total} misses</span></h2>
    <p class="sub" style="margin:-4px 0 12px">Every catalog search in this window that returned
    zero results, from the API/CLI route and from agents over MCP. This is the demand signal one
    step <b>before</b> the tool-request queue above — most agents that miss never file, so the
    query text is all they leave behind.<br/>
    <b>Read each against the catalog before adding anything:</b> a miss for something treg already
    serves is a naming/discovery failure — fix the endpoint's words, not the coverage.</p>
    {misses_html}</div>

  <div class="card"><h2>Catalog endpoints — all {len(cataloged)}</h2>
    <p class="sub" style="margin:-4px 0 12px">Calls that resolved to a catalog endpoint. The three
    failure columns are separated because each has a different owner:<br/>
    <b>Provider said no</b> — treg passed the call on and the provider rejected it. Their end.<br/>
    <b>Bad request</b> — wrong HTTP method, or a missing/invalid parameter. treg stopped it; nothing
    was sent and nothing was charged. <span class="dim">(stored as <code>request</code>)</span><br/>
    <b>Not found</b> — no such endpoint, or it exists but treg has no key to call it with. Also
    stopped before sending. <span class="dim">(stored as <code>resolution</code>)</span><br/>
    <b>Out of credit / capped</b> — the org ran out of balance or hit its own cap. Not a bug; this is
    demand you did not serve.<br/>
    Click any row with errors for the status codes and an example request.</p>
    {toolbar("t-cat", len(cataloged))}{endpoints}</div>

  <div class="card"><h2>Not a catalog endpoint — all {len(other)}</h2>
    <p class="sub" style="margin:-4px 0 12px">Everything absent from the table above, because no
    <code>endpoint_id</code> was attached. Two populations live here: <b>tools a team registered
    itself</b> (real hosts — google-ads, search-console, render, vercel), and <b>calls treg could not
    make sense of</b> — a bare catalog id sent where <code>/call/</code> wants the full upstream URL,
    or <code>https:</code> written with one slash. A high <b>Not found</b> count is the second kind.</p>
    {toolbar("t-non", len(other))}{noncatalog}</div>

  <div class="cols">
    <div class="card"><h2>Why treg said no</h2>{refusals}
      <p class="sub" style="margin:12px 0 0">These never reached a provider and cost nothing.
      <code>balance</code> is demand that arrived and was turned away.</p></div>
    <div class="card"><h2>Provider failures</h2>{failures}</div>
  </div>

  <div class="card"><h2>Providers</h2>{providers}</div>

  <div class="cols">
    <div class="card"><h2>Whose credential paid</h2>{tiers}</div>
    <div class="card"><h2>Which agent called</h2>{clients}</div>
  </div>


  <div class="card"><h2>Busiest orgs</h2>{orgs}</div>

  <footer>
    <b>Stopped by treg</b> = refused before anything went upstream (bad request, not found, out of
    credit, capped, bad token, blocked) — costs nothing and is not a provider fault.
    <b>Provider said no</b> = the call went upstream and came back 4xx/5xx.
    <b>spend</b> = <code>cost_charged_micro</code>, what actually hit the org's balance — not the
    reserved estimate.<br>
    Generated by <code>scripts/usage_report.py</code> from the production database.
  </footer>
</div>
<script>{JS}</script>
</body></html>"""


# ---- entry point ---------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=1, help="window size in days (default 1)")
    ap.add_argument("--top", type=int, default=25, help="endpoints to list (default 25)")
    ap.add_argument("--html", metavar="PATH",
                    help="write the dashboard here (default reports/usage-<date>.html; '-' to skip)")
    ap.add_argument("--json", action="store_true", help="dump the raw aggregates to stdout instead")
    args = ap.parse_args()

    days = max(1, args.days)
    # Naive UTC — the convention every timestamp column in this app stores (see reconcile.window_start).
    since = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days)
    unit = "hour" if days <= 2 else "day"  # a one-day report wants the day's shape, not one bar
    data = asyncio.run(collect(since, unit))

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return

    print_summary(data, days, args.top)

    if args.html == "-":
        return
    # The window goes in the FILENAME. Without it a --days 1 and a --days 7 run on the same date
    # write to the same path, and the second silently replaces the first — two very different
    # reports, one name, no way to tell them apart afterwards.
    out = Path(args.html) if args.html else REPO / "reports" / f"usage-{dt.date.today()}-{days}d.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(data, days, args.top, since, unit), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
