#!/bin/bash
# Assistant Intake tokens: candidates only, never your /bets/ login. One token per client, each revocable alone.
#   scripts/assistant-token.sh create [LABEL]     add a token (default label ChatGPT); the others keep working
#   scripts/assistant-token.sh rotate [ID]        new secret for that token (same id); its old secret stops at once
#        (create/rotate write the token to ~/.config/matched-betting-assistant-token.txt (0600);
#         add --print to show it once in this terminal instead)
#   scripts/assistant-token.sh disable [ID]       turn that token off at once; the others keep working
#   scripts/assistant-token.sh status             every token: id, label, dates, on/off (never a secret)
#   scripts/assistant-token.sh openapi            OpenAPI description to paste into the GPT's Actions
#   ID may be left out when there is only one token.
set -euo pipefail
APP=/opt/matched-betting
TARGET="$HOME/.config/matched-betting-assistant-token.txt"
cd "$APP"
run() { sudo -u bets "$APP/.venv/bin/python" -m bets.cli "$@"; }
usage() { sed -n 2,10p "$0"; exit 1; }
action="${1:-}"
[ $# -gt 0 ] && shift
print=0; ref=()
for a in "$@"; do
  if [ "$a" = "--print" ]; then print=1; else ref+=("$a"); fi
done
[ "${#ref[@]}" -le 1 ] || usage
case "$action" in
  create|rotate)
    if [ "$print" = 1 ]; then run assistant-token "$action" - ${ref[@]+"${ref[@]}"}; exit 0; fi
    install -d -m 0700 "$HOME/.config"
    umask 077
    if run assistant-token "$action" - ${ref[@]+"${ref[@]}"} > "$TARGET.tmp"; then
      mv "$TARGET.tmp" "$TARGET"; chmod 0600 "$TARGET"
      echo "Token written to $TARGET (mode 0600). Give it to its client and your password manager, then delete the file."
    else
      rm -f "$TARGET.tmp"; exit 1
    fi ;;
  disable) run assistant-token disable ${ref[@]+"${ref[@]}"} ;;
  status) run assistant-token status ;;
  openapi) run assistant-openapi ;;
  *) usage ;;
esac
