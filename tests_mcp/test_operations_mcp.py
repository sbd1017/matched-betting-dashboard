"""The operating workflow through the real MCP layer (OAuth -> MCP -> Assistant API -> services): plan, calculate and
validate on the server, record what the user placed atomically, partial fills, funding, settlement by outcome,
rewards, evidence, dismissed issues, and what the assistant may never do (approve its own held change)."""
import itertools

import pytest

from tests_mcp.conftest import Bets, Mcp, call_tool, err, list_tools, ok

_n = itertools.count(1)


@pytest.fixture(scope="module")
def bets(tmp_path_factory):
    b = Bets(tmp_path_factory.mktemp("bets-operations"))
    yield b
    b.close()


@pytest.fixture(scope="module")
def mcp(tmp_path_factory, bets):
    m = Mcp(tmp_path_factory.mktemp("mcp-operations"), bets)
    yield m
    m.close()


@pytest.fixture
def token(mcp):
    return mcp.token()


def rid(prefix: str) -> str:
    return f"{prefix}-{next(_n)}"


@pytest.fixture(scope="module")
def world(bets):
    return bets.seed('''
now = times.now()
iso = lambda h: times.iso(now + timedelta(hours=h))
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
ll = conn.execute("SELECT id FROM accounts WHERE name = 'Lloyds'").fetchone()[0]
services.update_account(conn, sm, commission_bp=0)
coral = services.create_account(conn, name="Coral", kind="bookmaker")
bx = services.create_account(conn, name="Betdaq", kind="exchange", commission_bp=0, opening_balance=5000)
services.create_transfer(conn, from_id=ll, to_id=sm, amount=12000, at=iso(-48), capital_type="bankroll_contribution")
tok = services.create_free_bet(conn, account_id=coral, face_value=1000, kind="snr", expires_at=iso(100))
out.update(coral=coral, token=tok, sm=sm, bx=bx, ll=ll)
''')


def call(mcp, token, name, **args):
    return ok(call_tool(mcp, token, name, args))


def profit(mcp, token):
    return float(call(mcp, token, "get_dashboard_totals")["data"]["realised_profit"]["all_time"])


def test_tools_are_exposed_and_none_can_approve(mcp, token):
    names = {t.name for t in list_tools(mcp, token)}
    for n in ("get_operating_brief", "get_next_actions", "create_bet_plan", "validate_bet_plan", "recalculate_bet_plan",
              "record_matched_bet", "record_exchange_fill", "get_position_outcomes", "calculate_multi_outcome_hedge",
              "reconcile_rewards", "dismiss_issue", "acknowledge_issue", "get_funding_requirement", "add_record_evidence"):
        assert n in names, n
    assert not [n for n in names if n.startswith(("confirm", "approve", "decide", "place_", "withdraw"))]


