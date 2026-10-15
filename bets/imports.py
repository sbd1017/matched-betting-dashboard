"""One pipeline for every way a bet gets in:

    input (Quick Add form, JSON from a future iPhone Shortcut, pasted text, CSV, OCR...)
      -> normalise()  validate + resolve accounts; nothing trusted yet
      -> preview()    figures the user checks (lay stake, liability, expected result)
      -> user confirms
      -> apply()      the normal services (create_position / add_leg), one transaction

Imported candidates are stored as ``needs_confirmation`` and never touch balances until
the user confirms them. A future parser only has to produce the raw dict accepted here.

Besides positions, a candidate can be an account, a balance check, a transfer, an offer, a free
bet or a note (``KINDS``). Each kind has a normaliser and, on confirmation, calls the same
services the forms use. The Assistant Intake API (assistant.py) only ever reaches this module.
"""
from __future__ import annotations

import contextvars
import json
import secrets
import sqlite3
from datetime import timedelta
from decimal import Decimal

from . import calc, commission, services, times
from .money import commission_text, fmt, odds_text, parse_commission, parse_odds, plain, to_pence

LARGE_STAKE = 200_00
LARGE_AMOUNT = 1_000_00                  # transfers / balances worth a second look
SOURCES = ("manual", "quick_add", "api", "shortcut", "paste", "csv", "ocr", "assistant")
KINDS = ("position", "account", "balance", "transfer", "offer", "free_bet", "note", "settlement", "reward", "arrival",
         "account_status", "amend", "reverse")
CORRECTIONS = ("amend", "reverse")                       # always wait for the user, even in Trusted Assistant mode
TRUSTABLE = tuple(k for k in KINDS if k not in CORRECTIONS)
KIND_TITLES = {"position": "Matched position", "account": "New account", "balance": "Balance check",
               "transfer": "Transfer", "offer": "Offer", "free_bet": "Free bet", "note": "Note",
               "settlement": "Settlement", "reward": "Reward arrived", "arrival": "Withdrawal arrived",
               "account_status": "Account standing", "amend": "Correction", "reverse": "Undo"}
MAX_RAW = 16_000


class CandidateError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# References to entities that are themselves still waiting for confirmation (e.g. a transfer into an account the
# assistant submitted a moment ago). Allowed only for stored candidates (store / edit / decide / dry runs), never
# for Quick Add; recorded as `depends_on`, and resolved again when confirming, which runs in dependency order.
_PENDING: contextvars.ContextVar[list | None] = contextvars.ContextVar("pending_refs", default=None)


def _pending_candidate(conn, kind: str, field: str, value) -> dict | None:
    deps = _PENDING.get()
    if deps is None or value in (None, "") or isinstance(value, int) or str(value).strip().isdigit():
        return None
    for (cj,) in conn.execute("SELECT candidate_json FROM import_candidates WHERE status = 'needs_confirmation' "
                              "AND kind = ? ORDER BY id", (kind,)):
        c = json.loads(cj)
        if str(c.get(field, "")).strip().lower() == str(value).strip().lower():
            deps.append(f"{'account' if kind == 'account' else 'offer'} '{c.get(field)}'")
            return c
    return None


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes", "on")


def _pending_token(conn, bookmaker: str, value: int, kind: str) -> dict | None:
    """A free-bet token still waiting for confirmation that would suit this bet (confirmed first in bulk)."""
    deps = _PENDING.get()
    if deps is None:
        return None
    for (cj,) in conn.execute("SELECT candidate_json FROM import_candidates WHERE status = 'needs_confirmation' "
                              "AND kind = 'free_bet' ORDER BY id"):
        c = json.loads(cj)
        if (str(c.get("bookmaker", "")).lower(), c.get("face_value_pence"), c.get("kind")) == (bookmaker.lower(), value, kind):
            deps.append(f"free bet '{fmt(value)} {kind.upper()} at {c['bookmaker']}'")
            return c
    return None


def _account_or_pending(conn, value) -> dict | None:
    acc = _account(conn, value)
    if acc:
        return acc
    c = _pending_candidate(conn, "account", "name", value)
    return {"id": None, "name": c["name"], "kind": c["account_type"], "commission_bp": c["commission_bp"],
            "pending": True} if c else None


def _offer_or_pending(conn, value) -> dict | None:
    found = _offer(conn, value)
    if found:
        return found
    c = _pending_candidate(conn, "offer", "title", value)
    return {"id": None, "title": c["title"], "pending": True} if c else None


def _account(conn, value) -> dict | None:
    if value in (None, ""):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        r = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(value),)).fetchone()
    else:
        r = conn.execute("SELECT * FROM accounts WHERE name = ? COLLATE NOCASE", (str(value).strip(),)).fetchone()
    return dict(r) if r else None


def _money(value, label, errors, *, required=True, allow_zero=False, allow_negative=False):
    if value in (None, ""):
        if required:
            errors.append(f"{label}: missing")
        return None
    try:
        if isinstance(value, float):
            text = repr(value)               # 10.5 -> '10.5'; 10.123 is refused (3 decimals)
        elif isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            text = format(value, "f")
        else:
            text = value
        return to_pence(text, allow_zero=allow_zero, allow_negative=allow_negative)
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return None


def _odds(value, label, errors):
    try:
        return parse_odds(value)
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return None


def _time(value, label, errors, happened=False):
    if value in (None, ""):
        return None
    try:
        return times.from_local_input(str(value), happened=happened)
    except ValueError:
        errors.append(f"{label}: not a valid date/time")
        return None


def normalise(conn, raw: dict, source: str = "manual") -> tuple[dict, list[str]]:
    """Validate untrusted input. Returns (candidate, warnings); raises CandidateError."""
    if not isinstance(raw, dict):
        raise CandidateError(["Expected an object"])
    errors: list[str] = []
    warnings: list[str] = []
    source = source if source in SOURCES else "api"
    kind = raw.get("position_type") or raw.get("kind") or "qualifying"
    if kind not in ("qualifying", "free_bet_snr", "free_bet_sr", "other"):
        errors.append("kind: unknown position type")
    event = str(raw.get("event") or "").strip()[:160]
    position_id = raw.get("position_id") if raw.get("position_id") not in (None, "") else raw.get("add_to_position_id")
    if position_id not in (None, ""):
        pos = services.row(conn, "positions", int(position_id)) if str(position_id).isdigit() else None
        if not pos or pos["deleted_at"]:
            errors.append("position_id: no such position")
        position_id = int(position_id) if pos else None
    else:
        position_id = None
        if not event:
            errors.append("event: missing")
    offer_id = raw.get("offer_id") or None
    if offer_id is not None and not (str(offer_id).isdigit() and services.row(conn, "offers", int(offer_id))):
        errors.append("offer_id: no such offer")
    legs_in = raw.get("legs")
    if not isinstance(legs_in, list) or not legs_in:
        errors.append("legs: at least one bet is needed")
        legs_in = []
    if len(legs_in) > 12:
        errors.append("legs: too many bets in one position")
        legs_in = legs_in[:12]
    legs = []
    for i, leg in enumerate(legs_in, 1):
        if not isinstance(leg, dict):
            errors.append(f"bet {i}: expected an object")
            continue
        label = f"bet {i}"
        acc = _account_or_pending(conn, leg.get("account") if leg.get("account") is not None else leg.get("account_id"))
        side = leg.get("side")
        if side not in ("back", "lay"):
            errors.append(f"{label}: side must be back or lay")
        if not acc:
            errors.append(f"{label}: unknown bookmaker/exchange '{leg.get('account') or leg.get('account_id') or ''}'"
                          " (add it under Accounts first)")
        elif acc["kind"] == "bank":
            errors.append(f"{label}: {acc['name']} is a bank account")
        elif side == "lay" and acc["kind"] != "exchange":
            errors.append(f"{label}: lay bets go on an exchange, not {acc['name']}")
        stake = _money(leg.get("stake"), f"{label} stake", errors, required=side != "lay")
        odds = _odds(leg.get("odds"), f"{label} odds", errors)
        commission_bp = None
        if leg.get("commission") not in (None, ""):
            try:
                commission_bp = parse_commission(leg.get("commission"))
            except ValueError as exc:
                errors.append(f"{label} commission: {exc}")
        elif acc and acc["kind"] == "exchange":
            commission_bp = acc["commission_bp"]
        if side == "lay" and acc and acc["kind"] == "exchange":
            stale = commission.stale_warning(acc, commission_bp)
            if stale:
                warnings.append(f"{label}: {stale}")
        # Free bets: the token is chosen when confirming (services.choose_token), not now, so several pending
        # positions never claim the same token; an explicitly linked token always takes precedence.
        free_bet_id = leg.get("free_bet_id") or None
        token_kind = services.FREE_BET_KINDS.get(kind)
        marked_used = _truthy(leg.get("token_marked_used"))
        uses_free_bet = side == "back" and (free_bet_id is not None or _truthy(leg.get("use_free_bet"))
                                            or token_kind is not None)
        if uses_free_bet and free_bet_id is None and token_kind is None:
            errors.append(f"{label}: say whether the free bet is SNR or SR (position_type free_bet_snr / free_bet_sr)")
        if free_bet_id is not None:
            fb = services.row(conn, "free_bets", int(free_bet_id)) if str(free_bet_id).isdigit() else None
            if not fb or fb["deleted_at"]:
                errors.append(f"{label}: free bet not found")
            elif acc and acc["id"] and fb["account_id"] != acc["id"]:
                errors.append(f"{label}: that free bet belongs to a different bookmaker")
            elif services.token_in_use(conn, fb["id"]):
                errors.append(f"{label}: that free bet is already linked to another bet")
            elif fb["status"] != "available" and not (fb["status"] == "used" and marked_used):
                errors.append(f"{label}: free bet is not available")
            free_bet_id = int(free_bet_id) if fb else None
        elif uses_free_bet and token_kind and acc and stake:
            found = acc["id"] and services.choose_token(conn, account_id=acc["id"], value=stake, kind=token_kind,
                                                        marked_used=marked_used)
            if not found and (marked_used or not _pending_token(conn, acc["name"], stake, token_kind)):
                warnings.append(f"{label}: no unused {fmt(stake)} {token_kind.upper()} free bet"
                                f"{' marked used by hand' if marked_used else ''} at {acc['name']} to link yet — "
                                "it is chosen when you confirm, so add or confirm the token first")
        placed_at = _time(leg.get("placed_at"), f"{label} placed time", errors, happened=True)
        if stake and stake >= LARGE_STAKE:
            warnings.append(f"{label}: stake {fmt(stake)} is unusually large — check it")
        if odds and odds > 50:
            warnings.append(f"{label}: odds {odds_text(odds)} are unusually long — check them")
        if times.in_future(placed_at):
            warnings.append(f"{label}: placed time is in the future")
        legs.append({"account_id": acc["id"] if acc else None, "account": acc["name"] if acc else None,
                     "side": side, "stake_pence": stake, "odds": odds_text(odds) if odds else None,
                     "commission_bp": commission_bp or 0, "free_bet_id": free_bet_id, "placed_at": placed_at,
                     "uses_free_bet": uses_free_bet, "token_marked_used": marked_used and uses_free_bet,
                     "external_ref": str(leg.get("external_ref") or "")[:120], "source": source})
    _fill_lay_stake(conn, kind, legs, errors, warnings)
    offer_ref = None
    if offer_id is None and raw.get("offer"):
        found = _offer_or_pending(conn, raw.get("offer"))
        if not found:
            errors.append(f"offer: no offer called '{str(raw.get('offer'))[:80]}'")
        offer_id = found["id"] if found else None
        offer_ref = found["title"] if found and found["id"] is None else None
    event_at = _time(raw.get("event_at"), "event time", errors)
    if event_at and event_at < times.now_iso() and not position_id:
        warnings.append("event time is in the past")
    hedge = str(raw.get("hedge_strategy") or "").strip().lower() or None
    reason = str(raw.get("unhedged_reason") or "").strip()[:300]
    if hedge and hedge not in services.HEDGE_STRATEGIES:
        errors.append("hedge_strategy: matched, intentionally_unhedged or pending")
    if hedge == "intentionally_unhedged":
        if any(l["side"] == "lay" for l in legs):
            errors.append("hedge_strategy: intentionally_unhedged means no lay; remove the lay or say matched")
        if any(l.get("uses_free_bet") or l.get("free_bet_id") for l in legs):
            errors.append("hedge_strategy: a free-bet back bet cannot be left unhedged by design")
        if kind not in ("qualifying", "other"):
            errors.append("hedge_strategy: only a qualifying (cash) bet can be left unhedged by design")
        if not reason:
            errors.append("unhedged_reason: say why it is deliberately not laid (e.g. the stake is only 5p)")
    if errors:
        raise CandidateError(errors)
    candidate = {"position_id": position_id, "event": event, "market": str(raw.get("market") or "").strip()[:120],
                 "selection": str(raw.get("selection") or "").strip()[:120], "event_at": event_at, "kind": kind,
                 "offer_id": int(offer_id) if offer_id else None, "notes": str(raw.get("notes") or "").strip()[:2000],
                 "source": source, "legs": legs}
    if hedge:
        candidate["hedge_strategy"] = hedge
        if hedge == "intentionally_unhedged":
            candidate["unhedged_reason"] = reason
    if offer_ref:
        candidate["offer_ref"] = offer_ref                    # a pending offer, resolved when confirming
    candidate["expected_pnl_pence"] = preview(conn, candidate)["expected"]
    warnings += sanity_warnings(conn, candidate)
    return candidate, warnings


