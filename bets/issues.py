"""Everything that needs putting right, in one list: the /issues page, the dashboard, the nav count and the
assistant's get_issues.

Read-only. Each issue says what is wrong in plain words, lists the records involved and links to the page where it
is fixed. Where the repair is clear, it carries `repairs`: maintenance changes (bets/ops.py) with their parameters,
which the Issues page offers as buttons and the assistant can apply with the change tool of the same name.

Severities:
  fix   the ledger or a record is wrong or incomplete (red)
  act   something to do now that the app cannot do for you (amber)
  info  worth a look; nothing is wrong yet (ink)
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal

from . import calc, commission, metrics, times
from .money import fmt

SEVERITY_ORDER = {"fix": 0, "act": 1, "info": 2}
SEVERITY_LABELS = {"fix": "Fix", "act": "To do", "info": "Worth a look"}
CHECK_STALE_DAYS = 14             # balances not checked for this long are flagged
REWARD_WAIT_DAYS = 2              # a reward that has not arrived this long after qualifying is flagged
LARGE_RECONCILIATION = 1000       # a P&L adjustment from a balance check this large (pence) deserves a second look


def _issue(key: str, severity: str, kind: str, title: str, text: str, href: str, action: str, *, records=(),
           repairs=(), **extra) -> dict:
    return {"key": key, "severity": severity, "kind": kind, "title": title, "text": text, "href": href,
            "action": action, "records": list(records), "repairs": list(repairs), **extra}


def _repair(change: str, label: str, **params) -> dict:
    return {"change": change, "label": label, "params": params}


def _info_score(fb) -> int:
    return (1 if fb["expires_at"] else 0) + (1 if fb["offer_id"] else 0)


def collect(conn, s: metrics.Summary | None = None, include_dismissed: bool = False) -> list[dict]:
    s = s or metrics.summary(conn)
    now = times.now_iso()
    out: list[dict] = []
    from . import exposure
    for p in conn.execute("SELECT p.id, p.event FROM positions p JOIN exposure_scenarios e ON e.position_id=p.id "
                          "WHERE p.status='open' AND p.deleted_at IS NULL"):
        if exposure.valid(conn, p['id']) is None:
            out.append(_issue(f"exposure:{p['id']}", "fix", "Exposure", p['event'],
                              "The bets changed after their market outcomes were recorded. Liability is shown conservatively until reviewed.",
                              f"/positions/{p['id']}", "Review"))

    from . import offer_state
    for o in offer_state.out_of_step(conn):
        out.append(_issue(f"offer:status:{o['offer_id']}", "act", "Offer", o["title"],
                          f"Status says {o['stored'].replace('_', ' ')} but its records say {o['derived'].replace('_', ' ')} "
                          "(it is brought in line on the next change or dashboard visit)",
                          f"/offers/{o['offer_id']}", "Open", records=[f"offer #{o['offer_id']}"]))

    # ------------------------------------------------------------ positions
    for p in conn.execute("SELECT id, event, selection, event_at FROM positions WHERE status = 'open' "
                          "AND deleted_at IS NULL AND event_at IS NOT NULL AND event_at < ? ORDER BY event_at", (now,)):
        out.append(_issue(f"started:{p['id']}", "act", "Started", p["event"],
                          " · ".join(x for x in (p["selection"], f"started {times.relative(p['event_at'])}",
                                                 "enter the result") if x),
                          f"/positions/{p['id']}", "Open", position_id=p["id"], settle=True,
                          when=times.local(p["event_at"], "%a %H:%M"), records=[f"position #{p['id']}"]))

    for p in metrics.incomplete_positions(conn):
        missing = "lay" if p["backs"] else ("back" if p["lays"] else "back and lay")
        out.append(_issue(f"incomplete:{p['id']}", "fix", "Incomplete", p["event"],
                          f"Missing the {missing} bet · the position can’t show a result until it’s added",
                          f"/positions/{p['id']}", "Add the bet",
                          when=times.local(p["event_at"], "%a %H:%M") if p["event_at"] else "—",
                          records=[f"position #{p['id']}"]))

    for p in conn.execute("SELECT p.id, p.event, sum(l.result <> 'pending') AS done, sum(l.result = 'pending') AS todo "
                          "FROM positions p JOIN legs l ON l.position_id = p.id AND l.deleted_at IS NULL "
                          "WHERE p.deleted_at IS NULL AND p.status = 'open' GROUP BY p.id HAVING done > 0 AND todo > 0"):
        out.append(_issue(f"partly_settled:{p['id']}", "fix", "Half settled", p["event"],
                          f"{p['done']} bet(s) settled, {p['todo']} still open · settle the rest (e.g. the exchange side) "
                          "so the result is complete", f"/positions/{p['id']}", "Open", records=[f"position #{p['id']}"]))

    _position_projection_checks(conn, out)

    # ------------------------------------------------------------ ledger checks
    failed = {c["name"]: c for c in s.checks if not c["ok"] and not c.get("warning")}
    if "Balances explained by capital, funding and profit" in failed:
        out.append(_issue("check:identity", "fix", "Ledger", "Balances don’t add up",
                          failed["Balances explained by capital, funding and profit"]["detail"]
                          + ". Check each account’s real balance to find the difference.",
                          "/accounts/check", "Check balances"))
    if "Every transfer nets to zero" in failed:
        out.append(_issue("check:transfers", "fix", "Ledger", "A transfer doesn’t balance",
                          failed["Every transfer nets to zero"]["detail"] + ". Reverse it and record it again.",
                          "/ledger?category=transfer", "See transfers"))
    for leg in conn.execute(
            "SELECT l.id, l.position_id, p.event, a.name AS account FROM legs l JOIN positions p ON p.id = l.position_id "
            "JOIN accounts a ON a.id = l.account_id WHERE l.deleted_at IS NULL AND p.deleted_at IS NULL AND l.side = 'back' "
            "AND l.free_bet_id IS NULL AND NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.posting_key = "
            "'leg:' || l.id || ':stake' AND e.superseded_at IS NULL)"):
        out.append(_issue(f"stake:{leg['id']}", "fix", "Ledger", leg["event"],
                          f"The {leg['account']} back bet has no stake in the ledger. Open the bet and save it again.",
                          f"/positions/{leg['position_id']}", "Open", records=[f"position #{leg['position_id']}"]))
    for e in conn.execute(
            "SELECT e.id, e.amount_pence, l.position_id FROM ledger_entries e JOIN legs l ON l.id = e.leg_id "
            "WHERE e.superseded_at IS NULL AND l.deleted_at IS NOT NULL"):
        out.append(_issue(f"orphan:{e['id']}", "fix", "Ledger", f"{fmt(e['amount_pence'], signed=True)} on a removed bet",
                          "Money is still attached to a bet that was removed.",
                          f"/ledger?position={e['position_id']}&superseded=1", "See entries"))

    for a in conn.execute("SELECT a.id, a.name, sum(e.amount_pence) AS b FROM accounts a JOIN ledger_entries e "
                          "ON e.account_id = a.id WHERE e.superseded_at IS NULL AND a.kind <> 'bank' GROUP BY a.id "
                          "HAVING b < 0"):
        out.append(_issue(f"negative:{a['id']}", "fix", "Balance", f"{a['name']} is below zero",
                          f"The app has {fmt(a['b'])}. A deposit or a bet is probably missing; check the real balance.",
                          f"/accounts/{a['id']}", "Check it", records=[f"account {a['name']}"]))

    # ------------------------------------------------------------ inbox
    held = conn.execute("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation' "
                        "AND hold_reason IS NOT NULL AND hold_reason <> ''").fetchone()[0]
    held += conn.execute("SELECT count(*) FROM assistant_ops WHERE status = 'held'").fetchone()[0]
    if held:
        out.append(_issue("imports:held", "act", "Inbox", f"{held} assistant item{'s' if held > 1 else ''} waiting for you",
                          "The assistant's entries or changes that need your confirmation. Check and confirm or reject them.",
                          "/imports", "Review"))

    # ------------------------------------------------------------ offers
    cutoff = times.iso(times.now() - timedelta(days=REWARD_WAIT_DAYS))
    for o in conn.execute("SELECT o.id, o.title, o.updated_at, a.name AS account FROM offers o LEFT JOIN accounts a "
                          "ON a.id = o.account_id WHERE o.deleted_at IS NULL AND o.status = 'awaiting_reward' "
                          "AND o.updated_at < ?", (cutoff,)):
        out.append(_issue(f"reward:{o['id']}", "act", "Reward", o["title"],
                          f"{o['account'] or 'The bookmaker'}’s reward hasn’t been recorded "
                          f"{times.relative(o['updated_at']).replace(' ago', '')} after qualifying. Has it arrived?",
                          f"/offers/{o['id']}", "Open offer", records=[f"offer #{o['id']}"]))
    for o in conn.execute("SELECT o.id, o.title FROM offers o WHERE o.deleted_at IS NULL AND o.reward_credited = 1 "
                          "AND NOT EXISTS (SELECT 1 FROM free_bets f WHERE f.offer_id = o.id AND f.deleted_at IS NULL) "
                          "AND NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.offer_id = o.id AND e.class = 'pnl' "
                          "AND e.superseded_at IS NULL AND e.position_id IS NULL)"):
        out.append(_issue(f"reward_missing:{o['id']}", "info", "Reward", o["title"],
                          "Marked as reward received, but no free bet or cash reward is linked to it. Link its free "
                          "bets to the offer (or record the reward).", f"/offers/{o['id']}", "Open offer",
                          records=[f"offer #{o['id']}"]))

    _free_bet_checks(conn, out, now)
    _transfer_checks(conn, out)
    _reconciliation_checks(conn, out)

    # ------------------------------------------------------------ money on its way, balances not checked
    late = times.iso(times.now() - timedelta(days=5))
    for t in conn.execute("SELECT t.id, t.amount_pence, t.at, a.name AS src, b.name AS dst FROM transfers t "
                          "JOIN accounts a ON a.id = t.from_account_id JOIN accounts b ON b.id = t.to_account_id "
                          "WHERE t.voided_at IS NULL AND t.in_transit = 1 AND t.at < ?", (late,)):
        out.append(_issue(f"transit:{t['id']}", "act", "Withdrawal", f"{fmt(t['amount_pence'])} from {t['src']} not arrived",
                          f"Sent to {t['dst']} {times.relative(t['at'])}. Check with {t['src']} if it still hasn’t arrived.",
                          "/accounts#transit", "See it", records=[f"transfer #{t['id']}"]))

    stale_cut = times.iso(times.now() - timedelta(days=CHECK_STALE_DAYS))
    stale = [r["name"] for r in conn.execute(
        "SELECT a.name FROM accounts a WHERE a.kind <> 'bank' AND a.active = 1 AND a.created_at < :cut "
        "AND coalesce((SELECT sum(amount_pence) FROM ledger_entries e WHERE e.account_id = a.id "
        "              AND e.superseded_at IS NULL), 0) <> 0 "
        "AND coalesce((SELECT max(at) FROM reconciliations r WHERE r.account_id = a.id), '') < :cut "
        "ORDER BY a.name COLLATE NOCASE", {"cut": stale_cut})]
    if stale:
        out.append(_issue("balances:stale", "info", "Balances",
                          f"{len(stale)} balance{'s' if len(stale) > 1 else ''} not checked for {CHECK_STALE_DAYS} days",
                          ", ".join(stale[:6]) + (" and more" if len(stale) > 6 else ""),
                          "/accounts/check", "Check balances"))

    for p in commission.promos(conn):
        if p["expired"]:
            out.append(_issue(f"commission:{p['account_id']}", "act", "Commission", p["title"],
                              p["text"] + " Calculations still use it until you record the rate that applies now.",
                              f"/accounts/{p['account_id']}?edit=commission#edit", "Verify rate",
                              records=[f"account {p['name']}"]))

    _plan_checks(conn, out, now)
    _reward_checks(conn, out, now)
    temp_raw = conn.execute("SELECT coalesce(sum(CASE WHEN capital_type = 'temporary_funding' THEN amount_pence END), 0) - "
                            "coalesce(sum(CASE WHEN capital_type = 'temporary_return' THEN temp_portion_pence END), 0) "
                            "FROM transfers WHERE voided_at IS NULL").fetchone()[0]
    if temp_raw < 0:
        out.append(_issue("funding:negative", "fix", "Funding", "More temporary funding returned than borrowed",
                          f"Returns exceed temporary funding by {fmt(-temp_raw)}: a return is probably classified wrongly.",
                          "/accounts#transfers", "See transfers"))

    out = _apply_dismissals(conn, out, include_dismissed)
    out.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    return out


# ---------------------------------------------------------------- dismissed (reviewed) issues

def fingerprint(issue: dict) -> str:
    """The facts an issue was raised on: if they change, a dismissal no longer covers it."""
    basis = {"key": issue["key"], "records": sorted(issue.get("records") or []), "facts": issue.get("facts", issue["text"])}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _apply_dismissals(conn, found: list[dict], include: bool) -> list[dict]:
    active = {r["issue_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM issue_dismissals WHERE reopened_at IS NULL ORDER BY id")}
    out = []
    for i in found:
        i["fingerprint"] = fingerprint(i)
        d = active.get(i["key"])
        if d and d["fingerprint"] == i["fingerprint"]:
            i["dismissed"] = {"reason": d["reason"], "by": d["actor"], "at": d["created_at"], "id": d["id"]}
            if include:
                out.append(i)
            continue
        if d:
            i["reopened"] = {"previously_dismissed": d["reason"], "because": "its facts changed since it was reviewed"}
        out.append(i)
    return out


def dismiss(conn, issue: dict, reason: str) -> int:
    from . import services
    fp = issue.get("fingerprint") or fingerprint(issue)
    old = conn.execute("SELECT id FROM issue_dismissals WHERE issue_key = ? AND reopened_at IS NULL", (issue["key"],)).fetchone()
    if old:
        conn.execute("UPDATE issue_dismissals SET reopened_at = ?, reopen_note = 'superseded by a new review' WHERE id = ?",
                     (times.now_iso(), old[0]))
    cur = conn.execute("INSERT INTO issue_dismissals (issue_key, fingerprint, records_json, reason, actor, request_id, "
                       "created_at) VALUES (?,?,?,?,?,?,?)",
                       (issue["key"], fp, json.dumps(issue.get("records") or []), reason.strip()[:500],
                        services.current_actor(), services._REQUEST.get(), times.now_iso()))
    services.audit(conn, "issue", cur.lastrowid, "dismissed", None, {"issue_key": issue["key"], "title": issue["title"],
                                                                     "records": issue.get("records") or [],
                                                                     "fingerprint": fp}, reason)
    return cur.lastrowid


def reopen(conn, key: str, note: str) -> int:
    from . import services
    n = 0
    for (did,) in conn.execute("SELECT id FROM issue_dismissals WHERE issue_key = ? AND reopened_at IS NULL", (key,)).fetchall():
        conn.execute("UPDATE issue_dismissals SET reopened_at = ?, reopen_note = ? WHERE id = ?", (times.now_iso(), note[:300], did))
        services.audit(conn, "issue", did, "reopened", None, {"issue_key": key}, note)
        n += 1
    return n


def sync_dismissals(conn) -> int:
    """Reopen dismissals whose issue is back with different facts (recorded in the audit trail)."""
    current = {i["key"]: i for i in collect(conn, include_dismissed=True)}
    n = 0
    for d in conn.execute("SELECT * FROM issue_dismissals WHERE reopened_at IS NULL").fetchall():
        i = current.get(d["issue_key"])
        if i and i.get("reopened"):
            n += reopen(conn, d["issue_key"], "its facts changed since it was reviewed")
    return n


def dismissed(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM issue_dismissals ORDER BY id DESC LIMIT 100")]


# ---------------------------------------------------------------- bet plans, rewards

def _plan_checks(conn, out: list, now: str) -> None:
    from . import plans
    soon = times.iso(times.now() + timedelta(hours=2))
    for p in conn.execute("SELECT * FROM bet_plans WHERE status IN ('" + "','".join(plans.ACTIVE) + "') ORDER BY id"):
        rec, title = [f"plan #{p['id']}"], f"Plan #{p['id']}: {p['event']}"
        href = f"/plans/{p['id']}"
        if p["status"] in plans.PRE_PLACEMENT and p["event_at"] and p["event_at"] < now:
            out.append(_issue(f"plan:expired:{p['id']}", "info", "Plan", title, "The event has started and nothing was "
                              "placed: cancel the plan.", href, "Open", records=rec,
                              repairs=[_repair("cancel_bet_plan", "cancel it", plan_id=p["id"], reason="event started")]))
            continue
        if p["status"] == "bookmaker_placed":
            urgent = bool(p["event_at"] and p["event_at"] < soon)
            out.append(_issue(f"plan:unhedged:{p['id']}", "fix" if urgent else "act", "Hedge", title,
                              "The bookmaker bet is placed but nothing is laid yet" + (
                                  f": it starts {times.relative(p['event_at'])}" if p["event_at"] else "") + ".",
                              href, "Open", records=rec))
        full = plans.load(conn, p["id"])
        for l in full["legs"]:
            if l["side"] == "lay" and l["order_stake_pence"] and plans._remaining(l) > 0:
                stale = l["updated_at"] < times.iso(times.now() - timedelta(hours=2))
                out.append(_issue(f"plan:unmatched:{l['id']}", "act", "Hedge", title,
                                  f"{fmt(plans._remaining(l))} of the lay at {l['order_odds']} is still unmatched"
                                  + (" (for over 2 hours)" if stale else "") + ": keep it, cancel it or re-lay at a new price.",
                                  href, "Open", records=rec))
        if p["status"] in ("bookmaker_placed", "exchange_partially_matched") and p["event_at"] and p["event_at"] < now:
            out.append(_issue(f"plan:started_incomplete:{p['id']}", "fix", "Hedge", title,
                              "The event started while the hedge was incomplete: record what matched and settle it.",
                              href, "Open", records=rec))
        if p["status"] in ("validated", "ready_to_place"):
            fund = plans.funding(conn, full)
            if fund["shortfall"] > 0:
                out.append(_issue(f"plan:funds:{p['id']}", "act", "Funding", title,
                                  f"The exchange needs {fmt(fund['shortfall'])} more for the liability before this can be "
                                  "placed (add funding, then validate again).", href, "Open", records=rec))
            rating = plans.rate(conn, full["kind"], plans.figures(conn, full), full["min_retention_pct"])
            if rating.get("meets_minimum") is False:
                out.append(_issue(f"plan:below_target:{p['id']}", "info", "Plan", title,
                                  f"{rating['measure'].replace('_', ' ')} {rating['value']}% no longer meets the target: "
                                  "prices may have moved; recalculate before placing.", href, "Open", records=rec))


def _reward_checks(conn, out: list, now: str) -> None:
    for r in conn.execute("SELECT e.*, a.name AS account, o.title FROM expected_rewards e JOIN accounts a ON a.id = "
                          "e.account_id LEFT JOIN offers o ON o.id = e.offer_id WHERE e.status IN ('waiting', "
                          "'partially_received') AND e.expected_by < ?", (now,)):
        out.append(_issue(f"reward:overdue:{r['id']}", "act", "Reward", r["title"] or f"{r['account']} reward",
                          f"{r['count']} x {fmt(r['each_pence'])} {r['kind'].upper()} was due by {times.local(r['expected_by'])}"
                          f" and hasn't been matched to a free bet: check {r['account']} (or reconcile if it arrived).",
                          f"/offers/{r['offer_id']}" if r["offer_id"] else "/free-bets", "Check",
                          records=[f"expected_reward #{r['id']}"],
                          repairs=[_repair("reconcile_rewards", "match free bets that arrived", expected_reward_id=r["id"])]))
    for r in conn.execute("SELECT e.id, e.count, e.each_pence, count(f.id) AS n, sum(f.face_value_pence <> e.each_pence) AS wrong "
                          "FROM expected_rewards e JOIN free_bets f ON f.expected_reward_id = e.id AND f.deleted_at IS NULL "
                          "AND f.status <> 'void' GROUP BY e.id HAVING n > e.count OR wrong > 0"):
        out.append(_issue(f"reward:mismatch:{r['id']}", "fix", "Reward", f"Expected reward #{r['id']}",
                          f"{r['n']} free bet(s) linked where {r['count']} x {fmt(r['each_pence'])} was expected"
                          + (" (some of a different value)" if r["wrong"] else " (a duplicate?)") + ".",
                          "/free-bets?show=all", "Open", records=[f"expected_reward #{r['id']}"]))
    for o in conn.execute("SELECT o.id, o.title FROM offers o WHERE o.deleted_at IS NULL AND o.reward_kind IS NOT NULL AND "
                          "o.reward_each_pence > 0 AND o.reward_credited = 0 AND o.status NOT IN ('complete', 'expired', "
                          "'abandoned') AND EXISTS (SELECT 1 FROM positions p WHERE p.offer_id = o.id AND p.kind = "
                          "'qualifying' AND p.status = 'settled' AND p.deleted_at IS NULL) AND NOT EXISTS (SELECT 1 FROM "
                          "expected_rewards e WHERE e.offer_id = o.id AND e.status <> 'void')"):
        out.append(_issue(f"reward:not_expected:{o['id']}", "act", "Reward", o["title"],
                          "Its qualifier settled but no reward is expected or recorded.", f"/offers/{o['id']}", "Open",
                          records=[f"offer #{o['id']}"],
                          repairs=[_repair("expect_reward", "expect the reward from its terms", offer_id=o["id"])]))
    week = times.iso(times.now() - timedelta(days=7))
    for f in conn.execute("SELECT f.id, f.face_value_pence, f.kind, f.expires_at, a.name FROM free_bets f JOIN accounts a ON "
                          "a.id = f.account_id WHERE f.status = 'expired' AND f.deleted_at IS NULL AND f.expires_at >= ?", (week,)):
        out.append(_issue(f"freebet:wasted:{f['id']}", "info", "Free bet", f"{f['name']} {fmt(f['face_value_pence'])} "
                          f"{f['kind'].upper()} expired unused", f"Expired {times.local(f['expires_at'])}.", "/free-bets?show=all",
                          "Open", records=[f"free_bet #{f['id']}"]))


# ---------------------------------------------------------------- positions: projections that can't be right

def _position_projection_checks(conn, out: list) -> None:
    from . import imports, exposure
    for p in conn.execute("SELECT id, event, kind FROM positions WHERE status = 'open' AND deleted_at IS NULL"):
        legs = [dict(r) for r in conn.execute("SELECT * FROM legs WHERE position_id = ? AND deleted_at IS NULL", (p["id"],))]
        backs = [l for l in legs if l["side"] == "back"]
        lays = [l for l in legs if l["side"] == "lay"]
        if not backs or not lays or exposure.valid(conn, p["id"]) is not None:
            continue
        win, lose = imports.outcomes({"kind": p["kind"], "legs": legs})
        cash_stakes = sum(l["stake_pence"] for l in backs if not l["free_bet_id"] and p["kind"] == "qualifying")
        if len(lays) > 1 and min(win, lose) < -(cash_stakes + 100):
            out.append(_issue(f"projection:{p['id']}", "info", "Projection", p["event"],
                              f"{len(lays)} lay bets: the projection assumes they all lose together "
                              f"({fmt(min(win, lose), signed=True)}). If they cover different outcomes (e.g. correct "
                              "scores) only one can lose; the real worst case is smaller.",
                              f"/positions/{p['id']}", "Open", records=[f"position #{p['id']}"]))
            continue
        if len(backs) == 1 and len(lays) == 1:
            b, l = backs[0], lays[0]
            kind = {"free_bet_snr": "free_bet_snr", "free_bet_sr": "free_bet_sr"}.get(p["kind"], "qualifying")
            try:
                m = calc.match(kind, b["stake_pence"], Decimal(b["odds"]), Decimal(l["odds"]), l["commission_bp"])
            except (ValueError, ArithmeticError):
                continue
            if m.lay_stake and not (0.7 <= l["stake_pence"] / m.lay_stake <= 1.4):
                out.append(_issue(f"stakes:{p['id']}", "info", "Stakes", p["event"],
                                  f"Lay stake {fmt(l['stake_pence'])} is far from the matched stake "
                                  f"{fmt(m.lay_stake)} for these odds. Check the stakes and odds (or the position type).",
                                  f"/positions/{p['id']}", "Open", records=[f"position #{p['id']}"]))


# ---------------------------------------------------------------- free bets

def _free_bet_checks(conn, out: list, now: str) -> None:
    live = "FROM legs l WHERE l.free_bet_id = f.id AND l.deleted_at IS NULL AND l.result <> 'void' ORDER BY l.id LIMIT 1"
    tokens = [dict(r) for r in conn.execute(
        f"SELECT f.*, a.name AS account, (SELECT l.id {live}) AS leg_id, (SELECT l.position_id {live}) AS position_id, "
        "(SELECT count(*) FROM legs l WHERE l.free_bet_id = f.id AND l.deleted_at IS NOT NULL) AS removed_legs "
        "FROM free_bets f JOIN accounts a ON a.id = f.account_id WHERE f.deleted_at IS NULL")]

    def same(a, b):
        return (a["account_id"], a["face_value_pence"], a["kind"]) == (b["account_id"], b["face_value_pence"], b["kind"])

    for fb in tokens:
        label = f"{fb['account']} {fmt(fb['face_value_pence'])} {fb['kind'].upper()} free bet #{fb['id']}"
        if fb["status"] == "used" and not fb["leg_id"]:
            twins = [t for t in tokens if t["id"] != fb["id"] and same(t, fb) and t["leg_id"]
                     and t["status"] == "used" and _info_score(t) < 2]
            repairs, text = [], ("Marked used but not linked to any bet"
                                 + (" (the bet that used it was removed)" if fb["removed_legs"] else "") + ".")
            for t in twins:
                keep, drop = (fb, t) if _info_score(fb) > _info_score(t) else (t, fb)
                repairs.append(_repair("merge_duplicate_free_bets",
                                       f"#{t['id']} (used on position #{t['position_id']}) looks like the same free bet: "
                                       f"keep #{keep['id']}, void #{drop['id']}",
                                       keep_free_bet_id=keep["id"], duplicate_free_bet_id=drop["id"],
                                       reason=f"duplicate records of one {fb['account']} free bet"))
            for leg in conn.execute("SELECT l.id, l.position_id, p.event FROM legs l JOIN positions p ON p.id = l.position_id "
                                    "WHERE l.deleted_at IS NULL AND p.deleted_at IS NULL AND l.side = 'back' AND "
                                    "l.free_bet_id IS NULL AND p.kind IN ('free_bet_snr', 'free_bet_sr') AND "
                                    "l.account_id = ? AND l.stake_pence = ?", (fb["account_id"], fb["face_value_pence"])):
                repairs.append(_repair("link_free_bet_to_position", f"link it to position #{leg['position_id']} "
                                       f"({leg['event']}), recorded as a cash bet", free_bet_id=fb["id"],
                                       position_id=leg["position_id"], leg_id=leg["id"],
                                       reason="the free bet this bet used"))
            repairs.append(_repair("void_free_bet", "it was never really used: void it", free_bet_id=fb["id"],
                                   reason="marked used but no bet used it"))
            if twins:
                text += " Another record of the same free bet is linked to a bet: probably a duplicate."
            out.append(_issue(f"freebet:unlinked:{fb['id']}", "fix", "Free bet", label, text,
                              f"/free-bets?show=all#fb-{fb['id']}", "Open", records=[f"free_bet #{fb['id']}"]
                              + [f"free_bet #{t['id']}" for t in twins], repairs=repairs))
        elif fb["status"] == "available" and fb["leg_id"]:
            out.append(_issue(f"freebet:status:{fb['id']}", "fix", "Free bet", label,
                              f"Linked to position #{fb['position_id']} but still marked available.",
                              f"/positions/{fb['position_id']}", "Open", records=[f"free_bet #{fb['id']}"]))
        elif fb["status"] == "available" and fb["expires_at"] and fb["expires_at"] < now:
            out.append(_issue(f"freebet:expired:{fb['id']}", "info", "Free bet", label,
                              f"Expired {times.local(fb['expires_at'])} but still counted as available.",
                              "/free-bets", "Open", records=[f"free_bet #{fb['id']}"],
                              repairs=[_repair("update_free_bet", "mark it expired", free_bet_id=fb["id"], status="expired",
                                               reason="passed its expiry unused")]))
        if fb["offer_id"]:
            oacc = conn.execute("SELECT account_id, title FROM offers WHERE id = ?", (fb["offer_id"],)).fetchone()
            if oacc and oacc["account_id"] and oacc["account_id"] != fb["account_id"]:
                out.append(_issue(f"freebet:offer:{fb['id']}", "fix", "Free bet", label,
                                  f"Linked to “{oacc['title']}”, an offer from a different bookmaker.",
                                  "/free-bets?show=all", "Open", records=[f"free_bet #{fb['id']}", f"offer #{fb['offer_id']}"],
                                  repairs=[_repair("update_free_bet", "remove the wrong offer link", free_bet_id=fb["id"],
                                                   clear_offer=True, reason="offer from a different bookmaker")]))

    # likely double entries: same bookmaker, value, type, notes, entered within two minutes, neither used
    unused = sorted((t for t in tokens if t["status"] == "available" and not t["leg_id"]), key=lambda t: t["id"])
    for i, a in enumerate(unused):
        for b in unused[i + 1:]:
            if same(a, b) and (a["notes"] or "") == (b["notes"] or "") and a["notes"] and a["expires_at"] == b["expires_at"] \
                    and abs((times.parse(b["created_at"]) - times.parse(a["created_at"])).total_seconds()) < 120:
                count = conn.execute("SELECT reward_count FROM offers WHERE id = ?", (a["offer_id"],)).fetchone() \
                    if a["offer_id"] else None
                if count and count[0] and count[0] > 1:
                    continue                                    # several identical tokens from one reward are normal
                out.append(_issue(f"freebet:double:{b['id']}", "info", "Free bet", f"{a['account']} free bets #{a['id']} "
                                  f"and #{b['id']}", "Same bookmaker, value, expiry and notes, entered within two "
                                  "minutes: possibly recorded twice. Leave both if you really have two.",
                                  "/free-bets", "Open", records=[f"free_bet #{a['id']}", f"free_bet #{b['id']}"],
                                  repairs=[_repair("void_free_bet", f"void #{b['id']} (the second record)",
                                                   free_bet_id=b["id"], reason=f"recorded twice (same as #{a['id']})")]))
    for o in conn.execute("SELECT o.id, o.title, o.reward_count, count(f.id) AS n FROM offers o JOIN free_bets f ON "
                          "f.offer_id = o.id AND f.deleted_at IS NULL AND f.status <> 'void' WHERE o.deleted_at IS NULL "
                          "AND o.reward_count > 0 GROUP BY o.id HAVING n > o.reward_count"):
        out.append(_issue(f"freebet:too_many:{o['id']}", "info", "Free bet", o["title"],
                          f"{o['n']} free bets are linked to this offer, which gives {o['reward_count']}: look for a "
                          "duplicate.", f"/offers/{o['id']}", "Open offer", records=[f"offer #{o['id']}"]))

    for p in conn.execute("SELECT p.id, p.event, l.id AS leg_id, l.account_id, l.stake_pence FROM positions p JOIN legs l "
                          "ON l.position_id = p.id AND l.deleted_at IS NULL WHERE p.deleted_at IS NULL AND p.kind IN "
                          "('free_bet_snr', 'free_bet_sr') AND l.side = 'back' AND l.free_bet_id IS NULL"):
        out.append(_issue(f"position:no_token:{p['leg_id']}", "fix", "Free bet", p["event"],
                          f"A free-bet position, but its back bet #{p['leg_id']} is recorded as a cash stake (no free bet "
                          "linked), so its stake is counted as spent.", f"/positions/{p['id']}", "Open",
                          records=[f"position #{p['id']}"]))
    for p in conn.execute("SELECT position_id, count(*) AS n FROM legs WHERE deleted_at IS NULL AND free_bet_id IS NOT NULL "
                          "GROUP BY position_id HAVING n > 1"):
        out.append(_issue(f"position:tokens:{p['position_id']}", "info", "Free bet", f"Position #{p['position_id']}",
                          f"{p['n']} free bets are used on one position. Fine if you really placed several.",
                          f"/positions/{p['position_id']}", "Open", records=[f"position #{p['position_id']}"]))


# ---------------------------------------------------------------- transfers

def _transfer_checks(conn, out: list) -> None:
    from . import services
    for b in conn.execute("SELECT id, name FROM accounts WHERE kind = 'bank'"):
        outstanding = services.outstanding_temporary(conn, b["id"])
        if not outstanding:
            continue
        first = conn.execute("SELECT min(at) FROM transfers WHERE voided_at IS NULL AND capital_type = 'temporary_funding' "
                             "AND from_account_id = ?", (b["id"],)).fetchone()[0]
        for t in conn.execute("SELECT t.*, a.name AS src FROM transfers t JOIN accounts a ON a.id = t.from_account_id "
                              "WHERE t.voided_at IS NULL AND t.to_account_id = ? AND t.capital_type IN "
                              "('bankroll_withdrawal', 'profit_withdrawal') AND t.at >= ? AND t.amount_pence <= ?",
                              (b["id"], first or "", outstanding)):
            out.append(_issue(f"transfer:return:{t['id']}", "act", "Funding",
                              f"{fmt(outstanding)} temporary funding still out to {b['name']}",
                              f"{fmt(t['amount_pence'])} from {t['src']} to {b['name']} (transfer #{t['id']}) is recorded "
                              "as a bankroll withdrawal. If it paid back the borrowed money, it is a temporary return.",
                              "/accounts#borrowed", "See it", records=[f"transfer #{t['id']}"],
                              repairs=[_repair("reclassify_transfer", f"transfer #{t['id']} was the temporary funding "
                                               "being paid back", transfer_id=t["id"], funding_type="temporary_return",
                                               reason="it repaid the temporary funding")]))
    rows = conn.execute("SELECT * FROM transfers WHERE voided_at IS NULL ORDER BY at, id").fetchall()
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if (b["from_account_id"], b["to_account_id"], b["amount_pence"]) == (a["from_account_id"], a["to_account_id"],
                                                                                 a["amount_pence"]) and \
                    abs((times.parse(b["at"]) - times.parse(a["at"])).total_seconds()) <= 900:
                if _reviewed_separate(a["notes"], b["id"]) or _reviewed_separate(b["notes"], a["id"]):
                    continue                    # the records say they were checked and are separate transfers
                out.append(_issue(f"transfer:double:{b['id']}", "info", "Funding",
                                  f"Transfers #{a['id']} and #{b['id']} look the same",
                                  f"{fmt(a['amount_pence'])} between the same accounts within 15 minutes. Reverse one "
                                  "if it was recorded twice; leave both if you really moved it twice.",
                                  "/accounts", "See them", records=[f"transfer #{a['id']}", f"transfer #{b['id']}"],
                                  repairs=[_repair("reverse_transfer", f"reverse #{b['id']} (the second one)",
                                                   transfer_id=b["id"], reason=f"recorded twice (same as #{a['id']})"),
                                           _repair("dismiss_issue", "both are genuine: mark it reviewed",
                                                   issue_key=f"transfer:double:{b['id']}",
                                                   reason="genuine separate transfers")],
                                  facts=[[x["id"], x["amount_pence"], x["at"], x["from_account_id"], x["to_account_id"],
                                          x["capital_type"]] for x in (a, b)]))


_SEPARATE = re.compile(r"not\s+(?:a\s+)?duplicate|genuine(?:ly)?\s+separate|separate\s+transfer", re.I)


def _reviewed_separate(notes: str, other_id: int) -> bool:
    """Notes that say the transfer was checked and is not a duplicate (of the other one, or in general)."""
    text = notes or ""
    if not _SEPARATE.search(text):
        return False
    mentioned = re.findall(r"#(\d+)", text)
    return not mentioned or str(other_id) in mentioned


# ---------------------------------------------------------------- reconciliations

def _liability_at(conn, account_id: int, at: str) -> int:
    rows = conn.execute("SELECT l.stake_pence, l.odds FROM legs l WHERE l.account_id = ? AND l.side = 'lay' AND "
                        "l.deleted_at IS NULL AND l.placed_at <= ? AND (l.result = 'pending' OR l.settled_at > ?)",
                        (account_id, at, at)).fetchall()
    return sum(calc.lay_liability(r[0], Decimal(r[1])) for r in rows)


def _reconciliation_checks(conn, out: list) -> None:
    for r in conn.execute("SELECT r.*, a.name AS account, a.kind, e.superseded_at AS entry_gone, e.amount_pence AS entry_amount "
                          "FROM reconciliations r JOIN accounts a ON a.id = r.account_id LEFT JOIN ledger_entries e ON "
                          "e.id = r.entry_id ORDER BY r.id"):
        rec = f"reconciliation #{r['id']}"
        title = f"{r['account']} balance check #{r['id']} ({fmt(r['difference_pence'], signed=True)})"
        if r["outcome"] == "unresolved":
            out.append(_issue(f"reconciliation:unresolved:{r['id']}", "act", "Balance", title,
                              f"Observed {r['basis']} {fmt(r['actual_balance_pence'])}, app expected "
                              f"{fmt(r['app_balance_pence'])}. Not explained by recorded transfers, bets or lay liability; "
                              "nothing was posted to profit. Look for a missing deposit, withdrawal or settlement, then "
                              "record the difference or dismiss it.", f"/accounts/{r['account_id']}", "Open",
                              records=[rec], repairs=[
                                  _repair("resolve_reconciliation", "record it as a profit/loss adjustment",
                                          reconciliation_id=r["id"], action="record_adjustment",
                                          reason="unexplained difference confirmed by the user"),
                                  _repair("resolve_reconciliation", "dismiss it (timing or already fixed)",
                                          reconciliation_id=r["id"], action="dismiss", reason="explained elsewhere")]))
            continue
        if r["outcome"] == "adjusted" and r["entry_id"] and r["entry_gone"]:
            out.append(_issue(f"reconciliation:withdrawn:{r['id']}", "info", "Balance", title,
                              "Its profit/loss adjustment was already withdrawn, but the balance check still says "
                              "adjusted. Mark it reversed so the history reads correctly.", f"/accounts/{r['account_id']}",
                              "Open", records=[rec], repairs=[
                                  _repair("reverse_reconciliation", "mark it reversed (no money moves)",
                                          reconciliation_id=r["id"], reason="its adjustment had already been withdrawn")]))
            continue
        if r["outcome"] != "adjusted" or not r["entry_id"] or r["entry_gone"]:
            continue
        diff = r["difference_pence"]
        reverse = _repair("reverse_reconciliation", f"reverse it (removes the {fmt(diff, signed=True)} P&L adjustment)",
                          reconciliation_id=r["id"], reason="not a real profit or loss")
        window = (times.iso(times.parse(r["at"]) - timedelta(days=3)), times.iso(times.parse(r["at"]) + timedelta(days=3)))
        match = conn.execute("SELECT id, amount_pence, capital_type FROM transfers WHERE voided_at IS NULL AND "
                             "(from_account_id = ? OR to_account_id = ?) AND abs(amount_pence - ?) <= 1 AND at BETWEEN ? AND ?",
                             (r["account_id"], r["account_id"], abs(diff), *window)).fetchone()
        liability = r["liability_pence"] or (_liability_at(conn, r["account_id"], r["at"]) if r["kind"] == "exchange" else 0)
        if match:
            out.append(_issue(f"reconciliation:transfer:{r['id']}", "fix", "Balance", title,
                              f"The {fmt(diff, signed=True)} adjustment equals transfer #{match['id']} "
                              f"({fmt(match['amount_pence'])}) around the same time: the deposit/withdrawal is probably "
                              "counted twice, once as capital and once as profit/loss.", f"/accounts/{r['account_id']}",
                              "Open", records=[rec, f"transfer #{match['id']}"], repairs=[reverse]))
        elif r["kind"] == "exchange" and r["basis"] == "total" and liability and abs(diff + liability) <= 50:
            out.append(_issue(f"reconciliation:liability:{r['id']}", "fix", "Balance", title,
                              f"The {fmt(diff, signed=True)} adjustment is about the {fmt(liability)} lay liability held at "
                              "the time: the figure read was probably the exchange's AVAILABLE balance, which excludes "
                              "liability. Not a real loss.", f"/accounts/{r['account_id']}", "Open", records=[rec],
                              repairs=[reverse]))
        elif abs(diff) >= LARGE_RECONCILIATION:
            out.append(_issue(f"reconciliation:large:{r['id']}", "info", "Balance", title,
                              f"A {fmt(diff, signed=True)} profit/loss adjustment from a balance check. Make sure it is a "
                              "real gain or loss, not a missing transfer, bet or settlement.", f"/accounts/{r['account_id']}",
                              "Open", records=[rec], repairs=[reverse]))


def counts(issues: list[dict]) -> dict:
    return {sev: sum(1 for i in issues if i["severity"] == sev) for sev in SEVERITY_ORDER}
