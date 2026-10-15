"""Exact money, the calculator, and the ledger's accounting rules."""
from decimal import Decimal

import pytest

from bets import calc, metrics, services
from bets.money import fmt, parse_commission, parse_odds, plain, to_pence
from bets.services import UserError


def pounds(text):
    return to_pence(text, allow_negative=True)


# ---------------------------------------------------------------- money

@pytest.mark.parametrize("text,pence", [("10", 1000), ("10.5", 1050), ("£1,234.56", 123456), ("0.01", 1),
                                        (" £7.56 ", 756), (Decimal("2.40"), 240)])
def test_money_parsing_is_exact(text, pence):
    assert to_pence(text) == pence
    assert isinstance(to_pence(text), int)


@pytest.mark.parametrize("bad", ["10.555", "abc", "", "1e3", "£", "10..5", None, True])
def test_money_parsing_refuses_ambiguous_input(bad):
    with pytest.raises(ValueError):
        to_pence(bad)


def test_negative_and_zero_rules_and_formatting():
    with pytest.raises(ValueError):
        to_pence("-5")
    assert to_pence("-5", allow_negative=True) == -500
    with pytest.raises(ValueError):
        to_pence("0", allow_zero=False)
    assert fmt(123456) == "£1,234.56" and fmt(-320) == "−£3.20" and fmt(240, signed=True) == "+£2.40"
    assert plain(-5) == "-0.05"
    # The classic float trap does not exist here.
    assert to_pence("0.1") + to_pence("0.2") == to_pence("0.3")


def test_odds_and_commission():
    assert parse_odds("4.2") == Decimal("4.2") and parse_odds("7/2") == Decimal("4.5")
    for bad in ("1", "0.5", "abc", "1001", "5/0"):
        with pytest.raises(ValueError):
            parse_odds(bad)
    assert parse_commission("2") == 200 and parse_commission("2%") == 200 and parse_commission("0.5") == 50
    with pytest.raises(ValueError):
        parse_commission("100")


# ---------------------------------------------------------------- calculator

def test_qualifying_bet_back_lay_figures():
    m = calc.match("qualifying", 1000, Decimal("4.0"), Decimal("4.2"), 200)
    assert m.lay_stake == 957            # 40 / 4.18 = 9.569 -> 9.57
    assert m.liability == 3062           # 9.57 * 3.2 = 30.624
    assert m.if_back_wins == -62         # 30.00 - 30.62
    assert m.if_lay_wins == -62          # 9.57 - 0.19 commission - 10.00
    assert m.expected == -62


def test_zero_commission_and_balanced_outcomes():
    m = calc.match("qualifying", 2500, Decimal("2.0"), Decimal("2.1"), 0)
    assert m.lay_stake == 2381 and abs(m.if_back_wins - m.if_lay_wins) <= 2


def test_free_bet_snr_and_sr():
    snr = calc.match("free_bet_snr", 1000, Decimal("5.0"), Decimal("5.2"), 200)
    assert (snr.lay_stake, snr.liability, snr.if_back_wins, snr.if_lay_wins) == (772, 3242, 758, 757)
    assert snr.retention_pct == Decimal("75.7")
    sr = calc.match("free_bet_sr", 1000, Decimal("5.0"), Decimal("5.2"), 200)
    assert (sr.lay_stake, sr.liability, sr.if_back_wins, sr.if_lay_wins) == (965, 4053, 947, 946)


def test_calculator_rejects_impossible_input():
    with pytest.raises(ValueError):
        calc.match("qualifying", 0, Decimal("2"), Decimal("2"), 0)
    with pytest.raises(ValueError):
        calc.match("mystery", 100, Decimal("2"), Decimal("2"), 0)


# ---------------------------------------------------------------- capital is never profit

