"""Read-only views of the records, as plain JSON for the assistant (and the before/after of every change).

Money is a string in the dashboard's currency ("12.34", "-3.20"); times are UK local "YYYY-MM-DD HH:MM". Every balance says exactly
what it is, so nothing has to be inferred from one ambiguous "balance":

  exchange   total_funds = available_cash + reserved_lay_liability (what the ledger holds for the account)
             available_cash = what the exchange shows as available / free to bet
             reserved_lay_liability = held back by the exchange for open lay bets (not a loss while they are open)
  bookmaker  cash_balance (open back stakes have already left it: unsettled_back_stakes shows how much)
  bank       a funding source: net_sent_to_betting and temporary funding still out
"""
from __future__ import annotations

from . import exposure, changes, commission, issues, metrics, outlook, services, times
from .calc import lay_liability
from .money import commission_text, plain

CAPITAL_MEANING = {
    "bankroll_contribution": "permanent bankroll money paid in (capital, never profit)",
    "bankroll_withdrawal": "permanent bankroll money taken out (capital, never profit)",
    "temporary_funding": "borrowed money paid in that has to go back to the bank (capital; tracked as temporary "
                         "funding outstanding)",
    "temporary_return": "temporary funding paid back to the bank (capital; reduces temporary funding outstanding; any "
                        "excess over what was outstanding counts as a bankroll withdrawal)",
    "profit_withdrawal": "winnings taken out of the bankroll (capital movement; realised profit is unchanged)",
    "internal": "between two betting accounts (capital)",
}
LIMIT = 200


def t(value: str | None) -> str | None:
    return times.local(value, "%Y-%m-%d %H:%M") if value else None


def m(pence: int | None) -> str | None:
    return plain(pence) if pence is not None else None


def _name(conn, account_id) -> str | None:
    r = conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone() if account_id else None
    return r[0] if r else None


def _offer_ref(conn, offer_id) -> dict | None:
    r = conn.execute("SELECT id, title FROM offers WHERE id = ?", (offer_id,)).fetchone() if offer_id else None
    return {"id": r["id"], "title": r["title"]} if r else None


# ---------------------------------------------------------------- accounts

def balances(conn, a: dict) -> dict:
    total = services.balance(conn, a["id"])
    if a["kind"] == "exchange":
        from . import plans
        liability = metrics.open_lay_liability(conn, a["id"])
        orders = plans.unmatched_order_reserve(conn, a["id"])
        unsettled = metrics.unsettled_back_stakes(conn, a["id"])
        return {"total_funds": m(total), "available_cash": m(total - liability - orders),
                "reserved_lay_liability": m(liability), "unmatched_order_reserve": m(orders),
                "unsettled_back_stakes": m(unsettled),
                "meaning": "total_funds = available_cash + reserved_lay_liability. The exchange's 'available' / "
                           "'balance' figure is normally available_cash; reserved_lay_liability is exposure held for "
                           "open lay bets, not a loss. Record an exchange balance with figure='available' or 'total'."}
    if a["kind"] == "bookmaker":
        unsettled = metrics.unsettled_back_stakes(conn, a["id"])
        return {"cash_balance": m(total), "unsettled_back_stakes": m(unsettled),
                "cash_plus_open_stakes": m(total + unsettled),
                "meaning": "cash_balance is what the bookmaker should show as withdrawable cash; stakes on open bets "
                           "have already left it. Free bets are not cash."}
    return {"net_sent_to_betting": m(-total), "temporary_funding_outstanding": m(services.outstanding_temporary(conn, a["id"])),
            "meaning": "a bank is a funding source, not bankroll."}


