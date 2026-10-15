"""Maintenance: reading records, named changes (bets/ops.py) with dependency checks, permissions, idempotency and an
audit trail; the issues engine's repairs; balance-check safety; and the regression cases A-D from real use."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from bets import issues, metrics, ops, outlook, readapi, services, times

from .test_assistant import token  # noqa: F401 (fixture)

API = "/bets/api/assistant"


@pytest.fixture
def api(app, token):  # noqa: F811
    with TestClient(app, base_url="http://testserver") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def past(hours: float) -> str:
    return times.iso(times.now() - timedelta(hours=hours))


def future(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


def q1(app, sql, *args):
    with app.state.db.read() as conn:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r else None


def change(api, name, request_id="r1", **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": request_id})
    return r.status_code, r.json()


def profit(app) -> int:
    with app.state.db.read() as conn:
        return metrics.summary(conn).profit_lifetime


def issue_keys(app) -> set:
    with app.state.db.read() as conn:
        return {i["key"] for i in issues.collect(conn)}


@pytest.fixture
def book(app):
    """Coral, Betfred, Smarkets, First Direct and Lloyds, as in the user's records."""
    with app.state.db.tx() as conn:
        ids = {"coral": services.create_account(conn, name="Coral", kind="bookmaker"),
               "betfred": services.create_account(conn, name="Betfred", kind="bookmaker"),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange", commission_bp=0),
               "fd": services.create_account(conn, name="First Direct", kind="bank"),
               "lloyds": services.create_account(conn, name="Lloyds", kind="bank")}
        services.create_transfer(conn, from_id=ids["fd"], to_id=ids["smarkets"], amount=10000, at=past(24 * 20),
                                 capital_type="bankroll_contribution")
        ids["offer"] = services.create_offer(conn, title="Bet £5 Get £30", account_id=ids["coral"])
    return ids


def coral_duplicate(app, book):
    """Regression A as it was: six welcome tokens, five linked; the sixth left 'used' without a bet, while a second
    record of it (no expiry, no offer) is what position #16-like actually used."""
    with app.state.db.tx() as conn:
        toks = services.create_free_bets(conn, count=6, account_id=book["coral"], face_value=500, kind="snr",
                                         offer_id=book["offer"], expires_at="2026-10-14T10:49:00Z")
        for i, t in enumerate(toks[:5]):
            pid = services.create_position(conn, event=f"Welcome game {i}", selection="Draw", kind="free_bet_snr",
                                           event_at=past(30), legs=[
                {"account_id": book["coral"], "side": "back", "stake_pence": 500, "odds": "5.0", "free_bet_id": t},
                {"account_id": book["smarkets"], "side": "lay", "stake_pence": 380, "odds": "5.2"}])
            services.quick_settle(conn, pid, "back_lost")
        orphan = toks[5]
        services.update_free_bet(conn, orphan, {"status": "used"}, note="marked used by hand")
        dup = services.create_free_bet(conn, account_id=book["coral"], face_value=500, kind="snr",
                                       notes="Horse Racing Bet Builder free bet used on 17:00 Newcastle")
        p16 = services.create_position(conn, event="17:00 Newcastle", selection="Bet Builder", kind="free_bet_snr",
                                       legs=[{"account_id": book["coral"], "side": "back", "stake_pence": 500,
                                              "odds": "5.60", "free_bet_id": dup}])
        services.quick_settle(conn, p16, "back_lost")
    return {"orphan": orphan, "dup": dup, "p16": p16, "tokens": toks}


# ================================================================ regression A: Coral duplicate, fixed by the assistant

