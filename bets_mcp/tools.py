"""The plugin's tools. Each maps to exactly one Assistant Intake operation; there is no generic
request, query or shell tool. Arguments are typed (enums where the values are known) and the
betting app re-validates everything. Results are structured so ChatGPT can act on them:
success -> status pending_confirmation (or valid for a dry run); failure -> error codes such as
unknown_account / missing_field / invalid_amount with the field and value concerned.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections import deque
from typing import Annotated, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .mixed import OAUTH, OPTIONAL, check_token
from .upstream import CANDIDATE_ID, Upstream

log = logging.getLogger("bets_mcp")

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
SUBMIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

PENDING_NOTE = ("Submitted as a PENDING candidate. Nothing is recorded until the user confirms it at the "
                "confirmation_url (Confirm / Edit / Reject in /bets/). Do not say it has been recorded.")
DRY_NOTE = "Dry run: checked by the server, nothing was stored."
CONFIRMED_NOTE = ("Recorded: the user's Trusted Assistant mode confirmed this automatically (it was complete and "
                  "passed every check). Tell the user it has been recorded; it is visible at the confirmation_url.")
HELD_NOTE = "Trusted Assistant mode is on but held this for the user's review: "

# ---------------------------------------------------------------- shared argument types

Money = Annotated[str, Field(max_length=20, description="GBP amount as the user said it, e.g. \"43.27\" "
                                                       "(at most 2 decimals). Never invent or round it.")]
Odds = Annotated[str, Field(max_length=12, description="Odds as the user said them: decimal \"4.2\" or fractional \"16/5\"")]
When = Annotated[str, Field(max_length=25, description="UK local time \"YYYY-MM-DDTHH:MM\" (BST/GMT is applied), or a "
                                                      "date \"YYYY-MM-DD\". Leave empty for 'now'. Only if the user "
                                                      "said it; for something that happened today a date alone means now.")]
AccountName = Annotated[str, Field(min_length=1, max_length=60, description="Account name exactly as the user calls it "
                                                                          "(must already exist in the dashboard)")]
Notes = Annotated[str, Field(max_length=500, description="Optional note from the user")]
DryRun = Annotated[bool, Field(description="true = only check with the server, store nothing (use when the user "
                                           "asks for a dry run or check)")]
RequestId = Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9._:-]+$",
                                 description="Optional id for this logical submission; reuse it when retrying the "
                                             "same submission so it is never stored twice")]


# ---------------------------------------------------------------- errors ChatGPT can act on

FIELD_NAMES = {
    "account": {"name": "name", "account_type": "account_type", "balance": "balance", "commission": "commission"},
    "balance": {"account": "account", "balance": "balance", "at": "observed_at"},
    "transfer": {"from": "from_account", "to": "to_account", "amount": "amount", "capital_type": "funding_type",
                 "at": "when"},
    "offer": {"title": "title", "bookmaker": "bookmaker", "offer_type": "offer_type", "reward_value": "reward_value",
              "starts_at": "starts_at", "expires_at": "expires_at"},
    "free_bet": {"bookmaker": "bookmaker", "value": "value", "free_bet_type": "free_bet_type",
                 "expires_at": "expires_at", "offer": "offer"},
    "note": {"target": "target", "target_ref": "target_ref", "text": "text"},
    "reward": {"offer": "offer", "count": "count", "each": "each", "reward_type": "reward_type",
               "received_at": "received_at", "expires_at": "expires_at"},
    "arrival": {"transfer": "transfer_number", "from": "from_account", "to": "to_account", "amount": "amount",
                "arrived_at": "arrived_at"},
    "account_status": {"account": "account", "standing": "standing", "verified": "verified"},
    "amend": {"ref": "ref", "target": "target", "reason": "reason"},
    "reverse": {"ref": "ref", "target": "target", "reason": "reason"},
    "settlement": {"position": "position_number", "event": "event", "selection": "selection",
                   "settled_at": "settled_at", "outcome": "outcome", "back_result": "back_result",
                   "back_amount": "back_amount", "lay_result": "lay_result", "lay_amount": "lay_amount",
                   "reported_back_return": "reported_back_return", "reported_net_result": "reported_net_result"},
    "position": {"event": "event", "event time": "event_time", "offer": "offer", "kind": "position_type",
                 "bet 1": "bookmaker", "bet 1 stake": "back_stake", "bet 1 odds": "back_odds",
                 "bet 2": "exchange", "bet 2 stake": "lay_stake", "bet 2 odds": "lay_odds",
                 "bet 2 commission": "commission", "lay stake": "lay_stake", "legs": "bookmaker"},
}


def classify(kind: str, text: str) -> dict:
    label, _, message = text.partition(": ")
    if not message:
        label, message = "", text
    low = message.lower()
    quoted = re.search(r"'([^']*)'", message)
    if "unknown" in low and ("account" in low or "bookmaker" in low):
        code = "unknown_account"
    elif "already exists" in low:
        code = "account_exists"
    elif low.startswith("missing") or "missing" in low.split(" (")[0]:
        code = "missing_field"
    elif "odds" in label or "odds" in low:
        code = "invalid_odds"
    elif "capital_type" in label or "does not fit" in low:
        code = "invalid_funding_type"
    elif any(w in low for w in ("decimal places", "amount like", "negative", "more than zero", "implausibly")):
        code = "invalid_amount"
    elif "date/time" in low:
        code = "invalid_datetime"
    elif "bank account" in low or "exchange, not" in low or "funding source" in low:
        code = "wrong_account_kind"
    elif "commission" in label or "commission" in low:
        code = "invalid_commission"
    elif low.startswith("no ") or "no such" in low or "not available" in low:
        code = "not_found"
    else:
        code = "invalid_value"
    field = FIELD_NAMES.get(kind, {}).get(label, label or None)
    return {"error": code, "field": field, "value": quoted.group(1) if quoted else None, "message": text}


HINTS = {"unknown_account": "Ask the user whether to add this account (submit_account_candidate); once submitted, "
                            "entries that use it can be submitted too and are confirmed after it.",
         "account_exists": "This account already exists: for its current balance use submit_balance_candidate.",
         "missing_field": "Ask the user for the missing detail; do not guess it.",
         "invalid_amount": "Ask the user to restate the amount (GBP, at most 2 decimals).",
         "invalid_odds": "Ask the user to restate the odds.",
         "invalid_funding_type": "Ask the user whether this was temporary funding or a permanent bankroll movement.",
         "duplicate_conflict": "This request_id was already used for something else: use a new request_id.",
         "rate_limited": "Too many submissions: stop and tell the user to try again later.",
         "assistant_disabled": "The intake connection is switched off on the Pi; tell the user.",
         "not_found": "Check the name with the user."}


def _result(payload: dict, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                          structured_content=payload, is_error=is_error)


def _error(code: str, message: str, *, field=None, value=None, details=None) -> CallToolResult:
    payload = {"error": code, "field": field, "value": value, "message": message}
    if details:
        payload["errors"] = details
    if code in HINTS:
        payload["hint"] = HINTS[code]
    return _result(payload, is_error=True)


def _warnings(texts: list[str]) -> list[dict]:
    out = []
    for w in texts:
        code = "suspicious_value" if "unusually" in w else ("server_calculated" if "calculated by the app" in w
                                                            else "check")
        out.append({"code": code, "message": w})
    return out


# ---------------------------------------------------------------- limits and idempotency

class Limits:
    def __init__(self, per_minute: int, per_day: int):
        self.per_minute, self.per_day = per_minute, per_day
        self.calls: dict[str, deque] = {}
        self.seen: dict[str, tuple[float, str]] = {}

    def allow(self, who: str) -> bool:
        now = time.time()
        q = self.calls.setdefault(who, deque())
        while q and now - q[0] > 86400:
            q.popleft()
        if len(q) >= self.per_day or sum(1 for t in q if now - t < 60) >= self.per_minute:
            return False
        q.append(now)
        return True

    def key(self, who: str, tool: str, args: dict, request_id: str | None) -> str:
        """Same request_id -> same key. Without one, an identical call within 15 minutes is treated
        as a retry (same key), so a retried tool call never stores a second candidate."""
        if request_id:
            return f"mcp:{who}:{request_id}"
        digest = hashlib.sha256(json.dumps([tool, args], sort_keys=True).encode()).hexdigest()[:24]
        now = time.time()
        self.seen = {k: v for k, v in self.seen.items() if now - v[0] < 900}
        if f"{who}:{digest}" in self.seen:
            return self.seen[f"{who}:{digest}"][1]
        key = f"mcp-auto:{who}:{digest}:{secrets.token_hex(4)}"
        self.seen[f"{who}:{digest}"] = (now, key)
        return key


def _who() -> str:
    token = get_access_token()
    return token.subject or token.client_id if token else "anonymous"


# Mixed authentication (bets_mcp/mixed.py): structure and stateless arithmetic are open; everything that
# touches the user's dashboard (submissions, candidate status, stored commission) needs the user's sign-in.
SCHEMES = {"get_intake_schema": OPTIONAL, "calculate_match": OPTIONAL, "get_candidate_status": OAUTH,
           "submit_account_candidate": OAUTH, "submit_balance_candidate": OAUTH, "submit_transfer_candidate": OAUTH,
           "submit_position_candidate": OAUTH, "submit_offer_candidate": OAUTH, "submit_free_bet_candidate": OAUTH,
           "submit_note_candidate": OAUTH, "submit_settlement_candidate": OAUTH, "submit_reward_candidate": OAUTH,
           "submit_arrival_candidate": OAUTH, "submit_account_status_candidate": OAUTH, "submit_amend_candidate": OAUTH,
           "submit_reverse_candidate": OAUTH}


def _meta(name: str) -> dict:
    return {"securitySchemes": SCHEMES[name]}


def register(server, upstream: Upstream, limits: Limits, settings) -> dict:
    """Registers the tools; returns tool -> securitySchemes (also sent at the top level of each tool)."""

    async def submit(tool: str, kind: str, body: dict, dry_run: bool, request_id: str | None) -> CallToolResult:
        denied = check_token(settings)                      # fail closed: no valid sign-in, nothing happens
        if denied:
            log.info("tool=%s result=auth_required", tool)
            return denied
        who = _who()
        if not limits.allow(who):
            log.info("tool=%s result=rate_limited", tool)
            return _error("rate_limited", "Too many calls; slow down and try again later.")
        body = {k: v for k, v in body.items() if v not in (None, "")}
        if dry_run:
            body["dry_run"] = True
        else:
            body["idempotency_key"] = limits.key(who, tool, body, request_id)
        try:
            status, data = await upstream.submit(kind, body)
        except ValueError:
            return _error("unsupported_type", "That kind of entry is not supported.")
        return _outcome(tool, kind, status, data)

    def _outcome(tool: str, kind: str, status: int, data: dict) -> CallToolResult:
        if status in (200, 201) and data.get("status") in ("pending_confirmation", "valid", "confirmed"):
            out = {"status": data["status"], "type": kind, "summary": data.get("summary", {}),
                   "warnings": _warnings(data.get("warnings", []))}
            if data["status"] == "valid":
                out["note"] = DRY_NOTE
            else:
                out.update(candidate_id=data["candidate_id"], confirmation_url=data["confirmation_url"],
                           note=PENDING_NOTE)
                if data["status"] == "confirmed":
                    out.update(auto_confirmed=True, note=CONFIRMED_NOTE)
                elif data.get("held_for_review"):
                    out.update(held_for_review=data["held_for_review"],
                               note=HELD_NOTE + data["held_for_review"] + ". " + PENDING_NOTE)
                if data.get("duplicate"):
                    out["duplicate"] = True
                    out["note"] = "Already submitted earlier (retry detected) — not stored twice. " + out["note"]
            log.info("tool=%s type=%s candidate=%s result=%s", tool, kind, out.get("candidate_id", "-"), data["status"])
            return _result(out)
        log.info("tool=%s type=%s result=error status=%s", tool, kind, status)
        if status == 400:
            details = [classify(kind, e) for e in data.get("errors", [])] or [classify(kind, "invalid request")]
            first = details[0]
            return _error(first["error"], first["message"], field=first["field"], value=first["value"],
                          details=details if len(details) > 1 else None)
        if status == 409:
            return _error("duplicate_conflict", "This request_id was already used for a different submission.")
        if status == 429:
            return _error("rate_limited", "The dashboard is limiting submissions; try again later.")
        if status in (0, 401) and data.get("error") != "unreachable":
            return _error("assistant_disabled", "The intake connection on the Pi is not linked or was switched off.")
        if status == 404:
            return _error("not_found", "Not found.")
        return _error("unavailable", "The dashboard did not answer; nothing was stored. Try again later.")

    # ------------------------------------------------------------ read-only

    @server.tool(name="get_intake_schema", title="What details each entry needs", annotations=READ, meta=_meta("get_intake_schema"))
    async def get_intake_schema() -> CallToolResult:
        """Use this when you need to know which details a kind of entry requires (account, balance, transfer, offer,
        free bet, matched position, note) and which values are allowed, so you can ask the user only for what is
        missing. Returns structure and rules only — never balances, profit, the ledger or history."""
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down.")
        status, data = await upstream.schema()
        if status != 200:
            return _outcome("get_intake_schema", "schema", status, data)
        tools = {}
        for t in await server.list_tools():
            if t.name.startswith("submit_"):
                props = t.input_schema.get("properties", {})
                tools[t.name] = {"required": t.input_schema.get("required", []),
                                 "fields": {k: {x: v[x] for x in ("type", "enum", "description") if x in v}
                                            for k, v in props.items()}}
        return _result({"rules": data.get("rules", []), "money": "GBP", "times": "UK local time",
                        "tools": tools, "server_types": data.get("types", {})})

    @server.tool(name="calculate_match", title="Lay stake calculator (no records)", annotations=READ, meta=_meta("calculate_match"))
    async def calculate_match(
            back_stake: Money, back_odds: Odds, lay_odds: Odds,
            position_type: Annotated[Literal["qualifying", "free_bet_snr", "free_bet_sr"],
                                     Field(description="qualifying bet, or a free bet: snr = stake not returned, "
                                                       "sr = stake returned")] = "qualifying",
            commission: Annotated[str, Field(max_length=6, description="Exchange commission %, e.g. \"2\"")] = "",
            exchange: Annotated[str, Field(max_length=60, description="Exchange name to use its stored commission "
                                                                      "instead")] = "") -> CallToolResult:
        """Use this to tell the user the lay stake, liability and outcomes for a back/lay pair BEFORE or without
        recording anything. The server does the arithmetic; repeat its figures, never your own. Creates nothing.
        If commission_warning is set, tell the user: the exchange's stored promotional rate has ended and the
        figures use it until they record the rate that applies now."""
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down.")
        body = {"back_stake": back_stake, "back_odds": back_odds, "lay_odds": lay_odds, "position_type": position_type}
        if commission:
            body["commission"] = commission
        elif exchange:
            denied = check_token(settings)                  # an exchange's stored commission is the user's data
            if denied:
                return denied
            body["exchange"] = exchange
        status, data = await upstream.calculate(body)
        if status == 200:
            log.info("tool=calculate_match result=ok")
            return _result({k: data.get(k) for k in ("position_type", "commission", "lay_stake", "liability",
                                                      "if_back_wins", "if_lay_wins", "expected_result",
                                                      "retention_percent", "commission_warning")}
                           | {"note": "Calculated by the server; nothing was stored."})
        if status == 400:
            first = classify("position", (data.get("errors") or ["invalid"])[0])
            return _error(first["error"], first["message"], field=first["field"], value=first["value"])
        return _outcome("calculate_match", "calculation", status, data)

    @server.tool(name="get_candidate_status", title="Has the user confirmed it?", annotations=READ, meta=_meta("get_candidate_status"))
    async def get_candidate_status(
            candidate_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$",
                                               description="candidate_id returned by a submit tool")]) -> CallToolResult:
        """Use this when the user asks whether something you submitted was confirmed. Returns pending_confirmation,
        confirmed or rejected for candidates submitted through this assistant only."""
        denied = check_token(settings)
        if denied:
            return denied
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down.")
        if not CANDIDATE_ID.match(candidate_id):
            return _error("invalid_value", "Not a candidate id.", field="candidate_id")
        status, data = await upstream.status(candidate_id)
        if status == 200:
            log.info("tool=get_candidate_status candidate=%s result=%s", candidate_id, data.get("status"))
            return _result({k: data.get(k) for k in ("candidate_id", "type", "status", "summary", "edited_by_user",
                                                     "confirmation_url")})
        return _outcome("get_candidate_status", "status", status, data)

    # ------------------------------------------------------------ submissions (pending until the user confirms)

    @server.tool(name="submit_account_candidate", title="Add a bookmaker, exchange or bank", annotations=SUBMIT, meta=_meta("submit_account_candidate"))
    async def submit_account_candidate(
            name: Annotated[str, Field(min_length=1, max_length=60, description="Account name, e.g. \"Betfred\"")],
            account_type: Annotated[Literal["bookmaker", "exchange", "bank"],
                                    Field(description="Ask the user if not said. bank = where money comes from "
                                                      "(e.g. Lloyds)")],
            balance: Annotated[str, Field(max_length=20, description="Money in the account now, as the user said it "
                                                                     "(GBP). It is their capital, not profit. Not "
                                                                     "for banks.")] = "",
            commission: Annotated[str, Field(max_length=6, description="Exchanges only: commission %")] = "",
            notes: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user wants to add a NEW bookmaker, exchange or bank account. Ask for the account type
        and current balance if the user has not said them. If the server says the account already exists, use
        submit_balance_candidate for its balance instead. Creates a candidate the user confirms (or their Trusted Assistant mode records automatically)."""
        return await submit("submit_account_candidate", "account",
                            {"name": name, "account_type": account_type, "balance": balance,
                             "commission": commission, "notes": notes}, dry_run, request_id)

    @server.tool(name="submit_balance_candidate", title="Record an account's current balance", annotations=SUBMIT, meta=_meta("submit_balance_candidate"))
    async def submit_balance_candidate(
            account: AccountName,
            balance: Annotated[str, Field(max_length=20, description="The balance the bookmaker/exchange shows now, "
                                                                     "as the user said it (GBP)")],
            observed_at: When = "", note: Notes = "", dry_run: DryRun = False,
            request_id: RequestId = "") -> CallToolResult:
        """Use this when the user tells you the current balance of an account that ALREADY exists. It becomes a
        reconciliation candidate: on confirmation any difference is recorded visibly — the balance is never
        silently overwritten. Creates a candidate the user confirms (or their Trusted Assistant mode records automatically)."""
        return await submit("submit_balance_candidate", "balance",
                            {"account": account, "balance": balance, "at": observed_at, "note": note},
                            dry_run, request_id)

    @server.tool(name="submit_transfer_candidate", title="Record money moved between accounts", annotations=SUBMIT, meta=_meta("submit_transfer_candidate"))
    async def submit_transfer_candidate(
            from_account: AccountName, to_account: AccountName, amount: Money,
            funding_type: Annotated[Literal["temporary", "permanent", "between_betting_accounts", "profit_withdrawal"],
                                    Field(description="Ask the user if not clear. temporary = money lent from the "
                                                      "bank to bet with (or returning it); permanent = a lasting "
                                                      "bankroll contribution (or an ordinary bankroll withdrawal); "
                                                      "between_betting_accounts = bookmaker/exchange to "
                                                      "bookmaker/exchange; profit_withdrawal = winnings taken out "
                                                      "from a betting account to the bank, ONLY when the user "
                                                      "explicitly says it is winnings/profit (never assume it).")],
            when: When = "",
            return_by: Annotated[str, Field(max_length=25, description="Temporary funding from the bank only: when the "
                                                                       "money has to go back (UK local date/time). "
                                                                       "Always ask the user for it.")] = "",
            in_transit: Annotated[bool, Field(description="Withdrawal to the bank only: true if it has left the "
                                                          "betting account but not arrived yet")] = False,
            notes: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user moved their own money between accounts (deposit, withdrawal, top-up). Transfers are
        never profit or loss, including a profit withdrawal (it moves winnings already counted; it is not new
        profit). Ask whether money from the bank is temporary funding or a permanent bankroll contribution, and for
        temporary funding when it has to go back (return_by). A withdrawal to the bank is a profit_withdrawal only if
        the user says it is winnings/profit; otherwise ask whether it returns temporary funding or is an ordinary
        bankroll withdrawal. Profit withdrawals always wait for the user's confirmation, and the dashboard refuses one
        larger than the profit still in the bankroll. Creates a candidate the user confirms (or their Trusted
        Assistant mode records automatically, except profit withdrawals)."""
        capital = {"temporary": "temporary", "permanent": "permanent", "between_betting_accounts": "internal",
                   "profit_withdrawal": "profit_withdrawal"}
        return await submit("submit_transfer_candidate", "transfer",
                            {"from": from_account, "to": to_account, "amount": amount,
                             "capital_type": capital[funding_type], "at": when, "return_by": return_by,
                             "in_transit": True if in_transit else None, "notes": notes}, dry_run, request_id)

    @server.tool(name="submit_position_candidate", title="Record bets the user placed", annotations=SUBMIT, meta=_meta("submit_position_candidate"))
    async def submit_position_candidate(
            event: Annotated[str, Field(min_length=1, max_length=160, description="The event, e.g. \"Arsenal v Spurs\" "
                                                                                 "(ask if only a team was named)")],
            bookmaker: AccountName, back_stake: Money, back_odds: Odds,
            selection: Annotated[str, Field(max_length=120, description="What was backed, e.g. \"Arsenal\"")] = "",
            market: Annotated[str, Field(max_length=120, description="e.g. \"Match odds\"")] = "",
            event_time: When = "",
            position_type: Annotated[Literal["qualifying", "free_bet_snr", "free_bet_sr"],
                                     Field(description="qualifying (default) or a free bet back: snr = stake not "
                                                       "returned, sr = stake returned")] = "qualifying",
            back_used_free_bet: Annotated[bool, Field(description="true if the back bet used a free bet at that "
                                                                  "bookmaker (implied by a free_bet_* position_type); "
                                                                  "the token is chosen when the user confirms")] = False,
            free_bet_already_marked_used: Annotated[bool, Field(
                description="Historical repair only: true if the user says the free bet token was already marked "
                            "used by hand in the dashboard")] = False,
            placed_at: Annotated[str, Field(max_length=25, description="When the bets were placed, for historical "
                                                                       "bets (UK local \"YYYY-MM-DDTHH:MM\" or a "
                                                                       "date)")] = "",
            exchange: Annotated[str, Field(max_length=60, description="Exchange the lay was placed on, if any")] = "",
            lay_odds: Annotated[str, Field(max_length=12, description="Lay odds, if there is a lay")] = "",
            lay_stake: Annotated[str, Field(max_length=20, description="ONLY if the user explicitly said the stake "
                                                                       "they laid. Otherwise leave empty: the server "
                                                                       "calculates it.")] = "",
            commission: Annotated[str, Field(max_length=6, description="Exchange commission % if the user said it "
                                                                       "(default: the exchange's stored rate)")] = "",
            offer: Annotated[str, Field(max_length=160, description="Title of the offer this is for, if any")] = "",
            hedge_strategy: Annotated[Literal["matched", "intentionally_unhedged", "pending"] | None, Field(
                description="intentionally_unhedged ONLY when the user deliberately did not lay a tiny cash qualifier "
                            "(give unhedged_reason); pending if the lay is still to come")] = None,
            unhedged_reason: Annotated[str, Field(max_length=300, description="Why it was deliberately not laid, e.g. "
                                                  "\"stake only £0.05; max loss 5p\"")] = "",
            notes: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user tells you bets THEY placed themselves: a back bet at a bookmaker and usually a lay
        at an exchange (this never places a bet). Never calculate the lay stake, liability or qualifying loss — leave
        lay_stake empty and report the server's figures. A tiny qualifier deliberately left unlaid: prefer
        record_unhedged_qualifier (it also moves the offer on). Creates a candidate the user confirms (or their Trusted Assistant mode records automatically)."""
        if hedge_strategy == "intentionally_unhedged" and exchange:
            return _error("invalid_value", "hedge_strategy: intentionally_unhedged means there is no lay",
                          field="hedge_strategy", value=hedge_strategy)
        if (exchange and not lay_odds) or (lay_odds and not exchange):
            missing = "lay_odds" if exchange else "exchange"
            return _error("missing_field", f"{missing}: missing (a lay needs both the exchange and the lay odds)",
                          field=missing, value=None)
        legs = [{"account": bookmaker, "side": "back", "stake": back_stake, "odds": back_odds}]
        if back_used_free_bet or position_type != "qualifying":
            legs[0]["use_free_bet"] = True
            if free_bet_already_marked_used:
                legs[0]["token_marked_used"] = True
        if exchange:
            lay = {"account": exchange, "side": "lay", "odds": lay_odds}
            if lay_stake:
                lay["stake"] = lay_stake
            if commission:
                lay["commission"] = commission
            legs.append(lay)
        if placed_at:
            for leg in legs:
                leg["placed_at"] = placed_at
        return await submit("submit_position_candidate", "position",
                            {"event": event, "selection": selection, "market": market, "event_at": event_time,
                             "position_type": position_type, "offer": offer, "notes": notes, "legs": legs,
                             "hedge_strategy": hedge_strategy, "unhedged_reason": unhedged_reason},
                            dry_run, request_id)

    @server.tool(name="submit_offer_candidate", title="Track a bookmaker offer", annotations=SUBMIT, meta=_meta("submit_offer_candidate"))
    async def submit_offer_candidate(
            title: Annotated[str, Field(min_length=1, max_length=160, description="e.g. \"Bet £10 Get £30\"")],
            bookmaker: Annotated[str, Field(max_length=60, description="Bookmaker name, if known")] = "",
            offer_type: Annotated[Literal["sign_up", "reload", "free_bet", "bet_builder", "enhanced_odds", "qualifying",
                                          "recurring", "other"], Field(description="Kind of offer")] = "other",
            requirement: Annotated[str, Field(max_length=300, description="What must be done")] = "",
            reward: Annotated[str, Field(max_length=300, description="What is given, e.g. \"3 x £10 free bets\"")] = "",
            reward_value: Annotated[str, Field(max_length=20, description="Face value of the reward (GBP); worked out "
                                                                          "from reward_count x reward_each if given")] = "",
            qualifying_stake: Annotated[str, Field(max_length=20, description="Stake the qualifying bet needs (GBP)")] = "",
            min_odds: Annotated[str, Field(max_length=12, description="Minimum back odds to qualify, e.g. \"2.0\"")] = "",
            reward_count: Annotated[int | None, Field(ge=1, le=20, description="How many free bets (1 for cash)")] = None,
            reward_each: Annotated[str, Field(max_length=20, description="Value of each free bet, or the cash (GBP)")] = "",
            reward_type: Annotated[Literal["snr", "sr", "cash"] | None, Field(description="Free bets SNR / SR, or cash")] = None,
            reward_expiry_days: Annotated[int | None, Field(ge=1, le=365, description="Free bets expire this many days "
                                                                                       "after they arrive")] = None,
            reward_trigger: Annotated[Literal["bet_placed", "bet_fully_matched", "bet_settled", "bet_lost", "other"] | None,
                                      Field(description="What makes the reward due, from the terms: bet_placed (credited "
                                                        "once the qualifier is accepted), bet_settled (default), "
                                                        "bet_lost (money back)...")] = None,
            starts_at: When = "", expires_at: When = "", notes: Notes = "", dry_run: DryRun = False,
            request_id: RequestId = "") -> CallToolResult:
        """Use this when the user wants to track a promotion/offer. Give the terms as fields when the user says them
        (qualifying stake, minimum odds, how many free bets worth how much, SNR/SR/cash, expiry days): the dashboard
        then pre-fills bets, checks they qualify and creates the reward's free bets when it arrives. Creates a
        candidate the user confirms (or their Trusted Assistant mode records automatically)."""
        return await submit("submit_offer_candidate", "offer",
                            {"title": title, "bookmaker": bookmaker, "offer_type": offer_type, "requirement": requirement,
                             "reward": reward, "reward_value": reward_value, "qualifying_stake": qualifying_stake,
                             "min_odds": min_odds, "reward_count": reward_count, "reward_each": reward_each,
                             "reward_type": reward_type, "reward_expiry_days": reward_expiry_days,
                             "reward_trigger": reward_trigger,
                             "starts_at": starts_at, "expires_at": expires_at, "notes": notes}, dry_run, request_id)

    @server.tool(name="submit_free_bet_candidate", title="Record a free bet credited", annotations=SUBMIT, meta=_meta("submit_free_bet_candidate"))
    async def submit_free_bet_candidate(
            bookmaker: AccountName, value: Money,
            free_bet_type: Annotated[Literal["snr", "sr"], Field(description="Ask if not said: snr = stake not "
                                                                             "returned (usual), sr = stake returned")],
            expires_at: When = "",
            offer: Annotated[str, Field(max_length=160, description="Title of the offer it came from, if any")] = "",
            count: Annotated[int, Field(ge=1, le=20, description="How many identical free bets, e.g. 3 for 3 x £10")] = 1,
            notes: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when a bookmaker credited the user free bets that are not tied to a tracked offer's reward (for an
        offer the dashboard knows, prefer submit_reward_candidate). Free bets are not cash. Creates a candidate the
        user confirms (or their Trusted Assistant mode records automatically)."""
        return await submit("submit_free_bet_candidate", "free_bet",
                            {"bookmaker": bookmaker, "value": value, "free_bet_type": free_bet_type, "count": count,
                             "expires_at": expires_at, "offer": offer, "notes": notes}, dry_run, request_id)

    @server.tool(name="submit_note_candidate", title="Add a note to an account, offer or position", annotations=SUBMIT, meta=_meta("submit_note_candidate"))
    async def submit_note_candidate(
            target: Annotated[Literal["account", "offer", "position"], Field(description="What the note is about")],
            target_ref: Annotated[str, Field(min_length=1, max_length=80, description="Account name, offer title or "
                                                                                      "position number")],
            text: Annotated[str, Field(min_length=1, max_length=1000, description="The note, in the user's words")],
            dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user wants a note added to an existing account, offer or position (e.g. "ID verified at
        Betfred"). It is appended only after the user confirms it."""
        return await submit("submit_note_candidate", "note", {"target": target, "target_ref": target_ref, "text": text},
                            dry_run, request_id)

    @server.tool(name="submit_settlement_candidate", title="Record the result of bets the user placed",
                 annotations=SUBMIT, meta=_meta("submit_settlement_candidate"))
    async def submit_settlement_candidate(
            settled_at: Annotated[str, Field(min_length=4, max_length=25,
                                             description="When the bets were settled (required): UK local "
                                                         "\"YYYY-MM-DDTHH:MM\" or a date. Ask if not said.")],
            position_number: Annotated[int | None, Field(ge=1, description="The position number, if the user "
                                                                           "gives it")] = None,
            event: Annotated[str, Field(max_length=160, description="Event (when no number), e.g. \"Moldova vs "
                                                                    "Faroe Islands\"")] = "",
            selection: Annotated[str, Field(max_length=120, description="What was backed, to tell positions on "
                                                                        "the same event apart")] = "",
            outcome: Annotated[Literal["back_won", "back_lost", "void"] | None, Field(
                description="What happened to the backed selection, as the user said. Never guess it.")] = None,
            back_result: Annotated[Literal["won", "lost", "void", "cashout", "manual"] | None, Field(
                description="Instead of outcome, only for exceptions (dead heat, cash-out): the back bet's result")] = None,
            back_amount: Annotated[str, Field(max_length=20, description="Only with back_result cashout/manual: what "
                                                                         "the bookmaker paid (GBP)")] = "",
            lay_result: Annotated[Literal["won", "lost", "void", "cashout", "manual"] | None, Field(
                description="Instead of outcome, only for exceptions: the lay's result (won = the selection "
                            "lost)")] = None,
            lay_amount: Annotated[str, Field(max_length=20, description="Only with lay_result cashout/manual: the "
                                                                        "lay's net amount, negative if lost")] = "",
            reported_back_return: Annotated[str, Field(max_length=20, description="Optional: what the user says the "
                                                                                  "bookmaker paid; only checked")] = "",
            reported_net_result: Annotated[str, Field(max_length=20, description="Optional: the user's net result, "
                                                                                 "e.g. \"-0.62\"; only checked")] = "",
            notes: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user tells you how bets THEY placed were settled (including backfilling old results).
        Give the position number, or the event (and selection) of exactly one open position — or of a position you
        just submitted, which is then confirmed first. The result must come from the user: never guess it. The
        server calculates every amount; reported figures are only checked. Creates a candidate the user confirms (or
        their Trusted Assistant mode records automatically)."""
        if not position_number and not event:
            return _error("missing_field", "position: give the position number, or the event", field="position_number")
        if not outcome and not (back_result or lay_result):
            return _error("missing_field", "outcome: missing — ask the user what happened (never guessed)",
                          field="outcome")
        return await submit("submit_settlement_candidate", "settlement",
                            {"position": position_number, "event": event, "selection": selection,
                             "settled_at": settled_at, "outcome": outcome, "back_result": back_result,
                             "back_amount": back_amount, "lay_result": lay_result, "lay_amount": lay_amount,
                             "reported_back_return": reported_back_return, "reported_net_result": reported_net_result,
                             "notes": notes}, dry_run, request_id)

    @server.tool(name="submit_reward_candidate", title="Record that an offer's reward arrived", annotations=SUBMIT,
                 meta=_meta("submit_reward_candidate"))
    async def submit_reward_candidate(
            offer: Annotated[str, Field(min_length=1, max_length=160, description="Offer title (or number) as tracked")],
            received_at: When = "",
            expires_at: Annotated[str, Field(max_length=25, description="When the free bets expire, if the offer's terms "
                                                                        "don't say (ask the user)")] = "",
            count: Annotated[int | None, Field(ge=1, le=20, description="Only if different from the offer's terms")] = None,
            each: Annotated[str, Field(max_length=20, description="Only if different: value of each (GBP)")] = "",
            reward_type: Annotated[Literal["snr", "sr", "cash"] | None, Field(description="Only if different")] = None,
            notes: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user says the reward for an offer has arrived ("my Coral free bets came through"). The
        dashboard creates the free bets (or the cash reward) from the offer's terms and moves the offer on."""
        return await submit("submit_reward_candidate", "reward",
                            {"offer": offer, "received_at": received_at, "expires_at": expires_at, "count": count,
                             "each": each, "reward_type": reward_type, "notes": notes}, dry_run, request_id)

    @server.tool(name="submit_arrival_candidate", title="Record that a withdrawal reached the bank", annotations=SUBMIT,
                 meta=_meta("submit_arrival_candidate"))
    async def submit_arrival_candidate(
            amount: Annotated[str, Field(max_length=20, description="Amount of the withdrawal (GBP)")] = "",
            from_account: Annotated[str, Field(max_length=60, description="Betting account it came from")] = "",
            to_account: Annotated[str, Field(max_length=60, description="The bank")] = "",
            transfer_number: Annotated[int | None, Field(ge=1, description="Transfer number, if the user gives it")] = None,
            arrived_at: When = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when a withdrawal the user recorded as on its way has arrived in their bank. Changes no balance."""
        return await submit("submit_arrival_candidate", "arrival",
                            {"amount": amount, "from": from_account, "to": to_account, "transfer": transfer_number,
                             "arrived_at": arrived_at}, dry_run, request_id)

    @server.tool(name="submit_account_status_candidate", title="Record a bookmaker restriction, closure or verification",
                 annotations=SUBMIT, meta=_meta("submit_account_status_candidate"))
    async def submit_account_status_candidate(
            account: AccountName,
            standing: Annotated[Literal["ok", "restricted", "closed"] | None, Field(
                description="restricted = gubbed (stakes limited or promotions removed); ok = fine again")] = None,
            verified: Annotated[bool | None, Field(description="true once ID / verification is done")] = None,
            note: Notes = "", dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user says a bookmaker restricted (gubbed) or closed their account, it is fine again, or
        verification is done."""
        if standing is None and verified is None:
            return _error("missing_field", "standing / verified: give at least one", field="standing")
        return await submit("submit_account_status_candidate", "account_status",
                            {"account": account, "standing": standing, "verified": verified, "note": note},
                            dry_run, request_id)

    @server.tool(name="submit_amend_candidate", title="Correct something already recorded", annotations=SUBMIT,
                 meta=_meta("submit_amend_candidate"))
    async def submit_amend_candidate(
            target: Annotated[Literal["position", "leg", "offer", "free_bet", "transfer", "account"],
                              Field(description="What to correct; a leg is one bet inside a position")],
            ref: Annotated[str, Field(min_length=1, max_length=160, description="Position number or event; for a leg the "
                                                                                "position number or event; offer number "
                                                                                "or title; free bet / transfer number; "
                                                                                "account name")],
            reason: Annotated[str, Field(min_length=1, max_length=300, description="Why, in the user's words")],
            side: Annotated[Literal["back", "lay"] | None, Field(description="Leg only: which bet")] = None,
            account: Annotated[str, Field(max_length=60, description="Leg: the bet's account if needed; free bet / "
                                                                     "transfer without a number: an account involved")] = "",
            value: Annotated[str, Field(max_length=20, description="Free bet without a number: its value")] = "",
            amount: Annotated[str, Field(max_length=20, description="Transfer without a number: its amount")] = "",
            event: str = "", selection: str = "", market: str = "",
            event_time: Annotated[str, Field(max_length=25, description="Position: new start time")] = "",
            offer: Annotated[str, Field(max_length=160, description="Position / free bet: the offer it belongs to")] = "",
            stake: Annotated[str, Field(max_length=20, description="Leg: new stake (GBP)")] = "",
            odds: Annotated[str, Field(max_length=12, description="Leg: new odds")] = "",
            commission: Annotated[str, Field(max_length=6, description="Leg (lay): new commission %")] = "",
            placed_at: Annotated[str, Field(max_length=25, description="Leg: when it was placed")] = "",
            reference: Annotated[str, Field(max_length=120, description="Leg: bet reference")] = "",
            move_to: Annotated[str, Field(max_length=160, description="Leg: another position (number or event) or "
                                                                      "\"new\"")] = "",
            status: Annotated[str, Field(max_length=20, description="Offer or free bet status")] = "",
            expires_at: Annotated[str, Field(max_length=25, description="Offer / free bet: new expiry")] = "",
            title: Annotated[str, Field(max_length=160, description="Offer: new title")] = "",
            requirement: Annotated[str, Field(max_length=300, description="Offer: new requirement")] = "",
            reward: Annotated[str, Field(max_length=300, description="Offer: new reward text")] = "",
            when: Annotated[str, Field(max_length=25, description="Transfer: when it really happened")] = "",
            return_by: Annotated[str, Field(max_length=25, description="Transfer (temporary funding): return date")] = "",
            name: Annotated[str, Field(max_length=60, description="Account: new name")] = "",
            dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user says something recorded is wrong (wrong odds or stake, wrong match, bet on the wrong
        position, wrong date, offer status, account name). Send only the fields that change. The user sees before →
        after and must confirm it; corrections are never recorded automatically."""
        return await submit("submit_amend_candidate", "amend",
                            {"target": target, "ref": ref, "reason": reason, "side": side, "account": account,
                             "value": value, "amount": amount, "event": event, "selection": selection,
                             "market": market, "event_at": event_time, "offer": offer, "stake": stake, "odds": odds,
                             "commission": commission, "placed_at": placed_at, "reference": reference,
                             "move_to": move_to, "status": status, "expires_at": expires_at, "title": title,
                             "requirement": requirement, "reward": reward, "at": when, "return_by": return_by,
                             "name": name}, dry_run, request_id)

    @server.tool(name="submit_reverse_candidate", title="Undo something recorded by mistake", annotations=SUBMIT,
                 meta=_meta("submit_reverse_candidate"))
    async def submit_reverse_candidate(
            target: Annotated[Literal["transfer", "entry", "leg", "position", "free_bet", "settlement"],
                              Field(description="transfer = void it; entry = reverse a manual ledger entry; leg = remove "
                                                "one bet; position = remove it; free_bet = remove an unused one; "
                                                "settlement = reopen a settled position")],
            ref: Annotated[str, Field(min_length=1, max_length=160, description="Number, or for positions / legs / "
                                                                                "settlements the event")],
            reason: Annotated[str, Field(min_length=1, max_length=300, description="Why, in the user's words")],
            side: Annotated[Literal["back", "lay"] | None, Field(description="Leg only: which bet")] = None,
            account: Annotated[str, Field(max_length=60, description="Leg: the bet's account if needed; free bet / "
                                                                     "transfer without a number: an account involved")] = "",
            value: Annotated[str, Field(max_length=20, description="Free bet without a number: its value")] = "",
            amount: Annotated[str, Field(max_length=20, description="Transfer without a number: its amount")] = "",
            dry_run: DryRun = False, request_id: RequestId = "") -> CallToolResult:
        """Use this when the user wants to undo something recorded by mistake. Nothing is deleted: entries are
        withdrawn and kept in the history. The user must confirm it; undos are never recorded automatically."""
        return await submit("submit_reverse_candidate", "reverse",
                            {"target": target, "ref": ref, "reason": reason, "side": side, "account": account,
                             "value": value, "amount": amount}, dry_run, request_id)

    from . import maintenance, operations           # read + corrections; plans, placement, rewards, brief
    return {**SCHEMES, **maintenance.register(server, upstream, limits, settings),
            **operations.register(server, upstream, limits, settings)}
