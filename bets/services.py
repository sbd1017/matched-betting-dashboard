"""The one place that changes financial data.

Every input route (manual forms, Quick Add, the import/preview API, future Shortcut/OCR/CSV
importers) ends up calling these functions inside one transaction (``db.tx()``), so the
ledger rules and the audit trail are the same whatever the input method.

Ledger rules
------------
* ``capital`` entries (opening balances, transfers) move your own money; never profit.
* ``stake`` entries are cash a bookmaker took for a bet; they count towards profit only
  once that leg is settled (until then the money is "tied up").
* ``pnl`` entries (winnings, lay results, commission, rewards, adjustments) are profit/loss.
* Lay liability is exposure held by the exchange, not a cash movement, so it is computed
  from open lay legs rather than posted.
* System postings have a ``posting_key``; re-posting a key supersedes the previous entry
  (kept and linked) instead of duplicating or deleting money.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
from decimal import Decimal

from . import times
from .calc import lay_liability
from .money import commission_fraction, fmt, odds_text, parse_odds, round_pence


class UserError(ValueError):
    """A problem with the input, safe to show to the user."""


CAPITAL = {"opening_balance", "transfer"}
PNL = {"winnings", "lay_win", "lay_loss", "commission", "refund", "free_bet_winnings", "promotion_reward",
       "bonus_cash", "adjustment", "reconciliation", "other"}
MANUAL_CATEGORIES = ("promotion_reward", "bonus_cash", "adjustment", "refund", "other")
BETTING_KINDS = ("bookmaker", "exchange")
OFFER_STATUSES = ("not_started", "qualifying", "awaiting_reward", "reward_received", "converting", "complete",
                  "expired", "abandoned")
OFFER_CLOSURES = ("complete", "expired", "abandoned")       # the only statuses set by hand; the rest follow the records
REWARD_TRIGGERS = ("bet_placed", "bet_fully_matched", "bet_settled", "bet_lost", "other")
HEDGE_STRATEGIES = ("matched", "intentionally_unhedged", "pending")
OFFER_TYPES = ("sign_up", "reload", "free_bet", "bet_builder", "enhanced_odds", "qualifying", "recurring", "other")
LEG_RESULTS = ("pending", "won", "lost", "void", "cashout", "manual")


def entry_class(category: str) -> str:
    if category in CAPITAL:
        return "capital"
    if category == "stake":
        return "stake"
    if category in PNL:
        return "pnl"
    raise UserError("Unknown ledger category")


# ---------------------------------------------------------------- audit

# Who is acting, for the audit trail: 'user' (signed-in web requests), 'assistant:<token>' (the Assistant API),
# 'assistant+user' (an assistant change the user confirmed) or 'system' (CLI, repairs). Set per request.
_ACTOR = contextvars.ContextVar("bets_actor", default="system")
_REQUEST = contextvars.ContextVar("bets_request", default=None)


class acting:
    """with acting("assistant:abc", request_id): ... -- audit rows inside carry that actor and request id."""

    def __init__(self, actor: str, request_id: str | None = None):
        self.actor, self.request_id = actor, request_id

    def __enter__(self):
        self._tokens = (_ACTOR.set(self.actor), _REQUEST.set(self.request_id))
        return self

    def __exit__(self, *exc):
        _ACTOR.reset(self._tokens[0])
        _REQUEST.reset(self._tokens[1])
        return False


def current_actor() -> str:
    return _ACTOR.get()


def audit(conn, entity: str, entity_id, action: str, before=None, after=None, note: str = "",
          reverses_id: int | None = None) -> int:
    cur = conn.execute("INSERT INTO audit_log (at, entity, entity_id, action, before_json, after_json, note, actor, "
                       "request_id, reverses_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (times.now_iso(), entity, entity_id, action,
                        json.dumps(before, default=str, sort_keys=True) if before is not None else None,
                        json.dumps(after, default=str, sort_keys=True) if after is not None else None, note[:500],
                        _ACTOR.get(), _REQUEST.get(), reverses_id))
    return cur.lastrowid


def row(conn, table: str, id_: int) -> dict | None:
    r = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id_,)).fetchone()  # table names are constants
    return dict(r) if r else None


def _need(conn, table: str, id_: int, what: str) -> dict:
    r = row(conn, table, id_)
    if not r or r.get("deleted_at"):
        raise UserError(f"{what} not found")
    return r


# ---------------------------------------------------------------- postings

def post(conn, *, account_id: int, amount: int, category: str, at: str, key: str | None = None,
         description: str = "", position_id=None, leg_id=None, offer_id=None, transfer_id=None,
         free_bet_id=None, note: str = "") -> int | None:
    """Create a ledger entry. With a key, an existing active entry for that key is superseded
    (unless identical, in which case nothing changes). amount == 0 with a key cancels it."""
    cls = entry_class(category)
    now = times.now_iso()
    old = conn.execute("SELECT * FROM ledger_entries WHERE posting_key = ? AND superseded_at IS NULL",
                       (key,)).fetchone() if key else None
    if old and (old["account_id"], old["amount_pence"], old["category"], old["at"]) == (account_id, amount, category, at):
        return old["id"]
    if old:
        conn.execute("UPDATE ledger_entries SET superseded_at = ?, supersede_note = ? WHERE id = ?",
                     (now, note or "replaced", old["id"]))
    if amount == 0 and key:
        if old:
            audit(conn, "ledger", old["id"], "superseded", dict(old), None, note or "cancelled")
        return None
    cur = conn.execute(
        "INSERT INTO ledger_entries (at, account_id, amount_pence, category, class, position_id, leg_id, offer_id, "
        "transfer_id, free_bet_id, posting_key, description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (at, account_id, amount, category, cls, position_id, leg_id, offer_id, transfer_id, free_bet_id, key,
         description[:200], now))
    new_id = cur.lastrowid
    if old:
        conn.execute("UPDATE ledger_entries SET superseded_by = ? WHERE id = ?", (new_id, old["id"]))
        audit(conn, "ledger", old["id"], "superseded", dict(old), {"by": new_id, "amount_pence": amount},
              note or "replaced")
    return new_id


def cancel_key(conn, key: str, note: str) -> None:
    old = conn.execute("SELECT * FROM ledger_entries WHERE posting_key = ? AND superseded_at IS NULL", (key,)).fetchone()
    if old:
        conn.execute("UPDATE ledger_entries SET superseded_at = ?, supersede_note = ? WHERE id = ?",
                     (times.now_iso(), note, old["id"]))
        audit(conn, "ledger", old["id"], "superseded", dict(old), None, note)


def cancel_prefix(conn, prefix: str, note: str) -> None:
    for r in conn.execute("SELECT posting_key FROM ledger_entries WHERE posting_key LIKE ? AND superseded_at IS NULL",
                          (prefix + "%",)).fetchall():
        cancel_key(conn, r[0], note)


def balance(conn, account_id: int) -> int:
    return conn.execute("SELECT coalesce(sum(amount_pence), 0) FROM ledger_entries "
                        "WHERE account_id = ? AND superseded_at IS NULL", (account_id,)).fetchone()[0]


# ---------------------------------------------------------------- accounts

def create_account(conn, *, name: str, kind: str, commission_bp: int = 0, notes: str = "",
                   opening_balance: int = 0, at: str | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise UserError("Enter an account name")
    if kind not in ("bookmaker", "exchange", "bank"):
        raise UserError("Choose bookmaker, exchange or bank")
    if conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise UserError("An account with that name already exists")
    if kind != "exchange":
        commission_bp = 0
    now = times.now_iso()
    cur = conn.execute("INSERT INTO accounts (name, kind, commission_bp, notes, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (name, kind, commission_bp, notes.strip(), now, now))
    acc = cur.lastrowid
    if opening_balance:
        if kind == "bank":
            raise UserError("Bank accounts are funding sources; record transfers instead of a balance")
        post(conn, account_id=acc, amount=opening_balance, category="opening_balance", at=at or now,
             key=f"account:{acc}:opening", description="Opening balance (your existing bankroll)")
    audit(conn, "account", acc, "created", None, {"name": name, "kind": kind, "commission_bp": commission_bp,
                                                    "opening_balance_pence": opening_balance})
    return acc


STANDINGS = ("ok", "restricted", "closed")


def update_account(conn, account_id: int, *, name=None, commission_bp=None, active=None, notes=None,
                   standing=None, verified=None, commission_expires_at=None) -> None:
    """commission_expires_at: None leaves it, "" clears it, a UTC ISO sets it (exchanges only)."""
    before = _need(conn, "accounts", account_id, "Account")
    changes = {}
    if standing is not None and standing != before["standing"]:
        if standing not in STANDINGS:
            raise UserError("Standing: fine, restricted or closed")
        changes["standing"] = standing
        changes["standing_at"] = times.now_iso()
    if verified is not None and int(bool(verified)) != before["verified"]:
        changes["verified"] = int(bool(verified))
    if name is not None and name.strip() and name.strip() != before["name"]:
        if conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE AND id <> ?",
                        (name.strip(), account_id)).fetchone():
            raise UserError("An account with that name already exists")
        changes["name"] = name.strip()
    if commission_bp is not None and before["kind"] == "exchange":
        changes["commission_bp"] = commission_bp
    if commission_expires_at is not None and before["kind"] == "exchange" \
            and (commission_expires_at or None) != before["commission_expires_at"]:
        changes["commission_expires_at"] = commission_expires_at or None
    # A new rate saved after a promotion ended is the rate that applies now: the old end date no longer does.
    if changes.get("commission_bp", before["commission_bp"]) != before["commission_bp"]:
        ends = changes.get("commission_expires_at", before["commission_expires_at"])
        if ends and ends <= times.now_iso():
            changes["commission_expires_at"] = None
    if active is not None:
        changes["active"] = 1 if active else 0
    if notes is not None:
        changes["notes"] = notes.strip()
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE accounts SET {sets}, updated_at = ? WHERE id = ?",
                 (*changes.values(), times.now_iso(), account_id))
    audit(conn, "account", account_id, "updated", {k: before[k] for k in changes}, changes)


# ---------------------------------------------------------------- transfers (capital)

def outstanding_temporary(conn, bank_account_id: int) -> int:
    """Temporary funding from this bank still sitting in betting accounts."""
    got = conn.execute(
        "SELECT coalesce(sum(CASE WHEN capital_type = 'temporary_funding' AND from_account_id = :b THEN amount_pence END), 0) -"
        "       coalesce(sum(CASE WHEN capital_type = 'temporary_return' AND to_account_id = :b THEN temp_portion_pence END), 0)"
        " FROM transfers WHERE voided_at IS NULL", {"b": bank_account_id}).fetchone()[0]
    return max(0, got)


def default_capital_type(from_kind: str, to_kind: str) -> str:
    if from_kind == "bank" and to_kind in BETTING_KINDS:
        return "temporary_funding"
    if from_kind in BETTING_KINDS and to_kind == "bank":
        return "temporary_return"
    if from_kind in BETTING_KINDS and to_kind in BETTING_KINDS:
        return "internal"
    raise UserError("Transfers between two bank accounts are not betting activity")


def allowed_capital_types(natural: str) -> set[str]:
    """Which classifications fit a transfer whose direction gives `natural`."""
    return {"temporary_funding": {"temporary_funding", "bankroll_contribution"},
            "temporary_return": {"temporary_return", "bankroll_withdrawal", "profit_withdrawal"},
            "internal": {"internal"}}[natural]


def create_transfer(conn, *, from_id: int, to_id: int, amount: int, at: str | None = None,
                    capital_type: str | None = None, notes: str = "", due_at: str | None = None,
                    in_transit: bool = False) -> int:
    if amount <= 0:
        raise UserError("Transfer amount must be more than zero")
    if from_id == to_id:
        raise UserError("Choose two different accounts")
    src = _need(conn, "accounts", from_id, "From account")
    dst = _need(conn, "accounts", to_id, "To account")
    natural = default_capital_type(src["kind"], dst["kind"])
    capital_type = capital_type or natural
    allowed = allowed_capital_types(natural)
    if capital_type not in allowed:
        raise UserError("That transfer type does not fit these accounts")
    if capital_type == "profit_withdrawal":
        from . import metrics                    # winnings can only come out of profit that is still inside
        retained = metrics.summary(conn).retained_profit
        if amount > retained:
            raise UserError(f"A profit withdrawal can be at most the retained profit ({fmt(max(retained, 0))}); "
                            "record the rest as a bankroll withdrawal")
    if due_at and capital_type != "temporary_funding":
        raise UserError("Only temporary funding has a date to return it by")
    if in_transit and dst["kind"] != "bank":
        raise UserError("Only a withdrawal to your bank can be on its way")
    if capital_type == "temporary_funding":
        temp = amount
    elif capital_type == "temporary_return":
        temp = min(amount, outstanding_temporary(conn, to_id))     # any excess is a bankroll withdrawal
    else:
        temp = 0
    at = at or times.now_iso()
    cur = conn.execute("INSERT INTO transfers (from_account_id, to_account_id, amount_pence, at, capital_type, "
                       "temp_portion_pence, notes, created_at, due_at, in_transit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (from_id, to_id, amount, at, capital_type, temp, notes.strip(), times.now_iso(), due_at,
                        1 if in_transit else 0))
    tid = cur.lastrowid
    label = f"{src['name']} → {dst['name']}"
    post(conn, account_id=from_id, amount=-amount, category="transfer", at=at, key=f"transfer:{tid}:out",
         description=label, transfer_id=tid)
    post(conn, account_id=to_id, amount=amount, category="transfer", at=at, key=f"transfer:{tid}:in",
         description=label, transfer_id=tid)
    audit(conn, "transfer", tid, "created", None, {"from": src["name"], "to": dst["name"], "amount_pence": amount,
                                                   "capital_type": capital_type, "temp_portion_pence": temp,
                                                   "due_at": due_at})
    return tid


def mark_arrived(conn, transfer_id: int, at: str | None = None) -> None:
    """A withdrawal that was on its way has reached the bank (no balance changes: it left when it was sent)."""
    t = row(conn, "transfers", transfer_id)
    if not t or t["voided_at"]:
        raise UserError("Transfer not found")
    if not t["in_transit"]:
        raise UserError("That transfer has already arrived")
    at = at or times.now_iso()
    conn.execute("UPDATE transfers SET in_transit = 0, arrived_at = ? WHERE id = ?", (at, transfer_id))
    audit(conn, "transfer", transfer_id, "arrived", {"in_transit": 1}, {"arrived_at": at})


def set_transfer_due(conn, transfer_id: int, due_at: str | None, note: str = "") -> None:
    """Set, change or clear the date temporary funding has to go back to the bank (audited)."""
    t = row(conn, "transfers", transfer_id)
    if not t or t["voided_at"]:
        raise UserError("Transfer not found")
    if t["capital_type"] != "temporary_funding":
        raise UserError("Only temporary funding has a date to return it by")
    if due_at == t["due_at"]:
        return
    conn.execute("UPDATE transfers SET due_at = ? WHERE id = ?", (due_at, transfer_id))
    audit(conn, "transfer", transfer_id, "due_date", {"due_at": t["due_at"]}, {"due_at": due_at}, note)


def void_transfer(conn, transfer_id: int, note: str = "") -> None:
    t = row(conn, "transfers", transfer_id)
    if not t or t["voided_at"]:
        raise UserError("Transfer not found")
    conn.execute("UPDATE transfers SET voided_at = ? WHERE id = ?", (times.now_iso(), transfer_id))
    cancel_prefix(conn, f"transfer:{transfer_id}:", note or "transfer voided")
    audit(conn, "transfer", transfer_id, "voided", t, None, note)
    _recompute_temp_portions(conn, _bank_side(conn, t))


def _bank_side(conn, t: dict) -> int | None:
    for aid in (t["from_account_id"], t["to_account_id"]):
        acc = row(conn, "accounts", aid)
        if acc and acc["kind"] == "bank":
            return aid
    return None


def _recompute_temp_portions(conn, bank_id: int | None) -> None:
    """Walk one bank's temporary funding and returns in time order: each return repays what is still outstanding
    (any excess is a bankroll withdrawal). Keeps 'temporary funding out' right after a correction."""
    if not bank_id:
        return
    outstanding = 0
    for t in conn.execute("SELECT * FROM transfers WHERE voided_at IS NULL AND ((from_account_id = :b AND capital_type = "
                          "'temporary_funding') OR (to_account_id = :b AND capital_type = 'temporary_return')) "
                          "ORDER BY at, id", {"b": bank_id}).fetchall():
        if t["capital_type"] == "temporary_funding":
            want = t["amount_pence"]
            outstanding += want
        else:
            want = min(t["amount_pence"], outstanding)
            outstanding -= want
        if want != t["temp_portion_pence"]:
            conn.execute("UPDATE transfers SET temp_portion_pence = ? WHERE id = ?", (want, t["id"]))
            audit(conn, "transfer", t["id"], "temporary_portion", {"temp_portion_pence": t["temp_portion_pence"]},
                  {"temp_portion_pence": want}, "recalculated after another transfer to this bank changed")


def reclassify_transfer(conn, transfer_id: int, capital_type: str, note: str = "") -> bool:
    """Correct what kind of capital movement a transfer was (temporary funding, its return, bankroll money in or
    out, winnings taken out). Balances do not change and it never touches profit: transfers are capital.
    Returns False when it was already that kind."""
    t = row(conn, "transfers", transfer_id)
    if not t or t["voided_at"]:
        raise UserError("Transfer not found")
    src, dst = row(conn, "accounts", t["from_account_id"]), row(conn, "accounts", t["to_account_id"])
    natural = default_capital_type(src["kind"], dst["kind"])
    if capital_type not in allowed_capital_types(natural):
        raise UserError("That transfer type does not fit these accounts (" + ", ".join(sorted(allowed_capital_types(natural)))
                        + ")")
    if capital_type == t["capital_type"]:
        return False
    if capital_type == "profit_withdrawal":
        from . import metrics
        if t["amount_pence"] > metrics.summary(conn).retained_profit:
            raise UserError("A profit withdrawal can be at most the retained profit; record it as a bankroll withdrawal")
    changes = {"capital_type": capital_type,
               "temp_portion_pence": t["amount_pence"] if capital_type == "temporary_funding" else 0}
    if t["due_at"] and capital_type != "temporary_funding":
        changes["due_at"] = None
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE transfers SET {sets} WHERE id = ?", (*changes.values(), transfer_id))
    audit(conn, "transfer", transfer_id, "reclassified", {k: t[k] for k in changes}, changes, note)
    _recompute_temp_portions(conn, _bank_side(conn, t))
    return True


def update_transfer_notes(conn, transfer_id: int, notes: str, note: str = "") -> None:
    t = row(conn, "transfers", transfer_id)
    if not t or t["voided_at"]:
        raise UserError("Transfer not found")
    if (notes or "").strip() == t["notes"]:
        return
    conn.execute("UPDATE transfers SET notes = ? WHERE id = ?", (notes.strip()[:2000], transfer_id))
    audit(conn, "transfer", transfer_id, "edited", {"notes": t["notes"]}, {"notes": notes.strip()[:2000]}, note)


def retime_transfer(conn, transfer_id: int, at: str, note: str) -> None:
    """Correct when a transfer happened, nothing else: same accounts, amount and classification. Its two ledger
    entries are re-dated by superseding them (kept, audited); balances and capital do not change."""
    t = row(conn, "transfers", transfer_id)
    if not t or t["voided_at"]:
        raise UserError("Transfer not found")
    try:
        valid = times.parse(at) is not None
    except ValueError:
        valid = False
    if not valid:
        raise UserError("Enter a valid date and time")
    if at == t["at"]:
        return
    if not note.strip():
        raise UserError("Say why the time is being corrected")
    conn.execute("UPDATE transfers SET at = ? WHERE id = ?", (at, transfer_id))
    label = conn.execute("SELECT description FROM ledger_entries WHERE posting_key = ? AND superseded_at IS NULL",
                         (f"transfer:{transfer_id}:out",)).fetchone()[0]
    for key, account_id, amount in ((f"transfer:{transfer_id}:out", t["from_account_id"], -t["amount_pence"]),
                                    (f"transfer:{transfer_id}:in", t["to_account_id"], t["amount_pence"])):
        post(conn, account_id=account_id, amount=amount, category="transfer", at=at, key=key, description=label,
             transfer_id=transfer_id, note="re-dated: " + note)
    audit(conn, "transfer", transfer_id, "retimed", {"at": t["at"]}, {"at": at}, note)


# ---------------------------------------------------------------- positions and legs

def _clean_leg(conn, data: dict) -> dict:
    acc = _need(conn, "accounts", int(data["account_id"]), "Account")
    if acc["kind"] not in BETTING_KINDS:
        raise UserError("Bets are placed with a bookmaker or exchange, not a bank account")
    side = data.get("side")
    if side not in ("back", "lay"):
        raise UserError("Choose back or lay")
    if side == "lay" and acc["kind"] != "exchange":
        raise UserError("Lay bets are placed on an exchange")
    stake = int(data["stake_pence"])
    if stake <= 0:
        raise UserError("Stake must be more than zero")
    odds = parse_odds(data["odds"])
    commission_bp = int(data.get("commission_bp", acc["commission_bp"] if acc["kind"] == "exchange" else 0) or 0)
    free_bet_id = data.get("free_bet_id") or None
    if free_bet_id is not None:
        if side != "back":
            raise UserError("A free bet can only be used on a back bet")
        fb = _need(conn, "free_bets", int(free_bet_id), "Free bet")
        if fb["account_id"] != acc["id"]:
            raise UserError("That free bet belongs to a different bookmaker")
        if fb["face_value_pence"] != stake:
            raise UserError("Stake must equal the free bet value")
    return {"account_id": acc["id"], "side": side, "stake_pence": stake, "odds": odds_text(odds),
            "commission_bp": commission_bp, "free_bet_id": int(free_bet_id) if free_bet_id else None,
            "token_marked_used": bool(data.get("token_marked_used")),
            "placed_at": data.get("placed_at") or times.now_iso(),
            "external_ref": str(data.get("external_ref") or "")[:120], "source": str(data.get("source") or "manual")[:40]}


def create_position(conn, *, event: str, market: str = "", selection: str = "", event_at: str | None = None,
                    kind: str = "qualifying", offer_id=None, expected_pnl: int | None = None, notes: str = "",
                    source: str = "manual", legs: list[dict] = (), hedge_strategy: str | None = None,
                    unhedged_reason: str = "") -> int:
    event = (event or "").strip()
    if not event:
        raise UserError("Enter the event")
    if kind not in ("qualifying", "free_bet_snr", "free_bet_sr", "other"):
        raise UserError("Unknown position type")
    if offer_id:
        _need(conn, "offers", int(offer_id), "Offer")
    now = times.now_iso()
    cur = conn.execute("INSERT INTO positions (kind, event, market, selection, event_at, offer_id, expected_pnl_pence, "
                       "notes, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (kind, event, market.strip(), selection.strip(), event_at, int(offer_id) if offer_id else None,
                        expected_pnl, notes.strip(), source[:40], now, now))
    pid = cur.lastrowid
    audit(conn, "position", pid, "created", None, {"event": event, "selection": selection, "kind": kind,
                                                   "offer_id": offer_id, "expected_pnl_pence": expected_pnl,
                                                   "source": source})
    for leg in legs:
        add_leg(conn, pid, leg, sync_offer=False)
    if hedge_strategy:
        set_hedge_strategy(conn, pid, hedge_strategy, unhedged_reason)
    if offer_id:
        from . import offer_state
        offer_state.sync(conn, int(offer_id), note=f"position {pid} recorded")
    return pid


def hedge_state(pos: dict, lays: int) -> str:
    """matched (has a lay) / intentionally_unhedged (deliberately no lay) / pending (a lay is still expected)."""
    if lays:
        return "matched"
    return "intentionally_unhedged" if pos.get("hedge_strategy") == "intentionally_unhedged" else "pending"


def set_hedge_strategy(conn, position_id: int, strategy: str, reason: str = "", note: str = "") -> None:
    """Say how a position is hedged. intentionally_unhedged: a cash back bet knowingly left without a lay (e.g. a 5p
    qualifier): it needs a reason, has no lay and no free bet, and records the accepted exposure (the stake)."""
    pos = _need(conn, "positions", position_id, "Position")
    if strategy not in HEDGE_STRATEGIES:
        raise UserError("Hedging: matched, intentionally_unhedged or pending")
    legs = conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL AND result <> 'void'",
                        (position_id,)).fetchall()
    backs = [l for l in legs if l["side"] == "back"]
    exposure, why = None, None
    if strategy == "intentionally_unhedged":
        if any(l["side"] == "lay" for l in legs):
            raise UserError("This position has a lay, so it is hedged")
        if not backs:
            raise UserError("Add the bookmaker bet first")
        if any(l["free_bet_id"] for l in backs):
            raise UserError("A free-bet back bet cannot be left unhedged by design")
        if pos["kind"] not in ("qualifying", "other"):
            raise UserError("Only a qualifying (cash) bet can be left unhedged by design")
        why = (reason or "").strip()[:300]
        if not why:
            raise UserError("Say why it is deliberately not laid")
        exposure = sum(l["stake_pence"] for l in backs)
    new = {"hedge_strategy": strategy, "accepted_exposure_pence": exposure, "unhedged_reason": why}
    old = {k: pos[k] for k in new}
    if new == old:
        return
    conn.execute("UPDATE positions SET hedge_strategy = ?, accepted_exposure_pence = ?, unhedged_reason = ?, "
                 "updated_at = ? WHERE id = ?", (strategy, exposure, why, times.now_iso(), position_id))
    audit(conn, "position", position_id, "hedging", old, new, note or why or "")


def add_leg(conn, position_id: int, data: dict, sync_offer: bool = True) -> int:
    pos = _need(conn, "positions", position_id, "Position")
    if pos["status"] != "open":
        raise UserError("This position is settled; reopen a leg to change it")
    leg = _clean_leg(conn, data)
    fb_before = None
    if leg["free_bet_id"]:
        fb_before = row(conn, "free_bets", leg["free_bet_id"])
        _check_token_for(conn, fb_before, pos, leg["token_marked_used"])
    now = times.now_iso()
    cur = conn.execute("INSERT INTO legs (position_id, account_id, side, stake_pence, odds, commission_bp, free_bet_id, "
                       "placed_at, external_ref, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (position_id, leg["account_id"], leg["side"], leg["stake_pence"], leg["odds"],
                        leg["commission_bp"], leg["free_bet_id"], leg["placed_at"], leg["external_ref"],
                        leg["source"], now, now))
    leg_id = cur.lastrowid
    if fb_before:
        _mark_token_used(conn, fb_before, leg_id, position_id)
    _post_leg_stake(conn, leg_id)
    audit(conn, "leg", leg_id, "created", None, {**leg, "position_id": position_id})
    if pos["hedge_strategy"] == "intentionally_unhedged":
        if leg["side"] == "lay":                 # a lay was added after all: it is hedged now
            conn.execute("UPDATE positions SET hedge_strategy = 'matched', accepted_exposure_pence = NULL, "
                         "updated_at = ? WHERE id = ?", (now, position_id))
            audit(conn, "position", position_id, "hedging", {"hedge_strategy": "intentionally_unhedged"},
                  {"hedge_strategy": "matched"}, "a lay was added")
        elif not leg["free_bet_id"]:
            conn.execute("UPDATE positions SET accepted_exposure_pence = coalesce(accepted_exposure_pence, 0) + ? "
                         "WHERE id = ?", (leg["stake_pence"], position_id))
    if sync_offer and pos["offer_id"]:
        from . import offer_state
        offer_state.sync(conn, pos["offer_id"])
    return leg_id


# ---------------------------------------------------------------- free-bet tokens <-> bets

FREE_BET_KINDS = {"free_bet_snr": "snr", "free_bet_sr": "sr"}


def token_in_use(conn, free_bet_id: int, except_leg: int | None = None) -> bool:
    """A token is linked when a live, non-void bet leg uses it (a void leg returns the token)."""
    return conn.execute("SELECT 1 FROM legs WHERE free_bet_id = ? AND deleted_at IS NULL AND result <> 'void' "
                        "AND id IS NOT ?", (free_bet_id, except_leg)).fetchone() is not None


def choose_token(conn, *, account_id: int, value: int, kind: str, marked_used: bool = False,
                 exclude: tuple = ()) -> dict | None:
    """The token a free-bet back bet uses: same bookmaker, value and SNR/SR type, not linked to any bet;
    status 'available' (or, for a historical repair the user asked for, 'used' by hand). Deterministic:
    earliest expiry first (no expiry last), then oldest created, then lowest id."""
    r = conn.execute(
        "SELECT * FROM free_bets f WHERE f.account_id = ? AND f.face_value_pence = ? AND f.kind = ? "
        "AND f.status = ? AND f.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM legs l WHERE l.free_bet_id = f.id "
        "AND l.deleted_at IS NULL AND l.result <> 'void') "
        + (f"AND f.id NOT IN ({','.join('?' * len(exclude))}) " if exclude else "") +
        "ORDER BY f.expires_at IS NULL, f.expires_at, f.created_at, f.id LIMIT 1",
        (account_id, value, kind, "used" if marked_used else "available", *exclude)).fetchone()
    return dict(r) if r else None


def _check_token_for(conn, fb: dict, pos: dict, marked_used: bool, except_leg: int | None = None) -> None:
    if not fb or fb.get("deleted_at"):
        raise UserError("Free bet not found")
    want = FREE_BET_KINDS.get(pos["kind"])
    if want and fb["kind"] != want:
        raise UserError(f"That free bet is {fb['kind'].upper()}, but the position is {want.upper()}")
    if token_in_use(conn, fb["id"], except_leg):
        raise UserError("That free bet is already linked to another bet")
    if fb["status"] == "available":
        return
    if fb["status"] == "used" and marked_used:
        return                                          # historical repair: a token the user marked used by hand
    raise UserError("That free bet is not available" + (" (it was marked used: link it as a historical token)"
                                                         if fb["status"] == "used" else ""))


def _mark_token_used(conn, fb: dict, leg_id: int, position_id: int) -> None:
    after = {"status": "used", "leg_id": leg_id, "position_id": position_id, "offer_id": fb["offer_id"]}
    if fb["status"] == "available":
        conn.execute("UPDATE free_bets SET status = 'used', updated_at = ? WHERE id = ?", (times.now_iso(), fb["id"]))
        audit(conn, "free_bet", fb["id"], "used", {"status": "available"}, after)
    else:
        audit(conn, "free_bet", fb["id"], "linked", {"status": fb["status"]}, after,
              "historical token (marked used by hand) linked to the bet that used it")
    refresh_offer_progress(conn, fb["offer_id"])


def link_free_bet(conn, leg_id: int, free_bet_id: int, note: str = "") -> None:
    """Link an existing back bet of a free-bet position to the token it used (audited). The leg's cash stake is
    withdrawn (a free bet is not cash) and a settled leg is re-settled with free-bet maths."""
    leg = _need(conn, "legs", leg_id, "Bet")
    pos = row(conn, "positions", leg["position_id"])
    if leg["side"] != "back":
        raise UserError("A free bet can only be used on a back bet")
    if pos["kind"] not in FREE_BET_KINDS:
        raise UserError("Set the position type to a free bet (SNR or SR) first")
    if leg["free_bet_id"]:
        raise UserError("This bet is already linked to a free bet")
    if leg["result"] in ("cashout", "manual"):
        raise UserError("Reopen this bet first (it was settled with an entered amount)")
    fb = _need(conn, "free_bets", int(free_bet_id), "Free bet")
    if fb["account_id"] != leg["account_id"] or fb["face_value_pence"] != leg["stake_pence"]:
        raise UserError("The free bet must be from the same bookmaker and for the same amount as the stake")
    _check_token_for(conn, fb, pos, marked_used=True, except_leg=leg_id)
    conn.execute("UPDATE legs SET free_bet_id = ?, updated_at = ? WHERE id = ?", (fb["id"], times.now_iso(), leg_id))
    _mark_token_used(conn, fb, leg_id, pos["id"])
    audit(conn, "leg", leg_id, "free_bet_linked", {"free_bet_id": None}, {"free_bet_id": fb["id"]}, note)
    _post_leg_stake(conn, leg_id)
    if leg["result"] in ("won", "lost", "void"):
        settle_leg(conn, leg_id, leg["result"], at=leg["settled_at"], note="re-settled after its free bet was linked")


def relink_free_bet(conn, leg_id: int, free_bet_id: int, previous_status: str = "void", note: str = "") -> int | None:
    """Point a free-bet back bet at a different token of the same bookmaker, value and type (e.g. a duplicate record
    replaced by the real one). Nothing about the money changes. The token it used before becomes `previous_status`.
    Returns the previous token id."""
    leg = _need(conn, "legs", leg_id, "Bet")
    if not leg["free_bet_id"]:
        raise UserError("This bet does not use a free bet yet; link one instead")
    if leg["free_bet_id"] == int(free_bet_id):
        return None
    if previous_status not in ("void", "available"):
        raise UserError("The replaced free bet becomes void or available")
    old = row(conn, "free_bets", leg["free_bet_id"])
    new = _need(conn, "free_bets", int(free_bet_id), "Free bet")
    if (new["account_id"], new["face_value_pence"], new["kind"]) != (old["account_id"], old["face_value_pence"], old["kind"]):
        raise UserError("Only a free bet from the same bookmaker, for the same value and type, can replace it")
    if token_in_use(conn, new["id"]):
        raise UserError(f"Free bet #{new['id']} is already linked to another bet")
    if new["status"] not in ("available", "used"):
        raise UserError(f"Free bet #{new['id']} is {new['status']}")
    now = times.now_iso()
    conn.execute("UPDATE legs SET free_bet_id = ?, updated_at = ? WHERE id = ?", (new["id"], now, leg_id))
    audit(conn, "leg", leg_id, "free_bet_replaced", {"free_bet_id": old["id"]}, {"free_bet_id": new["id"]}, note)
    conn.execute("UPDATE free_bets SET status = 'used', updated_at = ? WHERE id = ?", (now, new["id"]))
    audit(conn, "free_bet", new["id"], "linked", {"status": new["status"]},
          {"status": "used", "leg_id": leg_id, "position_id": leg["position_id"]}, note)
    conn.execute("UPDATE free_bets SET status = ?, updated_at = ? WHERE id = ?", (previous_status, now, old["id"]))
    audit(conn, "free_bet", old["id"], "unlinked", {"status": old["status"], "leg_id": leg_id},
          {"status": previous_status, "replaced_by": new["id"]}, note)
    for offer_id in {old["offer_id"], new["offer_id"]}:
        refresh_offer_progress(conn, offer_id)
    return old["id"]


def unlink_free_bet(conn, free_bet_id: int, then_status: str = "available", note: str = "") -> int:
    """Take a free bet off the bet it was linked to. That bet then counts as a cash stake (its stake is posted and a
    settled bet is re-settled), so this changes money: preview it first. Returns the leg id."""
    fb = _need(conn, "free_bets", int(free_bet_id), "Free bet")
    if then_status not in ("available", "void", "expired"):
        raise UserError("After unlinking, the free bet is available, void or expired")
    leg = conn.execute("SELECT * FROM legs WHERE free_bet_id = ? AND deleted_at IS NULL ORDER BY id LIMIT 1",
                       (fb["id"],)).fetchone()
    if not leg:
        raise UserError("That free bet is not linked to a bet")
    if leg["result"] in ("cashout", "manual"):
        raise UserError("Reopen that bet first (it was settled with an entered amount)")
    now = times.now_iso()
    conn.execute("UPDATE legs SET free_bet_id = NULL, updated_at = ? WHERE id = ?", (now, leg["id"]))
    audit(conn, "leg", leg["id"], "free_bet_unlinked", {"free_bet_id": fb["id"]}, {"free_bet_id": None}, note)
    conn.execute("UPDATE free_bets SET status = ?, updated_at = ? WHERE id = ?", (then_status, now, fb["id"]))
    audit(conn, "free_bet", fb["id"], "unlinked", {"status": fb["status"], "leg_id": leg["id"]}, {"status": then_status}, note)
    _post_leg_stake(conn, leg["id"])
    if leg["result"] in ("won", "lost", "void"):
        settle_leg(conn, leg["id"], leg["result"], at=leg["settled_at"], note="re-settled after its free bet was unlinked")
    refresh_offer_progress(conn, fb["offer_id"])
    return leg["id"]


def _post_leg_stake(conn, leg_id: int) -> None:
    leg = row(conn, "legs", leg_id)
    key = f"leg:{leg_id}:stake"
    if leg["deleted_at"] or leg["side"] != "back" or leg["free_bet_id"]:
        cancel_key(conn, key, "no cash stake for this leg")
        return
    post(conn, account_id=leg["account_id"], amount=-leg["stake_pence"], category="stake", at=leg["placed_at"],
         key=key, description="Back stake", position_id=leg["position_id"], leg_id=leg_id)


def update_leg(conn, leg_id: int, data: dict, note: str = "") -> None:
    before = _need(conn, "legs", leg_id, "Leg")
    merged = {**before, **{k: v for k, v in data.items() if v is not None}}
    merged.setdefault("source", before["source"])
    clean = _clean_leg(conn, {**merged, "free_bet_id": before["free_bet_id"]})
    changes = {k: v for k, v in clean.items()
               if before.get(k) != v and k not in ("free_bet_id", "source", "token_marked_used")}
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE legs SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), times.now_iso(), leg_id))
    audit(conn, "leg", leg_id, "edited", {k: before[k] for k in changes}, changes, note)
    _post_leg_stake(conn, leg_id)
    after = row(conn, "legs", leg_id)
    if after["result"] != "pending":    # keep settlement postings consistent with the corrected leg
        settle_leg(conn, leg_id, after["result"],
                   amount=after["settled_amount_pence"] if after["result"] in ("cashout", "manual") else None,
                   commission=after["commission_paid_pence"] if after["result"] in ("cashout", "manual") else None,
                   at=after["settled_at"], note="recalculated after edit")


def delete_leg(conn, leg_id: int, note: str = "") -> None:
    leg = _need(conn, "legs", leg_id, "Leg")
    conn.execute("UPDATE legs SET deleted_at = ?, updated_at = ? WHERE id = ?", (times.now_iso(), times.now_iso(), leg_id))
    cancel_prefix(conn, f"leg:{leg_id}:", note or "leg removed")
    if leg["free_bet_id"]:
        conn.execute("UPDATE free_bets SET status = 'available', updated_at = ? WHERE id = ?",
                     (times.now_iso(), leg["free_bet_id"]))
        audit(conn, "free_bet", leg["free_bet_id"], "released", {"status": "used"}, {"status": "available"})
    audit(conn, "leg", leg_id, "removed", leg, None, note)
    _refresh_position_status(conn, leg["position_id"])


def move_leg(conn, leg_id: int, target_position_id: int, note: str = "") -> None:
    """Move a bet recorded against the wrong position. Its ledger entries are re-posted under the new position
    (the old ones are superseded and kept), its settlement is kept, and both positions' status is refreshed."""
    leg = _need(conn, "legs", leg_id, "Bet")
    if target_position_id == leg["position_id"]:
        return
    src = row(conn, "positions", leg["position_id"])
    dst = _need(conn, "positions", target_position_id, "Position")
    if dst["status"] != "open" and leg["result"] == "pending":
        raise UserError("Move an unsettled bet into an open position, or settle it first")
    if leg["free_bet_id"] and FREE_BET_KINDS.get(dst["kind"]) != FREE_BET_KINDS.get(src["kind"]):
        raise UserError("A free-bet bet can only move into a position of the same free bet type")
    if not (note or "").strip():
        raise UserError("Say why the bet is being moved")
    conn.execute("UPDATE legs SET position_id = ?, updated_at = ? WHERE id = ?", (dst["id"], times.now_iso(), leg_id))
    cancel_prefix(conn, f"leg:{leg_id}:", "moved to position " + str(dst["id"]))
    _post_leg_stake(conn, leg_id)
    moved = row(conn, "legs", leg_id)
    if moved["result"] != "pending":
        manual = moved["result"] in ("cashout", "manual")
        settle_leg(conn, leg_id, moved["result"], amount=moved["settled_amount_pence"] if manual else None,
                   commission=moved["commission_paid_pence"] if manual else None, at=moved["settled_at"],
                   note="re-posted after moving")
    audit(conn, "leg", leg_id, "moved", {"position_id": src["id"]}, {"position_id": dst["id"]}, note)
    _refresh_position_status(conn, src["id"])
    _refresh_position_status(conn, dst["id"])


