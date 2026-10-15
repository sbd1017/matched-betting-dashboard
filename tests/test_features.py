"""Features added after the v3 redesign: issues list, one-tap settle, balance checks, and later batches."""
from datetime import timedelta

from bets import offer_state, issues, metrics, services, times

from .test_web import seed


def past(hours=3):
    return times.iso(times.now() - timedelta(hours=hours))


def started_position(app, ids):
    with app.state.db.tx() as conn:
        return services.create_position(conn, event="Spurs v Chelsea", selection="Spurs", event_at=past(), legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "3.0"},
            {"account_id": ids["smarkets"], "side": "lay", "stake_pence": 1000, "odds": "3.1"}])


def test_issues_list_started_incomplete_and_negative(app, browser):
    ids = seed(app)
    pid = started_position(app, ids)
    with app.state.db.tx() as conn:
        half = services.create_position(conn, event="Leeds v Wolves", selection="Leeds", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 500, "odds": "2.0"}])
        neg = services.create_account(conn, name="Kwiff", kind="bookmaker")
        services.add_manual_entry(conn, account_id=neg, amount=-300, category="adjustment", description="test")
    with app.state.db.read() as conn:
        keys = {i["key"]: i for i in issues.collect(conn)}
    assert f"started:{pid}" in keys and keys[f"started:{pid}"]["settle"]
    assert f"incomplete:{half}" in keys and "lay" in keys[f"incomplete:{half}"]["text"]
    assert f"negative:{neg}" in keys
    page = browser.get("/bets/issues").text
    for text in ("Spurs v Chelsea", "Back won", "Leeds v Wolves", "Kwiff is below zero", "Ledger checks"):
        assert text in page, text
    assert "Issues<sup" in browser.get("/bets/").text          # nav shows the count


def test_settle_from_the_dashboard_returns_there(app, browser):
    ids = seed(app)
    pid = started_position(app, ids)
    dash = browser.get("/bets/").text
    assert f'/bets/positions/{pid}/settle' in dash and 'value="back_lost"' in dash
    r = browser.post(f"/bets/positions/{pid}/settle", {"outcome": "back_lost", "next": "/bets/"})
    assert r.status_code == 303 and r.headers["location"].startswith("/bets/?ok=Settled")
    with app.state.db.read() as conn:
        assert services.row(conn, "positions", pid)["status"] == "settled"
    # an unsafe return path falls back to the dashboard, never another site
    pid2 = started_position(app, ids)
    r = browser.post(f"/bets/positions/{pid2}/settle", {"outcome": "void", "next": "https://evil.example/"})
    assert r.headers["location"].startswith("/bets/?ok=")


def test_balance_check_matches_and_differences(app, browser):
    ids = seed(app)
    page = browser.get("/bets/accounts/check")
    assert page.status_code == 200 and "Betfred" in page.text and "Never" in page.text
    with app.state.db.read() as conn:
        betfred_before = services.balance(conn, ids["betfred"])
    r = browser.post("/bets/accounts/check", {f"match_{ids['smarkets']}": "1",
                                               f"actual_{ids['betfred']}": f"{(betfred_before - 40) / 100:.2f}"})
    assert r.status_code == 303 and "2 balances checked" in r.headers["location"].replace("+", " ")
    with app.state.db.read() as conn:
        recs = conn.execute("SELECT account_id, difference_pence FROM reconciliations ORDER BY id").fetchall()
        assert [(a, d) for a, d in recs] == [(ids["betfred"], -40), (ids["smarkets"], 0)]
        assert services.balance(conn, ids["betfred"]) == betfred_before - 40
        assert metrics.summary(conn).consistent
    nothing = browser.post("/bets/accounts/check", {})
    assert "err=" in nothing.headers["location"]


def test_stale_balances_and_waiting_rewards_are_flagged(app):
    ids = seed(app)
    with app.state.db.tx() as conn:
        conn.execute("UPDATE offers SET status = 'awaiting_reward', updated_at = ?", (past(24 * 5),))
        conn.execute("UPDATE accounts SET created_at = ?", (past(24 * 30),))   # older than the 14-day window
    with app.state.db.read() as conn:
        keys = {i["key"] for i in issues.collect(conn)}
    assert "balances:stale" in keys and any(k.startswith("reward:") for k in keys)


