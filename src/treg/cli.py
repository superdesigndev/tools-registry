"""treg — a thin CLI over the registry API. It owns NO logic of its own (charter: the API is the
only brain); every command is one HTTP call. Config lives in ~/.treg/config.json.

Auth is identity-first: `treg login` opens the browser, you authenticate with GitHub, and the CLI
stores a single **identity token** (first login also registers you). Then you work across all your
orgs — `treg org ls` / `treg org use <slug>` picks the active one, sent as `X-Treg-Org`. Agents/CI
can instead `treg login --token <token>` with a per-org token. `treg logout` clears it.

    treg config --base-url https://treg.superdesign.dev
    treg login                       # GitHub (register-or-login); or: treg login --token <token>
    treg org ls | org use <slug>
    treg secret add / tool add / call / calls / health / skill / admin
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import itertools
import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from urllib.parse import quote

import httpx

from . import agents as _agents

CONFIG_PATH = Path.home() / ".treg" / "config.json"

# Per-invocation `--org <slug>` override (stripped from argv in main); overrides the active org.
_ORG_OVERRIDE: str | None = None


# ---- config (identity-first: one bearer token + an active org slug) -----------------------
def _load_config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        raw = {}  # a corrupt/half-written config must not brick every command (incl. login/logout)
    base = raw.get("base_url", "http://localhost:18790")
    if "orgs" in raw:  # migrate legacy multi-org config → the active org's token as the bearer
        active = raw.get("active_org")
        tok = (raw.get("orgs", {}).get(active) or {}).get("token")
        return {"base_url": base, "token": tok, "email": raw.get("email"), "active_org": active,
                "identity": False, "admin_token": raw.get("admin_token")}
    return {"base_url": base, "token": raw.get("token"), "email": raw.get("email"),
            "active_org": raw.get("active_org"), "identity": raw.get("identity", False),
            "admin_token": raw.get("admin_token")}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so an interrupted save (kill / full disk) can't leave a truncated,
    # unparseable config that bricks every subsequent command.
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, CONFIG_PATH)


def _pick_active_org(cfg: dict) -> None:
    """Best-effort: set the active org from GET /orgs. The token is already persisted by the
    caller, so a transient failure here (proxy hiccup, cold restart) must never lose it."""
    try:
        with _client(cfg) as c:
            r = c.get("/orgs")
        orgs = r.json() if r.status_code == 200 else []
        if orgs:
            cfg["active_org"] = next((o for o in orgs if o.get("active")), orgs[0])["slug"]
            _save_config(cfg)
    except Exception:
        pass


def _effective_org(cfg: dict) -> str | None:
    return _ORG_OVERRIDE or cfg.get("active_org")


class _RegistryClient(httpx.Client):
    """An httpx client that survives an upstream WAF. Some edges (Cloudflare, incl. Render's) 403 a
    request whose body matches an injection signature -- e.g. a skill recipe or a proxied `call` that
    legitimately carries SQL/HTML. On such a block (a 403 whose body is an HTML block page, never
    treg's own JSON 403s) it re-sends the request base64-encoded with `X-Treg-Body-Encoding: base64`,
    which the server decodes back to the real bytes. Transparent: no effect on any request that isn't
    blocked, and it retries at most once."""

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        resp = super().send(request, **kwargs)
        body = request.content or b""
        if (resp.status_code != 403 or not body
                or "html" not in resp.headers.get("content-type", "").lower()
                or request.headers.get("x-treg-body-encoding")):
            return resp  # not a WAF block (treg's 403s are JSON), nothing to encode, or already retried
        retry = self.build_request(request.method, request.url, content=base64.b64encode(body))
        retry.headers["x-treg-body-encoding"] = "base64"
        if "content-type" in request.headers:  # preserve JSON so the server still parses it after decode
            retry.headers["content-type"] = request.headers["content-type"]
        print("  (edge WAF blocked the request body; retrying base64-encoded)", file=sys.stderr)
        return super().send(retry, **kwargs)


def _client(cfg: dict, *, auth: bool = True) -> httpx.Client:
    headers = {"ngrok-skip-browser-warning": "1"}
    if auth and cfg.get("token"):
        headers["X-Treg-Token"] = cfg["token"]
        org = _effective_org(cfg)
        if org:
            headers["X-Treg-Org"] = org  # ignored for per-org tokens; picks the org for identity tokens
    return _RegistryClient(base_url=cfg["base_url"], headers=headers, timeout=30.0)


def _admin_client(cfg: dict) -> httpx.Client:
    token = cfg.get("admin_token") or cfg.get("token") or ""
    return httpx.Client(base_url=cfg["base_url"], headers={"X-Treg-Token": token, "ngrok-skip-browser-warning": "1"}, timeout=30.0)


def _active_org_id(cfg: dict, c: httpx.Client) -> int | None:
    """The active org's numeric id (for /orgs/{id}/... endpoints), resolved via GET /orgs."""
    r = c.get("/orgs")
    if r.status_code != 200:
        return None
    orgs = r.json()
    target = _effective_org(cfg)
    if target:
        for o in orgs:
            if o["slug"] == target:
                return o["org_id"]
    for o in orgs:
        if o.get("active"):
            return o["org_id"]
    return None


def _load_json_arg(s: str, label: str):
    """Parse an inline-JSON command-line argument, exiting cleanly (not tracebacking) on bad JSON."""
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        sys.exit(f"--{label} is not valid JSON: {exc}")


def _show(resp: httpx.Response) -> None:
    try:
        print(json.dumps(resp.json(), indent=2))
    except Exception:
        print(resp.text)
    if resp.status_code >= 400:
        sys.exit(1)


def _detail_url(cfg: dict, kind: str, name: str) -> str:
    """The shareable dashboard page for a registered skill/tool. Printed after every registration so
    sharing is just forwarding the link — the page carries the preview + the agent install prompt."""
    base = (cfg.get("base_url") or "https://treg.superdesign.dev").rstrip("/")
    return f"{base}/app/{'skills' if kind == 'skill' else 'tools'}/{quote(str(name), safe='')}"


# ---- auth --------------------------------------------------------------------------------
def cmd_config(args, cfg) -> None:
    if args.base_url:
        cfg["base_url"] = args.base_url
        _save_config(cfg)
    print(json.dumps({"base_url": cfg["base_url"], "email": cfg.get("email"),
                      "active_org": cfg.get("active_org"), "logged_in": bool(cfg.get("token"))}, indent=2))


def cmd_login(args, cfg) -> None:
    if args.token:  # agent / CI: a token directly (a per-org token, or a dashboard identity token)
        cfg.update(token=args.token, active_org=None, identity=False)  # drop any stale active_org
        # VERIFY before claiming success — a rejected token used to print "Token saved" and only fail on
        # the first real call ("misleading"). /auth/me needs no org, so it validates either token kind.
        try:
            with _client(cfg) as c:
                who = c.get("/auth/me")
        except Exception as exc:  # noqa: BLE001 — network/DNS: report, don't persist a maybe-bad token
            sys.exit(f"Could not reach {cfg['base_url']} to verify the token: {exc}")
        if who.status_code == 401:
            sys.exit("That token was rejected (401 invalid token). It's expired or from a different "
                     "server — copy a fresh one from the dashboard ('API token' / the Access instruction).")
        if who.status_code >= 400:
            sys.exit(f"Token check failed ({who.status_code}): {who.text[:120]}")
        cfg["email"] = who.json().get("email")
        _save_config(cfg)  # persist only a VERIFIED token
        _pick_active_org(cfg)
        if cfg.get("active_org"):
            print(f"✓ Token saved. Active org: {cfg['active_org']}")
        else:  # a valid identity/token whose user has no team yet — the calls would 400 "choose an org"
            print("✓ Token saved, but you're not in a team yet. Create one with "
                  "`treg org create \"Your Team\"` or accept an invite, then retry.")
        return
    if getattr(args, "email", None):  # email one-time-code (register-or-login by proving an email)
        base = cfg["base_url"].rstrip("/")
        h = {"ngrok-skip-browser-warning": "1"}
        r = httpx.post(f"{base}/auth/email/start", json={"email": args.email}, headers=h, timeout=15)
        if r.status_code >= 400:
            _show(r)
            return
        d = r.json()
        print(f"(dev) your code is: {d['dev_code']}" if d.get("dev_code")
              else f"We sent a 6-digit code to {args.email}.")
        code = input("Enter code: ").strip()
        r = httpx.post(f"{base}/auth/email/verify", json={"email": args.email, "code": code}, headers=h, timeout=15)
        if r.status_code >= 400:
            _show(r)
            return
        d = r.json()
        cfg.update(token=d["token"], email=d["email"], identity=True)
        _save_config(cfg)  # persist the freshly-minted token BEFORE the optional org lookup
        _pick_active_org(cfg)
        print(f"✓ Logged in as {cfg['email']}. Active org: {cfg.get('active_org')}")
        _maybe_offer_onboarding(cfg)
        return
    # Browser handshake (register-or-login) — the /login page reuses an existing dashboard
    # session with one click, else offers every configured door (GitHub / Google / email code).
    import secrets as _secrets
    base = cfg["base_url"].rstrip("/")
    # Ask the SERVER to start the login: it mints the login_id AND a short pairing code shown only here
    # (never in the URL). The browser must echo the code back before it finishes, so a login you didn't
    # start — someone mailing you a /login?cli=… link — can't be approved into a token for them. If the
    # server is too old to know /start, fall back to a locally-minted id (no code) so login still works.
    code = None
    try:
        st = httpx.post(f"{base}/auth/cli/start", headers={"ngrok-skip-browser-warning": "1"}, timeout=10)
        if st.status_code == 200:
            j = st.json(); lid = j["login_id"]; code = j.get("code")
        else:
            lid = _secrets.token_urlsafe(18)
    except Exception:
        lid = _secrets.token_urlsafe(18)
    url = f"{base}/login?cli={lid}"
    print(f"Opening your browser to sign in…\nIf it doesn't open, visit:\n  {url}\n")
    if code:
        print(f"  Enter this code in the browser to confirm it's you:  {_B}{_TEAL}{code}{_R}\n")
    print("Waiting for authorization…")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    for _ in range(180):  # ~3 min
        time.sleep(1)
        try:
            d = httpx.get(f"{base}/auth/cli/poll",
                          params={"login_id": lid}, headers={"ngrok-skip-browser-warning": "1"}, timeout=10).json()
        except Exception:
            continue
        if d.get("token"):
            cfg.update(token=d["token"], email=d.get("email"), identity=True)
            if d.get("active_org"):
                cfg["active_org"] = d["active_org"]  # the team the user picked in the browser
            _save_config(cfg)  # persist first; the login_id is single-use, don't risk losing it
            if not cfg.get("active_org"):
                _pick_active_org(cfg)  # older server (no picker) — fall back to guessing
            print(f"✓ Logged in as {cfg['email']}. Active org: {cfg.get('active_org')}")
            _maybe_offer_onboarding(cfg)
            return
    sys.exit("Login timed out — run `treg login` again.")


def cmd_logout(args, cfg) -> None:
    cfg.update(token=None, email=None, active_org=None, identity=False)
    _save_config(cfg)
    print("Logged out.")


# ---- onboarding: a guided first-run — pick a style, in colour --------------------------------
def _c(code: str) -> str:  # emit ANSI only to a real terminal that hasn't opted out
    return code if (sys.stdout.isatty() and not os.environ.get("NO_COLOR")) else ""
_A = _c("\033[38;2;224;112;63m")   # clay accent   _G green   _M muted   _TEAL token   _AM amber tip
_G, _M, _TEAL = _c("\033[38;2;127;174;114m"), _c("\033[38;2;169;158;136m"), _c("\033[38;2;95;158;160m")
_AM = _c("\033[38;2;208;162;74m")
_B, _R = _c("\033[1m"), _c("\033[0m")

# Interactive-picker chrome: a clean ❯ cursor + ○/● markers, plain rows. We deliberately DON'T style
# `highlighted` (pointed row) or `selected` (ticked row) — a foreground colour there gets rendered as a
# reverse-video BACKGROUND BAR, which is the heavy look we're avoiding. Only the cursor/qmark are tinted.
def _picker_style():
    import questionary
    return questionary.Style([
        ("qmark", "fg:#e0703f bold"),        # leading ?
        ("pointer", "fg:#e0703f bold"),      # the ❯ cursor
        ("instruction", "fg:#a99e88"),       # the hint line
        ("selected", "noreverse"),           # ticked (●) row: plain text, NO reverse-video bar
        ("highlighted", "noreverse"),        # pointed row: plain, no bar either
    ])

def _checkbox(message: str, choices, **kw):
    import questionary
    return questionary.checkbox(message, choices=choices, pointer="❯", style=_picker_style(),
                                instruction="↑↓ move, space select, enter confirm", **kw)

def _select(message: str, choices, **kw):
    import questionary
    return questionary.select(message, choices=choices, pointer="❯", style=_picker_style(),
                              instruction="↑↓ move, enter confirm", **kw)

def _brand(sub: str) -> None: print(f"\n{_A}{_B}▚ tools-registry{_R} {_M}— {sub}{_R}")
def _ok(t: str) -> None: print(f"  {_G}✓{_R} {t}")
def _dim(t: str) -> None: print(f"{_M}{t}{_R}")
def _kv(k: str, v: str) -> None: print(f"  {_M}{k:<7}{_R}{v}")


def _pause(yes: bool) -> None:
    if yes or not sys.stdin.isatty():
        return
    try:
        input(f"  {_M}↵ enter to continue…{_R}")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)


def _onboard_active_org(cfg: dict) -> dict | None:
    """The active org's summary {slug,name,role,tool_count} — drives the onboarding hint + guards.
    None if the caller has no team (a path then points them at `treg org create` / an invite)."""
    try:
        with _client(cfg) as c:
            orgs = c.get("/orgs").json()
    except Exception:  # noqa: BLE001 — a transient failure just means "no smart hint"
        return None
    if not isinstance(orgs, list) or not orgs:
        return None
    active = cfg.get("active_org")
    return (next((o for o in orgs if o.get("slug") == active), None)
            or next((o for o in orgs if o.get("active")), None) or orgs[0])


_PATHS = {"1": "setup", "2": "access", "3": "demo"}


def _pick_path(cfg: dict) -> str:
    """The 3-path onboarding menu (Set up / Access / Demo) with a smart default from the active org:
    a team with tools → Access; an empty team you admin → Set up; else Demo."""
    org = _onboard_active_org(cfg)
    has_tools = bool(org and org.get("tool_count"))
    is_admin = bool(org and org.get("role") in ("admin", "owner"))
    default = "2" if has_tools else ("1" if is_admin else "3")
    if not sys.stdin.isatty():
        return _PATHS[default]
    print(f"\n{_B}What do you want to do?{_R}")
    print(f"  {_A}{_B}1{_R}  {_B}Setup{_R}                        {_M}— upload your skills & env, share them safely (admins){_R}")
    print(f"  {_A}{_B}2{_R}  {_B}Connect existing tool-registry{_R}   {_M}— pull your team's shared skills + make a call{_R}")
    print(f"  {_A}{_B}3{_R}  {_B}Demo{_R}                         {_M}— see how treg works with a throwaway team{_R}")
    hint = {"1": "Setup", "2": "Connect", "3": "Demo"}[default]
    ans = input(f"  Pick [{_A}1{_R}/2/3]  ({_M}↵ = {hint}{_R}): ").strip()
    return _PATHS.get(ans or default, "demo")


def _maybe_offer_onboarding(cfg: dict) -> None:
    """After a first HUMAN login, offer onboarding — skippable, TTY-only, asked just once."""
    if not sys.stdin.isatty():
        return
    base = cfg["base_url"].rstrip("/")
    try:
        me = httpx.get(f"{base}/auth/me", headers={"X-Treg-Token": cfg["token"], "ngrok-skip-browser-warning": "1"}, timeout=10).json()
    except Exception:
        return
    if me.get("onboarded"):
        return
    ans = input(f"\n{_A}✨ New here?{_R} Want a quick setup? [{_A}Y{_R}/n] ").strip().lower()
    if ans in ("n", "no"):
        with _client(cfg) as c:
            c.post("/onboard/skip")  # remember the decline so we don't ask again
        _dim("No problem — run `treg onboard` whenever you like.")
        return
    _dispatch_onboard(cfg, _pick_path(cfg), argparse.Namespace(name=None, yes=False, source=None))


def _section(title: str) -> None:
    bar = _A + "─" * 58 + _R
    print(f"\n{bar}\n {_B}{title}{_R}\n{bar}")


def _arrow(t: str) -> None:
    print(f"  {_A}→{_R} {_M}{t}{_R}")


def _cmd(s: str) -> None:  # show the actual command the user is learning
    print(f"  {_M}${_R} {_B}{_A}{s}{_R}")


def _tip(t: str) -> None:  # an amber aside
    print(f"  {_AM}✦ {t}{_R}")


@contextlib.contextmanager
def _spinner(msg: str):
    """An animated braille spinner for slow steps (health runs, seeding). TTY-only: piped/agent
    output gets one static line instead, so logs stay clean and deterministic."""
    if not sys.stdout.isatty():
        print(f"  … {msg}")
        yield
        return
    stop = threading.Event()

    def _spin() -> None:
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if stop.wait(0.08):
                break
            print(f"\r  {_A}{ch}{_R} {msg}…", end="", flush=True)

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=0.3)
        print("\r" + " " * (len(msg) + 6) + "\r", end="", flush=True)  # wipe the line


def _show_calls(cfg: dict) -> None:
    with _client(cfg) as c:
        calls = c.get("/calls", params={"limit": 6}).json()
    for cr in (calls if isinstance(calls, list) else [])[:6]:
        st = cr.get("status_code", "")
        col = _G if (isinstance(st, int) and st < 400) else _M
        print(f"   {cr.get('user_email',''):<26}{_M}{cr.get('method',''):<5}{_R}{col}{st}{_R}  {_M}{cr.get('tool_name','')}{_R}")
    _arrow("full log:  treg calls")


def _dispatch_onboard(cfg: dict, path: str, args) -> None:
    if path == "setup":
        _run_setup(cfg, args)
    elif path == "access":
        _run_access(cfg, args)
    else:
        _run_demo(cfg, args)


