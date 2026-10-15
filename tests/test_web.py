"""Authentication, CSRF/origin, base path, information exposure, pages and exports."""
import json
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from bets import services
from bets.config import Settings
from bets.web import create_app
from tests.conftest import PASSWORD, USER, Browser, csrf_of, make_settings

PUBLIC = {"/bets/login", "/bets/manifest.webmanifest", "/bets/healthz"}
ESCAPES = re.compile(r'(?:href|src|action)="(?!/bets/|#|\?|https://example\.com/bets/)([^"]*)"')


def seed(app):
    with app.state.db.tx() as conn:
        ids = {"betfred": services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=5000),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200,
                                                   opening_balance=8000),
               "lloyds": services.create_account(conn, name="Lloyds", kind="bank")}
        oid = services.create_offer(conn, title="Bet £10 Get £30", account_id=ids["betfred"],
                                    expires_at="2099-01-01T12:00:00Z")
        pid = services.create_position(conn, event="Liverpool v Arsenal", selection="Liverpool", offer_id=oid,
                                       event_at="2099-01-01T15:00:00Z", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "4.0"},
            {"account_id": ids["smarkets"], "side": "lay", "stake_pence": 957, "odds": "4.2"}])
        fid = services.create_free_bet(conn, account_id=ids["betfred"], face_value=1000, expires_at="2099-01-02T00:00:00Z")
        ids.update(offer=oid, position=pid, free_bet=fid)
        return ids


def concrete(path: str, ids: dict) -> str:
    return (path.replace("{pid}", str(ids["position"])).replace("{oid}", str(ids["offer"]))
            .replace("{fid}", str(ids["free_bet"])).replace("{aid}", str(ids["betfred"])).replace("{leg_id}", "1")
            .replace("{tid}", "1").replace("{eid}", "1").replace("{iid}", "1").replace("{ref}", "1").replace("{kind}", "offer")
            .replace("{item_id}", str(ids["offer"])))


# ---------------------------------------------------------------- authentication on every route

def test_every_route_requires_the_login(app, anon):
    ids = seed(app)
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in PUBLIC:
            continue
        path = concrete(route.path, ids)
        for method in route.methods - {"HEAD"}:
            r = anon.request(method, path, follow_redirects=False)
            assert r.status_code in (303, 401), (method, path, r.status_code)
            if r.status_code == 303:
                assert r.headers["location"].startswith("/bets/login?next=")
            assert "Liverpool" not in r.text and "Betfred" not in r.text and "£" not in r.text
            checked += 1
    assert checked >= 40
    js = anon.get("/bets/static/app.js", follow_redirects=False)
    assert js.status_code == 303                                  # the app's code needs the login too
    assert js.headers["cache-control"] == "no-store"              # never cache the redirect (login loop)
    assert "app.js" not in anon.get("/bets/login").text           # the login page does not load it
    assert anon.get("/bets/static/design.css").status_code == 200    # the login page's stylesheet does not
    assert anon.get("/bets/healthz").text == "ok"                 # and reveals nothing


def test_login_logout_and_wrong_password(app):
    with TestClient(app) as c:
        b = Browser(c)
        assert b.login(password="wrong-password-123").status_code == 401
        assert b.login(user="someone", password=PASSWORD).status_code == 401
        assert b.login().status_code == 303
        assert c.get("/bets/", follow_redirects=False).status_code == 200
        assert b.post("/bets/logout").status_code == 303
        assert c.get("/bets/", follow_redirects=False).status_code == 303