def test_deposit_and_withdrawal_are_not_profit(conn, world):
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=pounds("100"))
    s = metrics.summary(conn)
    assert (s.profit_lifetime, s.betting_balance, s.temporary_outstanding, s.own_capital) == (0, 10000, 10000, 0)
    services.create_transfer(conn, from_id=world["smarkets"], to_id=world["lloyds"], amount=pounds("100"))
    s = metrics.summary(conn)
    assert (s.profit_lifetime, s.betting_balance, s.temporary_outstanding, s.own_capital) == (0, 0, 0, 0)
    assert s.consistent


def test_temporary_funding_example_from_lloyds(conn, world):
    """Lloyds -> Smarkets £100; a lay settles for +£2.40 net; Smarkets -> Lloyds £102.40."""
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=pounds("100"))
    pid = services.create_position(conn, event="Example", kind="other", legs=[
        {"account_id": world["smarkets"], "side": "lay", "stake_pence": 245, "odds": "3.0"}])
    services.quick_settle(conn, pid, "back_lost")              # lay wins 2.45, commission 0.05
    assert services.balance(conn, world["smarkets"]) == 10240
    s = metrics.summary(conn)
    assert s.profit_lifetime == 240 and s.temporary_outstanding == 10000
    tid = services.create_transfer(conn, from_id=world["smarkets"], to_id=world["lloyds"], amount=pounds("102.40"))
    t = services.row(conn, "transfers", tid)
    assert (t["capital_type"], t["temp_portion_pence"]) == ("temporary_return", 10000)   # £2.40 is a withdrawal
    s = metrics.summary(conn)
    assert s.profit_lifetime == 240                            # only the betting result
    assert s.temporary_outstanding == 0 and s.betting_balance == 0
    assert s.bankroll_withdrawn == 240 and s.profit_withdrawn == 0 and s.own_capital == -240
    assert s.consistent


def test_large_temporary_transfer_never_looks_like_profit_or_loss(conn, world):
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=pounds("500"))
    assert metrics.summary(conn).profit_lifetime == 0
    assert metrics.summary(conn).own_capital == 0              # not a bankroll increase
    services.create_transfer(conn, from_id=world["smarkets"], to_id=world["lloyds"], amount=pounds("500"))
    s = metrics.summary(conn)
    assert (s.profit_lifetime, s.temporary_outstanding, s.own_capital) == (0, 0, 0)


def test_permanent_contribution_withdrawal_and_internal_moves(conn, world):
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=pounds("200"),
                             capital_type="bankroll_contribution")
    services.create_transfer(conn, from_id=world["betfred"], to_id=world["smarkets"], amount=pounds("50"))
    s = metrics.summary(conn)
    assert (s.own_capital, s.temporary_outstanding, s.profit_lifetime, s.betting_balance) == (20000, 0, 0, 20000)
    services.create_transfer(conn, from_id=world["smarkets"], to_id=world["lloyds"], amount=pounds("30"),
                             capital_type="bankroll_withdrawal")
    s = metrics.summary(conn)
    assert s.own_capital == 17000 and s.profit_lifetime == 0 and s.consistent
    with pytest.raises(UserError):          # bank -> bank is not betting activity
        services.create_account(conn, name="Monzo", kind="bank")
        services.create_transfer(conn, from_id=world["lloyds"], to_id=services.row(conn, "accounts", 5)["id"], amount=100)
    with pytest.raises(UserError):          # a funding transfer cannot be labelled a withdrawal
        services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=100,
                                 capital_type="bankroll_withdrawal")


def test_opening_balance_is_capital(conn):
    acc = services.create_account(conn, name="Bet365", kind="bookmaker", opening_balance=pounds("46.27"))
    s = metrics.summary(conn)
    assert (s.betting_balance, s.own_capital, s.profit_lifetime) == (4627, 4627, 0)
    with pytest.raises(UserError):
        services.create_account(conn, name="Barclays", kind="bank", opening_balance=100)
    assert acc