# ---------------------------------------------------------------- batch 2: return dates, withdrawals, still to come

from bets import imports, outlook


def future(days=3):
    return times.iso(times.now() + timedelta(days=days))


def test_temporary_funding_is_repaid_oldest_first_with_due_dates(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        t1 = services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["smarkets"], amount=10000, at=past(48),
                                      due_at=future(2))
        t2 = services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["betfred"], amount=5000, at=past(24))
        services.create_transfer(conn, from_id=ids["smarkets"], to_id=ids["lloyds"], amount=12000, at=past(1))
        dues = {d["transfer_id"]: d for d in outlook.temporary_dues(conn)}
        assert t1 not in dues and dues[t2]["remaining"] == 3000
        services.set_transfer_due(conn, t2, future(5), "agreed with myself")
        try:
            bank_out = services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["smarkets"], amount=100,
                                                capital_type="bankroll_contribution", due_at=future(1))
        except services.UserError as exc:
            assert "Only temporary funding" in str(exc)
        else:
            raise AssertionError(bank_out)
    page = browser.get("/bets/").text
    assert "Return £30.00 to Lloyds" in page and "Record return" in page
    accounts = browser.get("/bets/accounts").text
    assert "Borrowed from your bank" in accounts and "Return by" in accounts
    ics = browser.get(f"/bets/calendar/return/{t2}.ics").text
    assert "Return £30.00 to Lloyds" in ics


def test_assistant_transfer_asks_for_the_return_date(app):
    ids = seed(app)
    with app.state.db.tx() as conn:
        c, warnings = imports.normalise_kind(conn, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "50",
                                                                "capital_type": "temporary"})
        assert any("return_by not given" in w for w in warnings)
        c, warnings = imports.normalise_kind(conn, "transfer", {"from": "Lloyds", "to": "Smarkets", "amount": "50",
                                                                "capital_type": "temporary", "return_by": "2099-01-05"})
        assert c["due_at"] and not any("return_by" in w for w in warnings)
        tid = int(imports.apply_kind(conn, "transfer", c).split(":")[1])
        assert services.row(conn, "transfers", tid)["due_at"] == c["due_at"]
        try:
            imports.normalise_kind(conn, "transfer", {"from": "Smarkets", "to": "Lloyds", "amount": "5",
                                                      "return_by": "2099-01-05"})
        except imports.CandidateError as exc:
            assert "only for temporary funding" in str(exc)
        else:
            raise AssertionError("return_by on a return was accepted")


def test_withdrawal_prompt_only_when_nothing_is_left_to_do(app, browser):
    ids = seed(app)                                    # Betfred has an open offer, a free bet and an open bet
    with app.state.db.tx() as conn:
        kwiff = services.create_account(conn, name="Kwiff", kind="bookmaker", opening_balance=2200)
        assert [w["account"] for w in outlook.withdrawable(conn)] == ["Kwiff"]
    page = browser.get("/bets/").text
    assert "Withdraw £22.00 from 1 bookmaker" in page and "Not now" in page
    snooze_form = page[page.index("/bets/withdrawals/snooze"):][:400]
    assert f'name="csrf_token" value="{browser.csrf}"' in snooze_form
    r = browser.post("/bets/withdrawals/snooze", {"accounts": str(kwiff)})
    assert r.status_code == 303
    with app.state.db.read() as conn:
        assert outlook.withdrawable(conn) == []


