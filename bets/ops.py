"""Maintenance operations: the one way existing records are edited, linked, voided, reversed or deleted, whether the
request comes from the assistant (Assistant API / MCP) or from the newer controls in the web app.

Each operation:
  * validates its parameters and looks at the records it depends on first (a linked free bet is never deleted, a
    settled position is never silently re-settled);
  * declares a permission level (below), sometimes depending on the records (editing a settled bet is financial);
  * changes data only through bets/services.py, so the ledger rules and the audit trail are the usual ones;
  * is previewed exactly: the change is applied inside a SAVEPOINT, the after-state captured, and rolled back;
  * reports already_done instead of repeating itself when the records are already in the requested state.

Permission levels (the user chooses in Settings which ones the assistant may apply without asking):
  safe_edit           metadata and corrections that move no money: expiry, notes, offer links, relinking a free bet
                      to the bet that used it, settling an open position with a result the user gave, reclassifying
                      a transfer, recording a balance (never as profit/loss)
  financial_reverse   changes a realised figure: reopening or re-settling a settled position, editing a settled bet,
                      reversing a transfer or a balance check's adjustment, turning a cash bet into a free bet
  destructive_delete  removing a record (free bet, position, offer); always waits for the user

Assistant requests are idempotent: the same request_id returns the first result (a repeat never applies twice).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from . import changes, issues, metrics, outlook, readapi, services, times
from .money import commission_text, fmt, parse_commission, parse_odds, plain, to_pence
from .services import UserError

LEVELS = ("planning", "safe_edit", "financial_reverse", "destructive_delete")
LEVEL_TITLES = {"read": "Read records", "planning": "Planning", "safe_edit": "Safe bookkeeping",
                "financial_reverse": "Financial changes", "destructive_delete": "Deletions"}
PERM_SETTING = "assistant_permissions"
DEFAULT_PERMS = {"read": True, "planning": "auto", "safe_edit": "auto", "financial_reverse": "ask",
                 "destructive_delete": "ask"}
ANOMALY_SETTING = "anomaly_thresholds"
DEFAULT_ANOMALY = {"profit_change": 2500, "balance_change_without_transfer": 10000, "free_bets_change": 3,
                   "liability_change": 20000}
REASON_REQUIRED = {"financial_reverse", "destructive_delete"}


@dataclass
class Plan:
    op: str
    level: str
    summary: str
    touches: list = field(default_factory=list)        # [(kind, id)] shown before / after
    effects: list = field(default_factory=list)        # plain-language consequences
    dependencies: list = field(default_factory=list)   # records that refer to the target
    warnings: list = field(default_factory=list)
    blocked: str | None = None                         # why it cannot be done (with what to do instead)
    noop: bool = False                                 # already in the requested state
    apply: Callable | None = None
    created: list = field(default_factory=list)        # [(kind, id)] records the change created (filled by apply)


def permissions(conn) -> dict:
    saved = services.get_setting(conn, PERM_SETTING, {}) or {}
    out = dict(DEFAULT_PERMS)
    if isinstance(saved.get("read"), bool):
        out["read"] = saved["read"]
    for k in ("planning", "safe_edit", "financial_reverse"):
        if saved.get(k) in ("auto", "ask"):
            out[k] = saved[k]
    out["destructive_delete"] = "ask"                  # never automatic
    return out


def save_permissions(conn, read: bool, safe_edit: str, financial_reverse: str, planning: str = "auto") -> dict:
    before = permissions(conn)
    new = {"read": bool(read), "planning": "auto" if planning == "auto" else "ask",
           "safe_edit": "auto" if safe_edit == "auto" else "ask",
           "financial_reverse": "auto" if financial_reverse == "auto" else "ask", "destructive_delete": "ask"}
    services.set_setting(conn, PERM_SETTING, new)
    services.audit(conn, "setting", None, "assistant_permissions", before, new, "changed in Settings")
    return new


# ---------------------------------------------------------------- parameter helpers

def _int(p: dict, key: str, required: bool = True) -> int | None:
    v = p.get(key)
    if v in (None, ""):
        if required:
            raise UserError(f"{key}: required")
        return None
    s = str(v).strip().lstrip("#")
    if not s.isdigit():
        raise UserError(f"{key}: a record number")
    return int(s)


def _str(p: dict, key: str, limit: int = 500) -> str | None:
    v = p.get(key)
    return None if v is None else str(v).strip()[:limit]


HAPPENED = ("placed_at", "matched_at", "when", "captured_at", "received_at", "settled_at", "observed_at")


def _when(p: dict, key: str) -> str | None:
    v = _str(p, key, 25)
    if not v:
        return None
    try:
        return times.from_local_input(v, happened=key in HAPPENED)
    except ValueError:
        raise UserError(f"{key}: a UK local date/time YYYY-MM-DD or YYYY-MM-DDTHH:MM") from None


def _need(conn, table: str, rid: int, what: str) -> dict:
    r = services.row(conn, table, rid)
    if not r or r.get("deleted_at"):
        raise UserError(f"No {what} #{rid}")
    return r


def _live_leg_for_token(conn, fid: int):
    return conn.execute("SELECT l.*, p.event FROM legs l JOIN positions p ON p.id = l.position_id WHERE l.free_bet_id = ? "
                        "AND l.deleted_at IS NULL ORDER BY l.id LIMIT 1", (fid,)).fetchone()


def _reason(p: dict) -> str:
    return (_str(p, "reason", 300) or "").strip()


# ---------------------------------------------------------------- free bets

def op_update_free_bet(conn, p: dict) -> Plan:
    fid = _int(p, "free_bet_id")
    fb = _need(conn, "free_bets", fid, "free bet")
    data, effects = {}, []
    if p.get("expires_at") not in (None, ""):
        data["expires_at"] = _when(p, "expires_at")
    if p.get("clear_expiry"):
        data["expires_at"] = None
    if p.get("notes") is not None:
        data["notes"] = _str(p, "notes", 2000)
    if p.get("append_note"):
        data["notes"] = ((fb["notes"] + " · ") if fb["notes"] else "") + _str(p, "append_note", 1000)
    if p.get("offer_id") not in (None, ""):
        data["offer_id"] = _int(p, "offer_id")
    if p.get("clear_offer"):
        data["offer_id"] = None
    status = _str(p, "status", 20)
    link = _live_leg_for_token(conn, fid)
    if status:
        if status == "used":
            return Plan("update_free_bet", "safe_edit", "", blocked="A free bet becomes 'used' by linking it to the bet "
                        "that used it: use link_free_bet_to_position")
        if status not in ("available", "expired", "void"):
            raise UserError("status: available, expired or void")
        if link and status != fb["status"]:
            return Plan("update_free_bet", "safe_edit", "", dependencies=[f"linked to position #{link['position_id']} "
                        f"({link['event']}), bet #{link['id']}"], blocked="It is linked to a bet: relink or unlink it "
                        "first (link_free_bet_to_position / unlink_free_bet_from_position)")
        data["status"] = status
    for key, col in (("value", "face_value_pence"), ("type", "kind")):
        if p.get(key) not in (None, ""):
            if link:
                return Plan("update_free_bet", "safe_edit", "", blocked=f"It is linked to bet #{link['id']}: its value "
                            "and type decide that bet's result. Correct the bet instead.")
            data[col] = to_pence(str(p[key]), allow_zero=False) if key == "value" else str(p[key]).lower()
    if data.get("offer_id"):
        offer = _need(conn, "offers", data["offer_id"], "offer")
        if offer["account_id"] and offer["account_id"] != fb["account_id"]:
            raise UserError("offer_id: that offer is from a different bookmaker")
    changed = {k: v for k, v in data.items() if fb.get(k) != v}
    plan = Plan("update_free_bet", "safe_edit", f"Update free bet #{fid}", touches=[("free_bet", fid)],
                noop=not changed)
    plan.apply = lambda c: services.update_free_bet(c, fid, changed, note=_reason(p) or "assistant correction")
    return plan


def _target_leg(conn, pos: dict, fb: dict, leg_id: int | None):
    legs = conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL AND side = 'back' ORDER BY id",
                        (pos["id"],)).fetchall()
    if leg_id:
        legs = [l for l in legs if l["id"] == leg_id]
        if not legs:
            raise UserError(f"leg_id: bet #{leg_id} is not a back bet of position #{pos['id']}")
        return legs[0]
    fits = [l for l in legs if l["account_id"] == fb["account_id"] and l["stake_pence"] == fb["face_value_pence"]]
    if len(fits) == 1:
        return fits[0]
    if not fits:
        raise UserError(f"Position #{pos['id']} has no {readapi._name(conn, fb['account_id'])} back bet of "
                        f"{fmt(fb['face_value_pence'])} for this free bet")
    raise UserError(f"Position #{pos['id']} has several matching back bets: give leg_id")


def op_link_free_bet_to_position(conn, p: dict) -> Plan:
    fid, pid = _int(p, "free_bet_id"), _int(p, "position_id")
    fb = _need(conn, "free_bets", fid, "free bet")
    pos = _need(conn, "positions", pid, "position")
    leg = _target_leg(conn, pos, fb, _int(p, "leg_id", required=False))
    previous = _str(p, "previous_token_status", 12) or "void"
    current = _live_leg_for_token(conn, fid)
    if current and current["id"] == leg["id"]:
        return Plan("link_free_bet_to_position", "safe_edit", f"Free bet #{fid} is already linked to position #{pid}",
                    touches=[("free_bet", fid)], noop=True)
    if current:
        return Plan("link_free_bet_to_position", "safe_edit", "", dependencies=[
            f"already linked to position #{current['position_id']} ({current['event']}), bet #{current['id']}"],
            blocked="That free bet is linked to another bet: unlink it there first, or link the other bet's correct token")
    if leg["free_bet_id"]:
        old = services.row(conn, "free_bets", leg["free_bet_id"])
        plan = Plan("link_free_bet_to_position", "safe_edit",
                    f"Bet #{leg['id']} on position #{pid} uses free bet #{fid} instead of #{old['id']} "
                    f"(#{old['id']} becomes {previous})",
                    touches=[("free_bet", fid), ("free_bet", old["id"]), ("position", pid)],
                    effects=["no money changes: same bookmaker, value and type"])
        if (old["account_id"], old["face_value_pence"], old["kind"]) != (fb["account_id"], fb["face_value_pence"], fb["kind"]):
            plan.blocked = "The bet uses a free bet of a different bookmaker, value or type; correct the bet instead"
        plan.apply = lambda c: services.relink_free_bet(c, leg["id"], fid, previous, note=_reason(p) or "relinked")
        return plan
    plan = Plan("link_free_bet_to_position", "financial_reverse",
                f"Bet #{leg['id']} on position #{pid} is recorded as a cash bet: it becomes free bet #{fid}",
                touches=[("free_bet", fid), ("position", pid)],
                effects=["its cash stake is withdrawn and a settled bet is re-settled with free-bet maths (changes "
                         "the position's result)"])
    plan.apply = lambda c: services.link_free_bet(c, leg["id"], fid, note=_reason(p) or "linked")
    return plan


def op_unlink_free_bet_from_position(conn, p: dict) -> Plan:
    fid = _int(p, "free_bet_id")
    _need(conn, "free_bets", fid, "free bet")
    then = _str(p, "then_status", 12) or "available"
    link = _live_leg_for_token(conn, fid)
    if not link:
        return Plan("unlink_free_bet_from_position", "financial_reverse", f"Free bet #{fid} is not linked to a bet",
                    touches=[("free_bet", fid)], noop=True)
    plan = Plan("unlink_free_bet_from_position", "financial_reverse",
                f"Take free bet #{fid} off bet #{link['id']} (position #{link['position_id']}); it becomes {then}",
                touches=[("free_bet", fid), ("position", link["position_id"])],
                effects=["that bet then counts as a cash stake: its stake is posted and a settled bet re-settled "
                         "(changes the position's result). To swap in the right token instead, use "
                         "link_free_bet_to_position with that token."])
    plan.apply = lambda c: services.unlink_free_bet(c, fid, then, note=_reason(p))
    return plan


def op_void_free_bet(conn, p: dict) -> Plan:
    fid = _int(p, "free_bet_id")
    fb = _need(conn, "free_bets", fid, "free bet")
    link = _live_leg_for_token(conn, fid)
    if link:
        return Plan("void_free_bet", "safe_edit", "", dependencies=[f"linked to position #{link['position_id']} "
                    f"({link['event']}), bet #{link['id']}"], blocked="It is linked to a bet: relink that bet to the "
                    "right token (link_free_bet_to_position) or unlink it first")
    plan = Plan("void_free_bet", "safe_edit", f"Void free bet #{fid} ({readapi._name(conn, fb['account_id'])} "
                f"{fmt(fb['face_value_pence'])})", touches=[("free_bet", fid)], noop=fb["status"] == "void",
                effects=["it stops counting in free bets held and Still to come; kept in the history"])
    note = _reason(p) or "voided"
    plan.apply = lambda c: services.update_free_bet(c, fid, {"status": "void", "notes": ((fb["notes"] + " · ") if fb["notes"]
                                                                                         else "") + f"Void: {note}"}, note=note)
    return plan


def op_delete_free_bet(conn, p: dict) -> Plan:
    fid = _int(p, "free_bet_id")
    fb = services.row(conn, "free_bets", fid)
    if not fb:
        raise UserError(f"No free bet #{fid}")
    if fb["deleted_at"]:
        return Plan("delete_free_bet", "destructive_delete", f"Free bet #{fid} is already removed", noop=True)
    link = _live_leg_for_token(conn, fid)
    used_before = conn.execute("SELECT count(*) FROM legs WHERE free_bet_id = ?", (fid,)).fetchone()[0]
    if link:
        return Plan("delete_free_bet", "destructive_delete", "", dependencies=[f"linked to position #{link['position_id']} "
                    f"({link['event']}), bet #{link['id']}"], blocked="It is linked to a bet, so it cannot be deleted. "
                    "If it is a duplicate, use merge_duplicate_free_bets or link the bet to the right token and void this one")
    plan = Plan("delete_free_bet", "destructive_delete", f"Delete free bet #{fid} ({readapi._name(conn, fb['account_id'])} "
                f"{fmt(fb['face_value_pence'])}, {fb['status']})", touches=[("free_bet", fid)],
                effects=["removed from every list; the record stays in the audit history"])
    if used_before:
        plan.warnings.append("a removed bet once used it; voiding keeps that trail visible")
    plan.apply = lambda c: services.remove_free_bet(c, fid, note=_reason(p))
    return plan


def op_merge_duplicate_free_bets(conn, p: dict) -> Plan:
    keep_id, dup_id = _int(p, "keep_free_bet_id"), _int(p, "duplicate_free_bet_id")
    if keep_id == dup_id:
        raise UserError("Choose two different free bets")
    keep = _need(conn, "free_bets", keep_id, "free bet")
    dup = _need(conn, "free_bets", dup_id, "free bet")
    if (keep["account_id"], keep["face_value_pence"], keep["kind"]) != (dup["account_id"], dup["face_value_pence"], dup["kind"]):
        return Plan("merge_duplicate_free_bets", "safe_edit", "", blocked="They are not the same bookmaker, value and "
                    "type, so they are not duplicates")
    keep_leg, dup_leg = _live_leg_for_token(conn, keep_id), _live_leg_for_token(conn, dup_id)
    if keep_leg and dup_leg:
        return Plan("merge_duplicate_free_bets", "safe_edit", "", blocked=f"Both were used by bets (#{keep_leg['id']} and "
                    f"#{dup_leg['id']}), so they are two real free bets, not duplicates")
    if dup["status"] == "void" and not dup_leg and _merged_already(keep, dup):
        return Plan("merge_duplicate_free_bets", "safe_edit", f"#{dup_id} is already merged into #{keep_id}",
                    touches=[("free_bet", keep_id), ("free_bet", dup_id)], noop=True)
    fill = {}
    if not keep["expires_at"] and dup["expires_at"]:
        fill["expires_at"] = dup["expires_at"]
    if not keep["offer_id"] and dup["offer_id"]:
        fill["offer_id"] = dup["offer_id"]
    if dup["notes"] and dup["notes"] not in (keep["notes"] or ""):
        fill["notes"] = ((keep["notes"] + " · ") if keep["notes"] else "") + dup["notes"]
    effects = [f"#{dup_id} becomes void (kept in the history)"]
    if dup_leg:
        effects.insert(0, f"bet #{dup_leg['id']} (position #{dup_leg['position_id']}) now uses #{keep_id}; no money changes")
    if fill:
        effects.append(f"#{keep_id} takes #{dup_id}'s " + ", ".join(k.replace("_id", "").replace("_at", "") for k in fill))
    touches = [("free_bet", keep_id), ("free_bet", dup_id)] + ([("position", dup_leg["position_id"])] if dup_leg else [])
    plan = Plan("merge_duplicate_free_bets", "safe_edit", f"Merge duplicate free bet #{dup_id} into #{keep_id}",
                touches=touches, effects=effects)
    note = _reason(p) or f"duplicate of #{keep_id}"

    def apply(c):
        if fill:
            services.update_free_bet(c, keep_id, fill, note=f"merged from duplicate #{dup_id}: {note}")
        if dup_leg:
            services.relink_free_bet(c, dup_leg["id"], keep_id, "void", note=f"duplicate #{dup_id} merged into #{keep_id}")
        cur = services.row(c, "free_bets", dup_id)
        services.update_free_bet(c, dup_id, {"status": "void", "notes": ((cur["notes"] + " · ") if cur["notes"] else "")
                                             + f"Duplicate of #{keep_id}: {note}"}, note=note)
    plan.apply = apply
    return plan


def _merged_already(keep: dict, dup: dict) -> bool:
    return f"Duplicate of #{keep['id']}" in (dup["notes"] or "")


# ---------------------------------------------------------------- positions

def op_update_position(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = _need(conn, "positions", pid, "position")
    data = {}
    for key, col in (("event", "event"), ("selection", "selection"), ("market", "market"), ("notes", "notes")):
        if p.get(key) is not None:
            data[col] = _str(p, key, 2000 if key == "notes" else 160)
    if p.get("event_time") not in (None, ""):
        data["event_at"] = _when(p, "event_time")
    if p.get("offer_id") not in (None, ""):
        data["offer_id"] = _int(p, "offer_id")
        _need(conn, "offers", data["offer_id"], "offer")
    if p.get("clear_offer"):
        data["offer_id"] = None
    level = "safe_edit"
    if p.get("type") not in (None, ""):
        if p["type"] not in ("qualifying", "free_bet_snr", "free_bet_sr", "other"):
            raise UserError("type: qualifying, free_bet_snr, free_bet_sr or other")
        data["kind"] = p["type"]
        if pos["status"] != "open" and data["kind"] != pos["kind"]:
            level = "financial_reverse"
    changed = {k: v for k, v in data.items() if pos.get(k) != v}
    plan = Plan("update_position", level, f"Update position #{pid}", touches=[("position", pid)], noop=not changed)
    plan.apply = lambda c: services.update_position(c, pid, changed, note=_reason(p) or "correction")
    return plan


def op_update_leg(conn, p: dict) -> Plan:
    lid = _int(p, "leg_id")
    leg = _need(conn, "legs", lid, "bet")
    data = {}
    if p.get("stake") not in (None, ""):
        data["stake_pence"] = to_pence(str(p["stake"]), allow_zero=False)
    if p.get("odds") not in (None, ""):
        from .money import odds_text
        data["odds"] = odds_text(parse_odds(p["odds"]))
    if p.get("commission") not in (None, ""):
        data["commission_bp"] = parse_commission(str(p["commission"]))
    if p.get("placed_at") not in (None, ""):
        data["placed_at"] = _when(p, "placed_at")
    if p.get("reference") is not None:
        data["external_ref"] = _str(p, "reference", 120)
    changed = {k: v for k, v in data.items() if leg.get(k) != v}
    settled = leg["result"] != "pending"
    plan = Plan("update_leg", "financial_reverse" if settled and set(changed) - {"external_ref", "placed_at"} else "safe_edit",
                f"Update bet #{lid} on position #{leg['position_id']}", touches=[("position", leg["position_id"])],
                noop=not changed)
    if settled and changed:
        plan.effects.append("the bet is settled: its settlement is recalculated from the corrected details")
    plan.apply = lambda c: services.update_leg(c, lid, changed, note=_reason(p) or "correction")
    return plan


OUTCOMES = ("back_won", "back_lost", "void")


def _wanted_results(conn, pid: int, p: dict) -> dict:
    legs = conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL", (pid,)).fetchall()
    if not legs:
        raise UserError("That position has no bets")
    outcome = _str(p, "outcome", 12)
    wanted = {}
    if outcome:
        if outcome not in OUTCOMES:
            raise UserError("outcome: back_won, back_lost or void")
        for l in legs:
            wanted[l["id"]] = ("void" if outcome == "void" else
                               ("won" if (l["side"] == "back") == (outcome == "back_won") else "lost"), None)
    for item in p.get("legs") or []:
        lid = _int(item, "leg_id")
        if lid not in {l["id"] for l in legs}:
            raise UserError(f"legs: bet #{lid} is not part of position #{pid}")
        res = str(item.get("result") or "")
        if res not in ("won", "lost", "void", "cashout", "manual"):
            raise UserError("legs: result won, lost, void, cashout or manual")
        amount = to_pence(str(item["amount"]), allow_negative=True) if item.get("amount") not in (None, "") else None
        if res in ("cashout", "manual") and amount is None:
            raise UserError("legs: cashout / manual need the amount actually credited or debited")
        wanted[lid] = (res, amount)
    if not wanted:
        raise UserError("Give the outcome (back_won / back_lost / void), the outcome_bucket, or each bet's result")
    return {"legs": {l["id"]: dict(l) for l in legs}, "wanted": wanted}


UNUSUAL = ("void", "abandoned", "postponed", "dead_heat", "rule_4", "cash_out", "reduction_factor", "partial", "other")


def _bucket_results(conn, pid: int, bucket: str) -> list[dict]:
    """Each leg's result in one outcome bucket, from the position's verified outcome table (bets/exposure.py)."""
    from . import exposure
    data = exposure.valid(conn, pid)
    if data is None:
        pos = services.row(conn, "positions", pid)
        if exposure.configured(conn, pid):
            raise UserError("The bets changed after their outcomes were recorded: review the outcomes first")
        simple = [pos["selection"] or "Selection", f"Not {pos['selection'] or 'Selection'}"]
        if bucket not in simple:
            raise UserError(f"outcome_bucket: this position has two outcomes: {simple[0]!r} or {simple[1]!r}")
        return [{"leg_id": None, "outcome": "back_won" if bucket == simple[0] else "back_lost"}]
    legs, scenarios = data
    match = [s for s in scenarios if s["label"] == bucket]
    if not match:
        raise UserError("outcome_bucket: one of " + ", ".join(repr(s["label"]) for s in scenarios))
    s = match[0]
    return [{"leg_id": l["id"], "result": "won" if s["pnl"][str(l["id"])] > 0 else "lost"} for l in legs]


def op_settle_position(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = _need(conn, "positions", pid, "position")
    if p.get("outcome_bucket"):
        mapped = _bucket_results(conn, pid, str(p["outcome_bucket"]).strip())
        if mapped and mapped[0]["leg_id"] is None:
            p = {**p, "outcome": mapped[0]["outcome"]}
        else:
            p = {**p, "legs": [{"leg_id": m["leg_id"], "result": m["result"]} for m in mapped]}
    unusual = _str(p, "unusual", 30)
    if unusual and unusual not in UNUSUAL:
        raise UserError("unusual: " + ", ".join(UNUSUAL))
    w = _wanted_results(conn, pid, p)
    at = _when(p, "settled_at")
    differs = [lid for lid, (res, amount) in w["wanted"].items()
               if w["legs"][lid]["result"] != "pending" and (w["legs"][lid]["result"] != res or
                                                              (amount is not None and amount != w["legs"][lid]["settled_amount_pence"]))]
    todo = [lid for lid, (res, amount) in w["wanted"].items() if w["legs"][lid]["result"] == "pending" or lid in differs]
    if not todo:
        return Plan("settle_position", "safe_edit", f"Position #{pid} is already settled that way",
                    touches=[("position", pid)], noop=True)
    resettle = bool(differs)
    if resettle and not p.get("resettle"):
        return Plan("settle_position", "financial_reverse", "", dependencies=[
            "already settled: " + ", ".join(f"bet #{lid} {w['legs'][lid]['result']}" for lid in differs)],
            blocked="Already settled differently. Say resettle=true to change a settled result (financial), or "
                    "reopen_position first")
    plan = Plan("settle_position", "financial_reverse" if resettle or unusual else "safe_edit",
                f"Settle position #{pid} ({pos['event']}): " + ", ".join(
                    f"{w['legs'][lid]['side']} #{lid} {w['wanted'][lid][0]}" for lid in todo)
                + (f" [{unusual.replace('_', ' ')}]" if unusual else ""),
                touches=[("position", pid)],
                effects=["realised profit changes by the position's result; it leaves the open-bet projections"])
    if unusual:
        plan.warnings.append(f"unusual settlement ({unusual.replace('_', ' ')}): held for review unless the user "
                             "allows financial changes automatically")
    note = _reason(p) or ("result given by the user" + (f": {p.get('outcome')}" if p.get("outcome") else ""))

    def apply(c):
        for lid in todo:
            res, amount = w["wanted"][lid]
            services.settle_leg(c, lid, res, amount=amount, at=at, note=note)
    plan.apply = apply
    return plan


def op_reopen_position(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = _need(conn, "positions", pid, "position")
    legs = [r["id"] for r in conn.execute("SELECT id FROM legs WHERE position_id = ? AND deleted_at IS NULL AND result <> "
                                          "'pending'", (pid,))]
    plan = Plan("reopen_position", "financial_reverse", f"Reopen position #{pid} ({pos['event']})",
                touches=[("position", pid)], noop=not legs,
                effects=[f"its settlement entries are withdrawn: realised profit changes by "
                         f"{fmt(-services.position_pnl(conn, pid), signed=True)}"] if legs else [])
    plan.apply = lambda c: [services.settle_leg(c, lid, "pending", note=_reason(p)) for lid in legs]
    return plan


def op_void_position(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = _need(conn, "positions", pid, "position")
    legs = conn.execute("SELECT id, result FROM legs WHERE position_id = ? AND deleted_at IS NULL", (pid,)).fetchall()
    if not legs:
        raise UserError("That position has no bets")
    if all(l["result"] == "void" for l in legs):
        return Plan("void_position", "safe_edit", f"Position #{pid} is already void", touches=[("position", pid)], noop=True)
    settled = any(l["result"] not in ("pending", "void") for l in legs)
    plan = Plan("void_position", "financial_reverse" if settled else "safe_edit",
                f"Void position #{pid} ({pos['event']}): every bet void (stakes refunded, free bets returned)",
                touches=[("position", pid)])
    plan.apply = lambda c: [services.settle_leg(c, l["id"], "void", note=_reason(p) or "voided by the bookmaker")
                            for l in legs if l["result"] != "void"]
    return plan


def op_delete_position(conn, p: dict) -> Plan:
    pid = _int(p, "position_id")
    pos = services.row(conn, "positions", pid)
    if not pos:
        raise UserError(f"No position #{pid}")
    if pos["deleted_at"]:
        return Plan("delete_position", "destructive_delete", f"Position #{pid} is already removed", noop=True)
    tokens = [r[0] for r in conn.execute("SELECT free_bet_id FROM legs WHERE position_id = ? AND deleted_at IS NULL AND "
                                         "free_bet_id IS NOT NULL", (pid,))]
    pnl = services.position_pnl(conn, pid) if pos["status"] != "open" else 0
    plan = Plan("delete_position", "destructive_delete", f"Delete position #{pid} ({pos['event']}, {pos['status']})",
                touches=[("position", pid)] + [("free_bet", f) for f in tokens],
                effects=["every bet and its ledger entries are withdrawn (kept in the history)"]
                + ([f"realised profit changes by {fmt(-pnl, signed=True)}"] if pnl else [])
                + ([f"free bet(s) {', '.join('#' + str(f) for f in tokens)} become available again"] if tokens else []))
    plan.apply = lambda c: services.delete_position(c, pid, _reason(p))
    return plan


# ---------------------------------------------------------------- transfers

def op_update_transfer(conn, p: dict) -> Plan:
    tid = _int(p, "transfer_id")
    x = services.row(conn, "transfers", tid)
    if not x or x["voided_at"]:
        raise UserError(f"No transfer #{tid}")
    steps, desc = [], []
    note = _reason(p) or "correction"
    when = _when(p, "when")
    if when and when != x["at"]:
        steps.append(lambda c: services.retime_transfer(c, tid, when, note))
        desc.append("time")
    if p.get("notes") is not None and _str(p, "notes", 2000) != x["notes"]:
        steps.append(lambda c: services.update_transfer_notes(c, tid, _str(p, "notes", 2000), note))
        desc.append("notes")
    if p.get("return_by") not in (None, "") or p.get("clear_return_by"):
        due = None if p.get("clear_return_by") else _when(p, "return_by")
        if due != x["due_at"]:
            steps.append(lambda c: services.set_transfer_due(c, tid, due, note))
            desc.append("return date")
    if p.get("arrived") and x["in_transit"]:
        steps.append(lambda c: services.mark_arrived(c, tid))
        desc.append("arrived")
    plan = Plan("update_transfer", "safe_edit", f"Update transfer #{tid}: " + (", ".join(desc) or "nothing to change"),
                touches=[("transfer", tid)], noop=not steps, effects=["balances and profit are unchanged"])
    plan.apply = lambda c: [s(c) for s in steps]
    return plan


def op_reclassify_transfer(conn, p: dict) -> Plan:
    tid = _int(p, "transfer_id")
    x = services.row(conn, "transfers", tid)
    if not x or x["voided_at"]:
        raise UserError(f"No transfer #{tid}")
    kind = _str(p, "funding_type", 30)
    if kind not in readapi.CAPITAL_MEANING:
        raise UserError("funding_type: " + ", ".join(readapi.CAPITAL_MEANING))
    natural = services.default_capital_type(services.row(conn, "accounts", x["from_account_id"])["kind"],
                                            services.row(conn, "accounts", x["to_account_id"])["kind"])
    if kind not in services.allowed_capital_types(natural):
        raise UserError("funding_type: for these accounts use " + ", ".join(sorted(services.allowed_capital_types(natural))))
    level = "financial_reverse" if "profit_withdrawal" in (kind, x["capital_type"]) else "safe_edit"
    plan = Plan("reclassify_transfer", level, f"Transfer #{tid} ({fmt(x['amount_pence'])}): {x['capital_type'].replace('_', ' ')} → {kind.replace('_', ' ')}",
                touches=[("transfer", tid)], noop=kind == x["capital_type"],
                effects=["balances and realised profit are unchanged (transfers are capital)",
                         "temporary funding outstanding is recalculated for the bank involved"])
    plan.apply = lambda c: services.reclassify_transfer(c, tid, kind, _reason(p) or "classification corrected by the user")
    return plan


def op_reverse_transfer(conn, p: dict) -> Plan:
    tid = _int(p, "transfer_id")
    x = services.row(conn, "transfers", tid)
    if not x:
        raise UserError(f"No transfer #{tid}")
    if x["voided_at"]:
        return Plan("reverse_transfer", "financial_reverse", f"Transfer #{tid} is already reversed",
                    touches=[("transfer", tid)], noop=True)
    plan = Plan("reverse_transfer", "financial_reverse", f"Reverse transfer #{tid} ({fmt(x['amount_pence'])} "
                f"{readapi._name(conn, x['from_account_id'])} → {readapi._name(conn, x['to_account_id'])})",
                touches=[("transfer", tid)],
                effects=["both accounts' balances move back; realised profit is unchanged; kept in the history"])
    plan.apply = lambda c: services.void_transfer(c, tid, _reason(p))
    return plan


# ---------------------------------------------------------------- reconciliations and balances

def op_update_reconciliation(conn, p: dict) -> Plan:
    rid = _int(p, "reconciliation_id")
    r = services.row(conn, "reconciliations", rid)
    if not r:
        raise UserError(f"No balance check #{rid}")
    text = _str(p, "note", 500) or ""
    plan = Plan("update_reconciliation", "safe_edit", f"Update the note on balance check #{rid}",
                touches=[("reconciliation", rid)], noop=text == r["note"])
    plan.apply = lambda c: services.update_reconciliation_note(c, rid, text, _reason(p))
    return plan


def op_reverse_reconciliation(conn, p: dict) -> Plan:
    rid = _int(p, "reconciliation_id")
    r = services.row(conn, "reconciliations", rid)
    if not r:
        raise UserError(f"No balance check #{rid}")
    e = services.row(conn, "ledger_entries", r["entry_id"]) if r["entry_id"] else None
    active = bool(e and not e["superseded_at"])
    if r["outcome"] == "reversed" or (not active and r["outcome"] != "adjusted"):
        return Plan("reverse_reconciliation", "financial_reverse", f"Balance check #{rid} has nothing to reverse",
                    touches=[("reconciliation", rid)], noop=True)
    name = readapi._name(conn, r["account_id"])
    effects = ([f"P&L adjustment removed: {fmt(e['amount_pence'], signed=True)} (realised profit changes by "
                f"{fmt(-e['amount_pence'], signed=True)})", f"{name}'s balance in the app moves back by "
                f"{fmt(-e['amount_pence'], signed=True)}"] if active else
               ["its adjustment was already withdrawn: this only marks the balance check as reversed"])
    effects += ["transfers and settled bets are unchanged"]
    plan = Plan("reverse_reconciliation", "financial_reverse", f"Reverse balance check #{rid} on {name} "
                f"({fmt(r['difference_pence'], signed=True)})", touches=[("reconciliation", rid), ("account", r["account_id"])],
                effects=effects)
    plan.apply = lambda c: services.reverse_reconciliation(c, rid, _reason(p) or "reversed")
    return plan


def op_resolve_reconciliation(conn, p: dict) -> Plan:
    rid = _int(p, "reconciliation_id")
    r = services.row(conn, "reconciliations", rid)
    if not r:
        raise UserError(f"No balance check #{rid}")
    action = _str(p, "action", 20)
    if action not in ("record_adjustment", "dismiss"):
        raise UserError("action: record_adjustment or dismiss")
    if r["outcome"] != "unresolved":
        return Plan("resolve_reconciliation", "safe_edit", f"Balance check #{rid} is {r['outcome']}",
                    touches=[("reconciliation", rid)], noop=True)
    adjust = action == "record_adjustment"
    plan = Plan("resolve_reconciliation", "financial_reverse" if adjust else "safe_edit",
                f"Balance check #{rid}: " + (f"record {fmt(r['difference_pence'], signed=True)} as a P&L adjustment" if adjust
                                            else "close the difference without an adjustment"),
                touches=[("reconciliation", rid), ("account", r["account_id"])],
                effects=[f"realised profit changes by {fmt(r['difference_pence'], signed=True)}"] if adjust else
                ["profit unchanged"])
    plan.apply = lambda c: services.resolve_reconciliation(c, rid, "adjust" if adjust else "dismiss",
                                                           _reason(p) or ("unexplained difference" if adjust else "dismissed"))
    return plan


def op_record_balance(conn, p: dict) -> Plan:
    """A balance the user read off the bookmaker or exchange. Never posts profit/loss: a difference the app can't
    explain is recorded as unresolved and shown as an issue, for the user to decide."""
    acc = changes.find_account(conn, p.get("account"))
    observed = to_pence(str(p.get("observed") or p.get("balance") or ""), allow_negative=True)
    figure = _str(p, "figure", 12) or ""
    if acc["kind"] == "bank":
        raise UserError("Banks are funding sources and are not balance-checked")
    exp = services.expected_balance(conn, acc["id"], "total")
    if acc["kind"] == "exchange":
        if figure not in ("available", "total"):
            if exp["liability"]:
                raise UserError(f"figure: say whether {fmt(observed)} is the exchange's available balance or its total "
                                f"(it holds {fmt(exp['liability'])} of lay liability: available excludes it)")
            figure = "available"
    else:
        figure = "total"
    exp = services.expected_balance(conn, acc["id"], figure)
    diff = observed - exp["expected"]
    plan = Plan("record_balance", "safe_edit", f"{acc['name']} {figure} balance {fmt(observed)}: "
                + ("matches" if not diff else f"app expected {fmt(exp['expected'])}, difference {fmt(diff, signed=True)}"),
                touches=[("account", acc["id"])])
    if acc["kind"] == "exchange":
        plan.effects.append(f"app: total funds {fmt(exp['total'])} = available {fmt(exp['available'])} + lay liability "
                            f"{fmt(exp['liability'])}")
    if diff:
        plan.effects.append("recorded as UNRESOLVED: no profit/loss is posted. Check for missing deposits, "
                            "withdrawals, bets or settlements; the user can then record or dismiss the difference.")
    plan.apply = lambda c: services.reconcile(c, acc["id"], observed, note=_str(p, "note", 300) or "",
                                              at=_when(p, "observed_at"), basis=figure, adjust=False)
    return plan


# ---------------------------------------------------------------- offers

def op_update_offer(conn, p: dict) -> Plan:
    oid = _int(p, "offer_id")
    o = _need(conn, "offers", oid, "offer")
    data = {}
    for key in ("title", "requirement", "reward", "notes"):
        if p.get(key) is not None:
            data[key] = _str(p, key, 2000 if key == "notes" else 300)
    if p.get("status") not in (None, ""):
        if p["status"] not in services.OFFER_STATUSES:
            raise UserError("status: " + ", ".join(services.OFFER_STATUSES))
        if p["status"] not in services.OFFER_CLOSURES and o["status"] not in services.OFFER_CLOSURES \
                and p["status"] != o["status"]:
            raise UserError("status: an offer's status follows its records. Record the qualifying bet "
                            "(record_unhedged_qualifier / record_matched_bet / submit_position_candidate), or the "
                            "bookmaker's confirmation (update_offer_progress qualified=true), and the reward when it "
                            "arrives (mark_reward_received). Only complete, expired or abandoned are set directly.")
        data["status"] = p["status"]
    if p.get("expires_at") not in (None, ""):
        data["expires_at"] = _when(p, "expires_at")
    changed = {k: v for k, v in data.items() if o.get(k) != v}
    plan = Plan("update_offer", "safe_edit", f"Update offer #{oid} ({o['title']})", touches=[("offer", oid)], noop=not changed)
    plan.apply = lambda c: services.update_offer(c, oid, changed, note=_reason(p) or "correction")
    return plan


def op_void_offer(conn, p: dict) -> Plan:
    oid = _int(p, "offer_id")
    o = _need(conn, "offers", oid, "offer")
    plan = Plan("void_offer", "safe_edit", f"Close offer #{oid} ({o['title']}) as abandoned", touches=[("offer", oid)],
                noop=o["status"] == "abandoned", effects=["its positions and free bets are unchanged"])
    plan.apply = lambda c: services.set_offer_status(c, oid, "abandoned", note=_reason(p) or "abandoned")
    return plan


def op_delete_offer(conn, p: dict) -> Plan:
    oid = _int(p, "offer_id")
    o = services.row(conn, "offers", oid)
    if not o:
        raise UserError(f"No offer #{oid}")
    if o["deleted_at"]:
        return Plan("delete_offer", "destructive_delete", f"Offer #{oid} is already removed", noop=True)
    deps = services.offer_dependents(conn, oid)
    if any(deps.values()):
        return Plan("delete_offer", "destructive_delete", "", dependencies=[
            f"{k.replace('_', ' ')}: " + ", ".join(f"#{i}" for i in v) for k, v in deps.items() if v],
            blocked="Records refer to this offer; close it with void_offer instead")
    plan = Plan("delete_offer", "destructive_delete", f"Delete offer #{oid} ({o['title']})", touches=[("offer", oid)],
                effects=["removed from every list; kept in the audit history"])
    plan.apply = lambda c: services.delete_offer(c, oid, _reason(p))
    return plan


OPS = {
    "update_free_bet": op_update_free_bet, "link_free_bet_to_position": op_link_free_bet_to_position,
    "unlink_free_bet_from_position": op_unlink_free_bet_from_position, "void_free_bet": op_void_free_bet,
    "delete_free_bet": op_delete_free_bet, "merge_duplicate_free_bets": op_merge_duplicate_free_bets,
    "update_position": op_update_position, "update_leg": op_update_leg, "settle_position": op_settle_position,
    "reopen_position": op_reopen_position, "void_position": op_void_position, "delete_position": op_delete_position,
    "update_transfer": op_update_transfer, "reclassify_transfer": op_reclassify_transfer,
    "reverse_transfer": op_reverse_transfer, "update_reconciliation": op_update_reconciliation,
    "reverse_reconciliation": op_reverse_reconciliation, "resolve_reconciliation": op_resolve_reconciliation,
    "record_balance": op_record_balance, "update_offer": op_update_offer, "void_offer": op_void_offer,
    "delete_offer": op_delete_offer,
}
TITLES = {k: k.replace("_", " ").capitalize() for k in OPS}


# ---------------------------------------------------------------- running a change

def plan_for(conn, name: str, params: dict, *, require_reason: bool = True) -> Plan:
    """Validate a change. A reason is required to APPLY a financial change or a deletion; previews don't need one
    (in the web app it is typed on the confirm form)."""
    if name not in OPS:
        raise UserError(f"Unknown change '{name}'")
    if not isinstance(params, dict):
        raise UserError("Send the change's details as an object")
    plan = OPS[name](conn, params)
    if require_reason and not plan.blocked and not plan.noop and plan.level in REASON_REQUIRED and not _reason(params):
        plan.blocked = "reason: say why (in the user's words) — required for financial changes and deletions"
    return plan


def totals(conn) -> dict:
    s = metrics.summary(conn)
    stc = outlook.still_to_come(conn)
    return {"realised_profit": plain(s.profit_lifetime), "still_to_come": plain(stc["total"]),
            "temporary_funding_outstanding": plain(s.temporary_outstanding), "betting_balance": plain(s.betting_balance),
            "free_bets_held": f"{s.free_bets_count} x {plain(s.free_bets_value)}", "issues": len(issues.collect(conn, s))}


def _snapshots(conn, touches) -> list[dict]:
    return [{"record": f"{k} #{i}", "state": readapi.snapshot(conn, k, i)} for k, i in touches]


def _changed_totals(before: dict, after: dict) -> dict:
    out = {k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]}
    out["issues"] = {"before": before["issues"], "after": after["issues"]}
    return out


def _describe(plan: Plan) -> dict:
    return {"change": plan.op, "level": plan.level, "summary": plan.summary, "effects": plan.effects,
            "warnings": plan.warnings, "dependencies": plan.dependencies}


def execute(conn, plan: Plan, *, actor: str, request_id: str | None = None) -> dict:
    """Apply a plan inside the caller's transaction and report before → after. All or nothing: if a service refuses
    part-way, everything this change did is rolled back and the refusal is raised as UserError."""
    from . import plans as _plans
    t0, before, n0 = totals(conn), _snapshots(conn, plan.touches), numbers(conn)
    conn.execute("SAVEPOINT apply_change")
    try:
        with services.acting(actor, request_id):
            returned = plan.apply(conn)
            from . import offer_state
            offer_state.sync_all(conn)                  # offers always follow what was just recorded
            services.audit(conn, "change", None, plan.op, {"records": [r["record"] for r in before]},
                           {"summary": plan.summary, "level": plan.level}, plan.summary)
    except _plans.Conflict:
        conn.execute("ROLLBACK TO SAVEPOINT apply_change")
        conn.execute("RELEASE SAVEPOINT apply_change")
        raise
    except (ValueError, sqlite3.IntegrityError) as exc:
        conn.execute("ROLLBACK TO SAVEPOINT apply_change")
        conn.execute("RELEASE SAVEPOINT apply_change")
        raise UserError(str(exc) or "That change could not be applied") from None
    conn.execute("RELEASE SAVEPOINT apply_change")
    after = _snapshots(conn, plan.touches + list(getattr(plan, "created", [])))
    before += [{"record": f"{k} #{i}", "state": None} for k, i in getattr(plan, "created", [])]
    out = {**_describe(plan), "status": "applied",
           "records": [{"record": b["record"], "before": b["state"], "after": a["state"]} for b, a in zip(before, after)],
           "totals": _changed_totals(t0, totals(conn))}
    if isinstance(returned, dict):
        out["result"] = returned
    n1 = numbers(conn)
    out["integrity"] = integrity(conn, n0, n1)
    out["anomalies"] = anomalies(conn, plan.op, n0, n1)
    return out


def numbers(conn) -> dict:
    s = metrics.summary(conn)
    return {"profit": s.profit_lifetime, "balance": s.betting_balance, "free_bets": s.free_bets_count,
            "temporary": s.temporary_outstanding, "liability": s.lay_liability, "consistent": s.consistent,
            "transfers": conn.execute("SELECT count(*) FROM transfers WHERE voided_at IS NULL").fetchone()[0],
            "orphans": conn.execute("SELECT count(*) FROM free_bets f WHERE f.status = 'used' AND f.deleted_at IS NULL AND "
                                    "NOT EXISTS (SELECT 1 FROM legs l WHERE l.free_bet_id = f.id AND l.deleted_at IS NULL "
                                    "AND l.result <> 'void')").fetchone()[0],
            "double_tokens": conn.execute("SELECT count(*) FROM (SELECT free_bet_id FROM legs WHERE deleted_at IS NULL AND "
                                          "free_bet_id IS NOT NULL AND result <> 'void' GROUP BY free_bet_id HAVING "
                                          "count(*) > 1)").fetchone()[0],
            "temp_raw": conn.execute(
                "SELECT coalesce(sum(CASE WHEN capital_type = 'temporary_funding' THEN amount_pence END), 0) - "
                f"coalesce(sum(CASE WHEN {services.REPAID_SQL} THEN temp_portion_pence END), 0) FROM transfers "
                "WHERE voided_at IS NULL").fetchone()[0]}


def integrity(conn, n0: dict, n1: dict) -> dict:
    """Checks run after every change: the ledger still adds up and nothing was left half-linked."""
    warnings = []
    if n0["consistent"] and not n1["consistent"]:
        warnings.append("the ledger checks no longer pass after this change (see get_issues)")
    if n1["orphans"] > n0["orphans"]:
        warnings.append("a free bet is now marked used without a bet")
    if n1["double_tokens"] > n0["double_tokens"]:
        warnings.append("a free bet is now used by more than one bet")
    if n1["temp_raw"] < 0:
        warnings.append("more temporary funding was returned than was borrowed")
    return {"ok": not warnings, "warnings": warnings}


def anomaly_thresholds(conn) -> dict:
    saved = services.get_setting(conn, ANOMALY_SETTING, {}) or {}
    return {k: int(saved.get(k, v)) for k, v in DEFAULT_ANOMALY.items()}


def anomalies(conn, op: str, n0: dict, n1: dict) -> list[str]:
    """Unexpectedly large effects of one change: not blocked, but called out for the user to review."""
    th, out = anomaly_thresholds(conn), []
    if abs(n1["profit"] - n0["profit"]) > th["profit_change"]:
        out.append(f"realised profit moved by {fmt(n1['profit'] - n0['profit'], signed=True)} in one change: review it")
    if n1["transfers"] == n0["transfers"] and abs(n1["balance"] - n0["balance"]) > th["balance_change_without_transfer"]:
        out.append(f"betting balances moved by {fmt(n1['balance'] - n0['balance'], signed=True)} without a transfer")
    if abs(n1["free_bets"] - n0["free_bets"]) >= th["free_bets_change"]:
        out.append(f"free bets held changed by {n1['free_bets'] - n0['free_bets']} in one change")
    if abs(n1["liability"] - n0["liability"]) > th["liability_change"]:
        out.append(f"exchange liability moved by {fmt(n1['liability'] - n0['liability'], signed=True)}")
    return out


def preview(conn, plan: Plan) -> dict:
    """Exactly what the change would do: applied in a savepoint, measured, rolled back."""
    if plan.blocked:
        return {**_describe(plan), "status": "blocked", "reason": plan.blocked}
    if plan.noop:
        return {**_describe(plan), "status": "already_done", "records": [{"record": r["record"], "state": r["state"]}
                                                                         for r in _snapshots(conn, plan.touches)]}
    conn.execute("SAVEPOINT preview_change")
    try:
        result = execute(conn, plan, actor="preview")
    except UserError as exc:
        return {**_describe(plan), "status": "blocked", "reason": str(exc)}
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT preview_change")
        conn.execute("RELEASE SAVEPOINT preview_change")
    return {**result, "status": "preview"}


def run(conn, name: str, params: dict, *, actor: str = "user", request_id: str | None = None) -> dict:
    """For the web app: validate, then apply (or report blocked / already done)."""
    plan = plan_for(conn, name, params)
    if plan.blocked:
        return {**_describe(plan), "status": "blocked", "reason": plan.blocked}
    if plan.noop:
        return {**_describe(plan), "status": "already_done"}
    result = execute(conn, plan, actor=actor, request_id=request_id)
    reconcile_waiting(conn)
    return result


# ---------------------------------------------------------------- assistant requests (idempotent, permission-aware)

def _public_id(conn) -> str:
    while True:
        pid = secrets.token_hex(8)
        if not conn.execute("SELECT 1 FROM assistant_ops WHERE public_id = ?", (pid,)).fetchone():
            return pid


def _store(conn, *, key, name, params, plan: Plan, status, result=None, submitted_by=None, hold_reason="") -> dict:
    public_id = _public_id(conn)
    conn.execute("INSERT INTO assistant_ops (public_id, request_key, op, params_json, level, status, summary, reason, "
                 "hold_reason, result_json, submitted_by, created_at, decided_at, decided_by) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (public_id, key, name, json.dumps(params, sort_keys=True), plan.level, status, plan.summary[:300],
                  _reason(params), hold_reason, json.dumps(result) if result is not None else None, submitted_by,
                  times.now_iso(), None if status == "held" else times.now_iso(),
                  None if status == "held" else "assistant-auto"))
    return {"change_id": public_id}


def assistant_request(conn, name: str, params: dict, *, token_id: str, request_id: str, dry_run: bool = False) -> tuple[int, dict]:
    """The Assistant API entry point. Returns (http status, body)."""
    if name not in OPS:
        return 404, {"status": "rejected", "errors": [f"Unknown change '{name}'"]}
    if not dry_run and not (isinstance(request_id, str) and 1 <= len(request_id) <= 100):
        return 400, {"status": "rejected", "errors": ["request_id: required (reuse the same one when retrying)"]}
    key = f"{token_id}:{request_id}" if request_id else None
    if key and not dry_run:
        old = conn.execute("SELECT * FROM assistant_ops WHERE request_key = ?", (key,)).fetchone()
        if old:
            if old["op"] != name or json.loads(old["params_json"]) != params:
                return 409, {"status": "rejected", "errors": ["request_id was already used for a different change"]}
            return 200, {**stored_response(dict(old)), "duplicate": True,
                         "note": "Already received (retry detected): nothing was applied twice."}
    try:
        plan = plan_for(conn, name, params, require_reason=not dry_run)
    except ValueError as exc:                        # UserError, a lookup problem or a bad amount
        return 400, {"status": "rejected", "errors": [str(exc)]}
    if dry_run:
        out = preview(conn, plan)
        if out["status"] == "preview":
            out["would_need_confirmation"] = permissions(conn).get(plan.level) != "auto"
        return 200, {**out, "dry_run": True, "note": "Preview only: nothing was changed."}
    if plan.blocked:
        body = {**_describe(plan), "status": "blocked", "reason": plan.blocked}
        _store(conn, key=key, name=name, params=params, plan=plan, status="blocked", result=body,
               submitted_by=f"assistant:{token_id}")
        return 200, body
    if plan.noop:
        body = {**_describe(plan), "status": "already_done",
                "records": [{"record": r["record"], "state": r["state"]} for r in _snapshots(conn, plan.touches)]}
        _store(conn, key=key, name=name, params=params, plan=plan, status="already_done", result=body,
               submitted_by=f"assistant:{token_id}")
        return 200, body
    if permissions(conn).get(plan.level) != "auto":
        prev = preview(conn, plan)
        hold = f"{LEVEL_TITLES[plan.level]} wait for your confirmation (Settings → Assistant permissions)"
        ref = _store(conn, key=key, name=name, params=params, plan=plan, status="held", result=prev,
                     submitted_by=f"assistant:{token_id}", hold_reason=hold)
        services.audit(conn, "change", None, "held", None, {"change": name, "change_id": ref["change_id"],
                                                             "level": plan.level}, plan.summary)
        return 202, {**prev, "status": "needs_confirmation", "change_id": ref["change_id"], "held_because": hold}
    from .plans import Conflict
    try:
        result = execute(conn, plan, actor=f"assistant:{token_id}", request_id=request_id)
    except Conflict as exc:
        return 409, {"status": "conflict", "errors": [str(exc)],
                     "note": "Someone changed this record meanwhile: read it again, then retry with a new request_id"}
    except UserError as exc:
        return 400, {"status": "rejected", "errors": [str(exc)]}
    reconcile_waiting(conn)
    ref = _store(conn, key=key, name=name, params=params, plan=plan, status="applied", result=result,
                 submitted_by=f"assistant:{token_id}")
    return 200, {**result, "change_id": ref["change_id"]}


def stored_response(row: dict) -> dict:
    body = json.loads(row["result_json"]) if row["result_json"] else {}
    status = {"held": "needs_confirmation"}.get(row["status"], row["status"])
    out = {**body, "status": status, "change_id": row["public_id"], "change": row["op"], "summary": row["summary"]}
    if row["status"] == "held":
        out["held_because"] = row["hold_reason"]
    if row["decided_by"] and row["status"] != "held":
        out["decided_by"] = row["decided_by"]
    return out


def held(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM assistant_ops WHERE status = 'held' ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["result"] = json.loads(r["result_json"]) if r["result_json"] else {}
        out.append(d)
    return out


def reconcile_held(conn) -> list[str]:
    """A held change that would now do nothing (its effect is already in the records, e.g. the same reversal was
    confirmed from Imports) stops waiting: status already_done, decided_by 'system'. Re-checked with the change's own
    plan, so it is never applied twice; a change that now errors or is blocked stays held for the user."""
    done = []
    for r in conn.execute("SELECT * FROM assistant_ops WHERE status = 'held' ORDER BY id").fetchall():
        try:
            plan = plan_for(conn, r["op"], json.loads(r["params_json"]))
        except (UserError, ValueError, KeyError, TypeError):
            continue
        if not plan.noop or plan.blocked:
            continue
        body = {**_describe(plan), "status": "already_done",
                "note": "Nothing to do: the records were already in this state, so it no longer needs you."}
        cur = conn.execute("UPDATE assistant_ops SET status = 'already_done', result_json = ?, decided_at = ?, "
                           "decided_by = 'system' WHERE id = ? AND status = 'held'",
                           (json.dumps(body), times.now_iso(), r["id"]))
        if cur.rowcount:
            services.audit(conn, "change", None, "already_done", None, {"change": r["op"], "change_id": r["public_id"]},
                           plan.summary)
            done.append(r["public_id"])
    return done


def reconcile_waiting(conn) -> dict:
    """Both queues of things waiting for the user (Imports candidates and held changes) drop what is already done."""
    from . import imports
    return {"imports": imports.reconcile_pending(conn), "changes": reconcile_held(conn)}


def decide(conn, public_id: str, confirm: bool) -> dict:
    """The user confirms or rejects a held assistant change. Re-checked against the records as they are now."""
    r = conn.execute("SELECT * FROM assistant_ops WHERE public_id = ?", (public_id,)).fetchone()
    if not r:
        raise UserError("No such change")
    if r["status"] != "held":
        raise UserError(f"That change was already {r['status'].replace('_', ' ')}")
    now = times.now_iso()
    if not confirm:
        conn.execute("UPDATE assistant_ops SET status = 'rejected', decided_at = ?, decided_by = 'user' WHERE id = ?",
                     (now, r["id"]))
        services.audit(conn, "change", None, "rejected", None, {"change": r["op"], "change_id": public_id}, r["summary"])
        return {"status": "rejected"}
    params = json.loads(r["params_json"])
    plan = plan_for(conn, r["op"], params)
    if plan.blocked or plan.noop:
        status = "blocked" if plan.blocked else "already_done"
        body = {**_describe(plan), "status": status, "reason": plan.blocked}
        conn.execute("UPDATE assistant_ops SET status = ?, result_json = ?, decided_at = ?, decided_by = 'user' WHERE id = ?",
                     (status, json.dumps(body), now, r["id"]))
        return body
    request_id = r["request_key"].split(":", 1)[1] if r["request_key"] else None
    result = execute(conn, plan, actor="assistant+user", request_id=request_id)
    conn.execute("UPDATE assistant_ops SET status = 'applied', result_json = ?, decided_at = ?, decided_by = 'user' "
                 "WHERE id = ?", (json.dumps(result), now, r["id"]))
    reconcile_waiting(conn)
    return result


from . import ops_operations  # noqa: E402,F401  (registers the planning / placement / reward changes)
