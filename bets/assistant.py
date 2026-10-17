"""Assistant Intake: a separate, narrow credential for a conversational assistant (e.g. ChatGPT).

Scope: WRITE CANDIDATES ONLY. With the token a client can
  * read the field schema / OpenAPI description,
  * ask the app's calculator for lay stake and liability (pure arithmetic, no stored data),
  * submit candidates (accounts, balance checks, transfers, offers, free bets, positions, notes),
  * read back the status of candidates it submitted itself.
It cannot confirm anything, settle, delete, void, reconcile directly, export, or read balances,
profit, the ledger or history. Candidates go through imports.py like every other input and
change nothing until the user confirms them in /bets/imports.

The token (``mbai_<id>_<secret>``, 256 random bits) is stored only as a SHA-256 hash in
``var/assistant.json`` (0600, service user). It is accepted only as ``Authorization: Bearer``
on /bets/api/assistant/ (never in a URL, never on any other path) and never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path

from . import imports, times

PREFIX = "mbai_"
TOKEN_RE = re.compile(r"^mbai_([0-9a-f]{8})_[A-Za-z0-9_-]{43}$")
SUBMISSIONS_PER_HOUR = 60
MAX_PENDING = 100
MAX_BODY = 16 * 1024
STATUS = {"needs_confirmation": "pending_confirmation", "applied": "confirmed", "rejected": "rejected"}


# ---------------------------------------------------------------- the credential

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _write(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_state(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def issue(path: Path, label: str = "ChatGPT") -> str:
    """Create a new token, replacing (revoking) any previous one. Returns the plaintext once."""
    tid = secrets.token_hex(4)
    token = f"{PREFIX}{tid}_{secrets.token_urlsafe(32)}"
    _write(path, {"id": tid, "sha256": _digest(token), "label": label[:40], "created_at": times.now_iso()})
    return token


def disable(path: Path) -> str | None:
    """Revoke the token (the hash is removed). Returns the id that was disabled, if any."""
    state = read_state(path)
    if not state or not state.get("sha256"):
        return None
    _write(path, {"id": state["id"], "label": state.get("label", ""), "created_at": state.get("created_at"),
                  "disabled_at": times.now_iso()})
    return state["id"]


def status(path: Path) -> dict:
    state = read_state(path) or {}
    return {"enabled": bool(state.get("sha256")), "id": state.get("id"), "label": state.get("label"),
            "created_at": state.get("created_at"), "disabled_at": state.get("disabled_at")}


class TokenCheck:
    """Verifies bearer tokens against the hash file (re-read when it changes: rotate/disable apply at once)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stamp = None
        self._state = None

    def _current(self) -> dict | None:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            self._stamp, self._state = None, None
            return None
        if stamp != self._stamp:
            self._stamp, self._state = stamp, read_state(self.path)
        return self._state

    def verify(self, header: str | None) -> str | None:
        """'Bearer mbai_...' -> token id, or None."""
        if not header or not header.startswith("Bearer ") or len(header) > 200:
            return None
        token = header[7:].strip()
        m = TOKEN_RE.match(token)
        state = self._current()
        if not m or not state or not state.get("sha256"):
            return None
        if not hmac.compare_digest(_digest(token), state["sha256"]) or m.group(1) != state.get("id"):
            return None
        return state["id"]


class Quota:
    """Submissions per token per hour (in memory; nginx also limits requests per IP)."""

    def __init__(self, per_hour: int = SUBMISSIONS_PER_HOUR):
        self.per_hour = per_hour
        self.hits: dict[str, deque] = {}

    def take(self, key: str) -> bool:
        now = time.time()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_hour:
            return False
        q.append(now)
        return True


# ---------------------------------------------------------------- what the assistant may send

MONEY = "Amount in the dashboard's currency as a string with at most 2 decimals, e.g. \"43.27\""
WHEN = "Date/time: \"YYYY-MM-DDTHH:MM\" in UK local time, or ISO 8601 with Z/offset. Date only = end of that day."
ACCOUNT_REF = "Account name exactly as added in the app (case-insensitive), e.g. \"Betfred\""


def f(name, typ, desc, required=False, enum=None):
    d = {"name": name, "type": typ, "required": required, "description": desc}
    if enum:
        d["enum"] = list(enum)
    return d


