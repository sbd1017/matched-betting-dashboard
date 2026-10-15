"""SQLite access and versioned migrations (bets/migrations/NNNN_name.sql, applied in order)."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def tx(self):
        """One atomic unit of work: BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any error)."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> list[str]:
        """Apply pending migrations; returns the names applied now."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                         "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            applied = []
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                version = int(path.name[:4])
                if version in done:
                    continue
                sql = path.read_text()
                if re.search(r"\b(BEGIN|COMMIT)\b", sql, re.I):
                    raise RuntimeError(f"{path.name}: migrations must not manage transactions")
                # A table rebuild of a table other tables reference (SQLite's documented procedure) needs foreign
                # keys off; that pragma only works outside a transaction, so the migration asks for it explicitly.
                fk_off = sql.lstrip().startswith("-- pragma: foreign_keys=off")
                if fk_off:
                    conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _statements(sql):
                        conn.execute(statement)
                    if fk_off and conn.execute("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError(f"{path.name}: foreign key check failed")
                    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (version, path.stem))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    if fk_off:
                        conn.execute("PRAGMA foreign_keys = ON")
                applied.append(path.stem)
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise RuntimeError(f"foreign key check failed: {len(fk)} rows")
            return applied
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
            return row[0] or 0


def _statements(sql: str):
    """Split a migration into statements. Comments (whole-line and trailing) are removed first;
    migrations must not put '--' or ';' inside string literals."""
    body = re.sub(r"--[^\n]*", "", sql)
    for part in body.split(";"):
        if part.strip():
            yield part