def test_retention_and_still_to_come(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        assert outlook.retention(conn)["snr"]["own"] is False
        for i in range(3):
            fid = services.create_free_bet(conn, account_id=ids["betfred"], face_value=1000)
            pid = services.create_position(conn, event=f"Match {i}", selection="Draw", kind="free_bet_snr", legs=[
                {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0", "free_bet_id": fid},
                {"account_id": ids["smarkets"], "side": "lay", "stake_pence": 800, "odds": "5.0"}])
            services.quick_settle(conn, pid, "back_lost")
        r = outlook.retention(conn)["snr"]
        assert r["own"] and r["count"] == 3 and r["pct"] == 78        # 800 * 0.98 = 784 kept of 1000
        come = outlook.still_to_come(conn)
        assert come["free_bets"] == round(1000 * 0.784) and come["locked"] < 0     # the seeded qualifier
        assert come["total"] == come["locked"] + come["free_bets"] + come["rewards"]
    page = browser.get("/bets/").text
    assert "Still to come" in page and "your 78% retention" in page


# ---------------------------------------------------------------- batch 3: offers that move themselves

def test_offer_terms_prefill_and_qualifying_checks(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Bet £5 Get £20", account_id=ids["betfred"], terms={
            "qualifying_stake_pence": 500, "min_odds": "2", "reward_count": 4, "reward_each_pence": 500,
            "reward_kind": "snr", "reward_expiry_days": 7})
        o = services.row(conn, "offers", oid)
        assert o["reward"] == "4 × £5 SNR free bets" and o["reward_value_pence"] == 2000 and o["min_odds"] == "2.00"
    page = browser.get(f"/bets/quick-add?offer_id={oid}").text
    assert 'value="5.00"' in page and "min odds 2.00" in page
    with app.state.db.read() as conn:
        c, warnings = imports.normalise(conn, {"event": "A v B", "selection": "A", "offer_id": oid, "legs": [
            {"account": "Betfred", "side": "back", "stake": "4", "odds": "1.8"},
            {"account": "Smarkets", "side": "lay", "odds": "1.85"}]}, "quick_add")
    assert any("below the offer's minimum 2.00" in w for w in warnings)
    assert any("below the offer's qualifying stake £5.00" in w for w in warnings)


def test_reward_arrives_then_offer_converts_and_completes(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Bet £10 Get £20", account_id=ids["betfred"], terms={
            "reward_count": 2, "reward_each_pence": 1000, "reward_kind": "snr", "reward_expiry_days": 7})
        offer_state.mark_qualified(conn, oid)          # the bookmaker shows it qualified: the reward is due
        assert services.row(conn, "offers", oid)["status"] == "awaiting_reward"
    dash = browser.get("/bets/").text
    assert "It arrived" in dash and f"/bets/offers/{oid}/reward" in dash
    form = dash[dash.index(f"/bets/offers/{oid}/reward"):][:300]
    assert f'name="csrf_token" value="{browser.csrf}"' in form
    r = browser.post(f"/bets/offers/{oid}/reward", {"next": "/bets/"})
    assert r.status_code == 303 and "2+free+bets+added" in r.headers["location"]
    with app.state.db.tx() as conn:
        tokens = [dict(t) for t in conn.execute("SELECT * FROM free_bets WHERE offer_id = ? ORDER BY id", (oid,))]
        assert len(tokens) == 2 and all(t["expires_at"] and t["status"] == "available" for t in tokens)
        assert services.row(conn, "offers", oid)["status"] == "reward_received"
        pids = []
        for t in tokens:
            pids.append(services.create_position(conn, event=f"Use {t['id']}", kind="free_bet_snr", legs=[
                {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0", "free_bet_id": t["id"]},
                {"account_id": ids["smarkets"], "side": "lay", "stake_pence": 800, "odds": "5.0"}]))
            assert services.row(conn, "offers", oid)["status"] == "converting"
        services.quick_settle(conn, pids[0], "back_lost")
        assert services.row(conn, "offers", oid)["status"] == "converting"        # one still open
        services.quick_settle(conn, pids[1], "back_won")
        o = services.row(conn, "offers", oid)
        assert o["status"] == "complete" and o["reward_used"] == 1


def test_cash_reward_and_several_free_bets_at_once(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="£5 cash back", account_id=ids["betfred"],
                                    terms={"reward_each_pence": 500, "reward_kind": "cash"})
        before = services.balance(conn, ids["betfred"])
        assert services.receive_reward(conn, oid) == []
        assert services.balance(conn, ids["betfred"]) == before + 500
        assert services.row(conn, "offers", oid)["status"] == "complete"
    r = browser.post("/bets/free-bets", {"account_id": ids["betfred"], "face_value": "10", "count": "3", "kind": "snr"})
    assert "3+free+bets+added" in r.headers["location"]


def test_assistant_reward_and_counted_free_bets(app):
    ids = seed(app)
    with app.state.db.tx() as conn:
        oid = services.create_offer(conn, title="Coral 4x5", account_id=ids["betfred"], terms={
            "reward_count": 4, "reward_each_pence": 500, "reward_kind": "snr", "reward_expiry_days": 7})
        c, warnings = imports.normalise_kind(conn, "reward", {"offer": "Coral 4x5"})
        assert c["count"] == 4 and c["each_pence"] == 500 and c["expires_at"] and not warnings
        imports.apply_kind(conn, "reward", c)
        assert conn.execute("SELECT count(*) FROM free_bets WHERE offer_id = ?", (oid,)).fetchone()[0] == 4
        c, _ = imports.normalise_kind(conn, "free_bet", {"bookmaker": "Betfred", "value": "10", "count": 3})
        assert imports.title("free_bet", c) == "Betfred 3 × £10.00 SNR free bets"
        imports.apply_kind(conn, "free_bet", c)
        assert conn.execute("SELECT count(*) FROM free_bets WHERE face_value_pence = 1000 AND offer_id IS NULL "
                            "AND status = 'available'").fetchone()[0] == 4      # 3 new + the seeded one
        c, _ = imports.normalise_kind(conn, "offer", {"title": "Bet 10 get 30", "bookmaker": "Betfred",
                                                      "reward_count": 3, "reward_each": "10", "reward_type": "snr",
                                                      "qualifying_stake": "10", "min_odds": "1.5"})
        new = int(imports.apply_kind(conn, "offer", c).split(":")[1])
        assert services.row(conn, "offers", new)["reward_value_pence"] == 3000


# ---------------------------------------------------------------- batch 4: search, same again, autocomplete, drawer

def test_search_finds_positions_offers_accounts_and_ledger(app, browser):
    ids = seed(app)
    page = browser.get("/bets/search?q=liverpool").text
    assert "Liverpool v Arsenal" in page and f"/bets/positions/{ids['position']}" in page
    assert "Bet £10 Get £30" in browser.get("/bets/search?q=get").text
    assert "/bets/accounts/" in browser.get("/bets/search?q=smark").text
    assert "Nothing found" in browser.get("/bets/search?q=zzzz").text
    assert browser.get("/bets/search?q=%25_%5C").status_code == 200          # wildcards are plain text


def test_same_again_autocomplete_and_calculator_drawer(app, browser, anon):
    ids = seed(app)
    pos = browser.get(f"/bets/positions/{ids['position']}").text
    assert "Same again" in pos and "event=Liverpool+v+Arsenal" in pos and f"back_account={ids['betfred']}" in pos
    qa = browser.get("/bets/quick-add?event=Liverpool+v+Arsenal").text
    assert 'list="ev-list"' in qa and '<option value="Liverpool v Arsenal">' in qa
    dash = browser.get("/bets/").text
    assert 'id="calc"' in dash and "data-calc" in dash and "/bets/search" in dash
    assert 'id="calc"' not in anon.get("/bets/login").text


# ---------------------------------------------------------------- batch 5: undo, move / split / merge

def undo_form(browser, location):
    page = browser.get(location).text
    form = page[page.index('action="/bets/undo"'):][:400]
    assert f'name="csrf_token" value="{browser.csrf}"' in form
    return form.split('name="token" value="')[1].split('"')[0]


def test_undo_a_settlement_free_bets_and_a_transfer(app, browser):
    ids = seed(app)
    pid = started_position(app, ids)
    r = browser.post(f"/bets/positions/{pid}/settle", {"outcome": "back_won", "next": "/bets/"})
    token = undo_form(browser, r.headers["location"])
    assert token == f"settle:{pid}"
    r = browser.post("/bets/undo", {"token": token})
    assert r.status_code == 303 and "Reopened+2+bets" in r.headers["location"]
    with app.state.db.read() as conn:
        assert services.row(conn, "positions", pid)["status"] == "open"
        assert metrics.summary(conn).consistent
    r = browser.post("/bets/free-bets", {"account_id": ids["betfred"], "face_value": "5", "count": "2"})
    token = undo_form(browser, r.headers["location"])
    assert "Removed+2+free+bets" in browser.post("/bets/undo", {"token": token}).headers["location"]
    r = browser.post("/bets/transfers", {"from_id": ids["lloyds"], "to_id": ids["smarkets"], "amount": "20"})
    token = undo_form(browser, r.headers["location"])
    browser.post("/bets/undo", {"token": token})
    with app.state.db.read() as conn:
        assert conn.execute("SELECT voided_at FROM transfers ORDER BY id DESC").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM free_bets WHERE face_value_pence = 500 AND deleted_at IS NULL").fetchone()[0] == 0
    assert "err=" in browser.post("/bets/undo", {"token": "settle:99999"}).headers["location"]
    assert "err=" in browser.post("/bets/undo", {"token": "drop table"}).headers["location"]


def test_undo_refused_when_too_late_or_changed(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        fid = services.create_free_bet(conn, account_id=ids["betfred"], face_value=700)
        services.create_position(conn, event="X", kind="free_bet_snr", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 700, "odds": "4", "free_bet_id": fid}])
    r = browser.post("/bets/undo", {"token": f"freebets:{fid}"})
    assert "err=" in r.headers["location"]                               # already used: refused