def split_leg(conn, leg_id: int, note: str = "") -> int:
    """Move a bet into a new position with the same details (event, selection, market, type, offer)."""
    leg = _need(conn, "legs", leg_id, "Bet")
    src = row(conn, "positions", leg["position_id"])
    now = times.now_iso()
    cur = conn.execute("INSERT INTO positions (kind, event, market, selection, event_at, offer_id, notes, source, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (src["kind"], src["event"], src["market"], src["selection"], src["event_at"], src["offer_id"],
                        "", "split", now, now))
    pid = cur.lastrowid
    audit(conn, "position", pid, "created", None, {"split_from": src["id"], "leg_id": leg_id}, note)
    move_leg(conn, leg_id, pid, note or f"split from position {src['id']}")
    return pid


def merge_positions(conn, source_id: int, target_id: int, note: str = "") -> None:
    """Move every bet of one position into another, then remove the empty one (kept in the audit trail)."""
    if source_id == target_id:
        raise UserError("Choose a different position to merge into")
    _need(conn, "positions", source_id, "Position")
    _need(conn, "positions", target_id, "Position")
    if not (note or "").strip():
        raise UserError("Say why the positions are being merged")
    for (lid,) in conn.execute("SELECT id FROM legs WHERE position_id = ? AND deleted_at IS NULL ORDER BY id",
                               (source_id,)).fetchall():
        move_leg(conn, lid, target_id, note)
    delete_position(conn, source_id, f"merged into position {target_id}: {note}")


