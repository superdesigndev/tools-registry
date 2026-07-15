"""DB-backed ephemeral state + rate limiter (treg.ratestore) — backlog #3.

The point of moving OTP codes + the auth throttles into the DB is that they (a) survive a restart and
(b) are shared across instances. We prove (a)/(b) here by writing with one session and reading with a
SEPARATE session — if the state were still a per-process dict, the second session would see nothing.
"""

from __future__ import annotations

from treg import ratestore
from treg.db import reset_db, session_maker
from treg.models import Ephemeral


async def _fresh_db():
    await reset_db()


async def test_kv_put_get_pop_roundtrip():
    await _fresh_db()
    async with session_maker() as db:
        await ratestore.kv_put(db, "otp", "a@b.io", {"hash": "H", "attempts": 5}, ttl_s=600)
        await db.commit()
    # a SEPARATE session sees it → it is in the DB, not process memory
    async with session_maker() as db:
        v = await ratestore.kv_get(db, "otp", "a@b.io")
        assert v == {"hash": "H", "attempts": 5}
        popped = await ratestore.kv_pop(db, "otp", "a@b.io")
        await db.commit()
        assert popped == {"hash": "H", "attempts": 5}
    async with session_maker() as db:
        assert await ratestore.kv_get(db, "otp", "a@b.io") is None  # gone after pop


async def test_kv_get_treats_expired_as_missing():
    await _fresh_db()
    async with session_maker() as db:
        await ratestore.kv_put(db, "otp", "old@b.io", {"hash": "H", "attempts": 5}, ttl_s=-1)  # already expired
        await db.commit()
    async with session_maker() as db:
        assert await ratestore.kv_get(db, "otp", "old@b.io") is None


async def test_kv_put_ttl_none_keeps_expiry():
    await _fresh_db()
    async with session_maker() as db:
        await ratestore.kv_put(db, "otp", "e@b.io", {"attempts": 5}, ttl_s=600)
        await db.commit()
        row1 = await db.get(Ephemeral, ("otp", "e@b.io"))
        exp1 = row1.expires_at
        # update the payload only — expiry must not move (the wrong-guess path must not extend the code)
        await ratestore.kv_put(db, "otp", "e@b.io", {"attempts": 4}, ttl_s=None)
        await db.commit()
        row2 = await db.get(Ephemeral, ("otp", "e@b.io"))
        assert row2.v == {"attempts": 4} and row2.expires_at == exp1


async def test_rate_check_sliding_window_all_or_nothing():
    await _fresh_db()
    async with session_maker() as db:
        # cap of 3 for the key; the 4th within the window is refused
        for i in range(3):
            assert await ratestore.rate_check(db, "otp_start", [("e:x", 3)], window_s=900) is True
            await db.commit()
        assert await ratestore.rate_check(db, "otp_start", [("e:x", 3)], window_s=900) is False
        await db.commit()
    # a DIFFERENT key is independent (per-email, not global)
    async with session_maker() as db:
        assert await ratestore.rate_check(db, "otp_start", [("e:y", 3)], window_s=900) is True


async def test_rate_check_is_all_keys_or_none():
    await _fresh_db()
    async with session_maker() as db:
        # exhaust one of two keys, then a request naming both must be refused AND must not record a hit
        # under the still-open key (all-or-nothing)
        for _ in range(2):
            assert await ratestore.rate_check(db, "otp_start", [("e:z", 2)], window_s=900) is True
            await db.commit()
        assert await ratestore.rate_check(db, "otp_start", [("e:z", 2), ("i:1.2.3.4", 30)], window_s=900) is False
        await db.commit()
        # the IP key recorded nothing on the rejected request
        row = await db.get(Ephemeral, ("otp_start", "i:1.2.3.4"))
        assert row is None


async def test_sweep_drops_expired_rows_only():
    await _fresh_db()
    async with session_maker() as db:
        await ratestore.kv_put(db, "sandbox_hit", "live", {"hits": [1e12]}, ttl_s=3600)
        await ratestore.kv_put(db, "sandbox_hit", "dead", {"hits": [1]}, ttl_s=-1)
        await db.commit()
        await ratestore.sweep(db, "sandbox_hit")
        await db.commit()
        assert await db.get(Ephemeral, ("sandbox_hit", "dead")) is None
        assert await db.get(Ephemeral, ("sandbox_hit", "live")) is not None
