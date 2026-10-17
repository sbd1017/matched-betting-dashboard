"""The ChatGPT plugin (MCP): mixed authentication, CIMD, tools, structured errors, idempotency and the boundary."""
import json
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from bets_mcp.tools import Limits
from tests_mcp.conftest import (CALLBACK, CB_CIMD, CB_CIMD_URL, FAKE_CIMD, SITE, STABLE_CIMD_URL, FakeCimd, call_tool,
                                err, jdump, list_tools, ok, pkce)

EXPECTED_TOOLS = {"get_intake_schema", "calculate_match", "get_candidate_status", "submit_account_candidate",
                  "submit_balance_candidate", "submit_transfer_candidate", "submit_position_candidate",
                  "submit_offer_candidate", "submit_free_bet_candidate", "submit_note_candidate",
                  "submit_settlement_candidate",
                  "submit_reward_candidate", "submit_arrival_candidate", "submit_account_status_candidate",
                  "submit_amend_candidate", "submit_reverse_candidate"}
ANONYMOUS_OK = {"get_intake_schema", "calculate_match"}
OPTIONAL = [{"type": "noauth"}, {"type": "oauth2", "scopes": ["bets.intake"]}]
OAUTH = [{"type": "oauth2", "scopes": ["bets.intake"]}]
PRM = f"{SITE}/.well-known/oauth-protected-resource/bets/mcp"
# Minimal valid arguments for every protected tool (they must fail closed before looking at them).
PROTECTED_CALLS = {
    "get_candidate_status": {"candidate_id": "0" * 16},
    "submit_account_candidate": {"name": "TestBook", "account_type": "bookmaker", "balance": "12.34"},
    "submit_balance_candidate": {"account": "Betfred", "balance": "47.10"},
    "submit_transfer_candidate": {"from_account": "Lloyds", "to_account": "Smarkets", "amount": "1",
                                  "funding_type": "temporary"},
    "submit_position_candidate": {"event": "A v B", "bookmaker": "Betfred", "back_stake": "5", "back_odds": "2"},
    "submit_offer_candidate": {"title": "Anon offer"},
    "submit_free_bet_candidate": {"bookmaker": "Betfred", "value": "5", "free_bet_type": "snr"},
    "submit_note_candidate": {"target": "account", "target_ref": "Betfred", "text": "anon"},
    "submit_settlement_candidate": {"position_number": 1, "outcome": "back_won", "settled_at": "2026-10-01"},
}
# Maintenance tools (bets_mcp/maintenance.py): reading the records and named corrections, all behind the sign-in.
READ_TOOLS = {"get_dashboard_totals": {}, "get_issues": {}, "get_still_to_come_breakdown": {}, "get_profit_breakdown": {},
              "list_accounts": {}, "get_account": {"account": "Smarkets"},
              "get_account_balance_breakdown": {"account": "Smarkets"}, "get_exchange_exposure": {"exchange": "Smarkets"},
              "list_positions": {}, "get_position": {"position": "1"}, "list_free_bets": {}, "get_free_bet": {"free_bet_id": 1},
              "list_transfers": {}, "get_transfer": {"transfer_id": 1}, "list_reconciliations": {},
              "get_reconciliation": {"reconciliation_id": 1}, "list_offers": {}, "get_offer": {"offer_id": 1},
              "get_ledger_entries": {}, "get_record_history": {"record_type": "free_bet", "record_id": 1},
              "get_change_status": {"change_id": "0" * 16}}
CHANGE_TOOLS = {"update_free_bet": {"free_bet_id": 1, "notes": "x"}, "link_free_bet_to_position": {"free_bet_id": 1, "position_id": 1},
                "unlink_free_bet_from_position": {"free_bet_id": 1}, "void_free_bet": {"free_bet_id": 1},
                "delete_free_bet": {"free_bet_id": 1},
                "merge_duplicate_free_bets": {"keep_free_bet_id": 1, "duplicate_free_bet_id": 2},
                "update_position": {"position_id": 1, "notes": "x"}, "update_leg": {"leg_id": 1, "odds": "2"},
                "settle_position": {"position_id": 1, "outcome": "back_won"}, "reopen_position": {"position_id": 1},
                "void_position": {"position_id": 1}, "delete_position": {"position_id": 1},
                "update_transfer": {"transfer_id": 1, "notes": "x"},
                "reclassify_transfer": {"transfer_id": 1, "funding_type": "bankroll_contribution"},
                "reverse_transfer": {"transfer_id": 1}, "record_balance": {"account": "Smarkets", "observed": "1"},
                "update_reconciliation": {"reconciliation_id": 1, "note": "x"},
                "reverse_reconciliation": {"reconciliation_id": 1},
                "resolve_reconciliation": {"reconciliation_id": 1, "action": "dismiss"},
                "update_offer": {"offer_id": 1, "notes": "x"}, "void_offer": {"offer_id": 1}, "delete_offer": {"offer_id": 1}}
DESTRUCTIVE_TOOLS = {"delete_free_bet", "delete_position", "delete_offer", "reverse_transfer", "reverse_reconciliation",
                     "reopen_position"}
READ_TOOLS.update({"get_operating_brief": {}, "get_next_actions": {}, "list_bet_plans": {}, "get_bet_plan": {"plan_id": 1},
                   "get_funding_requirement": {"plan_id": 1}, "get_position_outcomes": {"position": "1"},
                   "calculate_multi_outcome_hedge": {"backs": [{"stake": "10", "odds": "6.5"}], "lays": [{"odds": "6.8"}]},
                   "get_retention_policy": {}, "list_expected_rewards": {}, "get_expected_reward": {"expected_reward_id": 1},
                   "list_record_evidence": {"record_type": "plan", "record_id": 1}, "list_dismissed_issues": {}})
