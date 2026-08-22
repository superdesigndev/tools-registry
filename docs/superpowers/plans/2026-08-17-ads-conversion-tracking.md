# Google Ads Conversion Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record signup, first successful call, and first top-up as Google Ads conversions attributed to the ad click that produced them.

**Architecture:** A `gclid` captured in a first-party cookie on landing is persisted onto the `Org` at signup. Three server-side chokepoints write rows into a durable `AdConversion` outbox table inside the same transaction as the event they describe. A background worker drains unsent rows and uploads them to the Google Ads API. Nothing routes through `audit.py` or `analytics.py` — both shed rows under load, which is correct for charts and wrong for conversions.

**Tech Stack:** FastAPI, SQLModel, httpx, Google Ads API v22. Design doc: `docs/superpowers/specs/2026-08-17-ads-conversion-tracking-design.md`.

## Global Constraints

- Branch is `feat/ads-conversion-tracking`. **Commit with `git commit -- <paths>` (path-scoped)** — there is unrelated staged work in `src/treg/api.py`, `docs/context/interface/api.md` and `src/treg/web/landing.html` that must never enter a commit. A plain `git commit` takes the whole index and would sweep that work in.
- **A path-scoped commit cannot see an untracked file.** For any file this plan *creates*, run `git add <path>` first, then `git commit -- <path>`. The `add` stages only that path; the path-scoped commit still ignores everything else in the index. Skipping the `add` fails with `pathspec … did not match any file(s) known to git`.
- **Always `uv run --frozen`.** Never run `uv lock` or `uv sync` — on an older uv it rewrites `uv.lock` into an older format, a ~650-line diff changing no versions. Hand-add dependencies to the lock instead.
- **No new dependencies.** `httpx` and SQLModel are already present.
- **Never import a heavy dependency at the top of a CLI-path module** (`cli.py`, `convert.py`, `skills.py`, `providers.py`, `localrun.py`, `shell.py`, `agents.py`, `egress.py`, `fsjail.py`). Nothing in this plan touches those.
- **Money is integer micro-USD.** Never floats, never cents. The FX conversion is integer arithmetic only.
- **`ledger.py` is the only path that moves money**; this feature never writes money, it only reads amounts already credited.
- Migrations use `create_all` + a guarded `ALTER` in `db.py`. The project does **not** use Alembic.
- Test suite: `uv run --frozen python -m pytest -q`.
- Fixed FX rate: **1 AUD = 0.70 USD, set 2026-08-17**. `aud_micro = usd_micro * 10 // 7`.
- Live conversion action IDs on account `5149790776`: signup `7723667014`, first call `7723667017`, first top-up `7723667020`.
- Google Ads API version is **v22**. v21 returns `UNSUPPORTED_VERSION`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/treg/adsconv.py` | **new** — FX helper, action constants, `queue()` outbox writer, uploader worker. All Ads-conversion logic lives here. |
| `src/treg/models.py` | **modify** — four new `Org` columns, new `AdConversion` table. |
| `src/treg/db.py` | **modify** — guarded `ALTER`s for the new `Org` columns. |
| `src/treg/config.py` | **modify** — three settings that gate the feature off by default. |
| `src/treg/api.py` | **modify** — gclid read at signup, two fire chokepoints, worker start in `lifespan`. |
| `src/treg/billing.py` | **modify** — first top-up chokepoint inside `_credit()`. |
| `src/treg/web/adtrack.js` | **new** — the ~10-line first-party capture snippet. |
| `tests/test_adsconv.py` | **new** — FX, outbox, dedupe, uploader payload and drain. |
| `docs/context/architecture/ads-conversions.md` | **new** — the subsystem fragment CLAUDE.md requires. |

---

### Task 1: FX conversion and action constants

Pure functions with no I/O — the easiest thing to get exactly right, and everything else depends on the numbers.

**Files:**
- Create: `src/treg/adsconv.py`
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `usd_micro_to_aud_micro(usd_micro: int) -> int`; string constants `ACTION_SIGNUP = "signup"`, `ACTION_FIRST_CALL = "first_call"`, `ACTION_PAID = "paid"`; `CONVERSION_ACTION_IDS: dict[str, str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adsconv.py
from treg import adsconv


def test_usd_to_aud_uses_fixed_rate():
    # 1 AUD = 0.70 USD, so USD converts UP into AUD: US$20.00 -> A$28.57
    assert adsconv.usd_micro_to_aud_micro(20_000_000) == 28_571_428


def test_usd_to_aud_is_integer_only():
    # No float ever appears: 1 micro-USD must not become 1.4285... micro-AUD
    result = adsconv.usd_micro_to_aud_micro(1)
    assert isinstance(result, int)
    assert result == 1


def test_usd_to_aud_zero_and_negative():
    assert adsconv.usd_micro_to_aud_micro(0) == 0
    # A refund/negative should not silently flip sign under floor division
    assert adsconv.usd_micro_to_aud_micro(-7_000_000) == -10_000_000


def test_action_ids_cover_every_action():
    assert set(adsconv.CONVERSION_ACTION_IDS) == {
        adsconv.ACTION_SIGNUP, adsconv.ACTION_FIRST_CALL, adsconv.ACTION_PAID
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'treg.adsconv'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/treg/adsconv.py
"""Google Ads conversion tracking — the outbox and its uploader.

Unlike audit.py and analytics.py, which are deliberately droppable, a conversion that is
lost is a conversion Google never learns about, and the bidding is then trained on
undercounted data. So the write is DURABLE (a row, in the caller's transaction) and only
the UPLOAD is asynchronous. Nothing here may route through audit.py.
"""

from __future__ import annotations

# Fixed FX, set 2026-08-17: 1 AUD = 0.70 USD. Deliberately a constant rather than a live
# rate so reported conversion value stays stable — a change in ROAS should mean the
# business moved, not that the currency market did. Revisit if the rate drifts far.
AUD_PER_USD_NUM = 10
AUD_PER_USD_DEN = 7

ACTION_SIGNUP = "signup"
ACTION_FIRST_CALL = "first_call"
ACTION_PAID = "paid"

# Created live on account 5149790776 on 2026-08-17 (type UPLOAD_CLICKS).
CONVERSION_ACTION_IDS: dict[str, str] = {
    ACTION_SIGNUP: "7723667014",
    ACTION_FIRST_CALL: "7723667017",
    ACTION_PAID: "7723667020",
}


def usd_micro_to_aud_micro(usd_micro: int) -> int:
    """Convert integer micro-USD to integer micro-AUD at the fixed rate.

    Integer-only, per the money-code rule: a float here would round differently on
    different platforms and the value is uploaded as a monetary amount.
    """
    return usd_micro * AUD_PER_USD_NUM // AUD_PER_USD_DEN
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ads): fixed-rate USD->AUD conversion and conversion action ids" -- src/treg/adsconv.py tests/test_adsconv.py
```

---

### Task 2: Schema — Org columns and the AdConversion outbox

**Files:**
- Modify: `src/treg/models.py` (add to `class Org`, add new table after `class Secret`)
- Modify: `src/treg/db.py` (guarded ALTERs alongside the existing `(A33)`/`(A34)` block)
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Org.ad_gclid: str | None`, `Org.ad_click_at: datetime | None`, `Org.ad_landing: str | None`, `Org.first_call_at: datetime | None`; `AdConversion(id, org_id, action, dedupe_key, value_usd_micro, created_at, uploaded_at, attempts, error)` with a unique constraint on `(org_id, action)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_adsconv.py
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from treg.db import session_maker
from treg.models import AdConversion, Org


async def test_ad_conversion_is_unique_per_org_and_action(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-adsconv")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        db.add(AdConversion(org_id=org.id, action="signup", dedupe_key="signup"))
        await db.commit()

        db.add(AdConversion(org_id=org.id, action="signup", dedupe_key="signup"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_org_has_ad_attribution_columns(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-adcols", ad_gclid="ABC123", ad_landing="p2")
        db.add(org)
        await db.commit()
        got = (await db.execute(select(Org).where(Org.slug == "t-adcols"))).scalar_one()
        assert got.ad_gclid == "ABC123"
        assert got.ad_landing == "p2"
        assert got.first_call_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL with `ImportError: cannot import name 'AdConversion'`

> Fixture note: `clients` (`tests/conftest.py:144`) yields **one** `AsyncClient`, already authed as a registered user — it is not a tuple. Direct DB access goes through `session_maker` imported from `treg.db`, which is the pattern `tests/test_billing.py` uses. Taking `clients` as a parameter is still required even when a test only touches the DB: the fixture is what calls `reset_db()`.

- [ ] **Step 3: Add the columns and the table**

In `src/treg/models.py`, inside `class Org`, after the `balance_micro` field:

```python
    # ---- Google Ads attribution (see adsconv.py) --------------------------------------------------
    # The click that produced this team, captured as a first-party cookie on landing and persisted
    # here at signup. Kept for the life of the team: a top-up weeks later still attributes to it.
    ad_gclid: str | None = Field(default=None)
    ad_click_at: datetime | None = Field(default=None)
    ad_landing: str | None = Field(default=None)  # utm_content — the landing page id (p1…p5)
    # Set ONCE, by a guarded UPDATE in the /call/ handler. Deliberately not derived from CallRecord:
    # audit.py sheds rows past its queue bound, so a derived value undercounts exactly under load.
    first_call_at: datetime | None = Field(default=None)
```

Then add the table (place it after `class Secret`):

```python
class AdConversion(SQLModel, table=True):
    """One conversion owed to Google Ads — an OUTBOX row, not a log line.

    Written synchronously inside the transaction of the event it describes, so the event and its
    pending conversion commit or fail together. A background worker uploads it later; until then
    `uploaded_at` is NULL. The unique constraint on (org_id, action) is what makes every fire site
    idempotent — a webhook redelivery or a retried signup bounces off it instead of double-counting.
    """

    __table_args__ = (UniqueConstraint("org_id", "action", name="uq_adconversion_org_action"),)

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="org.id")
    action: str  # adsconv.ACTION_* — "signup" | "first_call" | "paid"
    dedupe_key: str = Field(default="")  # provenance (e.g. the Stripe PaymentIntent id); not the key
    value_usd_micro: int = Field(default=0)  # converted to AUD at upload time, never stored as AUD
    # `_now`, NOT `datetime.now(timezone.utc)`: these columns are TIMESTAMP WITHOUT TIME ZONE and
    # asyncpg rejects a tz-aware value into a naive column. SQLite is lax, so a tz-aware default
    # passes every test here and fails on the Postgres deploy target. See `_now` at the top of this
    # file — 24 other tables already follow it.
    created_at: datetime = Field(default_factory=_now)
    uploaded_at: datetime | None = Field(default=None, index=True)
    attempts: int = Field(default=0)
    error: str = Field(default="")  # last upload error; a permanent failure stops the retries
