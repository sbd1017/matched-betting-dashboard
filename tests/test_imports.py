"""Untrusted input -> normalise -> preview -> confirm -> the same services."""
import json

import pytest

from bets import imports, metrics, services


def raw(**kw):
    base = {"event": "Liverpool v Arsenal", "selection": "Liverpool",
            "legs": [{"account": "Betfred", "side": "back", "stake": "10", "odds": "4.0"},
                     {"account": "smarkets", "side": "lay", "stake": "9.57", "odds": "4.2"}]}
    base.update(kw)
    return base


def test_normalise_resolves_accounts_and_previews(conn, world):
    c, warnings = imports.normalise(conn, raw(), "shortcut")
    assert c["legs"][1]["account_id"] == world["smarkets"] and c["legs"][1]["commission_bp"] == 200
    assert c["expected_pnl_pence"] == -62 and warnings == []
    pv = imports.preview(conn, c)
    assert pv["match"].lay_stake == 957 and not pv["lay_differs"] and pv["legs"][1]["liability"] == 3062


@pytest.mark.parametrize("change,problem", [
    ({"legs": [{"account": "Nobody", "side": "back", "stake": "10", "odds": "2"}]}, "unknown bookmaker"),
    ({"legs": [{"account": "Betfred", "side": "lay", "stake": "10", "odds": "2"}]}, "lay bets go on an exchange"),
    ({"legs": [{"account": "Lloyds", "side": "back", "stake": "10", "odds": "2"}]}, "is a bank account"),
    ({"legs": [{"account": "Betfred", "side": "back", "stake": "10.555", "odds": "2"}]}, "two decimal places"),
    ({"legs": [{"account": "Betfred", "side": "back", "stake": "10", "odds": "0.5"}]}, "Odds must be above"),
    ({"legs": []}, "at least one bet"),
    ({"event": ""}, "event: missing"),
    ({"legs": [{"account": "Betfred", "side": "sideways", "stake": "10", "odds": "2"}]}, "back or lay"),
])
def test_bad_input_is_rejected_with_reasons(conn, world, change, problem):
    with pytest.raises(imports.CandidateError) as exc:
        imports.normalise(conn, raw(**change))
    assert any(problem in e for e in exc.value.errors), exc.value.errors


def test_suspicious_figures_are_flagged_not_trusted(conn, world):
    """OCR reads £100 instead of £10: stored as a candidate with a warning; nothing moves."""
    typo = raw(legs=[{"account": "Betfred", "side": "back", "stake": "1000", "odds": "4.0"}])
    iid, candidate, warnings = imports.store(conn, typo, "ocr")
    assert any("unusually large" in w for w in warnings)
    assert services.row(conn, "import_candidates", iid)["status"] == "needs_confirmation"
    assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
    assert imports.decide(conn, iid, accept=False) is None
    assert services.row(conn, "import_candidates", iid)["status"] == "rejected"
    with pytest.raises(services.UserError):
        imports.decide(conn, iid, accept=True)
    assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == 0


def test_confirmed_import_goes_through_the_normal_services(conn, world):
    iid, _, _ = imports.store(conn, raw(), "shortcut")
    pid = imports.decide(conn, iid, accept=True)
    pos = services.row(conn, "positions", pid)
    assert pos["source"] == "shortcut" and pos["expected_pnl_pence"] == -62
    legs = conn.execute("SELECT source, side FROM legs WHERE position_id = ?", (pid,)).fetchall()
    assert {(l[0], l[1]) for l in legs} == {("shortcut", "back"), ("shortcut", "lay")}
    assert metrics.summary(conn).unsettled_stakes == 1000
    assert services.row(conn, "import_candidates", iid)["applied_position_id"] == pid


def test_a_later_lay_can_join_an_existing_position(conn, world):
    """The future screenshot flow: back first, the exchange confirmation later."""
    c, _ = imports.normalise(conn, raw(legs=[{"account": "Betfred", "side": "back", "stake": "10", "odds": "4"}]))
    pid, _ = imports.apply(conn, c)
    assert metrics.incomplete_positions(conn)[0]["id"] == pid
    later = {"position_id": pid, "legs": [{"account": "Smarkets", "side": "lay", "stake": "9.57", "odds": "4.2"}]}
    iid, _, _ = imports.store(conn, later, "shortcut")
    assert imports.decide(conn, iid, True) == pid
    assert metrics.incomplete_positions(conn) == []


def test_candidate_is_revalidated_at_confirmation(conn, world):
    fid = services.create_free_bet(conn, account_id=world["betfred"], face_value=1000)
    item = raw(kind="free_bet_snr", legs=[{"account": "Betfred", "side": "back", "stake": "10", "odds": "5",
                                           "free_bet_id": fid}])
    iid, _, _ = imports.store(conn, item, "api")
    services.update_free_bet(conn, fid, {"status": "expired"})
    with pytest.raises(imports.CandidateError):
        imports.decide(conn, iid, True)
    assert json.loads(services.row(conn, "import_candidates", iid)["raw_json"])["event"] == "Liverpool v Arsenal"


def test_outcomes_match_the_calculator(conn, world):
    c, _ = imports.normalise(conn, raw())
    assert imports.outcomes(c) == (-62, -62)