TYPES = {
    "account": {
        "description": "Add a new bookmaker, exchange or bank (funding source). For an account that already "
                       "exists, send a 'balance' candidate instead.",
        "fields": [f("name", "string", "Account name, e.g. \"Betfred\"", True),
                   f("account_type", "string", "Kind of account", True, ("bookmaker", "exchange", "bank")),
                   f("balance", "money", "Money in the account right now (" + MONEY + "). Recorded as its opening "
                     "balance: existing capital, never profit. Not allowed for banks."),
                   f("commission", "percent", "Exchanges only: commission percentage, e.g. \"2\""),
                   f("notes", "string", "Optional notes")]},
    "balance": {
        "description": "The real current balance of an existing bookmaker/exchange account. On confirmation the "
                       "app records it as a balance check; any difference becomes a visible adjustment.",
        "fields": [f("account", "string", ACCOUNT_REF, True),
                   f("balance", "money", "Balance shown by the bookmaker/exchange now (" + MONEY + ")", True),
                   f("at", "datetime", "When the balance was seen (default: now). " + WHEN),
                   f("note", "string", "Optional note")]},
    "transfer": {
        "description": "Money moved between accounts (deposits, withdrawals). Transfers are never profit or loss. "
                       "Ask the user whether money from a bank is temporary funding (to be returned) or a permanent "
                       "bankroll contribution. The app works out how much of a withdrawal repays temporary funding.",
        "fields": [f("from", "string", ACCOUNT_REF + " (e.g. \"Lloyds\")", True),
                   f("to", "string", ACCOUNT_REF + " (e.g. \"Smarkets\")", True),
                   f("amount", "money", MONEY, True),
                   f("capital_type", "string", "Bank → betting: temporary_funding or bankroll_contribution. "
                     "Betting → bank: temporary_return, bankroll_withdrawal, or profit_withdrawal ONLY when the user says "
                     "they are taking winnings out (it is never new profit; it always waits for the user's confirmation "
                     "and is refused if larger than the profit still in the bankroll). Between betting accounts: internal. "
                     "\"temporary\" / \"permanent\" are accepted. Bank to bank is not a transfer here: temporary "
                     "funding paid back from the hub bank to the bank that lent it is a funding repayment "
                     "(record_funding_repayment). Omit only if the user does not know (the app "
                     "then assumes temporary and flags it).", False,
                     ("temporary_funding", "bankroll_contribution", "temporary_return", "bankroll_withdrawal",
                      "profit_withdrawal", "internal", "temporary", "permanent")),
                   f("at", "datetime", "When it happened (default: now). " + WHEN),
                   f("in_transit", "boolean", "Withdrawals to the bank only: true if the money has left the betting "
                     "account but not arrived in the bank yet (send an 'arrival' when it lands)"),
                   f("return_by", "datetime", "Temporary funding only: when the money has to go back to the bank. "
                     "Always ask the user for this when they move money from their bank as temporary funding. " + WHEN),
                   f("notes", "string", "Optional notes")]},
    "offer": {
        "description": "A bookmaker promotion to track.",
        "fields": [f("title", "string", "Short title, e.g. \"Bet £10 Get £30\"", True),
                   f("bookmaker", "string", ACCOUNT_REF),
                   f("offer_type", "string", "Kind of offer (default other)", False,
                     ("sign_up", "reload", "free_bet", "bet_builder", "enhanced_odds", "qualifying", "recurring", "other")),
                   f("requirement", "string", "What has to be done, e.g. \"£10 at min odds 1/2\""),
                   f("reward", "string", "What you get, e.g. \"3 x £10 free bets\""),
                   f("reward_value", "money", "Face value of the reward (" + MONEY + "). Worked out from "
                     "reward_count x reward_each when those are given."),
                   f("qualifying_stake", "money", "The stake the qualifying bet needs (" + MONEY + ")"),
                   f("min_odds", "odds", "Minimum back odds for the qualifying bet, decimal, e.g. \"2.0\""),
                   f("reward_count", "integer", "How many free bets the reward is (1 for a cash reward)"),
                   f("reward_each", "money", "Value of each free bet, or the cash amount (" + MONEY + ")"),
                   f("reward_type", "string", "snr / sr free bets, or cash", False, ("snr", "sr", "cash")),
                   f("reward_expiry_days", "integer", "Free bets expire this many days after they arrive"),
                   f("reward_trigger", "string", "What makes the reward due (default bet_settled)", False,
                     ("bet_placed", "bet_fully_matched", "bet_settled", "bet_lost", "other")),
                   f("starts_at", "datetime", WHEN), f("expires_at", "datetime", WHEN),
                   f("notes", "string", "Optional notes")]},
    "arrival": {
        "description": "A withdrawal recorded as in_transit has reached the bank. Matched by from / to / amount "
                       "(or the transfer number). Changes no balance; it only records that the money arrived.",
        "fields": [f("from", "string", ACCOUNT_REF + " (the betting account it came from)"),
                   f("to", "string", ACCOUNT_REF + " (the bank)"),
                   f("amount", "money", MONEY),
                   f("transfer", "integer", "Transfer number, if the user gives it"),
                   f("arrived_at", "datetime", "When it arrived (default: now). " + WHEN)]},
    "account_status": {
        "description": "A bookmaker or exchange's standing changed: restricted (gubbed: stakes limited or "
                       "promotions removed), closed, or fine again; or it was verified (ID checks done).",
        "fields": [f("account", "string", ACCOUNT_REF, True),
                   f("standing", "string", "ok, restricted or closed (\"gubbed\" = restricted)", False,
                     ("ok", "restricted", "closed", "gubbed")),
                   f("verified", "boolean", "true once verification is done"),
                   f("note", "string", "What happened, e.g. \"max stake £2.50 on football\"")]},
    "amend": {
        "description": "Correct something already recorded. Name the target (position, leg, offer, free_bet, transfer, "
                       "account) and how to find it, give ONLY the fields that change, and the reason. The user sees "
                       "before → after and confirms; corrections are never confirmed automatically.",
        "fields": [f("target", "string", "What to change", True, ("position", "leg", "offer", "free_bet", "transfer", "account")),
                   f("ref", "string", "Position number or event; offer number or title; free bet / transfer number; account "
                     "name. For a leg (one bet): the position number or event.", True),
                   f("side", "string", "Leg only: back or lay", False, ("back", "lay")),
                   f("account", "string", "Leg: the bet's account if the position has several on that side. Free bet / "
                     "transfer without a number: an account involved"),
                   f("value", "money", "Free bet without a number: its value (" + MONEY + ")"),
                   f("amount", "money", "Transfer without a number: its amount (" + MONEY + ")"),
                   f("event", "string", "Position: new event name"), f("selection", "string", "Position: new selection"),
                   f("market", "string", "Position: new market"), f("event_at", "datetime", "Position: new start. " + WHEN),
                   f("offer", "string", "Position / free bet: the offer it belongs to (title or number)"),
                   f("stake", "money", "Leg: new stake (" + MONEY + ")"), f("odds", "odds", "Leg: new odds"),
                   f("commission", "percent", "Leg (lay): new commission %"), f("placed_at", "datetime", "Leg: when placed. " + WHEN),
                   f("reference", "string", "Leg: bet reference"),
                   f("move_to", "string", "Leg: move it to another position (number or event) or \"new\""),
                   f("status", "string", "Offer: not_started, qualifying, awaiting_reward, reward_received, converting, "
                     "complete, expired, abandoned. Free bet: available, used, expired, void"),
                   f("expires_at", "datetime", "Offer / free bet: new expiry. " + WHEN),
                   f("title", "string", "Offer: new title"), f("requirement", "string", "Offer: new requirement"),
                   f("reward", "string", "Offer: new reward text"),
                   f("at", "datetime", "Transfer: when it really happened. " + WHEN),
                   f("return_by", "datetime", "Transfer (temporary funding): new return date. " + WHEN),
                   f("name", "string", "Account: new name"),
                   f("reason", "string", "Why (goes in the audit trail)", True)]},
    "reverse": {
        "description": "Undo something recorded by mistake: void a transfer, reverse a manual ledger entry, remove a bet "
                       "(leg), a whole position or an unused free bet, or reopen a settled position (settlement). Nothing "
                       "is deleted: entries are withdrawn and kept in the history. Always confirmed by the user.",
        "fields": [f("target", "string", "What to undo", True, ("transfer", "entry", "leg", "position", "free_bet", "settlement")),
                   f("ref", "string", "Number, or for positions/legs/settlements the event; entry needs its number", True),
                   f("side", "string", "Leg only: back or lay", False, ("back", "lay")),
                   f("account", "string", "Leg: the bet's account if needed. Free bet / transfer without a number: an account involved"),
                   f("value", "money", "Free bet without a number: its value"),
                   f("amount", "money", "Transfer without a number: its amount"),
                   f("reason", "string", "Why (goes in the audit trail)", True)]},
    "reward": {
        "description": "An offer's reward has arrived (for example the 4 x £5 free bets after a qualifying bet). On "
                       "confirmation the app creates the free bets (or the cash reward) from the offer's terms and "
                       "moves the offer on. Prefer this to separate free_bet entries when the user names the offer.",
        "fields": [f("offer", "string", "Title of the offer (as in the app) or its number", True),
                   f("received_at", "datetime", "When it arrived (default: now). " + WHEN),
                   f("expires_at", "datetime", "When the free bets expire, if the offer's terms don't say. " + WHEN),
                   f("count", "integer", "Only if different from the offer's terms: how many free bets"),
                   f("each", "money", "Only if different from the offer's terms: value of each (" + MONEY + ")"),
                   f("reward_type", "string", "Only if different from the offer's terms", False, ("snr", "sr", "cash")),
                   f("notes", "string", "Optional notes")]},
    "free_bet": {
        "description": "A free bet token credited by a bookmaker. Free bets are not cash: nothing changes in the "
                       "balance until one is used and wins.",
        "fields": [f("bookmaker", "string", ACCOUNT_REF, True),
                   f("value", "money", "Face value (" + MONEY + ")", True),
                   f("free_bet_type", "string", "snr = stake not returned (usual), sr = stake returned", False, ("snr", "sr")),
                   f("count", "integer", "How many identical free bets arrived (default 1, at most 20), e.g. 3 for 3 x £10"),
                   f("expires_at", "datetime", WHEN),
                   f("offer", "string", "Title of the offer it came from, if any"),
                   f("notes", "string", "Optional notes")]},
    "position": {
        "description": "A matched position: the bets the user has ALREADY placed themselves (this never places a "
                       "bet). Usually one back bet at a bookmaker and one lay at an exchange. Leave a lay's stake "
                       "out to have the app calculate it; the app also calculates liability and the expected result. "
                       "To add a later lay to an existing position, set add_to_position_id.",
        "fields": [f("event", "string", "Event, e.g. \"Arsenal v Spurs\"", True),
                   f("selection", "string", "What was backed, e.g. \"Arsenal\""),
                   f("market", "string", "Market, e.g. \"Match odds\""),
                   f("position_type", "string", "qualifying (default), free_bet_snr or free_bet_sr", False,
                     ("qualifying", "free_bet_snr", "free_bet_sr", "other")),
                   f("event_at", "datetime", "Kick-off/start. " + WHEN),
                   f("offer", "string", "Title of the offer this is for, if any"),
                   f("add_to_position_id", "integer", "Only when adding bets to a position the user names by number"),
                   f("hedge_strategy", "string", "intentionally_unhedged only for a tiny cash qualifier the user "
                     "deliberately did not lay (give unhedged_reason); pending if the lay is still to come", False,
                     ("matched", "intentionally_unhedged", "pending")),
                   f("unhedged_reason", "string", "Why it was deliberately not laid"),
                   f("notes", "string", "Optional notes"),
                   f("legs", "array", "The bets (1-12)", True)],
        "leg_fields": [f("account", "string", ACCOUNT_REF, True),
                       f("side", "string", "back (bookmaker or exchange) or lay (exchange only)", True, ("back", "lay")),
                       f("stake", "money", "Stake (" + MONEY + "). Required for backs; for a lay, omit to let the "
                         "app calculate it from the back bet."),
                       f("odds", "odds", "Decimal (\"4.2\") or fractional (\"16/5\") odds", True),
                       f("commission", "percent", "Lay only: exchange commission % (default: the exchange's setting)"),
                       f("use_free_bet", "boolean", "Back only: true if this back bet used a free bet at that bookmaker "
                         "(the token is chosen when the user confirms; a free-bet position_type implies it)"),
                       f("token_marked_used", "boolean", "Back only, historical repair: true only if the user says the "
                         "free bet token was already marked used by hand in the dashboard"),
                       f("placed_at", "datetime", WHEN)]},
    "settlement": {
        "description": "The result of a position the user already placed (backfilling or today's results). The "
                       "user must state the result: never guess it. The app calculates every amount; reported "
                       "figures are only checked against it. Identify the position by number, or by event (and "
                       "selection) when exactly one open position matches.",
        "fields": [f("position", "integer", "Position number, if the user gives it"),
                   f("event", "string", "Event, e.g. \"Moldova vs Faroe Islands\" (when no number is given)"),
                   f("selection", "string", "What was backed, to tell positions on the same event apart"),
                   f("settled_at", "datetime", "When the bets were settled (required). " + WHEN, True),
                   f("outcome", "string", "What happened to the backed selection", False,
                     ("back_won", "back_lost", "void")),
                   f("back_result", "string", "Instead of outcome, for exceptions (dead heat, cash-out): the back "
                     "bet's result", False, ("won", "lost", "void", "cashout", "manual")),
                   f("back_amount", "money", "Only for cashout/manual: what the bookmaker paid (" + MONEY + ")"),
                   f("lay_result", "string", "Instead of outcome, for exceptions: the lay's result (won = the "
                     "selection lost)", False, ("won", "lost", "void", "cashout", "manual")),
                   f("lay_amount", "money", "Only for cashout/manual: the lay's net amount, negative if it lost"),
                   f("reported_back_return", "money", "Optional: what the user says the bookmaker paid (checked only)"),
                   f("reported_net_result", "money", "Optional: the user's net result, e.g. \"-0.62\" (checked only)"),
                   f("notes", "string", "Optional notes")]},
    "note": {
        "description": "A note to add to an existing account, offer or position.",
        "fields": [f("target", "string", "What the note is about", True, ("account", "offer", "position")),
                   f("target_ref", "string", "Account name, offer title, or position number", True),
                   f("text", "string", "The note", True)]},
}
RESERVED = {"dry_run", "idempotency_key"}
RULES = [
    "Each submission becomes a candidate that the user confirms, edits or rejects in the app. Only if the user "
    "turned on Trusted Assistant mode for that type is a complete, unremarkable entry recorded automatically; the "
    "response then says auto_confirmed (otherwise held_for_review may say why it waits).",
    "Never say a bet has been placed. Say it is waiting for confirmation at the confirmation_url, unless the response "
    "says auto_confirmed.",
    "Do not calculate lay stakes, liabilities or profit yourself: use POST /calculate or leave the lay stake out "
    "and report the figures the app returns.",
    "Ask the user for anything required that they have not said (for transfers from a bank: temporary or permanent, "
    "and for temporary funding: when it has to go back, as return_by).",
    "Send dry_run: true to check a submission without storing it.",
    "Reuse the same idempotency_key when retrying the same submission, so it is not stored twice.",
    "Corrections go through 'amend' (change fields) and 'reverse' (undo); both always wait for the user's confirmation.",
    "This API cannot read balances, profit, the ledger or bet history, and cannot confirm, settle or delete anything.",
]


