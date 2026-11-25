"""The Profit chart's two plots, free bets by expiry, settled ladder cells and when a loss is red."""
from datetime import datetime, timedelta, timezone

from bets import outlook, profit, services, times


def period(net, running, current=False):
    return {"net": net, "running": running, "key": "k", "label": "L", "short": "S", "current": current, "count": 1}


def test_chart_bars_have_their_own_scale_below_the_running_total():
    c = profit.chart([period(100, 100_000), period(400, 100_400), period(-200, 100_200, current=True)])
    tall = max(c["bars"], key=lambda b: b["h"])
    assert tall["row"]["net"] == 400
    assert tall["h"] > (profit.BARS_BAND[1] - profit.BARS_BAND[0]) / 2        # not squashed by the running total
    assert all(b["y"] >= profit.BARS_BAND[0] and b["y"] + b["h"] <= profit.BARS_BAND[1] for b in c["bars"])
    assert all(profit.LINE_BAND[0] <= b["py"] <= profit.LINE_BAND[1] for b in c["bars"])


def test_chart_average_leaves_out_the_period_still_running():
    assert profit.chart([period(100, 100), period(300, 400), period(-900, -500, current=True)])["average"] == 200
    assert profit.chart([period(100, 100), period(300, 400, current=True)])["average"] is None
    assert profit.chart([])["bars"] == []


def bet(fid, face, expires):
    return {"id": fid, "face_value_pence": face, "expires_at": expires, "kind": "snr", "account_id": 1, "account": "B", "notes": ""}


NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


def test_expiry_strip_puts_each_free_bet_on_its_uk_day():
    strip = outlook.expiry_strip([bet(1, 500, "2026-11-06T22:00:00Z"), bet(2, 1000, "2026-11-07T00:30:00Z"),
                                  bet(3, 2500, "2026-11-12T23:00:00Z"), bet(4, 700, "2026-11-13T00:00:00Z"),
                                  bet(5, 900, None)], now=NOW)
    assert [d["label"] for d in strip["days"]][:2] == ["Today", "Sat 7"]
    assert [[t["id"] for t in d["tokens"]] for d in strip["days"]] == [[1], [2], [], [], [], [], [3]]
    assert [t["size"] for d in strip["days"] for t in d["tokens"]] == ["s", "m", "l"]
    assert (strip["later"], strip["later_value"], strip["undated"]) == (1, 700, 1)


def test_expiry_strip_counts_what_will_not_fit_and_keeps_every_bet():
    strip = outlook.expiry_strip([bet(i, 500, "2026-11-06T22:00:00Z") for i in range(5)], now=NOW)
    assert len(strip["days"][0]["tokens"]) == outlook.STRIP_TOKENS_A_DAY and strip["days"][0]["more"] == 2


def test_a_loss_is_red_only_when_worse_than_expected():
    assert outlook.result_cls(500, -20) == "gain"
    assert outlook.result_cls(0) == "" and outlook.result_cls(None) == ""
    assert outlook.result_cls(-20, -20) == "" and outlook.result_cls(-21, -20) == ""     # to plan, or a rounding penny
    assert outlook.result_cls(-22, -20) == "loss"
    assert outlook.result_cls(-20, None) == "loss"                                       # nothing to compare it with


def settled_position(conn, a, expected):
    pid = services.create_position(conn, event="Example FC v Sample Town", selection="Example FC", expected_pnl=expected, legs=[
        dict(account_id=a["betfred"], side="back", stake_pence=1000, odds="3"),
        dict(account_id=a["smarkets"], side="lay", stake_pence=1000, odds="3.1", commission_bp=0)])
    back, lay = [r["id"] for r in conn.execute("SELECT id FROM legs WHERE position_id = ? ORDER BY side", (pid,))]
    services.settle_leg(conn, back, "won")
    services.settle_leg(conn, lay, "lost")                # back +£20.00, lay −£21.00: a £1.00 loss
    return pid


