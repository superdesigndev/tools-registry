

# ---- deleting a team must clear everything that points at it -------------------------------

async def test_org_delete_clears_EVERY_org_scoped_table(clients):
    """The list in `_cascade_delete_org` has to stay in step with the schema, and twice it did not:
    the money tables arrived with the prepaid balance and `CapabilityPin` with capability pins, and
    neither was added. The effect was invisible until someone tried it — and because every NEW team
    is granted $1.00, every team has a CreditBlock, so NO team could be deleted at all. Production
    returned a bare 500.

    So this walks the models module rather than restating the list: any future model carrying an
    `org_id` fails here until it is handled."""
    import inspect

    from sqlmodel import SQLModel

    from treg import models as m
    from treg.api import _ORG_SCOPED_MODELS

    covered = {model.__name__ for model in _ORG_SCOPED_MODELS}
    missing = []
    for name, obj in vars(m).items():
        if not (inspect.isclass(obj) and issubclass(obj, SQLModel) and obj is not SQLModel):
            continue
        table = getattr(obj, "__table__", None)
        if table is not None and "org_id" in table.columns and name not in covered:
            missing.append(name)
    assert not missing, (
        f"these models carry org_id but _cascade_delete_org never deletes them: {missing}. "
        f"A team holding any of these rows cannot be deleted — the foreign key fails with a 500.")


async def test_a_team_with_a_BALANCE_can_still_be_deleted(clients):
    """The exact production failure, end to end. A team is granted $1.00 on creation, so it owns a
    CreditBlock and a LedgerEntry from its first moment — which is precisely why every delete broke.

    WORTH KNOWING: this test passes even with the bug present. The suite runs on SQLite, which does
    not enforce foreign keys by default; production is Postgres, which does. That difference is why
    the bug shipped and why the tests were quiet about it. The guard that actually holds is
    `test_org_delete_clears_EVERY_org_scoped_table` above, which checks the SCHEMA rather than the
    behaviour. This test is kept because it documents the real-world shape of the failure, but do not
    mistake it for the protection."""
    r = await clients.post("/orgs", json={"name": "throwaway-team"})
    assert r.status_code == 200, r.text
    org_id, slug, token = r.json()["org_id"], r.json()["org"], r.json()["token"]
    # the new team's OWN token: a per-org token has its org baked in, so X-Treg-Org cannot switch it
    hdr = {"X-Treg-Token": token}

    bal = await clients.get(f"/orgs/{org_id}/balance", headers=hdr)
    assert bal.status_code == 200 and bal.json()["balance_micro"] > 0, (
        "expected the new-team grant — without a balance this test would not reproduce the bug")

    gone = await clients.request("DELETE", f"/orgs/{org_id}", params={"confirm": slug}, headers=hdr)
    assert gone.status_code == 200, gone.text
    assert (await clients.get(f"/orgs/{org_id}/balance", headers=hdr)).status_code != 200


async def test_a_team_with_oauth_family_authority_can_be_deleted(clients):
    """OAuthGrant deliberately calls its FK `current_org_id`, so the schema-walking `org_id` guard
    above cannot discover it. Pin its explicit cascade path before Postgres turns an omission into a
    foreign-key 500 (SQLite does not enforce that FK in this suite)."""
    from datetime import datetime
    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import OAuthGrant, OAuthRefresh

    made = (await clients.post("/orgs", json={"name": "oauth-family-team"})).json()
    async with session_maker() as db:
        db.add(OAuthGrant(family_id="delete-family", current_org_id=made["org_id"]))
        # Provenance can name a PREVIOUS team. Deleting current family authority still revokes and
        # removes the whole family; otherwise this historical row is orphaned from its authority.
        db.add(OAuthRefresh(token_hash="delete-token", family_id="delete-family", client_id="c",
                            user_id=1, org_id=made["org_id"] + 999,
                            expires_at=datetime(2026, 9, 1)))
        await db.commit()
    gone = await clients.request("DELETE", f"/orgs/{made['org_id']}",
                                 params={"confirm": made["org"]},
                                 headers={"X-Treg-Token": made["token"]})
    assert gone.status_code == 200, gone.text
    async with session_maker() as db:
        assert (await db.execute(select(OAuthGrant).where(
            OAuthGrant.family_id == "delete-family"))).scalar_one_or_none() is None
        assert (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.family_id == "delete-family"))).scalar_one_or_none() is None


async def test_deleting_a_former_grant_team_removes_the_whole_family(clients):
    """A moved family no longer names its former team through OAuthGrant; only the retired token's
    immutable provenance does. Deleting that team used to delete just the retired evidence and leave
    the live destination token working, so replay of the stolen old token became "unknown" instead
    of triggering reuse detection. Team deletion must therefore revoke the provenance-side family
    just as completely as the authority-side case above."""
    from datetime import datetime, timedelta, timezone
    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import OAuthGrant, OAuthRefresh, User

    former = (await clients.post("/orgs", json={"name": "former-grant-team"})).json()
    current = (await clients.post("/orgs", json={"name": "current-grant-team"})).json()
    async with session_maker() as db:
        user = (await db.execute(select(User).where(
            User.email == "tim@superdesign.dev"))).scalar_one()
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        db.add(OAuthGrant(family_id="moved-delete-family", current_org_id=current["org_id"]))
        db.add(OAuthRefresh(
            token_hash="retired-former-token", family_id="moved-delete-family", client_id="c",
            user_id=user.id, org_id=former["org_id"], expires_at=expires,
            retired_at=datetime.now(timezone.utc).replace(tzinfo=None), retired_reason="rotated"))
        db.add(OAuthRefresh(
            token_hash="live-current-token", family_id="moved-delete-family", client_id="c",
            user_id=user.id, org_id=current["org_id"], expires_at=expires))
        await db.commit()

    gone = await clients.request(
        "DELETE", f"/orgs/{former['org_id']}", params={"confirm": former["org"]},
        headers={"X-Treg-Token": former["token"]})
    assert gone.status_code == 200, gone.text
    async with session_maker() as db:
        assert (await db.execute(select(OAuthGrant).where(
            OAuthGrant.family_id == "moved-delete-family"))).scalar_one_or_none() is None
        assert (await db.execute(select(OAuthRefresh).where(
            OAuthRefresh.family_id == "moved-delete-family"))).scalars().all() == []
