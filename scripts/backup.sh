#!/bin/bash
# Take a consistent database backup now (also runs daily via matched-betting-backup.timer).
set -euo pipefail
APP=/opt/matched-betting
cd "$APP"
sudo -u bets "$APP/.venv/bin/python" -m bets.cli backup --keep 30
