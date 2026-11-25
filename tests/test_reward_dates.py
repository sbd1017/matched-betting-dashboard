"""When an offer's reward is due: from its terms or a date someone gave, never a guess. The regression case is a weekly
club: no reward delay was recorded, the qualifier settled on the Tuesday, and the app made up a due date of
the Wednesday and called it overdue. Sky credits it after the week ends on Sunday, by Monday 19:00."""
from datetime import timedelta

import pytest

from bets import brief, issues, metrics, offer_state, rewards, services, times

from .forms import forms_in
from .test_assistant import API, api, token  # noqa: F401 (fixtures)

SUN, MON = "2099-11-29T23:59", "2099-11-30T19:00"           # a Sunday and Monday in winter (GMT)


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


def change(api, name, rid, **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": rid})
    return r.status_code, r.json()


def make(app, *, settled=-30, terms=None, **offer):
    """A Sky Bet Club week whose qualifier settled `settled` hours ago (no reward date in its terms unless given)."""
    with app.state.db.tx() as conn:
        sky = services.create_account(conn, name="Sky Bet", kind="bookmaker", opening_balance=5000)
        sm = services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=0, opening_balance=50000)
        oid = services.create_offer(conn, title="Sky Bet Club", account_id=sky, offer_type="recurring", **offer,
                                    terms={"reward_count": 2, "reward_each_pence": 250, "reward_kind": "snr",
                                           "reward_expiry_days": 7, **(terms or {})})
        pid = services.create_position(conn, event="Qualifier", selection="Home", offer_id=oid, event_at=at(settled - 3), legs=[
            dict(account_id=sky, side="back", stake_pence=3000, odds="2.5", placed_at=at(settled - 5)),
            dict(account_id=sm, side="lay", stake_pence=2940, odds="2.55", placed_at=at(settled - 5))])
        for leg in conn.execute("SELECT id, side FROM legs WHERE position_id = ?", (pid,)).fetchall():
            services.settle_leg(conn, leg["id"], "lost" if leg["side"] == "back" else "won", at=at(settled))
    return oid


def reward(app, oid) -> dict:
    with app.state.db.read() as conn:
        rows = conn.execute("SELECT * FROM expected_rewards WHERE offer_id = ? AND status <> 'void'", (oid,)).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def overdue(app) -> bool:
    with app.state.db.read() as conn:
        return any(i["key"].startswith("reward:overdue") for i in issues.collect(conn))


def stage(app, oid) -> dict:
    with app.state.db.read() as conn:
        return offer_state.derive(conn, services.row(conn, "offers", oid))


# ---------------------------------------------------------------- unknown date

@pytest.mark.parametrize("trigger", [None, "bet_placed", "bet_fully_matched", "bet_settled", "bet_lost"])
def test_with_no_delay_and_no_date_the_due_date_is_unknown_and_never_overdue(app, trigger):
    oid = make(app, settled=-24 * 60, terms={"reward_trigger": trigger} if trigger else {})
    r = reward(app, oid)
    assert r["status"] == "waiting" and r["expected_by"] is None and r["due_source"] is None
    assert not overdue(app)
    st = stage(app, oid)
    assert st["stage"] == "reward_expected" and st["deadline"] is None and st["status"] == "awaiting_reward"
    with app.state.db.read() as conn:
        assert rewards.overdue(conn) == []
        d = rewards.describe(conn, r)
    assert d["due"] == "date unknown" and d["expected_by"] is None and d["overdue"] is False


def test_an_unknown_date_is_shown_as_unknown(app, api, browser):
    oid = make(app)
    page = browser.get("/bets/").text
    waiting = page.split("<h2>Up next</h2>", 1)[1].split("</section>", 1)[0]
    assert "date unknown" in waiting and "overdue" not in waiting
    assert "date unknown" in browser.get(f"/bets/offers/{oid}").text
    got = api.get(f"{API}/read/expected_rewards").json()["data"][0]
    assert got["due"] == "date unknown" and got["overdue"] is False


def test_the_offers_own_end_is_not_passed_off_as_the_rewards_deadline(app):
    make(app, expires_at=at(20))
    with app.state.db.read() as conn:
        a = [x for x in brief.next_actions(conn) if x["kind"] == "offer"]
    assert len(a) == 1 and a[0]["deadline"] is None and a[0]["priority"] == "medium"


def test_marking_an_offer_qualified_makes_up_no_date_either(app, api):
    with app.state.db.tx() as conn:
        acc = services.create_account(conn, name="Sky Bet", kind="bookmaker")
        oid = services.create_offer(conn, title="Club", account_id=acc, terms={"reward_count": 2, "reward_each_pence": 250,
                                                                              "reward_kind": "snr"})
    code, body = change(api, "update_offer_progress", "q-1", offer_id=oid, qualified=True, when=times.to_local_input(at(-90)))
    assert code == 200, body
    assert reward(app, oid)["expected_by"] is None and not overdue(app)


