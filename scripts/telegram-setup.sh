#!/usr/bin/env bash
# Telegram morning summary and alerts: store the bot token and chat id (prompted, never echoed or logged)
# in var/telegram.json, readable only by the service user, then send a test message.
#   scripts/telegram-setup.sh           set up / replace
#   scripts/telegram-setup.sh test      send the morning summary now
#   scripts/telegram-setup.sh remove    stop (deletes the config)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
RUN=(sudo -u bets "$PY" -m bets.cli)
case "${1:-setup}" in
  setup)
    read -r -s -p "Bot token (from @BotFather): " token; echo
    read -r -p "Chat id (your user id, e.g. from @userinfobot): " chat
    printf '%s\n%s\n' "$token" "$chat" | "${RUN[@]}" telegram-config
    "${RUN[@]}" digest --send && echo "Sent a test summary."
    echo "Now enable the timers: sudo systemctl enable --now matched-betting-digest.timer matched-betting-alerts.timer"
    ;;
  test) "${RUN[@]}" digest --send && echo "Sent." ;;
  remove) sudo -u bets rm -f var/telegram.json && echo "Removed. Disable the timers too:" \
            && echo "sudo systemctl disable --now matched-betting-digest.timer matched-betting-alerts.timer" ;;
  *) echo "usage: $0 [setup|test|remove]"; exit 2 ;;
esac
