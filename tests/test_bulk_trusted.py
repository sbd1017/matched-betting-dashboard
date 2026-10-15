"""Bulk confirmation (dependency-aware, all-or-nothing or valid-only) and Trusted Assistant mode."""
import json

import pytest

from bets import imports, metrics, services
from tests.test_assistant import API, api, post, token  # noqa: F401  (fixtures)


def one(app, sql, *args):
    with app.state.db.read() as conn:
        return conn.execute(sql, args).fetchone()[0]


def iid_of(app, body):
    return one(app, "SELECT id FROM import_candidates WHERE public_id = ?", body["candidate_id"])


def bulk(browser, action, ids=()):
    data = {"csrf_token": browser.csrf, "action": action, "ids": [str(i) for i in ids]}
    return browser.c.post("/bets/imports/bulk", data=data, follow_redirects=False)


@pytest.fixture
def base(app):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Lloyds", kind="bank")
        services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200, opening_balance=5000)
        services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=5000)


# ---------------------------------------------------------------- pending references and dependency order

def test_transfer_into_a_pending_account_waits_for_it(app, api, browser, base):
    acc = post(api, "account", {"name": "Coral", "account_type": "bookmaker"}).json()
    tr = post(api, "transfer", {"from": "Lloyds", "to": "Coral", "amount": "20", "capital_type": "temporary"})
    assert tr.status_code == 201, tr.json()
    assert any("waits for the pending account 'Coral'" in w for w in tr.json()["warnings"])
    # on its own it cannot be confirmed yet ...
    r = browser.post(f"/bets/imports/{iid_of(app, tr.json())}", {"decision": "accept"})
    assert "err=" in r.headers["location"] and "Coral" in r.headers["location"]
    assert "Waits for: account" in browser.get("/bets/imports").text
    # ... but selected together (in either order) the account is confirmed first, then the transfer
    page = bulk(browser, "selected", [iid_of(app, tr.json()), iid_of(app, acc)])
    assert page.status_code == 200 and "2 confirmed" in page.text
    with app.state.db.read() as conn:
        coral = conn.execute("SELECT id FROM accounts WHERE name = 'Coral'").fetchone()[0]
        t = conn.execute("SELECT to_account_id, capital_type, amount_pence FROM transfers").fetchone()
        assert tuple(t) == (coral, "temporary_funding", 2000)
        order = [json.loads(r[0]).get("created") for r in conn.execute(
            "SELECT after_json FROM audit_log WHERE entity = 'import' AND action = 'applied' ORDER BY id")]
        assert order[0].startswith("account:") and order[1].startswith("transfer:")
        assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'import_batch'").fetchone()[0] == 1
        assert conn.execute("SELECT count(DISTINCT decided_by) FROM import_candidates").fetchone()[0] == 1   # user-bulk


def test_deposit_is_confirmed_before_its_return(app, api, browser, base):
    back = post(api, "transfer", {"from": "Smarkets", "to": "Lloyds", "amount": "102.40", "capital_type": "temporary"}).json()
    dep = post(api, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "100", "capital_type": "temporary"}).json()
    assert bulk(browser, "all_valid").status_code == 200
    with app.state.db.read() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT capital_type, amount_pence, temp_portion_pence FROM transfers ORDER BY id")]
        assert rows == [("temporary_funding", 10000, 10000), ("temporary_return", 10240, 10000)]
        s = metrics.summary(conn)
        assert s.temporary_outstanding == 0 and s.profit_lifetime == 0                  # never profit
    assert back and dep


def test_offer_before_the_free_bet_that_references_it(app, api, browser, base):
    offer = post(api, "offer", {"title": "Bet £10 Get £50", "bookmaker": "Betfred"}).json()
    fb = post(api, "free_bet", {"bookmaker": "Betfred", "value": "10", "free_bet_type": "snr", "offer": "Bet £10 Get £50"})
    assert fb.status_code == 201 and any("pending offer" in w for w in fb.json()["warnings"])
    bulk(browser, "selected", [iid_of(app, fb.json()), iid_of(app, offer)])
    with app.state.db.read() as conn:
        oid = conn.execute("SELECT id FROM offers").fetchone()[0]
        assert conn.execute("SELECT offer_id FROM free_bets").fetchone()[0] == oid      # offer -> token link kept


