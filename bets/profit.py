"""Profit reporting: realised profit by period and source, and how much of it is retained or withdrawn.

One rule, the same as ``metrics.realised_profit`` (so every figure reconciles with the dashboard):
realised profit = active P&L ledger entries + the stakes of settled bets. Each counts on its
**settlement** date (a P&L entry is dated when its bet settles; a stake counts from its leg's
settlement time, never from when the bet was placed). Periods are UK calendar weeks (Monday 00:00),
months and years. Transfers of any kind (bankroll contributions and withdrawals, temporary funding,
profit withdrawals) are capital, never P&L, so they cannot appear here.

Read-only: nothing in this module writes to the ledger.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import metrics, services, times

FREE_KINDS = ("free_bet_snr", "free_bet_sr")
REWARD_CATEGORIES = ("promotion_reward", "bonus_cash", "free_bet_winnings")
SOURCE_LABELS = {"qualifying": "Qualifying bets", "free": "Free bets and rewards", "other": "Other"}
VIEWS = ("week", "month", "year", "all")
WEEKS_SHOWN = 26
MIN_SLOTS = 12              # a chart always has room for at least 12 bars


def _uk(at: str) -> datetime:
    return times.parse(at).astimezone(times.UK)


def items(conn) -> list[dict]:
    """Every ledger amount that makes up realised profit, with its settlement date and what it came from."""
    rows = conn.execute(
        "SELECT e.id, e.amount_pence, e.category, e.position_id, e.offer_id AS entry_offer_id, a.name AS account, "
        "CASE WHEN e.class = 'stake' THEN l.settled_at ELSE e.at END AS at, "
        "p.kind, p.event, p.selection, p.offer_id AS position_offer_id "
        "FROM ledger_entries e JOIN accounts a ON a.id = e.account_id LEFT JOIN legs l ON l.id = e.leg_id "
        "LEFT JOIN positions p ON p.id = e.position_id "
        f"WHERE {metrics.ACTIVE} AND {metrics.SETTLED_OR_PNL} ORDER BY at, e.id").fetchall()
    # A position's bookmaker is its (first) back bet's; its offer is the position's, else the free bet's.
    backs = {}
    for r in conn.execute(
            "SELECT l.position_id, a.name AS account, f.offer_id FROM legs l JOIN accounts a ON a.id = l.account_id "
            "LEFT JOIN free_bets f ON f.id = l.free_bet_id WHERE l.side = 'back' ORDER BY l.deleted_at IS NOT NULL, l.id"):
        backs.setdefault(r["position_id"], dict(r))
    offers = {r["id"]: dict(r) for r in conn.execute("SELECT id, title, status FROM offers")}
    out = []
    for r in rows:
        d = dict(r)
        pid = d["position_id"]
        back = backs.get(pid, {}) if pid else {}
        offer_id = d["position_offer_id"] or back.get("offer_id") or d["entry_offer_id"]
        if pid:
            source = "qualifying" if d["kind"] == "qualifying" else ("free" if d["kind"] in FREE_KINDS else "other")
        else:
            source = "free" if d["category"] in REWARD_CATEGORIES else "other"
        d.update(local=_uk(d["at"]), bookmaker=back.get("account") or d["account"], offer_id=offer_id,
                 offer=(offers.get(offer_id) or {}).get("title"), source=source,
                 type=d["kind"] if pid else d["category"])
        out.append(d)
    return out


# ---------------------------------------------------------------- periods

def period_key(dt: datetime, view: str) -> str:
    if view == "week":
        return (dt.date() - timedelta(days=dt.weekday())).isoformat()       # Monday of that UK week
    if view in ("month", "year-months"):
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y")


def period_label(key: str, view: str) -> str:
    if view == "week":
        d = date.fromisoformat(key)
        return f"w/c {d.day} {d.strftime('%b %Y')}"
    if view in ("month", "year-months"):
        return date.fromisoformat(key + "-01").strftime("%B %Y")
    return key


def period_short(key: str, view: str) -> str:
    """Label under a chart bar."""
    if view == "week":
        d = date.fromisoformat(key)
        return f"{d.day}/{d.month}"
    if view in ("month", "year-months"):
        d = date.fromisoformat(key + "-01")
        return d.strftime("%b %Y") if d.month == 1 else d.strftime("%b")      # the year only where it changes
    return key


def _next(key: str, view: str) -> str:
    if view == "week":
        return (date.fromisoformat(key) + timedelta(days=7)).isoformat()
    if view in ("month", "year-months"):
        y, m = map(int, key.split("-"))
        return f"{y + m // 12}-{m % 12 + 1:02d}"
    return str(int(key) + 1)


def default_view(all_items: list[dict]) -> str:
    """Month once settled history spans two calendar months; until then a week view says more."""
    months = {i["local"].strftime("%Y-%m") for i in all_items}
    return "month" if len(months) >= 2 else "week"


def periods(all_items: list[dict], view: str, year: int | None = None) -> list[dict]:
    """Rows for the chart and table, oldest first, with no gaps up to the current period.
    view: week (last 26 weeks), month (every month), year (the months of one year), all (one row per year)."""
    now = times.now().astimezone(times.UK)
    kind = "year-months" if view == "year" else view
    sums: dict = {}
    for i in all_items:
        row = sums.setdefault(period_key(i["local"], kind), {"net": 0, "qualifying": 0, "free": 0, "other": 0, "count": 0})
        row["net"] += i["amount_pence"]
        row[i["source"]] += i["amount_pence"]
        row["count"] += 1
    current = period_key(now, kind)
    if view == "year":
        year = year or now.year
        first, last = f"{year}-01", (current if year == now.year else f"{year}-12")
    else:
        first = min(sums) if sums else current
        last = max(current, max(sums)) if sums else current
    keys, k = [], first
    while k <= last:
        keys.append(k)
        k = _next(k, kind)
    if view == "week":
        keys = keys[-WEEKS_SHOWN:]
    running = sum(i["amount_pence"] for i in all_items if period_key(i["local"], kind) < keys[0]) if keys else 0
    out = []
    for k in keys:
        row = sums.get(k, {"net": 0, "qualifying": 0, "free": 0, "other": 0, "count": 0})
        running += row["net"]
        out.append({"key": k, "label": period_label(k, kind), "short": period_short(k, kind), "running": running,
                    "current": k == current, **row})
    return out


def in_period(i: dict, view: str, key: str) -> bool:
    return period_key(i["local"], "year-months" if view == "year" else view) == key


def contributions(selected: list[dict]) -> list[dict]:
    """What produced a period's figure: one line per position (or per reward entry), newest first."""
    lines: dict = {}
    for i in selected:
        ref = ("position", i["position_id"]) if i["position_id"] else ("entry", i["id"])
        line = lines.setdefault(ref, {"position_id": i["position_id"], "event": i["event"] or services_label(i["category"]),
                                      "selection": i["selection"], "bookmaker": i["bookmaker"], "offer": i["offer"],
                                      "offer_id": i["offer_id"], "type": i["type"], "settled": i["at"], "result": 0})
        line["result"] += i["amount_pence"]
        line["settled"] = max(line["settled"], i["at"])
    return sorted(lines.values(), key=lambda line: line["settled"], reverse=True)


