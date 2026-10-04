"""Trusted clock.

Three time modes (a setting):
  auto         internet time when reachable, otherwise the guarded computer clock (default)
  system_only  never touches the network; guarded computer clock only
  network_only unlocking and locking need internet time; nothing unlocks without it

The guarded computer clock does not simply believe the wall clock. It keeps a virtual
time that moves with the machine's uptime counter (which a user cannot set):
  * inside one run, a sudden jump of the wall clock is ignored and flagged;
  * on Linux the boot id lets a restart in the same boot carry on exactly, so winding the
    clock forward between runs is ignored too;
  * after a reboot the virtual time can never fall below the last saved time plus the
    uptime since boot, so winding the clock back is ignored;
  * a saved high-water mark stops any backwards step.
Known limit: wind the clock forward, then reboot while offline: that cannot be told from
a genuine long gap. Internet time, when reachable, always wins and flags it."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import functions_config as cfg
from . import functions_crypto as crypto
from . import functions_log as log

_HW_LABEL = b"high_water"
_CLK_LABEL = b"clock-state"


class ClockUnavailable(Exception):
    """network_only mode and the internet time could not be fetched."""


def uptime_seconds() -> float:
    """Seconds since boot, including time asleep where the platform tells us."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    if sys.platform == "win32":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
        except Exception:
            pass
    return time.monotonic()


