#!/bin/bash
# ChatGPT plugin (Matched Betting Assistant, MCP) access. Never prints a stored secret.
#   scripts/mcp-access.sh connect-code     one-time code to approve a ChatGPT connection (15 minutes)
#   scripts/mcp-access.sh status           on/off, link to the Assistant Intake API, connections
#   scripts/mcp-access.sh revoke           disconnect every ChatGPT connection now
#   scripts/mcp-access.sh disable|enable   switch the plugin off (revokes everything) / on
#   scripts/mcp-access.sh link-assistant   give the plugin a NEW Assistant Intake token (old one stops working)
set -euo pipefail
APP=/opt/matched-betting
cd "$APP"
mcp() { sudo -u bets-mcp "$APP/.venv-mcp/bin/python" -m bets_mcp.cli "$@"; }
case "${1:-}" in
  connect-code|status|revoke|disable|enable) mcp "$1" ;;
  link-assistant)
    # The token goes straight from the betting app's CLI into the plugin's 0600 file; it is never shown.
    sudo -u bets "$APP/.venv/bin/python" -m bets.cli assistant-token rotate - 2>/dev/null | mcp store-assistant-token ;;
  *) sed -n 2,8p "$0"; exit 1 ;;
esac