def test_voided_transfer_is_withdrawn_but_kept(conn, world):
    tid = services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=pounds("20"))
    services.void_transfer(conn, tid, "entered twice")
    assert services.balance(conn, world["betfred"]) == 0
    assert conn.execute("SELECT count(*) FROM ledger_entries WHERE transfer_id = ?", (tid,)).fetchone()[0] == 2
    assert metrics.summary(conn).temporary_outstanding == 0


def test_retimed_transfer_moves_only_its_date(conn, world):
    tid = services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=pounds("20"),
                                   at="2026-09-27T17:10:00Z", capital_type="bankroll_contribution")
    before = metrics.summary(conn)
    services.retime_transfer(conn, tid, "2026-09-27T18:10:06Z", "screenshot time is GMT")
    after = metrics.summary(conn)
    assert (after.own_capital, after.betting_balance, after.temporary_outstanding) == \
        (before.own_capital, before.betting_balance, before.temporary_outstanding)
    live = conn.execute("SELECT at, amount_pence FROM ledger_entries WHERE transfer_id = ? AND superseded_at IS NULL "
                        "ORDER BY amount_pence", (tid,)).fetchall()
    assert [tuple(r) for r in live] == [("2026-09-27T18:10:06Z", -2000), ("2026-09-27T18:10:06Z", 2000)]
    assert conn.execute("SELECT count(*) FROM ledger_entries WHERE transfer_id = ?", (tid,)).fetchone()[0] == 4
    assert services.row(conn, "transfers", tid)["at"] == "2026-09-27T18:10:06Z"
    assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'transfer' AND action = 'retimed'").fetchone()[0] == 1
    with pytest.raises(UserError):
        services.retime_transfer(conn, tid, "not a time", "x")
    with pytest.raises(UserError):
        services.retime_transfer(conn, tid, "2026-09-28T10:00:00Z", " ")


# ---------------------------------------------------------------- matched positions

def qualifying(conn, world, offer_id=None):
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=pounds("10"))
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=pounds("40"))
    return services.create_position(conn, event="Liverpool v Arsenal", selection="Liverpool", offer_id=offer_id,
                                    expected_pnl=-62, legs=[
        {"account_id": world["betfred"], "side": "back", "stake_pence": 1000, "odds": "4.0"},
        {"account_id": world["smarkets"], "side": "lay", "stake_pence": 957, "odds": "4.2"}])


def test_open_position_ties_up_stake_and_liability(conn, world):
    qualifying(conn, world)
    s = metrics.summary(conn)
    assert services.balance(conn, world["betfred"]) == 0           # stake taken by the bookmaker
    assert s.unsettled_stakes == 1000 and s.lay_liability == 3062 and s.tied_up == 4062
    assert s.profit_lifetime == 0                                  # nothing realised yet
    assert s.available == 4000 - 3062
    assert s.consistent


@pytest.mark.parametrize("outcome,betfred,smarkets", [("back_won", 4000, 4000 - 3062), ("back_lost", 0, 4000 + 957 - 19)])
def test_settlement_both_ways_gives_the_qualifying_loss(conn, world, outcome, betfred, smarkets):
    oid = services.create_offer(conn, title="Bet £10 Get £30", account_id=world["betfred"])
    pid = qualifying(conn, world, oid)
    assert services.row(conn, "offers", oid)["status"] == "qualifying"
    services.quick_settle(conn, pid, outcome)
    assert services.balance(conn, world["betfred"]) == betfred
    assert services.balance(conn, world["smarkets"]) == smarkets
    assert services.position_pnl(conn, pid) == -62
    s = metrics.summary(conn)
    assert s.profit_lifetime == -62 and s.tied_up == 0 and s.consistent
    assert services.row(conn, "positions", pid)["status"] == "settled"
    assert services.row(conn, "offers", oid)["status"] == "awaiting_reward"


