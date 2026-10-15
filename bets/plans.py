"""Bet plans: a matched bet from proposal to settlement.

A plan holds the proposed bookmaker bet(s) and exchange lay(s) with their outcome model, works out stakes and the
result in every outcome (bets/outcomes.py), keeps every price revision, validates the plan against the offer, the
free bet, the exchange balance and the retention policy, and then tracks placement:

  draft -> validated / ready_to_place -> bookmaker_placed -> exchange_partially_matched -> fully_matched -> settled
           (cancelled at any point before a bet is placed; converted_to_position when recorded in one go as planned)

A plan holds no money. The moment a bet is really placed (a bookmaker placement or an exchange fill), the plan creates
its position and adds that bet as a leg through the normal services: the stake, the free bet and the exchange
liability are then in the ledger and every figure, exactly as if the bet had been entered by hand. Each exchange fill
is its own lay leg (its own price), so partial matches and rematches at other prices are exact. For plans with an
explicit outcome model the position's outcome table (bets/exposure.py) is kept in step, so liability and projections
use the real outcomes.
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import calc, commission, exposure, normalise, outcomes, services, times
from .money import fmt, odds_text, parse_odds, plain
from .services import UserError

ACTIVE = ("draft", "validated", "ready_to_place", "bookmaker_placed", "exchange_partially_matched", "fully_matched")
PRE_PLACEMENT = ("draft", "validated", "ready_to_place")
CLOSED = ("cancelled", "converted_to_position", "settled")
STATUS_TITLES = {"draft": "Draft", "validated": "Validated (with warnings)", "ready_to_place": "Ready to place",
                 "bookmaker_placed": "Bookmaker bet placed", "exchange_partially_matched": "Lay partly matched",
                 "fully_matched": "Fully matched", "cancelled": "Cancelled", "converted_to_position": "Recorded",
                 "settled": "Settled"}
POLICY_SETTING = "retention_policy"
DEFAULT_POLICY = {"snr": {"excellent": 80, "very_good": 75, "acceptable": 70, "reject_below": 65},
                  "sr": {"excellent": 95, "very_good": 90, "acceptable": 85, "reject_below": 80},
                  "qualifying": {"excellent_loss_pct": 3, "good_loss_pct": 5, "max_loss_pct": 10,
                                 "unhedged_max_loss": 0.50}}
TOLERANCE = 1       # pence: a fill within this of the target counts as complete


def unhedged_limit(conn) -> int:
    """Pence: the largest cash qualifier the user is happy to leave unlaid (policy qualifying.unhedged_max_loss, £)."""
    return int(round(float(policy(conn)["qualifying"]["unhedged_max_loss"]) * 100))


class Conflict(UserError):
    """The plan changed since the caller read it (version mismatch)."""


# ---------------------------------------------------------------- retention policy

def policy(conn) -> dict:
    saved = services.get_setting(conn, POLICY_SETTING, {}) or {}
    out = json.loads(json.dumps(DEFAULT_POLICY))
    for kind, values in saved.items():
        if kind in out and isinstance(values, dict):
            out[kind].update({k: float(v) for k, v in values.items() if k in out[kind]})
    return out


def save_policy(conn, data: dict) -> dict:
    before = policy(conn)
    new = json.loads(json.dumps(before))
    for kind, values in (data or {}).items():
        if kind in new and isinstance(values, dict):
            for k, v in values.items():
                if k in new[kind]:
                    v = float(v)
                    if not 0 <= v <= 100:
                        raise UserError(f"{kind}.{k}: a percentage between 0 and 100")
                    new[kind][k] = v
    services.set_setting(conn, POLICY_SETTING, new)
    services.audit(conn, "setting", None, "retention_policy", before, new, "retention policy changed")
    return new


def rate(conn, kind: str, figures: dict, override: float | None = None) -> dict:
    """Rating of a plan's economics against the policy: for free bets by retention %, for qualifiers by loss %."""
    pol = policy(conn)
    free = outcomes.FREE.get(kind)
    if free:
        p = pol[free]
        pct = figures.get("retention_pct")
        minimum = override if override is not None else p["acceptable"]
        if pct is None:
            return {"rating": "unknown", "minimum": minimum}
        rating = ("excellent" if pct >= p["excellent"] else "very_good" if pct >= p["very_good"] else
                  "acceptable" if pct >= p["acceptable"] else "poor" if pct >= p["reject_below"] else "reject")
        return {"rating": rating, "measure": "retention_pct", "value": pct, "minimum": minimum,
                "reject_below": p["reject_below"], "distance_from_minimum": round(pct - minimum, 1),
                "meets_minimum": pct >= minimum}
    p = pol["qualifying"]
    loss_pct = figures.get("qualifying_loss_pct")
    if loss_pct is None:
        return {"rating": "unknown"}
    maximum = override if override is not None else p["max_loss_pct"]
    rating = ("excellent" if loss_pct <= p["excellent_loss_pct"] else "good" if loss_pct <= p["good_loss_pct"] else
              "acceptable" if loss_pct <= maximum else "poor")
    return {"rating": rating, "measure": "qualifying_loss_pct", "value": loss_pct, "maximum": maximum,
            "distance_from_maximum": round(maximum - loss_pct, 1), "meets_minimum": loss_pct <= maximum}


# ---------------------------------------------------------------- loading

