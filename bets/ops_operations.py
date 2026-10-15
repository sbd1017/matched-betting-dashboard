"""Named changes for the operating workflow (registered into bets/ops.py OPS): bet plans, placement and exchange
fills, atomic recording, funding, evidence, expected rewards, offer lifecycle facts, outcome models and dismissing
issues. Same rules as every change: validated first, applied only through services, previewable, idempotent by
request_id, audited, and run under the user's permission levels:
  planning    create / update / recalculate / validate / cancel plans, attach evidence, expect a reward
  safe_edit   record placements and fills the user confirmed, record funding the user made, reconcile rewards,
              offer facts, outcome models, dismiss an issue reviewed as a false positive
"""
from __future__ import annotations

import json
from decimal import Decimal
from datetime import timedelta

from . import brief, evidence, offer_state, readapi, exposure, issues, outcomes, plans, rewards, services, times
from .money import fmt, plain
from .ops import OPS, TITLES, Plan, _int, _reason, _str, _when
from .services import UserError


def _plan(conn, p: dict) -> dict:
    return plans.load(conn, _int(p, "plan_id"))


def _version(p: dict):
    v = p.get("expected_version")
    return int(v) if v not in (None, "") else None


def _plan_data(p: dict) -> dict:
    keys = ("event", "market", "selection", "kind", "bookmaker", "back_stake", "back_odds", "exchange", "lay_odds",
            "lay_stake", "commission", "free_bet_id", "offer_id", "outcomes", "back_covers", "back_label", "lays",
            "min_retention_pct", "max_liability", "sport", "builder_legs", "notes", "allow_duplicate")
    out = {k: p[k] for k in keys if p.get(k) not in (None, "")}
    if p.get("event_time"):
        out["event_at"] = _when(p, "event_time")
    return out


# ---------------------------------------------------------------- plans (planning)

def op_create_bet_plan(conn, p: dict) -> Plan:
    data = _plan_data(p)
    if not data.get("event"):
        raise UserError("event: required")
    plan = Plan("create_bet_plan", "planning", f"New bet plan: {data['event']}",
                effects=["a plan only: no money, free bet or offer changes until bets are recorded as placed"])

    def apply(c):
        try:
            pid = plans.create(c, data)
        except UserError as exc:
            raise UserError(str(exc)) from None
        plan.created.append(("plan", pid))
        return {"plan_id": pid, "validation_hint": "call validate_bet_plan before telling the user to place it"}
    plan.apply = apply
    return plan