def update_position(conn, position_id: int, data: dict, note: str = "") -> None:
    before = _need(conn, "positions", position_id, "Position")
    fields = ("event", "market", "selection", "event_at", "offer_id", "notes", "kind", "expected_pnl_pence")
    changes = {k: data[k] for k in fields if k in data and data[k] != before[k]}
    if "event" in changes and not (changes["event"] or "").strip():
        raise UserError("Enter the event")
    if changes.get("offer_id"):
        _need(conn, "offers", int(changes["offer_id"]), "Offer")
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE positions SET {sets}, updated_at = ? WHERE id = ?",
                 (*changes.values(), times.now_iso(), position_id))
    audit(conn, "position", position_id, "edited", {k: before[k] for k in changes}, changes, note)
    if "offer_id" in changes or "kind" in changes:
        from . import offer_state
        for oid in {before["offer_id"], changes.get("offer_id", before["offer_id"])}:
            offer_state.sync(conn, oid)


def delete_position(conn, position_id: int, note: str = "") -> None:
    pos = _need(conn, "positions", position_id, "Position")
    for leg in conn.execute("SELECT id FROM legs WHERE position_id = ? AND deleted_at IS NULL", (position_id,)).fetchall():
        delete_leg(conn, leg[0], note or "position removed")
    conn.execute("UPDATE positions SET deleted_at = ?, updated_at = ? WHERE id = ?",
                 (times.now_iso(), times.now_iso(), position_id))
    audit(conn, "position", position_id, "removed", pos, None, note)
    from . import offer_state
    offer_state.sync(conn, pos["offer_id"])