```

Ensure `UniqueConstraint` is imported at the top of `models.py` (`from sqlalchemy import UniqueConstraint`) — check whether it is already there before adding, since other tables may use it.

- [ ] **Step 4: Add the guarded migration**

In `src/treg/db.py`, immediately after the `(A34)` block:

```python
    # (A35) additive: org ad-attribution columns + first_call_at. `create_all` builds them on a
    # fresh database; this is for one created before this feature shipped. All nullable, so no
    # backfill is meaningful — a team that predates the ads work has no click to attribute to.
    if "org" in tables:
        org_cols = {c["name"] for c in insp.get_columns("org")}
        for col, ddl in (("ad_gclid", "VARCHAR"), ("ad_landing", "VARCHAR"),
                         ("ad_click_at", "TIMESTAMP"), ("first_call_at", "TIMESTAMP")):
            if col not in org_cols:
                conn.execute(text(f"ALTER TABLE org ADD COLUMN {col} {ddl}"))
```

- [ ] **Step 5: Run the tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS

- [ ] **Step 6: Run the whole suite to prove no schema regression**

Run: `uv run --frozen python -m pytest -q`
Expected: no new failures versus the pre-task baseline. Record the baseline first if you have not.

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(ads): Org attribution columns and the AdConversion outbox table" -- src/treg/models.py src/treg/db.py tests/test_adsconv.py
```

