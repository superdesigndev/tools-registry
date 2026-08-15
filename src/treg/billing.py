"""The ONE module that talks to Stripe — the mirror of `ledger.py`, which is the one module that
moves money. Neither reaches into the other's job: Stripe authorizes a payment here, and the ledger
credits balance there, and the only thing that crosses between them is
`ledger.topup(org, amount_micro, payment_ref)`.

Two unit systems meet in this file and NOWHERE else:
  * our ledger speaks **integer micro-USD** (1e-6 USD) — a catalog call costs ~600 micro,
  * Stripe speaks **integer cents**,
so 1 cent == 10_000 micro and every crossing goes through `micro_to_cents` / `cents_to_micro`,
which refuse to lose a fraction rather than silently rounding someone's money away. Whole dollars
appear only in the settings and in what a human types.

**Credit happens on the WEBHOOK, not on the browser's return from Checkout.** The success redirect is
a URL the payer controls — treating it as proof of payment means anyone can type it and mint balance.
The one exception is the off-session auto-top-up charge, where the *server itself* holds the
PaymentIntent's confirmed status in its hand (nothing attacker-supplied is involved), so it credits
immediately and the webhook redelivery lands as a no-op. That safety comes from `ledger.topup` being
idempotent on the PaymentIntent id, which is also what makes Stripe's at-least-once webhook delivery
harmless: the same event twice credits once.

Async: the Stripe SDK is synchronous and this is an async FastAPI app, so every SDK call goes through
`_sdk()` onto a worker thread. A blocking network call on the event loop would stall every other
in-flight request, including the proxy's hot path.

Auto-top-up is the part that can go wrong expensively, so it is guarded in depth (see
`attempt_auto_topup`): recorded consent (the PSD2/SCA mandate — a compliance requirement, not a
checkbox), a monthly cap, a cooldown stamped in the DB *before* the charge so a second web worker
sees it, a consecutive-failure limit, and an idempotency key derived from the threshold crossing so a
burst of concurrent calls that all notice the low balance produces exactly ONE charge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

import anyio.to_thread
import stripe
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from . import analytics
from . import email as email_mod
from . import ledger
from .config import get_settings
from .models import CreditBlock, LedgerEntry, Membership, Org, User

log = logging.getLogger("treg.billing")

MICRO_PER_CENT = 10_000
MICRO_PER_USD = 1_000_000
# A ceiling on a single top-up. Not a business rule — a fat-finger guard: the difference between "$50"
# and "$5000" is one keystroke, and a card that big is likelier a mistake or a stolen number than a
# customer. Raise it deliberately when someone asks.
TOPUP_MAX_USD = 2_000
# Tags Checkout sessions in the Stripe dashboard so this flow's conversion can be compared against
# any other we add later (required suffix is 8 letters — see Stripe's integration_identifier docs).
INTEGRATION_ID = "tregtopup-qwkmzhrb"


class BillingNotConfigured(Exception):
    """No `TREG_STRIPE_SECRET_KEY`. The endpoints turn this into a 503 — top-ups are simply off on
    this deployment, which is a different thing from the caller doing something wrong."""


class TopupRejected(Exception):
    """The requested amount isn't one we'll charge (below the minimum, not whole dollars, absurd).
    Carries a message written for the person who typed it; the endpoints return it as a 422."""


# ---- units -------------------------------------------------------------------------------------
def usd_to_micro(amount_usd: int | float) -> int:
    """Whole-dollar setting → micro-USD. Accepts a float only so a config like `5.0` works; the
    result is always an integer, and fractional cents are refused rather than rounded."""
    micro = round(float(amount_usd) * MICRO_PER_USD)
    if micro % MICRO_PER_CENT:
        raise TopupRejected(f"${amount_usd} is not a whole number of cents")
    return int(micro)


def micro_to_cents(amount_micro: int) -> int:
    """micro-USD → the integer cents Stripe charges. Refuses a sub-cent remainder: Stripe cannot
    charge it, so silently dropping it would make the ledger and the card disagree."""
    if int(amount_micro) % MICRO_PER_CENT:
        raise TopupRejected(f"{amount_micro} micro-USD is not a whole number of cents")
    return int(amount_micro) // MICRO_PER_CENT


def cents_to_micro(amount_cents: int) -> int:
    """The integer cents Stripe actually charged → micro-USD for the ledger. Always exact."""
    return int(amount_cents) * MICRO_PER_CENT


def validate_topup_usd(amount_usd) -> int:
    """The one gate on "how much". Returns whole dollars; raises `TopupRejected` with a message the
    payer can act on.

    Whole dollars only, at or above `topup_min_usd`. The presets are SUGGESTIONS, not an allow-list —
    someone who wants $37 should get $37; the minimum is the part that has to hold, because at
    2.9% + $0.30 a $1 top-up loses a third of itself to fees.
    """
    s = get_settings()
    try:
        value = float(amount_usd)
    except (TypeError, ValueError):
        raise TopupRejected("amount must be a number of US dollars")
    if value != int(value):
        raise TopupRejected("top-ups are whole dollars (e.g. 10, not 10.50)")
    value = int(value)
    if value < s.topup_min_usd:
        raise TopupRejected(f"the minimum top-up is ${s.topup_min_usd} (card fees eat a smaller one)")
    if value > TOPUP_MAX_USD:
        raise TopupRejected(f"the maximum single top-up is ${TOPUP_MAX_USD}")
    return value


def _now() -> datetime:
    # Naive UTC — the models._now convention (TIMESTAMP WITHOUT TIME ZONE; asyncpg rejects tz-aware).
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- the Stripe SDK, off the event loop --------------------------------------------------------
def configured() -> bool:
    return bool(get_settings().stripe_secret_key)


def _require_configured() -> str:
    key = get_settings().stripe_secret_key
    if not key:
        raise BillingNotConfigured("TREG_STRIPE_SECRET_KEY is not set — top-ups are disabled")
    return key


async def _sdk(fn, /, **kwargs):
    """Run one Stripe SDK call on a worker thread with our key. The SDK is sync; the app is not.

    The key is passed per-call (`api_key=`) rather than set on the module, so nothing depends on
    global mutable state and a test can monkeypatch this single function to intercept every call.
    """
    key = _require_configured()
    return await anyio.to_thread.run_sync(lambda: fn(api_key=key, **kwargs))


# ---- policy reads ------------------------------------------------------------------------------
def autotopup_prefs(org: Org) -> dict:
    """The org's effective auto-top-up numbers in micro-USD. A stored 0 means "we never chose", so it
    resolves to the deployment default — which is what lets the default move for everyone who never
    set their own, without a data migration."""
    s = get_settings()
    return {
        "threshold_micro": int(org.autotopup_threshold_micro or usd_to_micro(s.autotopup_default_threshold_usd)),
        "amount_micro": int(org.autotopup_amount_micro or usd_to_micro(s.autotopup_default_amount_usd)),
        "monthly_cap_micro": int(org.autotopup_monthly_cap_micro or usd_to_micro(s.autotopup_monthly_cap_usd)),
    }


def _month_start() -> datetime:
    n = _now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def monthly_autotopup_spend(db: AsyncSession, org_id: int) -> int:
    """How much AUTOMATIC top-up this org has bought since the 1st, in micro-USD.

    Computed from the ledger rather than a counter column, because the ledger is the record that gets
    reconciled against Stripe — a counter can drift, and a cap enforced against a drifted counter is
    either a runaway or a lockout. Manual top-ups are excluded on purpose: the cap exists to bound
    what happens WITHOUT a human, and a human choosing to add funds is not that.
    """
    # The `auto` flag lives in the entry's JSON meta, and JSON containment isn't portable across
    # SQLite and Postgres — so the month's top-ups are narrowed in SQL (indexed on org + created_at)
    # and the flag is matched in Python. A month's top-ups is a handful of rows, not a scan.
    rows = (await db.execute(
        select(LedgerEntry.amount_micro, LedgerEntry.meta).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "topup",
            LedgerEntry.created_at >= _month_start(),
        )
    )).all()
    return sum(int(a) for a, meta in rows if isinstance(meta, dict) and meta.get("auto"))


async def owner_email(db: AsyncSession, org_id: int) -> str:
    """Where a billing email goes: the org's owner, falling back to an admin. Billing is not team
    news — it names a card and a charge — so it goes to whoever can actually act on it, not to
    everyone with a token."""
    rows = (await db.execute(
        select(User.email, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id, Membership.role.in_(("owner", "admin")))
    )).all()
    for email, role in rows:
        if role == "owner":
            return email
    return rows[0][0] if rows else ""


# ---- customer + payment method -----------------------------------------------------------------
async def get_or_create_customer(db: AsyncSession, org: Org, *, email: str = "") -> str:
    """The org's Stripe Customer id, creating it on first use. Commits when it creates one.

    Stored on the org and never recreated: the Customer is what the saved card hangs off, so a second
    one would orphan the payment method and silently break auto-top-up.
    """
    if org.stripe_customer_id:
        return org.stripe_customer_id
    if not email:
        email = await owner_email(db, org.id)
    customer = await _sdk(
        stripe.Customer.create,
        name=org.name or org.slug,
        **({"email": email} if email else {}),
        # The org id travels with the customer so a Stripe-side investigation ("who is this charge
        # for?") resolves without a lookup table.
        metadata={"treg_org_id": str(org.id), "treg_org_slug": org.slug},
    )
    org.stripe_customer_id = customer["id"]
    db.add(org)
    await db.commit()
    return org.stripe_customer_id


async def _set_default_pm(db: AsyncSession, org_id: int, payment_method: str | None) -> bool:
    """Remember which saved card to charge off-session. An opaque `pm_…` reference — no card data
    ever reaches our database. Returns True when something changed."""
    if not payment_method:
        return False
    org = await db.get(Org, org_id)
    if org is None or org.stripe_default_pm == payment_method:
        return False
    org.stripe_default_pm = payment_method
    db.add(org)
    await db.commit()
    return True


async def _pm_of_payment_intent(pi_id: str) -> str | None:
    """The PaymentMethod a succeeded PaymentIntent used — how a Checkout with
    `setup_future_usage` hands us the card to reuse, without any Stripe.js on our side."""
    try:
        pi = await _sdk(stripe.PaymentIntent.retrieve, id=pi_id)
    except Exception as e:  # noqa: BLE001 — a card we can't look up just means no auto-top-up yet
        log.warning("billing: could not retrieve PaymentIntent %s: %s", pi_id, e)
        return None
    pm = pi.get("payment_method")
    return pm if isinstance(pm, str) else (pm or {}).get("id")


# ---- manual top-up (Stripe-hosted Checkout) ----------------------------------------------------
async def create_topup_checkout(
    db: AsyncSession, org: Org, amount_usd, *, return_base: str = "", email: str = "",
) -> dict:
    """Start a hosted Checkout for a one-off top-up and return `{url, session_id, amount_micro}`.

    Stripe-hosted (`mode=payment`) rather than an embedded form: it needs no Stripe.js, no card fields
    of ours, and no PCI surface — the payer leaves, pays on Stripe, and comes back.

    `setup_future_usage="off_session"` is what makes auto-top-up possible later: it saves the card
    WITH the mandate signals PSD2/SCA requires for an unattended charge. Asking for it at purchase
    time costs the payer nothing; retrofitting consent afterwards costs them a second card entry.

    `invoice_creation` is what makes the payment expensable. Stripe's card receipt proves a card was
    charged; an invoice is the document with an invoice number, a billing address and a tax ID on it,
    which is what a finance team actually accepts. It is post-purchase — the invoice appears once the
    payment succeeds, not when the session opens — so nothing here changes what the payer sees.

    Currency is pinned to USD explicitly — the Stripe account's own default is AUD, and inheriting it
    would charge a number the ledger would then credit as dollars.
    """
    amount = validate_topup_usd(amount_usd)
    amount_micro = usd_to_micro(amount)
    customer = await get_or_create_customer(db, org, email=email)
    base = (return_base or get_settings().public_url).rstrip("/")
    session = await _sdk(
        stripe.checkout.Session.create,
        mode="payment",
        customer=customer,
        # No `payment_method_types`: omitting it lets Stripe pick the methods most likely to convert
        # for this payer, configured from the dashboard rather than from our code.
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": micro_to_cents(amount_micro),
                "product_data": {
                    "name": "treg balance top-up",
                    "description": f"${amount} of prepaid API call balance for {org.name or org.slug}",
                },
            },
        }],
        payment_intent_data={
            # The card is saved for auto-top-up (see docstring). The metadata is repeated here because
            # the webhook may arrive as `payment_intent.succeeded`, which carries the PI's metadata and
            # not the session's — the credit path must work from either event.
            "setup_future_usage": "off_session",
            "metadata": {"treg_org_id": str(org.id), "treg_kind": "topup", "treg_auto": "0"},
            **({"receipt_email": email} if email else {}),
        },
        # The expensable document (see docstring). `invoice_data` carries the same org identity as the
        # session so an invoice found in the Stripe dashboard resolves to a team without a lookup.
        invoice_creation={
            "enabled": True,
            "invoice_data": {
                "description": f"Prepaid API call balance for {org.name or org.slug}",
                "metadata": {"treg_org_id": str(org.id), "treg_kind": "topup"},
            },
        },
        # Idempotent per (org, amount, minute): a double-clicked "Add funds" reuses the same session
        # instead of opening a second one the payer might also complete.
        metadata={"treg_org_id": str(org.id), "treg_kind": "topup"},
        integration_identifier=INTEGRATION_ID,
        success_url=f"{base}/app?topup=success#billing",
        cancel_url=f"{base}/app?topup=cancelled#billing",
    )
    return {"url": session["url"], "session_id": session["id"],
            "amount_micro": amount_micro, "amount_usd": amount}


async def create_setup_checkout(db: AsyncSession, org: Org, *, return_base: str = "", email: str = "") -> dict:
    """Start a hosted Checkout in `mode=setup` — save a card WITHOUT charging it, so auto-top-up can
    be armed by an org that hasn't bought anything yet.

    Chosen over a bare SetupIntent + Elements for exactly one reason: Elements needs Stripe.js and a
    card form we would own. This is the same hosted page as the top-up flow, so there is one payment
    surface to reason about instead of two.
    """
    customer = await get_or_create_customer(db, org, email=email)
    base = (return_base or get_settings().public_url).rstrip("/")
    session = await _sdk(
        stripe.checkout.Session.create,
        mode="setup",
        customer=customer,
        # `off_session` tells Stripe the saved card is destined for unattended charges, which is what
        # makes the bank collect the SCA mandate NOW instead of declining the first auto-charge later.
        setup_intent_data={"metadata": {"treg_org_id": str(org.id), "treg_kind": "autotopup_card"},
                           "usage": "off_session"},
        metadata={"treg_org_id": str(org.id), "treg_kind": "autotopup_card"},
        integration_identifier=INTEGRATION_ID,
        success_url=f"{base}/app?card=saved#billing",
        cancel_url=f"{base}/app?card=cancelled#billing",
    )
    return {"url": session["url"], "session_id": session["id"], "mode": "setup"}


# ---- the hosted billing portal -----------------------------------------------------------------
async def create_portal_session(db: AsyncSession, org: Org, *, return_base: str = "") -> dict:
    """A one-time link into Stripe's hosted Customer Portal, where the payer manages their own
    billing details: card, billing address, tax ID, and the invoice history with its PDFs.

    Hosted rather than rebuilt here for the same reason Checkout is: every one of those fields is a
    form we would otherwise own, and the tax-ID one carries validation rules per country that go
    stale. The portal also renders the invoice archive we now generate, so "download an old invoice"
    needs no page of ours at all.

    Requires a portal CONFIGURATION saved in the Stripe dashboard (Settings → Billing → Customer
    portal); without one Stripe rejects the call, which surfaces here as the usual Stripe error.

    Refuses an org with no Stripe customer instead of creating one. A customer exists once someone has
    paid or saved a card; minting one to open an empty portal would fill the Stripe account with
    customers that never bought anything, and the portal would have nothing to show anyway.
    """
    _require_configured()
    if not org.stripe_customer_id:
        raise TopupRejected("no billing details yet — add funds once and the portal opens after that")
    base = (return_base or get_settings().public_url).rstrip("/")
    session = await _sdk(
        stripe.billing_portal.Session.create,
        customer=org.stripe_customer_id,
        return_url=f"{base}/app#billing",
    )
    return {"url": session["url"]}


# ---- payment history ---------------------------------------------------------------------------
async def list_payments(db: AsyncSession, org: Org, *, limit: int = 24) -> dict:
    """The org's completed top-ups, newest first, each with a link to its invoice or receipt.

    The ROWS come from our own `CreditBlock` table, not from Stripe. That table is what the balance is
    computed from, so a history built on it can never show a payment the balance disagrees with — and
    it needs no network call to render amounts and dates.

    Stripe is asked only for the DOCUMENTS, in two list calls rather than two per row: charges (which
    carry `receipt_url` and point at the invoice) and invoices (which carry the PDF). A failure there
    degrades to rows without links — a Stripe hiccup should cost the payer their download button, not
    their payment history. `stripe_ok` says which happened so the UI can tell them.

    Both Stripe windows cap at 100 payments, so a very old top-up on a heavily-used account can come
    back link-less; the portal (`create_portal_session`) is the unbounded archive.

    Read-only: nothing here moves money or writes to the ledger.
    """
    blocks = (await db.execute(
        select(CreditBlock)
        .where(CreditBlock.org_id == org.id, CreditBlock.kind == "purchased",
               CreditBlock.stripe_payment_intent.is_not(None))
        .order_by(CreditBlock.created_at.desc())
        .limit(max(1, min(int(limit), 100)))
    )).scalars().all()

    # Which of these top-ups were automatic. The flag lives in the ledger entry's JSON meta, and JSON
    # containment isn't portable across SQLite and Postgres — so the rows are narrowed by block id
    # (exactly the page we're rendering) and the flag is read in Python, the same shape as
    # `monthly_autotopup_spend`.
    auto: set[str] = set()
    if blocks:
        rows = (await db.execute(
            select(LedgerEntry.block_id, LedgerEntry.meta).where(
                LedgerEntry.org_id == org.id, LedgerEntry.kind == "topup",
                LedgerEntry.block_id.in_([b.id for b in blocks]),
            )
        )).all()
        auto = {str(bid) for bid, meta in rows if isinstance(meta, dict) and meta.get("auto")}

    docs, stripe_ok = await _payment_documents(org, len(blocks))
    items = []
    for b in blocks:
        d = docs.get(b.stripe_payment_intent or "", {})
        items.append({
            "payment_intent": b.stripe_payment_intent,
            "amount_micro": int(b.amount_micro),
            "amount_usd": ledger.usd(int(b.amount_micro)),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "auto": b.id in auto,
            "invoice_number": d.get("number") or "",
            "invoice_pdf": d.get("invoice_pdf") or "",
            "hosted_invoice_url": d.get("hosted_invoice_url") or "",
            "receipt_url": d.get("receipt_url") or "",
        })
    return {"items": items, "stripe_ok": stripe_ok}


async def _payment_documents(org: Org, wanted: int) -> tuple[dict[str, dict], bool]:
    """`{payment_intent_id: {receipt_url, number, invoice_pdf, hosted_invoice_url}}` for one customer.

    Two list calls, joined in memory through the charge's `invoice` field. Returns `({}, False)` on
    any Stripe failure — the caller renders amounts without links rather than a 500.
    """
    if not (org.stripe_customer_id and wanted and configured()):
        return {}, bool(configured())
    window = max(wanted, 100)
    try:
        charges = await _sdk(stripe.Charge.list, customer=org.stripe_customer_id, limit=window)
        invoices = await _sdk(stripe.Invoice.list, customer=org.stripe_customer_id, limit=window)
    except Exception as e:  # noqa: BLE001 — see docstring: links are optional, the history is not
        log.warning("billing: could not load payment documents for org %s: %s", org.id, e)
        return {}, False

    by_invoice = {}
    for inv in (invoices.get("data") or []):
        if inv.get("id"):
            by_invoice[inv["id"]] = {"number": inv.get("number") or "",
                                     "invoice_pdf": inv.get("invoice_pdf") or "",
                                     "hosted_invoice_url": inv.get("hosted_invoice_url") or ""}
    out: dict[str, dict] = {}
    for ch in (charges.get("data") or []):
        pi = ch.get("payment_intent")
        pi = pi if isinstance(pi, str) else (pi or {}).get("id")
        if not pi:
            continue
        inv = ch.get("invoice")
        inv = inv if isinstance(inv, str) else (inv or {}).get("id")
        out[pi] = {"receipt_url": ch.get("receipt_url") or "", **by_invoice.get(inv or "", {})}
    return out, True


# ---- auto top-up -------------------------------------------------------------------------------
# Per-org in-process lock. Cheap first line of defence against a burst of concurrent calls all
# noticing the same low balance; the DB cooldown below is what covers the multi-instance case, and
# the Stripe idempotency key is what covers both even if the first two fail.
_locks: dict[int, asyncio.Lock] = {}


def _lock(org_id: int) -> asyncio.Lock:
    lock = _locks.get(org_id)
    if lock is None:
        lock = _locks[org_id] = asyncio.Lock()
    return lock


def _idempotency_key(org_id: int, amount_micro: int, crossing: int, payment_method: str) -> str:
    """One key per threshold CROSSING, not per attempt.

    `crossing` is how many automatic top-ups this org has already bought this month, so every caller
    that notices the same dip computes the same key and Stripe collapses them into a single charge —
    while a genuine later dip (crossing+1) is free to charge again. The day is included so a stuck
    counter can't suppress tomorrow's charge forever.

    `payment_method` is in the key because Stripe rejects a reused key whose PARAMETERS changed, with
    an error, not a replay. Without it, one failed attempt would poison the crossing: swap the declined
    card for a working one and the retry dies on "keys for idempotent requests can only be used with
    the same parameters" until the day rolls over. Including it keeps the collapse-a-burst property
    (same card, same key) while letting a genuinely different charge through.
    """
    raw = f"autotopup:{org_id}:{_now():%Y-%m-%d}:{crossing}:{amount_micro}:{payment_method}"
    return "treg_" + hashlib.sha256(raw.encode()).hexdigest()[:40]


async def _disable_autotopup(db: AsyncSession, org: Org, reason: str, *, recovery_pi: str = "") -> None:
    """Turn auto-top-up off and SAY WHY. A silent disable is how an org finds out from its agents
    failing; the reason is what the dashboard banner and the email are built from."""
    org.autotopup_enabled = False
    org.autotopup_disabled_reason = reason
    if recovery_pi:
        org.autotopup_recovery_pi = recovery_pi
    db.add(org)
    await db.commit()
    to = await owner_email(db, org.id)
    if to:
        await email_mod.send_autotopup_disabled(to, org.name or org.slug, reason,
                                                recovery_pi=bool(recovery_pi))


async def attempt_auto_topup(db: AsyncSession, org_id: int) -> dict:
    """Charge the saved card off-session to refill a low balance. Returns
    `{"ok": bool, "reason": str, ...}` and never raises at the caller — a failed refill must degrade
    to "the agent gets a 402", never to a 500 on someone else's request.

    Every guard below is a way this can cost money it shouldn't, checked in cheapest-first order:

      consent   — no `autotopup_consented_at` means no mandate; charging anyway is an unauthorized
                  charge, so this refuses even if the enabled flag somehow says yes.
      cooldown  — `autotopup_last_attempt_at` is stamped in the DB *before* the Stripe call, so a
                  second web worker (or a redeployed instance) sees it. This is the cross-instance
                  half of the lock.
      cap       — the month's automatic spend, from the ledger, plus this charge, against the cap.
      attempts  — consecutive failures; hitting the limit disables rather than retrying forever.
      threshold — re-read under the lock, because the balance may have been topped up by the time we
                  got here (a manual top-up, or the winner of a race).
    """
    if not configured():
        return {"ok": False, "reason": "not_configured"}
    async with _lock(org_id):
        org = await db.get(Org, org_id)
        if org is None:
            return {"ok": False, "reason": "no_org"}
        if not org.autotopup_enabled:
            return {"ok": False, "reason": "disabled"}
        if not org.autotopup_consented_at:
            return {"ok": False, "reason": "no_consent"}
        if not (org.stripe_customer_id and org.stripe_default_pm):
            return {"ok": False, "reason": "no_card"}

        prefs = autotopup_prefs(org)
        s = get_settings()
        if org.autotopup_failures >= s.autotopup_max_attempts:
            await _disable_autotopup(db, org, "max_attempts")
            return {"ok": False, "reason": "max_attempts"}
        last = org.autotopup_last_attempt_at
        if last is not None and (_now() - last).total_seconds() < s.autotopup_cooldown_s:
            return {"ok": False, "reason": "cooldown"}

        balance = await ledger.balance_of(db, org_id)
        if balance >= prefs["threshold_micro"]:
            return {"ok": False, "reason": "above_threshold", "balance_micro": balance}

        amount_micro = prefs["amount_micro"]
        spent = await monthly_autotopup_spend(db, org_id)
        if spent + amount_micro > prefs["monthly_cap_micro"]:
            return {"ok": False, "reason": "monthly_cap", "spent_micro": spent,
                    "cap_micro": prefs["monthly_cap_micro"]}

        # Claim the attempt BEFORE talking to Stripe. If this process dies mid-charge, the cooldown
        # has already been recorded — the next attempt waits instead of double-charging blind.
        org.autotopup_last_attempt_at = _now()
        db.add(org)
        await db.commit()

        crossing = spent // max(1, amount_micro)
        to = await owner_email(db, org_id)
        try:
            pi = await _sdk(
                stripe.PaymentIntent.create,
                amount=micro_to_cents(amount_micro),
                currency="usd",                    # explicit: the account's own default is AUD
                customer=org.stripe_customer_id,
                payment_method=org.stripe_default_pm,
                off_session=True,                  # nobody is at the keyboard — declare it
                confirm=True,
                metadata={"treg_org_id": str(org_id), "treg_kind": "topup", "treg_auto": "1"},
                **({"receipt_email": to} if to else {}),
                idempotency_key=_idempotency_key(org_id, amount_micro, crossing, org.stripe_default_pm),
            )
        except stripe.CardError as e:
            code = (getattr(e, "code", "") or (e.error.code if getattr(e, "error", None) else "") or "")
            # The declined PaymentIntent hangs off the error as a StripeObject (not a dict), so reach
            # for it defensively — a missing id must not turn a decline into a crash.
            pi_id = ""
            err = getattr(e, "error", None)
            declined = getattr(err, "payment_intent", None) if err is not None else None
            if declined is not None:
                pi_id = str(declined["id"]) if "id" in declined else ""
            if code == "authentication_required":
                # Retrying off-session cannot fix 3DS — the cardholder has to be present. Keep the
                # PaymentIntent so the dashboard can offer "finish this payment" instead of starting a
                # fresh charge the bank will decline the same way.
                await _disable_autotopup(db, org, "authentication_required", recovery_pi=pi_id)
                return {"ok": False, "reason": "authentication_required", "payment_intent": pi_id}
            return await _record_failure(db, org_id, code or "card_declined")
        except Exception as e:  # noqa: BLE001 — a Stripe outage must not surface as a 500 elsewhere
            log.warning("billing: auto top-up for org %s failed: %s", org_id, e)
            return await _record_failure(db, org_id, "stripe_error")

        if pi.get("status") != "succeeded":
            # `requires_action` off-session is 3DS by another name; anything else pending is not money
            # we can credit. Either way the balance stays as it was.
            return await _record_failure(db, org_id, f"status_{pi.get('status')}")

        # Credit here rather than waiting for the webhook: unlike a browser redirect, this succeeded
        # status came back on OUR request to Stripe, so it is server-authoritative. The webhook's
        # later delivery of the same PaymentIntent id is a no-op (ledger.topup is idempotent on it).
        credited = cents_to_micro(pi.get("amount_received") or pi.get("amount") or 0)
        await ledger.topup(db, org_id, credited, pi["id"], meta={"auto": True, "source": "stripe"})
        org = await db.get(Org, org_id)
        org.autotopup_failures = 0
        org.autotopup_disabled_reason = None
        db.add(org)
        await db.commit()
        if to:
            await email_mod.send_topup_receipt(
                to, org.name or org.slug, credited, await ledger.balance_of(db, org_id), auto=True)
        return {"ok": True, "payment_intent": pi["id"], "amount_micro": credited}


async def _record_failure(db: AsyncSession, org_id: int, reason: str) -> dict:
    """Count a decline and disable auto-top-up once they stop looking like bad luck. Consecutive, not
    cumulative: one success clears the count, because a card that works now is not a card with a
    history problem."""
    org = await db.get(Org, org_id)
    if org is None:
        return {"ok": False, "reason": reason}
    org.autotopup_failures = int(org.autotopup_failures or 0) + 1
    db.add(org)
    await db.commit()
    if org.autotopup_failures >= get_settings().autotopup_max_attempts:
        await _disable_autotopup(db, org, f"max_attempts:{reason}")
        return {"ok": False, "reason": "max_attempts", "last_error": reason}
    return {"ok": False, "reason": reason, "failures": org.autotopup_failures}


# In-flight org ids, so a hot call path that fires this on every request doesn't pile up a task per
# call. The lock inside `attempt_auto_topup` would serialize them anyway — this stops them existing.
_scheduled: set[int] = set()


def maybe_schedule_autotopup(org: Org) -> bool:
    """FIRE AND FORGET a refill check for this org. Returns whether a task was actually started.

    Deliberately synchronous and non-awaitable: the caller is on a request path (phase 3 calls it
    right after `reserve`), and a Stripe round-trip is hundreds of milliseconds that must never land
    in an agent's call latency. The cheap in-memory checks happen here so the common case — auto-top-up
    off, or balance healthy — costs a dict lookup and no task at all.
    """
    if not (configured() and org and org.id and org.autotopup_enabled and org.autotopup_consented_at):
        return False
    if not (org.stripe_customer_id and org.stripe_default_pm):
        return False
    if org.balance_micro >= autotopup_prefs(org)["threshold_micro"]:
        return False
    org_id = int(org.id)
    if org_id in _scheduled:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # no event loop (a sync CLI/test context) — nothing to schedule onto
    _scheduled.add(org_id)
    loop.create_task(_run_autotopup(org_id))
    return True


async def _run_autotopup(org_id: int) -> None:
    """The scheduled task's body: its OWN session, because the request that scheduled it will have
    closed its session (and committed or rolled back) long before this runs."""
    from .db import session_maker
    try:
        async with session_maker() as db:
            result = await attempt_auto_topup(db, org_id)
        if not result.get("ok") and result.get("reason") not in (
                "above_threshold", "cooldown", "disabled", "no_consent", "no_card", "not_configured"):
            log.info("billing: auto top-up for org %s did not fund: %s", org_id, result)
    except Exception as e:  # noqa: BLE001 — a background task's exception must not vanish silently
        log.warning("billing: auto top-up task for org %s crashed: %s", org_id, e)
    finally:
        _scheduled.discard(org_id)


# ---- webhook ------------------------------------------------------------------------------------
def verify_event(payload: bytes, signature: str) -> dict:
    """Verify a Stripe webhook signature and return the parsed event. Raises `ValueError` on a bad
    signature or payload.

    Uses the SDK's `verify_header` rather than our own HMAC (the demo feed's `pubfeed.verify_signature`
    does it by hand): it is the code Stripe maintains, and it already handles the timestamp tolerance
    that stops a captured delivery being replayed, plus the several-signatures case during secret
    rotation.

    Deliberately NOT `construct_event`, which additionally deserializes into the SDK's typed Event
    model and raises on an envelope it doesn't recognize. Authenticity and shape are separate
    questions: a genuine event of a type this SDK version predates must still be accepted (and then
    ignored by the dispatcher), not rejected as forged.
    """
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise ValueError("no webhook secret configured")
    try:
        # `verify_header` signs a STRING (`f"{t}.{payload}"`), so bytes must be decoded first or the
        # HMAC is computed over the literal `b'…'` repr and every genuine event fails to verify.
        body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        stripe.WebhookSignature.verify_header(body, signature, secret, stripe.Webhook.DEFAULT_TOLERANCE)
        return json.loads(body)
    except Exception as e:  # SignatureVerificationError, ValueError, … — all mean "don't trust this"
        raise ValueError(f"bad signature or payload: {e}") from e


def _org_id_from(obj: dict) -> int | None:
    meta = obj.get("metadata") or {}
    raw = meta.get("treg_org_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def handle_webhook_event(db: AsyncSession, event: dict) -> dict:
    """Dispatch ONE verified Stripe event. Returns a small dict for the response and the logs.

    Everything here must be safe to run twice: Stripe delivers at least once, retries for days, and
    the CLI's `stripe events resend` exists. Idempotency comes from `ledger.topup` keying on the
    PaymentIntent id, which is why both `checkout.session.completed` and `payment_intent.succeeded`
    can credit the same purchase without doubling it — whichever arrives first does the work.
    """
    kind = event.get("type") or ""
    obj = ((event.get("data") or {}).get("object") or {})
    if kind == "checkout.session.completed":
        return await _on_checkout_completed(db, obj)
    if kind == "payment_intent.succeeded":
        return await _on_payment_succeeded(db, obj)
    if kind == "payment_intent.payment_failed":
        return await _on_payment_failed(db, obj)
    if kind == "setup_intent.succeeded":
        return await _on_setup_succeeded(db, obj)
    # Everything else acknowledged and dropped — deliberately, not by omission. `invoice_creation` on
    # the top-up Checkout makes Stripe emit `invoice.created` / `invoice.paid` for every purchase, and
    # crediting on those too would be a second door onto the same money. The invoice is a document; the
    # PaymentIntent is the payment.
    return {"handled": False, "type": kind}


async def _credit(db: AsyncSession, org_id: int, amount_micro: int, pi_id: str, *, auto: bool) -> dict:
    """Credit a paid PaymentIntent to the org's balance, once. Emails a receipt only when this
    delivery is the one that actually moved money — a redelivery must not re-notify."""
    # "Has this PaymentIntent already been credited?" is asked BEFORE the credit rather than inferred
    # from a balance change afterwards — a concurrent reserve moves the balance too, and mistaking that
    # for a fresh credit would email a receipt on every webhook redelivery.
    already = (await db.execute(
        select(CreditBlock.id).where(CreditBlock.stripe_payment_intent == pi_id)
    )).first() is not None
    block = await ledger.topup(db, org_id, amount_micro, pi_id,
                              meta={"auto": auto, "source": "stripe"})
    after = await ledger.balance_of(db, org_id)
    fresh = not already
    if fresh:
        org = await db.get(Org, org_id)
        if org is not None and (org.autotopup_failures or org.autotopup_disabled_reason):
            # A payment that went through clears the failure history: whatever was wrong with the card
            # isn't wrong now. (The disabled FLAG is left alone — re-arming is the org's decision.)
            org.autotopup_failures = 0
            db.add(org)
            await db.commit()
        to = await owner_email(db, org_id)
        if to:
            await email_mod.send_topup_receipt(to, (org.name or org.slug) if org else "", amount_micro,
                                              after, auto=auto)
        # Rides `fresh` like the receipt above, so a webhook redelivery re-emits nothing. The webhook
        # is org-scoped, so the owner's email is the best available person (the payer may differ);
        # the `team` group is what makes org-level revenue exact. capture() never raises — an
        # exception here would 500 the webhook and make Stripe retry an already-credited payment.
        analytics.capture(to or f"org:{org_id}", "topup_completed",
                          {"amount_micro": amount_micro,
                           "amount_usd": amount_micro / 1_000_000,  # display-only, never computed against
                           "auto": auto, "balance_after_micro": after,
                           "org": org.slug if org else ""},
                          groups={"team": org.slug if org else ""})
    return {"handled": True, "credited": fresh, "block_id": block.id,
            "amount_micro": amount_micro, "balance_micro": after}


async def _on_checkout_completed(db: AsyncSession, session: dict) -> dict:
    """A hosted Checkout finished. For `mode=payment` this is the manual top-up landing; for
    `mode=setup` it is a card being saved for auto-top-up."""
    org_id = _org_id_from(session)
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    if session.get("mode") == "setup":
        si = session.get("setup_intent")
        si_id = si if isinstance(si, str) else (si or {}).get("id")
        if si_id:
            try:
                si_obj = await _sdk(stripe.SetupIntent.retrieve, id=si_id)
            except Exception as e:  # noqa: BLE001
                log.warning("billing: could not retrieve SetupIntent %s: %s", si_id, e)
                return {"handled": False, "reason": "setup_intent unreadable"}
            return await _on_setup_succeeded(db, si_obj)
        return {"handled": False, "reason": "no setup_intent on session"}
    if session.get("payment_status") != "paid":
        return {"handled": False, "reason": f"payment_status={session.get('payment_status')}"}
    pi = session.get("payment_intent")
    pi_id = pi if isinstance(pi, str) else (pi or {}).get("id")
    if not pi_id:
        return {"handled": False, "reason": "no payment_intent on session"}
    # `amount_total` is what Stripe actually collected, in cents. We do NOT trust the session's
    # metadata for the amount — the charged number is the only one allowed to become balance.
    amount_micro = cents_to_micro(session.get("amount_total") or 0)
    if amount_micro <= 0:
        return {"handled": False, "reason": "zero amount"}
    result = await _credit(db, org_id, amount_micro, pi_id, auto=False)
    # The Checkout saved the card (setup_future_usage); remember it so auto-top-up can be armed
    # without asking for a second card entry.
    await _set_default_pm(db, org_id, await _pm_of_payment_intent(pi_id))
    return result


async def _on_payment_succeeded(db: AsyncSession, pi: dict) -> dict:
    """A PaymentIntent succeeded. Fires for the manual Checkout purchase AND the off-session
    auto-charge; `treg_kind` on the metadata is what tells our top-ups apart from anything else the
    account does (the landing-page demo charges, for one)."""
    meta = pi.get("metadata") or {}
    if meta.get("treg_kind") != "topup":
        return {"handled": False, "reason": "not a treg top-up"}
    org_id = _org_id_from(pi)
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    amount_micro = cents_to_micro(pi.get("amount_received") or pi.get("amount") or 0)
    if amount_micro <= 0:
        return {"handled": False, "reason": "zero amount"}
    result = await _credit(db, org_id, amount_micro, pi["id"], auto=meta.get("treg_auto") == "1")
    pm = pi.get("payment_method")
    await _set_default_pm(db, org_id, pm if isinstance(pm, str) else (pm or {}).get("id"))
    return result


async def _on_payment_failed(db: AsyncSession, pi: dict) -> dict:
    """A top-up PaymentIntent failed. Only the AUTOMATIC ones need bookkeeping: a human who saw a
    decline on the hosted page can just try again, but an unattended charge that keeps failing has to
    stop by itself."""
    meta = pi.get("metadata") or {}
    if meta.get("treg_kind") != "topup":
        return {"handled": False, "reason": "not a treg top-up"}
    org_id = _org_id_from(pi)
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    if meta.get("treg_auto") != "1":
        return {"handled": True, "auto": False}  # manual decline — nothing to disable
    code = ((pi.get("last_payment_error") or {}).get("code")) or "card_declined"
    if code == "authentication_required":
        org = await db.get(Org, org_id)
        if org is not None:
            await _disable_autotopup(db, org, "authentication_required", recovery_pi=pi.get("id") or "")
        return {"handled": True, "disabled": "authentication_required"}
    return {"handled": True, **await _record_failure(db, org_id, code)}


async def _on_setup_succeeded(db: AsyncSession, si: dict) -> dict:
    """A card was saved (SetupIntent). Store the PaymentMethod id so auto-top-up has something to
    charge, and clear a `no_card`-shaped disable — the reason it was off is now gone."""
    org_id = _org_id_from(si)
    if org_id is None:
        return {"handled": False, "reason": "no org in metadata"}
    pm = si.get("payment_method")
    pm_id = pm if isinstance(pm, str) else (pm or {}).get("id")
    changed = await _set_default_pm(db, org_id, pm_id)
    org = await db.get(Org, org_id)
    if org is not None and org.autotopup_consented_at and not org.autotopup_enabled \
            and org.autotopup_disabled_reason in (None, "no_card"):
        # The org already consented and was only waiting on a card — arming it now is what they asked
        # for. A disable for any OTHER reason (a decline, 3DS) stays off until a human re-enables it.
        org.autotopup_enabled = True
        org.autotopup_disabled_reason = None
        org.autotopup_failures = 0
        db.add(org)
        await db.commit()
    return {"handled": True, "payment_method_saved": bool(pm_id), "changed": changed}


# ---- read model for the API --------------------------------------------------------------------
async def billing_state(db: AsyncSession, org: Org) -> dict:
    """Everything the dashboard/CLI needs to render billing in one round-trip: is Stripe on, is there
    a customer and a card, what the auto-top-up policy is, and how much of the monthly cap the
    automatic charges have used so far."""
    prefs = autotopup_prefs(org)
    s = get_settings()
    spent = await monthly_autotopup_spend(db, org.id) if org.id else 0
    return {
        "org_id": org.id,
        "configured": configured(),
        "balance_micro": int(org.balance_micro or 0),
        "balance_usd": ledger.usd(int(org.balance_micro or 0)),
        "customer": bool(org.stripe_customer_id),
        "card_on_file": bool(org.stripe_default_pm),
        # Whether the hosted portal can be opened at all. It hangs off the Stripe customer, which only
        # exists once someone has paid — so the button stays hidden rather than 422-ing a new team.
        "portal": configured() and bool(org.stripe_customer_id),
        "topup": {"min_usd": s.topup_min_usd, "default_usd": s.topup_default_usd,
                  "presets": list(s.topup_presets)},
        "autotopup": {
            "enabled": bool(org.autotopup_enabled),
            "consented_at": org.autotopup_consented_at.isoformat() if org.autotopup_consented_at else None,
            "threshold_micro": prefs["threshold_micro"],
            "threshold_usd": ledger.usd(prefs["threshold_micro"]),
            "amount_micro": prefs["amount_micro"],
            "amount_usd": ledger.usd(prefs["amount_micro"]),
            "monthly_cap_micro": prefs["monthly_cap_micro"],
            "monthly_cap_usd": ledger.usd(prefs["monthly_cap_micro"]),
            "month_spend_micro": spent,
            "month_spend_usd": ledger.usd(spent),
            "disabled_reason": org.autotopup_disabled_reason,
            "failures": int(org.autotopup_failures or 0),
            "last_attempt_at": org.autotopup_last_attempt_at.isoformat() if org.autotopup_last_attempt_at else None,
            "recovery_payment_intent": org.autotopup_recovery_pi,
            "cooldown_s": s.autotopup_cooldown_s,
            "max_attempts": s.autotopup_max_attempts,
        },
    }


async def set_autotopup(
    db: AsyncSession, org: Org, *, enabled: bool, consent: bool,
    threshold_usd=None, amount_usd=None, monthly_cap_usd=None,
) -> None:
    """Store the org's auto-top-up preferences and its consent timestamp. Commits.

    Enabling REQUIRES `consent` on this very request when there is no mandate on file: the timestamp
    has to correspond to a specific human agreeing to a specific threshold and amount, so it is
    stamped here, next to the numbers it agreed to — not inferred later from the fact that the feature
    is on.
    """
    if threshold_usd is not None:
        org.autotopup_threshold_micro = usd_to_micro(validate_topup_usd(threshold_usd))
    if amount_usd is not None:
        org.autotopup_amount_micro = usd_to_micro(validate_topup_usd(amount_usd))
    if monthly_cap_usd is not None:
        org.autotopup_monthly_cap_micro = usd_to_micro(validate_topup_usd(monthly_cap_usd))
    if enabled:
        if consent:
            org.autotopup_consented_at = _now()
        org.autotopup_enabled = bool(org.stripe_default_pm)  # armed only once there's a card to charge
        org.autotopup_disabled_reason = None if org.stripe_default_pm else "no_card"
        org.autotopup_failures = 0
    else:
        org.autotopup_enabled = False
        org.autotopup_disabled_reason = None  # a deliberate off is not a failure — clear the banner
    db.add(org)
    await db.commit()
