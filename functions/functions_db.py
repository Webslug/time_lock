"""SQLite schema, connection helper and the vault secret."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import functions_config as cfg
from . import functions_log as log

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    label         TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    unlock_hidden BLOB NOT NULL,
    wrapped_key   BLOB NOT NULL,
    payload       BLOB NOT NULL,
    b_concealed   INTEGER NOT NULL DEFAULT 1,
    reveal_count  INTEGER NOT NULL DEFAULT 0,
    exported_path TEXT,
    label_enc     BLOB
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
"""


class VaultProblem(Exception):
    """The key file or database cannot be used. The message is written for a human."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "missing_key", "damaged_key", "damaged_database"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or cfg.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)  # guarded by TimeLockStore lock
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        if "label_enc" not in columns:          # databases made before Blind mode existed
            conn.execute("ALTER TABLE items ADD COLUMN label_enc BLOB")
            conn.commit()
    except sqlite3.DatabaseError as exc:
        log.failure("database cannot be opened: %s", exc)
        raise VaultProblem("damaged_database",
                           f"The database file {path} could not be opened. It may be damaged.") from exc
    return conn


def load_vault(conn: sqlite3.Connection, key_path: Path | None = None) -> bytes:
    """Read the vault secret, creating it on first run. If items already exist but the
    secret is gone they are unrecoverable, so refuse to silently invent a new one."""
    path = Path(key_path or cfg.VAULT_KEY_PATH)
    if path.exists():
        secret = path.read_bytes()
        if len(secret) != 32:
            log.failure("vault key %s has the wrong size (%d bytes)", path, len(secret))
            raise VaultProblem("damaged_key", f"The key file {path} is damaged.")
        return secret
    if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
        log.failure("vault key %s is missing but items exist", path)
        raise VaultProblem("missing_key", f"The key file {path} is missing.")
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    try:   # owner-only from the very first moment, and never overwrite an existing file
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_vault(conn, key_path)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    log.event("created new vault secret at %s", path)
    return secret
