"""Time Lock entry point.

  python app.py                  run normally (opens the browser)
  python app.py --debug          turn DEBUG_MODE on for this run (audit log file)
  python app.py --test-mode      adds 10s/30s/60s presets to the Add window
  python app.py --offline        never ask the network for the time
  python app.py --seconds-demo   seed demo items that unlock in a few seconds
  python app.py --data-dir DIR   keep all data in DIR (scratch testing)
  python app.py --selftest       run the automated tests and exit
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

from functions import functions_config as cfg
from functions import functions_log as log
from functions.functions_db import VaultProblem
from functions.functions_lock import TimeLockStore
from functions.functions_routes import create_app


def seed_demo(store: TimeLockStore) -> None:
    now = store.clock.now()
    store.lock_text("Demo secret: unlocks after 10 seconds", now + 10, "Demo text (10s)")
    store.lock_text("Demo secret: unlocks after 30 seconds", now + 30, "Demo text (30s)")
    print("Seeded two demo items (10s and 30s).")


def main() -> int:
    p = argparse.ArgumentParser(description="Time Lock")
    p.add_argument("--port", type=int, default=5239)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--test-mode", action="store_true")
    p.add_argument("--seconds-demo", action="store_true")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--data-dir", help="use another folder for the database, vault key and unlocked jpgs (testing)")
    p.add_argument("--debug", action="store_true", help="turn DEBUG_MODE on for this run (writes logs/time_lock.log)")
    p.add_argument("--quiet", action="store_true", help="turn DEBUG_MODE off for this run")
    args = p.parse_args()

    if args.debug:
        cfg.DEBUG_MODE = True
    if args.quiet:
        cfg.DEBUG_MODE = False
    if args.data_dir:
        base = Path(args.data_dir).resolve()
        cfg.DB_PATH, cfg.VAULT_KEY_PATH, cfg.UNLOCKED_DIR = base / "time_lock.sqlite", base / "vault.key", base / "unlocked"
        cfg.WITNESS_DIR, cfg.LOG_DIR = base / "witness", base / "logs"
    if args.selftest:
        import unittest
        here = Path(__file__).resolve().parent
        suite = unittest.defaultTestLoader.discover(str(here / "tests"), top_level_dir=str(here))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1

    try:
        store = TimeLockStore(offline=args.offline)
    except VaultProblem as problem:
        store = None      # the page will explain, kindly, what to do
        print(f"\nTime Lock cannot open your vault: {problem}\n"
              "Nothing has been changed. Open the page in your browser for help.\n")
    if store and args.seconds_demo:
        seed_demo(store)
    app = create_app(store, test_mode=args.test_mode or args.seconds_demo, offline=args.offline)
    url = f"http://127.0.0.1:{args.port}/"
    log.quiet_console()
    log.event("starting on %s offline=%s test_mode=%s", url, args.offline, args.test_mode)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"Time Lock is running at {url}  (press Ctrl+C to stop)")
    if cfg.DEBUG_MODE:
        print(f"DEBUG_MODE is on: events are logged to {cfg.LOG_DIR}")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
