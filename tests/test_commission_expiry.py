"""Promotional commission rates with an end date: shown on the account and calendar, surfaced on the dashboard
30 and 7 days ahead, an issue once ended, and never changed automatically."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from bets import commission, imports, issues, metrics, services, times

from .test_assistant import token  # noqa: F401 (fixture)
from .test_web import seed


def day(offset: int) -> str:
    return (times.now().astimezone(times.UK).date() + timedelta(days=offset)).isoformat()


def ends_in(app, aid, days):
    with app.state.db.tx() as conn:
        services.update_account(conn, aid, commission_expires_at=commission.expiry_from_date(day(days)))


def zero_rate(app, ids):
    with app.state.db.tx() as conn:
        services.update_account(conn, ids["smarkets"], commission_bp=0)


def needs_count(html: str) -> int:
    return int(re.search(r'<span class="n">(\d+) needs? you</span>', html).group(1))


def needs_section(html: str) -> str:
    start = html.index('<div class="grp">Needs you')
    nxt = html.find('<div class="grp">', start + 10)
    return html[start:nxt if nxt > 0 else None]


def test_expiry_is_stored_as_the_start_of_that_uk_day_and_audited(app):
    ids = seed(app)
    zero_rate(app, ids)
    with app.state.db.tx() as conn:
        services.update_account(conn, ids["smarkets"], commission_expires_at=commission.expiry_from_date("2027-09-28"))
    with app.state.db.read() as conn:
        acc = services.row(conn, "accounts", ids["smarkets"])
        assert acc["commission_bp"] == 0
        assert acc["commission_expires_at"] == "2027-09-27T23:00:00Z"        # 00:00 BST on 28 Sep
        assert commission.promo(acc)["date"] == "28 Sep 2027" and commission.date_input(acc["commission_expires_at"]) == "2027-09-28"
        audit = conn.execute("SELECT after_json FROM audit_log WHERE entity = 'account' AND entity_id = ? "
                             "ORDER BY id DESC LIMIT 1", (ids["smarkets"],)).fetchone()[0]
        assert "commission_expires_at" in audit
        # bookmakers and banks have no commission, so no end date either
        services.update_account(conn, ids["betfred"], commission_expires_at=commission.expiry_from_date("2027-09-28"))
        assert services.row(conn, "accounts", ids["betfred"])["commission_expires_at"] is None


@pytest.mark.parametrize("days,level", [(200, "later"), (31, "later"), (30, "soon"), (8, "soon"), (7, "urgent"),
                                        (1, "urgent"), (0, "expired"), (-5, "expired")])
def test_levels(app, days, level):
    ids = seed(app)
    ends_in(app, ids["smarkets"], days)
    with app.state.db.read() as conn:
        p = commission.promo(services.row(conn, "accounts", ids["smarkets"]))
    assert p["level"] == level and p["days"] == days


def test_dashboard_quiet_beyond_30_days_then_up_next_then_needs_you(app, browser):
    ids = seed(app)
    zero_rate(app, ids)
    base = browser.get("/bets/").text
    ends_in(app, ids["smarkets"], 31)
    far = browser.get("/bets/").text
    assert "commission expires" not in far and needs_count(far) == needs_count(base)

    ends_in(app, ids["smarkets"], 30)
    soon = browser.get("/bets/").text
    assert "Smarkets 0% commission expires in 30 days" in soon
    assert "Verify the rate that will apply afterwards" in soon
    assert needs_count(soon) == needs_count(base) and "commission expires" not in needs_section(soon)

    ends_in(app, ids["smarkets"], 7)
    urgent = browser.get("/bets/").text
    assert needs_count(urgent) == needs_count(base) + 1
    section = needs_section(urgent)
    assert "Smarkets 0% commission expires in 7 days" in section and "k-amb" in section and "Check rate" in section

    ends_in(app, ids["smarkets"], 1)
    assert "Smarkets 0% commission expires tomorrow" in browser.get("/bets/").text


def test_expired_rate_is_an_issue_and_never_changed(app, browser):
    ids = seed(app)
    zero_rate(app, ids)
    ends_in(app, ids["smarkets"], -1)
    with app.state.db.read() as conn:
        found = [i for i in issues.collect(conn) if i["key"] == f"commission:{ids['smarkets']}"]
        assert services.row(conn, "accounts", ids["smarkets"])["commission_bp"] == 0
    assert found and found[0]["title"] == "Verify Smarkets commission rate"
    assert found[0]["href"] == f"/accounts/{ids['smarkets']}?edit=commission#edit"
    assert "Your recorded 0% promotional rate expired" in found[0]["text"]
    dash = browser.get("/bets/").text
    section = needs_section(dash)
    assert "Verify Smarkets commission rate" in section and "Verify rate" in section
    assert f'/bets/accounts/{ids["smarkets"]}?edit=commission#edit' in section
    assert "to put right" not in section                              # its own row, not also in the roll-up
    page = browser.get("/bets/issues").text
    assert "Verify Smarkets commission rate" in page
    acc = browser.get(f"/bets/accounts/{ids['smarkets']}?edit=commission").text
    assert 'id="edit" open' in acc and "Promotional rate expired" in acc and "may be out of date" in acc


def test_calendar_and_ics_show_the_end_date_once_however_far_ahead(app, browser):
    ids = seed(app)
    zero_rate(app, ids)
    ends_in(app, ids["smarkets"], 200)
    page = browser.get("/bets/calendar").text
    assert page.count("Smarkets 0% commission expires") == 1
    assert "verify the rate that applies afterwards" in page and "All day" in page
    end = times.now().astimezone(times.UK).date() + timedelta(days=200)
    assert end.strftime("%-d %b") in page
    ics = browser.get("/bets/calendar.ics").text
    assert ics.count(f"UID:commission-{ids['smarkets']}@") == 1
    ends_in(app, ids["smarkets"], -1)                                 # after the day, the issue takes over
    assert "commission expires" not in browser.get("/bets/calendar").text


def test_account_pages_say_the_rate_is_promotional(app, browser):
    ids = seed(app)
    zero_rate(app, ids)
    with app.state.db.tx() as conn:
        services.update_account(conn, ids["smarkets"], commission_expires_at=commission.expiry_from_date("2027-09-28"))
    acc = browser.get(f"/bets/accounts/{ids['smarkets']}").text
    assert "Promotional rate · expires 28 Sep 2027" in acc and 'value="2027-09-28"' in acc and 'id="edit" open' not in acc
    assert "0% commission · <span class=\"\">promotional rate, expires 28 Sep 2027</span>" in browser.get("/bets/accounts").text


def test_editing_the_end_date_and_saving_a_verified_rate(app, browser):
    ids = seed(app)
    aid = ids["smarkets"]
    form = {"name": "Smarkets", "commission": "0", "active": "1", "notes": "", "commission_expires_shown": "1"}
    assert browser.post(f"/bets/accounts/{aid}", form | {"commission_expires": day(100)}).status_code == 303
    with app.state.db.read() as conn:
        assert services.row(conn, "accounts", aid)["commission_expires_at"] == commission.expiry_from_date(day(100))
    # a new rate before the end keeps the date (the user can clear it)
    browser.post(f"/bets/accounts/{aid}", form | {"commission": "1", "commission_expires": day(100)})
    with app.state.db.read() as conn:
        assert services.row(conn, "accounts", aid)["commission_expires_at"] is not None
    # once ended, saving the rate that applies now clears the old date, and the issue goes
    browser.post(f"/bets/accounts/{aid}", form | {"commission": "1", "commission_expires": day(-2)})
    with app.state.db.read() as conn:
        assert any(i["key"] == f"commission:{aid}" for i in issues.collect(conn))
    browser.post(f"/bets/accounts/{aid}", form | {"commission": "2", "commission_expires": day(-2)})
    with app.state.db.read() as conn:
        acc = services.row(conn, "accounts", aid)
        assert acc["commission_bp"] == 200 and acc["commission_expires_at"] is None
        assert not any(i["key"].startswith("commission:") for i in issues.collect(conn))
    # clearing the field clears the date; a bad date is refused
    browser.post(f"/bets/accounts/{aid}", form | {"commission_expires": day(50)})
    browser.post(f"/bets/accounts/{aid}", form | {"commission_expires": ""})
    with app.state.db.read() as conn:
        assert services.row(conn, "accounts", aid)["commission_expires_at"] is None
    r = browser.post(f"/bets/accounts/{aid}", form | {"commission_expires": "28/09/2027"}, follow_redirects=True)
    assert "end date" in r.text


def test_calculators_warn_only_once_the_stored_rate_has_ended(app, browser, token):
    ids = seed(app)
    zero_rate(app, ids)
    ends_in(app, ids["smarkets"], 40)
    assert "data-stale" not in browser.get("/bets/quick-add").text
    with TestClient(app, base_url="http://testserver") as api:
        api.headers["Authorization"] = f"Bearer {token}"
        body = {"back_stake": "10", "back_odds": "4", "lay_odds": "4.2", "exchange": "Smarkets"}
        before = api.post("/bets/api/assistant/calculate", json=body).json()
        assert before["commission"] == "0%" and before["commission_warning"] is None
        ends_in(app, ids["smarkets"], -1)
        after = api.post("/bets/api/assistant/calculate", json=body).json()
        assert after["lay_stake"] == before["lay_stake"]               # still calculated with the stored 0%
        assert "promotional commission recorded for" in after["commission_warning"]
        typed = api.post("/bets/api/assistant/calculate", json=body | {"exchange": "", "commission": "0"}).json()
        assert typed["commission_warning"] is None                      # a typed rate isn't the stored one
    qa = browser.get("/bets/quick-add").text
    assert "data-stale=" in qa and "data-commission-warning" in qa
    raw = {"event": "A v B", "legs": [{"account": "Betfred", "side": "back", "stake": "10", "odds": "4"},
                                      {"account": "Smarkets", "side": "lay", "odds": "4.2"}]}
    with app.state.db.read() as conn:
        _, warnings = imports.normalise(conn, raw, "quick_add")
        _, typed_warnings = imports.normalise(conn, raw | {"legs": [raw["legs"][0], raw["legs"][1] | {"commission": "2"}]},
                                              "quick_add")
    assert any("promotional commission recorded for" in w for w in warnings)
    assert not any("promotional" in w for w in typed_warnings)
    pos = browser.get(f"/bets/positions/{ids['position']}").text
    assert "A blank Commission uses it" in pos


def test_accounts_without_an_end_date_are_untouched(app, browser):
    ids = seed(app)
    with app.state.db.read() as conn:
        before = metrics.summary(conn)
        assert commission.promos(conn) == [] and commission.promo(services.row(conn, "accounts", ids["smarkets"])) is None
        assert not any(i["key"].startswith("commission:") for i in issues.collect(conn))
    page = browser.get("/bets/accounts").text
    assert "2% commission" in page and "promotional" not in page
    assert "data-stale" not in browser.get("/bets/quick-add").text
    assert "Commission <b>" not in browser.get(f"/bets/accounts/{ids['smarkets']}").text
    cal = browser.get("/bets/calendar").text
    assert "commission expires" not in cal
    # setting an end date changes no figures
    ends_in(app, ids["smarkets"], 10)
    with app.state.db.read() as conn:
        after = metrics.summary(conn)
    assert (after.profit_lifetime, after.betting_balance, after.available) == \
           (before.profit_lifetime, before.betting_balance, before.available)
