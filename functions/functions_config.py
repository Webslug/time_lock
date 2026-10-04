"""Central settings. Every path is relative to the project folder so the whole
folder can be zipped onto a USB stick and run elsewhere."""

from __future__ import annotations

from pathlib import Path

# When True: write audit logs (events, big writes, successes, failures).
# When False: quiet console, no log files.
DEBUG_MODE = False

ROOT_DIR = Path(__file__).resolve().parent.parent
LAUNCH_DIR = Path.cwd()                     # the folder Time Lock was started from
DB_DIR = ROOT_DIR / "db"
DB_PATH = DB_DIR / "time_lock.sqlite"
VAULT_KEY_PATH = DB_DIR / "vault.key"
LOG_DIR = ROOT_DIR / "logs"
UNLOCKED_DIR = ROOT_DIR / "unlocked"
ASSETS_DIR = ROOT_DIR / "assets"
HTML_DIR = ASSETS_DIR / "html"

DEFAULT_LOCK_SECONDS = 7 * 24 * 3600        # default: one week
LONG_LOCK_SECONDS = 30 * 24 * 3600          # warn above 30 days
REVEAL_PENALTY_SECONDS = 48 * 3600          # cost of "Reveal Now"
MAX_TEXT_BYTES = 1 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
LARGE_WRITE_BYTES = 1 * 1024 * 1024         # audit-log writes above this size

NETWORK_TIME_URL = "https://www.cloudflare.com"
NETWORK_TIME_TIMEOUT = 3.0
NETWORK_TIME_REFRESH = 300                  # seconds between network checks

# Rollback protection: a counter is kept inside the database and in "witness" files.
WITNESS_DIR = Path.home() / ".time_lock"     # second witness, outside the project folder
ROLLBACK_FREEZE_SECONDS = 48 * 3600          # unlocking pauses this long after a rollback is found

# Offline clock guard
CLOCK_TOLERANCE_SECONDS = 90                 # wall clock vs uptime disagreement allowed
NETWORK_RETRY_SECONDS = 60                   # retry after a failed network time check
CLOCK_STATE_SAVE_SECONDS = 10
HIGH_WATER_SAVE_SECONDS = 30                 # the "never go backwards" mark is saved at most this often

# "Maximum Random Days" setting
RANDOM_DAYS_DEFAULT = 30
RANDOM_DAYS_MAX = 9999
RANDOM_MIN_SECONDS = 60                      # a random lock is never shorter than a minute

# Decoy locks and Blind mode
DECOY_MAX_PER_ACTION = 10
DECOY_MARKER = b"\x00time-lock-decoy\x00"      # inside the encrypted data, so decoys look real from outside
BLIND_LABEL = "Hidden item"
