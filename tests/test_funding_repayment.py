"""Temporary funding routed through the matched-betting hub bank and repaid bank to bank (hub -> lender).
Betting accounts only deal with the hub bank; the lender is tracked separately from the payment rail."""
from datetime import timedelta

import pytest

from bets import brief, issues, metrics, readapi, services, times

from .test_assistant import API, api, token  # noqa: F401 (fixtures)


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


@pytest.fixture
def banks(app):
    with app.state.db.tx() as conn:
        ids = {"fd": services.create_account(conn, name="First Direct", kind="bank"),
               "lloyds": services.create_account(conn, name="Lloyds", kind="bank"),
               "starling": services.create_account(conn, name="Starling", kind="bank"),
               "smarkets": services.create_account(conn, name="Smarkets", kind="exchange")}
        services.create_transfer(conn, from_id=ids["fd"], to_id=ids["smarkets"], amount=10000, at=at(-200),
                                 capital_type="bankroll_contribution")
    return ids


def lend(conn, ids, *amounts, lender="lloyds"):
    out = []
    for i, a in enumerate(amounts):
        out.append(services.create_transfer(conn, from_id=ids[lender], to_id=ids["smarkets"], amount=a, at=at(-100 + i),
                                            capital_type="temporary_funding"))
    return out


def owed(conn, ids, bank="lloyds"):
    return services.outstanding_temporary(conn, ids[bank])


