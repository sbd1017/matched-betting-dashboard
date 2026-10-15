"""Every state-changing form, submitted exactly as rendered (the test never supplies the CSRF token itself).

Regression: forms built with the csrf_field() macro rendered an empty token because macros imported
without `with context` cannot see `csrf`, so e.g. adding Betfred on Accounts failed with
"Security token missing or expired".
"""
import re
from pathlib import Path

from fastapi.routing import APIRoute

from bets import assistant, services
from tests.forms import find_form, post_forms, submit
from tests.test_web import seed

TEMPLATES = Path(__file__).resolve().parents[1] / "bets" / "templates"
NOT_FORMS = {"/bets/login",              # its own page, checked in test_web
             "/bets/transfers/{tid}/void",  # route only; no page offers it yet
             "/bets/withdrawals/snooze",    # shown only when a bookmaker has idle cash; token checked in test_features
             "/bets/offers/{oid}/reward",   # shown only for offers with reward terms; token checked in test_features
             "/bets/undo",                  # shown only right after an undoable action; token checked in test_features
             "/bets/transfers/{tid}/arrived",  # shown only while a withdrawal is on its way; token checked in test_features
             "/bets/ops/{name}",            # named changes (/ops/void_free_bet …); tokens checked in test_maintenance
             "/bets/changes/{public_id}/{decision}"}  # held assistant changes; tokens checked in test_maintenance
# /bets/api/... are JSON APIs (session + X-CSRF-Token, or the assistant's bearer token), not forms.


def count(app, sql, *args):
    """First column of the first row."""
    with app.state.db.read() as conn:
        return conn.execute(sql, args).fetchone()[0]


def ok(r):
    assert r.status_code == 303, (r.status_code, r.text[:300])
    assert "err=" not in r.headers["location"], r.headers["location"]
    return r.headers["location"]


def test_add_account_form_creates_betfred(app, browser):
    """The exact failure: log in, open Accounts, add Betfred, submit."""
    r = submit(browser.c, "/bets/accounts", "/bets/accounts", {"name": "Betfred", "kind": "bookmaker"})
    assert "Security token" not in r.text
    assert ok(r).startswith("/bets/accounts?ok=")
    assert count(app, "SELECT count(*) FROM accounts WHERE name='Betfred' AND kind='bookmaker'") == 1
    assert "Betfred" in browser.get("/bets/accounts").text
    # The same form on Settings.
    ok(submit(browser.c, "/bets/settings", "/bets/accounts", {"name": "Smarkets", "kind": "exchange", "commission": "2"}))
    assert count(app, "SELECT commission_bp FROM accounts WHERE name='Smarkets'") == 200