def test_regression_a_coral_duplicate_repaired_through_the_api(app, book, api):
    case = coral_duplicate(app, book)
    keys = issue_keys(app)
    assert f"freebet:unlinked:{case['orphan']}" in keys
    found = api.get(f"{API}/read/issues").json()["data"]
    issue = next(i for i in found["issues"] if i["key"] == f"freebet:unlinked:{case['orphan']}")
    merge = next(r for r in issue["repairs"] if r["change"] == "merge_duplicate_free_bets")
    assert merge["params"] == {"keep_free_bet_id": case["orphan"], "duplicate_free_bet_id": case["dup"],
                               "reason": merge["params"]["reason"]} and merge["level"] == "safe_edit"
    profit_before = profit(app)
    code, body = change(api, "merge_duplicate_free_bets", "coral-dup-1", **merge["params"])
    assert code == 200 and body["status"] == "applied", body
    assert body["totals"]["issues"]["before"] >= 1 and f"freebet:unlinked:{case['orphan']}" not in issue_keys(app)
    with app.state.db.read() as conn:
        kept = readapi.free_bet(conn, case["orphan"])
        dup = readapi.free_bet(conn, case["dup"])
        live = conn.execute("SELECT count(*) FROM free_bets WHERE account_id = ? AND status <> 'void' AND deleted_at IS NULL",
                            (book["coral"],)).fetchone()[0]
    assert kept["status"] == "used" and kept["position"]["id"] == case["p16"] and kept["offer"]["id"] == book["offer"]
    assert kept["expires_at"] == "2026-10-14 11:49" and "Horse Racing Bet Builder" in kept["notes"]
    assert dup["status"] == "void" and dup["position"] is None
    assert live == 6                                       # one record per real welcome token
    assert profit(app) == profit_before                    # relinking a duplicate moves no money
    before = {r["record"]: r["before"]["status"] for r in body["records"] if r["record"].startswith("free_bet")}
    assert before == {f"free_bet #{case['orphan']}": "used", f"free_bet #{case['dup']}": "used"}


# ================================================================ regression B: Smarkets available balance, no fake loss

def smarkets_open_lay(app, book):
    with app.state.db.tx() as conn:
        services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=2500, at=past(48),
                                 capital_type="bankroll_contribution")
        services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=5000, at=past(47),
                                 capital_type="bankroll_contribution")
        services.create_transfer(conn, from_id=book["lloyds"], to_id=book["smarkets"], amount=7500, at=past(46),
                                 capital_type="temporary_funding")
        fb = services.create_free_bet(conn, account_id=book["coral"], face_value=1000, kind="snr")
        pid = services.create_position(conn, event="Tottenham v Coventry City", selection="Coventry City",
                                       kind="free_bet_snr", event_at=future(24 * 5), legs=[
            {"account_id": book["coral"], "side": "back", "stake_pence": 1000, "odds": "6.50", "free_bet_id": fb},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 809, "odds": "6.80"}])
    return pid


def test_regression_b_temporary_return_and_available_balance_create_no_profit(app, book, api):
    smarkets_open_lay(app, book)
    p0 = profit(app)
    with app.state.db.tx() as conn:                        # "I withdrew £75 from Smarkets back to Lloyds"
        services.create_transfer(conn, from_id=book["smarkets"], to_id=book["lloyds"], amount=7500)
    with app.state.db.read() as conn:
        s = metrics.summary(conn)
        exp = services.expected_balance(conn, book["smarkets"], "available")
    assert s.temporary_outstanding == 0 and s.profit_lifetime == p0
    assert exp["liability"] == 4692                         # 8.09 at 6.80: £46.92 reserved
    observed = exp["available"]
    # the assistant must say which figure; it is told the liability
    code, body = change(api, "record_balance", "bal-1", account="Smarkets", observed=f"{observed / 100:.2f}")
    assert code == 400 and "available" in body["errors"][0] and "46.92" in body["errors"][0]
    code, body = change(api, "record_balance", "bal-2", account="Smarkets", observed=f"{observed / 100:.2f}",
                        figure="available")
    assert code == 200 and body["status"] == "applied" and "matches" in body["summary"]
    # the same figure recorded as the TOTAL would be £46.92 short: it is recorded as unresolved, never as profit
    code, body = change(api, "record_balance", "bal-3", account="Smarkets", observed=f"{observed / 100:.2f}",
                        figure="total")
    assert code == 200 and "difference −£46.92" in body["summary"]
    assert profit(app) == p0
    rid = q1(app, "SELECT max(id) FROM reconciliations")
    assert q1(app, "SELECT outcome FROM reconciliations WHERE id = ?", rid) == "unresolved"
    assert f"reconciliation:unresolved:{rid}" in issue_keys(app)
    code, body = change(api, "resolve_reconciliation", "bal-4", reconciliation_id=rid, action="dismiss",
                        reason="I read the available balance")
    assert body["status"] == "applied" and profit(app) == p0 and f"reconciliation:unresolved:{rid}" not in issue_keys(app)