def accounts(conn):
    return {"betfred": services.create_account(conn, name="Betfred", kind="bookmaker"),
            "smarkets": services.create_account(conn, name="Smarkets", kind="exchange")}


def test_position_page_colours_a_planned_loss_neutrally_and_hollows_the_losing_leg(app, browser):
    with app.state.db.tx() as conn:
        a = accounts(conn)
        planned = settled_position(conn, a, -100)
        worse = settled_position(conn, a, 0)
    page = browser.get(f"/bets/positions/{planned}").text
    assert 'class="v ">−£1.00' in page and 'class="v loss"' not in page
    assert 'class="odd back"' in page and 'class="odd lay lost"' in page
    assert 'class="v loss">−£1.00' in browser.get(f"/bets/positions/{worse}").text


def test_dashboard_shows_free_bets_by_expiry_and_still_links_to_using_them(app, browser):
    with app.state.db.tx() as conn:
        a = accounts(conn)
        fid = services.create_free_bet(conn, account_id=a["betfred"], face_value=2500, kind="snr",
                                       expires_at="2099-01-01T00:00:00Z")
        services.create_free_bet(conn, account_id=a["betfred"], face_value=500, kind="snr",
                                 expires_at=times.iso(times.now() + timedelta(days=1)))
    page = browser.get("/bets/").text
    assert 'class="expiry"' in page and "odd free tok s" in page
    assert "1 more after" in page and "£25.00" in page
    assert f"free_bet_id={fid}" not in page.split('id="free-bets-held"')[1].split("</section>")[0]


def test_betting_tool_link_is_off_unless_configured(browser):
    assert 'class="ib bot"' not in browser.get("/bets/").text


def test_betting_tool_link_shows_as_a_small_header_icon_when_configured(tmp_path):
    from fastapi.testclient import TestClient
    from bets.web import create_app
    from tests.conftest import Browser, make_settings
    app = create_app(make_settings(tmp_path, autobet_url="https://autobet.example.com"))
    with TestClient(app, base_url="http://testserver") as c:
        b = Browser(c)
        b.login()
        page = b.get("/bets/").text
    link = page.split('class="ib bot"')[1].split("</a>")[0]
    assert 'href="https://autobet.example.com/"' in link and 'rel="noopener noreferrer"' in link


def test_betting_tool_link_only_accepts_an_https_origin():
    from bets.config import autobet_url
    assert autobet_url(" https://autobet.example.com/ ") == "https://autobet.example.com"
    for bad in ("http://autobet.example.com", "https://autobet.example.com/x", "javascript:alert(1)",
                "https://user@autobet.example.com", "https://autobet.example.com?x=1", ""):
        assert autobet_url(bad) == ""


def test_where_it_came_from_shows_the_top_five_and_hides_the_rest_behind_show_all(app, browser):
    with app.state.db.tx() as conn:
        a = accounts(conn)
        for i in range(7):
            oid = services.create_offer(conn, title=f"Offer {i}", account_id=a["betfred"])
            pid = services.create_position(conn, event=f"Team {i} v Other", selection=f"Team {i}", offer_id=oid, legs=[
                {"account_id": a["betfred"], "side": "back", "stake_pence": 1000, "odds": "4.0",
                 "placed_at": "2026-10-01T10:00:00Z"},
                {"account_id": a["smarkets"], "side": "lay", "stake_pence": 957, "odds": "4.2",
                 "placed_at": "2026-10-01T10:00:00Z"}])
            services.quick_settle(conn, pid, "back_lost", at="2026-10-02T10:00:00Z")
    page = browser.get("/bets/profit").text
    offers = page.split("<th>Offer</th>")[1].split("</table>")[0]
    shown, rest = offers.split('<tbody id="from-offer" hidden>')
    assert shown.count("<tr>") == 5 and rest.count("<tr>") == 2
    after = page.split("<th>Offer</th>")[1].split("</section>")[0]
    assert 'data-expand="from-offer"' in after and ">Show all 7</button>" in after
    assert 'id="from-bookmaker"' not in page                               # one bookmaker: nothing to hide