CHANGE_TOOLS.update({
    "create_bet_plan": {"event": "A v B", "bookmaker": "Betfred", "back_odds": "2"},
    "update_bet_plan": {"plan_id": 1, "notes": "x"}, "recalculate_bet_plan": {"plan_id": 1, "back_odds": "2"},
    "validate_bet_plan": {"plan_id": 1}, "cancel_bet_plan": {"plan_id": 1},
    "record_bookmaker_placement": {"plan_id": 1}, "record_exchange_order": {"plan_id": 1, "stake": "1", "odds": "2"},
    "record_exchange_fill": {"plan_id": 1, "stake": "1", "odds": "2"}, "cancel_exchange_order": {"plan_id": 1},
    "mark_exchange_fully_matched": {"plan_id": 1, "confirmed_fully_matched": True},
    "record_matched_bet": {"plan_id": 1, "confirmed_fully_matched": True},
    "convert_bet_plan_to_position": {"plan_id": 1, "confirmed_placed_as_planned": True},
    "record_funding_transfer": {"from_account": "Lloyds", "to_account": "Smarkets", "amount": "1"},
    "add_record_evidence": {"record_type": "plan", "record_id": 1, "kind": "other"},
    "expect_reward": {"offer_id": 1}, "reconcile_rewards": {}, "mark_reward_received": {"expected_reward_id": 1},
    "update_offer_progress": {"offer_id": 1, "opted_in": True},
    "set_position_outcomes": {"position_id": 1, "outcomes": ["A", "B"], "legs": [{"leg_id": 1, "covers": ["A"]},
                                                                                  {"leg_id": 2, "covers": ["A"]}]},
    "dismiss_issue": {"issue_key": "x:1", "reason": "test"},
    "acknowledge_issue": {"issue_key": "x:1", "reason": "test"}, "reopen_issue": {"issue_key": "x:1"},
    "mark_alerts_notified": {"alert_keys": ["settle:position #1"]},
    "record_unhedged_qualifier": {"offer_id": 1, "event": "A v B", "stake": "0.05", "odds": "2.8", "reason": "tiny stake"},
    "set_hedge_strategy": {"position_id": 1, "hedge_strategy": "pending"},
    "record_funding_repayment": {"from_account": "First Direct", "funding_source": "Lloyds", "amount": "1"}})
PROTECTED_CALLS.update(READ_TOOLS)
PROTECTED_CALLS.update({k: {**v, "request_id": "probe-1"} for k, v in CHANGE_TOOLS.items()})
EXPECTED_TOOLS |= set(READ_TOOLS) | set(CHANGE_TOOLS)


def pending(bets):
    return bets.one("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'")


def ledger(bets):
    return bets.one("SELECT count(*) FROM ledger_entries")


def raw_rpc(url, method, params=None, token=None, headers=None):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    with httpx2.Client(timeout=15) as c:
        return c.post(url, headers=h, content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                           "params": params or {}}))


def assert_challenge(result):
    """The documented auth-required result: isError + _meta["mcp/www_authenticate"] (Bearer challenge)."""
    assert result.is_error, result.structured_content
    (challenge,) = result.meta["mcp/www_authenticate"]
    assert challenge.startswith("Bearer ") and f'resource_metadata="{PRM}"' in challenge
    assert 'error="insufficient_scope"' in challenge and 'error_description="' in challenge


def mint(mcp, scopes=("bets.intake",), resource=None, ttl=3600):
    code = mcp.store.new_grant_and_code("test-client", "test", {"scopes": list(scopes)}, 60)
    row = mcp.store.take_code(code)
    access, _ = mcp.store.issue_tokens(row["grant_id"], "test-client", list(scopes),
                                       resource or mcp.settings.resource, ttl, 86400)
    return access


# ---------------------------------------------------------------- mixed authentication: who can call what

def test_anonymous_initialize_and_tools_list_with_security_schemes(mcp):
    tools = list_tools(mcp, None)                                                   # official client, no token
    assert {t.name for t in tools} == EXPECTED_TOOLS
    listed = raw_rpc(mcp.url, "tools/list").json()["result"]["tools"]               # the wire payload
    for t in listed:
        expected = OPTIONAL if t["name"] in ANONYMOUS_OK else OAUTH
        assert t["securitySchemes"] == expected and t["_meta"]["securitySchemes"] == expected, t["name"]
        assert any(s["type"] == "oauth2" and s["scopes"] == ["bets.intake"] for s in t["securitySchemes"])


def test_server_version_tracks_the_plugin_and_the_tool_manifest(mcp):
    """ChatGPT's connector may cache the tool list by serverInfo name + version: the version must change whenever
    the tools do (it was a constant 1.0.0 while the tool set grew to 96)."""
    import json as _json
    from bets_mcp.server import PLUGIN_MANIFEST, tools_fingerprint
    info = raw_rpc(mcp.url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "1"}}).json()["result"]["serverInfo"]
    plugin = _json.loads(PLUGIN_MANIFEST.read_text())["version"]
    assert info["name"] == "matched-betting-assistant" and info["version"] != "1.0.0"
    assert info["version"] == f"{plugin}+tools.{tools_fingerprint(mcp.server)}"
    before = tools_fingerprint(mcp.server)
    mcp.server.add_tool(lambda: None, name="zz_probe_tool", description="probe")   # any change to the tools...
    try:
        assert tools_fingerprint(mcp.server) != before                            # ...changes the fingerprint
    finally:
        mcp.server.remove_tool("zz_probe_tool")
    assert tools_fingerprint(mcp.server) == before


def test_tool_manifest_is_exactly_the_expected_96(mcp):
    names = [t.name for t in list_tools(mcp, None)]
    assert len(names) == len(set(names)) == 97 == len(EXPECTED_TOOLS)
    assert {"record_unhedged_qualifier", "set_hedge_strategy"} <= set(names)