def _need(conn, pid: int) -> dict:
    r = conn.execute("SELECT * FROM bet_plans WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise UserError(f"No bet plan #{pid}")
    return dict(r)


def load(conn, pid: int) -> dict:
    plan = _need(conn, pid)
    plan["buckets"] = json.loads(plan["outcomes_json"]) or outcomes.simple_buckets(plan["selection"])
    plan["explicit_outcomes"] = bool(json.loads(plan["outcomes_json"]))
    plan["legs"] = []
    for r in conn.execute("SELECT p.*, a.name AS account, a.kind AS account_kind FROM plan_legs p JOIN accounts a ON "
                          "a.id = p.account_id WHERE p.plan_id = ? ORDER BY p.side, p.id", (pid,)):
        leg = dict(r)
        leg["covers"] = json.loads(leg["covers_json"]) or [plan["buckets"][0]]
        leg["fills"] = [dict(f) for f in conn.execute("SELECT * FROM plan_fills WHERE plan_leg_id = ? ORDER BY id",
                                                      (leg["id"],))]
        leg["matched_pence"] = sum(f["stake_pence"] for f in leg["fills"])
        plan["legs"].append(leg)
    return plan


def _free(plan: dict) -> str | None:
    return outcomes.FREE.get(plan["kind"])


def _engine_leg(plan: dict, leg: dict, stake: int, odds) -> dict:
    return {"side": leg["side"], "stake_pence": stake, "odds": str(odds), "commission_bp": leg["commission_bp"],
            "covers": leg["covers"], "free": _free(plan) if leg["side"] == "back" else None,
            "account_id": leg["account_id"]}


def _target(leg: dict) -> int:
    """How much this lay is meant to be matched: the order if one was submitted, else the planned stake."""
    return int(leg["order_stake_pence"] or leg["stake_pence"] or 0)


def _remaining(leg: dict) -> int:
    return max(0, _target(leg) - leg["matched_pence"] - leg["unmatched_cancelled_pence"])


# ---------------------------------------------------------------- figures

def _auto_stakes(plan: dict) -> dict:
    """Server-calculated stakes for lays marked automatic, given the backs and any fixed lays."""
    backs = [_engine_leg(plan, l, int(l["stake_pence"]), l["odds"]) for l in plan["legs"] if l["side"] == "back"]
    auto = [l for l in plan["legs"] if l["side"] == "lay" and l["stake_auto"]]
    fixed = [_engine_leg(plan, l, int(l["stake_pence"]), l["odds"]) for l in plan["legs"]
             if l["side"] == "lay" and not l["stake_auto"] and l["stake_pence"]]
    if not backs or not auto:
        return {}
    h = outcomes.hedge(plan["buckets"], backs, [_engine_leg(plan, l, 1, l["odds"]) for l in auto],
                       max_liability=plan["max_liability_pence"], fixed=fixed)
    return {l["id"]: s for l, s in zip(auto, h["lay_stakes"])}


def recompute_stakes(conn, plan_id: int) -> None:
    plan = load(conn, plan_id)
    for leg_id, stake in _auto_stakes(plan).items():
        conn.execute("UPDATE plan_legs SET stake_pence = ?, updated_at = ? WHERE id = ?", (stake, times.now_iso(), leg_id))


def figures(conn, plan: dict, *, planned: bool = True) -> dict:
    """The plan's numbers. planned=True: as proposed (stakes and prices of the plan). planned=False: what is really
    placed and matched so far (backs placed, lay fills), plus what is still unmatched."""
    legs = []
    for l in plan["legs"]:
        if l["side"] == "back":
            if planned:
                legs.append(_engine_leg(plan, l, int(l["stake_pence"]), l["odds"]))
            elif l["status"] == "placed":
                legs.append(_engine_leg(plan, l, int(l["placed_stake_pence"]), l["placed_odds"]))
        elif planned:
            if l["stake_pence"]:
                legs.append(_engine_leg(plan, l, int(l["stake_pence"]), l["odds"]))
        else:
            legs += [_engine_leg(plan, l, f["stake_pence"], f["odds"]) for f in l["fills"]]
    out = {"basis": "planned" if planned else "placed_so_far", "buckets": plan["buckets"]}
    backs = [l for l in legs if l["side"] == "back"]
    lays = [l for l in legs if l["side"] == "lay"]
    if not backs and not lays:
        return {**out, "table": None}
    if len(backs) + len(lays) and backs and lays:
        tab = outcomes.table(plan["buckets"], legs)
    else:
        tab = {"buckets": [{"outcome": b, "legs": [outcomes.leg_pnl(l, b) for l in legs],
                            "total": sum(outcomes.leg_pnl(l, b) for l in legs)} for b in plan["buckets"]]}
        totals = [r["total"] for r in tab["buckets"]]
        tab.update(worst=min(totals), best=max(totals), spread=max(totals) - min(totals), locked=False,
                   guaranteed=min(totals), worst_outcome=tab["buckets"][totals.index(min(totals))]["outcome"],
                   best_outcome=tab["buckets"][totals.index(max(totals))]["outcome"])
    out["table"] = tab
    out["liability"] = outcomes.lay_liability_total(lays)
    out["exchange_exposure"] = {a: outcomes.exchange_exposure(plan["buckets"], legs, a)
                                for a in {l["account_id"] for l in lays}}
    out["lay_stake"] = sum(l["stake_pence"] for l in lays)
    face = sum(l["stake_pence"] for l in backs)
    if _free(plan) and face:
        out["face_value"] = face
        out["retention_pct"] = round(tab["worst"] / face * 100, 1)
    elif face:
        out["qualifying_loss"] = min(0, tab["worst"])
        out["qualifying_loss_pct"] = round(max(0, -tab["worst"]) / face * 100, 1)
    if not planned:
        unmatched = [{"plan_leg_id": l["id"], "label": l["label"], "remaining": _remaining(l),
                      "odds": l["order_odds"] or l["odds"],
                      "liability_if_matched": calc.lay_liability(_remaining(l), Decimal(str(l["order_odds"] or l["odds"])))}
                     for l in plan["legs"] if l["side"] == "lay" and _remaining(l) > 0]
        out["unmatched"] = unmatched
        out["fully_hedged"] = not unmatched and all(
            l["matched_pence"] + TOLERANCE >= _target(l) for l in plan["legs"] if l["side"] == "lay")
    return out


def weighted_odds(fills: list[dict]) -> str | None:
    total = sum(f["stake_pence"] for f in fills)
    if not total:
        return None
    return format((sum(Decimal(f["stake_pence"]) * Decimal(str(f["odds"])) for f in fills) / Decimal(total)).quantize(
        Decimal("0.01")), "f")


def unmatched_order_reserve(conn, account_id: int | None = None) -> int:
    """Liability exchanges hold for submitted lay orders not matched yet (separate from matched liability)."""
    total = 0
    q = ("SELECT pl.*, (SELECT coalesce(sum(stake_pence), 0) FROM plan_fills f WHERE f.plan_leg_id = pl.id) AS matched "
         "FROM plan_legs pl JOIN bet_plans p ON p.id = pl.plan_id WHERE pl.side = 'lay' AND pl.order_stake_pence IS NOT NULL "
         "AND p.status IN ('" + "','".join(ACTIVE) + "')")
    args: tuple = ()
    if account_id is not None:
        q += " AND pl.account_id = ?"
        args = (account_id,)
    for r in conn.execute(q, args):
        left = max(0, r["order_stake_pence"] - r["matched"] - r["unmatched_cancelled_pence"])
        if left:
            total += calc.lay_liability(left, Decimal(str(r["order_odds"] or r["odds"])))
    return total


# ---------------------------------------------------------------- funding

def funding(conn, plan: dict) -> dict:
    """For each exchange in the plan: free cash now, what this plan's unmatched lays still need, the shortfall, and
    temporary funding linked to the plan (and whether it can go back)."""
    out = []
    for account_id in sorted({l["account_id"] for l in plan["legs"] if l["side"] == "lay"}):
        exp = services.expected_balance(conn, account_id, "available")
        reserve = unmatched_order_reserve(conn, account_id)
        own_reserve = sum(calc.lay_liability(_remaining(l), Decimal(str(l["order_odds"] or l["odds"])))
                          for l in plan["legs"] if l["side"] == "lay" and l["account_id"] == account_id
                          and l["order_stake_pence"] and _remaining(l))
        available = exp["available"] - reserve
        needed = 0
        for l in plan["legs"]:
            if l["side"] == "lay" and l["account_id"] == account_id and plan["status"] not in CLOSED:
                still = max(0, int(l["stake_pence"] or 0) - l["matched_pence"] - l["unmatched_cancelled_pence"])
                if l["order_stake_pence"]:
                    still = 0                      # an order already holds its liability (in `reserve`)
                needed += calc.lay_liability(still, Decimal(str(l["odds"]))) if still else 0
        acc = services.row(conn, "accounts", account_id)
        out.append({"account_id": account_id, "exchange": acc["name"], "total_funds": exp["total"],
                    "matched_liability_held": exp["liability"], "unmatched_order_reserve": reserve,
                    "this_plans_orders_reserve": own_reserve, "available_now": available,
                    "new_liability_required": needed, "shortfall": max(0, needed - available),
                    "available_after_bet": available - needed})
    linked = [dict(r) for r in conn.execute(
        "SELECT t.*, a.name AS from_name, b.name AS to_name FROM transfers t JOIN accounts a ON a.id = t.from_account_id "
        "JOIN accounts b ON b.id = t.to_account_id WHERE t.plan_id = ? AND t.voided_at IS NULL ORDER BY t.at", (plan["id"],))]
    released = plan["status"] in CLOSED or (plan["position_id"] and services.row(conn, "positions", plan["position_id"])
                                            ["status"] != "open")
    temp_in = [t for t in linked if t["capital_type"] == "temporary_funding"]
    returnable = []
    if released:
        for t in temp_in:
            outstanding = services.outstanding_temporary(conn, t["from_account_id"])
            if outstanding:
                returnable.append({"transfer_id": t["id"], "bank": t["from_name"], "exchange": t["to_name"],
                                   "amount": min(t["amount_pence"], outstanding)})
    banks = [{"bank": r["name"], "account_id": r["id"], "temporary_outstanding": services.outstanding_temporary(conn, r["id"])}
             for r in conn.execute("SELECT id, name FROM accounts WHERE kind = 'bank' AND active = 1 ORDER BY name")]
    return {"exchanges": out, "shortfall": sum(e["shortfall"] for e in out), "linked_transfers": linked,
            "liability_released": bool(released), "returnable": returnable, "banks": banks}


# ---------------------------------------------------------------- creating and editing

def _account(conn, ref, kinds: tuple, what: str) -> dict:
    if ref in (None, ""):
        raise UserError(f"{what}: required")
    if isinstance(ref, int) or str(ref).lstrip("#").isdigit():
        acc = services.row(conn, "accounts", int(str(ref).lstrip("#")))
    else:
        acc = conn.execute("SELECT * FROM accounts WHERE name = ? COLLATE NOCASE", (str(ref).strip(),)).fetchone()
        acc = dict(acc) if acc else None
    if not acc or acc["kind"] not in kinds:
        raise UserError(f"{what}: no {' / '.join(kinds)} called '{str(ref)[:60]}'")
    return acc


def _odds(value, what: str) -> str:
    try:
        o = parse_odds(value)
    except (ValueError, TypeError):
        raise UserError(f"{what}: valid odds (e.g. 6.5)") from None
    if o <= 1:
        raise UserError(f"{what}: odds must be above 1")
    return odds_text(o)


def _pence(value, what: str) -> int:
    from .money import to_pence
    try:
        return to_pence(str(value), allow_zero=False)
    except (ValueError, TypeError) as exc:
        raise UserError(f"{what}: {exc}") from None


def _covers(raw, buckets: list[str], default: list[str], what: str) -> list[str]:
    covers = [str(c).strip() for c in (raw or []) if str(c).strip()] or default
    bad = [c for c in covers if c not in buckets]
    if bad:
        raise UserError(f"{what}: unknown outcome(s) {', '.join(bad)}; outcomes are {', '.join(buckets)}")
    return covers


def snapshot_legs(plan: dict) -> list[dict]:
    return [{"plan_leg_id": l["id"], "side": l["side"], "account": l["account"], "label": l["label"],
             "stake": plain(l["stake_pence"]) if l["stake_pence"] else None, "odds": l["odds"],
             "commission_bp": l["commission_bp"], "covers": l["covers"]} for l in plan["legs"]]


def figures_summary(conn, plan: dict) -> dict:
    f = figures(conn, plan)
    t = f.get("table") or {}
    return {"lay_stake": plain(f.get("lay_stake", 0)), "liability": plain(f.get("liability", 0)),
            "worst": plain(t.get("worst")) if t else None, "best": plain(t.get("best")) if t else None,
            "retention_pct": f.get("retention_pct"), "qualifying_loss_pct": f.get("qualifying_loss_pct")}


def _revision(conn, plan_id: int, reason: str) -> int:
    plan = load(conn, plan_id)
    n = (conn.execute("SELECT max(revision) FROM plan_revisions WHERE plan_id = ?", (plan_id,)).fetchone()[0] or 0) + 1
    conn.execute("INSERT INTO plan_revisions (plan_id, revision, reason, legs_json, figures_json, actor, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (plan_id, n, reason[:300], json.dumps(snapshot_legs(plan)),
                                            json.dumps(figures_summary(conn, plan)), services.current_actor(),
                                            times.now_iso()))
    return n