def test_every_post_form_carries_the_session_token(app, browser, settings):
    ids = seed(app)
    assistant.issue(settings.assistant_file)                 # so Settings shows its Disable form
    with app.state.db.tx() as conn:
        services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["smarkets"], amount=10000)
        services.add_manual_entry(conn, account_id=ids["betfred"], amount=500, category="bonus_cash", description="x")
    r = browser.c.post("/bets/api/import", headers={"X-CSRF-Token": browser.csrf}, json={
        "event": "Imported v Match", "legs": [{"account": "Betfred", "side": "back", "stake": "5", "odds": "3"}]})
    assert r.status_code == 201
    pages = sorted({p for route in app.routes if isinstance(route, APIRoute) and "GET" in route.methods
                    for p in [route.path.replace("{pid}", str(ids["position"])).replace("{oid}", str(ids["offer"]))
                              .replace("{aid}", str(ids["betfred"])).replace("{ref}", str(r.json()["id"]))]
                    if "{" not in p})
    htmls = [browser.get(p) for p in pages]
    htmls = [h.text for h in htmls if h.headers.get("content-type", "").startswith("text/html")]
    with app.state.db.tx() as conn:                          # a free-bet position whose back bet has no token yet
        fb_pid = services.create_position(conn, event="Free bet match", kind="free_bet_snr", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0"}])
    htmls.append(browser.get(f"/bets/positions/{fb_pid}").text)
    preview = find_form(browser.get("/bets/quick-add").text, "/bets/quick-add/preview")
    htmls.append(browser.c.post("/bets/quick-add/preview", data={
        **preview.fields, "event": "A v B", "back_account": ids["betfred"], "back_stake": "10", "back_odds": "3",
        "lay_account": ids["smarkets"], "lay_odds": "3.1"}).text)
    covered = set()
    for html in htmls:
        for form in post_forms(html):
            assert form.fields.get("csrf_token") == browser.csrf, f"{form.action}: token {form.fields.get('csrf_token')!r}"
            covered.add(re.sub(r"/\d+(?=/|$)", "/{n}", form.action))
    routes = {re.sub(r"\{\w+\}", "{n}", route.path) for route in app.routes
              if isinstance(route, APIRoute) and "POST" in route.methods}
    missing = {r for r in routes if not r.startswith("/bets/api/")} - covered \
        - {re.sub(r"\{\w+\}", "{n}", p) for p in NOT_FORMS}
    assert not missing, f"POST routes with no rendered form checked: {missing}"


def test_templates_import_macros_with_context():
    for path in TEMPLATES.glob("*.html"):
        for line in re.findall(r"\{%-?\s*from .*? import .*?%\}", path.read_text()):
            assert "with context" in line, f"{path.name}: {line}"


def test_account_edit_reconcile_and_transfer_forms(app, browser):
    ids = seed(app)
    page = f"/bets/accounts/{ids['betfred']}"
    ok(submit(browser.c, page, page, {"name": "Betfred UK", "notes": "main"}))
    assert count(app, "SELECT count(*) FROM accounts WHERE name='Betfred UK'") == 1
    ok(submit(browser.c, page, page + "/reconcile", {"actual": "39.85", "note": "checked app"}))
    assert count(app, "SELECT count(*) FROM reconciliations") == 1
    ok(submit(browser.c, "/bets/accounts", "/bets/transfers",
              {"from_id": ids["lloyds"], "to_id": ids["smarkets"], "amount": "100"}))
    assert count(app, "SELECT count(*) FROM transfers") == 1


def test_quick_add_preview_and_confirm_forms(app, browser):
    ids = seed(app)
    before = count(app, "SELECT count(*) FROM positions")
    fields = {"event": "Spurs v Chelsea", "selection": "Spurs", "back_account": ids["betfred"], "back_stake": "10",
              "back_odds": "4.0", "lay_account": ids["smarkets"], "lay_odds": "4.2", "commission": "2"}
    preview = submit(browser.c, "/bets/quick-add", "/bets/quick-add/preview", fields)
    assert preview.status_code == 200 and "£9.57" in preview.text
    confirm = find_form(preview.text, "/bets/quick-add/confirm")
    r = browser.c.post(confirm.action, data=confirm.fields, follow_redirects=False)
    ok(r)
    assert count(app, "SELECT count(*) FROM positions") == before + 1


def test_use_free_bet_through_quick_add(app, browser):
    ids = seed(app)
    page = f"/bets/quick-add?kind=free_bet_snr&free_bet_id={ids['free_bet']}&back_account={ids['betfred']}"
    preview = submit(browser.c, page, "/bets/quick-add/preview", {
        "event": "Free bet match", "back_stake": "10", "back_odds": "5", "lay_account": ids["smarkets"], "lay_odds": "5.2"})
    assert preview.status_code == 200, preview.text[:300]
    confirm = find_form(preview.text, "/bets/quick-add/confirm")
    ok(browser.c.post(confirm.action, data=confirm.fields, follow_redirects=False))
    assert count(app, "SELECT count(*) FROM legs WHERE free_bet_id=? AND deleted_at IS NULL", ids["free_bet"]) == 1


def test_position_and_bet_forms(app, browser):
    ids = seed(app)
    page = f"/bets/positions/{ids['position']}"
    ok(submit(browser.c, page, page, {"market": "Match odds"}))
    ok(submit(browser.c, page, page + "/legs", {"side": "lay", "account_id": ids["smarkets"], "stake": "1",
                                                "odds": "4.4", "commission": "2"}))
    with app.state.db.read() as conn:
        legs = [r[0] for r in conn.execute("SELECT id FROM legs WHERE position_id=? AND deleted_at IS NULL ORDER BY id",
                                           (ids["position"],))]
    ok(submit(browser.c, page, f"/bets/legs/{legs[-1]}", {"stake": "2"}))
    ok(submit(browser.c, page, f"/bets/legs/{legs[-1]}/delete", {"note": "typo"}))
    ok(submit(browser.c, page, page + "/settle", {"outcome": "back_won"}))
    assert count(app, "SELECT status FROM positions WHERE id=?", ids["position"]) == "settled"
    ok(submit(browser.c, page, f"/bets/legs/{legs[0]}/settle", {"result": "lost"}))
    assert count(app, "SELECT result FROM legs WHERE id=?", legs[0]) == "lost"
    ok(submit(browser.c, page, page + "/delete", {"note": "test"}))


def test_offer_forms(app, browser):
    ids = seed(app)
    ok(submit(browser.c, "/bets/offers", "/bets/offers", {"title": "Bet £5 Get £5", "account_id": ids["betfred"]}))
    oid = count(app, "SELECT id FROM offers WHERE title='Bet £5 Get £5'")
    page = f"/bets/offers/{oid}"
    ok(submit(browser.c, page, page, {"status": "abandoned"}, has="status"))
    assert count(app, "SELECT status FROM offers WHERE id=?", oid) == "abandoned"
    ok(submit(browser.c, page, page, {"status": "follow"}, has="status"))           # reopen: it follows its records
    assert count(app, "SELECT status FROM offers WHERE id=?", oid) == "not_started"
    ok(submit(browser.c, page, page, {"title": "Bet £5 Get £10"}, has="title"))
    assert count(app, "SELECT title FROM offers WHERE id=?", oid) == "Bet £5 Get £10"


def test_free_bet_forms(app, browser):
    ids = seed(app)
    ok(submit(browser.c, "/bets/free-bets", "/bets/free-bets", {"account_id": ids["betfred"], "face_value": "5"}))
    fid = count(app, "SELECT max(id) FROM free_bets")
    ok(submit(browser.c, "/bets/free-bets", f"/bets/free-bets/{fid}", {"status": "expired"}))
    assert count(app, "SELECT status FROM free_bets WHERE id=?", fid) == "expired"


def test_ledger_entry_and_reverse_forms(app, browser):
    ids = seed(app)
    ok(submit(browser.c, "/bets/ledger", "/bets/ledger", {"account_id": ids["betfred"], "category": "bonus_cash",
                                                         "amount": "5", "description": "casino bonus"}))
    eid = count(app, "SELECT id FROM ledger_entries WHERE description='casino bonus'")
    ok(submit(browser.c, "/bets/ledger", f"/bets/ledger/{eid}/reverse", {"note": "wrong account"}))
    assert count(app, "SELECT superseded_at IS NOT NULL FROM ledger_entries WHERE id=?", eid) == 1


def test_import_confirmation_form(app, browser):
    seed(app)
    r = browser.c.post("/bets/api/import", headers={"X-CSRF-Token": browser.csrf}, json={
        "event": "Imported v Match", "legs": [{"account": "Betfred", "side": "back", "stake": "5", "odds": "3"}]})
    iid = r.json()["id"]
    page = f"/bets/imports/{iid}"
    ok(submit(browser.c, page, page, {"decision": "accept"}))
    assert count(app, "SELECT status FROM import_candidates WHERE id=?", iid) == "applied"
    assert count(app, "SELECT count(*) FROM positions WHERE event='Imported v Match'") == 1


def test_logout_form(browser):
    r = submit(browser.c, "/bets/", "/bets/logout")
    assert r.status_code == 303
    assert browser.get("/bets/", follow_redirects=False).status_code == 303
