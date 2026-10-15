"""The drawer and the calculator page take their default commission from the chosen exchange's stored rate."""
import re
from decimal import Decimal

from bets import calc, commission, services

from .test_commission_expiry import day
from .test_web import seed


def drawer(html: str) -> str:
    return html[html.index('<aside class="drawer" id="calc"'):html.index("</aside>")]


def page_form(html: str) -> str:
    start = html.index('<form method="get" class="panel cols2" data-calc>')
    return html[start:html.index("</form>", start)]


def commission_value(fragment: str) -> str:
    return re.search(r'name="commission" inputmode="decimal" value="([^"]*)"', fragment).group(1)


def selected(fragment: str) -> str:
    return re.search(r'<option value="(\d*)"[^>]*selected', fragment).group(1)


def audit_count(app) -> int:
    with app.state.db.read() as conn:
        return conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]


def test_defaults_come_from_the_exchange_in_drawer_and_page(app, browser):
    ids = seed(app)                                                      # Smarkets stored at 2%
    for frag in (drawer(browser.get("/bets/").text), page_form(browser.get("/bets/calculator").text)):
        assert selected(frag) == str(ids["smarkets"]) and commission_value(frag) == "2"
        assert 'data-commission="2"' in frag and "Smarkets · 2%" in frag
    with app.state.db.tx() as conn:
        services.update_account(conn, ids["smarkets"], commission_bp=0)
    for frag in (drawer(browser.get("/bets/").text), page_form(browser.get("/bets/calculator").text)):
        assert commission_value(frag) == "0" and 'data-commission="0"' in frag     # no hardcoded 2%
        assert "data-stale" not in frag and "data-commission-warning" in frag


def test_each_exchange_carries_its_own_rate_and_the_last_used_one_is_chosen(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        bf = services.create_account(conn, name="Betfair", kind="exchange", commission_bp=500)
        services.create_position(conn, event="Late game", selection="Draw", legs=[
            {"account_id": ids["betfred"], "side": "back", "stake_pence": 500, "odds": "3.0"},
            {"account_id": bf, "side": "lay", "stake_pence": 500, "odds": "3.1", "placed_at": "2099-01-01T10:00:00Z"}])
    frag = drawer(browser.get("/bets/").text)
    assert f'<option value="{bf}" data-commission="5"' in frag and f'<option value="{ids["smarkets"]}" data-commission="2"' in frag
    assert selected(frag) == str(bf) and commission_value(frag) == "5"
    # the page can be pointed at another exchange; its stored rate is used
    p = page_form(browser.get(f"/bets/calculator?exchange={ids['smarkets']}").text)
    assert selected(p) == str(ids["smarkets"]) and commission_value(p) == "2"


def test_typed_commission_overrides_for_that_calculation_only(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        services.update_account(conn, ids["smarkets"], commission_bp=0)
    before = audit_count(app)
    q = f"stake=10&back_odds=4&lay_odds=4.2&exchange={ids['smarkets']}"
    stored = browser.get(f"/bets/calculator?{q}").text                    # no commission typed: the stored 0%
    typed = browser.get(f"/bets/calculator?{q}&commission=5").text
    assert commission_value(page_form(typed)) == "5"
    zero = calc.match("qualifying", 1000, Decimal("4"), Decimal("4.2"), 0)
    five = calc.match("qualifying", 1000, Decimal("4"), Decimal("4.2"), 500)
    assert f'data-out="lay_stake">£{zero.lay_stake / 100:.2f}' in stored
    assert f'data-out="lay_stake">£{five.lay_stake / 100:.2f}' in typed and zero.lay_stake != five.lay_stake
    api = browser.get("/bets/api/calc?kind=qualifying&stake=10&back_odds=4&lay_odds=4.2&commission=5").json()
    assert api["lay_stake"] == f"{five.lay_stake / 100:.2f}"
    with app.state.db.read() as conn:
        assert services.row(conn, "accounts", ids["smarkets"])["commission_bp"] == 0
    assert audit_count(app) == before                                    # nothing written back


def test_an_ended_promotional_rate_is_still_used_with_its_warning(app, browser):
    ids = seed(app)
    with app.state.db.tx() as conn:
        services.update_account(conn, ids["smarkets"], commission_bp=0)
        services.update_account(conn, ids["smarkets"], commission_expires_at=commission.expiry_from_date(day(-1)))
    for frag in (drawer(browser.get("/bets/").text), page_form(browser.get("/bets/calculator").text)):
        assert commission_value(frag) == "0"                              # not swapped for 2% or anything else
        assert 'data-stale="The 0% promotional commission recorded for Smarkets expired' in frag
    r = browser.get(f"/bets/calculator?stake=10&back_odds=4&lay_odds=4.2&exchange={ids['smarkets']}")
    assert r.status_code == 200 and 'data-out="lay_stake">£' in r.text  # the calculation still runs


def test_without_an_exchange_the_old_default_stays(app, browser):
    with app.state.db.tx() as conn:
        services.create_account(conn, name="Betfred", kind="bookmaker")
    for frag in (drawer(browser.get("/bets/").text), page_form(browser.get("/bets/calculator").text)):
        assert 'name="exchange"' not in frag and commission_value(frag) == "2"
