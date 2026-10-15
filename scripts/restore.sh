#!/bin/bash
# Restore the database from a backup file in var/backups. Usage: sudo scripts/restore.sh bets-YYYYMMDDTHHMMSSZ.sqlite3
# The current database is kept as var/bets.sqlite3.before-restore-<time>.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
APP=/opt/matched-betting
NAME=$(basename "${1:?backup file name from var/backups}")
SRC="$APP/var/backups/$NAME"
[ -f "$SRC" ] || { echo "No such backup: $NAME"; ls "$APP/var/backups"; exit 1; }
CHECK=$(python3 -c "import sqlite3,sys; print(sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchone()[0])" "$SRC")
[ "$CHECK" = "ok" ] || { echo "Backup fails its integrity check; not restoring."; exit 1; }
systemctl stop matched-betting.service
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
for f in bets.sqlite3 bets.sqlite3-wal bets.sqlite3-shm; do
  [ -e "$APP/var/$f" ] && mv "$APP/var/$f" "$APP/var/$f.before-restore-$STAMP"
done
install -o bets -g bets -m 0600 "$SRC" "$APP/var/bets.sqlite3"
systemctl start matched-betting.service
echo "Restored $NAME (previous database kept with suffix .before-restore-$STAMP)."
