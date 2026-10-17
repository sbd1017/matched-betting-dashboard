# Assistant handover: what changed and how to use it

For ChatGPT (Matched Betting Assistant plugin / custom GPT) and other AI assistants that record matched-betting
activity in https://example.com/bets/. The plugin's built-in instructions
(`plugin/matched-betting-assistant/skills/matched-betting-intake/SKILL.md`) already contain all of this; this
page is the human-readable summary to paste or point an assistant at.

## The rules

- **New entries** (submit_* tools) are **pending candidates** the user confirms, edits or rejects at /bets/imports;
  Trusted Assistant mode can confirm complete, unremarkable ones automatically, except corrections, undos and
  profit withdrawals.
- **Reading** (get_* / list_* tools): the assistant can read balances, positions, free bets, transfers, balance
  checks, offers, the ledger, profit, Still to come and issues, unless the user switches reading off
  (Settings → Assistant permissions). Every balance is labelled; an exchange has `available_cash`,
  `reserved_lay_liability` and `total_funds`, never one ambiguous balance.
- **Maintaining** (named change tools): edits, links, settlements, voids, reversals and deletions of existing
  records, each checked against its dependencies and applied through the audited services. Permission levels:
  **safe edits** (default: automatic), **financial reversals** (default: ask the user), **deletions** (always ask).
  A held change answers `needs_confirmation` with a link; nothing changes until the user confirms it.
- Every change needs a `request_id` (idempotency: a repeat returns the first result) and answers with
  before → after for each record and the dashboard figures that moved.
- The assistant never places bets and never calculates lay stakes / liability / profit itself (the server does).
- Transfers, temporary funding, free-bet face value and open liability never change realised profit; a balance
  check never posts profit/loss unless the user says the difference is a real gain or loss.

## Maintenance tools

| Tool | Level | Use it when |
|---|---|---|
| `update_free_bet` | safe | expiry, notes, offer, status (available / expired / void), value/type if unlinked |
| `link_free_bet_to_position` | safe (swap) / financial (cash bet) | a free bet used on a position isn't linked, or a duplicate token is |
| `merge_duplicate_free_bets` | safe | two records of one real free bet: keep one, move the link, void the other |
| `unlink_free_bet_from_position` | financial | a bet that did NOT use a free bet was linked to one |
| `void_free_bet` / `delete_free_bet` | safe / deletion | stop it counting / remove a mistaken, unreferenced record |
| `update_position`, `update_leg` | safe (financial on settled bets) | wrong event, time, offer, stake, odds, commission |
| `settle_position` | safe (financial to re-settle) | the user says how it ended (outcome or per-bet results) |
| `reopen_position`, `void_position`, `delete_position` | financial / safe-or-financial / deletion | wrong settlement / bookmaker voided / never happened |
| `update_transfer`, `reclassify_transfer` | safe | time, notes, return-by reminder; permanent vs temporary vs return |
| `reverse_transfer` | financial | it never happened or was recorded twice |
| `record_balance` | safe | the user read a balance (`figure` available/total for exchanges); never posts P&L |
| `reverse_reconciliation`, `resolve_reconciliation` | financial (dismiss: safe) | a wrong P&L adjustment / an unresolved difference |
| `update_offer`, `void_offer`, `delete_offer` | safe / safe / deletion | correct, abandon, or remove an unreferenced offer |

Read tools: `get_dashboard_totals`, `get_issues` (each issue lists records and repairs to apply), `get_still_to_come_breakdown`,
`get_profit_breakdown`, `list_accounts`, `get_account`, `get_account_balance_breakdown`, `get_exchange_exposure`,
`list_positions`, `get_position`, `list_free_bets`, `get_free_bet`, `list_transfers`, `get_transfer`,
`list_reconciliations`, `get_reconciliation`, `list_offers`, `get_offer`, `get_ledger_entries`,
`get_record_history`, `get_change_status`.

`calculate_match` with an exchange name returns `commission_warning` when that exchange's stored
promotional rate has ended: pass it on, because the figures still use the stored rate until the user updates it.

## Operating tools (plugin 1.4.0): from screenshots to settled

The workflow: the user sends screenshots/odds → the assistant reads state (`get_operating_brief`), creates or
updates a **Bet Plan**, the server calculates and validates it → the user places the bets themselves → the
assistant records what the confirmations show (atomically, idempotently) → settles → reconciles rewards → next
actions answer "What should I do now?".