def duplicates(conn, *, event_key: str, selection_key: str, back_account_id: int, stake: int, exclude_plan=None) -> list[str]:
    """Live plans and open positions that look like the same bet (same event, selection, bookmaker and stake)."""
    found = []
    for r in conn.execute("SELECT p.id FROM bet_plans p JOIN plan_legs l ON l.plan_id = p.id AND l.side = 'back' WHERE "
                          "p.status IN ('" + "','".join(ACTIVE) + "') AND p.event_key = ? AND p.selection_key = ? AND "
                          "l.account_id = ? AND l.stake_pence = ? AND p.id IS NOT ?",
                          (event_key, selection_key, back_account_id, stake, exclude_plan)):
        found.append(f"plan #{r[0]}")
    al = normalise.aliases(conn)
    for r in conn.execute("SELECT p.id, p.event, p.selection FROM positions p JOIN legs l ON l.position_id = p.id AND "
                          "l.side = 'back' AND l.deleted_at IS NULL WHERE p.deleted_at IS NULL AND p.status = 'open' AND "
                          "l.account_id = ? AND l.stake_pence = ?", (back_account_id, stake)):
        if normalise.event_key(r["event"], al) == event_key and normalise.selection_key(r["selection"], al) == selection_key:
            if not conn.execute("SELECT 1 FROM bet_plans WHERE position_id = ? AND id IS ?", (r["id"], exclude_plan)).fetchone():
                found.append(f"position #{r['id']}")
    return found


