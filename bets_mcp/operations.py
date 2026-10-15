"""Operating tools: the brief, next actions, bet plans from proposal to fully matched, outcome tables and hedges,
funding, evidence, expected rewards, offer progress and reviewed issues. Same contract as bets_mcp/maintenance.py:
the server calculates and validates everything; every change needs a request_id; answers say applied /
already_done / needs_confirmation / blocked / preview / conflict with before -> after.

Typical flow (screenshots -> records):
  get_operating_brief -> create_bet_plan -> validate_bet_plan (SAFE_TO_PLACE?) -> user places the bets ->
  record_bookmaker_placement / record_exchange_fill / mark_exchange_fully_matched (or record_matched_bet at once)
  -> settle_position (outcome or outcome_bucket) -> reconcile_rewards / mark_reward_received.
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from mcp_types import CallToolResult
from pydantic import BaseModel, Field

from .maintenance import EDIT, ChangeId, DryRun, Limit, Reason, RecordNo, WhenUK, _key
from .mixed import OAUTH, check_token
from .tools import READ, _error, _result, _who

log = logging.getLogger("bets_mcp")
SCHEMES: dict = {}
Money = Annotated[str, Field(max_length=20, description="Amount, e.g. \"10\" or \"8.09\"")]
OddsT = Annotated[str, Field(max_length=12, description="Decimal or fractional odds as shown, e.g. \"6.8\"")]
Outcome = Annotated[str, Field(min_length=1, max_length=60)]

NOTES = {
    "applied": "Applied (audited as the assistant's change). Report the before -> after, `result`, and any `integrity` "
               "warnings or `anomalies` to the user.",
    "already_done": "Nothing changed: already in that state (a retry or a repeat). Say so.",
    "needs_confirmation": "NOT changed yet: waits for the user's confirmation (confirmation_url).",
    "blocked": "Not done: explain the reason and the linked records; offer the alternative it names.",
    "preview": "Preview only: nothing changed.",
}


class LayIn(BaseModel):
    exchange: Annotated[str, Field(max_length=60, description="Exchange name (default: the plan's exchange)")] = ""
    odds: OddsT
    covers: Annotated[list[Outcome], Field(description="Outcome(s) this lay's selection covers (multi-outcome plans)")] = []
    stake: Annotated[str, Field(max_length=20, description="Fixed lay stake; leave empty to let the server calculate it")] = ""
    commission: Annotated[str, Field(max_length=6, description="Commission % (default: the exchange's stored rate)")] = ""
    label: Annotated[str, Field(max_length=80, description="e.g. \"Lay 1-1\"")] = ""
    market: Annotated[str, Field(max_length=80)] = ""


class LegOdds(BaseModel):
    plan_leg_id: Annotated[int, Field(ge=1)]
    odds: OddsT


class Fill(BaseModel):
    stake: Money
    odds: OddsT
    matched_at: WhenUK = ""


class LayMatched(BaseModel):
    plan_leg_id: Annotated[int | None, Field(ge=1, description="Needed when the plan has several lays")] = None
    stake: Annotated[str, Field(max_length=20, description="Total matched (empty = the plan's stake)")] = ""
    odds: Annotated[str, Field(max_length=12, description="Odds of any part not given in fills")] = ""
    fills: list[Fill] = []


class Fact(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=40, description="e.g. status, stake, odds, reference")]
    value: Annotated[str, Field(max_length=200)]


class EvidenceIn(BaseModel):
    kind: Literal["bet_slip", "bookmaker_confirmation", "exchange_confirmation", "fully_matched", "partially_matched",
                  "offer_terms", "promo_banner", "reward_credited", "free_bet_wallet", "bank_transaction", "balance",
                  "event_result", "settled_receipt", "other"]
    description: Annotated[str, Field(max_length=300)] = ""
    facts: list[Fact] = []
    captured_at: WhenUK = ""


class Liquidity(BaseModel):
    plan_leg_id: Annotated[int, Field(ge=1)]
    available: Money


class LegCovers(BaseModel):
    leg_id: Annotated[int, Field(ge=1)]
    covers: Annotated[list[Outcome], Field(min_length=1)]


class BackIn(BaseModel):
    stake: Money
    odds: OddsT
    covers: Annotated[list[Outcome], Field(description="Outcomes in which this back wins")] = []


class LayHedge(BaseModel):
    odds: OddsT
    covers: Annotated[list[Outcome], Field(description="Outcomes in which this lay's selection happens")] = []
    commission: Annotated[str, Field(max_length=6)] = "0"


def register(server, upstream, limits, settings) -> dict:

    def tool(name: str, title: str, annotations):
        SCHEMES[name] = OAUTH
        return server.tool(name=name, title=title, annotations=annotations, meta={"securitySchemes": OAUTH})

    async def read(name: str, resource: str, params: dict) -> CallToolResult:
        denied = check_token(settings)
        if denied:
            return denied
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down.")
        status, data = await upstream.read(resource, {k: str(v) for k, v in params.items() if v not in (None, "")})
        log.info("tool=%s result=%s", name, status)
        if status == 200:
            return _result(data)
        if status == 403:
            return _error("reading_disabled", data.get("message") or "Reading records is switched off.")
        if status == 404:
            return _error("not_found", data.get("message") or "Not found.")
        if status in (0, 401) and data.get("error") != "unreachable":
            return _error("assistant_disabled", "The intake connection on the Pi is not linked or was switched off.")
        return _error("unavailable", "The dashboard did not answer. Try again later.")

    async def change(name: str, body: dict, reason: str, dry_run: bool, request_id: str) -> CallToolResult:
        denied = check_token(settings)
        if denied:
            return denied
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down and try again later.")
        body = {k: v for k, v in body.items() if v not in (None, "", [], False)}
        if reason:
            body["reason"] = reason
        if dry_run:
            body["dry_run"] = True
        body["request_id"] = _key(request_id)
        status, data = await upstream.change(name, body)
        log.info("tool=%s result=%s/%s", name, status, data.get("status"))
        if status in (200, 202) and data.get("status") in NOTES:
            out = {k: data[k] for k in ("status", "change", "level", "summary", "effects", "warnings", "dependencies",
                                        "reason", "result", "records", "totals", "integrity", "anomalies", "change_id",
                                        "confirmation_url", "held_because", "duplicate", "would_need_confirmation")
                   if k in data and data[k] not in (None, [], {})}
            out["note"] = ("Retry detected: nothing was applied twice. " if data.get("duplicate") else "") + NOTES[data["status"]]
            return _result(out)
        if status == 409:
            msg = (data.get("errors") or ["conflict"])[0]
            code = "conflict" if data.get("status") == "conflict" else "duplicate_conflict"
            return _error(code, msg + (". Read the record again and retry with a new request_id." if code == "conflict" else ""))
        if status == 400:
            errors = data.get("errors") or ["invalid request"]
            return _error("invalid_value", errors[0], details=[{"message": e} for e in errors] if len(errors) > 1 else None)
        if status == 404:
            return _error("not_found", (data.get("errors") or ["Not found."])[0])
        if status == 429:
            return _error("rate_limited", "The dashboard is limiting changes; try again later.")
        if status in (0, 401) and data.get("error") != "unreachable":
            return _error("assistant_disabled", "The intake connection on the Pi is not linked or was switched off.")
        return _error("unavailable", "The dashboard did not answer; nothing was changed. Retry with the SAME request_id.")

    # ============================================================ the starting point

    @tool("get_operating_brief", "Everything needed to decide what to do next", READ)
    async def get_operating_brief() -> CallToolResult:
        """Use this FIRST in a conversation and whenever the user asks "what should we do?", "anything expiring?",
        "what have I got left today?". One call: profit, bankroll (exchange available cash / reserved liability /
        total, temporary funding), free bets by expiry with estimated worth and restrictions, offers with their stage
        and next action, expected rewards, live bet plans, open positions, Still to come, issues, ordered next actions
        and the recommended next three."""
        return await read("get_operating_brief", "operating_brief", {})

    @tool("get_next_actions", "What needs doing, in order", READ)
    async def get_next_actions() -> CallToolResult:
        """Use this for an ordered to-do list: priority, deadline, explanation, records, recommended action, value, and
        whether you (the assistant) can resolve it or the user must act outside the app (place a bet, deposit, opt in)."""
        return await read("get_next_actions", "next_actions", {})

    # ============================================================ plans: read

    @tool("list_bet_plans", "Bet plans", READ)
    async def list_bet_plans(status: Literal["active", "draft", "validated", "ready_to_place", "bookmaker_placed",
                                             "exchange_partially_matched", "fully_matched", "cancelled",
                                             "converted_to_position", "settled"] | None = "active",
                             limit: Limit = 30) -> CallToolResult:
        """Use this to see bet plans (proposed or in-progress matched bets) with status, locked-in result and retention."""
        return await read("list_bet_plans", "bet_plans", {"status": status, "limit": limit})

    @tool("get_bet_plan", "One bet plan in full", READ)
    async def get_bet_plan(plan_id: RecordNo) -> CallToolResult:
        """Use this before telling the user what to place or recording a placement: legs (plan_leg_id, stake, odds,
        covers, status, fills, unmatched), the outcome table as planned and as placed so far, rating, validation,
        funding, price revisions, evidence and version (pass it as expected_version when changing the plan)."""
        return await read("get_bet_plan", "bet_plan", {"id": plan_id})

    @tool("get_funding_requirement", "Exchange cash needed for a plan", READ)
    async def get_funding_requirement(plan_id: RecordNo) -> CallToolResult:
        """Use this when liability may exceed the exchange's free cash: available now, new liability required,
        shortfall, cash after the bet, linked funding transfers and whether temporary funding can go back."""
        return await read("get_funding_requirement", "funding_requirement", {"id": plan_id})

    @tool("get_position_outcomes", "Profit/loss of a position in every outcome", READ)
    async def get_position_outcomes(position: Annotated[str, Field(min_length=1, max_length=160)]) -> CallToolResult:
        """Use this for a position's outcome table (each bet's P&L in each outcome, worst / best case, locked or not,
        liability the exchange holds). Multi-lay positions use their verified outcome model."""
        return await read("get_position_outcomes", "position_outcomes", {"ref": position})

    @tool("calculate_multi_outcome_hedge", "Optimal lay stakes for any outcome set (no records)", READ)
    async def calculate_multi_outcome_hedge(
            backs: Annotated[list[BackIn], Field(min_length=1, max_length=6)],
            lays: Annotated[list[LayHedge], Field(min_length=1, max_length=8)],
            outcomes: Annotated[list[Outcome], Field(description="Mutually exclusive outcomes covering everything, e.g. "
                                                                 "[\"1-1\", \"2-2\", \"Any other score\"]; empty = simple "
                                                                 "two-way")] = [],
            selection: Annotated[str, Field(max_length=60)] = "",
            type: Literal["qualifying", "snr", "sr"] = "qualifying",
            max_liability: Annotated[str, Field(max_length=20, description="Cap on total lay liability (e.g. the exchange's "
                                                                           "free cash)")] = "") -> CallToolResult:
        """Use this to work out lay stakes BEFORE creating a plan, for any hedge: simple back/lay, several lays,
        correct scores, Bet Builders as several outcomes. The server equalises the result across outcomes (or scales
        to max_liability) and returns the outcome table. Nothing is stored."""
        denied = check_token(settings)
        if denied:
            return denied
        if not limits.allow(_who()):
            return _error("rate_limited", "Too many calls; slow down.")
        status, data = await upstream.hedge({"backs": [b.model_dump() for b in backs], "lays": [l.model_dump() for l in lays],
                                             "outcomes": outcomes, "selection": selection, "type": type,
                                             "max_liability": max_liability})
        if status == 200:
            return _result(data)
        return _error("invalid_value", (data.get("errors") or [data.get("error") or "invalid"])[0])

    @tool("get_retention_policy", "Retention thresholds", READ)
    async def get_retention_policy() -> CallToolResult:
        """Use this to know the user's thresholds: SNR/SR excellent / very good / acceptable / reject-below %, and the
        qualifying loss limits."""
        return await read("get_retention_policy", "retention_policy", {})

    @tool("list_expected_rewards", "Rewards an offer should pay", READ)
    async def list_expected_rewards(status: Literal["open", "waiting", "partially_received", "received", "void"] | None = "open"
                                    ) -> CallToolResult:
        """Use this to see rewards expected from offers (what, by when, overdue or not, which free bets matched them).
        Expected rewards are not money or profit."""
        return await read("list_expected_rewards", "expected_rewards", {"status": status})

    @tool("get_expected_reward", "One expected reward", READ)
    async def get_expected_reward(expected_reward_id: RecordNo) -> CallToolResult:
        """Use this for one expected reward, including free bets that could be it (candidates)."""
        return await read("get_expected_reward", "expected_reward", {"id": expected_reward_id})

    @tool("list_record_evidence", "Evidence attached to a record", READ)
    async def list_record_evidence(record_type: Literal["plan", "position", "leg", "offer", "free_bet", "transfer",
                                                        "reconciliation", "expected_reward", "account"],
                                   record_id: RecordNo) -> CallToolResult:
        """Use this to see the screenshots/receipts already recorded for a record and what they showed."""
        return await read("list_record_evidence", "evidence", {"entity": record_type, "id": record_id})

    @tool("list_dismissed_issues", "Issues marked not a problem", READ)
    async def list_dismissed_issues() -> CallToolResult:
        """Use this to see issues reviewed as false positives (and reopened ones)."""
        return await read("list_dismissed_issues", "dismissed_issues", {})

    # ============================================================ plans: planning changes

    @tool("create_bet_plan", "Plan a matched bet (no money moves)", EDIT)
    async def create_bet_plan(
            event: Annotated[str, Field(min_length=1, max_length=160)], request_id: ChangeId,
            bookmaker: Annotated[str, Field(min_length=1, max_length=60)], back_odds: OddsT,
            kind: Literal["qualifying", "free_bet_snr", "free_bet_sr", "other"] = "qualifying",
            back_stake: Annotated[str, Field(max_length=20, description="Cash stake; a free bet's stake is its value")] = "",
            exchange: Annotated[str, Field(max_length=60)] = "", lay_odds: Annotated[str, Field(max_length=12)] = "",
            commission: Annotated[str, Field(max_length=6)] = "",
            lays: Annotated[list[LayIn], Field(max_length=8, description="Several lays / multi-outcome hedges (instead of "
                                                                          "exchange + lay_odds)")] = [],
            outcomes: Annotated[list[Outcome], Field(max_length=12, description="Outcome buckets for multi-outcome hedges; "
                                                                                 "empty = simple selection / not")] = [],
            back_covers: Annotated[list[Outcome], Field(description="Outcomes in which the back wins")] = [],
            selection: Annotated[str, Field(max_length=160)] = "", market: Annotated[str, Field(max_length=120)] = "",
            event_time: WhenUK = "", free_bet_id: Annotated[int | None, Field(ge=1)] = None,
            offer_id: Annotated[int | None, Field(ge=1)] = None,
            min_retention_pct: Annotated[float | None, Field(ge=0, le=100)] = None,
            max_liability: Annotated[str, Field(max_length=20)] = "", sport: Annotated[str, Field(max_length=40)] = "",
            builder_legs: Annotated[int | None, Field(ge=1, le=30)] = None, notes: Annotated[str, Field(max_length=500)] = "",
            allow_duplicate: bool = False, reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user wants to convert a free bet or place a qualifier: the server picks the free bet (if not
        given), calculates the lay stake(s) and the result in every outcome. A plan holds no money and uses no free bet
        until bets are recorded as placed. For Bet Builders / correct scores give `outcomes`, `back_covers` and per-lay
        `covers`. Then call validate_bet_plan."""
        return await change("create_bet_plan", {
            "event": event, "bookmaker": bookmaker, "back_odds": back_odds, "kind": kind, "back_stake": back_stake,
            "exchange": exchange, "lay_odds": lay_odds, "commission": commission,
            "lays": [l.model_dump(exclude_defaults=False) for l in lays], "outcomes": outcomes, "back_covers": back_covers,
            "selection": selection, "market": market, "event_time": event_time, "free_bet_id": free_bet_id,
            "offer_id": offer_id, "min_retention_pct": min_retention_pct, "max_liability": max_liability, "sport": sport,
            "builder_legs": builder_legs, "notes": notes, "allow_duplicate": allow_duplicate}, reason, dry_run, request_id)

    @tool("update_bet_plan", "Change a plan's details", EDIT)
    async def update_bet_plan(plan_id: RecordNo, request_id: ChangeId,
                              expected_version: Annotated[int | None, Field(ge=1, description="The version you read; a "
                                                                                              "newer one means a conflict")] = None,
                              event: Annotated[str | None, Field(max_length=160)] = None,
                              selection: Annotated[str | None, Field(max_length=160)] = None,
                              market: Annotated[str | None, Field(max_length=120)] = None, event_time: WhenUK = "",
                              offer_id: Annotated[int | None, Field(ge=1)] = None,
                              free_bet_id: Annotated[int | None, Field(ge=1)] = None,
                              min_retention_pct: Annotated[float | None, Field(ge=0, le=100)] = None,
                              max_liability: Annotated[str, Field(max_length=20)] = "",
                              builder_legs: Annotated[int | None, Field(ge=1, le=30)] = None,
                              notes: Annotated[str | None, Field(max_length=2000)] = None,
                              reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this to correct a plan not placed yet (event, time, offer, free bet, minimum retention, liability cap,
        builder selections, notes). Prices change through recalculate_bet_plan."""
        return await change("update_bet_plan", {
            "plan_id": plan_id, "expected_version": expected_version, "event": event, "selection": selection,
            "market": market, "event_time": event_time, "offer_id": offer_id, "free_bet_id": free_bet_id,
            "min_retention_pct": min_retention_pct, "max_liability": max_liability, "builder_legs": builder_legs,
            "notes": notes}, reason, dry_run, request_id)

    @tool("recalculate_bet_plan", "Prices moved: recalculate a plan", EDIT)
    async def recalculate_bet_plan(plan_id: RecordNo, request_id: ChangeId, back_odds: Annotated[str, Field(max_length=12)] = "",
                                   lay_odds: Annotated[str, Field(max_length=12, description="Single-lay plans")] = "",
                                   leg_odds: Annotated[list[LegOdds], Field(description="Several lays: new odds per plan_leg_id")] = [],
                                   back_stake: Annotated[str, Field(max_length=20)] = "",
                                   expected_version: Annotated[int | None, Field(ge=1)] = None,
                                   reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user says prices moved. The old quote is kept as a price revision; lay stakes are
        recalculated; the result says previous vs current retention, the minimum and ACCEPTABLE / BELOW_MINIMUM /
        REJECT. The plan must be validated again."""
        return await change("recalculate_bet_plan", {
            "plan_id": plan_id, "back_odds": back_odds, "lay_odds": lay_odds,
            "leg_odds": {str(x.plan_leg_id): x.odds for x in leg_odds} if leg_odds else None,
            "back_stake": back_stake, "expected_version": expected_version}, reason, dry_run, request_id)

    @tool("validate_bet_plan", "Is it safe to place?", EDIT)
    async def validate_bet_plan(plan_id: RecordNo, request_id: ChangeId,
                                liquidity: Annotated[list[Liquidity], Field(description="Money available to lay at the "
                                                                                        "price, if the user said")] = [],
                                allow_started: bool = False, dry_run: DryRun = False) -> CallToolResult:
        """Use this before telling the user to place anything. Checks the offer (active, opted in, qualifying stake,
        minimum odds, market, builder selections), the free bet (available, right bookmaker/type, not expired, not
        allocated elsewhere), the bets (odds, event not started, duplicates), the exchange (free cash for the liability,
        commission) and the economics (retention vs policy, locked). result.status: SAFE_TO_PLACE, WARNING (placeable,
        tell the user the warnings) or DO_NOT_PLACE (blockers). Give the user the exact stakes and odds from the result."""
        return await change("validate_bet_plan", {"plan_id": plan_id, "allow_started": allow_started,
                                                  "liquidity": {str(x.plan_leg_id): x.available for x in liquidity} or None},
                            "", dry_run, request_id)

    @tool("cancel_bet_plan", "Cancel a plan not placed", EDIT)
    async def cancel_bet_plan(plan_id: RecordNo, request_id: ChangeId, expected_version: Annotated[int | None, Field(ge=1)] = None,
                              reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user drops a plan before placing it (frees its free bet). Refused once a bet is placed."""
        return await change("cancel_bet_plan", {"plan_id": plan_id, "expected_version": expected_version}, reason, dry_run,
                            request_id)

    # ============================================================ placement (what the user did)

    @tool("record_bookmaker_placement", "The user placed the bookmaker bet", EDIT)
    async def record_bookmaker_placement(plan_id: RecordNo, request_id: ChangeId, stake: Money = "", odds: Annotated[str, Field(max_length=12)] = "",
                                         reference: Annotated[str, Field(max_length=120)] = "", placed_at: WhenUK = "",
                                         plan_leg_id: Annotated[int | None, Field(ge=1)] = None,
                                         reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user's bet slip / confirmation shows the bookmaker bet placed. Give the CONFIRMED stake and
        odds (differences from the plan are reported). Creates the position: the cash stake leaves the bookmaker
        balance, or the free bet is used. Repeats are already_done."""
        return await change("record_bookmaker_placement", {"plan_id": plan_id, "stake": stake, "odds": odds,
                                                           "reference": reference, "placed_at": placed_at,
                                                           "plan_leg_id": plan_leg_id}, reason, dry_run, request_id)

    @tool("record_exchange_order", "A lay order was submitted", EDIT)
    async def record_exchange_order(plan_id: RecordNo, stake: Money, odds: OddsT, request_id: ChangeId,
                                    plan_leg_id: Annotated[int | None, Field(ge=1)] = None,
                                    reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user submitted a lay that is not (fully) matched yet: the unmatched part holds liability
        separately and is tracked until matched or cancelled. Matched amounts go to record_exchange_fill."""
        return await change("record_exchange_order", {"plan_id": plan_id, "stake": stake, "odds": odds,
                                                      "plan_leg_id": plan_leg_id}, reason, dry_run, request_id)

    @tool("record_exchange_fill", "Part of a lay matched", EDIT)
    async def record_exchange_fill(plan_id: RecordNo, stake: Money, odds: OddsT, request_id: ChangeId,
                                   plan_leg_id: Annotated[int | None, Field(ge=1)] = None, matched_at: WhenUK = "",
                                   allow_duplicate: bool = False, reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this for each matched part of a lay (e.g. £12 of £20 matched at 5.2, then £8 at 5.3). Each fill is its own
        lay bet with its own liability; weighted odds and outcomes are recalculated. A fill identical to the last one is
        refused unless allow_duplicate (it is probably the same screenshot twice)."""
        return await change("record_exchange_fill", {"plan_id": plan_id, "stake": stake, "odds": odds,
                                                     "plan_leg_id": plan_leg_id, "matched_at": matched_at,
                                                     "allow_duplicate": allow_duplicate}, reason, dry_run, request_id)

    @tool("cancel_exchange_order", "The unmatched part was cancelled", EDIT)
    async def cancel_exchange_order(plan_id: RecordNo, request_id: ChangeId, plan_leg_id: Annotated[int | None, Field(ge=1)] = None,
                                    reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user cancelled the unmatched part of a lay (the position stays as matched; it may not be
        fully hedged)."""
        return await change("cancel_exchange_order", {"plan_id": plan_id, "plan_leg_id": plan_leg_id}, reason, dry_run,
                            request_id)

    @tool("mark_exchange_fully_matched", "The exchange shows FULLY MATCHED", EDIT)
    async def mark_exchange_fully_matched(plan_id: RecordNo, request_id: ChangeId,
                                          confirmed_fully_matched: Annotated[bool, Field(description="true only after seeing "
                                                                                                     "FULLY MATCHED on the exchange")],
                                          plan_leg_id: Annotated[int | None, Field(ge=1)] = None,
                                          matched_stake: Annotated[str, Field(max_length=20, description="Total matched; any part "
                                                                                                        "not recorded is added")] = "",
                                          odds: Annotated[str, Field(max_length=12)] = "", matched_at: WhenUK = "",
                                          reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this only when the exchange confirmation says FULLY MATCHED (an order placed is not enough)."""
        return await change("mark_exchange_fully_matched", {
            "plan_id": plan_id, "confirmed_fully_matched": confirmed_fully_matched, "plan_leg_id": plan_leg_id,
            "matched_stake": matched_stake, "odds": odds, "matched_at": matched_at}, reason, dry_run, request_id)

    @tool("record_matched_bet", "Record the whole matched bet at once", EDIT)
    async def record_matched_bet(plan_id: RecordNo, request_id: ChangeId,
                                 confirmed_fully_matched: Annotated[bool, Field(description="true only when every lay is "
                                                                                            "FULLY MATCHED")],
                                 bookmaker_stake: Money = "", bookmaker_odds: Annotated[str, Field(max_length=12)] = "",
                                 bookmaker_reference: Annotated[str, Field(max_length=120)] = "", placed_at: WhenUK = "",
                                 lays: list[LayMatched] = [], evidence: list[EvidenceIn] = [],
                                 funding_transfer_id: Annotated[int | None, Field(ge=1)] = None,
                                 reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user sends both confirmations together: bookmaker bet + every lay fill + fully matched +
        free bet used + evidence, in ONE all-or-nothing change (if any part fails nothing is recorded). Idempotent: a
        repeat never creates a second position, uses a second free bet or doubles liability."""
        return await change("record_matched_bet", {
            "plan_id": plan_id, "confirmed_fully_matched": confirmed_fully_matched,
            "bookmaker": {"stake": bookmaker_stake, "odds": bookmaker_odds, "reference": bookmaker_reference,
                          "placed_at": placed_at},
            "lays": [{"plan_leg_id": l.plan_leg_id, "stake": l.stake, "odds": l.odds,
                      "fills": [f.model_dump() for f in l.fills]} for l in lays],
            "evidence": [{"kind": e.kind, "description": e.description, "captured_at": e.captured_at,
                          "facts": {f.name: f.value for f in e.facts}} for e in evidence],
            "funding_transfer_id": funding_transfer_id}, reason, dry_run, request_id)

    @tool("convert_bet_plan_to_position", "Placed exactly as planned", EDIT)
    async def convert_bet_plan_to_position(plan_id: RecordNo, request_id: ChangeId,
                                           confirmed_placed_as_planned: bool, reason: Reason = "",
                                           dry_run: DryRun = False) -> CallToolResult:
        """Use this only when the user confirms both bets were placed at the planned stakes and odds (no screenshots to
        check): records the position as planned."""
        return await change("convert_bet_plan_to_position", {"plan_id": plan_id,
                                                             "confirmed_placed_as_planned": confirmed_placed_as_planned},
                            reason, dry_run, request_id)

    # ============================================================ funding, evidence

    @tool("record_funding_transfer", "Money the user moved to fund a bet", EDIT)
    async def record_funding_transfer(from_account: Annotated[str, Field(min_length=1, max_length=60)],
                                      to_account: Annotated[str, Field(min_length=1, max_length=60)], amount: Money,
                                      request_id: ChangeId,
                                      funding_type: Literal["temporary_funding", "bankroll_contribution"] = "temporary_funding",
                                      plan_id: Annotated[int | None, Field(ge=1)] = None, when: WhenUK = "",
                                      return_by: WhenUK = "", notes: Annotated[str, Field(max_length=300)] = "",
                                      allow_duplicate: bool = False, reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user says they moved money in to cover liability ("I'll put £75 in from Lloyds
        temporarily" -> after they did it). Linked to the plan, the next actions say when it can go back. Capital only:
        never profit. Never withdraws anything."""
        return await change("record_funding_transfer", {
            "from_account": from_account, "to_account": to_account, "amount": amount, "funding_type": funding_type,
            "plan_id": plan_id, "when": when, "return_by": return_by, "notes": notes, "allow_duplicate": allow_duplicate},
            reason, dry_run, request_id)

    @tool("add_record_evidence", "Record what a screenshot shows", EDIT)
    async def add_record_evidence(record_type: Literal["plan", "position", "leg", "offer", "free_bet", "transfer",
                                                       "reconciliation", "expected_reward", "account"],
                                  record_id: RecordNo, request_id: ChangeId,
                                  kind: Literal["bet_slip", "bookmaker_confirmation", "exchange_confirmation", "fully_matched",
                                                "partially_matched", "offer_terms", "promo_banner", "reward_credited",
                                                "free_bet_wallet", "bank_transaction", "balance", "event_result",
                                                "settled_receipt", "other"],
                                  description: Annotated[str, Field(max_length=300)] = "",
                                  facts: Annotated[list[Fact], Field(description="What it shows, e.g. status=FULLY MATCHED, "
                                                                                 "stake=8.09, odds=6.8")] = [],
                                  captured_at: WhenUK = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this after reading a screenshot the user sent: record what it proves on the record it supports (the
        user can upload the image itself in the app). The same evidence twice is recognised, not duplicated."""
        return await change("add_record_evidence", {"record_type": record_type, "record_id": record_id, "kind": kind,
                                                    "description": description, "captured_at": captured_at,
                                                    "facts": {f.name: f.value for f in facts}}, "", dry_run, request_id)

    # ============================================================ rewards and offers

    @tool("expect_reward", "Expect an offer's reward", EDIT)
    async def expect_reward(request_id: ChangeId, offer_id: Annotated[int | None, Field(ge=1)] = None,
                            bookmaker: Annotated[str, Field(max_length=60)] = "",
                            reward_type: Literal["snr", "sr", "cash"] | None = None,
                            count: Annotated[int | None, Field(ge=1, le=20)] = None, each: Annotated[str, Field(max_length=20)] = "",
                            expected_by: WhenUK = "", expiry_days: Annotated[int | None, Field(ge=1, le=365)] = None,
                            position_id: Annotated[int | None, Field(ge=1)] = None,
                            notes: Annotated[str, Field(max_length=300)] = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when a qualifier is done and a reward is due (normally automatic when a qualifying position of an offer
        with reward terms settles). Not money; matched to the free bets when they arrive."""
        return await change("expect_reward", {"offer_id": offer_id, "bookmaker": bookmaker, "reward_type": reward_type,
                                              "count": count, "each": each, "expected_by": expected_by,
                                              "expiry_days": expiry_days, "position_id": position_id, "notes": notes},
                            "", dry_run, request_id)

    @tool("reconcile_rewards", "Match expected rewards to free bets that arrived", EDIT)
    async def reconcile_rewards(request_id: ChangeId, expected_reward_id: Annotated[int | None, Field(ge=1)] = None,
                                dry_run: DryRun = False) -> CallToolResult:
        """Use this after free bets arrive (or to check): links unambiguous matches, reports ambiguous ones. Never
        creates free bets."""
        return await change("reconcile_rewards", {"expected_reward_id": expected_reward_id}, "", dry_run, request_id)

    @tool("mark_reward_received", "An expected reward arrived", EDIT)
    async def mark_reward_received(expected_reward_id: RecordNo, request_id: ChangeId,
                                   free_bet_ids: Annotated[list[int], Field(description="Free bets already recorded that are "
                                                                                        "this reward")] = [],
                                   count: Annotated[int | None, Field(ge=1, le=20)] = None, expires_at: WhenUK = "",
                                   received_at: WhenUK = "", notes: Annotated[str, Field(max_length=300)] = "",
                                   reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user shows the reward credited. Links matching free bets already recorded and creates only
        the missing ones -- the same reward reported twice is never recorded twice."""
        return await change("mark_reward_received", {"expected_reward_id": expected_reward_id, "free_bet_ids": free_bet_ids,
                                                     "count": count, "expires_at": expires_at, "received_at": received_at,
                                                     "notes": notes}, reason, dry_run, request_id)

    @tool("update_offer_progress", "Offer terms and progress (opt-in, deposit...)", EDIT)
    async def update_offer_progress(offer_id: RecordNo, request_id: ChangeId,
                                    opted_in: bool | None = None, deposit_done: bool | None = None, reviewed: bool | None = None,
                                    opt_in_required: bool | None = None, deposit_required: bool | None = None,
                                    qualifying_stake: Annotated[str, Field(max_length=20)] = "",
                                    min_odds: Annotated[str, Field(max_length=12)] = "",
                                    reward_count: Annotated[int | None, Field(ge=1, le=20)] = None,
                                    reward_each: Annotated[str, Field(max_length=20)] = "",
                                    reward_type: Literal["snr", "sr", "cash"] | None = None,
                                    reward_expiry_days: Annotated[int | None, Field(ge=1, le=365)] = None,
                                    reward_delay_hours: Annotated[int | None, Field(ge=1, le=720)] = None,
                                    required_sport: Annotated[str | None, Field(max_length=40)] = None,
                                    required_market: Annotated[str | None, Field(max_length=80)] = None,
                                    min_legs: Annotated[int | None, Field(ge=1, le=30)] = None, when: WhenUK = "",
                                    qualified: Annotated[bool | None, Field(description="true when the bookmaker itself "
                                                                            "shows the qualifier as done")] = None,
                                    reward_trigger: Literal["bet_placed", "bet_fully_matched", "bet_settled", "bet_lost",
                                                            "other"] | None = None,
                                    reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this after reading offer terms or when the user opted in / deposited / the bookmaker shows the
        qualifier done: structured terms and facts drive validation and the offer's lifecycle (result = its status,
        stage and next action). The status itself always follows the records; never set it to move an offer on."""
        return await change("update_offer_progress", {
            "offer_id": offer_id, "opted_in": opted_in, "deposit_done": deposit_done, "reviewed": reviewed,
            "qualified": qualified, "reward_trigger": reward_trigger,
            "opt_in_required": opt_in_required, "deposit_required": deposit_required, "qualifying_stake": qualifying_stake,
            "min_odds": min_odds, "reward_count": reward_count, "reward_each": reward_each, "reward_type": reward_type,
            "reward_expiry_days": reward_expiry_days, "reward_delay_hours": reward_delay_hours,
            "required_sport": required_sport, "required_market": required_market, "min_legs": min_legs, "when": when},
            reason, dry_run, request_id)

    # ============================================================ outcomes, issues

    @tool("set_position_outcomes", "Define which outcomes each bet covers", EDIT)
    async def set_position_outcomes(position_id: RecordNo, request_id: ChangeId,
                                    outcomes: Annotated[list[Outcome], Field(min_length=2, max_length=12)],
                                    legs: Annotated[list[LegCovers], Field(min_length=2)],
                                    reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this for a position with several lays on different outcomes (correct scores, Bet Builders): give the
        mutually exclusive outcomes and, for every bet (leg id from get_position), the outcomes it covers. Liability and
        projections then use the real outcomes instead of assuming every lay loses together."""
        return await change("set_position_outcomes", {"position_id": position_id, "outcomes": outcomes,
                                                      "legs": [l.model_dump() for l in legs]}, reason, dry_run, request_id)

    @tool("dismiss_issue", "Mark an issue reviewed: not a problem", EDIT)
    async def dismiss_issue(issue_key: Annotated[str, Field(min_length=3, max_length=120)], reason: Reason,
                            request_id: ChangeId, dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user confirms an issue is a false positive (e.g. two genuine transfers flagged as a
        duplicate). It stops showing while its facts stay the same and comes back automatically if they change.
        Nothing else changes. Never use it to hide a real problem."""
        return await change("dismiss_issue", {"issue_key": issue_key}, reason, dry_run, request_id)

    @tool("acknowledge_issue", "Mark an issue reviewed: not a problem", EDIT)
    async def acknowledge_issue(issue_key: Annotated[str, Field(min_length=3, max_length=120)], reason: Reason,
                                request_id: ChangeId, dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user has reviewed an issue and says it is not a problem (same as dismiss_issue). It
        stays out of the list until its facts change, and stays in the history."""
        return await change("dismiss_issue", {"issue_key": issue_key}, reason, dry_run, request_id)

    @tool("record_unhedged_qualifier", "Record a tiny qualifier deliberately not laid", EDIT)
    async def record_unhedged_qualifier(
            offer_id: RecordNo, event: Annotated[str, Field(min_length=1, max_length=160)], stake: Money, odds: OddsT,
            reason: Annotated[str, Field(min_length=3, max_length=300, description="Why it was deliberately not laid, "
                                         "e.g. \"Qualifier stake only £0.05; maximum loss 5p\"")],
            request_id: ChangeId,
            selection: Annotated[str, Field(max_length=120)] = "", market: Annotated[str, Field(max_length=120)] = "",
            bookmaker: Annotated[str, Field(max_length=60, description="Default: the offer's bookmaker")] = "",
            reference: Annotated[str, Field(max_length=120, description="Bookmaker bet reference, if shown")] = "",
            placed_at: WhenUK = "", event_time: WhenUK = "",
            evidence: list[EvidenceIn] = [],
            reward_credited: Annotated[bool, Field(description="true if the user's screenshot already shows the reward "
                                                               "credited (e.g. Sky Bet's 3 x £10 straight after the "
                                                               "bet): it is recorded and linked in the same change")] = False,
            reward_free_bet_ids: Annotated[list[int], Field(description="Free bets already recorded that are this "
                                                                        "reward (otherwise they are created)")] = [],
            reward_expires_at: WhenUK = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user placed a small cash qualifying bet for a tracked offer and deliberately did NOT lay
        it (e.g. a £0.05 first bet). One all-or-nothing change: records the bookmaker bet (cash -> unsettled stake),
        links it to the offer, moves the offer on by its reward trigger, creates the expected reward and, with
        reward_credited, records and links the reward without duplicates. No exchange leg is ever created. Above the
        user's unhedged limit it waits for them. Repeats are already_done."""
        return await change("record_unhedged_qualifier", {
            "offer_id": offer_id, "event": event, "stake": stake, "odds": odds, "reason": reason, "selection": selection,
            "market": market, "bookmaker": bookmaker, "reference": reference, "placed_at": placed_at,
            "event_time": event_time,
            "evidence": [{"kind": e.kind, "description": e.description, "facts": {f.name: f.value for f in e.facts}}
                         for e in evidence],
            "reward_credited": reward_credited, "reward_free_bet_ids": reward_free_bet_ids,
            "reward_expires_at": reward_expires_at}, "", dry_run, request_id)

    @tool("set_hedge_strategy", "Say how a position is hedged", EDIT)
    async def set_hedge_strategy(position_id: RecordNo, request_id: ChangeId,
                                 hedge_strategy: Literal["matched", "intentionally_unhedged", "pending"],
                                 reason: Annotated[str, Field(max_length=300, description="Required for "
                                                              "intentionally_unhedged")] = "",
                                 dry_run: DryRun = False) -> CallToolResult:
        """Use this when an existing qualifying position has no lay and the user says that was deliberate (a tiny
        stake), or that the lay is still to come (pending)."""
        return await change("set_hedge_strategy", {"position_id": position_id, "hedge_strategy": hedge_strategy,
                                                   "reason": reason}, "", dry_run, request_id)

    @tool("mark_alerts_notified", "Record that reminders were sent", EDIT)
    async def mark_alerts_notified(alert_keys: Annotated[list[Annotated[str, Field(min_length=3, max_length=160)]],
                                                         Field(min_length=1, max_length=50)],
                                   request_id: ChangeId, dry_run: DryRun = False) -> CallToolResult:
        """Use this after you have told the user about alerts from get_operating_brief's alerts_to_send (or
        get_next_actions items with already_notified false): pass their alert_key values so the same reminder is not
        repeated. An alert is offered again only if it escalates. Nothing else changes."""
        return await change("mark_alerts_notified", {"alert_keys": alert_keys}, "", dry_run, request_id)

    @tool("reopen_issue", "Show a dismissed issue again", EDIT)
    async def reopen_issue(issue_key: Annotated[str, Field(min_length=3, max_length=120)], request_id: ChangeId,
                           reason: Reason = "", dry_run: DryRun = False) -> CallToolResult:
        """Use this when the user wants a dismissed issue back on the list."""
        return await change("reopen_issue", {"issue_key": issue_key}, reason, dry_run, request_id)

    return dict(SCHEMES)