def test_a_snr_screenshot_workflow(mcp, bets, token, world):
    brief = call(mcp, token, "get_operating_brief")["data"]
    assert brief["free_bets"] and brief["free_bets"][0]["bookmaker"] == "Coral"
    p0 = profit(mcp, token)
    made = call(mcp, token, "create_bet_plan", request_id="a-plan", event="Tottenham v Coventry City",
                selection="Coventry City", kind="free_bet_snr", bookmaker="Coral", back_odds="6.5", exchange="Smarkets",
                lay_odds="6.8", event_time="2026-12-20 15:00")
    assert made["status"] == "applied"
    pid = made["result"]["plan_id"]
    again = call(mcp, token, "create_bet_plan", request_id="a-plan", event="Tottenham v Coventry City",
                 selection="Coventry City", kind="free_bet_snr", bookmaker="Coral", back_odds="6.5", exchange="Smarkets",
                 lay_odds="6.8", event_time="2026-12-20 15:00")
    assert again["duplicate"] and bets.one("SELECT count(*) FROM bet_plans") == 1
    plan = call(mcp, token, "get_bet_plan", plan_id=pid)["data"]
    assert plan["legs"][1]["stake"] == "8.09" and plan["legs"][1]["liability"] == "46.92"
    v = call(mcp, token, "validate_bet_plan", plan_id=pid, request_id=rid("v"))["result"]
    assert v["status"] == "SAFE_TO_PLACE" and v["exchange"][0]["lay_stake"] == "8.09"
    not_yet = call(mcp, token, "record_matched_bet", plan_id=pid, request_id=rid("r"), confirmed_fully_matched=False)
    assert not_yet["status"] == "blocked" and "FULLY MATCHED" in str(not_yet)
    assert bets.one("SELECT count(*) FROM positions") == 0
    args = dict(plan_id=pid, request_id="a-record", confirmed_fully_matched=True, bookmaker_stake="10",
                bookmaker_odds="6.5", bookmaker_reference="CR123",
                evidence=[{"kind": "fully_matched", "description": "Smarkets shows FULLY MATCHED",
                           "facts": [{"name": "status", "value": "FULLY MATCHED"}]}])
    rec = call(mcp, token, "record_matched_bet", **args)
    assert rec["status"] == "applied" and rec["integrity"]["ok"]
    assert call(mcp, token, "record_matched_bet", **args)["duplicate"]
    assert call(mcp, token, "record_matched_bet", **{**args, "request_id": "a-record-2"})["status"] == "already_done"
    assert bets.one("SELECT count(*) FROM positions") == 1 and bets.one("SELECT count(*) FROM evidence") == 1
    pos = bets.one("SELECT position_id FROM bet_plans WHERE id = ?", pid)
    outc = call(mcp, token, "get_position_outcomes", position=str(pos))["data"]
    assert [r["total"] for r in outc["rows"]] == ["8.08", "8.09"]
    st = call(mcp, token, "settle_position", position_id=pos, request_id=rid("s"), outcome_bucket="Not Coventry City")
    assert st["status"] == "applied"
    assert profit(mcp, token) == pytest.approx(p0 + 8.09)
    assert call(mcp, token, "get_bet_plan", plan_id=pid)["data"]["status"] == "settled"


def test_b_atletico_multi_lay(mcp, bets, token, world):
    calc = call(mcp, token, "calculate_multi_outcome_hedge", outcomes=["1-1", "2-2", "Any other score"],
                backs=[{"stake": "10", "odds": "5.08", "covers": ["1-1", "2-2"]}],
                lays=[{"odds": "9.0", "covers": ["1-1"]}, {"odds": "15.0", "covers": ["2-2"]}])["data"]
    assert calc["lay_stakes"] == ["5.64", "3.39"] and calc["exchange_liability"] == "41.82"
    made = call(mcp, token, "create_bet_plan", request_id="b-plan", event="Atlético de Madrid v Manchester United",
                selection="Draw / BTTS / Under 4.5", market="Bet Builder", bookmaker="Betfred", back_stake="10",
                back_odds="5.08", outcomes=["1-1", "2-2", "Any other score"], back_covers=["1-1", "2-2"],
                lays=[{"exchange": "Smarkets", "odds": "9.0", "covers": ["1-1"], "label": "Lay 1-1"},
                      {"exchange": "Smarkets", "odds": "15.0", "covers": ["2-2"], "label": "Lay 2-2"}])
    pid = made["result"]["plan_id"]
    plan = call(mcp, token, "get_bet_plan", plan_id=pid)["data"]
    rows = {r["outcome"]: r["total"] for r in plan["planned"]["outcome_table"]["rows"]}
    assert rows == {"1-1": "-0.93", "2-2": "-1.02", "Any other score": "-0.97"}
    assert plan["planned"]["outcome_table"]["worst_case"] == "-1.02"


def test_c_partial_fill_then_rematch(mcp, bets, token, world):
    pid = call(mcp, token, "create_bet_plan", request_id="c-plan", event="Partial game", selection="Home",
               bookmaker="Betfred", back_stake="20", back_odds="5.0", exchange="Smarkets", lay_odds="5.0",
               lays=[{"exchange": "Smarkets", "odds": "5.0", "stake": "20"}])["result"]["plan_id"]
    call(mcp, token, "record_bookmaker_placement", plan_id=pid, request_id=rid("c"))
    call(mcp, token, "record_exchange_order", plan_id=pid, stake="20", odds="5.0", request_id=rid("c"))
    call(mcp, token, "record_exchange_fill", plan_id=pid, stake="12", odds="5.0", request_id=rid("c"))
    plan = call(mcp, token, "get_bet_plan", plan_id=pid)["data"]
    assert plan["status"] == "exchange_partially_matched"
    assert plan["placed_so_far"]["unmatched"][0]["remaining"] == "8.00"
    dup = call(mcp, token, "record_exchange_fill", plan_id=pid, stake="12", odds="5.0", request_id=rid("c"))
    assert dup["status"] == "blocked"
    call(mcp, token, "record_exchange_fill", plan_id=pid, stake="8", odds="5.2", request_id=rid("c"))
    plan = call(mcp, token, "get_bet_plan", plan_id=pid)["data"]
    assert plan["status"] == "fully_matched" and plan["legs"][1]["weighted_odds"] == "5.08"