def test_regression_b_wrong_reconciliation_reversal_restores_profit(app, book, api):
    smarkets_open_lay(app, book)
    p0 = profit(app)
    with app.state.db.tx() as conn:                        # the old behaviour: available read as total, -£46.92 "loss"
        exp = services.expected_balance(conn, book["smarkets"], "available")
        services.reconcile(conn, book["smarkets"], exp["available"], note="balance after withdrawal")
    rid = q1(app, "SELECT max(id) FROM reconciliations")
    assert profit(app) == p0 - 4692
    assert f"reconciliation:liability:{rid}" in issue_keys(app)     # flagged as "included open liability"
    code, body = change(api, "reverse_reconciliation", "rev-1", reconciliation_id=rid)
    assert code == 200 and body["status"] == "blocked" and "reason" in body["reason"]
    code, body = change(api, "reverse_reconciliation", "rev-2", reconciliation_id=rid,
                        reason="£159.52 was the available balance")
    assert code == 202 and body["status"] == "needs_confirmation"   # financial: waits for the user by default
    assert profit(app) == p0 - 4692
    with app.state.db.tx() as conn:
        result = ops.decide(conn, body["change_id"], True)
    assert result["status"] == "applied" and profit(app) == p0
    assert result["totals"]["realised_profit"]["after"] == f"{p0 / 100:.2f}"
    assert q1(app, "SELECT outcome FROM reconciliations WHERE id = ?", rid) == "reversed"
    assert q1(app, "SELECT count(*) FROM transfers WHERE voided_at IS NULL") == 4    # transfers untouched
    assert not any(k.startswith("reconciliation:") for k in issue_keys(app))


# ================================================================ regression C: finished match settled by the assistant

