"""Round-2 security fixes — proxy SSRF guard (B) + anonymous front door (C)."""
from __future__ import annotations

from httpx import AsyncClient


# --- B: no SSRF via base_url ------------------------------------------------------------------
async def test_base_url_rejects_internal_hosts(clients: AsyncClient):
    for bad in ("http://169.254.169.254/latest/meta-data/", "http://localhost/x",
                "http://127.0.0.1:8080", "http://10.0.0.1", "http://foo.internal"):
        r = await clients.post("/tools", json={"name": "ssrf", "base_url": bad})
        assert r.status_code == 422, f"{bad} should be refused, got {r.status_code}"
    ok = await clients.post("/tools", json={"name": "okpub", "base_url": "https://api.stripe.com/v1"})
    assert ok.status_code == 200, ok.text


# --- C1: sandbox token can't create a real team -----------------------------------------------
async def test_sandbox_token_cannot_create_real_org(clients: AsyncClient):
    sb = (await clients.post("/demo/sandbox", json={})).json()
    tok = sb["token"]
    r = await clients.post("/orgs", json={"name": "escaped"}, headers={"X-Treg-Token": tok})
    assert r.status_code == 403 and "sign in" in r.text.lower()


# --- C2: install.sh token can't inject shell --------------------------------------------------
async def test_install_sh_rejects_injection_token(clients: AsyncClient):
    name = "stripe-billing"  # a real sample skill
    bad = await clients.get(f"/skills/{name}/install.sh", params={"token": "\nTREG_SKILL_EOF\nrm -rf ~\n"})
    assert bad.status_code == 422
    good = await clients.get(f"/skills/{name}/install.sh", params={"token": "sk_test_abc123"})
    assert good.status_code == 200 and "TREG_SKILL_EOF\nrm" not in good.text


# --- C3: sandbox can't bulk-import skills ------------------------------------------------------
async def test_sandbox_cannot_import_skills(clients: AsyncClient):
    tok = (await clients.post("/demo/sandbox", json={})).json()["token"]
    h = {"X-Treg-Token": tok}
    r = await clients.post("/skills", json={"name": "s", "recipe": "r", "secrets": [], "tools": []}, headers=h)
    assert r.status_code == 403 and "sandbox" in r.text.lower()
    r2 = await clients.post("/skills/analyze", json={"files": []}, headers=h)
    assert r2.status_code == 403