QUALIFYING_LOSS_WARN = Decimal("0.10")     # a qualifying loss above 10% of the stake usually means the wrong odds
RETENTION_WARN = Decimal("0.60")           # keeping less than 60% of a free bet usually means poor odds or a typo


def sanity_warnings(conn, c: dict) -> list[str]:
    """Things that are allowed but usually a mistake: odds below the offer's minimum, a stake below its requirement,
    an unusually big qualifying loss or low free-bet retention, a lay liability bigger than the exchange holds."""
    out = []
    for l in c["legs"]:
        acc = services.row(conn, "accounts", l["account_id"]) if l.get("account_id") else None
        if acc and acc["standing"] != "ok":
            out.append(f"{acc['name']} is marked {acc['standing']}: check the bet was accepted at full stake")
    backs = [l for l in c["legs"] if l["side"] == "back" and l.get("odds")]
    lays = [l for l in c["legs"] if l["side"] == "lay" and l.get("odds")]
    offer = services.row(conn, "offers", c["offer_id"]) if c.get("offer_id") else None
    if offer and c["kind"] == "qualifying":
        for b in backs:
            if offer["min_odds"] and Decimal(b["odds"]) < Decimal(offer["min_odds"]):
                out.append(f"back odds {b['odds']} are below the offer's minimum {offer['min_odds']}: it may not qualify")
            if offer["qualifying_stake_pence"] and b["stake_pence"] and b["stake_pence"] < offer["qualifying_stake_pence"]:
                out.append(f"back stake {fmt(b['stake_pence'])} is below the offer's qualifying stake "
                           f"{fmt(offer['qualifying_stake_pence'])}")
    expected = c.get("expected_pnl_pence")
    if expected is not None and len(backs) == 1 and backs[0]["stake_pence"]:
        stake = Decimal(backs[0]["stake_pence"])
        if c["kind"] == "qualifying" and expected < 0 and Decimal(-expected) > stake * QUALIFYING_LOSS_WARN:
            out.append(f"qualifying loss {fmt(-expected)} is more than {int(QUALIFYING_LOSS_WARN * 100)}% of the stake: "
                       "check the back and lay odds")
        if c["kind"] in services.FREE_BET_KINDS and Decimal(expected) < stake * RETENTION_WARN:
            out.append(f"you'd keep only {fmt(expected)} of a {fmt(int(stake))} free bet: check the odds")
    for l in lays:
        if not l.get("account_id") or not l.get("stake_pence"):
            continue
        if not conn.execute("SELECT 1 FROM ledger_entries WHERE account_id = ? AND superseded_at IS NULL LIMIT 1",
                            (l["account_id"],)).fetchone():
            continue                             # the app isn't tracking this exchange's money yet
        from . import metrics
        free = services.balance(conn, l["account_id"]) - metrics.open_lay_liability(conn, l["account_id"])
        need = calc.lay_liability(l["stake_pence"], Decimal(l["odds"]))
        if need > free:
            out.append(f"liability {fmt(need)} is more than the {fmt(max(free, 0))} available on {l['account']}")
    return out


def _fill_lay_stake(conn, kind, legs, errors, warnings) -> None:
    """A lay sent without a stake gets the app's own figure (never the sender's arithmetic)."""
    missing = [l for l in legs if l["side"] == "lay" and l["stake_pence"] is None]
    if not missing:
        return
    backs = [l for l in legs if l["side"] == "back"]
    lay = missing[0]
    if len(missing) > 1 or len(backs) != 1 or not backs[0]["stake_pence"] or not backs[0]["odds"] or not lay["odds"]:
        errors.append("lay stake: missing (the app can only calculate it for one back bet and one lay)")
        return
    back = backs[0]
    calc_kind = kind if kind in calc.KINDS else "qualifying"
    if calc_kind == "qualifying" and back["free_bet_id"]:
        fb = services.row(conn, "free_bets", back["free_bet_id"])
        calc_kind = f"free_bet_{fb['kind']}"
    m = calc.match(calc_kind, back["stake_pence"], Decimal(back["odds"]), Decimal(lay["odds"]), lay["commission_bp"])
    lay["stake_pence"] = m.lay_stake
    warnings.append(f"lay stake calculated by the app: {fmt(m.lay_stake)} (liability {fmt(m.liability)})")


def preview(conn, candidate: dict) -> dict:
    """Figures to show before saving. Uses the calculator when there is one back and one lay."""
    from .calc import lay_liability
    legs = []
    for leg in candidate["legs"]:
        d = dict(leg)
        d["liability"] = lay_liability(leg["stake_pence"], Decimal(leg["odds"])) if leg["side"] == "lay" else None
        d["commission"] = commission_text(leg["commission_bp"]) if leg["side"] == "lay" else ""
        legs.append(d)
    backs = [l for l in candidate["legs"] if l["side"] == "back"]
    lays = [l for l in candidate["legs"] if l["side"] == "lay"]
    result = {"legs": legs, "match": None, "expected": None, "similar": []}
    if len(backs) == 1 and len(lays) == 1:
        kind = candidate["kind"] if candidate["kind"] in calc.KINDS else "qualifying"
        m = calc.match(kind, backs[0]["stake_pence"], Decimal(backs[0]["odds"]), Decimal(lays[0]["odds"]),
                       lays[0]["commission_bp"])
        # actual lay figures as entered (may differ from the suggestion)
        lay_stake = lays[0]["stake_pence"]
        if_back, if_lay = outcomes(candidate)
        result.update(match=m, if_back=if_back, if_lay=if_lay, expected=min(if_back, if_lay),
                      lay_differs=lay_stake != m.lay_stake)
    if candidate.get("position_id") is None and candidate["event"]:
        result["similar"] = [dict(r) for r in conn.execute(
            "SELECT id, event, selection, status FROM positions WHERE deleted_at IS NULL AND status = 'open' "
            "AND event = ? COLLATE NOCASE ORDER BY id DESC LIMIT 5", (candidate["event"],))]
    return result


