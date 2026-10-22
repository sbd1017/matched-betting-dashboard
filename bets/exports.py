"""Exports so you are never locked in: CSV ledger, CSV positions, full JSON, and .ics deadlines."""
from __future__ import annotations

import csv
import io
import json
import uuid

from . import offer_state, services, times
from .money import fmt, plain

LEDGER_COLUMNS = ["id", "at", "account", "account_kind", "category", "class", "amount", "description", "position_id",
                  "leg_id", "offer_id", "transfer_id", "free_bet_id", "status", "superseded_at", "superseded_by",
                  "supersede_note"]


def ledger_csv(conn, include_superseded: bool = True) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(LEDGER_COLUMNS)
    sql = ("SELECT e.*, a.name AS account, a.kind AS account_kind FROM ledger_entries e JOIN accounts a ON a.id = e.account_id"
           + ("" if include_superseded else " WHERE e.superseded_at IS NULL") + " ORDER BY e.at, e.id")
    for r in conn.execute(sql):
        w.writerow([r["id"], r["at"], r["account"], r["account_kind"], r["category"], r["class"], plain(r["amount_pence"]),
                    _safe(r["description"]), r["position_id"], r["leg_id"], r["offer_id"], r["transfer_id"],
                    r["free_bet_id"], "superseded" if r["superseded_at"] else "active", r["superseded_at"] or "",
                    r["superseded_by"] or "", _safe(r["supersede_note"])])
    return out.getvalue()


def positions_csv(conn) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "created_at", "event", "market", "selection", "event_at", "kind", "status", "offer",
                "expected_pnl", "actual_pnl", "legs", "notes"])
    rows = conn.execute("SELECT p.*, o.title AS offer FROM positions p LEFT JOIN offers o ON o.id = p.offer_id "
                        "WHERE p.deleted_at IS NULL ORDER BY p.created_at, p.id").fetchall()
    for p in rows:
        legs = conn.execute("SELECT l.*, a.name AS account FROM legs l JOIN accounts a ON a.id = l.account_id "
                            "WHERE l.position_id = ? AND l.deleted_at IS NULL ORDER BY l.id", (p["id"],)).fetchall()
        leg_text = "; ".join(f"{l['side']} {l['account']} {plain(l['stake_pence'])}@{l['odds']}"
                             + (" (free bet)" if l["free_bet_id"] else "") + f" {l['result']}" for l in legs)
        actual = services.position_pnl(conn, p["id"]) if p["status"] != "open" else None
        w.writerow([p["id"], p["created_at"], _safe(p["event"]), _safe(p["market"]), _safe(p["selection"]),
                    p["event_at"] or "", p["kind"], p["status"], _safe(p["offer"] or ""),
                    plain(p["expected_pnl_pence"]) if p["expected_pnl_pence"] is not None else "",
                    plain(actual) if actual is not None else "", _safe(leg_text), _safe(p["notes"])])
    return out.getvalue()


def _safe(text) -> str:
    """Stop spreadsheet formula injection from free-text fields."""
    text = "" if text is None else str(text)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


TABLES = ("accounts", "offers", "free_bets", "positions", "legs", "transfers", "ledger_entries", "reconciliations",
          "import_candidates", "audit_log", "schema_migrations")


