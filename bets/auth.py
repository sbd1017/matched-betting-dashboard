"""Login for /bets: its own username/password (not the shared private-apps login).

* Password: scrypt hash in ``var/auth.json`` (0600, service user). Never in git.
* Session: signed cookie (HMAC-SHA256, key in ``var/secret.key``), HttpOnly, Secure,
  SameSite=Strict, Path=/bets, absolute expiry. It also carries the CSRF token.
* Failed logins are limited per client IP and overall.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

N, R, P = 2 ** 14, 8, 1
COOKIE = "bets_session"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Use at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(N, R, P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def load_credentials(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
        if data.get("username") and data.get("password_hash"):
            return data
    except (OSError, ValueError):
        pass
    return None


def write_credentials(path: Path, username: str, password: str) -> None:
    username = username.strip()
    if not username or len(username) > 40:
        raise ValueError("Username must be 1-40 characters")
    data = {"username": username, "password_hash": hash_password(password), "changed_at": int(time.time())}
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)   # first run: the data directory may not exist yet
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_secret(path: Path) -> bytes:
    path = Path(path)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) < 32:
        raise RuntimeError("session key file is too short")
    return key


class SessionCodec:
    def __init__(self, key: bytes, max_age_seconds: int):
        self.key, self.max_age = key, max_age_seconds

    def _sig(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.key, payload, hashlib.sha256).digest()).decode().rstrip("=")

    def dumps(self, data: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        return payload + "." + self._sig(payload.encode())

    def loads(self, token: str | None) -> dict | None:
        if not token or "." not in token or len(token) > 2048:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(payload.encode())):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        return data

    def new(self, user: str | None = None) -> dict:
        now = int(time.time())
        return {"u": user, "csrf": secrets.token_urlsafe(24), "iat": now, "exp": now + self.max_age}


class LoginLimiter:
    """Refuse login attempts after too many failures from one IP, or overall (spread guessing)."""

    def __init__(self, per_ip: int = 5, window: int = 900, overall: int = 30):
        self.per_ip, self.window, self.overall = per_ip, window, overall
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        recent = [t for t in self.failures.get(key, []) if now - t < self.window]
        self.failures[key] = recent
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.per_ip or len(self._recent("*")) >= self.overall

    def fail(self, ip: str) -> None:
        for key in (ip, "*"):
            self.failures.setdefault(key, []).append(time.time())

    def reset(self, ip: str) -> None:
        self.failures.pop(ip, None)
