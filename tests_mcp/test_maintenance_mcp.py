"""The assistant maintaining records through the MCP tools, end to end (OAuth -> MCP -> Assistant API -> services):
regression cases A-D from real use, idempotent retries, confirmation of held changes, and the read tools."""
import itertools

import pytest

from tests_mcp.conftest import Bets, Mcp, call_tool, err, ok

_n = itertools.count(1)


# These scenarios get their own betting app and MCP server, so they neither depend on nor disturb the shared ones.
@pytest.fixture(scope="module")
def bets(tmp_path_factory):
    b = Bets(tmp_path_factory.mktemp("bets-maintenance"))
    yield b
    b.close()


@pytest.fixture(scope="module")
def mcp(tmp_path_factory, bets):
    m = Mcp(tmp_path_factory.mktemp("mcp-maintenance"), bets)
    yield m
    m.close()


@pytest.fixture
def token(mcp):
    return mcp.token()


def rid(prefix: str) -> str:
    return f"{prefix}-{next(_n)}"


@pytest.fixture(scope="module")
def world(bets):
    """Coral welcome tokens with the duplicate/orphan pair, First Direct + Lloyds funding, an open Smarkets lay,
    the finished Atlético position and the two live Betfred tokens."""
    return bets.seed('''
now = times.now()
iso = lambda h: times.iso(now + timedelta(hours=h))
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
bf = conn.execute("SELECT id FROM accounts WHERE name = 'Betfred'").fetchone()[0]
ll = conn.execute("SELECT id FROM accounts WHERE name = 'Lloyds'").fetchone()[0]
services.update_account(conn, sm, commission_bp=0)
coral = services.create_account(conn, name="Coral", kind="bookmaker")
fd = services.create_account(conn, name="First Direct", kind="bank")
offer = services.create_offer(conn, title="Bet £5 Get £30 (Coral)", account_id=coral)
toks = services.create_free_bets(conn, count=6, account_id=coral, face_value=500, kind="snr", offer_id=offer,
                                 expires_at="2026-10-14T10:49:00Z")
for i, t in enumerate(toks[:5]):
    p = services.create_position(conn, event=f"Coral welcome {i}", selection="Draw", kind="free_bet_snr",
                                 event_at=iso(-40), legs=[
        {"account_id": coral, "side": "back", "stake_pence": 500, "odds": "5.0", "free_bet_id": t},
        {"account_id": sm, "side": "lay", "stake_pence": 380, "odds": "5.2"}])
    services.quick_settle(conn, p, "back_lost")
services.update_free_bet(conn, toks[5], {"status": "used"}, note="marked used by hand")
dup = services.create_free_bet(conn, account_id=coral, face_value=500, kind="snr",
                               notes="Horse Racing Bet Builder free bet used on 17:00 Newcastle")
p16 = services.create_position(conn, event="17:00 Newcastle", selection="Bet Builder", kind="free_bet_snr", legs=[
    {"account_id": coral, "side": "back", "stake_pence": 500, "odds": "5.60", "free_bet_id": dup}])
services.quick_settle(conn, p16, "back_lost")
for amount in (2500, 5000):
    services.create_transfer(conn, from_id=fd, to_id=sm, amount=amount, at=iso(-30), capital_type="bankroll_contribution")
services.create_transfer(conn, from_id=ll, to_id=sm, amount=7500, at=iso(-29))
cfb = services.create_free_bet(conn, account_id=coral, face_value=1000, kind="snr")
spurs = services.create_position(conn, event="Tottenham v Coventry City", selection="Coventry City", kind="free_bet_snr",
                                 event_at=iso(24 * 5), legs=[
    {"account_id": coral, "side": "back", "stake_pence": 1000, "odds": "6.50", "free_bet_id": cfb},
    {"account_id": sm, "side": "lay", "stake_pence": 809, "odds": "6.80"}])
services.create_transfer(conn, from_id=fd, to_id=bf, amount=1000, at=iso(-8), capital_type="bankroll_contribution")
atl = services.create_position(conn, event="Atlético de Madrid vs Manchester United", selection="Draw / BTTS / U4.5",
                               kind="qualifying", event_at=iso(-6), legs=[
    {"account_id": bf, "side": "back", "stake_pence": 1000, "odds": "5.08"},
    {"account_id": sm, "side": "lay", "stake_pence": 564, "odds": "9.00"},
    {"account_id": sm, "side": "lay", "stake_pence": 339, "odds": "15.00"}])
bb5 = services.create_free_bet(conn, account_id=bf, face_value=500, kind="snr", expires_at=iso(30), notes="Bet Builder only")
bb10 = services.create_free_bet(conn, account_id=bf, face_value=1000, kind="snr", expires_at=iso(54),
                                notes="Bet Builder Money Back")
out.update(orphan=toks[5], dup=dup, p16=p16, spurs=spurs, atl=atl, bb5=bb5, bb10=bb10, offer=offer)
''')