def _run_setup(cfg: dict, args) -> None:
    """Path 1 — Set up (admin): scan this folder's .env + skills, pick what to share, register it
    (value-internal via `treg upload`), batch health-check, then print the teammate hand-off."""
    _brand("setup — upload your skills & env, share them safely")
    org = _onboard_active_org(cfg)
    if org is None:
        _dim('You\'re not in a team yet. Create one:  treg org create "Your Team"')
        return
    _kv("team", org.get("name") or org.get("slug"))
    print("  You pick what to share; values are read internally, never on the command line.")
    from . import skills as sk
    cwd = Path(os.getcwd())

    def _has_skills(d: Path) -> bool:
        try:
            return d.is_dir() and (sk.is_skill_dir(d) or any(c.is_dir() and sk.is_skill_dir(c) for c in d.iterdir()))
        except OSError:
            return False

    from . import agents as _ag
    scanned = False

    # Where to look: this project, the machine-wide agent skill folders (~/.claude/skills,
    # ~/.codex/skills, …), or both. Cross-project skills usually live in the global folders, so
    # setup offers them — interactively when possible, via --source local|global|both otherwise.
    global_dirs: list[Path] = []
    seen_globals: set[str] = set()
    for a in _ag.detect_installed():
        d = _ag.global_dir(a)
        if str(d) not in seen_globals and _has_skills(d):
            seen_globals.add(str(d)); global_dirs.append(d)
    local_here = os.path.isfile(cwd / ".env") or _has_skills(cwd) or any(
        _has_skills(cwd / a["project"]) for a in _ag.AGENTS.values())
    source = getattr(args, "source", None)
    if source is None:
        if sys.stdin.isatty() and global_dirs and not getattr(args, "yes", False):
            default = "1" if local_here else "2"
            shown = ", ".join(str(g).replace(str(Path.home()), "~") for g in global_dirs[:3])
            print(f"\n{_B}Import from where?{_R}")
            print(f"  {_A}{_B}1{_R}  this project           {_M}— {cwd}{_R}")
            print(f"  {_A}{_B}2{_R}  global agent folders   {_M}— {shown}{'…' if len(global_dirs) > 3 else ''}{_R}")
            print(f"  {_A}{_B}3{_R}  both")
            hint = {"1": "this project", "2": "global", "3": "both"}[default]
            ans = input(f"  Pick [{_A}1{_R}/2/3]  ({_M}↵ = {hint}{_R}): ").strip()
            source = {"1": "local", "2": "global", "3": "both"}.get(ans or default, "local")
        else:  # non-interactive / --yes / nothing global to offer: keep the old local behavior
            source = "local" if (local_here or not global_dirs) else "global"
    want_local = source in ("local", "both")
    want_global = source in ("global", "both")

    # 1) THIS project's .env → API keys (one interactive pick). Global folders carry no project .env.
    if want_local and os.path.isfile(cwd / ".env"):
        imp = build_parser().parse_args(["upload", "env"]); imp.dir = str(cwd); imp.no_oauth = True
        print(f"\n  {_M}▸ API keys in this project's .env{_R}")
        cmd_import(imp, cfg); scanned = True

    # 2) ALL skill folders in ONE deduped pass — the cwd's top-level skills + every known agent's project
    #    dir (.claude/skills, .agents/skills, .roo/skills, …), plus the chosen global dirs. `skill
    #    install` mirrors a skill into several of these, so scanning them separately would prompt for the
    #    same skill repeatedly; we collect the distinct dirs and hand them to `_import_skills`, which
    #    dedupes by skill NAME → one pick.
    skill_dirs: list[str] = []
    seen_dirs: set[str] = set()
    candidates: list[Path] = []
    if want_local:
        candidates += [cwd] + [cwd / a["project"] for a in _ag.AGENTS.values()]
    if want_global:
        candidates += global_dirs
    for cand in candidates:
        key = str(cand.resolve()) if cand.exists() else str(cand)
        if key not in seen_dirs and _has_skills(cand):
            seen_dirs.add(key); skill_dirs.append(str(cand))
    if skill_dirs:
        env_path = (str(cwd / ".env") if os.path.isfile(cwd / ".env") else (_find_env_upwards(skill_dirs[0]) or str(cwd / ".env")))
        print(f"\n  {_M}▸ skills across {len(skill_dirs)} folder(s){_R}")
        imp = build_parser().parse_args(["upload", "skills"]); imp.no_oauth = True
        _import_skills(imp, cfg, skill_dirs, env_path); scanned = True

    if scanned:
        _section("Verify — one batched health run")
        try:
            with _spinner("checking each credential against its provider"), _client(cfg) as c:
                hr = c.post("/health/run").json()
            rows = hr.get("all", []) if isinstance(hr, dict) else (hr if isinstance(hr, list) else [])
            ok = [r for r in rows if r.get("status") == "ok"]
            bad = [r for r in rows if r.get("status") == "invalid"]
            unknown = [r for r in rows if r.get("status") == "unknown"]
            if ok or bad:
                extra = []
                if bad:
                    extra.append(f"{len(bad)} need attention")
                if unknown:  # not unhealthy — just no probe to validate against yet
                    extra.append(f"{len(unknown)} unchecked (no probe)")
                _ok(f"{len(ok)} credential(s) healthy" + (f" · {' · '.join(extra)}" if extra else ""))
                for r in bad:
                    print(f"   {_M}✗ {r.get('name') or r.get('secret_id')}: {r.get('detail','invalid')}{_R}")
                if unknown:
                    _dim(f"   unchecked = registered before catalog probes; re-upload to validate:  treg upload env --dir . --replace")
            else:
                # No probe → nothing to validate (not a failure). Common for tools registered before the
                # catalog gained probes, or re-runs where everything was already registered.
                _dim(f"  {len(unknown) or len(rows)} credential(s) stored, but none carry a health probe yet — nothing to validate.")
                _dim("  Add probes + validate:  treg upload env --dir . --replace   then   treg health --run")
        except Exception:  # noqa: BLE001
            _dim("  (run `treg health --run` to validate)")
    else:
        where = {"local": "this project", "global": "your global agent folders", "both": "this project or your global agent folders"}[source]
        _dim(f"  Nothing to share from {where} (no .env or skills found).")
        _dim("  cd into the repo that has your credentialed skills / .env, then re-run  treg onboard --path setup")
        if source == "local":
            _dim("  or import your machine-wide skills:  treg onboard --path setup --source global")
    base = (cfg.get("base_url") or "").rstrip("/")
    _section("✓ Done — you're all set")
    print(f"  Your team's tools & skills are shared. {_B}Nothing more to do here.{_R}\n")
    print(f"  {_M}Manage your team in the dashboard:{_R}  {_A}{base}/#orgs{_R}")
    print(f"\n  {_M}To give a TEAMMATE access — invite them:{_R}")
    _cmd("treg org invite teammate@company.com")
    print(f"\n  {_M}…then THEY run this on THEIR machine (not you) to pull the skills + call your tools with no keys:{_R}")
    _cmd(f"curl -fsSL {base}/install.sh | sh")
    _cmd("treg login   →   treg onboard   (pick Connect)")
    print()


def _run_access(cfg: dict, args) -> None:
    """Path 2 — Access (consumer): show the team's tools + skills, multi-select which skills to
    install, then make one no-key test call. Consumers never pull keys — treg injects server-side."""
    _brand("connect — your team's shared skills & tools")
    org = _onboard_active_org(cfg)
    if org is None:
        _dim("You're not in a team yet — ask an admin to invite you, then `treg accept`.")
        return
    _kv("team", org.get("name") or org.get("slug"))
    with _client(cfg) as c:
        tools = c.get("/tools").json()
        bundles = c.get("/bundles").json()
    tools = tools if isinstance(tools, list) else []
    bundles = bundles if isinstance(bundles, list) else []
    _section("① What your team shares")
    if tools:
        print(f"  {_M}tools (call any with NO key — treg injects server-side):{_R}")
        for t in tools[:15]:
            print(f"   {_A}{t['name']:<18}{_R}{_M}{t.get('host','')}{_R}")
    else:
        _dim("  no tools registered yet")
    _section("② Save skills into your agent's skills folder(s)")
    _onboard_install_skills(cfg, bundles)
    _section("③ Try one — no key on your machine")
    _onboard_test_call(cfg, tools)
    print()
    _dim("You're set — `treg tool ls` / `treg skill ls` anytime.")


def _onboard_install_skills(cfg: dict, bundles: list) -> None:
    if not bundles:
        _dim("  no shared skills yet"); return
    names = [b["name"] for b in bundles]
    chosen = names
    if sys.stdin.isatty():
        try:
            import questionary
            choices = [questionary.Choice(title=n, value=n, checked=True) for n in names]
            chosen = _checkbox("Install which skills?", choices).ask() or []
        except ImportError:
            pass  # no questionary → install all
    if not chosen:
        _dim("  none selected"); return
    # ONE call for the whole subset → one "Installed N" summary (not one per skill).
    cmd_skill_install(argparse.Namespace(dir=None, all=False, name=None, names=set(chosen), force=False), cfg)


def _testable_path(t: dict) -> tuple[str, str] | None:
    """A (path, method) that actually hits a real endpoint — an example, else a health_check probe.
    None if the tool has neither: calling its base-URL ROOT usually 404/401s (looks like a bad key)."""
    ex = (t.get("examples") or [None])[0]
    if ex and ex.get("path"):
        return ex["path"].lstrip("/"), (ex.get("method") or "GET")
    hc = t.get("health_check") or {}
    if hc.get("path"):
        return str(hc["path"]).lstrip("/"), (hc.get("method") or "GET")
    return None


def _onboard_test_call(cfg: dict, tools: list) -> None:
    if not tools:
        _dim("  nothing to call yet"); return
    # Prefer tools with a KNOWN-GOOD path — a bare root call 404/401s and looks like a bad credential.
    callable_tools = [t for t in tools if _testable_path(t)]
    pool = callable_tools or tools
    tool = pool[0]
    if sys.stdin.isatty() and len(pool) > 1:
        try:
            import questionary
            tool = _select("Test which tool?", [questionary.Choice(t["name"], t) for t in pool]).ask() or tool
        except ImportError:
            pass
    tp = _testable_path(tool)
    if tp is None:
        _cmd(f"treg call {tool['name']} <path>")
        _dim(f"  ({tool['name']} has no known test path — its root usually isn't a valid endpoint; "
             "pick a real path from its docs)"); return
    path, method = tp
    _cmd(f"treg call {tool['name']} {path}".rstrip())
    try:
        with _client(cfg) as c:
            r = c.request(method, f"/call/{tool['name']}/{path}".rstrip("/"))
        col = _G if r.status_code < 400 else _M
        print(f"  → {col}{r.status_code}{_R} — treg injected the credential; you never held it.")
    except Exception as exc:  # noqa: BLE001
        _dim(f"  (call failed: {exc})")


def _demo_scan_preview(cfg: dict) -> None:
    """Read-only: show what treg WOULD detect to share (API keys + skills). A DEMO — nothing is uploaded."""
    from . import providers as prov, skills as sk
    cwd = Path(os.getcwd())
    env_path = cwd / ".env"
    keys: list[str] = []
    if env_path.is_file():
        try:
            keys = [a.tool_name for a in prov.plan_actions(prov.scan_env(str(env_path), _load_catalog(cfg))) if a.supported]
        except Exception:  # noqa: BLE001
            pass
    skill_names: set[str] = set()
    for cand in [cwd, cwd / ".claude" / "skills", cwd / ".agents" / "skills"]:
        try:
            if cand.is_dir():
                for det in sk.scan_skills(str(cand)):
                    skill_names.add(det.name)
        except Exception:  # noqa: BLE001
            pass
    if keys:
        print(f"  {_M}API keys in your .env treg could share:{_R} {', '.join(keys[:8])}"
              + (f" +{len(keys)-8} more" if len(keys) > 8 else ""))
    if skill_names:
        print(f"  {_M}skills in this project:{_R} {len(skill_names)}")
    if not keys and not skill_names:
        _dim("  (no .env or skills here — in a real project treg detects your keys + skill folders)")
    print()
    _dim("  This is just a DEMO — nothing is uploaded. The 'Upload' path is where you actually share.")


def _demo_teammate_call(cfg: dict) -> str | None:
    """Auto-pick ONE registered tool and make a REAL call, shown as the actual upstream API endpoint
    (URL-passthrough form) so it's unmistakably a real API. Falls back to a Stripe example if the team
    has no callable tool yet. Returns the tool name that was called (for the audit-log illustration)."""
    with _client(cfg) as c:
        raw = c.get("/tools").json()
    tools = [t for t in (raw if isinstance(raw, list) else []) if t.get("name") != "echo" and _testable_path(t)]
    print(f"  {_M}A teammate on THEIR machine — no key on it — hits a REAL API through treg:{_R}")
    if not tools:  # nothing callable yet → a recognizable Stripe example (illustrative, not executed)
        _cmd("treg call https://api.stripe.com/v1/balance")
        _dim("  → treg would inject your Stripe key server-side and relay Stripe's real response — the teammate never sees the key.")
        return None
    tool = tools[0]
    path, method = _testable_path(tool)
    endpoint = f"{tool['base_url'].rstrip('/')}/{path}"
    _cmd(f"treg call {endpoint}")   # DISPLAY the real upstream URL so it's clearly a real API…
    try:
        with _client(cfg) as c:     # …but EXECUTE via the tool name (reliable; the host-passthrough form
            r = c.request(method, f"/call/{tool['name']}/{path}".rstrip("/"))  # can be ambiguous w/ dup hosts)
        col = _G if r.status_code < 400 else _M
        print(f"  → {col}{r.status_code}{_R} — a real response from {tool.get('host') or tool['name']}. "
              "treg injected the key server-side; the teammate never held it.")
    except Exception as exc:  # noqa: BLE001
        _dim(f"  (call failed: {exc})")
    return tool["name"]


def _demo_call_log(cfg: dict, called: str | None) -> None:
    """An illustrative audit log: the real call you just made, plus example teammates (on YOUR email
    domain, so they read as real) calling other shared tools. The real ledger is `treg calls`."""
    me = cfg.get("email") or "you@company.com"
    dom = me.split("@", 1)[1] if "@" in me else "company.com"
    rows = [(me, "GET", 200, called or "stripe"),
            (f"alex@{dom}", "GET", 200, "render"),
            (f"ben@{dom}", "POST", 200, "intercom"),
            (f"cora@{dom}", "GET", 200, "gsc")]
    for email, method, st, tool in rows:
        print(f"   {email:<26}{_M}{method:<5}{_R}{_G}{st}{_R}  {_M}{tool}{_R}")
    _arrow("your real ledger:  treg calls")


def _demo_next_steps(cfg: dict) -> None:
    base = (cfg.get("base_url") or "").rstrip("/")
    _section("That's the loop")
    print("  Detect → share (no key leaves the server) → teammates call → every call logged.\n")
    _kv("do it", "treg onboard   →   Setup (share yours) · Connect (use the team's)")
    _kv("learn", f"{base}/tutorial")
    print()


def _run_demo(cfg: dict, args) -> None:
    """Path 3 — Demo: an illustrative walkthrough. NO team is created, NOTHING is uploaded. It shows the
    loop: ① what you could share → ② sharing gives each teammate a role → ③ a teammate calling a service
    with no key → ④ the audit log. A real call is made when the active team already has a callable tool."""
    yes = getattr(args, "yes", False)
    _brand("demo — the whole loop (a walkthrough; nothing is changed)")

    _section("① Auto-discover local skills & env")
    _demo_scan_preview(cfg)
    _pause(yes)

    _section("② Share credentials & skills with your team")
    print("  Share once, and every teammate gets a role — they use your tools, never your keys:")
    for role, who, note in [("owner", "you", "(you)"), ("admin", "Alex", "example teammate"),
                            ("member", "Ben", "example teammate"), ("viewer", "Cora", "example teammate")]:
        print(f"   {_A}{role:<7}{_R}{who:<8} {_M}{note}{_R}")
    _pause(yes)

    _section("③ A teammate calls a service — without your key")
    called = _demo_teammate_call(cfg)
    _pause(yes)

    _section("④ Every call is on the record")
    _demo_call_log(cfg, called)
    _pause(yes)
    _demo_next_steps(cfg)


def cmd_onboard(args, cfg) -> None:
    if args.reset:
        if not cfg.get("token"):
            sys.exit("Log in first:  treg login")
        with _client(cfg) as c:
            _show(c.post("/onboard/reset"))
        return
    if not cfg.get("token"):
        sys.exit("Log in first:  treg login")
    if not cfg.get("active_org"):
        _pick_active_org(cfg)  # identity token needs an active org so requests carry X-Treg-Org
    path = args.path or ("demo" if args.mode == "quick" else None) or _pick_path(cfg)  # --mode kept for back-compat
    _dispatch_onboard(cfg, path, args)


def cmd_invites(args, cfg) -> None:
    """Invites addressed to YOU (your proven email) — the code-free door."""
    with _client(cfg) as c:
        _show(c.get("/invites/mine"))


def cmd_accept(args, cfg) -> None:
    """Accept an invite addressed to you by org slug (or invite id) — no code needed."""
    with _client(cfg) as c:
        mine = c.get("/invites/mine")
        if mine.status_code != 200:
            _show(mine)
            return
        inv = next((i for i in mine.json() if i["org"] == args.org or str(i["id"]) == args.org), None)
        if inv is None:
            sys.exit(f"no pending invite for '{args.org}' — run `treg invites`")
        r = c.post(f"/invites/{inv['id']}/accept")
        if r.status_code == 200:
            cfg["active_org"] = inv["org"]
            _save_config(cfg)
        _show(r)


# ---- secrets ------------------------------------------------------------------------------
def cmd_secret_add(args, cfg) -> None:
    src_file = None
    if getattr(args, "env_var", None):
        # Read ONE named var from an .env using treg's own parser (strips a balanced quote pair,
        # handles `export `) — the correct, value-internal way to register an unmatched key. The agent
        # never hand-extracts (which kept the surrounding quotes → a malformed secret) and the value
        # never lands on the command line.
        from . import providers as prov
        env_file = args.env_file or os.path.join(os.getcwd(), ".env")
        if not os.path.isfile(env_file):
            sys.exit(f"no .env at {env_file} (use --env-file PATH)")
        vals = prov.env_values(env_file, [args.env_var])
        value = vals.get(args.env_var)
        if not value:
            sys.exit(f"{args.env_var} not found (or empty) in {env_file}")
    elif args.dir:
        from .convert import find_secret_file
        try:
            src_file = find_secret_file(args.dir, args.kind)
        except (FileNotFoundError, ValueError) as exc:  # "no X secret" / "ambiguous — use --file"
            sys.exit(str(exc))
        print(f"[using {src_file}]", file=sys.stderr)
        value = src_file.read_text().strip()  # a trailing newline would become an illegal header value
    elif args.file:
        value = Path(args.file).read_text().strip()
    elif args.value is not None:
        value = args.value
    else:
        sys.exit("provide --value, --env-var, --file, or --dir")
    with _client(cfg) as c:
        r = c.post("/secrets", json={"name": args.name, "value": value, "kind": args.kind})
    if r.status_code == 200 and args.dir:
        _sync_contract_secret(args.dir, src_file, args.name, args.kind)
    _show(r)


def _sync_contract_secret(skill_dir, src_file, name: str, kind: str) -> None:
    # Runs AFTER the secret is already created server-side — a sync hiccup must never turn a
    # successful command into a traceback/exit-1, so downgrade any failure here to a warning.
    from .convert import CONTRACT_FILE
    path = Path(skill_dir) / CONTRACT_FILE
    if not path.exists() or src_file is None:
        return
    try:
        contract = json.loads(path.read_text())
        rel = str(Path(src_file).resolve().relative_to(Path(skill_dir).resolve()))
        secrets = contract.setdefault("secrets", [])
        entry = next((s for s in secrets if s.get("file") == rel), None)
        if entry is None:
            secrets.append({"file": rel, "name": name, "kind": kind})
        else:
            entry["name"], entry["kind"] = name, kind
        path.write_text(json.dumps(contract, indent=2))
        print(f"[synced {CONTRACT_FILE}]", file=sys.stderr)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"[warning: could not sync {CONTRACT_FILE}: {exc}]", file=sys.stderr)


def cmd_secret_ls(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.get("/secrets"))


def cmd_secret_rm(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.delete(f"/secrets/{args.id}"))


def cmd_secret_update(args, cfg) -> None:
    body = {k: v for k, v in (("name", args.name), ("value", args.value), ("kind", args.kind)) if v is not None}
    if not body:
        sys.exit("nothing to update (use --name / --value / --kind)")
    with _client(cfg) as c:
        _show(c.patch(f"/secrets/{args.id}", json=body))