@pytest.mark.parametrize("name", sorted(PROTECTED_CALLS))
def test_every_protected_tool_fails_closed_without_a_valid_token(mcp, bets, name):
    before = (bets.one("SELECT count(*) FROM import_candidates"), ledger(bets))
    for token in (None, "mbma_forged", bets.assistant_token):
        assert_challenge(call_tool(mcp, token, name, PROTECTED_CALLS[name]))
    assert (bets.one("SELECT count(*) FROM import_candidates"), ledger(bets)) == before


@pytest.mark.parametrize("case", ["wrong_scope", "other_resource", "expired"])
def test_token_must_be_ours_for_this_resource_unexpired_and_scoped(mcp, case):
    token = {"wrong_scope": lambda: mint(mcp, scopes=("other.scope",)),
             "other_resource": lambda: mint(mcp, resource=f"{SITE}/bets/elsewhere"),
             "expired": lambda: mint(mcp, ttl=-10)}[case]()
    assert_challenge(call_tool(mcp, token, "submit_account_candidate", PROTECTED_CALLS["submit_account_candidate"]))


def test_anonymous_tools_reveal_no_user_data(mcp, token):
    schema = ok(call_tool(mcp, None, "get_intake_schema"))
    assert schema["rules"] and "80.00" not in jdump(schema) and "50.00" not in jdump(schema)
    calc = ok(call_tool(mcp, None, "calculate_match", {"back_stake": "10", "back_odds": "4", "lay_odds": "4.2",
                                                       "commission": "2"}))
    assert calc["lay_stake"] == "9.57"
    # a stored exchange commission is the user's data: by name it needs the sign-in
    assert_challenge(call_tool(mcp, None, "calculate_match", {"back_stake": "10", "back_odds": "4", "lay_odds": "4.2",
                                                              "exchange": "Smarkets"}))
    assert ok(call_tool(mcp, token, "calculate_match", {"back_stake": "10", "back_odds": "4", "lay_odds": "4.2",
                                                        "exchange": "Smarkets"}))["commission"] == "2%"


def test_bets_session_does_not_authenticate_mcp(mcp, bets):
    browser = bets.browser()
    cookie = "; ".join(f"{k}={v}" for k, v in browser.cookies.items())
    r = raw_rpc(mcp.url, "tools/call", {"name": "submit_account_candidate",
                                        "arguments": PROTECTED_CALLS["submit_account_candidate"]},
                headers={"Cookie": cookie, "X-CSRF-Token": browser.csrf})
    assert r.json()["result"]["isError"] is True and "mcp/www_authenticate" in r.json()["result"]["_meta"]


def test_mcp_token_does_not_open_bets_or_the_intake_api(mcp, bets, token):
    with httpx2.Client(base_url=f"http://127.0.0.1:{bets.port}", timeout=10, follow_redirects=False) as c:
        h = {"Authorization": f"Bearer {token}"}
        for path in ("/bets/", "/bets/ledger", "/bets/accounts", "/bets/export/full.json", "/bets/imports"):
            assert c.get(path, headers=h).status_code in (303, 401), path
        assert c.get("/bets/api/assistant/schema", headers=h).status_code == 401
        assert c.post("/bets/api/assistant/candidates/account", headers=h,
                      json={"name": "X", "account_type": "bookmaker"}).status_code == 401


def test_only_the_intended_tools_exist(mcp, token):
    tools = list_tools(mcp, token)
    assert {t.name for t in tools} == EXPECTED_TOOLS
    words = ("url", "uri", "method", "path", "sql", "query", "command", "shell", "headers", "endpoint", "host")
    for t in tools:
        assert t.description and "Use this" in t.description, t.name
        assert t.input_schema.get("type") == "object"
        for name, spec in t.input_schema.get("properties", {}).items():
            assert name not in words, (t.name, name)
            assert spec.get("type") != "object", (t.name, name)            # no free-form objects
        read_only = t.name in ("get_intake_schema", "calculate_match", "get_candidate_status") or t.name in READ_TOOLS
        assert t.annotations.read_only_hint is read_only
        assert t.annotations.destructive_hint is (t.name in DESTRUCTIVE_TOOLS), t.name
        if t.name in CHANGE_TOOLS:                    # every change is idempotent by a required request_id
            assert "request_id" in t.input_schema.get("required", []), t.name
    enums = {t.name: t.input_schema["properties"] for t in tools}
    assert enums["submit_account_candidate"]["account_type"]["enum"] == ["bookmaker", "exchange", "bank"]
    assert set(enums["submit_transfer_candidate"]["funding_type"]["enum"]) == {"temporary", "permanent",
                                                                               "between_betting_accounts",
                                                                               "profit_withdrawal"}
    assert enums["submit_free_bet_candidate"]["free_bet_type"]["enum"] == ["snr", "sr"]


@pytest.mark.parametrize("name", ["request", "http_request", "confirm_candidate", "reject_candidate", "confirm_change",
                                  "void_transfer", "reconcile_account", "export_data", "set_permissions",
                                  "get_balances", "read_ledger", "get_profit", "run_sql", "delete_transfer",
                                  "delete_ledger_entry"])
def test_forbidden_operations_do_not_exist(mcp, token, name):
    r = call_tool(mcp, token, name, {"id": "1"})
    assert r.is_error


# ---------------------------------------------------------------- 4-13: the tools

def test_intake_schema(mcp, token):
    data = ok(call_tool(mcp, token, "get_intake_schema"))
    assert set(data["tools"]) == {t for t in EXPECTED_TOOLS if t.startswith("submit_")}
    assert "name" in data["tools"]["submit_account_candidate"]["required"]
    assert data["server_types"]["transfer"]["fields"] and data["rules"]
    text = jdump(data)
    assert "80.00" not in text and "50.00" not in text and "profit" not in text.split('"rules"')[0]


