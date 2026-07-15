"""Operational fixes — dev-code locked out of prod, and audit writes capped but reliable."""
from __future__ import annotations

from httpx import AsyncClient
from sqlmodel import select


def test_dev_code_exposed_only_on_local_sqlite():
    from treg.config import Settings
    assert Settings(email_dev_mode=True, database_url="sqlite+aiosqlite:///./x.db").expose_dev_code is True
    # a real (Postgres) deploy NEVER exposes the code, even if the flag is on by mistake
    assert Settings(email_dev_mode=True, database_url="postgresql+asyncpg://u@h/db").expose_dev_code is False
    assert Settings(email_dev_mode=False, database_url="sqlite+aiosqlite:///./x.db").expose_dev_code is False


async def test_audit_writes_are_capped_but_all_recorded(clients: AsyncClient):
    from treg import audit
    from treg.db import session_maker
    from treg.models import CallRecord
    assert audit._MAX_CONCURRENT_WRITES <= 8   # bounded — can't grab the whole DB pool
    for _ in range(25):  # a burst: far more than the concurrent cap
        audit.record_call(org_id=1, user_email="a@b.c", tool_name="captest",
                          method="GET", path="/x", status_code=200)
    await audit.drain()
    async with session_maker() as s:
        rows = (await s.execute(select(CallRecord).where(CallRecord.tool_name == "captest"))).scalars().all()
    assert len(rows) == 25   # every row still landed, despite the concurrency cap
