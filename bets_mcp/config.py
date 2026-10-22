"""Settings from the environment (systemd unit). Secrets live in the data directory, never here."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# OpenAI's OAuth callbacks (https://developers.openai.com/plugins/build/auth): ChatGPT's stable URI (used
# now that issuer identification is advertised), its per-connector callback (connections made before), and the
# OpenAI app review tool's callback, which ChatGPT includes in the same registration request.
# Nothing else may receive an authorization code.
CHATGPT_REDIRECTS = (re.compile(r"^https://chatgpt\.com/connector_platform_oauth_redirect$"),
                     re.compile(r"^https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}$"),
                     re.compile(r"^https://platform\.openai\.com/apps-manage/oauth$"))
SCOPE = "bets.intake"


@dataclass
class Settings:
    data_dir: Path = PROJECT_ROOT / "mcp-var"
    host: str = "127.0.0.1"
    port: int = 8762
    public_origin: str = "https://example.com"
    mcp_path: str = "/bets/mcp"                        # resource (MCP endpoint) and OAuth issuer
    upstream: str = "http://127.0.0.1:8760/bets/api/assistant"
    trusted_proxy: str = "127.0.0.1"
    extra_redirects: tuple = ()                        # tests only (BETS_MCP_TEST_REDIRECT)
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 24 * 3600
    code_ttl: int = 300
    consent_ttl: int = 900
    connect_code_ttl: int = 900
    calls_per_minute: int = 20                         # per connection; one person typing
    calls_per_day: int = 400
    max_clients: int = 20
    registrations_per_hour: int = 10
    allowed_hosts: list = field(default_factory=list)
    # Client registration: CIMD (ChatGPT's client_id is its https://chatgpt.com/oauth/[<callback_id>/]client.json,
    # fetched and validated; bets_mcp/cimd.py). Dynamic registration (DCR) is off. Tokens: public client + PKCE.
    cimd: bool = True
    dcr: bool = False
    token_auth_methods: tuple = ("none",)

    @property
    def resource(self) -> str:
        return self.public_origin + self.mcp_path

    @property
    def issuer(self) -> str:
        return self.resource

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mcp.sqlite3"

    @property
    def assistant_token_file(self) -> Path:
        return self.data_dir / "assistant-token"

    def redirect_allowed(self, uri: str) -> bool:
        return any(p.match(uri) for p in CHATGPT_REDIRECTS) or uri in self.extra_redirects


def load_settings() -> Settings:
    env = os.environ
    s = Settings()
    if env.get("BETS_MCP_DATA_DIR"):
        s.data_dir = Path(env["BETS_MCP_DATA_DIR"])
    s.port = int(env.get("BETS_MCP_PORT", s.port))
    s.public_origin = env.get("BETS_MCP_PUBLIC_ORIGIN", s.public_origin).rstrip("/")
    s.upstream = env.get("BETS_MCP_UPSTREAM", s.upstream).rstrip("/")
    if env.get("BETS_MCP_TEST_REDIRECT"):
        s.extra_redirects = (env["BETS_MCP_TEST_REDIRECT"],)
    host = s.public_origin.split("://", 1)[1]
    s.allowed_hosts = [host, f"127.0.0.1:{s.port}", f"localhost:{s.port}"]
    return s
