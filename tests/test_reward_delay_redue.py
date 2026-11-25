"""Changing an offer's reward delay moves the due time of its rewards still waiting. The regression case is a weekly
club (reward credited days after the qualifier): the delay was added after the reward was already expected, and the
old due time (qualifier + 2 hours) kept showing it as late."""
from datetime import timedelta

import pytest

from bets import rewards, services, times

from .test_assistant import API, api, token  # noqa: F401 (fixtures)


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


def change(api, name, rid, **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": rid})
    return r.status_code, r.json()


def expected(conn, oid):
    return dict(conn.execute("SELECT * FROM expected_rewards WHERE offer_id = ? AND status <> 'void'", (oid,)).fetchone())


@pytest.fixture
def club(app):
    """Sky Bet Club: reward due once the qualifier is done (bet_placed), confirmed done 20 hours ago, and its delay
    was recorded as 2 hours, so the reward is now overdue."""
    with app.state.db.tx() as conn:
        acc = services.create_account(conn, name="Sky Bet", kind="bookmaker")
        oid = services.create_offer(conn, title="Sky Bet Club", account_id=acc,
                                    terms={"reward_count": 2, "reward_each_pence": 250, "reward_kind": "snr",
                                           "reward_trigger": "bet_placed", "reward_expiry_days": 7,
                                           "reward_delay_hours": 2})
    return oid


def qualify(api, oid, hours_ago=20):
    code, body = change(api, "update_offer_progress", f"q-{oid}", offer_id=oid, qualified=True,
                        when=times.to_local_input(at(-hours_ago)))
    assert code == 200, body


def test_a_new_delay_moves_a_waiting_reward_and_it_is_no_longer_overdue(app, api, club):
    qualify(api, club)
    with app.state.db.read() as conn:
        e0 = expected(conn, club)
        q = conn.execute("SELECT qualified_at FROM offers WHERE id = ?", (club,)).fetchone()[0]
    assert e0["status"] == "waiting" and e0["expected_by"] < times.now_iso()   # late under the 2 hour delay

    code, body = change(api, "update_offer_progress", "delay-1", offer_id=club, reward_delay_hours=146, dry_run=True)
    assert code == 200, body
    assert any(f"expected reward #{e0['id']}" in e for e in body["effects"]), body["effects"]
    with app.state.db.read() as conn:
        assert expected(conn, club)["expected_by"] == e0["expected_by"]      # a preview changes nothing

    code, body = change(api, "update_offer_progress", "delay-2", offer_id=club, reward_delay_hours=146)
    assert code == 200, body
    with app.state.db.read() as conn:
        e1 = expected(conn, club)
        audit = conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'expected_reward' AND entity_id = ? AND "
                             "action = 'due'", (e1["id"],)).fetchone()[0]
    assert e1["id"] == e0["id"] and e1["status"] == "waiting"
    assert e1["expected_by"] == times.iso(times.parse(q) + timedelta(hours=146))
    assert e1["expected_by"] > times.now_iso()
    assert audit == 1


def test_a_received_reward_keeps_its_due_time(app, api, club):
    qualify(api, club)
    with app.state.db.tx() as conn:
        e0 = expected(conn, club)
        fbs = services.create_free_bets(conn, count=2, account_id=e0["account_id"], face_value=250, kind="snr",
                                        offer_id=club, credited_at=at(-1), expires_at=at(160))
        rewards.link(conn, e0["id"], fbs)
    code, body = change(api, "update_offer_progress", "delay-3", offer_id=club, reward_delay_hours=146)
    assert code == 200, body
    with app.state.db.read() as conn:
        assert expected(conn, club)["expected_by"] == e0["expected_by"]


def test_no_reward_moves_before_the_qualifier_is_done(app, api, club):
    code, body = change(api, "update_offer_progress", "delay-4", offer_id=club, reward_delay_hours=146)
    assert code == 200, body
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM expected_rewards WHERE offer_id = ?", (club,)).fetchone()[0] == 0
