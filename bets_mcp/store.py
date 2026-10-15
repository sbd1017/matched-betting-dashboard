"""OAuth state for the plugin, in its own SQLite file (mcp-var/, 0700, the MCP service user).

Every secret (connection codes, authorization codes, access and refresh tokens) is stored only as
a SHA-256 hash. A *grant* is one approval of one ChatGPT connection; revoking it kills all its
tokens. Nothing here is betting data.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info_json TEXT NOT NULL, created_at REAL NOT NULL,
                                    last_used_at REAL);
CREATE TABLE IF NOT EXISTS consent_requests (id_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, params_json TEXT NOT NULL,
                                             expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, grant_id TEXT NOT NULL,
                                  params_json TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, client_name TEXT NOT NULL,
                                   created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS tokens (token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
                                   grant_id TEXT NOT NULL, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
                                   resource TEXT, expires_at REAL NOT NULL, used_at REAL);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens (grant_id);
CREATE TABLE IF NOT EXISTS connect_codes (code_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                                          used_at REAL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# Connection codes are typed by a person: no 0/O/1/I/L.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def normalise_code(text: str) -> str:
    return "".join(ch for ch in (text or "").upper() if ch in CODE_ALPHABET)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------ on/off

    def enabled(self) -> bool:
        with self.tx() as c:
            r = c.execute("SELECT value FROM state WHERE key = 'enabled'").fetchone()
        return r is None or r[0] == "1"

    def set_enabled(self, on: bool) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO state (key, value) VALUES ('enabled', ?) ON CONFLICT(key) DO UPDATE SET value = "
                      "excluded.value", ("1" if on else "0",))
            if not on:
                self._revoke_all(c)

    # ------------------------------------------------------------ connection codes (typed on the consent page)

    def new_connect_code(self, ttl: int) -> str:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(20))       # ~99 bits
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM connect_codes WHERE expires_at < ? OR used_at IS NOT NULL", (now - 86400,))
            c.execute("INSERT INTO connect_codes (code_hash, created_at, expires_at) VALUES (?, ?, ?)",
                      (digest(raw), now, now + ttl))
        return "-".join(raw[i:i + 5] for i in range(0, 20, 5))

    def use_connect_code(self, typed: str) -> bool:
        code = normalise_code(typed)
        if len(code) != 20:
            return False
        with self.tx() as c:
            cur = c.execute("UPDATE connect_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL "
                            "AND expires_at > ?", (time.time(), digest(code), time.time()))
            return cur.rowcount == 1

    # ------------------------------------------------------------ clients (dynamic registration)

    def get_client(self, client_id: str) -> dict | None:
        with self.tx() as c:
            r = c.execute("SELECT info_json FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def add_client(self, client_id: str, info: dict, max_clients: int, per_hour: int) -> bool:
        now = time.time()
        with self.tx() as c:
            # Forget registrations that never completed a connection after a day.
            c.execute("DELETE FROM clients WHERE last_used_at IS NULL AND created_at < ? AND client_id NOT IN "
                      "(SELECT client_id FROM grants)", (now - 86400,))
            recent = c.execute("SELECT count(*) FROM clients WHERE created_at > ?", (now - 3600,)).fetchone()[0]
            total = c.execute("SELECT count(*) FROM clients").fetchone()[0]
            if recent >= per_hour or total >= max_clients:
                return False
            c.execute("INSERT INTO clients (client_id, info_json, created_at) VALUES (?, ?, ?)",
                      (client_id, json.dumps(info), now))
        return True

    # ------------------------------------------------------------ consent requests -> codes -> tokens

    def new_consent(self, client_id: str, params: dict, ttl: int) -> str:
        rid = new_secret()
        with self.tx() as c:
            c.execute("DELETE FROM consent_requests WHERE expires_at < ?", (time.time(),))
            c.execute("INSERT INTO consent_requests (id_hash, client_id, params_json, expires_at) VALUES (?, ?, ?, ?)",
                      (digest(rid), client_id, json.dumps(params), time.time() + ttl))
        return rid

    def get_consent(self, rid: str) -> tuple[str, dict] | None:
        with self.tx() as c:
            r = c.execute("SELECT client_id, params_json FROM consent_requests WHERE id_hash = ? AND expires_at > ?",
                          (digest(rid or ""), time.time())).fetchone()
        return (r[0], json.loads(r[1])) if r else None

    def drop_consent(self, rid: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM consent_requests WHERE id_hash = ?", (digest(rid or ""),))

    def new_grant_and_code(self, client_id: str, client_name: str, params: dict, ttl: int) -> str:
        code = new_secret()
        gid = secrets.token_hex(6)
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO grants (id, client_id, client_name, created_at) VALUES (?, ?, ?, ?)",
                      (gid, client_id, client_name[:80], now))
            c.execute("INSERT INTO codes (code_hash, client_id, grant_id, params_json, expires_at) VALUES (?, ?, ?, ?, ?)",
                      (digest(code), client_id, gid, json.dumps(params), now + ttl))
            c.execute("UPDATE clients SET last_used_at = ? WHERE client_id = ?", (now, client_id))
        return code

    def get_code(self, code: str) -> dict | None:
        with self.tx() as c:
            r = c.execute("SELECT * FROM codes WHERE code_hash = ? AND expires_at > ?", (digest(code), time.time())).fetchone()
        return dict(r) if r else None

    def take_code(self, code: str) -> dict | None:
        """Single use: the row is deleted as it is read."""
        with self.tx() as c:
            r = c.execute("SELECT * FROM codes WHERE code_hash = ? AND expires_at > ?", (digest(code), time.time())).fetchone()
            c.execute("DELETE FROM codes WHERE code_hash = ?", (digest(code),))
            if r and c.execute("SELECT revoked_at FROM grants WHERE id = ?", (r["grant_id"],)).fetchone()[0]:
                return None
        return dict(r) if r else None

    def issue_tokens(self, grant_id: str, client_id: str, scopes: list[str], resource: str | None,
                     access_ttl: int, refresh_ttl: int) -> tuple[str, str]:
        access, refresh = new_secret("mbma_"), new_secret("mbmr_")
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM tokens WHERE expires_at < ?", (now - 86400,))
            for token, kind, ttl in ((access, "access", access_ttl), (refresh, "refresh", refresh_ttl)):
                c.execute("INSERT INTO tokens (token_hash, kind, grant_id, client_id, scopes, resource, expires_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (digest(token), kind, grant_id, client_id, " ".join(scopes),
                                                          resource, now + ttl))
        return access, refresh

    def find_token(self, token: str, kind: str) -> dict | None:
        """A live token of a live grant. Re-presenting a used refresh token revokes the whole grant."""
        with self.tx() as c:
            r = c.execute("SELECT t.*, g.revoked_at FROM tokens t JOIN grants g ON g.id = t.grant_id "
                          "WHERE t.token_hash = ? AND t.kind = ?", (digest(token or ""), kind)).fetchone()
            if not r or r["revoked_at"] or r["expires_at"] < time.time():
                return None
            if r["used_at"]:
                c.execute("UPDATE grants SET revoked_at = ? WHERE id = ?", (time.time(), r["grant_id"]))
                return None
            if kind == "access":
                c.execute("UPDATE grants SET last_used_at = ? WHERE id = ?", (time.time(), r["grant_id"]))
        return dict(r)

    def mark_refresh_used(self, token: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE tokens SET used_at = ? WHERE token_hash = ? AND kind = 'refresh' AND used_at IS NULL",
                            (time.time(), digest(token)))
            return cur.rowcount == 1

    def revoke_grant_of(self, token: str) -> None:
        with self.tx() as c:
            r = c.execute("SELECT grant_id FROM tokens WHERE token_hash = ?", (digest(token or ""),)).fetchone()
            if r:
                c.execute("UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), r[0]))

    def _revoke_all(self, c) -> int:
        n = c.execute("UPDATE grants SET revoked_at = ? WHERE revoked_at IS NULL", (time.time(),)).rowcount
        c.execute("DELETE FROM codes")
        c.execute("DELETE FROM consent_requests")
        c.execute("DELETE FROM tokens")
        return n

    def revoke_all(self) -> int:
        with self.tx() as c:
            return self._revoke_all(c)

    def grants(self) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT id, client_name, created_at, last_used_at, revoked_at FROM grants "
                                               "ORDER BY created_at DESC LIMIT 20")]