def test_confirm_selected_is_all_or_nothing(app, api, browser, base):
    a = post(api, "account", {"name": "Coral", "account_type": "bookmaker"}).json()
    b = post(api, "account", {"name": "coral", "account_type": "bookmaker"}).json()     # same account twice
    page = bulk(browser, "selected", [iid_of(app, a), iid_of(app, b)])
    assert "Nothing was confirmed" in page.text and "already exists" in page.text
    assert one(app, "SELECT count(*) FROM accounts WHERE name LIKE 'coral'") == 0         # rolled back entirely
    assert one(app, "SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'") == 2


def test_confirm_all_valid_holds_only_what_cannot_be_confirmed(app, api, browser, base):
    a = post(api, "account", {"name": "Coral", "account_type": "bookmaker"}).json()
    b = post(api, "account", {"name": "coral", "account_type": "bookmaker"}).json()
    t = post(api, "transfer", {"from": "Lloyds", "to": "Coral", "amount": "5", "capital_type": "temporary"}).json()
    page = bulk(browser, "all_valid")
    assert "2 confirmed" in page.text and "1 left pending" in page.text
    assert one(app, "SELECT count(*) FROM accounts WHERE name LIKE 'coral'") == 1
    assert one(app, "SELECT count(*) FROM transfers") == 1
    held = one(app, "SELECT status FROM import_candidates WHERE id = ?", iid_of(app, b))
    assert held == "needs_confirmation" and a and t


def test_bulk_needs_the_session_and_token(app, anon, browser, base):
    assert anon.post("/bets/imports/bulk", data={"action": "all_valid"}, follow_redirects=False).status_code in (401, 403)
    assert browser.c.post("/bets/imports/bulk", data={"action": "all_valid", "csrf_token": "forged"},
                          follow_redirects=False).status_code == 403
    assert "err=" in bulk(browser, "selected", []).headers["location"]


def test_imports_page_has_bulk_controls_and_keeps_single_ones(app, api, browser, base):
    body = post(api, "account", {"name": "Coral", "account_type": "bookmaker"}).json()
    page = browser.get("/bets/imports").text
    for text in ('name="ids"', "data-select-all", 'value="selected"', 'value="all_valid"', "Confirm selected",
                 "Confirm all valid"):
        assert text in page, text
    detail = browser.get(f"/bets/imports/{iid_of(app, body)}").text
    assert 'value="accept"' in detail and 'value="reject"' in detail and "?edit=1" in detail


# ---------------------------------------------------------------- Trusted Assistant mode

def trust(browser, *kinds, enabled=True):
    data = {"csrf_token": browser.csrf, "kinds": list(kinds), **({"enabled": "on"} if enabled else {})}
    r = browser.c.post("/bets/settings/assistant/auto-confirm", data=data, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]


def test_trusted_mode_is_off_by_default(app, api, browser, base):
    assert "Currently OFF" in browser.get("/bets/settings").text
    body = post(api, "account", {"name": "Coral", "account_type": "bookmaker"}).json()
    assert body["status"] == "pending_confirmation" and body["auto_confirmed"] is False
    assert one(app, "SELECT count(*) FROM accounts WHERE name = 'Coral'") == 0


def test_trusted_mode_confirms_allowed_types_with_full_audit(app, api, browser, base):
    trust(browser, "account")
    assert "Currently ON for New account" in browser.get("/bets/settings").text
    body = post(api, "account", {"name": "Coral", "account_type": "bookmaker", "balance": "12.34"}).json()
    assert body["status"] == "confirmed" and body["auto_confirmed"] is True
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM accounts WHERE name = 'Coral'").fetchone()[0] == 1
        item = conn.execute("SELECT * FROM import_candidates WHERE public_id = ?", (body["candidate_id"],)).fetchone()
        assert item["status"] == "applied" and item["decided_by"] == "assistant-auto"
        notes = [r[0] for r in conn.execute("SELECT note FROM audit_log WHERE entity = 'import' AND entity_id = ? "
                                            "AND action = 'auto_confirmed'", (item["id"],))]
        assert notes and "Matched Betting Assistant" in notes[0]
        assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'setting'").fetchone()[0] == 1
    assert "confirmed automatically by the Matched Betting Assistant" in browser.get(f"/bets/imports/{item['id']}").text
    # a type that is not ticked still waits
    t = post(api, "transfer", {"from": "Lloyds", "to": "Coral", "amount": "5", "capital_type": "temporary"}).json()
    assert t["status"] == "pending_confirmation" and t["held_for_review"] is None