# ---------------------------------------------------------------- settlement

def default_settlement(leg: dict, result: str) -> tuple[int, int]:
    """(amount credited (+) / debited (-), commission) for a standard result."""
    stake = leg["stake_pence"]
    odds = Decimal(leg["odds"])
    fb_kind = None
    if leg.get("free_bet_kind"):
        fb_kind = leg["free_bet_kind"]
    if leg["side"] == "back":
        if result == "won":
            if fb_kind == "snr":
                return round_pence(Decimal(stake) / 100 * (odds - 1)), 0
            return round_pence(Decimal(stake) / 100 * odds), 0
        if result == "void":
            return (0 if fb_kind else stake), 0
        return 0, 0
    # lay
    if result == "won":            # the selection lost: you keep the backer's stake, less commission
        commission = round_pence(Decimal(stake) / 100 * commission_fraction(leg["commission_bp"]))
        return stake, commission
    if result == "lost":           # the selection won: you pay the liability
        return -lay_liability(stake, odds), 0
    return 0, 0


def settle_leg(conn, leg_id: int, result: str, *, amount: int | None = None, commission: int | None = None,
               at: str | None = None, note: str = "") -> None:
    if result not in LEG_RESULTS:
        raise UserError("Unknown result")
    leg = _need(conn, "legs", leg_id, "Leg")
    pos = row(conn, "positions", leg["position_id"])
    fb = row(conn, "free_bets", leg["free_bet_id"]) if leg["free_bet_id"] else None
    at = at or times.now_iso()
    base = {**leg, "free_bet_kind": fb["kind"] if fb else None}
    key = f"leg:{leg_id}"
    before = {k: leg[k] for k in ("result", "settled_amount_pence", "commission_paid_pence", "settled_at")}
    if result == "pending":
        if fb and leg["result"] == "void":
            if fb["status"] != "available":
                raise UserError("The free bet from this void leg has been used elsewhere")
            conn.execute("UPDATE free_bets SET status = 'used', updated_at = ? WHERE id = ?", (times.now_iso(), fb["id"]))
        cancel_key(conn, key + ":settle", note or "leg reopened")
        cancel_key(conn, key + ":commission", note or "leg reopened")
        conn.execute("UPDATE legs SET result = 'pending', settled_amount_pence = NULL, commission_paid_pence = NULL, "
                     "settled_at = NULL, updated_at = ? WHERE id = ?", (times.now_iso(), leg_id))
        audit(conn, "leg", leg_id, "reopened", before, {"result": "pending"}, note)
        _refresh_position_status(conn, leg["position_id"])
        return
    if result in ("cashout", "manual"):
        if amount is None:
            raise UserError("Enter the amount actually credited or debited")
        if leg["side"] == "back" and amount < 0:
            raise UserError("A back bet cannot pay out a negative amount")
        commission = commission or 0
    else:
        default_amount, default_commission = default_settlement(base, result)
        amount = default_amount if amount is None else amount
        commission = default_commission if commission is None else commission
    if commission < 0:
        raise UserError("Commission cannot be negative")
    common = dict(account_id=leg["account_id"], at=at, position_id=leg["position_id"], leg_id=leg_id,
                  free_bet_id=leg["free_bet_id"], note=note or f"settled {result}")
    if leg["side"] == "back":
        if amount:
            category = "free_bet_winnings" if fb and result != "void" else ("refund" if result == "void" else "winnings")
            post(conn, amount=amount, category=category, key=key + ":settle",
                 description=f"Back {result}: {pos['event']}"[:200], **common)
        else:
            cancel_key(conn, key + ":settle", note or f"settled {result}: nothing credited")
        cancel_key(conn, key + ":commission", "no commission on back leg")
    else:
        if amount:
            category = "lay_win" if amount > 0 else "lay_loss"
            post(conn, amount=amount, category=category, key=key + ":settle",
                 description=f"Lay {result}: {pos['event']}"[:200], **common)
        else:
            cancel_key(conn, key + ":settle", note or f"settled {result}: no movement")
        if commission:
            post(conn, amount=-commission, category="commission", key=key + ":commission",
                 description=f"Exchange commission: {pos['event']}"[:200], **common)
        else:
            cancel_key(conn, key + ":commission", "no commission")
    if fb and result == "void":       # bookmakers normally return a voided free bet token
        conn.execute("UPDATE free_bets SET status = 'available', updated_at = ? WHERE id = ?", (times.now_iso(), fb["id"]))
        cancel_key(conn, key + ":settle", "free bet returned")
        audit(conn, "free_bet", fb["id"], "returned", {"status": fb["status"]}, {"status": "available"}, "leg void")
    conn.execute("UPDATE legs SET result = ?, settled_amount_pence = ?, commission_paid_pence = ?, settled_at = ?, "
                 "updated_at = ? WHERE id = ?", (result, amount, commission, at, times.now_iso(), leg_id))
    audit(conn, "leg", leg_id, "settled", before,
          {"result": result, "settled_amount_pence": amount, "commission_paid_pence": commission, "settled_at": at}, note)
    _refresh_position_status(conn, leg["position_id"])