def outcomes(candidate: dict) -> tuple[int, int]:
    """Net result if the back selection wins / loses, from the legs exactly as entered.

    A back leg is a free bet when the position is a free-bet position or the leg uses one;
    SNR free bets pay winnings only, SR also return the stake; neither costs cash if lost."""
    from .money import commission_fraction, round_pence
    position_fb = {"free_bet_snr": "snr", "free_bet_sr": "sr"}.get(candidate["kind"])
    win = lose = 0
    for leg in candidate["legs"]:
        stake, odds = leg["stake_pence"], Decimal(leg["odds"])
        if leg["side"] == "back":
            free = position_fb or ("snr" if leg.get("free_bet_id") or leg.get("uses_free_bet") else None)
            if free == "sr":
                win += round_pence(Decimal(stake) / 100 * odds)
            elif free == "snr":
                win += round_pence(Decimal(stake) / 100 * (odds - 1))
            else:
                win += round_pence(Decimal(stake) / 100 * (odds - 1))
                lose -= stake
        else:
            win -= calc.lay_liability(stake, odds)
            lose += stake - round_pence(Decimal(stake) / 100 * commission_fraction(leg["commission_bp"]))
    return win, lose


def apply(conn, candidate: dict) -> tuple[int, list[int]]:
    """Create the position (or add legs to an existing one) through the normal services. Free-bet backs get their
    token now: same bookmaker, value and SNR/SR type, unlinked, earliest expiry / oldest / lowest id first."""
    legs, chosen = [], []
    for full in candidate["legs"]:
        leg = {k: v for k, v in full.items() if k not in ("account", "uses_free_bet")}
        if full.get("uses_free_bet") and not full.get("free_bet_id"):
            kind = services.FREE_BET_KINDS.get(candidate["kind"])
            fb = kind and services.choose_token(conn, account_id=full["account_id"], value=full["stake_pence"], kind=kind,
                                                marked_used=bool(full.get("token_marked_used")), exclude=tuple(chosen))
            if not fb:
                raise services.UserError(
                    f"No unused {fmt(full['stake_pence'])} {(kind or 'free bet').upper()} free bet"
                    f"{' marked used by hand' if full.get('token_marked_used') else ''} at {full['account']} to link: "
                    "add or confirm the token first")
            leg["free_bet_id"] = fb["id"]
            chosen.append(fb["id"])
        legs.append(leg)
    if candidate.get("position_id"):
        pid = candidate["position_id"]
        ids = [services.add_leg(conn, pid, leg) for leg in legs]
        return pid, ids
    pid = services.create_position(conn, event=candidate["event"], market=candidate["market"],
                                   selection=candidate["selection"], event_at=candidate["event_at"],
                                   kind=candidate["kind"], offer_id=candidate["offer_id"],
                                   expected_pnl=candidate.get("expected_pnl_pence"), notes=candidate["notes"],
                                   source=candidate["source"], legs=legs, hedge_strategy=candidate.get("hedge_strategy"),
                                   unhedged_reason=candidate.get("unhedged_reason") or "")
    ids = [r[0] for r in conn.execute("SELECT id FROM legs WHERE position_id = ? ORDER BY id", (pid,))]
    return pid, ids


# ---------------------------------------------------------------- other candidate kinds

