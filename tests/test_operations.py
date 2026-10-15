"""Operations: bet plans, the outcome engine, price revisions, validation, funding, placement and fills, atomic
recording, rewards, evidence, next actions / brief, settlement by outcome, permissions, concurrency, dismissed issues,
and the regression scenarios from the brief (multi-lay Atlético, ordinary SNR, partial lay, funding, duplicates)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from bets import brief, calc, evidence, issues, metrics, ops, outcomes, plans, rewards, services, times

from .test_assistant import token  # noqa: F401 (fixture)

API = "/bets/api/assistant"


@pytest.fixture
def api(app, token):  # noqa: F811
    with TestClient(app, base_url="http://testserver") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


def change(api, name, request_id, **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": request_id})
    return r.status_code, r.json()


def q1(app, sql, *args):
    with app.state.db.read() as conn:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r else None


def summary(app):
    with app.state.db.read() as conn:
        return metrics.summary(conn)


@pytest.fixture
def book(app):
    with app.state.db.tx() as conn:
        ids = {"coral": services.create_account(conn, name="Coral", kind="bookmaker"),
               "betfred": services.create_account(conn, name="Betfred", kind="bookmaker"),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=0),
               "fd": services.create_account(conn, name="First Direct", kind="bank"),
               "lloyds": services.create_account(conn, name="Lloyds", kind="bank")}
        services.create_transfer(conn, from_id=ids["fd"], to_id=ids["smarkets"], amount=20000, at=at(-48),
                                 capital_type="bankroll_contribution")
        services.create_transfer(conn, from_id=ids["fd"], to_id=ids["betfred"], amount=5000, at=at(-48),
                                 capital_type="bankroll_contribution")
        ids["token"] = services.create_free_bet(conn, account_id=ids["coral"], face_value=1000, kind="snr",
                                                expires_at=at(72), notes="any sport")
    return ids


def snr_plan(api, rid="plan-1", **extra):
    code, body = change(api, "create_bet_plan", rid, event="Tottenham v Coventry City", selection="Coventry City",
                        event_time=times.local(at(120), "%Y-%m-%dT%H:%M"), kind="free_bet_snr", bookmaker="Coral",
                        back_odds="6.5", exchange="Smarkets", lay_odds="6.8", **extra)
    assert code == 200 and body["status"] == "applied", body
    return body["result"]["plan_id"]


# ================================================================ regression 35: ordinary SNR, start to finish

def test_snr_plan_lifecycle(app, book, api, browser):
    s0 = summary(app)
    pid = snr_plan(api)
    plan = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]
    assert plan["status"] == "draft" and plan["free_bet"]["id"] == book["token"]
    lay = plan["legs"][1]
    assert (lay["stake"], lay["liability"], lay["stake_calculated_by_server"]) == ("8.09", "46.92", True)
    tab = plan["planned"]["outcome_table"]
    assert [r["total"] for r in tab["rows"]] == ["8.08", "8.09"] and tab["locked"] and plan["planned"]["retention_pct"] == 80.8
    s1 = summary(app)
    assert (s1.profit_lifetime, s1.betting_balance, s1.free_bets_count) == (s0.profit_lifetime, s0.betting_balance,
                                                                             s0.free_bets_count)   # a plan holds no money
    code, v = change(api, "validate_bet_plan", "v-1", plan_id=pid)
    res = v["result"]
    assert res["status"] == "SAFE_TO_PLACE" and res["exchange"][0]["lay_stake"] == "8.09"
    assert res["available_exchange_cash"] == "200.00" and res["after_bet_available_cash"] == "153.08"
    code, b = change(api, "record_bookmaker_placement", "p-1", plan_id=pid, stake="10", odds="6.5", reference="CR123")
    assert b["status"] == "applied" and q1(app, "SELECT status FROM free_bets WHERE id = ?", book["token"]) == "used"
    pos_id = q1(app, "SELECT position_id FROM bet_plans WHERE id = ?", pid)
    assert pos_id and q1(app, "SELECT status FROM bet_plans WHERE id = ?", pid) == "bookmaker_placed"
    code, f = change(api, "mark_exchange_fully_matched", "m-1", plan_id=pid, confirmed_fully_matched=True)
    assert f["status"] == "applied" and q1(app, "SELECT status FROM bet_plans WHERE id = ?", pid) == "fully_matched"
    assert summary(app).lay_liability == 4692
    code, st = change(api, "settle_position", "s-1", position_id=pos_id, outcome="back_lost")
    assert st["status"] == "applied" and summary(app).profit_lifetime - s0.profit_lifetime == 809
    assert q1(app, "SELECT status FROM bet_plans WHERE id = ?", pid) == "settled" and summary(app).lay_liability == 0
    page = browser.get(f"/bets/plans/{pid}").text
    assert "Outcomes" in page and "8.09" in page and "Settled" in page


def test_draft_plans_and_cancel_free_the_token(app, book, api):
    pid = snr_plan(api)
    code, body = change(api, "create_bet_plan", "dup-1", event="Tottenham v Coventry City", selection="Coventry City",
                        kind="free_bet_snr", bookmaker="Coral", back_odds="6.5", exchange="Smarkets", lay_odds="6.8")
    assert code == 400 and "plan #" in body["errors"][0]                # a second plan for the same bet is refused
    code, c = change(api, "cancel_bet_plan", "c-1", plan_id=pid, reason="no longer")
    assert c["status"] == "applied" and q1(app, "SELECT status FROM free_bets WHERE id = ?", book["token"]) == "available"
    assert snr_plan(api, "plan-2") != pid                                # the token can be planned again


# ================================================================ regression 34: multi-lay Atlético Bet Builder

ATL = dict(event="Atlético de Madrid v Manchester United", selection="Draw / BTTS Yes / Under 4.5", market="Bet Builder",
           kind="qualifying", bookmaker="Betfred", back_stake="10", back_odds="5.08",
           outcomes=["1-1", "2-2", "Any other score"], back_covers=["1-1", "2-2"],
           lays=[{"exchange": "Smarkets", "odds": "9.0", "covers": ["1-1"], "label": "Lay 1-1", "market": "Correct Score"},
                 {"exchange": "Smarkets", "odds": "15.0", "covers": ["2-2"], "label": "Lay 2-2", "market": "Correct Score"}])


def test_multi_lay_outcome_engine_atletico(app, book, api):
    code, body = change(api, "create_bet_plan", "atl-1", **ATL, event_time=times.local(at(30), "%Y-%m-%dT%H:%M"))
    pid = body["result"]["plan_id"]
    plan = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]
    assert [l["stake"] for l in plan["legs"] if l["side"] == "lay"] == ["5.64", "3.39"]   # the solver = the real stakes
    rows = {r["outcome"]: r["total"] for r in plan["planned"]["outcome_table"]["rows"]}
    assert rows == {"1-1": "-0.93", "2-2": "-1.02", "Any other score": "-0.97"}      # not "both lays lose together"
    change(api, "validate_bet_plan", "atl-v", plan_id=pid)
    code, rec = change(api, "record_matched_bet", "atl-r", plan_id=pid, confirmed_fully_matched=True,
                       bookmaker={"stake": "10", "odds": "5.08"})
    assert rec["status"] == "applied", rec
    pos = q1(app, "SELECT position_id FROM bet_plans WHERE id = ?", pid)
    outc = api.get(f"{API}/read/position_outcomes", params={"ref": str(pos)}).json()["data"]
    assert outc["model"] == "verified outcome table" and outc["worst_case"] == "-1.02"
    with app.state.db.read() as conn:
        assert metrics.open_lay_liability(conn) == 4182                # the exchange's real exposure, not 92.58
    p0 = summary(app).profit_lifetime
    code, st = change(api, "settle_position", "atl-s", position_id=pos, outcome_bucket="Any other score")
    assert st["status"] == "applied" and summary(app).profit_lifetime - p0 == -97


def test_engine_rejects_bad_models_and_handles_overlaps():
    B = ["1-1", "2-2", "Other"]
    with pytest.raises(outcomes.ModelError):
        outcomes.table(B, [{"side": "lay", "stake_pence": 100, "odds": "2", "covers": B}])
    with pytest.raises(outcomes.ModelError):
        outcomes.table(B, [{"side": "back", "stake_pence": 100, "odds": "2", "covers": ["3-3"]}])
    legs = [{"side": "back", "stake_pence": 1000, "odds": "4.0", "covers": ["1-1", "2-2"]},
            {"side": "lay", "stake_pence": 1000, "odds": "3.0", "covers": ["1-1", "2-2"]},     # overlapping: lay the draw
            {"side": "lay", "stake_pence": 200, "odds": "11", "covers": ["2-2"]}]
    tab = outcomes.table(B, legs)
    assert [r["total"] for r in tab["buckets"]] == [3000 - 2000 + 200, 3000 - 2000 - 2000, -1000 + 1000 + 200]
    assert tab["worst_outcome"] == "2-2" and not tab["locked"]


def test_calculations_match_the_app_calculator():
    for kind, free in (("qualifying", None), ("free_bet_snr", "snr"), ("free_bet_sr", "sr")):
        for com in (0, 200):
            h = outcomes.hedge(outcomes.simple_buckets("X"), [{"stake_pence": 2500, "odds": "3.4", "covers": ["X"],
                                                                "free": free}], [{"odds": "3.55", "covers": ["X"],
                                                                                 "commission_bp": com}])
            m = calc.match(kind, 2500, Decimal("3.4"), Decimal("3.55"), com)
            assert h["lay_stakes"] == [m.lay_stake] and h["liability"] == m.liability
            assert {r["total"] for r in h["table"]["buckets"]} == {m.if_back_wins, m.if_lay_wins}


# ================================================================ price drift and the retention policy

def test_recalculation_keeps_history_and_rates_against_the_policy(app, book, api):
    pid = snr_plan(api)
    code, r = change(api, "recalculate_bet_plan", "rc-1", plan_id=pid, lay_odds="7.2")
    res = r["result"]
    assert res["result"] == "ACCEPTABLE" and res["rating_before"]["value"] == 80.8 and res["rating_after"]["value"] == 76.3
    code, r = change(api, "recalculate_bet_plan", "rc-2", plan_id=pid, lay_odds="9.5")
    assert r["result"]["result"] == "REJECT"
    revs = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]["price_revisions"]
    assert [x["legs"][1]["odds"] for x in revs] == ["6.80", "7.20", "9.50"] and revs[0]["reason"] == "original quote"
    code, v = change(api, "validate_bet_plan", "rc-v", plan_id=pid)
    assert v["result"]["status"] == "DO_NOT_PLACE" and any("Retention" in b for b in v["result"]["blockers"])
    policy = api.get(f"{API}/read/retention_policy").json()["data"]["policy"]
    assert policy["snr"] == {"excellent": 80.0, "very_good": 75.0, "acceptable": 70.0, "reject_below": 65.0}
    with app.state.db.tx() as conn:
        plans.save_policy(conn, {"snr": {"reject_below": 50}})
    code, v = change(api, "validate_bet_plan", "rc-v2", plan_id=pid)
    assert not any("Retention" in b for b in v["result"]["blockers"])


# ================================================================ validation

def test_validation_offer_token_and_exchange_rules(app, book, api):
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Bet £10 Get £10", account_id=book["betfred"],
                                    terms={"qualifying_stake_pence": 1000, "min_odds": "2.0", "reward_count": 1,
                                           "reward_each_pence": 1000, "reward_kind": "snr"})
        conn.execute("UPDATE offers SET opt_in_required = 1 WHERE id = ?", (oid,))
    code, body = change(api, "create_bet_plan", "q-1", event="Spurs v Leeds", selection="Spurs", bookmaker="Betfred",
                        back_stake="5", back_odds="1.8", exchange="Smarkets", lay_odds="1.85", offer_id=oid)
    pid = body["result"]["plan_id"]
    v = change(api, "validate_bet_plan", "q-v", plan_id=pid)[1]["result"]
    text = " | ".join(v["blockers"])
    assert v["status"] == "DO_NOT_PLACE" and "Opted in" in text and "Qualifying stake" in text and "Minimum odds" in text
    change(api, "update_offer_progress", "q-o", offer_id=oid, opted_in=True)
    change(api, "recalculate_bet_plan", "q-r", plan_id=pid, back_odds="2.1", lay_odds="2.14", back_stake="10")
    assert change(api, "validate_bet_plan", "q-v2", plan_id=pid)[1]["result"]["status"] in ("SAFE_TO_PLACE", "WARNING")
    # expired token, insufficient liability
    with app.state.db.tx() as conn:
        conn.execute("UPDATE free_bets SET expires_at = ? WHERE id = ?", (at(-1), book["token"]))
    tp = snr_plan(api, "plan-exp")
    v = change(api, "validate_bet_plan", "e-v", plan_id=tp)[1]["result"]
    assert any("Free bet not expired" in b for b in v["blockers"])
    code, body = change(api, "create_bet_plan", "big-1", event="Big game", selection="Home", bookmaker="Betfred",
                        back_stake="50", back_odds="9.0", exchange="Smarkets", lay_odds="9.2")
    v = change(api, "validate_bet_plan", "big-v", plan_id=body["result"]["plan_id"])[1]["result"]
    assert any("funds for the liability" in b and "shortfall" in b for b in v["blockers"])


def test_one_token_per_live_plan(app, book, api):
    pid = snr_plan(api)
    code, body = change(api, "create_bet_plan", "t-2", event="Other game", selection="Home", kind="free_bet_snr",
                        bookmaker="Coral", back_odds="5", exchange="Smarkets", lay_odds="5.1", free_bet_id=book["token"])
    assert code == 400 and f"plan #{pid}" in body["errors"][0]
    assert q1(app, "SELECT count(*) FROM bet_plans WHERE free_bet_id = ? AND status NOT IN ('cancelled')", book["token"]) == 1


# ================================================================ regression 36: partial lay, rematch at another price

def test_partial_fills_and_weighted_odds(app, book, api):
    code, body = change(api, "create_bet_plan", "pf-1", event="Partial match", selection="Home", bookmaker="Betfred",
                        back_stake="20", back_odds="5.0", exchange="Smarkets", lay_odds="5.0", lay_stake="20")
    pid = body["result"]["plan_id"]
    change(api, "record_bookmaker_placement", "pf-b", plan_id=pid)
    change(api, "record_exchange_order", "pf-o", plan_id=pid, stake="20", odds="5.0")
    with app.state.db.read() as conn:
        assert plans.unmatched_order_reserve(conn) == 8000                 # unmatched order holds its own liability
        assert metrics.summary(conn).lay_liability == 0                     # ...not matched liability
    change(api, "record_exchange_fill", "pf-f1", plan_id=pid, stake="12", odds="5.0")
    plan = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]
    assert plan["status"] == "exchange_partially_matched"
    unmatched = plan["placed_so_far"]["unmatched"][0]
    assert (unmatched["remaining"], unmatched["liability_if_matched"]) == ("8.00", "32.00")
    assert plan["placed_so_far"]["fully_hedged"] is False
    assert summary(app).lay_liability == 4800
    code, again = change(api, "record_exchange_fill", "pf-f1b", plan_id=pid, stake="12", odds="5.0")
    assert again["status"] == "blocked"                                    # same screenshot twice
    change(api, "record_exchange_fill", "pf-f2", plan_id=pid, stake="8", odds="5.2")
    plan = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]
    lay = plan["legs"][1]
    assert plan["status"] == "fully_matched" and lay["weighted_odds"] == "5.08" and lay["matched"] == "20.00"
    assert summary(app).lay_liability == 4800 + 3360
    assert q1(app, "SELECT count(*) FROM legs WHERE position_id = ? AND side = 'lay'", plan["position_id"]) == 2


def test_cancel_unmatched_remainder(app, book, api):
    code, body = change(api, "create_bet_plan", "cu-1", event="Cancel rest", selection="Home", bookmaker="Betfred",
                        back_stake="20", back_odds="5.0", exchange="Smarkets", lay_odds="5.0", lay_stake="20")
    pid = body["result"]["plan_id"]
    change(api, "record_bookmaker_placement", "cu-b", plan_id=pid)
    change(api, "record_exchange_order", "cu-o", plan_id=pid, stake="20", odds="5.0")
    change(api, "record_exchange_fill", "cu-f", plan_id=pid, stake="12", odds="5.0")
    code, c = change(api, "cancel_exchange_order", "cu-c", plan_id=pid)
    assert c["result"]["cancelled"] == "8.00"
    with app.state.db.read() as conn:
        assert plans.unmatched_order_reserve(conn) == 0
        assert not [i for i in issues.collect(conn) if i["key"].startswith("plan:unmatched")]
    plan = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]
    assert plan["legs"][1]["matched"] == "12.00" and plan["placed_so_far"]["fully_hedged"] is False


# ================================================================ regression 38: duplicate submissions, atomic recording

def test_record_matched_bet_is_atomic_and_idempotent(app, book, api):
    pid = snr_plan(api)
    s0 = summary(app)
    ledger0 = q1(app, "SELECT count(*) FROM ledger_entries")
    args = dict(plan_id=pid, confirmed_fully_matched=True, bookmaker={"stake": "10", "odds": "6.5"},
                evidence=[{"kind": "fully_matched", "description": "Smarkets FULLY MATCHED", "facts": {"status": "FULLY MATCHED"}}])
    code, first = change(api, "record_matched_bet", "rm-1", **args)
    assert first["status"] == "applied" and first["integrity"]["ok"]
    code, retry = change(api, "record_matched_bet", "rm-1", **args)
    assert retry["duplicate"] and retry["status"] == "applied"
    code, again = change(api, "record_matched_bet", "rm-2", **args)
    assert again["status"] == "already_done"
    assert q1(app, "SELECT count(*) FROM positions") == 1
    assert q1(app, "SELECT count(*) FROM legs WHERE free_bet_id = ?", book["token"]) == 1
    assert summary(app).lay_liability == 4692 and summary(app).free_bets_count == s0.free_bets_count - 1
    assert q1(app, "SELECT count(*) FROM ledger_entries") - ledger0 == 0    # SNR back + unsettled lay: nothing posted yet
    assert q1(app, "SELECT count(*) FROM evidence WHERE entity = 'plan' AND entity_id = ?", pid) == 1


def test_failed_recording_leaves_nothing_behind(app, book, api):
    pid = snr_plan(api)
    code, body = change(api, "record_matched_bet", "bad-1", plan_id=pid, confirmed_fully_matched=True,
                        bookmaker={"stake": "10", "odds": "6.5"}, lays=[{"stake": "8.09", "odds": "0.5"}])
    assert code == 400
    assert q1(app, "SELECT count(*) FROM positions") == 0
    assert q1(app, "SELECT status FROM free_bets WHERE id = ?", book["token"]) == "available"
    assert q1(app, "SELECT status FROM bet_plans WHERE id = ?", pid) == "draft"
    assert q1(app, "SELECT count(*) FROM legs") == 0


# ================================================================ regression 37: funding the liability

def test_funding_shortfall_top_up_and_return(app, book, api):
    with app.state.db.tx() as conn:                       # Smarkets down to £50 free
        services.create_transfer(conn, from_id=book["smarkets"], to_id=book["fd"], amount=15000,
                                 capital_type="bankroll_withdrawal")
    code, body = change(api, "create_bet_plan", "fu-1", event="Funded game", selection="Away", bookmaker="Betfred",
                        back_stake="30", back_odds="5.0", exchange="Smarkets", lay_odds="5.0",
                        event_time=times.local(at(6), "%Y-%m-%dT%H:%M"))
    pid = body["result"]["plan_id"]
    fund = api.get(f"{API}/read/funding_requirement", params={"id": pid}).json()["data"]
    ex = fund["exchanges"][0]
    assert (ex["available_now"], ex["new_liability_required"], ex["shortfall"]) == ("50.00", "120.00", "70.00")
    assert change(api, "validate_bet_plan", "fu-v", plan_id=pid)[1]["result"]["status"] == "DO_NOT_PLACE"
    p0 = summary(app).profit_lifetime
    code, t = change(api, "record_funding_transfer", "fu-t", from_account="Lloyds", to_account="Smarkets", amount="75",
                     plan_id=pid)
    assert t["status"] == "applied"
    ex = api.get(f"{API}/read/funding_requirement", params={"id": pid}).json()["data"]["exchanges"][0]
    assert (ex["available_now"], ex["shortfall"], ex["available_after_bet"]) == ("125.00", "0.00", "5.00")
    assert summary(app).temporary_outstanding == 7500 and summary(app).profit_lifetime == p0
    assert change(api, "validate_bet_plan", "fu-v2", plan_id=pid)[1]["result"]["status"] in ("SAFE_TO_PLACE", "WARNING")
    change(api, "record_matched_bet", "fu-r", plan_id=pid, confirmed_fully_matched=True)
    pos = q1(app, "SELECT position_id FROM bet_plans WHERE id = ?", pid)
    acts = api.get(f"{API}/read/next_actions").json()["data"]
    assert not any(a["kind"] == "return_funding" for a in acts)              # liability still held
    change(api, "settle_position", "fu-s", position_id=pos, outcome="back_lost")
    acts = api.get(f"{API}/read/next_actions").json()["data"]
    ret = [a for a in acts if a["kind"] == "return_funding"]
    assert ret and "£75.00 temporary Lloyds funding can be returned" in ret[0]["title"]
    with app.state.db.tx() as conn:
        services.create_transfer(conn, from_id=book["smarkets"], to_id=book["lloyds"], amount=7500)
    s = summary(app)
    assert s.temporary_outstanding == 0 and s.profit_lifetime == p0 + 30 * 100 - 3000   # back lost -30, lay won +30


# ================================================================ rewards

def test_expected_reward_lifecycle_and_reconciliation(app, book, api):
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Betfred UCL Tuesday", account_id=book["betfred"],
                                    terms={"qualifying_stake_pence": 1000, "min_odds": "2", "reward_count": 1,
                                           "reward_each_pence": 1000, "reward_kind": "snr", "reward_expiry_days": 7})
        pid = services.create_position(conn, event="Qualifier", selection="Home", offer_id=oid, legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.5"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 980, "odds": "2.55"}])
        services.quick_settle(conn, pid, "back_lost")
        services.quick_settle(conn, pid, "back_lost")
    exp = api.get(f"{API}/read/expected_rewards").json()["data"]
    assert len(exp) == 1 and exp[0]["expected"] == "1 x £10.00 SNR" and exp[0]["counts_in_profit"] is False
    rid = exp[0]["id"]
    with app.state.db.tx() as conn:
        lc = rewards.lifecycle(conn, services.row(conn, "offers", oid))
        assert lc["stage"] == "reward_expected"
        conn.execute("UPDATE expected_rewards SET expected_by = ? WHERE id = ?", (at(-1), rid))
    assert any(i["key"] == f"reward:overdue:{rid}" for i in issues.collect(app.state.db.connect()))
    p0 = summary(app).profit_lifetime
    with app.state.db.tx() as conn:                          # the user records the token another way
        token_id = services.create_free_bet(conn, account_id=book["betfred"], face_value=1000, kind="snr")
    code, r = change(api, "reconcile_rewards", "rr-1")
    assert r["status"] == "applied" and q1(app, "SELECT expected_reward_id FROM free_bets WHERE id = ?", token_id) == rid
    code, again = change(api, "mark_reward_received", "rr-2", expected_reward_id=rid)
    assert again["status"] == "already_done"
    assert q1(app, "SELECT count(*) FROM free_bets WHERE account_id = ? AND deleted_at IS NULL", book["betfred"]) == 1
    assert summary(app).profit_lifetime == p0                  # rewards are not profit until converted
    assert not any(i["key"].startswith("reward:") for i in issues.collect(app.state.db.connect()))


def test_mark_received_creates_only_missing_tokens(app, book, api):
    with app.state.db.tx() as conn:
        rid, _ = rewards.expect(conn, account_id=book["coral"], kind="snr", count=3, each=500, expiry_days=7)
        existing = services.create_free_bet(conn, account_id=book["coral"], face_value=500, kind="snr")
    code, r = change(api, "mark_reward_received", "mr-1", expected_reward_id=rid)
    assert len(r["result"]["created"]) == 2 and existing in r["result"]["linked"]
    code, r2 = change(api, "mark_reward_received", "mr-2", expected_reward_id=rid)
    assert r2["status"] == "already_done"
    assert q1(app, "SELECT count(*) FROM free_bets WHERE expected_reward_id = ?", rid) == 3


# ================================================================ next actions and the brief

def test_next_actions_order_and_operating_brief(app, book, api):
    with app.state.db.tx() as conn:
        services.update_free_bet(conn, book["token"], {"expires_at": at(10)})
        services.create_free_bet(conn, account_id=book["betfred"], face_value=500, kind="snr", expires_at=at(60))
        services.create_position(conn, event="Finished match", selection="Home", event_at=at(-5), legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.0"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 1000, "odds": "2.02"}])
    acts = api.get(f"{API}/read/next_actions").json()["data"]
    assert acts[0]["priority"] == "urgent" and "Coral" in acts[0]["title"] and acts[0]["potential_value"]
    kinds = [a["kind"] for a in acts]
    assert "settle" in kinds and kinds.index("free_bet_expiry") < kinds.index("settle")
    settle = next(a for a in acts if a["kind"] == "settle")
    assert settle["assistant_can_resolve"] and not settle["needs_user_externally"]
    b = api.get(f"{API}/read/operating_brief").json()["data"]
    for key in ("profit", "bankroll", "free_bets", "offers", "expected_rewards", "bet_plans", "positions", "still_to_come",
                "issues", "next_actions", "recommended_next_three"):
        assert key in b, key
    assert b["free_bets"][0]["bookmaker"] == "Coral" and len(b["recommended_next_three"]) == 3
    assert b["bankroll"]["exchange"]["reserved_lay_liability"] == "10.20"
    stc = b["still_to_come"]
    assert stc["not_counted_event_probably_finished"][0]["event"] == "Finished match"


# ================================================================ settlement by outcome, unusual results held

def test_settlement_simple_bucket_and_unusual_results(app, book, api):
    with app.state.db.tx() as conn:
        pid = services.create_position(conn, event="Simple", selection="Home", event_at=at(-4), legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.0"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 1000, "odds": "2.02"}])
    code, r = change(api, "settle_position", "sb-1", position_id=pid, outcome_bucket="Home")
    assert r["status"] == "applied" and q1(app, "SELECT result FROM legs WHERE position_id = ? AND side = 'back'", pid) == "won"
    with app.state.db.tx() as conn:
        p2 = services.create_position(conn, event="Abandoned", selection="Home", event_at=at(-4), legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.0"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 1000, "odds": "2.02"}])
    code, r = change(api, "settle_position", "sb-2", position_id=p2, outcome="void", unusual="abandoned", reason="match abandoned")
    assert r["status"] == "needs_confirmation" and r["level"] == "financial_reverse"


# ================================================================ permissions, concurrency, anomalies

def test_planning_permission_and_conflicts(app, book, api, browser):
    pid = snr_plan(api)
    ver = api.get(f"{API}/read/bet_plan", params={"id": pid}).json()["data"]["version"]
    code, ok = change(api, "update_bet_plan", "cv-1", plan_id=pid, notes="first", expected_version=ver)
    assert ok["status"] == "applied"
    code, conflict = change(api, "update_bet_plan", "cv-2", plan_id=pid, notes="stale", expected_version=ver)
    assert code == 409 and conflict["status"] == "conflict"
    browser.post("/bets/settings/assistant/permissions", {"read": "on", "planning": "ask", "safe_edit": "auto",
                                                          "financial_reverse": "ask"})
    code, held = change(api, "update_bet_plan", "cv-3", plan_id=pid, notes="needs ok")
    assert code == 202 and held["status"] == "needs_confirmation"
    page = browser.get("/bets/settings").text
    assert 'name="planning"' in page and 'id="policy"' in page


def test_large_change_is_flagged_for_review(app, book, api):
    with app.state.db.tx() as conn:
        pid = services.create_position(conn, event="Big win", selection="Home", event_at=at(-4), legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 5000, "odds": "3.0"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 2000, "odds": "2.5"}])
    code, r = change(api, "settle_position", "an-1", position_id=pid, outcome="back_won")
    assert r["status"] == "applied" and r["anomalies"] and "realised profit moved" in r["anomalies"][0]


# ================================================================ evidence

def test_evidence_metadata_and_upload(app, book, api, browser):
    pid = snr_plan(api)
    args = dict(record_type="plan", record_id=pid, kind="bet_slip", description="Coral slip £10 @ 6.5",
                facts={"stake": "10", "odds": "6.5"})
    code, a = change(api, "add_record_evidence", "ev-1", **args)
    code, b = change(api, "add_record_evidence", "ev-2", **args)
    assert a["status"] == "applied" and b["status"] == "already_done"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = browser.c.post("/bets/evidence", data={"csrf_token": browser.csrf, "entity": "plan", "entity_id": str(pid),
                                               "kind": "fully_matched", "description": "screen", "next": f"/plans/{pid}"},
                       files={"file": ("shot.png", png, "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    eid = q1(app, "SELECT id FROM evidence WHERE file_name IS NOT NULL")
    f = browser.get(f"/bets/evidence/{eid}/file")
    assert f.status_code == 200 and f.content == png
    bad = browser.c.post("/bets/evidence", data={"csrf_token": browser.csrf, "entity": "plan", "entity_id": str(pid),
                                                 "kind": "other"}, files={"file": ("x.exe", b"MZ" + b"0" * 50, "x/y")},
                         follow_redirects=False)
    assert "err=" in bad.headers["location"]
    nocsrf = browser.c.post("/bets/evidence", data={"entity": "plan", "entity_id": str(pid), "kind": "other"},
                            files={"file": ("shot.png", png, "image/png")}, follow_redirects=False)
    assert nocsrf.status_code == 403


# ================================================================ dismissed issues (the transfer #5 / #6 case)

def test_dismissed_issue_stays_dismissed_until_its_facts_change(app, book, api):
    with app.state.db.tx() as conn:
        a = services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=5000, at=at(-30),
                                     capital_type="temporary_funding")
        b = services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=5000, at=at(-29.9),
                                     capital_type="temporary_funding")
    key = f"transfer:double:{b}"
    found = {i["key"]: i for i in api.get(f"{API}/read/issues").json()["data"]["issues"]}
    assert key in found and [r["change"] for r in found[key]["repairs"]] == ["reverse_transfer", "dismiss_issue"]
    code, d = change(api, "dismiss_issue", "di-1", issue_key=key, reason="two genuine halves of £100")
    assert d["status"] == "applied"
    assert key not in {i["key"] for i in api.get(f"{API}/read/issues").json()["data"]["issues"]}
    assert q1(app, "SELECT count(*) FROM transfers WHERE voided_at IS NULL AND id IN (?, ?)", a, b) == 2
    with app.state.db.tx() as conn:                           # the facts change: it comes back, and that is recorded
        services.retime_transfer(conn, b, at(-29.95), "corrected time")
        assert issues.sync_dismissals(conn) == 1
    assert key in {i["key"] for i in api.get(f"{API}/read/issues").json()["data"]["issues"]}
    assert q1(app, "SELECT count(*) FROM audit_log WHERE entity = 'issue' AND action IN ('dismissed', 'reopened')") == 2


def test_reviewed_notes_suppress_duplicate_transfer_warning(app, book):
    with app.state.db.tx() as conn:
        a = services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=5000, at=at(-30))
        b = services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=5000, at=at(-29.9),
                                     notes=f"Reviewed: genuine separate transfer; not a duplicate of #{0}")
        conn.execute("UPDATE transfers SET notes = ? WHERE id = ?", (f"Reviewed: not a duplicate of #{a}", b))
    with app.state.db.read() as conn:
        assert f"transfer:double:{b}" not in {i["key"] for i in issues.collect(conn)}


# ================================================================ pages

def test_operations_and_plan_pages(app, book, api, browser):
    pid = snr_plan(api)
    change(api, "validate_bet_plan", "pg-v", plan_id=pid)
    ops_page = browser.get("/bets/operations").text
    assert "Needs me" in ops_page and f"Place plan #{pid}" in ops_page and "Completed today" in ops_page
    plans_page = browser.get("/bets/plans").text
    assert f"#{pid}" in plans_page and "/bets/ops/create_bet_plan" in plans_page
    detail = browser.get(f"/bets/plans/{pid}").text
    assert "SAFE TO PLACE" in detail and "Record placement" in detail and "Price history" not in detail
    assert f'name="csrf_token" value="{browser.csrf}"' in detail
    r = browser.post("/bets/ops/record_bookmaker_placement", {"plan_id": str(pid), "stake": "10", "odds": "6.5",
                                                               "next": f"/plans/{pid}"})
    assert "ok=" in r.headers["location"]
    assert "Operations" in browser.get("/bets/").text


# ================================================================ alert state: no noisy duplicate reminders

def test_alerts_are_not_repeated_until_they_escalate(app, book, api):
    with app.state.db.tx() as conn:
        services.update_free_bet(conn, book["token"], {"expires_at": at(60)})
    brief0 = api.get(f"{API}/read/operating_brief").json()["data"]
    alert = next(a for a in brief0["alerts_to_send"] if a["alert_key"] == f"free_bet_expiry:free_bet #{book['token']}")
    assert alert["priority"] == "high"
    code, r = change(api, "mark_alerts_notified", "al-1", alert_keys=[alert["alert_key"]])
    assert r["status"] == "applied" and r["result"]["notified"] == [alert["alert_key"]]
    assert change(api, "mark_alerts_notified", "al-2", alert_keys=[alert["alert_key"]])[1]["status"] == "already_done"
    brief1 = api.get(f"{API}/read/operating_brief").json()["data"]
    assert alert["alert_key"] not in [a["alert_key"] for a in brief1["alerts_to_send"]]
    acts = {a["alert_key"]: a for a in api.get(f"{API}/read/next_actions").json()["data"]}
    assert acts[alert["alert_key"]]["already_notified"] is True              # still listed, just not re-sent
    with app.state.db.tx() as conn:                                          # escalates: under 24 hours left
        services.update_free_bet(conn, book["token"], {"expires_at": at(10)})
    brief2 = api.get(f"{API}/read/operating_brief").json()["data"]
    again = next(a for a in brief2["alerts_to_send"] if a["alert_key"] == alert["alert_key"])
    assert again["priority"] == "urgent"
    code, bad = change(api, "mark_alerts_notified", "al-3", alert_keys=["nope:1"])
    assert code == 400


def test_open_positions_list_soonest_first(app, book, browser):
    with app.state.db.tx() as conn:
        for name, h in (("Later game", 72), ("No time game", None), ("Sooner game", 5)):
            services.create_position(conn, event=name, selection="Home", event_at=at(h) if h else None, legs=[
                {"account_id": book["betfred"], "side": "back", "stake_pence": 500, "odds": "2.0"}])
    page = browser.get("/bets/positions").text
    assert page.index("Sooner game") < page.index("Later game") < page.index("No time game")
