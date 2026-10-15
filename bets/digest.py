"""Telegram: a morning summary and alerts for things about to cost money.

Run by systemd timers as the service user (deploy/matched-betting-digest.*):

    python -m bets.cli digest            # print the morning summary
    python -m bets.cli digest --send     # send it
    python -m bets.cli alerts --send     # send alerts not sent before (free bets expiring within 6 hours,
                                         # bets started 3+ hours ago and still open, money due back today)

The bot token and chat id live in var/telegram.json (0600, written by scripts/telegram-setup.sh), never in
the code or the database. The main web service cannot reach the internet (IPAddressDeny), so only the timer's
own unit is allowed out, and only to send these messages. Messages contain figures, so they go only to the one
chat id configured.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from . import exports, issues, metrics, outlook, services, times
from .money import fmt

ALERT_SENT_KEY = "telegram_alerts_sent"
EXPIRY_ALERT = timedelta(hours=6)
STARTED_ALERT = timedelta(hours=3)


def config_path(settings) -> Path:
    return settings.data_dir / "telegram.json"


def load_config(settings) -> dict | None:
    p = config_path(settings)
    if not p.exists():
        return None
    cfg = json.loads(p.read_text())
    return cfg if cfg.get("bot_token") and cfg.get("chat_id") else None


def save_config(settings, bot_token: str, chat_id: str) -> None:
    import os
    p = config_path(settings)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"bot_token": bot_token.strip(), "chat_id": str(chat_id).strip()}, f)


def send(settings, text: str) -> None:
    cfg = load_config(settings)
    if not cfg:
        raise SystemExit("Telegram is not set up: run scripts/telegram-setup.sh")
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:              # noqa: S310 (fixed https host)
        if r.status != 200:
            raise SystemExit(f"Telegram refused the message (HTTP {r.status})")


def _url(settings, path: str) -> str:
    origin = settings.public_origins[0] if settings.public_origins else ""
    return f"{origin}{settings.base_path}{path}"


def morning(conn, settings) -> str:
    """Today at a glance: what needs doing, what's coming, yesterday's profit."""
    now = times.now()
    s = metrics.summary(conn)
    lines = [f"Matched betting · {now.astimezone(times.UK).strftime('%a %-d %b')}"]
    todo = []
    tomorrow = times.iso(now + timedelta(hours=36))
    for f in conn.execute("SELECT f.face_value_pence, f.expires_at, a.name FROM free_bets f JOIN accounts a ON "
                          "a.id = f.account_id WHERE f.status = 'available' AND f.deleted_at IS NULL "
                          "AND f.expires_at IS NOT NULL AND f.expires_at <= ? ORDER BY f.expires_at", (tomorrow,)):
        todo.append(f"• Use {f['name']} {fmt(f['face_value_pence'])} free bet: expires {times.local(f['expires_at'])}")
    for i in issues.collect(conn, s):
        if i["key"].startswith("started:"):
            todo.append(f"• Settle {i['title']} (started {i['when']})")
    for d in outlook.temporary_dues(conn):
        if d["due_at"] and d["due_at"] <= times.iso(now + timedelta(days=3)):
            short = max(0, d["remaining"] - max(s.available, 0))
            todo.append(f"• Return {fmt(d['remaining'])} to {d['bank']} by {times.local(d['due_at'])}"
                        + (f" ({fmt(short)} short until bets settle)" if short else ""))
    for o in conn.execute("SELECT title FROM offers WHERE deleted_at IS NULL AND status = 'awaiting_reward' "
                          "AND reward_credited = 0"):
        todo.append(f"• Check whether the reward for {o['title']} has arrived")
    pending = conn.execute("SELECT count(*) FROM import_candidates WHERE status = 'needs_confirmation'").fetchone()[0]
    if pending:
        todo.append(f"• Confirm {pending} imported entr{'ies' if pending > 1 else 'y'}")
    fix = [i for i in issues.collect(conn, s) if i["severity"] == "fix"]
    if fix:
        todo.append(f"• {len(fix)} thing{'s' if len(fix) > 1 else ''} to fix: " + "; ".join(i["title"] for i in fix[:3]))
    lines.append("")
    lines += (["To do"] + todo) if todo else ["Nothing needs you today."]
    upcoming = [e for e in exports.deadline_events(conn) if e["uid"].startswith("position-") and e["at"] <= tomorrow]
    if upcoming:
        lines += ["", "Kick-offs"] + [f"• {times.local(e['at'], '%a %H:%M')} {e['title'].replace('Starts: ', '')}"
                                     for e in upcoming[:6]]
    yesterday = metrics.realised_profit(conn, times.iso(now - timedelta(days=1)))
    come = outlook.still_to_come(conn)
    lines += ["", f"Profit {fmt(s.profit_lifetime, signed=True)} · last 24 h {fmt(yesterday, signed=True)} · "
                  f"this month {fmt(s.profit_month, signed=True)}",
              f"Still to come {fmt(come['total'], signed=True)} · available to bet {fmt(s.available)}",
              "", _url(settings, "/")]
    return "\n".join(lines)


def alerts(conn, settings) -> list[tuple[str, str]]:
    """(key, text) for things about to cost money that have not been alerted yet."""
    now = times.now()
    sent = services.get_setting(conn, ALERT_SENT_KEY, {}) or {}
    out = []
    for f in conn.execute("SELECT f.id, f.face_value_pence, f.expires_at, a.name FROM free_bets f JOIN accounts a ON "
                          "a.id = f.account_id WHERE f.status = 'available' AND f.deleted_at IS NULL "
                          "AND f.expires_at IS NOT NULL AND f.expires_at BETWEEN ? AND ?",
                          (times.iso(now), times.iso(now + EXPIRY_ALERT))):
        out.append((f"expiry:{f['id']}", f"⏰ {f['name']} {fmt(f['face_value_pence'])} free bet expires "
                                         f"{times.relative(f['expires_at'])} ({times.local(f['expires_at'], '%H:%M')}). "
                                         + _url(settings, "/free-bets")))
    for p in conn.execute("SELECT id, event, event_at FROM positions WHERE status = 'open' AND deleted_at IS NULL "
                          "AND event_at IS NOT NULL AND event_at <= ?", (times.iso(now - STARTED_ALERT),)):
        out.append((f"started:{p['id']}", f"✅ {p['event']} started {times.relative(p['event_at'])}: enter the result. "
                                          + _url(settings, f"/positions/{p['id']}")))
    today_end = times.iso(now + timedelta(hours=18))
    for d in outlook.temporary_dues(conn):
        if d["due_at"] and d["due_at"] <= today_end:
            out.append((f"due:{d['transfer_id']}:{d['remaining']}",
                        f"🏦 Return {fmt(d['remaining'])} to {d['bank']} by {times.local(d['due_at'], '%a %H:%M')}. "
                        + _url(settings, "/accounts#borrowed")))
    return [(k, t) for k, t in out if k not in sent]


def mark_sent(conn, keys: list[str]) -> None:
    cut = times.iso(times.now() - timedelta(days=30))
    sent = {k: v for k, v in (services.get_setting(conn, ALERT_SENT_KEY, {}) or {}).items() if v >= cut}
    sent.update({k: times.now_iso() for k in keys})
    services.set_setting(conn, ALERT_SENT_KEY, sent)
