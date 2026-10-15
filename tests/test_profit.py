"""Profit reporting: periods by settlement date (UK time), what is and isn't profit, and retained/withdrawn winnings."""
import pytest

from bets import metrics, profit, services, times
from bets.services import UserError


def qualifier(conn, w, *, placed, settled, outcome="back_lost", kind="qualifying", stake=1000, back="4.0", lay="4.2",
              lay_stake=957, offer_id=None, event="Liverpool v Arsenal"):
    """A matched position placed at one time and settled at another."""
    pid = services.create_position(conn, event=event, selection="Liverpool", kind=kind, offer_id=offer_id, legs=[
        {"account_id": w["betfred"], "side": "back", "stake_pence": stake, "odds": back, "placed_at": placed},
        {"account_id": w["smarkets"], "side": "lay", "stake_pence": lay_stake, "odds": lay, "placed_at": placed}])
    services.quick_settle(conn, pid, outcome, at=settled)
    return pid


@pytest.fixture
def funded(conn, world):
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=10000,
                             capital_type="bankroll_contribution", at="2026-09-01T10:00:00Z")
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=10000,
                             capital_type="bankroll_contribution", at="2026-09-01T10:00:00Z")
    return world


def by_key(rows):
    return {r["key"]: r for r in rows}


def test_items_reconcile_with_realised_profit(conn, funded):
    qualifier(conn, funded, placed="2026-09-10T12:00:00Z", settled="2026-09-10T21:00:00Z")
    qualifier(conn, funded, placed="2026-10-02T12:00:00Z", settled="2026-10-02T21:00:00Z", outcome="back_won")
    items = profit.items(conn)
    assert sum(i["amount_pence"] for i in items) == metrics.realised_profit(conn) == metrics.summary(conn).profit_lifetime
    assert profit.periods(items, "all")[-1]["running"] == metrics.realised_profit(conn)


def test_month_boundary_uses_uk_time_of_settlement(conn, funded):
    # 30 Sep 23:30 UTC is 1 Oct 00:30 in the UK (BST): October.
    qualifier(conn, funded, placed="2026-09-30T20:00:00Z", settled="2026-09-30T23:30:00Z")
    # 30 Sep 22:30 UTC is 30 Sep 23:30 UK: September.
    qualifier(conn, funded, placed="2026-09-30T20:00:00Z", settled="2026-09-30T22:30:00Z", event="Spurs v Chelsea")
    rows = by_key(profit.periods(profit.items(conn), "month"))
    assert rows["2026-09"]["count"] and rows["2026-10"]["count"]
    assert rows["2026-09"]["net"] == rows["2026-10"]["net"] != 0


def test_week_boundary_is_monday_midnight_uk(conn, funded):
    # Sun 4 Oct 22:59 UTC = 23:59 UK (week of 28 Sep); Sun 4 Oct 23:00 UTC = Mon 5 Oct 00:00 UK (week of 5 Oct).
    qualifier(conn, funded, placed="2026-10-04T12:00:00Z", settled="2026-10-04T22:59:00Z")
    qualifier(conn, funded, placed="2026-10-04T12:00:00Z", settled="2026-10-04T23:00:00Z", event="Spurs v Chelsea")
    rows = by_key(profit.periods(profit.items(conn), "week"))
    assert rows["2026-09-28"]["count"] and rows["2026-10-05"]["count"]
    assert rows["2026-09-28"]["net"] == rows["2026-10-05"]["net"]


def test_grouping_uses_settlement_date_not_placement(conn, funded):
    # Placed in September, settled in October: the whole result, stake included, is October's.
    pid = qualifier(conn, funded, placed="2026-09-28T12:00:00Z", settled="2026-10-03T21:00:00Z")
    rows = by_key(profit.periods(profit.items(conn), "month"))
    assert rows["2026-10"]["net"] == services.position_pnl(conn, pid)
    assert "2026-09" not in rows or rows["2026-09"]["count"] == 0