def atletico(app, book):
    with app.state.db.tx() as conn:
        pid = services.create_position(conn, event="Atlético de Madrid vs Manchester United",
                                       selection="Draw / BTTS Yes / Under 4.5", kind="qualifying", event_at=past(6), legs=[
            {"account_id": book["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.08"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 564, "odds": "9.00"},
            {"account_id": book["smarkets"], "side": "lay", "stake_pence": 339, "odds": "15.00"}])
        services.create_transfer(conn, from_id=book["fd"], to_id=book["betfred"], amount=1000, at=past(30),
                                 capital_type="bankroll_contribution")
        refund = services.create_free_bet(conn, account_id=book["betfred"], face_value=1000, kind="snr",
                                          expires_at=future(48), notes="Bet Builder Money Back")
    return pid, refund


def test_regression_c_finished_position_settled_once_and_leaves_projections(app, book, api):
    pid, refund = atletico(app, book)
    with app.state.db.read() as conn:
        b = outlook.still_to_come_breakdown(conn)
    assert [i["position_id"] for i in b["not_counted_finished"]] == [pid] and not b["positions"]   # not distorting it
    assert f"started:{pid}" in issue_keys(app) and f"projection:{pid}" in issue_keys(app)
    p0 = profit(app)
    pos = api.get(f"{API}/read/position", params={"ref": "Atlético de Madrid vs Manchester United"}).json()["data"]
    assert pos["id"] == pid and pos["event_started"] and pos["projection"]["warning"]
    code, body = change(api, "settle_position", "atl-1", position_id=pid, outcome="back_lost")
    assert code == 200 and body["status"] == "applied"
    assert profit(app) - p0 == -1000 + 564 + 339 == -97                      # net -£0.97 realised
    code, again = change(api, "settle_position", "atl-1", position_id=pid, outcome="back_lost")
    assert again["status"] == "applied" and again.get("duplicate")           # retry: same answer, not applied twice
    code, again = change(api, "settle_position", "atl-2", position_id=pid, outcome="back_lost")
    assert again["status"] == "already_done"
    assert profit(app) - p0 == -97
    assert q1(app, "SELECT count(*) FROM ledger_entries WHERE position_id = ? AND superseded_at IS NULL "
                   "AND category IN ('lay_win', 'lay_loss', 'winnings')", pid) == 2
    code, other = change(api, "settle_position", "atl-3", position_id=pid, outcome="back_won")
    assert other["status"] == "blocked" and "resettle" in other["reason"]
    with app.state.db.read() as conn:
        b = outlook.still_to_come_breakdown(conn)
        assert services.row(conn, "free_bets", refund)["status"] == "available"
    assert not b["not_counted_finished"] and [f["free_bet_id"] for f in b["free_bet_items"]] == [refund]
    assert f"started:{pid}" not in issue_keys(app)


# ================================================================ regression D: live tokens are never consumed by repairs

def test_regression_d_available_tokens_survive_repairs(app, book, api):
    case = coral_duplicate(app, book)
    with app.state.db.tx() as conn:
        bb5 = services.create_free_bet(conn, account_id=book["betfred"], face_value=500, kind="snr",
                                       expires_at=future(30), notes="Bet Builder only")
        bb10 = services.create_free_bet(conn, account_id=book["betfred"], face_value=1000, kind="snr",
                                        expires_at=future(54), notes="Bet Builder money back")
    for i, item in enumerate(api.get(f"{API}/read/issues").json()["data"]["issues"]):
        for r in item["repairs"]:
            if r["level"] == "safe_edit" and not r["blocked"] and r["change"] != "void_free_bet":
                change(api, r["change"], f"auto-{i}-{r['change']}", **r["params"])
    with app.state.db.read() as conn:
        assert [services.row(conn, "free_bets", f)["status"] for f in (bb5, bb10)] == ["available", "available"]
        assert metrics.summary(conn).free_bets_count == 2
    code, body = change(api, "link_free_bet_to_position", "d-1", free_bet_id=bb5, position_id=case["p16"])
    assert body["status"] != "applied"                     # different bookmaker: no bet to link it to


# ================================================================ free bets: edit, link, unlink, void, delete

def test_free_bet_changes_and_dependency_protection(app, book, api):
    case = coral_duplicate(app, book)
    linked = case["tokens"][0]
    code, body = change(api, "delete_free_bet", "fb-1", free_bet_id=linked, reason="x")
    assert body["status"] == "blocked" and body["dependencies"] and "linked" in body["reason"]
    code, body = change(api, "void_free_bet", "fb-2", free_bet_id=linked)
    assert body["status"] == "blocked"
    code, body = change(api, "update_free_bet", "fb-3", free_bet_id=linked, status="available")
    assert body["status"] == "blocked"
    code, body = change(api, "update_free_bet", "fb-4", free_bet_id=case["orphan"], status="used")
    assert body["status"] == "blocked" and "link_free_bet_to_position" in body["reason"]
    code, body = change(api, "update_free_bet", "fb-5", free_bet_id=linked, append_note="checked", expires_at="2026-10-20")
    assert body["status"] == "applied" and q1(app, "SELECT expires_at FROM free_bets WHERE id = ?", linked) == "2026-10-20T22:59:00Z"
    # an unreferenced extra record: void (safe) or delete (always asks)
    with app.state.db.tx() as conn:
        spare = services.create_free_bet(conn, account_id=book["coral"], face_value=500, kind="snr", notes="typo")
    code, body = change(api, "delete_free_bet", "fb-6", free_bet_id=spare, reason="entered by mistake")
    assert code == 202 and body["status"] == "needs_confirmation" and body["confirmation_url"].endswith(body["change_id"])
    assert q1(app, "SELECT deleted_at FROM free_bets WHERE id = ?", spare) is None
    with app.state.db.tx() as conn:
        ops.decide(conn, body["change_id"], True)
    assert q1(app, "SELECT deleted_at FROM free_bets WHERE id = ?", spare) is not None
    # relink: swap the duplicate for the real token directly (no money moves)
    p0 = profit(app)
    code, body = change(api, "link_free_bet_to_position", "fb-7", free_bet_id=case["orphan"], position_id=case["p16"])
    assert body["status"] == "applied" and body["level"] == "safe_edit" and profit(app) == p0
    assert q1(app, "SELECT status FROM free_bets WHERE id = ?", case["dup"]) == "void"
    code, body = change(api, "link_free_bet_to_position", "fb-8", free_bet_id=case["orphan"], position_id=case["p16"])
    assert body["status"] == "already_done"
    # unlink: the bet becomes a cash bet, so it is financial and waits
    code, body = change(api, "unlink_free_bet_from_position", "fb-9", free_bet_id=case["orphan"], reason="test")
    assert body["status"] == "needs_confirmation" and body["level"] == "financial_reverse"
    assert any("cash stake" in e for e in body["effects"])


def test_free_bet_page_links_used_tokens_and_offers_delete(app, book, browser):
    case = coral_duplicate(app, book)
    page = browser.get("/bets/free-bets?show=all").text
    assert f'/bets/ops/delete_free_bet?free_bet_id={case["orphan"]}' in page and "Used in position" in page
    r = browser.post(f"/bets/free-bets/{case['orphan']}", {"status": "used"})
    assert "err=" in r.headers["location"] and "position" in r.headers["location"]
    r = browser.post(f"/bets/free-bets/{case['orphan']}", {"status": "used", "position_id": str(case["p16"])})
    assert "ok=" in r.headers["location"], r.headers["location"]
    assert q1(app, "SELECT free_bet_id FROM legs WHERE position_id = ?", case["p16"]) == case["orphan"]
    preview = browser.get(f"/bets/ops/delete_free_bet?free_bet_id={case['dup']}&next=/free-bets").text
    assert "Delete free bet" in preview and f'name="csrf_token" value="{browser.csrf}"' in preview
    r = browser.post("/bets/ops/delete_free_bet", {"free_bet_id": str(case["dup"]), "reason": "duplicate", "next": "/free-bets"})
    assert "ok=" in r.headers["location"] and q1(app, "SELECT deleted_at FROM free_bets WHERE id = ?", case["dup"])
    blocked = browser.get(f"/bets/ops/delete_free_bet?free_bet_id={case['orphan']}").text
    assert "Can’t do this" in blocked and "Delete free bet</button>" not in blocked
    assert q1(app, "SELECT actor FROM audit_log WHERE entity = 'free_bet' AND entity_id = ? ORDER BY id DESC LIMIT 1",
              case["dup"]) == "user"


# ================================================================ transfers

def test_reclassify_transfer_and_temporary_funding(app, book, api):
    with app.state.db.tx() as conn:
        t_in = services.create_transfer(conn, from_id=book["lloyds"], to_id=book["smarkets"], amount=7500, at=past(10))
        t_back = services.create_transfer(conn, from_id=book["smarkets"], to_id=book["lloyds"], amount=7500, at=past(2),
                                          capital_type="bankroll_withdrawal")
    p0 = profit(app)
    with app.state.db.read() as conn:
        assert metrics.summary(conn).temporary_outstanding == 7500       # no return_by needed for it to exist
    assert f"transfer:return:{t_back}" in issue_keys(app)
    code, body = change(api, "reclassify_transfer", "tr-1", transfer_id=t_back, funding_type="temporary_return",
                        reason="it paid back the Lloyds money")
    assert body["status"] == "applied" and body["totals"]["temporary_funding_outstanding"] == {"before": "75.00",
                                                                                              "after": "0.00"}
    assert profit(app) == p0 and f"transfer:return:{t_back}" not in issue_keys(app)
    # the funding was really permanent: the return's temporary part shrinks with it
    code, body = change(api, "reclassify_transfer", "tr-2", transfer_id=t_in, funding_type="bankroll_contribution")
    assert body["status"] == "applied"
    with app.state.db.read() as conn:
        assert services.row(conn, "transfers", t_back)["temp_portion_pence"] == 0
        assert metrics.summary(conn).temporary_outstanding == 0
    assert profit(app) == p0
    code, body = change(api, "reverse_transfer", "tr-3", transfer_id=t_back, reason="x")
    assert body["status"] == "needs_confirmation"
    code, body = change(api, "reclassify_transfer", "tr-4", transfer_id=t_in, funding_type="internal")
    assert code == 400


def test_transfers_listed_on_accounts_page_with_controls(app, book, browser):
    page = browser.get("/bets/accounts").text
    tid = q1(app, "SELECT max(id) FROM transfers")
    assert f"#{tid} · 100.00 First Direct → Smarkets" in page and "/bets/ops/reclassify_transfer" in page
    r = browser.post("/bets/ops/reclassify_transfer", {"transfer_id": str(tid), "funding_type": "temporary_funding",
                                                       "next": "/accounts#transfers"})
    assert "ok=" in r.headers["location"] and q1(app, "SELECT capital_type FROM transfers WHERE id = ?", tid) == "temporary_funding"


# ================================================================ positions

def test_position_reopen_void_delete_levels_and_previews(app, book, api, browser):
    pid, _ = atletico(app, book)
    change(api, "settle_position", "p-1", position_id=pid, outcome="back_lost")
    p_settled = profit(app)
    code, body = change(api, "reopen_position", "p-2", position_id=pid, reason="wrong result")
    assert body["status"] == "needs_confirmation" and any("realised profit changes by +£0.97" in e for e in body["effects"])
    page = browser.get(f"/bets/ops/reopen_position?position_id={pid}&next=/positions/{pid}").text
    assert "Before → after" in page and "Realised profit" in page and "Reopen position</button>" in page
    r = browser.post("/bets/ops/reopen_position", {"position_id": str(pid), "reason": "wrong result",
                                                   "next": f"/positions/{pid}"})
    assert "ok=" in r.headers["location"] and profit(app) == p_settled + 97
    code, body = change(api, "void_position", "p-3", position_id=pid, reason="match abandoned")
    assert body["status"] == "applied" and body["level"] == "safe_edit"
    assert q1(app, "SELECT status FROM positions WHERE id = ?", pid) == "void"
    code, body = change(api, "delete_position", "p-4", position_id=pid, reason="never placed")
    assert body["status"] == "needs_confirmation" and body["level"] == "destructive_delete"
    held = browser.get("/bets/imports").text
    assert body["change_id"] in held
    page = browser.get(f"/bets/changes/{body['change_id']}").text
    assert f'name="csrf_token" value="{browser.csrf}"' in page and "Confirm</button>" in page
    r = browser.post(f"/bets/changes/{body['change_id']}/reject")
    assert "ok=" in r.headers["location"] and q1(app, "SELECT deleted_at FROM positions WHERE id = ?", pid) is None
    code, again = change(api, "delete_position", "p-4", position_id=pid, reason="never placed")
    assert again["status"] == "rejected" and again["duplicate"]


# ================================================================ idempotency, permissions, audit

def test_request_ids_are_idempotent_and_scoped(app, book, api):
    case = coral_duplicate(app, book)
    one = change(api, "update_free_bet", "same-id", free_bet_id=case["orphan"], append_note="once")
    two = change(api, "update_free_bet", "same-id", free_bet_id=case["orphan"], append_note="once")
    assert one[1]["status"] == "applied" and two[1]["duplicate"] and two[1]["status"] == "applied"
    assert q1(app, "SELECT notes FROM free_bets WHERE id = ?", case["orphan"]).count("once") == 1
    code, body = change(api, "update_free_bet", "same-id", free_bet_id=case["orphan"], append_note="different")
    assert code == 409
    r = api.post(f"{API}/changes/update_free_bet", json={"free_bet_id": case["orphan"], "notes": "x"})
    assert r.status_code == 400 and "request_id" in r.json()["errors"][0]
    preview = api.post(f"{API}/changes/void_free_bet", json={"free_bet_id": case["dup"], "dry_run": True}).json()
    assert preview["status"] == "blocked"                     # linked: the dry run says so without changing anything


def test_permission_levels_from_settings(app, book, api, browser):
    case = coral_duplicate(app, book)
    r = browser.post("/bets/settings/assistant/permissions", {"read": "on", "safe_edit": "ask", "financial_reverse": "ask"})
    assert "ok=" in r.headers["location"]
    code, body = change(api, "update_free_bet", "perm-1", free_bet_id=case["orphan"], append_note="x")
    assert code == 202 and body["status"] == "needs_confirmation"
    browser.post("/bets/settings/assistant/permissions", {"safe_edit": "auto", "financial_reverse": "auto"})
    assert api.get(f"{API}/read/dashboard").status_code == 403          # reading switched off
    code, body = change(api, "update_free_bet", "perm-2", free_bet_id=case["orphan"], append_note="y")
    assert body["status"] == "applied"
    code, body = change(api, "delete_position", "perm-3", position_id=case["p16"], reason="x")
    assert body["status"] == "needs_confirmation"                        # deletions always ask
    with app.state.db.read() as conn:
        assert ops.permissions(conn)["destructive_delete"] == "ask"
    page = browser.get("/bets/settings").text
    assert 'id="permissions"' in page and "Deletions</b> (free bets, positions, offers) always wait" in page


def test_audit_trail_records_actor_request_and_before_after(app, book, api):
    case = coral_duplicate(app, book)
    change(api, "update_free_bet", "audit-1", free_bet_id=case["orphan"], append_note="checked")
    with app.state.db.read() as conn:
        row = conn.execute("SELECT actor, request_id, before_json, after_json FROM audit_log WHERE entity = 'free_bet' "
                           "AND entity_id = ? ORDER BY id DESC LIMIT 1", (case["orphan"],)).fetchone()
        history = readapi.history(conn, "free_bet", case["orphan"])
    assert row["actor"].startswith("assistant:") and row["request_id"].endswith("audit-1") and row["before_json"]
    assert history[0]["actor"].startswith("assistant:")
    hist = api.get(f"{API}/read/history", params={"entity": "free_bet", "id": case["orphan"]}).json()["data"]
    assert hist[0]["request_id"].endswith("audit-1")


# ================================================================ reads say exactly what each figure is

def test_reads_label_exchange_figures(app, book, api):
    smarkets_open_lay(app, book)
    acc = api.get(f"{API}/read/account", params={"ref": "Smarkets"}).json()["data"]
    b = acc["balances"]
    assert set(b) >= {"total_funds", "available_cash", "reserved_lay_liability"} and "balance" not in b
    assert float(b["available_cash"]) + float(b["reserved_lay_liability"]) == pytest.approx(float(b["total_funds"]))
    dash = api.get(f"{API}/read/dashboard").json()["data"]
    assert dash["exchange"]["reserved_lay_liability"] == "46.92" and dash["temporary_funding_outstanding"] == "75.00"
    exposure = api.get(f"{API}/read/exchange_exposure", params={"ref": "Smarkets"}).json()["data"]
    assert exposure["open_lays"][0]["liability"] == "46.92"
    stc = api.get(f"{API}/read/still_to_come").json()["data"]
    assert stc["open_positions"]["items"][0]["event"] == "Tottenham v Coventry City"
    for resource in ("positions", "free_bets", "transfers", "reconciliations", "offers", "ledger", "profit", "issues",
                     "accounts"):
        assert api.get(f"{API}/read/{resource}").status_code == 200, resource
    assert api.get(f"{API}/read/nothing").status_code == 404


# ================================================================ accounting invariants

def test_profit_invariants(app, book):
    p0 = profit(app)
    with app.state.db.tx() as conn:
        services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=2500,
                                 capital_type="bankroll_contribution")
        services.create_transfer(conn, from_id=book["fd"], to_id=book["smarkets"], amount=5000,
                                 capital_type="bankroll_contribution")
        services.create_transfer(conn, from_id=book["lloyds"], to_id=book["smarkets"], amount=7500)
        services.create_transfer(conn, from_id=book["smarkets"], to_id=book["lloyds"], amount=7500)
        services.create_free_bet(conn, account_id=book["coral"], face_value=3000, kind="snr")
        s = metrics.summary(conn)
    assert s.profit_lifetime == p0                          # deposits, temporary funding and its return, face value
    assert s.temporary_outstanding == 0
    pid = smarkets_open_lay(app, book)
    assert profit(app) == p0                                # open liability is reserved, not a loss
    with app.state.db.tx() as conn:
        services.quick_settle(conn, pid, "back_lost")
    assert profit(app) == p0 + 809                          # free bet converted only through the settled position
    with app.state.db.read() as conn:
        b = outlook.still_to_come_breakdown(conn)
    assert all(i["position_id"] != pid for i in b["positions"])           # settled: no longer projected