def test_d_funding_shortfall_and_return(mcp, bets, token, world):
    p0 = profit(mcp, token)
    pid = call(mcp, token, "create_bet_plan", request_id="d-plan", event="Funded game", selection="Away",
               bookmaker="Betfred", back_stake="30", back_odds="5.0", exchange="Betdaq", lay_odds="5.0",
               event_time="2026-12-21 20:00")["result"]["plan_id"]
    ex = call(mcp, token, "get_funding_requirement", plan_id=pid)["data"]["exchanges"][0]
    assert (ex["available_now"], ex["new_liability_required"], ex["shortfall"]) == ("50.00", "120.00", "70.00")
    assert call(mcp, token, "validate_bet_plan", plan_id=pid, request_id=rid("d"))["result"]["status"] == "DO_NOT_PLACE"
    t = call(mcp, token, "record_funding_transfer", from_account="Lloyds", to_account="Betdaq", amount="75",
             funding_type="temporary_funding", plan_id=pid, request_id="d-fund")
    assert t["status"] == "applied"
    again = call(mcp, token, "record_funding_transfer", from_account="Lloyds", to_account="Betdaq", amount="75",
                 funding_type="temporary_funding", plan_id=pid, request_id="d-fund-2")
    assert again["status"] == "blocked"                        # the same screenshot sent twice
    totals = call(mcp, token, "get_dashboard_totals")["data"]
    assert totals["temporary_funding_outstanding"] == "75.00" and profit(mcp, token) == pytest.approx(p0)
    call(mcp, token, "record_matched_bet", plan_id=pid, request_id=rid("d"), confirmed_fully_matched=True)
    pos = bets.one("SELECT position_id FROM bet_plans WHERE id = ?", pid)
    call(mcp, token, "settle_position", position_id=pos, outcome="back_lost", request_id=rid("d"))
    acts = call(mcp, token, "get_next_actions")["data"]
    assert any(a["kind"] == "return_funding" and "£75.00" in a["title"] for a in acts)
    bets.seed('''
bx = conn.execute("SELECT id FROM accounts WHERE name = 'Betdaq'").fetchone()[0]
ll = conn.execute("SELECT id FROM accounts WHERE name = 'Lloyds'").fetchone()[0]
services.create_transfer(conn, from_id=bx, to_id=ll, amount=7500)
''')
    assert call(mcp, token, "get_dashboard_totals")["data"]["temporary_funding_outstanding"] == "0.00"
    assert profit(mcp, token) == pytest.approx(p0)
    assert not any(a["kind"] == "return_funding" for a in call(mcp, token, "get_next_actions")["data"])


def test_e_unusual_result_is_held_and_cannot_be_self_approved(mcp, bets, token, world):
    seeded = bets.seed('''
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
bf = conn.execute("SELECT id FROM accounts WHERE name = 'Betfred'").fetchone()[0]
out["pid"] = services.create_position(conn, event="Abandoned match", selection="Home", legs=[
    {"account_id": bf, "side": "back", "stake_pence": 1000, "odds": "2.0"},
    {"account_id": sm, "side": "lay", "stake_pence": 1000, "odds": "2.02"}])
''')
    held = call(mcp, token, "settle_position", position_id=seeded["pid"], outcome="void", unusual="abandoned",
                reason="the user says it was abandoned", request_id=rid("e"))
    assert held["status"] == "needs_confirmation" and held["change_id"]
    assert bets.one("SELECT status FROM positions WHERE id = ?", seeded["pid"]) == "open"
    status = call(mcp, token, "get_change_status", change_id=held["change_id"])
    assert status["status"] == "needs_confirmation"