def quick_settle(conn, position_id: int, outcome: str, *, at: str | None = None) -> None:
    """Settle every open leg from one event result: back_won, back_lost or void."""
    if outcome not in ("back_won", "back_lost", "void"):
        raise UserError("Choose what happened")
    _need(conn, "positions", position_id, "Position")
    legs = conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL", (position_id,)).fetchall()
    if not legs:
        raise UserError("This position has no bets to settle")
    from . import exposure
    if outcome != "void" and exposure.configured(conn, position_id):
        raise UserError("These bets have different selections. Settle each bet separately using its actual result.")
    for leg in legs:
        if outcome == "void":
            result = "void"
        elif leg["side"] == "back":
            result = "won" if outcome == "back_won" else "lost"
        else:
            result = "lost" if outcome == "back_won" else "won"
        settle_leg(conn, leg["id"], result, at=at, note=f"quick settle: {outcome}")


def _refresh_position_status(conn, position_id: int) -> None:
    pos = row(conn, "positions", position_id)
    legs = conn.execute("SELECT result, settled_at FROM legs WHERE position_id = ? AND deleted_at IS NULL",
                        (position_id,)).fetchall()
    if legs and all(l["result"] != "pending" for l in legs):
        status = "void" if all(l["result"] == "void" for l in legs) else "settled"
        settled_at = max(l["settled_at"] for l in legs)
    else:
        status, settled_at = "open", None
    if (status, settled_at) != (pos["status"], pos["settled_at"]):
        conn.execute("UPDATE positions SET status = ?, settled_at = ?, updated_at = ? WHERE id = ?",
                     (status, settled_at, times.now_iso(), position_id))
        audit(conn, "position", position_id, "status", {"status": pos["status"]}, {"status": status})
        from . import offer_state, plans          # its offers and bet plan follow
        if pos["offer_id"]:
            offer_state.sync(conn, pos["offer_id"], note=f"position {position_id} {status}")
        for (offer_id,) in conn.execute("SELECT DISTINCT f.offer_id FROM legs l JOIN free_bets f ON f.id = l.free_bet_id "
                                        "WHERE l.position_id = ? AND f.offer_id IS NOT NULL", (position_id,)).fetchall():
            offer_state.sync(conn, offer_id)
        plans.on_position_status(conn, position_id)