def test_move_split_and_merge_keep_the_ledger_consistent(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        a = services.create_position(conn, event="Leeds v Wolves", selection="Leeds", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "2.0"}])
        b = services.create_position(conn, event="Leeds v Wolves", selection="Leeds", legs=[
            {"account_id": ids["smarkets"], "side": "lay", "stake_pence": 1000, "odds": "2.05"}])
        lay = conn.execute("SELECT id FROM legs WHERE position_id = ?", (b,)).fetchone()[0]
    page = browser.get(f"/bets/positions/{b}").text
    assert "Move to another position" in page and "Merge into another position" in page
    r = browser.post(f"/bets/legs/{lay}/move", {"target": str(a), "note": "lay on the wrong record"})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/bets/positions/{a}")
    with app.state.db.tx() as conn:
        assert conn.execute("SELECT position_id FROM legs WHERE id = ?", (lay,)).fetchone()[0] == a
        services.quick_settle(conn, a, "back_lost")
        assert services.position_pnl(conn, a) == -1000 + 980          # back stake lost, lay won less 2%
        new = services.split_leg(conn, lay, "split test")
        assert services.row(conn, "positions", new)["event"] == "Leeds v Wolves"
        assert services.position_pnl(conn, new) == 980 and services.position_pnl(conn, a) == -1000
        services.merge_positions(conn, new, a, "back together")
        assert services.position_pnl(conn, a) == -20 and services.row(conn, "positions", new)["deleted_at"]
        assert metrics.summary(conn).consistent


