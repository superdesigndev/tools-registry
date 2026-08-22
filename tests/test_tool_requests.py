"""Tool requests — the "the catalog doesn't have X" report (`POST /tool-requests`).

The contract under test: anyone may file (no auth — the filer is usually an agent that just got
zero search results and holds no token), per-IP rate limiting is the abuse valve, fields are
capped, and identity — when the caller happens to have one — is attribution on the stored row,
never a requirement. Plus the discovery half: the zero-result search surfaces file-a-request as
its hint, in the API and over MCP.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import audit
from treg.api import TOOLREQ_RATE_MAX, app
from treg.db import session_maker
from treg.models import SearchMiss, ToolRequest


async def _fetch_rows() -> list[ToolRequest]:
    async with session_maker() as s:
        return list((await s.execute(select(ToolRequest))).scalars())


async def test_anyone_can_file_without_a_token(clients: AsyncClient):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as anon:
        r = await anon.post("/tool-requests", json={
            "capability": "Ahrefs backlinks", "query": "ahrefs backlinks", "source": "cli"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "received" and r.json()["id"]

    (row,) = await _fetch_rows()
    assert row.capability == "Ahrefs backlinks"
    assert row.query == "ahrefs backlinks"
    assert row.source == "cli"
    assert row.status == "open"
    assert row.org_id is None and row.user_email == ""  # anonymous stays anonymous


async def test_a_token_becomes_attribution_on_the_row(clients: AsyncClient):
    """The default `clients` fixture carries a per-org token — the row should say who asked."""
    r = await clients.post("/tool-requests", json={"capability": "flight prices"})
    assert r.status_code == 200, r.text
    (row,) = await _fetch_rows()
    assert row.user_email == "tim@superdesign.dev"
    assert row.org_id is not None


async def test_blank_capability_is_refused(clients: AsyncClient):
    r = await clients.post("/tool-requests", json={"capability": "   "})
    assert r.status_code == 422
    assert await _fetch_rows() == []


async def test_a_novel_capability_is_refused_when_too_long(clients: AsyncClient):
    r = await clients.post("/tool-requests", json={"capability": "x" * 201})
    assert r.status_code == 422
    assert await _fetch_rows() == []


async def test_free_text_fields_are_capped_not_refused(clients: AsyncClient):
    """The capability is the headline and must be short; the rest is best-effort context — clip it
    rather than bounce a filing over an over-long note."""
    r = await clients.post("/tool-requests", json={
        "capability": "HN comments", "note": "n" * 5000, "contact": "c" * 500,
        "query": "q" * 500, "source": "definitely-not-a-source"})
    assert r.status_code == 200, r.text
    (row,) = await _fetch_rows()
    assert len(row.note) == 2000 and len(row.contact) == 200 and len(row.query) == 300
    assert row.source == "api"  # an unknown source label collapses to "api", not garbage


async def test_filings_are_rate_limited_per_ip(clients: AsyncClient):
    for i in range(TOOLREQ_RATE_MAX):
        r = await clients.post("/tool-requests", json={"capability": f"thing {i}"})
        assert r.status_code == 200, r.text
    r = await clients.post("/tool-requests", json={"capability": "one too many"})
    assert r.status_code == 429
    assert len(await _fetch_rows()) == TOOLREQ_RATE_MAX


async def test_the_empty_search_names_the_way_to_file_one(clients: AsyncClient):
    """Discovery: an agent that searched and found nothing is the exact caller we want a filing
    from — the zero-result response has to say so, or the feature might as well not exist."""
    r = await clients.get("/catalog/search", params={"q": "zzzz-no-such-capability"})
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    assert any("tool-requests" in h for h in body["hints"]), body["hints"]


async def _fetch_misses() -> list[SearchMiss]:
    async with session_maker() as s:
        return list((await s.execute(select(SearchMiss))).scalars())


async def test_the_empty_search_is_logged_as_a_miss(clients: AsyncClient):
    """The other half of discovery: most agents that miss never file, so the miss itself is the
    record (models.SearchMiss). Written fire-and-forget — drain() flushes it for the assert."""
    r = await clients.get("/catalog/search", params={"q": "zzzz-no-such-capability"})
    assert r.status_code == 200 and r.json()["results"] == []
    await audit.drain()
    (row,) = await _fetch_misses()
    assert row.query == "zzzz-no-such-capability"
    assert row.source == "api"


async def test_a_search_that_matches_logs_no_miss(clients: AsyncClient):
    r = await clients.get("/catalog/search", params={"q": "backlinks"})
    assert r.status_code == 200 and r.json()["results"]
    await audit.drain()
    assert await _fetch_misses() == []


async def test_a_blank_search_logs_no_miss(clients: AsyncClient):
    """No q is a browse, not a failed ask — logging it would drown the signal in noise."""
    r = await clients.get("/catalog/search")
    assert r.status_code == 200
    await audit.drain()
    assert await _fetch_misses() == []
