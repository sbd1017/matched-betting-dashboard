"""Expected rewards and the offer lifecycle.

Expected reward: what an offer should pay once its qualifier has settled (e.g. "£10 SNR Bet Builder free bet within
24 hours"). It is created automatically when a qualifying position with reward terms settles, or by hand. It is not
money and never counts in profit or free bets held. When the free bets arrive they are matched to it (reconcile), so
the same reward reported twice is never recorded twice, and a missing, short or wrong reward shows as an issue.

Offer lifecycle: the stage of an offer is worked out from what is recorded (status, opt-in and deposit facts, plans,
positions, expected rewards, free bets), so it can't drift out of step with the records:
  discovered / reviewed -> opted_in -> deposit_required -> qualifier_required -> qualifier_planned -> qualifier_placed
  -> qualifier_settled -> reward_expected / reward_overdue -> reward_credited -> reward_partially_used -> reward_used
  -> completed;  failed / expired / void are ends.
"""
from __future__ import annotations

from datetime import timedelta

from . import services, times
from .money import fmt
from .services import UserError

DEFAULT_DELAY_HOURS = 24
STAGES = ("discovered", "reviewed", "opted_in", "deposit_required", "deposit_complete", "qualifier_required",
          "qualifier_planned", "qualifier_placed", "qualifier_settled", "reward_expected", "reward_overdue",
          "reward_credited", "reward_partially_used", "reward_used", "completed", "failed", "expired", "void")


# ---------------------------------------------------------------- expected rewards

def expect(conn, *, offer_id=None, account_id=None, kind=None, count=None, each=None, expected_by=None,
           expiry_days=None, source_position_id=None, notes: str = "", sync: bool = True) -> tuple[int, bool]:
    """Create an expected reward (or return the existing one for the same offer and qualifier). -> (id, created)."""
    offer = services.row(conn, "offers", int(offer_id)) if offer_id else None
    if offer_id and (not offer or offer["deleted_at"]):
        raise UserError(f"No offer #{offer_id}")
    kind = kind or (offer["reward_kind"] if offer else None)
    each = int(each or (offer["reward_each_pence"] if offer else 0) or 0)
    count = int(count or (offer["reward_count"] if offer else 1) or 1)
    account_id = int(account_id or (offer["account_id"] if offer else 0) or 0)
    if kind not in ("snr", "sr", "cash") or each <= 0 or not account_id:
        raise UserError("Give the reward: bookmaker, type (snr / sr / cash), value each and how many")
    existing = conn.execute("SELECT id FROM expected_rewards WHERE status <> 'void' AND offer_id = ? AND kind = ? AND "
                            "each_pence = ?", (int(offer_id), kind, each)).fetchone() if offer_id else \
        conn.execute("SELECT id FROM expected_rewards WHERE status <> 'void' AND offer_id IS NULL AND "
                     "coalesce(source_position_id, 0) = ? AND kind = ? AND each_pence = ? AND account_id = ?",
                     (int(source_position_id or 0), kind, each, account_id)).fetchone()
    if existing:
        return existing[0], False
    now = times.now_iso()
    cur = conn.execute("INSERT INTO expected_rewards (offer_id, account_id, source_position_id, kind, count, each_pence, "
                       "expected_by, expiry_days, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (int(offer_id) if offer_id else None, account_id, source_position_id, kind, count, each,
                        expected_by, int(expiry_days) if expiry_days else (offer["reward_expiry_days"] if offer else None),
                        notes.strip()[:500], now, now))
    rid = cur.lastrowid
    services.audit(conn, "expected_reward", rid, "expected", None, {"offer_id": offer_id, "kind": kind, "count": count,
                                                                    "each_pence": each, "expected_by": expected_by,
                                                                    "source_position_id": source_position_id})
    if offer and sync:
        from . import offer_state
        offer_state.sync(conn, offer["id"], note=f"reward #{rid} expected")
    return rid, True


def on_position_status(conn, position_id: int) -> None:
    """A qualifying position changed: its offer's lifecycle (which expects the reward once it is due) follows."""
    pos = services.row(conn, "positions", position_id)
    if pos and pos["offer_id"]:
        from . import offer_state
        offer_state.sync(conn, pos["offer_id"])


def linked_tokens(conn, rid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM free_bets WHERE expected_reward_id = ? AND deleted_at IS NULL",
                                          (rid,))]