def _text(value, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _account_named(conn, value) -> dict | None:
    """Account by id or name (case-insensitive), or a pending account candidate (stored candidates only)."""
    return _account_or_pending(conn, value)


def _offer(conn, value) -> dict | None:
    if value in (None, ""):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        r = conn.execute("SELECT * FROM offers WHERE id = ?", (int(value),)).fetchone()
    else:
        r = conn.execute("SELECT * FROM offers WHERE title = ? COLLATE NOCASE ORDER BY id DESC LIMIT 1",
                         (str(value).strip(),)).fetchone()
    return dict(r) if r else None


def _betting_account(conn, value, label, errors) -> dict | None:
    acc = _account_named(conn, value)
    if value in (None, ""):
        errors.append(f"{label}: missing")
    elif not acc:
        errors.append(f"{label}: unknown account '{_text(value, 60)}' (add it first)")
    elif acc["kind"] == "bank":
        errors.append(f"{label}: {acc['name']} is a bank account")
        return None
    return acc


def _norm_account(conn, raw: dict, source: str):
    errors, warnings = [], []
    name = _text(raw.get("name"), 60)
    kind = raw.get("account_type") or raw.get("kind")
    if not name:
        errors.append("name: missing")
    elif conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        errors.append(f"name: {name} already exists — send a 'balance' candidate to record its current balance")
    if kind not in ("bookmaker", "exchange", "bank"):
        errors.append("account_type: must be bookmaker, exchange or bank")
    balance = _money(raw.get("balance"), "balance", errors, required=False, allow_zero=True)
    if balance and kind == "bank":
        errors.append("balance: a bank is a funding source — record transfers from it instead of a balance")
    commission_bp = 0
    if raw.get("commission") not in (None, ""):
        try:
            commission_bp = parse_commission(raw.get("commission"))
        except ValueError as exc:
            errors.append(f"commission: {exc}")
        if kind != "exchange":
            warnings.append("commission ignored: only exchanges charge it")
            commission_bp = 0
    elif kind == "exchange":
        warnings.append("commission not given: 0% will be used unless you edit it")
    if balance and balance >= LARGE_AMOUNT:
        warnings.append(f"balance {fmt(balance)} is unusually large — check it")
    if errors:
        raise CandidateError(errors)
    return {"name": name, "account_type": kind, "balance_pence": balance or 0, "commission_bp": commission_bp,
            "notes": _text(raw.get("notes"), 2000), "source": source}, warnings


def _norm_balance(conn, raw: dict, source: str):
    errors, warnings = [], []
    acc = _betting_account(conn, raw.get("account"), "account", errors)
    balance = _money(raw.get("balance"), "balance", errors, allow_zero=True)
    at = _time(raw.get("at"), "at", errors, happened=True)
    if balance and balance >= LARGE_AMOUNT:
        warnings.append(f"balance {fmt(balance)} is unusually large — check it")
    if errors:
        raise CandidateError(errors)
    return {"account_id": acc["id"], "account": acc["name"], "balance_pence": balance, "at": at,
            "note": _text(raw.get("note") or raw.get("notes"), 300), "source": source}, warnings


CAPITAL_ALIASES = {"temporary": ("temporary_funding", "temporary_return"),
                   "permanent": ("bankroll_contribution", "bankroll_withdrawal"),
                   "bankroll": ("bankroll_contribution", "bankroll_withdrawal")}
PROFIT_ALIASES = ("profit", "winnings", "profit_withdrawal")      # betting -> bank only


def _norm_transfer(conn, raw: dict, source: str):
    errors, warnings = [], []
    src = _account_named(conn, raw.get("from"))
    dst = _account_named(conn, raw.get("to"))
    for label, value, acc in (("from", raw.get("from"), src), ("to", raw.get("to"), dst)):
        if value in (None, ""):
            errors.append(f"{label}: missing")
        elif not acc:
            errors.append(f"{label}: unknown account '{_text(value, 60)}' (add it first)")
    amount = _money(raw.get("amount"), "amount", errors)
    at = _time(raw.get("at"), "at", errors, happened=True)
    capital_type = None
    if src and dst:
        if src["name"].lower() == dst["name"].lower():
            errors.append("to: choose two different accounts")
        else:
            try:
                natural = services.default_capital_type(src["kind"], dst["kind"])
            except services.UserError as exc:
                errors.append(str(exc))
                natural = None
            if natural:
                allowed = services.allowed_capital_types(natural)
                given = _text(raw.get("capital_type"), 40).lower()
                if given in PROFIT_ALIASES:
                    given = "profit_withdrawal"
                elif given in CAPITAL_ALIASES:
                    given = CAPITAL_ALIASES[given][0 if natural == "temporary_funding" else 1] \
                        if natural != "internal" else "internal"
                if not given:
                    capital_type = natural
                    if natural != "internal":
                        warnings.append(f"capital_type not given: treated as {natural.replace('_', ' ')}")
                elif given in allowed:
                    capital_type = given
                else:
                    errors.append(f"capital_type: {given} does not fit {src['name']} → {dst['name']} "
                                  f"(use {' or '.join(sorted(allowed))})")
    if amount and amount >= LARGE_AMOUNT:
        warnings.append(f"amount {fmt(amount)} is unusually large — check it")
    if times.in_future(at):
        warnings.append("transfer time is in the future")
    due_at = _time(raw.get("return_by"), "return_by", errors) if raw.get("return_by") not in (None, "") else None
    if due_at and capital_type and capital_type != "temporary_funding":
        errors.append("return_by: only for temporary funding (money from the bank that goes back)")
    elif capital_type == "temporary_funding" and not due_at:
        warnings.append("return_by not given: ask when this money has to go back to the bank")
    elif due_at and at and due_at < at:
        errors.append("return_by: before the transfer itself")
    on_its_way = _truthy(raw.get("in_transit"))
    if on_its_way and dst and dst["kind"] != "bank":
        errors.append("in_transit: only a withdrawal to the bank can be on its way")
    if errors:
        raise CandidateError(errors)
    return {"from_id": src["id"], "from": src["name"], "to_id": dst["id"], "to": dst["name"], "amount_pence": amount,
            "capital_type": capital_type, "at": at, "due_at": due_at, "in_transit": on_its_way,
            "notes": _text(raw.get("notes"), 500), "source": source}, warnings


def _norm_offer(conn, raw: dict, source: str):
    errors, warnings = [], []
    title = _text(raw.get("title"), 160)
    if not title:
        errors.append("title: missing")
    acc = _betting_account(conn, raw.get("bookmaker"), "bookmaker", errors) if raw.get("bookmaker") else None
    offer_type = raw.get("offer_type") or "other"
    if offer_type not in services.OFFER_TYPES:
        errors.append(f"offer_type: one of {', '.join(services.OFFER_TYPES)}")
    reward_value = _money(raw.get("reward_value"), "reward_value", errors, required=False, allow_zero=True)
    starts_at = _time(raw.get("starts_at"), "starts_at", errors)
    expires_at = _time(raw.get("expires_at"), "expires_at", errors)
    if expires_at and expires_at < times.now_iso():
        warnings.append("offer has already expired")
    if title and conn.execute("SELECT 1 FROM offers WHERE title = ? COLLATE NOCASE AND status NOT IN "
                              "('complete', 'expired', 'abandoned')", (title,)).fetchone():
        warnings.append("an open offer with this title already exists")
    if acc and acc.get("standing", "ok") != "ok":
        warnings.append(f"{acc['name']} is marked {acc['standing']}: promotions may not be available")
    if acc and offer_type == "sign_up" and conn.execute(
            "SELECT 1 FROM offers WHERE account_id = ? AND offer_type = 'sign_up' AND deleted_at IS NULL "
            "AND status NOT IN ('expired', 'abandoned')", (acc["id"],)).fetchone():
        warnings.append(f"you already have a sign-up offer at {acc['name']}: sign-up offers are usually once only")
    terms = {"qualifying_stake_pence": _money(raw.get("qualifying_stake"), "qualifying_stake", errors, required=False),
             "min_odds": None, "reward_count": None,
             "reward_each_pence": _money(raw.get("reward_each"), "reward_each", errors, required=False),
             "reward_kind": _text(raw.get("reward_type"), 4).lower() or None, "reward_expiry_days": None,
             "reward_trigger": _text(raw.get("reward_trigger"), 20).lower() or None}
    if terms["reward_trigger"] and terms["reward_trigger"] not in services.REWARD_TRIGGERS:
        errors.append("reward_trigger: " + ", ".join(services.REWARD_TRIGGERS))
    if raw.get("min_odds") not in (None, ""):
        try:
            terms["min_odds"] = odds_text(parse_odds(raw.get("min_odds")))
        except ValueError as exc:
            errors.append(f"min_odds: {exc}")
    for key, label in (("reward_count", "reward_count"), ("reward_expiry_days", "reward_expiry_days")):
        if raw.get(label) not in (None, ""):
            if str(raw.get(label)).strip().isdigit():
                terms[key] = int(str(raw.get(label)).strip())
            else:
                errors.append(f"{label}: a whole number")
    try:
        terms = services.clean_terms(terms)
    except services.UserError as exc:
        errors.append(str(exc))
        terms = {}
    if errors:
        raise CandidateError(errors)
    return {"title": title, "account_id": acc["id"] if acc else None, "bookmaker": acc["name"] if acc else None,
            "offer_type": offer_type, "requirement": _text(raw.get("requirement"), 300),
            "reward": _text(raw.get("reward"), 300), "reward_value_pence": reward_value or 0, "starts_at": starts_at,
            "expires_at": expires_at, "notes": _text(raw.get("notes"), 2000), "terms": terms, "source": source}, warnings


def _norm_arrival(conn, raw: dict, source: str):
    """A withdrawal that was on its way has reached the bank: matched by from / to / amount (or its number)."""
    errors = []
    q = ("SELECT t.*, a.name AS from_name, b.name AS to_name FROM transfers t JOIN accounts a ON a.id = t.from_account_id "
         "JOIN accounts b ON b.id = t.to_account_id WHERE t.voided_at IS NULL AND t.in_transit = 1")
    rows = [dict(r) for r in conn.execute(q)]
    ref = raw.get("transfer")
    if ref not in (None, ""):
        rows = [r for r in rows if str(r["id"]) == str(ref).strip()]
    for field, col in (("from", "from_name"), ("to", "to_name")):
        if raw.get(field) not in (None, ""):
            rows = [r for r in rows if r[col].lower() == str(raw[field]).strip().lower()]
    if raw.get("amount") not in (None, ""):
        amount = _money(raw.get("amount"), "amount", errors)
        rows = [r for r in rows if r["amount_pence"] == amount]
    arrived_at = _time(raw.get("arrived_at"), "arrived_at", errors, happened=True)
    if not errors and len(rows) != 1:
        errors.append("transfer: " + ("no withdrawal on its way matches" if not rows else
                                      f"{len(rows)} withdrawals on their way match: give the amount or the number"))
    if errors:
        raise CandidateError(errors)
    t = rows[0]
    return {"transfer_id": t["id"], "from": t["from_name"], "to": t["to_name"], "amount_pence": t["amount_pence"],
            "sent_at": t["at"], "arrived_at": arrived_at, "source": source}, []


def _norm_account_status(conn, raw: dict, source: str):
    """A bookmaker's standing changed (restricted / gubbed, closed, fine again) or it was verified."""
    errors, warnings = [], []
    acc = _account_named(conn, raw.get("account"))
    if raw.get("account") in (None, ""):
        errors.append("account: missing")
    elif not acc:
        errors.append(f"account: unknown account '{_text(raw.get('account'), 60)}'")
    standing = _text(raw.get("standing"), 12).lower() or None
    aliases = {"gubbed": "restricted", "limited": "restricted", "fine": "ok", "normal": "ok", "active": "ok"}
    standing = aliases.get(standing, standing)
    if standing and standing not in services.STANDINGS:
        errors.append("standing: ok, restricted or closed")
    verified = None if raw.get("verified") in (None, "") else _truthy(raw.get("verified"))
    if not standing and verified is None:
        errors.append("standing / verified: give at least one")
    if errors:
        raise CandidateError(errors)
    return {"account_id": acc["id"], "account": acc["name"], "standing": standing, "verified": verified,
            "note": _text(raw.get("note") or raw.get("notes"), 300), "source": source}, warnings


def _norm_reward(conn, raw: dict, source: str):
    """An offer's reward has arrived: its free bets (or cash) are created from the offer's terms on confirmation."""
    errors, warnings = [], []
    offer = _offer(conn, raw.get("offer")) if raw.get("offer") not in (None, "") else None
    if raw.get("offer") in (None, ""):
        errors.append("offer: missing (the offer whose reward arrived)")
    elif not offer:
        errors.append(f"offer: no offer called '{_text(raw.get('offer'), 80)}'")
    received_at = _time(raw.get("received_at"), "received_at", errors, happened=True)
    expires_at = _time(raw.get("expires_at"), "expires_at", errors)
    count = None
    if raw.get("count") not in (None, ""):
        count = int(raw["count"]) if str(raw["count"]).strip().isdigit() else None
        if not count or not 1 <= count <= 20:
            errors.append("count: a whole number from 1 to 20")
    each = _money(raw.get("each"), "each", errors, required=False)
    kind = _text(raw.get("reward_type"), 4).lower() or None
    if kind and kind not in services.REWARD_KINDS:
        errors.append("reward_type: snr, sr or cash")
    if offer:
        if offer["status"] in ("complete", "expired", "abandoned"):
            errors.append(f"offer: '{offer['title']}' is closed")
        if not (kind or offer["reward_kind"]) or not (each or offer["reward_each_pence"]):
            errors.append("each / reward_type: the offer has no reward terms, so give how much each free bet is worth "
                          "and SNR, SR or cash")
        if offer["reward_credited"]:
            warnings.append("this offer's reward was already recorded once: check it isn't a duplicate")
        if not offer["account_id"]:
            errors.append("offer: it has no bookmaker; add one to the offer first")
    if errors:
        raise CandidateError(errors)
    kind = kind or offer["reward_kind"]
    each = each or offer["reward_each_pence"]
    count = 1 if kind == "cash" else (count or offer["reward_count"] or 1)
    if not expires_at and kind != "cash" and offer["reward_expiry_days"]:
        from datetime import timedelta
        expires_at = times.iso(times.parse(received_at or times.now_iso()) + timedelta(days=offer["reward_expiry_days"]))
    elif not expires_at and kind != "cash":
        warnings.append("expires_at not given: ask when the free bets expire")
    return {"offer_id": offer["id"], "offer": offer["title"], "count": count, "each_pence": each, "reward_kind": kind,
            "received_at": received_at, "expires_at": expires_at, "notes": _text(raw.get("notes"), 500),
            "source": source}, warnings


def _norm_free_bet(conn, raw: dict, source: str):
    errors, warnings = [], []
    acc = _betting_account(conn, raw.get("bookmaker"), "bookmaker", errors)
    value = _money(raw.get("value"), "value", errors)
    kind = _text(raw.get("free_bet_type"), 3).lower()
    if not kind:
        kind = "snr"
        warnings.append("free_bet_type not given: SNR (stake not returned) assumed")
    elif kind not in ("snr", "sr"):
        errors.append("free_bet_type: snr or sr")
    expires_at = _time(raw.get("expires_at"), "expires_at", errors)
    offer = _offer_or_pending(conn, raw.get("offer")) if raw.get("offer") not in (None, "") else None
    if raw.get("offer") not in (None, "") and not offer:
        errors.append(f"offer: no offer called '{_text(raw.get('offer'), 80)}'")
    if expires_at and expires_at < times.now_iso():
        warnings.append("free bet has already expired")
    if value and value >= LARGE_STAKE:
        warnings.append(f"value {fmt(value)} is unusually large — check it")
    count = 1
    if raw.get("count") not in (None, ""):
        count = int(raw["count"]) if str(raw["count"]).strip().isdigit() else 0
        if not 1 <= count <= 20:
            errors.append("count: a whole number from 1 to 20")
    if errors:
        raise CandidateError(errors)
    return {"account_id": acc["id"], "bookmaker": acc["name"], "face_value_pence": value, "kind": kind,
            "expires_at": expires_at, "offer_id": offer["id"] if offer else None, "count": count,
            "offer": offer["title"] if offer else None, "notes": _text(raw.get("notes"), 2000), "source": source}, warnings


NOTE_TARGETS = ("account", "offer", "position")


def _norm_note(conn, raw: dict, source: str):
    errors = []
    target = raw.get("target")
    ref = raw.get("target_ref")
    text = _text(raw.get("text"), 1000)
    found = None
    if target not in NOTE_TARGETS:
        errors.append(f"target: one of {', '.join(NOTE_TARGETS)}")
    elif ref in (None, ""):
        errors.append("target_ref: missing (id, or name/title)")
    elif target == "account":
        found = _account_named(conn, ref)
    elif target == "offer":
        found = _offer(conn, ref)
    elif str(ref).isdigit():
        found = services.row(conn, "positions", int(ref))
        found = None if found and found["deleted_at"] else found
    if target in NOTE_TARGETS and ref not in (None, "") and not found:
        errors.append(f"target_ref: no such {target}")
    if not text:
        errors.append("text: missing")
    if errors:
        raise CandidateError(errors)
    return {"target": target, "target_id": found["id"], "target_ref": _text(ref, 80), "text": text,
            "source": source}, []


OUTCOMES = {"back_won": "back won", "back_lost": "back lost", "void": "void"}
SETTLE_RESULTS = ("won", "lost", "void", "cashout", "manual")


def _pending_position(conn, event: str, selection: str):
    """A matched position still waiting for confirmation (so a backfilled bet and its result can be sent together)."""
    deps = _PENDING.get()
    if deps is None:
        return None
    found = []
    for (cj,) in conn.execute("SELECT candidate_json FROM import_candidates WHERE status = 'needs_confirmation' "
                              "AND kind = 'position' ORDER BY id"):
        c = json.loads(cj)
        if c.get("event", "").lower() == event.lower() and (not selection or c.get("selection", "").lower() == selection.lower()):
            found.append(c)
    if len(found) == 1:
        deps.append(f"position '{found[0]['event']}'")
        return found[0]
    return "ambiguous" if found else None


def _norm_settlement(conn, raw: dict, source: str):
    """The result of a position the user already placed. Never guessed: the outcome (or each bet's result) and the
    settlement date must be given; every amount is calculated by the app (services.default_settlement)."""
    errors, warnings = [], []
    event, selection = _text(raw.get("event"), 160), _text(raw.get("selection"), 120)
    pos, pending = None, None
    ref = raw.get("position")
    if ref not in (None, "", 0, "0"):
        pos = services.row(conn, "positions", int(ref)) if str(ref).strip().isdigit() else None
        if not pos or pos["deleted_at"]:
            errors.append(f"position: no position #{_text(ref, 12)}")
            pos = None
    elif event:
        q, args = "SELECT * FROM positions WHERE deleted_at IS NULL AND status = 'open' AND event = ? COLLATE NOCASE", [event]
        if selection:
            q, args = q + " AND selection = ? COLLATE NOCASE", args + [selection]
        rows = [dict(r) for r in conn.execute(q, args)]
        what = f"'{event}'" + (f" / '{selection}'" if selection else "")
        if len(rows) == 1:
            pos = rows[0]
        elif len(rows) > 1:
            errors.append(f"position: {len(rows)} open positions match {what}: give the position number")
        else:
            pending = _pending_position(conn, event, selection)
            if pending == "ambiguous":
                errors.append(f"position: several pending positions match {what}: give the selection")
            elif pending is None:
                errors.append(f"position: no open position matches {what} (submit the bets first, or give the number)")
    else:
        errors.append("position: give the position number, or the event (and selection)")
    if pos and pos["status"] != "open":
        errors.append(f"position: #{pos['id']} is already settled (reopen it in the app to change it)")
    if raw.get("settled_at") in (None, ""):
        errors.append("settled_at: missing (when the bets were settled)")
    settled_at = _time(raw.get("settled_at"), "settled_at", errors, happened=True)
    outcome = raw.get("outcome") or None
    results = {side: (raw.get(f"{side}_result") or None) for side in ("back", "lay")}
    if outcome and any(results.values()):
        errors.append("outcome: give either outcome or back_result/lay_result, not both")
    elif not outcome and not any(results.values()):
        errors.append("outcome: missing (back_won, back_lost or void) — ask the user; it is never guessed")
    if outcome and outcome not in OUTCOMES:
        errors.append("outcome: one of back_won, back_lost, void")
    amounts = {}
    for side, result in results.items():
        if result and result not in SETTLE_RESULTS:
            errors.append(f"{side}_result: one of {', '.join(SETTLE_RESULTS)}")
        if result in ("cashout", "manual"):
            amounts[side] = _money(raw.get(f"{side}_amount"), f"{side}_amount", errors, allow_zero=True,
                                   allow_negative=side == "lay")
        elif raw.get(f"{side}_amount") not in (None, ""):
            errors.append(f"{side}_amount: only for a cashout or manual result (otherwise the app calculates it)")
    reported_return = _money(raw.get("reported_back_return"), "reported_back_return", errors, required=False,
                             allow_zero=True)
    reported_net = _money(raw.get("reported_net_result"), "reported_net_result", errors, required=False,
                          allow_zero=True, allow_negative=True)
    plan, net, offer_change = [], None, None
    if pos and not errors:
        legs = [dict(r) for r in conn.execute(
            "SELECT l.*, a.name AS account, f.kind AS free_bet_kind FROM legs l JOIN accounts a ON a.id = l.account_id "
            "LEFT JOIN free_bets f ON f.id = l.free_bet_id WHERE l.position_id = ? AND l.deleted_at IS NULL "
            "ORDER BY l.side, l.id", (pos["id"],))]
        if not legs:
            errors.append(f"position: #{pos['id']} has no bets to settle")
        for side in ("back", "lay"):
            side_legs = [l for l in legs if l["side"] == side]
            if not outcome and results[side] and len(side_legs) != 1:
                errors.append(f"{side}_result: position #{pos['id']} has {len(side_legs)} {side} bets — settle it in the app")
            elif not outcome and side_legs and not results[side]:
                errors.append(f"{side}_result: missing (every bet needs a result)")
        for leg in legs if not errors else []:
            if outcome:
                result = "void" if outcome == "void" else (
                    ("won" if outcome == "back_won" else "lost") if leg["side"] == "back"
                    else ("lost" if outcome == "back_won" else "won"))
            else:
                result = results[leg["side"]]
            if result in ("cashout", "manual"):
                amount, commission = amounts[leg["side"]], 0
            else:
                amount, commission = services.default_settlement(leg, result)
            plan.append({"leg_id": leg["id"], "side": leg["side"], "account": leg["account"], "result": result,
                         "amount_pence": amount, "commission_pence": commission})
        if plan:
            cash_stakes = sum(l["stake_pence"] for l in legs if l["side"] == "back" and not l["free_bet_id"])
            net = sum(p["amount_pence"] for p in plan) - sum(p["commission_pence"] for p in plan) - cash_stakes
            if times.in_future(settled_at):
                warnings.append("settlement date is in the future")
            if settled_at and pos["event_at"] and settled_at < pos["event_at"]:
                warnings.append("settlement date is before the event started")
            given = [l["placed_at"] for l in legs if abs((times.parse(l["placed_at"]) - times.parse(l["created_at"]))
                                                       .total_seconds()) > 120]       # placed time stated, not defaulted
            if settled_at and given and settled_at < min(given):
                warnings.append("settlement date is before the bet was placed")
            if any(p["result"] in ("cashout", "manual") for p in plan):
                warnings.append("cash-out / manual amount entered: check it")
            backs = [p for p in plan if p["side"] == "back"]
            if reported_return is not None and len(backs) == 1 and reported_return != backs[0]["amount_pence"]:
                warnings.append(f"reported return {fmt(reported_return)} differs from the calculated "
                                f"{fmt(backs[0]['amount_pence'])} — check it")
            if reported_net is not None and reported_net != net:
                warnings.append(f"reported net result {fmt(reported_net, signed=True)} differs from the calculated "
                                f"{fmt(net, signed=True)} — check it")
            if pos["offer_id"] and pos["kind"] == "qualifying":
                offer = services.row(conn, "offers", pos["offer_id"])
                if offer and offer["status"] == "qualifying":
                    offer_change = f"Offer '{offer['title']}': qualifying → awaiting reward"
    if errors:
        raise CandidateError(errors)
    src = pos or (pending if isinstance(pending, dict) else {})
    return {"position_id": pos["id"] if pos else None, "event": src.get("event") or event,
            "selection": src.get("selection") or selection, "settled_at": settled_at, "outcome": outcome,
            "back_result": results["back"], "lay_result": results["lay"],
            "back_amount_pence": amounts.get("back"), "lay_amount_pence": amounts.get("lay"),
            "reported_back_return_pence": reported_return, "reported_net_pence": reported_net,
            "legs": plan, "net_pence": net, "offer_change": offer_change,
            "notes": _text(raw.get("notes"), 500), "source": source}, warnings


NORMALISERS = {"account": _norm_account, "balance": _norm_balance, "transfer": _norm_transfer,
               "offer": _norm_offer, "free_bet": _norm_free_bet, "note": _norm_note, "settlement": _norm_settlement,
               "reward": _norm_reward, "arrival": _norm_arrival, "account_status": _norm_account_status,
               "amend": lambda conn, raw, source: _changes().normalise_amend(conn, raw, source),
               "reverse": lambda conn, raw, source: _changes().normalise_reverse(conn, raw, source)}


def _changes():
    from . import changes
    return changes


def normalise_kind(conn, kind: str, raw: dict, source: str = "api", *,
                   allow_pending: bool = False) -> tuple[dict, list[str]]:
    if kind not in KINDS:
        raise CandidateError([f"type: one of {', '.join(KINDS)}"])
    if not isinstance(raw, dict):
        raise CandidateError(["Expected an object"])
    source = source if source in SOURCES else "api"
    token = _PENDING.set([] if allow_pending else None)
    try:
        candidate, warnings = (normalise(conn, raw, source) if kind == "position"
                               else NORMALISERS[kind](conn, raw, source))
        deps = sorted(set(_PENDING.get() or []))
    finally:
        _PENDING.reset(token)
    if deps:
        candidate["depends_on"] = deps
        warnings = warnings + [f"waits for the pending {d}: confirm that first (bulk confirmation does it in order)"
                               for d in deps]
    return candidate, warnings


def title(kind: str, c: dict) -> str:
    """One line for lists (shown to the signed-in user only)."""
    if kind == "position":
        return c.get("event") or f"Add to position #{c.get('position_id')}"
    if kind == "account":
        return f"{c['name']} ({c['account_type']})"
    if kind == "balance":
        return f"{c['account']} balance {fmt(c['balance_pence'])}"
    if kind == "transfer":
        return f"{c['from']} → {c['to']} {fmt(c['amount_pence'])}"
    if kind == "offer":
        return c["title"]
    if kind == "free_bet":
        n = c.get("count") or 1
        return f"{c['bookmaker']} {str(n) + ' × ' if n > 1 else ''}{fmt(c['face_value_pence'])} {c['kind'].upper()} free bet" \
               + ("s" if n > 1 else "")
    if kind == "reward":
        return f"Reward arrived: {c['offer']}"
    if kind == "arrival":
        return f"Arrived: {c['from']} → {c['to']} {fmt(c['amount_pence'])}"
    if kind == "amend":
        return f"Change {c['label']}"
    if kind == "reverse":
        return f"Undo {c['label']}"
    if kind == "account_status":
        bits = [{"ok": "fine", "restricted": "restricted", "closed": "closed"}[c["standing"]] if c["standing"] else "",
                ("verified" if c["verified"] else "not verified") if c["verified"] is not None else ""]
        return f"{c['account']}: " + ", ".join(b for b in bits if b)
    if kind == "settlement":
        what = OUTCOMES.get(c["outcome"]) or ", ".join(f"{s} {c[s + '_result']}" for s in ("back", "lay")
                                                         if c.get(s + "_result"))
        return f"Settle {'#' + str(c['position_id']) + ' ' if c['position_id'] else ''}{c['event']}: {what}"
    return f"Note on {c['target']} {c['target_ref']}"


def summary(kind: str, c: dict) -> dict:
    """What a submitter gets back: only the figures it sent and the app's calculations on them.
    Never balances, profit, other records, or resolved names of existing positions."""
    if kind == "account":
        out = {"name": c["name"], "account_type": c["account_type"], "balance": plain(c["balance_pence"])}
        if c["account_type"] == "exchange":
            out["commission"] = commission_text(c["commission_bp"])
        return out
    if kind == "balance":
        return {"account": c["account"], "reported_balance": plain(c["balance_pence"])}
    if kind == "transfer":
        out = {"from": c["from"], "to": c["to"], "amount": plain(c["amount_pence"]), "capital_type": c["capital_type"]}
        if c.get("due_at"):
            out["return_by"] = c["due_at"]
        return out
    if kind == "offer":
        return {"title": c["title"], "bookmaker": c["bookmaker"], "offer_type": c["offer_type"], "reward": c["reward"],
                "reward_value": plain(c["reward_value_pence"]), "expires_at": c["expires_at"]}
    if kind == "free_bet":
        return {"bookmaker": c["bookmaker"], "value": plain(c["face_value_pence"]), "free_bet_type": c["kind"],
                "expires_at": c["expires_at"], "offer": c["offer"], "count": c.get("count") or 1}
    if kind == "reward":
        return {"offer": c["offer"], "count": c["count"], "each": plain(c["each_pence"]), "reward_type": c["reward_kind"],
                "expires_at": c["expires_at"]}
    if kind == "arrival":
        return {"from": c["from"], "to": c["to"], "amount": plain(c["amount_pence"]), "arrived_at": c["arrived_at"]}
    if kind in ("amend", "reverse"):
        out = {"target": c["target"], "what": c["label"], "reason": c["reason"]}
        if kind == "amend":
            out["changes"] = {k: (plain(v) if k.endswith("_pence") and isinstance(v, int) else v) for k, v in c["changes"].items()}
        else:
            out["effect"] = c["effect"]
        return out
    if kind == "account_status":
        return {"account": c["account"], "standing": c["standing"], "verified": c["verified"]}
    if kind == "note":
        return {"target": c["target"], "target_ref": c["target_ref"], "text": c["text"][:200]}
    if kind == "settlement":
        out = {"position_number": c["position_id"], "event": c["event"], "selection": c["selection"],
               "settled_at": c["settled_at"], "outcome": c["outcome"], "back_result": c["back_result"],
               "lay_result": c["lay_result"],
               "legs": [{"side": p["side"], "account": p["account"], "result": p["result"],
                         "amount": plain(p["amount_pence"]), "commission": plain(p["commission_pence"])}
                        for p in c["legs"]],
               "net_result": plain(c["net_pence"]) if c["net_pence"] is not None else None}
        if c["offer_change"]:
            out["offer_change"] = c["offer_change"]
        return out
    legs = []
    for leg in c["legs"]:
        d = {"account": leg["account"], "side": leg["side"], "stake": plain(leg["stake_pence"]), "odds": leg["odds"]}
        if leg["side"] == "lay":
            d["commission"] = commission_text(leg["commission_bp"])
            d["liability"] = plain(calc.lay_liability(leg["stake_pence"], Decimal(leg["odds"])))
        if leg["free_bet_id"] or leg.get("uses_free_bet"):
            d["free_bet"] = True
            if leg.get("token_marked_used"):
                d["historical_token"] = True
        legs.append(d)
    out = {"event": c["event"], "selection": c["selection"], "market": c["market"], "position_type": c["kind"],
           "add_to_position_id": c["position_id"], "legs": legs}
    if any(l["side"] == "back" for l in c["legs"]) and any(l["side"] == "lay" for l in c["legs"]):
        win, lose = outcomes(c)
        out.update(if_back_wins=plain(win), if_back_loses=plain(lose), expected_result=plain(min(win, lose)))
    return out


def _new_public_id(conn) -> str:
    while True:
        pid = secrets.token_hex(8)
        if not conn.execute("SELECT 1 FROM import_candidates WHERE public_id = ?", (pid,)).fetchone():
            return pid


# ---------------------------------------------------------------- stored candidates (async imports)

def store(conn, raw: dict, source: str, *, kind: str = "position", idempotency_key: str | None = None,
          submitted_by: str | None = None) -> tuple[int, dict, list[str]]:
    raw_json = json.dumps(raw)
    if len(raw_json) > MAX_RAW:
        raise CandidateError(["Too large"])
    candidate, warnings = normalise_kind(conn, kind, raw, source, allow_pending=True)
    cur = conn.execute("INSERT INTO import_candidates (source, kind, raw_json, candidate_json, warnings_json, created_at, "
                       "public_id, idempotency_key, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (candidate["source"], kind, raw_json, json.dumps(candidate), json.dumps(warnings),
                        times.now_iso(), _new_public_id(conn), idempotency_key, submitted_by))
    services.audit(conn, "import", cur.lastrowid, "received", None,
                   {"source": candidate["source"], "kind": kind, "title": title(kind, candidate),
                    "submitted_by": submitted_by})
    return cur.lastrowid, candidate, warnings


