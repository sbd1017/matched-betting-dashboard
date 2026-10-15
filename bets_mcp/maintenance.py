"""Maintenance tools: read the user's records, then correct them safely.

READ tools (get_* / list_*) return the records exactly as the dashboard holds them, with every balance labelled
(an exchange has available_cash, reserved_lay_liability and total_funds -- never one ambiguous "balance").

CHANGE tools edit existing records. Each maps to one named change in the betting app (bets/ops.py), which checks
dependencies, applies only through its audited services and answers with before -> after:
  status applied             done; tell the user exactly what changed (records before/after, totals)
  status already_done        the records were already like that; nothing was changed
  status needs_confirmation  the user's permissions say this kind of change waits for them: nothing has changed
                             yet; give them the confirmation_url
  status blocked             not possible as asked (e.g. a free bet linked to a bet can't be deleted); explain the
                             reason and the dependencies, and offer the alternative it names
  status preview             dry_run=true: exactly what would change; nothing was changed

Every change needs a request_id (idempotency): a new one per change, the same one only when retrying that exact call.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Annotated, Literal

from mcp_types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from .mixed import OAUTH, check_token
from .tools import READ, _error, _result, _who

log = logging.getLogger("bets_mcp")

EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

ChangeId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$",
                                description="Idempotency key: a NEW unique id for this change (e.g. 'link-fb27-pos16-1'). "
                                            "Reuse the same id only when retrying this exact call after a timeout or "
                                            "error; the server then returns the first result instead of applying twice.")]
Reason = Annotated[str, Field(max_length=300, description="Why, in the user's words. Required for financial changes "
                                                          "(reopen, re-settle, reverse) and deletions.")]
DryRun = Annotated[bool, Field(description="true = preview exactly what would change (before/after), change nothing")]
RecordNo = Annotated[int, Field(ge=1, description="The record's number (#) as shown by the read tools")]
WhenUK = Annotated[str, Field(max_length=25, description="UK local 'YYYY-MM-DDTHH:MM' (BST/GMT applied) or "
                                                        "'YYYY-MM-DD'; leave empty for now")]
Limit = Annotated[int, Field(ge=1, le=200, description="How many to return (newest first)")]

NOTES = {
    "applied": "Applied and recorded in the audit trail as the assistant's change. Tell the user exactly what changed: "
               "each record's before -> after and the dashboard totals that moved (issues before -> after).",
    "already_done": "Nothing changed: the records were already in that state. Say so.",
    "needs_confirmation": "NOT changed yet: the user's Assistant permissions make this wait for them. Give them the "
                          "confirmation_url; do not say it is done.",
    "blocked": "Not done. Explain the reason and the linked records to the user, and offer the alternative it names.",
    "preview": "Preview only (dry run): nothing was changed. Show the user the before -> after and ask before applying.",
}
SCHEMES: dict = {}


def _key(request_id: str) -> str:
    """Scope the user's request_id to the signed-in user (the betting app keys it by the intake token too)."""
    who = hashlib.sha256(_who().encode()).hexdigest()[:10]
    return f"mcp-{who}:{request_id}"


class LegResult(BaseModel):
    leg_id: Annotated[int, Field(ge=1, description="The bet's id (legs[].id in get_position)")]
    result: Literal["won", "lost", "void", "cashout", "manual"]
    amount: Annotated[str | None, Field(max_length=20, description="cashout / manual only: GBP actually credited (+) "
                                                                   "or debited (-)")] = None