# ---------------------------------------------------------------- batch 6: standing, transit, calendar, help

def test_withdrawal_in_transit_then_arrived(app, browser):
    ids = seed(app)
    r = browser.post("/bets/transfers", {"from_id": ids["smarkets"], "to_id": ids["lloyds"], "amount": "15",
                                          "in_transit": "1"})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    dash = browser.get("/bets/").text
    assert "On its way" in dash and "£15.00 from Smarkets to Lloyds" in dash
    with app.state.db.read() as conn:
        tid = conn.execute("SELECT id FROM transfers WHERE in_transit = 1").fetchone()[0]
    form = dash[dash.index(f"/bets/transfers/{tid}/arrived"):][:300]
    assert f'name="csrf_token" value="{browser.csrf}"' in form
    browser.post(f"/bets/transfers/{tid}/arrived", {"next": "/bets/"})
    with app.state.db.read() as conn:
        t = services.row(conn, "transfers", tid)
        assert t["in_transit"] == 0 and t["arrived_at"]
    with app.state.db.tx() as conn:                     # the assistant can report the arrival too
        tid2 = services.create_transfer(conn, from_id=ids["betfred"], to_id=ids["lloyds"], amount=700, in_transit=True)
        c, _ = imports.normalise_kind(conn, "arrival", {"from": "Betfred", "amount": "7"})
        assert c["transfer_id"] == tid2
        imports.apply_kind(conn, "arrival", c)
        assert services.row(conn, "transfers", tid2)["in_transit"] == 0


