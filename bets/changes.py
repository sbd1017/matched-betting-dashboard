"""Corrections an AI assistant (e.g. ChatGPT) can ask for: "amend" changes a recorded item, "reverse" undoes one.

Both are ordinary import candidates: the user sees before → after on the Imports page and confirms or rejects
them. They are never confirmed automatically, even in Trusted Assistant mode. On confirmation they call the same
services as the forms, so every change is audited and ledger entries are superseded, never edited or deleted.

What can be named (``target``) and how it is found (``ref``, plus a few helper fields):

  position   number, or the event (must match exactly one position)
  leg        a bet: its position (number or event) + side (back/lay), + account if the position has several
  offer      number or title
  free_bet   number, or bookmaker + value (exactly one unused free bet)
  transfer   number, or amount + an account at either end (exactly one in the last 90 days)
  entry      a manual ledger entry's number (reverse only)
  account    name (amend: rename only; standing has its own type, account_status)
"""
from __future__ import annotations

from datetime import timedelta

from . import services, times
from .money import fmt, odds_text, parse_commission, parse_odds, plain

AMEND_TARGETS = ("position", "leg", "offer", "free_bet", "transfer", "account")
REVERSE_TARGETS = ("transfer", "entry", "leg", "position", "free_bet", "settlement")
AMEND_FIELDS = {
    "position": ("event", "selection", "market", "event_at", "offer"),
    "leg": ("stake", "odds", "commission", "placed_at", "reference", "move_to"),
    "offer": ("title", "status", "expires_at", "requirement", "reward"),
    "free_bet": ("expires_at", "status", "offer"),
    "transfer": ("at", "return_by"),
    "account": ("name",),
}


class Problem(ValueError):
    pass


def _im():
    from . import imports
    return imports


def _one(rows: list, what: str, hint: str) -> dict:
    if len(rows) == 1:
        return dict(rows[0])
    raise Problem(f"ref: {'no ' + what + ' matches' if not rows else f'{len(rows)} {what}s match'} ({hint})")


def find_position(conn, ref) -> dict:
    ref = str(ref or "").strip()
    if not ref:
        raise Problem("ref: give the position number or the event")
    if ref.lstrip("#").isdigit():
        p = services.row(conn, "positions", int(ref.lstrip("#")))
        if not p or p["deleted_at"]:
            raise Problem(f"ref: no position #{ref}")
        return p
    rows = conn.execute("SELECT * FROM positions WHERE deleted_at IS NULL AND event = ? COLLATE NOCASE "
                        "ORDER BY id DESC", (ref,)).fetchall()
    open_rows = [r for r in rows if r["status"] == "open"]
    return _one(open_rows if len(open_rows) == 1 else rows, "position", "give the position number")


def find_leg(conn, raw: dict) -> dict:
    pos = find_position(conn, raw.get("ref") or raw.get("position"))
    side = str(raw.get("side") or "").lower()
    if side not in ("back", "lay"):
        raise Problem("side: back or lay (which bet in the position)")
    q = ("SELECT l.*, a.name AS account FROM legs l JOIN accounts a ON a.id = l.account_id WHERE l.position_id = ? "
         "AND l.deleted_at IS NULL AND l.side = ?")
    args = [pos["id"], side]
    if raw.get("account"):
        q += " AND a.name = ? COLLATE NOCASE"
        args.append(str(raw["account"]).strip())
    leg = _one(conn.execute(q, args).fetchall(), f"{side} bet", "give the account too")
    leg["event"] = pos["event"]
    return leg


def find_account(conn, ref) -> dict:
    found = _im()._account_named(conn, ref)
    if not found:
        raise Problem(f"ref: no account '{str(ref)[:60]}'")
    return found


def resolve(target: str, item: dict) -> dict:
    """How to find the same record again when the candidate is confirmed later (stable ids, not names)."""
    if target == "leg":
        return {"ref": item["position_id"], "side": item["side"], "account": item["account"]}
    if target == "account":
        return {"ref": item["name"]}
    return {"ref": item["id"]}


def find_offer(conn, ref) -> dict:
    found = _im()._offer(conn, ref)
    if not found:
        raise Problem(f"ref: no offer '{str(ref)[:80]}'")
    return found


def find_free_bet(conn, raw: dict) -> dict:
    ref = str(raw.get("ref") or "").strip()
    if ref.lstrip("#").isdigit():
        fb = services.row(conn, "free_bets", int(ref.lstrip("#")))
        if not fb or fb["deleted_at"]:
            raise Problem(f"ref: no free bet #{ref}")
        return fb
    errors: list = []
    value = _im()._money(raw.get("value"), "value", errors, required=False)
    if errors or not raw.get("account") or not value:
        raise Problem("ref: give the free bet number, or the bookmaker (account) and its value")
    rows = conn.execute("SELECT f.* FROM free_bets f JOIN accounts a ON a.id = f.account_id WHERE f.deleted_at IS NULL "
                        "AND f.status = 'available' AND a.name = ? COLLATE NOCASE AND f.face_value_pence = ?",
                        (str(raw["account"]).strip(), value)).fetchall()
    return _one(rows, "unused free bet", "give its number")


