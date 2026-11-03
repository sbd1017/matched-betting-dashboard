"""Correcting an expected reward's frozen snapshot (count, value each, type) with update_expected_reward. The
regression case is the real bet365 welcome offer: terms corrected from 1 x £30 to 3 x £10 SNR after the reward was
expected, three £10 SNR free bets already linked, and the false "3 free bet(s) linked where 1 x £30.00" issue."""
from datetime import timedelta

import pytest

from bets import issues, metrics, ops, rewards, services, times

from .test_assistant import API, api, token  # noqa: F401 (fixtures)


def at(hours: float) -> str:
    return times.iso(times.now() + timedelta(hours=hours))


def change(api, name, rid, **params):
    r = api.post(f"{API}/changes/{name}", json={**params, "request_id": rid})
    return r.status_code, r.json()


def snapshot(conn, table, rid):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone())


def keys(conn):
    return {i["key"]: i for i in issues.collect(conn)}


@pytest.fixture
def b365(app):
    """bet365: offer expected as 1 x £30 SNR, then its terms corrected to 3 x £10; three £10 SNR free bets linked
    (two used, one available) -- exactly as live. Plus an unrelated Betfred expected reward."""
    with app.state.db.tx() as conn:
        ids = {"b365": services.create_account(conn, name="bet365", kind="bookmaker"),
               "betfred": services.create_account(conn, name="Betfred", kind="bookmaker")}
        ids["offer"] = services.create_offer(
            conn, title="bet365 Open Account Offer – Bet £10 Get £30 in Bet Credits", account_id=ids["b365"],
            terms={"reward_count": 1, "reward_each_pence": 3000, "reward_kind": "snr", "reward_expiry_days": 7})
        ids["er"], _ = rewards.expect(conn, offer_id=ids["offer"], expected_by=at(-80), notes="due because the qualifier settled")
        services.update_offer(conn, ids["offer"], {"reward_count": 3, "reward_each_pence": 1000}, note="terms corrected")
        ids["fbs"] = services.create_free_bets(conn, count=3, account_id=ids["b365"], face_value=1000, kind="snr",
                                               offer_id=ids["offer"], credited_at=at(-50), expires_at=at(70))
        rewards.link(conn, ids["er"], ids["fbs"])
        other = services.create_offer(conn, title="Betfred Bet £10 Get £10", account_id=ids["betfred"],
                                      terms={"reward_count": 1, "reward_each_pence": 1000, "reward_kind": "snr"})
        ids["other_er"], _ = rewards.expect(conn, offer_id=other, expected_by=at(24))
    return ids


def test_the_bet365_snapshot_is_corrected_without_touching_free_bets(app, api, b365):
    rid = b365["er"]
    with app.state.db.read() as conn:
        er0 = snapshot(conn, "expected_rewards", rid)
        fbs0 = [snapshot(conn, "free_bets", f) for f in b365["fbs"]]
        n_fbs0 = conn.execute("SELECT count(*) FROM free_bets").fetchone()[0]
        other0 = snapshot(conn, "expected_rewards", b365["other_er"])
        p0, ledger0 = metrics.summary(conn).profit_lifetime, conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
        found = keys(conn)
        assert (er0["count"], er0["each_pence"], er0["status"]) == (1, 3000, "received")
        mismatch = found[f"reward:mismatch:{rid}"]
        assert "3 free bet(s) linked where 1 x £30.00 was expected" in mismatch["text"]
        assert mismatch["repairs"] == [{"change": "update_expected_reward", "label":
                                        "correct it to the offer's terms (3 x £10.00 SNR)",
                                        "params": {"expected_reward_id": rid, "count": 3, "each": "10.00",
                                                   "reward_type": "snr"}}]
    code, r = change(api, "update_expected_reward", "er-fix-1", **mismatch["repairs"][0]["params"],
                     reason="offer terms corrected to 3 x £10 SNR")
    assert code == 200 and r["status"] == "applied", r
    assert "free bet" in " ".join(r.get("effects", [])) or r["result"]["changed"]
    assert not any("total" in w for w in r.get("warnings", []))                  # £30 before and after
    with app.state.db.read() as conn:
        er1 = snapshot(conn, "expected_rewards", rid)
        assert (er1["count"], er1["each_pence"], er1["kind"]) == (3, 1000, "snr")
        assert er1["count"] * er1["each_pence"] == er0["count"] * er0["each_pence"] == 3000
        for k in ("offer_id", "account_id", "source_position_id", "expected_by", "expiry_days", "status", "received_at",
                  "notes", "created_at"):
            assert er1[k] == er0[k], k
        # no free bet created, deleted or relinked
        assert conn.execute("SELECT count(*) FROM free_bets").fetchone()[0] == n_fbs0
        assert [snapshot(conn, "free_bets", f) for f in b365["fbs"]] == fbs0
        # unrelated expected reward untouched; never money or profit
        assert snapshot(conn, "expected_rewards", b365["other_er"]) == other0
        assert metrics.summary(conn).profit_lifetime == p0
        assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == ledger0
        # the false issue is gone, and nothing else appears
        after = keys(conn)
        assert f"reward:mismatch:{rid}" not in after and not any(k.startswith("reward:") for k in after)
        # audited, with the old and new shape; earlier history kept
        rows = conn.execute("SELECT action, before_json, after_json, note, actor FROM audit_log WHERE entity = "
                            "'expected_reward' AND entity_id = ? ORDER BY id", (rid,)).fetchall()
        assert [x["action"] for x in rows][:1] == ["expected"]
        fixed = [x for x in rows if x["action"] == "corrected"]
        assert len(fixed) == 1 and '"count": 1' in fixed[0]["before_json"] and '"each_pence": 3000' in fixed[0]["before_json"]
        assert '"count": 3' in fixed[0]["after_json"] and '"each_pence": 1000' in fixed[0]["after_json"]
        assert fixed[0]["note"] == "offer terms corrected to 3 x £10 SNR" and fixed[0]["actor"].startswith("assistant")
    # a retry changes nothing and is not logged twice
    code, again = change(api, "update_expected_reward", "er-fix-1", **mismatch["repairs"][0]["params"],
                         reason="offer terms corrected to 3 x £10 SNR")
    assert code == 200 and again.get("duplicate")
    with app.state.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM audit_log WHERE entity = 'expected_reward' AND entity_id = ? AND "
                            "action = 'corrected'", (rid,)).fetchone()[0] == 1