def test_dry_run_account_stores_nothing(mcp, bets, token):
    before = bets.one("SELECT count(*) FROM import_candidates")
    data = ok(call_tool(mcp, token, "submit_account_candidate",
                        {"name": "TestBook", "account_type": "bookmaker", "balance": "12.34", "dry_run": True}))
    assert data["status"] == "valid" and data["summary"] == {"name": "TestBook", "account_type": "bookmaker",
                                                             "balance": "12.34"}
    assert "candidate_id" not in data and "nothing was stored" in data["note"]
    assert bets.one("SELECT count(*) FROM import_candidates") == before
    assert bets.one("SELECT count(*) FROM accounts WHERE name = 'TestBook'") == 0


def test_submit_account_then_user_rejects(mcp, bets, token):
    entries = ledger(bets)
    data = ok(call_tool(mcp, token, "submit_account_candidate",
                        {"name": "TestBook", "account_type": "bookmaker", "balance": "12.34"}))
    assert data["status"] == "pending_confirmation" and data["summary"]["balance"] == "12.34"
    assert data["confirmation_url"].endswith(f"/bets/imports/{data['candidate_id']}")   # https://example.com/... live
    assert "Nothing is recorded until the user confirms" in data["note"]
    assert ledger(bets) == entries and bets.one("SELECT count(*) FROM accounts WHERE name = 'TestBook'") == 0
    browser = bets.browser()
    listing = browser.get("/bets/imports").text
    assert "TestBook (bookmaker)" in listing and "Assistant (ChatGPT)" in listing
    iid = bets.one("SELECT id FROM import_candidates WHERE public_id = ?", data["candidate_id"])
    r = browser.post(f"/bets/imports/{iid}", data={"csrf_token": browser.csrf, "decision": "reject"})
    assert r.status_code == 303
    status = ok(call_tool(mcp, token, "get_candidate_status", {"candidate_id": data["candidate_id"]}))
    assert status["status"] == "rejected"
    assert bets.one("SELECT count(*) FROM accounts WHERE name = 'TestBook'") == 0 and ledger(bets) == entries


def test_existing_account_balance_is_a_reconciliation_candidate(mcp, bets, token):
    exists = err(call_tool(mcp, token, "submit_account_candidate",
                           {"name": "Betfred", "account_type": "bookmaker", "balance": "47.10"}))
    assert exists["error"] == "account_exists" and "submit_balance_candidate" in exists["hint"]
    data = ok(call_tool(mcp, token, "submit_balance_candidate", {"account": "Betfred", "balance": "47.10"}))
    assert data["type"] == "balance" and data["status"] == "pending_confirmation"
    assert data["summary"] == {"account": "Betfred", "reported_balance": "47.10"}            # never the app's £50
    assert bets.one("SELECT count(*) FROM reconciliations") == 0                              # not reconciled directly


def test_temporary_and_permanent_transfers(mcp, bets, token):
    entries = ledger(bets)
    t = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Lloyds", "to_account": "Smarkets",
                                                               "amount": "100", "funding_type": "temporary"}))
    assert t["summary"] == {"from": "Lloyds", "to": "Smarkets", "amount": "100.00", "capital_type": "temporary_funding"}
    p = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Lloyds", "to_account": "Smarkets",
                                                               "amount": "250", "funding_type": "permanent"}))
    assert p["summary"]["capital_type"] == "bankroll_contribution"
    back = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Smarkets", "to_account": "Lloyds",
                                                                  "amount": "102.40", "funding_type": "temporary"}))
    assert back["summary"]["capital_type"] == "temporary_return"
    assert ledger(bets) == entries and bets.one("SELECT count(*) FROM transfers") == 0


def test_profit_withdrawal_only_when_said_and_never_automatic(mcp, bets, token):
    entries = ledger(bets)
    w = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Smarkets", "to_account": "Lloyds",
                                                               "amount": "20", "funding_type": "profit_withdrawal"}))
    assert w["summary"]["capital_type"] == "profit_withdrawal" and w["status"] == "pending_confirmation"
    plain = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Smarkets", "to_account": "Lloyds",
                                                                   "amount": "20", "funding_type": "permanent"}))
    assert plain["summary"]["capital_type"] == "bankroll_withdrawal"            # never reinterpreted as profit
    e = err(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Lloyds", "to_account": "Smarkets",
                                                                "amount": "20", "funding_type": "profit_withdrawal"}))
    assert e["error"] == "invalid_funding_type"                                  # bank -> betting can't be winnings out
    browser = bets.browser()
    r = browser.post("/bets/settings/assistant/auto-confirm",
                     data={"csrf_token": browser.csrf, "kinds": ["transfer"], "enabled": "on"})
    assert r.status_code == 303
    try:
        held = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Smarkets", "to_account": "Lloyds",
                                                                      "amount": "1", "funding_type": "profit_withdrawal"}))
        assert held["status"] == "pending_confirmation" and held["held_for_review"] == "profit withdrawals always wait for you"
    finally:
        browser.post("/bets/settings/assistant/auto-confirm", data={"csrf_token": browser.csrf})
    assert ledger(bets) == entries


def test_matched_position_lay_stake_from_the_server(mcp, bets, token):
    entries = ledger(bets)
    data = ok(call_tool(mcp, token, "submit_position_candidate", {
        "event": "Liverpool v Everton", "selection": "Liverpool", "bookmaker": "Betfred", "back_stake": "10",
        "back_odds": "4.0", "exchange": "Smarkets", "lay_odds": "4.2"}))
    legs = data["summary"]["legs"]
    assert [(l["side"], l["account"], l["stake"]) for l in legs] == [("back", "Betfred", "10.00"),
                                                                     ("lay", "Smarkets", "9.57")]
    assert legs[1]["liability"] == "30.62" and data["summary"]["expected_result"] == "-0.62"
    assert any(w["code"] == "server_calculated" for w in data["warnings"])
    assert ledger(bets) == entries and bets.one("SELECT count(*) FROM positions") == 0
    calc = ok(call_tool(mcp, token, "calculate_match", {"back_stake": "10", "back_odds": "4", "lay_odds": "4.2",
                                                        "exchange": "Smarkets"}))
    assert calc["lay_stake"] == "9.57" and "nothing was stored" in calc["note"]