def unknown_fields(kind: str, data: dict) -> list[str]:
    spec = TYPES[kind]
    names = {x["name"] for x in spec["fields"]} | RESERVED
    out = [k for k in data if k not in names]
    if kind == "position" and isinstance(data.get("legs"), list):
        leg_names = {x["name"] for x in spec["leg_fields"]}
        out += sorted({f"legs.{k}" for leg in data["legs"] if isinstance(leg, dict) for k in leg if k not in leg_names})
    return out


def _currency() -> str:
    from .money import currency
    return currency()


def schema_doc(base_url: str) -> dict:
    return {"api": "Matched betting — Assistant Intake", "version": 1, "currency": _currency(), "base_url": base_url,
            "authentication": "Authorization: Bearer <assistant token>",
            "endpoints": {"GET /schema": "this description",
                          "GET /openapi.json": "OpenAPI 3.1 description (for ChatGPT Actions)",
                          "POST /candidates/{type}": "submit a candidate; body = the type's fields (+ dry_run, "
                                                     "idempotency_key)",
                          "GET /candidates/{candidate_id}": "status of a candidate you submitted",
                          "POST /calculate": "lay stake, liability and outcomes for a back/lay pair"},
            "rules": RULES, "money": MONEY, "datetimes": WHEN, "types": TYPES,
            "response": {"status": "pending_confirmation | valid (dry run) | rejected",
                         "candidate_id": "opaque id", "type": "candidate type",
                         "summary": "the fields as the app understood them, with its own calculations",
                         "warnings": "things the user should check", "confirmation_url": "where the user confirms"}}