def dash(mcp, token):
    return ok(call_tool(mcp, token, "get_dashboard_totals"))["data"]


def test_a_coral_duplicate_fixed_entirely_by_the_assistant(mcp, bets, token, world):
    # inspect: the orphan used token and the issue that explains it
    used = ok(call_tool(mcp, token, "list_free_bets", {"status": "used", "bookmaker": "Coral"}))["data"]
    orphan = next(f for f in used if f["id"] == world["orphan"])
    assert orphan["position"] is None and orphan["expires_at"] == "2026-10-14 11:49"
    issues = ok(call_tool(mcp, token, "get_issues"))["data"]
    issue = next(i for i in issues["issues"] if i["key"] == f"freebet:unlinked:{world['orphan']}")
    repair = next(r for r in issue["repairs"] if r["change"] == "merge_duplicate_free_bets")
    pos = ok(call_tool(mcp, token, "get_position", {"position": str(world["p16"])}))["data"]
    assert pos["legs"][0]["free_bet"]["id"] == world["dup"]
    profit_before = dash(mcp, token)["realised_profit"]["all_time"]
    # preview, then apply with an idempotency key
    preview = ok(call_tool(mcp, token, "merge_duplicate_free_bets", {**repair["params"], "request_id": "coral-fix-1",
                                                                     "dry_run": True}))
    assert preview["status"] == "preview" and preview["records"]
    done = ok(call_tool(mcp, token, "merge_duplicate_free_bets", {**repair["params"], "request_id": "coral-fix-1"}))
    assert done["status"] == "applied" and "tell the user" in done["note"].lower()
    assert done["totals"]["issues"]["after"] < done["totals"]["issues"]["before"]
    retry = ok(call_tool(mcp, token, "merge_duplicate_free_bets", {**repair["params"], "request_id": "coral-fix-1"}))
    assert retry["status"] == "applied" and retry["duplicate"] and "nothing was applied twice" in retry["note"]
    # verify
    kept = ok(call_tool(mcp, token, "get_free_bet", {"free_bet_id": world["orphan"]}))["data"]
    gone = ok(call_tool(mcp, token, "get_free_bet", {"free_bet_id": world["dup"]}))["data"]
    assert kept["status"] == "used" and kept["position"]["id"] == world["p16"] and kept["offer"]["id"] == world["offer"]
    assert "Horse Racing Bet Builder" in kept["notes"] and gone["status"] == "void"
    keys = {i["key"] for i in ok(call_tool(mcp, token, "get_issues"))["data"]["issues"]}
    assert f"freebet:unlinked:{world['orphan']}" not in keys
    assert dash(mcp, token)["realised_profit"]["all_time"] == profit_before
    history = ok(call_tool(mcp, token, "get_record_history", {"record_type": "free_bet", "record_id": world["dup"]}))["data"]
    assert history[0]["actor"].startswith("assistant:") and history[0]["request_id"].endswith("coral-fix-1")


