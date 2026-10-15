"""Admin commands (run as the service user, e.g. via scripts/*.sh):

    python -m bets.cli migrate
    python -m bets.cli set-password [username]      # prompts; nothing is echoed or logged
    python -m bets.cli init-login FILE [username]   # first install: random password written to FILE (0600)
    python -m bets.cli backup [--keep N]
    python -m bets.cli assistant-token create|rotate FILE|-   # Assistant Intake token, shown once (- = stdout)
    python -m bets.cli assistant-token disable|status
    python -m bets.cli assistant-openapi                      # OpenAPI description to paste into ChatGPT
    python -m bets.cli digest [--send]                        # Telegram morning summary (print or send)
    python -m bets.cli alerts [--send]                        # Telegram alerts not sent before
    python -m bets.cli telegram-config                        # reads bot token and chat id from stdin (setup script)
    python -m bets.cli audit                                  # dry run: everything that looks wrong, and the repairs
    python -m bets.cli audit --apply KEY [KEY ...]            # apply the first offered repair of these issues (audited
                                                              # as 'system'); never applied without being named
"""
from __future__ import annotations

import getpass
import os
import secrets
import sqlite3
import sys
from pathlib import Path

import json

from . import assistant, services, times
from .auth import load_credentials, write_credentials
from .config import load_settings
from .db import Database


AUDIT_SECTIONS = (
    ("Free bets used without a bet (orphans / unmatched used tokens)", ("freebet:unlinked:",)),
    ("Likely duplicate free bets", ("freebet:double:", "freebet:too_many:")),
    ("Free bets with other problems", ("freebet:status:", "freebet:expired:", "freebet:offer:", "position:no_token:",
                                       "position:tokens:")),
    ("Open positions whose event has finished", ("started:", "partly_settled:")),
    ("Positions whose figures look wrong", ("projection:", "stakes:", "incomplete:")),
    ("Temporary funding anomalies", ("transfer:return:",)),
    ("Suspicious balance-check profit/loss", ("reconciliation:",)),
    ("Possible duplicate transfers", ("transfer:double:",)),
    ("Impossible balances and ledger problems", ("negative:", "check:", "stake:", "orphan:")),
)


def audit_records(settings, apply_keys: list[str]) -> None:
    """Historical audit: print what looks wrong, grouped, with the repair each issue offers. With --apply, apply the
    first offered repair of each named issue (and only those), as a 'system' change with an audit trail."""
    from . import issues, metrics, ops
    db = Database(settings.db_path)
    with db.read() as conn:
        found = issues.collect(conn)
        s = metrics.summary(conn)
    print(f"Audit at {times.now_iso()} · realised profit {s.profit_lifetime / 100:.2f} · ledger checks "
          f"{'pass' if s.consistent else 'FAIL'} · {len(found)} issue(s)")
    shown = set()
    for title, prefixes in AUDIT_SECTIONS:
        items = [i for i in found if i["key"].startswith(prefixes)]
        print(f"\n== {title}: {len(items) or 'none'}")
        for i in items:
            shown.add(i["key"])
            print(f"  [{i['severity']}] {i['key']}: {i['title']}\n      {i['text']}")
            for r in i["repairs"]:
                print(f"      repair: {r['change']} {json.dumps(r['params'], sort_keys=True)} — {r['label']}")
    rest = [i for i in found if i["key"] not in shown]
    if rest:
        print("\n== Other")
        for i in rest:
            print(f"  [{i['severity']}] {i['key']}: {i['title']} — {i['text']}")
    if not apply_keys:
        print("\nDry run: nothing was changed.")
        return
    by_key = {i["key"]: i for i in found}
    for key in apply_keys:
        issue = by_key.get(key)
        if not issue or not issue["repairs"]:
            print(f"\n{key}: no such issue with a repair; skipped")
            continue
        repair = issue["repairs"][0]
        params = {**repair["params"], "reason": "historical repair: " + repair["params"].get("reason", repair["label"])}
        with db.tx() as conn:
            result = ops.run(conn, repair["change"], params, actor="system", request_id=f"audit:{key}")
        print(f"\n{key}: {result['status']} — {result['summary']}")
        for rec in result.get("records", []):
            print(f"   {rec['record']}: {json.dumps(rec.get('before'), sort_keys=True)[:300]}")
            print(f"   {' ' * len(rec['record'])}  → {json.dumps(rec.get('after'), sort_keys=True)[:300]}")
        if result.get("totals"):
            print("   totals:", json.dumps(result["totals"], sort_keys=True))


