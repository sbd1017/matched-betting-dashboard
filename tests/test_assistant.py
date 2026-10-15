"""Assistant Intake API: a separate bearer token that can only submit candidates the user confirms."""
import json
import logging
import os
import stat

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from bets import assistant, cli, metrics, services
from tests.forms import submit
from tests.test_web import PUBLIC, seed

API = "/bets/api/assistant"


@pytest.fixture
def token(settings):
    return assistant.issue(settings.assistant_file)


@pytest.fixture
def api(app, token):
    with TestClient(app, base_url="http://testserver") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def count(app, sql, *args):
    with app.state.db.read() as conn:
        return conn.execute(sql, args).fetchone()[0]


def ledger_rows(app):
    return count(app, "SELECT count(*) FROM ledger_entries")


def post(api, kind, data):
    return api.post(f"{API}/candidates/{kind}", json=data)


# ---------------------------------------------------------------- the credential and its scope

def test_no_token_wrong_token_or_session_login_is_refused(app, browser, token):
    bad = [None, "Bearer", f"Bearer {token}x", f"Bearer {token[:-1]}{'B' if token[-1] == 'A' else 'A'}", f"Basic {token}", token,
           "Bearer mbai_00000000_" + "A" * 43]
    with TestClient(app, base_url="http://testserver") as c:
        for header in bad:
            for method, path in (("GET", "/schema"), ("POST", "/candidates/account"), ("POST", "/calculate"),
                                 ("GET", "/openapi.json")):
                r = c.request(method, API + path, headers={"Authorization": header} if header else {},
                              json={"name": "X", "account_type": "bookmaker"})
                assert r.status_code in (401, 429), (header, path, r.status_code)
                assert "set-cookie" not in r.headers
        # never in the URL
        assert c.get(f"{API}/schema?token={token}").status_code in (401, 429)
    # a signed-in browser session is not the assistant credential
    r = browser.c.post(f"{API}/candidates/account", json={"name": "X", "account_type": "bookmaker"},
                       headers={"X-CSRF-Token": browser.csrf})
    assert r.status_code in (401, 429)                      # (429: this client already failed many times above)
    assert count(app, "SELECT count(*) FROM import_candidates") == 0


def test_token_opens_nothing_outside_the_assistant_api(app, api):
    seed(app)
    reached = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path.startswith(API) or route.path in PUBLIC:
            continue
        path = route.path.replace("{", "").replace("}", "")
        for method in route.methods - {"HEAD"}:
            r = api.request(method, path, follow_redirects=False)
            if r.status_code not in (303, 401, 404):
                reached.append((method, route.path, r.status_code))
            assert "£" not in r.text and "Betfred" not in r.text, (method, route.path)
    assert reached == []
    # in particular: no confirming, settling, exporting, or reading figures
    for path in ("/bets/", "/bets/ledger", "/bets/accounts", "/bets/export/full.json", "/bets/export/ledger.csv",
                 "/bets/api/calc?stake=10&back_odds=2&lay_odds=2", "/bets/imports"):
        assert api.get(path, follow_redirects=False).status_code in (303, 401), path
    assert api.post("/bets/imports/1", data={"decision": "accept"}, follow_redirects=False).status_code in (401, 403)


