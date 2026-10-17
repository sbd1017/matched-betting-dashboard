"""What is coming: money owed back to the bank, cash ready to withdraw, and value still to come.

Read-only, like metrics.py. Every figure comes from the ledger and the records, so it reconciles.

* Temporary funding is repaid oldest first: each bank's returns (their temporary portion) are allocated to its
  funding transfers in date order, so every funding transfer has a remaining amount and, optionally, a due date.
* Withdrawable cash: a bookmaker holding at least £1 with nothing left to do there (no free bets to use, no
  open bets, no offer in progress). Snoozed accounts are left out until the snooze ends.
* Retention: what you actually kept from free bets you have used, as a share of their face value, from your
  own settled free-bet positions. Until there are enough of them, a typical figure is used and labelled so.
* Still to come: locked-in results of open positions (the worse of the two outcomes, so it is never
  optimistic), free bets held valued at your retention, and rewards you have qualified for but not received.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import imports, services, times

MIN_WITHDRAW = 100                     # pence: below this a withdrawal is not worth prompting
SNOOZE_DAYS = 14
TYPICAL = {"snr": Decimal("0.75"), "sr": Decimal("0.90")}    # used until there is enough of your own history
MIN_HISTORY = 3                        # settled free-bet positions needed before your own figure is used
ACTIVE_OFFER = ("not_started", "qualifying", "awaiting_reward", "reward_received", "converting")


def temporary_dues(conn) -> list[dict]:
    """Temporary funding still out, per funding transfer (oldest repaid first), with its due date if set."""
    out = []
    banks = conn.execute("SELECT id, name FROM accounts WHERE kind = 'bank'").fetchall()
    for bank in banks:
        repaid = conn.execute("SELECT coalesce(sum(temp_portion_pence), 0) FROM transfers WHERE voided_at IS NULL "
                              f"AND {services.REPAID_SQL} AND to_account_id = ?", (bank["id"],)).fetchone()[0]
        for t in conn.execute(
                "SELECT t.*, a.name AS to_name, a.kind AS to_kind FROM transfers t JOIN accounts a ON a.id = t.to_account_id "
                "WHERE t.voided_at IS NULL AND t.capital_type = 'temporary_funding' AND t.from_account_id = ? "
                "ORDER BY t.at, t.id", (bank["id"],)):
            used = min(repaid, t["amount_pence"])
            repaid -= used
            remaining = t["amount_pence"] - used
            if remaining > 0:
                out.append({"transfer_id": t["id"], "bank_id": bank["id"], "bank": bank["name"],
                            "account_id": t["to_account_id"], "account": t["to_name"], "amount": t["amount_pence"],
                            "remaining": remaining, "sent_at": t["at"], "due_at": t["due_at"]})
    return sorted(out, key=lambda d: (d["due_at"] is None, d["due_at"] or "", d["sent_at"]))


def _snoozed(conn) -> dict:
    now = times.now_iso()
    return {int(k): v for k, v in (services.get_setting(conn, "withdraw_snooze", {}) or {}).items() if v > now}


def withdrawable(conn) -> list[dict]:
    snoozed = _snoozed(conn)
    rows = conn.execute(
        "SELECT a.id, a.name, coalesce(sum(e.amount_pence), 0) AS balance FROM accounts a "
        "LEFT JOIN ledger_entries e ON e.account_id = a.id AND e.superseded_at IS NULL "
        "WHERE a.kind = 'bookmaker' AND a.active = 1 GROUP BY a.id HAVING balance >= ? ORDER BY balance DESC",
        (MIN_WITHDRAW,)).fetchall()
    out = []
    for a in rows:
        if a["id"] in snoozed:
            continue
        busy = conn.execute(
            "SELECT (SELECT count(*) FROM free_bets WHERE account_id = :a AND status = 'available' AND deleted_at IS NULL)"
            " + (SELECT count(*) FROM legs l JOIN positions p ON p.id = l.position_id WHERE l.account_id = :a "
            "    AND l.result = 'pending' AND l.deleted_at IS NULL AND p.deleted_at IS NULL)"
            " + (SELECT count(*) FROM offers WHERE account_id = :a AND deleted_at IS NULL AND status IN "
            f"    ({','.join(repr(s) for s in ACTIVE_OFFER)}))", {"a": a["id"]}).fetchone()[0]
        if not busy:
            out.append({"account_id": a["id"], "account": a["name"], "balance": a["balance"]})
    return out


def snooze_withdrawals(conn, account_ids: list[int], days: int = SNOOZE_DAYS) -> None:
    from datetime import timedelta
    until = times.iso(times.now() + timedelta(days=days))
    current = {k: v for k, v in _snoozed(conn).items()}
    current.update({int(a): until for a in account_ids})
    services.set_setting(conn, "withdraw_snooze", {str(k): v for k, v in current.items()})
    services.audit(conn, "setting", None, "withdraw_snoozed", None, {"accounts": account_ids, "until": until})


def retention(conn) -> dict:
    """Your free-bet retention per type (snr/sr): kept / face value over settled free-bet positions."""
    out = {}
    for kind, pkind in (("snr", "free_bet_snr"), ("sr", "free_bet_sr")):
        kept = face = n = 0
        for p in conn.execute("SELECT id FROM positions WHERE deleted_at IS NULL AND status = 'settled' AND kind = ?",
                              (pkind,)):
            value = conn.execute("SELECT coalesce(sum(stake_pence), 0) FROM legs WHERE position_id = ? AND side = 'back' "
                                 "AND deleted_at IS NULL", (p["id"],)).fetchone()[0]
            if value:
                kept += services.position_pnl(conn, p["id"])
                face += value
                n += 1
        own = n >= MIN_HISTORY and face > 0
        rate = (Decimal(kept) / Decimal(face)) if own else TYPICAL[kind]
        out[kind] = {"rate": rate, "pct": int((rate * 100).to_integral_value(ROUND_HALF_UP)), "own": own, "count": n}
    return out


def _value(face: int, rate: Decimal) -> int:
    return int((Decimal(face) * rate).to_integral_value(ROUND_HALF_UP))


FINISHED_AFTER_HOURS = 3        # an open position whose event started this long ago has probably finished


def position_projection(conn, position_id: int) -> dict | None:
    """What an open position is set to make: the net result if the back selection wins / loses, and the lower of
    the two (locked in). None unless it has both a back and a lay bet."""
    p = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    legs = [dict(r) for r in conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL", (position_id,))]
    if not (any(l["side"] == "back" for l in legs) and any(l["side"] == "lay" for l in legs)):
        return None
    from . import exposure
    win, lose = exposure.outcomes(conn, position_id, lambda: imports.outcomes({"kind": p["kind"], "legs": legs}))
    if win is None:
        return None
    lays = sum(1 for l in legs if l["side"] == "lay")
    return {"if_back_wins": win, "if_back_loses": lose, "locked": min(win, lose), "lays": lays,
            "assumes_all_lays_lose_together": lays > 1 and not exposure.configured(conn, position_id)}


def still_to_come_breakdown(conn) -> dict:
    """Still to come, item by item: each open position's locked-in result, each unused free bet at your retention,
    and rewards not yet credited. Open positions whose event has probably finished are listed separately and NOT
    counted, so an unsettled result can't quietly distort the figure (settle them)."""
    ret = retention(conn)
    cut = times.iso(times.now() - timedelta(hours=FINISHED_AFTER_HOURS))
    positions, finished = [], []
    for p in conn.execute("SELECT id, event, selection, kind, event_at FROM positions WHERE status = 'open' "
                          "AND deleted_at IS NULL ORDER BY event_at IS NULL, event_at, id"):
        proj = position_projection(conn, p["id"])
        item = {"position_id": p["id"], "event": p["event"], "selection": p["selection"], "event_at": p["event_at"],
                "projection": proj}
        if p["event_at"] and p["event_at"] < cut:
            finished.append(item)
        elif proj:
            positions.append(item)
        else:
            item["note"] = "missing a back or a lay bet: not counted"
            positions.append(item)
    free = []
    for f in conn.execute("SELECT f.id, f.kind, f.face_value_pence, f.expires_at, a.name AS account FROM free_bets f "
                          "JOIN accounts a ON a.id = f.account_id WHERE f.status = 'available' AND f.deleted_at IS NULL "
                          "ORDER BY f.expires_at IS NULL, f.expires_at, f.id"):
        rate = ret[f["kind"]]["rate"] if f["kind"] in ret else TYPICAL["snr"]
        free.append({"free_bet_id": f["id"], "account": f["account"], "kind": f["kind"], "face_value": f["face_value_pence"],
                     "retention_pct": int((rate * 100).to_integral_value(ROUND_HALF_UP)),
                     "own_retention": bool(ret.get(f["kind"], {}).get("own")),
                     "worth": _value(f["face_value_pence"], rate), "expires_at": f["expires_at"]})
    rewards = []
    for o in conn.execute("SELECT id, title, reward_value_pence, reward_kind FROM offers WHERE deleted_at IS NULL AND "
                          "status = 'awaiting_reward' AND reward_credited = 0 AND reward_value_pence > 0"):
        kind = o["reward_kind"] or "snr"
        rewards.append({"offer_id": o["id"], "title": o["title"], "reward_value": o["reward_value_pence"],
                        "worth": o["reward_value_pence"] if kind == "cash" else
                        _value(o["reward_value_pence"], ret[kind]["rate"] if kind in ret else ret["snr"]["rate"])})
    locked = sum(i["projection"]["locked"] for i in positions if i["projection"])
    free_total = sum(f["worth"] for f in free)
    reward_total = sum(r["worth"] for r in rewards)
    return {"total": locked + free_total + reward_total, "locked": locked, "free_bets": free_total,
            "rewards": reward_total, "positions": positions, "free_bet_items": free, "reward_items": rewards,
            "not_counted_finished": finished, "retention": ret}


