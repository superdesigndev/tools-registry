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

    Note: // floors toward negative infinity, so negative amounts round away from zero
    while positive amounts round toward zero. Real inputs are always positive
    (top-ups); the negative case is defensive only.
    """
    return usd_micro * AUD_PER_USD_NUM // AUD_PER_USD_DEN


import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import get_settings
from .models import AdConversion, Org


def enabled() -> bool:
    """Missing upload configuration = OFF. Keeps tests and self-hosted instances inert.

    `google_ads_developer_token` is deliberately NOT required here: it is the read-side Ads API's
    header (see oauth_providers.GOOGLE_ADS), and Data Manager has no developer-token header at all
    (see `_auth_headers`). `ads_conv_org_slug` is gone entirely — the uploader no longer borrows a
    customer-facing OAuth connection; it authenticates with its own platform refresh token.
    """
    s = get_settings()
    return bool(s.google_ads_customer_id and s.ads_conv_refresh_token)


async def queue(db: AsyncSession, org: Org, action: str, *,
                value_usd_micro: int = 0, dedupe_key: str = "") -> bool:
    """Record that `org` owes Google a conversion. Returns True if a row was written.

    Call this INSIDE the caller's transaction: the event and its pending conversion must commit
    together, or a crash between them loses a conversion with no trace. The `paid` caller
    (`billing._credit`) cannot honour this — `ledger.topup()` has already committed by the time
    `_credit` gets here, so that one path is a known, accepted two-commit gap rather than a bug (see
    `docs/context/architecture/ads-conversions.md`).

    A no-op when the team has no click to attribute to, which is most teams. Duplicate fires are
    absorbed by the unique constraint rather than checked for first — the check-then-insert race
    is real under concurrent webhook redelivery.
    """
    if not enabled() or not org.ad_gclid:
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


# ---- the uploader ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    """Naive UTC. Our datetime columns are TIMESTAMP WITHOUT TIME ZONE and asyncpg rejects a
    tz-aware value into one; see `_now` in models.py, which every other table already follows.
    `api.py` has its own copy of this for the same reason — it is private to that module."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Data Manager is its own product with its own `v1`, unrelated to the Google Ads REST version
# sunset cycle (oauth_providers.GOOGLE_ADS, catalog yaml, auth-secrets.md still track that
# separately for the read-side Ads catalog calls) — there is nothing to keep in sync here.
DATA_MANAGER_URL = "https://datamanager.googleapis.com/v1/events:ingest"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_MAX_ATTEMPTS = 8
_RETRY_BASE_S = 5 * 60
_RETRY_CAP_S = 24 * 3600
_CLICK_ID_FIELDS = frozenset({"gclid", "gbraid", "wbraid"})
# Data Manager has no analogue for CLICK_CONVERSION_ALREADY_EXISTS: within one conversion action it
# dedupes silently on `events[].transactionId` (see `_payload_and_rows`) rather than erroring, so a
# retry after an ambiguous prior outcome just gets folded into the existing conversion server-side
# and comes back as an ordinary 200. The set stays here, empty, so the by-index classification below
# keeps the same three-way shape (acknowledged / retryable / dead-letter) and stays extensible if
# Google ever does surface a duplicate-style rejection.
_ACKNOWLEDGED_ROW_ERRORS: frozenset[str] = frozenset()
_RETRYABLE_ROW_ERRORS = frozenset({"INTERNAL_ERROR"})
# Matches the event index out of a `google.rpc.BadRequest.FieldViolation.field` path such as
# "events[2].userData..." or "events.events[2]...", and equally out of a `FieldWarning.fieldPath`.
_EVENT_INDEX_RE = re.compile(r"events\[(\d+)\]")