def find(conn, ref) -> dict | None:
    """A stored candidate by its number (signed-in pages) or its public id."""
    ref = str(ref)
    if ref.isdigit():
        return services.row(conn, "import_candidates", int(ref))
    r = conn.execute("SELECT * FROM import_candidates WHERE public_id = ?", (ref,)).fetchone()
    return dict(r) if r else None


def edit(conn, import_id: int, raw: dict) -> list[str]:
    """Replace a pending candidate's input with the user's corrections (validated again)."""
    item = services.row(conn, "import_candidates", import_id)
    if not item or item["status"] != "needs_confirmation":
        raise services.UserError("This import has already been handled")
    candidate, warnings = normalise_kind(conn, item["kind"], raw, item["source"], allow_pending=True)
    conn.execute("UPDATE import_candidates SET raw_json = ?, candidate_json = ?, warnings_json = ?, edited_at = ? "
                 "WHERE id = ?", (json.dumps(raw), json.dumps(candidate), json.dumps(warnings), times.now_iso(),
                                  import_id))
    services.audit(conn, "import", import_id, "edited", json.loads(item["candidate_json"]), candidate)
    return warnings


def decide(conn, import_id: int, accept: bool, decided_by: str = "user") -> int | None:
    """Reject, or confirm through the normal services. Returns the position id for positions.
    decided_by: 'user', 'user-bulk' or 'assistant-auto' (Trusted Assistant mode)."""
    item = services.row(conn, "import_candidates", import_id)
    if not item or item["status"] != "needs_confirmation":
        raise services.UserError("This import has already been handled")
    if not accept:
        conn.execute("UPDATE import_candidates SET status = 'rejected', decided_at = ?, decided_by = ? WHERE id = ?",
                     (times.now_iso(), decided_by, import_id))
        services.audit(conn, "import", import_id, "rejected", None, {"decided_by": decided_by})
        return None
    kind = item["kind"]
    # Re-validate now: accounts/free bets/offers may have changed since it arrived; pending references must
    # have been confirmed by now (bulk confirmation runs in dependency order).
    raw = (_raw_from_candidate(json.loads(item["candidate_json"])) if kind == "position"
           else to_raw(kind, json.loads(item["candidate_json"]), ids=True))
    candidate, _ = normalise_kind(conn, kind, raw, item["source"], allow_pending=True)
    if candidate.get("depends_on"):
        raise services.UserError("Waits for the pending " + ", ".join(candidate["depends_on"]) + ": confirm that first")
    if kind == "position":
        pid, legs = apply(conn, candidate)
        ref = f"position:{pid}"
        conn.execute("UPDATE import_candidates SET status = 'applied', decided_at = ?, applied_position_id = ?, "
                     "applied_leg_id = ?, applied_ref = ?, decided_by = ? WHERE id = ?",
                     (times.now_iso(), pid, legs[0] if legs else None, ref, decided_by, import_id))
        services.audit(conn, "import", import_id, "applied", None, {"position_id": pid, "legs": legs,
                                                                    "decided_by": decided_by})
        supersede_pending(conn, kind, candidate, ref)
        return pid
    ref = apply_kind(conn, kind, candidate)
    conn.execute("UPDATE import_candidates SET status = 'applied', decided_at = ?, applied_ref = ?, decided_by = ? "
                 "WHERE id = ?", (times.now_iso(), ref, decided_by, import_id))
    services.audit(conn, "import", import_id, "applied", None, {"created": ref, "decided_by": decided_by})
    supersede_pending(conn, kind, candidate, ref)
    return None


