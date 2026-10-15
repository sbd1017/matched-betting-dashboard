"""Free-bet tokens chosen at confirmation (deterministic, auditable, historical repair) and settlement candidates."""
import json

import pytest

from bets import imports, metrics, services
from tests.test_assistant import API, api, post, token  # noqa: F401  (fixtures)
from tests.test_bulk_trusted import bulk, iid_of, one, trust


def rows(app, sql, *args):
    with app.state.db.read() as conn:
        return [tuple(r) for r in conn.execute(sql, args)]


@pytest.fixture
def book(app):
    with app.state.db.tx() as conn:
        ids = {"lloyds": services.create_account(conn, name="Lloyds", kind="bank"),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200,
                                                   opening_balance=10000),
               "betfred": services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=5000)}
        ids["offer"] = services.create_offer(conn, title="Bet £10 Get £50", account_id=ids["betfred"], status="complete")
    return ids


def tokens(app, book, n=5, status="available", expiries=None):
    out = []
    with app.state.db.tx() as conn:
        for i in range(n):
            fid = services.create_free_bet(conn, account_id=book["betfred"], face_value=1000, kind="snr",
                                           expires_at=(expiries or [None] * n)[i])
            if status == "used":
                services.update_free_bet(conn, fid, {"status": "used"}, note="marked used by hand")
            out.append(fid)
    return out


def fb_position(api, event, **extra):
    return post(api, "position", {"event": event, "selection": "X", "position_type": "free_bet_snr", "legs": [
        {"account": "Betfred", "side": "back", "stake": "10", "odds": "5.0", **extra},
        {"account": "Smarkets", "side": "lay", "odds": "5.2"}]})


# ---------------------------------------------------------------- tokens chosen when confirming

def test_five_pending_positions_take_five_different_tokens_deterministically(app, api, browser, book):
    ids = tokens(app, book, expiries=["2099-01-05T00:00:00Z", None, "2099-01-01T00:00:00Z", None, "2099-01-01T00:00:00Z"])
    subs = [fb_position(api, f"Match {i}").json() for i in range(5)]
    assert all(s["summary"]["legs"][0]["free_bet"] for s in subs)                      # intent, no token yet
    page = bulk(browser, "selected", [iid_of(app, s) for s in subs])
    assert "5 confirmed" in page.text
    linked = rows(app, "SELECT p.event, l.free_bet_id FROM legs l JOIN positions p ON p.id = l.position_id "
                       "WHERE l.side = 'back' ORDER BY p.id")
    # earliest expiry first (ties: oldest created / lowest id), no-expiry tokens last
    assert [t for _, t in linked] == [ids[2], ids[4], ids[0], ids[1], ids[3]]
    assert rows(app, "SELECT count(*) FROM free_bets WHERE status = 'available'") == [(0,)]
    assert "No free bets" in browser.get("/bets/free-bets?show=available").text      # gone from "to use"
    with app.state.db.read() as conn:
        audits = conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'free_bet' AND action = 'used'").fetchone()[0]
        assert audits == 5
        # free bets are not cash: no stake left Betfred
        assert services.balance(conn, book["betfred"]) == 5000


def test_explicit_token_wins_and_types_must_match(app, api, browser, book):
    ids = tokens(app, book, n=2)
    with app.state.db.tx() as conn:
        sr = services.create_free_bet(conn, account_id=book["betfred"], face_value=1000, kind="sr")
    r = post(api, "position", {"event": "Explicit", "position_type": "free_bet_snr", "legs": [
        {"account": "Betfred", "side": "back", "stake": "10", "odds": "4", "free_bet_id": ids[1]},
        {"account": "Smarkets", "side": "lay", "odds": "4.2"}]}).json()
    browser.post(f"/bets/imports/{iid_of(app, r)}", {"decision": "accept"})
    assert rows(app, "SELECT free_bet_id FROM legs WHERE side = 'back'") == [(ids[1],)]
    sr_pos = post(api, "position", {"event": "SR one", "position_type": "free_bet_sr", "legs": [
        {"account": "Betfred", "side": "back", "stake": "10", "odds": "4"}]}).json()
    browser.post(f"/bets/imports/{iid_of(app, sr_pos)}", {"decision": "accept"})
    assert rows(app, "SELECT free_bet_id FROM legs l JOIN positions p ON p.id = l.position_id WHERE p.event = 'SR one'") == [(sr,)]


