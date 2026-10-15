"""The one offer lifecycle.

An offer's status, stage, next action, "is a qualifier still required" and its expected reward are all worked out
here from the recorded facts -- qualifying positions, the reward trigger in its terms, the bookmaker's own
confirmation (qualified_at), expected rewards, free bets and cash rewards -- so get_offer, list_offers, the operating
brief, next actions, Issues, Still to come and the pages can never disagree.

`offers.status` is a stored copy of the derived status (so lists and SQL can filter on it). `sync` keeps it in step
and is called by every change that can move an offer on; nothing else writes it. The only statuses a person sets
directly are the closures: complete (closed by hand), expired and abandoned.

Reward trigger (offers.reward_trigger): what makes the reward due --
  bet_placed         the qualifying bet was accepted (e.g. Sky Bet: tokens appear straight away)
  bet_fully_matched  the qualifier was placed and hedged (or deliberately left unhedged)
  bet_settled        the qualifier settled (the default, as before)
  bet_lost           the qualifier settled and its back bet lost (money-back offers)
  other              only the bookmaker's confirmation (qualified_at) counts
"""
from __future__ import annotations

from datetime import timedelta

from . import services, times

TRIGGERS = ("bet_placed", "bet_fully_matched", "bet_settled", "bet_lost", "other")
DEFAULT_TRIGGER = "bet_settled"
CLOSED_BY_HAND = ("expired", "abandoned")
DELAY_HOURS = {"bet_placed": 2}            # how soon a reward is due after its trigger, unless the offer says
DEFAULT_DELAY_HOURS = 24
TRIGGER_TEXT = {"bet_placed": "the qualifying bet was placed", "bet_fully_matched": "the qualifier was placed and hedged",
                "bet_settled": "the qualifier settled", "bet_lost": "the qualifier lost",
                "other": "the bookmaker confirmed it"}


def trigger(offer: dict) -> str:
    return offer.get("reward_trigger") or DEFAULT_TRIGGER


def _qualifiers(conn, offer_id: int) -> list[dict]:
    out = []
    for p in conn.execute("SELECT * FROM positions WHERE offer_id = ? AND kind = 'qualifying' AND deleted_at IS NULL "
                          "AND status <> 'void' ORDER BY id", (offer_id,)):
        legs = [dict(l) for l in conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL "
                                              "AND result <> 'void'", (p["id"],))]
        backs = [l for l in legs if l["side"] == "back"]
        if backs:
            out.append({**dict(p), "backs": backs, "lays": [l for l in legs if l["side"] == "lay"]})
    return out


def _satisfied(offer: dict, q: dict) -> tuple[bool, str | None, str]:
    """(satisfied, when, why-not) for one qualifying position under the offer's trigger."""
    placed = min((l["placed_at"] or l["created_at"]) for l in q["backs"])
    trig = trigger(offer)
    if trig == "bet_placed":
        return True, placed, ""
    if trig == "bet_fully_matched":
        hedged = bool(q["lays"]) or q.get("hedge_strategy") == "intentionally_unhedged"
        return hedged, placed if hedged else None, "the lay is not recorded yet"
    if trig == "bet_settled":
        return q["status"] == "settled", q["settled_at"], "the qualifier has not settled"
    if trig == "bet_lost":
        lost = q["status"] == "settled" and any(l["result"] == "lost" for l in q["backs"])
        return lost, q["settled_at"] if lost else None, "the qualifier has not lost"
    return False, None, "waiting for the bookmaker to confirm the qualifier"