# ---------------------------------------------------------------- weekly rewards, after the week ends

def test_a_weekly_reward_is_due_from_the_offers_end_not_from_when_the_bet_settled(app):
    oid = make(app, expires_at=times.from_local_input(SUN), terms={"reward_delay_hours": 19, "reward_due_from": "offer_end"})
    r = reward(app, oid)
    assert r["expected_by"] == times.from_local_input(MON) and r["due_source"] == "offer"
    assert not overdue(app) and stage(app, oid)["deadline"] == times.from_local_input(MON)


def test_a_weekly_reward_keeps_its_uk_time_over_a_clock_change(app):
    # clocks go back at 01:00 UTC on Sunday 25 October 2099
    assert offer_state._after_end(times.from_local_input("2099-10-24T23:59"), 43) == times.from_local_input("2099-10-26T19:00")
    assert offer_state._after_end(times.from_local_input("2099-10-24T23:59"), 19) == times.from_local_input("2099-10-25T19:00")
    assert offer_state._after_end(times.from_local_input("2099-11-30T10:59"), 2) == times.from_local_input("2099-11-30T13:00")
    assert offer_state._after_end(times.from_local_input("2099-11-30T11:00"), 2) == times.from_local_input("2099-11-30T13:00")


def test_moving_the_week_moves_its_waiting_reward(app, api):
    oid = make(app, expires_at=times.from_local_input(SUN), terms={"reward_delay_hours": 19, "reward_due_from": "offer_end"})
    code, body = change(api, "update_offer", "w-1", offer_id=oid, expires_at="2099-12-06T23:59")
    assert code == 200, body
    assert any("due Mon 7 Dec 19:00 instead of due Mon 30 Nov 19:00" in e for e in body["effects"]), body["effects"]
    assert reward(app, oid)["expected_by"] == times.from_local_input("2099-12-07T19:00")


def test_a_weekly_offer_with_no_delay_or_no_end_stays_unknown(app):
    assert reward(app, make(app, expires_at=times.from_local_input(SUN), terms={"reward_due_from": "offer_end"}))["expected_by"] is None


def test_a_weekly_offer_with_a_delay_but_no_end_stays_unknown(app):
    assert reward(app, make(app, terms={"reward_due_from": "offer_end", "reward_delay_hours": 19}))["expected_by"] is None


def test_an_offers_end_means_nothing_to_a_reward_counted_from_the_qualifier(app, api):
    oid = make(app, expires_at=times.from_local_input(SUN), terms={"reward_delay_hours": 48})
    before = reward(app, oid)["expected_by"]
    change(api, "update_offer", "e-1", offer_id=oid, expires_at="2099-12-06T23:59")
    assert reward(app, oid)["expected_by"] == before


def test_setting_what_the_delay_counts_from_moves_the_waiting_reward(app, api):
    oid = make(app, expires_at=times.from_local_input(SUN), terms={"reward_delay_hours": 19})
    assert overdue(app)                                        # 19 hours after the qualifier, 30 hours ago
    code, body = change(api, "update_offer_progress", "f-1", offer_id=oid, reward_due_from="offer_end")
    assert code == 200, body
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON) and not overdue(app)


# ---------------------------------------------------------------- the credited-by date

def test_a_credited_by_date_on_the_offer_is_the_rewards_date(app):
    oid = make(app, terms={"reward_credited_by": times.from_local_input(MON), "reward_delay_hours": 2})
    r = reward(app, oid)
    assert r["expected_by"] == times.from_local_input(MON) and r["due_source"] == "offer" and not overdue(app)


def test_update_offer_sets_changes_and_clears_the_credited_by_date(app, api):
    oid = make(app)
    code, body = change(api, "update_offer", "c-1", offer_id=oid, credited_by=MON, dry_run=True)
    assert code == 200 and any("due Mon 30 Nov 19:00 instead of date unknown" in e for e in body["effects"]), body
    assert reward(app, oid)["expected_by"] is None             # a preview changes nothing
    change(api, "update_offer", "c-2", offer_id=oid, credited_by=MON)
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON)
    assert api.get(f"{API}/read/offer?id={oid}").json()["data"]["terms"]["credited_by"] == "2099-11-30 19:00"
    change(api, "update_offer", "c-3", offer_id=oid, credited_by="2099-12-01T10:00")
    assert reward(app, oid)["expected_by"] == times.from_local_input("2099-12-01T10:00")
    code, body = change(api, "update_offer", "c-4", offer_id=oid, credited_by="2099-12-01T10:00")
    assert body["status"] == "already_done"
    change(api, "update_offer", "c-5", offer_id=oid, clear_credited_by=True)
    assert reward(app, oid)["expected_by"] is None
    assert api.get(f"{API}/read/offer?id={oid}").json()["data"]["terms"]["credited_by"] is None