def test_b_available_balance_and_temporary_return_create_no_profit(mcp, bets, token, world):
    before = dash(mcp, token)
    assert before["temporary_funding_outstanding"] == "75.00"
    bets.seed('''
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
ll = conn.execute("SELECT id FROM accounts WHERE name = 'Lloyds'").fetchone()[0]
services.create_transfer(conn, from_id=sm, to_id=ll, amount=7500)
''')
    after = dash(mcp, token)
    assert after["temporary_funding_outstanding"] == "0.00"
    assert after["realised_profit"] == before["realised_profit"]
    breakdown = ok(call_tool(mcp, token, "get_account_balance_breakdown", {"account": "Smarkets"}))["data"]
    lays = ok(call_tool(mcp, token, "get_exchange_exposure", {"exchange": "Smarkets"}))["data"]["open_lays"]
    reserved = breakdown["reserved_lay_liability"]
    assert float(reserved) == pytest.approx(sum(float(l["liability"]) for l in lays)) and "46.92" in [l["liability"] for l in lays]
    available = breakdown["available_cash"]
    e = err(call_tool(mcp, token, "record_balance", {"account": "Smarkets", "observed": available,
                                                     "request_id": rid("bal")}))
    assert "available" in e["message"] and reserved in e["message"]
    r = ok(call_tool(mcp, token, "record_balance", {"account": "Smarkets", "observed": available, "figure": "available",
                                                    "request_id": rid("bal")}))
    assert r["status"] == "applied" and "matches" in r["summary"]
    assert dash(mcp, token)["realised_profit"] == before["realised_profit"]


def test_b_wrong_reconciliation_reversed_after_the_user_confirms(mcp, bets, token, world):
    seeded = bets.seed('''
sm = conn.execute("SELECT id FROM accounts WHERE name = 'Smarkets'").fetchone()[0]
exp = services.expected_balance(conn, sm, "available")
services.reconcile(conn, sm, exp["available"], note="available read as total (the old mistake)")
out["rid"] = services.last_reconciliation_id(conn, sm)
''')
    wrong = ok(call_tool(mcp, token, "get_reconciliation", {"reconciliation_id": seeded["rid"]}))["data"]
    reserved = ok(call_tool(mcp, token, "get_account_balance_breakdown", {"account": "Smarkets"}))["data"]["reserved_lay_liability"]
    assert wrong["difference"] == "-" + reserved and wrong["adjustment"]["active"]
    profit_wrong = dash(mcp, token)["realised_profit"]["all_time"]
    held = ok(call_tool(mcp, token, "reverse_reconciliation", {"reconciliation_id": seeded["rid"], "request_id": rid("rev"),
                                                               "reason": "that was the available balance"}))
    assert held["status"] == "needs_confirmation" and held["confirmation_url"].endswith(held["change_id"])
    assert "NOT changed" in held["note"]
    browser = bets.browser()                                   # the user confirms in /bets/
    r = browser.post(f"/bets/changes/{held['change_id']}/confirm", data={"csrf_token": browser.csrf})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    status = ok(call_tool(mcp, token, "get_change_status", {"change_id": held["change_id"]}))
    assert status["status"] == "applied" and status["decided_by"] == "user"
    fixed = float(dash(mcp, token)["realised_profit"]["all_time"])
    assert fixed == pytest.approx(float(profit_wrong) + float(reserved))
    rec = ok(call_tool(mcp, token, "get_reconciliation", {"reconciliation_id": seeded["rid"]}))["data"]
    assert rec["outcome"] == "reversed" and not rec["adjustment"]["active"]