| Tool | Level | Use it when |
|---|---|---|
| `get_operating_brief`, `get_next_actions` | read | "What should I do now?": priorities, deadlines, value, who acts |
| `create_bet_plan`, `update_bet_plan`, `cancel_bet_plan` | planning | plan a bet (single lay or multi-outcome lays); plans hold no money |
| `recalculate_bet_plan` | planning | prices moved: keeps revisions, rates retention against the policy (ACCEPTABLE / BELOW_MINIMUM / REJECT) |
| `validate_bet_plan` | planning | SAFE_TO_PLACE / WARNING / DO_NOT_PLACE with every check (offer, token, odds, funds, liquidity, start time) |
| `get_bet_plan`, `list_bet_plans`, `get_funding_requirement` | read | plan detail, outcome table, revisions, funding per exchange |
| `calculate_multi_outcome_hedge`, `get_position_outcomes` | read | stateless hedge solver / a position's verified outcome table |
| `record_bookmaker_placement`, `record_exchange_order`, `record_exchange_fill`, `cancel_exchange_order` | safe | step-by-step placement and partial fills (weighted odds server-side) |
| `mark_exchange_fully_matched`, `record_matched_bet` | safe | only with `confirmed_fully_matched: true` after seeing FULLY MATCHED; record_matched_bet is all-or-nothing |
| `convert_bet_plan_to_position` | safe | placed exactly as planned (`confirmed_placed_as_planned`) |
| `record_funding_transfer` | safe | money moved for a plan's liability (temporary or permanent), linked to the plan |
| `settle_position` (`outcome_bucket`, `unusual`) | safe / financial | settle by the outcome that happened; unusual results wait for the user |
| `expect_reward`, `reconcile_rewards`, `mark_reward_received`, `list_expected_rewards` | planning / safe / read | expected rewards (never profit) and their arrival |
| `update_offer_progress` | safe | opt-in, deposit, structured terms, required sport/market/legs, reward delay |
| `add_record_evidence`, `list_record_evidence` | planning / read | what a screenshot showed (kind, facts); files upload at /bets/ |
| `set_position_outcomes` | safe | give an existing multi-lay position its outcome model |
| `dismiss_issue` / `acknowledge_issue`, `reopen_issue`, `list_dismissed_issues` | safe | the user confirms an issue is a false positive; it returns if its facts change |
| `mark_alerts_notified` | planning | after telling the user about `alerts_to_send` (expiring free bets, offer deadlines, overdue rewards, hedges near kick-off, finished events, funding to return): no repeats unless it escalates |
| `record_unhedged_qualifier` | safe (above the unhedged limit: financial) | a tiny cash qualifier deliberately not laid: bet + offer link + lifecycle + expected reward (+ the reward with `reward_credited`) in one change |
| `set_hedge_strategy` | safe | an existing position: matched / intentionally_unhedged (with a reason) / pending |
| `get_retention_policy` | read | the user's thresholds (SNR 80 / 75 / 70, reject below 65; SR and qualifying likewise) |

Rules: never compute stakes, liabilities or retention yourself; never record a placement or fill the user has not
shown or stated; "order placed" or "partially matched" is not fully matched; never guess a result; the assistant
cannot approve a change waiting for the user, and never moves money.

## Entry types (tools)

| Type (tool) | Use it when the user… | Ask for if missing |
|---|---|---|
| account (`submit_account_candidate`) | adds a bookmaker, exchange or bank | type, current balance (not for banks) |
| balance (`submit_balance_candidate`) | says what an existing account shows now | the balance |
| transfer (`submit_transfer_candidate`) | moved their own money | temporary or permanent; for temporary funding an optional `return_by` reminder; `in_transit` for a withdrawal not yet arrived |
| transfer: profit withdrawal (`funding_type: profit_withdrawal`) **new in the plugin** | explicitly takes **winnings** out to the bank | which accounts; only when they say winnings/profit |
| arrival (`submit_arrival_candidate`) **new** | says a withdrawal reached the bank | amount and an account, or the transfer number |
| offer (`submit_offer_candidate`) | wants a promotion tracked | the terms as fields: `qualifying_stake`, `min_odds`, `reward_count`, `reward_each`, `reward_type` (snr/sr/cash), `reward_expiry_days` |
| reward (`submit_reward_candidate`) **new** | says an offer's reward arrived | the offer's title; expiry if the offer's terms don't say |
| free_bet (`submit_free_bet_candidate`) | got free bets not tied to a tracked offer | value, SNR/SR, expiry, **`count`** (e.g. 3 for 3 × £10) |
| position (`submit_position_candidate`) | placed bets themselves | event, bookmaker, back stake/odds, exchange and lay odds (no lay stake) |
| settlement (`submit_settlement_candidate`) | says how bets settled | the outcome (never guessed) and when |
| account_status (`submit_account_status_candidate`) **new** | was restricted ("gubbed"), closed, is fine again, or got verified | which account |
| note (`submit_note_candidate`) | wants a note on an account, offer or position | the note |
| amend (`submit_amend_candidate`) **new** | says something recorded is wrong | the target, how to find it, only the fields that change, the reason |
| reverse (`submit_reverse_candidate`) **new** | wants something recorded by mistake undone | the target, how to find it, the reason |