def migrate(settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    applied = Database(settings.db_path).migrate()
    print("migrations applied:", ", ".join(applied) if applied else "none (up to date)")


def set_password(settings, username: str | None) -> None:
    current = load_credentials(settings.auth_file)
    username = username or (current or {}).get("username") or input("Username: ").strip()
    first = getpass.getpass("New password (12+ characters): ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords did not match; nothing changed.")
    write_credentials(settings.auth_file, username, first)
    print(f"Password updated for '{username}'. Existing sessions stay valid until they expire; "
          "delete var/secret.key and restart the service to sign everything out.")


def init_login(settings, target: str, username: str) -> None:
    if load_credentials(settings.auth_file):
        sys.exit("A login already exists; use set-password to change it.")
    password = secrets.token_urlsafe(18)
    write_credentials(settings.auth_file, username, password)
    text = ("Matched betting (https://example.com/bets/)\n"
            f"Username: {username}\nPassword: {password}\n\n"
            "Move this into your password manager, then delete this file.\n")
    if target == "-":                       # install script pipes this into a file only you can read
        sys.stdout.write(text)
        return
    target_path = Path(target)
    fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    print(f"Login created for '{username}'. Password written to {target_path} (mode 0600).")


def backup(settings, keep: int = 14) -> Path:
    """Consistent online copy (SQLite backup API) into var/backups; keeps the newest `keep`."""
    settings.backup_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = times.now().strftime("%Y%m%dT%H%M%SZ")
    target = settings.backup_dir / f"bets-{stamp}.sqlite3"
    src = sqlite3.connect(settings.db_path)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    check = sqlite3.connect(target).execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        target.unlink()
        sys.exit(f"backup failed integrity check: {check}")
    old = sorted(settings.backup_dir.glob("bets-*.sqlite3"))[:-keep]
    for p in old:
        p.unlink()
    print(target)
    return target


def _audit_token(settings, action: str, token_id) -> None:
    db = Database(settings.db_path)
    db.migrate()
    with db.tx() as conn:
        services.audit(conn, "assistant_token", None, action, None, {"id": token_id}, "command line")


def assistant_token(settings, action: str, target: str | None) -> None:
    path = settings.assistant_file
    current = assistant.status(path)
    if action == "status":
        if current["enabled"]:
            print(f"Assistant token {current['id']} is ENABLED (issued {current['created_at']}).")
        elif current["id"]:
            print(f"Assistant token {current['id']} is DISABLED (since {current['disabled_at']}).")
        else:
            print("No assistant token has been issued.")
        return
    if action == "disable":
        tid = assistant.disable(path)
        if tid:
            _audit_token(settings, "disabled", tid)
        print(f"Assistant token {tid} disabled; it stops working immediately." if tid else "No token was enabled.")
        return
    if action not in ("create", "rotate") or not target:
        sys.exit(__doc__)
    if action == "create" and current["enabled"]:
        sys.exit(f"Token {current['id']} is already enabled; use 'rotate' to replace it (the old one stops working).")
    token = assistant.issue(path)
    tid = assistant.status(path)["id"]
    _audit_token(settings, "rotated" if current["enabled"] else "issued", tid)
    origin = settings.public_origins[0] if settings.public_origins else ""
    text = ("Matched betting — Assistant Intake token (for ChatGPT)\n"
            f"Token: {token}\n"
            f"API:   {origin}{settings.base_path}/api/assistant  (Authorization: Bearer <token>)\n\n"
            "In ChatGPT (custom GPT → Actions): Authentication → API key → Auth type: Bearer → paste the token.\n"
            "Then keep it in your password manager and DELETE this file.\n"
            "It can only submit candidates that you confirm in /bets/imports. It cannot read balances or profit.\n"
            "Replace it: scripts/assistant-token.sh rotate · Turn it off: scripts/assistant-token.sh disable "
            "(or Settings → Disable in the app).\n")
    if target == "-":
        sys.stdout.write(text)
    else:
        fd = os.open(Path(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    sys.stderr.write(f"Assistant token {tid} issued" + (" (the previous token no longer works)" if current["enabled"]
                                                         else "") + ".\n")


def _load_currency(settings) -> None:
    """Admin output (digest, alerts) uses the dashboard's display currency."""
    if settings.db_path.exists():
        from . import money
        try:
            with Database(settings.db_path).read() as conn:
                money.set_currency(services.get_setting(conn, "currency", None))
        except Exception:                                   # noqa: BLE001 - display only; never block a command
            pass


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    settings = load_settings()
    _load_currency(settings)
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd == "migrate":
        migrate(settings)
    elif cmd == "set-password":
        set_password(settings, args[0] if args else None)
    elif cmd == "init-login" and args:
        init_login(settings, args[0], args[1] if len(args) > 1 else "admin")
    elif cmd == "assistant-token" and args:
        assistant_token(settings, args[0], args[1] if len(args) > 1 else None)
    elif cmd == "assistant-openapi":
        origin = settings.public_origins[0] if settings.public_origins else ""
        print(json.dumps(assistant.openapi(f"{origin}{settings.base_path}/api/assistant"), indent=2))
    elif cmd in ("digest", "alerts", "telegram-config"):
        from . import digest
        db = Database(settings.db_path)
        if cmd == "telegram-config":
            token, chat = (sys.stdin.readline().strip(), sys.stdin.readline().strip())
            if not token or not chat.lstrip("-").isdigit():
                sys.exit("Give the bot token, then the numeric chat id.")
            digest.save_config(settings, token, chat)
            print("Telegram settings saved.")
        elif cmd == "digest":
            with db.read() as conn:
                text = digest.morning(conn, settings)
            digest.send(settings, text) if "--send" in args else print(text)
        else:
            with db.read() as conn:
                found = digest.alerts(conn, settings)
            for key, text in found:
                if "--send" in args:
                    digest.send(settings, text)
                else:
                    print(text)
            if found and "--send" in args:
                with db.tx() as conn:
                    digest.mark_sent(conn, [k for k, _ in found])
    elif cmd == "backup":
        keep = int(args[args.index("--keep") + 1]) if "--keep" in args else 14
        backup(settings, keep)
    elif cmd == "audit":
        audit_records(settings, args[args.index("--apply") + 1:] if "--apply" in args else [])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