# ---- import: scan an env file, auto-register detected providers as tools ------------------
def _import_select(supported: list, args) -> list:
    """Pick which detected providers to register: --select <names>, --all / non-TTY = all, else an
    interactive checkbox (questionary). Returns the chosen Action list."""
    if args.select:
        want = {s.strip().lower() for s in args.select.split(",")}
        return [a for a in supported if a.tool_name in want or (a.detection.provider or "").lower() in want]
    if args.all or args.dry_run:
        return supported
    if not sys.stdin.isatty():   # never silently import credentials unattended (agents/CI) — require intent
        sys.exit("non-interactive: pass --all to import everything, --select to choose, or --dry-run to preview")
    try:
        import questionary
    except ImportError:
        print("[questionary not installed — registering all detected; use --select to choose]", file=sys.stderr)
        return supported
    choices = [questionary.Choice(title=f"{a.tool_name:<14} {a.base_url}", value=a, checked=True) for a in supported]
    picked = _checkbox("Providers detected in your env — register which as tools?", choices).ask()
    return picked or []


def _load_catalog(cfg) -> list:
    """The provider catalog for detection: refresh from the registry's GET /providers.json (so a
    provider added server-side reaches every CLI), caching it; fall back to the cache, then to the
    bundled CATALOG when offline. Keeps `treg upload` working with no server + always up to date with one."""
    import hashlib
    from . import providers as prov
    # Key the cache by base_url — different deployments ship different catalogs; don't serve server A's
    # cached catalog when pointed at server B.
    tag = hashlib.sha1((cfg.get("base_url") or "").encode()).hexdigest()[:10]
    cache = CONFIG_PATH.parent / f"providers-cache-{tag}.json"
    try:
        with _client(cfg, auth=False) as c:
            r = c.get("/providers.json")
        body = r.json() if r.status_code == 200 else {}
        if body.get("providers"):
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(r.text)
            except OSError:
                pass
            # Prefer the NEWER catalog: a CLI updated ahead of its server (mid-deploy) must NOT regress to
            # the server's older one — that loses new CLIs + fields (auth_mechanism/detect) and degrades
            # classification. Server wins on ties (it's canonical + can grow without a CLI release).
            if int(body.get("version") or 0) >= prov.CATALOG_VERSION:
                return body["providers"]
    except Exception:
        pass
    try:
        if cache.exists():
            cached = json.loads(cache.read_text())
            if cached.get("providers") and int(cached.get("version") or 0) >= prov.CATALOG_VERSION:
                return cached["providers"]
    except (OSError, json.JSONDecodeError):
        pass
    return prov.CATALOG  # bundled — the floor; used when it's newer than (or as new as) the server/cache


def _find_env_upwards(start: str) -> str | None:
    """The nearest .env walking up from `start`. A skills dir (`./.claude/skills`) usually sits UNDER a
    project whose `.env` is at the root, so a skill whose credential is an env var (render/vercel — no
    local `.secrets/`) would otherwise be gapped "needs env var … not found" and skipped as a bundle."""
    d = os.path.abspath(start)
    for _ in range(8):  # cap the walk so a stray dir can't scan forever
        cand = os.path.join(d, ".env")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def cmd_import(args, cfg) -> None:
    """Dispatch to env-import and/or skill-import. Bare `treg upload` does BOTH for the target dir;
    `treg upload env` / `treg upload skills` restrict it. `treg scan` is the read-only preview
    (forces dry_run). Location: --dir (default cwd)."""
    from . import skills as sk
    if getattr(args, "cmd", None) == "import":
        print("note: `treg import` is now `treg upload` (same command; preview with `treg scan`)", file=sys.stderr)
    base_dir = args.dir or os.getcwd()
    env_path = args.env_file or os.path.join(base_dir, ".env")
    skills_dir = args.skills_dir or base_dir
    mode = args.mode  # "env" | "skills" | None (both)
    have_env = os.path.isfile(env_path)
    have_skills = False
    if os.path.isdir(skills_dir):
        p = Path(skills_dir)
        try:
            have_skills = sk.is_skill_dir(p) or any(c.is_dir() and sk.is_skill_dir(c) for c in p.iterdir())
        except OSError:   # unreadable dir → treat as no skills, not a traceback
            have_skills = False

    ran = False
    if mode in (None, "env"):
        if have_env:
            _import_env(args, cfg, env_path); ran = True
        elif mode == "env":
            sys.exit(f"no .env at {env_path} (use --dir DIR or --env-file FILE)")
    if mode in (None, "clis"):  # scan the machine for installed catalog CLIs → register + report
        if ran:
            print()
        _import_clis(args, cfg, env_path); ran = True
    if mode in (None, "skills"):
        if have_skills:
            if ran:
                print()
            # A skill's env credential lives in the PROJECT .env, which may sit ABOVE the skills dir
            # (`treg upload skills --dir ./.claude/skills` from a repo root). Resolve it by walking up,
            # so env-credentialed skills (render/vercel) register as bundles instead of being gapped.
            skills_env = env_path if os.path.isfile(env_path) else (args.env_file or _find_env_upwards(skills_dir) or env_path)
            _import_skills(args, cfg, skills_dir, skills_env); ran = True
        elif mode == "skills":
            sys.exit(f"no skills (subdirs with SKILL.md) under {skills_dir}")
    if not ran:
        verb = "scan" if getattr(args, "as_scan", False) else "upload"
        sys.exit(f"nothing to {verb} in {base_dir}: no .env, no skill subdirs. Use --dir / --skills-dir.")


def _import_env(args, cfg, env_path: str) -> None:
    from . import providers as prov
    # --dry-run honors its no-network promise (bundled catalog); a real run refreshes from the server.
    catalog = prov.CATALOG if args.dry_run else _load_catalog(cfg)
    detections = prov.scan_env(env_path, catalog)
    actions = prov.plan_actions(detections)
    supported = [a for a in actions if a.supported]
    oauth_dets = [d for d in detections if d.kind == "oauth_pair" and prov.oauth_ready(d)]
    # deferred = the rest, minus the oauth pairs we now handle via the connect loop below.
    deferred = [a for a in actions if not a.supported
                and not (a.detection.kind == "oauth_pair" and prov.oauth_ready(a.detection))]
    print(f"Scanned {env_path}: {len(supported)} key(s) to register, {len(oauth_dets)} OAuth, {len(deferred)} other.\n")

    chosen = _import_select(supported, args) if supported else []
    if args.select and supported and not chosen:
        print(f"  (no detected provider key matched --select {args.select})")

    if args.dry_run:
        scan = getattr(args, "as_scan", False)
        if chosen:
            print("Found — `treg upload` registers these:" if scan else "DRY RUN — would register:")
            for a in chosen:
                b = a.binding or {}
                print(f"  ✓ {a.tool_name:<14} {a.base_url}   [{b.get('name')}: {b.get('format')}]  (secret {a.secret_name})")
        if oauth_dets:
            print("\nFound OAuth pairs — `treg upload` connects them one by one:" if scan
                  else "\nDRY RUN — would prompt to connect one by one:")
            for d in oauth_dets:
                print(f"  ◆ {d.provider:<14} {' + '.join(d.vars)}")
        if args.llm:
            unknowns = [d for d in detections if d.kind == "unknown_secret"]
            if unknowns:
                print(f"\nDRY RUN — would ask {args.llm_model} to resolve: " + ", ".join(d.vars[0] for d in unknowns))
        _import_show_skipped(deferred)
        return

    with _client(cfg) as c:
        if chosen:
            need = []
            for a in chosen:
                need += list(a.combine) if a.combine else ([a.secret_name] if a.secret_name else [])
            values = prov.env_values(env_path, need)
            # Existing tools/secrets, to stay idempotent on re-run (don't create an orphan secret when
            # the tool then 409s). {name: id} maps for --replace deletes. (Fetch each ONCE.)
            _rt, _rs = c.get("/tools"), c.get("/secrets")
            existing_tools = {t["name"]: t["id"] for t in (_rt.json() if _rt.status_code == 200 else [])}
            existing_secrets = {s["name"]: s["id"] for s in (_rs.json() if _rs.status_code == 200 else [])}
            ok = 0
            for a in chosen:
                if a.combine:   # basic pair: base64(username:password) → one secret, "Basic {secret}" binding
                    import base64
                    uvar, pvar = a.combine
                    u, pw = values.get(uvar), values.get(pvar)
                    if not (u and pw):
                        print(f"  ✗ {a.tool_name}: {uvar}/{pvar} missing in the env — skipped"); continue
                    val = base64.b64encode(f"{u}:{pw}".encode()).decode()
                else:
                    val = values.get(a.secret_name)
                    if not val:
                        print(f"  ✗ {a.tool_name}: {a.secret_name} has no value in the env — skipped"); continue
                if a.tool_name in existing_tools:               # already registered → skip (or replace) BEFORE writing a secret
                    if not args.replace:
                        print(f"  · {a.tool_name}: already registered (use --replace)")
                        print(f"    ↗ {_detail_url(cfg, 'tool', a.tool_name)}"); continue
                    c.delete(f"/tools/{existing_tools[a.tool_name]}")
                    if a.secret_name in existing_secrets:
                        c.delete(f"/secrets/{existing_secrets[a.secret_name]}")
                elif a.secret_name in existing_secrets:         # stale orphan secret from a prior failed run
                    if args.replace:
                        c.delete(f"/secrets/{existing_secrets[a.secret_name]}")
                rs = c.post("/secrets", json={"name": a.secret_name, "value": val, "kind": "env"})
                if rs.status_code >= 400:
                    print(f"  ✗ {a.tool_name}: secret failed ({rs.status_code}) {rs.text[:100]}"); continue
                sid = rs.json().get("id") or rs.json().get("secret_id")
                binding = {**a.binding, "secret_id": sid}
                tool_body = {"name": a.tool_name, "base_url": a.base_url, "bindings": [binding]}
                if a.health:  # a catalog probe → the tool self-validates on `health --run` + gives a real test path
                    tool_body["health_check"] = a.health
                rt = c.post("/tools", json=tool_body)
                if rt.status_code >= 400:
                    print(f"  ✗ {a.tool_name}: tool failed ({rt.status_code}) {rt.text[:100]}"); continue
                print(f"  ✓ {a.tool_name:<14} {a.base_url}")
                print(f"    ↗ {_detail_url(cfg, 'tool', a.tool_name)}"); ok += 1
            print(f"\nRegistered {ok}/{len(chosen)} tools.")
        if oauth_dets:
            if not args.no_oauth and sys.stdin.isatty():
                _import_oauth_loop(c, oauth_dets, env_path)
            else:  # --no-oauth (e.g. onboarding) or non-interactive: mention, never auto-launch the browser
                provs = ", ".join(d.provider or "?" for d in oauth_dets)
                print(f"\n{len(oauth_dets)} OAuth app(s) detected ({provs}) — not connected. "
                      f"Connect when ready:  treg oauth connect <name>")
        unknowns = [d for d in detections if d.kind == "unknown_secret"]
        if args.llm and unknowns:
            _import_llm(c, unknowns, env_path, args)
    _import_show_skipped(deferred)


def _import_clis(args, cfg, env_path: str) -> None:
    """Scan the machine for INSTALLED catalog CLIs, classify each (server-injectable / local / gap), and
    register the ready ones on the right tier — server-side when treg can hold the key, local when the
    credential lives in the CLI's own config. Prints an actionable report; fix a gap and re-run. `--status`
    (or `--dry-run`) reports without registering. See docs/CLI-AUTOIMPORT-PLAN.md."""
    import shutil
    from . import providers as prov
    if getattr(args, "add", None):  # phase 3: register an UNKNOWN (non-catalog) installed CLI
        return _import_add_cli(args, cfg)
    catalog = prov.CATALOG if args.dry_run else _load_catalog(cfg)
    env_vals = prov.env_values(env_path, prov.var_names(env_path)) if os.path.isfile(env_path) else {}

    def _val(name):  # the credential value, from the process env or the project .env
        return (os.environ.get(name) if name else None) or (env_vals.get(name) if name else None)

    def _logged_in(cli):  # a login-config file present ⇒ the CLI is authenticated on this machine
        return any(os.path.exists(os.path.expanduser(p)) for p in (cli.get("detect") or {}).get("config_paths", []))

    scanned = []  # (entry, cli, decision, envvar)
    for entry in catalog:
        cli = entry.get("cli")
        if not cli or not cli.get("bin"):
            continue
        d = prov.classify_cli(entry, installed=shutil.which(cli["bin"]) is not None,
                              secret_present=bool(_val(prov.cli_env_var(cli))), logged_in=_logged_in(cli))
        scanned.append((entry, cli, d, prov.cli_env_var(cli)))

    ready = [x for x in scanned if x[2]["status"] == "ready"]
    report_only = args.status or args.dry_run
    registered = []  # (name, tier, result)
    if ready and not report_only:
        with _client(cfg) as c:
            existing = {t["name"]: t["id"] for t in (c.get("/tools").json() if c.get("/tools").status_code == 200 else [])}
            for entry, cli, d, envvar in ready:
                name = cli["bin"].replace("_", "-")
                if name in existing and not args.replace:
                    registered.append((name, d["tier"], "exists")); continue
                if name in existing:  # --replace: delete-then-recreate
                    c.delete(f"/tools/{existing[name]}")
                registered.append((name, d["tier"], _register_cli_tool(c, entry, cli, d, envvar, _val)))
    _print_cli_report(scanned, registered, report_only, args.status, cfg)


def _register_cli_tool(c, entry, cli, decision, envvar, val_getter) -> str:
    """Register ONE ready CLI as a tool with its `cli` profile enabled. Server tier: store the key + bind
    it (so the API tool AND server-injected runs both work). Local tier: secret-less (the CLI reads its
    own config on the member's machine). Returns 'ok' or a short error string."""
    from . import providers as prov
    name = cli["bin"].replace("_", "-")
    profile = {k: v for k, v in cli.items() if k != "verified"}  # runtime profile; owner-enabled by this import
    profile["enabled"] = True
    bindings = []
    if decision["tier"] == "server":
        val = val_getter(envvar)
        if not val:
            return f"no value for {envvar}"
        rs = c.post("/secrets", json={"name": f"{name}-key", "value": val, "kind": "env"})
        if rs.status_code >= 400:
            return f"secret failed ({rs.status_code})"
        sid = rs.json().get("id") or rs.json().get("secret_id")
        binding = prov.build_binding(entry.get("auth") or {})
        if binding:  # an HTTP binding → the sole bound secret resolves the cli inject too
            bindings = [{**binding, "secret_id": sid}]
        else:  # no HTTP shape → point the inject at the secret directly
            profile["inject"] = [{**e, "secret_id": sid} for e in (profile.get("inject") or [])]
    else:  # local: no secret to hold — inject NOTHING so the run just execs the (self-authenticating) bin.
        # Store an EXPLICIT empty inject (not a pop): effective_profile merges the catalog profile back
        # over tool.cli at grant time, so a missing inject key would let the catalog's inject (e.g. gh's
        # GH_TOKEN) leak in and fail to resolve. An empty list overrides it — treg injects nothing.
        profile["inject"] = []
    rt = c.post("/tools", json={"name": name, "base_url": entry["base_url"], "bindings": bindings, "cli": profile})
    return "ok" if rt.status_code < 400 else f"tool failed ({rt.status_code}) {rt.text[:80]}"


def _import_add_cli(args, cfg) -> None:
    """Register an INSTALLED CLI that isn't in the catalog (phase 3). Prompts for the key env var (blank =
    it authenticates via its own login → local tool) and the provider API base_url, registers it enabled,
    and prints a catalog-entry snippet to share so it can be added for everyone."""
    import shutil
    from . import providers as prov
    bin_ = args.add.strip()
    if not shutil.which(bin_):
        sys.exit(f"'{bin_}' is not on your PATH — install it first (or check the name).")
    if any((e.get("cli") or {}).get("bin") == bin_ for e in _load_catalog(cfg)):
        sys.exit(f"'{bin_}' is already in the catalog — just run `treg upload clis`.")
    envvar = (args.env if args.env is not None else
              input(f"Env var {bin_} reads its key from (blank = it logs in via its own config): ").strip()) or None
    base_url = args.base_url or input(f"Provider API base_url for {bin_} (e.g. https://api.example.com): ").strip()
    if not base_url:
        sys.exit("a base_url is required to register the tool (a CLI with no HTTP API isn't supported via --add yet).")
    name = bin_.replace("_", "-")
    mech = "env" if envvar else "config_file"
    profile = {"bin": bin_, "enabled": True, "auth_mechanism": mech}
    with _client(cfg) as c:
        existing = {t["name"]: t["id"] for t in (c.get("/tools").json() if c.get("/tools").status_code == 200 else [])}
        if name in existing:
            if not args.replace:
                sys.exit(f"a tool named '{name}' already exists (use --replace).")
            c.delete(f"/tools/{existing[name]}")
        bindings = []
        if envvar:
            val = os.environ.get(envvar)
            if val:  # the key is in the env → store + inject server-side
                rs = c.post("/secrets", json={"name": f"{name}-key", "value": val, "kind": "env"})
                if rs.status_code >= 400:
                    sys.exit(f"secret failed ({rs.status_code}) {rs.text[:100]}")
                sid = rs.json().get("id") or rs.json().get("secret_id")
                profile["inject"] = [{"via": "env", "name": envvar, "secret_id": sid}]
                bindings = [{"via": "header", "name": "Authorization", "format": "Bearer {secret}", "secret_id": sid}]
            else:  # env var named but not set → register the profile; user sets it, re-run tools work
                profile["inject"] = [{"via": "env", "name": envvar}]
                print(f"  note: {envvar} isn't set — the tool is registered; set it before running server-side.")
        rt = c.post("/tools", json={"name": name, "base_url": base_url, "bindings": bindings, "cli": profile})
        if rt.status_code >= 400:
            sys.exit(f"tool failed ({rt.status_code}) {rt.text[:120]}")
    # An unknown bin is NOT on the server allow-list (the RCE guard), so it runs LOCALLY; server-run needs
    # an admin to allow-list the bin. If a key was bound it's also a callable HTTP tool.
    print(f"✓ Registered '{name}'. Run it locally: `treg run {name}`.")
    print(f"  ↗ {_detail_url(cfg, 'tool', name)}")
    if bindings:
        print(f"  (key stored — also a callable API tool; to run '{bin_}' on the SERVER too, an admin adds it to TREG_RUN_ALLOWED_BINS.)")
    import json as _json
    entry = {"provider": name.title(), "tokens": [envvar.split("_")[0]] if envvar else [], "base_url": base_url,
             "auth": {"shape": "bearer"},
             "cli": {"bin": bin_, "auth_mechanism": mech, **({"inject": [{"via": "env", "name": envvar}]} if envvar else {})}}
    print("\nShare this to add it to the catalog for everyone:\n  " + _json.dumps(entry))


def _print_cli_report(scanned, registered, report_only, verbose, cfg=None) -> None:
    """Group the scan into an actionable report: what's ready (and where it runs), what needs a key or a
    login (with the exact next step), what isn't supported, and how many catalog CLIs aren't installed."""
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for row in scanned:
        buckets[row[2]["status"]].append(row)
    def _list(rows):  # one bin per line, sorted — a plain, scannable list (no emoji, no colour)
        for b in sorted(c["bin"] for _, c, _, _ in rows):
            print(f"  {b}")

    installed = sum(len(buckets[s]) for s in ("ready", "needs_key", "needs_login", "unsupported"))
    print(f"Scanned {len(scanned)} catalog CLIs — {installed} installed here.\n")
    server = [r for r in buckets["ready"] if r[2]["tier"] == "server"]
    local = [r for r in buckets["ready"] if r[2]["tier"] == "local"]
    verb = "Would register" if report_only else "Registered"
    if server:
        print(f"{verb} (server, key injected):")
        _list(server)
    if local:
        print(f"{verb} (local, uses your login):")
        _list(local)
    if cfg and not report_only:  # each registered CLI's shareable page (send the link to share it)
        for n, _t, r in registered:
            if r in ("ok", "exists"):
                print(f"  ↗ {_detail_url(cfg, 'tool', n)}")
    if buckets["needs_key"] or buckets["needs_login"]:
        print("\nNeeds setup before it can register:")
        for _e, cli, d, _v in buckets["needs_key"]:
            alt = f" (or run: {d['login']})" if d.get("login") else ""
            print(f"  {cli['bin']}: set {d.get('env') or 'the API key'} in your env{alt}")
        for _e, cli, d, _v in buckets["needs_login"]:
            print(f"  {cli['bin']}: {d['action']}")
        print("  then re-run: treg upload clis")
    if buckets["unsupported"]:
        print("\nNot supported:")
        for _e, cli, d, _v in buckets["unsupported"]:
            print(f"  {cli['bin']}: {d['reason']}")
    failed = [(n, r) for n, _t, r in registered if r not in ("ok", "exists")]
    if failed:
        print("\nFailed to register:")
        for n, r in failed:
            print(f"  {n}: {r}")
    ni = buckets["not_installed"]
    if ni and verbose:
        print("\nIn the catalog, not installed here:")
        for e, cli, d, _v in ni:
            print(f"  {cli['bin']:<12} {d.get('action') or ''}".rstrip())
    elif ni:
        print(f"\n{len(ni)} more catalog CLIs aren't installed. List them with: treg scan clis --status")
    if not report_only and registered:
        done = sum(1 for _n, _t, r in registered if r == "ok")
        print(f"\nRegistered {done} CLI tool(s). Run one with: treg run <name>")