def _refresh(conn, rid: int) -> str:
    r = services.row(conn, "expected_rewards", rid)
    if r["status"] == "void":
        return "void"
    got = len([t for t in linked_tokens(conn, rid) if t["status"] != "void"]) if r["kind"] != "cash" else (
        r["count"] if r["received_at"] else 0)
    status = "received" if got >= r["count"] else ("partially_received" if got else "waiting")
    if status != r["status"]:
        conn.execute("UPDATE expected_rewards SET status = ?, received_at = CASE WHEN ? = 'waiting' THEN NULL ELSE "
                     "coalesce(received_at, ?) END, updated_at = ? WHERE id = ?",
                     (status, status, times.now_iso(), times.now_iso(), rid))
        services.audit(conn, "expected_reward", rid, "status", {"status": r["status"]}, {"status": status})
    return status


def candidates(conn, r: dict) -> list[dict]:
    """Free bets that could be this reward: same bookmaker, type and value, not linked to another reward, not void,
    credited no earlier than a day before the reward was expected to be due."""
    since = times.iso(times.parse(r["created_at"]) - timedelta(days=2))
    return [dict(t) for t in conn.execute(
        "SELECT * FROM free_bets WHERE deleted_at IS NULL AND status <> 'void' AND expected_reward_id IS NULL AND "
        "account_id = ? AND kind = ? AND face_value_pence = ? AND coalesce(credited_at, created_at) >= ? AND "
        "(offer_id IS NULL OR offer_id IS ?) ORDER BY coalesce(credited_at, created_at), id",
        (r["account_id"], r["kind"], r["each_pence"], since, r["offer_id"]))]


def reconcile(conn, *, apply: bool = False, reward_id: int | None = None) -> dict:
    """Match waiting expected rewards to the free bets that arrived. A match is applied only when unambiguous: the
    candidate tokens are exactly the ones still missing (or fewer), and no other waiting reward wants them."""
    waiting = [dict(r) for r in conn.execute("SELECT * FROM expected_rewards WHERE status IN ('waiting', "
                                             "'partially_received') AND kind <> 'cash'" + (" AND id = ?" if reward_id else "")
                                             + " ORDER BY id", (reward_id,) if reward_id else ())]
    wanted: dict[int, list[int]] = {}
    for r in waiting:
        for t in candidates(conn, r):
            wanted.setdefault(t["id"], []).append(r["id"])
    out = []
    for r in waiting:
        need = r["count"] - len([t for t in linked_tokens(conn, r["id"]) if t["status"] != "void"])
        cands = candidates(conn, r)
        mine = [t for t in cands if wanted.get(t["id"]) == [r["id"]]]
        item = {"expected_reward_id": r["id"], "offer_id": r["offer_id"], "missing": need,
                "candidates": [t["id"] for t in cands]}
        if not cands:
            item["result"] = "nothing_arrived_yet"
        elif len(mine) < len(cands) and len(cands) > need:
            item["result"] = "ambiguous"
            item["detail"] = "more matching free bets than the reward (or another reward wants them): link by hand"
        else:
            take = mine[:need]
            item["result"] = "matched" if len(take) >= need else "partly_matched"
            item["link"] = [t["id"] for t in take]
            if apply and take:
                link(conn, r["id"], [t["id"] for t in take])
        out.append(item)
    return {"rewards": out, "applied": apply}


def link(conn, rid: int, free_bet_ids: list[int], sync: bool = True) -> list[int]:
    r = services.row(conn, "expected_rewards", rid)
    if not r or r["status"] == "void":
        raise UserError(f"No expected reward #{rid}")
    done = []
    for fid in free_bet_ids:
        fb = services.row(conn, "free_bets", int(fid))
        if not fb or fb["deleted_at"]:
            raise UserError(f"No free bet #{fid}")
        if fb["expected_reward_id"] == rid:
            continue
        if fb["expected_reward_id"]:
            raise UserError(f"Free bet #{fid} already belongs to expected reward #{fb['expected_reward_id']}")
        if (fb["account_id"], fb["kind"]) != (r["account_id"], r["kind"]):
            raise UserError(f"Free bet #{fid} is a different bookmaker or type")
        changes = {"expected_reward_id": rid}
        if r["offer_id"] and not fb["offer_id"]:
            changes["offer_id"] = r["offer_id"]
        conn.execute("UPDATE free_bets SET " + ", ".join(f"{k} = ?" for k in changes) + ", updated_at = ? WHERE id = ?",
                     (*changes.values(), times.now_iso(), fb["id"]))
        services.audit(conn, "free_bet", fb["id"], "reward_matched", {k: fb[k] for k in changes}, changes)
        done.append(fb["id"])
    _refresh(conn, rid)
    if r["offer_id"] and done and sync:
        from . import offer_state
        offer_state.sync(conn, r["offer_id"], note=f"expected reward #{rid} received")
    return done


