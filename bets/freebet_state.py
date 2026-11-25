"""What has happened to a free bet, worked out from its records. Nothing here is stored and nothing is set by hand.

  not_placed  still to use
  planned     a live bet plan holds it, but its bookmaker bet is not recorded
  placed      its bet is recorded and the lay is fully matched
  uncovered   its bet is recorded but the lay is missing or part matched
  check       its bet is recorded, but the records don't show a fully matched lay or a missing one (the next lay of a
              double or acca is still to come, the lays are on different selections, or no bet is linked)
  missed      it expired unused
  void        voided
"""
from __future__ import annotations

from decimal import Decimal

from . import exposure, plans, services, times
from .money import fmt

# A lay worth less than this share of the one that evens out both results counts as part matched. Deliberate small
# underlays sit just below 1 (0.95); a lay that is really short is far lower.
MIN_COVER = Decimal("0.9")
LEG_OF_MULTIPLE = Decimal("0.6")      # lay odds below this share of the back odds lay another selection (one acca leg)
LIVE_PLAN = "status NOT IN ('cancelled', 'settled', 'converted_to_position')"
LABELS = {"not_placed": "Not placed", "planned": "Planned", "placed": "Placed", "uncovered": "Placed, not covered",
          "check": "Placed, check lay", "missed": "Missed", "void": "Void"}


# ---------------------------------------------------------------- words

def expiry_text(fb: dict) -> str:
    """'Wed 25 Nov 23:59', or 'Wed 25 Nov, about 23:59' when the time is an estimate."""
    if not fb.get("expires_at"):
        return "No expiry recorded"
    if fb.get("expiry_estimated"):
        return times.local(fb["expires_at"], "%a %-d %b, about %H:%M")
    return times.local(fb["expires_at"])


def usable_text(fb: dict, now: str | None = None) -> str:
    """'Not usable before Wed 25 Nov 18:00' while that is ahead, 'Usable since ...' after."""
    if not fb.get("usable_from"):
        return ""
    ahead = fb["usable_from"] > (now or times.now_iso())
    return ("Not usable before " if ahead else "Usable since ") + times.local(fb["usable_from"])


def event_time(at: str | None) -> str:
    """Just the time for today's events, with the day for later ones."""
    if not at:
        return ""
    d = times.parse(at).astimezone(times.UK)
    days = (d.date() - times.now().astimezone(times.UK).date()).days
    return d.strftime("%H:%M") if days == 0 else d.strftime("%a %H:%M" if 0 < days < 7 else "%a %-d %b %H:%M")


# ---------------------------------------------------------------- is the lay fully matched?

def _live_legs(conn, position_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT l.*, a.name AS account, f.kind AS token_kind FROM legs l JOIN accounts a ON a.id = l.account_id "
        "LEFT JOIN free_bets f ON f.id = l.free_bet_id WHERE l.position_id = ? AND l.deleted_at IS NULL "
        "AND l.result <> 'void' ORDER BY l.id", (position_id,))]


def _plan_shortfall(plan: dict, lays: list[dict], started_only: bool = False) -> tuple[int, int] | None:
    """(matched, wanted) of the plan's lays that are short, or None when every one is matched. Only fills whose lay is
    still on the position count; a lay added to the position by hand counts towards a short one. started_only leaves
    out lays nothing has been done about yet (the later legs of a double or acca)."""
    live = {l["id"]: l["stake_pence"] for l in lays}
    tied = {f["leg_id"] for l in plan["legs"] for f in l["fills"]}
    loose = sum(v for k, v in live.items() if k not in tied)
    matched = wanted = 0
    for l in plan["legs"]:
        if l["side"] != "lay":
            continue
        got = sum(live.get(f["leg_id"], 0) for f in l["fills"])
        target = plans._target(l)
        used = min(loose, max(0, target - got))
        loose -= used
        got += used
        if started_only and not got and not l["order_stake_pence"]:
            continue
        if not got or got + plans.TOLERANCE < target:
            matched, wanted = matched + got, wanted + target
    return (matched, wanted) if wanted else None