def account(conn, a: dict | int) -> dict:
    if isinstance(a, int):
        a = services.row(conn, "accounts", a)
        if not a:
            raise services.UserError("Account not found")
    last = conn.execute("SELECT id, at, basis, outcome FROM reconciliations WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                        (a["id"],)).fetchone()
    promo = commission.promo(a)
    out = {"id": a["id"], "name": a["name"], "type": a["kind"], "active": bool(a["active"]),
           "standing": a["standing"], "verified": bool(a["verified"]), "balances": balances(conn, a),
           "last_balance_check": {"id": last["id"], "at": t(last["at"]), "figure": last["basis"],
                                  "outcome": last["outcome"]} if last else None, "notes": a["notes"]}
    if a["kind"] == "exchange":
        out["commission_percent"] = commission_text(a["commission_bp"])
        out["commission_promotion"] = ({"ends": promo["date"], "expired": promo["expired"], "warning": promo["warning"] or None}
                                       if promo else None)
    return out


def list_accounts(conn, kind: str | None = None, include_inactive: bool = False) -> list[dict]:
    q, args = "SELECT * FROM accounts WHERE 1=1", []
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if not include_inactive:
        q += " AND active = 1"
    return [account(conn, dict(r)) for r in conn.execute(q + " ORDER BY kind, name COLLATE NOCASE", args)]


def exchange_exposure(conn, account_id: int) -> dict:
    a = services.row(conn, "accounts", account_id)
    if not a or a["kind"] != "exchange":
        raise services.UserError("That is not an exchange")
    lays = []
    for r in conn.execute("SELECT l.*, p.event, p.selection, p.event_at FROM legs l JOIN positions p ON p.id = l.position_id "
                          "WHERE l.account_id = ? AND l.side = 'lay' AND l.result = 'pending' AND l.deleted_at IS NULL "
                          "AND p.deleted_at IS NULL ORDER BY p.event_at, l.id", (account_id,)):
        from decimal import Decimal
        lays.append({"leg_id": r["id"], "position_id": r["position_id"], "event": r["event"], "selection": r["selection"],
                     "event_at": t(r["event_at"]), "lay_stake": m(r["stake_pence"]), "odds": r["odds"],
                     "liability": m(exposure.leg_liability(conn, dict(r), lay_liability(r["stake_pence"], Decimal(r["odds"]))))})
    return {"account": a["name"], "balances": balances(conn, a), "open_lays": lays}


# ---------------------------------------------------------------- positions

def leg(conn, l: dict) -> dict:
    from decimal import Decimal
    fb = services.row(conn, "free_bets", l["free_bet_id"]) if l["free_bet_id"] else None
    out = {"id": l["id"], "side": l["side"], "account": _name(conn, l["account_id"]), "stake": m(l["stake_pence"]),
           "odds": l["odds"], "result": l["result"], "settled_amount": m(l["settled_amount_pence"]),
           "placed_at": t(l["placed_at"]), "settled_at": t(l["settled_at"]), "reference": l["external_ref"] or None,
           "free_bet": {"id": fb["id"], "value": m(fb["face_value_pence"]), "type": fb["kind"]} if fb else None}
    if l["side"] == "lay":
        out.update(commission_percent=commission_text(l["commission_bp"]),
                   liability=m(exposure.leg_liability(conn, l, lay_liability(l["stake_pence"], Decimal(l["odds"])))),
                   commission_paid=m(l["commission_paid_pence"]))
    if l.get("deleted_at"):
        out["removed"] = True
    return out


def position(conn, pid: int) -> dict:
    p = services.row(conn, "positions", pid)
    if not p:
        raise services.UserError(f"No position #{pid}")
    legs = [dict(r) for r in conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL ORDER BY id", (pid,))]
    out = {"id": p["id"], "event": p["event"], "selection": p["selection"], "market": p["market"] or None,
           "type": p["kind"], "status": p["status"], "event_at": t(p["event_at"]), "settled_at": t(p["settled_at"]),
           "offer": _offer_ref(conn, p["offer_id"]), "notes": p["notes"] or None, "legs": [leg(conn, l) for l in legs],
           "hedging": services.hedge_state(p, sum(1 for l in legs if l["side"] == "lay" and l["result"] != "void"))}
    if out["hedging"] == "intentionally_unhedged":
        out["accepted_exposure"] = m(p["accepted_exposure_pence"])
        out["unhedged_reason"] = p["unhedged_reason"]
    if p["deleted_at"]:
        out["removed"] = True
        return out
    if p["status"] == "open":
        proj = outlook.position_projection(conn, pid)
        out["projection"] = ({"if_back_wins": m(proj["if_back_wins"]), "if_back_loses": m(proj["if_back_loses"]),
                              "locked_in": m(proj["locked"]),
                              "warning": "several lay bets: the projection assumes they all lose together"
                              if proj["assumes_all_lays_lose_together"] else None} if proj else None)
        out["event_started"] = bool(p["event_at"] and p["event_at"] < times.now_iso())
        out["realised_result"] = None
    else:
        out["realised_result"] = m(services.position_pnl(conn, pid))
    return out


def list_positions(conn, *, status: str | None = None, event: str | None = None, account: str | None = None,
                   limit: int = 50) -> list[dict]:
    q, args = "SELECT DISTINCT p.id FROM positions p LEFT JOIN legs l ON l.position_id = p.id AND l.deleted_at IS NULL " \
              "LEFT JOIN accounts a ON a.id = l.account_id WHERE p.deleted_at IS NULL", []
    if status in ("open", "settled", "void"):
        q += " AND p.status = ?"
        args.append(status)
    if event:
        q += " AND (p.event LIKE ? OR p.selection LIKE ?)"
        args += [f"%{event}%", f"%{event}%"]
    if account:
        q += " AND a.name = ? COLLATE NOCASE"
        args.append(account)
    q += " ORDER BY p.id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 50), LIMIT)))
    return [position(conn, r[0]) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- free bets

def free_bet(conn, fid: int) -> dict:
    f = services.row(conn, "free_bets", fid)
    if not f:
        raise services.UserError(f"No free bet #{fid}")
    link = conn.execute("SELECT l.id, l.position_id, l.result, p.event, p.status FROM legs l JOIN positions p ON "
                        "p.id = l.position_id WHERE l.free_bet_id = ? AND l.deleted_at IS NULL ORDER BY l.id LIMIT 1",
                        (fid,)).fetchone()
    out = {"id": f["id"], "bookmaker": _name(conn, f["account_id"]), "value": m(f["face_value_pence"]), "type": f["kind"],
           "status": f["status"], "expires_at": t(f["expires_at"]), "credited_at": t(f["credited_at"]),
           "offer": _offer_ref(conn, f["offer_id"]), "notes": f["notes"] or None,
           "position": {"id": link["position_id"], "event": link["event"], "leg_id": link["id"],
                        "position_status": link["status"]} if link else None}
    if f["deleted_at"]:
        out["removed"] = True
    return out


def list_free_bets(conn, *, status: str | None = None, account: str | None = None, limit: int = 100) -> list[dict]:
    q, args = "SELECT f.id FROM free_bets f JOIN accounts a ON a.id = f.account_id WHERE f.deleted_at IS NULL", []
    if status in ("available", "used", "expired", "void"):
        q += " AND f.status = ?"
        args.append(status)
    if account:
        q += " AND a.name = ? COLLATE NOCASE"
        args.append(account)
    q += " ORDER BY f.id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), LIMIT)))
    return [free_bet(conn, r[0]) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- transfers

def transfer(conn, tid: int) -> dict:
    x = services.row(conn, "transfers", tid)
    if not x:
        raise services.UserError(f"No transfer #{tid}")
    out = {"id": x["id"], "from": _name(conn, x["from_account_id"]), "to": _name(conn, x["to_account_id"]),
           "amount": m(x["amount_pence"]), "funding_type": x["capital_type"],
           "meaning": CAPITAL_MEANING.get(x["capital_type"]), "when": t(x["at"]), "notes": x["notes"] or None,
           "affects_profit": False}
    if x["capital_type"] == "temporary_funding":
        out.update(return_by=t(x["due_at"]), temporary_amount=m(x["temp_portion_pence"]))
    if x["capital_type"] == "temporary_return":
        out.update(repays_temporary_funding=m(x["temp_portion_pence"]),
                   excess_counted_as_bankroll_withdrawal=m(x["amount_pence"] - x["temp_portion_pence"]))
    if x["in_transit"] or x["arrived_at"]:
        out.update(in_transit=bool(x["in_transit"]), arrived_at=t(x["arrived_at"]))
    if x["voided_at"]:
        out.update(reversed=True, reversed_at=t(x["voided_at"]))
    return out


def list_transfers(conn, *, account: str | None = None, funding_type: str | None = None, include_reversed: bool = False,
                   limit: int = 100) -> list[dict]:
    q, args = ("SELECT DISTINCT t.id FROM transfers t JOIN accounts a ON a.id IN (t.from_account_id, t.to_account_id) "
               "WHERE 1=1"), []
    if not include_reversed:
        q += " AND t.voided_at IS NULL"
    if account:
        q += " AND a.name = ? COLLATE NOCASE"
        args.append(account)
    if funding_type:
        q += " AND t.capital_type = ?"
        args.append(funding_type)
    q += " ORDER BY t.at DESC, t.id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), LIMIT)))
    return [transfer(conn, r[0]) for r in conn.execute(q, args)]


def temporary_funding(conn) -> list[dict]:
    """Per bank: temporary funding paid in, repaid, and still outstanding."""
    out = []
    for b in conn.execute("SELECT id, name FROM accounts WHERE kind = 'bank' ORDER BY name"):
        tin = conn.execute("SELECT coalesce(sum(amount_pence), 0) FROM transfers WHERE voided_at IS NULL AND "
                           "capital_type = 'temporary_funding' AND from_account_id = ?", (b["id"],)).fetchone()[0]
        back = conn.execute("SELECT coalesce(sum(temp_portion_pence), 0) FROM transfers WHERE voided_at IS NULL AND "
                            "capital_type = 'temporary_return' AND to_account_id = ?", (b["id"],)).fetchone()[0]
        if tin or back:
            out.append({"bank": b["name"], "temporary_in": m(tin), "repaid": m(back),
                        "outstanding": m(services.outstanding_temporary(conn, b["id"]))})
    return out


# ---------------------------------------------------------------- reconciliations

def reconciliation(conn, rid: int) -> dict:
    r = services.row(conn, "reconciliations", rid)
    if not r:
        raise services.UserError(f"No balance check #{rid}")
    e = services.row(conn, "ledger_entries", r["entry_id"]) if r["entry_id"] else None
    return {"id": r["id"], "account": _name(conn, r["account_id"]), "at": t(r["at"]),
            "figure_observed": r["basis"], "observed": m(r["actual_balance_pence"]),
            "app_expected": m(r["app_balance_pence"]), "difference": m(r["difference_pence"]),
            "lay_liability_at_the_time": m(r["liability_pence"]) if r["basis"] == "available" or r["liability_pence"] else None,
            "outcome": r["outcome"], "note": r["note"] or None,
            "adjustment": {"ledger_entry_id": e["id"], "amount": m(e["amount_pence"]), "counts_in_profit": True,
                           "active": not e["superseded_at"]} if e else None,
            "reversed_at": t(r["reversed_at"]), "reversal_note": r["reversal_note"] or None}


def list_reconciliations(conn, *, account: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT r.id FROM reconciliations r JOIN accounts a ON a.id = r.account_id WHERE 1=1", []
    if account:
        q += " AND a.name = ? COLLATE NOCASE"
        args.append(account)
    q += " ORDER BY r.id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 50), LIMIT)))
    return [reconciliation(conn, r[0]) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- offers

def offer(conn, oid: int) -> dict:
    o = services.row(conn, "offers", oid)
    if not o:
        raise services.UserError(f"No offer #{oid}")
    deps = services.offer_dependents(conn, oid)
    from . import rewards
    life = rewards.lifecycle(conn, o)
    out = {"id": o["id"], "title": o["title"], "bookmaker": _name(conn, o["account_id"]), "type": o["offer_type"],
           "stage": life["stage"], "next_action": life.get("next_action"),
           "qualifier_required": life["qualifier_required"], "qualified": life["qualified"],
           "qualified_at": t(life["qualified_at"]), "reward_trigger": life["trigger"],
           "terms": {"qualifying_stake": m(o["qualifying_stake_pence"]), "min_odds": o["min_odds"],
                     "reward_count": o["reward_count"], "reward_each": m(o["reward_each_pence"]),
                     "reward_type": o["reward_kind"], "reward_expiry_days": o["reward_expiry_days"],
                     "opt_in_required": bool(o["opt_in_required"]), "opted_in": bool(o["opted_in_at"]),
                     "deposit_required": bool(o["deposit_required"]), "deposit_done": bool(o["deposit_done_at"]),
                     "required_sport": o["required_sport"], "required_market": o["required_market"],
                     "min_builder_selections": o["min_legs"], "reward_delay_hours": o["reward_delay_hours"]},
           "status": life["status"], "expires_at": t(o["expires_at"]), "requirement": o["requirement"] or None,
           "reward": o["reward"] or services.reward_text(o) or None, "reward_received": life["reward_received"],
           "positions": deps["positions"], "free_bets": deps["free_bets"],
           "realised": m(services.offer_realised(conn, oid)), "notes": o["notes"] or None}
    if o["deleted_at"]:
        out["removed"] = True
    return out


def list_offers(conn, *, status: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT id FROM offers WHERE deleted_at IS NULL", []
    if status:
        q += " AND status = ?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 50), LIMIT)))
    return [offer(conn, r[0]) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- ledger, totals, history

def ledger_entries(conn, *, account: str | None = None, position_id: int | None = None, category: str | None = None,
                   include_superseded: bool = False, limit: int = 100) -> list[dict]:
    q, args = ("SELECT e.*, a.name AS account, l.result AS leg_result FROM ledger_entries e JOIN accounts a ON "
               "a.id = e.account_id LEFT JOIN legs l ON l.id = e.leg_id WHERE 1=1"), []
    if not include_superseded:
        q += " AND e.superseded_at IS NULL"
    if account:
        q += " AND a.name = ? COLLATE NOCASE"
        args.append(account)
    if position_id:
        q += " AND e.position_id = ?"
        args.append(int(position_id))
    if category:
        q += " AND e.category = ?"
        args.append(category)
    q += " ORDER BY e.at DESC, e.id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), LIMIT)))
    out = []
    for e in conn.execute(q, args):
        counts = e["class"] == "pnl" or (e["class"] == "stake" and e["leg_result"] not in (None, "pending"))
        out.append({"id": e["id"], "at": t(e["at"]), "account": e["account"], "amount": m(e["amount_pence"]),
                    "category": e["category"], "class": e["class"], "counts_in_realised_profit": counts,
                    "description": e["description"], "position_id": e["position_id"], "transfer_id": e["transfer_id"],
                    "superseded": bool(e["superseded_at"])})
    return out


def money_currency() -> str:
    from .money import currency
    return currency()


def dashboard_totals(conn) -> dict:
    s = metrics.summary(conn)
    stc = outlook.still_to_come(conn)
    found = issues.collect(conn, s)
    ex_total = s.exchange_balance
    return {"currency": money_currency(), 
        "realised_profit": {"all_time": m(s.profit_lifetime), "this_month": m(s.profit_month), "this_week": m(s.profit_week),
                            "meaning": "settled bets and explicit P&L entries only; never deposits, withdrawals, "
                                       "temporary funding, free-bet face value or open liability"},
        "bookmaker_cash": m(s.bookmaker_balance),
        "exchange": {"total_funds": m(ex_total), "available_cash": m(ex_total - s.lay_liability),
                     "reserved_lay_liability": m(s.lay_liability)},
        "unsettled_back_stakes": m(s.unsettled_stakes),
        "available_to_bet": m(s.available),
        "own_capital_in": m(s.own_capital),
        "temporary_funding_outstanding": m(s.temporary_outstanding),
        "temporary_funding_by_bank": temporary_funding(conn),
        "bankroll_withdrawn": m(s.bankroll_withdrawn),
        "profit_withdrawn": m(s.profit_withdrawn),
        "free_bets_held": {"count": s.free_bets_count, "face_value": m(s.free_bets_value),
                           "meaning": "face value, not cash or profit"},
        "still_to_come": m(stc["total"]),
        "open_positions_not_counted_because_event_finished": stc["finished_open"],
        "issues": issues.counts(found) | {"total": len(found)},
        "ledger_checks_pass": s.consistent,
    }


def profit_breakdown(conn) -> dict:
    s = metrics.summary(conn)
    by_cat = {r[0]: r[1] for r in conn.execute(
        "SELECT e.category, coalesce(sum(e.amount_pence), 0) FROM ledger_entries e WHERE e.superseded_at IS NULL "
        "AND e.class = 'pnl' GROUP BY e.category")}
    settled_stakes = conn.execute(
        "SELECT coalesce(sum(e.amount_pence), 0) FROM ledger_entries e JOIN legs l ON l.id = e.leg_id WHERE "
        "e.superseded_at IS NULL AND e.class = 'stake' AND l.result <> 'pending'").fetchone()[0]
    bets = sum(by_cat.get(k, 0) for k in ("winnings", "lay_win", "lay_loss", "commission", "refund", "free_bet_winnings"))
    return {"realised_profit": m(s.profit_lifetime),
            "components": {"settled_bets": m(bets + settled_stakes),
                           "promotion_rewards_and_bonus_cash": m(by_cat.get("promotion_reward", 0) + by_cat.get("bonus_cash", 0)),
                           "reconciliation_adjustments": m(by_cat.get("reconciliation", 0)),
                           "manual_adjustments": m(by_cat.get("adjustment", 0) + by_cat.get("other", 0))},
            "this_month": m(s.profit_month), "this_week": m(s.profit_week),
            "note": "reconciliation_adjustments are balance-check differences recorded as profit/loss; review any that "
                    "are not genuine (get_issues lists suspicious ones)."}


def still_to_come_breakdown(conn) -> dict:
    b = outlook.still_to_come_breakdown(conn)

    def pos(i):
        p = i["projection"]
        return {"position_id": i["position_id"], "event": i["event"], "selection": i["selection"],
                "event_at": t(i["event_at"]), "locked_in": m(p["locked"]) if p else None,
                "if_back_wins": m(p["if_back_wins"]) if p else None, "if_back_loses": m(p["if_back_loses"]) if p else None,
                "note": i.get("note") or ("several lay bets: assumes they all lose together"
                                          if p and p["assumes_all_lays_lose_together"] else None)}
    return {"still_to_come": m(b["total"]),
            "open_positions": {"total": m(b["locked"]), "items": [pos(i) for i in b["positions"]]},
            "free_bets": {"total": m(b["free_bets"]),
                          "items": [{"free_bet_id": f["free_bet_id"], "bookmaker": f["account"], "type": f["kind"],
                                     "face_value": m(f["face_value"]), "retention_pct": f["retention_pct"],
                                     "estimated_worth": m(f["worth"]), "expires_at": t(f["expires_at"])}
                                    for f in b["free_bet_items"]]},
            "rewards_not_yet_credited": {"total": m(b["rewards"]),
                                         "items": [{"offer_id": r["offer_id"], "title": r["title"],
                                                    "estimated_worth": m(r["worth"])} for r in b["reward_items"]]},
            "not_counted_event_probably_finished": [pos(i) for i in b["not_counted_finished"]],
            "meaning": "locked-in result of open positions + unused free bets at your retention + rewards still to "
                       "arrive. Positions whose event has probably finished are listed but not counted: settle them."}


def history(conn, entity: str, entity_id: int, limit: int = 30) -> list[dict]:
    import json
    rows = conn.execute("SELECT * FROM audit_log WHERE entity = ? AND entity_id = ? ORDER BY id DESC LIMIT ?",
                        (entity, int(entity_id), max(1, min(int(limit), LIMIT)))).fetchall()
    return [{"audit_id": r["id"], "at": t(r["at"]), "action": r["action"], "actor": r["actor"] or "earlier (not recorded)",
             "request_id": r["request_id"], "note": r["note"] or None,
             "before": json.loads(r["before_json"]) if r["before_json"] else None,
             "after": json.loads(r["after_json"]) if r["after_json"] else None} for r in rows]


# ---------------------------------------------------------------- snapshots for a change's before/after

def _plan_snapshot(conn, pid):
    from . import brief
    return brief.plan_compact(conn, pid)


def _reward_snapshot(conn, rid):
    from . import brief
    return brief.expected_reward(conn, rid)


SNAPSHOT = {"free_bet": free_bet, "position": position, "transfer": transfer, "reconciliation": reconciliation,
            "offer": offer, "account": account, "plan": _plan_snapshot, "expected_reward": _reward_snapshot}


def snapshot(conn, kind: str, rid: int) -> dict | None:
    try:
        return SNAPSHOT[kind](conn, rid)
    except services.UserError:
        return None


def find(conn, kind: str, ref) -> int:
    """Resolve a position by number or event (the assistant's own words), an account by name; others by number."""
    if kind == "position":
        return changes.find_position(conn, ref)["id"]
    if kind == "account":
        return changes.find_account(conn, ref)["id"]
    if str(ref).lstrip("#").isdigit():
        return int(str(ref).lstrip("#"))
    raise services.UserError(f"Give the {kind.replace('_', ' ')} number")
