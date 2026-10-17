"""Read-only figures for the dashboard, accounts page and consistency checks.

Identity that must always hold (checked on the dashboard):

    betting balances = own capital (net) + temporary funding outstanding
                       + retained profit - unsettled back stakes

where betting balances are bookmaker + exchange accounts only (bank accounts are
funding sources), own capital = opening balances + bankroll contributions - bankroll
withdrawals (including any return above the temporary funding it repaid) + bank-to-bank repayments of temporary
funding (the borrowed money already came out to the hub bank), and
retained profit = realised profit - profit withdrawals (winnings deliberately taken out;
a transfer, so it never changes realised profit).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import services, times

ACTIVE = "e.superseded_at IS NULL"
SETTLED_OR_PNL = "(e.class = 'pnl' OR (e.class = 'stake' AND l.result <> 'pending'))"


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0] or 0


def realised_profit(conn, since: str | None = None) -> int:
    """P&L entries + stakes of settled legs. A stake counts from its leg's settlement time."""
    sql = (f"SELECT coalesce(sum(e.amount_pence), 0) FROM ledger_entries e LEFT JOIN legs l ON l.id = e.leg_id "
           f"WHERE {ACTIVE} AND {SETTLED_OR_PNL}")
    if since:
        sql += " AND (CASE WHEN e.class = 'stake' THEN l.settled_at ELSE e.at END) >= ?"
        return _one(conn, sql, (since,))
    return _one(conn, sql)