def position_pnl(conn, position_id: int) -> int:
    """Realised result: P&L entries plus the stakes of settled legs."""
    return conn.execute(
        "SELECT coalesce(sum(e.amount_pence), 0) FROM ledger_entries e LEFT JOIN legs l ON l.id = e.leg_id "
        "WHERE e.position_id = ? AND e.superseded_at IS NULL "
        "AND (e.class = 'pnl' OR (e.class = 'stake' AND l.result <> 'pending'))", (position_id,)).fetchone()[0]


# ---------------------------------------------------------------- offers and free bets

REWARD_KINDS = ("snr", "sr", "cash")
TERM_FIELDS = ("qualifying_stake_pence", "min_odds", "reward_count", "reward_each_pence", "reward_kind",
               "reward_expiry_days", "reward_trigger")


def clean_terms(terms: dict) -> dict:
    """Validate the optional structured offer terms. Fills reward text/value from count x each when blank."""
    t = {k: terms.get(k) for k in TERM_FIELDS if terms.get(k) not in (None, "")}
    if "min_odds" in t:
        t["min_odds"] = odds_text(parse_odds(t["min_odds"]))
    for k in ("qualifying_stake_pence", "reward_each_pence"):
        if k in t and int(t[k]) <= 0:
            raise UserError("Offer amounts must be more than zero")
    if "reward_count" in t and not 1 <= int(t["reward_count"]) <= 20:
        raise UserError("Number of free bets: between 1 and 20")
    if "reward_expiry_days" in t and not 1 <= int(t["reward_expiry_days"]) <= 365:
        raise UserError("Free bet expiry: between 1 and 365 days")
    if "reward_kind" in t and t["reward_kind"] not in REWARD_KINDS:
        raise UserError("Reward type: SNR, SR or cash")
    if "reward_trigger" in t and t["reward_trigger"] not in REWARD_TRIGGERS:
        raise UserError("Reward trigger: " + ", ".join(REWARD_TRIGGERS))
    if t.get("reward_kind") == "cash":
        t["reward_count"] = 1
    return t


def reward_text(t: dict) -> str:
    if not t.get("reward_each_pence"):
        return ""
    if t.get("reward_kind") == "cash":
        return f"{fmt(t['reward_each_pence'])} cash"
    n = int(t.get("reward_count") or 1)
    return f"{n} × {fmt(t['reward_each_pence']).replace('.00', '')} {(t.get('reward_kind') or 'snr').upper()} free bet" \
           + ("s" if n > 1 else "")


def create_offer(conn, *, title: str, account_id=None, offer_type: str = "sign_up", requirement: str = "",
                 reward: str = "", reward_value: int = 0, starts_at=None, expires_at=None,
                 status: str = "not_started", notes: str = "", terms: dict | None = None) -> int:
    if not (title or "").strip():
        raise UserError("Enter the offer title")
    if offer_type not in OFFER_TYPES or status not in OFFER_STATUSES:
        raise UserError("Unknown offer type or status")
    if account_id:
        _need(conn, "accounts", int(account_id), "Bookmaker")
    t = clean_terms(terms or {})
    if t.get("reward_each_pence"):
        reward = reward.strip() or reward_text(t)
        reward_value = reward_value or int(t.get("reward_count") or 1) * int(t["reward_each_pence"])
    now = times.now_iso()
    cols = ["account_id", "title", "offer_type", "requirement", "reward", "reward_value_pence", "starts_at",
            "expires_at", "status", "notes", "created_at", "updated_at", *t.keys()]
    vals = [int(account_id) if account_id else None, title.strip(), offer_type, requirement.strip(), reward.strip(),
            reward_value, starts_at, expires_at, status, notes.strip(), now, now, *t.values()]
    cur = conn.execute(f"INSERT INTO offers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    audit(conn, "offer", cur.lastrowid, "created", None, {"title": title.strip(), "status": status, **t})
    return cur.lastrowid


def update_offer(conn, offer_id: int, data: dict, note: str = "") -> None:
    before = _need(conn, "offers", offer_id, "Offer")
    fields = ("title", "account_id", "offer_type", "requirement", "reward", "reward_value_pence", "starts_at",
              "expires_at", "status", "reward_credited", "reward_used", "notes", *TERM_FIELDS)
    if any(k in data for k in TERM_FIELDS):
        cleaned = clean_terms({k: data.get(k) for k in TERM_FIELDS})
        data = {**data, **{k: cleaned.get(k) for k in TERM_FIELDS if k in data or k in cleaned}}
    changes = {k: data[k] for k in fields if k in data and data[k] != before[k]}
    if changes.get("status") and changes["status"] not in OFFER_STATUSES:
        raise UserError("Unknown offer status")
    if "status" in changes and changes["status"] not in OFFER_CLOSURES and before["status"] not in OFFER_CLOSURES:
        raise UserError("An offer's status follows its records: record the qualifying bet (or the bookmaker's "
                        "confirmation that it qualified) and the reward when it arrives, and it moves on by itself. "
                        "Only complete, expired or abandoned are set by hand.")
    if changes.get("offer_type") and changes["offer_type"] not in OFFER_TYPES:
        raise UserError("Unknown offer type")
    if "title" in changes and not (changes["title"] or "").strip():
        raise UserError("Enter the offer title")
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE offers SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), times.now_iso(), offer_id))
    audit(conn, "offer", offer_id, "status" if set(changes) == {"status"} else "edited",
          {k: before[k] for k in changes}, changes, note)
    if set(changes) - {"title", "requirement", "reward", "notes", "reward_value_pence"}:
        from . import offer_state
        offer_state.sync(conn, offer_id)