def test_failed_tokens_are_rate_limited(app):
    with TestClient(app, base_url="http://testserver") as c:
        codes = [c.get(f"{API}/schema", headers={"Authorization": "Bearer nope"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_others_failures_never_lock_out_the_real_token(app, token):
    with TestClient(app, base_url="http://testserver") as c:
        for _ in range(15):
            c.get(f"{API}/schema", headers={"Authorization": "Bearer nope"})
        assert c.get(f"{API}/schema", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_token_is_stored_hashed_and_can_be_rotated_or_disabled(app, settings, api, token):
    path = settings.assistant_file
    assert token not in path.read_text() and token.startswith("mbai_")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert api.get(f"{API}/schema").status_code == 200
    new = assistant.issue(path)                                    # rotate: the old one stops at once
    assert api.get(f"{API}/schema").status_code == 401
    assert api.get(f"{API}/schema", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    assistant.disable(path)
    assert api.get(f"{API}/schema", headers={"Authorization": f"Bearer {new}"}).status_code == 401
    assert "sha256" not in json.loads(path.read_text())


def test_settings_page_can_disable_it(app, browser, api):
    page = browser.get("/bets/settings").text
    assert "Assistant Intake" in page and "Disable assistant access" in page
    r = submit(browser.c, "/bets/settings", "/bets/settings/assistant/disable")
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert api.get(f"{API}/schema").status_code == 401
    assert "Off" in browser.get("/bets/settings").text


def test_cli_issues_rotates_and_disables(settings, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("BETS_DATA_DIR", str(settings.data_dir))
    out = tmp_path / "token.txt"
    cli.main(["assistant-token", "create", str(out)])
    text = out.read_text()
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600 and "Token: mbai_" in text
    tok = text.split("Token: ")[1].split()[0]
    assert assistant.TokenCheck(settings.assistant_file).verify(f"Bearer {tok}")
    with pytest.raises(SystemExit):
        cli.main(["assistant-token", "create", str(out)])           # never silently replaced
    cli.main(["assistant-token", "status"])
    cli.main(["assistant-token", "rotate", "-"])
    cli.main(["assistant-token", "disable"])
    cli.main(["assistant-openapi"])
    printed = capsys.readouterr()
    assert tok not in printed.out + printed.err
    assert not assistant.TokenCheck(settings.assistant_file).verify(f"Bearer {tok}")
    assert assistant.status(settings.assistant_file)["enabled"] is False


def test_token_never_reaches_logs_or_audit(app, api, token, caplog):
    seed(app)
    caplog.set_level(logging.DEBUG)
    post(api, "account", {"name": "Bet365", "account_type": "bookmaker", "balance": "5"})
    api.get(f"{API}/schema")
    assert token not in caplog.text
    with app.state.db.read() as conn:
        audit = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert token not in audit and f"assistant:{token.split('_')[1]}" in audit


def test_schema_and_openapi(api):
    schema = api.get(f"{API}/schema").json()
    assert set(schema["types"]) == {"account", "balance", "transfer", "offer", "free_bet", "position", "note",
                                    "settlement", "reward", "arrival", "account_status", "amend", "reverse"}
    assert any("confirm" in rule for rule in schema["rules"])
    spec = api.get(f"{API}/openapi.json").json()
    assert spec["openapi"].startswith("3.1") and spec["servers"][0]["url"] == "https://example.com/bets/api/assistant"
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    descriptions = []

    def walk(node):
        if isinstance(node, dict):
            descriptions.extend(v for k, v in node.items() if k in ("description", "summary") and isinstance(v, str))
            [walk(v) for v in node.values()]
        elif isinstance(node, list):
            [walk(v) for v in node]
    walk(spec)
    assert descriptions and max(map(len, descriptions)) <= 300          # ChatGPT Actions limit
    ops = [op["operationId"] for p in spec["paths"].values() for op in p.values()]
    assert len(ops) == len(set(ops)) and "submitAccountCandidate" in ops and "calculateLay" in ops
    for name in ops:                          # only submits candidates, checks status, schema and arithmetic
        assert name.startswith(("submit", "get", "calculate")), name
        assert not any(w in name.lower() for w in ("confirm", "delete", "export", "balances", "ledger", "profit"))


# ---------------------------------------------------------------- the conversations from the brief

def test_add_betfred_with_balance_then_confirm(app, api, browser):
    r = post(api, "account", {"name": "Betfred", "account_type": "bookmaker", "balance": "43.27"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending_confirmation" and body["type"] == "account"
    assert body["summary"] == {"name": "Betfred", "account_type": "bookmaker", "balance": "43.27"}
    assert body["warnings"] == [] and body["confirmation_url"] == f"https://example.com/bets/imports/{body['candidate_id']}"
    assert set(body) == {"status", "candidate_id", "type", "summary", "warnings", "confirmation_url", "edited_by_user",
                         "auto_confirmed", "held_for_review"}
    assert body["auto_confirmed"] is False and body["held_for_review"] is None          # Trusted mode is off by default
    assert count(app, "SELECT count(*) FROM accounts") == 0 and ledger_rows(app) == 0      # nothing real yet
    listing = browser.get("/bets/imports").text
    assert "Betfred (bookmaker)" in listing and "Pending" in listing and "Assistant (ChatGPT)" in listing
    page = browser.get(f"/bets/imports/{body['candidate_id']}").text
    for text in ("Betfred", "Bookmaker", "£43.27", "Confirm", "Edit", "Reject", "Pending — nothing saved yet"):
        assert text in page, text
    iid = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", body["candidate_id"])
    r = submit(browser.c, f"/bets/imports/{iid}", f"/bets/imports/{iid}", {"decision": "accept"})
    assert r.status_code == 303 and r.headers["location"].startswith("/bets/accounts/")
    with app.state.db.read() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE name = 'Betfred'").fetchone()
        assert services.balance(conn, acc["id"]) == 4327
        assert [tuple(r) for r in conn.execute("SELECT category, class FROM ledger_entries")] == [("opening_balance", "capital")]
        s = metrics.summary(conn)
        assert s.profit_lifetime == 0                                                 # capital, not profit
    assert api.get(f"{API}/candidates/{body['candidate_id']}").json()["status"] == "confirmed"


def test_temporary_transfer_lloyds_to_smarkets(app, api, browser):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Lloyds", kind="bank")
        services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200)
    r = post(api, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "100", "capital_type": "temporary"})
    body = r.json()
    assert r.status_code == 201 and body["summary"] == {"from": "Lloyds", "to": "Smarkets", "amount": "100.00",
                                                        "capital_type": "temporary_funding"}
    assert count(app, "SELECT count(*) FROM transfers") == 0
    page = browser.get(f"/bets/imports/{body['candidate_id']}").text
    assert "Lloyds → Smarkets" in page and "£100.00" in page and "Temporary funding" in page
    iid = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", body["candidate_id"])
    assert submit(browser.c, f"/bets/imports/{iid}", f"/bets/imports/{iid}", {"decision": "accept"}).status_code == 303
    with app.state.db.read() as conn:
        assert tuple(conn.execute("SELECT capital_type, amount_pence FROM transfers").fetchone()) == ("temporary_funding", 10000)
        s = metrics.summary(conn)
        assert s.profit_lifetime == 0 and s.temporary_outstanding == 10000


def test_transfer_classification_is_checked_by_the_app(app, api):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Lloyds", kind="bank")
        services.create_account(conn, name="Monzo", kind="bank")
        services.create_account(conn, name="Smarkets", kind="exchange")
    r = post(api, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "50"})
    assert r.status_code == 201 and any("capital_type not given" in w for w in r.json()["warnings"])
    assert post(api, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "50", "capital_type": "permanent"}
                ).json()["summary"]["capital_type"] == "bankroll_contribution"
    assert post(api, "transfer", {"from": "Smarkets", "to": "Lloyds", "amount": "50", "capital_type": "temporary"}
                ).json()["summary"]["capital_type"] == "temporary_return"
    bad = post(api, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "50", "capital_type": "temporary_return"})
    assert bad.status_code == 400 and bad.json()["status"] == "rejected"
    assert post(api, "transfer", {"from": "Lloyds", "to": "Monzo", "amount": "5"}).status_code == 400
    assert post(api, "transfer", {"from": "Lloyds", "to": "Nowhere", "amount": "5"}).status_code == 400


def test_matched_position_lay_stake_comes_from_the_app(app, api, browser):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=5000)
        services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200, opening_balance=5000)
    before = ledger_rows(app)
    r = post(api, "position", {"event": "Arsenal v Spurs", "selection": "Arsenal", "legs": [
        {"account": "Betfred", "side": "back", "stake": "10", "odds": "4.0"},
        {"account": "Smarkets", "side": "lay", "odds": "4.2"}]})
    body = r.json()
    assert r.status_code == 201, body
    lay = body["summary"]["legs"][1]
    assert (lay["stake"], lay["liability"], lay["commission"]) == ("9.57", "30.62", "2%")
    assert body["summary"]["expected_result"] == "-0.62"
    assert any("calculated by the app" in w for w in body["warnings"])
    assert ledger_rows(app) == before and count(app, "SELECT count(*) FROM positions") == 0
    iid = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", body["candidate_id"])
    r = submit(browser.c, f"/bets/imports/{iid}", f"/bets/imports/{iid}", {"decision": "accept"})
    assert r.status_code == 303 and r.headers["location"].startswith("/bets/positions/")
    assert count(app, "SELECT count(*) FROM legs") == 2
    assert count(app, "SELECT stake_pence FROM legs WHERE side = 'lay'") == 957


def test_calculate_is_arithmetic_only(app, api):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200)
    audit_before = count(app, "SELECT count(*) FROM audit_log")
    r = api.post(f"{API}/calculate", json={"back_stake": "10", "back_odds": "4", "lay_odds": "4.2", "exchange": "Smarkets"})
    assert r.status_code == 200 and r.json()["lay_stake"] == "9.57" and r.json()["expected_result"] == "-0.62"
    fb = api.post(f"{API}/calculate", json={"position_type": "free_bet_snr", "back_stake": "10", "back_odds": "5",
                                            "lay_odds": "5.2", "commission": "2"}).json()
    assert fb["retention_percent"] is not None
    assert api.post(f"{API}/calculate", json={"back_stake": "10", "back_odds": "x", "lay_odds": "2"}).status_code == 400
    assert count(app, "SELECT count(*) FROM audit_log") == audit_before