def _same_thing(kind: str, c: dict):
    """What makes two candidates the same real-world entry (None: never matched automatically)."""
    from . import normalise
    if kind == "transfer":
        return (c.get("from_id"), c.get("to_id"), c.get("amount_pence"), c.get("capital_type"))
    if kind == "position" and not c.get("position_id"):
        return (normalise.event_key(c.get("event") or ""),
                tuple(sorted((l.get("account_id"), l.get("side"), l.get("stake_pence"),
                              str(Decimal(str(l["odds"])).normalize()) if l.get("odds") else None)
                             for l in c.get("legs") or [])))
    return None


def supersede_pending(conn, kind: str, applied: dict, ref: str) -> list[int]:
    """A corrected resubmission went through: the earlier held attempt at the same entry is closed as superseded
    (status rejected, decided_by 'system', with the reason and a link to what was recorded), only when exactly one
    held candidate is clearly the same entry. Ambiguous cases are left for the user. Both stay in the history."""
    key = _same_thing(kind, applied)
    if not key or None in key[:3]:
        return []
    since = times.iso(times.now() - timedelta(hours=48))
    same = []
    for item in conn.execute("SELECT * FROM import_candidates WHERE status = 'needs_confirmation' AND kind = ? AND "
                             "created_at >= ?", (kind, since)).fetchall():
        try:
            c = json.loads(item["candidate_json"])
            if _same_thing(kind, c) != key:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        if kind == "transfer" and c.get("at") and applied.get("at") and \
                times.local(c["at"], "%Y-%m-%d") != times.local(applied["at"], "%Y-%m-%d"):
            continue
        same.append(item)
    if len(same) != 1:
        return []
    item = same[0]
    conn.execute("UPDATE import_candidates SET status = 'rejected', decided_at = ?, decided_by = 'system', "
                 "hold_reason = ? WHERE id = ?", (times.now_iso(), f"superseded by {ref}"[:300], item["id"]))
    services.audit(conn, "import", item["id"], "superseded", None, {"by": ref, "decided_by": "system"},
                   "a corrected resubmission of the same entry was recorded")
    return [item["id"]]