def test_account_standing_and_warnings(app, browser):
    ids = seed(app)
    r = browser.post(f"/bets/accounts/{ids['betfred']}", {"name": "Betfred", "standing": "restricted", "active": "1",
                                                          "verified_shown": "1", "verified": "1"})
    assert r.status_code == 303
    page = browser.get(f"/bets/accounts/{ids['betfred']}").text
    assert "Restricted" in page and "Verified" in page
    assert "Restricted" in browser.get("/bets/accounts").text
    with app.state.db.tx() as conn:
        c, warnings = imports.normalise(conn, {"event": "X v Y", "legs": [
            {"account": "Betfred", "side": "back", "stake": "10", "odds": "2"}]}, "quick_add")
        assert any("marked restricted" in w for w in warnings)
        c, _ = imports.normalise_kind(conn, "account_status", {"account": "Betfred", "standing": "fine"})
        imports.apply_kind(conn, "account_status", c)
        assert services.row(conn, "accounts", ids["betfred"])["standing"] == "ok"
        services.create_offer(conn, title="Sign up", account_id=ids["betfred"], offer_type="sign_up")
        _, warnings = imports.normalise_kind(conn, "offer", {"title": "Another", "bookmaker": "Betfred",
                                                             "offer_type": "sign_up"})
        assert any("once only" in w for w in warnings)


def test_calendar_help_and_explainer(app, browser):
    ids = seed(app)
    cal = browser.get("/bets/calendar").text
    assert "Calendar" in cal and "Add all to your calendar" in cal
    assert "Retention" in browser.get("/bets/help").text
    dash = browser.get("/bets/").text
    assert "How these figures are worked out" in dash and "/bets/calendar" in dash


# ---------------------------------------------------------------- batch 7: rebuilt pages

def test_rebuilt_pages_show_their_key_figures(app, browser):
    ids = seed(app)
    pid = started_position(app, ids)
    pos = browser.get("/bets/positions").text
    assert "To settle" in pos and "Lay liability held" in pos and f"/bets/positions/{pid}/settle" in pos
    one = browser.get(f"/bets/positions/{ids['position']}").text
    assert "If the back wins" in one and "Back selection won" in one and 'class="odd back"' in one
    offers = browser.get("/bets/offers").text
    assert "Finished offers" in offers and "In progress" in offers and "Add an offer" in offers
    fb = browser.get("/bets/free-bets").text
    assert "Worth to you" in fb and "Expiring in 3 days" in fb
    acc = browser.get("/bets/accounts").text
    assert "Check balances" in acc and "Last checked" in acc and "Never" in acc