def test_balance_offer_free_bet_and_note_candidates(app, api, browser):
    with app.state.db.tx() as conn:
        bf = services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=5000)
    responses = [
        post(api, "balance", {"account": "Betfred", "balance": "47.10"}),
        post(api, "offer", {"title": "Bet £10 Get £30", "bookmaker": "Betfred", "offer_type": "sign_up",
                            "reward_value": "30", "expires_at": "2099-01-01T12:00"}),
        post(api, "free_bet", {"bookmaker": "Betfred", "value": "10", "expires_at": "2099-01-02"}),
        post(api, "note", {"target": "account", "target_ref": "Betfred", "text": "Verified ID"})]
    for r in responses:
        assert r.status_code == 201, r.json()
        assert "50.00" not in r.text and "50" not in json.dumps(r.json()["summary"])   # never the app's balance
    assert any("SNR" in w for w in responses[2].json()["warnings"])
    assert count(app, "SELECT count(*) FROM offers") == 0 and count(app, "SELECT count(*) FROM free_bets") == 0
    page = browser.get(f"/bets/imports/{responses[0].json()['candidate_id']}").text
    assert "£50.00" in page and "−£2.90" in page                     # the signed-in page shows the difference
    for r in responses:
        iid = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", r.json()["candidate_id"])
        assert submit(browser.c, f"/bets/imports/{iid}", f"/bets/imports/{iid}", {"decision": "accept"}).status_code == 303
    with app.state.db.read() as conn:
        assert services.balance(conn, bf) == 4710
        assert conn.execute("SELECT category FROM ledger_entries ORDER BY id DESC").fetchone()[0] == "reconciliation"
        assert conn.execute("SELECT count(*) FROM offers").fetchone()[0] == 1
        assert tuple(conn.execute("SELECT kind, face_value_pence FROM free_bets").fetchone()) == ("snr", 1000)
        assert "Verified ID" in conn.execute("SELECT notes FROM accounts WHERE id = ?", (bf,)).fetchone()[0]
    assert all(api.get(f"{API}/candidates/{r.json()['candidate_id']}").json()["status"] == "confirmed" for r in responses)