def apply_kind(conn, kind: str, c: dict) -> str:
    """Confirmed non-position candidates -> the same services the forms use. Returns 'entity:id'."""
    if kind == "settlement":                  # the existing settlement functions do every calculation
        if c["outcome"]:
            services.quick_settle(conn, c["position_id"], c["outcome"], at=c["settled_at"])
        else:
            for p in c["legs"]:
                services.settle_leg(conn, p["leg_id"], p["result"],
                                    amount=p["amount_pence"] if p["result"] in ("cashout", "manual") else None,
                                    at=c["settled_at"], note="settlement confirmed from an import")
        return f"position:{c['position_id']}"
    if kind == "account":
        aid = services.create_account(conn, name=c["name"], kind=c["account_type"], commission_bp=c["commission_bp"],
                                      notes=c["notes"], opening_balance=c["balance_pence"])
        return f"account:{aid}"
    if kind == "balance":
        services.reconcile(conn, c["account_id"], c["balance_pence"], note=c["note"] or "balance confirmed from import",
                           at=c["at"])
        return f"account:{c['account_id']}"
    if kind == "transfer":
        tid = services.create_transfer(conn, from_id=c["from_id"], to_id=c["to_id"], amount=c["amount_pence"],
                                       at=c["at"], capital_type=c["capital_type"], notes=c["notes"],
                                       due_at=c.get("due_at"), in_transit=bool(c.get("in_transit")))
        return f"transfer:{tid}"
    if kind == "offer":
        oid = services.create_offer(conn, title=c["title"], account_id=c["account_id"], offer_type=c["offer_type"],
                                    requirement=c["requirement"], reward=c["reward"],
                                    reward_value=c["reward_value_pence"], starts_at=c["starts_at"],
                                    expires_at=c["expires_at"], notes=c["notes"], terms=c.get("terms") or {})
        return f"offer:{oid}"
    if kind == "free_bet":
        fids = services.create_free_bets(conn, count=c.get("count") or 1, account_id=c["account_id"],
                                         face_value=c["face_value_pence"], kind=c["kind"], offer_id=c["offer_id"],
                                         expires_at=c["expires_at"], notes=c["notes"])
        return f"free_bet:{fids[0]}"
    if kind == "reward":
        services.receive_reward(conn, c["offer_id"], at=c["received_at"], expires_at=c["expires_at"], count=c["count"],
                                each=c["each_pence"], kind=c["reward_kind"])
        return f"offer:{c['offer_id']}"
    if kind == "arrival":
        services.mark_arrived(conn, c["transfer_id"], at=c["arrived_at"])
        return f"transfer:{c['transfer_id']}"
    if kind == "amend":
        return _changes().apply_amend(conn, c)
    if kind == "reverse":
        return _changes().apply_reverse(conn, c)
    if kind == "account_status":
        acc = services.row(conn, "accounts", c["account_id"])
        notes = None
        if c["note"]:
            line = f"{times.local(times.now_iso())}: {c['note']}"
            notes = f"{acc['notes']}\n{line}".strip() if acc["notes"] else line
        services.update_account(conn, c["account_id"], standing=c["standing"], verified=c["verified"], notes=notes)
        return f"account:{c['account_id']}"
    table = {"account": "accounts", "offer": "offers", "position": "positions"}[c["target"]]
    current = services.row(conn, table, c["target_id"])
    line = f"{times.local(times.now_iso())}: {c['text']}"
    notes = f"{current['notes']}\n{line}".strip() if current["notes"] else line
    if c["target"] == "account":
        services.update_account(conn, c["target_id"], notes=notes)
    elif c["target"] == "offer":
        services.update_offer(conn, c["target_id"], {"notes": notes}, note="note added from import")
    else:
        services.update_position(conn, c["target_id"], {"notes": notes}, note="note added from import")
    return f"{c['target']}:{c['target_id']}"


