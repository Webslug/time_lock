"""Lock, list, reveal and unlock items."""

from __future__ import annotations

import functools
import hashlib
import io
import re
import random
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path

from . import functions_config as cfg
from . import functions_crypto as crypto
from . import functions_db as db
from . import functions_guard as guard_mod
from . import functions_log as log
from . import functions_settings as settings_mod
from . import functions_time as clock_mod

try:  # optional: converts png/webp/etc. to jpg
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


class LockError(Exception):
    """A request the user can fix (bad input, still locked, ...)."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


class NeedsConfirmation(LockError):
    pass


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40] or "image"


def _locked(method):
    """One request at a time: the sqlite connection is shared between Flask threads."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class TimeLockStore:
    def __init__(self, conn: sqlite3.Connection | None = None, vault: bytes | None = None,
                 offline: bool = False, fixed_now=None, unlocked_dir: Path | None = None,
                 witness_paths: list[Path] | None = None, **clock_hooks):
        self._lock = threading.RLock()
        self.conn = conn or db.connect()
        self.vault = vault or db.load_vault(self.conn)
        self.settings = settings_mod.Settings(self.conn, self.vault)
        self.clock = clock_mod.TrustedClock(self.conn, self.vault, offline, fixed_now,
                                            settings=self.settings, **clock_hooks)
        self.guard = guard_mod.RollbackGuard(self.conn, self.vault, witness_paths)
        self.unlocked_dir = Path(unlocked_dir or cfg.UNLOCKED_DIR)
        self._open_cache: dict[str, tuple[bytes, dict]] = {}

    @_locked
    def update_settings(self, changes: dict) -> dict:
        """Settings share the database connection, so they take the same turnstile as everything else."""
        return self.settings.update(changes)

    # ---------- clock + rollback ----------
    def _tick(self, mutating: bool = False) -> tuple[int, int]:
        """Current trusted time and the time unlocking is paused until (0 = not paused)."""
        try:
            now = self.clock.now()                 # this also performs any due internet check
            if mutating:
                self.clock.require_online_if_strict()
        except clock_mod.ClockUnavailable as exc:
            raise LockError(str(exc), status=503)
        return now, self.guard.check(now)

    @staticmethod
    def _effective(unlock_at: int, freeze: int) -> int:
        return max(unlock_at, freeze)

    def status(self) -> dict:
        return self._status(*self._tick())

    def _status(self, now: int, freeze: int) -> dict:
        return {
            "clock_source": self.clock.source, "clock_mode": self.clock.mode,
            "clock_suspect": self.clock.suspect_reason,
            "paused_until": freeze or None,
            "rollback_detected": self.guard.rollback_detected_at is not None or bool(freeze),
            "settings_damaged": self.settings.damaged,
        }

    # ---------- locking ----------
    def _check_when(self, unlock_at: int | None, confirm_long: bool, now: int,
                    random_pick: bool = False) -> int:
        if random_pick:
            # Let fate decide: anywhere from a minute to "Maximum Random Days" from now.
            # The chosen moment is never shown or logged, so nobody knows when it opens.
            max_days = self.settings.get()["max_random_days"]
            longest = max_days * 86400
            if longest > cfg.LONG_LOCK_SECONDS and not confirm_long:
                raise NeedsConfirmation(
                    f"A random lock could last for up to {max_days} days. Please confirm.",
                    status=409, needs_confirm=True)
            return now + cfg.RANDOM_MIN_SECONDS + secrets.randbelow(longest - cfg.RANDOM_MIN_SECONDS + 1)
        if unlock_at is None:
            unlock_at = now + self.settings.get()["default_lock_minutes"] * 60
        unlock_at = int(unlock_at)
        if unlock_at <= now:
            raise LockError("The unlock time must be in the future.")
        if unlock_at - now > cfg.LONG_LOCK_SECONDS and not confirm_long:
            raise NeedsConfirmation(
                "This locks the item for more than 30 days. Please confirm.",
                status=409, needs_confirm=True)
        return unlock_at

    def _store(self, kind: str, label: str, data: bytes, unlock_at: int, now: int) -> str:
        item_id = uuid.uuid4().hex
        key = crypto.new_key()
        payload = crypto.encrypt(key, data, item_id.encode())
        blind = self.settings.get()["blind_mode"]
        shown_label, label_enc = label, None
        if blind:                                   # the name is encrypted with the item key too
            shown_label = cfg.BLIND_LABEL
            label_enc = crypto.encrypt(key, label.encode(), b"label:" + item_id.encode())
        self.conn.execute(
            "INSERT INTO items(id, kind, label, created_at, unlock_hidden, wrapped_key, payload, label_enc) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (item_id, kind, shown_label, now,
             crypto.hide_date(self.vault, item_id, unlock_at),
             crypto.wrap_key(self.vault, item_id, unlock_at, key), payload, label_enc))
        self.conn.commit()
        self.guard.bump()
        if len(payload) >= cfg.LARGE_WRITE_BYTES:
            log.event("large write: %s item %s, %d bytes", kind, item_id, len(payload))
        if blind:
            log.event("locked %s item %s (blind mode: name not logged)", kind, item_id)
        else:
            log.event("locked %s item %s (label=%r)", kind, item_id, label)
        return item_id

    @_locked
    def lock_text(self, text: str, unlock_at: int | None = None, label: str = "",
                  confirm_long: bool = False, random_pick: bool = False) -> str:
        data = (text or "").encode("utf-8")
        if not data:
            raise LockError("Please enter some text to lock.")
        if len(data) > cfg.MAX_TEXT_BYTES:
            raise LockError("That text is too large (limit 1 MB).")
        if data.startswith(cfg.DECOY_MARKER):
            raise LockError("That text cannot be locked.")
        now, _ = self._tick(mutating=True)
        when = self._check_when(unlock_at, confirm_long, now, random_pick)
        return self._store("text", (label or "").strip()[:80] or "Locked text", data, when, now)

    @_locked
    def lock_image(self, raw: bytes, unlock_at: int | None = None, label: str = "",
                   confirm_long: bool = False, source_path: Path | None = None,
                   delete_source: bool = False, random_pick: bool = False) -> tuple[str, bool | None]:
        """Lock an image. Returns (item id, whether the original was deleted or None when
        deleting was not requested). The original is deleted only after the stored copy
        has been read back and matches."""
        if not raw:
            raise LockError("Please choose an image.")
        if len(raw) > cfg.MAX_IMAGE_BYTES:
            raise LockError("That image is too large (limit 25 MB).")
        jpg = self._to_jpeg(raw)
        now, _ = self._tick(mutating=True)
        when = self._check_when(unlock_at, confirm_long, now, random_pick)
        item_id = self._store("image", (label or "").strip()[:80] or "Locked image", jpg, when, now)
        if not (delete_source and source_path):
            return item_id, None
        try:
            if self._decrypt(self._row(item_id)) != jpg:
                raise crypto.TamperError("read-back mismatch")
            Path(source_path).unlink()
        except (OSError, crypto.TamperError) as exc:
            log.failure("could not delete original %s: %s", source_path, exc)
            return item_id, False
        log.event("deleted original image %s after verifying item %s", source_path, item_id)
        return item_id, True

    @staticmethod
    def _to_jpeg(raw: bytes) -> bytes:
        if Image is None:
            if raw[:3] == b"\xff\xd8\xff":
                return raw
            raise LockError("Only JPEG images are supported (Pillow is not installed).")
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            raise LockError("That file is not a readable image.")
        if img.format == "JPEG":
            return raw
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=92)
        return out.getvalue()

    # ---------- reading ----------
    def _row(self, item_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise LockError("No such item.", status=404)
        return row

    def _unlock_at(self, row: sqlite3.Row) -> int:
        return crypto.show_date(self.vault, row["id"], bytes(row["unlock_hidden"]))

    def _item_key(self, row: sqlite3.Row) -> bytes:
        return crypto.unwrap_key(self.vault, row["id"], self._unlock_at(row), bytes(row["wrapped_key"]))

    def _decrypt(self, row: sqlite3.Row) -> bytes:
        return crypto.decrypt(self._item_key(row), bytes(row["payload"]), row["id"].encode())

    @_locked
    def decrypt_item(self, item_id: str) -> bytes:
        row = self._row(item_id)
        now, freeze = self._tick()
        unlock_at = self._effective(self._unlock_at(row), freeze)
        if now < unlock_at:
            log.failure("refused early decrypt of %s", item_id)
            raise LockError("This item is still locked.", status=423)
        try:
            data = self._decrypt(row)
        except crypto.TamperError:
            log.failure("tamper detected on item %s", item_id)
            raise LockError("This item failed its integrity check and cannot be opened.", status=422)
        log.event("decrypted item %s", item_id)
        return data

    @_locked
    def list_items(self) -> dict:
        now, freeze = self._tick()
        items = []
        hidden_count = 0
        for row in self.conn.execute("SELECT * FROM items ORDER BY created_at, rowid"):
            unlock_at = self._effective(self._unlock_at(row), freeze)
            unlocked = now >= unlock_at
            label = row["label"]
            if row["label_enc"] is not None and not unlocked:
                hidden_count += 1
                label = f"{cfg.BLIND_LABEL} {hidden_count}"
            entry = {
                "id": row["id"], "kind": row["kind"], "label": label,
                "created_at": row["created_at"], "state": "unlocked" if unlocked else "locked",
                "b_concealed": bool(row["b_concealed"]), "reveal_count": row["reveal_count"],
            }
            if unlocked or not row["b_concealed"]:
                entry["unlock_at"] = unlock_at
            if not unlocked and not row["b_concealed"]:
                entry["seconds_remaining"] = unlock_at - now
            if unlocked:
                self._attach_unlocked(row, entry)
            items.append(entry)
        return {"now": now, **self._status(now, freeze), "items": items}

    def _signature(self, row: sqlite3.Row) -> bytes:
        """Changes whenever anything that affects an item's opened form changes."""
        h = hashlib.sha256()
        for part in (row["payload"], row["wrapped_key"], row["unlock_hidden"], row["label_enc"] or b""):
            h.update(bytes(part))
            h.update(b"|")
        return h.digest()

    def _attach_unlocked(self, row: sqlite3.Row, entry: dict) -> None:
        """Fill in an unlocked item. Opened results are remembered, and a picture that has
        already been written out is not decrypted again: the page asks every few seconds,
        and decrypting a large picture takes noticeable time."""
        sig = self._signature(row)
        cached = self._open_cache.get(row["id"])
        if cached and cached[0] == sig:
            entry.update(cached[1])
            return
        extra: dict = {}
        try:
            key = self._item_key(row)
            if row["label_enc"] is not None:        # Blind mode: the name appears only now
                extra["label"] = crypto.decrypt(
                    key, bytes(row["label_enc"]), b"label:" + row["id"].encode()).decode("utf-8", "replace")
            shown = extra.get("label", row["label"])
            existing = row["exported_path"]
            if row["kind"] == "image" and existing and self._exported_file(existing).is_file():
                extra["exported_path"] = existing   # already on disk: nothing to decrypt
            else:
                data = crypto.decrypt(key, bytes(row["payload"]), row["id"].encode())
                if row["kind"] == "text":
                    if data.startswith(cfg.DECOY_MARKER):
                        extra["decoy"] = True
                        extra["text"] = ""
                    else:
                        extra["text"] = data.decode("utf-8", "replace")
                else:
                    extra["exported_path"] = self._export_image(row, data, shown)
        except crypto.TamperError:
            entry["state"] = "tampered"
            return
        self._open_cache[row["id"]] = (sig, extra)
        entry.update(extra)

    def _exported_file(self, name: str) -> Path:
        return self.unlocked_dir / Path(name).name     # a bare file name only, never a path

    def _export_image(self, row: sqlite3.Row, data: bytes, label: str | None = None) -> str:
        """Automatically write the jpg the first time an image is seen unlocked."""
        existing = row["exported_path"]
        if existing and self._exported_file(existing).is_file():
            return Path(existing).name
        self.unlocked_dir.mkdir(parents=True, exist_ok=True)
        name = f"{_slug(label or row['label'])}_{row['id'][:8]}.jpg"
        (self.unlocked_dir / name).write_bytes(data)
        self.conn.execute("UPDATE items SET exported_path=? WHERE id=?", (name, row["id"]))
        self.conn.commit()
        log.event("exported unlocked image %s -> %s (%d bytes)", row["id"], name, len(data))
        return name

    # ---------- reveal / extend / delete ----------
    def _require_unpaused(self, freeze: int) -> None:
        if freeze:
            raise LockError("Time Lock is paused after an older copy of the database was "
                            "detected. Please try again later.", status=423, paused_until=freeze)

    def _rewrap(self, row: sqlite3.Row, new_unlock: int, revealed: bool = False) -> None:
        """Move an item's unlock time, proving the data is intact first."""
        item_id = row["id"]
        try:
            key = crypto.unwrap_key(self.vault, item_id, self._unlock_at(row), bytes(row["wrapped_key"]))
            crypto.decrypt(key, bytes(row["payload"]), item_id.encode())
        except crypto.TamperError:
            raise LockError("This item failed its integrity check.", status=422)
        sql = "UPDATE items SET unlock_hidden=?, wrapped_key=?"
        if revealed:
            sql += ", b_concealed=0, reveal_count=reveal_count+1"
        self.conn.execute(sql + " WHERE id=?", (
            crypto.hide_date(self.vault, item_id, new_unlock),
            crypto.wrap_key(self.vault, item_id, new_unlock, key), item_id))
        self.conn.commit()
        self.guard.bump()

    @_locked
    def reveal(self, item_id: str) -> int:
        row = self._row(item_id)
        now, freeze = self._tick(mutating=True)
        self._require_unpaused(freeze)
        old = self._unlock_at(row)
        if now >= old:
            raise LockError("This item is already unlocked.")
        if not row["b_concealed"]:
            raise LockError("The unlock date is already revealed.")
        new = old + cfg.REVEAL_PENALTY_SECONDS
        self._rewrap(row, new, revealed=True)
        log.event("revealed item %s; unlock moved +48h to %d", item_id, new)
        return new

    @_locked
    def extend(self, item_id: str, extra_seconds: int, confirm_long: bool = False) -> dict:
        """Lock longer. Time can only be added, never removed."""
        row = self._row(item_id)
        now, freeze = self._tick(mutating=True)
        self._require_unpaused(freeze)
        if isinstance(extra_seconds, bool) or not isinstance(extra_seconds, int) or extra_seconds < 60:
            raise LockError("Please add at least one minute.")
        if extra_seconds > 5 * 365 * 24 * 3600:
            raise LockError("That is too long to add at once (limit 5 years).")
        old = self._unlock_at(row)
        if now >= old:
            raise LockError("This item is already unlocked.")
        new = old + extra_seconds
        if new - now > cfg.LONG_LOCK_SECONDS and not confirm_long:
            raise NeedsConfirmation(
                "This keeps the item locked for more than 30 days from now. Please confirm.",
                status=409, needs_confirm=True)
        self._rewrap(row, new)
        log.event("extended item %s by %ds", item_id, extra_seconds)
        return {"unlock_at": new if not row["b_concealed"] else None}

    @_locked
    def delete(self, item_id: str) -> None:
        row = self._row(item_id)
        now, freeze = self._tick()
        if now < self._effective(self._unlock_at(row), freeze):
            log.failure("refused delete of locked item %s", item_id)
            raise LockError("Locked items cannot be removed.", status=423)
        self.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.conn.commit()
        self._open_cache.pop(item_id, None)
        self.guard.bump()
        log.event("deleted unlocked item %s", item_id)

    # ---------- decoys ----------
    _DECOY_NAMES = ("Notes", "Passwords", "Private", "Old diary", "Draft", "Ideas", "Backup codes",
                    "Letter", "Photos", "Plans", "Reminder", "Secret", "Journal", "Scratch pad")
    _DECOY_WORDS = ("amber", "river", "quiet", "garden", "window", "silver", "morning", "paper", "orbit",
                    "candle", "harbour", "meadow", "violet", "lantern", "thunder", "ribbon", "marble")

    @_locked
    def add_decoys(self, count: int) -> int:
        """Add fake locked items that look exactly like real ones from the outside.
        Their contents (and the fact that they are decoys) are encrypted like everything else."""
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= cfg.DECOY_MAX_PER_ACTION:
            raise LockError(f"Please choose between 1 and {cfg.DECOY_MAX_PER_ACTION} decoys.")
        now, _ = self._tick(mutating=True)
        longest = self.settings.get()["max_random_days"] * 86400
        for _ in range(count):
            filler = " ".join(random.choice(self._DECOY_WORDS) for _ in range(random.randint(6, 18)))
            when = now + cfg.RANDOM_MIN_SECONDS + secrets.randbelow(longest - cfg.RANDOM_MIN_SECONDS + 1)
            self._store("text", random.choice(self._DECOY_NAMES), cfg.DECOY_MARKER + filler.encode(), when, now)
        log.event("added %d decoy items", count)
        return count

    # ---------- panic seal ----------
    @_locked
    def panic_seal(self, extra_seconds: int, confirm_long: bool = False) -> dict:
        """Lock every currently unlocked item again for the chosen period.
        Unlocked picture files are removed from the unlocked folder (the pictures stay
        safe inside the database)."""
        now, freeze = self._tick(mutating=True)
        self._require_unpaused(freeze)
        if isinstance(extra_seconds, bool) or not isinstance(extra_seconds, int) or extra_seconds < 60:
            raise LockError("Please choose at least one minute.")
        if extra_seconds > 5 * 365 * 24 * 3600:
            raise LockError("That is too long to choose at once (limit 5 years).")
        if extra_seconds > cfg.LONG_LOCK_SECONDS and not confirm_long:
            raise NeedsConfirmation("This seals everything for more than 30 days. Please confirm.",
                                    status=409, needs_confirm=True)
        new_unlock = now + extra_seconds
        sealed = skipped = 0
        for row in self.conn.execute("SELECT * FROM items").fetchall():
            if now < self._unlock_at(row):
                continue                                    # still locked already
            try:
                key = self._item_key(row)
                crypto.decrypt(key, bytes(row["payload"]), row["id"].encode())
            except crypto.TamperError:
                skipped += 1
                continue
            self.conn.execute(
                "UPDATE items SET unlock_hidden=?, wrapped_key=?, b_concealed=1, exported_path=NULL WHERE id=?",
                (crypto.hide_date(self.vault, row["id"], new_unlock),
                 crypto.wrap_key(self.vault, row["id"], new_unlock, key), row["id"]))
            if row["exported_path"]:
                self._exported_file(row["exported_path"]).unlink(missing_ok=True)
            sealed += 1
        self.conn.commit()
        self._open_cache.clear()                            # forget everything that was open
        if not sealed and not skipped:
            raise LockError("Nothing is unlocked right now, so there is nothing to seal.")
        self.guard.bump()
        log.event("panic seal: %d items re-locked (%d skipped)", sealed, skipped)
        return {"sealed": sealed, "skipped": skipped}

    @_locked
    def image_path(self, item_id: str) -> Path:
        """Ensure the jpg exists (decrypting if due) and return its path."""
        row = self._row(item_id)
        if row["kind"] != "image":
            raise LockError("That item is not an image.")
        data = self.decrypt_item(item_id)
        row = self._row(item_id)
        label = row["label"]
        if row["label_enc"] is not None:
            label = crypto.decrypt(self._item_key(row), bytes(row["label_enc"]),
                                   b"label:" + item_id.encode()).decode("utf-8", "replace")
        return self._exported_file(self._export_image(row, data, label))
