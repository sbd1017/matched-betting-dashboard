"""offer_state.sync_all and out_of_step skip closed offers their records cannot move (settled_ids). The results must be
exactly what deriving and syncing every offer gives, including when a closed offer's records changed underneath it."""
from datetime import datetime, timezone
from unittest import mock

import pytest

from bets import offer_state, services

FREE_BETS = {"reward_count": 2, "reward_each_pence": 1000, "reward_kind": "snr", "reward_trigger": "bet_placed"}
CASH = {"reward_count": 1, "reward_each_pence": 500, "reward_kind": "cash", "reward_trigger": "bet_placed"}


@pytest.fixture(autouse=True)
def clock():
    with mock.patch("bets.times.now", return_value=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)):
        yield


def full_sync_all(conn) -> int:
    """sync_all before settled offers were skipped: every offer."""
    n = 0
    for (oid,) in conn.execute("SELECT id FROM offers WHERE deleted_at IS NULL").fetchall():
        before = conn.execute("SELECT status FROM offers WHERE id = ?", (oid,)).fetchone()[0]
        state = offer_state.sync(conn, oid)
        n += bool(state and state["status"] != before)
    return n


def full_out_of_step(conn) -> list[dict]:
    bad = []
    for o in conn.execute("SELECT * FROM offers WHERE deleted_at IS NULL").fetchall():
        st = offer_state.derive(conn, dict(o))
        if st["status"] != o["status"]:
            bad.append({"offer_id": o["id"], "title": o["title"], "stored": o["status"], "derived": st["status"],
                        "stage": st["stage"]})
    return bad


def dump(conn) -> dict:
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("offers", "expected_rewards", "free_bets", "audit_log")}


def outcome(conn, fast: bool):
    """(out_of_step, offers sync_all moved, out_of_step after, every record sync could write), then undone."""
    conn.execute("SAVEPOINT compare")
    try:
        before = offer_state.out_of_step(conn) if fast else full_out_of_step(conn)
        moved = offer_state.sync_all(conn) if fast else full_sync_all(conn)
        return before, moved, offer_state.out_of_step(conn) if fast else full_out_of_step(conn), dump(conn)
    finally:
        conn.execute("ROLLBACK TO compare")
        conn.execute("RELEASE compare")


@pytest.fixture
def offers(conn, world):
    """Closed offers of every kind, plus open ones."""
    o = {}
    for i in range(2):                               # free bets received, used and settled: complete
        oid = o[f"used{i}"] = services.create_offer(conn, title=f"Free bets {i}", account_id=world["betfred"],
                                                    terms=FREE_BETS)
        for fid in services.receive_reward(conn, oid):
            pid = services.create_position(conn, event=f"Use {fid}", kind="free_bet_snr", legs=[
                {"account_id": world["betfred"], "side": "back", "stake_pence": 1000, "odds": "5.0", "free_bet_id": fid},
                {"account_id": world["smarkets"], "side": "lay", "stake_pence": 800, "odds": "5.2"}])
            for leg in conn.execute("SELECT id, side FROM legs WHERE position_id = ?", (pid,)).fetchall():
                services.settle_leg(conn, leg["id"], "lost" if leg["side"] == "back" else "won")
        offer_state.sync(conn, oid)
    o["cash"] = services.create_offer(conn, title="Cash back", account_id=world["paddy"], terms=CASH)
    services.receive_reward(conn, o["cash"])
    o["by_hand"] = services.create_offer(conn, title="Closed by hand", account_id=world["paddy"], status="complete")
    o["expired"] = services.create_offer(conn, title="Too late", account_id=world["paddy"], status="expired")
    o["abandoned"] = services.create_offer(conn, title="Skipped", account_id=world["paddy"], status="abandoned")
    o["held"] = services.create_offer(conn, title="Free bets to use", account_id=world["betfred"], terms=FREE_BETS)
    services.receive_reward(conn, o["held"])
    o["new"] = services.create_offer(conn, title="Not started", account_id=world["betfred"], terms=FREE_BETS)
    return o