def test_no_suitable_token_holds_the_position(app, api, browser, book):
    r = fb_position(api, "No token").json()
    assert any("no unused £10.00 SNR free bet" in w for w in r["warnings"])
    resp = browser.post(f"/bets/imports/{iid_of(app, r)}", {"decision": "accept"})
    assert "err=" in resp.headers["location"] and rows(app, "SELECT count(*) FROM positions") == [(0,)]
    trust(browser, "position")
    held = fb_position(api, "No token 2").json()
    assert held["status"] == "pending_confirmation" and "no unused" in held["held_for_review"]
    trust(browser, enabled=False)


def test_qualifying_position_with_a_free_bet_must_say_snr_or_sr(app, api, book):
    tokens(app, book, n=1)
    r = post(api, "position", {"event": "Q", "legs": [{"account": "Betfred", "side": "back", "stake": "10",
                                                        "odds": "3", "use_free_bet": True}]})
    assert r.status_code == 400 and "SNR or SR" in json.dumps(r.json())


def test_pending_token_is_confirmed_before_the_position_using_it(app, api, browser, book):
    tok = post(api, "free_bet", {"bookmaker": "Betfred", "value": "10", "free_bet_type": "snr"}).json()
    pos = fb_position(api, "Uses pending token").json()
    assert any("waits for the pending free bet" in w for w in pos["warnings"])
    page = bulk(browser, "selected", [iid_of(app, pos), iid_of(app, tok)])
    assert "2 confirmed" in page.text
    assert rows(app, "SELECT count(*) FROM legs WHERE free_bet_id IS NOT NULL") == [(1,)]


# ---------------------------------------------------------------- historical repair (tokens marked used by hand)

def test_historical_positions_use_the_tokens_marked_used_by_hand(app, api, browser, book):
    ids = tokens(app, book, status="used")
    plain_try = fb_position(api, "Old match 0").json()                                 # without saying so: held
    assert any("no unused" in w for w in plain_try["warnings"])
    subs = [fb_position(api, f"Old match {i}", token_marked_used=True).json() for i in range(1, 6)]
    assert all(s["summary"]["legs"][0]["historical_token"] for s in subs)
    trust(browser, "position")
    held = fb_position(api, "Old match 6", token_marked_used=True).json()
    assert "historical repair" in held["held_for_review"]                              # never automatic
    trust(browser, enabled=False)
    page = bulk(browser, "selected", [iid_of(app, s) for s in subs])
    assert "5 confirmed" in page.text
    assert [t for (t,) in rows(app, "SELECT l.free_bet_id FROM legs l JOIN positions p ON p.id = l.position_id "
                                    "WHERE l.side = 'back' ORDER BY p.id")] == ids     # oldest first, one each
    assert rows(app, "SELECT DISTINCT status FROM free_bets") == [("used",)]
    with app.state.db.read() as conn:
        linked = conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'free_bet' AND action = 'linked'").fetchone()[0]
        assert linked == 5 and services.balance(conn, book["betfred"]) == 5000         # no balance change