def test_login_attempts_are_limited(app):
    with TestClient(app) as c:
        b = Browser(c)
        codes = [b.login(password=f"wrong-{i}-password").status_code for i in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        assert b.login().status_code == 429                       # even the right password, for a while


def test_login_next_cannot_leave_the_app(app):
    with TestClient(app) as c:
        page = c.get("/bets/login")
        for target in ("https://evil.example/", "//evil.example", "/home/", "/bets\\..\\x"):
            r = c.post("/bets/login", data={"csrf_token": csrf_of(page.text), "username": USER, "password": PASSWORD,
                                            "next": target}, follow_redirects=False)
            assert r.headers["location"] == "/bets/"
            c.post("/bets/logout", data={"csrf_token": csrf_of(c.get("/bets/").text)})
            page = c.get("/bets/login")


def test_session_cookie_attributes(tmp_path):
    app = create_app(make_settings(tmp_path, secure_cookies=True))
    with TestClient(app) as c:
        r = c.get("/bets/login")
        cookie = r.headers["set-cookie"]
        for part in ("HttpOnly", "Secure", "Path=/bets", "SameSite=strict"):
            assert part.lower() in cookie.lower()


def test_tampered_session_is_ignored(browser):
    cookie = browser.c.cookies.get("bets_session")
    browser.c.cookies.set("bets_session", cookie[:-3] + "AAA", path="/bets")
    assert browser.get("/bets/", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- CSRF / cross-origin

def test_writes_need_csrf_token_and_same_origin(browser, app):
    seed(app)
    ok = {"name": "Bet365", "kind": "bookmaker"}
    assert browser.c.post("/bets/accounts", data=ok, follow_redirects=False).status_code == 403          # no token
    assert browser.c.post("/bets/accounts", data={**ok, "csrf_token": "forged"}, follow_redirects=False).status_code == 403
    for headers in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/page"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
        r = browser.post("/bets/accounts", ok, headers=headers)
        assert r.status_code == 403, headers
    assert browser.post("/bets/accounts", ok, headers={"Origin": "https://example.com"}).status_code == 303
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM accounts WHERE name = 'Bet365'").fetchone()[0] == 1


def test_no_cors_headers(browser):
    r = browser.c.options("/bets/api/import", headers={"Origin": "https://evil.example",
                                                       "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-origin" not in browser.get("/bets/api/calc?stake=10&back_odds=2&lay_odds=2").headers


# ---------------------------------------------------------------- import API

def test_import_api_needs_login_csrf_and_confirmation(app, anon, browser):
    ids = seed(app)
    body = {"source": "shortcut", "event": "Spurs v Chelsea",
            "legs": [{"account": "Betfred", "side": "back", "stake": "10", "odds": "2.5"}]}
    assert anon.post("/bets/api/import", json=body).status_code == 401
    assert browser.c.post("/bets/api/import", json=body).status_code == 403                 # no CSRF header
    r = browser.c.post("/bets/api/import", json=body, headers={"X-CSRF-Token": browser.csrf})
    assert r.status_code == 201 and r.json()["status"] == "needs_confirmation"
    iid = r.json()["id"]
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM positions WHERE event = 'Spurs v Chelsea'").fetchone()[0] == 0
    page = browser.get(f"/bets/imports/{iid}")
    assert 'value="accept"' in page.text
    r = browser.post(f"/bets/imports/{iid}", {"decision": "accept"})
    assert r.status_code == 303 and "/bets/positions/" in r.headers["location"]
    bad = browser.c.post("/bets/api/import", json={"event": "x", "legs": [{"account": "Nope", "side": "back",
                                                                            "stake": "1", "odds": "2"}]},
                         headers={"X-CSRF-Token": browser.csrf})
    assert bad.status_code == 400 and "problems" in bad.json()
    assert ids


# ---------------------------------------------------------------- base path, redirects, leaks

def test_everything_stays_under_bets(app, browser):
    ids = seed(app)
    r = browser.get("/bets?x=1", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (308, "/bets/?x=1")
    assert browser.get("/bets/positions/", follow_redirects=False).status_code == 404        # no slash redirect
    assert browser.get("/other", follow_redirects=False).status_code == 404
    pages = ["/bets/", "/bets/positions", f"/bets/positions/{ids['position']}", "/bets/offers",
             f"/bets/offers/{ids['offer']}", "/bets/free-bets", "/bets/accounts", f"/bets/accounts/{ids['betfred']}",
             "/bets/ledger", "/bets/ledger?account=1", "/bets/calculator?stake=10&back_odds=4&lay_odds=4.2&commission=2",
             "/bets/quick-add", "/bets/imports", "/bets/settings", "/bets/audit", "/bets/positions?status=all"]
    for path in pages:
        page = browser.get(path)
        assert page.status_code == 200, path
        assert ESCAPES.findall(page.text) == [], (path, ESCAPES.findall(page.text)[:3])
        assert 'name="viewport"' in page.text
    for r in (browser.post(f"/bets/positions/{ids['position']}/settle", {"outcome": "back_lost"}),
              browser.post("/bets/free-bets", {"account_id": ids["betfred"], "face_value": "5"})):
        assert r.status_code == 303 and r.headers["location"].startswith("/bets/")


def test_no_internal_details_leak(app, browser, settings, monkeypatch):
    seed(app)
    secrets_ = [str(settings.data_dir), "/home/", "/opt/", "Traceback", "password_hash", "scrypt$"]
    for path in ("/bets/", "/bets/settings", "/bets/audit", "/bets/export/full.json", "/bets/nonexistent",
                 "/bets/positions/99999", "/bets/api/calc?stake=x"):
        text = browser.get(path).text
        for s in secrets_:
            assert s not in text, (path, s)
    from bets import metrics

    def boom(conn):
        raise RuntimeError("SECRET-DETAIL /home/someone/private")
    monkeypatch.setattr(metrics, "summary", boom)
    r = browser.get("/bets/")
    assert r.status_code == 500 and "SECRET" not in r.text and "Traceback" not in r.text


def test_security_headers(browser):
    for r in (browser.get("/bets/"), browser.get("/bets/static/design.css"), browser.get("/bets/nope")):
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
    assert browser.get("/bets/").headers["cache-control"] == "no-store"


def test_templates_use_no_inline_scripts_or_styles():
    import pathlib
    for p in pathlib.Path("bets/templates").glob("*.html"):
        text = p.read_text()
        assert "style=" not in text and "onclick" not in text and "<script>" not in text, p.name


# ---------------------------------------------------------------- pages and workflows

def test_dashboard_shows_figures(app, browser):
    seed(app)
    page = browser.get("/bets/").text
    for text in ("Realised profit", "Available to bet", "Up next", "Needs you", "Your money", "£130.00",
                 "Lay liability held", "£30.62", "Stakes in play", "Temporary funding out", "Free bets",
                 "Liverpool v Arsenal", "If back wins", "If lay wins", "Bet £10 Get £30", "checks pass",
                 "Recently settled", "View ledger"):
        assert text in page, text
    assert "Latest ledger movements" not in page and "Open positions" not in page


def test_dashboard_groups_identical_free_bet_expiries(app, browser):
    from bets import times
    ids = seed(app)
    soon = times.iso(times.now() + times.timedelta(days=3))
    with app.state.db.tx() as conn:
        for _ in range(4):
            services.create_free_bet(conn, account_id=ids["betfred"], face_value=500, expires_at=soon)
    page = browser.get("/bets/").text
    assert page.count("4 Betfred free bets") == 2          # once in Up next, once in the headline alert
    assert "Free bets expiring" in page and "Find a match" in page
    up_next = page.split("Up next", 1)[1].split('id="free-bets-held"', 1)[0]
    assert up_next.count('<b>Expires</b>') == 2             # the group of four, plus the 2099 token (Later)


def test_quick_add_preview_then_confirm(app, browser):
    ids = seed(app)
    form = {"kind": "qualifying", "event": "Spurs v Chelsea", "selection": "Spurs", "back_account": ids["betfred"],
            "back_stake": "10", "back_odds": "4.0", "lay_account": ids["smarkets"], "lay_odds": "4.2", "commission": "2"}
    with app.state.db.read() as conn:
        before = conn.execute("SELECT count(*) FROM positions").fetchone()[0]
    preview = browser.post("/bets/quick-add/preview", form)
    assert preview.status_code == 200
    for text in ("£9.57", "£30.62", "−£0.62", "Save position"):
        assert text in preview.text, text
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == before    # nothing saved yet
    r = browser.post("/bets/quick-add/confirm", {**form})
    assert r.status_code == 303
    page = browser.get(r.headers["location"])
    assert "Spurs v Chelsea" in page.text and "Ledger effect" in page.text
    bad = browser.post("/bets/quick-add/preview", {**form, "back_stake": "10.555"})
    assert bad.status_code == 400 and "two decimal places" in bad.text


def test_calculator_api(browser):
    r = browser.get("/bets/api/calc?kind=qualifying&stake=10&back_odds=4&lay_odds=4.2&commission=2").json()
    assert r["lay_stake"] == "9.57" and r["liability"] == "30.62" and r["expected"] == "-0.62"
    assert browser.get("/bets/api/calc?stake=10&back_odds=1&lay_odds=2").status_code == 400


def test_accounts_transfer_and_reconcile_pages(app, browser):
    ids = seed(app)
    r = browser.post("/bets/transfers", {"from_id": ids["lloyds"], "to_id": ids["smarkets"], "amount": "100"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    page = browser.get("/bets/accounts").text
    assert "Temporary funding out" in page and "£100.00" in page
    r = browser.post(f"/bets/accounts/{ids['betfred']}/reconcile", {"actual": "39.85", "note": "checked app"})
    assert r.status_code == 303
    assert "Reconciliation" in browser.get(f"/bets/accounts/{ids['betfred']}").text


def test_ledger_filters_and_running_balance(app, browser):
    ids = seed(app)
    page = browser.get(f"/bets/ledger?account={ids['betfred']}").text
    assert "Balance" in page and "Opening balance" in page and "Stake" in page
    assert "Transfer" not in browser.get("/bets/ledger?class=pnl").text.split("<tbody>")[1]


def test_settlement_offer_and_free_bet_pages(app, browser):
    ids = seed(app)
    assert browser.post(f"/bets/positions/{ids['position']}/settle", {"outcome": "back_won"}).status_code == 303
    page = browser.get(f"/bets/positions/{ids['position']}").text
    assert "Actual result" in page and "−£0.62" in page
    assert "Awaiting reward" in browser.get(f"/bets/offers/{ids['offer']}").text
    fb = browser.get("/bets/free-bets").text
    assert "£10.00" in fb and "SNR" in fb and "Use" in fb


def test_exports(app, browser):
    seed(app)
    csv_text = browser.get("/bets/export/ledger.csv").text
    assert csv_text.startswith("id,at,account") and "Betfred" in csv_text and "-10.00" in csv_text
    pos = browser.get("/bets/export/positions.csv").text
    assert "Liverpool v Arsenal" in pos and "back Betfred 10.00@4.00" in pos
    data = json.loads(browser.get("/bets/export/full.json").text)
    assert data["money_unit"] == "pence" and data["tables"]["accounts"]
    assert "password" not in json.dumps(data).lower()
    ics = browser.get("/bets/calendar.ics")
    assert ics.headers["content-type"].startswith("text/calendar") and "BEGIN:VEVENT" in ics.text
    for figure in ("£120", "£80", "£40", "£30.62", "balance", "profit"):   # titles only, no money figures
        assert figure not in ics.text


def test_csv_neutralises_spreadsheet_formulas(app, browser):
    with app.state.db.tx() as conn:
        services.create_position(conn, event="=HYPERLINK(\"http://x\")")
    assert "'=HYPERLINK" in browser.get("/bets/export/positions.csv").text


def test_service_binds_loopback_only():
    assert Settings().host == "127.0.0.1"


def test_first_login_can_be_created_before_the_data_directory_exists(tmp_path):
    from bets.auth import load_credentials, write_credentials
    target = tmp_path / "fresh" / "var" / "auth.json"
    write_credentials(target, "admin", "a long enough password")
    assert load_credentials(target)["username"] == "admin"
    assert (target.parent.stat().st_mode & 0o777) == 0o700