def test_offer_free_bet_and_note(mcp, token):
    o = ok(call_tool(mcp, token, "submit_offer_candidate", {"title": "Bet £10 Get £30", "bookmaker": "Betfred",
                                                            "offer_type": "sign_up", "reward_value": "30"}))
    assert o["type"] == "offer" and o["status"] == "pending_confirmation"
    f = ok(call_tool(mcp, token, "submit_free_bet_candidate", {"bookmaker": "Betfred", "value": "10",
                                                               "free_bet_type": "snr", "expires_at": "2099-01-01"}))
    assert f["summary"]["free_bet_type"] == "snr"
    n = ok(call_tool(mcp, token, "submit_note_candidate", {"target": "account", "target_ref": "Betfred",
                                                           "text": "ID verified"}))
    assert n["type"] == "note"


# ---------------------------------------------------------------- 14-18: errors ChatGPT can act on

def test_unknown_account_is_a_structured_error(mcp, token):
    e = err(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Lloyds", "to_account": "SomeBet",
                                                                "amount": "20", "funding_type": "temporary"}))
    assert (e["error"], e["field"], e["value"]) == ("unknown_account", "to_account", "SomeBet")
    assert "add" in e["hint"]
    e = err(call_tool(mcp, token, "submit_position_candidate", {"event": "A v B", "bookmaker": "NoSuchBook",
                                                                "back_stake": "5", "back_odds": "2"}))
    assert (e["error"], e["field"], e["value"]) == ("unknown_account", "bookmaker", "NoSuchBook")


def test_invalid_amount_odds_missing_and_suspicious(mcp, token):
    e = err(call_tool(mcp, token, "submit_free_bet_candidate", {"bookmaker": "Betfred", "value": "10.555",
                                                                "free_bet_type": "snr"}))
    assert (e["error"], e["field"]) == ("invalid_amount", "value")
    e = err(call_tool(mcp, token, "submit_position_candidate", {"event": "A v B", "bookmaker": "Betfred",
                                                                "back_stake": "5", "back_odds": "evens-ish"}))
    assert (e["error"], e["field"]) == ("invalid_odds", "back_odds")
    e = err(call_tool(mcp, token, "submit_position_candidate", {"event": "A v B", "bookmaker": "Betfred",
                                                                "back_stake": "5", "back_odds": "2",
                                                                "exchange": "Smarkets"}))
    assert (e["error"], e["field"]) == ("missing_field", "lay_odds")
    e = err(call_tool(mcp, token, "submit_balance_candidate", {"account": "Betfred", "balance": ""}))
    assert (e["error"], e["field"]) == ("missing_field", "balance")
    big = ok(call_tool(mcp, token, "submit_position_candidate", {"event": "Big v Bet", "bookmaker": "Betfred",
                                                                 "back_stake": "500", "back_odds": "2", "dry_run": True}))
    assert any(w["code"] == "suspicious_value" for w in big["warnings"])
    bad_enum = call_tool(mcp, token, "submit_account_candidate", {"name": "X", "account_type": "casino"})
    assert bad_enum.is_error
    bad_id = call_tool(mcp, token, "get_candidate_status", {"candidate_id": "../../ledger"})
    assert bad_id.is_error


# ---------------------------------------------------------------- 19-21: retries, limits, status

def test_retries_never_store_twice(mcp, bets, token):
    args = {"bookmaker": "Betfred", "value": "7", "free_bet_type": "sr"}
    first = ok(call_tool(mcp, token, "submit_free_bet_candidate", args))
    again = ok(call_tool(mcp, token, "submit_free_bet_candidate", args))                 # a retried tool call
    assert again["candidate_id"] == first["candidate_id"] and again["duplicate"] is True
    assert bets.one("SELECT count(*) FROM import_candidates WHERE public_id = ?", first["candidate_id"]) == 1
    keyed = {**args, "value": "8", "request_id": "chat-42-msg-3"}
    a = ok(call_tool(mcp, token, "submit_free_bet_candidate", keyed))
    b = ok(call_tool(mcp, token, "submit_free_bet_candidate", keyed))
    assert a["candidate_id"] == b["candidate_id"]
    c = ok(call_tool(mcp, token, "submit_free_bet_candidate", {**keyed, "request_id": "chat-42-msg-4"}))
    assert c["candidate_id"] != a["candidate_id"]                                           # a genuine second one
    clash = err(call_tool(mcp, token, "submit_free_bet_candidate", {**keyed, "value": "9"}))
    assert clash["error"] == "duplicate_conflict"


def test_rate_limited(mcp, monkeypatch):
    token = mcp.token()                                                    # its own connection (own counters)
    limits = Limits(per_minute=3, per_day=100)
    original = Limits.allow
    monkeypatch.setattr(Limits, "allow", lambda self, who: original(limits, who))
    results = [call_tool(mcp, token, "calculate_match", {"back_stake": "10", "back_odds": "2", "lay_odds": "2.1"})
               for _ in range(4)]
    assert [r.is_error for r in results] == [False, False, False, True]
    assert results[-1].structured_content["error"] == "rate_limited"