def derive(conn, offer: dict) -> dict:
    """Everything about where an offer stands. status is what offers.status must hold."""
    o = dict(offer)
    base = {"trigger": trigger(o), "reward_received": False, "qualifier_required": False, "qualified": False, "qualified_at": None,
            "qualifier_position_ids": [], "deadline": o["expires_at"], "actor": None, "next_action": None}
    if o["deleted_at"]:
        return {**base, "status": o["status"], "stage": "void"}
    if o["status"] in CLOSED_BY_HAND:
        return {**base, "status": o["status"], "stage": {"expired": "expired", "abandoned": "void"}[o["status"]],
                "deadline": None}
    now = times.now_iso()
    quals = _qualifiers(conn, o["id"])
    base["qualifier_position_ids"] = [q["id"] for q in quals]
    tokens = [dict(r) for r in conn.execute(
        "SELECT f.*, (SELECT p.status FROM legs l JOIN positions p ON p.id = l.position_id WHERE l.free_bet_id = f.id "
        "AND l.deleted_at IS NULL AND l.result <> 'void' LIMIT 1) AS pos FROM free_bets f WHERE f.deleted_at IS NULL AND "
        "f.status <> 'void' AND (f.offer_id = ? OR f.expected_reward_id IN (SELECT id FROM expected_rewards WHERE "
        "offer_id = ?))", (o["id"], o["id"]))]
    cash = conn.execute("SELECT count(*) FROM ledger_entries WHERE offer_id = ? AND superseded_at IS NULL AND category = "
                        "'promotion_reward'", (o["id"],)).fetchone()[0]
    expected = [dict(r) for r in conn.execute("SELECT * FROM expected_rewards WHERE offer_id = ? AND status <> 'void' "
                                              "ORDER BY id", (o["id"],))]
    sat = [(q, *_satisfied(o, q)) for q in quals]
    hit = [(q, when) for q, ok, when, _ in sat if ok]
    if hit:
        base.update(qualified=True, qualified_at=min(w or now for _, w in hit))
    elif o["qualified_at"]:
        base.update(qualified=True, qualified_at=o["qualified_at"])
    base["reward_received"] = bool(tokens or cash)
    if tokens or cash:
        base["qualified"] = True
        avail = [t for t in tokens if t["status"] == "available"]
        used = [t for t in tokens if t["pos"]]
        open_ = [t for t in used if t["pos"] == "open"]
        soon = min((t["expires_at"] for t in avail if t["expires_at"]), default=None)
        if avail and not used:
            return {**base, "status": "reward_received", "stage": "reward_credited", "deadline": soon, "actor": "both",
                    "next_action": f"Use the {len(avail)} free bet(s)" + (" before they expire" if soon else "")}
        if avail:
            return {**base, "status": "converting", "stage": "reward_partially_used", "deadline": soon, "actor": "both",
                    "next_action": f"Use the {len(avail)} free bet(s) left"}
        if open_:
            return {**base, "status": "converting", "stage": "reward_used", "deadline": None, "actor": "assistant",
                    "next_action": "Settle the free-bet positions when they finish"}
        return {**base, "status": "complete", "stage": "completed", "deadline": None}
    if o["status"] == "complete":                          # closed by hand with nothing recorded to say otherwise
        return {**base, "status": "complete", "stage": "completed", "deadline": None}
    if base["qualified"]:
        waiting = [e for e in expected if e["status"] in ("waiting", "partially_received")]
        late = [e for e in waiting if e["expected_by"] and e["expected_by"] < now]
        stage = "reward_overdue" if late else "reward_expected"
        return {**base, "status": "awaiting_reward", "stage": stage, "actor": "user" if late else "both",
                "deadline": (late or waiting or [{"expected_by": None}])[0]["expected_by"],
                "next_action": ("The reward is overdue: check the bookmaker and chase it if it has not arrived" if late else
                                "Check the bookmaker for the reward; when it shows, record it (reconcile_rewards / "
                                "mark_reward_received)")}
    if quals:
        q, _, _, why = sat[0]
        if trigger(o) == "bet_lost" and all(x["status"] == "settled" for x in quals):
            return {**base, "status": "complete", "stage": "not_triggered", "deadline": None,
                    "next_action": None, "note": "the qualifier did not lose, so no refund is due"}
        return {**base, "status": "qualifying", "stage": "qualifier_placed", "actor": "assistant",
                "next_action": {"bet_settled": "Settle the qualifier when it finishes",
                                "bet_lost": "Settle the qualifier when it finishes",
                                "bet_fully_matched": "Record the qualifier's lay",
                                }.get(trigger(o), "Record the bookmaker's confirmation that it qualified"),
                "waiting_for": why}
    base["qualifier_required"] = True
    if o["expires_at"] and o["expires_at"] < now:
        return {**base, "status": "expired", "stage": "expired", "qualifier_required": False, "deadline": None,
                "next_action": None}
    plans = conn.execute("SELECT count(*) FROM bet_plans WHERE offer_id = ? AND kind = 'qualifying' AND status IN "
                         "('draft', 'validated', 'ready_to_place')", (o["id"],)).fetchone()[0]
    if plans:
        return {**base, "status": "not_started", "stage": "qualifier_planned", "actor": "user",
                "next_action": "Validate and place the qualifying bet"}
    if o["opt_in_required"] and not o["opted_in_at"]:
        return {**base, "status": "not_started", "stage": "reviewed" if o["reviewed_at"] else "discovered",
                "actor": "user", "next_action": "Opt in to the offer"}
    if o["deposit_required"] and not o["deposit_done_at"]:
        return {**base, "status": "not_started", "stage": "deposit_required", "actor": "user",
                "next_action": "Make the qualifying deposit"}
    return {**base, "status": "not_started", "stage": "qualifier_required", "actor": "assistant",
            "next_action": "Plan the qualifying bet"}