def _conversion_time(dt: datetime) -> str:
    """RFC 3339, e.g. '2026-08-18T10:00:00Z'. Data Manager rejects the old 'yyyy-mm-dd hh:mm:ss+hh:mm'.

    `dt` comes out of the database as NAIVE UTC (see models._now), so it is stamped with UTC, not
    converted. `.astimezone()` on a naive value would read it as LOCAL time — on the Sydney deploy
    target that shifts every conversion by 10-11 hours, which Google would either reject as
    pre-dating the click or attribute to the wrong day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload_and_rows(
    rows: list[AdConversion], orgs: dict[int, Org]
) -> tuple[dict, list[AdConversion]]:
    """Build the events:ingest request and preserve its operation-index -> outbox-row mapping.

    Data Manager takes every destination (conversion action) and every event in ONE request, so a
    batch spanning signup/first_call/paid rows is sent as a single call: `destinations[]` lists each
    action touched by this batch exactly once (deduped by `reference`), and each event names the one
    destination it belongs to via `destinationReferences`. `events[index]` stays a straight 1:1
    mapping onto `payload_rows[index]` — `destinations` is a separate, deduped list, so it never
    disturbs that index. `drain_once` and `_partial_failure_errors` rely on that 1:1 mapping to
    attribute a rejected request back to the row that caused it.
    """
    cid = get_settings().google_ads_customer_id
    login_cid = (get_settings().google_ads_login_customer_id or "").replace("-", "").strip()
    destinations: list[dict] = []
    dest_index_by_action: dict[str, int] = {}
    events = []
    payload_rows = []
    for row in rows:
        org = orgs.get(row.org_id)
        if org is None or not org.ad_gclid:
            continue
        click_field = org.ad_click_id_type or "gclid"  # NULL means a pre-type-migration GCLID.
        if click_field not in _CLICK_ID_FIELDS:
            click_field = "gclid"  # defensive for a manually edited legacy row
        if row.action not in dest_index_by_action:
            destination = {
                "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": cid},
                "productDestinationId": CONVERSION_ACTION_IDS[row.action],
                "reference": row.action,
            }
            # `loginAccount` is per-destination, not a header — never infer it from
            # Secret.resource_ref (that's the target CLIENT account discovery stored, not the
            # manager Google requires here). Direct client-account auth leaves it unset.
            if login_cid:
                destination["loginAccount"] = {"accountType": "GOOGLE_ADS", "accountId": login_cid}
            dest_index_by_action[row.action] = len(destinations)
            destinations.append(destination)
        event = {
            "adIdentifiers": {click_field: org.ad_gclid},
            "eventTimestamp": _conversion_time(row.created_at),
            "eventSource": "WEB",
            "destinationReferences": [row.action],
            # Stable per outbox row, so a resend after an ambiguous prior outcome dedupes into the
            # SAME conversion on Google's side instead of double-counting. The `treg-` prefix is
            # REQUIRED, not cosmetic: Data Manager rejects a purely numeric transactionId with a
            # bare 400 INVALID_ARGUMENT on `events[N]` (verified live 2026-08-18 — "2"/"3" fail,
            # "row-2"/"row-3" succeed with every other field identical). validateOnly does NOT
            # catch it, so only a real ingest surfaces this.
            "transactionId": f"treg-{row.id}",
        }
        if row.value_usd_micro:
            # The one permitted float: conversionValue is a wire double, so a decimal amount is what
            # the JSON boundary requires. The arithmetic that produced the micro amount stayed
            # integral (usd_micro_to_aud_micro).
            event["conversionValue"] = usd_micro_to_aud_micro(row.value_usd_micro) / 1_000_000
            event["currency"] = "AUD"
        events.append(event)
        payload_rows.append(row)
    return {"destinations": destinations, "events": events, "validateOnly": False}, payload_rows


def build_payload(rows: list[AdConversion], orgs: dict[int, Org]) -> dict:
    """Turn outbox rows into an events:ingest body.

    `drain_once` retains the row mapping from `_payload_and_rows` and reads the response before
    acknowledging anything. Value is converted to the ACCOUNT's currency here, at upload time; the
    outbox stores USD so a rate change never rewrites history.
    """
    return _payload_and_rows(rows, orgs)[0]


# Module-level cache for the platform access token: ONE credential, shared by every drain, so a
# per-call or per-db-session cache would just re-exchange the refresh token every pass for no
# reason. `worker` drains every 300s; a token typically lives ~3599s, so almost every pass should
# hit this cache rather than call Google. `_token_expires_at` is a `time.time()` epoch, not a naive
# UTC datetime (unlike the rest of this module) — it is process-local runtime state, never
# persisted or compared against a DB column, so there is no naive/aware mismatch to guard against.
_cached_access_token: str | None = None
_token_expires_at: float = 0.0
_TOKEN_SKEW_S = 60.0  # refresh this many seconds before actual expiry, same margin as oauth.py


async def _exchange_refresh_token(client) -> tuple[str, float]:
    """One refresh_token grant against Google's token endpoint. Raises on any failure — the caller
    (`_auth_headers`) decides whether that's fatal to this drain; `drain_once` is called inside
    `worker`'s try/except, so a raise here just means "retry next pass," not a crashed loop."""
    settings = get_settings()
    resp = await client.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": settings.ads_conv_refresh_token,
            "client_id": settings.google_ads_client_id,
            "client_secret": settings.google_ads_client_secret,
        },
    )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — an unparseable body is not a usable token either
        body = {}
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError(
            f"ads conversion token refresh failed: {resp.status_code} {resp.text[:300]}"
        )
    try:
        expires_in = float(body.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0.0
    return token, time.time() + expires_in


async def _auth_headers(client) -> dict[str, str]:
    """Bearer-only headers for a direct call to the Data Manager REST API.

    Data Manager has neither a developer-token header nor a login-customer-id header: the manager
    account is expressed per-destination as `loginAccount` in the request BODY instead (see
    `_payload_and_rows`). Do not resurrect either header here.

    This does NOT go through proxy.relay/injectors.py — the uploader is not a caller-issued
    `/call/` request, it's treg spending its OWN platform connection, so there is no Tool/bindings
    row (and no per-tenant Secret) to resolve credentials from. It exchanges treg's OWN long-lived
    `settings.ads_conv_refresh_token` for an access token directly against Google's token endpoint,
    redeemed with the same `google_ads_client_id`/`_secret` the refresh token was issued against.
    Two different purposes stay on two different credentials: a customer connecting Google Ads
    (oauth_providers.GOOGLE_ADS, `adwords` scope only) never touches this path, and this path never
    touches a customer's OAuth connection.

    The access token is cached in module state with its expiry (`_cached_access_token`,
    `_token_expires_at`) and only re-exchanged within `_TOKEN_SKEW_S` of expiring — `worker` drains
    every few minutes, so re-exchanging on every pass would be wasteful and risks Google's refresh
    rate limit.
    """
    global _cached_access_token, _token_expires_at
    if _cached_access_token is None or time.time() > _token_expires_at - _TOKEN_SKEW_S:
        _cached_access_token, _token_expires_at = await _exchange_refresh_token(client)
    return {"Authorization": f"Bearer {_cached_access_token}", "Content-Type": "application/json"}


def _retry_delay(attempts: int) -> timedelta:
    """Exponential retry delay, bounded so a repaired integration eventually drains."""
    exponent = min(max(attempts - 1, 0), 9)  # 2^9 already exceeds the 24-hour cap.
    seconds = min(_RETRY_BASE_S * (2 ** exponent), _RETRY_CAP_S)
    return timedelta(seconds=seconds)


def _partial_failure_errors(body: dict) -> tuple[dict[int, list[tuple[str, str]]], list[tuple[str, str]]]:
    """Parse Data Manager's error envelope for a REJECTED request (non-200 status).

    Unlike ConversionUploadService, Data Manager has no partial-success envelope on a 200: as
    `drain_once` handles separately, a 200 means the WHOLE request was accepted and every event in
    it is acknowledged. A non-200 means the WHOLE request was rejected before anything was
    ingested — but the standard `google.rpc.Status` error body Google returns can still say WHICH
    row caused it, via `error.details[].fieldViolations[].field` paths like "events[2]...". A row
    named that way is a genuine candidate for dead-lettering once it hits the attempt ceiling
    (`drain_once`); every OTHER row in the same rejected batch was caught in the crossfire — its
    own data was never at fault — and must keep retrying regardless of its own attempt count.
    """
    by_index: dict[int, list[tuple[str, str]]] = {}
    general: list[tuple[str, str]] = []
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return by_index, general
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("fieldViolations") or []:
            if not isinstance(violation, dict):
                continue
            code = str(violation.get("reason") or "UNKNOWN")
            message = str(violation.get("description") or error.get("message") or "request rejected")
            match = _EVENT_INDEX_RE.search(str(violation.get("field") or ""))
            target = by_index.setdefault(int(match.group(1)), []) if match else general
            target.append((code, message))
    if not by_index and not general and error:
        general.append(("UNKNOWN", str(error.get("message") or "request rejected")))
    return by_index, general


def _field_warnings_by_index(body: dict) -> dict[int, list[tuple[str, str]]]:
    """Non-fatal per-event notes on an ACCEPTED (200) response.

    Per Data Manager's own contract, a warning means "the API didn't reject the record, but had to
    ignore part of its data" — the event is still ingested. So a row with a warning is still
    `_acknowledge`d in `drain_once`; the text is kept on `row.error` purely for operator visibility,
    never as a reason to retry or dead-letter.
    """
    by_index: dict[int, list[tuple[str, str]]] = {}
    for warning in body.get("fieldWarnings") or []:
        if not isinstance(warning, dict):
            continue
        match = _EVENT_INDEX_RE.search(str(warning.get("fieldPath") or ""))
        if not match:
            continue
        code = str(warning.get("reason") or "UNKNOWN")
        message = str(warning.get("description") or "field warning")
        by_index.setdefault(int(match.group(1)), []).append((code, message))
    return by_index


def _schedule_retry(row: AdConversion, now: datetime, error: str) -> None:
    row.error = error[:300]
    row.next_attempt_at = now + _retry_delay(row.attempts)
    row.failed_at = None


def _acknowledge(row: AdConversion, now: datetime) -> None:
    row.uploaded_at = now
    row.next_attempt_at = None
    row.failed_at = None
    row.error = ""


async def drain_once(db: AsyncSession, client) -> dict:
    """Upload one batch of due rows. Returns a small dict for logging.

    Due = not uploaded/terminal, older than the click-availability delay, and past its retry time.
    HTTP failures retry indefinitely with backoff. Per-row permanent failures are dead-lettered
    after `_MAX_ATTEMPTS`; they remain queryable with `failed_at` + the last Google error.

    Data Manager's ingest response has no per-event partial-success shape the way the old
    ConversionUploadService did: a 200 means the WHOLE request was accepted (every event in it),
    and a non-200 means the WHOLE request was rejected (nothing in it was ingested) — see
    `_partial_failure_errors` for how a rejection can still be traced back to one row.
    """
    if not enabled():
        return {"sent": 0, "reason": "disabled"}
    # Naive UTC on BOTH sides: created_at is a naive column, and comparing it against a tz-aware
    # value is an asyncpg error on Postgres (and a silently wrong comparison elsewhere).
    now = _utcnow_naive()
    rows = (await db.execute(
        select(AdConversion)
        .where(AdConversion.uploaded_at.is_(None),
               AdConversion.failed_at.is_(None),
               or_(AdConversion.next_attempt_at.is_(None), AdConversion.next_attempt_at <= now))
        .order_by(AdConversion.created_at, AdConversion.id)
        .limit(100)
    )).scalars().all()
    if not rows:
        return {"sent": 0}
    orgs = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_({r.org_id for r in rows})))).scalars().all()}
    payload, payload_rows = _payload_and_rows(rows, orgs)
    payload_ids = {row.id for row in payload_rows}
    skipped = [row for row in rows if row.id not in payload_ids]
    for row in skipped:
        row.attempts += 1
        row.failed_at = now
        row.error = "outbox row has no attributable org/click id"
        db.add(row)
    if not payload["events"]:
        await db.commit()
        return {"sent": 0, "failed": len(skipped)}
    headers = await _auth_headers(client)
    resp = await client.post(DATA_MANAGER_URL, json=payload, headers=headers)

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — an unparseable body must not acknowledge durable rows
        body = {}
    if not isinstance(body, dict):
        body = {}

    sent = retried = failed = 0
    if resp.status_code == 200:
        request_id = body.get("requestId")
        if not request_id:
            # An unexpected/empty 200 body is not proof of acceptance — retry the whole batch
            # rather than guess which rows, if any, actually landed.
            for row in payload_rows:
                row.attempts += 1
                _schedule_retry(row, now, "200: missing requestId")
                db.add(row)
                retried += 1
        else:
            warnings_by_index = _field_warnings_by_index(body)
            for index, row in enumerate(payload_rows):
                row.attempts += 1
                _acknowledge(row, now)
                warning = warnings_by_index.get(index)
                if warning:
                    row.error = ("; ".join(f"{code}: {message}" for code, message in warning))[:300]
                sent += 1
                db.add(row)
    else:
        indexed_errors, general_errors = _partial_failure_errors(body)
        for index, row in enumerate(payload_rows):
            row.attempts += 1
            row_errors = indexed_errors.get(index)
            errors = row_errors or general_errors
            codes = {code for code, _ in errors}
            detail = "; ".join(f"{code}: {message}" for code, message in errors) \
                or f"{resp.status_code}: {resp.text[:260]}"
            if row_errors is None or codes & _RETRYABLE_ROW_ERRORS or row.attempts < _MAX_ATTEMPTS:
                # A row this rejection didn't name (row_errors is None) was caught in the
                # crossfire of a sibling's bad data — never dead-letter it on that basis alone.
                _schedule_retry(row, now, detail)
                retried += 1
            else:
                row.error = detail[:300]
                row.next_attempt_at = None
                row.failed_at = now
                failed += 1
            db.add(row)
    await db.commit()
    return {"sent": sent, "retried": retried, "failed": failed + len(skipped),
            "status": resp.status_code}


async def worker(session_factory, client) -> None:
    """Drain forever. Runs from `lifespan`; a failure here must never take the server down."""
    log = logging.getLogger("treg")
    while True:
        try:
            async with session_factory() as db:
                result = await drain_once(db, client)
                if result.get("retried") or result.get("failed") or result.get("status", 200) >= 400:
                    log.warning("ads conversion drain incomplete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad batch must not kill the loop
            log.warning("ads conversion drain failed: %s", exc)
        await asyncio.sleep(300)
