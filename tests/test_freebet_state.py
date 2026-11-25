"""What has happened to each free bet, worked out from its token, plan, bet and lays."""
import re
from datetime import timedelta

import pytest

from bets import freebet_state as fs, imports, ops, plans, readapi, services, times
from bets.db import MIGRATIONS, Database
from bets.services import UserError


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


@pytest.fixture
def ids(conn):
    out = {"mid": services.create_account(conn, name="Midnite", kind="bookmaker", opening_balance=5000),
           "bf": services.create_account(conn, name="Betfair Sportsbook", kind="bookmaker", opening_balance=5000),
           "sm": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=0, opening_balance=50000)}
    out["fb"] = services.create_free_bet(conn, account_id=out["mid"], face_value=500, expires_at=at(12))
    return out


def state(conn, fid):
    return fs.derive(conn, services.row(conn, "free_bets", fid))


def plan(conn, ids, fb=None, **kw):
    data = {"event": "Arsenal v Spurs", "selection": "Draw", "event_at": at(6), "free_bet_id": fb or ids["fb"],
            "back_odds": "5.0", "exchange": "Smarkets", "lay_odds": "5.2", **kw}
    return plans.create(conn, data)


def bet(conn, ids, fbs, lay=385, lay_odds="5.2", odds="5.0", account="mid", event="Arsenal v Spurs", **kw):
    legs = [dict(account_id=ids[account], side="back", stake_pence=services.row(conn, "free_bets", f)["face_value_pence"],
                 odds=odds, free_bet_id=f) for f in fbs]
    if lay:
        legs.append(dict(account_id=ids["sm"], side="lay", stake_pence=lay, odds=lay_odds))
    return services.create_position(conn, event=event, selection="Draw", kind="free_bet_snr", event_at=at(6), legs=legs, **kw)


# ---------------------------------------------------------------- not placed / planned

def test_not_placed(conn, ids):
    st = state(conn, ids["fb"])
    assert (st["status"], st["label"], st["plan"], st["bet"]) == ("not_placed", "Not placed", None, None)


def test_planned_gives_the_event_and_the_plan(conn, ids):
    p = plan(conn, ids)
    st = state(conn, ids["fb"])
    start = fs.event_time(services.row(conn, "bet_plans", p)["event_at"])
    assert st["status"] == "planned" and st["plan"]["id"] == p
    assert st["plan"]["text"] == f"Planned: Arsenal v Spurs, {start}" and not st["plan"]["note"]


@pytest.mark.parametrize("status", ["draft", "validated", "ready_to_place"])
def test_every_state_before_placement_is_planned(conn, ids, status):
    p = plan(conn, ids)
    conn.execute("UPDATE bet_plans SET status = ? WHERE id = ?", (status, p))
    assert state(conn, ids["fb"])["status"] == "planned"


def test_a_cancelled_plan_makes_it_not_placed_again(conn, ids):
    p = plan(conn, ids)
    plans.cancel(conn, p, "price moved")
    assert state(conn, ids["fb"])["status"] == "not_placed"


def test_a_plan_whose_event_started_says_so(conn, ids):
    plan(conn, ids, event_at=at(-1))
    assert state(conn, ids["fb"])["plan"]["note"] == "already started, not recorded as placed"


def test_a_lay_matched_before_the_bookmaker_bet_is_still_planned_with_a_warning(conn, ids):
    p = plan(conn, ids)
    plans.fill(conn, p, stake="3.85", odds="5.2")
    st = state(conn, ids["fb"])
    assert st["status"] == "planned" and "bookmaker bet is not recorded" in st["plan"]["note"]


# ---------------------------------------------------------------- placed

def test_placed_through_a_plan(conn, ids):
    p = plan(conn, ids)
    plans.record_matched_bet(conn, p)
    st = state(conn, ids["fb"])
    assert (st["status"], st["label"]) == ("placed", "Placed") and "Lay fully matched" in st["text"]
    assert st["bet"]["position_id"] == services.row(conn, "bet_plans", p)["position_id"]


def test_a_settled_one_stays_placed(conn, ids):
    services.quick_settle(conn, bet(conn, ids, [ids["fb"]]), "back_lost")
    st = state(conn, ids["fb"])
    assert st["status"] == "placed" and st["text"].endswith("settled")


def test_placed_without_a_plan(conn, ids):
    bet(conn, ids, [ids["fb"]])
    assert state(conn, ids["fb"])["status"] == "placed"


def test_a_small_deliberate_underlay_is_still_covered(conn, ids):
    bet(conn, ids, [ids["fb"]], lay=365)                    # 95% of the lay that evens out both results
    assert state(conn, ids["fb"])["status"] == "placed"


