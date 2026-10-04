"""Rollback protection.

Every state change (lock, reveal, extend, delete) raises a counter that is stored
sealed in the database AND in witness files: one beside the database and one in the
user's home folder. If the database is later swapped for an older copy, its counter is
lower than a witness, which is detected. The response is a 48 hour pause on unlocking,
the same size as the "Reveal Now" penalty, so undoing a reveal gains nothing.

Limits: someone who restores the database and every witness file together defeats it."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import functions_config as cfg
from . import functions_crypto as crypto
from . import functions_log as log

_LABEL = b"rollback-state"


class RollbackGuard:
    def __init__(self, conn, vault: bytes, witness_paths: list[Path] | None = None):
        self.conn = conn
        self.vault = vault
        vault_id = crypto.subkey(vault, b"vault-id")[:8].hex()
        self.vault_id = vault_id
        self.paths = witness_paths if witness_paths is not None else [
            cfg.DB_PATH.parent / "state.seal", cfg.WITNESS_DIR / f"{vault_id}.seal"]
        self.rollback_detected_at: int | None = None

    # ---- sealed state: {"id", "ctr", "freeze"} ----
    def _read_blob(self, blob: bytes) -> dict | None:
        data = crypto.open_bytes(self.vault, _LABEL, blob)
        if data is None:
            return None
        try:
            state = json.loads(data)
        except ValueError:
            return None
        if state.get("id") != self.vault_id:
            return None
        return {"ctr": int(state.get("ctr", 0)), "freeze": int(state.get("freeze", 0))}

    def _make_blob(self, ctr: int, freeze: int) -> bytes:
        body = json.dumps({"id": self.vault_id, "ctr": ctr, "freeze": freeze}).encode()
        return crypto.seal_bytes(self.vault, _LABEL, body)

    def _db_state(self) -> dict:
        row = self.conn.execute("SELECT value FROM meta WHERE key='guard'").fetchone()
        if row:
            state = self._read_blob(bytes(row["value"]))
            if state is not None:
                return state
            log.failure("rollback counter in the database failed its integrity check")
        return {"ctr": 0, "freeze": 0}

    def _witness_states(self) -> list[dict]:
        states = []
        for path in self.paths:
            try:
                state = self._read_blob(path.read_bytes())
            except OSError:
                continue
            if state is not None:
                states.append(state)
        return states

    def _write(self, ctr: int, freeze: int) -> None:
        blob = self._make_blob(ctr, freeze)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('guard', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (blob,))
        self.conn.commit()                       # database first, witnesses second
        for path in self.paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                temp.write_bytes(blob)
                os.replace(temp, path)               # all or nothing, never a half-written witness
            except OSError as exc:
                log.failure("could not write witness %s: %s", path, exc)

    # ---- public ----
    def check(self, now: int) -> int:
        """Look for a rolled-back database. Returns the time unlocking is paused until
        (0 when not paused). Safe to call often."""
        db_state = self._db_state()
        witnesses = self._witness_states()
        newest = max([w["ctr"] for w in witnesses], default=0)
        freeze = max([db_state["freeze"]] + [w["freeze"] for w in witnesses])
        if newest > db_state["ctr"]:
            freeze = max(freeze, now + cfg.ROLLBACK_FREEZE_SECONDS)
            self.rollback_detected_at = now
            log.failure("ROLLBACK detected: database counter %d is behind witness %d; "
                        "unlocking paused until %d", db_state["ctr"], newest, freeze)
            self._write(newest + 1, freeze)
        elif len(witnesses) < len(self.paths) or db_state["freeze"] != freeze:
            self._write(db_state["ctr"], freeze)   # first run, or a missing witness: repair it
        return freeze if freeze > now else 0

    def bump(self) -> None:
        """Record a state change. Call after the database change has been committed."""
        state = self._db_state()
        witnesses = self._witness_states()
        ctr = max([state["ctr"]] + [w["ctr"] for w in witnesses]) + 1
        freeze = max([state["freeze"]] + [w["freeze"] for w in witnesses])
        self._write(ctr, freeze)