def boot_id() -> str | None:
    """A value that changes at every reboot (Linux only)."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


class TrustedClock:
    def __init__(self, conn, vault: bytes, offline: bool = False, fixed_now=None,
                 settings=None, uptime_fn=None, wall_fn=None, boot_fn=None, fetch_fn=None):
        self.conn = conn
        self.vault = vault
        self.settings = settings
        self._forced_offline = offline          # --offline flag
        self._fixed_now = fixed_now             # tests: callable returning epoch seconds
        self._uptime = uptime_fn or uptime_seconds
        self._wall = wall_fn or time.time
        self._boot = boot_fn or boot_id
        self._fetch = fetch_fn or self._network_epoch
        self._anchor: tuple[float, float] | None = None   # (virtual epoch, uptime)
        self._last_net_try = float("-inf")
        self._last_save = float("-inf")
        self._last_net_ok = False
        self._net_was_ok = True                 # so the first failure is logged once
        self._hw: int | None = None             # high-water mark, loaded on first use
        self._hw_saved = 0
        self.source = "system"
        self.suspect_reason: str | None = None  # set when clock tampering is suspected
        self.mode_note = ""

    # ---------- settings ----------
    @property
    def mode(self) -> str:
        if self._forced_offline:
            return "system_only"
        return self.settings.get()["time_mode"] if self.settings else "auto"

    def _url(self) -> str:
        return self.settings.get()["network_url"] if self.settings else cfg.NETWORK_TIME_URL

    # ---------- network ----------
    def _network_epoch(self) -> float | None:
        req = urllib.request.Request(self._url(), method="HEAD",
                                     headers={"User-Agent": "TimeLock/1.0 (clock check)"})
        try:
            try:
                with urllib.request.urlopen(req, timeout=cfg.NETWORK_TIME_TIMEOUT) as resp:
                    return parsedate_to_datetime(resp.headers["Date"]).timestamp()
            except urllib.error.HTTPError as err:   # an error page still carries a valid Date
                return parsedate_to_datetime(err.headers["Date"]).timestamp()
        except Exception as exc:  # offline, DNS, TLS, missing header: all fall back
            if self._net_was_ok:                 # log the change, not every retry
                log.failure("network time became unavailable: %s", exc)
            self._net_was_ok = False
            return None

    def _maybe_sync_network(self, uptime_now: float) -> None:
        if self.mode == "system_only":
            self._last_net_ok = False
            return
        wait = cfg.NETWORK_TIME_REFRESH if self._last_net_ok else cfg.NETWORK_RETRY_SECONDS
        if uptime_now - self._last_net_try < wait:
            return
        self._last_net_try = uptime_now
        epoch = self._fetch()
        if epoch is None:
            self._last_net_ok = False
            return
        self._last_net_ok = True
        self._net_was_ok = True
        wall_skew = abs(epoch - self._wall())
        if wall_skew > cfg.CLOCK_TOLERANCE_SECONDS:
            self.suspect_reason = (f"The computer's clock is {int(wall_skew)} seconds away from "
                                   "internet time. Time Lock is using internet time.")
            log.failure("system clock differs from network time by %.0fs", wall_skew)
        self._anchor = (float(epoch), uptime_now)      # internet time is the authority
        self._save_state(force=True)

    # ---------- guarded system clock ----------
    def _load_state(self) -> dict | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='clk'").fetchone()
        if not row:
            return None
        data = crypto.open_bytes(self.vault, _CLK_LABEL, bytes(row["value"]))
        if data is None:
            log.failure("saved clock state failed its integrity check; ignoring it")
            return None
        try:
            return json.loads(data)
        except ValueError:
            return None

    def _save_state(self, force: bool = False) -> None:
        if self._anchor is None:
            return
        up = self._uptime()
        if not force and up - self._last_save < cfg.CLOCK_STATE_SAVE_SECONDS:
            return
        self._last_save = up
        virtual = self._anchor[0] + (up - self._anchor[1])
        body = json.dumps({"boot": self._boot(), "uptime": up, "virtual": virtual,
                           "wall": self._wall()}).encode()
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('clk', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (crypto.seal_bytes(self.vault, _CLK_LABEL, body),))
        self.conn.commit()

    def _init_anchor(self, up: float) -> None:
        wall = self._wall()
        state = self._load_state()
        tol = cfg.CLOCK_TOLERANCE_SECONDS
        if state and self._boot() and state.get("boot") == self._boot() and up >= state["uptime"]:
            virtual = state["virtual"] + (up - state["uptime"])      # same boot: exact
            if abs(wall - virtual) > tol:
                self.suspect_reason = (
                    f"The computer's clock changed by {int(wall - virtual)} seconds while the "
                    "machine was running. Time Lock is ignoring the change.")
                log.failure("clock tampering suspected (same boot): wall %.0f vs virtual %.0f",
                            wall, virtual)
        elif state:
            floor = state["virtual"] + up        # time since this boot began is a safe minimum
            virtual = max(wall, floor)
            if wall < floor - tol:
                self.suspect_reason = ("The computer's clock is behind where it was last seen. "
                                       "Time Lock is ignoring the change.")
                log.failure("clock behind last known time (new boot): wall %.0f floor %.0f", wall, floor)
        else:
            virtual = wall                       # first ever run
        self._anchor = (virtual, up)
        self._save_state(force=True)

    def _guarded_now(self) -> float:
        up = self._uptime()
        if self._anchor is None:
            self._init_anchor(up)
        virtual = self._anchor[0] + (up - self._anchor[1])
        if abs(self._wall() - virtual) > cfg.CLOCK_TOLERANCE_SECONDS and self.suspect_reason is None:
            self.suspect_reason = ("The computer's clock jumped while Time Lock was running. "
                                   "Time Lock is ignoring the jump.")
            log.failure("clock jump ignored: wall %.0f vs virtual %.0f", self._wall(), virtual)
        self._save_state()
        return virtual

    # ---------- public ----------
    def _best_time(self) -> float:
        up = self._uptime()
        self._maybe_sync_network(up)
        if self.mode == "network_only" and not self._last_net_ok and self._anchor is None:
            raise ClockUnavailable("Internet time is required (Settings) but cannot be reached.")
        if self.mode == "network_only" and not self._last_net_ok:
            # keep counting from the last internet sync, but flag that we are offline
            self.mode_note = "offline"
        value = self._guarded_now()
        self.source = "network" if self._last_net_ok else "system"
        return value

    def _high_water(self) -> int:
        """The highest time ever trusted. Read once, then kept in memory."""
        if self._hw is None:
            self._hw = 0
            row = self.conn.execute("SELECT value FROM meta WHERE key='hw'").fetchone()
            if row:
                value = crypto.open_number(self.vault, _HW_LABEL, row["value"])
                if value is None:
                    log.failure("high-water mark failed its integrity check; ignoring it")
                else:
                    self._hw = value
            self._hw_saved = self._hw
        return self._hw

    def now(self) -> int:
        best = int(self._fixed_now()) if self._fixed_now else int(self._best_time())
        hw = self._high_water()
        if best < hw:
            log.failure("clock is behind high-water mark by %ds; using the mark", hw - best)
        effective = max(best, hw)
        self._hw = effective
        if effective - self._hw_saved >= cfg.HIGH_WATER_SAVE_SECONDS:
            # Saving on every check would mean thousands of disk writes a day for nothing.
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('hw', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (crypto.seal_number(self.vault, _HW_LABEL, effective),),
            )
            self.conn.commit()
            self._hw_saved = effective
        return effective

    def require_online_if_strict(self) -> None:
        """network_only mode: refuse to change anything without internet time."""
        if self.mode == "network_only" and not self._last_net_ok and not self._fixed_now:
            raise ClockUnavailable("Internet time is required (Settings) but cannot be reached.")