def test_user_can_edit_or_reject(app, api, browser):
    r = post(api, "account", {"name": "Betfred", "account_type": "bookmaker", "balance": "432.70"})
    cid = r.json()["candidate_id"]
    iid = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", cid)
    page = f"/bets/imports/{iid}"
    assert 'id="edit" open' not in browser.get(page).text
    assert 'href="?edit=1#edit"' in browser.get(page).text and 'id="edit" open' in browser.get(page + "?edit=1").text
    e = submit(browser.c, page, f"{page}/edit", {"f_balance": "43.27"})
    assert e.status_code == 303 and "ok=" in e.headers["location"]
    status = api.get(f"{API}/candidates/{cid}").json()
    assert status["summary"]["balance"] == "43.27" and status["edited_by_user"] is True
    bad = submit(browser.c, page, f"{page}/edit", {"f_balance": "43.271"})
    assert "err=" in bad.headers["location"]
    submit(browser.c, page, page, {"decision": "accept"})
    with app.state.db.read() as conn:
        assert services.balance(conn, conn.execute("SELECT id FROM accounts").fetchone()[0]) == 4327
    r2 = post(api, "account", {"name": "Coral", "account_type": "bookmaker"})
    iid2 = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", r2.json()["candidate_id"])
    submit(browser.c, f"/bets/imports/{iid2}", f"/bets/imports/{iid2}", {"decision": "reject"})
    assert api.get(f"{API}/candidates/{r2.json()['candidate_id']}").json()["status"] == "rejected"
    assert count(app, "SELECT count(*) FROM accounts WHERE name = 'Coral'") == 0


