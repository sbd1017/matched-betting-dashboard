"""Undo the last thing you did, for a short while afterwards.

After an undoable action the page shows the usual message with an Undo button. The button posts a small token
("settle:12", "transfer:5", ...) to /undo. Nothing is deleted: undoing reopens, voids or reverses through the
same services, and both the action and its undo stay in the audit trail.

An undo is refused if it is older than WINDOW, or if the records changed since (for example a free bet created
by mistake has already been used), so it can never undo more than the one action it was offered for.
"""
from __future__ import annotations

import re
from datetime import timedelta

from . import services, times
from .services import UserError

WINDOW = timedelta(minutes=30)
TOKEN = re.compile(r"^(settle|leg|freebets|reward|transfer|entry|recs):([0-9,]+)(?::([0-9,]+))?$")


def _recent(conn, entity: str, entity_id: int, actions: tuple) -> bool:
    cut = times.iso(times.now() - WINDOW)
    r = conn.execute("SELECT at, action FROM audit_log WHERE entity = ? AND entity_id = ? ORDER BY id DESC LIMIT 1",
                     (entity, entity_id)).fetchone()
    return bool(r) and r["action"] in actions and r["at"] >= cut


def _ids(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x]


def label(token: str) -> str:
    """Short description for the Undo button's title."""
    kind = token.split(":", 1)[0]
    return {"settle": "Reopen the bets you just settled", "leg": "Reopen the bet you just settled",
            "freebets": "Remove the free bets you just added", "reward": "Remove the reward you just recorded",
            "transfer": "Void the transfer you just recorded", "entry": "Reverse the entry you just added",
            "recs": "Reverse the balance adjustments you just recorded"}.get(kind, "Undo")


def undo(conn, token: str) -> tuple[str, str]:
    """Apply an undo. Returns (message, page to go back to)."""
    m = TOKEN.match(token or "")
    if not m:
        raise UserError("That can’t be undone")
    kind, first, second = m.group(1), m.group(2), m.group(3)
    note = "undone"

    if kind in ("settle", "leg"):
        if kind == "settle":
            pid = int(first)
            legs = [dict(r) for r in conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL "
                                                  "AND result <> 'pending'", (pid,))]
        else:
            leg = services.row(conn, "legs", int(first))
            legs = [leg] if leg and not leg["deleted_at"] and leg["result"] != "pending" else []
            pid = leg["position_id"] if leg else 0
        if not legs or not all(_recent(conn, "leg", l["id"], ("settled",)) for l in legs):
            raise UserError("Too late to undo: reopen the bets on the position page instead")
        for l in legs:
            services.settle_leg(conn, l["id"], "pending", note=note)
        return f"Reopened {len(legs)} bet{'s' if len(legs) > 1 else ''}", f"/positions/{pid}"

    if kind in ("freebets", "reward"):
        ids = _ids(second if kind == "reward" else first)
        for fid in ids:
            fb = services.row(conn, "free_bets", fid)
            if not fb or fb["deleted_at"] or fb["status"] != "available" or services.token_in_use(conn, fid) \
                    or not _recent(conn, "free_bet", fid, ("credited",)):
                raise UserError("Too late to undo: a free bet has been used or changed since")
        for fid in ids:
            services.remove_free_bet(conn, fid, note)
        if kind == "reward":
            offer = services.row(conn, "offers", int(first))
            if offer and offer["reward_credited"]:
                conn.execute("UPDATE offers SET reward_credited = 0 WHERE id = ?", (offer["id"],))
            from . import offer_state
            offer_state.sync(conn, int(first), note=note)
            return "Reward removed: the offer is waiting for it again", f"/offers/{int(first)}"
        return f"Removed {len(ids)} free bet{'s' if len(ids) > 1 else ''}", "/free-bets"

    if kind == "transfer":
        tid = int(first)
        if not _recent(conn, "transfer", tid, ("created", "due_date")):
            raise UserError("Too late to undo: void the transfer from the ledger instead")
        services.void_transfer(conn, tid, note)
        return "Transfer voided", "/accounts"

    ids = _ids(first)                                   # entry / recs: manual or reconciliation entries
    for eid in ids:
        e = services.row(conn, "ledger_entries", eid)
        if not e or e["superseded_at"] or e["created_at"] < times.iso(times.now() - WINDOW):
            raise UserError("Too late to undo: reverse the entry from the ledger instead")
    for eid in ids:
        services.reverse_entry(conn, eid, note)
    return (f"Reversed {len(ids)} entr{'ies' if len(ids) > 1 else 'y'}", "/accounts" if kind == "recs" else "/ledger")