# ---------------------------------------------------------------- two free bets in one bet

def test_two_free_bets_in_one_bet_share_one_lay(conn, ids):
    two = services.create_free_bets(conn, count=2, account_id=ids["bf"], face_value=500, expires_at=at(20))
    pid = bet(conn, ids, two, lay=769, account="bf")
    assert [state(conn, f)["status"] for f in two] == ["placed", "placed"]
    assert [state(conn, f)["bet"]["position_id"] for f in two] == [pid, pid]


def test_two_free_bets_with_a_lay_for_only_one_are_not_covered(conn, ids):
    two = services.create_free_bets(conn, count=2, account_id=ids["bf"], face_value=500, expires_at=at(20))
    bet(conn, ids, two, lay=385, account="bf")
    assert [state(conn, f)["status"] for f in two] == ["uncovered", "uncovered"]


def test_a_second_free_bet_added_to_a_fully_matched_plan_is_not_covered_by_it(conn, ids):
    p = plan(conn, ids)
    plans.record_matched_bet(conn, p)
    pid = services.row(conn, "bet_plans", p)["position_id"]
    second = services.create_free_bet(conn, account_id=ids["mid"], face_value=500, expires_at=at(12))
    services.add_leg(conn, pid, dict(account_id=ids["mid"], side="back", stake_pence=500, odds="5.0", free_bet_id=second))
    assert state(conn, second)["status"] == "uncovered" and state(conn, ids["fb"])["status"] == "uncovered"
    services.add_leg(conn, pid, dict(account_id=ids["sm"], side="lay", stake_pence=385, odds="5.2"))
    assert state(conn, second)["status"] == "placed" and state(conn, ids["fb"])["status"] == "placed"


def test_two_free_bets_recorded_as_two_positions_of_one_event_each_have_a_state(conn, ids):
    two = services.create_free_bets(conn, count=2, account_id=ids["bf"], face_value=500, expires_at=at(20))
    a, b = bet(conn, ids, two[:1], account="bf"), bet(conn, ids, two[1:], lay=None, account="bf")
    assert [state(conn, f)["status"] for f in two] == ["placed", "uncovered"]
    assert [state(conn, f)["bet"]["position_id"] for f in two] == [a, b]


# ---------------------------------------------------------------- placed, not covered

def test_bookmaker_bet_with_no_lay_is_not_covered(conn, ids):
    pid = bet(conn, ids, [ids["fb"]], lay=None)
    st = state(conn, ids["fb"])
    assert (st["status"], st["label"], st["text"]) == ("uncovered", "Placed, not covered", "No lay recorded")
    assert st["bet"]["position_id"] == pid


def test_a_part_matched_lay_through_a_plan(conn, ids):
    p = plan(conn, ids)
    plans.place_back(conn, p)
    assert state(conn, ids["fb"])["text"] == "No lay recorded"
    plans.submit_order(conn, p, stake="3.85", odds="5.2")
    plans.fill(conn, p, stake="1.50", odds="5.2")
    st = state(conn, ids["fb"])
    assert st["status"] == "uncovered" and st["text"] == "Lay part matched, £1.50 of £3.85"
    plans.fill(conn, p, stake="2.35", odds="5.2")
    assert state(conn, ids["fb"])["status"] == "placed"


def test_a_part_matched_lay_with_the_rest_cancelled_is_still_short(conn, ids):
    p = plan(conn, ids)
    plans.place_back(conn, p)
    plans.submit_order(conn, p, stake="3.85", odds="5.2")
    plans.fill(conn, p, stake="1.50", odds="5.2")
    plans.cancel_unmatched(conn, p, reason="price went")
    assert state(conn, ids["fb"])["status"] == "uncovered"


def test_the_rest_of_a_plans_lay_added_by_hand_counts(conn, ids):
    p = plan(conn, ids)
    plans.place_back(conn, p)
    plans.fill(conn, p, stake="1.50", odds="5.2")
    pid = services.row(conn, "bet_plans", p)["position_id"]
    services.add_leg(conn, pid, dict(account_id=ids["sm"], side="lay", stake_pence=235, odds="5.2"))
    assert state(conn, ids["fb"])["status"] == "placed"


def test_a_part_matched_lay_without_a_plan(conn, ids):
    bet(conn, ids, [ids["fb"]], lay=150)
    st = state(conn, ids["fb"])
    assert st["status"] == "uncovered" and "part matched" in st["text"]


