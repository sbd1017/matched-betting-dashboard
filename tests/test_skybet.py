"""Deliberately unhedged qualifiers, the single offer lifecycle, reward triggers, UK-local 'when it happened' times and
superseded held candidates -- with the real Sky Bet welcome offer as the regression case."""
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from bets import brief, imports, issues, metrics, offer_state, outlook, readapi, services, times

from .test_assistant import token  # noqa: F401 (fixture)
from .test_bulk_trusted import trust

API = "/bets/api/assistant"


@pytest.fixture
def api(app, token):  # noqa: F811
    with TestClient(app, base_url="http://testserver") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def change(api, name, rid, **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": rid})
    return r.status_code, r.json()


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


@pytest.fixture
def sky(app):
    """Sky Bet: £5 deposit from First Direct; offer #: first bet £0.05+ at 2.00+, 3 x £10 SNR, 30 days, credited when
    the qualifying bet is accepted."""
    with app.state.db.tx() as conn:
        ids = {"fd": services.create_account(conn, name="First Direct", kind="bank"),
               "sky": services.create_account(conn, name="Sky Bet", kind="bookmaker"),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=0,
                                                   opening_balance=10000),
               "betfred": services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=5000)}
        services.create_transfer(conn, from_id=ids["fd"], to_id=ids["sky"], amount=500,
                                 capital_type="bankroll_contribution")
        ids["offer"] = services.create_offer(
            conn, title="£30 in Free Bets when you place any bet", account_id=ids["sky"], reward_value=3000,
            requirement="Deposit £5; FIRST bet £0.05+ single/each-way at 2.00+",
            terms={"qualifying_stake_pence": 5, "min_odds": "2.00", "reward_count": 3, "reward_each_pence": 1000,
                   "reward_kind": "snr", "reward_expiry_days": 30, "reward_trigger": "bet_placed"})
    return ids


QUALIFIER = dict(event="Lyon v Crystal Palace", selection="Crystal Palace", market="Full Time Result", stake="0.05",
                 odds="2.80", reason="Qualifier stake only £0.05; deliberately not laid because maximum loss is 5p.",
                 evidence=[{"kind": "bet_slip", "description": "Sky Bet: bet placed, £0.05 @ 9/5",
                            "facts": {"stake": "0.05", "odds": "9/5"}}])


def state(app, oid):
    with app.state.db.read() as conn:
        o = services.row(conn, "offers", oid)
        return o, offer_state.derive(conn, o)


# ================================================================ 13. Sky Bet: the exact real case