def test_link_an_existing_bet_to_its_token_and_the_token_to_its_offer(app, browser, book):
    (fid,) = tokens(app, book, n=1, status="used")
    with app.state.db.tx() as conn:                       # an old free-bet position recorded without its token
        pid = services.create_position(conn, event="Recorded before linking", kind="free_bet_snr", legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0"}])
        services.quick_settle(conn, pid, "back_won", at="2026-09-30T20:00:00Z")
        before = services.balance(conn, book["betfred"])
    assert before == 5000 - 1000 + 5000                    # wrongly treated as a cash bet: stake out, £50 back
    page = browser.get(f"/bets/positions/{pid}").text
    assert "Link the free bet it used" in page and "marked used by hand" in page
    leg = rows(app, "SELECT id FROM legs WHERE position_id = ?", pid)[0][0]
    r = browser.post(f"/bets/legs/{leg}/free-bet", {"free_bet_id": fid, "note": "historical"})
    assert "ok=" in r.headers["location"]
    with app.state.db.read() as conn:
        assert services.balance(conn, book["betfred"]) == 5000 + 4000                  # SNR: £40 winnings, no stake
        assert conn.execute("SELECT free_bet_id FROM legs WHERE id = ?", (leg,)).fetchone()[0] == fid
    # token -> offer, through the Free bets page (any status of offer, same bookmaker only)
    r = browser.post(f"/bets/free-bets/{fid}", {"offer_id": book["offer"], "status": "used"})
    assert "ok=" in r.headers["location"]
    chain = rows(app, "SELECT o.title, f.id, l.id, p.event FROM free_bets f JOIN offers o ON o.id = f.offer_id "
                      "JOIN legs l ON l.free_bet_id = f.id JOIN positions p ON p.id = l.position_id")
    assert chain == [("Bet £10 Get £50", fid, leg, "Recorded before linking")]          # offer -> token -> leg -> position
    with app.state.db.tx() as conn:
        other = services.create_account(conn, name="Coral", kind="bookmaker")
        coral_offer = services.create_offer(conn, title="Coral thing", account_id=other)
    assert "err=" in browser.post(f"/bets/free-bets/{fid}", {"offer_id": coral_offer}).headers["location"]


# ---------------------------------------------------------------- settlement candidates

@pytest.fixture
def placed(app, book):
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Qualifier", account_id=book["betfred"], status="qualifying")
        pid = services.create_position(conn, event="Moldova vs Faroe Islands", selection="Draw", offer_id=oid, legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "3.00"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 968, "odds": "3.10"}])
    return pid


def settle(api, **data):
    return post(api, "settlement", {"settled_at": "2026-09-28T21:00", **data})


def test_settle_by_number_previews_then_confirms_with_existing_functions(app, api, browser, placed):
    r = settle(api, position=placed, outcome="back_lost", reported_net_result="-0.49")
    body = r.json()
    assert r.status_code == 201, body
    s = body["summary"]
    assert [(l["side"], l["result"], l["amount"]) for l in s["legs"]] == [("back", "lost", "0.00"), ("lay", "won", "9.68")]
    assert s["net_result"] == "-0.51" and any("differs from the calculated" in w for w in body["warnings"])
    assert "awaiting reward" in s["offer_change"]
    page = browser.get(f"/bets/imports/{iid_of(app, body)}").text
    assert "Net result for the position" in page and "−£0.51" in page and "reopened" in page
    with app.state.db.read() as conn:
        assert metrics.summary(conn).profit_lifetime == 0                               # nothing yet
    browser.post(f"/bets/imports/{iid_of(app, body)}", {"decision": "accept"})
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM positions WHERE id = ?", (placed,)).fetchone()[0] == "settled"
        assert services.position_pnl(conn, placed) == -51 and metrics.summary(conn).profit_lifetime == -51
        assert conn.execute("SELECT status FROM offers WHERE title = 'Qualifier'").fetchone()[0] == "awaiting_reward"
        leg = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'back'", (placed,)).fetchone()[0]
        services.settle_leg(conn, leg, "pending", note="reopened")                       # Reopen still works
        assert conn.execute("SELECT status FROM positions WHERE id = ?", (placed,)).fetchone()[0] == "open"