def cover(conn, position_id: int) -> dict:
    """How far a position's lays cover its bookmaker bets: state full / part / none / next_lay / check, with words."""
    legs = _live_legs(conn, position_id)
    backs = [l for l in legs if l["side"] == "back"]
    lays = [l for l in legs if l["side"] == "lay"]
    if not lays:
        return {"state": "none", "text": "No lay recorded"}
    where = " and ".join(dict.fromkeys(l["account"] for l in lays))
    total = sum(l["stake_pence"] for l in lays)
    full = {"state": "full", "text": f"Lay fully matched, {fmt(total)} at {where}"}
    r = conn.execute("SELECT id FROM bet_plans WHERE position_id = ? AND status <> 'cancelled' ORDER BY id DESC LIMIT 1",
                     (position_id,)).fetchone()
    plan = plans.load(conn, r[0]) if r else None
    planned_backs = {l["leg_id"] for l in plan["legs"] if l["side"] == "back"} if plan else set()
    from_plan = plan and any(l["side"] == "lay" for l in plan["legs"]) and all(b["id"] in planned_backs for b in backs)
    # An outcome table with a later lay still to come: a double or acca laid one leg at a time.
    in_turn = exposure.after_next_lay(conn, position_id) is not None
    if from_plan:
        short = _plan_shortfall(plan, lays, started_only=in_turn)
        if short:
            return {"state": "part", "text": f"Lay part matched, {fmt(short[0])} of {fmt(short[1])}"}
    if in_turn:
        return {"state": "next_lay", "text": f"{fmt(total)} laid at {where}, next lay still to place", "laid": f"{fmt(total)} laid at {where}"}
    if from_plan:
        return full

    if exposure.configured(conn, position_id):
        data = exposure.valid(conn, position_id)
        worst = min((sum(s["pnl"].values()) for s in data[1]), default=0) if data else 0
        return full if worst > 0 else {"state": "check", "text": f"{fmt(total)} laid at {where}, check the outcomes"}
    if len({l["exchange_selection"] for l in lays if l["exchange_selection"]}) > 1:
        return {"state": "check", "text": f"{fmt(total)} laid at {where} on several selections, check the lays"}
    if any(Decimal(l["odds"]) < LEG_OF_MULTIPLE * min(Decimal(b["odds"]) for b in backs) for l in lays):
        return {"state": "check", "text": f"{fmt(total)} laid at {where} at much shorter odds, check the lays"}
    need = sum(Decimal(b["stake_pence"]) * (Decimal(b["odds"]) - (1 if b["token_kind"] == "snr" else 0)) for b in backs)
    got = sum(Decimal(l["stake_pence"]) * (Decimal(l["odds"]) - Decimal(l["commission_bp"]) / 10000) for l in lays)
    if need and got / need < MIN_COVER:
        return {"state": "part", "text": f"Lay part matched, {fmt(total)} of about {fmt(int(total * need / got))}"}
    return full


# ---------------------------------------------------------------- one free bet

def live_bet(conn, free_bet_id: int) -> dict | None:
    """The bet that uses this free bet: a live leg (not removed, not void) of a live position."""
    r = conn.execute(
        "SELECT l.id AS leg_id, l.placed_at, l.result, p.id AS position_id, p.event, p.event_at, p.status FROM legs l "
        "JOIN positions p ON p.id = l.position_id WHERE l.free_bet_id = ? AND l.deleted_at IS NULL AND l.result <> 'void' "
        "AND p.deleted_at IS NULL AND p.status <> 'void' ORDER BY l.id LIMIT 1", (free_bet_id,)).fetchone()
    return dict(r) if r else None


def live_plan(conn, free_bet_id: int, now: str | None = None) -> dict | None:
    """The live bet plan holding this free bet. `note` says what doesn't add up, if anything."""
    r = conn.execute(f"SELECT id, event, event_at, status, position_id FROM bet_plans WHERE free_bet_id = ? AND {LIVE_PLAN} "
                     "ORDER BY id DESC LIMIT 1", (free_bet_id,)).fetchone()
    if not r:
        return None
    plan, note = dict(r), ""
    laid = plan["position_id"] and conn.execute(
        "SELECT 1 FROM legs WHERE position_id = ? AND side = 'lay' AND deleted_at IS NULL AND result <> 'void'",
        (plan["position_id"],)).fetchone()
    if laid:
        note = "a lay is matched, but the bookmaker bet is not recorded"
    elif plan["status"] not in plans.PRE_PLACEMENT:
        note = "the plan says it is placed, but no bookmaker bet is recorded"
    elif plan["event_at"] and plan["event_at"] <= (now or times.now_iso()):
        note = "already started, not recorded as placed"
    at = event_time(plan["event_at"])
    return {**plan, "note": note, "text": f"Planned: {plan['event']}" + (f", {at}" if at else "")}


def derive(conn, fb: dict, now: str | None = None) -> dict:
    """{status, label, text, bet, plan} for one free bet (a free_bets row)."""
    now = now or times.now_iso()

    def out(status, text="", **kw):
        return {"status": status, "label": LABELS[status], "text": text, "bet": None, "plan": None, "cover": None, **kw}

    bet = live_bet(conn, fb["id"])
    if bet:
        c = cover(conn, bet["position_id"])
        if c["state"] == "next_lay" and bet["status"] != "open":     # settled: no next lay is coming
            c = {"state": "full", "text": c["laid"]}
        status = {"full": "placed", "part": "uncovered", "none": "uncovered"}.get(c["state"], "check")
        done = " · settled" if bet["status"] == "settled" else ""
        return out(status, c["text"] + done, bet=bet, cover=c["state"])
    if fb["status"] == "void":
        return out("void")
    if fb["status"] == "used":
        return out("check", "Marked used, but no bet is linked to it")
    if fb["status"] == "expired" or (fb["expires_at"] and fb["expires_at"] <= now):
        return out("missed", "Expired unused")
    plan = live_plan(conn, fb["id"], now)
    if plan:
        return out("planned", plan["text"] + (f" ({plan['note']})" if plan["note"] else ""), plan=plan)
    return out("not_placed")


# ---------------------------------------------------------------- the assistant's view

def view(conn, fb: dict) -> dict:
    """The worked-out state for the read API and MCP tools."""
    st = derive(conn, fb)
    bet, plan = st["bet"], st["plan"]
    return {"status": st["status"], "label": st["label"], "detail": st["text"] or None,
            "plan": {"id": plan["id"], "event": plan["event"], "event_at": times.local(plan["event_at"], "%Y-%m-%d %H:%M")
                     if plan["event_at"] else None} if plan else None,
            "position_id": bet["position_id"] if bet else None,
            "lay_fully_matched": (st["cover"] == "full" if st["cover"] in ("full", "part", "none") else None) if bet else None}