def test_f_dismiss_duplicate_transfer_without_touching_money(mcp, bets, token, world):
    seeded = bets.seed('''
ll = conn.execute("SELECT id FROM accounts WHERE name = 'Lloyds'").fetchone()[0]
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
now = times.now()
a = services.create_transfer(conn, from_id=ll, to_id=sm, amount=5000, at=times.iso(now - timedelta(hours=3)))
b = services.create_transfer(conn, from_id=ll, to_id=sm, amount=5000, at=times.iso(now - timedelta(hours=2.9)))
out.update(a=a, b=b)
''')
    key = f"transfer:double:{seeded['b']}"
    keys = {i["key"] for i in call(mcp, token, "get_issues")["data"]["issues"]}
    assert key in keys
    before = call(mcp, token, "get_dashboard_totals")["data"]
    d = call(mcp, token, "acknowledge_issue", issue_key=key, reason="two genuine separate transfers", request_id=rid("f"))
    assert d["status"] == "applied"
    assert key not in {i["key"] for i in call(mcp, token, "get_issues")["data"]["issues"]}
    listed = call(mcp, token, "list_dismissed_issues")["data"]
    assert listed[0]["issue_key"] == key and listed[0]["reason"] == "two genuine separate transfers"
    after = call(mcp, token, "get_dashboard_totals")["data"]
    assert {k: v for k, v in after.items() if k != "issues"} == {k: v for k, v in before.items() if k != "issues"}
    assert bets.one("SELECT count(*) FROM transfers WHERE voided_at IS NULL AND id IN (?, ?)", seeded["a"], seeded["b"]) == 2
    again = call(mcp, token, "dismiss_issue", issue_key=key, reason="again", request_id=rid("f"))
    assert again["status"] == "already_done"


def test_g_rewards_and_evidence(mcp, bets, token, world):
    seeded = bets.seed('''
bf = conn.execute("SELECT id FROM accounts WHERE name = 'Betfred'").fetchone()[0]
out["offer"] = services.create_offer(conn, title="Betfred Bet £10 Get £10", account_id=bf)
''')
    call(mcp, token, "update_offer_progress", offer_id=seeded["offer"], request_id=rid("g"), qualifying_stake="10",
         min_odds="2", reward_count=1, reward_each="10", reward_type="snr", opted_in=True)
    e = call(mcp, token, "expect_reward", offer_id=seeded["offer"], request_id="g-expect")
    assert e["status"] == "applied"
    rid_ = e["result"]["expected_reward_id"]
    listed = call(mcp, token, "list_expected_rewards")["data"]
    assert any(r["id"] == rid_ and r["counts_in_profit"] is False for r in listed)
    assert call(mcp, token, "reconcile_rewards", request_id=rid("g"))["status"] == "already_done"   # nothing arrived yet
    got = call(mcp, token, "mark_reward_received", expected_reward_id=rid_, request_id="g-recv")
    assert len(got["result"]["created"]) == 1
    assert call(mcp, token, "mark_reward_received", expected_reward_id=rid_, request_id="g-recv-2")["status"] == "already_done"
    ev = call(mcp, token, "add_record_evidence", record_type="offer", record_id=seeded["offer"], kind="reward_credited",
              description="Betfred shows £10 free bet", request_id=rid("g"))
    assert ev["status"] == "applied"
    assert call(mcp, token, "list_record_evidence", record_type="offer", record_id=seeded["offer"])["data"]


def test_h_bad_input_is_rejected_server_side(mcp, token, world):
    e = err(call_tool(mcp, token, "create_bet_plan", {"event": "X", "bookmaker": "Nobody", "back_odds": "2",
                                                       "back_stake": "5", "request_id": rid("h")}))
    assert e["error"] == "invalid_value"
    e = err(call_tool(mcp, token, "create_bet_plan", {"event": "X", "bookmaker": "Betfred", "back_odds": "0.5",
                                                       "back_stake": "5", "request_id": rid("h")}))
    assert "odds" in str(e)
    e = err(call_tool(mcp, token, "update_bet_plan", {"plan_id": 2, "notes": "x", "expected_version": 999,
                                                       "request_id": rid("h")}))
    assert e["error"] == "conflict"


