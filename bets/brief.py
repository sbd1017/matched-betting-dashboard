"""Read views for the operating workflow: bet plans, outcome tables, expected rewards, evidence, Next Actions and the
Operating Brief (one call that tells the assistant -- or the Operations page -- everything needed to decide what to
do next). Read-only. Money is a GBP string, times UK local, like bets/readapi.py.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from . import (calc, evidence, exposure, issues, metrics, outcomes, outlook, plans, readapi, rewards, services,
               times)
from .money import fmt, plain
from .readapi import m, t

PRIORITY = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------- plans

def _table_out(tab: dict | None, legs_meta: list[dict]) -> dict | None:
    if not tab:
        return None
    return {"columns": [x["label"] for x in legs_meta],
            "rows": [{"outcome": r["outcome"], "legs": [m(v) for v in r["legs"]], "total": m(r["total"])}
                     for r in tab["buckets"]],
            "worst_case": m(tab["worst"]), "worst_outcome": tab.get("worst_outcome"),
            "best_case": m(tab["best"]), "best_outcome": tab.get("best_outcome"),
            "locked": bool(tab.get("locked")), "spread": m(tab.get("spread", 0)),
            "guaranteed": m(tab["worst"]) if tab.get("locked") else None}


def _leg_label(l: dict) -> str:
    base = f"{'Back' if l['side'] == 'back' else 'Lay'} {l['account']}"
    return base + (f" ({l['label']})" if l["label"] else (f" ({', '.join(l['covers'])})" if len(l["covers"]) else ""))


def plan(conn, pid: int) -> dict:
    p = plans.load(conn, pid)
    planned = plans.figures(conn, p, planned=True)
    placed = plans.figures(conn, p, planned=False)
    rating = plans.rate(conn, p["kind"], planned, p["min_retention_pct"])
    meta = []
    for l in p["legs"]:
        if l["side"] == "back" or l["stake_pence"]:
            meta.append({"label": _leg_label(l)})
    placed_meta = ([{"label": _leg_label(l)} for l in p["legs"] if l["side"] == "back" and l["status"] == "placed"]
                   + [{"label": f"{_leg_label(l)} fill {i}"} for l in p["legs"] if l["side"] == "lay"
                      for i, _ in enumerate(l["fills"], 1)])
    legs = []
    for l in p["legs"]:
        item = {"plan_leg_id": l["id"], "side": l["side"], "account": l["account"], "label": l["label"] or None,
                "covers": l["covers"], "stake": m(l["stake_pence"]), "stake_calculated_by_server": bool(l["stake_auto"])
                and l["side"] == "lay", "odds": l["odds"], "status": l["status"]}
        if l["side"] == "lay":
            item.update(commission_percent=format(Decimal(l["commission_bp"]) / 100, "f"),
                        liability=m(calc.lay_liability(l["stake_pence"] or 0, Decimal(str(l["odds"])))),
                        order={"stake": m(l["order_stake_pence"]), "odds": l["order_odds"]} if l["order_stake_pence"] else None,
                        fills=[{"stake": m(f["stake_pence"]), "odds": f["odds"], "matched_at": t(f["matched_at"]),
                                "position_leg_id": f["leg_id"]} for f in l["fills"]],
                        matched=m(l["matched_pence"]), unmatched=m(plans._remaining(l)),
                        weighted_odds=plans.weighted_odds(l["fills"]),
                        cancelled_unmatched=m(l["unmatched_cancelled_pence"]) if l["unmatched_cancelled_pence"] else None)
        else:
            item.update(placed={"stake": m(l["placed_stake_pence"]), "odds": l["placed_odds"], "reference": l["reference"] or None,
                                "at": t(l["placed_at"]), "position_leg_id": l["leg_id"]} if l["status"] == "placed" else None)
        legs.append(item)
    offer = services.row(conn, "offers", p["offer_id"]) if p["offer_id"] else None
    fb = services.row(conn, "free_bets", p["free_bet_id"]) if p["free_bet_id"] else None
    revisions = [{"revision": r["revision"], "at": t(r["created_at"]), "reason": r["reason"],
                  "figures": json.loads(r["figures_json"]), "legs": json.loads(r["legs_json"])}
                 for r in conn.execute("SELECT * FROM plan_revisions WHERE plan_id = ? ORDER BY revision", (pid,))]
    fund = plans.funding(conn, p)
    return {
        "id": p["id"], "status": p["status"], "status_title": plans.STATUS_TITLES[p["status"]], "version": p["version"],
        "type": p["kind"], "event": p["event"], "event_at": t(p["event_at"]), "market": p["market"] or None,
        "selection": p["selection"] or None, "event_key": p["event_key"],
        "offer": {"id": offer["id"], "title": offer["title"]} if offer else None,
        "free_bet": {"id": fb["id"], "value": m(fb["face_value_pence"]), "type": fb["kind"], "status": fb["status"],
                     "expires_at": t(fb["expires_at"]), "notes": fb["notes"] or None} if fb else None,
        "outcomes": p["buckets"], "explicit_outcome_model": p["explicit_outcomes"], "legs": legs,
        "planned": {"outcome_table": _table_out(planned.get("table"), meta), "lay_stake": m(planned.get("lay_stake")),
                    "liability": m(planned.get("liability")), "retention_pct": planned.get("retention_pct"),
                    "qualifying_loss": m(planned.get("qualifying_loss")),
                    "qualifying_loss_pct": planned.get("qualifying_loss_pct"), "rating": rating},
        "placed_so_far": {"outcome_table": _table_out(placed.get("table"), placed_meta),
                          "unmatched": [{**u, "remaining": m(u["remaining"]), "liability_if_matched": m(u["liability_if_matched"])}
                                        for u in placed.get("unmatched", [])],
                          "fully_hedged": placed.get("fully_hedged")} if p["status"] not in plans.PRE_PLACEMENT else None,
        "position_id": p["position_id"],
        "validation": json.loads(p["validation_json"]) if p["validation_json"] else None,
        "funding": {"exchanges": [{**e, **{k: m(e[k]) for k in ("total_funds", "matched_liability_held",
                                                                  "unmatched_order_reserve", "this_plans_orders_reserve",
                                                                  "available_now", "new_liability_required", "shortfall",
                                                                  "available_after_bet")}} for e in fund["exchanges"]],
                    "shortfall": m(fund["shortfall"]),
                    "linked_transfers": [{"id": x["id"], "from": x["from_name"], "to": x["to_name"], "amount": m(x["amount_pence"]),
                                          "type": x["capital_type"]} for x in fund["linked_transfers"]],
                    "liability_released": fund["liability_released"],
                    "can_return": [{**r, "amount": m(r["amount"])} for r in fund["returnable"]]},
        "price_revisions": revisions, "evidence": evidence.for_record(conn, "plan", pid),
        "min_retention_pct": p["min_retention_pct"], "notes": p["notes"] or None,
        "created_at": t(p["created_at"]), "updated_at": t(p["updated_at"]),
        "counts_in_profit": False, "note": "A plan holds no money: only bets recorded as placed are in the ledger."}


def list_plans(conn, status: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT id FROM bet_plans", []
    if status == "active":
        q += " WHERE status IN ('" + "','".join(plans.ACTIVE) + "')"
    elif status:
        q += " WHERE status = ?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 50), 200)))
    out = []
    for (pid,) in conn.execute(q, args):
        d = plan(conn, pid)
        out.append({k: d[k] for k in ("id", "status", "status_title", "type", "event", "event_at", "selection", "offer",
                                      "free_bet", "position_id", "version")}
                   | {"locked": (d["planned"]["outcome_table"] or {}).get("worst_case"),
                      "retention_pct": d["planned"]["retention_pct"], "rating": d["planned"]["rating"].get("rating"),
                      "liability": d["planned"]["liability"]})
    return out


# ---------------------------------------------------------------- positions' outcome tables

def position_outcomes(conn, pid: int) -> dict:
    pos = services.row(conn, "positions", pid)
    if not pos:
        raise services.UserError(f"No position #{pid}")
    legs = exposure.legs_for(conn, pid)
    label = {l["id"]: f"{'Back' if l['side'] == 'back' else 'Lay'} {readapi._name(conn, l['account_id'])} "
                      f"{plain(l['stake_pence'])} @ {l['odds']}" for l in legs}
    data = exposure.valid(conn, pid)
    if data:
        _, scenarios = data
        rows = [{"outcome": s["label"], "legs": [m(s["pnl"][str(l["id"])]) for l in legs], "total": m(sum(s["pnl"].values()))}
                for s in scenarios]
        totals = [sum(s["pnl"].values()) for s in scenarios]
        model = "verified outcome table"
    else:
        if exposure.configured(conn, pid):
            return {"position_id": pid, "model": "invalid", "note": "The bets changed after their outcomes were recorded: "
                    "set_position_outcomes again. Liability is counted conservatively until then."}
        sel = pos["selection"] or "Selection"
        free = outcomes.FREE.get(pos["kind"])
        eng = [{"side": l["side"], "stake_pence": l["stake_pence"], "odds": l["odds"], "commission_bp": l["commission_bp"],
                "covers": [sel], "free": (free or ("snr" if l["free_bet_id"] else None)) if l["side"] == "back" else None}
               for l in legs]
        rows, totals = [], []
        for b in (sel, f"Not {sel}"):
            vals = [outcomes.leg_pnl(e, b) for e in eng]
            rows.append({"outcome": b, "legs": [m(v) for v in vals], "total": m(sum(vals))})
            totals.append(sum(vals))
        model = "simple two-way (every lay assumed on the same selection as the back)"
    worst, best = (min(totals), max(totals)) if totals else (0, 0)
    return {"position_id": pid, "event": pos["event"], "status": pos["status"], "model": model,
            "columns": [label[l["id"]] for l in legs], "rows": rows, "worst_case": m(worst), "best_case": m(best),
            "locked": best - worst <= outcomes.LOCKED_SPREAD,
            "exchange_liability_held": m(metrics.open_lay_liability(conn, position_id=pid)) if pos["status"] == "open" else None,
            "realised_result": m(services.position_pnl(conn, pid)) if pos["status"] != "open" else None}


def hedge_calculation(conn, data: dict) -> dict:
    """Stateless: the optimal lay stakes for given bets and outcomes (nothing stored)."""
    from .money import parse_commission
    buckets = [str(b).strip() for b in data.get("outcomes") or [] if str(b).strip()]
    sel = str(data.get("selection") or "Selection")
    if not buckets:
        buckets = outcomes.simple_buckets(sel)
    free = {"free_bet_snr": "snr", "free_bet_sr": "sr", "snr": "snr", "sr": "sr"}.get(data.get("type") or "")
    backs = [{"stake_pence": plans._pence(b["stake"], "back stake"), "odds": plans._odds(b["odds"], "back odds"),
              "covers": b.get("covers") or [buckets[0]], "free": free} for b in data.get("backs") or []]
    lays = [{"odds": plans._odds(l["odds"], "lay odds"), "covers": l.get("covers") or [buckets[0]],
             "commission_bp": parse_commission(str(l.get("commission") or "0"))} for l in data.get("lays") or []]
    cap = plans._pence(data["max_liability"], "max_liability") if data.get("max_liability") else None
    h = outcomes.hedge(buckets, backs, lays, max_liability=cap)
    meta = [{"label": f"Back {plain(b['stake_pence'])} @ {b['odds']}"} for b in backs] + \
           [{"label": f"Lay @ {l['odds']} ({', '.join(l['covers'])})"} for l in lays]
    face = sum(b["stake_pence"] for b in backs)
    return {"outcomes": buckets, "lay_stakes": [m(s) for s in h["lay_stakes"]], "liability": m(h["liability"]),
            "exchange_liability": m(h["exposure"]),
            "liability_note": "liability = every lay losing at once; exchange_liability = what an exchange holding all "
                              "these lays reserves (lays on different outcomes cannot all lose together)",
            "method": h["method"], "capped_by_max_liability": h["capped"], "outcome_table": _table_out(h["table"], meta),
            "retention_pct": round(h["table"]["worst"] / face * 100, 1) if free and face else None,
            "note": "Calculated by the server; nothing was stored."}


# ---------------------------------------------------------------- expected rewards, evidence, policy

def expected_rewards(conn, status: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM expected_rewards", []
    if status == "open":
        q += " WHERE status IN ('waiting', 'partially_received')"
    elif status:
        q += " WHERE status = ?"
        args.append(status)
    return [rewards.describe(conn, dict(r)) for r in conn.execute(q + " ORDER BY id DESC LIMIT 100", args)]


def expected_rewards_for_offer(conn, offer_id: int) -> list[dict]:
    return [rewards.describe(conn, dict(r)) for r in conn.execute(
        "SELECT * FROM expected_rewards WHERE offer_id = ? AND status <> 'void' ORDER BY id", (offer_id,))]


def expected_reward(conn, rid: int) -> dict:
    r = services.row(conn, "expected_rewards", rid)
    if not r:
        raise services.UserError(f"No expected reward #{rid}")
    out = rewards.describe(conn, r)
    out["candidates"] = [{"id": c["id"], "value": m(c["face_value_pence"]), "status": c["status"]}
                         for c in rewards.candidates(conn, r)]
    out["evidence"] = evidence.for_record(conn, "expected_reward", rid)
    return out


def retention_policy(conn) -> dict:
    return {"policy": plans.policy(conn),
            "meaning": "Free bets: retention % = locked-in result / face value. excellent / very_good / acceptable "
                       "thresholds; below reject_below a plan is DO_NOT_PLACE. Qualifiers: qualifying loss as % of stake. "
                       "A plan can set its own min_retention_pct."}


# ---------------------------------------------------------------- next actions

def _action(priority, kind, title, explanation, recommended, *, deadline=None, records=(), value=None,
            assistant=False, user=False) -> dict:
    return {"priority": priority, "kind": kind, "title": title, "explanation": explanation,
            "recommended_action": recommended, "deadline": deadline, "records": list(records), "value_pence": value,
            "assistant_can_resolve": assistant, "needs_user_externally": user}


def alert_key(a: dict) -> str:
    """Stable identity of an alert: its kind and main record (a free bet, plan, position, offer, bank...)."""
    return f"{a['kind']}:{a['records'][0] if a['records'] else a['title']}"[:160]


def mark_notified(conn, keys: list[str]) -> dict:
    """Record that these alerts were sent (as they stand now). Unknown keys are reported, not stored."""
    from . import services
    current = {a["alert_key"]: a for a in next_actions(conn)}
    done, unknown, already = [], [], []
    for k in keys:
        a = current.get(k)
        if not a:
            unknown.append(k)
        elif a["already_notified"]:
            already.append(k)
        else:
            conn.execute("INSERT INTO alert_state (alert_key, fingerprint, notified_at, actor, request_id) VALUES "
                         "(?,?,?,?,?) ON CONFLICT(alert_key) DO UPDATE SET fingerprint = excluded.fingerprint, "
                         "notified_at = excluded.notified_at, actor = excluded.actor, request_id = excluded.request_id",
                         (k, a["alert_fingerprint"], times.now_iso(), services.current_actor(), services._REQUEST.get()))
            done.append(k)
    return {"notified": done, "already_notified": already, "unknown": unknown}


def next_actions(conn, s=None) -> list[dict]:
    s = s or metrics.summary(conn)
    now_dt = times.now()
    now = times.iso(now_dt)
    h24, h72 = times.iso(now_dt + timedelta(hours=24)), times.iso(now_dt + timedelta(hours=72))
    out = []
    ret = outlook.retention(conn)

    # free bets by expiry
    for f in conn.execute("SELECT f.*, a.name FROM free_bets f JOIN accounts a ON a.id = f.account_id WHERE f.status = "
                          "'available' AND f.deleted_at IS NULL ORDER BY f.expires_at IS NULL, f.expires_at"):
        rate = ret[f["kind"]]["rate"] if f["kind"] in ret else outlook.TYPICAL["snr"]
        worth = outlook._value(f["face_value_pence"], rate)
        planned = plans.live_for_free_bet(conn, f["id"])
        pr = "urgent" if f["expires_at"] and f["expires_at"] <= h24 else ("high" if f["expires_at"] and f["expires_at"] <= h72
                                                                          else "low")
        restriction = (f["notes"] or "")[:80]
        out.append(_action(pr, "free_bet_expiry", f"{f['name']} {fmt(f['face_value_pence'])} {f['kind'].upper()}"
                           + (f" expires {times.local(f['expires_at'], '%a %-d %b %H:%M')}" if f["expires_at"] else " (no expiry)"),
                           f"Estimated worth {fmt(worth)} at {int(rate * 100)}% retention."
                           + (f" Restriction: {restriction}." if restriction else "")
                           + (f" Plan #{planned} is set up for it." if planned else ""),
                           f"Continue plan #{planned}" if planned else "Find a conversion: create a bet plan for it",
                           deadline=f["expires_at"], records=[f"free_bet #{f['id']}"] + ([f"plan #{planned}"] if planned else []),
                           value=worth, assistant=True, user=True))

    # bet plans
    for p in conn.execute("SELECT * FROM bet_plans WHERE status IN ('" + "','".join(plans.ACTIVE) + "') ORDER BY event_at"):
        rec = [f"plan #{p['id']}"]
        starts = f" (starts {times.local(p['event_at'], '%a %H:%M')})" if p["event_at"] else ""
        if p["status"] == "draft":
            out.append(_action("medium", "plan", f"Validate plan #{p['id']}: {p['event']}", "Drafted but not validated.",
                               "validate_bet_plan", deadline=p["event_at"], records=rec, assistant=True))
        elif p["status"] in ("validated", "ready_to_place"):
            full = plans.load(conn, p["id"])
            fund = plans.funding(conn, full)
            if fund["shortfall"] > 0:
                out.append(_action("high", "funding", f"Add {fmt(fund['shortfall'])} to "
                                   f"{fund['exchanges'][0]['exchange']} for plan #{p['id']}",
                                   "Not enough free cash on the exchange for the lay liability.",
                                   "Deposit (temporary or permanent), then record_funding_transfer and validate again",
                                   deadline=p["event_at"], records=rec, user=True))
            else:
                val = json.loads(p["validation_json"] or "{}")
                bk = val.get("bookmaker") or {}
                lay = (val.get("exchange") or [{}])[0]
                out.append(_action("high" if p["event_at"] and p["event_at"] <= h24 else "medium", "place",
                                   f"Place plan #{p['id']}: {p['event']}{starts}",
                                   (f"Back £{bk.get('stake')} @ {bk.get('odds')} at {bk.get('account')}; lay "
                                    f"£{lay.get('lay_stake')} @ {lay.get('lay_odds')} at {lay.get('account')} "
                                    f"(liability £{lay.get('liability')})") if bk else "Validated and ready.",
                                   "Place both bets, then send the confirmations", deadline=p["event_at"], records=rec,
                                   user=True))
        elif p["status"] == "bookmaker_placed":
            out.append(_action("urgent", "hedge", f"Lay plan #{p['id']} now: {p['event']}{starts}",
                               "The bookmaker bet is placed but not hedged.", "Place the lay, then send the confirmation",
                               deadline=p["event_at"], records=rec, user=True))
        elif p["status"] == "exchange_partially_matched":
            full = plans.load(conn, p["id"])
            left = sum(plans._remaining(l) for l in full["legs"] if l["side"] == "lay")
            out.append(_action("urgent", "hedge", f"Finish the lay of plan #{p['id']}: {fmt(left)} unmatched",
                               "Partly matched: keep waiting, cancel the rest, or re-lay at the current price.",
                               "Check the exchange; record fills / cancel_exchange_order", deadline=p["event_at"],
                               records=rec, user=True, assistant=True))

    # positions
    cut = times.iso(now_dt - timedelta(hours=outlook.FINISHED_AFTER_HOURS))
    for p in conn.execute("SELECT * FROM positions WHERE status = 'open' AND deleted_at IS NULL ORDER BY event_at"):
        if p["event_at"] and p["event_at"] < cut:
            out.append(_action("high", "settle", f"Settle position #{p['id']}: {p['event']}",
                               f"The event started {times.relative(p['event_at'])} and has probably finished.",
                               "Ask the user for the result (or a settled receipt), then settle_position",
                               deadline=p["event_at"], records=[f"position #{p['id']}"], assistant=True))
        elif p["event_at"] and p["event_at"] < now:
            out.append(_action("low", "in_play", f"In play: {p['event']}", "Settle when it finishes.", "Wait",
                               records=[f"position #{p['id']}"]))

    # funding to return
    for b in conn.execute("SELECT id, name FROM accounts WHERE kind = 'bank'"):
        outstanding = services.outstanding_temporary(conn, b["id"])
        if not outstanding:
            continue
        linked = conn.execute("SELECT p.status, p.position_id FROM transfers t JOIN bet_plans p ON p.id = t.plan_id WHERE "
                              "t.from_account_id = ? AND t.capital_type = 'temporary_funding' AND t.voided_at IS NULL",
                              (b["id"],)).fetchall()
        released = all(r["status"] in plans.CLOSED for r in linked) if linked else not conn.execute(
            "SELECT 1 FROM positions WHERE status = 'open' AND deleted_at IS NULL").fetchone()
        dest = conn.execute("SELECT t.to_account_id FROM transfers t WHERE t.from_account_id = ? AND t.capital_type = "
                            "'temporary_funding' AND t.voided_at IS NULL ORDER BY t.at DESC LIMIT 1", (b["id"],)).fetchone()
        free_cash = services.expected_balance(conn, dest[0], "available")["available"] if dest else 0
        due = conn.execute("SELECT min(due_at) FROM transfers WHERE from_account_id = ? AND capital_type = 'temporary_funding' "
                           "AND voided_at IS NULL AND due_at IS NOT NULL", (b["id"],)).fetchone()[0]
        if released and free_cash > 0:
            amount = min(outstanding, free_cash)
            out.append(_action("medium", "return_funding", f"{fmt(amount)} temporary {b['name']} funding can be returned",
                               f"{fmt(outstanding)} is outstanding and the liability it covered has been released.",
                               "Withdraw it to the bank, then record the temporary return", deadline=due,
                               records=[f"account {b['name']}"], user=True))
        elif due and due <= h72:
            out.append(_action("high", "return_funding", f"{fmt(outstanding)} due back to {b['name']} "
                               f"{times.local(due, '%a %-d %b')}", "Temporary funding with a return date.",
                               "Return it once the bets it funds have settled", deadline=due,
                               records=[f"account {b['name']}"], user=True))

    # offers and rewards
    for o in conn.execute("SELECT * FROM offers WHERE deleted_at IS NULL AND status NOT IN ('complete', 'expired', 'abandoned')"):
        life = rewards.lifecycle(conn, dict(o))
        if not life.get("next_action") or life["stage"] in ("reward_credited", "reward_partially_used", "reward_used"):
            continue
        pr = "high" if life["stage"] == "reward_overdue" or (o["expires_at"] and o["expires_at"] <= h72) else (
            "low" if life.get("actor") == "waiting" else "medium")
        out.append(_action(pr, "offer", f"{o['title']}: {life['next_action']}", f"Stage: {life['stage'].replace('_', ' ')}.",
                           life["next_action"], deadline=life.get("deadline") or o["expires_at"], records=[f"offer #{o['id']}"],
                           assistant=life.get("actor") in ("assistant", "both"), user=life.get("actor") in ("user", "both")))
    for r in conn.execute("SELECT e.*, a.name FROM expected_rewards e JOIN accounts a ON a.id = e.account_id WHERE e.status "
                          "IN ('waiting', 'partially_received') AND e.offer_id IS NULL"):
        late = r["expected_by"] and r["expected_by"] < now
        out.append(_action("high" if late else "low", "reward", f"{r['name']} reward {r['count']} x {fmt(r['each_pence'])} "
                           + ("overdue" if late else "expected"), "Not matched to a free bet yet.",
                           "Check the bookmaker; reconcile_rewards if it arrived", deadline=r["expected_by"],
                           records=[f"expected_reward #{r['id']}"], assistant=True, user=bool(late)))

    # issues and unresolved balances (the ones not already covered above)
    covered = ("started:", "plan:", "reward:overdue")
    for i in issues.collect(conn, s):
        if i["severity"] == "info" or i["key"].startswith(covered):
            continue
        safe = [r for r in i.get("repairs", [])]
        out.append(_action("high" if i["severity"] == "fix" else "medium", "issue", i["title"], i["text"],
                           (f"{safe[0]['change']}: {safe[0]['label']}" if safe else i["action"]),
                           records=i.get("records", []), assistant=bool(safe) or i["severity"] == "fix"))
    out.sort(key=lambda a: (PRIORITY[a["priority"]], a["deadline"] or "9999"))
    sent = {r[0]: r[1] for r in conn.execute("SELECT alert_key, fingerprint FROM alert_state")}
    for a in out:
        a["alert_key"] = alert_key(a)
        a["alert_fingerprint"] = f"{a['priority']}|{a['deadline'] or ''}"
        a["already_notified"] = sent.get(a["alert_key"]) == a["alert_fingerprint"]
        a["deadline_label"] = times.local(a["deadline"], "%a %d %b %H:%M") if a["deadline"] else None
        a["deadline"] = t(a["deadline"]) if a["deadline"] else None
        a["potential_value"] = m(a.pop("value_pence")) if a.get("value_pence") is not None else None
        a.pop("value_pence", None)
    return out


# ---------------------------------------------------------------- operating brief

def operating_brief(conn) -> dict:
    s = metrics.summary(conn)
    dash = readapi.dashboard_totals(conn)
    actions = next_actions(conn, s)
    stc = readapi.still_to_come_breakdown(conn)
    ret = outlook.retention(conn)
    free = []
    for f in conn.execute("SELECT f.*, a.name FROM free_bets f JOIN accounts a ON a.id = f.account_id WHERE f.status = "
                          "'available' AND f.deleted_at IS NULL ORDER BY f.expires_at IS NULL, f.expires_at"):
        rate = ret[f["kind"]]["rate"] if f["kind"] in ret else outlook.TYPICAL["snr"]
        free.append({"id": f["id"], "bookmaker": f["name"], "value": m(f["face_value_pence"]), "type": f["kind"],
                     "expires_at": t(f["expires_at"]), "restriction": f["notes"] or None,
                     "estimated_worth": m(outlook._value(f["face_value_pence"], rate)),
                     "plan_id": plans.live_for_free_bet(conn, f["id"])})
    offers = []
    for o in conn.execute("SELECT * FROM offers WHERE deleted_at IS NULL AND status NOT IN ('complete', 'expired', 'abandoned') "
                          "ORDER BY id DESC"):
        life = rewards.lifecycle(conn, dict(o))
        offers.append({"id": o["id"], "title": o["title"], "bookmaker": readapi._name(conn, o["account_id"]),
                       "status": life["status"], "stage": life["stage"], "qualifier_required": life["qualifier_required"],
                       "next_action": life.get("next_action"), "deadline": t(life.get("deadline")),
                       "expires_at": t(o["expires_at"])})
    positions = []
    for p in conn.execute("SELECT * FROM positions WHERE status = 'open' AND deleted_at IS NULL ORDER BY event_at"):
        proj = outlook.position_projection(conn, p["id"])
        positions.append({"id": p["id"], "event": p["event"], "selection": p["selection"], "event_at": t(p["event_at"]),
                          "locked_in": m(proj["locked"]) if proj else None,
                          "liability_held": m(metrics.open_lay_liability(conn, position_id=p["id"])),
                          "finished_probably": bool(p["event_at"] and p["event_at"] < times.iso(
                              times.now() - timedelta(hours=outlook.FINISHED_AFTER_HOURS)))})
    found = issues.collect(conn, s)
    return {
        "as_of": t(times.now_iso()),
        "profit": dash["realised_profit"],
        "bankroll": {"bookmaker_cash": dash["bookmaker_cash"], "exchange": dash["exchange"],
                     "unsettled_back_stakes": dash["unsettled_back_stakes"], "available_to_bet": dash["available_to_bet"],
                     "temporary_funding_outstanding": dash["temporary_funding_outstanding"],
                     "temporary_funding_by_bank": dash["temporary_funding_by_bank"], "own_capital_in": dash["own_capital_in"],
                     "bankroll_withdrawn": dash["bankroll_withdrawn"], "profit_withdrawn": dash["profit_withdrawn"],
                     "exchanges": [readapi.account(conn, dict(a)) | {} for a in conn.execute(
                         "SELECT * FROM accounts WHERE kind = 'exchange' AND active = 1")]},
        "free_bets": free,
        "offers": offers,
        "expected_rewards": expected_rewards(conn, "open"),
        "bet_plans": list_plans(conn, "active"),
        "positions": positions,
        "still_to_come": stc,
        "issues": {"count": len(found), "by_severity": issues.counts(found),
                   "top": [{"key": i["key"], "severity": i["severity"], "title": i["title"], "text": i["text"]}
                           for i in found[:8]]},
        "next_actions": actions,
        "alerts_to_send": [{"alert_key": a["alert_key"], "priority": a["priority"], "title": a["title"]}
                           for a in actions if not a["already_notified"] and a["priority"] in ("urgent", "high")],
        "recommended_next_three": [{"priority": a["priority"], "title": a["title"], "do": a["recommended_action"],
                                    "who": "assistant" if a["assistant_can_resolve"] and not a["needs_user_externally"]
                                    else ("user" if a["needs_user_externally"] and not a["assistant_can_resolve"] else "both")}
                                   for a in actions[:3]],
        "retention": {k: v["pct"] for k, v in ret.items()},
        "note": "Figures come from the server. Realised profit counts settled results only; plans, expected rewards, "
                "free-bet face value and open liability are not profit.",
    }


def plan_compact(conn, pid: int) -> dict:
    """A plan's state for a change's before -> after (the full view is get_bet_plan)."""
    d = plan(conn, pid)
    return {"id": d["id"], "status": d["status"], "version": d["version"], "event": d["event"],
            "free_bet": (d["free_bet"] or {}).get("id"), "position_id": d["position_id"],
            "legs": [{"id": l["plan_leg_id"], "side": l["side"], "stake": l["stake"], "odds": l["odds"], "status": l["status"],
                      **({"matched": l["matched"], "unmatched": l["unmatched"]} if l["side"] == "lay" else {})}
                     for l in d["legs"]],
            "locked_in": (d["planned"]["outcome_table"] or {}).get("worst_case"),
            "retention_pct": d["planned"]["retention_pct"], "liability": d["planned"]["liability"]}
