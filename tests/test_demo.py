"""The public demo: made-up records built through the app's own services (demo/seed.py) and the open, no sign-in
wrapper around the app (demo/serve.py)."""
import re

import pytest
from fastapi.testclient import TestClient

from bets import issues, metrics, offer_state, services
from bets.config import Settings
from bets.db import Database
from demo import seed
from demo.serve import create_demo_app

TODAY = "2026-10-20"


def build(path, days=70, today=TODAY):
    assert seed.main(["--data-dir", str(path), "--today", today, "--days", str(days)]) == 0
    return Database(path / "bets.sqlite3")


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    return build(tmp_path_factory.mktemp("demo") / "var")


def test_records_balance_and_follow_the_rules(demo_db):
    with demo_db.read() as conn:
        s = metrics.summary(conn)
        failed = {c["name"] for c in s.checks if not c["ok"]}
        assert failed <= {"No open positions for events that have started"}      # the one left for the visitor
        assert s.profit_lifetime > 0 and s.free_bets_count > 0
        assert not offer_state.out_of_step(conn)
        assert not [a for a in metrics.account_rows(conn) if a["kind"] != "bank" and a["balance"] < 0]
        one = lambda sql: conn.execute(sql).fetchone()[0]
        assert one("SELECT count(*) FROM legs l JOIN positions p ON p.id = l.position_id WHERE l.side = 'back' "
                   "AND l.free_bet_id IS NULL AND NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.posting_key = "
                   "'leg:' || l.id || ':stake' AND e.superseded_at IS NULL)") == 0
        assert one("SELECT count(*) FROM exposure_scenarios") > 0                   # bet builders have outcome tables
        assert one("SELECT count(*) FROM bet_plans WHERE status = 'ready_to_place'") + \
            one("SELECT count(*) FROM bet_plans WHERE status = 'bookmaker_placed'") >= 1
        for kind, lo, hi in (("qualifying", -200, 10),):
            for (pid,) in conn.execute("SELECT id FROM positions WHERE kind = ? AND status = 'settled'", (kind,)):
                assert lo <= services.position_pnl(conn, pid) <= hi
        names = {r[0] for r in conn.execute("SELECT name FROM accounts")}
        assert {"Bet365", "Smarkets", "Betfair Exchange"} <= names
        kinds = {i["kind"] for i in issues.collect(conn)}
        assert {"Started", "Reward", "Withdrawal", "Balance"} <= kinds
        assert services.get_setting(conn, "demo") == {"seeded_for": TODAY}


def test_same_day_same_records(tmp_path):
    def fingerprint(db):
        with db.read() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT at, account_id, amount_pence, category FROM ledger_entries ORDER BY id")]
    assert fingerprint(build(tmp_path / "a", days=40)) == fingerprint(build(tmp_path / "b", days=40))


def test_never_adds_to_an_existing_database(tmp_path, demo_db):
    path = tmp_path / "var"
    build(path, days=30)
    assert seed.main(["--data-dir", str(path), "--today", TODAY, "--days", "30"]) == 1


@pytest.fixture(scope="module")
def client(tmp_path_factory, demo_db):
    var = demo_db.path.parent
    s = Settings(data_dir=var, secure_cookies=False)
    with TestClient(create_demo_app(s), base_url="http://testserver") as c:
        yield c


def csrf(html):
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_open_without_sign_in_and_changes_still_need_the_token(client):
    r = client.get("/bets/")
    assert r.status_code == 200 and 'class="demo-banner"' in r.text and "/bets/logout" not in r.text
    assert client.get("/bets/login", follow_redirects=False).headers["location"] == "/bets/"
    assert client.post("/bets/settings/retention-policy", data={}, follow_redirects=False).status_code == 403
    token = csrf(client.get("/bets/settings").text)
    r = client.post("/bets/settings/retention-policy", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303


def test_outside_connections_and_shared_settings_are_off(client):
    assert client.get("/bets/api/assistant/schema").status_code == 404
    page = client.get("/bets/settings").text
    assert "Assistant Intake" not in page and "Currency" not in page and "Signed in as" not in page
    token = csrf(page)
    r = client.post("/bets/settings/currency", data={"csrf_token": token, "currency": "EUR"},
                    headers={"referer": "http://testserver/bets/settings"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/bets/settings?err=")
    assert client.get("/bets/demo.css").status_code == 200


def test_refuses_a_database_the_seed_did_not_build(tmp_path):
    s = Settings(data_dir=tmp_path / "var", secure_cookies=False)
    Database(s.db_path).migrate()
    with pytest.raises(SystemExit):
        create_demo_app(s)