def account_rows(conn, include_inactive: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT a.*, coalesce((SELECT sum(amount_pence) FROM ledger_entries WHERE account_id = a.id AND superseded_at IS NULL), 0) AS balance,"
        " (SELECT max(at) FROM ledger_entries WHERE account_id = a.id AND superseded_at IS NULL) AS last_activity"
        " FROM accounts a" + ("" if include_inactive else " WHERE a.active = 1") +
        " ORDER BY CASE a.kind WHEN 'bookmaker' THEN 0 WHEN 'exchange' THEN 1 ELSE 2 END, a.name COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["liability"] = open_lay_liability(conn, d["id"])
        d["unsettled_stakes"] = unsettled_back_stakes(conn, d["id"])
        d["available"] = d["balance"] - d["liability"]
        d["free_bets"] = conn.execute("SELECT count(*), coalesce(sum(face_value_pence), 0) FROM free_bets "
                                      "WHERE account_id = ? AND status = 'available' AND deleted_at IS NULL",
                                      (d["id"],)).fetchone()
        if d["kind"] == "bank":
            d["temporary_outstanding"] = services.outstanding_temporary(conn, d["id"])
        out.append(d)
    return out


def open_lay_liability(conn, account_id: int | None = None, position_id: int | None = None) -> int:
    from decimal import Decimal
    from .calc import lay_liability
    from . import exposure
    rows = conn.execute("SELECT l.* FROM legs l JOIN positions p ON p.id=l.position_id "
                        "WHERE l.side='lay' AND l.result='pending' AND l.deleted_at IS NULL "
                        "AND p.deleted_at IS NULL").fetchall()
    by_position = {}
    for row in rows:
        if account_id is not None and row['account_id'] != account_id:
            continue
        if position_id is not None and row['position_id'] != position_id:
            continue
        by_position.setdefault(row['position_id'], []).append(row)
    total = 0
    for pid, legs in by_position.items():
        net = exposure.liability(conn, pid, account_id)
        total += net if net is not None else sum(lay_liability(l['stake_pence'], Decimal(l['odds'])) for l in legs)
    return total


def unsettled_back_stakes(conn, account_id: int | None = None) -> int:
    sql = ("SELECT coalesce(-sum(e.amount_pence), 0) FROM ledger_entries e JOIN legs l ON l.id = e.leg_id "
           f"WHERE {ACTIVE} AND e.class = 'stake' AND l.result = 'pending'")
    if account_id is not None:
        return _one(conn, sql + " AND e.account_id = ?", (account_id,))
    return _one(conn, sql)


@dataclass
class Summary:
    profit_lifetime: int
    profit_month: int
    profit_week: int
    bookmaker_balance: int
    exchange_balance: int
    lay_liability: int
    unsettled_stakes: int
    own_capital: int
    temporary_outstanding: int
    adjustments: int
    bankroll_withdrawn: int       # ordinary bankroll withdrawals + returns above temporary funding
    profit_withdrawn: int         # transfers classified as profit withdrawals (winnings taken out)
    free_bets_value: int
    free_bets_count: int
    checks: list = field(default_factory=list)

    @property
    def betting_balance(self) -> int:
        return self.bookmaker_balance + self.exchange_balance

    @property
    def retained_profit(self) -> int:
        """Realised profit still inside the betting bankroll."""
        return self.profit_lifetime - self.profit_withdrawn

    @property
    def available(self) -> int:
        """Cash you could stake now: bookmaker balances + exchange balances less held liability."""
        return self.bookmaker_balance + self.exchange_balance - self.lay_liability

    @property
    def tied_up(self) -> int:
        return self.unsettled_stakes + self.lay_liability

    @property
    def consistent(self) -> bool:
        """Hard checks only; warnings (negative balance, stale open bets) are shown but not failures."""
        return all(c["ok"] or c.get("warning") for c in self.checks)

    @property
    def warnings(self) -> list:
        return [c for c in self.checks if not c["ok"] and c.get("warning")]


def summary(conn) -> Summary:
    by_kind = dict(conn.execute(
        "SELECT a.kind, coalesce(sum(e.amount_pence), 0) FROM accounts a JOIN ledger_entries e ON e.account_id = a.id "
        f"WHERE {ACTIVE} GROUP BY a.kind").fetchall())
    caps = conn.execute(
        "SELECT"
        " coalesce(sum(CASE WHEN capital_type = 'bankroll_contribution' THEN amount_pence END), 0),"
        " coalesce(sum(CASE WHEN capital_type = 'bankroll_withdrawal' THEN amount_pence END), 0),"
        " coalesce(sum(CASE WHEN capital_type = 'temporary_funding' THEN amount_pence END), 0),"
        " coalesce(sum(CASE WHEN capital_type = 'temporary_return' THEN temp_portion_pence END), 0),"
        " coalesce(sum(CASE WHEN capital_type = 'temporary_return' THEN amount_pence - temp_portion_pence END), 0),"
        " coalesce(sum(CASE WHEN capital_type = 'profit_withdrawal' THEN amount_pence END), 0),"
        " coalesce(sum(CASE WHEN capital_type = 'funding_repayment' THEN temp_portion_pence END), 0)"
        " FROM transfers WHERE voided_at IS NULL").fetchone()
    contributions, withdrawals, temp_in, temp_back, excess_returned, profit_out, bank_repaid = caps
    # A bank-to-bank repayment (hub -> lender) moves no betting money: the borrowed money had already come out as a
    # withdrawal to the hub. So it repays the lender, and that much of what was counted as withdrawn was really the
    # lender's money going back (own capital rises by the same amount). Never counted as withdrawn twice.
    temp_back += bank_repaid
    was_borrowed = min(bank_repaid, withdrawals + excess_returned)
    opening = _one(conn, f"SELECT coalesce(sum(e.amount_pence), 0) FROM ledger_entries e WHERE {ACTIVE} "
                         "AND e.category = 'opening_balance'")
    own_capital = opening + contributions - withdrawals - excess_returned + bank_repaid
    temporary = temp_in - temp_back
    fb = conn.execute("SELECT count(*), coalesce(sum(face_value_pence), 0) FROM free_bets "
                      "WHERE status = 'available' AND deleted_at IS NULL").fetchone()
    s = Summary(
        profit_lifetime=realised_profit(conn),
        profit_month=realised_profit(conn, times.month_start()),
        profit_week=realised_profit(conn, times.week_start()),
        bookmaker_balance=by_kind.get("bookmaker", 0),
        exchange_balance=by_kind.get("exchange", 0),
        lay_liability=open_lay_liability(conn),
        unsettled_stakes=unsettled_back_stakes(conn),
        own_capital=own_capital,
        temporary_outstanding=temporary,
        adjustments=_one(conn, f"SELECT coalesce(sum(e.amount_pence), 0) FROM ledger_entries e WHERE {ACTIVE} "
                               "AND e.category IN ('adjustment', 'reconciliation')"),
        bankroll_withdrawn=withdrawals + excess_returned - was_borrowed, profit_withdrawn=profit_out,
        free_bets_value=fb[1], free_bets_count=fb[0],
    )
    s.checks = consistency_checks(conn, s)
    return s


def consistency_checks(conn, s: Summary) -> list[dict]:
    checks = []
    expected = s.own_capital + s.temporary_outstanding + s.retained_profit - s.unsettled_stakes
    checks.append({"name": "Balances explained by capital, funding and profit",
                   "ok": expected == s.betting_balance,
                   "detail": "" if expected == s.betting_balance else
                   f"Betting balances {s.betting_balance / 100:.2f} vs explained {expected / 100:.2f}"})
    unbalanced = conn.execute(
        "SELECT count(*) FROM (SELECT transfer_id, sum(amount_pence) AS t FROM ledger_entries "
        "WHERE transfer_id IS NOT NULL AND superseded_at IS NULL GROUP BY transfer_id HAVING t <> 0)").fetchone()[0]
    checks.append({"name": "Every transfer nets to zero", "ok": unbalanced == 0,
                   "detail": f"{unbalanced} transfer(s) do not balance" if unbalanced else ""})
    missing_stake = conn.execute(
        "SELECT count(*) FROM legs l JOIN positions p ON p.id = l.position_id WHERE l.deleted_at IS NULL "
        "AND p.deleted_at IS NULL AND l.side = 'back' AND l.free_bet_id IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM ledger_entries e WHERE e.posting_key = 'leg:' || l.id || ':stake' AND e.superseded_at IS NULL)"
    ).fetchone()[0]
    checks.append({"name": "Every cash back bet has its stake in the ledger", "ok": missing_stake == 0,
                   "detail": f"{missing_stake} back bet(s) without a stake entry" if missing_stake else ""})
    orphan = conn.execute(
        "SELECT count(*) FROM ledger_entries e JOIN legs l ON l.id = e.leg_id WHERE e.superseded_at IS NULL "
        "AND l.deleted_at IS NOT NULL").fetchone()[0]
    checks.append({"name": "No money attached to removed bets", "ok": orphan == 0,
                   "detail": f"{orphan} entr(ies) on removed legs" if orphan else ""})
    stale = conn.execute(
        "SELECT count(*) FROM positions WHERE status = 'open' AND deleted_at IS NULL AND event_at IS NOT NULL "
        "AND event_at < ?", (times.iso(times.now().replace(microsecond=0)) ,)).fetchone()[0]
    checks.append({"name": "No open positions for events that have started", "ok": stale == 0, "warning": True,
                   "detail": f"{stale} open position(s) past their start time: settle them" if stale else ""})
    negative = [r["name"] for r in conn.execute(
        "SELECT a.name, sum(e.amount_pence) AS b FROM accounts a JOIN ledger_entries e ON e.account_id = a.id "
        "WHERE e.superseded_at IS NULL AND a.kind <> 'bank' GROUP BY a.id HAVING b < 0")]
    checks.append({"name": "No betting account below zero", "ok": not negative, "warning": True,
                   "detail": ("Negative: " + ", ".join(negative)) if negative else ""})
    return checks


def incomplete_positions(conn) -> list[dict]:
    """Open positions missing a back or a lay leg (qualifying and free-bet positions need both, unless a qualifier was
    deliberately left unhedged: hedge_strategy intentionally_unhedged)."""
    rows = conn.execute(
        "SELECT p.*, sum(l.side = 'back') AS backs, sum(l.side = 'lay') AS lays FROM positions p "
        "LEFT JOIN legs l ON l.position_id = p.id AND l.deleted_at IS NULL "
        "WHERE p.deleted_at IS NULL AND p.status = 'open' AND p.kind <> 'other' GROUP BY p.id "
        "HAVING (coalesce(backs, 0) = 0 OR coalesce(lays, 0) = 0) AND NOT (coalesce(p.hedge_strategy, '') = "
        "'intentionally_unhedged' AND coalesce(backs, 0) > 0 AND coalesce(lays, 0) = 0) ORDER BY p.event_at").fetchall()
    return [dict(r) for r in rows]