def change(api, name, rid, **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": rid})
    return r.status_code, r.json()


# 1
def test_hub_repayment_clears_the_lender(app, banks):
    with app.state.db.tx() as conn:
        lend(conn, banks, 10000)
        services.create_transfer(conn, from_id=banks["smarkets"], to_id=banks["fd"], amount=10000,
                                 capital_type="bankroll_withdrawal", at=at(-50))
        s0 = metrics.summary(conn)
        assert (owed(conn, banks), s0.temporary_outstanding, s0.bankroll_withdrawn) == (10000, 10000, 10000)
        services.record_funding_repayment(conn, from_id=banks["fd"], lender_id=banks["lloyds"], amount=10000)
        s1 = metrics.summary(conn)
        assert owed(conn, banks) == 0 and owed(conn, banks, "fd") == 0
        assert s1.temporary_outstanding == 0 and s1.bankroll_withdrawn == 0      # not counted as withdrawn twice
        assert s1.profit_lifetime == s0.profit_lifetime and s1.betting_balance == s0.betting_balance
        assert s1.consistent, s1.checks


# 2 and 3
def test_partial_and_multiple_fundings_allocate_oldest_first(app, banks):
    with app.state.db.tx() as conn:
        f = lend(conn, banks, 10000, 10000, 3000, 1000)
        assert owed(conn, banks) == 24000
        r1 = services.record_funding_repayment(conn, from_id=banks["fd"], lender_id=banks["lloyds"], amount=12500)
        led = services.funding_ledger(conn, banks["lloyds"])
        assert owed(conn, banks) == 11500
        assert [(x["transfer_id"], x["repaid"], x["outstanding"]) for x in led["fundings"].values()] == \
            [(f[0], 10000, 0), (f[1], 2500, 7500), (f[2], 0, 3000), (f[3], 0, 1000)]
        assert led["repayments"][r1]["allocations"] == [{"funding_transfer_id": f[0], "amount": 10000},
                                                        {"funding_transfer_id": f[1], "amount": 2500}]
        assert led["repayments"][r1]["outstanding_after"] == 11500
        services.record_funding_repayment(conn, from_id=banks["fd"], lender_id=banks["lloyds"], amount=11500)
        assert owed(conn, banks) == 0
        assert all(x["outstanding"] == 0 for x in services.funding_ledger(conn, banks["lloyds"])["fundings"].values())
        assert metrics.summary(conn).consistent


# 4
def test_only_the_named_lender_is_repaid(app, banks):
    with app.state.db.tx() as conn:
        lend(conn, banks, 10000)
        lend(conn, banks, 5000, lender="starling")
        services.record_funding_repayment(conn, from_id=banks["fd"], lender_id=banks["lloyds"], amount=10000)
        assert (owed(conn, banks), owed(conn, banks, "starling")) == (0, 5000)
        assert metrics.summary(conn).temporary_outstanding == 5000


# 5
def test_a_withdrawal_to_the_hub_never_repays_the_lender(app, banks):
    with app.state.db.tx() as conn:
        lend(conn, banks, 10000, 10000, 3000, 1000)
        for a in (20000, 6500):
            services.create_transfer(conn, from_id=banks["smarkets"], to_id=banks["fd"], amount=a,
                                     capital_type="bankroll_withdrawal")
        assert owed(conn, banks) == 24000 and owed(conn, banks, "fd") == 0
        with pytest.raises(services.UserError):           # the hub is not the lender: nothing to repay to it
            services.record_funding_repayment(conn, from_id=banks["lloyds"], lender_id=banks["fd"], amount=100)


# 6
def test_over_repayment_and_bad_input_are_refused(app, banks, api):
    with app.state.db.tx() as conn:
        lend(conn, banks, 24000)
        for kw, msg in ((dict(amount=30000), "Only £240.00"), (dict(amount=0), "more than zero"),
                        (dict(amount=100, lender_id=banks["starling"]), "No temporary funding"),
                        (dict(amount=100, from_id=banks["lloyds"]), "two different banks"),
                        (dict(amount=100, from_id=banks["smarkets"]), "one bank to another")):
            args = {"from_id": banks["fd"], "lender_id": banks["lloyds"], **kw}
            with pytest.raises(services.UserError, match=msg):
                services.record_funding_repayment(conn, **args)
        assert owed(conn, banks) == 24000
        with pytest.raises(services.UserError, match="not betting activity"):     # ordinary bank-to-bank still refused
            services.create_transfer(conn, from_id=banks["fd"], to_id=banks["lloyds"], amount=100)
    code, r = change(api, "record_funding_repayment", "over-1", from_account="First Direct", funding_source="Lloyds",
                     amount="300")
    assert r["status"] == "blocked" and "Only £240.00" in r["reason"]


# 7
def test_reversal_restores_the_debt_through_the_protected_flow(app, banks, api, browser):
    with app.state.db.tx() as conn:
        lend(conn, banks, 24000)
    code, r = change(api, "record_funding_repayment", "rep-1", from_account="First Direct", funding_source="Lloyds",
                     amount="240")
    tid = r["result"]["transfer_id"]
    with app.state.db.read() as conn:
        assert owed(conn, banks) == 0
        bal = (services.balance(conn, banks["smarkets"]), metrics.summary(conn).profit_lifetime)
    code, held = change(api, "reverse_transfer", "rev-1", transfer_id=tid, reason="entered by mistake")
    assert code == 202 and held["status"] == "needs_confirmation"                   # financial: waits for the user
    browser.post(f"/bets/changes/{held['change_id']}/confirm")
    with app.state.db.read() as conn:
        assert owed(conn, banks) == 24000
        assert (services.balance(conn, banks["smarkets"]), metrics.summary(conn).profit_lifetime) == bal
        assert metrics.summary(conn).consistent


# 8
def test_retries_and_duplicates_never_repay_twice(app, banks, api):
    with app.state.db.tx() as conn:
        lend(conn, banks, 24000)
    args = dict(from_account="First Direct", funding_source="Lloyds", amount="100")
    code, first = change(api, "record_funding_repayment", "dup-1", **args)
    assert code == 200 and first["status"] == "applied"                            # auto, like an ordinary transfer
    code, again = change(api, "record_funding_repayment", "dup-1", **args)
    assert again["duplicate"]
    code, second = change(api, "record_funding_repayment", "dup-2", **args)
    assert second["status"] == "blocked" and "allow_duplicate" in second["reason"]
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM transfers WHERE capital_type = 'funding_repayment'").fetchone()[0] == 1
        assert owed(conn, banks) == 14000
        assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'transfer' AND action = 'created' AND "
                            "after_json LIKE '%funding_repayment%'").fetchone()[0] == 1