def test_void_duplicate_does_not_change_position_pnl(app, book, api):
    case = coral_duplicate(app, book)
    with app.state.db.read() as conn:
        before = services.position_pnl(conn, case["p16"])
    change(api, "merge_duplicate_free_bets", "m-1", keep_free_bet_id=case["orphan"], duplicate_free_bet_id=case["dup"])
    with app.state.db.read() as conn:
        assert services.position_pnl(conn, case["p16"]) == before
        assert metrics.summary(conn).free_bets_count == 0    # void tokens are not held


def test_balance_check_page_keeps_differences_out_of_profit(app, book, browser):
    smarkets_open_lay(app, book)
    p0 = profit(app)
    page = browser.get("/bets/accounts/check").text
    assert 'name="basis_' in page and "I read available" in page
    sid = book["smarkets"]
    with app.state.db.read() as conn:
        avail = services.expected_balance(conn, sid, "available")["available"]
    r = browser.post("/bets/accounts/check", {"adjust_shown": "1", f"basis_{sid}": "available",
                                               f"actual_{sid}": f"{avail / 100:.2f}"})
    assert "all matched" in r.headers["location"].replace("+", " ")
    r = browser.post("/bets/accounts/check", {"adjust_shown": "1", f"basis_{sid}": "total",
                                               f"actual_{sid}": f"{avail / 100:.2f}"})
    assert "left unresolved" in r.headers["location"].replace("+", " ") and profit(app) == p0
    acc = browser.get(f"/bets/accounts/{sid}").text
    assert "Available cash" in acc and "Lay liability reserved" in acc and "Total funds" in acc and "Dismiss…" in acc


