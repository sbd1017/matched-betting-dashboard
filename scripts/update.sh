#!/bin/bash
# Update to the newest version without touching your data.
#
# Everything you enter lives in var/ (database, login, backups). Git never tracks it, and the app upgrades its own
# database when it starts, so updating is: back up, pull the new code, install anything new, restart.
#
#   scripts/update.sh      (run it from anywhere; on a server install it asks for sudo where it needs to)
set -euo pipefail
cd "$(dirname "$0")/.."
SVC=bets                  # the app's own user on a server install (scripts/install.sh)
UNIT=matched-betting      # its systemd service

if systemctl cat "$UNIT.service" >/dev/null 2>&1; then MODE=server; else MODE=local; fi
as_app() { if [ "$MODE" = server ]; then sudo -u "$SVC" "$@"; else "$@"; fi; }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Some of the project's files have been changed here, and updating would overwrite them."
  echo "Undo or commit these first:"
  git status --short --untracked-files=no
  exit 1
fi
[ -x .venv/bin/python ] || { echo "There's no .venv here yet. Set it up first (see the README)."; exit 1; }

before=$(git rev-parse --short HEAD)
echo "1/4 Backing up your data"
as_app .venv/bin/python -m bets.cli backup --keep 30
echo "2/4 Getting the new version"
git pull --ff-only
after=$(git rev-parse --short HEAD)
if [ "$before" = "$after" ]; then
  echo "You're already on the newest version."
  exit 0
fi
echo "3/4 Installing requirements and upgrading the database"
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
if [ -x .venv-mcp/bin/pip ]; then .venv-mcp/bin/pip install --quiet --disable-pip-version-check -r requirements-mcp.txt; fi
as_app .venv/bin/python -m bets.cli migrate
echo "4/4 Restarting"
if [ "$MODE" = server ]; then
  sudo systemctl restart "$UNIT"
  if systemctl cat "$UNIT-mcp.service" >/dev/null 2>&1; then sudo systemctl restart "$UNIT-mcp"; fi
  for _ in $(seq 1 20); do curl -fsS -o /dev/null http://127.0.0.1:8760/bets/healthz && break; sleep 1; done
  systemctl is-active "$UNIT"
else
  echo "If the app is running, stop it (Ctrl+C) and start it again: .venv/bin/python -m bets.serve"
fi
echo "Updated from $before to $after. Your data wasn't touched, and there's a backup in var/backups."
