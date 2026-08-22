"""The ONE code path that moves money. Nothing else may write creditblock/ledgerentry/hold or
`org.balance_micro` — every mutation goes through the five operations below.

Units: **integer micro-USD** (1e-6 USD) everywhere. Never floats, never cents. A catalog call costs
~600 micro ($0.0006), so cents cannot represent one call and floats cannot be summed for a year
without drifting. The only float in sight is the margin RATE, and it is turned into an integer
immediately (see `with_margin`).

Shape:
  grant   → new promotional block, balance up            (org creation)
  topup   → new purchased block, balance up              (phase 4: after Stripe has authorized)
  reserve → balance down by the estimate, Hold opened    (the hot-path spend gate)
  settle  → blocks down by the observed cost, Hold closed, balance refunded the difference
  release → Hold closed, balance refunded in full        (upstream failure — not billable)

Invariant: `org.balance_micro == sum(block.remaining_micro) - sum(open hold.amount_micro)`.
It is a materialized column, not a query, because `reserve` has to be a single conditional UPDATE
(see below) — and every operation writes its LedgerEntry in the SAME transaction as the movement,
synchronously, in-request. Never route a ledger write through `audit.py`: it drops rows past its
queue bound and swallows exceptions, which is right for analytics and fatal for money.

**Margin is applied INSIDE this module** (`with_margin`), at reserve AND at settle — callers pass raw
provider cost in micro-USD and never think about markup. Doing it here is what makes the rate
auditable: every reserve/settle entry records the `margin` that was in force, so a rate change can't
retroactively rewrite what a call cost. (The alternative — margin at the call site — would let two
call sites disagree.)

Concurrency: `reserve` is one statement —
    UPDATE org SET balance_micro = balance_micro - :est WHERE id = :org AND balance_micro >= :est
so the database, not our process, decides who gets the last cent. rowcount 0 means insufficient
funds. This is why there is no SELECT-then-UPDATE and no application lock, and it behaves the same
on SQLite and Postgres. N concurrent callers against a balance that affords K get exactly K
successes.

Hold reaper: LAZY, at the top of `reserve`, scoped to the org that is calling. A crash between
relay and settle would otherwise strand that money forever, and the alternative (a background timer)
would need a scheduler, a leader election on multi-instance deploys, and would still only run on a
timer. Sweeping one org's stale holds is a single indexed DELETE-ish pass on the rare occasion any
exist, paid by the caller who benefits from it — and any org that never calls again has no balance
to strand in the first place.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .config import get_settings
from .models import CreditBlock, Hold, LedgerEntry, Org, TagSpend

# Block consumption order: promotional credit burns first, then the oldest purchased block. Promo is
# a marketing expense and never refundable, so spending it first keeps the refundable (and disputable)
# purchased pool as small as possible for as long as possible.
#
# `referral` sits alongside `promotional` at 0 for exactly that reason — a referral bonus is marketing
# spend we can never be asked to return, so it must burn before the money someone actually paid us.
# It is a separate kind ONLY so reporting can tell the two apart. Note that an unrecognised kind
# sorts LAST (`.get(kind, 99)` in `_consume_blocks`), i.e. after purchased: any new non-refundable
# kind must be added here or it silently gets the most expensive treatment available.
_KIND_ORDER = {"promotional": 0, "referral": 0, "purchased": 1}


class InsufficientBalance(Exception):
    """`reserve` lost the race for the last cent (or never had it). Carries the numbers a 402 needs."""

    def __init__(self, org_id: int, balance_micro: int, required_micro: int) -> None:
        self.org_id = org_id
        self.balance_micro = balance_micro
        self.required_micro = required_micro
        super().__init__(
            f"org {org_id}: balance {balance_micro} micro-USD is short of the {required_micro} "
            f"micro-USD this call reserves"
        )


def _now() -> datetime:
    # Naive UTC — same convention as models._now (TIMESTAMP WITHOUT TIME ZONE; asyncpg rejects
    # tz-aware values into those columns).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _id() -> str:
    return uuid.uuid4().hex


def with_margin(cost_micro: int) -> int:
    """Provider cost → what the org is charged, in micro-USD. Rounds UP (int + 1 on any remainder):
    never charge less than the call cost us, and a sub-micro remainder is not representable."""
    margin = get_settings().platform_margin
    if margin <= 0:
        return int(cost_micro)
    scaled = int(cost_micro) * (1.0 + margin)
    whole = int(scaled)
    return whole + 1 if scaled > whole else whole


def usd(amount_micro: int) -> float:
    """micro-USD → USD, for DISPLAY only. Never feed this back into a balance computation."""
    return round(amount_micro / 1_000_000, 6)


async def _entry(
    db: AsyncSession, *, org_id: int, kind: str, amount_micro: int,
    block_id: str | None = None, call_id: str | None = None,
    endpoint_id: str | None = None, meta: dict | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    # `created_at` is passed in when another row must carry the SAME instant (settle stamps its tag
    # rows with it). Two `_now()` calls a microsecond apart can straddle a reporting boundary, and
    # then one query counts the call and the other doesn't.
    row = LedgerEntry(
        id=_id(), org_id=org_id, kind=kind, amount_micro=amount_micro, block_id=block_id,
        call_id=call_id, endpoint_id=endpoint_id, meta=meta or {},
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(row)
    return row


async def _add_balance(db: AsyncSession, org_id: int, delta_micro: int) -> None:
    """Unconditional balance move (grants, refunds). The CONDITIONAL one lives in `reserve`."""
    await db.execute(
        update(Org).where(Org.id == org_id).values(balance_micro=Org.balance_micro + delta_micro)
    )


# ---- funding -----------------------------------------------------------------------------------
async def grant(
    db: AsyncSession, org_id: int, *, amount_micro: int | None = None,
    kind: str = "promotional", meta: dict | None = None, once: bool = True,
) -> CreditBlock | None:
    """Credit an org with a non-purchased block (the signup promo). Commits.

    `once=True` (the default) makes it idempotent per (org, kind): an org that already holds a block
    of this kind gets nothing and `None` comes back. That is what lets the org-creation hook be safe
    to call from more than one door, and what keeps a retry from double-granting.
    """
    amount = get_settings().promo_grant_micro if amount_micro is None else int(amount_micro)
    if amount <= 0:
        return None
    if once:
        existing = (await db.execute(
            select(CreditBlock.id).where(CreditBlock.org_id == org_id, CreditBlock.kind == kind)
        )).first()
        if existing is not None:
            return None
    block = CreditBlock(id=_id(), org_id=org_id, kind=kind, amount_micro=amount, remaining_micro=amount)
    db.add(block)
    await _add_balance(db, org_id, amount)
    await _entry(db, org_id=org_id, kind="grant", amount_micro=amount, block_id=block.id,
                 meta={**(meta or {}), "block_kind": kind})
    await db.commit()
    return block


async def topup(
    db: AsyncSession, org_id: int, amount_micro: int, payment_ref: str, *, meta: dict | None = None,
) -> CreditBlock:
    """Credit PURCHASED balance for an already-authorized payment. Commits.

    `payment_ref` (a Stripe PaymentIntent id in phase 4) is the idempotency key: webhooks redeliver,
    so a second call with the same ref returns the existing block and moves no money. This module
    knows nothing about Stripe — the caller is responsible for having verified the payment first.
    """
    if amount_micro <= 0:
        raise ValueError("topup amount must be positive")
    if not payment_ref:
        raise ValueError("topup requires a payment reference (the idempotency key)")
    existing = await _block_for_payment(db, payment_ref)
    if existing is not None:
        return existing  # already credited — a redelivered webhook, not a second purchase
    block = CreditBlock(id=_id(), org_id=org_id, kind="purchased", amount_micro=int(amount_micro),
                        remaining_micro=int(amount_micro), stripe_payment_intent=payment_ref)
    db.add(block)
    try:
        # FLUSH before anything else moves. The check above is a SELECT and this is the INSERT, so a
        # concurrent delivery of the same PaymentIntent can land between them; the UNIQUE index is
        # what actually stops the second credit. Flushing here means the loser finds out BEFORE the
        # balance is touched — and losing the race then gives the same answer as the sequential path
        # instead of a 500 that makes Stripe retry forever.
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = await _block_for_payment(db, payment_ref)
        if winner is None:  # pragma: no cover — the constraint fired, so a row exists
            raise
        return winner
    await _add_balance(db, org_id, int(amount_micro))
    await _entry(db, org_id=org_id, kind="topup", amount_micro=int(amount_micro), block_id=block.id,
                 meta={**(meta or {}), "payment_ref": payment_ref})
    await db.commit()
    return block


async def _block_for_payment(db: AsyncSession, payment_ref: str) -> CreditBlock | None:
    return (await db.execute(
        select(CreditBlock).where(CreditBlock.stripe_payment_intent == payment_ref)
    )).scalars().first()


# ---- the call path -----------------------------------------------------------------------------
async def reserve(
    db: AsyncSession, org_id: int, endpoint_id: str, est_micro: int, *, meta: dict | None = None,
    tags: dict[str, str] | None = None, call_id: str | None = None,
) -> str:
    """Withhold the estimated cost of one call and return its `call_id`. Commits.

    `est_micro` is the RAW provider estimate; the margin is applied here (see module docstring), so
    the amount actually withheld is `with_margin(est_micro)`. Raises `InsufficientBalance` when the
    balance can't cover it — the caller turns that into a 402.

    `tags` is the caller's `X-Treg-Meta` bag. An EXPLICIT parameter, never read out of `meta`: what a
    call is attributed to decides who a builder bills, so it may not arrive through the same
    free-form channel that carries provenance. `settle`/`release` take it back off the stored rows
    rather than from their own caller — whoever reserved is whoever pays, so a header changing
    mid-flight cannot reattribute a charge that is already in progress.

    Reaps this org's stale holds first, so a call that crashed before settling can't keep money out
    of circulation (and can't make a funded org look broke).
    """
    await reap_stale_holds(db, org_id=org_id)
    charged = max(0, with_margin(int(est_micro)))
    # The caller may supply the id so ONE value identifies the call everywhere: the hold, both
    # ledger entries, the audit row and the X-Treg-Call-Id the caller gets back.
    call_id = call_id or _id()
    result = await db.execute(
        # The gate. One statement: the WHERE is the check and the SET is the debit, so two concurrent
        # callers cannot both pass it. rowcount 0 = the balance was not there.
        update(Org)
        .where(Org.id == org_id, Org.balance_micro >= charged)
        .values(balance_micro=Org.balance_micro - charged)
    )
    if result.rowcount != 1:
        await db.rollback()
        balance = (await db.execute(select(Org.balance_micro).where(Org.id == org_id))).scalar() or 0
        raise InsufficientBalance(org_id, int(balance), charged)
    db.add(Hold(id=call_id, org_id=org_id, endpoint_id=endpoint_id or "", amount_micro=charged))
    await _entry(db, org_id=org_id, kind="reserve", amount_micro=-charged, call_id=call_id,
                 endpoint_id=endpoint_id,
                 meta={**(meta or {}),
                       "estimated_micro": int(est_micro), "charged_micro": charged,
                       "margin": get_settings().platform_margin})
    # One row per caller tag, at the estimate, in THIS transaction. A builder's budget and invoice are
    # read from these; writing them anywhere else (or later) would make both lossy.
    for dim, val in (tags or {}).items():
        db.add(TagSpend(org_id=org_id, dim=dim, val=val, hold_id=call_id, amount_micro=charged))
    await db.commit()
    return call_id


class _ClaimedHold(NamedTuple):
    """A hold's fields, COPIED OUT before its row was deleted.

    Deliberately values and not the ORM object: the `delete()` that claims the row can expire the
    matching instance in the session, and touching an expired attribute afterwards is implicit async
    I/O (MissingGreenlet). Reading everything up front makes that impossible rather than merely
    unlikely.
    """

    amount_micro: int
    org_id: int
    endpoint_id: str


async def _claim_hold(db: AsyncSession, call_id: str) -> _ClaimedHold | None:
    """Take exclusive ownership of one open hold, or return None if it is gone or already claimed.

    CLAIM the hold before any money moves: the DELETE's rowcount is what decides which of
    settle/release gets to close it. Loading the row and testing it is a check-then-act, and two
    paths that both read before either writes will both proceed — settle consuming blocks while
    release refunds the same money, which breaks
    `balance == sum(blocks) - sum(open holds)` and (since tag rows live here too) deletes a
    customer's spend out of the invoice after it has already left the balance.
    Same idiom as `reserve`'s conditional UPDATE, for the same reason: where two paths read before
    either writes, the database has to be the one that says no.
    """
    hold = await db.get(Hold, call_id)
    if hold is None:
        return None
    claimed = _ClaimedHold(hold.amount_micro, hold.org_id, hold.endpoint_id)
    result = await db.execute(delete(Hold).where(Hold.id == call_id))
    if result.rowcount != 1:
        # Lost the claim: somebody else is closing this hold. Deliberately NO rollback — the DELETE
        # matched nothing and only a read precedes it, so there is nothing to discard, and rolling
        # back would EXPIRE every object in the session. `reap_stale_holds` shares one session across
        # a loop of preloaded Hold rows, so that expiry made the next `hold.id` attempt implicit async
        # I/O (MissingGreenlet) and killed the rest of the sweep.
        return None
    return claimed


async def settle(
    db: AsyncSession, call_id: str, actual_micro: int | None = None, *, meta: dict | None = None,
) -> int:
    """Close a hold against what the call ACTUALLY cost, and return the settled amount. Commits.

    `actual_micro` is the raw provider cost (margin applied here); `None` means "we never learned the
    real cost" and settles at the full reserved amount. Consumes blocks promotional-first then
    oldest-purchased-first, refunds `reserved - settled` to the balance, and deletes the hold.

    A no-op returning 0 when the hold is already gone — the reaper or a retry got there first, and a
    double settle must not charge twice.
    """
    hold = await _claim_hold(db, call_id)
    if hold is None:
        return 0
    reserved = hold.amount_micro
    # ONE instant for this settlement — shared by the ledger entry and the tag rows below.
    settled_at = _now()
    settled = reserved if actual_micro is None else max(0, with_margin(int(actual_micro)))
    consumed, shortfall = await _consume_blocks(db, hold.org_id, settled, call_id, hold.endpoint_id)
    # The hold came out of the balance at reserve time; give back whatever the call didn't use. If the
    # observed cost overran the estimate the delta is negative, which correctly takes MORE balance —
    # the next reserve is the gate that stops an overrun from compounding.
    if reserved != consumed:
        await _add_balance(db, hold.org_id, reserved - consumed)
    await _entry(
        db, org_id=hold.org_id, kind="settle", amount_micro=-consumed, call_id=call_id,
        endpoint_id=hold.endpoint_id, created_at=settled_at,
        meta={**(meta or {}),
              "reserved_micro": reserved, "observed_micro": None if actual_micro is None else int(actual_micro),
              "settled_micro": settled, "consumed_micro": consumed,
              "refunded_micro": reserved - consumed, "margin": get_settings().platform_margin,
              # Blocks came up short of what the call cost (price drift, or a race with a refund):
              # the entry says so rather than the balance silently absorbing it. Written
              # UNCONDITIONALLY (0 when there was none) — as a conditional key a caller could
              # supply their own `block_shortfall_micro` and it would survive the merge on every
              # call that had no shortfall, which is most of them.
              "block_shortfall_micro": shortfall},
    )
    # The tag rows were written at the ESTIMATE; move them to what the call actually consumed and mark
    # them settled, which is what makes them invoiceable.
    # `created_at` is restamped to SETTLE time on purpose: `spend_since` windows on the settle
    # entry, so a hold opened before a window and settled inside it belongs to that window. Left
    # at reserve time, such a call showed up in the org total but not in any tag total.
    await db.execute(update(TagSpend).where(TagSpend.hold_id == call_id)
                     .values(amount_micro=consumed, settled=True, created_at=settled_at))
    await db.commit()
    return consumed


async def release(db: AsyncSession, call_id: str, *, reason: str = "", meta: dict | None = None) -> int:
    """Return a hold in FULL and close it — the call produced nothing billable (provider 5xx, network
    error, our own crash before relay). Returns the amount returned; 0 if the hold is already gone.
    Commits."""
    hold = await _claim_hold(db, call_id)
    if hold is None:
        return 0
    amount = hold.amount_micro
    await _add_balance(db, hold.org_id, amount)
    await _entry(db, org_id=hold.org_id, kind="release", amount_micro=amount, call_id=call_id,
                 endpoint_id=hold.endpoint_id, meta={**(meta or {}), "reason": reason})
    # Nothing was billable, so nothing is attributable: the tag rows go with the hold. Leaving them
    # would bill a builder's user for a call the provider never completed.
    await db.execute(delete(TagSpend).where(TagSpend.hold_id == call_id))
    await db.commit()
    return amount


async def _consume_blocks(
    db: AsyncSession, org_id: int, amount_micro: int, call_id: str, endpoint_id: str,
) -> tuple[int, int]:
    """Draw `amount_micro` from the org's blocks, promotional-first then oldest-purchased-first.
    Returns (consumed, shortfall). Does NOT commit — the caller's transaction owns it.

    Consumption is capped by what the blocks actually hold: a shortfall is reported, never invented
    as negative `remaining_micro`.
    """
    if amount_micro <= 0:
        return 0, 0
    blocks = (await db.execute(
        select(CreditBlock).where(CreditBlock.org_id == org_id, CreditBlock.remaining_micro > 0)
    )).scalars().all()
    blocks.sort(key=lambda b: (_KIND_ORDER.get(b.kind, 99), b.created_at or _now(), b.id))
    left, consumed = amount_micro, 0
    for block in blocks:
        if left <= 0:
            break
        take = min(block.remaining_micro, left)
        block.remaining_micro -= take
        left -= take
        consumed += take
    return consumed, left


# ---- reaper ------------------------------------------------------------------------------------
def hold_ttl_s() -> int:
    """How long a hold may stay open before it is presumed stranded: the relay's own upstream
    timeout plus a grace window for our settle to land."""
    s = get_settings()
    return int(s.call_timeout_s) + int(s.hold_grace_s)


async def reap_stale_holds(db: AsyncSession, *, org_id: int | None = None, limit: int = 100) -> int:
    """Release holds older than `hold_ttl_s()` — a call that old cannot still be settling, so the
    money belongs back in the balance. `org_id=None` sweeps every org (for an admin/ops call);
    `reserve` passes its own org, which is the lazy strategy that keeps this off any scheduler.
    Returns how many were released. Commits (via `release`)."""
    cutoff = _now() - timedelta(seconds=hold_ttl_s())
    q = select(Hold).where(Hold.created_at < cutoff).order_by(Hold.created_at).limit(limit)
    if org_id is not None:
        q = q.where(Hold.org_id == org_id)
    stale = (await db.execute(q)).scalars().all()
    for hold in stale:
        await release(db, hold.id, reason="stale_hold_reaped",
                      meta={"age_s": int((_now() - hold.created_at).total_seconds())})
    return len(stale)


# ---- reads -------------------------------------------------------------------------------------
async def spent_today(db: AsyncSession, org_id: int) -> int:
    """Micro-USD this org has committed since midnight UTC: everything SETTLED today plus everything
    still HELD from today. Two indexed aggregates, and the number a daily spend cap is checked against.

    Deliberately not "sum of reserve entries": a reserve is refunded at settle, so counting both would
    double-charge every call. Settled + still-open is exactly the money that is gone or promised.
    """
    since = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    settled = (await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0)).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle", LedgerEntry.created_at >= since)
    )).scalar() or 0
    held = (await db.execute(
        select(func.coalesce(func.sum(Hold.amount_micro), 0)).where(
            Hold.org_id == org_id, Hold.created_at >= since)
    )).scalar() or 0
    return int(-settled) + int(held)  # settle entries are negative (money leaving); holds are positive


async def tag_calls_since(db: AsyncSession, org_id: int, dim: str, val: str, since: datetime) -> int:
    """How many billable calls one tag has made since `since`.

    Counted from `TagSpend`, NOT from `CallRecord`, for two reasons that both silently broke a cap:
    audit rows are shed under load, so a call-count cap would let a burst straight through exactly
    when it matters; and `CallRecord` only carries the PRIMARY dimension, so a cap on any other
    declared dimension matched nothing and never fired at all.

    Counts BILLABLE calls: a team calling on its own key writes no tag row, and a released call has
    its rows deleted. That is the right grain for a reseller's plan limit — say so in the docs.
    """
    total = (await db.execute(
        select(func.count()).select_from(TagSpend).where(
            TagSpend.org_id == org_id, TagSpend.dim == dim, TagSpend.val == val,
            TagSpend.created_at >= since)
    )).scalar() or 0
    return int(total)


async def calls_by_tag(db: AsyncSession, org_id: int, dim: str, since: datetime) -> dict[str, int]:
    """Billable call counts for every value of one tag key, grouped in SQL over the same index."""
    rows = (await db.execute(
        select(TagSpend.val, func.count()).where(
            TagSpend.org_id == org_id, TagSpend.dim == dim, TagSpend.created_at >= since)
        .group_by(TagSpend.val)
    )).all()
    return {val: int(n) for val, n in rows}


async def tag_spent_since(db: AsyncSession, org_id: int, dim: str, val: str, since: datetime) -> int:
    """What one tag has committed since `since` — settled spend PLUS money still held for it.

    The cap read. In-flight holds count at their ESTIMATE, so a burst of concurrent calls is counted
    before any of it settles and the cap errs toward refusing — the correct direction for money.

    One indexed aggregate. That is the whole reason `TagSpend` exists as a table instead of a JSON key:
    this runs on every proxied call.
    """
    total = (await db.execute(
        select(func.coalesce(func.sum(TagSpend.amount_micro), 0)).where(
            TagSpend.org_id == org_id, TagSpend.dim == dim, TagSpend.val == val,
            TagSpend.created_at >= since)
    )).scalar() or 0
    return int(total)


async def tag_invoice_since(db: AsyncSession, org_id: int, dim: str, val: str, since: datetime) -> int:
    """What one tag actually SPENT since `since` — settled rows only.

    The invoice read, deliberately not the same function as `tag_spent_since`. An open hold is not
    spend: billing it would charge for work in flight and then charge again when it settles.
    """
    total = (await db.execute(
        select(func.coalesce(func.sum(TagSpend.amount_micro), 0)).where(
            TagSpend.org_id == org_id, TagSpend.dim == dim, TagSpend.val == val,
            TagSpend.settled.is_(True), TagSpend.created_at >= since)
    )).scalar() or 0
    return int(total)


async def spend_by_tag(db: AsyncSession, org_id: int, dim: str, since: datetime) -> dict[str, int]:
    """Settled spend for every value of one tag key — `{"cust_8123": 41234, ...}`. What a builder
    invoices from. Grouped in SQL over the same index the cap uses; no JSON folding, so it stays
    portable across sqlite and Postgres (see reconcile.py's docstring for why that matters)."""
    rows = (await db.execute(
        select(TagSpend.val, func.coalesce(func.sum(TagSpend.amount_micro), 0)).where(
            TagSpend.org_id == org_id, TagSpend.dim == dim,
            TagSpend.settled.is_(True), TagSpend.created_at >= since)
        .group_by(TagSpend.val)
    )).all()
    return {val: int(total) for val, total in rows}


async def spend_since(db: AsyncSession, org_id: int, since: datetime) -> dict:
    """What this org has actually SPENT on platform keys since `since`: total micro-USD and how many
    calls it took. One aggregate over the settle entries — the ledger, not the audit table, is the
    authority on money (audit rows are allowed to be lost; ledger rows are not)."""
    row = (await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0), func.count()).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle", LedgerEntry.created_at >= since)
    )).first()
    total, calls = (row[0] or 0, row[1] or 0) if row else (0, 0)
    return {"spend_micro": int(-total), "spend_usd": usd(int(-total)), "billed_calls": int(calls)}


async def balance_of(db: AsyncSession, org_id: int) -> int:
    return int((await db.execute(select(Org.balance_micro).where(Org.id == org_id))).scalar() or 0)


async def blocks_of(db: AsyncSession, org_id: int) -> list[CreditBlock]:
    rows = (await db.execute(
        select(CreditBlock).where(CreditBlock.org_id == org_id).order_by(CreditBlock.created_at.desc())
    )).scalars().all()
    return list(rows)


async def entries_of(db: AsyncSession, org_id: int, *, limit: int = 50, offset: int = 0) -> list[LedgerEntry]:
    rows = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.org_id == org_id)
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .offset(offset).limit(limit)
    )).scalars().all()
    return list(rows)


async def open_holds_of(db: AsyncSession, org_id: int) -> list[Hold]:
    rows = (await db.execute(
        select(Hold).where(Hold.org_id == org_id).order_by(Hold.created_at)
    )).scalars().all()
    return list(rows)