def _import_oauth_loop(c, oauth_dets: list, env_path: str) -> None:
    """Walk the detected OAuth pairs one at a time: for each, prompt connect / skip / skip-all, and
    run the consent flow on yes before advancing (1/N → 2/N → …)."""
    from . import providers as prov
    total = len(oauth_dets)
    need = []
    for d in oauth_dets:
        need += [v for v in prov.oauth_parts(d.vars) if v]
    vals = prov.env_values(env_path, need)
    print(f"\n{total} OAuth provider(s) to connect (each opens a browser consent):")
    for i, d in enumerate(oauth_dets, 1):
        try:
            ans = input(f"\n  OAuth {i}/{total}: connect {d.provider} ({' + '.join(d.vars)})? "
                        f"[y = connect / n = skip / a = skip all]: ").strip().lower()
        except EOFError:
            ans = "a"
        if ans in ("a", "all", "skip-all"):
            print("  · skipping all remaining OAuth."); break
        if ans not in ("y", "yes"):
            print(f"  · {d.provider}: skipped."); continue
        _import_oauth_connect(c, d, vals)


def _import_oauth_connect(c, det, vals: dict) -> None:
    from . import providers as prov
    cid_var, csec_var = prov.oauth_parts(det.vars)
    body = {"name": prov._slug(det.provider or ""), "client_id": vals.get(cid_var), "client_secret": vals.get(csec_var),
            "auth_uri": det.auth["auth_uri"], "token_uri": det.auth["token_uri"], "scopes": det.auth.get("scopes", [])}
    r = c.post("/oauth/start", json=body)
    if r.status_code != 200:
        print(f"  ✗ {det.provider}: /oauth/start failed ({r.status_code}) {r.text[:100]}"); return
    try:                                   # a malformed 200 body must not abort the whole import loop
        d = r.json()
        redirect_uri, consent_url, state = d["redirect_uri"], d["consent_url"], d["state"]
    except (ValueError, KeyError, TypeError):
        print(f"  ✗ {det.provider}: unexpected /oauth/start response — skipped"); return
    print(f"    1. Ensure this redirect URI is allowed in the {det.provider} OAuth app:\n       {redirect_uri}")
    print(f"    2. Open to authorize:\n       {consent_url}\n    Waiting… (Ctrl-C to skip this one)")
    try:
        for _ in range(150):
            time.sleep(2)
            try:
                s = c.get(f"/oauth/status/{state}").json(); status = s.get("status")
            except Exception:  # a flaky/non-JSON poll shouldn't abort the loop
                continue
            if status == "done":
                print(f"  ✓ {det.provider} connected (oauth secret id {s.get('secret_id')})"); return
            if status == "error":
                print(f"  ✗ {det.provider}: {s.get('detail')}"); return
        print(f"  ✗ {det.provider}: timed out waiting for authorization.")
    except KeyboardInterrupt:               # Ctrl-C skips just this provider, not the whole import
        print(f"\n  · {det.provider}: skipped (Ctrl-C)")


LLM_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
LLM_DEFAULT_MODEL = "gemini-2.5-flash"


