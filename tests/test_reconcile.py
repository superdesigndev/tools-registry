"""Phase 5 — the reconciliation reports. Seeded rows, asserted arithmetic, and the auth gate.

These are pure reads over CallRecord (telemetry) and LedgerEntry (money), so the tests seed those
tables directly rather than driving the proxy: the point under test is the math and the grouping, and
the metered call path that writes the rows has its own suite (test_marketplace_call.py).

The auth assertion matters as much as the math: these endpoints expose CROSS-ORG spend and the
platform's margin, so an org admin — who may see their own bill — must be refused.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from conftest import make_upstream

from treg import reconcile
from treg.api import app
from treg.config import get_settings
from treg.db import reset_db, session_maker
from treg.models import CallRecord, LedgerEntry

ADMIN = "ENV-ADMIN-SECRET"


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


def _a() -> dict:
    return {"X-Treg-Token": ADMIN}


@pytest.fixture
async def c(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()
    get_settings.cache_clear()


async def _calls(rows: list[dict]) -> None:
    """Seed marketplace telemetry rows. Defaults are a platform-tier dataforseo call."""
    async with session_maker() as db:
        for i, r in enumerate(rows):
            db.add(CallRecord(
                org_id=r.get("org_id", 1), user_email="a@x.dev", tool_name=r.get("tool", "df"),
                method="POST", path="/v3/x", status_code=200, kind="call",
                endpoint_id=r.get("endpoint_id", "dataforseo.serp.google"),
                provider=r.get("provider", "dataforseo"),
                credential_tier=r.get("tier", "platform"),
                cost_estimated_micro=r.get("est"), cost_observed_micro=r.get("obs"),
                params_hash=r.get("hash", f"h{i}"),
            ))
        await db.commit()


async def _settles(rows: list[dict]) -> None:
    """Seed settle ledger entries. `amount` is the POSITIVE charge; the column is signed negative."""
    async with session_maker() as db:
        for i, r in enumerate(rows):
            db.add(LedgerEntry(
                id=f"e{i}", org_id=r.get("org_id", 1), kind=r.get("kind", "settle"),
                amount_micro=-int(r["amount"]), call_id=f"c{i}",
                endpoint_id=r.get("endpoint_id", "dataforseo.serp.google"),
                meta={"provider": r.get("provider", "dataforseo"),
                      **({"observed_micro": r["observed"]} if "observed" in r else {}),
                      **(r.get("meta") or {})},
            ))
        await db.commit()


def _since():
    return reconcile.window_start(1)


# ---- drift math --------------------------------------------------------------------------------
async def test_drift_flags_a_ten_percent_climb(c: AsyncClient):
    await _calls([{"est": 1000, "obs": 1100}] * 4)
    async with session_maker() as db:
        rows = await reconcile.price_drift(db, _since())
    assert len(rows) == 1
    r = rows[0]
    assert (r["endpoint_id"], r["provider"], r["calls"]) == ("dataforseo.serp.google", "dataforseo", 4)
    assert r["estimate_mean_micro"] == 1000 and r["observed_mean_micro"] == 1100
    assert r["drift_ratio"] == pytest.approx(0.1)
    assert r["drift_micro"] == 100
    assert r["flagged"] is True


async def test_drift_within_tolerance_is_not_flagged(c: AsyncClient):
    await _calls([{"est": 1000, "obs": 1020}] * 5)  # 2% — under the 5% tolerance
    async with session_maker() as db:
        rows = await reconcile.price_drift(db, _since())
    assert rows[0]["drift_ratio"] == pytest.approx(0.02)
    assert rows[0]["flagged"] is False


async def test_drift_needs_min_calls_before_it_flags(c: AsyncClient):
    await _calls([{"est": 1000, "obs": 2000}] * 2)  # 100% drift but only two samples
    async with session_maker() as db:
        few = await reconcile.price_drift(db, _since(), min_calls=3)
        enough = await reconcile.price_drift(db, _since(), min_calls=2)
    assert few[0]["drift_ratio"] == pytest.approx(1.0) and few[0]["flagged"] is False
    assert enough[0]["flagged"] is True  # same rows, lower bar → the drift surfaces


async def test_drift_handles_a_zero_estimate(c: AsyncClient):
    """"We thought it was free and it wasn't" has no ratio, and must still flag."""
    await _calls([{"est": 0, "obs": 500}] * 3)
    async with session_maker() as db:
        rows = await reconcile.price_drift(db, _since())
    assert rows[0]["drift_ratio"] is None
    assert rows[0]["flagged"] is True
    assert rows[0]["observed_mean_micro"] == 500


async def test_drift_ignores_rows_missing_either_number(c: AsyncClient):
    """A provider that never reports its charge has no second opinion — it must not appear at all."""
    await _calls([
        {"provider": "tikhub", "endpoint_id": "tikhub.a", "est": 900, "obs": None},
        {"provider": "tikhub", "endpoint_id": "tikhub.a", "est": None, "obs": None},
        {"provider": "scrapecreators", "endpoint_id": "sc.b", "est": 1000, "obs": 1500},
    ])
    async with session_maker() as db:
        rows = await reconcile.price_drift(db, _since(), min_calls=1)
    assert [r["endpoint_id"] for r in rows] == ["sc.b"]
    assert rows[0]["provider"] == "scrapecreators" and rows[0]["flagged"] is True


async def test_drift_separates_endpoints_and_sorts_worst_first(c: AsyncClient):
    await _calls([{"endpoint_id": "ep.mild", "est": 1000, "obs": 1060}] * 3
                 + [{"endpoint_id": "ep.bad", "est": 1000, "obs": 3000}] * 3)
    async with session_maker() as db:
        rows = await reconcile.price_drift(db, _since())
    assert [r["endpoint_id"] for r in rows] == ["ep.bad", "ep.mild"]
    assert all(r["flagged"] for r in rows)


# ---- provider spend ----------------------------------------------------------------------------
async def test_spend_groups_by_provider_and_counts_reported_costs(c: AsyncClient):
    await _settles([
        {"provider": "dataforseo", "amount": 1000, "observed": 1000},
        {"provider": "dataforseo", "amount": 500},  # settled at the estimate — provider stayed quiet
        {"provider": "tikhub", "amount": 300, "org_id": 2},
    ])
    async with session_maker() as db:
        out = await reconcile.provider_spend(db, _since())
    assert out["calls"] == 3 and out["charged_micro"] == 1800
    df = next(p for p in out["providers"] if p["provider"] == "dataforseo")
    assert (df["calls"], df["charged_micro"], df["reported_calls"]) == (2, 1500, 1)
    assert df["provider_reported_micro"] == 1000
    assert out["providers"][0]["provider"] == "dataforseo"  # biggest spend first
    th = next(p for p in out["providers"] if p["provider"] == "tikhub")
    assert th["orgs"] == 1 and th["charged_usd"] == 0.0003


async def test_spend_ignores_non_settle_entries_and_the_window(c: AsyncClient):
    await _settles([{"kind": "reserve", "amount": 9999}, {"kind": "release", "amount": 7777},
                    {"amount": 1000}])
    async with session_maker() as db:
        out = await reconcile.provider_spend(db, _since())
    assert out["calls"] == 1 and out["charged_micro"] == 1000


async def test_spend_backs_the_margin_out_of_the_charge(c: AsyncClient, monkeypatch):
    """`provider_cost_est_micro` is the invoice comparison, so it must be the charge WITHOUT margin."""
    monkeypatch.setenv("TREG_PLATFORM_MARGIN", "0.25")
    get_settings.cache_clear()
    await _settles([{"amount": 1250}])
    async with session_maker() as db:
        out = await reconcile.provider_spend(db, _since())
    assert out["margin"] == 0.25
    assert out["providers"][0]["provider_cost_est_micro"] == 1000
    get_settings.cache_clear()


# ---- repeat rate -------------------------------------------------------------------------------
async def test_repeat_rate_counts_distinct_params(c: AsyncClient):
    await _calls([
        {"hash": "aaa", "est": 100, "obs": 100},
        {"hash": "aaa", "est": 100, "obs": 100},
        {"hash": "aaa", "est": 100, "obs": 100},
        {"hash": "bbb", "est": 100, "obs": 100},
        {"provider": "tikhub", "endpoint_id": "tikhub.a", "hash": "ccc", "est": 50},
    ])
    async with session_maker() as db:
        out = await reconcile.repeat_rate(db, _since())
    df = next(p for p in out["providers"] if p["provider"] == "dataforseo")
    assert (df["calls"], df["distinct_params"], df["repeat_calls"]) == (4, 2, 2)
    assert df["repeat_ratio"] == 0.5
    assert df["cost_micro"] == 400  # observed preferred over the estimate
    th = next(p for p in out["providers"] if p["provider"] == "tikhub")
    assert th["repeat_calls"] == 0 and th["cost_micro"] == 50  # fell back to the estimate
    assert out["calls"] == 5 and out["repeat_calls"] == 2


async def test_repeat_rate_top_repeated_only_lists_actual_repeats(c: AsyncClient):
    await _calls([{"hash": "aaa", "est": 300, "obs": 300}] * 3 + [{"hash": "bbb", "est": 100, "obs": 100}])
    async with session_maker() as db:
        out = await reconcile.repeat_rate(db, _since())
    assert [t["params_hash"] for t in out["top_repeated"]] == ["aaa"]  # "bbb" was called once
    hot = out["top_repeated"][0]
    assert (hot["calls"], hot["cost_micro"]) == (3, 900)
    assert hot["repeat_cost_micro"] == 600  # the two calls a cache would have served
    assert hot["endpoint_id"] == "dataforseo.serp.google"


async def test_repeat_rate_is_empty_without_telemetry(c: AsyncClient):
    async with session_maker() as db:
        out = await reconcile.repeat_rate(db, _since())
    assert out == {"calls": 0, "repeat_calls": 0, "repeat_ratio": 0.0, "providers": [], "top_repeated": []}


# ---- the endpoints + their gate ----------------------------------------------------------------
@pytest.mark.parametrize("report", ["drift", "spend", "repeats"])
async def test_reconcile_endpoints_require_superadmin(c: AsyncClient, report: str):
    u = (await c.post("/users", json={"email": "orgadmin@x.dev"})).json()  # owner of their own org
    assert (await c.get(f"/admin/reconcile/{report}", headers=_h(u["token"]))).status_code == 403
    assert (await c.get(f"/admin/reconcile/{report}")).status_code == 401
    r = await c.get(f"/admin/reconcile/{report}", headers=_a())
    assert r.status_code == 200, r.text
    assert r.json()["since_days"] == 30


async def test_drift_endpoint_splits_out_the_flagged_rows(c: AsyncClient):
    await _calls([{"endpoint_id": "ep.bad", "est": 1000, "obs": 2000}] * 3
                 + [{"endpoint_id": "ep.ok", "est": 1000, "obs": 1000}] * 3)
    r = await c.get("/admin/reconcile/drift?since_days=7", headers=_a())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["since_days"] == 7 and body["tolerance"] == reconcile.DRIFT_TOLERANCE
    assert [x["endpoint_id"] for x in body["flagged"]] == ["ep.bad"]
    assert {x["endpoint_id"] for x in body["endpoints"]} == {"ep.bad", "ep.ok"}


async def test_spend_and_repeats_endpoints_report_their_windows(c: AsyncClient):
    await _settles([{"amount": 2000, "observed": 2000}])
    await _calls([{"hash": "aaa", "est": 100, "obs": 100}] * 2)
    spend = (await c.get("/admin/reconcile/spend", headers=_a())).json()
    assert spend["charged_micro"] == 2000 and spend["providers"][0]["reported_calls"] == 1
    repeats = (await c.get("/admin/reconcile/repeats?top=1", headers=_a())).json()
    assert repeats["repeat_calls"] == 1 and len(repeats["top_repeated"]) == 1


async def test_window_start_is_clamped(c: AsyncClient):
    """A hostile `since_days` can't turn the report into a full-table scan of all time (or the future)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert round((now - reconcile.window_start(0)).total_seconds() / 86400) == 1
    assert round((now - reconcile.window_start(99999)).total_seconds() / 86400) == 365


