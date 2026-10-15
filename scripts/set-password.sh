#!/bin/bash
# Choose a new /bets login password (prompted; never echoed or logged). Usage: scripts/set-password.sh [username]
set -euo pipefail
APP=/opt/matched-betting
cd "$APP"
sudo -u bets "$APP/.venv/bin/python" -m bets.cli set-password "$@"