def services_label(category: str) -> str:
    return category.replace("_", " ").capitalize()


def breakdown(selected: list[dict], field: str) -> list[dict]:
    totals: dict = {}
    for i in selected:
        key = i[field] or "—"
        row = totals.setdefault(key, {"name": key, "net": 0, "positions": set()})
        row["net"] += i["amount_pence"]
        row["positions"].add(i["position_id"] or ("entry", i["id"]))
    return sorted(({"name": r["name"], "net": r["net"], "count": len(r["positions"])} for r in totals.values()),
                  key=lambda r: r["net"], reverse=True)


def completed_offers(conn) -> dict:
    """Average realised profit per completed offer (everything linked to it: positions, its free bets, rewards)."""
    done = [r["id"] for r in conn.execute("SELECT id FROM offers WHERE status = 'complete' AND deleted_at IS NULL")]
    total = sum(services.offer_realised(conn, oid) for oid in done)
    return {"count": len(done), "total": total, "average": round(total / len(done)) if done else None}


# ---------------------------------------------------------------- winnings: retained, withdrawn, plan

PLAN_KEY = "winnings_plan"


def plan_settings(conn) -> dict:
    plan = services.get_setting(conn, PLAN_KEY, {}) or {}
    return {"min_bankroll_pence": int(plan.get("min_bankroll_pence") or 0), "put_away_pct": int(plan.get("put_away_pct") or 0)}


def save_plan(conn, min_bankroll_pence: int, put_away_pct: int) -> None:
    if min_bankroll_pence < 0:
        raise services.UserError("The minimum bankroll can't be negative")
    if not 0 <= put_away_pct <= 100:
        raise services.UserError("Choose a percentage from 0 to 100")
    before = plan_settings(conn)
    after = {"min_bankroll_pence": min_bankroll_pence, "put_away_pct": put_away_pct}
    if before != after:
        services.set_setting(conn, PLAN_KEY, after)
        services.audit(conn, "setting", None, "winnings_plan", before, after, "planning figures only; nothing is moved")