# ---- shared-plan recovery (the monthly review's input) -----------------------------------------
async def test_recovery_over_recovered_says_lower(c: AsyncClient):
    """Collected far above the (window-scaled) fee: the report must say so and suggest the rate at
    which the MEASURED volume breaks even (fee ÷ calls) — the review rule from
    docs/SHARED-PLAN-PRICING-PLAN.md applied mechanically.

    The fixture window is ONE day, so the monthly fee scales to 1/30 before comparison. The first
    version of this test assumed 30 days and failed against a correct report; the expectation now
    mirrors the scaling instead of hardcoding a month."""
    await _settles([{"provider": "alphavantage", "amount": 30_000_000,
                     "endpoint_id": "alphavantage.quote"} for _ in range(3)])
    async with session_maker() as db:
        out = await reconcile.shared_plan_recovery(db, _since())
    row = next(p for p in out["providers"] if p["provider"] == "alphavantage")
    fee_scaled = int(round(49.99 * 1_000_000 * row["window_days"] / 30))
    assert row["calls"] == 3 and row["collected_micro"] == 90_000_000
    assert row["fee_micro"] == fee_scaled
    assert row["recovery"] == round(90_000_000 / fee_scaled, 4)
    assert row["suggested_usd"] == round(fee_scaled / 3 / 1_000_000, 9)
    assert row["action"].startswith("over-recovered")