def _llm_chat(base_url: str, token: str, model: str, system: str, user: str) -> str:
    """One OpenAI-compatible chat call. Works against any OpenAI-shaped endpoint (Gemini's compat URL
    by default) — the token is the provider's own key, passed as a Bearer."""
    with httpx.Client(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60.0) as c:
        r = c.post("/chat/completions", json={"model": model, "temperature": 0,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _import_llm(c, unknowns: list, env_path: str, args) -> None:
    """Resolve unknown_secret vars via an LLM, then confirm + register each (LLM suggests, user confirms)."""
    from . import providers as prov
    token = args.llm_token or os.environ.get("TREG_LLM_TOKEN")
    if not token:
        print("\n[--llm needs an API token: pass --llm-token <key> or set TREG_LLM_TOKEN]"); return
    names = [d.vars[0] for d in unknowns]
    system, user = prov.llm_prompt(names)
    print(f"\nAsking {args.llm_model} to resolve {len(names)} unknown key(s)…")
    try:
        text = _llm_chat(args.llm_base_url, token, args.llm_model, system, user)
    except Exception as exc:
        print(f"  ✗ LLM call failed: {exc}"); return
    resolved = prov.llm_parse(text)
    if not resolved:
        print("  · the LLM returned no confident matches."); return
    vals = prov.env_values(env_path, names)
    # Idempotency + collision handling, same as the catalog path: know what's already registered and
    # keep tool names unique across this run.
    existing_tools = {t["name"]: t["id"] for t in (c.get("/tools").json() if c.get("/tools").status_code == 200 else [])}
    existing_secrets = {s["name"]: s["id"] for s in (c.get("/secrets").json() if c.get("/secrets").status_code == 200 else [])}
    used_names = set(existing_tools)
    for e in resolved:
        var, base, auth = e["var"], e["base_url"], e["auth"]
        binding = prov.build_binding(auth)
        if not binding:
            print(f"  · {var}: unsupported auth shape {auth.get('shape')} — skipped"); continue
        hdr = f" {auth.get('header') or auth.get('param')}" if auth.get("shape") != "bearer" else ""
        try:
            ans = input(f"  LLM: {var} → {e.get('provider')} {base}  [{auth.get('shape')}{hdr}]. Register? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print(f"  · {var}: skipped."); continue
        val = vals.get(var)
        if not val:
            print(f"  ✗ {var}: no value in the env — skipped"); continue
        tool_name, n = prov._slug(e.get("provider") or var), 2
        while tool_name in used_names and not (args.replace and tool_name in existing_tools):
            tool_name = f"{prov._slug(e.get('provider') or var)}-{n}"; n += 1
        if args.replace and var in existing_secrets:      # replace: clear the old secret + tool first
            existing_tools.get(tool_name) and c.delete(f"/tools/{existing_tools[tool_name]}")
            c.delete(f"/secrets/{existing_secrets[var]}")
        elif var in existing_secrets:
            print(f"  · {var}: already registered (use --replace)"); continue
        rs = c.post("/secrets", json={"name": var, "value": val, "kind": "env"})
        if rs.status_code >= 400:
            print(f"  ✗ {var}: secret failed ({rs.status_code}) {rs.text[:80]}"); continue
        sid = rs.json().get("id") or rs.json().get("secret_id")
        rt = c.post("/tools", json={"name": tool_name, "base_url": base, "bindings": [{**binding, "secret_id": sid}]})
        if rt.status_code < 400:
            used_names.add(tool_name)
            print(f"  ✓ {tool_name} {base}")
        else:
            print(f"  ✗ {var}: tool failed ({rt.status_code}) {rt.text[:80]}")


def _import_show_skipped(skipped: list) -> None:
    if not skipped:
        return
    print("\nNot auto-registered (need another path):")
    for a in skipped:
        vs = " + ".join(a.detection.vars)
        print(f"  · {vs}  —  {a.reason}")


# ---- import: skill directories (tools + recipe-only bundles) -------------------------------
def _skill_tag(kind: str) -> str:
    return {"contract": "tool (contract)", "generated": "tool (generated)", "recipe_only": "recipe-only"}.get(kind, kind)


def _import_select_skills(items: list, args) -> list:
    if args.select:
        want = {s.strip().lower() for s in args.select.split(",")}
        return [d for d in items if d.name.lower() in want]
    if args.all or args.dry_run:
        return items
    if not sys.stdin.isatty():   # don't silently import a whole skill library unattended — require intent
        sys.exit("non-interactive: pass --all to import everything, --select to choose, or --dry-run to preview")
    try:
        import questionary
    except ImportError:
        return items
    # Check a skill by default unless it's blocked by a gap we CAN'T resolve interactively — an
    # env-var gap is fixable (we prompt for the key), so those stay checked, not skipped.
    choices = [questionary.Choice(
        title=f"{d.name:<28} [{_skill_tag(d.kind)}] {d.base_url or ''}" + (f"  ⚠ {d.gaps[0]}" if d.gaps else ""),
        value=d, checked=_only_resolvable_gaps(d)) for d in items]
    return _checkbox("Skills to import (tools + recipes):", choices).ask() or []


_MISSING_ENV_RE = re.compile(r"needs (?:env var|credential) (\S+)")


def _only_resolvable_gaps(d) -> bool:
    """True if the skill has no gaps, or ONLY env-var gaps (which we can fix by asking for the key)."""
    return all(_MISSING_ENV_RE.match(g) for g in d.gaps)


def _prompt_missing_skill_creds(chosen: list, values: dict) -> None:
    """A chosen skill whose credential isn't in the .env: ASK for it (once per var) instead of skipping.
    Fills `values` and clears the now-satisfied gaps so the skill registers. Interactive only."""
    missing: list[str] = []
    for d in chosen:
        for g in d.gaps:
            m = _MISSING_ENV_RE.match(g)
            if m and m.group(1) not in values and m.group(1) not in missing:
                missing.append(m.group(1))
    if not missing:
        return
    print(f"\n{len(missing)} credential(s) your skills need aren't in the .env — enter to include those "
          "skills, or leave blank to skip:")
    for var in missing:
        try:
            val = getpass.getpass(f"  {var} (hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            val = ""
        if val:
            values[var] = val
    for d in chosen:  # a gap is resolved once its var has a value
        d.gaps = [g for g in d.gaps if not ((m := _MISSING_ENV_RE.match(g)) and m.group(1) in values)]


def _import_skills(args, cfg, skills_dir, env_path: str) -> None:
    from . import providers as prov, skills as sk
    dirs = [skills_dir] if isinstance(skills_dir, str) else list(skills_dir)
    have_env = os.path.isfile(env_path)
    env_names = set(prov.var_names(env_path)) if have_env else set()
    # A credential the skill needs can live in a .env OR already in the MACHINE ENVIRONMENT (e.g. a CLI's
    # key exported in your shell) — include both so treg finds it without asking when it's already there.
    # Only fold in CREDENTIAL-looking machine vars, so a skill needing a common name (HOME/USER/LANG)
    # isn't silently satisfied with an unrelated shell value.
    _authy = ("KEY", "TOKEN", "SECRET", "AUTH", "PAT", "PASSWORD", "CREDENTIAL", "APIKEY")
    env_names |= {k for k in os.environ if any(a in k.upper() for a in _authy)}
    catalog = prov.CATALOG if args.dry_run else _load_catalog(cfg)
    # Scan every dir but DEDUPE by skill name — `treg skill install` mirrors a skill into BOTH
    # .claude/skills and .agents/skills, so scanning both would prompt for each skill twice.
    seen: set[str] = set()
    dets = []
    for sd in dirs:
        for det in sk.scan_skills(sd, catalog=catalog, env_names=env_names):
            if det.name not in seen:
                seen.add(det.name); dets.append(det)
    tools = [d for d in dets if d.kind in ("contract", "generated")]
    recipes = [d for d in dets if d.kind == "recipe_only"]
    blocked = sum(1 for d in tools if d.gaps)
    loc = dirs[0] if len(dirs) == 1 else f"{len(dirs)} skill folders"
    env_note = f" · env: {os.path.relpath(env_path)} ({len(env_names)} vars)" if have_env else " · no .env found (env-credentialed skills will show a gap)"
    print(f"Scanned {loc}{env_note}: {len(tools)} API-tool skill(s) ({blocked} with gaps), {len(recipes)} recipe-only.")

    chosen = _import_select_skills(tools + recipes, args)
    if args.dry_run:
        print("\nFound skills — `treg upload` imports these:" if getattr(args, "as_scan", False)
              else "\nDRY RUN — would import:")
        for d in chosen:
            gap = "  ⚠ " + "; ".join(d.gaps) if d.gaps else ""
            print(f"  {'✓' if not d.gaps else '⚠'} {d.name:<28} {_skill_tag(d.kind):<18} {d.base_url or ''}{gap}")
        return
    if not chosen:
        print("Nothing selected."); return

    need = sk.env_needs([d for d in chosen if d.kind != "recipe_only"])
    values = prov.env_values(env_path, need) if (need and os.path.isfile(env_path)) else {}
    values.update({k: os.environ[k] for k in need if k in os.environ and k not in values})  # found on the machine
    if sys.stdin.isatty():   # ask for any credential neither the .env nor the machine env provided
        _prompt_missing_skill_creds(chosen, values)
    ok = 0
    with _client(cfg) as c:
        # Idempotency: a recipe-only bundle has no tool, so the server never 409s it — re-running would
        # silently pile up duplicate bundles. Look up what's already registered and skip (or --replace).
        existing_bundles: dict[str, list[int]] = {}
        rb = c.get("/bundles")
        if rb.status_code == 200:
            for b in rb.json():
                existing_bundles.setdefault(b["name"], []).append(b["id"])
        existing_tools = set()
        rt0 = c.get("/tools")
        if rt0.status_code == 200:
            existing_tools = {t["name"] for t in rt0.json()}
        for d in chosen:
            # A credential gap is now satisfiable from the machine env or the prompt above — judge by
            # `values`, not the stale classify-time gap. Still skip for any OTHER gap (missing base_url,
            # header collision, …).
            unmet = [k for k in sk.env_needs([d]) if k not in values] if d.kind != "recipe_only" else []
            other_gaps = [g for g in d.gaps if "needs credential" not in g and "needs env var" not in g]
            if unmet or other_gaps:
                reason = "; ".join(other_gaps) or ("missing " + ", ".join(unmet))
                print(f"  ⚠ {d.name}: {reason} — skipped (fix + rerun)"); continue
            clash = d.name in existing_bundles or (d.kind != "recipe_only" and d.name in existing_tools)
            if clash:
                if not args.replace:
                    print(f"  · {d.name}: already registered (use --replace to update)")
                    print(f"    ↗ {_detail_url(cfg, 'skill', d.name)}"); continue
                for bid in existing_bundles.get(d.name, []):   # delete the old bundle (cascades its tool+secrets)
                    c.delete(f"/bundles/{bid}")
            try:
                payload = sk.build_payload(d, values)
            except (ValueError, OSError) as exc:
                print(f"  ✗ {d.name}: {exc}"); continue
            r = c.post("/skills", json=payload)
            if r.status_code == 409:
                print(f"  · {d.name}: a tool with this name already exists (use --replace)"); continue
            if r.status_code >= 400:
                print(f"  ✗ {d.name}: {r.status_code} {r.text[:100]}"); continue
            wrote = sk.write_contract(d)                        # only after a successful push
            tag = "recipe" if d.kind == "recipe_only" else "tool"
            print(f"  ✓ {d.name:<28} ({tag})" + ("  [wrote treg.json]" if wrote else ""))
            print(f"    ↗ {_detail_url(cfg, 'skill', d.name)}"); ok += 1
    print(f"\nImported {ok}/{len(chosen)} skills. Share a skill by sending its ↗ link — "
          "the page previews it and carries the agent install prompt.")


# ---- tools --------------------------------------------------------------------------------
def _parse_bind(spec: str) -> dict:
    b = {"injector": "env", "location": "header", "name": "Authorization",
         "format": "Bearer {secret}", "secret_field": "access_token"}
    for part in spec.split(","):
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k in ("secret", "secret_id"):
            try:
                b["secret_id"] = int(v)
            except ValueError:
                raise SystemExit(f"--bind secret= must be an integer id, got {v!r}")
        elif k in b:
            b[k] = v
        else:
            raise SystemExit(f"unknown --bind key {k!r}")
    if "secret_id" not in b:
        raise SystemExit("each --bind needs secret=<id>")
    return b


def cmd_tool_add(args, cfg) -> None:
    body: dict = {"name": args.name, "base_url": args.base_url}
    if args.bind:
        body["bindings"] = [_parse_bind(s) for s in args.bind]
    elif args.binding:
        body["bindings"] = [_load_json_arg(b, "binding") for b in args.binding]
    elif args.secret is not None:
        body.update(secret_id=args.secret, injector=args.injector, auth_in=args.auth_in,
                    auth_name=args.auth_name, auth_format=args.auth_format, secret_field=args.secret_field)
    if args.health:
        body["health_check"] = _load_json_arg(args.health, "health")
    with _client(cfg) as c:
        _show(c.post("/tools", json=body))


def _resolve_secret_ref(c, ref):
    """A secret ref on the friendly `add` command is either an integer id or a secret NAME.
    Return the id, exiting cleanly if a name doesn't resolve."""
    if ref is None:
        return None
    try:
        return int(ref)
    except (TypeError, ValueError):
        pass
    r = c.get("/secrets")
    if r.status_code >= 400:
        _show(r); sys.exit(1)
    hits = [s for s in r.json() if s.get("name") == ref]
    if not hits:
        sys.exit(f"no secret named {ref!r} — add it first (treg secret add {ref} --value …) or use its id")
    return hits[0]["id"]


def cmd_add(args, cfg) -> None:
    """Friendly shortcut for `tool add`: register an upstream API + how to inject a credential.
    `--secret` accepts a NAME or an id; default injection is a Bearer token in the Authorization header."""
    base = args.base_url or args.base
    if not base:
        sys.exit("give the API base URL with --base-url")
    with _client(cfg) as c:
        sid = _resolve_secret_ref(c, args.secret)
        body: dict = {"name": args.name, "base_url": base}
        if sid is not None:
            body.update(secret_id=sid, injector="env", auth_in="header",
                        auth_name=args.header or "Authorization",
                        auth_format=args.format or "Bearer {secret}", secret_field="access_token")
        r = c.post("/tools", json=body)
        _show(r)
    print(f"↗ {_detail_url(cfg, 'tool', args.name)}")


def cmd_tool_ls(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.get("/tools"))


def cmd_tool_rm(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.delete(f"/tools/{args.id}"))


def cmd_tool_update(args, cfg) -> None:
    body: dict = {}
    if args.base_url is not None:
        body["base_url"] = args.base_url
    if args.bind:
        body["bindings"] = [_parse_bind(s) for s in args.bind]
    elif args.binding:
        body["bindings"] = [_load_json_arg(b, "binding") for b in args.binding]
    if args.health is not None:
        body["health_check"] = _load_json_arg(args.health, "health")
    with _client(cfg) as c:
        if getattr(args, "local_run", None):
            # PATCH replaces the whole cli profile — merge `enabled` into the CURRENT one so flipping
            # the toggle never wipes a contract-declared inject/deny list.
            r = c.get("/tools")
            if r.status_code != 200:
                _show(r)
            current = next((t for t in r.json() if t["id"] == args.id), None)
            if current is None:
                sys.exit(f"tool id {args.id} not found in this org")
            cli = dict(current.get("cli") or {})
            cli["enabled"] = args.local_run == "on"
            body["cli"] = cli
        if not body:
            sys.exit("nothing to update (use --base-url / --bind / --binding / --health / --local-run)")
        _show(c.patch(f"/tools/{args.id}", json=body))


# ---- call + audit -------------------------------------------------------------------------
def cmd_call(args, cfg) -> None:
    for kv in args.query:  # a token without '=' would crash dict()/split with an opaque traceback
        if "=" not in kv:
            sys.exit(f"--query expects K=V, got: {kv!r}")
    # A LIST of pairs (not a dict) so repeated --query keys (?tag=a&tag=b) survive to the upstream —
    # httpx serializes a list of tuples preserving duplicates; a dict would drop all but the last.
    params = [tuple(kv.split("=", 1)) for kv in args.query]
    content = Path(args.file).read_bytes() if args.file else (args.data.encode() if args.data else None)
    rest = args.target.rstrip("/")
    if args.path:
        rest += "/" + args.path.lstrip("/")
    with _client(cfg) as c:
        _show(c.request(args.method, f"/call/{rest}", params=params, content=content))


def cmd_calls(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.get("/calls", params={"limit": args.limit}))


def cmd_runs(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.get("/runs", params={"limit": args.limit}))


def cmd_run(args, cfg) -> None:
    """`treg run <tool> -- <args>` dispatcher over two execution tiers (docs/CLI-RUN-PLAN.md):
      --local  (default): run the CLI on THIS machine; the credential is isolated under the treg-run user.
      --server          : run the CLI on the registry server (Tier 0), streaming stdout/stderr back.
    """
    # argparse.REMAINDER swallows any treg flag placed AFTER the tool name, silently. Catch ONLY that
    # case — a tier flag typed after the tool but BEFORE the `--` separator — by reading the REAL command
    # line. A flag after `--` legitimately belongs to the vendor CLI (`treg run db -- --timeout 30`) and
    # must NOT trip this. (argparse already consumed the first `--`, so args.args can't tell them apart.)
    argv = sys.argv
    if args.tool in argv:
        after_tool = argv[argv.index(args.tool) + 1:]
        before_sep = after_tool[: after_tool.index("--")] if "--" in after_tool else after_tool
        misplaced = [f for f in ("--server", "--local", "--timeout", "--fs-jail") if f in before_sep]
        if misplaced:
            sys.exit(f"treg: put {misplaced[0]} BEFORE the tool name: "
                     f"treg run {misplaced[0]} {args.tool} -- <cli args>  (a flag after the tool name is "
                     f"passed to the CLI, not to treg)")
    if not getattr(args, "server", False) and getattr(args, "timeout", None) is not None:
        print("  ! --timeout only applies to --server runs; ignoring it for this local run", file=sys.stderr)
    if getattr(args, "server", False):
        _run_server(args, cfg)
    else:
        _run_local(args, cfg)


def _run_server(args, cfg) -> None:
    """`--server`: run the tool's CLI on the server — keys injected server-side, never on this
    machine. Mirrors the child's stdout/stderr + exit code so it behaves like running the CLI locally."""
    user_args = list(args.args)
    if user_args and user_args[0] == "--":   # match _run_local: don't forward the argparse `--` separator
        user_args = user_args[1:]
    body: dict = {"tool": args.tool, "args": user_args}
    if args.timeout is not None:
        body["timeout_s"] = args.timeout
    with _client(cfg) as c:
        r = c.post("/run", json=body)
    if r.status_code >= 400:
        _show(r); return  # _show exits non-zero on error
    data = r.json()
    if data.get("stdout"):
        sys.stdout.write(data["stdout"] if data["stdout"].endswith("\n") else data["stdout"] + "\n")
    if data.get("stderr"):
        sys.stderr.write(data["stderr"] if data["stderr"].endswith("\n") else data["stderr"] + "\n")
    if data.get("timed_out"):
        print("  (timed out on the server)", file=sys.stderr)
        sys.exit(1)  # a timeout is a failure — never exit 0 even if the server reports exit_code 0/null
    code = data.get("exit_code") or 0
    sys.exit(code if 0 <= code < 256 else 1)  # a signal/negative code maps to a generic failure


# ---- local runs: `treg run --local <tool> -- <cli args…>` (docs/CLI-RUN-PLAN.md) ----------------
# The credential must not be readable by another program of the same user. On Linux we run the CLI as a
# dedicated `treg-run` user (installed once via `treg setup-local-run`): a different uid cannot read the
# process's env/memory. The member's `treg run` hands off to that user via sudo; the RUNNER (as treg-run)
# fetches the credential and runs the CLI, so the vendor secret only ever exists under treg-run. Without
# that setup (or on non-Linux) it falls back to running as the member, best-effort, with a warning.
_RUN_USER = "treg-run"
_RUNNER_PATH = "/usr/local/bin/treg-runner"
_RUN_PROOF_PATH = "/etc/treg-run/proof"  # the isolated-runner proof — root-owned, readable ONLY by treg-run


class _StreamRedactor:
    """Streaming byte-replacer: scrubs known secret values out of a process's stdout/stderr before it
    reaches the terminal. Boundary-safe — a secret split across two reads is still caught by retaining
    the last (longest_secret - 1) bytes between feeds. Only used for SHARED-key runs (the server sets
    `redact_output`); an owned-key run keeps a raw, unbuffered TTY."""

    def __init__(self, secrets: list[bytes]):
        self._secrets = [s for s in secrets if s]
        self._keep = max((len(s) for s in self._secrets), default=0)
        self._buf = bytearray()

    def _scrub(self, data: bytes) -> bytes:
        for s in self._secrets:
            data = data.replace(s, b"***")
        return data

    def feed(self, chunk: bytes) -> bytes:
        self._buf.extend(chunk)
        cut = len(self._buf) - (self._keep - 1) if self._keep > 1 else len(self._buf)
        if cut <= 0:
            return b""
        out = self._scrub(bytes(self._buf[:cut]))
        del self._buf[:cut]
        return out

    def flush(self) -> bytes:
        out = self._scrub(bytes(self._buf))
        self._buf.clear()
        return out


def _run_helper(tool, user_args, cfg) -> None:
    """Fetch the grant, run the CLI with the credential injected, classify a failure, report it, and exit
    with the CLI's code. Runs as treg-run on the isolated path, or as the member in best-effort mode."""
    # The isolated runner proves itself with a value only treg-run can read (installed by
    # setup-local-run, exported by the runner script) — lets the server release a SHARED key to the
    # runner but refuse a direct member call. Absent on the best-effort path (owned keys only).
    proof = os.environ.get("TREG_RUN_PROOF", "")
    headers = {"X-Treg-Run-Proof": proof} if proof else {}
    with _client(cfg) as c:
        r = c.post(f"/tools/{quote(tool, safe='')}/grant", json={"argv": user_args}, headers=headers)
    if r.status_code >= 400:
        try:
            sys.exit(f"treg: {r.json().get('detail', r.text)}")
        except json.JSONDecodeError:
            sys.exit(f"treg: grant failed (HTTP {r.status_code})")
    grant = r.json()

    binary = grant.get("bin") or tool
    path = shutil.which(binary)
    if path is None:
        hint = f" — install it: {grant['install']}" if grant.get("install") else ""
        sys.exit(f"treg: {binary!r} is not on your PATH{hint}")
    for w in grant.get("warnings") or []:
        print(f"  ! {w}", file=sys.stderr)
    print(f"▸ {tool} · audit #{grant.get('audit_id')}", file=sys.stderr)

    # Apply each delivery-tagged inject item: `env` sets an env var; `argv` adds flags BEFORE the user's
    # args (global/auth flags belong first). Under treg-run the env is safe — a different uid can't read it.
    env = dict(os.environ)
    argv_extra: list[str] = []
    for item in grant.get("inject") or []:
        if item.get("via") == "env":
            env[item["name"]] = item["value"]
        elif item.get("via") == "argv":
            argv_extra += item.get("argv") or []
    cmd = [path, *argv_extra, *user_args]

    # --fs-jail (opt-in): confine the CLI's writes to a private per-run scratch (0700, treg-run-owned, so
    # the member can't read into it), pointed at as HOME. Closes the file-drop exfil channel. Removed after.
    fsjail_dir = None
    if os.environ.get("TREG_RUN_FSJAIL") == "1":
        if sys.platform == "darwin":
            from . import fsjail
            fsjail_dir = tempfile.mkdtemp(prefix="treg-fsjail-")
            os.chmod(fsjail_dir, 0o700)
            env["HOME"] = fsjail_dir      # tool caches land in the private scratch, not a readable HOME
            env["TMPDIR"] = fsjail_dir + "/"
            prof = os.path.join(fsjail_dir, "profile.sb")
            Path(prof).write_text(fsjail.macos_profile(fsjail_dir))
            cmd = fsjail.wrap_macos(cmd, prof)
        else:
            print("  ! --fs-jail is enforced on macOS only for now; running without it", file=sys.stderr)

    errors = grant.get("errors") or []
    # A SHARED-key run (server sets redact_output) scrubs the injected value from the CLI's output, so a
    # member can't print it back via a CLI feature. That needs us to capture stdout too — the cost is a
    # non-raw, slightly buffered terminal, paid ONLY on sensitive runs. An owned-key run is unchanged:
    # stdout/stdin stay on the terminal and stderr is teed only when there are error patterns to match.
    redact_vals: list[bytes] = []
    if grant.get("redact_output"):
        seen: set[str] = set()
        for item in grant.get("inject") or []:
            for v in ([item.get("value")] if item.get("via") == "env" else (item.get("argv") or [])):
                if v and v not in seen:
                    seen.add(v)
                    redact_vals.append(v.encode())
    scrub = bool(redact_vals)
    tee = bool(errors) or scrub  # capture stderr to match errors and/or to scrub it
    tail: deque[bytes] = deque(maxlen=256)
    proc = subprocess.Popen(cmd, env=env,  # noqa: S603 — argv list, no shell
                            stdout=subprocess.PIPE if scrub else None,
                            stderr=subprocess.PIPE if tee else None, bufsize=0)

    def _forward(signum, _frame):  # forward terminating signals so nothing is orphaned
        try:
            proc.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass
    # Look each signal up by NAME so a platform that lacks one (Windows has no SIGHUP) simply skips it —
    # a tuple of `signal.SIGHUP` would raise AttributeError at construction, before the hasattr guard runs.
    _sig = [getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, n)]
    prev = {s: signal.signal(s, _forward) for s in _sig}

    def _pump(src, dst, collect: deque | None) -> None:
        red = _StreamRedactor(redact_vals) if scrub else None
        while True:
            chunk = src.read(4096)
            if not chunk:
                break
            if collect is not None:
                collect.append(chunk)  # raw bytes, for error-pattern matching (never printed)
            dst.write(red.feed(chunk) if red else chunk)
            dst.flush()
        if red:
            dst.write(red.flush())
            dst.flush()

    pumps = []
    if scrub:
        pumps.append(threading.Thread(target=_pump, args=(proc.stdout, sys.stdout.buffer, None), daemon=True))
    if tee:
        pumps.append(threading.Thread(target=_pump, args=(proc.stderr, sys.stderr.buffer, tail), daemon=True))
    for p in pumps:
        p.start()
    try:
        rc = proc.wait()
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    for p in pumps:
        p.join(timeout=2)

    if rc != 0:
        verdict = "unknown_error"
        if tee:  # classify only when we captured stderr; bound the text (ReDoS defence)
            stderr_text = b"".join(tail).decode("utf-8", "replace")[-4000:]
            for e in errors:
                try:
                    if re.search(e.get("pattern", ""), stderr_text):
                        verdict = e.get("verdict", "unknown_error")
                        if e.get("message"):
                            print(f"treg: {e['message']}", file=sys.stderr)
                            if verdict == "credential_invalid":
                                print("treg: marked the credential invalid — an org owner can rotate it.", file=sys.stderr)
                        break
                except re.error:
                    continue
        try:  # best-effort: the verdict enum only, never raw output
            with _client(cfg) as c:
                c.post(f"/tools/{quote(tool, safe='')}/run-report",
                       json={"audit_id": grant.get("audit_id"), "exit_code": rc, "verdict": verdict})
        except Exception:  # noqa: BLE001 — reporting must never mask the CLI's own failure
            pass
    if fsjail_dir:
        shutil.rmtree(fsjail_dir, ignore_errors=True)  # wipe the private scratch (with any file the CLI wrote)
    sys.exit(rc)


def _traversable_by_others(path: str) -> bool:
    """Can a NON-owner/non-group user (like treg-run) reach `path`? True only if every component has the
    world-execute (traverse) bit. A cheap, subprocess-free proxy — used to avoid handing the isolated
    runner a cwd it can't stat (which makes its shell spam a getcwd error and gives the CLI an unusable
    working dir). Conservative: if unsure, it returns False and we hop to an accessible dir."""
    p = os.path.abspath(path)
    while True:
        try:
            if not (os.stat(p).st_mode & 0o001):
                return False
        except OSError:
            return False
        parent = os.path.dirname(p)
        if parent == p:
            return True
        p = parent


def _run_local(args, cfg) -> None:
    """`--local` (default): run the CLI on THIS machine. On Linux with local-run set up, hand off to the
    treg-run user so the vendor credential never touches the member's uid; otherwise run as the member,
    best-effort, with a warning."""
    user_args = list(args.args)
    if user_args and user_args[0] == "--":
        user_args = user_args[1:]
    if getattr(args, "fs_jail", False):
        os.environ["TREG_RUN_FSJAIL"] = "1"  # read by _run_helper; survives sudo via the runner's env_keep
    isolatable = sys.platform.startswith("linux") or sys.platform == "darwin"
    if isolatable and os.path.exists(_RUNNER_PATH):
        # treg-run can't enter a private (0700) home, so if the cwd isn't world-traversable, start the
        # runner from a neutral accessible dir — else its shell prints a getcwd error and the CLI runs in
        # a dir treg-run can't use anyway. (Only on the ISOLATED path; best-effort runs as the member.)
        if not _traversable_by_others(os.getcwd()):
            # NB: macOS's per-user $TMPDIR (/var/folders/…) is also 0700 → unreachable by treg-run; pick a
            # genuinely world-traversable dir instead.
            for _d in ("/tmp", "/"):
                if _traversable_by_others(_d):
                    try:
                        os.chdir(_d); break
                    except OSError:
                        pass
        # Hand off to treg-run. sudo connects the terminal, so input/output/signals/exit flow through.
        # The member's OWN token travels via env (preserved by the install-time sudoers rule) so the
        # runner can fetch the vendor credential itself — the member never holds that credential.
        env = dict(os.environ)
        env["TREG_RUN_TOKEN"] = cfg.get("token") or ""
        env["TREG_RUN_BASE"] = cfg.get("base_url") or ""
        env["TREG_RUN_ORG"] = _effective_org(cfg) or ""
        try:
            os.execvpe("sudo", ["sudo", "-u", _RUN_USER, "--", _RUNNER_PATH, args.tool, "--", *user_args], env)
        except OSError:
            pass
        sys.exit("treg: could not switch to the treg-run user — is local-run set up? (sudo treg setup-local-run)")
    if isolatable:
        print("  · best-effort (run `sudo treg setup-local-run` once for full isolation)", file=sys.stderr)
    _run_helper(args.tool, user_args, cfg)


def cmd_run_helper(args, cfg) -> None:
    """Internal (`__run-helper`): invoked as the treg-run user by the installed runner. Rebuilds the
    caller's config from the env the member passed through sudo, then runs the CLI so the credential
    only ever exists under treg-run. Not meant to be called directly."""
    hcfg = {"token": os.environ.get("TREG_RUN_TOKEN", ""),
            "base_url": os.environ.get("TREG_RUN_BASE", ""),
            "active_org": os.environ.get("TREG_RUN_ORG", "")}
    if not hcfg["token"] or not hcfg["base_url"]:
        sys.exit("treg: run-helper is missing its context (do not call __run-helper directly)")
    user_args = list(args.args)
    if user_args and user_args[0] == "--":
        user_args = user_args[1:]
    _run_helper(args.tool, user_args, hcfg)


_EGRESS_PLIST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0"><dict>\n'
    '  <key>Label</key><string>dev.treg.egress</string>\n'
    '  <key>ProgramArguments</key><array><string>{loader}</string></array>\n'
    '  <key>RunAtLoad</key><true/>\n'
    '</dict></plist>\n'
)
_EGRESS_LOADER = "/usr/local/bin/treg-egress-load"


def _member_registry_url(member: str) -> str | None:
    """The registry the MEMBER's CLI points at — read from THEIR config, since setup runs as root (whose
    config is empty). treg-run must be allowed to reach it, or the runner's /grant call is blocked."""
    try:
        import pwd
        home = pwd.getpwnam(member).pw_dir
        return json.loads((Path(home) / ".treg" / "config.json").read_text()).get("base_url")
    except Exception:  # noqa: BLE001 — a missing/odd config just means no registry host is added
        return None


def _install_egress(registry_url: str | None) -> None:
    """Install the static egress allow-list (Option 1): treg-run may reach ONLY the registry + the catalog
    vendor API hosts; every other destination is dropped, so a rogue CLI feature can't exfiltrate the key
    over the network. Loaded now AND persisted across reboots (macOS LaunchDaemon / Linux nft file)."""
    from . import egress, providers as prov
    hosts = egress.collect_hosts(registry_url, prov.CATALOG)
    ips = egress.resolve_hosts(hosts)
    if not ips:
        print("  ! egress: could not resolve any allow-list host — skipped (else runs would be blocked). "
              "Re-run with the registry reachable.", file=sys.stderr)
        return
    os.makedirs("/etc/treg-run", exist_ok=True)
    if sys.platform == "darwin":
        Path("/etc/treg-run/egress.pf").write_text(egress.pf_ruleset(ips, _RUN_USER))
        # A root-owned loader that re-applies Apple's ruleset + our per-uid rules (pf is last-match; our
        # `quick` user rules take effect without disturbing anyone else's traffic).
        Path(_EGRESS_LOADER).write_text("#!/bin/sh\ncat /etc/pf.conf /etc/treg-run/egress.pf | pfctl -f -\n")
        os.chmod(_EGRESS_LOADER, 0o755)
        subprocess.run([_EGRESS_LOADER], capture_output=True)  # enforce now
        plist = "/Library/LaunchDaemons/dev.treg.egress.plist"
        Path(plist).write_text(_EGRESS_PLIST.format(loader=_EGRESS_LOADER))
        subprocess.run(["launchctl", "load", "-w", plist], capture_output=True)  # re-apply at boot
        print(f"  egress: pf allow-list active — {_RUN_USER} may reach {len(hosts)} host(s), all else dropped")
    else:  # linux
        uid = subprocess.run(["id", "-u", _RUN_USER], capture_output=True, text=True).stdout.strip() or "0"
        Path("/etc/treg-run/egress.nft").write_text(egress.nft_ruleset(ips, int(uid)))
        subprocess.run(["nft", "-f", "/etc/treg-run/egress.nft"], capture_output=True)
        print(f"  egress: nftables allow-list active — {_RUN_USER} may reach {len(hosts)} host(s), all else dropped")
        print("    (to persist across reboot, load /etc/treg-run/egress.nft from your nftables service)")


def _pick_macos_service_uid() -> int:
    """A free system uid/gid for the hidden treg-run account (macOS service accounts sit below 500)."""
    out = subprocess.run(["dscl", ".", "-list", "/Users", "UniqueID"], capture_output=True, text=True).stdout
    used = {int(p[1]) for p in (ln.split() for ln in out.splitlines()) if len(p) == 2 and p[1].lstrip("-").isdigit()}
    for uid in range(380, 500):
        if uid not in used:
            return uid
    sys.exit("treg: no free system uid in 380-499 for the treg-run user")


def _create_run_user() -> None:
    """Create the dedicated no-login treg-run system user (idempotent). A DIFFERENT uid is what makes the
    vendor credential unreadable by the member — cross-uid `/proc/<pid>/environ` (Linux) and `task_for_pid`
    (macOS) are both denied. Linux: `useradd`. macOS: `dscl` (a hidden service account, free system uid)."""
    if subprocess.run(["id", _RUN_USER], capture_output=True).returncode == 0:
        print(f"system user {_RUN_USER!r} already exists")
        return
    if sys.platform == "darwin":
        uid = _pick_macos_service_uid()
        subprocess.run(["dscl", ".", "-create", f"/Groups/{_RUN_USER}"], check=True)
        subprocess.run(["dscl", ".", "-create", f"/Groups/{_RUN_USER}", "PrimaryGroupID", str(uid)], check=True)
        subprocess.run(["dscl", ".", "-create", f"/Users/{_RUN_USER}"], check=True)
        for key, val in (("UserShell", "/usr/bin/false"), ("RealName", "treg local run"),
                         ("UniqueID", str(uid)), ("PrimaryGroupID", str(uid)),
                         ("NFSHomeDirectory", "/var/empty"), ("IsHidden", "1")):
            subprocess.run(["dscl", ".", "-create", f"/Users/{_RUN_USER}", key, val], check=True)
        print(f"created hidden system user {_RUN_USER!r} (uid {uid})")
    else:  # linux
        subprocess.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", _RUN_USER], check=True)
        print(f"created system user {_RUN_USER!r}")


def cmd_setup_local_run(args, cfg) -> None:
    """One-time admin setup for isolated local runs (Linux + macOS): create the treg-run system user,
    install a fixed root-owned runner that can ONLY invoke the hidden helper (never a shell), and add a
    narrow sudoers rule letting the member run ONLY that runner as treg-run. Idempotent."""
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        sys.exit("treg: setup-local-run supports Linux and macOS.")
    if os.geteuid() != 0:
        sys.exit("treg: run this with sudo — it creates a system user and a sudoers rule:\n"
                 "  sudo treg setup-local-run")
    member = args.member or os.environ.get("SUDO_USER")
    if not member:
        sys.exit("treg: could not determine which OS user to allow — pass --member <user>")
    # --refresh-egress: only re-resolve + reinstall the network allow-list (IPs drift over time).
    if getattr(args, "refresh_egress", False):
        _install_egress(getattr(args, "registry", None) or _member_registry_url(member))
        return
    # The member name is interpolated into the sudoers file — it MUST be a plain unix username, or a
    # crafted value ("evil ALL=(ALL) NOPASSWD: ALL #") would inject a valid extra directive.
    if not re.match(r"^[a-z_][a-z0-9_-]*\$?$", member):
        sys.exit(f"treg: {member!r} is not a valid unix username")
    treg_bin = shutil.which("treg") or os.path.realpath(sys.argv[0])

    # 1) the dedicated system user (no home, no login shell)
    _create_run_user()

    # 2) the isolated-runner PROOF — a value only treg-run can read. The server releases a SHARED key
    #    (one the member doesn't own) only when the runner presents it, so a direct member `/grant` call
    #    can't read someone else's key. Root-owned dir + file, mode 0400 owner treg-run.
    proof = args.run_proof or os.environ.get("TREG_RUN_PROOF") or ""
    if proof:
        os.makedirs(os.path.dirname(_RUN_PROOF_PATH), exist_ok=True)
        Path(_RUN_PROOF_PATH).write_text(proof)
        subprocess.run(["chown", f"{_RUN_USER}:{_RUN_USER}", _RUN_PROOF_PATH], check=True)  # user:group (macOS-safe)
        os.chmod(_RUN_PROOF_PATH, 0o400)  # only treg-run (and root) can read it; the member cannot
        print(f"installed runner proof at {_RUN_PROOF_PATH} (shared-key local runs enabled)")
    else:
        print("no --run-proof given → only OWNED-key tools can run locally (shared-key runs stay blocked)")

    # 3) the runner — a fixed, root-owned wrapper that can ONLY run the hidden helper (so a member can
    #    never get an arbitrary command as treg-run). HOME=/tmp keeps any tool cache writable; it exports
    #    the proof (if installed) so the helper can present it — the member's shell never sees that value.
    Path(_RUNNER_PATH).write_text(
        '#!/bin/sh\nexport HOME=/tmp\n'
        f'[ -r {_RUN_PROOF_PATH} ] && export TREG_RUN_PROOF="$(cat {_RUN_PROOF_PATH})"\n'
        f'exec "{treg_bin}" __run-helper "$@"\n')
    os.chmod(_RUNNER_PATH, 0o755)  # we are root -> root-owned; the member cannot modify it
    print(f"installed runner at {_RUNNER_PATH}")

    # 3) a narrow sudoers rule: the member may run ONLY that runner, ONLY as treg-run, no password;
    #    preserve just the three context vars the runner needs (the member's own token + base + org).
    rule = (f'Defaults!{_RUNNER_PATH} env_keep += "TREG_RUN_TOKEN TREG_RUN_BASE TREG_RUN_ORG TREG_RUN_FSJAIL"\n'
            f'{member} ALL=({_RUN_USER}) NOPASSWD: {_RUNNER_PATH}\n')
    os.makedirs("/etc/sudoers.d", exist_ok=True)  # present on Linux; on macOS it's the @includedir target
    tmp = "/etc/sudoers.d/.treg-run.tmp"
    Path(tmp).write_text(rule)
    os.chmod(tmp, 0o440)
    if subprocess.run(["visudo", "-cf", tmp], capture_output=True).returncode != 0:
        os.unlink(tmp)
        sys.exit("treg: the generated sudoers rule failed validation — nothing installed")
    os.replace(tmp, "/etc/sudoers.d/treg-run")
    print(f"installed sudoers rule for member {member!r}")

    # Isolation works BY treg-run being unable to read into the member's files — which also means it can't
    # exec a treg installed inside the member's private (0700) home. Catch that here with the exact fix,
    # instead of a confusing "Permission denied" at the first run.
    if subprocess.run(["sudo", "-u", _RUN_USER, "test", "-x", treg_bin],
                      capture_output=True).returncode != 0:
        print(f"\n  ! {_RUN_USER} cannot execute treg at {treg_bin} — it's inside a private home dir.\n"
              f"    Install treg at a system path (e.g. /usr/local/bin) so the isolated runner can reach\n"
              f"    it; until then, isolated local runs will fail (the proxy + `treg call` are unaffected).",
              file=sys.stderr)

    # The network half of the sandbox: restrict treg-run's egress to the registry + catalog API hosts,
    # so a rogue CLI feature can't send the injected key to an arbitrary host (docs/CLI-SHELL-MODE-PLAN.md).
    if not getattr(args, "no_egress", False):
        _install_egress(getattr(args, "registry", None) or _member_registry_url(member))
    print(f"\ndone — {member} can now run:  treg run <tool> -- <args>   (the CLI runs as {_RUN_USER})")


# ---- shell mode: transparent CLI interception (`treg shell`) -------------------------------
def cmd_shell_start(args, cfg) -> None:
    """Open a subshell where the team's registered CLIs run with the credential injected — the member
    types `stripe …`/`gh …` normally and treg handles auth behind the scenes (docs/CLI-SHELL-MODE-PLAN.md).
    MVP: shims call `treg run <tool>`, reusing the whole local-run path."""
    from . import shell as sh
    if os.environ.get(sh.ENV_ACTIVE) == "1":
        sys.exit("treg: you're already in a treg shell — type `exit` to leave first.")
    if not cfg.get("token"):
        sys.exit("treg: sign in first — `treg login`.")
    with _client(cfg) as c:
        r = c.get("/tools")
    if r.status_code >= 400:
        _show(r); return  # _show exits non-zero on error
    server_for = frozenset(x.strip() for x in (args.server_for or "").split(",") if x.strip())
    entries, warnings = sh.plan_shims(r.json(), server_for)
    if not entries:
        sys.exit("treg: no runnable CLIs in this team yet. Register one with `treg upload clis`, or "
                 "enable local runs on a tool: `treg tool update <name> --local-run on`.")
    for w in warnings:
        print(f"  ! {w}", file=sys.stderr)
    treg_bin = shutil.which("treg") or os.path.realpath(sys.argv[0])
    sys.exit(sh.start_session(entries, treg_bin, ttl_minutes=args.ttl))


def cmd_shell_stop(args, cfg) -> None:
    """Leave the treg shell (equivalent to `exit`/Ctrl-D). Only meaningful inside a session."""
    from . import shell as sh
    sh.stop_session()


# ---- skills -------------------------------------------------------------------------------
def cmd_skill_scaffold(args, cfg) -> None:
    from .convert import scaffold_skill
    try:
        manifest = json.dumps(scaffold_skill(args.dir), indent=2)
    except (NotADirectoryError, OSError) as exc:
        sys.exit(str(exc))
    if args.out:
        Path(args.out).write_text(manifest)
        print(f"wrote {args.out} (fill in base_url + bindings, then: treg skill push {args.out})")
    else:
        print(manifest)


def cmd_skill_push(args, cfg) -> None:
    try:
        body = json.loads(Path(args.file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"could not read skill file {args.file!r}: {exc}")
    with _client(cfg) as c:
        _show(c.post("/skills", json=body))
    if body.get("name"):
        print(f"↗ {_detail_url(cfg, 'skill', body['name'])}")


def cmd_skill_init(args, cfg) -> None:
    from .convert import CONTRACT_FILE, generate_contract
    try:
        contract = generate_contract(args.dir)
    except (NotADirectoryError, OSError) as exc:
        sys.exit(str(exc))
    out = Path(args.out) if args.out else Path(args.dir) / CONTRACT_FILE
    fill = contract.pop("_fill", [])
    out.write_text(json.dumps(contract, indent=2))
    print(f"wrote {out}")
    print(f"  auto: base_url={contract['base_url'] or '(none)'} | secrets={[s['name'] for s in contract['secrets']]}")
    if fill:
        print("  review / fill:", file=sys.stderr)
        for f in fill:
            print(f"    - {f}", file=sys.stderr)
    print(f"then register it in your active org:  treg skill add --dir {args.dir}")


def cmd_skill_add(args, cfg) -> None:
    from .convert import CONTRACT_FILE, contract_to_skill_payload, load_contract
    try:
        contract = load_contract(args.dir)
    except ValueError as exc:  # malformed treg.json
        sys.exit(str(exc))
    if contract is None:
        sys.exit(f"no {CONTRACT_FILE} in {args.dir} — run 'treg skill init --dir {args.dir}' first")
    if not contract.get("base_url"):
        sys.exit(f"{CONTRACT_FILE} has no base_url — fill it in, then re-run")
    try:
        payload = contract_to_skill_payload(args.dir, contract)
    except (ValueError, FileNotFoundError) as exc:  # stale/edited contract → clear message, clean exit
        sys.exit(str(exc))
    with _client(cfg) as c:
        _show(c.post("/skills", json=payload))
    print(f"↗ {_detail_url(cfg, 'skill', payload['name'])}")


def cmd_skill_ls(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.get("/bundles"))


def cmd_skill_rm(args, cfg) -> None:
    with _client(cfg) as c:
        _show(c.delete(f"/bundles/{args.id}"))


def cmd_skill_install(args, cfg) -> None:
    """Pull a skill's recipe from the registry and write it to <dir>/<name>/SKILL.md so a coding agent
    loads it. By default it fans out to every agent's skills dir (`.agents/skills` + `.claude/skills`);
    `--agent <name>` targets one, `--all-agents` targets every known agent, `--global` writes into the
    detected-installed agents' global dirs, and `--dir` pins one explicit directory. `--all` installs
    the whole org library; a tool-backed skill notes its registered tools."""
    try:
        bases = _agents.resolve_targets(
            explicit_dir=args.dir,
            agent=getattr(args, "agent", None),
            scope_global=getattr(args, "global_scope", False),
            all_agents=getattr(args, "all_agents", False),
        )
    except KeyError:
        sys.exit(f"unknown agent {getattr(args, 'agent', None)!r} — see `treg agents ls` for names")
    with _client(cfg) as c:
        r = c.get("/bundles")
        if r.status_code >= 400:
            _show(r); return
        bundles = r.json()
        if args.all:
            targets = bundles
        elif getattr(args, "names", None):   # onboarding: a chosen SUBSET → one call, one summary
            targets = [b for b in bundles if b.get("name") in args.names]
        elif args.name:
            targets = [b for b in bundles if b.get("name") == args.name]
            if not targets:
                sys.exit(f"no skill named {args.name!r} in this org (see `treg skill ls`)")
        else:
            sys.exit("give a skill name or --all")
        seen: set[str] = set()
        skipped_existing: list[str] = []   # already on disk (in every base) — surfaced at the end
        n = 0
        for b in targets:
            name = b.get("name") or ""
            if name in seen:                        # duplicate bundle name (--all) — install once
                continue
            seen.add(name)
            # A bundle name becomes a filesystem path — reject anything that isn't a single, safe segment
            # (a name with '/' or '..' could escape a base dir).
            if not name or "/" in name or "\\" in name or name in ("..", "."):
                print(f"  ✗ {name!r}: unsafe skill name — skipped"); continue
            d = c.get(f"/bundles/{b['id']}")
            if d.status_code >= 400:
                print(f"  ✗ {name}: {d.status_code}"); continue
            bundle = d.json()
            recipe = bundle.get("recipe") or ""
            if not recipe.strip():
                print(f"  · {name}: no recipe — skipped"); continue
            wrote_to: list[Path] = []
            kept_in: list[Path] = []
            extra_files = 0
            for base in bases:
                dest = base / name
                skill_md = dest / "SKILL.md"
                if skill_md.exists() and not args.force:   # don't clobber a hand-edited local skill silently
                    kept_in.append(base); continue
                dest.mkdir(parents=True, exist_ok=True)
                skill_md.write_text(recipe)
                extra_files = _write_bundle_files(dest, bundle.get("files") or {})  # the rest of the folder
                wrote_to.append(base)
            tools = bundle.get("tools") or []
            extra = f"  (tools: {', '.join(t['name'] for t in tools)} — call via `treg call`)" if tools else ""
            more = f"  +{extra_files} file(s)" if extra_files else ""
            if wrote_to:
                where = ", ".join(str(p) for p in wrote_to)
                print(f"  ✓ {name:<28} → {where}{more}{extra}"); n += 1
            else:   # existed in every target base
                print(f"  · {name:<28} already on disk — kept your copy")
                skipped_existing.append(name)
    where_all = ", ".join(str(p) for p in bases)
    print(f"\nInstalled {n} skill(s) into {where_all}")
    if skipped_existing:
        # Surface the skips as an actionable next step so a caller (agent or human) DECIDES, rather
        # than burying "use --force" per-line. The Access instruction defers to this output.
        joined = ", ".join(skipped_existing)
        print(f"\n{_AM}⚠ {len(skipped_existing)} skill(s) already existed locally and were kept "
              f"(not overwritten):{_R} {joined}")
        print(f"  To replace one with the team's version:  {_B}treg skill install <name> --force{_R}")
        print(f"  {_M}Overwrites local edits — confirm before you --force.{_R}")


def cmd_skill_bootstrap(args, cfg) -> None:
    """Fetch the official tools-registry skill from the server and drop it into every detected agent's
    skills dir, so whatever agent the user runs already knows how to use treg. install.sh calls this
    right after installing the CLI; it's also runnable by hand. Global (per-user) scope by default —
    it runs outside any project — with `--project` to target repo-local dirs instead."""
    base_url = (cfg.get("base_url") or "https://treg.superdesign.dev").rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/skill.md", timeout=15, follow_redirects=True)
        resp.raise_for_status()
        recipe = resp.text
    except Exception as exc:  # noqa: BLE001 — network/HTTP; report and exit non-zero for install.sh
        sys.exit(f"could not fetch the treg skill from {base_url}: {exc}")
    if not recipe.strip():
        sys.exit("the server returned an empty skill")
    bases = _agents.resolve_targets(scope_global=not args.project, all_agents=args.all_agents)
    n = 0
    for b in bases:
        dest = b / "tools-registry"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(recipe)
        print(f"  ✓ tools-registry → {dest / 'SKILL.md'}"); n += 1
    detected = _agents.detect_installed()
    tail = f"detected: {', '.join(detected)}" if detected else "no agents detected — used sensible defaults"
    print(f"\nInstalled the treg skill into {n} location(s)  ({tail}).")


def cmd_agents_ls(args, cfg) -> None:
    """Show every agent treg knows how to install skills for, its project + global skills dirs, and
    which are actually installed on this machine (● detected / ○ not)."""
    detected = set(_agents.detect_installed())
    rows = [(name, meta["display"], meta["project"], str(meta["global_"]()), name in detected)
            for name, meta in _agents.AGENTS.items()]
    print(f"{_A}Agents treg can install skills for{_R}  ({_G}●{_R} detected here · {_M}○{_R} not)\n")
    name_w = max(len(r[0]) for r in rows)
    proj_w = max(len(r[2]) for r in rows)
    for name, _display, proj, glob, det in rows:
        mark = f"{_G}●{_R}" if det else f"{_M}○{_R}"
        print(f"  {mark} {name:<{name_w}}  project={proj:<{proj_w}}  global={glob}")
    print(f"\n{_M}Default fan-out (no --agent):{_R} {', '.join(_agents.DEFAULT_PROJECT_DIRS)}")
    print(f"{_M}`treg skill install <name>` writes to those; `--agent <name>` / `--all-agents` / `--global` to widen.{_R}")


def _write_bundle_files(dest: Path, files: dict) -> int:
    """Reconstruct a skill's companion files under `dest`, nested paths intact. Path-safety: each file
    must stay INSIDE dest (reject absolute/`..`/secret-dir paths — a malicious bundle can't escape)."""
    dest = dest.resolve()
    written = 0
    for rel, content in (files or {}).items():
        rel = str(rel).replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in rel.split("/") or rel == "SKILL.md" or not isinstance(content, str):
            continue
        target = (dest / rel).resolve()
        if not (target == dest or dest in target.parents):  # must not escape dest
            print(f"    ✗ unsafe path skipped: {rel}"); continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1
    return written


def cmd_health(args, cfg) -> None:
    with _client(cfg) as c:
        if args.run:
            with _spinner("running health checks against each provider"):
                r = c.post("/health/run")
            _show(r)
        else:
            _show(c.get("/health"))


def cli_version() -> str:
    """The installed treg version (from package metadata; falls back for an editable/source run)."""
    try:
        from importlib.metadata import version
        return version("tools-registry")
    except Exception:
        return "0.0.1"


def cmd_version(args, cfg) -> None:
    print(f"treg {cli_version()}")


def cmd_update(args, cfg) -> None:
    """Re-run the server's install.sh to upgrade the CLI in place (uv/pipx/pip, from the git repo)."""
    import subprocess
    base = (cfg.get("base_url") or "https://treg.superdesign.dev").rstrip("/")
    print(f"Updating treg from {base}/install.sh …")
    with _client(cfg, auth=False) as c:
        r = c.get("/install.sh")
    if r.status_code >= 400:
        sys.exit(f"could not fetch the installer ({r.status_code}) from {base}/install.sh")
    rc = subprocess.run(["sh", "-c", r.text]).returncode  # the installer prints its own progress
    sys.exit(rc)


# ---- orgs (teams) ------------------------------------------------------------------------
def cmd_org_create(args, cfg) -> None:
    with _client(cfg) as c:
        r = c.post("/orgs", json={"name": args.name})
    if r.status_code == 200:
        d = r.json()
        cfg["active_org"] = d["org"]
        if not cfg.get("identity"):  # per-org-token mode needs the new org's token to act in it
            cfg["token"] = d["token"]
        _save_config(cfg)
    _show(r)


def cmd_org_ls(args, cfg) -> None:
    with _client(cfg) as c:
        r = c.get("/orgs")
    if r.status_code != 200:
        _show(r)
        return
    active = _effective_org(cfg)
    for o in r.json():
        mark = "*" if o["slug"] == active else " "
        print(f"{mark} {o['slug']:<22} {o['name']:<22} {o['role']:<7}{'  (active)' if o['slug'] == active else ''}")


def cmd_org_use(args, cfg) -> None:
    cfg["active_org"] = args.slug
    _save_config(cfg)
    print(f"active org: {args.slug}")


def _org_tool_names(c, org_id) -> list[str]:
    r = c.get("/tools")
    return sorted(t["name"] for t in r.json()) if r.status_code == 200 else []


def _resolve_tool_access(c, org_id, args) -> list[str] | None:
    """Turn the access flags into the API's `tool_access` (None = all tools, else the allowed names).
    --all-tools → None; --tools a,b → that list; otherwise (interactively) offer all-or-customise."""
    if getattr(args, "all_tools", False):
        return None
    if getattr(args, "tools", None):
        return [t.strip() for t in args.tools.split(",") if t.strip()]
    if not sys.stdin.isatty():
        return None  # non-interactive default: all tools
    names = _org_tool_names(c, org_id)
    if not names:
        return None
    if input(f"Give access to all {len(names)} tools? [Y/n]: ").strip().lower() in ("", "y", "yes"):
        return None
    try:  # a checklist (all pre-checked) — uncheck the ones to withhold
        chosen = _checkbox("Tools this member may use", [{"name": n, "checked": True} for n in names]).ask()
        return None if chosen is None or set(chosen) >= set(names) else sorted(chosen)
    except Exception:  # noqa: BLE001 — questionary absent → fall back to a typed list
        raw = input("Comma-separated tool names to allow (blank = all): ").strip()
        return [t.strip() for t in raw.split(",") if t.strip()] or None


def cmd_org_invite(args, cfg) -> None:
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        body = {"email": args.email, "role": args.role, "expires_days": args.expires_days,
                "tool_access": _resolve_tool_access(c, org_id, args),
                "local_run_enabled": getattr(args, "local_run", "on") != "off"}
        _show(c.post(f"/orgs/{org_id}/invites", json=body))


def cmd_org_access(args, cfg) -> None:
    """Set which tools a member may use + whether they can run locally. Unspecified fields keep their
    current value (so `--local-run off` alone doesn't wipe a custom tool list)."""
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        members = c.get(f"/orgs/{org_id}/members")
        if members.status_code >= 400:
            _show(members); return
        cur = next((m for m in members.json() if m["user_id"] == args.user_id), None)
        if cur is None:
            sys.exit(f"treg: user {args.user_id} is not a member of this org")
        # tool_access: explicit flag wins; else keep current (unless nothing set + interactive → prompt)
        if getattr(args, "all_tools", False) or getattr(args, "tools", None):
            access = _resolve_tool_access(c, org_id, args)
        else:
            access = cur.get("tool_access")
        local = cur.get("local_run_enabled", True) if getattr(args, "local_run", None) is None \
            else args.local_run != "off"
        _show(c.patch(f"/orgs/{org_id}/members/{args.user_id}/access",
                      json={"tool_access": access, "local_run_enabled": local}))


def cmd_org_invites(args, cfg) -> None:
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        _show(c.get(f"/orgs/{org_id}/invites"))


def cmd_org_revoke(args, cfg) -> None:
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        _show(c.delete(f"/orgs/{org_id}/invites/{args.invite_id}"))


def cmd_org_members(args, cfg) -> None:
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        _show(c.get(f"/orgs/{org_id}/members"))


def cmd_org_set_role(args, cfg) -> None:
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        _show(c.patch(f"/orgs/{org_id}/members/{args.user_id}", json={"role": args.role}))


def cmd_org_join(args, cfg) -> None:
    with _client(cfg, auth=False) as c:
        r = c.post("/invites/accept", json={"code": args.code, "email": args.email})
    if r.status_code == 200:
        d = r.json()
        cfg.update(token=d["token"], active_org=d["org"], email=args.email, identity=False)
        _save_config(cfg)
    _show(r)


def _clear_active_if_targeted(cfg: dict) -> None:
    """Clear the stored active org only if THIS command acted on it. A one-shot `--org <slug>`
    override must not wipe an unrelated stored active org you never left/deleted."""
    if _ORG_OVERRIDE is None or _ORG_OVERRIDE == cfg.get("active_org"):
        cfg["active_org"] = None
    _save_config(cfg)


def cmd_org_leave(args, cfg) -> None:
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        r = c.post(f"/orgs/{org_id}/leave")
    if r.status_code == 200:
        _clear_active_if_targeted(cfg)
    _show(r)


def cmd_org_delete(args, cfg) -> None:
    eff = _effective_org(cfg)
    if args.slug != eff:
        sys.exit(f"refusing: name the org to delete — it must match the active org ({eff!r}), got {args.slug!r}")
    with _client(cfg) as c:
        org_id = _active_org_id(cfg, c)
        if org_id is None:
            sys.exit("no active org")
        r = c.delete(f"/orgs/{org_id}")
    if r.status_code == 200:
        _clear_active_if_targeted(cfg)
    _show(r)


# ---- super-admin --------------------------------------------------------------------------
def cmd_admin_login(args, cfg) -> None:
    cfg["admin_token"] = args.token
    _save_config(cfg)
    print("admin token saved")


def _admin_get(cfg, path: str) -> None:
    with _admin_client(cfg) as c:
        _show(c.get(path))


def cmd_admin_stats(args, cfg) -> None: _admin_get(cfg, "/admin/stats")
def cmd_admin_orgs(args, cfg) -> None: _admin_get(cfg, "/admin/orgs")
def cmd_admin_org(args, cfg) -> None: _admin_get(cfg, f"/admin/orgs/{args.org_id}")
def cmd_admin_users(args, cfg) -> None: _admin_get(cfg, "/admin/users")
def cmd_admin_tools(args, cfg) -> None: _admin_get(cfg, "/admin/tools")
def cmd_admin_calls(args, cfg) -> None: _admin_get(cfg, f"/admin/calls?limit={args.limit}")
def cmd_admin_health(args, cfg) -> None: _admin_get(cfg, "/admin/health")


def cmd_admin_grant(args, cfg) -> None:
    with _admin_client(cfg) as c:
        _show(c.post(f"/admin/users/{args.user_id}/superadmin", json={"value": True}))


def cmd_admin_revoke(args, cfg) -> None:
    with _admin_client(cfg) as c:
        _show(c.post(f"/admin/users/{args.user_id}/superadmin", json={"value": False}))


def cmd_admin_suspend_user(args, cfg) -> None:
    with _admin_client(cfg) as c:
        _show(c.post(f"/admin/users/{args.user_id}/suspend", json={"value": not args.undo}))


def cmd_admin_rm_user(args, cfg) -> None:
    with _admin_client(cfg) as c:
        _show(c.delete(f"/admin/users/{args.user_id}"))


def cmd_admin_suspend_org(args, cfg) -> None:
    with _admin_client(cfg) as c:
        _show(c.post(f"/admin/orgs/{args.org_id}/suspend", json={"value": not args.undo}))


def cmd_admin_rm_org(args, cfg) -> None:
    with _admin_client(cfg) as c:
        _show(c.delete(f"/admin/orgs/{args.org_id}"))


def cmd_oauth_connect(args, cfg) -> None:
    try:
        cs = json.loads(Path(args.client_secret).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"could not read client-secret JSON {args.client_secret!r}: {exc}")
    block = cs.get("installed") or cs.get("web") or cs
    if not isinstance(block, dict) or not block.get("client_id") or not block.get("client_secret"):
        sys.exit("client-secret JSON is missing client_id / client_secret (expected a Google OAuth client file)")
    body = {"name": args.name, "client_id": block["client_id"], "client_secret": block["client_secret"],
            "auth_uri": block.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": block.get("token_uri", "https://oauth2.googleapis.com/token"), "scopes": args.scopes}
    with _client(cfg) as c:
        r = c.post("/oauth/start", json=body)
        if r.status_code != 200:
            _show(r)
            return
        d = r.json()
        print(f"\n1. Ensure this redirect URI is allowed:\n   {d['redirect_uri']}")
        print(f"\n2. Open to authorize:\n   {d['consent_url']}\n\nWaiting…")
        for _ in range(150):
            time.sleep(2)
            try:
                s = c.get(f"/oauth/status/{d['state']}").json()
                status = s.get("status")
            except Exception:  # a flaky/non-JSON status poll shouldn't abort the whole wait
                continue
            if status == "done":
                print(f"✅ Connected. New oauth secret id: {s.get('secret_id')} ({s.get('name')})")
                return
            if status == "error":
                sys.exit(f"❌ Failed: {s.get('detail')}")  # non-zero exit on a failed connect
        sys.exit("Timed out waiting for authorization.")


# ---- parser ------------------------------------------------------------------------------
_RAWFMT = argparse.RawDescriptionHelpFormatter


def _ex(*lines: str) -> str:
    """A copy-paste 'Examples' block for a subcommand's --help epilog."""
    return "Examples:\n  " + "\n  ".join(lines)


def _pop_org_flag(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--org":
            if i + 1 >= len(argv):
                raise SystemExit("--org requires a value (an org slug)")
            argv.pop(i); return argv.pop(i)
        if a.startswith("--org="):
            argv.pop(i); return a.split("=", 1)[1]
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="treg", formatter_class=_RAWFMT,
        description="tools-registry (treg): call your team's APIs through one proxy, with no keys on your machine.",
        epilog=_ex(
            "treg login                                              # sign in; first login registers you",
            "treg add stripe --base-url https://api.stripe.com --secret STRIPE_KEY",
            "treg call https://api.stripe.com/v1/charges             # key injected server-side",
            "treg scan                                               # what would upload? (read-only)",
            "treg upload                                             # register a .env + a skills folder",
        ) + "\n\nGlobal: prepend `--org <slug>` to run any command in that team. `treg <command> -h` for details.")
    p.add_argument("--version", action="version", version=f"treg {cli_version()}", help="print the treg version and exit")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    def mk(parent, name, help_, *examples, **kw):  # subparser with description + a copy-paste Examples epilog
        return parent.add_parser(name, help=help_, description=help_,
                                 epilog=(_ex(*examples) if examples else None), formatter_class=_RAWFMT, **kw)

    # ---- setup / auth ----
    c = mk(sub, "config", "Show or set the registry this CLI talks to (base URL).",
           "treg config                                    # show current base URL",
           "treg config --base-url https://treg.superdesign.dev")
    c.add_argument("--base-url", help="point the CLI at this registry URL")
    c.set_defaults(fn=cmd_config)

    lg = mk(sub, "login", "Sign in. Opens the browser sign-in page; --email for a terminal-only code; --token for agents/CI.",
            "treg login                                     # browser (reuses a dashboard session, or GitHub/Google/email)",
            "treg login --email you@company.com             # emailed 6-digit code",
            "treg login --token <per-org-token>             # non-interactive (agents/CI)")
    lg.add_argument("--token", help="a per-org token (agents/CI) instead of the browser/email flow")
    lg.add_argument("--email", help="sign in with a one-time code emailed to this address")
    lg.set_defaults(fn=cmd_login)

    mk(sub, "logout", "Clear the stored credentials for this machine.",
       "treg logout").set_defaults(fn=cmd_logout)

    mk(sub, "update", "Upgrade the treg CLI in place (re-runs the registry's installer).",
       "treg update").set_defaults(fn=cmd_update)
    mk(sub, "version", "Print the installed treg version.", "treg version").set_defaults(fn=cmd_version)

    ob = mk(sub, "onboard", "First-run: Set up (share skills+keys) · Access (pull your team's) · Demo.",
            "treg onboard                                   # you're asked which path",
            "treg onboard --path access                     # pull your team's shared skills + a test call",
            "treg onboard --path setup --source global      # share skills from ~/.claude/skills etc., not this repo",
            "treg onboard --path demo --yes                 # non-interactive demo")
    ob.add_argument("--path", choices=["setup", "access", "demo"], help="which onboarding path (else you're asked)")
    ob.add_argument("--source", choices=["local", "global", "both"],
                    help="setup path: import from this project, your global agent skill folders (~/.claude/skills, ~/.codex/skills, …), or both (else you're asked)")
    ob.add_argument("--mode", choices=["guided", "quick"], help=argparse.SUPPRESS)  # back-compat: quick→demo
    ob.add_argument("--name", help="team name for the demo path (default: Acme Demo)")
    ob.add_argument("--yes", action="store_true", help="non-interactive: accept defaults, no pauses")
    ob.add_argument("--reset", action="store_true", help="remove your demo team(s) / teammates")
    ob.set_defaults(fn=cmd_onboard)

    mk(sub, "invites", "List invites addressed to your email (accept with `treg accept`).",
       "treg invites").set_defaults(fn=cmd_invites)
    acp = mk(sub, "accept", "Accept an invite addressed to your email (no code needed).",
             "treg accept superdesign")
    acp.add_argument("org", help="org slug (or invite id) to accept")
    acp.set_defaults(fn=cmd_accept)

    # ---- teams ----
    og = mk(sub, "org", "Manage teams (orgs): create, switch, invite, members, join, leave, delete.",
            "treg org ls", 'treg org create "Superdesign"', "treg org use superdesign",
            ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    oc2 = mk(og, "create", "Create a team and become its owner.", 'treg org create "Superdesign"')
    oc2.add_argument("name", help="the team's display name"); oc2.set_defaults(fn=cmd_org_create)
    mk(og, "ls", "List the teams you belong to (marks the active one).", "treg org ls").set_defaults(fn=cmd_org_ls)
    ou = mk(og, "use", "Switch the active team (used by later commands).", "treg org use superdesign")
    ou.add_argument("slug", help="the org slug to make active"); ou.set_defaults(fn=cmd_org_use)
    oi = mk(og, "invite", "Invite someone to the active team by email (choose their tool access).",
            "treg org invite bob@company.com --role member",
            "treg org invite bob@company.com --tools stripe,gh   # only these tools",
            "treg org invite bob@company.com --all-tools --local-run off")
    oi.add_argument("email", help="the invitee's email"); oi.add_argument("--role", default="member", choices=["viewer", "member", "admin"], help="role to grant (default: member)")
    oi.add_argument("--expires-days", type=int, default=7, help="invite validity in days (default: 7)")
    oi.add_argument("--tools", help="comma-separated tool names this member may use (default: prompt / all)")
    oi.add_argument("--all-tools", dest="all_tools", action="store_true", help="grant access to every tool (skip the prompt)")
    oi.add_argument("--local-run", dest="local_run", choices=["on", "off"], help="allow local CLI runs (default: on)")
    oi.set_defaults(fn=cmd_org_invite)
    oa = mk(og, "access", "Set which tools a member may use + whether they can run locally (admin+).",
            "treg org access 5 --tools stripe,gh", "treg org access 5 --all-tools", "treg org access 5 --local-run off")
    oa.add_argument("user_id", type=int, help="the member's user id (from `org members`)")
    oa.add_argument("--tools", help="comma-separated tool names to allow (replaces their current list)")
    oa.add_argument("--all-tools", dest="all_tools", action="store_true", help="give access to every tool")
    oa.add_argument("--local-run", dest="local_run", choices=["on", "off"], help="allow/forbid local CLI runs for this member")
    oa.set_defaults(fn=cmd_org_access)
    mk(og, "invites", "List pending invites for the active team (admin+).", "treg org invites").set_defaults(fn=cmd_org_invites)
    orv = mk(og, "revoke", "Revoke a pending invite before it's used.", "treg org revoke 3")
    orv.add_argument("invite_id", type=int, help="the invite id (from `org invites`)"); orv.set_defaults(fn=cmd_org_revoke)
    mk(og, "members", "List the active team's members and their roles.", "treg org members").set_defaults(fn=cmd_org_members)
    osr = mk(og, "set-role", "Change a member's role (owner only).", "treg org set-role 5 admin")
    osr.add_argument("user_id", type=int, help="the member's user id (from `org members`)")
    osr.add_argument("role", choices=["viewer", "member", "admin", "owner"], help="the new role"); osr.set_defaults(fn=cmd_org_set_role)
    oj = mk(og, "join", "Join a team using an invite code.", "treg org join <code> --email you@company.com")
    oj.add_argument("code", help="the one-time invite code"); oj.add_argument("--email", required=True, help="your email (creates you if new)"); oj.set_defaults(fn=cmd_org_join)
    mk(og, "leave", "Remove yourself from the active team.", "treg org leave").set_defaults(fn=cmd_org_leave)
    od = mk(og, "delete", "Delete a team you own (confirms by name).", "treg org delete superdesign")
    od.add_argument("slug", help="the org slug to delete"); od.set_defaults(fn=cmd_org_delete)

    # ---- secrets ----
    s = mk(sub, "secret", "Manage stored credentials (encrypted server-side, never returned).",
           "treg secret add STRIPE_KEY --value sk_live_…", "treg secret ls",
           ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    sa = mk(s, "add", "Store a secret (a value, an .env var, a file, or auto-found in a skill dir).",
            "treg secret add STRIPE_KEY --value sk_live_123",
            "treg secret add AHREFS_API_KEY --env-var AHREFS_API_KEY   # read + parse it from ./.env",
            "treg secret add gcp --file creds.json --kind secret_file")
    sa.add_argument("name", help="a name to reference this secret by")
    sa.add_argument("--value", help="the secret value inline")
    sa.add_argument("--env-var", dest="env_var", help="read the value of this variable from an .env (correctly parsed; value stays off the command line)")
    sa.add_argument("--env-file", dest="env_file", help="the .env to read --env-var from (default: ./.env)")
    sa.add_argument("--file", help="read the value from this file")
    sa.add_argument("--dir", help="auto-find the secret file in a skill dir"); sa.add_argument("--kind", default="env", help="env | oauth | secret_file | cli_auth (default: env)")
    sa.set_defaults(fn=cmd_secret_add)
    mk(s, "ls", "List your secrets (names + kinds; never values).", "treg secret ls").set_defaults(fn=cmd_secret_ls)
    sr = mk(s, "rm", "Delete a secret by id.", "treg secret rm 4")
    sr.add_argument("id", type=int, help="the secret id (from `secret ls`)"); sr.set_defaults(fn=cmd_secret_rm)
    suu = mk(s, "update", "Rename a secret, change its value, or its kind.", "treg secret update 4 --value sk_live_new")
    suu.add_argument("id", type=int, help="the secret id"); suu.add_argument("--name", help="new name"); suu.add_argument("--value", help="new value"); suu.add_argument("--kind", help="new kind"); suu.set_defaults(fn=cmd_secret_update)

    # ---- tools (add + the friendly shortcut) ----
    ad2 = mk(sub, "add", "Register a tool (friendly shortcut for `tool add`). --secret takes a name or id.",
             "treg add stripe --base-url https://api.stripe.com --secret STRIPE_KEY",
             "treg add gh --base-url https://api.github.com --secret GITHUB_TOKEN --format 'Bearer {secret}'")
    ad2.add_argument("name", help="a name for this tool (used in `treg call <name>`)")
    ad2.add_argument("--base-url", help="the upstream API root, e.g. https://api.stripe.com")
    ad2.add_argument("--base", help=argparse.SUPPRESS)  # alias for --base-url
    ad2.add_argument("--secret", help="the secret to inject, by NAME or id")
    ad2.add_argument("--header", help="header name to inject into (default: Authorization)")
    ad2.add_argument("--format", help="injection format (default: 'Bearer {secret}')")
    ad2.set_defaults(fn=cmd_add)

    t = mk(sub, "tool", "Manage tools (the full form; `treg add` is the quick path).",
           "treg tool ls", "treg tool add stripe --base-url https://api.stripe.com --secret 1",
           ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    ta = mk(t, "add", "Register a tool with full control over the credential binding(s).",
            "treg tool add stripe --base-url https://api.stripe.com --secret 1",
            "treg tool add ads --base-url https://api.x.com --bind 'secret=1' --bind 'secret=2,name=developer-token'")
    ta.add_argument("name", help="a name for this tool")
    ta.add_argument("--base-url", required=True, help="the upstream API root")
    ta.add_argument("--secret", type=int, help="secret id for a single default (Bearer) binding")
    ta.add_argument("--bind", action="append", help="a binding 'secret=<id>,name=<Header>,format=<fmt>,…' (repeatable)")
    ta.add_argument("--binding", action="append", help="a raw binding as JSON (repeatable)")
    ta.add_argument("--health", help="a health-check as JSON, e.g. '{\"path\":\"me\"}'")
    ta.add_argument("--injector", default="env", help="env | oauth | secret_file | cli_auth (default: env)")
    ta.add_argument("--auth-in", default="header", help="header | query (default: header)")
    ta.add_argument("--auth-name", default="Authorization", help="header/param name (default: Authorization)")
    ta.add_argument("--auth-format", default="Bearer {secret}", help="injection format (default: 'Bearer {secret}')")
    ta.add_argument("--secret-field", default="access_token", help="JSON field for file/oauth secrets (default: access_token)")
    ta.set_defaults(fn=cmd_tool_add)
    mk(t, "ls", "List registered tools (names, hosts, bindings).", "treg tool ls").set_defaults(fn=cmd_tool_ls)
    tr = mk(t, "rm", "Delete a tool by id.", "treg tool rm 2")
    tr.add_argument("id", type=int, help="the tool id (from `tool ls`)"); tr.set_defaults(fn=cmd_tool_rm)
    tu = mk(t, "update", "Change a tool's base URL, bindings, or health-check.", "treg tool update 2 --base-url https://api.stripe.com/v2")
    tu.add_argument("id", type=int, help="the tool id"); tu.add_argument("--base-url", help="new base URL")
    tu.add_argument("--bind", action="append", help="replace bindings (repeatable)"); tu.add_argument("--binding", action="append", help="replace bindings, raw JSON (repeatable)")
    tu.add_argument("--health", help="new health-check JSON")
    tu.add_argument("--local-run", choices=["on", "off"], help="allow/forbid `treg run` (local tier) for this tool (owner opt-in)")
    tu.set_defaults(fn=cmd_tool_update)

    # ---- calling ----
    cl = mk(sub, "call", "Call a tool through the proxy: `call <tool> <path>` or `call <full-url>`. Key injected server-side.",
            "treg call stripe v1/charges", "treg call https://api.stripe.com/v1/charges",
            "treg call posthog api/events --query limit=5", "treg call slack chat.postMessage --method POST --data '{\"channel\":\"C1\"}'")
    cl.add_argument("target", help="a tool name, or a full upstream URL")
    cl.add_argument("path", nargs="?", default="", help="the path when using a tool name")
    cl.add_argument("--method", default="GET", help="HTTP method (default: GET)")
    cl.add_argument("--query", action="append", default=[], metavar="K=V", help="a query param (repeatable)")
    cl.add_argument("--data", help="request body (string)"); cl.add_argument("--file", help="request body from a file")
    cl.set_defaults(fn=cmd_call)

    ca = mk(sub, "calls", "Show the audit log: who called what, when, and the result.", "treg calls --limit 20")
    ca.add_argument("--limit", type=int, default=50, help="how many recent calls (default: 50)"); ca.set_defaults(fn=cmd_calls)

    rn = mk(sub, "run",
            "Run a vendor CLI with the org's credential injected. Default (--local): runs on THIS machine "
            "(no login, nothing on disk). --server: runs on the registry server (Tier 0), streaming output back.",
            "treg run stripe -- get /v1/balance", "treg run gh -- pr list",
            "treg run --server agentmail-cli inboxes list")
    rn.add_argument("tool", help="the registered tool whose CLI to run (same name for --local and --server)")
    rn.add_argument("args", nargs=argparse.REMAINDER, metavar="-- <cli args>", help="everything after the tool name goes to the CLI verbatim")
    rng = rn.add_mutually_exclusive_group()
    rng.add_argument("--local", action="store_true", help="run on this machine (default; credential isolated under the treg-run user)")
    rng.add_argument("--server", action="store_true", help="run on the registry server (Tier 0) instead of locally")
    rn.add_argument("--timeout", type=int, help="[--server] max seconds on the server (default 120, cap 600)")
    rn.add_argument("--fs-jail", dest="fs_jail", action="store_true",
                    help="[--local] confine the CLI's file writes to a private scratch (macOS) — stops it "
                         "dropping the key in a member-readable file; also blocks the CLI writing output files")
    rn.set_defaults(fn=cmd_run)

    rns = mk(sub, "runs", "Show the CLI-run audit log: who ran which skill, when, and the exit code.", "treg runs --limit 20")
    rns.add_argument("--limit", type=int, default=50, help="how many recent runs (default: 50)"); rns.set_defaults(fn=cmd_runs)

    # hidden: invoked as the treg-run user by the installed runner (never called by a human directly)
    rh = sub.add_parser("__run-helper")
    rh.add_argument("tool")
    rh.add_argument("args", nargs=argparse.REMAINDER)
    rh.set_defaults(fn=cmd_run_helper)

    sl = mk(sub, "setup-local-run",
            "One-time admin setup: create the treg-run user + install the isolated runner (run with sudo).",
            "sudo treg setup-local-run")
    sl.add_argument("--member", help="the OS user allowed to run (default: the invoking sudo user)")
    sl.add_argument("--run-proof", default="", help="the server's TREG_RUN_PROOF value — enables running "
                    "SHARED-key tools (ones you don't own) locally; without it only your own-key tools run")
    sl.add_argument("--registry", help="registry URL treg-run must reach for /grant (default: the member's configured base_url)")
    sl.add_argument("--no-egress", dest="no_egress", action="store_true",
                    help="skip the network allow-list (treg-run keeps unrestricted egress)")
    sl.add_argument("--refresh-egress", dest="refresh_egress", action="store_true",
                    help="only re-resolve + reinstall the egress allow-list (host IPs drift over time)")
    sl.set_defaults(fn=cmd_setup_local_run)

    # ---- shell mode ----
    sh = mk(sub, "shell",
            "Open a shell where your team's registered CLIs run with the credential injected — use "
            "`stripe`, `gh`, … normally, no keys on your machine, every call audited.",
            "treg shell start", "treg shell stop",
            ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    shs = mk(sh, "start", "Start the treg shell (a subshell; registered CLIs are transparently injected).",
             "treg shell start",
             "treg shell start --server-for stripe,render   # run those on the server (no key on this machine)",
             "treg shell start --ttl 60                     # auto-close after 60 minutes")
    shs.add_argument("--server-for", dest="server_for", metavar="a,b",
                     help="comma-separated tools to run on the SERVER instead of locally (key never touches "
                          "this machine); only applies to server-runnable tools, others fall back to local")
    shs.add_argument("--ttl", type=int, metavar="MIN",
                     help="close the shell automatically after this many minutes (default: no limit)")
    shs.set_defaults(fn=cmd_shell_start)
    sht = mk(sh, "stop", "Leave the treg shell (same as typing `exit` or Ctrl-D).", "treg shell stop")
    sht.set_defaults(fn=cmd_shell_stop)

    # ---- skills ----
    sk = mk(sub, "skill", "Register / manage skills (a recipe + its secrets + tool(s), as one bundle).",
            "treg skill init --dir ./my-skill", "treg skill add --dir ./my-skill", "treg skill install seo-blog-writer",
            ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    sc = mk(sk, "scaffold", "Emit a skill-registration manifest stub for a folder (bindings need completing).", "treg skill scaffold ./my-skill")
    sc.add_argument("dir", help="the skill directory"); sc.add_argument("--out", help="write to this file instead of stdout"); sc.set_defaults(fn=cmd_skill_scaffold)
    si = mk(sk, "init", "Draft a treg.json for a skill dir (guesses base_url, finds secrets).", "treg skill init --dir ./my-skill")
    si.add_argument("--dir", required=True, help="the skill directory"); si.add_argument("--out", help="write the treg.json here"); si.set_defaults(fn=cmd_skill_init)
    sad = mk(sk, "add", "Register a skill folder (recipe + secrets + tool) from its treg.json.", "treg skill add --dir ./my-skill")
    sad.add_argument("--dir", required=True, help="the skill directory (must contain treg.json)"); sad.set_defaults(fn=cmd_skill_add)
    sp = mk(sk, "push", "Register a skill from a prepared manifest file.", "treg skill push ./manifest.json")
    sp.add_argument("file", help="the manifest JSON file"); sp.set_defaults(fn=cmd_skill_push)
    mk(sk, "ls", "List registered skills (bundles).", "treg skill ls").set_defaults(fn=cmd_skill_ls)
    skr = mk(sk, "rm", "Delete a skill (bundle) by id.", "treg skill rm 1")
    skr.add_argument("id", type=int, help="the bundle id (from `skill ls`)"); skr.set_defaults(fn=cmd_skill_rm)
    ski = mk(sk, "install", "Pull a shared skill into every agent's skills dir (.agents/skills + .claude/skills).",
             "treg skill install seo-blog-writer", "treg skill install --all", "treg skill install foo --agent cursor")
    ski.add_argument("name", nargs="?", help="the skill name (omit with --all)")
    ski.add_argument("--all", action="store_true", help="install every skill in the org")
    ski.add_argument("--dir", help="pin one explicit target directory (skips agent fan-out)")
    ski.add_argument("--agent", help="install for one agent only (see `treg agents ls`)")
    ski.add_argument("--all-agents", dest="all_agents", action="store_true",
                     help="fan out to every known agent's dir, not just the default two")
    ski.add_argument("--global", dest="global_scope", action="store_true",
                     help="write into detected-installed agents' GLOBAL dirs (not the project)")
    ski.add_argument("--force", action="store_true", help="overwrite an existing local SKILL.md")
    ski.set_defaults(fn=cmd_skill_install)

    skb = mk(sk, "bootstrap", "Install the official treg skill into every detected agent (used by the installer).",
             "treg skill bootstrap", "treg skill bootstrap --all-agents")
    skb.add_argument("--all-agents", dest="all_agents", action="store_true",
                     help="every known agent's dir, not just the ones detected on this machine")
    skb.add_argument("--project", action="store_true", help="write into project dirs instead of the per-user global dirs")
    skb.set_defaults(fn=cmd_skill_bootstrap)

    # ---- agents (which coding agents treg installs skills for) ----
    ag = mk(sub, "agents", "List the coding agents treg can install skills for (and which are detected here).",
            "treg agents ls").add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    mk(ag, "ls", "Show every known agent + its skills dirs + detection status.",
       "treg agents ls").set_defaults(fn=cmd_agents_ls)

    # ---- scan + upload (the bulk on-ramp; `import` kept as a deprecated alias of upload) ----
    #   `clis` (our auto-import mode) scans the MACHINE for installed catalog CLIs; env/skills scan a dir.
    _clis_flags = lambda p: (  # noqa: E731 — the clis-only registration flags, shared by scan + upload
        p.add_argument("--status", action="store_true", help="clis: scan + report only, register nothing"),
        p.add_argument("--add", metavar="BIN", help="clis: register an INSTALLED cli that's not in the catalog (prompts for its key env var + API base_url)"),
        p.add_argument("--env", metavar="VAR", help="clis --add: env var the cli reads its key from (blank = it authenticates via its own login/config)"),
        p.add_argument("--base-url", help="clis --add: the provider's API base_url for the tool"))

    sc = mk(sub, "scan", "Scan a directory / machine (read-only): list the keys, skills & CLIs treg would upload. Nothing leaves this machine.",
            "treg scan                                      # both .env + skills in this dir",
            "treg scan env                                  # just the .env keys",
            "treg scan clis                                 # installed CLIs treg can register",
            "treg scan skills --dir ~/.claude/skills")
    sc.add_argument("mode", nargs="?", choices=["env", "skills", "clis"], help="restrict to one side (env|skills|clis); omit for env + skills")
    sc.add_argument("--dir", help="base directory (default: cwd): its .env and its skill subdirs")
    sc.add_argument("--env-file", help="explicit path to the env file (overrides --dir/.env)")
    sc.add_argument("--skills-dir", help="explicit skills directory (overrides --dir)")
    sc.add_argument("--select", help="comma-separated names to show (else everything)")
    _clis_flags(sc)
    sc.set_defaults(fn=cmd_import, as_scan=True, dry_run=True, all=True, replace=False, no_oauth=True,
                    llm=False, llm_token=None, llm_model=LLM_DEFAULT_MODEL, llm_base_url=LLM_DEFAULT_BASE)

    def _upload_args(parser):
        parser.add_argument("mode", nargs="?", choices=["env", "skills", "clis"], help="restrict to one side (env|skills|clis); omit for env + skills")
        parser.add_argument("--dir", help="base directory (default: cwd): its .env and its skill subdirs")
        parser.add_argument("--env-file", help="explicit path to the env file (overrides --dir/.env)")
        parser.add_argument("--skills-dir", help="explicit skills directory (overrides --dir)")
        parser.add_argument("--select", help="comma-separated names to upload (else interactive)")
        parser.add_argument("--all", action="store_true", help="upload everything detected without prompting")
        parser.add_argument("--replace", action="store_true", help="delete-then-recreate anything already registered (re-run safe)")
        parser.add_argument("--no-oauth", action="store_true", help="skip the per-provider OAuth connect prompts")
        parser.add_argument("--llm", action="store_true", help="resolve UNKNOWN keys with an LLM (OpenAI-compatible)")
        parser.add_argument("--llm-token", help="LLM API token (or set TREG_LLM_TOKEN)")
        parser.add_argument("--llm-model", default=LLM_DEFAULT_MODEL, help=f"LLM model (default: {LLM_DEFAULT_MODEL})")
        parser.add_argument("--llm-base-url", default=LLM_DEFAULT_BASE, help="OpenAI-compatible base URL (default: Gemini)")
        _clis_flags(parser)
        parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)  # works, but we teach `treg scan`
        parser.set_defaults(fn=cmd_import, as_scan=False)

    up = mk(sub, "upload", "Upload a directory / machine: register its .env provider keys, skill subdirs, AND/OR installed CLIs (encrypted server-side).",
            "treg upload                                    # both .env + skills in this dir",
            "treg upload env --select openai,stripe        # just picked provider keys",
            "treg upload clis                               # register installed catalog CLIs",
            "treg upload skills --dir ~/.claude/skills --all",
            "treg scan                                      # preview first; nothing leaves the machine")
    _upload_args(up)
    # `import` still works as a silent back-compat alias (old scripts / cached agent
    # instructions), but it is deliberately absent from --help: we only teach scan/upload.
    im = sub.add_parser("import", description="(deprecated) old name for `treg upload`.", formatter_class=_RAWFMT)
    _upload_args(im)

    # ---- health + oauth ----
    he = mk(sub, "health", "Show tool/secret health, or run the checks now with --run.",
            "treg health", "treg health --run")
    he.add_argument("--run", action="store_true", help="run every tool's health check now"); he.set_defaults(fn=cmd_health)

    oa = mk(sub, "oauth", "Connect an OAuth credential via the hosted browser-consent flow.",
            "treg oauth connect gsc --client-secret ./client_secret.json --scopes https://www.googleapis.com/auth/webmasters.readonly",
            ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    oc = mk(oa, "connect", "Mint an auto-refreshed OAuth secret through browser consent.",
            "treg oauth connect gsc --client-secret ./client_secret.json --scopes <scope> <scope>")
    oc.add_argument("name", help="a name for the resulting oauth secret")
    oc.add_argument("--client-secret", required=True, help="path to the OAuth client-secret JSON")
    oc.add_argument("--scopes", nargs="+", default=[], help="one or more OAuth scopes"); oc.set_defaults(fn=cmd_oauth_connect)

    # ---- super-admin ----
    ad = mk(sub, "admin", "Super-admin (cross-tenant): platform-wide view + control.",
            "treg admin login --token <admin-token>", "treg admin stats", "treg admin orgs",
            ).add_subparsers(dest="sub", required=True, metavar="<subcommand>")
    al = mk(ad, "login", "Save the super-admin bearer token for later admin commands.", "treg admin login --token <admin-token>")
    al.add_argument("--token", required=True, help="the cross-tenant admin token"); al.set_defaults(fn=cmd_admin_login)
    mk(ad, "stats", "Platform-wide counts and health.", "treg admin stats").set_defaults(fn=cmd_admin_stats)
    mk(ad, "orgs", "List every org across all tenants.", "treg admin orgs").set_defaults(fn=cmd_admin_orgs)
    ao = mk(ad, "org", "Inspect one org by id.", "treg admin org 2")
    ao.add_argument("org_id", type=int, help="the org id"); ao.set_defaults(fn=cmd_admin_org)
    mk(ad, "users", "List every user.", "treg admin users").set_defaults(fn=cmd_admin_users)
    mk(ad, "tools", "List every tool across all orgs.", "treg admin tools").set_defaults(fn=cmd_admin_tools)
    ac = mk(ad, "calls", "The cross-tenant audit log.", "treg admin calls --limit 100")
    ac.add_argument("--limit", type=int, default=50, help="how many recent calls (default: 50)"); ac.set_defaults(fn=cmd_admin_calls)
    mk(ad, "health", "Platform-wide health rollup.", "treg admin health").set_defaults(fn=cmd_admin_health)
    ag = mk(ad, "grant", "Grant a user super-admin.", "treg admin grant 5")
    ag.add_argument("user_id", type=int, help="the user id"); ag.set_defaults(fn=cmd_admin_grant)
    arv = mk(ad, "revoke", "Revoke a user's super-admin.", "treg admin revoke 5")
    arv.add_argument("user_id", type=int, help="the user id"); arv.set_defaults(fn=cmd_admin_revoke)
    asu = mk(ad, "suspend-user", "Suspend (or --undo) a user platform-wide.", "treg admin suspend-user 5", "treg admin suspend-user 5 --undo")
    asu.add_argument("user_id", type=int, help="the user id"); asu.add_argument("--undo", action="store_true", help="un-suspend instead"); asu.set_defaults(fn=cmd_admin_suspend_user)
    aru = mk(ad, "rm-user", "Delete a user platform-wide.", "treg admin rm-user 5")
    aru.add_argument("user_id", type=int, help="the user id"); aru.set_defaults(fn=cmd_admin_rm_user)
    aso = mk(ad, "suspend-org", "Suspend (or --undo) an org platform-wide.", "treg admin suspend-org 2", "treg admin suspend-org 2 --undo")
    aso.add_argument("org_id", type=int, help="the org id"); aso.add_argument("--undo", action="store_true", help="un-suspend instead"); aso.set_defaults(fn=cmd_admin_suspend_org)
    aro = mk(ad, "rm-org", "Delete an org platform-wide.", "treg admin rm-org 2")
    aro.add_argument("org_id", type=int, help="the org id"); aro.set_defaults(fn=cmd_admin_rm_org)
    return p


def main(argv: list[str] | None = None) -> None:
    global _ORG_OVERRIDE
    argv = list(sys.argv[1:] if argv is None else argv)
    override = _pop_org_flag(argv)
    args = build_parser().parse_args(argv)
    cfg = _load_config()
    if override:
        _ORG_OVERRIDE = override
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