def test_c_atletico_settled_by_the_assistant(mcp, bets, token, world):
    stc = ok(call_tool(mcp, token, "get_still_to_come_breakdown"))["data"]
    assert world["atl"] in [i["position_id"] for i in stc["not_counted_event_probably_finished"]]
    found = ok(call_tool(mcp, token, "list_positions", {"status": "open", "event": "Atlético"}))["data"]
    assert [p["id"] for p in found] == [world["atl"]]
    before = float(dash(mcp, token)["realised_profit"]["all_time"])
    args = {"position_id": world["atl"], "outcome": "back_lost", "request_id": "atletico-settle-1"}
    r = ok(call_tool(mcp, token, "settle_position", args))
    assert r["status"] == "applied" and r["level"] == "safe_edit"
    assert ok(call_tool(mcp, token, "settle_position", args))["duplicate"]
    assert ok(call_tool(mcp, token, "settle_position", {**args, "request_id": "atletico-settle-2"}))["status"] == "already_done"
    pos = ok(call_tool(mcp, token, "get_position", {"position": str(world["atl"])}))["data"]
    assert pos["status"] == "settled" and pos["realised_result"] == "-0.97"
    assert float(dash(mcp, token)["realised_profit"]["all_time"]) == pytest.approx(before - 0.97)
    stc = ok(call_tool(mcp, token, "get_still_to_come_breakdown"))["data"]
    assert world["atl"] not in [i["position_id"] for i in stc["not_counted_event_probably_finished"]]
    assert world["atl"] not in [i["position_id"] for i in stc["open_positions"]["items"]]


def test_d_live_betfred_tokens_stay_available(mcp, token, world):
    available = ok(call_tool(mcp, token, "list_free_bets", {"status": "available", "bookmaker": "Betfred"}))["data"]
    assert {world["bb5"], world["bb10"]} <= {f["id"] for f in available}
    r = ok(call_tool(mcp, token, "void_free_bet", {"free_bet_id": world["bb5"], "request_id": rid("d"), "dry_run": True}))
    assert r["status"] == "preview"                           # only a preview: nothing consumed
    still = ok(call_tool(mcp, token, "get_free_bet", {"free_bet_id": world["bb5"]}))["data"]
    assert still["status"] == "available"


def test_dependencies_block_and_deletions_always_wait(mcp, token, world):
    blocked = ok(call_tool(mcp, token, "delete_free_bet", {"free_bet_id": world["orphan"], "request_id": rid("del"),
                                                           "reason": "test"}))
    assert blocked["status"] == "blocked" and blocked["dependencies"] and "Explain" in blocked["note"]
    held = ok(call_tool(mcp, token, "delete_position", {"position_id": world["spurs"], "request_id": rid("del"),
                                                        "reason": "test only"}))
    assert held["status"] == "needs_confirmation" and held["level"] == "destructive_delete"
    pos = ok(call_tool(mcp, token, "get_position", {"position": str(world["spurs"])}))["data"]
    assert pos["status"] == "open" and not pos.get("removed")
    assert call_tool(mcp, token, "settle_position", {"position_id": world["spurs"], "request_id": "x" * 65}).is_error


def test_transfer_reclassification_through_mcp(mcp, bets, token, world):
    seeded = bets.seed('''
fd = conn.execute("SELECT id FROM accounts WHERE name = 'First Direct'").fetchone()[0]
coral = conn.execute("SELECT id FROM accounts WHERE name = 'Coral'").fetchone()[0]
out["tid"] = services.create_transfer(conn, from_id=fd, to_id=coral, amount=1500)
''')
    t = ok(call_tool(mcp, token, "get_transfer", {"transfer_id": seeded["tid"]}))["data"]
    assert t["funding_type"] == "temporary_funding" and t["affects_profit"] is False
    profit = dash(mcp, token)["realised_profit"]
    r = ok(call_tool(mcp, token, "reclassify_transfer", {"transfer_id": seeded["tid"], "funding_type": "bankroll_contribution",
                                                         "request_id": rid("tr"), "reason": "it was permanent"}))
    assert r["status"] == "applied" and r["totals"]["temporary_funding_outstanding"]["after"] == "0.00"
    assert dash(mcp, token)["realised_profit"] == profit
