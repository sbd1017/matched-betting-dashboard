"""The only outbound call: the Assistant Intake API on 127.0.0.1 (never a URL from a tool argument).

Paths are fixed strings chosen by the tool; the assistant token (mcp-var/assistant-token, 0600)
is sent only there and re-read when it changes (rotation needs no restart).
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode

import httpx2

PATHS = {"schema": "/schema", "calculate": "/calculate"}
CANDIDATE_TYPES = ("account", "balance", "transfer", "offer", "free_bet", "position", "note", "settlement", "reward",
                   "arrival", "account_status", "amend", "reverse")
CANDIDATE_ID = re.compile(r"^[0-9a-f]{16}$")
# Read resources and named changes: fixed allow-lists (the path is never built from free text).
READ_RESOURCES = ("accounts", "account", "account_balance_breakdown", "exchange_exposure", "positions", "position",
                  "free_bets", "free_bet", "transfers", "transfer", "reconciliations", "reconciliation", "offers", "offer",
                  "ledger", "dashboard", "profit", "still_to_come", "issues", "history", "operating_brief",
                  "next_actions", "bet_plans", "bet_plan", "funding_requirement", "position_outcomes", "expected_rewards",
                  "expected_reward", "evidence", "retention_policy", "dismissed_issues")
CHANGES = ("update_free_bet", "link_free_bet_to_position", "unlink_free_bet_from_position", "void_free_bet",
           "delete_free_bet", "merge_duplicate_free_bets", "update_position", "update_leg", "settle_position",
           "reopen_position", "void_position", "delete_position", "update_transfer", "reclassify_transfer",
           "reverse_transfer", "update_reconciliation", "reverse_reconciliation", "resolve_reconciliation",
           "record_balance", "update_offer", "void_offer", "delete_offer", "create_bet_plan", "update_bet_plan",
           "recalculate_bet_plan", "validate_bet_plan", "cancel_bet_plan", "convert_bet_plan_to_position",
           "record_bookmaker_placement", "record_exchange_order", "record_exchange_fill", "cancel_exchange_order",
           "mark_exchange_fully_matched", "record_matched_bet", "record_funding_transfer", "add_record_evidence",
           "expect_reward", "reconcile_rewards", "mark_reward_received", "update_offer_progress",
           "set_position_outcomes", "dismiss_issue", "reopen_issue", "mark_alerts_notified",
           "record_unhedged_qualifier", "set_hedge_strategy")


class Upstream:
    def __init__(self, base: str, token_file: Path, transport=None):
        if not base.startswith("http://127.0.0.1:"):
            raise ValueError("the Assistant Intake API must be on 127.0.0.1")
        self.base, self.token_file, self.transport = base, Path(token_file), transport
        self._stamp, self._token = None, None

    def token(self) -> str | None:
        try:
            st = self.token_file.stat()
        except OSError:
            return None
        if (st.st_mtime_ns, st.st_size) != self._stamp:
            text = self.token_file.read_text().strip()
            self._stamp, self._token = (st.st_mtime_ns, st.st_size), text if text.startswith("mbai_") else None
        return self._token

    async def _send(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        token = self.token()
        if not token:
            return 0, {"error": "not_linked"}
        try:
            async with httpx2.AsyncClient(timeout=15, follow_redirects=False, transport=self.transport) as c:
                r = await c.request(method, self.base + path, json=body,
                                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except httpx2.HTTPError:
            return 0, {"error": "unreachable"}
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}

    async def schema(self) -> tuple[int, dict]:
        return await self._send("GET", PATHS["schema"])

    async def calculate(self, body: dict) -> tuple[int, dict]:
        return await self._send("POST", PATHS["calculate"], body)

    async def submit(self, kind: str, body: dict) -> tuple[int, dict]:
        if kind not in CANDIDATE_TYPES:
            raise ValueError("unsupported candidate type")
        return await self._send("POST", f"/candidates/{kind}", body)

    async def status(self, candidate_id: str) -> tuple[int, dict]:
        if not CANDIDATE_ID.match(candidate_id):
            raise ValueError("bad candidate id")
        return await self._send("GET", f"/candidates/{candidate_id}")

    async def read(self, resource: str, params: dict) -> tuple[int, dict]:
        if resource not in READ_RESOURCES:
            raise ValueError("unsupported resource")
        query = urlencode({k: str(v)[:200] for k, v in params.items()})
        return await self._send("GET", f"/read/{resource}" + (f"?{query}" if query else ""))

    async def change(self, name: str, body: dict) -> tuple[int, dict]:
        if name not in CHANGES:
            raise ValueError("unsupported change")
        return await self._send("POST", f"/changes/{name}", body)

    async def change_status(self, change_id: str) -> tuple[int, dict]:
        if not CANDIDATE_ID.match(change_id):
            raise ValueError("bad change id")
        return await self._send("GET", f"/changes/{change_id}")

    async def hedge(self, body: dict) -> tuple[int, dict]:
        return await self._send("POST", "/hedge", body)