def find_transfer(conn, raw: dict) -> dict:
    ref = str(raw.get("ref") or "").strip()
    if ref.lstrip("#").isdigit():
        t = services.row(conn, "transfers", int(ref.lstrip("#")))
        if not t or t["voided_at"]:
            raise Problem(f"ref: no transfer #{ref}")
        return t
    errors: list = []
    amount = _im()._money(raw.get("amount"), "amount", errors, required=False)
    if errors or not amount or not raw.get("account"):
        raise Problem("ref: give the transfer number, or its amount and an account at either end")
    since = times.iso(times.now() - timedelta(days=90))
    rows = conn.execute("SELECT t.* FROM transfers t JOIN accounts a ON a.id IN (t.from_account_id, t.to_account_id) "
                        "WHERE t.voided_at IS NULL AND t.amount_pence = ? AND a.name = ? COLLATE NOCASE AND t.at >= ?",
                        (amount, str(raw["account"]).strip(), since)).fetchall()
    return _one(rows, "transfer", "give its number")


def _label(conn, target: str, item: dict) -> str:
    if target == "position":
        return f"position #{item['id']} {item['event']}"
    if target == "leg":
        return f"{item['side']} bet at {item['account']} in {item['event']}"
    if target == "offer":
        return f"offer '{item['title']}'"
    if target == "free_bet":
        acc = services.row(conn, "accounts", item["account_id"])["name"]
        return f"{acc} {fmt(item['face_value_pence'])} free bet #{item['id']}"
    if target == "transfer":
        a = services.row(conn, "accounts", item["from_account_id"])["name"]
        b = services.row(conn, "accounts", item["to_account_id"])["name"]
        return f"transfer #{item['id']} {a} → {b} {fmt(item['amount_pence'])}"
    if target == "entry":
        return f"ledger entry #{item['id']} {fmt(item['amount_pence'], signed=True)} ({item['description']})"
    return f"account {item['name']}"


# ---------------------------------------------------------------- amend

def normalise_amend(conn, raw: dict, source: str):
    im = _im()
    errors, warnings = [], []
    target = str(raw.get("target") or "").strip()
    reason = im._text(raw.get("reason"), 300)
    if target not in AMEND_TARGETS:
        raise im.CandidateError([f"target: one of {', '.join(AMEND_TARGETS)}"])
    if not reason:
        errors.append("reason: say why it is being changed (it goes in the audit trail)")
    try:
        item = {"position": lambda: find_position(conn, raw.get("ref")), "leg": lambda: find_leg(conn, raw),
                "offer": lambda: find_offer(conn, raw.get("ref")), "free_bet": lambda: find_free_bet(conn, raw),
                "transfer": lambda: find_transfer(conn, raw), "account": lambda: find_account(conn, raw.get("ref"))}[target]()
    except Problem as exc:
        raise im.CandidateError([str(exc)] + errors) from None
    changes, before = {}, {}
    for name in AMEND_FIELDS[target]:
        value = raw.get(name)
        if value in (None, ""):
            continue
        try:
            if name in ("event_at", "placed_at", "expires_at", "at", "return_by"):
                new = im._time(value, name, errors)
                key = {"return_by": "due_at"}.get(name, name)
            elif name == "stake":
                new, key = im._money(value, name, errors), "stake_pence"
            elif name == "odds":
                new, key = odds_text(parse_odds(value)), "odds"
            elif name == "commission":
                new, key = parse_commission(str(value)), "commission_bp"
            elif name == "reference":
                new, key = im._text(value, 120), "external_ref"
            elif name == "offer":
                o = find_offer(conn, value)
                new, key = o["id"], "offer_id"
            elif name == "status":
                allowed = services.OFFER_STATUSES if target == "offer" else ("available", "used", "expired", "void")
                if value not in allowed:
                    raise Problem(f"status: one of {', '.join(allowed)}")
                new, key = value, "status"
            elif name == "move_to":
                if str(value).strip().lower() == "new":
                    new = "new"
                else:
                    new = find_position(conn, value)["id"]
                key = "move_to"
            else:
                new, key = im._text(value, 160), name
        except (Problem, ValueError) as exc:
            errors.append(f"{name}: {exc}" if not str(exc).startswith(name) else str(exc))
            continue
        old = item.get(key) if key != "move_to" else item.get("position_id")
        if new != old:
            changes[key] = new
            before[key] = old
    if not changes and not errors:
        errors.append("nothing to change: give at least one new value that differs from the current one")
    if target == "transfer" and "due_at" in changes and item["capital_type"] != "temporary_funding":
        errors.append("return_by: only temporary funding has a date to return it by")
    if target == "leg" and item["result"] != "pending" and set(changes) & {"stake_pence", "odds", "commission_bp"}:
        warnings.append("this bet is settled: its result will be recalculated with the new figures")
    if errors:
        raise im.CandidateError(errors)
    return {"target": target, "target_id": item["id"], "label": _label(conn, target, item), "changes": changes,
            "before": before, "reason": reason, "resolve": resolve(target, item), "source": source}, warnings