def test_candidate_status_and_no_confirm_path(mcp, bets, token):
    data = ok(call_tool(mcp, token, "submit_offer_candidate", {"title": "Status check offer"}))
    s = ok(call_tool(mcp, token, "get_candidate_status", {"candidate_id": data["candidate_id"]}))
    assert s["status"] == "pending_confirmation" and s["type"] == "offer"
    # Nothing the plugin can call confirms or rejects a candidate (or a held change): only the user, in /bets/.
    for name in EXPECTED_TOOLS:
        assert not name.startswith(("confirm", "reject", "approve", "decide", "set_permission")), name
        assert name.startswith(("submit_", "get_", "calculate_", "list_")) or name in CHANGE_TOOLS, name
    assert bets.one("SELECT status FROM import_candidates WHERE public_id = ?", data["candidate_id"]) == "needs_confirmation"
    e = err(call_tool(mcp, token, "get_candidate_status", {"candidate_id": "0" * 16}))
    assert e["error"] == "not_found"


def test_submissions_do_not_echo_figures(mcp, bets, token):
    """Submissions and the schema never echo the app's balances or profit (reading them is the read tools' job)."""
    outputs = [call_tool(mcp, token, "get_intake_schema"),
               call_tool(mcp, token, "submit_balance_candidate", {"account": "Smarkets", "balance": "1.00",
                                                                  "dry_run": True}),
               call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Smarkets", "to_account": "Lloyds",
                                                                   "amount": "1", "funding_type": "temporary",
                                                                   "dry_run": True})]
    for r in outputs:
        text = jdump(r.structured_content)
        assert "80.00" not in text and "£80" not in text and "outstanding" not in text


# ---------------------------------------------------------------- 31, 32: secrets never in logs

def test_secrets_never_logged(mcp, bets, token, caplog):
    from bets_mcp.server import configure_logging
    configure_logging()                                   # exactly what the service logs
    caplog.set_level(logging.DEBUG)
    call_tool(mcp, token, "submit_note_candidate", {"target": "account", "target_ref": "Betfred", "text": "log check"})
    call_tool(mcp, token, "submit_account_candidate", {"name": "Zed", "account_type": "casino"})
    text = caplog.text
    assert token not in text and bets.assistant_token not in text
    assert "tool=submit_note_candidate" in text                                   # what IS logged
    assert "log check" not in text                                                # payloads are not
    assert bets.assistant_token not in (bets.data.parent / "bets.log").read_text()


# ---------------------------------------------------------------- OAuth with CIMD (through the tracked nginx config)

def connect(nginx, mcp, *, code: str | None = None, verifier_override=None, client_id=CB_CIMD_URL,
            redirect_uri=CALLBACK):
    """Discovery -> authorize (client_id = ChatGPT's CIMD URL) -> consent (connection code) -> token, as ChatGPT does."""
    c = nginx.client()
    prm = c.get("/.well-known/oauth-protected-resource/bets/mcp").json()
    assert prm["resource"] == f"{SITE}/bets/mcp"
    meta = c.get("/.well-known/oauth-authorization-server/bets/mcp").json()
    assert meta["issuer"] == prm["authorization_servers"][0] and meta["client_id_metadata_document_supported"] is True
    verifier, challenge = pkce()
    r = c.get("/bets/mcp/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": "s1",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "bets.intake",
        "resource": f"{SITE}/bets/mcp"})
    assert r.status_code == 302, r.text[:300]
    consent = urlsplit(r.headers["location"])
    assert consent.path == "/bets/mcp/consent"
    page = c.get(f"{consent.path}?{consent.query}")
    assert page.status_code == 200 and "Connection code" in page.text and "will not be able to" in page.text
    rid = parse_qs(consent.query)["request"][0]
    code = code if code is not None else mcp.store.new_connect_code(900)
    r = c.post("/bets/mcp/consent", data={"request": rid, "code": code, "decision": "approve"})
    if r.status_code != 302:
        return c, None, r
    back = urlsplit(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == redirect_uri
    q = parse_qs(back.query)
    assert q["state"] == ["s1"] and q["iss"] == [f"{SITE}/bets/mcp"]
    tok = c.post("/bets/mcp/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": redirect_uri, "client_id": client_id,
        "code_verifier": verifier_override or verifier, "resource": f"{SITE}/bets/mcp"})         # no client secret
    return c, q["code"][0], tok


@pytest.mark.parametrize("client_id,redirect_uri", [(CB_CIMD_URL, CALLBACK),
                                                    (STABLE_CIMD_URL, "https://chatgpt.com/connector_platform_oauth_redirect")])
def test_cimd_connect_then_protected_tool_through_nginx(nginx, mcp, bets, client_id, redirect_uri):
    c, code, tok = connect(nginx, mcp, client_id=client_id, redirect_uri=redirect_uri)
    assert tok.status_code == 200, tok.text
    body = tok.json()
    assert body["token_type"].lower() == "bearer" and body["access_token"].startswith("mbma_") and body["refresh_token"]
    url, host = f"{nginx.base}/bets/mcp", {"Host": "example.com"}
    before = bets.one("SELECT count(*) FROM import_candidates")
    dry = call_tool(mcp, body["access_token"], "submit_account_candidate",
                    {"name": "TestBook", "account_type": "bookmaker", "balance": "12.34", "dry_run": True},
                    url=url, headers=host)
    assert not dry.is_error and dry.structured_content["status"] == "valid"
    assert bets.one("SELECT count(*) FROM import_candidates") == before
    # the code is single use
    again = c.post("/bets/mcp/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                             "client_id": client_id, "code_verifier": "x" * 50})
    assert again.status_code == 400
    # refresh rotates; re-using an old refresh token revokes the connection
    refresh = lambda rt: c.post("/bets/mcp/token", data={"grant_type": "refresh_token", "refresh_token": rt,
                                                          "client_id": client_id})
    new = refresh(body["refresh_token"])
    assert new.status_code == 200 and new.json()["access_token"] != body["access_token"]
    assert not call_tool(mcp, new.json()["access_token"], "get_candidate_status", {"candidate_id": "0" * 16}).meta
    assert refresh(body["refresh_token"]).status_code == 400
    assert_challenge(call_tool(mcp, new.json()["access_token"], "get_candidate_status", {"candidate_id": "0" * 16}))


def test_oauth_refuses_bad_codes_and_pkce(nginx, mcp):
    _, _, resp = connect(nginx, mcp, code="AAAAA-BBBBB-CCCCC-DDDDD")
    assert resp.status_code == 400 and "wrong, used or expired" in resp.text
    used = mcp.store.new_connect_code(900)
    assert connect(nginx, mcp, code=used)[2].status_code == 200
    assert connect(nginx, mcp, code=used)[2].status_code == 400                      # each code works once
    _, _, tok = connect(nginx, mcp, verifier_override="not-the-verifier-" + "x" * 30)
    assert tok.status_code == 400                                                     # PKCE enforced


def cimd_authorize(nginx, client_id, redirect_uri=CALLBACK):
    _, challenge = pkce()
    return nginx.client().get("/bets/mcp/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": "s",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "bets.intake", "resource": f"{SITE}/bets/mcp"})