def test_multiple_partial_lays(conn, world):
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=pounds("50"))
    pid = services.create_position(conn, event="Race", legs=[
        {"account_id": world["betfred"], "side": "back", "stake_pence": 1000, "odds": "4.0"},
        {"account_id": world["smarkets"], "side": "lay", "stake_pence": 500, "odds": "4.2"},
        {"account_id": world["smarkets"], "side": "lay", "stake_pence": 457, "odds": "4.3"}])
    assert metrics.summary(conn).lay_liability == 1600 + 1508
    services.quick_settle(conn, pid, "back_lost")
    # -10.00 stake + 5.00 + 4.57 lay wins - 0.10 - 0.09 commission
    assert services.position_pnl(conn, pid) == -1000 + 500 + 457 - 10 - 9
    s = metrics.summary(conn)
    assert s.consistent and [w["name"] for w in s.warnings] == ["No betting account below zero"]  # Betfred unfunded


def test_editing_a_stake_supersedes_instead_of_duplicating(conn, world):
    pid = qualifying(conn, world)
    leg = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'back'", (pid,)).fetchone()[0]
    services.update_leg(conn, leg, {"stake_pence": 1200}, note="typo")
    active = conn.execute("SELECT amount_pence FROM ledger_entries WHERE leg_id = ? AND superseded_at IS NULL", (leg,)).fetchall()
    assert [r[0] for r in active] == [-1200]
    old = conn.execute("SELECT superseded_by, supersede_note FROM ledger_entries WHERE leg_id = ? AND superseded_at IS NOT NULL",
                       (leg,)).fetchone()
    assert old[0] and old[1] == "replaced"
    assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'leg' AND entity_id = ? AND action = 'edited'",
                        (leg,)).fetchone()[0] == 1
    services.update_leg(conn, leg, {"stake_pence": 1200})       # no change -> no new entry
    assert conn.execute("SELECT count(*) FROM ledger_entries WHERE leg_id = ?", (leg,)).fetchone()[0] == 2


def test_editing_a_settled_leg_recalculates_settlement(conn, world):
    pid = qualifying(conn, world)
    services.quick_settle(conn, pid, "back_won")
    leg = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'back'", (pid,)).fetchone()[0]
    services.update_leg(conn, leg, {"odds": "4.5"}, note="odds were 4.5")
    assert conn.execute("SELECT amount_pence FROM ledger_entries WHERE posting_key = ? AND superseded_at IS NULL",
                        (f"leg:{leg}:settle",)).fetchone()[0] == 4500
    assert metrics.summary(conn).consistent


def test_removing_and_reopening_legs(conn, world):
    pid = qualifying(conn, world)
    lay = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'lay'", (pid,)).fetchone()[0]
    services.quick_settle(conn, pid, "back_lost")
    services.settle_leg(conn, lay, "pending", note="settled too early")
    assert services.row(conn, "positions", pid)["status"] == "open"      # reopened
    assert metrics.summary(conn).lay_liability == 3062
    services.delete_leg(conn, lay, "wrong exchange")                       # only the settled back is left
    assert services.row(conn, "positions", pid)["status"] == "settled"
    assert conn.execute("SELECT count(*) FROM ledger_entries WHERE leg_id = ? AND superseded_at IS NULL",
                        (lay,)).fetchone()[0] == 0
    assert metrics.summary(conn).consistent
    other = qualifying(conn, world)                                        # an open position missing its lay
    lay2 = conn.execute("SELECT id FROM legs WHERE position_id = ? AND side = 'lay'", (other,)).fetchone()[0]
    services.delete_leg(conn, lay2, "lay not placed yet")
    assert [p["id"] for p in metrics.incomplete_positions(conn)] == [other]