---

### Task 3: The outbox writer

**Files:**
- Modify: `src/treg/adsconv.py`
- Modify: `src/treg/config.py`
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: `AdConversion`, `Org` from Task 2; `ACTION_*` from Task 1.
- Produces: `async def queue(db: AsyncSession, org: Org, action: str, *, value_usd_micro: int = 0, dedupe_key: str = "") -> bool` — returns `True` if a row was written. Adds settings `google_ads_customer_id: str`, `ads_conv_org_slug: str`, `ads_conv_enabled` via `enabled()`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_adsconv.py
async def test_queue_writes_one_row_and_is_idempotent(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-queue", ad_gclid="CLICK1")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is True
        await db.commit()
        # Second call for the same (org, action) must be a silent no-op, not an error
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
        await db.commit()

        rows = (await db.execute(
            select(AdConversion).where(AdConversion.org_id == org.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].uploaded_at is None


async def test_queue_is_a_noop_without_a_gclid(clients):
    # Organic signups are the majority; they must not fill the outbox with unattributable rows.
    async with session_maker() as db:
        org = Org(name="t", slug="t-noclick")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL with `AttributeError: module 'treg.adsconv' has no attribute 'queue'`

- [ ] **Step 3: Add the settings**

In `src/treg/config.py`, next to the existing `google_ads_*` settings (around line 233):

```python
    # Google Ads conversion upload. Empty customer id = the whole feature is OFF (tests stay inert,
    # self-hosters send nothing) — the same gate shape analytics.py uses for posthog_key.
    google_ads_customer_id: str = ""
    # Which team's google-ads OAuth connection the uploader authenticates as. treg uploads to its
    # OWN ad account, so this is a platform setting, never a per-tenant one.
    ads_conv_org_slug: str = ""
```

- [ ] **Step 4: Implement `queue()` and `enabled()`**

Append to `src/treg/adsconv.py`:

```python
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import get_settings
from .models import AdConversion, Org


def enabled() -> bool:
    """Empty customer id = OFF. Keeps the test suite and self-hosted instances inert."""
    s = get_settings()
    return bool(s.google_ads_customer_id and s.ads_conv_org_slug)


async def queue(db: AsyncSession, org: Org, action: str, *,
                value_usd_micro: int = 0, dedupe_key: str = "") -> bool:
    """Record that `org` owes Google a conversion. Returns True if a row was written.

    Call this INSIDE the caller's transaction: the event and its pending conversion must commit
    together, or a crash between them loses a conversion with no trace.

    A no-op when the team has no click to attribute to, which is most teams. Duplicate fires are
    absorbed by the unique constraint rather than checked for first — the check-then-insert race
    is real under concurrent webhook redelivery.
    """
    if not org.ad_gclid:
        return False
    try:
        # A SAVEPOINT, not a bare flush: this runs inside the CALLER's transaction (the signup
        # grant, the Stripe credit), and a plain `db.rollback()` on the duplicate would roll back
        # THEIR work too — a redelivered webhook would undo a credit. The nested block confines the
        # rollback to this insert.
        async with db.begin_nested():
            db.add(AdConversion(org_id=org.id, action=action, dedupe_key=dedupe_key,
                                value_usd_micro=value_usd_micro))
    except IntegrityError:
        return False
    return True
```

- [ ] **Step 5: Run the tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(ads): durable outbox writer, gated off by default" -- src/treg/adsconv.py src/treg/config.py tests/test_adsconv.py
```

---

### Task 4: Capture the gclid — client snippet and signup persistence

**Files:**
- Create: `src/treg/web/adtrack.js`
- Modify: `src/treg/api.py` (serve the file; read the cookie in `register_user()` at ~3814 and `create_org()` at ~3877)
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: `Org` columns from Task 2.
- Produces: `_ad_attribution_from(request) -> tuple[str, str]` in `api.py`, returning `(gclid, landing)`; a `/adtrack.js` route.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_adsconv.py
async def test_signup_persists_the_gclid_cookie(clients):
    r = await clients.post(
        "/users",
        json={"email": "click@example.com"},
        cookies={"treg_ad": "CLICK_XYZ|p3"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid == "CLICK_XYZ"
        assert org.ad_landing == "p3"
        assert org.ad_click_at is not None


async def test_signup_without_the_cookie_leaves_attribution_null(clients):
    r = await clients.post("/users", json={"email": "organic@example.com"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid is None
```

> Confirm the registration route and payload shape against `register_user()` in `api.py` before running — if the path is not `/users` or the body needs more fields, match what the endpoint actually accepts.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL — `org.ad_gclid` is `None`, assertion error on `CLICK_XYZ`

- [ ] **Step 3: Write the capture snippet**

```javascript
// src/treg/web/adtrack.js
// First-party ad-click capture. No Google script, no third-party request: this reads the click id
// off our own URL and stores it in our own cookie, which the signup POST then carries to the server.
// gbraid/wbraid are what Google substitutes for gclid on iOS traffic — omitting them silently drops
// a large share of mobile conversions.
(function () {
  try {
    var q = new URLSearchParams(window.location.search);
    var id = q.get('gclid') || q.get('gbraid') || q.get('wbraid');
    if (!id) return;
    // utm_content is what _measurement.md specifies, but the use-case pages' own CTAs carry the
    // page id as ?ref=p1 (see the logged-out redirect in index.html). Read either, so attribution
    // does not come back empty on whichever convention a given page happens to use.
    var landing = q.get('utm_content') || q.get('ref') || '';
    // 90 days: Google's click-through conversion window. Lax so it survives the top-level
    // navigation from the ad, which is a cross-site GET.
    var v = encodeURIComponent(id + '|' + landing);
    document.cookie = 'treg_ad=' + v + ';path=/;max-age=7776000;samesite=lax' +
      (window.location.protocol === 'https:' ? ';secure' : '');
  } catch (e) { /* never break the page for a marketing cookie */ }
})();
```

- [ ] **Step 4: Serve it and read it at signup**

In `src/treg/api.py`, add a route next to the other static file routes (e.g. near the `/usecase.css` route at ~2323):

```python
@app.get("/adtrack.js", include_in_schema=False)
async def adtrack_js():
    f = _WEB_DIR / "adtrack.js"
    if not f.exists():
        raise HTTPException(status_code=404, detail="adtrack.js not bundled")
    return FileResponse(f, media_type="application/javascript")
```

Add the helper above `register_user()`:

```python
def _ad_attribution_from(request: Request) -> tuple[str, str]:
    """Read the first-party ad cookie set by /adtrack.js. Returns ("", "") when absent."""
    raw = request.cookies.get("treg_ad") or ""
    if not raw:
        return "", ""
    gclid, _, landing = unquote(raw).partition("|")
    return gclid.strip()[:255], landing.strip()[:64]
```

Ensure `from urllib.parse import unquote` is imported in `api.py`; check first, it may already be.

In **both** `register_user()` and `create_org()`, after the org exists and before `await db.commit()`:

```python
    gclid, landing = _ad_attribution_from(request)
    if gclid:
        org.ad_gclid = gclid
        org.ad_landing = landing or None
        org.ad_click_at = _utcnow_naive()   # naive UTC: asyncpg rejects tz-aware into a
                                           # TIMESTAMP WITHOUT TIME ZONE column (see models._now).
                                           # api.py already defines _utcnow_naive (~line 3832).
        db.add(org)
```

Both endpoints must take `request: Request` — `create_org` may not currently. Add the parameter if missing.

- [ ] **Step 5: Add the snippet to the homepage**

In `src/treg/web/index.html`, immediately before `</body>`:

```html
<script src="/adtrack.js"></script>
```

For the five `usecase-*.html` pages and `resources.html`, see the CONTROLLER ADDENDUM at the end of
this brief — they are now in scope, but are GENERATED and must not be hand-edited.

- [ ] **Step 6: Run the tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(ads): first-party gclid capture, persisted to the org at signup" -- src/treg/web/adtrack.js src/treg/web/index.html src/treg/api.py tests/test_adsconv.py
```

---

### Task 5: Fire on signup

**Files:**
- Modify: `src/treg/api.py` (`_grant_signup_promo()` at ~3748)
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: `adsconv.queue()` from Task 3; attribution columns from Task 4.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_adsconv.py
async def test_signup_queues_a_conversion_when_attributed(clients):
    r = await clients.post("/users", json={"email": "conv@example.com"},
                              cookies={"treg_ad": "CLICK_SIGNUP|p1"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == r.json()["org_id"]))).scalars().all()
        assert [x.action for x in rows] == [adsconv.ACTION_SIGNUP]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py::test_signup_queues_a_conversion_when_attributed -q`
Expected: FAIL — the row list is empty

- [ ] **Step 3: Fire it**

In `_grant_signup_promo()` in `src/treg/api.py`, inside the existing `try:` block after `await ledger.grant(db, org.id)`:

```python
        # Same door, same once-only guarantee: this function is already the single place a brand-new
        # real team comes into existence. A queue failure must not fail the signup, so it rides the
        # existing except below rather than getting its own.
        await adsconv.queue(db, org, adsconv.ACTION_SIGNUP)
        await db.commit()
```

Add `from . import adsconv` to the imports at the top of `api.py`.

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ads): queue a signup conversion from the promo-grant chokepoint" -- src/treg/api.py tests/test_adsconv.py
```

---

### Task 6: Fire on first successful call

**Files:**
- Modify: `src/treg/api.py` (the `/call/{rest:path}` handler at ~9098)
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: `adsconv.queue()`; `Org.first_call_at`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

First add a self-contained fixture near the top of `tests/test_adsconv.py`. The shape is copied from
the `env` fixture in `tests/test_access.py`, trimmed to just one callable HTTP tool:

```python
# tests/test_adsconv.py — add near the imports
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from conftest import make_upstream
from treg.api import app
from treg.db import reset_db, session_maker


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def callenv():
    """An ad-attributed org with one callable HTTP tool pointed at the fake upstream."""
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()),
                                 base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.post("/users", json={"email": "caller@example.com"})
        assert r.status_code == 200, r.text
        token, org_id = r.json()["token"], r.json()["org_id"]
        sid = (await c.post("/secrets", headers=_h(token),
                            json={"name": "a-key", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(token),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        async with session_maker() as db:            # attribute the org to an ad click
            org = await db.get(Org, org_id)
            org.ad_gclid = "CLICK_CALL"
            db.add(org)
            await db.commit()
        yield SimpleNamespace(c=c, token=token, org_id=org_id)
    await app.state.http.aclose()
```

Then the tests:

```python
# append to tests/test_adsconv.py
async def test_first_successful_call_fires_once(callenv):
    """Two successful calls: one timestamp, one conversion. The second must be a no-op."""
    r1 = await callenv.c.get("/call/alpha", headers=_h(callenv.token))
    assert 200 <= r1.status_code < 400, r1.text
    r2 = await callenv.c.get("/call/alpha", headers=_h(callenv.token))
    assert 200 <= r2.status_code < 400, r2.text

    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        assert org.first_call_at is not None
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == callenv.org_id,
            AdConversion.action == adsconv.ACTION_FIRST_CALL))).scalars().all()
        assert len(rows) == 1


async def test_unattributed_org_records_timestamp_but_no_conversion(callenv):
    """first_call_at is a product metric and must be set for every team; only ad-clicked ones queue."""
    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        org.ad_gclid = None
        db.add(org)
        await db.commit()

    assert (await callenv.c.get("/call/alpha", headers=_h(callenv.token))).status_code < 400
    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        assert org.first_call_at is not None
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == callenv.org_id))).scalars().all()
        assert rows == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL

- [ ] **Step 3: Implement the guarded update**

In the `/call/{rest:path}` handler, after the upstream response is known to be successful and before returning:

```python
    # First successful call — the metric that decides whether a vertical is real (see
    # marketing/landing/_measurement.md). A CONDITIONAL UPDATE, not a read-then-write: concurrent
    # first calls would both see NULL and both fire. Deliberately NOT derived from CallRecord —
    # audit.py sheds rows past its queue bound and would undercount exactly under load.
    if 200 <= response.status_code < 400 and caller.org_id:
        updated = (await db.execute(
            update(Org)
            .where(Org.id == caller.org_id, Org.first_call_at.is_(None))
            .values(first_call_at=_utcnow_naive())   # naive UTC — see models._now; asyncpg rejects tz-aware
        )).rowcount
        if updated:
            org_row = await db.get(Org, caller.org_id)
            if org_row is not None:
                await adsconv.queue(db, org_row, adsconv.ACTION_FIRST_CALL)
            await db.commit()
```

Ensure `update` is imported from `sqlalchemy` in `api.py`. Use the `_utcnow_naive()` helper that
already exists in `api.py` (defined around line 3832) — do NOT add a second copy, and do NOT use
`datetime.now(timezone.utc)`: `first_call_at` is a naive column and asyncpg rejects tz-aware values. Match the handler's real variable names for the response and the caller — the names above are indicative, read the surrounding code and use what is actually in scope.

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite — this touches the hot path**