@pytest.mark.parametrize("client_id", ["https://evil.example/client.json", "https://chatgpt.com.evil.example/oauth/client.json",
                                       "http://chatgpt.com/oauth/client.json", "https://chatgpt.com/oauth/../x/client.json",
                                       "https://chatgpt.com/oauth/a/b/client.json", "https://chatgpt.com/other.json"])
def test_cimd_never_fetches_other_urls(nginx, client_id):
    FAKE_CIMD.fetched.clear()
    assert cimd_authorize(nginx, client_id).status_code in (400, 401) and FAKE_CIMD.fetched == []


def test_cimd_redirect_must_be_in_the_document_and_no_dcr(nginx):
    assert cimd_authorize(nginx, CB_CIMD_URL, "https://chatgpt.com/connector/oauth/someone_else").status_code in (400, 401)
    assert nginx.client().post("/bets/mcp/register", json={"redirect_uris": [CALLBACK]}).status_code in (404, 405)


@pytest.mark.parametrize("change,reason", [
    ({"client_id": "https://chatgpt.com/oauth/other/client.json"}, "client_id"),
    ({"client_secret": "x"}, "secret"),
    ({"token_endpoint_auth_methods_supported": ["private_key_jwt"]}, "none"),
    ({"redirect_uris": ["https://evil.example/cb"]}, "redirect"),
    ({"grant_types": ["refresh_token"]}, "authorization_code"),
    ({"response_types": ["token"]}, "code")])
def test_cimd_document_validation(change, reason):
    from bets_mcp.cimd import CimdError, validate
    from bets_mcp.config import load_settings
    s = load_settings()
    good = validate(CB_CIMD_URL, CB_CIMD, s)
    assert good.token_endpoint_auth_method == "none" and [str(u) for u in good.redirect_uris] == [CALLBACK]
    assert not good.client_secret
    with pytest.raises(CimdError, match=reason):
        validate(CB_CIMD_URL, {**CB_CIMD, **change}, s)


def test_cimd_documents_are_cached():
    import asyncio
    from bets_mcp.cimd import CimdResolver
    from bets_mcp.config import load_settings
    fake = FakeCimd()
    resolver = CimdResolver(load_settings(), fake.fetch)
    first = asyncio.run(resolver.client(CB_CIMD_URL))
    second = asyncio.run(resolver.client(CB_CIMD_URL))
    assert first is second and fake.fetched == [CB_CIMD_URL]


def test_revoke_and_disable(nginx, mcp):
    _, _, tok = connect(nginx, mcp)
    access = tok.json()["access_token"]
    assert not call_tool(mcp, access, "get_candidate_status", {"candidate_id": "0" * 16}).meta
    mcp.store.revoke_all()
    assert_challenge(call_tool(mcp, access, "get_candidate_status", {"candidate_id": "0" * 16}))
    mcp.store.set_enabled(False)
    try:
        r = cimd_authorize(nginx, CB_CIMD_URL)
        assert "error=access_denied" in r.headers.get("location", "")
        assert_challenge(call_tool(mcp, mint(mcp), "get_candidate_status", {"candidate_id": "0" * 16}))
    finally:
        mcp.store.set_enabled(True)