def candidate_response(item: dict, base_origin: str, base_path: str) -> dict:
    c = json.loads(item["candidate_json"])
    return {"status": STATUS[item["status"]], "candidate_id": item["public_id"], "type": item["kind"],
            "summary": imports.summary(item["kind"], c), "warnings": json.loads(item["warnings_json"]),
            "edited_by_user": bool(item.get("edited_at")),
            "auto_confirmed": item.get("decided_by") == "assistant-auto",
            "held_for_review": item.get("hold_reason") if item["status"] == "needs_confirmation" else None,
            "confirmation_url": f"{base_origin}{base_path}/imports/{item['public_id']}"}


# ---------------------------------------------------------------- OpenAPI (ChatGPT Actions)

def _json_schema(field: dict) -> dict:
    typ = {"money": "string", "percent": "string", "odds": "string", "datetime": "string",
           "integer": "integer", "boolean": "boolean", "string": "string"}.get(field["type"], "string")
    d = {"type": typ, "description": field["description"]}
    if field.get("enum"):
        d["enum"] = field["enum"]
    return d


def _object(fields: list[dict], extra: dict | None = None) -> dict:
    props = {x["name"]: _json_schema(x) for x in fields if x["type"] != "array"}
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": [x["name"] for x in fields if x["required"]]}