def create(conn, data: dict) -> int:
    """data: event, event_at, market, selection, kind (qualifying / free_bet_snr / free_bet_sr), bookmaker, back_stake,
    back_odds, exchange, lay_odds, commission (%), free_bet_id, offer_id, outcomes (bucket list), back_covers,
    lays (list of {exchange, odds, covers, stake, label, market}), min_retention_pct, max_liability, sport,
    builder_legs, notes, allow_duplicate."""
    from .money import parse_commission
    event = str(data.get("event") or "").strip()[:160]
    if not event:
        raise UserError("event: required")
    kind = data.get("kind") or "qualifying"
    if kind not in ("qualifying", "free_bet_snr", "free_bet_sr", "other"):
        raise UserError("kind: qualifying, free_bet_snr, free_bet_sr or other")
    fb = None
    if data.get("free_bet_id"):
        fb = services.row(conn, "free_bets", int(data["free_bet_id"]))
        if not fb or fb["deleted_at"]:
            raise UserError(f"free_bet_id: no free bet #{data['free_bet_id']}")
        kind = {"snr": "free_bet_snr", "sr": "free_bet_sr"}[fb["kind"]]
        held = conn.execute("SELECT id FROM bet_plans WHERE free_bet_id = ? AND status NOT IN ('cancelled', 'settled', "
                            "'converted_to_position')", (fb["id"],)).fetchone()
        if held:
            raise UserError(f"free_bet_id: free bet #{fb['id']} is already allocated to plan #{held[0]}")
        if services.token_in_use(conn, fb["id"]):
            raise UserError(f"free_bet_id: free bet #{fb['id']} is already linked to a bet")
    book = _account(conn, data.get("bookmaker") or (fb["account_id"] if fb else None), ("bookmaker", "exchange"), "bookmaker")
    if outcomes.FREE.get(kind) and not fb:
        # The token this plan uses: same bookmaker and type, not linked to a bet nor allocated to another live plan;
        # the stated stake picks its value, otherwise the one expiring first.
        allocated = tuple(r[0] for r in conn.execute(
            "SELECT free_bet_id FROM bet_plans WHERE free_bet_id IS NOT NULL AND status NOT IN ('cancelled', 'settled', "
            "'converted_to_position')"))
        if data.get("back_stake"):
            fb = services.choose_token(conn, account_id=book["id"], value=_pence(data.get("back_stake"), "back_stake"),
                                       kind=outcomes.FREE[kind], exclude=allocated)
        else:
            r = conn.execute(
                "SELECT * FROM free_bets f WHERE f.account_id = ? AND f.kind = ? AND f.status = 'available' "
                "AND f.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM legs l WHERE l.free_bet_id = f.id "
                "AND l.deleted_at IS NULL AND l.result <> 'void') "
                + (f"AND f.id NOT IN ({','.join('?' * len(allocated))}) " if allocated else "") +
                "ORDER BY f.expires_at IS NULL, f.expires_at, f.created_at, f.id LIMIT 1",
                (book["id"], outcomes.FREE[kind], *allocated)).fetchone()
            fb = dict(r) if r else None
            if not fb:
                held = [f"plan #{r[0]}" for r in conn.execute(
                    "SELECT p.id FROM bet_plans p JOIN free_bets f ON f.id = p.free_bet_id WHERE f.account_id = ? "
                    "AND f.kind = ? AND p.status NOT IN ('cancelled', 'settled', 'converted_to_position')",
                    (book["id"], outcomes.FREE[kind]))]
                raise UserError(f"back_stake: no unallocated {outcomes.FREE[kind].upper()} free bet at {book['name']}"
                                + (f" (already allocated to {', '.join(held)})" if held else "")
                                + "; give the stake or free_bet_id")
    stake = int(fb["face_value_pence"]) if fb and not data.get("back_stake") else _pence(data.get("back_stake"), "back_stake")
    if fb and stake != fb["face_value_pence"]:
        raise UserError(f"back_stake: the free bet is worth {fmt(fb['face_value_pence'])}")
    back_odds = _odds(data.get("back_odds"), "back_odds")
    buckets = [str(b).strip()[:60] for b in (data.get("outcomes") or []) if str(b).strip()]
    selection = str(data.get("selection") or "").strip()[:160]
    all_buckets = buckets or outcomes.simple_buckets(selection)
    back_covers = _covers(data.get("back_covers"), all_buckets, [all_buckets[0]], "back_covers")
    lays_in = data.get("lays") or ([{"exchange": data.get("exchange"), "odds": data.get("lay_odds"),
                                     "commission": data.get("commission"), "stake": data.get("lay_stake")}]
                                   if data.get("exchange") or data.get("lay_odds") else [])
    if not isinstance(lays_in, list):
        raise UserError("lays: a list")
    lays = []
    for i, l in enumerate(lays_in, 1):
        ex = _account(conn, l.get("exchange") or data.get("exchange"), ("exchange",), f"lay {i} exchange")
        com = parse_commission(str(l["commission"])) if l.get("commission") not in (None, "") else ex["commission_bp"]
        stake_l = _pence(l["stake"], f"lay {i} stake") if l.get("stake") not in (None, "") else None
        lays.append({"account_id": ex["id"], "odds": _odds(l.get("odds"), f"lay {i} odds"), "commission_bp": com,
                     "covers": _covers(l.get("covers"), all_buckets, [all_buckets[0]], f"lay {i} covers"),
                     "stake": stake_l, "label": str(l.get("label") or "")[:80], "market": str(l.get("market") or data.get("market") or "")[:80]})
    if buckets:
        outcomes.check_model(all_buckets, [{"covers": back_covers}] + [{"covers": l["covers"]} for l in lays])
    al = normalise.aliases(conn)
    ek, sk = normalise.event_key(event, al), normalise.selection_key(selection, al)
    if not data.get("allow_duplicate"):
        dup = duplicates(conn, event_key=ek, selection_key=sk, back_account_id=book["id"], stake=stake)
        if dup:
            raise UserError(f"Looks like a bet already planned or placed ({', '.join(dup)}). Use that, or pass "
                            "allow_duplicate=true if this is really a second bet.")
    offer_id = int(data["offer_id"]) if data.get("offer_id") else (fb["offer_id"] if fb else None)
    if offer_id and not services.row(conn, "offers", offer_id):
        raise UserError(f"offer_id: no offer #{offer_id}")
    event_at = data.get("event_at") or None
    now = times.now_iso()
    cur = conn.execute(
        "INSERT INTO bet_plans (status, kind, event, event_key, event_at, market, market_key, selection, selection_key, "
        "offer_id, free_bet_id, outcomes_json, max_liability_pence, min_retention_pct, sport, builder_legs, notes, "
        "created_at, updated_at) VALUES ('draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, event, ek, event_at, str(data.get("market") or "")[:120], normalise.market_key(str(data.get("market") or "")),
         selection, sk, offer_id, fb["id"] if fb else None, json.dumps(buckets),
         _pence(data["max_liability"], "max_liability") if data.get("max_liability") else None,
         float(data["min_retention_pct"]) if data.get("min_retention_pct") not in (None, "") else None,
         str(data.get("sport") or "")[:40], int(data["builder_legs"]) if data.get("builder_legs") else None,
         str(data.get("notes") or "")[:2000], now, now))
    pid = cur.lastrowid
    conn.execute("INSERT INTO plan_legs (plan_id, side, account_id, label, covers_json, market, stake_pence, stake_auto, "
                 "odds, commission_bp, created_at, updated_at) VALUES (?, 'back', ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)",
                 (pid, book["id"], str(data.get("back_label") or "")[:80], json.dumps(back_covers if buckets else []),
                  str(data.get("market") or "")[:80], stake, back_odds, now, now))
    for l in lays:
        conn.execute("INSERT INTO plan_legs (plan_id, side, account_id, label, covers_json, market, stake_pence, stake_auto, "
                     "odds, commission_bp, created_at, updated_at) VALUES (?, 'lay', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (pid, l["account_id"], l["label"], json.dumps(l["covers"] if buckets else []), l["market"],
                      l["stake"], 0 if l["stake"] else 1, l["odds"], l["commission_bp"], now, now))
    recompute_stakes(conn, pid)
    _revision(conn, pid, "original quote")
    plan = load(conn, pid)
    services.audit(conn, "plan", pid, "created", None, {"event": event, "kind": kind, "legs": snapshot_legs(plan),
                                                        "figures": figures_summary(conn, plan)})
    return pid


def _bump(conn, plan_id: int, expected_version: int | None, changes: dict, action: str, note: str = "") -> None:
    plan = _need(conn, plan_id)
    if expected_version is not None and int(expected_version) != plan["version"]:
        raise Conflict(f"Plan #{plan_id} changed since you read it (version {expected_version} → {plan['version']}); "
                       "read it again and retry")
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE bet_plans SET {sets}{', ' if sets else ''}version = version + 1, updated_at = ? WHERE id = ?",
                 (*changes.values(), times.now_iso(), plan_id))
    if changes:
        services.audit(conn, "plan", plan_id, action, {k: plan[k] for k in changes}, changes, note)


def update(conn, plan_id: int, data: dict, expected_version: int | None = None) -> None:
    """Details of a plan not placed yet: event, time, market, selection, offer, free bet, notes, minimum retention,
    liability cap, sport, builder legs. Prices and stakes change through recalculate()."""
    plan = _need(conn, plan_id)
    changes = {}
    if plan["status"] not in PRE_PLACEMENT:
        allowed = {"notes"}
        if set(k for k in data if data[k] is not None) - allowed:
            raise UserError(f"Plan #{plan_id} is {plan['status'].replace('_', ' ')}: only its notes can change now")
    al = normalise.aliases(conn)
    for key in ("event", "market", "selection", "notes", "sport"):
        if data.get(key) is not None:
            changes[key] = str(data[key]).strip()[:2000 if key == "notes" else 160]
    if "event" in changes:
        changes["event_key"] = normalise.event_key(changes["event"], al)
    if "selection" in changes:
        changes["selection_key"] = normalise.selection_key(changes["selection"], al)
    if "market" in changes:
        changes["market_key"] = normalise.market_key(changes["market"])
    if data.get("event_at") is not None:
        changes["event_at"] = data["event_at"] or None
    if data.get("offer_id") is not None:
        changes["offer_id"] = int(data["offer_id"]) or None
    if data.get("min_retention_pct") is not None:
        changes["min_retention_pct"] = float(data["min_retention_pct"]) if data["min_retention_pct"] != "" else None
    if data.get("builder_legs") is not None:
        changes["builder_legs"] = int(data["builder_legs"]) if data["builder_legs"] else None
    if data.get("max_liability") is not None:
        changes["max_liability_pence"] = _pence(data["max_liability"], "max_liability") if data["max_liability"] else None
    if data.get("free_bet_id") is not None:
        fid = int(data["free_bet_id"]) if data["free_bet_id"] else None
        if fid:
            fb = services.row(conn, "free_bets", fid)
            back = conn.execute("SELECT * FROM plan_legs WHERE plan_id = ? AND side = 'back' ORDER BY id LIMIT 1",
                                (plan_id,)).fetchone()
            if not fb or fb["deleted_at"] or fb["account_id"] != back["account_id"] or fb["face_value_pence"] != back["stake_pence"]:
                raise UserError("free_bet_id: a free bet of this bookmaker for the back stake")
        changes["free_bet_id"] = fid
    changes = {k: v for k, v in changes.items() if plan.get(k) != v}
    try:
        _bump(conn, plan_id, expected_version, changes, "edited", data.get("reason") or "")
    except Exception as exc:                                  # the one-token-per-live-plan index
        if "UNIQUE" in str(exc):
            raise UserError("That free bet is already allocated to another live plan") from None
        raise
    if {"max_liability_pence"} & set(changes):
        recompute_stakes(conn, plan_id)
    if changes and plan["status"] in ("validated", "ready_to_place"):
        conn.execute("UPDATE bet_plans SET status = 'draft' WHERE id = ?", (plan_id,))   # needs validating again