def apply_amend(conn, c: dict) -> str:
    t, tid, ch, note = c["target"], c["target_id"], dict(c["changes"]), c["reason"]
    if t == "position":
        services.update_position(conn, tid, ch, note=note)
        return f"position:{tid}"
    if t == "leg":
        move = ch.pop("move_to", None)
        if ch:
            services.update_leg(conn, tid, ch, note=note)
        if move == "new":
            pid = services.split_leg(conn, tid, note)
            return f"position:{pid}"
        if move:
            services.move_leg(conn, tid, move, note)
        return f"position:{services.row(conn, 'legs', tid)['position_id']}"
    if t == "offer":
        services.update_offer(conn, tid, ch, note=note)
        return f"offer:{tid}"
    if t == "free_bet":
        services.update_free_bet(conn, tid, ch, note=note)
        return f"free_bet:{tid}"
    if t == "transfer":
        if "at" in ch:
            services.retime_transfer(conn, tid, ch["at"], note)
        if "due_at" in ch:
            services.set_transfer_due(conn, tid, ch["due_at"], note)
        return f"transfer:{tid}"
    services.update_account(conn, tid, name=ch.get("name"))
    return f"account:{tid}"


# ---------------------------------------------------------------- already applied?

_TABLES = {"position": "positions", "leg": "legs", "offer": "offers", "free_bet": "free_bets", "transfer": "transfers",
           "account": "accounts", "entry": "ledger_entries", "settlement": "positions"}
# The protected change (bets/ops.py) that does the same thing as a reverse candidate, and its record parameter.
SAME_CHANGE = {"transfer": ("reverse_transfer", "transfer_id"), "position": ("delete_position", "position_id"),
               "free_bet": ("delete_free_bet", "free_bet_id"), "settlement": ("reopen_position", "position_id")}


def already_applied(conn, kind: str, c: dict) -> str | None:
    """If the records already show what this amend/reverse candidate asks for (because the same correction was made
    another way, e.g. confirmed as a protected change), say so; otherwise None. A record that is missing, or in any
    other state, is NOT treated as done: that stays a real problem for the user to look at."""
    target, tid = c.get("target"), c.get("target_id")
    table = _TABLES.get(target)
    if not table or not tid:
        return None
    row = services.row(conn, table, int(tid))
    if not row:
        return None
    name = f"{'position' if target == 'settlement' else target.replace('_', ' ')} #{tid}"
    if kind == "reverse":
        when = None
        if target == "transfer":
            when = row["voided_at"]
        elif target == "entry":
            when = row["superseded_at"]
        elif target in ("leg", "position", "free_bet"):
            when = row["deleted_at"]
        elif target == "settlement":
            legs = conn.execute("SELECT result FROM legs WHERE position_id = ? AND deleted_at IS NULL", (tid,)).fetchall()
            if row["deleted_at"] or not legs or any(l["result"] != "pending" for l in legs):
                return None
            when = row["updated_at"]
        if not when:
            return None
        how = ""
        same = SAME_CHANGE.get(target)
        if same:
            ch = conn.execute("SELECT public_id, decided_by FROM assistant_ops WHERE op = ? AND status = 'applied' AND "
                              "CAST(json_extract(params_json, '$.' || ?) AS INTEGER) = ? ORDER BY id DESC LIMIT 1",
                              (same[0], same[1], int(tid))).fetchone()
            if ch:
                how = f" by change {ch['public_id']}" + (" (confirmed by you)" if ch["decided_by"] == "user" else "")
        verb = "reopened" if target == "settlement" else ("reversed" if target in ("transfer", "entry") else "removed")
        return f"{name} was already {verb} on {times.local(when)}{how}"
    if kind == "amend":
        if row.get("deleted_at") or row.get("voided_at"):
            return None
        for key, value in (c.get("changes") or {}).items():
            if key == "move_to":
                if value == "new" or row.get("position_id") != value:
                    return None
            elif row.get(key) != value:
                return None
        return f"{name} already has these values" if c.get("changes") else None
    return None


