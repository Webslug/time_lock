"""Settings, stored encrypted inside the SQLite database (meta table).

The settings key is derived from the vault secret, so the stored blob is unreadable
without it and any edit is detected. Missing or damaged settings fall back to the
safe defaults."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse

from . import functions_config as cfg
from . import functions_crypto as crypto
from . import functions_log as log

TIME_MODES = ("auto", "system_only", "network_only")

DEFAULTS = {
    "time_mode": "auto",                          # internet when possible, guarded clock otherwise
    "network_url": cfg.NETWORK_TIME_URL,
    "sound_enabled": True,
    "sound_volume": 70,                           # percent
    "default_lock_minutes": cfg.DEFAULT_LOCK_SECONDS // 60,
    "max_random_days": cfg.RANDOM_DAYS_DEFAULT,
    "blind_mode": False,                          # encrypt names too, shown only after unlock
}


def coerce_random_days(value) -> int:
    """Anything that is not a sensible whole number becomes 30 (a precaution);
    anything above 9999 is capped at 9999."""
    if isinstance(value, bool):
        return cfg.RANDOM_DAYS_DEFAULT
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return cfg.RANDOM_DAYS_DEFAULT
    if number < 1:
        return cfg.RANDOM_DAYS_DEFAULT
    return min(number, cfg.RANDOM_DAYS_MAX)


class SettingsError(ValueError):
    pass


def validate(values: dict) -> dict:
    """Return a clean copy of known settings, raising SettingsError for bad input."""
    out: dict = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise SettingsError(f"Unknown setting: {key}")
        if key == "time_mode":
            if value not in TIME_MODES:
                raise SettingsError("Time source must be auto, system_only or network_only.")
        elif key == "network_url":
            _check_url(value)
        elif key in ("sound_enabled", "blind_mode"):
            if not isinstance(value, bool):
                raise SettingsError("That switch must be on or off.")
        elif key == "sound_volume":
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
                raise SettingsError("Volume must be between 0 and 100.")
        elif key == "max_random_days":
            value = coerce_random_days(value)
        elif key == "default_lock_minutes":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 60 * 24 * 365:
                raise SettingsError("Default lock length must be between 1 minute and 1 year.")
        out[key] = value
    return out


def _check_url(value) -> None:
    """Only real https hosts: a loopback or private address could serve a forged clock."""
    if not isinstance(value, str) or len(value) > 200:
        raise SettingsError("The time website address is not valid.")
    parts = urlparse(value)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise SettingsError("The time website must start with https://")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise SettingsError("The time website must be on the internet, not this computer.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        raise SettingsError("The time website must be on the internet, not this computer.")


class Settings:
    def __init__(self, conn, vault: bytes):
        self.conn = conn
        self._key = crypto.subkey(vault, b"settings")
        self.damaged = False
        self._values = self._load()

    def _load(self) -> dict:
        row = self.conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        if not row:
            return dict(DEFAULTS)
        try:
            stored = json.loads(crypto.decrypt(self._key, bytes(row["value"]), b"settings"))
            return {**DEFAULTS, **validate({k: v for k, v in stored.items() if k in DEFAULTS})}
        except (crypto.TamperError, ValueError, TypeError) as exc:
            self.damaged = True
            log.failure("settings failed their integrity check, using defaults: %s", exc)
            return dict(DEFAULTS)

    def get(self) -> dict:
        return dict(self._values)

    def update(self, changes: dict) -> dict:
        merged = {**self._values, **validate(changes)}
        blob = crypto.encrypt(self._key, json.dumps(merged).encode(), b"settings")
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('settings', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (blob,))
        self.conn.commit()
        self._values = merged
        self.damaged = False
        log.event("settings updated: %s", sorted(changes))
        return dict(merged)