def still_to_come(conn) -> dict:
    b = still_to_come_breakdown(conn)
    return {"total": b["total"], "locked": b["locked"], "free_bets": b["free_bets"], "rewards": b["rewards"],
            "rewards_count": len(b["reward_items"]), "retention": b["retention"],
            "finished_open": len(b["not_counted_finished"])}


def in_transit(conn) -> list[dict]:
    """Withdrawals that have left a betting account but not reached the bank yet."""
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.amount_pence, t.at, a.name AS from_name, b.name AS to_name FROM transfers t "
        "JOIN accounts a ON a.id = t.from_account_id JOIN accounts b ON b.id = t.to_account_id "
        "WHERE t.voided_at IS NULL AND t.in_transit = 1 ORDER BY t.at")]


def efficiency(conn) -> dict:
    """How well the matched betting is going, from settled records only:
    qualifying loss as a share of qualifying stakes, free-bet retention (all time and last 30 days),
    average days from an offer's first bet to completion, and free bets that expired unused."""
    from datetime import timedelta
    def kept_and_face(since: str | None):
        kept = face = n = 0
        sql = ("SELECT id FROM positions WHERE deleted_at IS NULL AND status = 'settled' "
               "AND kind IN ('free_bet_snr', 'free_bet_sr')" + (" AND settled_at >= ?" if since else ""))
        for (pid,) in conn.execute(sql, (since,) if since else ()):
            value = conn.execute("SELECT coalesce(sum(stake_pence), 0) FROM legs WHERE position_id = ? AND side = 'back' "
                                 "AND deleted_at IS NULL", (pid,)).fetchone()[0]
            if value:
                kept += services.position_pnl(conn, pid)
                face += value
                n += 1
        return kept, face, n
    ql = stakes = qn = 0
    for (pid,) in conn.execute("SELECT id FROM positions WHERE deleted_at IS NULL AND status = 'settled' AND kind = 'qualifying'"):
        stake = conn.execute("SELECT coalesce(sum(stake_pence), 0) FROM legs WHERE position_id = ? AND side = 'back' "
                             "AND deleted_at IS NULL AND free_bet_id IS NULL", (pid,)).fetchone()[0]
        if stake:
            ql += services.position_pnl(conn, pid)
            stakes += stake
            qn += 1
    kept, face, fn = kept_and_face(None)
    kept30, face30, fn30 = kept_and_face(times.iso(times.now() - timedelta(days=30)))
    days = []                     # from the first bet placed for the offer to the last settlement of its bets
    for o in conn.execute("SELECT id FROM offers WHERE deleted_at IS NULL AND status = 'complete'"):
        span = conn.execute(
            "SELECT min(l.placed_at), max(p.settled_at) FROM positions p JOIN legs l ON l.position_id = p.id "
            "AND l.deleted_at IS NULL WHERE p.deleted_at IS NULL AND (p.offer_id = :o OR p.id IN (SELECT x.position_id "
            "FROM legs x JOIN free_bets f ON f.id = x.free_bet_id WHERE f.offer_id = :o AND x.deleted_at IS NULL))",
            {"o": o["id"]}).fetchone()
        if span[0] and span[1]:
            days.append((times.parse(span[1]) - times.parse(span[0])).total_seconds() / 86400)
    expired = conn.execute("SELECT count(*), coalesce(sum(face_value_pence), 0) FROM free_bets WHERE deleted_at IS NULL "
                           "AND status = 'expired'").fetchone()
    pct = lambda a, b: round(a * 100 / b, 1) if b else None
    return {"ql_pct": pct(-ql, stakes), "ql_total": ql, "ql_count": qn,
            "retention": pct(kept, face), "retention_n": fn, "retention_30": pct(kept30, face30), "retention_30_n": fn30,
            "days_to_complete": round(sum(days) / len(days), 1) if days else None, "completed": len(days),
            "expired_count": expired[0], "expired_value": expired[1]}