def sync(conn, offer_id: int | None, note: str = "") -> dict | None:
    """Bring offers.status (and reward_credited / reward_used) in line with the facts, expect the reward once it is
    due, and link reward free bets that arrived to it. Idempotent. Returns the derived state."""
    if not offer_id:
        return None
    from . import rewards
    o = services.row(conn, "offers", int(offer_id))
    if not o or o["deleted_at"]:
        return None
    state = derive(conn, o)
    has_terms = o["reward_kind"] and o["reward_each_pence"]
    arrived = o["reward_kind"] != "cash" and conn.execute(
        "SELECT 1 FROM free_bets WHERE offer_id = ? AND deleted_at IS NULL AND status <> 'void'", (o["id"],)).fetchone()
    # due (awaiting) -- or its free bets already arrived without one: expected after the fact, then matched to them
    if state["qualified"] and has_terms and (state["status"] == "awaiting_reward" or arrived) and not conn.execute(
            "SELECT 1 FROM expected_rewards WHERE offer_id = ? AND status <> 'void'", (o["id"],)).fetchone():
        delay = o["reward_delay_hours"] or DELAY_HOURS.get(trigger(o), DEFAULT_DELAY_HOURS)
        due = times.iso(times.parse(state["qualified_at"] or times.now_iso()) + timedelta(hours=delay))
        if arrived and state["status"] != "awaiting_reward":   # after the fact: due when it actually arrived
            due = conn.execute("SELECT min(coalesce(credited_at, created_at)) FROM free_bets WHERE offer_id = ? AND "
                               "deleted_at IS NULL AND status <> 'void'", (o["id"],)).fetchone()[0] or due
        rewards.expect(conn, offer_id=o["id"], expected_by=due, sync=False,
                       source_position_id=(state["qualifier_position_ids"] or [None])[0],
                       notes=f"due because {TRIGGER_TEXT[trigger(o)]}")
    for e in conn.execute("SELECT * FROM expected_rewards WHERE offer_id = ? AND status IN ('waiting', "
                          "'partially_received') AND kind <> 'cash'", (o["id"],)).fetchall():
        have = conn.execute("SELECT count(*) FROM free_bets WHERE expected_reward_id = ? AND deleted_at IS NULL AND "
                            "status <> 'void'", (e["id"],)).fetchone()[0]
        spare = [r[0] for r in conn.execute(
            "SELECT id FROM free_bets WHERE offer_id = ? AND expected_reward_id IS NULL AND deleted_at IS NULL AND "
            "status <> 'void' AND kind = ? AND face_value_pence = ? AND account_id = ? ORDER BY id",
            (o["id"], e["kind"], e["each_pence"], e["account_id"]))][:max(0, e["count"] - have)]
        if spare:
            rewards.link(conn, e["id"], spare, sync=False)
    state = derive(conn, services.row(conn, "offers", o["id"]))
    o = services.row(conn, "offers", o["id"])
    want = {"status": state["status"]}
    if state["reward_received"]:
        want["reward_credited"] = 1
        if state["stage"] == "completed":
            want["reward_used"] = 1
    changes = {k: v for k, v in want.items() if o[k] != v}
    if changes:
        conn.execute("UPDATE offers SET " + ", ".join(f"{k} = ?" for k in changes) + ", updated_at = ? WHERE id = ?",
                     (*changes.values(), times.now_iso(), o["id"]))
        services.audit(conn, "offer", o["id"], "status", {k: o[k] for k in changes}, {**changes, "stage": state["stage"]},
                       note or f"follows the records: {state['stage'].replace('_', ' ')}")
    return state


def mark_qualified(conn, offer_id: int, at: str | None = None, note: str = "") -> dict | None:
    """The bookmaker itself shows the qualifier as done (or credited the reward): a fact the lifecycle uses."""
    o = services.row(conn, "offers", int(offer_id))
    if o and not o["qualified_at"]:
        when = at or times.now_iso()
        conn.execute("UPDATE offers SET qualified_at = ?, updated_at = ? WHERE id = ?", (when, times.now_iso(), o["id"]))
        services.audit(conn, "offer", o["id"], "progress", {"qualified_at": None}, {"qualified_at": when},
                       note or "the bookmaker shows the qualifier as done")
    return sync(conn, offer_id)


def sync_all(conn) -> int:
    n = 0
    for (oid,) in conn.execute("SELECT id FROM offers WHERE deleted_at IS NULL").fetchall():
        before = conn.execute("SELECT status FROM offers WHERE id = ?", (oid,)).fetchone()[0]
        state = sync(conn, oid)
        n += bool(state and state["status"] != before)
    return n


def out_of_step(conn) -> list[dict]:
    """Offers whose stored status disagrees with their records (an integrity check; sync fixes it)."""
    bad = []
    for o in conn.execute("SELECT * FROM offers WHERE deleted_at IS NULL").fetchall():
        st = derive(conn, dict(o))
        if st["status"] != o["status"]:
            bad.append({"offer_id": o["id"], "title": o["title"], "stored": o["status"], "derived": st["status"],
                        "stage": st["stage"]})
    return bad