def mark_received(conn, rid: int, *, free_bet_ids: list[int] | None = None, count: int | None = None,
                  expires_at: str | None = None, at: str | None = None, notes: str = "",
                  use_candidates: bool = True) -> dict:
    """The reward arrived. Links the free bets given, else free bets already recorded that match, and only creates
    the missing ones -- never a second copy of a reward."""
    r = services.row(conn, "expected_rewards", rid)
    if not r or r["status"] == "void":
        raise UserError(f"No expected reward #{rid}")
    have = [t for t in linked_tokens(conn, rid) if t["status"] != "void"]
    if r["kind"] == "cash":
        if r["received_at"]:
            return {"already": True, "linked": [], "created": []}
        services.add_manual_entry(conn, account_id=r["account_id"], amount=r["each_pence"] * r["count"],
                                  category="promotion_reward", at=at, description="Cash reward" + (f": {notes}" if notes else ""),
                                  offer_id=r["offer_id"])
        conn.execute("UPDATE expected_rewards SET received_at = ? WHERE id = ?", (at or times.now_iso(), rid))
        _refresh(conn, rid)
        return {"already": False, "linked": [], "created": [], "cash": r["each_pence"] * r["count"]}
    target = int(count or r["count"])
    if len(have) >= target and not free_bet_ids:
        return {"already": True, "linked": [t["id"] for t in have], "created": []}
    linked = link(conn, rid, free_bet_ids) if free_bet_ids else []
    have = [t for t in linked_tokens(conn, rid) if t["status"] != "void"]
    missing = target - len(have)
    if missing > 0 and use_candidates:
        cands = candidates(conn, r)[:missing]
        linked += link(conn, rid, [t["id"] for t in cands]) if cands else []
        missing -= len(cands)
    created = []
    if missing > 0:
        if not expires_at and r["expiry_days"]:
            expires_at = times.iso(times.parse(at or times.now_iso()) + timedelta(days=int(r["expiry_days"])))
        created = services.create_free_bets(conn, count=missing, account_id=r["account_id"], face_value=r["each_pence"],
                                            kind=r["kind"], offer_id=r["offer_id"], credited_at=at,
                                            expires_at=expires_at, notes=notes)
        link(conn, rid, created)
    if count and int(count) != r["count"]:
        services.audit(conn, "expected_reward", rid, "count_differs", {"count": r["count"]}, {"received": int(count)})
    return {"already": False, "linked": linked, "created": created}


def overdue(conn) -> list[dict]:
    now = times.now_iso()
    return [dict(r) for r in conn.execute("SELECT * FROM expected_rewards WHERE status IN ('waiting', 'partially_received') "
                                          "AND expected_by IS NOT NULL AND expected_by < ?", (now,))]


def describe(conn, r: dict) -> dict:
    from .readapi import _name, _offer_ref, m, t
    tokens = linked_tokens(conn, r["id"])
    return {"id": r["id"], "offer": _offer_ref(conn, r["offer_id"]), "bookmaker": _name(conn, r["account_id"]),
            "expected": f"{r['count']} x {fmt(r['each_pence'])} {r['kind'].upper()}", "type": r["kind"],
            "count": r["count"], "each": m(r["each_pence"]), "expected_by": t(r["expected_by"]),
            "overdue": bool(r["status"] in ("waiting", "partially_received") and r["expected_by"]
                            and r["expected_by"] < times.now_iso()),
            "status": r["status"], "source_position_id": r["source_position_id"],
            "free_bets": [{"id": x["id"], "value": m(x["face_value_pence"]), "status": x["status"],
                           "expires_at": t(x["expires_at"])} for x in tokens],
            "notes": r["notes"] or None, "counts_in_profit": False}


# ---------------------------------------------------------------- offer lifecycle

def lifecycle(conn, offer: dict) -> dict:
    """{status, stage, next_action, deadline, actor, ...} for an offer: the one lifecycle in offer_state."""
    from . import offer_state
    return offer_state.derive(conn, offer)
