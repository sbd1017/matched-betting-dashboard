#!/bin/bash
# Assistant Intake token (for ChatGPT): candidates only, never your /bets/ login.
#   scripts/assistant-token.sh create|rotate          new token -> ~/.config/matched-betting-assistant-token.txt (0600)
#   scripts/assistant-token.sh create|rotate --print  new token shown once in this terminal instead
#   scripts/assistant-token.sh disable|status         turn it off at once / show whether it is on (never the token)
#   scripts/assistant-token.sh openapi                OpenAPI description to paste into the GPT's Actions
set -euo pipefail
APP=/opt/matched-betting
TARGET="$HOME/.config/matched-betting-assistant-token.txt"
cd "$APP"
run() { sudo -u bets "$APP/.venv/bin/python" -m bets.cli "$@"; }
case "${1:-}" in
  create|rotate)
    if [ "${2:-}" = "--print" ]; then run assistant-token "$1" -; exit 0; fi
    install -d -m 0700 "$HOME/.config"
    umask 077
    if run assistant-token "$1" - > "$TARGET.tmp"; then
      mv "$TARGET.tmp" "$TARGET"; chmod 0600 "$TARGET"
      echo "Token written to $TARGET (mode 0600). Put it in ChatGPT and your password manager, then delete the file."
    else
      rm -f "$TARGET.tmp"; exit 1
    fi ;;
  disable|status) run assistant-token "$1" ;;
  openapi) run assistant-openapi ;;
  *) sed -n 2,6p "$0"; exit 1 ;;
esac