def test_edit_position_legs(app, api, browser):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Betfred", kind="bookmaker")
        services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200)
    r = post(api, "position", {"event": "A v B", "legs": [{"account": "Betfred", "side": "back", "stake": "100", "odds": "3"}]})
    iid = count(app, "SELECT id FROM import_candidates WHERE public_id = ?", r.json()["candidate_id"])
    page = f"/bets/imports/{iid}"
    e = submit(browser.c, page, f"{page}/edit", {"leg0_stake": "10", "leg1_account": "Smarkets", "leg1_side": "lay",
                                                 "leg1_odds": "3.1", "leg1_stake": "9.80"})
    assert "ok=" in e.headers["location"], e.headers["location"]
    legs = api.get(f"{API}/candidates/{r.json()['candidate_id']}").json()["summary"]["legs"]
    assert [(l["side"], l["stake"]) for l in legs] == [("back", "10.00"), ("lay", "9.80")]


def test_dry_run_idempotency_and_validation(app, api):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Betfred", kind="bookmaker")
    dry = post(api, "free_bet", {"bookmaker": "Betfred", "value": "5", "free_bet_type": "snr", "dry_run": True})
    assert dry.status_code == 200 and dry.json()["status"] == "valid" and "candidate_id" not in dry.json()
    assert count(app, "SELECT count(*) FROM import_candidates") == 0
    data = {"bookmaker": "Betfred", "value": "5", "free_bet_type": "snr", "idempotency_key": "chat-1-msg-7"}
    first, again = post(api, "free_bet", data), post(api, "free_bet", data)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["duplicate"] is True
    assert first.json()["candidate_id"] == again.json()["candidate_id"]
    assert count(app, "SELECT count(*) FROM import_candidates") == 1
    assert post(api, "free_bet", {**data, "value": "6"}).status_code == 409
    assert post(api, "spaceship", {}).status_code == 404
    r = post(api, "account", {"name": "Coral", "account_type": "bookie"})
    assert r.status_code == 400 and r.json()["errors"]
    r = post(api, "account", {"name": "Coral", "account_type": "bookmaker", "colour": "blue"})
    assert r.status_code == 201 and "ignored unknown field: colour" in r.json()["warnings"]
    assert api.post(f"{API}/candidates/account", content=b"name=x",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert api.post(f"{API}/candidates/account", content=b"{nope", headers={"Content-Type": "application/json"}
                    ).status_code == 400
    assert api.post(f"{API}/candidates/account", content=b'{"notes": "' + b"x" * 17000 + b'"}',
                    headers={"Content-Type": "application/json"}).status_code == 413
    assert api.put(f"{API}/candidates/account", json={}).status_code == 405


def test_limits_on_pending_candidates(app, api, monkeypatch):
    monkeypatch.setattr(assistant, "MAX_PENDING", 2)
    codes = [post(api, "account", {"name": f"Book {i}", "account_type": "bookmaker"}).status_code for i in range(3)]
    assert codes == [201, 201, 429]


def test_status_only_for_the_assistants_own_candidates(app, api, browser):
    seed(app)
    r = browser.c.post("/bets/api/import", headers={"X-CSRF-Token": browser.csrf}, json={
        "event": "Other tool", "legs": [{"account": "Betfred", "side": "back", "stake": "5", "odds": "3"}]})
    assert r.status_code == 201
    pid = count(app, "SELECT public_id FROM import_candidates WHERE id = ?", r.json()["id"])
    assert api.get(f"{API}/candidates/{pid}").status_code == 404
    assert api.get(f"{API}/candidates/1").status_code == 404           # sequential ids mean nothing here