def test_a_lay_removed_after_it_was_fully_matched_is_not_covered(conn, ids):
    p = plan(conn, ids)
    plans.record_matched_bet(conn, p)
    pid = services.row(conn, "bet_plans", p)["position_id"]
    lay = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'lay'", (pid,)).fetchone()[0]
    services.delete_leg(conn, lay)
    assert state(conn, ids["fb"])["status"] == "uncovered"


def test_one_of_two_fills_removed_is_not_covered(conn, ids):
    p = plan(conn, ids)
    plans.place_back(conn, p)
    plans.fill(conn, p, stake="1.50", odds="5.2")
    second = plans.fill(conn, p, stake="2.35", odds="5.2")["leg_id"]
    assert state(conn, ids["fb"])["status"] == "placed"
    services.delete_leg(conn, second)
    st = state(conn, ids["fb"])
    assert st["status"] == "uncovered" and st["text"] == "Lay part matched, £1.50 of £3.85"


def test_a_void_bookmaker_leg_hands_it_back_while_the_lay_is_still_open(conn, ids):
    pid = bet(conn, ids, [ids["fb"]])
    leg = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'back'", (pid,)).fetchone()[0]
    services.settle_leg(conn, leg, "void")
    assert services.row(conn, "positions", pid)["status"] == "open"
    assert state(conn, ids["fb"])["status"] == "not_placed"


def test_a_void_lay_does_not_cover(conn, ids):
    pid = bet(conn, ids, [ids["fb"]])
    leg = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'lay'", (pid,)).fetchone()[0]
    services.settle_leg(conn, leg, "void")
    assert state(conn, ids["fb"])["text"].startswith("No lay recorded")


def test_one_unmatched_lay_of_several_is_not_covered(conn, ids):
    p = plan(conn, ids, exchange="", lay_odds="", outcomes=["1-0", "2-0", "Other"], back_covers=["1-0", "2-0"],
             lays=[{"exchange": "Smarkets", "odds": "9.0", "covers": ["1-0"]}, {"exchange": "Smarkets", "odds": "11.0", "covers": ["2-0"]}])
    plans.place_back(conn, p)
    first, second = [l["id"] for l in plans.load(conn, p)["legs"] if l["side"] == "lay"]
    plans.mark_fully_matched(conn, p, plan_leg_id=first)
    assert state(conn, ids["fb"])["status"] == "uncovered"
    plans.mark_fully_matched(conn, p, plan_leg_id=second)
    assert state(conn, ids["fb"])["status"] == "placed"


# ---------------------------------------------------------------- neither a tick nor an alarm

def test_a_lay_on_one_leg_of_an_acca_is_checked_not_ticked(conn, ids):
    bet(conn, ids, [ids["fb"]], odds="6.23", lay=347, lay_odds="1.40")
    st = state(conn, ids["fb"])
    assert st["status"] == "check" and "check" in st["text"]


def test_an_acca_laid_leg_by_leg_waits_for_its_next_lay(conn, ids, monkeypatch):
    bet(conn, ids, [ids["fb"]])
    monkeypatch.setattr(fs.exposure, "after_next_lay", lambda c, pid: 100)
    st = state(conn, ids["fb"])
    assert st["status"] == "check" and "next lay still to place" in st["text"]


def test_marked_used_with_no_bet_linked(conn, ids):
    services.update_free_bet(conn, ids["fb"], {"status": "used"})
    st = state(conn, ids["fb"])
    assert st["status"] == "check" and "no bet is linked" in st["text"]


# ---------------------------------------------------------------- voided, removed, expired

def test_a_voided_position_hands_the_free_bet_back(conn, ids):
    pid = bet(conn, ids, [ids["fb"]])
    services.quick_settle(conn, pid, "void")
    assert services.row(conn, "free_bets", ids["fb"])["status"] == "available"
    assert state(conn, ids["fb"])["status"] == "not_placed"


def test_a_voided_position_of_a_plan_hands_the_free_bet_back(conn, ids):
    p = plan(conn, ids)
    plans.record_matched_bet(conn, p)
    pid = services.row(conn, "bet_plans", p)["position_id"]
    ops.run(conn, "void_position", {"position_id": str(pid), "reason": "bookmaker voided it"})
    assert services.row(conn, "positions", pid)["status"] == "void"
    assert state(conn, ids["fb"])["status"] == "not_placed"


def test_a_deleted_position_hands_the_free_bet_back(conn, ids):
    pid = bet(conn, ids, [ids["fb"]])
    services.delete_position(conn, pid, "recorded by mistake")
    assert state(conn, ids["fb"])["status"] == "not_placed"


def test_a_voided_free_bet_is_void(conn, ids):
    services.update_free_bet(conn, ids["fb"], {"status": "void"})
    assert state(conn, ids["fb"])["status"] == "void"