def full_json(conn) -> str:
    """Everything in the database (there are no credentials in it). Money stays in integer pence."""
    data = {"format": "matched-betting-export", "version": 1, "exported_at": times.now_iso(), "money_unit": "pence",
            "tables": {}}
    for table in TABLES:
        data["tables"][table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    return json.dumps(data, indent=1, sort_keys=True)


# ---------------------------------------------------------------- calendar (.ics)

def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_time(value: str) -> str:
    return times.parse(value).strftime("%Y%m%dT%H%M%SZ")


def ics(events: list[dict], name: str = "Matched betting") -> str:
    """events: {uid, title, at (UTC iso), description}. Titles never include balances or profit."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//example//matched-betting//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_ics_escape(name)}"]
    stamp = _ics_time(times.now_iso())
    for ev in events:
        lines += ["BEGIN:VEVENT", f"UID:{ev['uid']}@bets.example.com", f"DTSTAMP:{stamp}",
                  f"DTSTART:{_ics_time(ev['at'])}", f"DTEND:{_ics_time(ev['at'])}",
                  f"SUMMARY:{_ics_escape(ev['title'])}"]
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{_ics_escape(ev['description'])}")
        if ev.get("alarm", True):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(ev['title'])}", "TRIGGER:-PT2H",
                      "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def _fold(line: str) -> str:
    out, data = [], line.encode()
    while len(data) > 75:
        cut = 75
        while (data[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(data[:cut].decode())
        data = b" " + data[cut:]
    out.append(data.decode())
    return "\r\n".join(out)


def deadline_events(conn, kind: str | None = None, item_id: int | None = None) -> list[dict]:
    events = []
    now = times.now_iso()
    if kind in (None, "free_bet"):
        sql = ("SELECT f.*, a.name AS account FROM free_bets f JOIN accounts a ON a.id = f.account_id "
               "WHERE f.deleted_at IS NULL AND f.expires_at IS NOT NULL")
        params: list = []
        if item_id:
            sql += " AND f.id = ?"
            params.append(item_id)
        else:
            sql += " AND f.status = 'available' AND f.expires_at >= ?"
            params.append(now)
        for f in conn.execute(sql, params):
            events.append({"uid": f"free-bet-{f['id']}", "at": f["expires_at"],
                           "title": f"Free bet expires: {f['account']}", "description": "Matched betting free bet"})
    if kind in (None, "offer"):
        sql = "SELECT o.*, a.name AS account FROM offers o LEFT JOIN accounts a ON a.id = o.account_id " \
              "WHERE o.deleted_at IS NULL AND o.expires_at IS NOT NULL"
        params = []
        if item_id:
            sql += " AND o.id = ?"
            params.append(item_id)
        else:
            sql += " AND o.status IN ('not_started', 'qualifying') AND o.expires_at >= ?"
            params.append(now)
        for o in conn.execute(sql, params):
            if not item_id and not offer_state.deadline_matters(conn, dict(o)):
                continue                         # qualifier placed or reward in: the deadline no longer needs you
            events.append({"uid": f"offer-{o['id']}", "at": o["expires_at"],
                           "title": f"Offer deadline: {o['title']}", "description": o["account"] or ""})
    if kind in (None, "return"):
        from . import outlook                    # temporary funding with a date to go back to the bank
        for d in outlook.temporary_dues(conn):
            if d["due_at"] and (not item_id or d["transfer_id"] == item_id) and (item_id or d["due_at"] >= now):
                events.append({"uid": f"return-{d['transfer_id']}", "at": d["due_at"],
                               "title": f"Return {fmt(d['remaining'])} to {d['bank']}",
                               "description": f"Temporary funding sent to {d['account']}"})
    if kind in (None, "position"):
        sql = "SELECT * FROM positions WHERE deleted_at IS NULL AND event_at IS NOT NULL"
        params = []
        if item_id:
            sql += " AND id = ?"
            params.append(item_id)
        else:
            sql += " AND status = 'open' AND event_at >= ?"
            params.append(now)
        for p in conn.execute(sql, params):
            events.append({"uid": f"position-{p['id']}", "at": p["event_at"], "title": f"Starts: {p['event']}",
                           "description": p["selection"] or "", "alarm": False})
    if kind in (None, "commission"):
        from . import commission                 # promotional commission rates ending (shown until the day itself)
        for p in commission.promos(conn):
            if (item_id and p["account_id"] != item_id) or (not item_id and p["days"] < 0):
                continue
            events.append({"uid": f"commission-{p['account_id']}", "at": p["expires_at"],
                           "title": f"{p['name']} {p['rate']} commission expires",
                           "description": "Promotional rate ends: verify the rate that applies afterwards",
                           "alarm": False, "all_day": True})
    return sorted(events, key=lambda e: e["at"])


def new_uid() -> str:
    return uuid.uuid4().hex