def test_issues_page_offers_repairs(app, book, browser):
    case = coral_duplicate(app, book)
    page = browser.get("/bets/issues").text
    assert "Fix: #" in page and "/bets/ops/merge_duplicate_free_bets" in page
    r = browser.post("/bets/ops/merge_duplicate_free_bets", {"keep_free_bet_id": str(case["orphan"]),
                                                             "duplicate_free_bet_id": str(case["dup"]), "next": "/issues"})
    assert "ok=" in r.headers["location"] and f"freebet:unlinked:{case['orphan']}" not in issue_keys(app)


def test_duplicate_transfer_and_double_entry_free_bets_are_flagged_not_merged(app, book):
    with app.state.db.tx() as conn:
        a = services.create_transfer(conn, from_id=book["fd"], to_id=book["coral"], amount=1000, at=past(1),
                                     capital_type="bankroll_contribution")
        b = services.create_transfer(conn, from_id=book["fd"], to_id=book["coral"], amount=1000, at=past(0.9),
                                     capital_type="bankroll_contribution")
        f1 = services.create_free_bet(conn, account_id=book["coral"], face_value=500, notes="Acca token")
        f2 = services.create_free_bet(conn, account_id=book["coral"], face_value=500, notes="Acca token")
    keys = issue_keys(app)
    assert f"transfer:double:{b}" in keys and f"freebet:double:{f2}" in keys
    assert q1(app, "SELECT count(*) FROM transfers WHERE voided_at IS NULL AND id IN (?, ?)", a, b) == 2
    assert q1(app, "SELECT count(*) FROM free_bets WHERE status = 'available' AND id IN (?, ?)", f1, f2) == 2


def test_still_to_come_page_lists_every_item(app, book, browser):
    smarkets_open_lay(app, book)
    pid, refund = atletico(app, book)
    page = browser.get("/bets/still-to-come").text
    assert "Tottenham v Coventry City" in page and "Atlético de Madrid vs Manchester United" in page
    assert "Not counted: probably finished, still open" in page and "Betfred £10.00" in page
    dash = browser.get("/bets/").text
    assert "/bets/still-to-come" in dash and "finished position not counted until settled" in dash