def test_void_cashout_and_manual_settlements(conn, world):
    pid = qualifying(conn, world)
    back, lay = [r[0] for r in conn.execute("SELECT id FROM legs WHERE position_id = ? ORDER BY side", (pid,))]
    services.settle_leg(conn, back, "void")
    services.settle_leg(conn, lay, "void")
    assert services.position_pnl(conn, pid) == 0 and services.row(conn, "positions", pid)["status"] == "void"
    services.settle_leg(conn, back, "cashout", amount=850)
    services.settle_leg(conn, lay, "manual", amount=-120, commission=0)
    assert services.position_pnl(conn, pid) == -1000 + 850 - 120
    with pytest.raises(UserError):
        services.settle_leg(conn, back, "cashout")              # amount required
    with pytest.raises(UserError):
        services.settle_leg(conn, back, "manual", amount=-5)    # a back bet cannot pay negative


def test_leg_validation(conn, world):
    with pytest.raises(UserError):
        services.create_position(conn, event="X", legs=[{"account_id": world["betfred"], "side": "lay",
                                                         "stake_pence": 100, "odds": "2"}])
    with pytest.raises(UserError):
        services.create_position(conn, event="X", legs=[{"account_id": world["lloyds"], "side": "back",
                                                         "stake_pence": 100, "odds": "2"}])
    with pytest.raises(ValueError):
        services.create_position(conn, event="X", legs=[{"account_id": world["betfred"], "side": "back",
                                                         "stake_pence": 100, "odds": "0.9"}])


# ---------------------------------------------------------------- free bets and offers