def recalculate(conn, plan_id: int, *, back_odds=None, lay_odds=None, back_stake=None, leg_odds: dict | None = None,
                reason: str = "", expected_version: int | None = None) -> dict:
    """New prices (or back stake): keeps the old quote as a revision, recalculates automatic lay stakes and compares."""
    plan = load(conn, plan_id)
    if plan["status"] not in PRE_PLACEMENT:
        raise UserError(f"Plan #{plan_id} is {plan['status'].replace('_', ' ')}: prices can only change before placing")
    if expected_version is not None and int(expected_version) != plan["version"]:
        raise Conflict(f"Plan #{plan_id} changed since you read it (version {expected_version} → {plan['version']})")
    before = figures(conn, plan)
    changed = []
    now = times.now_iso()
    for leg in plan["legs"]:
        new_odds = None
        if leg_odds and str(leg["id"]) in {str(k) for k in leg_odds}:
            new_odds = _odds({str(k): v for k, v in leg_odds.items()}[str(leg["id"])], f"leg {leg['id']} odds")
        elif leg["side"] == "back" and back_odds not in (None, ""):
            new_odds = _odds(back_odds, "back_odds")
        elif leg["side"] == "lay" and lay_odds not in (None, "") and sum(1 for l in plan["legs"] if l["side"] == "lay") == 1:
            new_odds = _odds(lay_odds, "lay_odds")
        if new_odds and new_odds != leg["odds"]:
            conn.execute("UPDATE plan_legs SET odds = ?, updated_at = ? WHERE id = ?", (new_odds, now, leg["id"]))
            changed.append(f"{leg['side']} {leg['odds']} → {new_odds}")
        if leg["side"] == "back" and back_stake not in (None, ""):
            stake = _pence(back_stake, "back_stake")
            if plan["free_bet_id"]:
                raise UserError("back_stake: a free bet's stake is its value")
            if stake != leg["stake_pence"]:
                conn.execute("UPDATE plan_legs SET stake_pence = ?, updated_at = ? WHERE id = ?", (stake, now, leg["id"]))
                changed.append(f"back stake {plain(leg['stake_pence'])} → {plain(stake)}")
    if lay_odds not in (None, "") and sum(1 for l in plan["legs"] if l["side"] == "lay") > 1 and not leg_odds:
        raise UserError("lay_odds: this plan has several lays; give leg_odds {plan_leg_id: odds}")
    if not changed:
        return {"changed": False, "note": "Same prices: nothing to recalculate", "figures": before}
    recompute_stakes(conn, plan_id)
    rev = _revision(conn, plan_id, reason or "prices moved: " + "; ".join(changed))
    _bump(conn, plan_id, None, {"status": "draft"} if plan["status"] != "draft" else {}, "repriced",
          "; ".join(changed))
    services.audit(conn, "plan", plan_id, "price_revision", {"figures": figures_summary(conn, plan)},
                   {"revision": rev, "changes": changed}, reason)
    after_plan = load(conn, plan_id)
    after = figures(conn, after_plan)
    r_before, r_after = rate(conn, plan["kind"], before, plan["min_retention_pct"]), rate(conn, plan["kind"], after,
                                                                                        plan["min_retention_pct"])
    verdict = ("ACCEPTABLE" if r_after.get("meets_minimum") else
               "REJECT" if r_after.get("rating") == "reject" else "BELOW_MINIMUM")
    return {"changed": True, "revision": rev, "changes": changed, "before": before, "after": after,
            "rating_before": r_before, "rating_after": r_after, "result": verdict}


def cancel(conn, plan_id: int, reason: str, expected_version: int | None = None) -> bool:
    plan = load(conn, plan_id)
    if plan["status"] == "cancelled":
        return False
    if plan["status"] not in PRE_PLACEMENT:
        raise UserError("Bets of this plan are already placed: settle or void its position instead")
    _bump(conn, plan_id, expected_version, {"status": "cancelled", "cancel_reason": reason[:300]}, "cancelled", reason)
    return True


# ---------------------------------------------------------------- validation