def test_nginx_boundary(nginx, mcp, token):
    c = nginx.client()
    assert c.request("PUT", "/bets/mcp").status_code == 405
    assert c.request("DELETE", "/bets/mcp/token").status_code == 405
    big = c.post("/bets/mcp", content=b"x" * (40 * 1024), headers={"Authorization": f"Bearer {token}",
                                                                   "Content-Type": "application/json"})
    assert big.status_code == 413
    r = c.post("/bets/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
               headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200 and len(r.headers.get_list("content-security-policy")) == 1   # anonymous list
    r = c.post("/bets/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "submit_account_candidate", "arguments": PROTECTED_CALLS["submit_account_candidate"]}},
               headers={"Accept": "application/json, text/event-stream"})
    assert r.json()["result"]["isError"] is True and PRM in r.json()["result"]["_meta"]["mcp/www_authenticate"][0]
    for path in ("/bets/mcp/../api/assistant/schema", "/bets/mcp/%2e%2e/ledger", "/bets/mcp/docs", "/bets/mcp/openapi.json",
                 "/bets/mcp-diagnostic", "/bets/mcp-lab/a", "/bets/mcp-lab/n"):
        assert c.get(path, headers={"Authorization": f"Bearer {token}"}).status_code in (400, 404, 303, 401), path
    page = c.get("/bets/mcp/consent?request=nope")
    assert page.status_code == 400 and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    log = (nginx.logs / "matched-betting.access.log").read_text()
    assert token not in log and "request=" not in log                                 # paths only


def test_consent_guessing_is_limited(nginx, mcp):
    c = nginx.client(ip="203.0.113.77")
    _, challenge = pkce()
    loc = c.get("/bets/mcp/authorize", params={"response_type": "code", "client_id": CB_CIMD_URL,
                                                "redirect_uri": CALLBACK, "state": "s", "code_challenge": challenge,
                                                "code_challenge_method": "S256"}).headers["location"]
    rid = parse_qs(urlsplit(loc).query)["request"][0]
    codes = [c.post("/bets/mcp/consent", data={"request": rid, "code": "WRONG-WRONG-WRONG-WRONG",
                                                "decision": "approve"}).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429


def test_metadata_meets_chatgpt_requirements(nginx):
    c = nginx.client()
    prm = c.get("/.well-known/oauth-protected-resource/bets/mcp").json()
    asm = c.get("/.well-known/oauth-authorization-server/bets/mcp").json()
    issuer = "https://example.com/bets/mcp"
    assert prm["resource"] == issuer and prm["authorization_servers"] == [issuer]          # exact, no trailing slash
    assert asm["issuer"] == issuer
    for key, path in (("authorization_endpoint", "/authorize"), ("token_endpoint", "/token"),
                      ("revocation_endpoint", "/revoke")):
        assert asm[key] == issuer + path
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["client_id_metadata_document_supported"] is True and "registration_endpoint" not in asm
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]                         # public client + PKCE
    assert asm["scopes_supported"] == ["bets.intake"] and prm["scopes_supported"] == ["bets.intake"]
    assert {"authorization_code", "refresh_token"} <= set(asm["grant_types_supported"]) and asm["response_types_supported"] == ["code"]
    assert not asm.get("authorization_response_iss_parameter_supported")                  # not claimed (see server.py)
    assert c.get("/bets/mcp/.well-known/oauth-authorization-server").json() == asm        # issuer-relative copy agrees


# ---------------------------------------------------------------- Trusted Assistant mode, as ChatGPT sees it

def test_trusted_mode_results_through_the_plugin(mcp, bets, token):
    browser = bets.browser()

    def trust(*kinds, enabled=True):
        r = browser.post("/bets/settings/assistant/auto-confirm",
                         data={"csrf_token": browser.csrf, "kinds": list(kinds), **({"enabled": "on"} if enabled else {})})
        assert r.status_code == 303
    trust("account", "transfer")
    try:
        done = ok(call_tool(mcp, token, "submit_account_candidate", {"name": "Trusted Book", "account_type": "bookmaker"}))
        assert done["status"] == "confirmed" and done["auto_confirmed"] is True and "Recorded" in done["note"]
        assert bets.one("SELECT count(*) FROM accounts WHERE name = 'Trusted Book'") == 1
        held = ok(call_tool(mcp, token, "submit_transfer_candidate", {"from_account": "Lloyds", "to_account": "Smarkets",
                                                                      "amount": "5000", "funding_type": "temporary"}))
        assert held["status"] == "pending_confirmation" and "unusually large" in held["held_for_review"]
        assert "held this for the user's review" in held["note"]
    finally:
        trust(enabled=False)


# ---------------------------------------------------------------- settlement and historical free bets via the plugin

def test_settlement_tool_structured_errors_and_dry_run(mcp, bets, token):
    e = err(call_tool(mcp, token, "submit_settlement_candidate", {"settled_at": "2026-09-28", "event": "Nobody v Nothing",
                                                                  "outcome": "void"}))
    assert e["error"] == "not_found" and e["field"] == "position_number"
    e = err(call_tool(mcp, token, "submit_settlement_candidate", {"settled_at": "2026-09-28", "position_number": 1}))
    assert (e["error"], e["field"]) == ("missing_field", "outcome")
    pos = ok(call_tool(mcp, token, "submit_position_candidate", {
        "event": "Plugin settle test", "selection": "A", "bookmaker": "Betfred", "back_stake": "10", "back_odds": "3",
        "exchange": "Smarkets", "lay_odds": "3.1", "placed_at": "2026-09-27T18:00"}))
    dry = ok(call_tool(mcp, token, "submit_settlement_candidate", {
        "settled_at": "2026-09-27T21:00", "event": "Plugin settle test", "selection": "A", "outcome": "back_lost",
        "dry_run": True}))
    assert dry["status"] == "valid" and any("pending position" in w["message"] for w in dry["warnings"])
    assert pos["status"] == "pending_confirmation"


def test_position_tool_marks_historical_tokens(mcp, token):
    data = ok(call_tool(mcp, token, "submit_position_candidate", {
        "event": "Historical FB", "selection": "A", "bookmaker": "Betfred", "back_stake": "10", "back_odds": "4",
        "position_type": "free_bet_snr", "free_bet_already_marked_used": True, "dry_run": True}))
    leg = data["summary"]["legs"][0]
    assert leg["free_bet"] is True and leg["historical_token"] is True


def test_calculate_passes_on_an_ended_promotional_rate(mcp, bets, token):
    from datetime import date, timedelta
    browser = bets.browser()
    assert browser.post("/bets/accounts", data={"csrf_token": browser.csrf, "name": "PromoX", "kind": "exchange",
                                                "commission": "0"}).status_code == 303
    aid = bets.one("SELECT id FROM accounts WHERE name = ?", "PromoX")
    args = {"back_stake": "10", "back_odds": "4", "lay_odds": "4.2", "exchange": "PromoX"}
    assert ok(call_tool(mcp, token, "calculate_match", args))["commission_warning"] is None
    ended = (date.today() - timedelta(days=2)).isoformat()
    assert browser.post(f"/bets/accounts/{aid}", data={"csrf_token": browser.csrf, "name": "PromoX", "commission": "0",
                                                       "active": "1", "commission_expires_shown": "1",
                                                       "commission_expires": ended}).status_code == 303
    calc = ok(call_tool(mcp, token, "calculate_match", args))
    assert calc["commission"] == "0%" and "promotional commission recorded for" in calc["commission_warning"]