def test_snr_free_bet_lifecycle(conn, world):
    oid = services.create_offer(conn, title="Bet £10 Get £10", account_id=world["betfred"])
    fid = services.create_free_bet(conn, account_id=world["betfred"], face_value=1000, kind="snr", offer_id=oid)
    assert services.row(conn, "offers", oid)["status"] == "reward_received"
    assert metrics.summary(conn).free_bets_value == 1000
    assert metrics.summary(conn).betting_balance == 0          # a free bet is not cash
    services.create_transfer(conn, from_id=world["lloyds"], to_id=world["smarkets"], amount=pounds("40"))
    pid = services.create_position(conn, event="Free bet", kind="free_bet_snr", legs=[
        {"account_id": world["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0", "free_bet_id": fid},
        {"account_id": world["smarkets"], "side": "lay", "stake_pence": 772, "odds": "5.2"}])
    assert services.row(conn, "free_bets", fid)["status"] == "used"
    assert conn.execute("SELECT count(*) FROM ledger_entries WHERE category = 'stake'").fetchone()[0] == 0
    services.quick_settle(conn, pid, "back_won")
    assert services.position_pnl(conn, pid) == 4000 - 3242
    assert services.free_bet_value_realised(conn, fid) == 758
    assert services.offer_realised(conn, oid) == 758
    with pytest.raises(UserError):                             # cannot be used twice
        services.create_position(conn, event="Again", legs=[
            {"account_id": world["betfred"], "side": "back", "stake_pence": 1000, "odds": "3", "free_bet_id": fid}])
    assert metrics.summary(conn).consistent


def test_void_free_bet_leg_returns_the_free_bet(conn, world):
    fid = services.create_free_bet(conn, account_id=world["betfred"], face_value=500)
    pid = services.create_position(conn, event="Abandoned match", kind="free_bet_snr", legs=[
        {"account_id": world["betfred"], "side": "back", "stake_pence": 500, "odds": "3", "free_bet_id": fid}])
    services.quick_settle(conn, pid, "void")
    assert services.row(conn, "free_bets", fid)["status"] == "available"
    services.create_position(conn, event="Replay", kind="free_bet_snr", legs=[
        {"account_id": world["betfred"], "side": "back", "stake_pence": 500, "odds": "3", "free_bet_id": fid}])
    assert services.row(conn, "free_bets", fid)["status"] == "used"


def test_expiry_marks_free_bets_and_offers(conn, world):
    fid = services.create_free_bet(conn, account_id=world["paddy"], face_value=500, expires_at="2020-01-01T00:00:00Z")
    oid = services.create_offer(conn, title="Old", expires_at="2020-01-01T00:00:00Z")
    assert services.expire_overdue(conn) == 2
    assert services.row(conn, "free_bets", fid)["status"] == "expired"
    assert services.row(conn, "offers", oid)["status"] == "expired"
    assert services.expire_overdue(conn) == 0


def test_cash_reward_counts_towards_offer_and_profit(conn, world):
    oid = services.create_offer(conn, title="£5 bonus", account_id=world["paddy"])
    services.add_manual_entry(conn, account_id=world["paddy"], amount=500, category="promotion_reward",
                              description="Bonus credited", offer_id=oid)
    assert services.offer_realised(conn, oid) == 500 and metrics.summary(conn).profit_lifetime == 500
    assert services.row(conn, "offers", oid)["reward_credited"] == 1


# ---------------------------------------------------------------- adjustments and reconciliation

def test_reconciliation_is_explicit_and_auditable(conn):
    acc = services.create_account(conn, name="Betfred", kind="bookmaker", opening_balance=4627)
    entry = services.reconcile(conn, acc, 4612, note="rounding on a cash-out")
    e = services.row(conn, "ledger_entries", entry)
    assert (e["amount_pence"], e["category"], e["class"]) == (-15, "reconciliation", "pnl")
    assert services.balance(conn, acc) == 4612
    rec = conn.execute("SELECT * FROM reconciliations").fetchone()
    assert (rec["app_balance_pence"], rec["actual_balance_pence"], rec["difference_pence"]) == (4627, 4612, -15)
    assert services.reconcile(conn, acc, 4612) is None         # already matches: nothing posted
    s = metrics.summary(conn)
    assert s.adjustments == -15 and s.profit_lifetime == -15 and s.consistent


def test_manual_entries_can_be_reversed_but_not_deleted(conn, world):
    eid = services.add_manual_entry(conn, account_id=world["paddy"], amount=300, category="adjustment",
                                    description="Bonus correction")
    with pytest.raises(UserError):
        services.reverse_entry(conn, eid, "")
    services.reverse_entry(conn, eid, "entered on the wrong account")
    assert services.balance(conn, world["paddy"]) == 0
    assert services.row(conn, "ledger_entries", eid)["superseded_at"]
    pid = qualifying(conn, world)
    stake = conn.execute("SELECT id FROM ledger_entries WHERE posting_key LIKE 'leg:%:stake'").fetchone()[0]
    with pytest.raises(UserError):
        services.reverse_entry(conn, stake, "trying to hide a stake")
    with pytest.raises(UserError):
        services.add_manual_entry(conn, account_id=world["lloyds"], amount=100, category="adjustment", description="x")
    assert pid


def test_consistency_check_catches_a_broken_ledger(conn, world):
    tid = services.create_transfer(conn, from_id=world["lloyds"], to_id=world["betfred"], amount=1000)
    assert metrics.summary(conn).consistent
    conn.execute("UPDATE ledger_entries SET superseded_at = '2026-01-01T00:00:00Z' WHERE posting_key = ?",
                 (f"transfer:{tid}:out",))
    checks = {c["name"]: c["ok"] for c in metrics.summary(conn).checks}
    assert checks["Every transfer nets to zero"] is False


def test_all_money_is_stored_as_integers(conn, world):
    qualifying(conn, world)
    services.reconcile(conn, world["betfred"], 1)
    for table, cols in (("ledger_entries", "amount_pence"), ("legs", "stake_pence"), ("transfers", "amount_pence")):
        types = {r[0] for r in conn.execute(f"SELECT DISTINCT typeof({cols}) FROM {table}")}
        assert types == {"integer"}, table


def test_audit_trail_records_financial_changes(conn, world):
    pid = qualifying(conn, world)
    services.quick_settle(conn, pid, "back_lost")
    actions = {(r["entity"], r["action"]) for r in conn.execute("SELECT entity, action FROM audit_log")}
    for expected in (("account", "created"), ("transfer", "created"), ("position", "created"), ("leg", "created"),
                     ("leg", "settled"), ("position", "status")):
        assert expected in actions