def test_the_offer_form_keeps_and_sets_the_dates(app, browser):
    oid = make(app, expires_at=times.from_local_input(SUN))
    page = browser.get(f"/bets/offers/{oid}").text
    form = next(f for f in forms_in(page) if "reward_credited_by" in f.fields)
    assert form.fields["reward_credited_by"] == "" and form.fields["reward_due_from"] == ""
    r = browser.c.post(form.action, data={**form.fields, "reward_credited_by": MON, "reward_delay_hours": "19",
                                          "reward_due_from": "offer_end"},
                       headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"], r.headers
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON)
    page = browser.get(f"/bets/offers/{oid}").text
    assert "credited by Mon 30 Nov 19:00" in page
    form = next(f for f in forms_in(page) if "reward_credited_by" in f.fields)
    assert (form.fields["reward_credited_by"], form.fields["reward_delay_hours"], form.fields["reward_due_from"]) == (
        MON, "19", "offer_end")
    browser.c.post(form.action, data={**form.fields, "title": "Renamed"}, headers={"Origin": "http://testserver"}, follow_redirects=False)      # saved unchanged
    with app.state.db.read() as conn:
        o = services.row(conn, "offers", oid)
    assert (o["title"], o["reward_credited_by"], o["reward_delay_hours"], o["reward_due_from"]) == (
        "Renamed", times.from_local_input(MON), 19, "offer_end")


def test_a_candidate_offer_carries_its_reward_date(app, api):
    from bets import imports
    with app.state.db.tx() as conn:
        c, _ = imports._norm_offer(conn, {"title": "Club", "credited_by": MON, "reward_delay_hours": "19",
                                          "reward_due_from": "offer_end", "reward_count": 2, "reward_each": "2.50",
                                          "reward_type": "snr"}, "t")
        assert c["terms"]["reward_credited_by"] == times.from_local_input(MON)
        assert c["terms"]["reward_delay_hours"] == 19 and c["terms"]["reward_due_from"] == "offer_end"
        raw = imports.to_raw("offer", c)
        assert (raw["credited_by"], raw["reward_due_from"]) == (MON, "offer_end") and int(raw["reward_delay_hours"]) == 19
        oid = services.create_offer(conn, title=c["title"], terms=c["terms"])
        assert services.row(conn, "offers", oid)["reward_credited_by"] == times.from_local_input(MON)
        with pytest.raises(imports.CandidateError):
            imports._norm_offer(conn, {"title": "Club", "reward_due_from": "whenever"}, "t")
    r = api.post(f"{API}/candidates/offer", json={"title": "Club", "credited_by": MON})
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------- the date on the reward itself

def test_update_expected_reward_sets_changes_and_clears_the_date(app, api):
    oid = make(app, terms={"reward_delay_hours": 2})
    rid, before = reward(app, oid)["id"], reward(app, oid)["expected_by"]
    assert overdue(app)
    code, body = change(api, "update_expected_reward", "u-1", expected_reward_id=rid, expected_by=MON, dry_run=True)
    assert code == 200 and "-> due Mon 30 Nov 19:00" in body["summary"], body
    assert reward(app, oid)["expected_by"] == before
    code, body = change(api, "update_expected_reward", "u-2", expected_reward_id=rid, expected_by=MON, reason="terms")
    assert code == 200 and body["status"] == "applied", body
    r = reward(app, oid)
    assert (r["expected_by"], r["due_source"], r["count"], r["each_pence"]) == (times.from_local_input(MON), "given", 2, 250)
    assert not overdue(app) and stage(app, oid)["stage"] == "reward_expected"
    code, body = change(api, "update_expected_reward", "u-3", expected_reward_id=rid, expected_by=MON)
    assert body["status"] == "already_done"
    code, body = change(api, "update_expected_reward", "u-4", expected_reward_id=rid, clear_expected_by=True)
    assert reward(app, oid)["expected_by"] == before and reward(app, oid)["due_source"] == "offer"   # back to the terms
    with app.state.db.read() as conn:
        log = conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'expected_reward' AND entity_id = ? AND action = "
                           "'due'", (rid,)).fetchone()[0]
    assert log == 2