Run: `uv run --frozen python -m pytest -q`
Expected: no new failures. `/call/` is the busiest route in the system; a regression here is serious.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(ads): set first_call_at and queue its conversion on the call path" -- src/treg/api.py tests/test_adsconv.py
```

---

### Task 7: Fire on first top-up

**Files:**
- Modify: `src/treg/billing.py` (`_credit()`)
- Test: `tests/test_billing.py` — the webhook machinery (`_org`, `_deliver`, `_pi_event`, `_no_sdk`) already lives there; duplicating it into `test_adsconv.py` would be a second way to stand up the same thing.

**Interfaces:**
- Consumes: `adsconv.queue()`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Add these imports to `tests/test_billing.py` (`Org`, `session_maker` are already imported):

```python
from sqlmodel import select

from treg import adsconv
from treg.models import AdConversion
```

```python
# append to tests/test_billing.py
async def test_first_topup_queues_exactly_one_ad_conversion(c, monkeypatch):
    """Stripe delivers at least once; a redelivery must not double-count the conversion."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.ad_gclid = "CLICK_PAID"
        db.add(org)
        await db.commit()

    event = _pi_event(org_id, pi="pi_ads_once", cents=2000)   # US$20.00
    assert (await _deliver(c, event)).status_code == 200
    assert (await _deliver(c, event)).status_code == 200      # the redelivery

    async with session_maker() as db:
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == org_id,
            AdConversion.action == adsconv.ACTION_PAID))).scalars().all()
        assert len(rows) == 1
        assert rows[0].value_usd_micro == 20_000_000
        assert rows[0].dedupe_key == "pi_ads_once"


async def test_a_second_topup_queues_nothing_further(c, monkeypatch):
    """We measure becoming a payer, not revenue — a second, different payment adds no conversion."""
    org_id, owner = await _org(c)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.ad_gclid = "CLICK_PAID"
        db.add(org)
        await db.commit()

    assert (await _deliver(c, _pi_event(org_id, pi="pi_first", cents=2000))).status_code == 200
    assert (await _deliver(c, _pi_event(org_id, pi="pi_second", cents=5000))).status_code == 200

    async with session_maker() as db:
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == org_id,
            AdConversion.action == adsconv.ACTION_PAID))).scalars().all()
        assert len(rows) == 1
        assert rows[0].dedupe_key == "pi_first"   # the FIRST payment is the one recorded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL

- [ ] **Step 3: Fire it**

In `_credit()` in `src/treg/billing.py`, inside the existing `if fresh:` block:

```python
        # First top-up only: `fresh` is already the "this delivery moved money" branch, and the
        # outbox's unique (org_id, action) makes a second top-up a silent no-op. We measure becoming
        # a payer, not revenue — value-based bidding needs volume treg does not have yet.
        if org is not None:
            await adsconv.queue(db, org, adsconv.ACTION_PAID,
                                value_usd_micro=amount_micro, dedupe_key=pi_id)
            await db.commit()
```

Add `from . import adsconv` to `billing.py`. Note `org` is already loaded in that branch, but only inside the `autotopup_failures` conditional — hoist the `org = await db.get(Org, org_id)` above that conditional so it is always available.

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py tests/test_billing.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ads): queue the first-top-up conversion from the credit branch" -- src/treg/billing.py tests/test_billing.py
```

---

### Task 8: The uploader

**Files:**
- Modify: `src/treg/adsconv.py`
- Modify: `src/treg/api.py` (`lifespan` at ~140)
- Test: `tests/test_adsconv.py`

**Interfaces:**
- Consumes: everything above; `oauth.ensure_fresh(secret, db, client)`.
- Produces: `def build_payload(rows: list[AdConversion], orgs: dict[int, Org]) -> dict`; `async def drain_once(db, client) -> dict`; `async def worker(session_factory, client) -> None`.

- [ ] **Step 1: Write the failing test for the payload**

```python
# append to tests/test_adsconv.py
from datetime import datetime, timedelta, timezone


def test_build_payload_converts_currency_and_formats_time():
    click = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1", ad_click_at=click)
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_PAID,
                       value_usd_micro=20_000_000,
                       created_at=click + timedelta(hours=6))
    payload = adsconv.build_payload([row], {1: org})
    conv = payload["conversions"][0]
    assert conv["gclid"] == "CLICK1"
    assert conv["conversionAction"].endswith("/conversionActions/7723667020")
    # US$20.00 at the fixed rate -> A$28.571428
    assert conv["conversionValue"] == pytest.approx(28.571428, rel=1e-6)
    assert conv["currencyCode"] == "AUD"
    assert payload["partialFailure"] is True


def test_build_payload_omits_value_for_non_revenue_actions():
    org = Org(id=1, name="t", slug="t", ad_gclid="C", ad_click_at=datetime.now(timezone.utc))
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP, value_usd_micro=0)
    conv = adsconv.build_payload([row], {1: org})["conversions"][0]
    assert "conversionValue" not in conv
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: FAIL with `AttributeError: module 'treg.adsconv' has no attribute 'build_payload'`

- [ ] **Step 3: Implement `build_payload`**

```python
def _utcnow_naive() -> datetime:
    """Naive UTC. Our datetime columns are TIMESTAMP WITHOUT TIME ZONE and asyncpg rejects a
    tz-aware value into one; see `_now` in models.py, which 24 other tables already follow.
    `api.py` has its own copy of this for the same reason — it is private to that module."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