def register(server, upstream, limits, settings) -> dict:

    def tool(name: str, title: str, annotations: ToolAnnotations):
        SCHEMES[name] = OAUTH
        return server.tool(name=name, title=title, annotations=annotations, meta={"securitySchemes": OAUTH})

    async def read(tool_name: str, resource: str, params: dict) -> CallToolResult:
        denied = check_token(settings)
        if denied:
            return denied
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down.")
        params = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in params.items()
                  if v not in (None, "", False)}
        status, data = await upstream.read(resource, params)
        log.info("tool=%s result=%s", tool_name, status)
        if status == 200:
            return _result(data)
        if status == 403:
            return _error("reading_disabled", data.get("message") or "Reading records is switched off.",
                          details=None)
        if status == 404:
            return _error("not_found", data.get("message") or data.get("error") or "Not found.")
        if status == 429:
            return _error("rate_limited", "The dashboard is limiting requests; try again later.")
        if status in (0, 401) and data.get("error") != "unreachable":
            return _error("assistant_disabled", "The intake connection on the Pi is not linked or was switched off.")
        return _error("unavailable", "The dashboard did not answer. Try again later.")

    async def change(tool_name: str, name: str, body: dict, reason: str, dry_run: bool, request_id: str) -> CallToolResult:
        denied = check_token(settings)
        if denied:
            return denied
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down and try again later.")
        body = {k: v for k, v in body.items() if v not in (None, "", False, [])}
        if reason:
            body["reason"] = reason
        if dry_run:
            body["dry_run"] = True
        body["request_id"] = _key(request_id)
        status, data = await upstream.change(name, body)
        log.info("tool=%s change=%s result=%s/%s", tool_name, name, status, data.get("status"))
        if status in (200, 202) and data.get("status") in NOTES:
            out = {k: data[k] for k in ("status", "change", "level", "summary", "effects", "warnings", "dependencies",
                                        "reason", "records", "totals", "change_id", "confirmation_url", "held_because",
                                        "duplicate", "would_need_confirmation") if k in data and data[k] not in (None, [], {})}
            out["note"] = (("Retry detected: nothing was applied twice. " if data.get("duplicate") else "")
                           + NOTES[data["status"]])
            return _result(out)
        if status == 400:
            errors = data.get("errors") or ["invalid request"]
            return _error("invalid_value", errors[0], details=[{"message": e} for e in errors] if len(errors) > 1 else None)
        if status == 409:
            return _error("duplicate_conflict", "This request_id was already used for a different change: use a new one.")
        if status == 404:
            return _error("not_found", (data.get("errors") or [data.get("error") or "Not found."])[0])
        if status == 429:
            return _error("rate_limited", "The dashboard is limiting changes; try again later.")
        if status in (0, 401) and data.get("error") != "unreachable":
            return _error("assistant_disabled", "The intake connection on the Pi is not linked or was switched off.")
        return _error("unavailable", "The dashboard did not answer; nothing was changed. Retry with the SAME request_id.")

    # ============================================================ read

    @tool("get_dashboard_totals", "Dashboard figures, labelled", READ)
    async def get_dashboard_totals() -> CallToolResult:
        """Use this first when the user asks about their overall position, and after every change to verify it:
        realised profit (settled P&L only), bookmaker cash, exchange available cash / reserved lay liability / total
        funds, temporary funding outstanding (per bank), free bets held (face value, not cash), Still to come and the
        issue count."""
        return await read("get_dashboard_totals", "dashboard", {})

    @tool("get_issues", "What needs putting right, with repairs", READ)
    async def get_issues() -> CallToolResult:
        """Use this when asked to audit or fix inconsistencies. Each issue explains what is wrong, lists the records
        and may carry `repairs`: a change tool name + params (+ its permission level). To apply one, call that change
        tool with those params and a new request_id. Re-run afterwards to confirm the issue is gone."""
        return await read("get_issues", "issues", {})

    @tool("get_still_to_come_breakdown", "Still to come, item by item", READ)
    async def get_still_to_come_breakdown() -> CallToolResult:
        """Use this when the user asks why Still to come is what it is (e.g. negative): each open position's locked-in
        result, each free bet at their retention, rewards still to arrive, and open positions whose event has
        probably finished (listed, not counted: they need settling)."""
        return await read("get_still_to_come_breakdown", "still_to_come", {})

    @tool("get_profit_breakdown", "Where realised profit comes from", READ)
    async def get_profit_breakdown() -> CallToolResult:
        """Use this when the user asks about profit: settled bets, rewards/bonus cash, reconciliation adjustments and
        manual adjustments, separately. Transfers, temporary funding and free-bet face value are never profit."""
        return await read("get_profit_breakdown", "profit", {})

    @tool("list_accounts", "Accounts with labelled balances", READ)
    async def list_accounts(type: Literal["bookmaker", "exchange", "bank"] | None = None,
                            include_inactive: bool = False) -> CallToolResult:
        """Use this to see the user's accounts and their balances, each labelled: exchanges show available_cash,
        reserved_lay_liability and total_funds; bookmakers cash_balance; banks are funding sources."""
        return await read("list_accounts", "accounts", {"type": type, "include_inactive": include_inactive})

    @tool("get_account", "One account in detail", READ)
    async def get_account(account: Annotated[str, Field(min_length=1, max_length=60, description="Account name")]) -> CallToolResult:
        """Use this for one account: labelled balances, last balance check, standing, commission (with any
        promotional end date)."""
        return await read("get_account", "account", {"ref": account})

    @tool("get_account_balance_breakdown", "What an account's balance is made of", READ)
    async def get_account_balance_breakdown(
            account: Annotated[str, Field(min_length=1, max_length=60, description="Account name")]) -> CallToolResult:
        """Use this BEFORE recording a balance the user read off a bookmaker or exchange, to know what the app
        expects for each figure (available vs total) and how much lay liability is reserved."""
        return await read("get_account_balance_breakdown", "account_balance_breakdown", {"ref": account})

    @tool("get_exchange_exposure", "Open lay bets holding liability", READ)
    async def get_exchange_exposure(
            exchange: Annotated[str, Field(min_length=1, max_length=60, description="Exchange name")]) -> CallToolResult:
        """Use this to see which open lay bets hold liability on an exchange and how much (available_cash +
        reserved_lay_liability = total_funds)."""
        return await read("get_exchange_exposure", "exchange_exposure", {"ref": exchange})

    @tool("list_positions", "Find positions", READ)
    async def list_positions(status: Literal["open", "settled", "void"] | None = None,
                             event: Annotated[str, Field(max_length=120, description="Part of the event or selection")] = "",
                             account: Annotated[str, Field(max_length=60, description="Bookmaker or exchange used")] = "",
                             limit: Limit = 30) -> CallToolResult:
        """Use this to find positions (matched bets) by status, event text or account, with every back and lay bet,
        linked offer and free bet, projection (open) or realised result (settled)."""
        return await read("list_positions", "positions", {"status": status, "event": event, "account": account,
                                                          "limit": limit})

    @tool("get_position", "One position with all its bets", READ)
    async def get_position(position: Annotated[str, Field(min_length=1, max_length=160,
                                                         description="Position number, or the event if unique")]) -> CallToolResult:
        """Use this before settling or correcting a position: every leg (id, side, account, stake, odds, liability,
        free bet, result), linked offer, projection or realised result."""
        return await read("get_position", "position", {"ref": position})

    @tool("list_free_bets", "Find free bets", READ)
    async def list_free_bets(status: Literal["available", "used", "expired", "void"] | None = None,
                             bookmaker: Annotated[str, Field(max_length=60)] = "", limit: Limit = 50) -> CallToolResult:
        """Use this to find free bets (tokens) with their status, expiry, offer, notes and the position that used
        them. A 'used' free bet without a position is an issue to fix."""
        return await read("list_free_bets", "free_bets", {"status": status, "account": bookmaker, "limit": limit})

    @tool("get_free_bet", "One free bet", READ)
    async def get_free_bet(free_bet_id: RecordNo) -> CallToolResult:
        """Use this for one free bet: value, type, status, expiry, offer, notes and linked position/bet."""
        return await read("get_free_bet", "free_bet", {"id": free_bet_id})

    @tool("list_transfers", "Find transfers (capital movements)", READ)
    async def list_transfers(account: Annotated[str, Field(max_length=60)] = "",
                             funding_type: Literal["bankroll_contribution", "bankroll_withdrawal", "temporary_funding",
                                                   "temporary_return", "profit_withdrawal", "internal"] | None = None,
                             include_reversed: bool = False, limit: Limit = 50) -> CallToolResult:
        """Use this to find deposits, withdrawals and transfers with their classification. Transfers are capital
        and never affect realised profit."""
        return await read("list_transfers", "transfers", {"account": account, "funding_type": funding_type,
                                                          "include_reversed": include_reversed, "limit": limit})

    @tool("get_transfer", "One transfer", READ)
    async def get_transfer(transfer_id: RecordNo) -> CallToolResult:
        """Use this for one transfer: accounts, amount, classification and what it means, temporary portion, return
        date, reversal state."""
        return await read("get_transfer", "transfer", {"id": transfer_id})

    @tool("list_reconciliations", "Balance checks", READ)
    async def list_reconciliations(account: Annotated[str, Field(max_length=60)] = "", limit: Limit = 30) -> CallToolResult:
        """Use this to see balance checks: figure observed (available/total), what the app expected, the difference,
        whether it became a P&L adjustment, and whether it was reversed."""
        return await read("list_reconciliations", "reconciliations", {"account": account, "limit": limit})

    @tool("get_reconciliation", "One balance check", READ)
    async def get_reconciliation(reconciliation_id: RecordNo) -> CallToolResult:
        """Use this for one balance check before reversing or resolving it."""
        return await read("get_reconciliation", "reconciliation", {"id": reconciliation_id})

    @tool("list_offers", "Offers", READ)
    async def list_offers(status: Annotated[str, Field(max_length=20)] = "", limit: Limit = 30) -> CallToolResult:
        """Use this to see offers with their status, positions, free bets and realised profit."""
        return await read("list_offers", "offers", {"status": status, "limit": limit})

    @tool("get_offer", "One offer", READ)
    async def get_offer(offer_id: RecordNo) -> CallToolResult:
        """Use this for one offer and everything linked to it."""
        return await read("get_offer", "offer", {"id": offer_id})

    @tool("get_ledger_entries", "Ledger entries", READ)
    async def get_ledger_entries(account: Annotated[str, Field(max_length=60)] = "",
                                 position_id: Annotated[int | None, Field(ge=1)] = None,
                                 category: Annotated[str, Field(max_length=30)] = "",
                                 include_superseded: bool = False, limit: Limit = 50) -> CallToolResult:
        """Use this to inspect the money movements behind a figure: each entry says whether it counts in realised
        profit. Superseded entries are history (withdrawn or replaced)."""
        return await read("get_ledger_entries", "ledger", {"account": account, "position_id": position_id,
                                                           "category": category, "include_superseded": include_superseded,
                                                           "limit": limit})

    @tool("get_record_history", "Who changed a record, and how", READ)
    async def get_record_history(record_type: Literal["free_bet", "position", "leg", "transfer", "reconciliation", "offer",
                                                      "account"], record_id: RecordNo) -> CallToolResult:
        """Use this to see a record's audit history: each change with before/after, when, and who (user, assistant,
        system)."""
        return await read("get_record_history", "history", {"entity": record_type, "id": record_id})

    @tool("get_change_status", "Was a held change confirmed?", READ)
    async def get_change_status(change_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$",
                                                                 description="change_id from a change tool")]) -> CallToolResult:
        """Use this when the user asks whether a change that needed their confirmation was confirmed."""
        denied = check_token(settings)
        if denied:
            return denied
        status, data = await upstream.change_status(change_id)
        if status == 200:
            return _result({k: data.get(k) for k in ("change_id", "change", "status", "summary", "decided_by")})
        return _error("not_found", "No such change.")

    # ============================================================ change: free bets

    @tool("update_free_bet", "Correct a free bet's details", EDIT)
    async def update_free_bet(free_bet_id: RecordNo, request_id: ChangeId,
                              expires_at: WhenUK = "", clear_expiry: bool = False,
                              notes: Annotated[str | None, Field(max_length=2000, description="Replace the notes")] = None,
                              append_note: Annotated[str, Field(max_length=1000)] = "",
                              offer_id: Annotated[int | None, Field(ge=1)] = None, clear_offer: bool = False,
                              status: Literal["available", "expired", "void"] | None = None,
                              value: Annotated[str, Field(max_length=20)] = "",
                              type: Literal["snr", "sr"] | None = None,
                              reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to correct a free bet's expiry, notes, offer, status (available / expired / void) or, if it is not
        linked to a bet, its value or type. Safe edit. To mark one used, link it to the bet instead
        (link_free_bet_to_position)."""
        return await change("update_free_bet", "update_free_bet", {
            "free_bet_id": free_bet_id, "expires_at": expires_at, "clear_expiry": clear_expiry, "notes": notes,
            "append_note": append_note, "offer_id": offer_id, "clear_offer": clear_offer, "status": status,
            "value": value, "type": type}, reason, dry_run, request_id)

    @tool("link_free_bet_to_position", "Link a free bet to the bet that used it", EDIT)
    async def link_free_bet_to_position(free_bet_id: RecordNo, position_id: RecordNo, request_id: ChangeId,
                                        leg_id: Annotated[int | None, Field(ge=1, description="Only if the position has "
                                                                                              "several matching back bets")] = None,
                                        previous_token_status: Literal["void", "available"] = "void",
                                        reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when a free bet was used on a position but the link is missing or points at the wrong (e.g.
        duplicate) token. If the bet already uses another token of the same bookmaker/value/type, it is swapped (no
        money moves; the old token becomes previous_token_status) — safe edit. If the bet was recorded as a cash bet,
        linking changes its result — financial, may need the user's confirmation."""
        return await change("link_free_bet_to_position", "link_free_bet_to_position", {
            "free_bet_id": free_bet_id, "position_id": position_id, "leg_id": leg_id,
            "previous_token_status": previous_token_status}, reason, dry_run, request_id)

    @tool("unlink_free_bet_from_position", "Take a free bet off a bet", EDIT)
    async def unlink_free_bet_from_position(free_bet_id: RecordNo, request_id: ChangeId,
                                            then_status: Literal["available", "void", "expired"] = "available",
                                            reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this only when a free bet was linked to a bet that did NOT use one: that bet then counts as a cash
        stake (financial). To fix a wrong/duplicate token, use link_free_bet_to_position with the right token
        instead."""
        return await change("unlink_free_bet_from_position", "unlink_free_bet_from_position",
                            {"free_bet_id": free_bet_id, "then_status": then_status}, reason, dry_run, request_id)

    @tool("void_free_bet", "Void a free bet", EDIT)
    async def void_free_bet(free_bet_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                            dry_run: DryRun = False) -> CallToolResult:
        """Use this for a free bet that should not count (cancelled, recorded by mistake, a duplicate not linked to
        any bet). Kept in history; stops counting in free bets held. Refused while linked to a bet."""
        return await change("void_free_bet", "void_free_bet", {"free_bet_id": free_bet_id}, reason, dry_run, request_id)

    @tool("delete_free_bet", "Delete an unreferenced free bet", DESTRUCTIVE)
    async def delete_free_bet(free_bet_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                              dry_run: DryRun = False) -> CallToolResult:
        """Use this only for a free bet that nothing refers to and that was recorded by mistake. Prefer void_free_bet
        (keeps it visible). Refused while linked to a bet. Deletions always wait for the user's confirmation."""
        return await change("delete_free_bet", "delete_free_bet", {"free_bet_id": free_bet_id}, reason, dry_run, request_id)

    @tool("merge_duplicate_free_bets", "Merge two records of one free bet", EDIT)
    async def merge_duplicate_free_bets(keep_free_bet_id: RecordNo, duplicate_free_bet_id: RecordNo, request_id: ChangeId,
                                        reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when two records describe ONE real free bet (same bookmaker, value, type): the kept record takes
        the duplicate's missing expiry/offer/notes and its bet link (no money moves); the duplicate becomes void.
        Refused if both were used by bets (then they are two real free bets)."""
        return await change("merge_duplicate_free_bets", "merge_duplicate_free_bets", {
            "keep_free_bet_id": keep_free_bet_id, "duplicate_free_bet_id": duplicate_free_bet_id}, reason, dry_run,
            request_id)

    # ============================================================ change: positions

    @tool("update_position", "Correct a position's details", EDIT)
    async def update_position(position_id: RecordNo, request_id: ChangeId,
                              event: Annotated[str | None, Field(max_length=160)] = None,
                              selection: Annotated[str | None, Field(max_length=160)] = None,
                              market: Annotated[str | None, Field(max_length=160)] = None,
                              event_time: WhenUK = "", offer_id: Annotated[int | None, Field(ge=1)] = None,
                              clear_offer: bool = False, notes: Annotated[str | None, Field(max_length=2000)] = None,
                              position_type: Literal["qualifying", "free_bet_snr", "free_bet_sr", "other"] | None = None,
                              reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to correct a position's event, selection, market, start time, offer link, notes or type. Safe
        edit (changing the type of a settled position is financial)."""
        return await change("update_position", "update_position", {
            "position_id": position_id, "event": event, "selection": selection, "market": market,
            "event_time": event_time, "offer_id": offer_id, "clear_offer": clear_offer, "notes": notes,
            "type": position_type}, reason, dry_run, request_id)

    @tool("update_leg", "Correct one bet's stake, odds or commission", EDIT)
    async def update_leg(leg_id: RecordNo, request_id: ChangeId,
                         stake: Annotated[str, Field(max_length=20)] = "", odds: Annotated[str, Field(max_length=12)] = "",
                         commission: Annotated[str, Field(max_length=6)] = "", placed_at: WhenUK = "",
                         reference: Annotated[str | None, Field(max_length=120)] = None,
                         reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to correct one bet (leg id from get_position). On an open bet it is a safe edit; on a settled bet
        its settlement is recalculated (financial)."""
        return await change("update_leg", "update_leg", {"leg_id": leg_id, "stake": stake, "odds": odds,
                                                         "commission": commission, "placed_at": placed_at,
                                                         "reference": reference}, reason, dry_run, request_id)

    @tool("settle_position", "Settle a position with the result the user gave", EDIT)
    async def settle_position(position_id: RecordNo, request_id: ChangeId,
                              outcome: Literal["back_won", "back_lost", "void"] | None = None,
                              legs: Annotated[list[LegResult] | None, Field(description="Per-bet results instead of one "
                                                                                       "outcome (e.g. several lays)")] = None,
                              outcome_bucket: Annotated[str, Field(max_length=60, description="The outcome that "
                                  "happened, as named in get_position_outcomes (e.g. \"2-2\" or \"Any other score\"); the "
                                  "server maps it to every bet's result")] = "",
                              unusual: Literal["void", "abandoned", "postponed", "dead_heat", "rule_4", "cash_out",
                                               "reduction_factor", "partial", "other"] | None = None,
                              settled_at: WhenUK = "", resettle: bool = False,
                              reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user says how a position ended. outcome back_won / back_lost / void settles every bet
        (each lay the opposite of the back); outcome_bucket settles a multi-outcome position by the outcome that
        happened; or give per-bet results in legs. Settling an open position with the user's result is a safe edit;
        a repeat is already_done. Any unusual result (void, abandoned, dead heat, Rule 4, cash out...) must be named
        in unusual and is held for the user. Changing a settled result needs resettle=true (financial). Never guess a
        result: ask the user. Find the position with list_positions / get_position first."""
        return await change("settle_position", "settle_position", {
            "position_id": position_id, "outcome": outcome,
            "legs": [l.model_dump(exclude_none=True) for l in legs] if legs else None,
            "outcome_bucket": outcome_bucket, "unusual": unusual,
            "settled_at": settled_at, "resettle": resettle}, reason, dry_run, request_id)

    @tool("reopen_position", "Reopen a settled position", DESTRUCTIVE)
    async def reopen_position(position_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                              dry_run: DryRun = False) -> CallToolResult:
        """Use this when a position was settled wrongly: its settlement entries are withdrawn (kept in history) and it
        is open again. Financial."""
        return await change("reopen_position", "reopen_position", {"position_id": position_id}, reason, dry_run, request_id)

    @tool("void_position", "Void every bet in a position", EDIT)
    async def void_position(position_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                            dry_run: DryRun = False) -> CallToolResult:
        """Use this when the bookmaker/exchange voided the bets (stakes refunded, free bets returned). Safe edit on an
        open position; financial if it was settled."""
        return await change("void_position", "void_position", {"position_id": position_id}, reason, dry_run, request_id)

    @tool("delete_position", "Delete a position recorded by mistake", DESTRUCTIVE)
    async def delete_position(position_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                              dry_run: DryRun = False) -> CallToolResult:
        """Use this only for a position that never happened (e.g. recorded twice). Its bets and ledger entries are
        withdrawn (kept in history) and any free bets become available. Always waits for the user's confirmation."""
        return await change("delete_position", "delete_position", {"position_id": position_id}, reason, dry_run, request_id)

    # ============================================================ change: transfers

    @tool("update_transfer", "Correct a transfer's time, notes or return date", EDIT)
    async def update_transfer(transfer_id: RecordNo, request_id: ChangeId, when: WhenUK = "",
                              notes: Annotated[str | None, Field(max_length=2000)] = None,
                              return_by: WhenUK = "", clear_return_by: bool = False, arrived: bool = False,
                              reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to correct when a transfer happened, its notes, the optional return-by reminder of temporary
        funding, or that a withdrawal arrived. Balances and profit never change. Safe edit."""
        return await change("update_transfer", "update_transfer", {
            "transfer_id": transfer_id, "when": when, "notes": notes, "return_by": return_by,
            "clear_return_by": clear_return_by, "arrived": arrived}, reason, dry_run, request_id)

    @tool("reclassify_transfer", "Correct what kind of money movement a transfer was", EDIT)
    async def reclassify_transfer(transfer_id: RecordNo, request_id: ChangeId,
                                  funding_type: Literal["bankroll_contribution", "bankroll_withdrawal", "temporary_funding",
                                                        "temporary_return", "profit_withdrawal", "internal"],
                                  reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user corrects a transfer's classification (e.g. it was permanent bankroll, not temporary
        funding; or it paid back temporary funding). Balances and realised profit never change; temporary funding
        outstanding is recalculated. Safe edit (involving profit_withdrawal: financial)."""
        return await change("reclassify_transfer", "reclassify_transfer",
                            {"transfer_id": transfer_id, "funding_type": funding_type}, reason, dry_run, request_id)

    @tool("reverse_transfer", "Reverse a transfer recorded by mistake", DESTRUCTIVE)
    async def reverse_transfer(transfer_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                               dry_run: DryRun = False) -> CallToolResult:
        """Use this when a transfer never happened or was recorded twice: both balances move back, kept in history.
        There is no hard delete for transfers; this is it. Financial."""
        return await change("reverse_transfer", "reverse_transfer", {"transfer_id": transfer_id}, reason, dry_run, request_id)

    # ============================================================ change: balances and balance checks

    @tool("record_balance", "Record a balance the user read off an account", EDIT)
    async def record_balance(account: Annotated[str, Field(min_length=1, max_length=60)],
                             observed: Annotated[str, Field(max_length=20, description="GBP figure exactly as shown")],
                             request_id: ChangeId,
                             figure: Literal["available", "total"] | None = None,
                             observed_at: WhenUK = "", note: Annotated[str, Field(max_length=300)] = "",
                             reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user tells you an account's balance. For an EXCHANGE, figure is required when it holds
        lay liability: 'available' is what exchanges show as available/balance (liability excluded); 'total'
        includes it. Ask the user if unsure, and check get_account_balance_breakdown first. Never posts profit/loss:
        an unexplained difference is recorded as unresolved for the user to decide."""
        return await change("record_balance", "record_balance", {"account": account, "observed": observed,
                                                                 "figure": figure, "observed_at": observed_at,
                                                                 "note": note}, reason, dry_run, request_id)

    @tool("update_reconciliation", "Correct a balance check's note", EDIT)
    async def update_reconciliation(reconciliation_id: RecordNo,
                                    note: Annotated[str, Field(max_length=500)], request_id: ChangeId,
                                    reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to change the note on a balance check."""
        return await change("update_reconciliation", "update_reconciliation",
                            {"reconciliation_id": reconciliation_id, "note": note}, reason, dry_run, request_id)

    @tool("reverse_reconciliation", "Reverse a balance check's profit/loss adjustment", DESTRUCTIVE)
    async def reverse_reconciliation(reconciliation_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                                     dry_run: DryRun = False) -> CallToolResult:
        """Use this when a balance check's P&L adjustment was wrong (e.g. the available balance was recorded as the
        total, or a deposit was counted as profit). Only that adjustment is withdrawn; transfers and settled bets
        are untouched; realised profit is recalculated. Financial."""
        return await change("reverse_reconciliation", "reverse_reconciliation", {"reconciliation_id": reconciliation_id},
                            reason, dry_run, request_id)

    @tool("resolve_reconciliation", "Decide an unresolved balance difference", EDIT)
    async def resolve_reconciliation(reconciliation_id: RecordNo,
                                     action: Literal["record_adjustment", "dismiss"], request_id: ChangeId,
                                     reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this only when the user decides about an unresolved difference: record_adjustment posts it as
        profit/loss (financial: only when the user confirms it is a real gain or loss); dismiss closes it without
        posting (e.g. timing)."""
        return await change("resolve_reconciliation", "resolve_reconciliation",
                            {"reconciliation_id": reconciliation_id, "action": action}, reason, dry_run, request_id)

    # ============================================================ change: offers

    @tool("update_offer", "Correct an offer", EDIT)
    async def update_offer(offer_id: RecordNo, request_id: ChangeId,
                           title: Annotated[str | None, Field(max_length=160)] = None,
                           status: Literal["not_started", "qualifying", "awaiting_reward", "reward_received", "converting",
                                           "complete", "expired", "abandoned"] | None = None,
                           expires_at: WhenUK = "", requirement: Annotated[str | None, Field(max_length=300)] = None,
                           reward: Annotated[str | None, Field(max_length=300)] = None,
                           notes: Annotated[str | None, Field(max_length=2000)] = None,
                           reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to correct an offer's title, status, expiry, requirement, reward or notes. Safe edit."""
        return await change("update_offer", "update_offer", {
            "offer_id": offer_id, "title": title, "status": status, "expires_at": expires_at,
            "requirement": requirement, "reward": reward, "notes": notes}, reason, dry_run, request_id)

    @tool("void_offer", "Close an offer as abandoned", EDIT)
    async def void_offer(offer_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                         dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user gives up on an offer. Its positions and free bets are unchanged."""
        return await change("void_offer", "void_offer", {"offer_id": offer_id}, reason, dry_run, request_id)

    @tool("delete_offer", "Delete an offer recorded by mistake", DESTRUCTIVE)
    async def delete_offer(offer_id: RecordNo, request_id: ChangeId, reason: Reason = "",
                           dry_run: DryRun = False) -> CallToolResult:
        """Use this only for an offer nothing refers to. Refused if positions, free bets or entries link to it (use
        void_offer). Always waits for the user's confirmation."""
        return await change("delete_offer", "delete_offer", {"offer_id": offer_id}, reason, dry_run, request_id)

    return dict(SCHEMES)