# 9 and 10: the real case
def test_the_real_case_hub_repayment_with_25_kept_in_first_direct(app, banks, api, browser):
    with app.state.db.tx() as conn:
        early = services.create_transfer(conn, from_id=banks["lloyds"], to_id=banks["smarkets"], amount=7500, at=at(-150),
                                         capital_type="temporary_funding")
        services.create_transfer(conn, from_id=banks["smarkets"], to_id=banks["lloyds"], amount=7500, at=at(-140),
                                 capital_type="temporary_return")                    # the earlier £75, as recorded
        lend(conn, banks, 10000, 10000, 3000, 1000)
        for a in (20000, 6500):
            services.create_transfer(conn, from_id=banks["smarkets"], to_id=banks["fd"], amount=a, at=at(-5),
                                     capital_type="bankroll_withdrawal")
        s0 = metrics.summary(conn)
        lloyds0 = next(b for b in readapi.temporary_funding(conn) if b["bank"] == "Lloyds")
        assert lloyds0 == {"bank": "Lloyds", "temporary_in": "315.00", "repaid": "75.00", "outstanding": "240.00"}
        transfers0 = conn.execute("SELECT count(*) FROM transfers").fetchone()[0]
    code, r = change(api, "record_funding_repayment", "fd-lloyds-240", from_account="First Direct",
                     funding_source="Lloyds", amount="240", when=times.local(times.now_iso(), "%Y-%m-%d"))
    assert r["status"] == "applied" and r["result"]["outstanding_to_lender_after"] == "0.00"
    assert len(r["result"]["repaid_funding"]) == 4
    with app.state.db.read() as conn:
        s1 = metrics.summary(conn)
        assert s1.temporary_outstanding == 0 and s1.consistent
        assert s1.profit_lifetime == s0.profit_lifetime                              # 9: profit unchanged
        assert s1.betting_balance == s0.betting_balance                              # no betting balance moved
        assert s1.bankroll_withdrawn == s0.bankroll_withdrawn - 24000 == 2500        # the £25 kept in First Direct
        assert s1.own_capital == s0.own_capital + 24000
        lloyds = next(b for b in readapi.temporary_funding(conn) if b["bank"] == "Lloyds")
        assert lloyds == {"bank": "Lloyds", "temporary_in": "315.00", "repaid": "315.00", "outstanding": "0.00"}
        assert owed(conn, banks, "fd") == 0
        assert conn.execute("SELECT count(*) FROM transfers").fetchone()[0] == transfers0 + 1
        assert not conn.execute("SELECT 1 FROM transfers WHERE from_account_id = ? AND to_account_id = ? AND voided_at IS "
                                "NULL AND id <> ?", (banks["smarkets"], banks["lloyds"], early + 1)).fetchone()  # no fake
        assert [x["amount_pence"] for x in conn.execute("SELECT amount_pence FROM transfers WHERE from_account_id = ? AND "
                                                         "to_account_id = ?", (banks["smarkets"], banks["fd"]))] == [20000, 6500]
        assert not [a for a in brief.next_actions(conn) if a["kind"] == "return_funding"]
        assert not [i for i in issues.collect(conn) if i["key"].startswith("funding")]
        t = readapi.transfer(conn, r["result"]["transfer_id"])
        assert (t["lender"], t["paid_from"], t["outstanding_to_lender_after"], t["affects_profit"]) == \
            ("Lloyds", "First Direct", "0.00", False)
    totals = api.get(f"{API}/read/dashboard").json()["data"]
    assert totals["temporary_funding_outstanding"] == "0.00"
    page = browser.get("/bets/").text
    assert "borrowed" not in page.split("Your money")[1][:600]                       # no "£240 borrowed"
    acc = browser.get("/bets/accounts").text
    assert "Temporary funding repaid, bank to bank" in acc and "of Lloyds's temporary funding" in acc


def test_web_form_records_a_repayment(app, banks, browser):
    with app.state.db.tx() as conn:
        lend(conn, banks, 5000)
    r = browser.post("/bets/transfers", {"from_id": banks["fd"], "to_id": banks["lloyds"], "amount": "50",
                                         "capital_type": "funding_repayment"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with app.state.db.read() as conn:
        assert owed(conn, banks) == 0


def test_a_repaid_loans_return_date_is_not_shown_as_due(app, banks):
    """Only funding still outstanding (repaid oldest first) has a return date that matters."""
    with app.state.db.tx() as conn:
        old = services.create_transfer(conn, from_id=banks["lloyds"], to_id=banks["smarkets"], amount=10000, at=at(-100),
                                       capital_type="temporary_funding", due_at=at(-50))
        services.record_funding_repayment(conn, from_id=banks["fd"], lender_id=banks["lloyds"], amount=10000, at=at(-60))
        new = services.create_transfer(conn, from_id=banks["lloyds"], to_id=banks["smarkets"], amount=5000, at=at(-10),
                                       capital_type="temporary_funding", due_at=at(240))
        services.create_transfer(conn, from_id=banks["smarkets"], to_id=banks["fd"], amount=25000,
                                 capital_type="bankroll_withdrawal", at=at(-5))       # no free cash left to return
        assert owed(conn, banks) == 5000
        assert not [a for a in brief.next_actions(conn) if a["kind"] == "return_funding"]   # due in 10 days
        services.set_transfer_due(conn, new, at(48))
        due = [a for a in brief.next_actions(conn) if a["kind"] == "return_funding"]
        stored = {r["id"]: r["due_at"] for r in conn.execute("SELECT id, due_at FROM transfers WHERE id IN (?, ?)",
                                                              (old, new))}
        assert [(a["priority"], a["deadline_label"]) for a in due] == [("high", times.local(stored[new], "%a %d %b %H:%M"))]
        assert due[0]["title"].startswith(f"{services.fmt(5000)} due back to Lloyds")