def to_raw(kind: str, c: dict, *, ids: bool = False) -> dict:
    """Candidate -> input fields again: for the edit form (names, local times) or for
    re-validation on confirmation (ids, so a renamed account still resolves)."""
    when = (lambda v: v) if ids else (lambda v: times.to_local_input(v) if v else "")
    money = lambda p: plain(p) if p else ""
    if kind == "position":
        r = _raw_from_candidate(c)
        if not ids:
            r = {"event": c["event"], "selection": c["selection"], "market": c["market"], "position_type": c["kind"],
                 "event_at": when(c["event_at"]), "offer": c["offer_id"] or c.get("offer_ref") or "", "notes": c["notes"],
                 "add_to_position_id": c["position_id"] or "",
                 "legs": [{"account": l["account"], "side": l["side"], "stake": plain(l["stake_pence"]), "odds": l["odds"],
                           "commission": format(Decimal(l["commission_bp"]) / 100, "f") if l["side"] == "lay" else "",
                           "free_bet_id": l["free_bet_id"] or "", "placed_at": when(l["placed_at"]),
                           "token_marked_used": "true" if l.get("token_marked_used") else ""} for l in c["legs"]]}
        return r
    if kind == "account":
        return {"name": c["name"], "account_type": c["account_type"], "balance": money(c["balance_pence"]),
                "commission": format(Decimal(c["commission_bp"]) / 100, "f") if c["account_type"] == "exchange" else "",
                "notes": c["notes"]}
    if kind == "balance":
        return {"account": (c["account_id"] or c["account"]) if ids else c["account"], "balance": plain(c["balance_pence"]),
                "at": when(c["at"]), "note": c["note"]}
    if kind == "transfer":
        return {"from": (c["from_id"] or c["from"]) if ids else c["from"], "to": (c["to_id"] or c["to"]) if ids else c["to"],
                "amount": plain(c["amount_pence"]), "capital_type": c["capital_type"], "at": when(c["at"]),
                "return_by": when(c["due_at"]) if c.get("due_at") else "",
                "in_transit": "true" if c.get("in_transit") else "", "notes": c["notes"]}
    if kind == "offer":
        t = c.get("terms") or {}
        return {"title": c["title"], "bookmaker": ((c["account_id"] or c["bookmaker"]) if ids else c["bookmaker"]) or "",
                "offer_type": c["offer_type"], "requirement": c["requirement"], "reward": c["reward"],
                "reward_value": money(c["reward_value_pence"]), "starts_at": when(c["starts_at"]),
                "expires_at": when(c["expires_at"]), "notes": c["notes"],
                "qualifying_stake": money(t.get("qualifying_stake_pence")), "min_odds": t.get("min_odds") or "",
                "reward_count": t.get("reward_count") or "", "reward_each": money(t.get("reward_each_pence")),
                "reward_type": t.get("reward_kind") or "", "reward_expiry_days": t.get("reward_expiry_days") or "",
                "reward_trigger": t.get("reward_trigger") or ""}
    if kind == "free_bet":
        return {"bookmaker": (c["account_id"] or c["bookmaker"]) if ids else c["bookmaker"], "value": plain(c["face_value_pence"]),
                "free_bet_type": c["kind"], "expires_at": when(c["expires_at"]), "count": c.get("count") or 1,
                "offer": ((c["offer_id"] or c["offer"]) if ids else c["offer"]) or "", "notes": c["notes"]}
    if kind in ("amend", "reverse"):
        return _changes().to_raw(kind, c)
    if kind == "arrival":
        return {"transfer": c["transfer_id"], "from": c["from"], "to": c["to"], "amount": plain(c["amount_pence"]),
                "arrived_at": when(c["arrived_at"])}
    if kind == "account_status":
        return {"account": c["account_id"] if ids else c["account"], "standing": c["standing"] or "",
                "verified": "" if c["verified"] is None else ("true" if c["verified"] else "false"), "note": c["note"]}
    if kind == "reward":
        return {"offer": c["offer_id"] if ids else c["offer"], "count": c["count"], "each": plain(c["each_pence"]),
                "reward_type": c["reward_kind"], "received_at": when(c["received_at"]), "expires_at": when(c["expires_at"]),
                "notes": c["notes"]}
    if kind == "settlement":
        signed = lambda p: plain(p) if p is not None else ""
        return {"position": c["position_id"] or "", "event": c["event"], "selection": c["selection"],
                "settled_at": when(c["settled_at"]), "outcome": c["outcome"] or "",
                "back_result": c["back_result"] or "", "back_amount": signed(c["back_amount_pence"]),
                "lay_result": c["lay_result"] or "", "lay_amount": signed(c["lay_amount_pence"]),
                "reported_back_return": signed(c["reported_back_return_pence"]),
                "reported_net_result": signed(c["reported_net_pence"]), "notes": c["notes"]}
    return {"target": c["target"], "target_ref": c["target_id"] if ids else c["target_ref"], "text": c["text"]}


def _raw_from_candidate(c: dict) -> dict:
    return {**{k: c[k] for k in ("position_id", "event", "market", "selection", "kind", "offer_id", "notes")},
            "offer": c.get("offer_ref"), "event_at": c["event_at"], "hedge_strategy": c.get("hedge_strategy"),
            "unhedged_reason": c.get("unhedged_reason"),
            "legs": [{"account_id": l["account_id"] or l["account"], "side": l["side"], "stake": f"{l['stake_pence'] / 100:.2f}",
                      "odds": l["odds"], "commission": format(Decimal(l["commission_bp"]) / 100, "f"), "free_bet_id": l["free_bet_id"],
                      "use_free_bet": bool(l.get("uses_free_bet")), "token_marked_used": bool(l.get("token_marked_used")),
                      "placed_at": l["placed_at"], "external_ref": l["external_ref"]} for l in c["legs"]]}


# ---------------------------------------------------------------- confirming many at once

# Dependencies first: accounts, then offers, free-bet tokens, transfers (money in before money out, so
# temporary funding exists before its return), positions, balance checks (after the activity they reflect), notes.
CONFIRM_ORDER = {"account": 0, "account_status": 1, "offer": 1, "reward": 2, "free_bet": 2, "transfer": 3,
                 "arrival": 4, "position": 4, "settlement": 5, "amend": 6, "reverse": 6, "balance": 7, "note": 8}
TRANSFER_ORDER = {"temporary_funding": 0, "bankroll_contribution": 0, "internal": 1, "temporary_return": 2,
                  "bankroll_withdrawal": 2}


def confirmation_order(items: list[dict]) -> list[dict]:
    def key(item):
        c = json.loads(item["candidate_json"])
        when = c.get("at") or c.get("settled_at") or c.get("event_at") or ""
        return (CONFIRM_ORDER[item["kind"]], TRANSFER_ORDER.get(c.get("capital_type"), 1) if item["kind"] == "transfer"
                else 0, when, item["id"])
    return sorted(items, key=key)


class BatchError(services.UserError):
    def __init__(self, results: list[dict]):
        super().__init__("Nothing was confirmed: some entries cannot be confirmed")
        self.results = results


def confirm_many(conn, ids: list[int], *, all_or_nothing: bool, decided_by: str = "user-bulk") -> list[dict]:
    """Confirm candidates in dependency order, each one atomically (savepoint). all_or_nothing: if any cannot be
    confirmed, raise BatchError so the caller's transaction rolls back and nothing at all is recorded.
    Otherwise the ones that cannot be confirmed (and anything waiting for them) stay pending."""
    items = [services.row(conn, "import_candidates", i) for i in dict.fromkeys(ids)]
    items = [i for i in items if i and i["status"] == "needs_confirmation"]
    results = []
    for item in confirmation_order(items):
        result = {"id": item["id"], "kind": item["kind"],
                  "title": title(item["kind"], json.loads(item["candidate_json"]))}
        conn.execute("SAVEPOINT confirm_one")
        try:
            decide(conn, item["id"], True, decided_by=decided_by)
            conn.execute("RELEASE SAVEPOINT confirm_one")
            result.update(ok=True, message="Confirmed")
        except (ValueError, sqlite3.IntegrityError) as exc:        # UserError and CandidateError are ValueErrors
            conn.execute("ROLLBACK TO SAVEPOINT confirm_one")
            conn.execute("RELEASE SAVEPOINT confirm_one")
            result.update(ok=False, message=str(exc)[:300] or "Cannot be confirmed")
        results.append(result)
    if all_or_nothing and any(not r["ok"] for r in results):
        raise BatchError(results)
    services.audit(conn, "import_batch", None, "confirmed", None,
                   {"confirmed": [r["id"] for r in results if r["ok"]], "held": [r["id"] for r in results if not r["ok"]],
                    "mode": "selected" if all_or_nothing else "all_valid", "decided_by": decided_by})
    return results


def check(conn, item: dict) -> str | None:
    """Why a pending candidate cannot be confirmed right now (None if it looks fine)."""
    raw = (_raw_from_candidate(json.loads(item["candidate_json"])) if item["kind"] == "position"
           else to_raw(item["kind"], json.loads(item["candidate_json"]), ids=True))
    try:
        candidate, _ = normalise_kind(conn, item["kind"], raw, item["source"], allow_pending=True)
    except CandidateError as exc:
        return "; ".join(exc.errors)[:300]
    if candidate.get("depends_on"):
        return "after " + ", ".join(candidate["depends_on"])
    return None


# ---------------------------------------------------------------- Trusted Assistant mode

AUTO_SETTING = "assistant_auto_confirm"


def plans_unhedged_limit(conn) -> int:
    from . import plans
    return plans.unhedged_limit(conn)
# Warnings that are routine for a complete, consistent entry; any other warning means a person should look.
ROUTINE_WARNINGS = ("lay stake calculated by the app", "event time is in the past")


def auto_confirm_settings(conn) -> dict:
    cfg = services.get_setting(conn, AUTO_SETTING, None) or {}
    kinds = [k for k in cfg.get("kinds", []) if k in TRUSTABLE]
    return {"enabled": bool(cfg.get("enabled")) and bool(kinds), "kinds": kinds}


def auto_confirm_hold(conn, kind: str, candidate: dict, warnings: list[str]) -> str | None:
    """Why an otherwise valid assistant submission must still wait for the user (None = may auto-confirm)."""
    if kind in CORRECTIONS:
        return "corrections and undos always wait for you"
    if kind == "transfer" and candidate.get("capital_type") == "profit_withdrawal":
        return "profit withdrawals always wait for you"
    if candidate.get("depends_on"):
        return "waits for the pending " + ", ".join(candidate["depends_on"])
    if kind == "settlement" and any(c in ("cashout", "manual") for c in (candidate["back_result"], candidate["lay_result"])):
        return "a cash-out / manual amount needs a look"
    unusual = [w for w in warnings if not w.startswith(ROUTINE_WARNINGS)]
    if unusual:
        return "needs a look: " + "; ".join(unusual)
    if kind == "position":
        sides = [l["side"] for l in candidate["legs"]]
        if candidate.get("hedge_strategy") == "intentionally_unhedged" and not candidate.get("position_id"):
            exposure = sum(l["stake_pence"] or 0 for l in candidate["legs"] if l["side"] == "back")
            limit = plans_unhedged_limit(conn)
            if exposure > limit:
                return (f"needs a look: {fmt(exposure)} left unhedged on purpose is above your {fmt(limit)} limit "
                        "for unhedged qualifiers")
        elif candidate["kind"] == "qualifying" and not candidate.get("position_id") and sorted(set(sides)) != ["back", "lay"]:
            return ("incomplete: a qualifying position needs the lay too (or hedge_strategy intentionally_unhedged with a "
                    "reason, for a tiny qualifier not worth laying)")
        if any(l.get("token_marked_used") for l in candidate["legs"]):
            return "links a free bet that was marked used by hand: check it (historical repair)"
        if not candidate["position_id"] and preview(conn, candidate)["similar"]:
            return "possible duplicate: an open position for this event already exists"
    return None