def set_offer_status(conn, offer_id: int, status: str, note: str = "") -> None:
    update_offer(conn, offer_id, {"status": status}, note)


def offer_dependents(conn, offer_id: int) -> dict:
    return {"positions": [r[0] for r in conn.execute("SELECT id FROM positions WHERE offer_id = ? AND deleted_at IS NULL",
                                                     (offer_id,))],
            "free_bets": [r[0] for r in conn.execute("SELECT id FROM free_bets WHERE offer_id = ? AND deleted_at IS NULL",
                                                     (offer_id,))],
            "entries": [r[0] for r in conn.execute("SELECT id FROM ledger_entries WHERE offer_id = ? AND superseded_at IS NULL",
                                                   (offer_id,))]}


def delete_offer(conn, offer_id: int, note: str = "") -> None:
    """Remove an offer recorded by mistake (kept in the audit trail). Refused while anything refers to it."""
    offer = _need(conn, "offers", offer_id, "Offer")
    deps = offer_dependents(conn, offer_id)
    if any(deps.values()):
        raise UserError("Positions, free bets or ledger entries refer to this offer; close it (abandoned) instead")
    conn.execute("UPDATE offers SET deleted_at = ?, updated_at = ? WHERE id = ?", (times.now_iso(), times.now_iso(), offer_id))
    audit(conn, "offer", offer_id, "removed", offer, None, note)


def create_free_bet(conn, *, account_id: int, face_value: int, kind: str = "snr", offer_id=None,
                    credited_at=None, expires_at=None, notes: str = "") -> int:
    acc = _need(conn, "accounts", int(account_id), "Bookmaker")
    if acc["kind"] == "bank":
        raise UserError("Free bets belong to a bookmaker or exchange")
    if face_value <= 0:
        raise UserError("Free bet value must be more than zero")
    if kind not in ("snr", "sr"):
        raise UserError("Choose SNR or SR")
    now = times.now_iso()
    cur = conn.execute("INSERT INTO free_bets (account_id, offer_id, face_value_pence, kind, credited_at, expires_at, notes, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (acc["id"], int(offer_id) if offer_id else None, face_value, kind, credited_at or now,
                        expires_at, notes.strip(), now, now))
    fid = cur.lastrowid
    audit(conn, "free_bet", fid, "credited", None, {"account": acc["name"], "face_value_pence": face_value,
                                                    "kind": kind, "offer_id": offer_id})
    if offer_id:
        from . import offer_state
        offer_state.sync(conn, int(offer_id), note=f"free bet {fid} credited")
    return fid


def create_free_bets(conn, *, count: int = 1, **kw) -> list[int]:
    """Several identical free bets at once (e.g. 3 x £10 SNR)."""
    if not 1 <= int(count) <= 20:
        raise UserError("How many: between 1 and 20")
    return [create_free_bet(conn, **kw) for _ in range(int(count))]


def receive_reward(conn, offer_id: int, *, at: str | None = None, expires_at: str | None = None,
                   count: int | None = None, each: int | None = None, kind: str | None = None) -> list[int]:
    """The offer's reward arrived: create its free bets (or post the cash reward) from the offer's terms,
    which the caller may override. Returns the new free bet ids (empty for cash)."""
    from datetime import timedelta
    offer = _need(conn, "offers", offer_id, "Offer")
    if offer["status"] in ("complete", "expired", "abandoned"):
        raise UserError("This offer is closed")
    kind = kind or offer["reward_kind"]
    each = each or offer["reward_each_pence"]
    count = count or offer["reward_count"] or 1
    if not kind or not each:
        raise UserError("Add the reward to the offer first (how many, worth how much, SNR/SR or cash)")
    if not offer["account_id"]:
        raise UserError("Choose the offer's bookmaker first")
    at = at or times.now_iso()
    if kind == "cash":
        add_manual_entry(conn, account_id=offer["account_id"], amount=int(each) * int(count), category="promotion_reward",
                         at=at, description=f"Reward: {offer['title']}", offer_id=offer_id)
        from . import offer_state
        offer_state.sync(conn, offer_id, note="cash reward received")
        return []
    if not expires_at and offer["reward_expiry_days"]:
        expires_at = times.iso(times.parse(at) + timedelta(days=int(offer["reward_expiry_days"])))
    return create_free_bets(conn, count=int(count), account_id=offer["account_id"], face_value=int(each), kind=kind,
                            offer_id=offer_id, credited_at=at, expires_at=expires_at)


def refresh_offer_progress(conn, offer_id: int | None) -> None:
    """Move an offer on from what its free bets show: converting once one is used, complete once none is left
    to use and every bet that used one has settled. Never touches offers you closed or set by hand earlier."""
    from . import offer_state                        # the one offer lifecycle (bets/offer_state.py)
    offer_state.sync(conn, offer_id)


def remove_free_bet(conn, free_bet_id: int, note: str = "") -> None:
    """Withdraw a free bet recorded by mistake (only while unused; kept in the audit trail)."""
    fb = _need(conn, "free_bets", free_bet_id, "Free bet")
    if token_in_use(conn, free_bet_id):
        raise UserError("That free bet is linked to a bet; remove the bet first")
    conn.execute("UPDATE free_bets SET deleted_at = ?, updated_at = ? WHERE id = ?",
                 (times.now_iso(), times.now_iso(), free_bet_id))
    audit(conn, "free_bet", free_bet_id, "removed", fb, None, note)


def update_free_bet(conn, free_bet_id: int, data: dict, note: str = "") -> None:
    before = _need(conn, "free_bets", free_bet_id, "Free bet")
    if data.get("offer_id"):
        offer = _need(conn, "offers", int(data["offer_id"]), "Offer")
        if offer["account_id"] and offer["account_id"] != before["account_id"]:
            raise UserError("That offer is from a different bookmaker")
        data = {**data, "offer_id": int(data["offer_id"])}
    fields = ("face_value_pence", "kind", "expires_at", "status", "notes", "offer_id")
    changes = {k: data[k] for k in fields if k in data and data[k] != before[k]}
    if changes.get("status") and changes["status"] not in ("available", "used", "expired", "void"):
        raise UserError("Unknown free bet status")
    if "face_value_pence" in changes and changes["face_value_pence"] <= 0:
        raise UserError("Free bet value must be more than zero")
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE free_bets SET {sets}, updated_at = ? WHERE id = ?",
                 (*changes.values(), times.now_iso(), free_bet_id))
    audit(conn, "free_bet", free_bet_id, "edited", {k: before[k] for k in changes}, changes, note)
    if "status" in changes:
        refresh_offer_progress(conn, changes.get("offer_id") or before["offer_id"])


def expire_overdue(conn) -> int:
    """Mark available free bets and live offers past their expiry as expired (audited)."""
    now = times.now_iso()
    n = 0
    for fb in conn.execute("SELECT * FROM free_bets WHERE status = 'available' AND deleted_at IS NULL "
                           "AND expires_at IS NOT NULL AND expires_at < ?", (now,)).fetchall():
        conn.execute("UPDATE free_bets SET status = 'expired', updated_at = ? WHERE id = ?", (now, fb["id"]))
        audit(conn, "free_bet", fb["id"], "expired", {"status": "available"}, {"status": "expired"}, "passed expiry")
        refresh_offer_progress(conn, fb["offer_id"])
        n += 1
    for of in conn.execute("SELECT * FROM offers WHERE status IN ('not_started', 'qualifying') AND deleted_at IS NULL "
                           "AND expires_at IS NOT NULL AND expires_at < ?", (now,)).fetchall():
        set_offer_status(conn, of["id"], "expired", note="passed expiry before qualifying")
        n += 1
    return n


def free_bet_value_realised(conn, free_bet_id: int) -> int | None:
    leg = conn.execute("SELECT position_id FROM legs WHERE free_bet_id = ? AND deleted_at IS NULL", (free_bet_id,)).fetchone()
    if not leg:
        return None
    pos = row(conn, "positions", leg[0])
    return position_pnl(conn, pos["id"]) if pos["status"] != "open" else None