@pytest.mark.parametrize("data,reason", [
    ({"from": "Lloyds", "to": "Smarkets", "amount": "100"}, "capital_type not given"),           # incomplete
    ({"from": "Lloyds", "to": "Smarkets", "amount": "2500", "capital_type": "temporary"}, "unusually large"),
])
def test_trusted_mode_holds_unusual_or_incomplete_entries(app, api, browser, base, data, reason):
    trust(browser, "transfer")
    body = post(api, "transfer", data).json()
    assert body["status"] == "pending_confirmation" and reason in body["held_for_review"]
    assert one(app, "SELECT count(*) FROM transfers") == 0
    assert reason in browser.get(f"/bets/imports/{iid_of(app, body)}").text


def test_trusted_mode_holds_dependencies_incomplete_and_duplicate_positions(app, api, browser, base):
    trust(browser, "transfer", "position")
    post(api, "account", {"name": "Coral", "account_type": "bookmaker"})                  # accounts not trusted
    dep = post(api, "transfer", {"from": "Lloyds", "to": "Coral", "amount": "5", "capital_type": "temporary"}).json()
    assert "waits for the pending account 'Coral'" in dep["held_for_review"]
    half = post(api, "position", {"event": "A v B", "legs": [{"account": "Betfred", "side": "back", "stake": "10", "odds": "3"}]}).json()
    assert "incomplete" in half["held_for_review"]
    full = {"event": "C v D", "legs": [{"account": "Betfred", "side": "back", "stake": "10", "odds": "3"},
                                       {"account": "Smarkets", "side": "lay", "odds": "3.1"}]}
    first = post(api, "position", full).json()
    assert first["status"] == "confirmed" and first["auto_confirmed"] is True
    again = post(api, "position", full).json()                                           # same event again
    assert again["status"] == "pending_confirmation" and "possible duplicate" in again["held_for_review"]
    assert one(app, "SELECT count(*) FROM positions") == 1


def test_trusted_mode_keeps_idempotency_and_only_applies_to_the_assistant(app, api, browser, base):
    trust(browser, "account", "position")
    data = {"name": "Coral", "account_type": "bookmaker", "idempotency_key": "chat-1"}
    first, again = post(api, "account", data).json(), post(api, "account", data)
    assert again.status_code == 200 and again.json()["candidate_id"] == first["candidate_id"]
    assert again.json()["status"] == "confirmed" and one(app, "SELECT count(*) FROM accounts WHERE name = 'Coral'") == 1
    # a signed-in import from another tool is never auto-confirmed
    r = browser.c.post("/bets/api/import", headers={"X-CSRF-Token": browser.csrf}, json={
        "event": "Other tool", "legs": [{"account": "Betfred", "side": "back", "stake": "5", "odds": "3"},
                                        {"account": "Smarkets", "side": "lay", "odds": "3.1"}]})
    assert r.status_code == 201 and one(app, "SELECT status FROM import_candidates WHERE id = ?", r.json()["id"]) == "needs_confirmation"


def test_trusted_mode_with_no_types_is_off(app, api, browser, base):
    trust(browser)                                                                          # ticked ON, no types
    assert "Currently OFF" in browser.get("/bets/settings").text
    assert post(api, "account", {"name": "Coral", "account_type": "bookmaker"}).json()["status"] == "pending_confirmation"
    trust(browser, "account", enabled=False)
    assert imports.auto_confirm_settings(app.state.db.connect())["enabled"] is False