def test_skybet_unhedged_qualifier_then_immediate_reward(app, sky, api, browser):
    with app.state.db.read() as conn:
        assert services.balance(conn, sky["sky"]) == 500
        p0 = metrics.summary(conn).profit_lifetime
    code, r = change(api, "record_unhedged_qualifier", "sky-1", offer_id=sky["offer"], **QUALIFIER)
    assert code == 200 and r["status"] == "applied", r                     # not held for lack of a lay
    res = r["result"]
    pid = res["position_id"]
    assert res["hedge"] == "intentionally_unhedged" and res["accepted_exposure"] == "0.05"
    assert res["hedge_advice"]["recommendation"] == "HEDGE_NOT_RECOMMENDED"
    with app.state.db.read() as conn:
        assert services.balance(conn, sky["sky"]) == 495                   # £4.95 cash
        assert metrics.unsettled_back_stakes(conn, sky["sky"]) == 5        # + £0.05 open stake
        assert metrics.summary(conn).profit_lifetime == p0
        assert conn.execute("SELECT count(*) FROM legs WHERE position_id = ? AND side = 'lay'", (pid,)).fetchone()[0] == 0
        pos = services.row(conn, "positions", pid)
        assert (pos["hedge_strategy"], pos["accepted_exposure_pence"], pos["offer_id"]) == ("intentionally_unhedged", 5,
                                                                                           sky["offer"])
        assert not [i for i in issues.collect(conn) if i["key"].startswith(("incomplete:", "offer:status"))]
        assert conn.execute("SELECT count(*) FROM evidence WHERE entity = 'position' AND entity_id = ?", (pid,)).fetchone()[0] == 1
    o, life = state(app, sky["offer"])
    assert o["status"] == life["status"] == "awaiting_reward" and life["stage"] == "reward_expected"
    assert life["qualifier_required"] is False and "reward" in life["next_action"]
    exp = res["expected_reward"]
    assert len(exp) == 1 and exp[0]["expected"] == "3 x £10.00 SNR" and exp[0]["counts_in_profit"] is False
    # every reader agrees
    got = api.get(f"{API}/read/offer", params={"id": sky["offer"]}).json()["data"]
    assert (got["status"], got["stage"], got["qualifier_required"]) == ("awaiting_reward", "reward_expected", False)
    listed = next(x for x in api.get(f"{API}/read/offers").json()["data"] if x["id"] == sky["offer"])
    assert listed["status"] == "awaiting_reward"
    b = api.get(f"{API}/read/operating_brief").json()["data"]
    bo = next(x for x in b["offers"] if x["id"] == sky["offer"])
    assert (bo["status"], bo["stage"], bo["qualifier_required"]) == ("awaiting_reward", "reward_expected", False)
    acts = api.get(f"{API}/read/next_actions").json()["data"]
    assert not any("Plan the qualifying bet" in a["recommended_action"] for a in acts)
    assert not any(a["kind"] == "hedge" or "lay" in a["title"].lower() for a in acts if "Sky" in a["title"])
    with app.state.db.read() as conn:                                       # Still to come: reward at SNR retention
        stc = [x for x in outlook.still_to_come_breakdown(conn)["reward_items"] if x["offer_id"] == sky["offer"]]
        assert stc and stc[0]["reward_value"] == 3000 and stc[0]["worth"] < 3000
    # a retry and a second report never duplicate anything
    assert change(api, "record_unhedged_qualifier", "sky-1", offer_id=sky["offer"], **QUALIFIER)[1]["duplicate"]
    assert change(api, "record_unhedged_qualifier", "sky-2", offer_id=sky["offer"], **QUALIFIER)[1]["status"] == "already_done"
    # Sky Bet credits the £30 straight away: the same call with reward_credited records and links it
    code, r2 = change(api, "record_unhedged_qualifier", "sky-3", offer_id=sky["offer"], reward_credited=True, **QUALIFIER)
    assert r2["status"] == "applied", r2
    with app.state.db.read() as conn:
        toks = [dict(t) for t in conn.execute("SELECT * FROM free_bets WHERE offer_id = ? AND deleted_at IS NULL",
                                              (sky["offer"],))]
        assert len(toks) == 3 and all(t["face_value_pence"] == 1000 and t["kind"] == "snr" and t["expected_reward_id"]
                                      for t in toks)
        life_days = (times.parse(toks[0]["expires_at"]) - times.parse(toks[0]["credited_at"])).days
        assert life_days == 30
        assert conn.execute("SELECT count(*) FROM expected_rewards WHERE offer_id = ?", (sky["offer"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM positions WHERE offer_id = ?", (sky["offer"],)).fetchone()[0] == 1
    o, life = state(app, sky["offer"])
    assert o["status"] == life["status"] == "reward_received" and life["stage"] == "reward_credited"
    assert o["reward_credited"] == 1
    assert change(api, "record_unhedged_qualifier", "sky-4", offer_id=sky["offer"], reward_credited=True,
                  **QUALIFIER)[1]["status"] == "already_done"
    assert change(api, "mark_reward_received", "sky-5",
                  expected_reward_id=toks[0]["expected_reward_id"])[1]["status"] == "already_done"
    acts = api.get(f"{API}/read/next_actions").json()["data"]
    assert not any("Plan the qualifying bet" in a["recommended_action"] or "Check the bookmaker for the reward" in
                   a["recommended_action"] for a in acts)
    page = browser.get(f"/bets/positions/{pid}").text
    assert "Intentionally unhedged" in page and "Accepted exposure: £0.05" in page and "Incomplete" not in page
    assert "Unhedged by design" in browser.get("/bets/positions").text
    assert "Lyon v Crystal Palace" not in browser.get("/bets/operations").text.split("Problems")[1].split("Completed")[0]


@pytest.mark.parametrize("outcome,cash,profit", [("back_lost", 495, -5), ("back_won", 509, 9)])
def test_unhedged_qualifier_settles_normally(app, sky, api, outcome, cash, profit):
    code, r = change(api, "record_unhedged_qualifier", "q-1", offer_id=sky["offer"], **QUALIFIER)
    pid = r["result"]["position_id"]
    with app.state.db.read() as conn:
        p0 = metrics.summary(conn).profit_lifetime
    code, s = change(api, "settle_position", "q-2", position_id=pid, outcome=outcome)
    assert s["status"] == "applied", s
    with app.state.db.read() as conn:
        assert services.balance(conn, sky["sky"]) == cash                 # lost: £4.95; won: £4.95 + £0.14 return
        assert metrics.unsettled_back_stakes(conn, sky["sky"]) == 0
        assert metrics.summary(conn).profit_lifetime - p0 == profit
    o, life = state(app, sky["offer"])
    assert o["status"] == life["status"] == "awaiting_reward"              # bet_placed trigger: settling changes nothing


def test_reward_reported_separately_reconciles_without_duplicates(app, sky, api):
    change(api, "record_unhedged_qualifier", "r-1", offer_id=sky["offer"], **QUALIFIER)
    with app.state.db.tx() as conn:                                        # recorded another way first
        services.create_free_bets(conn, count=3, account_id=sky["sky"], face_value=1000, kind="snr",
                                  offer_id=sky["offer"], expires_at=at(24 * 30))
    o, life = state(app, sky["offer"])
    assert life["status"] == o["status"] == "reward_received"
    with app.state.db.read() as conn:
        e = conn.execute("SELECT * FROM expected_rewards WHERE offer_id = ?", (sky["offer"],)).fetchall()
        assert len(e) == 1 and e[0]["status"] == "received"
        assert conn.execute("SELECT count(*) FROM free_bets WHERE offer_id = ?", (sky["offer"],)).fetchone()[0] == 3


# ================================================================ 14. a normal qualifier still needs its hedge

def test_normal_qualifier_without_a_lay_is_still_flagged(app, sky, api, browser):
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Bet £10 Get £10", account_id=sky["betfred"],
                                    terms={"qualifying_stake_pence": 1000, "reward_count": 1, "reward_each_pence": 1000,
                                           "reward_kind": "snr"})
        pid = services.create_position(conn, event="Arsenal v Leeds", selection="Arsenal", offer_id=oid, legs=[
            {"account_id": sky["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.0"}])
        assert any(i["key"] == f"incomplete:{pid}" for i in issues.collect(conn))
        assert services.row(conn, "offers", oid)["status"] == "qualifying"
    trust(browser, "position")
    body = api.post(f"{API}/candidates/position", json={"event": "Chelsea v Spurs", "selection": "Chelsea",
                                                        "offer": "Bet £10 Get £10", "legs": [
        {"account": "Betfred", "side": "back", "stake": "10", "odds": "2.1"}]}).json()
    assert body["auto_confirmed"] is False and "needs the lay" in body["held_for_review"]
    # explicit, but above the limit: waits for the user
    big = api.post(f"{API}/candidates/position", json={"event": "Chelsea v Spurs", "selection": "Chelsea",
                                                       "hedge_strategy": "intentionally_unhedged",
                                                       "unhedged_reason": "felt like it", "legs": [
        {"account": "Betfred", "side": "back", "stake": "10", "odds": "2.1"}]}).json()
    assert big["auto_confirmed"] is False and "above your £0.50 limit" in big["held_for_review"]
    with app.state.db.tx() as conn:
        oid2 = services.create_offer(conn, title="Bet £10 Get £5", account_id=sky["betfred"])
    code, r = change(api, "record_unhedged_qualifier", "big-1", offer_id=oid2, event="Chelsea v Spurs", stake="10",
                     odds="2.1", reason="not laying")
    assert code == 202 and r["status"] == "needs_confirmation", r
    code, v = change(api, "create_bet_plan", "pl-1", event="X v Y", selection="X", bookmaker="Betfred", back_stake="10",
                     back_odds="2.0", exchange="Smarkets", lay_odds="2.04", offer_id=oid)
    adv = change(api, "validate_bet_plan", "pl-2", plan_id=v["result"]["plan_id"])[1]["result"]["hedge_advice"]
    assert adv["recommendation"] == "HEDGE"


def test_trusted_mode_accepts_a_tiny_deliberately_unhedged_qualifier(app, sky, api, browser):
    trust(browser, "position")
    body = api.post(f"{API}/candidates/position", json={
        "event": "Lyon v Crystal Palace", "selection": "Crystal Palace", "offer": sky["offer"],
        "hedge_strategy": "intentionally_unhedged", "unhedged_reason": "5p qualifier", "legs": [
            {"account": "Sky Bet", "side": "back", "stake": "0.05", "odds": "9/5"}]}).json()
    assert body["auto_confirmed"] is True, body
    o, life = state(app, sky["offer"])
    assert o["status"] == life["status"] == "awaiting_reward"
    bad = api.post(f"{API}/candidates/position", json={
        "event": "A v B", "hedge_strategy": "intentionally_unhedged", "legs": [
            {"account": "Sky Bet", "side": "back", "stake": "0.05", "odds": "3"}]})
    assert bad.status_code == 400 and "unhedged_reason" in str(bad.json())


def test_hedge_advice_in_validation_for_a_tiny_qualifier(app, sky, api):
    code, v = change(api, "create_bet_plan", "t-1", event="Lyon v Crystal Palace", selection="Crystal Palace",
                     bookmaker="Sky Bet", back_stake="0.05", back_odds="2.8", exchange="Smarkets", lay_odds="2.9",
                     offer_id=sky["offer"])
    adv = change(api, "validate_bet_plan", "t-2", plan_id=v["result"]["plan_id"])[1]["result"]["hedge_advice"]
    assert adv["recommendation"] == "HEDGE_NOT_RECOMMENDED" and "£0.05" in adv["reason"]


# ================================================================ 3. one lifecycle: nobody can make it disagree

def test_offer_status_cannot_be_set_out_of_step(app, sky, api):
    code, r = change(api, "update_offer", "u-1", offer_id=sky["offer"], status="awaiting_reward")
    assert code == 400 and "follows its records" in r["errors"][0]
    o, life = state(app, sky["offer"])
    assert o["status"] == life["status"] == "not_started" and life["stage"] == "qualifier_required"
    code, r = change(api, "update_offer_progress", "u-2", offer_id=sky["offer"], qualified=True)
    o, life = state(app, sky["offer"])                                     # the bookmaker shows it done: a fact
    assert o["status"] == life["status"] == "awaiting_reward" and not life["qualifier_required"]
    code, r = change(api, "update_offer", "u-3", offer_id=sky["offer"], status="abandoned", reason="changed my mind")
    assert r["status"] == "applied" and state(app, sky["offer"])[1]["stage"] == "void"


def test_settled_trigger_offer_waits_for_settlement(app, sky, api):
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Bet £10 Get £10", account_id=sky["betfred"],
                                    terms={"reward_count": 1, "reward_each_pence": 1000, "reward_kind": "snr"})
        pid = services.create_position(conn, event="A v B", selection="A", offer_id=oid, legs=[
            {"account_id": sky["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.0"},
            {"account_id": sky["smarkets"], "side": "lay", "stake_pence": 1000, "odds": "2.02"}])
        st = offer_state.derive(conn, services.row(conn, "offers", oid))
        assert (st["status"], st["stage"]) == ("qualifying", "qualifier_placed")
        services.quick_settle(conn, pid, "back_lost")
        st = offer_state.derive(conn, services.row(conn, "offers", oid))
        assert (st["status"], services.row(conn, "offers", oid)["status"]) == ("awaiting_reward", "awaiting_reward")
        assert conn.execute("SELECT count(*) FROM expected_rewards WHERE offer_id = ?", (oid,)).fetchone()[0] == 1
        assert offer_state.out_of_step(conn) == []


# ================================================================ 10. UK local times

@pytest.mark.parametrize("value,expected", [
    ("2026-07-01T12:00", "2026-07-01T11:00:00Z"),           # BST
    ("2026-12-01T12:00", "2026-12-01T12:00:00Z"),           # GMT
    ("2026-03-29T00:30", "2026-03-29T00:30:00Z"),           # before the spring change (still GMT)
    ("2026-03-29T03:30", "2026-03-29T02:30:00Z"),           # after it (BST)
    ("2026-10-25T00:30", "2026-10-24T23:30:00Z"),           # before the autumn change (BST)
    ("2026-10-25T02:30", "2026-10-25T02:30:00Z"),           # after it (GMT)
    ("2026-10-15T11:29:00Z", "2026-10-15T10:29:00Z".replace("10:29", "11:29")),   # explicit UTC kept
    ("2026-10-15T11:29+01:00", "2026-10-15T10:29:00Z"),
])
def test_uk_local_conversion(value, expected):
    assert times.from_local_input(value) == expected


def test_a_date_alone_for_something_that_happened_today_means_now():
    fixed = datetime(2026, 10, 15, 10, 29, 30, tzinfo=timezone.utc)          # 11:29 BST
    with mock.patch("bets.times.now", return_value=fixed):
        assert times.from_local_input("2026-10-15", happened=True) == "2026-10-15T10:29:30Z"
        assert times.from_local_input("2026-10-15") == "2026-10-15T22:59:00Z"           # a deadline: end of day
        assert times.from_local_input("2026-10-14", happened=True) == "2026-10-14T22:59:00Z"
        assert times.from_local_input("2026-10-15T11:29", happened=True) == "2026-10-15T10:29:00Z"
        assert times.from_local_input("2026-10-15T11:35", happened=True) == "2026-10-15T10:29:30Z"  # clock skew
        assert times.from_local_input("2026-10-15T13:00", happened=True) == "2026-10-15T12:00:00Z"  # really later
        assert not times.in_future("2026-10-15T10:29:00Z") and times.in_future("2026-10-15T12:00:00Z")
    late = datetime(2026, 1, 15, 23, 50, tzinfo=timezone.utc)                # GMT, late evening
    with mock.patch("bets.times.now", return_value=late):
        assert times.from_local_input("2026-01-15", happened=True) == "2026-01-15T23:50:00Z"


def test_todays_deposit_is_not_held_as_future(app, sky, api, browser):
    trust(browser, "transfer")
    body = api.post(f"{API}/candidates/transfer", json={"from": "First Direct", "to": "Sky Bet", "amount": "5",
                                                        "capital_type": "permanent",
                                                        "at": times.local(times.now_iso(), "%Y-%m-%d")}).json()
    assert body["auto_confirmed"] is True and not body.get("held_for_review"), body
    now_minute = times.local(times.now_iso(), "%Y-%m-%dT%H:%M")
    body = api.post(f"{API}/candidates/transfer", json={"from": "First Direct", "to": "Sky Bet", "amount": "6",
                                                        "capital_type": "permanent", "at": now_minute}).json()
    assert body["auto_confirmed"] is True, body


# ================================================================ 11. a corrected resubmission supersedes the held one

def test_corrected_resubmission_supersedes_the_held_attempt(app, sky, api, browser):
    trust(browser, "transfer")
    held = api.post(f"{API}/candidates/transfer", json={"from": "First Direct", "to": "Sky Bet", "amount": "5",
                                                        "capital_type": "permanent", "at": "2099-01-01"}).json()
    assert "in the future" in held["held_for_review"]
    ok = api.post(f"{API}/candidates/transfer", json={"from": "First Direct", "to": "Sky Bet", "amount": "5",
                                                      "capital_type": "permanent"}).json()
    assert ok["auto_confirmed"] is True
    with app.state.db.read() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM import_candidates WHERE kind = 'transfer'")}
        first = [r for r in rows.values() if r["hold_reason"] and "superseded" in r["hold_reason"]]
        assert len(first) == 1 and first[0]["status"] == "rejected" and first[0]["decided_by"] == "system"
        assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'import' AND entity_id = ? AND action = "
                            "'superseded'", (first[0]["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'").fetchone()[0] == 0


def test_ambiguous_held_attempts_are_left_for_the_user(app, sky, api, browser):
    trust(browser, "transfer")
    for _ in range(2):
        api.post(f"{API}/candidates/transfer", json={"from": "First Direct", "to": "Sky Bet", "amount": "5",
                                                     "capital_type": "permanent", "at": "2099-01-01"})
    api.post(f"{API}/candidates/transfer", json={"from": "First Direct", "to": "Sky Bet", "amount": "5",
                                                 "capital_type": "permanent"})
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'").fetchone()[0] == 2


def test_held_qualifier_is_superseded_by_record_unhedged_qualifier(app, sky, api):
    body = api.post(f"{API}/candidates/position", json={
        "event": "Lyon v Crystal Palace", "selection": "Crystal Palace", "offer": sky["offer"], "legs": [
            {"account": "Sky Bet", "side": "back", "stake": "0.05", "odds": "9/5"}]}).json()
    assert body["status"] == "pending_confirmation"
    code, r = change(api, "record_unhedged_qualifier", "h-1", offer_id=sky["offer"], **QUALIFIER)
    assert r["result"]["superseded_candidates"]
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'").fetchone()[0] == 0


def test_reward_already_recorded_gets_its_expectation_after_the_fact(app, sky, api):
    """The live order of events: reward tokens recorded (one already used), the qualifier recorded afterwards."""
    with app.state.db.tx() as conn:
        toks = services.create_free_bets(conn, count=3, account_id=sky["sky"], face_value=1000, kind="snr",
                                         offer_id=sky["offer"], expires_at=at(24 * 30))
    code, r = change(api, "record_unhedged_qualifier", "late-1", offer_id=sky["offer"], **QUALIFIER)
    assert r["status"] == "applied", r
    with app.state.db.read() as conn:
        e = [dict(x) for x in conn.execute("SELECT * FROM expected_rewards WHERE offer_id = ?", (sky["offer"],))]
        assert len(e) == 1 and e[0]["status"] == "received" and e[0]["count"] == 3
        assert {x[0] for x in conn.execute("SELECT id FROM free_bets WHERE expected_reward_id = ?", (e[0]["id"],))} == set(toks)
        assert not [i for i in issues.collect(conn) if i["key"].startswith(("reward:", "incomplete:"))]
    o, life = state(app, sky["offer"])
    assert o["status"] == life["status"] == "reward_received"