def test_expecting_a_reward_again_with_a_new_date_updates_it(app, api):
    oid = make(app, terms={"reward_delay_hours": 2})
    first = reward(app, oid)
    code, body = change(api, "expect_reward", "x-1", offer_id=oid, expected_by=MON, dry_run=True)
    assert code == 200 and any("due Mon 30 Nov 19:00 instead of" in e for e in body["effects"]), body
    assert reward(app, oid) == first
    code, body = change(api, "expect_reward", "x-2", offer_id=oid, expected_by=MON)
    assert code == 200, body
    assert body["result"]["created"] is False and body["result"]["date_changed"] is True
    assert body["result"]["expected_reward_id"] == first["id"]
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON) and not overdue(app)
    code, body = change(api, "expect_reward", "x-3", offer_id=oid)        # no date: the one it has stays
    assert body["result"]["date_changed"] is False
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON)


def test_expecting_a_reward_by_hand_takes_the_offers_date_or_none(app, api):
    with app.state.db.tx() as conn:
        acc = services.create_account(conn, name="Sky Bet", kind="bookmaker")
        terms = {"reward_count": 1, "reward_each_pence": 250, "reward_kind": "snr"}
        a = services.create_offer(conn, title="A", account_id=acc, terms=terms)
        b = services.create_offer(conn, title="B", account_id=acc, terms={**terms, "reward_credited_by": times.from_local_input(MON)})
        c = services.create_offer(conn, title="C", account_id=acc, terms={**terms, "reward_delay_hours": 5})
    for oid in (a, b, c):
        change(api, "expect_reward", f"h-{oid}", offer_id=oid)
    assert reward(app, a)["expected_by"] is None
    assert reward(app, b)["expected_by"] == times.from_local_input(MON)
    assert reward(app, c)["expected_by"] is None               # counts from a qualifier that isn't done


def test_a_date_given_for_the_reward_survives_a_new_delay_but_follows_a_new_credited_by_date(app, api):
    oid = make(app, terms={"reward_delay_hours": 2})
    change(api, "update_expected_reward", "g-1", expected_reward_id=reward(app, oid)["id"], expected_by=MON)
    change(api, "update_offer_progress", "g-2", offer_id=oid, reward_delay_hours=100)
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON)
    change(api, "update_offer", "g-3", offer_id=oid, credited_by="2099-12-02T12:00")
    assert reward(app, oid)["expected_by"] == times.from_local_input("2099-12-02T12:00")


def test_a_date_recorded_before_its_source_was_kept_is_never_blanked(app, api):
    oid = make(app)
    with app.state.db.tx() as conn:
        conn.execute("UPDATE expected_rewards SET expected_by = ? WHERE offer_id = ?", (times.from_local_input(MON), oid))
    change(api, "update_offer_progress", "l-1", offer_id=oid, reward_trigger="bet_placed")
    assert reward(app, oid)["expected_by"] == times.from_local_input(MON)


def test_a_received_reward_is_left_alone_by_the_offers_dates(app, api):
    oid = make(app, terms={"reward_delay_hours": 2})
    r = reward(app, oid)
    with app.state.db.tx() as conn:
        rewards.mark_received(conn, r["id"])
    change(api, "update_offer", "r-1", offer_id=oid, credited_by=MON)
    assert reward(app, oid)["expected_by"] == r["expected_by"]


# ---------------------------------------------------------------- really overdue

@pytest.mark.parametrize("terms", [{"reward_delay_hours": 24}, {"reward_credited_by": "past"},
                                   {"reward_delay_hours": 2, "reward_due_from": "offer_end"}])
def test_a_reward_past_its_known_date_is_overdue(app, browser, terms):
    terms = {k: at(-3) if v == "past" else v for k, v in terms.items()}
    oid = make(app, settled=-60, terms=terms, expires_at=at(-40) if "reward_due_from" in terms else None)
    assert overdue(app)
    st = stage(app, oid)
    assert st["stage"] == "reward_overdue" and st["actor"] == "user" and st["deadline"] < times.now_iso()
    up_next = browser.get("/bets/").text.split("<h2>Up next</h2>", 1)[1].split("</section>", 1)[0]
    assert "overdue since" in up_next
    with app.state.db.read() as conn:
        a = [x for x in brief.next_actions(conn) if x["kind"] == "offer"]
    assert a[0]["priority"] == "high"


def test_dates_never_move_money(app, api):
    oid = make(app)
    with app.state.db.read() as conn:
        before = metrics.summary(conn)
    change(api, "update_offer", "m-1", offer_id=oid, credited_by=MON)
    change(api, "update_expected_reward", "m-2", expected_reward_id=reward(app, oid)["id"], expected_by="2099-12-01")
    change(api, "expect_reward", "m-3", offer_id=oid, expected_by="2099-12-02")
    with app.state.db.read() as conn:
        assert metrics.summary(conn) == before