@pytest.mark.parametrize("marked", [False, True])
def test_expired_unused_is_missed(conn, ids, marked):
    fb = services.create_free_bet(conn, account_id=ids["bf"], face_value=200, expires_at=at(-3))
    plan(conn, ids, fb=fb) if not marked else services.expire_overdue(conn)
    st = state(conn, fb)
    assert (st["status"], st["label"]) == ("missed", "Missed")


def test_deriving_changes_nothing(conn, ids):
    plan(conn, ids)
    n = conn.total_changes
    state(conn, ids["fb"])
    readapi.free_bet(conn, ids["fb"])
    assert conn.total_changes == n


# ---------------------------------------------------------------- terms: usable from, restriction, estimated expiry

def test_terms_are_stored_shown_and_checked(conn, ids):
    fb = services.create_free_bet(conn, account_id=ids["mid"], face_value=500, expires_at="2099-11-25T23:59:00Z",
                                  usable_from="2099-11-25T18:00:00Z", restriction=" in-play only, 3+ legs ", expiry_estimated=True)
    f = services.row(conn, "free_bets", fb)
    assert (f["restriction"], f["expiry_estimated"]) == ("in-play only, 3+ legs", 1)
    assert fs.expiry_text(f) == "Wed 25 Nov, about 23:59"
    assert fs.usable_text(f) == "Not usable before Wed 25 Nov 18:00"
    assert fs.usable_text(f, "2099-11-25T19:00:00Z") == "Usable since Wed 25 Nov 18:00"
    services.update_free_bet(conn, fb, {"expiry_estimated": False})
    assert fs.expiry_text(services.row(conn, "free_bets", fb)) == "Wed 25 Nov 23:59"
    for bad in ({"usable_from": "2099-11-26T18:00:00Z"}, {"restriction": "x" * 121}):
        with pytest.raises(UserError):
            services.update_free_bet(conn, fb, bad)
    services.update_free_bet(conn, fb, {"expires_at": None, "expiry_estimated": 1, "usable_from": ""})
    f = services.row(conn, "free_bets", fb)
    assert (f["expiry_estimated"], f["usable_from"]) == (0, None)          # no expiry, so nothing is estimated


def test_update_free_bet_change_sets_and_clears_the_terms(conn, ids):
    fid = str(ids["fb"])
    ops.run(conn, "update_free_bet", {"free_bet_id": fid, "usable_from": times.local(at(3), "%Y-%m-%dT%H:%M"),
                                      "restriction": "in-play only", "expiry_estimated": True, "reason": "wallet"})
    v = readapi.free_bet(conn, ids["fb"])
    assert v["usable_from"] and v["restriction"] == "in-play only" and v["expiry_estimated"] is True
    ops.run(conn, "update_free_bet", {"free_bet_id": fid, "clear_usable_from": True, "clear_restriction": True,
                                      "expiry_estimated": False, "reason": "exact now"})
    v = readapi.free_bet(conn, ids["fb"])
    assert (v["usable_from"], v["restriction"], v["expiry_estimated"]) == (None, None, False)


def test_an_edit_that_doesnt_mention_the_terms_leaves_them(conn, ids):
    services.update_free_bet(conn, ids["fb"], {"usable_from": at(2), "restriction": "x", "expiry_estimated": 1})
    ops.run(conn, "update_free_bet", {"free_bet_id": str(ids["fb"]), "notes": "seen in the wallet", "reason": "note"})
    f = services.row(conn, "free_bets", ids["fb"])
    assert f["usable_from"] and (f["restriction"], f["expiry_estimated"]) == ("x", 1)


def test_the_free_bet_candidate_carries_the_terms(conn, ids):
    raw = {"kind": "free_bet", "bookmaker": "Midnite", "value": "5", "free_bet_type": "snr",
           "expires_at": times.local(at(30), "%Y-%m-%dT%H:%M"), "usable_from": times.local(at(3), "%Y-%m-%dT%H:%M"),
           "restriction": "in-play only, 3+ legs", "expiry_estimated": "true"}
    c, _ = imports._norm_free_bet(conn, raw, "test")
    assert c["restriction"] == "in-play only, 3+ legs" and c["expiry_estimated"] is True and c["usable_from"]
    again, _ = imports._norm_free_bet(conn, imports.to_raw("free_bet", c), "test")
    assert {k: again[k] for k in imports.TERMS} == {k: c[k] for k in imports.TERMS}
    for bad in ({"usable_from": times.local(at(40), "%Y-%m-%dT%H:%M")}, {"restriction": "x" * 121}):
        with pytest.raises(imports.CandidateError):
            imports._norm_free_bet(conn, raw | bad, "test")
    plain, _ = imports._norm_free_bet(conn, {k: v for k, v in raw.items() if k not in imports.TERMS}, "test")
    assert not set(imports.TERMS) & set(plain)                           # older entries keep their shape