def winnings(conn, s: metrics.Summary | None = None) -> dict:
    """Realised, withdrawn and retained profit, the cash that is free to move now, and what the plan suggests.
    Planning arithmetic only: it never moves money and says nothing about what is safe to invest."""
    s = s or metrics.summary(conn)
    plan = plan_settings(conn)
    month_start = times.month_start()
    completed = metrics.realised_profit(conn) - metrics.realised_profit(conn, month_start)   # before this month
    # Cash you could actually withdraw now: each betting account's balance less the lay liability it holds
    # (open back stakes have already left the balance), minus temporary funding that must go back first.
    free_cash = sum(max(0, r["balance"] - r["liability"]) for r in metrics.account_rows(conn) if r["kind"] != "bank")
    unlocked = max(0, free_cash - s.temporary_outstanding)
    bankroll = s.betting_balance + s.unsettled_stakes - s.temporary_outstanding     # your money in play
    above_minimum = max(0, bankroll - plan["min_bankroll_pence"])
    target = round(max(0, completed) * plan["put_away_pct"] / 100)
    still_to_put_away = max(0, target - s.profit_withdrawn)
    allocatable = max(0, s.retained_profit)
    suggestion = min(still_to_put_away, allocatable, unlocked, above_minimum) if plan["put_away_pct"] else None
    withdrawals = [dict(r) for r in conn.execute(
        "SELECT t.*, a.name AS from_name, b.name AS to_name FROM transfers t JOIN accounts a ON a.id = t.from_account_id "
        "JOIN accounts b ON b.id = t.to_account_id WHERE t.capital_type = 'profit_withdrawal' AND t.voided_at IS NULL "
        "ORDER BY t.at DESC")]
    return {"realised": s.profit_lifetime, "withdrawn": s.profit_withdrawn, "retained": s.retained_profit,
            "allocatable": allocatable, "unlocked": unlocked, "free_cash": free_cash, "bankroll": bankroll,
            "above_minimum": above_minimum, "completed": completed, "target": target,
            "still_to_put_away": still_to_put_away, "suggestion": suggestion, "plan": plan,
            "withdrawals": withdrawals, "month_start": month_start}


# ---------------------------------------------------------------- chart geometry (SVG, no inline styles)

def chart(rows: list[dict], cumulative: bool = True) -> dict:
    """Bars for each period's net profit (green up, red down) on one scale with the running total."""
    n = len(rows)
    values = [r["net"] for r in rows] + ([r["running"] for r in rows] if cumulative else []) + [0]
    hi, lo = max(values), min(values)
    span = (hi - lo) or 1
    pad = 6
    y = lambda v: round(pad + (hi - v) / span * (100 - 2 * pad), 2)
    zero = y(0)
    slots = max(n, MIN_SLOTS)                 # short histories keep normal bar widths, newest on the right
    off = slots - n
    bars = []
    for i, r in enumerate(rows):
        top, bottom = sorted((y(r["net"]), zero))
        bars.append({"x": (off + i) * 10 + 1.5, "w": 7, "y": top, "h": max(bottom - top, 0.4 if r["net"] else 0),
                     "cls": "pos" if r["net"] > 0 else ("neg" if r["net"] < 0 else "nil"), "row": r})
    line = " ".join(f"{(off + i) * 10 + 5},{y(r['running'])}" for i, r in enumerate(rows)) if cumulative and n else ""
    for i, b in enumerate(bars):          # where the interactive marker sits: the running-total point of that period
        b["px"] = round(((off + i) * 10 + 5) / (slots * 10) * 100, 3)
        b["py"] = y(b["row"]["running"])
    step = max(1, -(-n // 8))                                   # at most ~8 labels under the chart
    labels = [""] * off + [r["short"] if (n - 1 - i) % step == 0 else "" for i, r in enumerate(rows)]
    return {"w": slots * 10, "zero": zero, "bars": bars, "line": line, "labels": labels, "n": slots,
            "best": max(rows, key=lambda r: r["net"]) if rows else None,
            "worst": min(rows, key=lambda r: r["net"]) if rows else None}


def history(all_items: list[dict]) -> dict:
    """All time: cumulative realised profit as a step line over real time."""
    if not all_items:
        return {"path": "", "zero": 50, "first": None, "last": None, "points": 0}
    t0 = all_items[0]["local"].timestamp()
    t1 = max(times.now().timestamp(), all_items[-1]["local"].timestamp())
    span_t = (t1 - t0) or 1
    running, pts, steps = 0, [], []
    for i in all_items:                       # one step per settlement moment (a bet's stake, return and lay together)
        running += i["amount_pence"]
        x = (i["local"].timestamp() - t0) / span_t * 1000
        if pts and pts[-1][0] == x:
            pts[-1] = (x, running)
            steps[-1].update(running=running, change=steps[-1]["change"] + i["amount_pence"],
                             events=steps[-1]["events"] | {i["event"] or services_label(i["category"])})
        else:
            pts.append((x, running))
            steps.append({"x": x, "at": i["at"], "running": running, "change": i["amount_pence"],
                          "events": {i["event"] or services_label(i["category"])}})
    values = [v for _, v in pts] + [0]
    hi, lo = max(values), min(values)
    span = (hi - lo) or 1
    y = lambda v: round(6 + (hi - v) / span * 88, 2)
    path = f"M0,{y(0)}"
    for x, v in pts:
        path += f" H{x:.1f} V{y(v)}"
    path += " H1000"
    for st in steps:                          # marker position in percent of the chart box (x) and of its height (y)
        st.update(px=round(st["x"] / 10, 3), py=y(st["running"]), events=sorted(st["events"]))
    return {"path": path, "zero": y(0), "first": all_items[0]["at"], "last": all_items[-1]["at"], "points": len(pts),
            "high": hi, "low": lo, "steps": steps}