def test_negative_period_and_sources(conn, funded):
    qualifier(conn, funded, placed="2026-09-10T12:00:00Z", settled="2026-09-10T21:00:00Z")          # qualifying loss
    fb = services.create_free_bet(conn, account_id=funded["betfred"], face_value=1000)
    pid = services.create_position(conn, event="Spurs v Chelsea", kind="free_bet_snr", legs=[
        {"account_id": funded["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0", "free_bet_id": fb},
        {"account_id": funded["smarkets"], "side": "lay", "stake_pence": 784, "odds": "5.1"}])
    services.quick_settle(conn, pid, "back_lost", at="2026-10-05T21:00:00Z")
    services.add_manual_entry(conn, account_id=funded["betfred"], amount=500, category="promotion_reward",
                              at="2026-10-06T09:00:00Z", description="Profit boost")
    rows = by_key(profit.periods(profit.items(conn), "month"))
    sep, oct_ = rows["2026-09"], rows["2026-10"]
    assert sep["net"] < 0 and sep["qualifying"] == sep["net"] and sep["free"] == 0
    assert oct_["free"] == oct_["net"] == services.position_pnl(conn, pid) + 500 and oct_["qualifying"] == 0
    assert oct_["running"] == sep["net"] + oct_["net"]
    types = {r["name"]: r["net"] for r in profit.breakdown(profit.items(conn), "type")}
    assert types["free_bet_snr"] == services.position_pnl(conn, pid) and types["promotion_reward"] == 500


def test_transfers_and_temporary_funding_are_never_profit(conn, funded):
    services.create_transfer(conn, from_id=funded["lloyds"], to_id=funded["smarkets"], amount=50000,
                             at="2026-10-01T10:00:00Z")                                   # temporary funding
    services.create_transfer(conn, from_id=funded["smarkets"], to_id=funded["lloyds"], amount=50000,
                             at="2026-10-02T10:00:00Z")                                   # repaid
    services.create_transfer(conn, from_id=funded["betfred"], to_id=funded["paddy"], amount=2000, at="2026-10-03T10:00:00Z")
    services.create_transfer(conn, from_id=funded["betfred"], to_id=funded["lloyds"], amount=1000,
                             capital_type="bankroll_withdrawal", at="2026-10-04T10:00:00Z")
    assert profit.items(conn) == []
    assert all(r["net"] == 0 for r in profit.periods(profit.items(conn), "month"))
    assert metrics.summary(conn).profit_lifetime == 0


def test_profit_withdrawal_keeps_realised_profit_and_reduces_retained(conn, funded):
    qualifier(conn, funded, placed="2026-09-10T12:00:00Z", settled="2026-09-10T21:00:00Z", outcome="back_won",
              back="6.0", lay="4.0", lay_stake=1000)                               # a winning position
    before = metrics.summary(conn)
    assert before.profit_lifetime > 1000
    tid = services.create_transfer(conn, from_id=funded["betfred"], to_id=funded["lloyds"], amount=1000,
                                   capital_type="profit_withdrawal", at="2026-10-01T10:00:00Z")
    assert services.row(conn, "transfers", tid)["capital_type"] == "profit_withdrawal"
    s = metrics.summary(conn)
    assert s.profit_lifetime == before.profit_lifetime                     # history is never rewritten
    assert s.profit_withdrawn == 1000 and s.retained_profit == before.profit_lifetime - 1000
    assert s.own_capital == before.own_capital and s.bankroll_withdrawn == 0
    assert s.consistent                                                    # balances still explained
    assert sum(r["net"] for r in profit.periods(profit.items(conn), "month")) == before.profit_lifetime
    w = profit.winnings(conn, s)
    assert (w["realised"], w["withdrawn"], w["retained"]) == (s.profit_lifetime, 1000, s.profit_lifetime - 1000)
    with pytest.raises(UserError, match="retained profit"):
        services.create_transfer(conn, from_id=funded["betfred"], to_id=funded["lloyds"], amount=s.retained_profit + 1,
                                 capital_type="profit_withdrawal")
    with pytest.raises(UserError):                                         # only betting -> bank
        services.create_transfer(conn, from_id=funded["lloyds"], to_id=funded["betfred"], amount=100,
                                 capital_type="profit_withdrawal")


def test_ordinary_withdrawals_are_not_reinterpreted(conn, funded):
    services.create_transfer(conn, from_id=funded["betfred"], to_id=funded["lloyds"], amount=3000,
                             capital_type="bankroll_withdrawal")
    s = metrics.summary(conn)
    assert s.bankroll_withdrawn == 3000 and s.profit_withdrawn == 0 and s.own_capital == 17000


def test_cash_unlocked_respects_liability_stakes_and_temporary_funding(conn, funded):
    services.create_transfer(conn, from_id=funded["lloyds"], to_id=funded["smarkets"], amount=5000)   # temporary
    services.create_position(conn, event="Open bet", legs=[
        {"account_id": funded["betfred"], "side": "back", "stake_pence": 1000, "odds": "4.0"},
        {"account_id": funded["smarkets"], "side": "lay", "stake_pence": 957, "odds": "4.2"}])          # liability 30.62
    s = metrics.summary(conn)
    w = profit.winnings(conn, s)
    # Betfred 100 - 10 staked = 90; Smarkets 150 - 30.62 liability = 119.38; less 50 temporary funding owed.
    assert w["unlocked"] == 9000 + (15000 - 3062) - 5000
    assert w["bankroll"] == 20000 and w["suggestion"] is None


def test_winnings_plan_is_planning_only(conn, funded):
    qualifier(conn, funded, placed="2026-01-10T12:00:00Z", settled="2026-01-10T21:00:00Z", outcome="back_won",
              back="6.0", lay="4.0", lay_stake=1000)                             # profit in a completed month
    ledger_before = conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
    profit.save_plan(conn, 15000, 50)
    w = profit.winnings(conn)
    realised = metrics.realised_profit(conn)
    assert w["completed"] == realised and w["target"] == round(realised / 2)
    assert w["suggestion"] == min(w["still_to_put_away"], w["allocatable"], w["unlocked"], w["above_minimum"])
    assert w["above_minimum"] == max(0, w["bankroll"] - 15000)
    assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == ledger_before   # nothing moved
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action = 'winnings_plan'").fetchone()[0] == 1
    with pytest.raises(UserError):
        profit.save_plan(conn, 0, 120)


def test_profit_page_and_dashboard_links(app, browser):
    from tests.test_web import seed
    ids = seed(app)
    with app.state.db.tx() as conn:
        services.quick_settle(conn, ids["position"], "back_lost")
        s = metrics.summary(conn)
        month_key = profit.period_key(times.now().astimezone(times.UK), "month")
    dash = browser.get("/bets/").text
    assert 'href="/bets/profit?view=all"' in dash and 'href="/bets/profit?view=month"' in dash \
        and 'href="/bets/profit?view=week"' in dash
    page = browser.get("/bets/profit").text
    for text in ("Realised profit, all time", "This month", "This week", "Where it came from", "Winnings",
                 "Profit available to allocate", "Cash unlocked now", "planning figures only"):
        assert text in page, text
    assert page.count(services.fmt(s.profit_lifetime, signed=True)) >= 2
    detail = browser.get(f"/bets/profit?view=month&p={month_key}").text
    assert "What made" in detail and "Liverpool v Arsenal" in detail and "Bet £10 Get £30" in detail
    for view in profit.VIEWS:
        assert browser.get(f"/bets/profit?view={view}").status_code == 200
    r = browser.post("/bets/profit/plan", {"min_bankroll": "100", "put_away_pct": "50"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert browser.post("/bets/profit/plan", {"min_bankroll": "100", "put_away_pct": "150"}).headers["location"].count("err=")
