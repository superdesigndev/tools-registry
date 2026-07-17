"""The landing page's live payments feed (the public Stripe demo).

A visitor curls the published demo token → treg relays a test charge to Stripe → Stripe fires
`charge.succeeded` at our webhook → the charge appears on the landing page over SSE, no refresh.

Deliberately tiny and in-memory: the feed is a marketing surface, not a system of record. A
dropped event on restart costs nothing (Stripe retries webhooks anyway), and multi-instance
deploys just mean each instance streams the events its own webhook delivery landed on.

Only server-chosen fields travel (amount/currency/created/id-suffix) — NEVER `description` or
any other visitor-controlled string, so the feed cannot be defaced (see landing-sandbox.md's
graffiti lesson).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections import deque
from collections.abc import AsyncIterator

FEED_MAX = 20               # replayed to a fresh subscriber
KEEPALIVE_S = 25            # SSE comment ping so proxies don't reap the idle stream
SIG_TOLERANCE_S = 300       # reject webhook timestamps older than this (replay guard)
_MAX_SUBSCRIBER_LAG = 100   # a stalled subscriber is dropped, not buffered forever

_events: deque[dict] = deque(maxlen=FEED_MAX)
_subscribers: set[asyncio.Queue] = set()

# The visitor "name" on each charge. The landing page bakes a wordlist name into the copyable
# command's `description`; since a visitor can edit the command freely, a description is shown
# ONLY when both words come from these exact lists (numbers ≤999). Anything else falls back to a
# name DERIVED from the charge id — so every row gets a friendly identity and hand-typed text can
# never reach the page. Keep these lists in sync with LIVE_ADJ/LIVE_ANIMAL in web/landing.html.
ADJECTIVES = (
    "swift", "brave", "calm", "clever", "cosmic", "daring", "eager", "fuzzy",
    "gentle", "golden", "happy", "jolly", "lucky", "mellow", "mighty", "neon",
    "nifty", "plucky", "proud", "quick", "shiny", "snappy", "solar", "sunny",
)
ANIMALS = (
    "otter", "fox", "lynx", "panda", "koala", "falcon", "heron", "badger",
    "dolphin", "gecko", "ibis", "jaguar", "kiwi", "lemur", "marmot", "narwhal",
    "ocelot", "puffin", "quokka", "raven", "seal", "tapir", "walrus", "wombat",
)


def _derived_name(charge_id: str) -> str:
    """A deterministic wordlist name from the charge id — the safe fallback for any charge whose
    description wasn't (or was tampered to not be) one of ours."""
    h = int(hashlib.sha256(charge_id.encode()).hexdigest(), 16)
    return f"{ADJECTIVES[h % len(ADJECTIVES)]}-{ANIMALS[(h // 100) % len(ANIMALS)]}-{h % 1000}"


def _is_wordlist_name(s) -> bool:
    if not isinstance(s, str):
        return False
    parts = s.split("-")
    return (len(parts) == 3 and parts[0] in ADJECTIVES and parts[1] in ANIMALS
            and parts[2].isdigit() and len(parts[2]) <= 3)


def _display_name(obj: dict) -> str:
    # metadata[visitor] is stamped by OUR relay (sandbox live wire) over whatever the caller sent,
    # so it's first choice; description covers page-generated commands. Both still pass the
    # wordlist gate — defense in depth against any path that lets caller text through.
    meta = obj.get("metadata") or {}
    for cand in (meta.get("visitor") if isinstance(meta, dict) else None, obj.get("description")):
        if _is_wordlist_name(cand):
            return cand
    return _derived_name(str(obj.get("id", "")))


def verify_signature(payload: bytes, header: str, secret: str) -> bool:
    """Stripe-Signature: `t=<unix>,v1=<hexhmac>[,v1=…]`. The signed payload is `{t}.{body}`.
    Constant-time compare; a stale timestamp fails (replayed capture)."""
    pairs = [kv.split("=", 1) for kv in header.split(",") if "=" in kv]
    t = next((v for k, v in pairs if k == "t"), "")
    if not t.isdigit() or abs(time.time() - int(t)) > SIG_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    # Stripe may send several v1 signatures during secret rotation — any match passes.
    candidates = [v for k, v in pairs if k == "v1"]
    return any(hmac.compare_digest(expected, c) for c in candidates)


def push_charge(obj: dict) -> None:
    """Record a charge for the feed — server-chosen fields only, never visitor-typed text.
    The receipt link is the skeptic-proof: a page rendered by pay.stripe.com, not by us."""
    receipt = obj.get("receipt_url")
    event = {
        "amount": obj.get("amount"),
        "currency": obj.get("currency"),
        "created": obj.get("created"),
        "id_suffix": str(obj.get("id", ""))[-6:],  # enough for "that's MY charge", useless otherwise
        "name": _display_name(obj),
        "receipt_url": receipt if isinstance(receipt, str) and receipt.startswith("https://pay.stripe.com/") else None,
    }
    _events.append(event)
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # a stalled consumer — drop it rather than buffer unboundedly
            _subscribers.discard(q)


async def stream() -> AsyncIterator[str]:
    """SSE generator: replay the ring buffer, then live events, with keepalive pings."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_SUBSCRIBER_LAG)
    _subscribers.add(q)
    try:
        for event in list(_events):
            yield f"data: {json.dumps(event)}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    finally:
        _subscribers.discard(q)


def reset() -> None:
    """Test hook: forget all events and subscribers."""
    _events.clear()
    _subscribers.clear()