### Hub bank and lender (funding repaid bank to bank)

Betting accounts only deal with the user's matched-betting hub bank (deposits leave it, withdrawals land in it). Temporary funding can come from another bank, the lender, routed through the hub. The lender is tracked separately from the payment rail: a withdrawal landing in the hub never repays the lender by itself, and the hub is never assumed to be the lender. When the hub sends the money back to the lender, that is a funding repayment (bank to bank). Use `record_funding_repayment` (from_account = the hub bank, funding_source = the lender), never a fake
betting → lender transfer. It can't exceed what is outstanding to the lender, is never profit or loss, auto-applies
like an ordinary transfer (safe edit), and is undone with `reverse_transfer`.

### Money out to the bank: three different things

- **Temporary funding return** (`temporary`): money borrowed from the bank going back. Repays the oldest
  funding first; anything above what is outstanding counts as a bankroll withdrawal.
- **Ordinary bankroll withdrawal** (`permanent`): the user's own capital coming out.
- **Profit withdrawal** (`profit_withdrawal`): deliberately removing realised winnings from a betting account to
  the bank. Only when the user says it is winnings/profit; an ordinary withdrawal is never reclassified. It moves
  profit that was already counted, so it never creates profit and never changes historical profit figures (it
  reduces *retained* profit, the winnings still inside the bankroll). It must go betting account → bank /
  funding source; the server refuses one larger than the retained profit; and it **always waits for the user's
  confirmation**, even with Trusted Assistant mode on.

### Finding a record for amend / reverse

`ref` is the record's number (shown in the dashboard), or: the event for a position; for a single bet (leg)
the position's number or event plus `side` (back/lay) and, if needed, the `account`; the title for an offer;
for a free bet the bookmaker (`account`) and `value`; for a transfer its `amount` and an `account` at either
end. If several records match, the result says so: ask the user for the number instead of guessing.

amend can change: a position's event / selection / market / start / offer; a bet's stake / odds /
commission / placed time / reference, or move it to another position (`move_to`: number, event or "new");
an offer's title / status / expiry / requirement / reward; a free bet's expiry / status / offer; a
transfer's date (`at`) or return date (`return_by`); an account's name.

reverse can: void a transfer, reverse a manual ledger entry (number only), remove a bet or a whole position,
remove an unused free bet, or reopen a settled position (`settlement`). Nothing is deleted: entries are
withdrawn and stay in the history and audit trail.

## Warnings the assistant should repeat

The server now warns (and Trusted Assistant mode holds the entry) when: back odds are below the offer's
minimum or the stake below its qualifying stake; the qualifying loss is over 10% of the stake; a free bet
would keep under 60%; the lay liability is more than the exchange holds; the bookmaker is marked restricted or
closed; a second sign-up offer is added at the same bookmaker; temporary funding has no return date; reward
free bets have no expiry. Repeat these to the user; don't "fix" them silently.

## What the dashboard now does by itself (so the assistant doesn't need to)

- Offers move on: qualifying → awaiting reward (qualifier settled) → reward received (free bets added) →
  converting (one used) → complete (none left). Use status amends only when the user says something unusual.
- Free bets past their expiry are marked expired; positions whose event has started appear under Issues
  with one-tap settle buttons.
- "Still to come" and retention are worked out from settled records; the assistant should never estimate them.

## For the user: after deploying

- Restart the plugin service so ChatGPT sees the new tools:
  `sudo systemctl restart matched-betting-mcp`. In ChatGPT, open the plugin/connector settings and
  refresh it (or reconnect) so the new tools appear. A custom GPT using Actions: re-import the schema from
  `scripts/assistant-token.sh openapi`.
- Nothing about the token or sign-in changes.

## Example conversation snippets

- "I sent £100 from First Direct to Smarkets." → "Temporary or permanent?" "Temporary." → "When does it need
  to go back?" "Wednesday." → transfer with `return_by`.
- "My Coral free bets have arrived." → reward (offer "Bet £5 Get £20").
- "The Smarkets lay on Chelsea was 2.46 not 2.44." → amend (leg, ref "Chelsea v Bournemouth", side lay,
  odds 2.46, reason).
- "I logged that £25 deposit twice." → reverse (transfer, amount 25, account, reason).
- "Betfred have gubbed me." → account_status (Betfred, restricted).
- "My Kwiff withdrawal landed." → arrival (from Kwiff, amount).