def op_update_bet_plan(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    data = {k: p[k] for k in ("event", "market", "selection", "notes", "offer_id", "free_bet_id", "min_retention_pct",
                              "max_liability", "sport", "builder_legs") if p.get(k) is not None}
    if p.get("event_time") is not None:
        data["event_at"] = _when(p, "event_time") if p["event_time"] else ""
    plan = Plan("update_bet_plan", "planning", f"Update bet plan #{pl['id']}", touches=[("plan", pl["id"])],
                noop=not data)
    plan.apply = lambda c: plans.update(c, pl["id"], {**data, "reason": _reason(p)}, _version(p))
    return plan


def op_recalculate_bet_plan(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = Plan("recalculate_bet_plan", "planning", f"New prices for bet plan #{pl['id']}", touches=[("plan", pl["id"])],
                effects=["the old quote is kept as a price revision; lay stakes are recalculated; the plan needs "
                         "validating again"])
    plan.apply = lambda c: plans.recalculate(c, pl["id"], back_odds=p.get("back_odds"), lay_odds=p.get("lay_odds"),
                                             back_stake=p.get("back_stake"), leg_odds=p.get("leg_odds"),
                                             reason=_reason(p), expected_version=_version(p))
    return plan


def op_validate_bet_plan(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = Plan("validate_bet_plan", "planning", f"Validate bet plan #{pl['id']}", touches=[("plan", pl["id"])])
    plan.apply = lambda c: plans.validate(c, pl["id"], liquidity=p.get("liquidity"), allow_started=bool(p.get("allow_started")))
    return plan


def op_cancel_bet_plan(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = Plan("cancel_bet_plan", "planning", f"Cancel bet plan #{pl['id']} ({pl['event']})", touches=[("plan", pl["id"])],
                noop=pl["status"] == "cancelled", effects=["its free bet is free for another plan"])
    if pl["status"] not in plans.PRE_PLACEMENT + ("cancelled",):
        plan.blocked = "Bets of this plan are already placed: settle or void its position instead"
    plan.apply = lambda c: plans.cancel(c, pl["id"], _reason(p) or "cancelled", _version(p))
    return plan


# ---------------------------------------------------------------- placement (safe bookkeeping: what the user did)

def _placement_plan(name: str, pl: dict, summary: str, **kw) -> Plan:
    touches = [("plan", pl["id"])] + ([("position", pl["position_id"])] if pl["position_id"] else [])
    plan = Plan(name, "safe_edit", summary, touches=touches, **kw)
    if pl["status"] in plans.CLOSED:
        plan.blocked = f"Plan #{pl['id']} is {pl['status'].replace('_', ' ')}"
    return plan


def _created_position(plan: Plan, pl_id: int):
    def after(c):
        pos = plans.load(c, pl_id)["position_id"]
        if pos and ("position", pos) not in plan.touches:
            plan.created.append(("position", pos))
    return after


def op_record_bookmaker_placement(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    backs = [l for l in pl["legs"] if l["side"] == "back"]
    done = all(l["status"] == "placed" for l in backs) and backs
    plan = _placement_plan("record_bookmaker_placement", pl, f"Bookmaker bet placed for plan #{pl['id']} ({pl['event']})",
                           noop=bool(done), effects=["the position is created (or updated) with the back bet: a cash "
                                                     "stake leaves the bookmaker balance; a free bet is used"])
    track = _created_position(plan, pl["id"])

    def apply(c):
        out = plans.place_back(c, pl["id"], stake=p.get("stake"), odds=p.get("odds"), reference=_str(p, "reference", 120) or "",
                               placed_at=_when(p, "placed_at"), plan_leg_id=p.get("plan_leg_id"))
        track(c)
        return out
    plan.apply = apply
    return plan


def op_record_exchange_order(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = _placement_plan("record_exchange_order", pl, f"Lay order submitted for plan #{pl['id']}",
                           effects=["an unmatched order holds liability on the exchange; it is not a matched bet"])
    plan.apply = lambda c: plans.submit_order(c, pl["id"], stake=p.get("stake"), odds=p.get("odds"),
                                              plan_leg_id=p.get("plan_leg_id"))
    return plan


def op_record_exchange_fill(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = _placement_plan("record_exchange_fill", pl, f"Lay matched for plan #{pl['id']}: "
                           f"{p.get('stake')} @ {p.get('odds')}",
                           effects=["added to the position as a lay bet: its liability is held on the exchange"])
    leg_ids = [l["id"] for l in pl["legs"] if l["side"] == "lay"]
    target = int(p["plan_leg_id"]) if p.get("plan_leg_id") else (leg_ids[0] if len(leg_ids) == 1 else None)
    if target and not p.get("allow_duplicate"):
        from .money import to_pence
        try:
            stake = to_pence(str(p.get("stake")), allow_zero=False)
        except (ValueError, TypeError):
            stake = None
        same = conn.execute("SELECT id FROM plan_fills WHERE plan_leg_id = ? AND stake_pence = ? AND odds = ? ORDER BY id "
                            "DESC LIMIT 1", (target, stake, plans._odds(p.get("odds"), "odds"))).fetchone() if stake else None
        if same:
            plan.blocked = (f"A fill of {p.get('stake')} @ {p.get('odds')} is already recorded (fill #{same[0]}). If this "
                            "is a second, separate fill, pass allow_duplicate=true")
    track = _created_position(plan, pl["id"])

    def apply(c):
        out = plans.fill(c, pl["id"], stake=p.get("stake"), odds=p.get("odds"), matched_at=_when(p, "matched_at"),
                         plan_leg_id=p.get("plan_leg_id"))
        track(c)
        return out
    plan.apply = apply
    return plan


def op_cancel_exchange_order(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = _placement_plan("cancel_exchange_order", pl, f"Unmatched lay cancelled for plan #{pl['id']}",
                           effects=["the unmatched part no longer holds liability; the hedge stays as matched so far"])
    plan.apply = lambda c: {"cancelled": plain(plans.cancel_unmatched(c, pl["id"], plan_leg_id=p.get("plan_leg_id"),
                                                                      reason=_reason(p)))}
    return plan


def op_mark_exchange_fully_matched(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    plan = _placement_plan("mark_exchange_fully_matched", pl, f"Lay FULLY MATCHED for plan #{pl['id']}",
                           effects=["any matched part not recorded yet is added as a lay bet"])
    if not p.get("confirmed_fully_matched"):
        plan.blocked = ("confirmed_fully_matched: set true only after seeing the exchange show FULLY MATCHED (not just "
                        "an order placed)")
    track = _created_position(plan, pl["id"])

    def apply(c):
        out = plans.mark_fully_matched(c, pl["id"], plan_leg_id=p.get("plan_leg_id"), matched_stake=p.get("matched_stake"),
                                       odds=p.get("odds"), matched_at=_when(p, "matched_at"))
        track(c)
        return out
    plan.apply = apply
    return plan


def op_record_matched_bet(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    done = pl["status"] in ("fully_matched", "converted_to_position", "settled")
    plan = _placement_plan("record_matched_bet", pl, f"Record matched bet of plan #{pl['id']} ({pl['event']})", noop=done,
                           effects=["all at once: bookmaker bet, every lay fill, fully matched, free bet used, liability "
                                    "held. If any part fails nothing is recorded."])
    if not done and not p.get("confirmed_fully_matched"):
        plan.blocked = "confirmed_fully_matched: set true only after seeing every lay FULLY MATCHED"
    track = _created_position(plan, pl["id"])

    def apply(c):
        out = plans.record_matched_bet(c, pl["id"], back=p.get("bookmaker") or {}, lays=p.get("lays") or None)
        for ev in p.get("evidence") or []:
            evidence.add(c, entity="plan", entity_id=pl["id"], kind=ev.get("kind", "other"),
                         description=ev.get("description", ""), facts=ev.get("facts") or {},
                         captured_at=ev.get("captured_at"))
        if p.get("funding_transfer_id"):
            c.execute("UPDATE transfers SET plan_id = ? WHERE id = ? AND plan_id IS NULL",
                      (pl["id"], int(p["funding_transfer_id"])))
        track(c)
        return out
    plan.apply = apply
    return plan


def op_convert_bet_plan_to_position(conn, p: dict) -> Plan:
    pl = _plan(conn, p)
    done = pl["status"] in ("converted_to_position", "fully_matched", "settled")
    plan = _placement_plan("convert_bet_plan_to_position", pl, f"Record plan #{pl['id']} as placed exactly as planned",
                           noop=done, effects=["the position is created with the planned stakes and prices"])
    if not done and not p.get("confirmed_placed_as_planned"):
        plan.blocked = "confirmed_placed_as_planned: set true only when the user confirms both bets were placed as planned"
    track = _created_position(plan, pl["id"])

    def apply(c):
        pos = plans.convert(c, pl["id"])
        track(c)
        return {"position_id": pos}
    plan.apply = apply
    return plan


# ---------------------------------------------------------------- funding

def op_record_funding_transfer(conn, p: dict) -> Plan:
    from .changes import find_account
    src, dst = find_account(conn, p.get("from_account")), find_account(conn, p.get("to_account"))
    amount = plans._pence(p.get("amount"), "amount")
    kind = _str(p, "funding_type", 30) or "temporary_funding"
    if kind not in ("temporary_funding", "bankroll_contribution"):
        raise UserError("funding_type: temporary_funding or bankroll_contribution")
    plan_id = _int(p, "plan_id", required=False)
    if plan_id:
        plans._need(conn, plan_id)
    at = _when(p, "when")
    since = times.iso(times.now() - timedelta(minutes=30))
    plan = Plan("record_funding_transfer", "safe_edit",
                f"{fmt(amount)} {src['name']} → {dst['name']} ({kind.replace('_', ' ')})" + (f" for plan #{plan_id}" if plan_id else ""),
                effects=["capital only: never profit; temporary funding shows as outstanding until it goes back"])
    dup = conn.execute("SELECT id FROM transfers WHERE voided_at IS NULL AND from_account_id = ? AND to_account_id = ? AND "
                       "amount_pence = ? AND created_at >= ?", (src["id"], dst["id"], amount, since)).fetchone()
    if dup and not p.get("allow_duplicate"):
        plan.blocked = (f"Transfer #{dup[0]} of the same amount between these accounts was recorded in the last 30 "
                        "minutes. If this is really a second transfer, pass allow_duplicate=true")

    def apply(c):
        tid = services.create_transfer(c, from_id=src["id"], to_id=dst["id"], amount=amount, at=at, capital_type=kind,
                                       notes=_str(p, "notes", 500) or (f"Liability funding for bet plan #{plan_id}" if plan_id else ""),
                                       due_at=_when(p, "return_by") if kind == "temporary_funding" else None)
        if plan_id:
            c.execute("UPDATE transfers SET plan_id = ? WHERE id = ?", (plan_id, tid))
        plan.created.append(("transfer", tid))
        return {"transfer_id": tid}
    plan.apply = apply
    return plan


# ---------------------------------------------------------------- evidence (planning: never moves money)

def op_add_record_evidence(conn, p: dict) -> Plan:
    entity, eid = _str(p, "record_type", 30), _int(p, "record_id")
    kind = _str(p, "kind", 40) or "other"
    facts = p.get("facts") or {}
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except ValueError:
            facts = {"note": facts}
    plan = Plan("add_record_evidence", "planning", f"Evidence ({kind.replace('_', ' ')}) on {entity} #{eid}",
                effects=["recorded as proof only: no money changes"])
    same = conn.execute("SELECT id FROM evidence WHERE entity = ? AND entity_id = ? AND kind = ? AND lower(description) = "
                        "lower(?) AND coalesce(captured_at, '') = coalesce(?, '') AND deleted_at IS NULL",
                        (entity, eid, kind, (_str(p, "description", 1000) or ""), _when(p, "captured_at"))).fetchone()
    plan.noop = bool(same)

    def apply(c):
        new_id, created = evidence.add(c, entity=entity, entity_id=eid, kind=kind, description=_str(p, "description", 1000) or "",
                                       facts=facts, captured_at=_when(p, "captured_at"),
                                       source=_str(p, "source", 40) or "assistant")
        return {"evidence_id": new_id, "created": created}
    plan.apply = apply
    return plan


# ---------------------------------------------------------------- rewards

def op_expect_reward(conn, p: dict) -> Plan:
    plan = Plan("expect_reward", "planning", "Expect a reward" + (f" for offer #{p.get('offer_id')}" if p.get("offer_id") else ""),
                effects=["an expectation only: not money, not profit, not a free bet until it arrives"])

    def apply(c):
        rid, created = rewards.expect(c, offer_id=p.get("offer_id"), account_id=_account_id(c, p.get("bookmaker")),
                                      kind=p.get("reward_type"), count=p.get("count"),
                                      each=plans._pence(p["each"], "each") if p.get("each") else None,
                                      expected_by=_when(p, "expected_by"), expiry_days=p.get("expiry_days"),
                                      source_position_id=_int(p, "position_id", required=False), notes=_str(p, "notes", 500) or "")
        plan.created.append(("expected_reward", rid))
        if p.get("offer_id"):
            offer_state.sync(c, int(p["offer_id"]))
        return {"expected_reward_id": rid, "created": created}
    plan.apply = apply
    return plan


def _account_id(conn, ref):
    if not ref:
        return None
    from .changes import find_account
    return find_account(conn, ref)["id"]


def op_reconcile_rewards(conn, p: dict) -> Plan:
    proposal = rewards.reconcile(conn, apply=False, reward_id=_int(p, "expected_reward_id", required=False))
    linkable = [r for r in proposal["rewards"] if r.get("link")]
    plan = Plan("reconcile_rewards", "safe_edit", f"Match {len(linkable)} expected reward(s) to free bets that arrived",
                touches=[("expected_reward", r["expected_reward_id"]) for r in linkable], noop=not linkable,
                effects=["only unambiguous matches are linked; ambiguous ones are reported for the user"])
    plan.apply = lambda c: rewards.reconcile(c, apply=True, reward_id=_int(p, "expected_reward_id", required=False))
    if not linkable:
        plan.summary = "Nothing to match: " + (", ".join(f"#{r['expected_reward_id']} {r['result'].replace('_', ' ')}"
                                                          for r in proposal["rewards"]) or "no rewards waiting")
    return plan


def op_mark_reward_received(conn, p: dict) -> Plan:
    rid = _int(p, "expected_reward_id")
    r = services.row(conn, "expected_rewards", rid)
    if not r:
        raise UserError(f"No expected reward #{rid}")
    have = len([t for t in rewards.linked_tokens(conn, rid) if t["status"] != "void"])
    done = r["status"] == "received" and not p.get("free_bet_ids")
    plan = Plan("mark_reward_received", "safe_edit", f"Reward #{rid} arrived ({r['count']} x {fmt(r['each_pence'])} "
                f"{r['kind'].upper()})", touches=[("expected_reward", rid)], noop=done,
                effects=["matching free bets already recorded are linked; only missing ones are created (never twice)"])
    if p.get("count") and int(p["count"]) != r["count"]:
        plan.warnings.append(f"received {p['count']} but the offer promised {r['count']}")

    def apply(c):
        out = rewards.mark_received(c, rid, free_bet_ids=[int(x) for x in p.get("free_bet_ids") or []],
                                    count=int(p["count"]) if p.get("count") else None, expires_at=_when(p, "expires_at"),
                                    at=_when(p, "received_at"), notes=_str(p, "notes", 500) or "")
        for fid in out.get("created", []):
            plan.created.append(("free_bet", fid))
        return out
    plan.apply = apply
    _ = have
    return plan


# ---------------------------------------------------------------- offer lifecycle facts

def op_update_offer_progress(conn, p: dict) -> Plan:
    oid = _int(p, "offer_id")
    o = services.row(conn, "offers", oid)
    if not o or o["deleted_at"]:
        raise UserError(f"No offer #{oid}")
    now = times.now_iso()
    data = {}
    for flag, col in (("opted_in", "opted_in_at"), ("deposit_done", "deposit_done_at"), ("reviewed", "reviewed_at"),
                      ("qualified", "qualified_at")):
        if p.get(flag) is True and not o[col]:
            data[col] = _when(p, "when") or now
        elif p.get(flag) is False and o[col]:
            data[col] = None
    for key, col in (("opt_in_required", "opt_in_required"), ("deposit_required", "deposit_required")):
        if p.get(key) is not None:
            data[col] = 1 if p[key] else 0
    for key in ("required_sport", "required_market"):
        if p.get(key) is not None:
            data[key] = _str(p, key, 80) or None
    for key in ("min_legs", "reward_delay_hours"):
        if p.get(key) not in (None, ""):
            data[key] = int(p[key])
    terms = {}
    for key, col in (("qualifying_stake", "qualifying_stake_pence"), ("reward_each", "reward_each_pence")):
        if p.get(key) not in (None, ""):
            terms[col] = plans._pence(p[key], key)
    if p.get("min_odds") not in (None, ""):
        terms["min_odds"] = plans._odds(p["min_odds"], "min_odds")
    for key in ("reward_count", "reward_expiry_days"):
        if p.get(key) not in (None, ""):
            terms[key] = int(p[key])
    if p.get("reward_type"):
        terms["reward_kind"] = p["reward_type"]
    if p.get("reward_trigger"):
        if p["reward_trigger"] not in services.REWARD_TRIGGERS:
            raise UserError("reward_trigger: " + ", ".join(services.REWARD_TRIGGERS))
        terms["reward_trigger"] = p["reward_trigger"]
    changed = {k: v for k, v in {**data, **terms}.items() if o.get(k) != v}
    plan = Plan("update_offer_progress", "safe_edit", f"Offer #{oid} ({o['title']}): " +
                (", ".join(k.replace("_at", "").replace("_pence", "").replace("_", " ") for k in changed) or "no change"),
                touches=[("offer", oid)], noop=not changed)

    def apply(c):
        if terms:
            services.update_offer(c, oid, {k: v for k, v in terms.items() if k in changed}, note=_reason(p) or "terms")
        facts = {k: v for k, v in data.items() if k in changed}
        if facts:
            c.execute("UPDATE offers SET " + ", ".join(f"{k} = ?" for k in facts) + ", updated_at = ? WHERE id = ?",
                      (*facts.values(), now, oid))
            services.audit(c, "offer", oid, "progress", {k: o[k] for k in facts}, facts, _reason(p))
        return offer_state.sync(c, oid) or rewards.lifecycle(c, services.row(c, "offers", oid))
    plan.apply = apply
    return plan


# ---------------------------------------------------------------- outcome model of an existing position

def op_set_position_outcomes(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = services.row(conn, "positions", pid)
    if not pos or pos["deleted_at"]:
        raise UserError(f"No position #{pid}")
    buckets = [str(b).strip()[:60] for b in p.get("outcomes") or [] if str(b).strip()]
    legs = exposure.legs_for(conn, pid)
    covers = {int(x["leg_id"]): [str(c) for c in x.get("covers") or []] for x in p.get("legs") or []}
    missing = [l["id"] for l in legs if l["id"] not in covers]
    if missing:
        raise UserError(f"legs: say which outcomes each bet covers (missing bet(s) {', '.join(map(str, missing))})")
    free = outcomes.FREE.get(pos["kind"])
    eng = [{"side": l["side"], "stake_pence": l["stake_pence"], "odds": l["odds"], "commission_bp": l["commission_bp"],
            "covers": covers[l["id"]], "free": (free or ("snr" if l["free_bet_id"] else None)) if l["side"] == "back" else None,
            "account_id": l["account_id"]} for l in legs]
    tab = outcomes.table(buckets, eng)
    scenarios = [{"label": row["outcome"], "back_wins": any(row["outcome"] in e["covers"] for e in eng if e["side"] == "back"),
                  "pnl": {str(l["id"]): v for l, v in zip(legs, row["legs"])}} for row in tab["buckets"]]
    if {True, False} != {s["back_wins"] for s in scenarios}:
        raise UserError("outcomes: include outcomes where the back wins and where it loses")
    plan = Plan("set_position_outcomes", "safe_edit", f"Outcome model for position #{pid} ({pos['event']}): "
                + ", ".join(buckets), touches=[("position", pid)],
                effects=["liability and projections use these outcomes (lays on outcomes that can't happen together "
                         "are no longer assumed to lose together)",
                         f"worst {fmt(tab['worst'], signed=True)} ({tab['worst_outcome']}), best {fmt(tab['best'], signed=True)}"])
    plan.apply = lambda c: exposure.configure(c, pid, scenarios, _reason(p) or "outcome model set by the assistant from "
                                              "the user's bets")
    return plan


# ---------------------------------------------------------------- issues reviewed as false positives

def op_dismiss_issue(conn, p: dict) -> Plan:
    key = _str(p, "issue_key", 120)
    current = {i["key"]: i for i in issues.collect(conn, include_dismissed=True)}
    if key not in current:
        raise UserError(f"issue_key: no current issue '{key}' (get_issues lists them)")
    issue = current[key]
    reason = _reason(p)
    plan = Plan("dismiss_issue", "safe_edit", f"Mark issue reviewed: {issue['title']}", noop=bool(issue.get("dismissed")),
                effects=["the issue stops showing while its facts stay the same; it comes back automatically if they "
                         "change. No record or money changes."])
    if not reason:
        plan.blocked = "reason: say why it is not a problem (kept in the history)"
    if issue["severity"] == "fix" and not p.get("confirm_fix_severity"):
        plan.level = "financial_reverse"
        plan.warnings.append("this is a 'fix' issue (the records look wrong): dismissing it waits for the user")
    plan.apply = lambda c: {"dismissal_id": issues.dismiss(c, issue, reason)}
    return plan


def op_reopen_issue(conn, p: dict) -> Plan:
    key = _str(p, "issue_key", 120)
    active = conn.execute("SELECT id FROM issue_dismissals WHERE issue_key = ? AND reopened_at IS NULL", (key,)).fetchone()
    plan = Plan("reopen_issue", "safe_edit", f"Show issue {key} again", noop=not active)
    plan.apply = lambda c: issues.reopen(c, key, _reason(p) or "reopened")
    return plan


def op_mark_alerts_notified(conn, p: dict) -> Plan:
    keys = p.get("alert_keys")
    if not isinstance(keys, list) or not keys or len(keys) > 50 or not all(isinstance(k, str) and k for k in keys):
        raise UserError("alert_keys: a list of alert_key values from get_next_actions")
    keys = [k.strip()[:160] for k in keys]
    current = {a["alert_key"]: a for a in brief.next_actions(conn)}
    unknown = [k for k in keys if k not in current]
    if unknown:
        raise UserError("alert_keys: not current alerts: " + ", ".join(unknown))
    todo = [k for k in keys if not current[k]["already_notified"]]
    plan = Plan("mark_alerts_notified", "planning", f"Mark {len(todo)} alert(s) as sent", noop=not todo,
                effects=["reminder bookkeeping only: the alert stays in next actions; it is offered again only if it "
                         "escalates (priority or deadline changes)"])
    plan.apply = lambda c: brief.mark_notified(c, keys)
    return plan


def _offer_of(conn, p: dict) -> dict:
    ref = p.get("offer_id") or p.get("offer")
    if not ref:
        raise UserError("offer_id: which offer this qualifies for")
    if isinstance(ref, int) or str(ref).isdigit():
        o = services.row(conn, "offers", int(ref))
    else:
        r = conn.execute("SELECT * FROM offers WHERE title = ? COLLATE NOCASE AND deleted_at IS NULL ORDER BY id DESC "
                         "LIMIT 1", (str(ref).strip(),)).fetchone()
        o = dict(r) if r else None
    if not o or o["deleted_at"]:
        raise UserError(f"offer: no offer {ref!r}")
    return o


def hedge_advice(conn, stake_pence: int) -> dict:
    """Is laying a cash qualifier worth it? Below the user's limit the bookmaker risk is cheaper than a lay."""
    limit = plans.unhedged_limit(conn)
    if stake_pence <= limit:
        return {"recommendation": "HEDGE_NOT_RECOMMENDED", "max_exposure": plain(stake_pence), "limit": plain(limit),
                "reason": f"Maximum qualifier exposure is {fmt(stake_pence)}; accepting the bookmaker risk is more "
                          f"efficient than laying (your limit for unhedged qualifiers is {fmt(limit)})."}
    return {"recommendation": "HEDGE", "max_exposure": plain(stake_pence), "limit": plain(limit),
            "reason": f"{fmt(stake_pence)} is above your {fmt(limit)} limit for unhedged qualifiers: lay it."}


def op_record_unhedged_qualifier(conn, p: dict) -> Plan:
    """A tiny qualifying bet the user placed and deliberately did not lay, recorded in one go: the bookmaker stake
    (cash moves to unsettled stake), the offer link, the offer's lifecycle and expected reward, and -- if the user
    shows the reward already credited -- the reward itself."""
    offer = _offer_of(conn, p)
    if offer["status"] in services.OFFER_CLOSURES:
        raise UserError(f"offer: #{offer['id']} is {offer['status']}")
    from .changes import find_account
    acc = find_account(conn, p["bookmaker"]) if p.get("bookmaker") else (
        services.row(conn, "accounts", offer["account_id"]) if offer["account_id"] else None)
    if not acc:
        raise UserError("bookmaker: which bookmaker")
    if offer["account_id"] and acc["id"] != offer["account_id"]:
        raise UserError(f"bookmaker: offer #{offer['id']} is at {readapi._name(conn, offer['account_id'])}")
    event = _str(p, "event", 160)
    if not event:
        raise UserError("event: required")
    stake = plans._pence(p.get("stake"), "stake")
    odds = plans._odds(p.get("odds"), "odds")
    reason = _str(p, "reason", 300) or _reason(p)
    if not reason:
        raise UserError("reason: why it was deliberately not laid (e.g. 'the stake is tiny, so the most I can lose is 5p')")
    advice = hedge_advice(conn, stake)
    plan = Plan("record_unhedged_qualifier", "safe_edit",
                f"Qualifier for offer #{offer['id']} ({offer['title']}): {acc['name']} {fmt(stake)} @ {odds} on "
                f"{_str(p, 'selection', 120) or event}, deliberately not laid",
                touches=[("offer", offer["id"])],
                effects=[f"{acc['name']} cash -{fmt(stake)} now held as an unsettled stake (no profit/loss until it settles)",
                         f"accepted exposure {fmt(stake)}: shown as 'unhedged by design', not an issue",
                         "the offer moves on by its own rules and expects its reward when due"])
    plan.warnings.append(advice["reason"]) if advice["recommendation"] == "HEDGE" else None
    if offer["qualifying_stake_pence"] and stake < offer["qualifying_stake_pence"]:
        plan.blocked = f"stake {fmt(stake)} is below the offer's qualifying stake {fmt(offer['qualifying_stake_pence'])}"
    if offer["min_odds"] and Decimal(odds) < Decimal(offer["min_odds"]):
        plan.blocked = f"odds {odds} are below the offer's minimum {offer['min_odds']}"
    if advice["recommendation"] == "HEDGE":
        plan.level = "financial_reverse"                  # leaving a bigger stake unhedged waits for the user
    existing = [q for q in conn.execute(
        "SELECT p.id, l.account_id, l.stake_pence, l.odds FROM positions p JOIN legs l ON l.position_id = p.id AND "
        "l.deleted_at IS NULL AND l.side = 'back' WHERE p.offer_id = ? AND p.kind = 'qualifying' AND p.deleted_at IS "
        "NULL AND p.status <> 'void'", (offer["id"],)).fetchall()]
    same = [q for q in existing if q["account_id"] == acc["id"] and q["stake_pence"] == stake
            and Decimal(q["odds"]) == Decimal(odds)]
    if existing and not same:
        plan.dependencies = [f"position #{q['id']}" for q in existing]
        plan.blocked = (f"offer #{offer['id']} already has a qualifying bet (position #{existing[0]['id']}); check it "
                        "before recording another")
    want_reward = bool(p.get("reward_credited"))
    state0 = offer_state.derive(conn, offer)
    if same and (not want_reward or state0["reward_received"]):
        plan.noop = True
        plan.summary = f"Already recorded: position #{same[0]['id']} qualifies for offer #{offer['id']}"
        return plan
    if want_reward:
        plan.effects.append(f"reward credited: {offer['reward_count'] or 1} x {fmt(offer['reward_each_pence'] or 0)} "
                            f"{(offer['reward_kind'] or '').upper()} linked to the expected reward (never twice)")

    def apply(c):
        pid = same[0]["id"] if same else services.create_position(
            c, event=event, market=_str(p, "market", 120) or "", selection=_str(p, "selection", 120) or "",
            event_at=_when(p, "event_time"), kind="qualifying", offer_id=offer["id"], source="assistant",
            notes=_str(p, "notes", 500) or "", hedge_strategy="intentionally_unhedged", unhedged_reason=reason,
            legs=[{"account_id": acc["id"], "side": "back", "stake_pence": stake, "odds": odds,
                   "placed_at": _when(p, "placed_at"), "external_ref": _str(p, "reference", 120) or ""}])
        if not same:
            plan.created.append(("position", pid))
        for e in p.get("evidence") or []:
            evidence.add(c, entity="position", entity_id=pid, kind=e.get("kind") or "bet_slip",
                         description=str(e.get("description") or "")[:300], facts=e.get("facts") or {})
        state = offer_state.sync(c, offer["id"], note=f"qualifier position #{pid}")
        reward = None
        if want_reward:
            if not state["qualified"]:                    # the bookmaker crediting it is its confirmation
                offer_state.mark_qualified(c, offer["id"], note="the reward was credited")
            er = c.execute("SELECT id FROM expected_rewards WHERE offer_id = ? AND status <> 'void' ORDER BY id DESC",
                           (offer["id"],)).fetchone()
            if not er:
                raise UserError("the offer has no reward terms to expect: set them with update_offer_progress")
            reward = rewards.mark_received(c, er[0], free_bet_ids=p.get("reward_free_bet_ids") or None,
                                           expires_at=_when(p, "reward_expires_at"), at=_when(p, "reward_received_at"))
            state = offer_state.sync(c, offer["id"])
        from . import imports
        superseded = imports.supersede_pending(c, "position", {"event": event, "legs": [
            {"account_id": acc["id"], "side": "back", "stake_pence": stake, "odds": odds}]}, f"position:{pid}")
        return {"position_id": pid, "hedge": "intentionally_unhedged", "accepted_exposure": plain(stake),
                "hedge_advice": advice, "offer": {"id": offer["id"], "status": state["status"], "stage": state["stage"],
                                                  "next_action": state["next_action"],
                                                  "qualifier_required": state["qualifier_required"]},
                "expected_reward": brief.expected_rewards_for_offer(c, offer["id"]), "reward": reward,
                "superseded_candidates": superseded}
    plan.apply = apply
    return plan


def op_set_hedge_strategy(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = services.row(conn, "positions", pid)
    if not pos or pos["deleted_at"]:
        raise UserError(f"No position #{pid}")
    strategy = _str(p, "hedge_strategy", 30)
    if strategy not in services.HEDGE_STRATEGIES:
        raise UserError("hedge_strategy: matched, intentionally_unhedged or pending")
    reason = _str(p, "reason", 300) or ""
    plan = Plan("set_hedge_strategy", "safe_edit", f"Position #{pid} ({pos['event']}): hedging {strategy.replace('_', ' ')}",
                touches=[("position", pid)], noop=pos["hedge_strategy"] == strategy and
                (strategy != "intentionally_unhedged" or (pos["unhedged_reason"] or "") == reason))
    plan.apply = lambda c: services.set_hedge_strategy(c, pid, strategy, reason, note=reason)
    return plan


NEW = {
    "create_bet_plan": op_create_bet_plan, "update_bet_plan": op_update_bet_plan,
    "recalculate_bet_plan": op_recalculate_bet_plan, "validate_bet_plan": op_validate_bet_plan,
    "cancel_bet_plan": op_cancel_bet_plan, "convert_bet_plan_to_position": op_convert_bet_plan_to_position,
    "record_bookmaker_placement": op_record_bookmaker_placement, "record_exchange_order": op_record_exchange_order,
    "record_exchange_fill": op_record_exchange_fill, "cancel_exchange_order": op_cancel_exchange_order,
    "mark_exchange_fully_matched": op_mark_exchange_fully_matched, "record_matched_bet": op_record_matched_bet,
    "record_funding_transfer": op_record_funding_transfer, "add_record_evidence": op_add_record_evidence,
    "expect_reward": op_expect_reward, "reconcile_rewards": op_reconcile_rewards,
    "mark_reward_received": op_mark_reward_received, "update_offer_progress": op_update_offer_progress,
    "set_position_outcomes": op_set_position_outcomes, "dismiss_issue": op_dismiss_issue, "reopen_issue": op_reopen_issue,
    "mark_alerts_notified": op_mark_alerts_notified,
    "record_unhedged_qualifier": op_record_unhedged_qualifier, "set_hedge_strategy": op_set_hedge_strategy,
}
OPS.update(NEW)
TITLES.update({k: k.replace("_", " ").capitalize() for k in NEW})