def validate(conn, plan_id: int, *, liquidity: dict | None = None, allow_started: bool = False, store: bool = True) -> dict:
    """Everything worth checking before the user places the bets. SAFE_TO_PLACE, WARNING (placeable, read the
    warnings) or DO_NOT_PLACE (blockers)."""
    plan = load(conn, plan_id)
    blockers, warnings, checks = [], [], []

    def check(name, ok, detail="", block=True):
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            (blockers if block else warnings).append(f"{name}: {detail}" if detail else name)

    backs = [l for l in plan["legs"] if l["side"] == "back"]
    lays = [l for l in plan["legs"] if l["side"] == "lay"]
    now = times.now_iso()
    check("Has a bookmaker bet", bool(backs), "add the back bet")
    check("Has an exchange lay", bool(lays), "add the lay that hedges it")
    for l in plan["legs"]:
        check(f"{l['side'].capitalize()} {l['account']} stake and odds", bool(l["stake_pence"]) and Decimal(str(l["odds"])) > 1,
              "stake and odds above 1 needed")
    if plan["event_at"]:
        check("Event not started", allow_started or plan["event_at"] > now,
              f"it started {times.local(plan['event_at'])}")
    else:
        check("Event time known", False, "no start time recorded: settlement reminders won't fire", block=False)
    if plan["status"] in CLOSED:
        check("Plan is live", False, f"the plan is {plan['status'].replace('_', ' ')}")

    # offer
    offer = services.row(conn, "offers", plan["offer_id"]) if plan["offer_id"] else None
    if offer:
        check("Offer active", offer["status"] not in ("complete", "expired", "abandoned") and not offer["deleted_at"],
              f"the offer is {offer['status']}")
        if offer["expires_at"]:
            check("Offer not expired", offer["expires_at"] > now, f"it ended {times.local(offer['expires_at'])}")
        if offer["opt_in_required"]:
            check("Opted in", bool(offer["opted_in_at"]), "opt in on the bookmaker's promotions page first")
        if offer["deposit_required"]:
            check("Qualifying deposit made", bool(offer["deposit_done_at"]), "make the qualifying deposit first")
        if plan["kind"] == "qualifying" and backs:
            if offer["qualifying_stake_pence"]:
                check("Qualifying stake met", backs[0]["stake_pence"] >= offer["qualifying_stake_pence"],
                      f"stake {fmt(backs[0]['stake_pence'])} is below {fmt(offer['qualifying_stake_pence'])}")
            if offer["min_odds"]:
                check("Minimum odds met", Decimal(str(backs[0]["odds"])) >= Decimal(str(offer["min_odds"])),
                      f"odds {backs[0]['odds']} are below the minimum {offer['min_odds']}")
            if offer["account_id"]:
                check("Offer's bookmaker", backs[0]["account_id"] == offer["account_id"], "the bet is with a different bookmaker")
        if offer["required_market"]:
            want = normalise.market_key(offer["required_market"])
            check("Required market", not plan["market_key"] or plan["market_key"] == want,
                  f"the offer needs {offer['required_market']}, the plan is {plan['market'] or 'unspecified'}",
                  block=bool(plan["market_key"]))
        if offer["min_legs"]:
            check("Bet builder selections", (plan["builder_legs"] or 0) >= offer["min_legs"],
                  f"the offer needs at least {offer['min_legs']} selections (plan: {plan['builder_legs'] or 'not given'})",
                  block=plan["builder_legs"] is not None)
        if offer["required_sport"]:
            check("Required sport", not plan["sport"] or plan["sport"].lower() == offer["required_sport"].lower(),
                  f"the offer is for {offer['required_sport']}")

    # free bet
    if outcomes.FREE.get(plan["kind"]):
        fb = services.row(conn, "free_bets", plan["free_bet_id"]) if plan["free_bet_id"] else None
        check("Free bet chosen", bool(fb), "no matching free bet available for this bookmaker, value and type")
        if fb and backs:
            check("Free bet available", fb["status"] == "available" and not fb["deleted_at"], f"it is {fb['status']}")
            check("Free bet's bookmaker", fb["account_id"] == backs[0]["account_id"], "the free bet is with another bookmaker")
            check("Free bet value", fb["face_value_pence"] == backs[0]["stake_pence"], "the stake must equal its value")
            check("Free bet type", fb["kind"] == outcomes.FREE[plan["kind"]], f"it is {fb['kind'].upper()}")
            if fb["expires_at"]:
                check("Free bet not expired", fb["expires_at"] > now, f"it expired {times.local(fb['expires_at'])}")
            notes = (fb["notes"] or "").lower()
            if "builder" in notes and plan["market_key"] and plan["market_key"] != "bet_builder":
                check("Free bet restriction", False, "its notes say Bet Builder only", block=False)
            other = conn.execute("SELECT id FROM bet_plans WHERE free_bet_id = ? AND id <> ? AND status NOT IN "
                                 "('cancelled', 'settled', 'converted_to_position')", (fb["id"], plan_id)).fetchone()
            check("Free bet not allocated elsewhere", not other, f"plan #{other[0]} holds it" if other else "")

    # exchange
    for l in lays:
        acc = services.row(conn, "accounts", l["account_id"])
        warn = commission.stale_warning(acc, l["commission_bp"])
        check(f"{l['account']} commission", not warn, warn, block=False)
        if liquidity and str(l["id"]) in {str(k) for k in liquidity}:
            liq = _pence({str(k): v for k, v in liquidity.items()}[str(l["id"])], "liquidity")
            check(f"{l['account']} liquidity", liq >= (l["stake_pence"] or 0),
                  f"only {fmt(liq)} available at {l['odds']} for a {fmt(l['stake_pence'])} lay", block=False)
    fund = funding(conn, plan)
    for e in fund["exchanges"]:
        check(f"{e['exchange']} funds for the liability", e["shortfall"] <= 0,
              f"needs {fmt(e['new_liability_required'])}, available {fmt(e['available_now'])}: shortfall {fmt(e['shortfall'])}")

    # matching and duplicates
    if plan["explicit_outcomes"] and backs and lays:
        unhedged = [b for b in plan["buckets"] if any(b in bl["covers"] for bl in backs)
                    and not any(b in ll["covers"] for ll in lays)]
        check("Every winning outcome hedged", not unhedged, "not hedged: " + ", ".join(unhedged), block=False)
    if backs:
        dup = duplicates(conn, event_key=plan["event_key"], selection_key=plan["selection_key"],
                         back_account_id=backs[0]["account_id"], stake=backs[0]["stake_pence"], exclude_plan=plan_id)
        check("Not a duplicate", not dup, "looks like " + ", ".join(dup), block=False)

    # economics
    fig = figures(conn, plan)
    rating = rate(conn, plan["kind"], fig, plan["min_retention_pct"])
    tab = fig.get("table")
    if tab:
        if rating.get("rating") == "reject":
            check("Retention", False, f"{rating['value']}% is below {rating['reject_below']}%: wait for better prices")
        elif rating.get("meets_minimum") is False:
            check("Economics", False, f"{rating['measure'].replace('_', ' ')} {rating['value']}% misses the target",
                  block=False)
        face = sum(b["stake_pence"] for b in backs)
        too_good = (fig.get("retention_pct") is not None and fig["retention_pct"] > 100) or (
            plan["kind"] == "qualifying" and face and tab["worst"] > face * 5 // 100)
        check("Prices look right", not too_good,
              "the locked result is better than these bets normally allow: re-check every odds figure against the "
              "screenshots (a misread price is the usual cause) and which outcomes each bet covers", block=False)
        check("Locked in", tab.get("locked", False),
              f"the result varies from {fmt(tab['worst'], signed=True)} to {fmt(tab['best'], signed=True)}", block=False)
    status = "DO_NOT_PLACE" if blockers else ("WARNING" if warnings else "SAFE_TO_PLACE")
    exch = fund["exchanges"][0] if fund["exchanges"] else None
    hedge_advice = None
    if plan["kind"] == "qualifying" and backs:
        from .ops_operations import hedge_advice as _advice
        hedge_advice = _advice(conn, sum(b["stake_pence"] for b in backs if not b.get("free_bet_id")))
    result = {
        "status": status, "plan_id": plan_id, "blockers": blockers, "warnings": warnings, "checks": checks,
        "bookmaker": {"account": backs[0]["account"], "stake": plain(backs[0]["stake_pence"]), "odds": backs[0]["odds"],
                      "free_bet_id": plan["free_bet_id"]} if backs else None,
        "exchange": [{"plan_leg_id": l["id"], "account": l["account"], "label": l["label"] or None,
                      "lay_stake": plain(l["stake_pence"]), "lay_odds": l["odds"],
                      "liability": plain(calc.lay_liability(l["stake_pence"] or 0, Decimal(str(l["odds"])))),
                      "covers": l["covers"]} for l in lays],
        "hedge_advice": hedge_advice,
        "locked_profit": plain(tab["worst"]) if tab else None, "best_case": plain(tab["best"]) if tab else None,
        "retention_pct": fig.get("retention_pct"), "qualifying_loss_pct": fig.get("qualifying_loss_pct"),
        "rating": rating, "available_exchange_cash": plain(exch["available_now"]) if exch else None,
        "after_bet_available_cash": plain(exch["available_after_bet"]) if exch else None,
        "funding_shortfall": plain(fund["shortfall"]), "checked_at": times.now_iso(), "plan_version": plan["version"]}
    if store and plan["status"] in PRE_PLACEMENT:
        new_status = {"SAFE_TO_PLACE": "ready_to_place", "WARNING": "validated", "DO_NOT_PLACE": "draft"}[status]
        conn.execute("UPDATE bet_plans SET validation_json = ?, validated_at = ?, status = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(result), result["checked_at"], new_status, times.now_iso(), plan_id))
        services.audit(conn, "plan", plan_id, "validated", {"status": plan["status"]},
                       {"status": new_status, "result": status, "blockers": blockers, "warnings": warnings})
    return result


# ---------------------------------------------------------------- placement (money becomes real here)

def _plan_leg(plan: dict, plan_leg_id, side: str) -> dict:
    legs = [l for l in plan["legs"] if l["side"] == side]
    if plan_leg_id:
        legs = [l for l in legs if l["id"] == int(plan_leg_id)]
        if not legs:
            raise UserError(f"plan_leg_id: no {side} leg #{plan_leg_id} in plan #{plan['id']}")
    if len(legs) != 1:
        raise UserError(f"plan #{plan['id']} has {len(legs)} {side} legs: give plan_leg_id")
    return legs[0]


def ensure_position(conn, plan_id: int) -> int:
    plan = load(conn, plan_id)
    if plan["position_id"]:
        return plan["position_id"]
    pid = services.create_position(conn, event=plan["event"], market=plan["market"], selection=plan["selection"],
                                   event_at=plan["event_at"], kind=plan["kind"], offer_id=plan["offer_id"],
                                   expected_pnl=(figures(conn, plan).get("table") or {}).get("worst"),
                                   notes=f"From bet plan #{plan_id}", source="plan")
    conn.execute("UPDATE bet_plans SET position_id = ?, updated_at = ? WHERE id = ?", (pid, times.now_iso(), plan_id))
    services.audit(conn, "plan", plan_id, "position_created", None, {"position_id": pid})
    return pid


def _sync_outcomes(conn, plan_id: int) -> None:
    """Keep the position's outcome table in step with the plan's model (explicit outcome models only)."""
    plan = load(conn, plan_id)
    if not plan["explicit_outcomes"] or not plan["position_id"]:
        return
    pos_legs = exposure.legs_for(conn, plan["position_id"])
    covers = {}
    for l in plan["legs"]:
        if l["leg_id"]:
            covers[l["leg_id"]] = (l, l["covers"])
        for f in l["fills"]:
            if f["leg_id"]:
                covers[f["leg_id"]] = (l, l["covers"])
    if not pos_legs or any(pl["id"] not in covers for pl in pos_legs):
        return
    if not any(pl["side"] == "back" for pl in pos_legs) or not any(pl["side"] == "lay" for pl in pos_legs):
        return
    scenarios = []
    for b in plan["buckets"]:
        pnl = {str(pl["id"]): outcomes.leg_pnl({"side": pl["side"], "stake_pence": pl["stake_pence"], "odds": pl["odds"],
                                                 "commission_bp": pl["commission_bp"], "covers": covers[pl["id"]][1],
                                                 "free": _free(plan) if pl["side"] == "back" else None}, b)
               for pl in pos_legs}
        scenarios.append({"label": b, "back_wins": any(b in covers[pl["id"]][1] for pl in pos_legs if pl["side"] == "back"),
                          "pnl": pnl})
    if {True, False} == {s["back_wins"] for s in scenarios}:
        exposure.configure(conn, plan["position_id"], scenarios, f"Outcome model of bet plan #{plan_id} (calculated)")


def refresh_status(conn, plan_id: int) -> str:
    plan = load(conn, plan_id)
    if plan["status"] in ("cancelled", "settled"):
        return plan["status"]
    pos = services.row(conn, "positions", plan["position_id"]) if plan["position_id"] else None
    if pos and pos["status"] != "open":
        new = "settled"
    elif plan["status"] == "converted_to_position":
        return plan["status"]
    else:
        backs = [l for l in plan["legs"] if l["side"] == "back"]
        lays = [l for l in plan["legs"] if l["side"] == "lay"]
        backs_placed = backs and all(l["status"] == "placed" for l in backs)
        lays_done = lays and all(l["matched_pence"] + TOLERANCE >= _target(l) and _target(l) > 0 for l in lays)
        any_fill = any(l["fills"] for l in lays)
        if backs_placed and lays_done:
            new = "fully_matched"
        elif any_fill or any(l["order_stake_pence"] for l in lays):
            new = "exchange_partially_matched" if any_fill else ("bookmaker_placed" if backs_placed else plan["status"])
        elif backs_placed:
            new = "bookmaker_placed"
        else:
            new = plan["status"]
    if new != plan["status"]:
        conn.execute("UPDATE bet_plans SET status = ?, version = version + 1, updated_at = ? WHERE id = ?",
                     (new, times.now_iso(), plan_id))
        services.audit(conn, "plan", plan_id, "status", {"status": plan["status"]}, {"status": new})
    return new


def place_back(conn, plan_id: int, *, stake=None, odds=None, reference: str = "", placed_at: str | None = None,
               plan_leg_id=None) -> dict:
    """The user placed the bookmaker bet: record it as the position's back leg (stake leaves the account, or the free
    bet is used). Returns what was recorded and any differences from the plan."""
    plan = load(conn, plan_id)
    if plan["status"] in CLOSED:
        raise UserError(f"Plan #{plan_id} is {plan['status'].replace('_', ' ')}")
    leg = _plan_leg(plan, plan_leg_id, "back")
    if leg["status"] == "placed":
        return {"already": True, "leg_id": leg["leg_id"]}
    stake_p = _pence(stake, "stake") if stake not in (None, "") else int(leg["stake_pence"])
    odds_t = _odds(odds, "odds") if odds not in (None, "") else leg["odds"]
    notes = []
    if stake_p != leg["stake_pence"]:
        notes.append(f"stake {fmt(stake_p)} differs from the plan's {fmt(leg['stake_pence'])}")
    if Decimal(odds_t) != Decimal(str(leg["odds"])):
        notes.append(f"odds {odds_t} differ from the plan's {leg['odds']}")
    if plan["offer_id"] and plan["kind"] == "qualifying":
        offer = services.row(conn, "offers", plan["offer_id"])
        if offer["min_odds"] and Decimal(odds_t) < Decimal(str(offer["min_odds"])):
            notes.append(f"odds {odds_t} are below the offer's minimum {offer['min_odds']}: it may not qualify")
    pid = ensure_position(conn, plan_id)
    leg_id = services.add_leg(conn, pid, {"account_id": leg["account_id"], "side": "back", "stake_pence": stake_p,
                                          "odds": odds_t, "free_bet_id": plan["free_bet_id"],
                                          "placed_at": placed_at or times.now_iso(), "external_ref": reference,
                                          "source": "plan"})
    conn.execute("UPDATE plan_legs SET status = 'placed', placed_stake_pence = ?, placed_odds = ?, placed_at = ?, "
                 "reference = ?, leg_id = ?, updated_at = ? WHERE id = ?",
                 (stake_p, odds_t, placed_at or times.now_iso(), reference[:120], leg_id, times.now_iso(), leg["id"]))
    services.audit(conn, "plan", plan_id, "bookmaker_placed", None, {"plan_leg_id": leg["id"], "leg_id": leg_id,
                                                                     "stake_pence": stake_p, "odds": odds_t,
                                                                     "reference": reference}, "; ".join(notes))
    _sync_outcomes(conn, plan_id)
    refresh_status(conn, plan_id)
    return {"already": False, "leg_id": leg_id, "position_id": pid, "differences": notes}


def submit_order(conn, plan_id: int, *, stake, odds, plan_leg_id=None) -> dict:
    """A lay order sent to the exchange (matched or not yet). Its unmatched part holds liability separately."""
    plan = load(conn, plan_id)
    if plan["status"] in CLOSED:
        raise UserError(f"Plan #{plan_id} is {plan['status'].replace('_', ' ')}")
    leg = _plan_leg(plan, plan_leg_id, "lay")
    stake_p, odds_t = _pence(stake, "stake"), _odds(odds, "odds")
    if leg["order_stake_pence"] == stake_p and leg["order_odds"] == odds_t:
        return {"already": True}
    if leg["matched_pence"] > stake_p:
        raise UserError(f"{fmt(leg['matched_pence'])} is already matched: the order can't be smaller than that")
    conn.execute("UPDATE plan_legs SET order_stake_pence = ?, order_odds = ?, status = CASE WHEN status = 'planned' "
                 "THEN 'submitted' ELSE status END, updated_at = ? WHERE id = ?", (stake_p, odds_t, times.now_iso(), leg["id"]))
    services.audit(conn, "plan", plan_id, "exchange_order", {"order_stake_pence": leg["order_stake_pence"]},
                   {"plan_leg_id": leg["id"], "order_stake_pence": stake_p, "order_odds": odds_t})
    refresh_status(conn, plan_id)
    return {"already": False}


def fill(conn, plan_id: int, *, stake, odds, matched_at: str | None = None, plan_leg_id=None) -> dict:
    """Part (or all) of a lay matched at a price: added to the position as its own lay leg (exact liability)."""
    plan = load(conn, plan_id)
    if plan["status"] in CLOSED:
        raise UserError(f"Plan #{plan_id} is {plan['status'].replace('_', ' ')}")
    leg = _plan_leg(plan, plan_leg_id, "lay")
    stake_p, odds_t = _pence(stake, "stake"), _odds(odds, "odds")
    target = _target(leg)
    if target and leg["matched_pence"] + stake_p > target + TOLERANCE and not leg["order_stake_pence"]:
        pass                                                  # a lay larger than planned: allowed, reported below
    pid = ensure_position(conn, plan_id)
    leg_id = services.add_leg(conn, pid, {"account_id": leg["account_id"], "side": "lay", "stake_pence": stake_p,
                                          "odds": odds_t, "commission_bp": leg["commission_bp"],
                                          "placed_at": matched_at or times.now_iso(), "source": "plan"})
    now = times.now_iso()
    conn.execute("INSERT INTO plan_fills (plan_leg_id, stake_pence, odds, matched_at, leg_id, created_at) VALUES (?,?,?,?,?,?)",
                 (leg["id"], stake_p, odds_t, matched_at or now, leg_id, now))
    matched = leg["matched_pence"] + stake_p
    status = "fully_matched" if target and matched + TOLERANCE >= target else "partially_matched"
    conn.execute("UPDATE plan_legs SET status = ?, updated_at = ? WHERE id = ?", (status, now, leg["id"]))
    services.audit(conn, "plan", plan_id, "exchange_fill", {"matched_pence": leg["matched_pence"]},
                   {"plan_leg_id": leg["id"], "leg_id": leg_id, "stake_pence": stake_p, "odds": odds_t,
                    "matched_pence": matched})
    _sync_outcomes(conn, plan_id)
    refresh_status(conn, plan_id)
    return {"leg_id": leg_id, "position_id": pid, "matched": matched, "target": target,
            "over_target": bool(target and matched > target + TOLERANCE)}


def cancel_unmatched(conn, plan_id: int, *, plan_leg_id=None, reason: str = "") -> int:
    """The unmatched part of a lay order was cancelled on the exchange. Returns the amount cancelled."""
    plan = load(conn, plan_id)
    leg = _plan_leg(plan, plan_leg_id, "lay")
    left = _remaining(leg)
    if not left:
        return 0
    conn.execute("UPDATE plan_legs SET unmatched_cancelled_pence = unmatched_cancelled_pence + ?, status = CASE WHEN ? > 0 "
                 "THEN 'partially_matched' ELSE 'cancelled' END, updated_at = ? WHERE id = ?",
                 (left, leg["matched_pence"], times.now_iso(), leg["id"]))
    services.audit(conn, "plan", plan_id, "unmatched_cancelled", None, {"plan_leg_id": leg["id"], "cancelled_pence": left},
                   reason)
    refresh_status(conn, plan_id)
    return left


def mark_fully_matched(conn, plan_id: int, *, plan_leg_id=None, matched_stake=None, odds=None,
                       matched_at: str | None = None) -> dict:
    """The exchange shows the lay FULLY MATCHED. With matched_stake: any part not yet recorded is added as a fill at
    `odds`. Without fills and without matched_stake the plan's stake and odds are taken as matched."""
    plan = load(conn, plan_id)
    leg = _plan_leg(plan, plan_leg_id, "lay")
    total = _pence(matched_stake, "matched_stake") if matched_stake not in (None, "") else _target(leg)
    missing = total - leg["matched_pence"]
    if missing < -TOLERANCE:
        raise UserError(f"{fmt(leg['matched_pence'])} is already recorded as matched, more than {fmt(total)}")
    added = None
    if missing > TOLERANCE:
        price = odds if odds not in (None, "") else (leg["order_odds"] or leg["odds"])
        added = fill(conn, plan_id, stake=plain(missing), odds=price, matched_at=matched_at, plan_leg_id=leg["id"])
    if leg["order_stake_pence"] and total + TOLERANCE < leg["order_stake_pence"]:
        conn.execute("UPDATE plan_legs SET order_stake_pence = ? WHERE id = ?", (total, leg["id"]))
    conn.execute("UPDATE plan_legs SET status = 'fully_matched', updated_at = ? WHERE id = ?", (times.now_iso(), leg["id"]))
    if not leg["order_stake_pence"] and total != (leg["stake_pence"] or 0):
        conn.execute("UPDATE plan_legs SET order_stake_pence = ?, order_odds = coalesce(order_odds, odds) WHERE id = ?",
                     (total, leg["id"]))
    services.audit(conn, "plan", plan_id, "fully_matched", None, {"plan_leg_id": leg["id"], "matched_pence": total})
    refresh_status(conn, plan_id)
    return {"added_fill": added, "matched": total}


def record_matched_bet(conn, plan_id: int, *, back: dict | None = None, lays: list[dict] | None = None) -> dict:
    """Everything at once, as the user's confirmations show it: the bookmaker bet and every lay fill, then fully
    matched. All or nothing (the caller's transaction / savepoint)."""
    plan = load(conn, plan_id)
    if plan["status"] in CLOSED:
        raise UserError(f"Plan #{plan_id} is {plan['status'].replace('_', ' ')}")
    out = {"back": None, "lays": []}
    if any(l["status"] != "placed" for l in plan["legs"] if l["side"] == "back"):
        b = back or {}
        out["back"] = place_back(conn, plan_id, stake=b.get("stake"), odds=b.get("odds"), reference=b.get("reference") or "",
                                 placed_at=b.get("placed_at"), plan_leg_id=b.get("plan_leg_id"))
    lay_legs = [l for l in plan["legs"] if l["side"] == "lay"]
    given = lays if lays else [{"plan_leg_id": l["id"]} for l in lay_legs]
    for item in given:
        leg = _plan_leg(load(conn, plan_id), item.get("plan_leg_id") or (lay_legs[0]["id"] if len(lay_legs) == 1 else None),
                        "lay")
        for f in item.get("fills") or []:
            fill(conn, plan_id, stake=f["stake"], odds=f["odds"], matched_at=f.get("matched_at"), plan_leg_id=leg["id"])
        res = mark_fully_matched(conn, plan_id, plan_leg_id=leg["id"], matched_stake=item.get("stake"),
                                 odds=item.get("odds"), matched_at=item.get("matched_at"))
        out["lays"].append({"plan_leg_id": leg["id"], **res})
    status = refresh_status(conn, plan_id)
    if status != "fully_matched":
        raise UserError(f"After recording, plan #{plan_id} is {status.replace('_', ' ')}, not fully matched: check the lays")
    return out


def convert(conn, plan_id: int) -> int:
    """Record the plan as placed exactly as planned (when the user confirms they placed it that way)."""
    plan = load(conn, plan_id)
    if plan["status"] == "converted_to_position" or plan["position_id"] and plan["status"] in ("fully_matched", "settled"):
        return plan["position_id"]
    record_matched_bet(conn, plan_id)
    conn.execute("UPDATE bet_plans SET status = 'converted_to_position', version = version + 1, updated_at = ? WHERE id = ?",
                 (times.now_iso(), plan_id))
    services.audit(conn, "plan", plan_id, "status", {"status": "fully_matched"}, {"status": "converted_to_position"})
    return load(conn, plan_id)["position_id"]


def on_position_status(conn, position_id: int) -> None:
    for (pid,) in conn.execute("SELECT id FROM bet_plans WHERE position_id = ? AND status NOT IN ('cancelled', 'settled')",
                               (position_id,)).fetchall():
        refresh_status(conn, pid)


def live_for_free_bet(conn, free_bet_id: int) -> int | None:
    r = conn.execute("SELECT id FROM bet_plans WHERE free_bet_id = ? AND status NOT IN ('cancelled', 'settled', "
                     "'converted_to_position')", (free_bet_id,)).fetchone()
    return r[0] if r else None
