"""Settings from the environment (systemd unit / .env). Secrets live in the data directory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = PROJECT_ROOT / "var"
    host: str = "127.0.0.1"
    port: int = 8760
    base_path: str = "/bets"                      # the app always lives under this prefix
    public_origins: tuple = ("https://example.com",)
    secure_cookies: bool = True                   # HTTPS through Cloudflare/nginx
    session_hours: int = 24 * 14
    trusted_proxy: str = "127.0.0.1"              # only nginx on this host may supply X-Real-IP
    login_max_failures: int = 5
    login_window_seconds: int = 900

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bets.sqlite3"

    @property
    def auth_file(self) -> Path:
        return self.data_dir / "auth.json"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def assistant_file(self) -> Path:
        return self.data_dir / "assistant.json"      # hash of the Assistant Intake token (never the token)

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_settings() -> Settings:
    env = os.environ
    s = Settings()
    if env.get("BETS_DATA_DIR"):
        s.data_dir = Path(env["BETS_DATA_DIR"])
    s.host = env.get("BETS_HOST", s.host)
    s.port = int(env.get("BETS_PORT", s.port))
    if env.get("BETS_PUBLIC_ORIGINS"):
        s.public_origins = tuple(o.strip().rstrip("/") for o in env["BETS_PUBLIC_ORIGINS"].split(",") if o.strip())
    s.secure_cookies = env.get("BETS_SECURE_COOKIES", "1") != "0"
    return s