def test_profit_page_shows_efficiency(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        services.quick_settle(conn, ids["position"], "back_lost")
        fid = services.create_free_bet(conn, account_id=ids["betfred"], face_value=500, expires_at=past(1))
        services.expire_overdue(conn)
        e = outlook.efficiency(conn)
    assert e["ql_count"] == 1 and e["ql_pct"] == 6.2 and e["expired_value"] == 500
    page = browser.get("/bets/profit").text
    assert "How well it’s going" in page and "6.2%" in page and "expired unused" in page


# ---------------------------------------------------------------- Telegram summary and alerts

def test_morning_summary_and_alerts_are_sent_once(app, settings, monkeypatch):
    from bets import digest
    ids = seed(app)
    pid = started_position(app, ids)
    with app.state.db.tx() as conn:
        conn.execute("UPDATE positions SET event_at = ? WHERE id = ?", (past(5), pid))
        services.create_free_bet(conn, account_id=ids["betfred"], face_value=1500, expires_at=times.iso(times.now() + timedelta(hours=2)))
        services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["smarkets"], amount=4000,
                                 due_at=times.iso(times.now() + timedelta(hours=10)))
    with app.state.db.read() as conn:
        text = digest.morning(conn, settings)
        found = digest.alerts(conn, settings)
    assert "Use Betfred £15.00 free bet" in text and "Settle Spurs v Chelsea" in text and "Return £40.00 to Lloyds" in text
    kinds = sorted(k.split(":")[0] for k, _ in found)
    assert kinds == ["due", "expiry", "started"]
    sent = []
    monkeypatch.setattr(digest, "send", lambda s, t: sent.append(t))
    assert digest.load_config(settings) is None
    digest.save_config(settings, "123:abc", "42")
    assert digest.load_config(settings) == {"bot_token": "123:abc", "chat_id": "42"}
    assert oct(digest.config_path(settings).stat().st_mode)[-3:] == "600"
    with app.state.db.tx() as conn:
        digest.mark_sent(conn, [k for k, _ in found])
    with app.state.db.read() as conn:
        assert digest.alerts(conn, settings) == []                       # not sent twice


# ---------------------------------------------------------------- corrections by an assistant: amend / reverse

def test_amend_candidates_change_records_through_the_services(app):
    ids = seed(app)
    with app.state.db.tx() as conn:
        c, _ = imports.normalise_kind(conn, "amend", {"target": "position", "ref": "Liverpool v Arsenal",
                                                      "selection": "Arsenal", "reason": "backed the other team"})
        assert c["changes"] == {"selection": "Arsenal"} and c["before"] == {"selection": "Liverpool"}
        imports.apply_kind(conn, "amend", c)
        assert services.row(conn, "positions", ids["position"])["selection"] == "Arsenal"
        # a bet: stored, then confirmed later (re-resolved by position + side)
        iid, c, _ = imports.store(conn, {"target": "leg", "ref": str(ids["position"]), "side": "lay", "odds": "4.3",
                                         "reason": "typo in the lay odds"}, "assistant", kind="amend")
        assert imports.auto_confirm_hold(conn, "amend", c, []) == "corrections and undos always wait for you"
        imports.decide(conn, iid, True)
        lay = conn.execute("SELECT odds FROM legs WHERE position_id = ? AND side = 'lay'", (ids["position"],)).fetchone()
        assert lay[0] == "4.30"
        # move the lay into a new position
        c, _ = imports.normalise_kind(conn, "amend", {"target": "leg", "ref": str(ids["position"]), "side": "lay",
                                                      "move_to": "new", "reason": "separate record"})
        ref = imports.apply_kind(conn, "amend", c)
        assert ref.startswith("position:") and int(ref.split(":")[1]) != ids["position"]
        tid = services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["smarkets"], amount=2500)
        c, _ = imports.normalise_kind(conn, "amend", {"target": "transfer", "ref": str(tid), "return_by": "2099-02-01",
                                                      "reason": "agreed date"})
        imports.apply_kind(conn, "amend", c)
        assert services.row(conn, "transfers", tid)["due_at"]
        try:
            imports.normalise_kind(conn, "amend", {"target": "position", "ref": "No such match", "event": "X",
                                                   "reason": "x"})
        except imports.CandidateError as exc:
            assert "no position matches" in str(exc)
        else:
            raise AssertionError("unknown position accepted")
        assert metrics.summary(conn).consistent