async def test_recovery_quiet_month_says_raise_or_demote(c: AsyncClient):
    await _settles([{"provider": "alphavantage", "amount": 500_000,
                     "endpoint_id": "alphavantage.quote"}])   # $0.50 against ~$1.67 of scaled fee
    async with session_maker() as db:
        out = await reconcile.shared_plan_recovery(db, _since())
    row = next(p for p in out["providers"] if p["provider"] == "alphavantage")
    assert row["recovery"] < 0.5
    assert "raise toward" in row["action"] and "demote" in row["action"]


async def test_recovery_zero_calls_has_no_suggestion(c: AsyncClient):
    """fee ÷ 0 is not a price. The suggestion must be absent, and the action must raise the demotion
    question rather than print infinity."""
    async with session_maker() as db:
        out = await reconcile.shared_plan_recovery(db, _since())
    row = next(p for p in out["providers"] if p["provider"] == "alphavantage")
    assert row["calls"] == 0 and row["suggested_usd"] is None
    assert "demot" in row["action"]


async def test_recovery_ignores_vendor_priced_providers(c: AsyncClient):
    """dataforseo's rate is the VENDOR's; fee-vs-collected is meaningless for it and its presence
    here would invite 'adjusting' a price that is not ours to adjust."""
    await _settles([{"provider": "dataforseo", "amount": 99_000_000}])
    async with session_maker() as db:
        out = await reconcile.shared_plan_recovery(db, _since())
    assert all(p["provider"] != "dataforseo" for p in out["providers"])


async def test_price_drift_never_sees_a_shared_plan_provider(c: AsyncClient):
    """Pinning a NATURAL property before someone breaks it: drift compares our estimate against the
    provider's own reported charge, and a flat-fee provider never reports one — there is no number
    to drift from. If a future `_observed_cost_micro` parser is added for such a provider, this test
    is the alarm that the drift report now polices a price treg itself set."""
    await _calls([{"endpoint_id": "alphavantage.quote", "provider": "alphavantage", "est": 1000}] * 3)
    async with session_maker() as db:
        rows = await reconcile.price_drift(db, _since(), min_calls=1)
    assert all(r["provider"] != "alphavantage" for r in rows)