def _cap(node, limit: int = 300):
    """ChatGPT Actions refuse long descriptions; the full text is in GET /schema."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("description", "summary") and isinstance(v, str) and len(v) > limit:
                cut = v[:limit - 1]
                v = (cut[:cut.rfind(". ") + 1] if ". " in cut else cut.rstrip() + "…")
            out[k] = _cap(v, limit)
        return out
    if isinstance(node, list):
        return [_cap(x, limit) for x in node]
    return node


def openapi(server_url: str) -> dict:
    common = {"dry_run": {"type": "boolean", "description": "Validate only; nothing is stored"},
              "idempotency_key": {"type": "string", "description": "Same key on a retry returns the first candidate"}}
    result = {"type": "object", "properties": {
        "status": {"type": "string", "enum": ["pending_confirmation", "valid", "confirmed", "rejected"]},
        "candidate_id": {"type": "string"}, "type": {"type": "string"},
        "summary": {"type": "object", "additionalProperties": True},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": {"type": "string"}},
        "confirmation_url": {"type": "string"}}}
    paths = {
        "/schema": {"get": {"operationId": "getIntakeSchema", "summary": "Accepted candidate types, fields and rules",
                            "responses": {"200": {"description": "Schema",
                                                  "content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/candidates/{candidate_id}": {"get": {
            "operationId": "getCandidateStatus", "summary": "Status of a candidate you submitted",
            "parameters": [{"name": "candidate_id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Candidate",
                                  "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result"}}}}}}},
        "/calculate": {"post": {
            "operationId": "calculateLay", "summary": "The app's lay stake, liability and outcomes for a back/lay pair",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["back_stake", "back_odds", "lay_odds"], "properties": {
                    "position_type": {"type": "string", "enum": ["qualifying", "free_bet_snr", "free_bet_sr"]},
                    "back_stake": {"type": "string", "description": MONEY},
                    "back_odds": {"type": "string"}, "lay_odds": {"type": "string"},
                    "commission": {"type": "string", "description": "Exchange commission %"},
                    "exchange": {"type": "string", "description": "Exchange name: use its stored commission"}}}}}},
            "responses": {"200": {"description": "Figures", "content": {"application/json": {"schema": {"type": "object"}}}}}}},
    }
    for kind, spec in TYPES.items():
        body = _object(spec["fields"], common)
        if kind == "position":
            body["properties"]["legs"] = {"type": "array", "minItems": 1, "maxItems": 12,
                                          "description": "The bets already placed", "items": _object(spec["leg_fields"])}
        name = "".join(p.capitalize() for p in kind.split("_"))
        paths[f"/candidates/{kind}"] = {"post": {
            "operationId": f"submit{name}Candidate", "summary": f"Submit a {kind.replace('_', ' ')} for confirmation",
            "description": spec["description"],
            "requestBody": {"required": True, "content": {"application/json": {"schema": body}}},
            "responses": {"201": {"description": "Stored, waiting for the user's confirmation",
                                  "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result"}}}},
                          "200": {"description": "Dry run or repeated idempotency key",
                                  "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result"}}}},
                          "400": {"description": "Not valid (see errors)",
                                  "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result"}}}}}}}
    return _cap({"openapi": "3.1.0",
            "info": {"title": "Matched betting — Assistant Intake", "version": "1",
                     "description": "Submit things the user tells you as candidates; the user confirms them in the "
                                    "app. Nothing is saved until then. Call getIntakeSchema for the full rules."},
            "servers": [{"url": server_url}], "paths": paths,
            "components": {"schemas": {"Result": result},
                           "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
            "security": [{"bearerAuth": []}]})