def test_settle_by_event_needs_exactly_one_match(app, api, book, placed):
    assert settle(api, event="Moldova vs Faroe Islands", outcome="back_won").status_code == 201
    with app.state.db.tx() as conn:
        services.create_position(conn, event="Moldova vs Faroe Islands", selection="Moldova", legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 500, "odds": "2"}])
    amb = settle(api, event="Moldova vs Faroe Islands", outcome="back_won")
    assert amb.status_code == 400 and "2 open positions match" in json.dumps(amb.json())
    assert settle(api, event="Moldova vs Faroe Islands", selection="Draw", outcome="back_won").status_code == 201
    assert "no open position matches" in json.dumps(settle(api, event="Nobody v Nothing", outcome="void").json())


@pytest.mark.parametrize("data,message", [
    ({"outcome": "back_won", "settled_at": ""}, "settled_at: missing"),
    ({}, "never guessed"),
    ({"outcome": "back_won", "back_result": "won"}, "not both"),
    ({"back_result": "won"}, "lay_result: missing"),
    ({"back_result": "cashout"}, "back_amount: missing"),
    ({"outcome": "back_won", "back_amount": "5"}, "only for a cashout"),
])
def test_settlement_is_never_guessed(app, api, placed, data, message):
    r = settle(api, position=placed, **data)
    assert r.status_code == 400 and message in json.dumps(r.json()), r.json()


def test_settlement_warnings_and_trusted_holds(app, api, browser, placed):
    trust(browser, "settlement")
    future = settle(api, position=placed, outcome="back_won", settled_at="2099-01-01").json()
    assert "in the future" in future.get("held_for_review", ""), future
    cash = settle(api, position=placed, back_result="cashout", back_amount="12", lay_result="lost").json()
    assert "cash-out" in cash["held_for_review"]
    mismatch = settle(api, position=placed, outcome="back_won", reported_back_return="31").json()
    assert "reported return £31.00 differs" in mismatch["held_for_review"]
    clean = settle(api, position=placed, outcome="back_won", reported_back_return="30").json()
    assert clean["status"] == "confirmed" and clean["auto_confirmed"] is True
    again = settle(api, position=placed, outcome="back_won").json()                      # already settled now
    assert "already settled" in json.dumps(again)
    trust(browser, enabled=False)


def test_settlement_before_the_bet_was_placed(app, api, book):
    with app.state.db.tx() as conn:
        pid = services.create_position(conn, event="Dated", selection="A", legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 500, "odds": "2",
             "placed_at": "2026-09-20T12:00:00Z"}])
    body = settle(api, position=pid, outcome="back_won", settled_at="2026-09-19").json()
    assert any("before the bet was placed" in w for w in body["warnings"])


def test_backfill_position_and_its_result_together(app, api, browser, book):
    tokens(app, book, n=1, status="used")
    pos = fb_position(api, "Backfilled match", token_marked_used=True, placed_at="2026-09-29T19:00").json()
    res = settle(api, event="Backfilled match", selection="X", outcome="back_won", settled_at="2026-09-29T21:00").json()
    assert any("waits for the pending position" in w for w in res["warnings"])
    page = bulk(browser, "selected", [iid_of(app, res), iid_of(app, pos)])               # any order
    assert "2 confirmed" in page.text
    with app.state.db.read() as conn:
        pid = conn.execute("SELECT id FROM positions WHERE event = 'Backfilled match'").fetchone()[0]
        assert conn.execute("SELECT status FROM positions WHERE id = ?", (pid,)).fetchone()[0] == "settled"
        # SNR free bet won at 5.0: £40 winnings; lay of £7.69 at 5.2 lost: liability £32.30 -> net +£7.70
        assert services.position_pnl(conn, pid) == 4000 - conn.execute(
            "SELECT -settled_amount_pence FROM legs WHERE position_id = ? AND side = 'lay'", (pid,)).fetchone()[0]
        assert metrics.summary(conn).free_bets_count == 0