# ---------------------------------------------------------------- reverse

def normalise_reverse(conn, raw: dict, source: str):
    im = _im()
    errors, warnings = [], []
    target = str(raw.get("target") or "").strip()
    reason = im._text(raw.get("reason"), 300)
    if target not in REVERSE_TARGETS:
        raise im.CandidateError([f"target: one of {', '.join(REVERSE_TARGETS)}"])
    if not reason:
        errors.append("reason: say why (it goes in the audit trail)")
    try:
        if target == "transfer":
            item = find_transfer(conn, raw)
            effect = "Voids it: both ledger entries are withdrawn (kept in the history)."
        elif target == "entry":
            ref = str(raw.get("ref") or "").lstrip("#")
            item = services.row(conn, "ledger_entries", int(ref)) if ref.isdigit() else None
            if not item or item["superseded_at"]:
                raise Problem("ref: no active ledger entry with that number")
            if item["posting_key"]:
                raise Problem("ref: that entry belongs to a bet or transfer; reverse the bet or transfer instead")
            effect = "Reverses it (kept in the history)."
        elif target == "leg":
            item = find_leg(conn, raw)
            effect = "Removes the bet; its stake and any settlement are withdrawn" + (
                "; its free bet becomes available again." if item["free_bet_id"] else ".")
        elif target in ("position", "settlement"):
            item = find_position(conn, raw.get("ref"))
            if target == "settlement":
                if item["status"] == "open":
                    raise Problem("ref: that position isn't settled")
                effect = "Reopens every bet in it: settlement entries are withdrawn and it becomes open again."
            else:
                effect = "Removes the position and every bet in it (kept in the audit trail)."
        else:
            item = find_free_bet(conn, raw)
            if services.token_in_use(conn, item["id"]):
                raise Problem("ref: that free bet is linked to a bet; remove the bet instead")
            effect = "Removes the free bet (only an unused one can be removed)."
    except Problem as exc:
        raise im.CandidateError([str(exc)] + errors) from None
    if errors:
        raise im.CandidateError(errors)
    label = _label(conn, "position" if target == "settlement" else target, item)
    return {"target": target, "target_id": item["id"], "label": label, "effect": effect, "reason": reason,
            "resolve": resolve("position" if target == "settlement" else target, item), "source": source}, warnings


def apply_reverse(conn, c: dict) -> str:
    t, tid, note = c["target"], c["target_id"], c["reason"]
    if t == "transfer":
        services.void_transfer(conn, tid, note)
        return f"transfer:{tid}"
    if t == "entry":
        services.reverse_entry(conn, tid, note)
        return f"entry:{tid}"
    if t == "leg":
        pid = services.row(conn, "legs", tid)["position_id"]
        services.delete_leg(conn, tid, note)
        return f"position:{pid}"
    if t == "position":
        services.delete_position(conn, tid, note)
        return f"position:{tid}"
    if t == "settlement":
        for (lid,) in conn.execute("SELECT id FROM legs WHERE position_id = ? AND deleted_at IS NULL AND result <> 'pending'",
                                   (tid,)).fetchall():
            services.settle_leg(conn, lid, "pending", note=note)
        return f"position:{tid}"
    services.remove_free_bet(conn, tid, note)
    return f"free_bet:{tid}"


def preview_rows(kind: str, c: dict) -> list[tuple[str, str]]:
    def show(key, v):
        if v is None or v == "":
            return "—"
        if key.endswith("_pence"):
            return fmt(v)
        if key in ("event_at", "placed_at", "expires_at", "at", "due_at"):
            return times.local(v)
        if key == "commission_bp":
            return f"{v / 100:g}%"
        return str(v)
    rows = [("Changes" if kind == "amend" else "Undoes", c["label"])]
    if kind == "amend":
        for k, v in c["changes"].items():
            rows.append((k.replace("_pence", "").replace("_bp", "").replace("_id", "").replace("_", " ").capitalize(),
                         f"{show(k, c['before'].get(k))} → {show(k, v)}"))
    else:
        rows.append(("Effect", c["effect"]))
    rows.append(("Reason", c["reason"]))
    return rows


def to_raw(kind: str, c: dict) -> dict:
    """Back to input fields (for the Edit form and for re-checking on confirmation)."""
    raw = {"target": c["target"], **c.get("resolve", {"ref": c["target_id"]}), "reason": c["reason"]}
    if kind == "amend":
        names = {"due_at": "return_by", "stake_pence": "stake", "commission_bp": "commission", "external_ref": "reference",
                 "offer_id": "offer"}
        for k, v in c["changes"].items():
            if k == "stake_pence":
                v = plain(v)
            elif k == "commission_bp":
                v = f"{v / 100:g}"
            raw[names.get(k, k)] = v
    return raw