def test_reverse_candidates_undo_without_deleting(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        tid = services.create_transfer(conn, from_id=ids["lloyds"], to_id=ids["smarkets"], amount=2500)
        c, _ = imports.normalise_kind(conn, "reverse", {"target": "transfer", "amount": "25", "account": "Lloyds",
                                                        "reason": "entered twice"})
        assert c["target_id"] == tid
        imports.apply_kind(conn, "reverse", c)
        assert services.row(conn, "transfers", tid)["voided_at"]
        services.quick_settle(conn, ids["position"], "back_won")
        c, _ = imports.normalise_kind(conn, "reverse", {"target": "settlement", "ref": str(ids["position"]),
                                                        "reason": "wrong result"})
        imports.apply_kind(conn, "reverse", c)
        assert services.row(conn, "positions", ids["position"])["status"] == "open"
        c, _ = imports.normalise_kind(conn, "reverse", {"target": "free_bet", "account": "Betfred", "value": "10",
                                                        "reason": "never credited"})
        imports.apply_kind(conn, "reverse", c)
        assert services.row(conn, "free_bets", ids["free_bet"])["deleted_at"]
        iid, c, _ = imports.store(conn, {"target": "leg", "ref": "Liverpool v Arsenal", "side": "lay",
                                         "reason": "lay belongs elsewhere"}, "assistant", kind="reverse")
        assert metrics.summary(conn).consistent
    page = browser.get(f"/bets/imports/{iid}").text
    assert "Undo" in page and "Removes the bet" in page and "lay belongs elsewhere" in page
    settings_page = browser.get("/bets/settings").text
    assert 'value="amend"' not in settings_page and 'value="reverse"' not in settings_page


def test_every_candidate_kind_can_be_stored(app):
    """The database accepts every kind the importer knows (the CHECK constraint is kept in step)."""
    seed(app)
    with app.state.db.tx() as conn:
        for kind in imports.KINDS:
            conn.execute("INSERT INTO import_candidates (source, kind, raw_json, candidate_json, created_at) "
                         "VALUES ('api', ?, '{}', '{}', '2026-01-01T00:00:00Z')", (kind,))
            conn.execute("DELETE FROM import_candidates WHERE kind = ? AND raw_json = '{}'", (kind,))


# ---------------------------------------------------------------- profit withdrawals from an assistant

def test_profit_withdrawal_candidate_is_capital_and_limited_to_retained_profit(app):
    ids = seed(app)
    with app.state.db.tx() as conn:
        services.quick_settle(conn, ids["position"], "back_won")              # realised profit -£0.62
        pid = services.create_position(conn, event="Free win", kind="free_bet_snr", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 1000, "odds": "4", "free_bet_id": ids["free_bet"]},
            {"account_id": ids["smarkets"], "side": "lay", "stake_pence": 700, "odds": "4.2"}])
        services.quick_settle(conn, pid, "back_lost")                         # + lay winnings
        before = metrics.summary(conn)
        c, warnings = imports.normalise_kind(conn, "transfer", {"from": "Smarkets", "to": "Lloyds", "amount": "2",
                                                                "capital_type": "winnings"})
        assert c["capital_type"] == "profit_withdrawal"
        assert imports.auto_confirm_hold(conn, "transfer", c, warnings) == "profit withdrawals always wait for you"
        imports.apply_kind(conn, "transfer", c)
        after = metrics.summary(conn)
        assert after.profit_lifetime == before.profit_lifetime                 # never new profit
        assert after.profit_withdrawn == before.profit_withdrawn + 200 and after.consistent
        c, _ = imports.normalise_kind(conn, "transfer", {"from": "Smarkets", "to": "Lloyds", "amount": "500",
                                                         "capital_type": "profit"})
        try:
            imports.apply_kind(conn, "transfer", c)
        except services.UserError as exc:
            assert "at most the retained profit" in str(exc)
        else:
            raise AssertionError("withdrew more than the retained profit")
        c, _ = imports.normalise_kind(conn, "transfer", {"from": "Smarkets", "to": "Lloyds", "amount": "2",
                                                         "capital_type": "permanent"})
        assert c["capital_type"] == "bankroll_withdrawal"



def test_new_accounts_are_not_flagged_as_unchecked(app):
    seed(app)                                             # accounts created just now: nothing to check yet
    with app.state.db.read() as conn:
        assert "balances:stale" not in {i["key"] for i in issues.collect(conn)}
