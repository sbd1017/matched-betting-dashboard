"""Plugin access commands (run as the MCP service user via scripts/mcp-access.sh):

    python -m bets_mcp.cli connect-code          # one-time code to approve a ChatGPT connection (15 min)
    python -m bets_mcp.cli status                # on/off, linked or not, connections (never secrets)
    python -m bets_mcp.cli revoke                # disconnect every ChatGPT connection now
    python -m bets_mcp.cli disable | enable      # switch the plugin off (also revokes) / on
    python -m bets_mcp.cli store-assistant-token # stdin: Assistant Intake token (from bets.cli), stored 0600
"""
from __future__ import annotations

import os
import re
import sys
import time

from .config import load_settings
from .store import Store


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit(__doc__)
    s = load_settings()
    s.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(s.db_path)
    cmd = argv[0]
    if cmd == "connect-code":
        if not store.enabled():
            sys.exit("The plugin is disabled; run 'enable' first.")
        code = store.new_connect_code(s.connect_code_ttl)
        print(f"Connection code (works once, for 15 minutes): {code}\n"
              "Type it on the 'Connect Matched Betting Assistant' page that opens when ChatGPT connects.")
    elif cmd == "status":
        linked = s.assistant_token_file.exists()
        print(f"Plugin: {'ON' if store.enabled() else 'OFF'} · Assistant Intake link: {'yes' if linked else 'NO'}")
        print(f"MCP URL: {s.resource}")
        for g in store.grants():
            state = f"revoked {_when(g['revoked_at'])}" if g["revoked_at"] else f"active, last used {_when(g['last_used_at'])}"
            print(f"  connection {g['id']} ({g['client_name']}) since {_when(g['created_at'])}: {state}")
    elif cmd == "revoke":
        print(f"Disconnected {store.revoke_all()} connection(s). Reconnect from ChatGPT with a new connection code.")
    elif cmd == "disable":
        store.set_enabled(False)
        print("Plugin switched OFF and all connections revoked.")
    elif cmd == "enable":
        store.set_enabled(True)
        print("Plugin switched ON. Connect from ChatGPT with a new connection code.")
    elif cmd == "store-assistant-token":
        m = re.search(r"\bmbai_[0-9a-f]{8}_[A-Za-z0-9_-]{43}\b", sys.stdin.read())
        if not m:
            sys.exit("No Assistant Intake token on stdin; nothing changed.")
        path = s.assistant_token_file
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(m.group(0))
        os.replace(tmp, path)
        print("Assistant Intake token stored for the plugin (mode 0600). It is never shown.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