API_VERSION = "v22"  # v21 is blocked: "Version v21 is deprecated" (verified 2026-08-17)
_UPLOAD_DELAY_S = 6 * 3600   # Google will not accept a conversion until hours after the click
_MAX_ATTEMPTS = 8


def _conversion_time(dt: datetime) -> str:
    """Ads wants 'yyyy-mm-dd hh:mm:ss+hh:mm'. ISO with a 'Z' is rejected.

    `dt` comes out of the database as NAIVE UTC (see models._now), so it is stamped with UTC, not
    converted. `.astimezone()` on a naive value would read it as LOCAL time — on the Sydney deploy
    target that shifts every conversion by 10-11 hours, which Google would either reject as
    pre-dating the click or attribute to the wrong day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def build_payload(rows: list[AdConversion], orgs: dict[int, Org]) -> dict:
    """Turn outbox rows into an uploadClickConversions body.

    `partialFailure` so one bad row cannot reject the batch — results are read per row.
    Value is converted to the ACCOUNT's currency here, at upload time; the outbox stores USD so a
    rate change never rewrites history.
    """
    cid = get_settings().google_ads_customer_id
    conversions = []
    for row in rows:
        org = orgs.get(row.org_id)
        if org is None or not org.ad_gclid:
            continue
        conv = {
            "gclid": org.ad_gclid,
            "conversionAction": f"customers/{cid}/conversionActions/{CONVERSION_ACTION_IDS[row.action]}",
            "conversionDateTime": _conversion_time(row.created_at),
        }
        if row.value_usd_micro:
            conv["conversionValue"] = usd_micro_to_aud_micro(row.value_usd_micro) / 1_000_000
            conv["currencyCode"] = "AUD"
        conversions.append(conv)
    return {"conversions": conversions, "partialFailure": True}
```

The division by `1_000_000` is the one permitted float: the Ads API's `conversionValue` field is a
double, so a decimal amount is what the wire format requires. The *arithmetic* stayed integral.

- [ ] **Step 4: Run the payload tests**

Run: `uv run --frozen python -m pytest tests/test_adsconv.py -q`
Expected: PASS

- [ ] **Step 5: Write the failing test for the drain**

```python
# append to tests/test_adsconv.py
async def test_drain_marks_rows_uploaded_and_skips_young_ones(clients, monkeypatch):
    """A row younger than the upload delay is left alone; an old one is sent and marked."""
    monkeypatch.setattr(adsconv, "enabled", lambda: True)
    sent = []

    class FakeResp:
        status_code = 200
        def json(self): return {"results": [{}]}
        text = "{}"

    class FakeClient:
        async def post(self, url, **kw):
            sent.append((url, kw.get("json")))
            return FakeResp()

    async with session_maker() as db:
        org = Org(name="t", slug="t-drain", ad_gclid="C",
                  ad_click_at=datetime.now(timezone.utc) - timedelta(days=1))
        db.add(org)
        await db.commit()
        await db.refresh(org)
        old = AdConversion(org_id=org.id, action=adsconv.ACTION_SIGNUP,
                           created_at=datetime.now(timezone.utc) - timedelta(hours=12))
        young = AdConversion(org_id=org.id, action=adsconv.ACTION_PAID,
                             created_at=datetime.now(timezone.utc))
        db.add(old); db.add(young)
        await db.commit()

        await adsconv.drain_once(db, FakeClient())

        await db.refresh(old); await db.refresh(young)
        assert old.uploaded_at is not None
        assert young.uploaded_at is None
        assert len(sent) == 1
```

- [ ] **Step 6: Implement `drain_once` and `worker`**

```python
async def drain_once(db: AsyncSession, client) -> dict:
    """Upload one batch of due rows. Returns a small dict for logging.

    Due = not yet uploaded, older than the click-availability delay, under the attempt ceiling.
    Every failure marks the row and leaves it for the next pass; nothing is dropped silently.
    """
    if not enabled():
        return {"sent": 0, "reason": "disabled"}
    # Naive UTC on BOTH sides: created_at is a naive column, and comparing it against a tz-aware
    # value is an asyncpg error on Postgres (and a silently wrong comparison elsewhere).
    cutoff = _utcnow_naive() - timedelta(seconds=_UPLOAD_DELAY_S)
    rows = (await db.execute(
        select(AdConversion)
        .where(AdConversion.uploaded_at.is_(None),
               AdConversion.created_at <= cutoff,
               AdConversion.attempts < _MAX_ATTEMPTS)
        .limit(100)
    )).scalars().all()
    if not rows:
        return {"sent": 0}
    orgs = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_({r.org_id for r in rows})))).scalars().all()}
    payload = build_payload(rows, orgs)
    if not payload["conversions"]:
        return {"sent": 0}
    cid = get_settings().google_ads_customer_id
    url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{cid}:uploadClickConversions"
    headers = await _auth_headers(db, client)
    resp = await client.post(url, json=payload, headers=headers)
    now = _utcnow_naive()
    for row in rows:
        row.attempts += 1
        if resp.status_code == 200:
            row.uploaded_at = now
            row.error = ""
        else:
            row.error = f"{resp.status_code}: {resp.text[:300]}"
        db.add(row)
    await db.commit()
    return {"sent": len(rows), "status": resp.status_code}


async def worker(session_factory, client) -> None:
    """Drain forever. Runs from `lifespan`; a failure here must never take the server down."""
    import logging
    while True:
        try:
            async with session_maker() as db:
                await drain_once(db, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad batch must not kill the loop
            logging.getLogger("treg").warning("ads conversion drain failed: %s", exc)
        await asyncio.sleep(300)
```

`_auth_headers` reads the platform org's `google-ads` OAuth `Secret`, calls
`oauth.ensure_fresh(secret, db, client)` and returns the bearer plus `developer-token` and
`login-customer-id`. Follow the pattern at `src/treg/health.py:205`, which already refreshes a
stored OAuth secret from a background task. The developer token comes from
`settings.google_ads_developer_token` — the same platform binding the proxy injects.

- [ ] **Step 7: Start it in `lifespan`**

In `src/treg/api.py`, inside `lifespan` after `app.state.http` is created:

```python
    ads_task = asyncio.create_task(adsconv.worker(async_session, app.state.http)) \
        if adsconv.enabled() else None
```

and in the `finally:` block, before `await app.state.http.aclose()`:

```python
        if ads_task is not None:
            ads_task.cancel()
```

Use whatever the session factory is actually named in `api.py`'s imports from `db.py`.

- [ ] **Step 8: Run the full suite**

Run: `uv run --frozen python -m pytest -q`
Expected: no new failures.

- [ ] **Step 9: Commit**

```bash
git commit -m "feat(ads): background uploader draining the conversion outbox to Ads v22" -- src/treg/adsconv.py src/treg/api.py tests/test_adsconv.py
```

---

### Task 9: Docs — the fragment, the stale skill, and the privacy line

CLAUDE.md requires docs updated **in the same commit as the code**; this is the catch-up commit for a feature built across several.

**Files:**
- Create: `docs/context/architecture/ads-conversions.md`
- Modify: `.agents/skills/google-ads/SKILL.md`
- Modify: `src/treg/web/privacy.html`

- [ ] **Step 1: Run the drift check**

Run: `bash .agents/skills/tools-registry-context/scripts/drift.sh`
Read its output and map every changed source to the fragment that names it. `src/treg/api.py`, `billing.py`, `models.py`, `db.py` and `config.py` all changed and are all named in existing fragments — those fragments need a line each about the new columns, the outbox and the worker.

- [ ] **Step 2: Write the new fragment**

Create `docs/context/architecture/ads-conversions.md` with frontmatter naming its sources, matching the shape of the other files in `docs/context/architecture/`:

```markdown
---
sources:
  - src/treg/adsconv.py
  - src/treg/web/adtrack.js
---
```

Body: the capture → store → fire → upload chain; why the outbox is durable while audit.py and
analytics.py are droppable; why first-call is not derived from `CallRecord`; the fixed FX constant
and its date; the three live conversion action ids.

- [ ] **Step 3: Fix the stale API version in the Google Ads skill**

`.agents/skills/google-ads/SKILL.md` §1 pins **v21** and instructs "do not guess downward". v21 now
returns `UNSUPPORTED_VERSION`. Change the pin to **v22** (verified 2026-08-17), update the paths in
its examples, and record that the failure mode changed: a dead version now returns a typed
`UNSUPPORTED_VERSION` error, not the HTML 404 the file describes.

- [ ] **Step 4: Add the privacy line**

`src/treg/web/privacy.html` needs a sentence covering advertising measurement: a first-party cookie
recording which ad click led to a signup, retained 90 days, not shared with third parties beyond the
conversion upload to Google Ads.

- [ ] **Step 5: Commit**

```bash
git commit -m "docs(ads): conversion-tracking fragment, v22 pin fix, privacy line" -- docs/context/architecture/ads-conversions.md .agents/skills/google-ads/SKILL.md src/treg/web/privacy.html
```

---

## Not in this plan

- **The five usecase landing pages.** Untracked and undeployed; the capture snippet is a shared
  include, so adding them later is one `<script>` line per page.
- **Campaign structure, keywords, budgets.** No code depends on them.
- **The paused PMax campaign `24150011650`** (A$44.81/day) — an account decision, not a code change.
- **The live-click verification** in the spec's Verification section. It needs a real ad click and
  cannot be automated; run it after this plan lands and before any real spend.
