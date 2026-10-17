"""A correction requested twice (as an Imports candidate and as a protected change) must not leave a stale
pending item behind once either one is confirmed. Never weakens the confirmation step and never applies twice."""
import pytest

from bets import imports, ops, services

from .test_assistant import API, api, post, token  # noqa: F401 (fixtures)
from .test_bulk_trusted import trust


@pytest.fixture
def accts(app):
    with app.state.db.tx() as conn:
        ids = {"fd": services.create_account(conn, name="First Direct", kind="bank"),
               "lloyds": services.create_account(conn, name="Lloyds", kind="bank"),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", opening_balance=50000)}
        ids["t"] = services.create_transfer(conn, from_id=ids["smarkets"], to_id=ids["lloyds"], amount=20000,
                                            capital_type="bankroll_withdrawal")
    return ids


def q(app, sql, *args):
    with app.state.db.read() as conn:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r else None


def ask_both(api, tid):
    """What the assistant did: an Undo in Imports, and the same reversal as a protected change."""
    cand = post(api, "reverse", {"target": "transfer", "ref": str(tid), "reason": "went to First Direct, not Lloyds"})
    assert cand.status_code == 201, cand.json()
    held = api.post(f"{API}/changes/reverse_transfer", json={"transfer_id": tid, "request_id": f"rev-{tid}",
                                                             "reason": "went to First Direct, not Lloyds"})
    assert held.status_code == 202 and held.json()["status"] == "needs_confirmation", held.json()
    iid = q(api.app, "SELECT id FROM import_candidates WHERE public_id = ?", cand.json()["candidate_id"])
    return iid, held.json()["change_id"]


def ledger_rows(app):
    return q(app, "SELECT count(*) FROM ledger_entries")


def test_confirming_the_protected_change_clears_the_matching_import(app, accts, api, browser):
    iid, change_id = ask_both(api, accts["t"])
    assert q(app, "SELECT status FROM import_candidates WHERE id = ?", iid) == "needs_confirmation"
    assert "Undo transfer" in browser.get("/bets/imports").text
    r = browser.post(f"/bets/changes/{change_id}/confirm")                        # the protected path, by the user
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert q(app, "SELECT voided_at FROM transfers WHERE id = ?", accts["t"])
    row = dict(zip(("status", "decided_by", "applied_ref", "hold_reason"), [q(app, f"SELECT {c} FROM import_candidates "
              "WHERE id = ?", iid) for c in ("status", "decided_by", "applied_ref", "hold_reason")]))
    assert row["status"] == "applied" and row["decided_by"] == "system" and row["applied_ref"] == f"transfer:{accts['t']}"
    assert f"by change {change_id} (confirmed by you)" in row["hold_reason"]
    page = browser.get("/bets/imports").text
    assert "Cannot confirm yet" not in page and "Already applied" in page and "Nothing more to do" in page
    assert f'name="ids" value="{iid}"' not in page                                # not selectable any more
    assert q(app, "SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'") == 0
    assert q(app, "SELECT count(*) FROM audit_log WHERE entity = 'import' AND entity_id = ? AND action = "
                  "'already_applied'", iid) == 1


def test_already_applied_before_the_imports_page_is_opened(app, accts, api):
    """Applied straight through the service (any path): the next look at the queue reconciles it."""
    iid, change_id = ask_both(api, accts["t"])
    with app.state.db.tx() as conn:
        services.void_transfer(conn, accts["t"], "reversed elsewhere")
        assert imports.check(conn, services.row(conn, "import_candidates", iid)).startswith("ref: no transfer")  # the old symptom
        out = ops.reconcile_waiting(conn)
    assert out["imports"] == [iid] and out["changes"] == [change_id]               # both representations settle
    assert q(app, "SELECT status FROM import_candidates WHERE id = ?", iid) == "applied"
    assert q(app, "SELECT status FROM assistant_ops WHERE public_id = ?", change_id) == "already_done"
    assert q(app, "SELECT decided_by FROM assistant_ops WHERE public_id = ?", change_id) == "system"


def test_confirming_the_import_clears_the_matching_protected_change(app, accts, api, browser):
    iid, change_id = ask_both(api, accts["t"])
    r = browser.post(f"/bets/imports/{iid}", {"decision": "accept"})              # Imports path, by the user
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert q(app, "SELECT status FROM import_candidates WHERE id = ?", iid) == "applied"
    assert q(app, "SELECT decided_by FROM import_candidates WHERE id = ?", iid) == "user"
    assert q(app, "SELECT status FROM assistant_ops WHERE public_id = ?", change_id) == "already_done"
    with app.state.db.read() as conn:
        assert ops.held(conn) == []


def test_reconciliation_is_idempotent(app, accts, api, browser):
    iid, change_id = ask_both(api, accts["t"])
    browser.post(f"/bets/changes/{change_id}/confirm")
    before = (ledger_rows(app), q(app, "SELECT count(*) FROM transfers"), q(app, "SELECT count(*) FROM audit_log"))
    for _ in range(3):
        with app.state.db.tx() as conn:
            assert ops.reconcile_waiting(conn) == {"imports": [], "changes": []}
        browser.get("/bets/imports")
        browser.get("/bets/")
    assert (ledger_rows(app), q(app, "SELECT count(*) FROM transfers")) == before[:2]
    assert q(app, "SELECT count(*) FROM audit_log WHERE action = 'voided' AND entity = 'transfer'") == 1
    assert q(app, "SELECT count(*) FROM audit_log WHERE action IN ('already_applied', 'already_done')") == 1
    assert q(app, "SELECT status FROM import_candidates WHERE id = ?", iid) == "applied"
    with app.state.db.read() as conn:
        bal = services.balance(conn, accts["smarkets"])
    assert bal == 50000                                                           # reversed exactly once


def test_a_genuinely_broken_reference_still_needs_looking_at(app, accts, api, browser):
    cand = post(api, "reverse", {"target": "transfer", "ref": str(accts["t"]), "reason": "test"}).json()
    iid = q(app, "SELECT id FROM import_candidates WHERE public_id = ?", cand["candidate_id"])
    with app.state.db.tx() as conn:                       # points at a transfer that does not exist at all
        c = conn.execute("SELECT candidate_json FROM import_candidates WHERE id = ?", (iid,)).fetchone()[0]
        conn.execute("UPDATE import_candidates SET candidate_json = ? WHERE id = ?",
                     (c.replace(f'"target_id": {accts["t"]}', '"target_id": 9999').replace('"ref": ' + str(accts["t"]),
                                                                                       '"ref": 9999'), iid))
        assert ops.reconcile_waiting(conn)["imports"] == []
    assert q(app, "SELECT status FROM import_candidates WHERE id = ?", iid) == "needs_confirmation"
    assert "Cannot confirm yet" in browser.get("/bets/imports").text
    # and an amend whose values are NOT already there stays pending
    amend = post(api, "amend", {"target": "transfer", "ref": str(accts["t"]), "at": "2026-01-02T10:00",
                                "reason": "wrong time"}).json()
    aid = q(app, "SELECT id FROM import_candidates WHERE public_id = ?", amend["candidate_id"])
    with app.state.db.tx() as conn:
        assert aid not in ops.reconcile_waiting(conn)["imports"]
    assert q(app, "SELECT status FROM import_candidates WHERE id = ?", aid) == "needs_confirmation"


def test_an_amend_already_made_another_way_is_reconciled(app, accts, api):
    amend = post(api, "amend", {"target": "transfer", "ref": str(accts["t"]), "at": "2026-01-02T10:00",
                                "reason": "wrong time"}).json()
    aid = q(app, "SELECT id FROM import_candidates WHERE public_id = ?", amend["candidate_id"])
    with app.state.db.tx() as conn:
        services.retime_transfer(conn, accts["t"], "2026-01-02T10:00:00Z", "fixed by hand")
        assert ops.reconcile_waiting(conn)["imports"] == [aid]
    assert q(app, "SELECT hold_reason FROM import_candidates WHERE id = ?", aid).startswith("already applied")


def test_ordinary_transfer_still_auto_confirms_in_trusted_mode(app, accts, api, browser):
    trust(browser, "transfer")
    body = post(api, "transfer", {"from": "Smarkets", "to": "First Direct", "amount": "200",
                                  "capital_type": "permanent"}).json()
    assert body["auto_confirmed"] is True and body["status"] == "confirmed"
    # corrections still always wait, even in Trusted mode, and the protected change still needs the user
    cand = post(api, "reverse", {"target": "transfer", "ref": str(accts["t"]), "reason": "test"}).json()
    assert cand["auto_confirmed"] is False
    held = api.post(f"{API}/changes/reverse_transfer", json={"transfer_id": accts["t"], "request_id": "rev-trusted",
                                                             "reason": "test"})
    assert held.status_code == 202
    assert q(app, "SELECT voided_at FROM transfers WHERE id = ?", accts["t"]) is None