def test_rewards_pass_the_terms_on(conn, ids):
    o = services.create_offer(conn, title="Bet £5 get £5", account_id=ids["mid"],
                              terms={"reward_count": 1, "reward_each_pence": 500, "reward_kind": "snr"})
    fid, = services.receive_reward(conn, o, expires_at=at(48), restriction="NFL only", expiry_estimated=True)
    f = services.row(conn, "free_bets", fid)
    assert (f["restriction"], f["expiry_estimated"]) == ("NFL only", 1)


def test_the_read_api_gives_the_worked_out_placement(conn, ids):
    assert readapi.free_bet(conn, ids["fb"])["placement"]["status"] == "not_placed"
    pid = bet(conn, ids, [ids["fb"]], lay=150)
    v = readapi.free_bet(conn, ids["fb"])["placement"]
    assert (v["status"], v["position_id"], v["lay_fully_matched"]) == ("uncovered", pid, False)


# ---------------------------------------------------------------- the pages

@pytest.fixture
def world(app):
    with app.state.db.tx() as conn:
        out = {"mid": services.create_account(conn, name="Midnite", kind="bookmaker", opening_balance=5000),
               "bf": services.create_account(conn, name="Betfair Sportsbook", kind="bookmaker", opening_balance=5000),
               "sm": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=0, opening_balance=50000)}
        out["fb"] = services.create_free_bet(conn, account_id=out["mid"], face_value=500, expires_at=at(12),
                                             restriction="in-play only, 3+ legs", usable_from=at(4), expiry_estimated=True)
    return out


def test_the_dashboard_shows_the_plain_countdown_and_no_list_of_placed_bets(app, world, browser):
    page = browser.get("/bets/").text
    stat = page.split('<div class="stat alert">', 1)[1].split("</div>", 1)[0]
    assert "Free bets expiring" in stat and "1 Midnite free bet, £5" in stat and "Find a match" in stat
    with app.state.db.tx() as conn:
        bet(conn, world, [world["fb"]])
    page = browser.get("/bets/").text
    assert "Free bets expiring" not in page and "Lay fully matched" not in page and "Free bets held" in page


def test_the_totals_do_not_depend_on_the_states(app, world, browser):
    with app.state.db.read() as conn:
        before = readapi.dashboard_totals(conn)
    browser.get("/bets/")
    with app.state.db.read() as conn:
        assert readapi.dashboard_totals(conn) == before


def test_free_bets_page_edits_the_terms(app, world, browser):
    r = browser.post(f"/bets/free-bets/{world['fb']}", {"status": "available", "has_terms": "1", "restriction": "NFL only",
                                                        "usable_from": "", "notes": ""})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    with app.state.db.read() as conn:
        f = services.row(conn, "free_bets", world["fb"])
    assert (f["restriction"], f["usable_from"], f["expiry_estimated"]) == ("NFL only", None, 0)
    r = browser.post("/bets/free-bets", {"account_id": str(world["mid"]), "face_value": "5", "kind": "snr", "count": "1",
                                         "expires_at": "2099-01-02T23:59", "expiry_estimated": "1", "usable_from": "2099-01-01T10:00",
                                         "restriction": "in-play"})
    with app.state.db.read() as conn:
        f = dict(conn.execute("SELECT * FROM free_bets ORDER BY id DESC LIMIT 1").fetchone())
    assert (f["restriction"], f["expiry_estimated"]) == ("in-play", 1) and f["usable_from"]


# ---------------------------------------------------------------- the migration

def test_the_migration_is_additive(tmp_path):
    sql = (MIGRATIONS / "0019_free_bet_terms.sql").read_text()
    body = re.sub(r"--[^\n]*", "", sql)
    assert all(re.match(r"ALTER TABLE free_bets ADD COLUMN ", s.strip()) for s in body.split(";") if s.strip())
    d = Database(tmp_path / "x.sqlite3")
    d.migrate()
    with d.tx() as conn:                       # a row written by the version before (it doesn't name the new columns)
        a = services.create_account(conn, name="Old", kind="bookmaker")
        conn.execute("INSERT INTO free_bets (account_id, face_value_pence, created_at, updated_at) VALUES (?, 500, 'x', 'x')", (a,))
        f = dict(conn.execute("SELECT * FROM free_bets").fetchone())
    assert (f["usable_from"], f["restriction"], f["expiry_estimated"]) == (None, "", 0)