def test_i_skybet_unhedged_qualifier_and_immediate_reward(mcp, bets, token, world):
    seeded = bets.seed('''
fd = services.create_account(conn, name="First Direct", kind="bank")
sky = services.create_account(conn, name="Sky Bet", kind="bookmaker")
services.create_transfer(conn, from_id=fd, to_id=sky, amount=500, capital_type="bankroll_contribution")
out["offer"] = services.create_offer(conn, title="£30 in Free Bets when you place any bet", account_id=sky,
    reward_value=3000, terms={"qualifying_stake_pence": 5, "min_odds": "2.00", "reward_count": 3,
    "reward_each_pence": 1000, "reward_kind": "snr", "reward_expiry_days": 30, "reward_trigger": "bet_placed"})
out["sky"] = sky
''')
    p0 = profit(mcp, token)
    args = dict(offer_id=seeded["offer"], event="Lyon v Crystal Palace", selection="Crystal Palace",
                market="Full Time Result", stake="0.05", odds="2.80",
                reason="Qualifier stake only £0.05; deliberately not laid because maximum loss is 5p.",
                evidence=[{"kind": "bet_slip", "description": "Sky Bet bet placed £0.05 @ 9/5"}])
    r = call(mcp, token, "record_unhedged_qualifier", request_id="i-sky-1", **args)
    assert r["status"] == "applied" and r["result"]["offer"]["status"] == "awaiting_reward", r
    assert r["result"]["offer"]["qualifier_required"] is False
    assert bets.one("SELECT sum(amount_pence) FROM ledger_entries WHERE account_id = ? AND superseded_at IS NULL",
                    seeded["sky"]) == 495
    o = call(mcp, token, "get_offer", offer_id=seeded["offer"])["data"]
    assert (o["status"], o["stage"], o["qualifier_required"]) == ("awaiting_reward", "reward_expected", False)
    assert not [i for i in call(mcp, token, "get_issues")["data"]["issues"] if i["key"].startswith("incomplete:")]
    r2 = call(mcp, token, "record_unhedged_qualifier", request_id="i-sky-2", reward_credited=True, **args)
    assert r2["status"] == "applied" and r2["result"]["offer"]["status"] == "reward_received", r2
    assert bets.one("SELECT count(*) FROM free_bets WHERE offer_id = ?", seeded["offer"]) == 3
    assert bets.one("SELECT count(*) FROM positions WHERE offer_id = ?", seeded["offer"]) == 1
    assert call(mcp, token, "record_unhedged_qualifier", request_id="i-sky-3", reward_credited=True,
                **args)["status"] == "already_done"
    assert profit(mcp, token) == pytest.approx(p0)
    bad = err(call_tool(mcp, token, "update_offer", {"offer_id": seeded["offer"], "status": "awaiting_reward",
                                                     "request_id": rid("i")}))
    assert "follows its records" in bad["message"]


def test_j_funding_repaid_from_the_hub_bank(mcp, bets, token, world):
    seeded = bets.seed('''
hub = services.create_account(conn, name="Hub Bank", kind="bank")
lender = services.create_account(conn, name="Lender Bank", kind="bank")
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
services.create_transfer(conn, from_id=lender, to_id=sm, amount=5000, capital_type="temporary_funding")
services.create_transfer(conn, from_id=sm, to_id=hub, amount=5000, capital_type="bankroll_withdrawal")
''')
    p0 = profit(mcp, token)
    before = {b["bank"]: b for b in call(mcp, token, "get_dashboard_totals")["data"]["temporary_funding_by_bank"]}
    assert before["Lender Bank"]["outstanding"] == "50.00"
    r = call(mcp, token, "record_funding_repayment", from_account="Hub Bank", funding_source="Lender Bank", amount="50",
             request_id="j-repay-1")
    assert r["status"] == "applied" and r["result"]["outstanding_to_lender_after"] == "0.00", r
    after = {b["bank"]: b for b in call(mcp, token, "get_dashboard_totals")["data"]["temporary_funding_by_bank"]}
    assert after["Lender Bank"] == {"bank": "Lender Bank", "temporary_in": "50.00", "repaid": "50.00", "outstanding": "0.00"}
    assert profit(mcp, token) == pytest.approx(p0)
    assert call(mcp, token, "record_funding_repayment", from_account="Hub Bank", funding_source="Lender Bank",
                amount="50", request_id="j-repay-1")["duplicate"]
    over = call(mcp, token, "record_funding_repayment", from_account="Hub Bank", funding_source="Lender Bank",
                amount="10", request_id="j-repay-2")
    assert over["status"] == "blocked" and "nothing to repay" in str(over)
