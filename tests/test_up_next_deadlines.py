"""Up next: an offer's deadline only needs the user while they still have to opt in, deposit or place the qualifier.
After that it is waiting (settlement, reward) or tracked by the reward's own free-bet expiry. The regression case is
the real Ladbrokes "Targeted Bet £5 Get £5": reward credited, offer deadline Fri, free bet expiring Mon."""
import re
from datetime import timedelta

import pytest

from bets import exports, offer_state, services, times

TITLE = "Targeted Bet £5 Get £5 Free Bet – Any Sport"


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


@pytest.fixture
def lad(app):
    with app.state.db.tx() as conn:
        ids = {"lad": services.create_account(conn, name="Ladbrokes", kind="bookmaker", opening_balance=2000),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=200,
                                                   opening_balance=5000)}
        ids["offer"] = services.create_offer(
            conn, title=TITLE, account_id=ids["lad"], offer_type="reload", expires_at=at(26),   # inside the 7-day window
            requirement="Bet £5 at 1.5+", terms={"reward_count": 1, "reward_each_pence": 500, "reward_kind": "snr"})
    return ids


def qualifier(conn, ids, event_at=None):
    return services.create_position(conn, event="Arsenal v Spurs", selection="Arsenal", offer_id=ids["offer"],
                                    event_at=event_at or at(30), legs=[
        dict(account_id=ids["lad"], side="back", stake_pence=500, odds="2.0"),
        dict(account_id=ids["smarkets"], side="lay", stake_pence=495, odds="2.04")])


def stage(conn, ids):
    o = services.row(conn, "offers", ids["offer"])
    return offer_state.derive(conn, o)["stage"], offer_state.deadline_matters(conn, o)


def offer_deadlines(conn):
    return [e for e in exports.deadline_events(conn) if e["uid"].startswith("offer-")]


def up_next(browser) -> dict[str, str]:
    """The Up next section split into its groups: {'Needs you': html, 'Waiting': html, 'Tomorrow': html, ...}."""
    html = browser.get("/bets/").text
    section = html.split("<h2>Up next</h2>", 1)[1].split("</section>", 1)[0]
    parts = re.split(r'<div class="grp">([^<]+)', section)[1:]
    return {parts[i].strip(): parts[i + 1] for i in range(0, len(parts), 2)}


def deadline_row(group_html: str) -> bool:
    return "<b>Deadline</b>" in group_html and TITLE in group_html


# 1. qualifier still required: the deadline is the user's to beat
def test_qualifier_still_required_shows_the_deadline(app, lad, browser):
    with app.state.db.read() as conn:
        assert stage(conn, lad) == ("qualifier_required", True)
        assert [e["uid"] for e in offer_deadlines(conn)] == [f"offer-{lad['offer']}"]
    groups = up_next(browser)
    assert deadline_row(groups["Needs you"])
    assert "Waiting" not in groups


@pytest.mark.parametrize("setup, expected", [
    ({"opt_in_required": 1}, "discovered"),
    ({"opt_in_required": 1, "opted_in_at": "2026-01-01T00:00:00Z", "deposit_required": 1}, "deposit_required"),
])
def test_opt_in_and_deposit_still_need_the_deadline(app, lad, setup, expected):
    with app.state.db.tx() as conn:
        conn.execute("UPDATE offers SET " + ", ".join(f"{k} = ?" for k in setup) + " WHERE id = ?",
                     (*setup.values(), lad["offer"]))
        assert stage(conn, lad) == (expected, True)


# 2. qualifier placed and waiting for settlement: nothing to do before the deadline
def test_qualifier_placed_hides_the_deadline(app, lad, browser):
    with app.state.db.tx() as conn:
        qualifier(conn, lad)
        offer_state.sync(conn, lad["offer"])
        assert stage(conn, lad) == ("qualifier_placed", False)
        assert offer_deadlines(conn) == []
    groups = up_next(browser)
    assert not any(deadline_row(g) for g in groups.values())
    assert any("Arsenal v Spurs" in g for name, g in groups.items() if name != "Needs you")   # the bet still shows


# 3. qualified, reward on its way: Waiting, not Needs you
def test_reward_expected_is_waiting_not_needs_you(app, lad, browser):
    with app.state.db.tx() as conn:
        offer_state.mark_qualified(conn, lad["offer"])
        assert stage(conn, lad) == ("reward_expected", False)
        assert offer_deadlines(conn) == []
    groups = up_next(browser)
    assert TITLE not in groups["Needs you"]
    assert "<b>Reward</b>" in groups["Waiting"] and TITLE in groups["Waiting"] and "expected by" in groups["Waiting"]
    assert "It arrived" in groups["Waiting"]                                  # can still be ticked off from there
    assert not any(deadline_row(g) for g in groups.values())


# 3b. reward overdue: chasing the bookmaker is a job for the user
def test_reward_overdue_needs_you(app, lad, browser):
    with app.state.db.tx() as conn:
        offer_state.mark_qualified(conn, lad["offer"], at=at(-72))            # due 24h later: two days late
        assert stage(conn, lad) == ("reward_overdue", False)
    groups = up_next(browser)
    assert "<b>Reward</b>" in groups["Needs you"] and "overdue" in groups["Needs you"]
    assert "Waiting" not in groups
    assert not any(deadline_row(g) for g in groups.values())


# 4 + 5. the real Ladbrokes case: reward credited, the free bet's own expiry shows instead of the offer deadline
def test_ladbrokes_reward_credited_shows_the_free_bet_not_the_offer_deadline(app, lad, browser):
    with app.state.db.tx() as conn:
        pid = qualifier(conn, lad, event_at=at(-30))
        offer_state.mark_qualified(conn, lad["offer"], at=at(-20))
        services.create_free_bet(conn, account_id=lad["lad"], face_value=500, offer_id=lad["offer"],
                                 credited_at=at(-19), expires_at=at(100))   # Mon, after the offer's Fri deadline
        offer_state.sync(conn, lad["offer"])
        o = services.row(conn, "offers", lad["offer"])
        assert o["status"] == "reward_received" and o["reward_credited"] == 1
        assert stage(conn, lad) == ("reward_credited", False)
        assert offer_deadlines(conn) == []
        assert any(e["uid"].startswith("free-bet-") for e in exports.deadline_events(conn))
        assert pid
    groups = up_next(browser)
    assert not any(deadline_row(g) for g in groups.values())                # DON'T: Deadline Fri · Targeted Bet ...
    expiry = groups.get("Needs you", "")
    assert "<b>Expires</b>" in expiry and "1 Ladbrokes free bet" in expiry and "£5" in expiry and "to use" in expiry


# the calendar feed and Calendar page use the same rule
def test_calendar_follows_the_same_rule(app, lad, browser):
    oid = lad["offer"]
    assert f"offer-{oid}" in browser.get("/bets/calendar.ics").text
    with app.state.db.tx() as conn:
        offer_state.mark_qualified(conn, oid)
    assert f"offer-{oid}" not in browser.get("/bets/calendar.ics").text
    assert f"/bets/offers/{oid}" not in browser.get("/bets/calendar").text
    with app.state.db.read() as conn:                  # one offer's own calendar file still has its deadline
        assert [e["uid"] for e in exports.deadline_events(conn, "offer", oid)] == [f"offer-{oid}"]


def test_finished_offers_never_show_a_deadline(app, lad):
    with app.state.db.tx() as conn:
        for status in ("complete", "expired", "abandoned"):
            conn.execute("UPDATE offers SET status = ? WHERE id = ?", (status, lad["offer"]))
            assert offer_deadlines(conn) == []