def status(conn, oid):
    return conn.execute("SELECT status FROM offers WHERE id = ?", (oid,)).fetchone()[0]


def test_closed_offers_are_skipped_and_nothing_changes(conn, offers):
    closed = ("used0", "used1", "cash", "by_hand", "expired", "abandoned")
    assert [status(conn, offers[k]) for k in closed] == ["complete"] * 4 + ["expired", "abandoned"]
    assert {status(conn, offers["held"]), status(conn, offers["new"])} == {"reward_received", "not_started"}
    want = {offers[k] for k in closed}
    assert offer_state.settled_ids(conn) == offer_state.settled_ids(conn, quiet=True) == want
    assert outcome(conn, fast=True) == outcome(conn, fast=False)
    assert outcome(conn, fast=True)[:3] == ([], 0, [])


def _free_bet(conn, oid):
    return conn.execute("SELECT id FROM free_bets WHERE offer_id = ? ORDER BY id LIMIT 1", (oid,)).fetchone()[0]


DRIFT = {
    # the records moved a complete offer on: derived again, never skipped
    "free bet available again": lambda c, o: c.execute("UPDATE free_bets SET status = 'available' WHERE id = ?",
                                                       (_free_bet(c, o["used0"]),)),
    "free-bet position open again": lambda c, o: c.execute(
        "UPDATE positions SET status = 'open' WHERE id = (SELECT position_id FROM legs WHERE free_bet_id = ?)",
        (_free_bet(c, o["used0"]),)),
    "reward linked through the expected reward only": lambda c, o: c.execute(
        "UPDATE free_bets SET offer_id = NULL, status = 'available' WHERE id = ?", (_free_bet(c, o["used1"]),)),
    # the status stays but sync has something to write
    "reward_credited not set": lambda c, o: c.execute("UPDATE offers SET reward_credited = 0 WHERE id = ?", (o["used0"],)),
    "reward_used not set": lambda c, o: c.execute("UPDATE offers SET reward_used = 0 WHERE id = ?", (o["cash"],)),
    "expected reward voided": lambda c, o: c.execute("UPDATE expected_rewards SET status = 'void' WHERE offer_id = ?",
                                                     (o["used1"],)),
    "expected reward waiting with spare free bets": lambda c, o: (
        c.execute("UPDATE free_bets SET expected_reward_id = NULL WHERE offer_id = ?", (o["used0"],)),
        c.execute("UPDATE expected_rewards SET status = 'waiting' WHERE offer_id = ?", (o["used0"],))),
    "abandoned offer with a waiting reward": lambda c, o: (
        c.execute("UPDATE offers SET status = 'abandoned' WHERE id = ?", (o["used1"],)),
        c.execute("UPDATE free_bets SET expected_reward_id = NULL WHERE offer_id = ?", (o["used1"],)),
        c.execute("UPDATE expected_rewards SET status = 'waiting' WHERE offer_id = ?", (o["used1"],))),
    # stored statuses out of step with the records
    "open offer stored as complete": lambda c, o: c.execute("UPDATE offers SET status = 'complete' WHERE id = ?",
                                                            (o["held"],)),
    "complete offer stored as qualifying": lambda c, o: c.execute("UPDATE offers SET status = 'qualifying' WHERE id = ?",
                                                                  (o["by_hand"],)),
    "held free bets voided": lambda c, o: c.execute("UPDATE free_bets SET status = 'void' WHERE offer_id = ?", (o["held"],)),
}


@pytest.mark.parametrize("name", DRIFT)
def test_records_changed_underneath_give_the_same_results(conn, offers, name):
    DRIFT[name](conn, offers)
    fast, full = outcome(conn, fast=True), outcome(conn, fast=False)
    assert fast == full
    assert fast[0] or fast[1] or fast[3] != dump(conn), "the drift should give sync something to do"