def test_preview_warns_when_the_total_changes_and_refuses_impossible_shapes(app, api, b365):
    rid = b365["er"]
    code, r = change(api, "update_expected_reward", "er-prev", expected_reward_id=rid, count=3, each="15", dry_run=True)
    assert code == 200 and r["status"] == "preview"
    assert any("total changes from £30.00 to £45.00" in w for w in r["warnings"])
    assert any("still won't match" in w for w in r["warnings"])
    for params, why in (({"reward_type": "cash"}, "can't become a cash reward"),
                        ({"reward_type": "sr"}, "linked free bets are SNR"),
                        ({"count": 0}, "count")):
        code, r = change(api, "update_expected_reward", f"er-bad-{why[:5]}", expected_reward_id=rid, **params)
        assert r["status"] in ("blocked", "rejected"), (params, r)
        assert why in (r.get("reason") or " ".join(r.get("errors", []))), (params, r)
    with app.state.db.read() as conn:
        assert snapshot(conn, "expected_rewards", rid)["count"] == 1                   # nothing applied


def test_waiting_reward_with_stale_terms_is_flagged_and_repaired(app, api):
    with app.state.db.tx() as conn:
        acc = services.create_account(conn, name="Coral", kind="bookmaker")
        oid = services.create_offer(conn, title="Coral Bet £5 Get £20", account_id=acc,
                                    terms={"reward_count": 1, "reward_each_pence": 2000, "reward_kind": "snr"})
        rid, _ = rewards.expect(conn, offer_id=oid, expected_by=at(24))
        services.update_offer(conn, oid, {"reward_count": 4, "reward_each_pence": 500}, note="4 x £5, not 1 x £20")
        issue = keys(conn)[f"reward:stale_terms:{rid}"]
        assert "still expects 1 x £20.00 SNR" in issue["text"] and "4 x £5.00 SNR" in issue["text"]
        assert issue["repairs"][0]["params"] == {"expected_reward_id": rid, "count": 4, "each": "5.00", "reward_type": "snr"}
    code, r = change(api, "update_expected_reward", "er-stale", **issue["repairs"][0]["params"])
    assert code == 200 and r["status"] == "applied", r
    with app.state.db.tx() as conn:
        e = snapshot(conn, "expected_rewards", rid)
        assert (e["count"], e["each_pence"], e["status"]) == (4, 500, "waiting")
        assert f"reward:stale_terms:{rid}" not in keys(conn)
        # and the free bets now match when they arrive
        fbs = services.create_free_bets(conn, count=4, account_id=acc, face_value=500, kind="snr", offer_id=oid)
        assert rewards.reconcile(conn, apply=True, reward_id=rid)
        assert snapshot(conn, "expected_rewards", rid)["status"] == "received"
        assert all(snapshot(conn, "free_bets", f)["expected_reward_id"] == rid for f in fbs)


def test_a_real_duplicate_gets_no_terms_repair(app, b365):
    """Only when the offer's terms describe exactly the free bets that arrived is the snapshot fix offered."""
    with app.state.db.tx() as conn:
        services.update_offer(conn, b365["offer"], {"reward_count": 2}, note="pretend: 2 x £10")
        issue = keys(conn)[f"reward:mismatch:{b365['er']}"]
        assert issue["repairs"] == [] and "(some of a different value)" in issue["text"]


def test_plan_is_safe_edit_and_noop_when_already_right(app, b365):
    with app.state.db.read() as conn:
        plan = ops.plan_for(conn, "update_expected_reward", {"expected_reward_id": b365["er"], "count": 1, "each": "30"})
        assert plan.level == "safe_edit" and plan.noop