def offer_realised(conn, offer_id: int) -> int:
    """Profit from everything linked to an offer: its positions, positions that used its free bets,
    and cash rewards posted against it."""
    positions = {r[0] for r in conn.execute("SELECT id FROM positions WHERE offer_id = ? AND deleted_at IS NULL", (offer_id,))}
    positions |= {r[0] for r in conn.execute(
        "SELECT l.position_id FROM legs l JOIN free_bets f ON f.id = l.free_bet_id "
        "WHERE f.offer_id = ? AND l.deleted_at IS NULL", (offer_id,))}
    total = sum(position_pnl(conn, p) for p in positions)
    total += conn.execute("SELECT coalesce(sum(amount_pence), 0) FROM ledger_entries WHERE offer_id = ? "
                          "AND position_id IS NULL AND class = 'pnl' AND superseded_at IS NULL", (offer_id,)).fetchone()[0]
    return total


# ---------------------------------------------------------------- manual entries, reconciliation

def add_manual_entry(conn, *, account_id: int, amount: int, category: str, at: str | None = None,
                     description: str = "", offer_id=None, position_id=None) -> int:
    if category not in MANUAL_CATEGORIES:
        raise UserError("Choose a valid entry type")
    if amount == 0:
        raise UserError("Amount cannot be zero")
    acc = _need(conn, "accounts", int(account_id), "Account")
    if acc["kind"] == "bank":
        raise UserError("Bank accounts only change through transfers")
    if not description.strip():
        raise UserError("Add a short description so the ledger stays auditable")
    eid = post(conn, account_id=acc["id"], amount=amount, category=category, at=at or times.now_iso(),
               description=description.strip(), offer_id=int(offer_id) if offer_id else None,
               position_id=int(position_id) if position_id else None)
    audit(conn, "ledger", eid, "manual_entry", None, {"account": acc["name"], "amount_pence": amount,
                                                      "category": category, "description": description.strip()})
    if offer_id and category in ("promotion_reward", "bonus_cash"):
        update_offer(conn, int(offer_id), {"reward_credited": 1}, note=f"cash reward entry {eid}")
    return eid


def reverse_entry(conn, entry_id: int, note: str) -> None:
    """Withdraw a manual entry. System postings change via their bet/transfer instead."""
    e = row(conn, "ledger_entries", entry_id)
    if not e or e["superseded_at"]:
        raise UserError("Entry not found")
    if e["posting_key"]:
        raise UserError("This entry belongs to a bet or transfer; change that instead")
    if not (note or "").strip():
        raise UserError("Say why the entry is being reversed")
    conn.execute("UPDATE ledger_entries SET superseded_at = ?, supersede_note = ? WHERE id = ?",
                 (times.now_iso(), note.strip(), entry_id))
    audit(conn, "ledger", entry_id, "reversed", e, None, note.strip())


def expected_balance(conn, account_id: int, basis: str = "total") -> dict:
    """What the app expects an account to show. 'total' = all funds in the account; 'available' = an exchange's free
    cash (total less the lay liability it holds back). Bookmakers have no difference between the two."""
    from . import metrics
    acc = _need(conn, "accounts", account_id, "Account")
    if basis not in ("total", "available"):
        raise UserError("Say whether the figure is the available balance or the total")
    from . import plans
    total = balance(conn, account_id)
    liability = metrics.open_lay_liability(conn, account_id) if acc["kind"] == "exchange" else 0
    orders = plans.unmatched_order_reserve(conn, account_id) if acc["kind"] == "exchange" else 0
    available = total - liability - orders              # exchanges also hold liability for unmatched lay orders
    return {"total": total, "liability": liability, "unmatched_order_reserve": orders, "available": available,
            "expected": available if basis == "available" else total}


def reconcile(conn, account_id: int, actual_balance: int, *, note: str = "", at: str | None = None,
              basis: str = "total", adjust: bool = True) -> int | None:
    """Record the real balance. `basis` says which figure it is ('available' for an exchange's free cash).
    A difference becomes a 'reconciliation' P&L entry only when `adjust` is true; otherwise it is recorded as
    unresolved (shown as an issue) and profit is untouched."""
    acc = _need(conn, "accounts", account_id, "Account")
    if acc["kind"] == "bank":
        raise UserError("Bank accounts are not reconciled here")
    exp = expected_balance(conn, account_id, basis)
    expected = exp["expected"]
    diff = actual_balance - expected
    at = at or times.now_iso()
    entry_id = None
    label = "available" if basis == "available" else "balance"
    if diff and adjust:
        entry_id = post(conn, account_id=account_id, amount=diff, category="reconciliation", at=at,
                        description=f"Reconciliation adjustment: {label} {actual_balance / 100:.2f} "
                                    f"(app expected {expected / 100:.2f})" + (f": {note.strip()}" if note.strip() else ""))
    outcome = "matched" if not diff else ("adjusted" if adjust else "unresolved")
    cur = conn.execute("INSERT INTO reconciliations (account_id, app_balance_pence, actual_balance_pence, difference_pence, "
                       "entry_id, note, at, basis, liability_pence, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (account_id, expected, actual_balance, diff, entry_id, note.strip(), at, basis, exp["liability"],
                        outcome))
    audit(conn, "account", account_id, "reconciled", {"app_expected_pence": expected, "basis": basis,
                                                      "liability_pence": exp["liability"]},
          {"actual_balance_pence": actual_balance, "difference_pence": diff, "outcome": outcome,
           "reconciliation_id": cur.lastrowid}, note)
    return entry_id


def last_reconciliation_id(conn, account_id: int) -> int | None:
    r = conn.execute("SELECT id FROM reconciliations WHERE account_id = ? ORDER BY id DESC LIMIT 1", (account_id,)).fetchone()
    return r[0] if r else None


def reverse_reconciliation(conn, reconciliation_id: int, note: str) -> bool:
    """Withdraw a balance check's P&L adjustment (kept in the history). Transfers, bets and every other entry are
    untouched. Returns False if there was nothing left to withdraw."""
    r = row(conn, "reconciliations", reconciliation_id)
    if not r:
        raise UserError("Balance check not found")
    if not (note or "").strip():
        raise UserError("Say why the balance check is being reversed")
    changed = False
    if r["entry_id"]:
        e = row(conn, "ledger_entries", r["entry_id"])
        if e and not e["superseded_at"]:
            conn.execute("UPDATE ledger_entries SET superseded_at = ?, supersede_note = ? WHERE id = ?",
                         (times.now_iso(), "reconciliation reversed: " + note.strip(), e["id"]))
            audit(conn, "ledger", e["id"], "reversed", e, None, "reconciliation reversed: " + note.strip())
            changed = True
    if r["outcome"] != "reversed" and (r["entry_id"] or r["outcome"] == "adjusted"):
        conn.execute("UPDATE reconciliations SET outcome = 'reversed', reversed_at = ?, reversal_note = ? WHERE id = ?",
                     (times.now_iso(), note.strip()[:500], reconciliation_id))
        audit(conn, "reconciliation", reconciliation_id, "reversed", {"outcome": r["outcome"]}, {"outcome": "reversed"},
              note.strip())
        changed = True
    return changed


def resolve_reconciliation(conn, reconciliation_id: int, action: str, note: str) -> None:
    """An unresolved difference: record it as a P&L adjustment ('adjust') or close it without one ('dismiss')."""
    r = row(conn, "reconciliations", reconciliation_id)
    if not r:
        raise UserError("Balance check not found")
    if r["outcome"] != "unresolved":
        raise UserError("That balance check has no unresolved difference")
    if action not in ("adjust", "dismiss"):
        raise UserError("Record the adjustment or dismiss the difference")
    if not (note or "").strip():
        raise UserError("Say why")
    entry_id = None
    if action == "adjust":
        label = "available" if r["basis"] == "available" else "balance"
        entry_id = post(conn, account_id=r["account_id"], amount=r["difference_pence"], category="reconciliation",
                        at=r["at"], description=f"Reconciliation adjustment: {label} "
                                                f"{r['actual_balance_pence'] / 100:.2f} (app expected "
                                                f"{r['app_balance_pence'] / 100:.2f}): {note.strip()}")
    outcome = "adjusted" if action == "adjust" else "dismissed"
    conn.execute("UPDATE reconciliations SET outcome = ?, entry_id = coalesce(?, entry_id), note = CASE WHEN note = '' "
                 "THEN ? ELSE note || ' · ' || ? END WHERE id = ?", (outcome, entry_id, note.strip(), note.strip(),
                                                                       reconciliation_id))
    audit(conn, "reconciliation", reconciliation_id, outcome, {"outcome": "unresolved"},
          {"outcome": outcome, "entry_id": entry_id}, note.strip())


def update_reconciliation_note(conn, reconciliation_id: int, note_text: str, note: str = "") -> None:
    r = row(conn, "reconciliations", reconciliation_id)
    if not r:
        raise UserError("Balance check not found")
    if (note_text or "").strip() == r["note"]:
        return
    conn.execute("UPDATE reconciliations SET note = ? WHERE id = ?", (note_text.strip()[:500], reconciliation_id))
    audit(conn, "reconciliation", reconciliation_id, "edited", {"note": r["note"]}, {"note": note_text.strip()[:500]}, note)


def get_setting(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))
