import tempfile
import unittest
from pathlib import Path

from functions import functions_config as cfg
from functions import functions_db as db
from functions import functions_settings as settings_mod
from functions import functions_time as clock_mod
from functions.functions_lock import LockError, NeedsConfirmation, TimeLockStore

cfg.DEBUG_MODE = False
T0 = 1_800_000_000


class Fake:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def open_store(tmp, fake=None, **hooks):
    tmp = Path(tmp)
    conn = db.connect(tmp / "t.sqlite")
    vault = db.load_vault(conn, tmp / "vault.key")
    return TimeLockStore(conn, vault, offline=hooks.pop("offline", True), fixed_now=fake,
                         unlocked_dir=tmp / "out", witness_paths=[tmp / "w1.seal", tmp / "w2.seal"],
                         **hooks)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.store = open_store(self.tmp.name, self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def reopen(self):
        self.store.conn.close()
        return open_store(self.tmp.name, self.fake)

    def test_restoring_old_database_pauses_unlocking(self):
        i = self.store.lock_text("secret", self.fake.t + 3600)
        snapshot = (Path(self.tmp.name) / "t.sqlite").read_bytes()    # copy before revealing
        self.store.reveal(i)                                          # +48h penalty
        self.store.conn.close()
        (Path(self.tmp.name) / "t.sqlite").write_bytes(snapshot)      # put the old copy back
        store = open_store(self.tmp.name, self.fake)
        status = store.status()
        self.assertTrue(status["rollback_detected"])
        self.assertEqual(status["paused_until"], self.fake.t + 48 * 3600)
        with self.assertRaises(LockError) as c:
            store.reveal(i)
        self.assertEqual(c.exception.status, 423)
        self.fake.t += 3601                                           # original time passes
        self.assertEqual(store.list_items()["items"][0]["state"], "locked")
        with self.assertRaises(LockError):
            store.decrypt_item(i)
        self.fake.t += 48 * 3600                                      # pause over
        self.assertEqual(store.list_items()["items"][0]["state"], "unlocked")
        self.assertIsNone(store.status()["paused_until"])

    def test_normal_use_never_triggers(self):
        i = self.store.lock_text("a", self.fake.t + 100)
        self.store.reveal(i)
        self.store = self.reopen()
        self.store.lock_text("b", self.fake.t + 100)
        self.assertFalse(self.store.status()["rollback_detected"])
        self.assertIsNone(self.store.status()["paused_until"])

    def test_one_witness_deleted_is_repaired(self):
        self.store.lock_text("a", self.fake.t + 100)
        (Path(self.tmp.name) / "w1.seal").unlink()
        self.store = self.reopen()
        self.assertIsNone(self.store.status()["paused_until"])
        self.assertTrue((Path(self.tmp.name) / "w1.seal").exists())

    def test_forged_witness_ignored(self):
        self.store.lock_text("a", self.fake.t + 100)
        (Path(self.tmp.name) / "w1.seal").write_bytes(b"x" * 80)
        (Path(self.tmp.name) / "w2.seal").write_bytes(b"x" * 80)
        self.store = self.reopen()
        self.assertIsNone(self.store.status()["paused_until"])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = open_store(self.tmp.name, Fake())

    def tearDown(self):
        self.tmp.cleanup()

    def test_encrypted_at_rest_and_persisted(self):
        self.store.settings.update({"time_mode": "system_only", "sound_volume": 33})
        raw = (Path(self.tmp.name) / "t.sqlite").read_bytes()
        self.assertNotIn(b"system_only", raw)
        self.assertNotIn(b"sound_volume", raw)
        again = open_store(self.tmp.name, Fake())
        self.assertEqual(again.settings.get()["time_mode"], "system_only")
        self.assertEqual(again.settings.get()["sound_volume"], 33)

    def test_validation(self):
        for bad in ({"time_mode": "yolo"}, {"sound_volume": 101}, {"nope": 1},
                    {"network_url": "http://time.example"}, {"network_url": "https://localhost/"},
                    {"network_url": "https://192.168.1.5/"}, {"network_url": "https://127.0.0.1/"},
                    {"default_lock_minutes": 0}, {"sound_enabled": "yes"}):
            with self.assertRaises(settings_mod.SettingsError, msg=bad):
                self.store.settings.update(bad)
        self.store.settings.update({"network_url": "https://www.example.org"})

    def test_tampered_settings_fall_back_to_defaults(self):
        self.store.settings.update({"sound_volume": 5})
        blob = bytearray(self.store.conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        blob[20] ^= 1
        self.store.conn.execute("UPDATE meta SET value=? WHERE key='settings'", (bytes(blob),))
        self.store.conn.commit()
        again = open_store(self.tmp.name, Fake())
        self.assertTrue(again.settings.damaged)
        self.assertEqual(again.settings.get()["sound_volume"], settings_mod.DEFAULTS["sound_volume"])

    def test_default_lock_minutes_used(self):
        fake = Fake()
        store = open_store(self.tmp.name, fake)
        store.settings.update({"default_lock_minutes": 2})
        i = store.lock_text("x")
        self.assertEqual(store._unlock_at(store._row(i)), fake.t + 120)


class ExtendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.store = open_store(self.tmp.name, self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extend_adds_time_and_still_decrypts(self):
        i = self.store.lock_text("secret", self.fake.t + 100)
        out = self.store.extend(i, 3600)
        self.assertIsNone(out["unlock_at"])                          # still concealed
        self.fake.t += 101
        self.assertEqual(self.store.list_items()["items"][0]["state"], "locked")
        self.fake.t += 3600
        self.assertEqual(self.store.decrypt_item(i), b"secret")

    def test_extend_revealed_returns_date(self):
        i = self.store.lock_text("s", self.fake.t + 100)
        new = self.store.reveal(i)
        self.assertEqual(self.store.extend(i, 600)["unlock_at"], new + 600)

    def test_rules(self):
        i = self.store.lock_text("s", self.fake.t + 100)
        for bad in (0, 30, -5, 10 ** 10):
            with self.assertRaises(LockError):
                self.store.extend(i, bad)
        with self.assertRaises(NeedsConfirmation):
            self.store.extend(i, 31 * 86400)
        self.store.extend(i, 31 * 86400, confirm_long=True)
        self.fake.t += 10 ** 7
        j = self.store.lock_text("t", self.fake.t + 5)
        self.fake.t += 5
        with self.assertRaises(LockError):                           # already unlocked
            self.store.extend(j, 600)


class RandomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.store = open_store(self.tmp.name, self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def test_random_inside_range_and_concealed(self):
        self.store.settings.update({"max_random_days": 10})
        picks = set()
        for _ in range(40):
            i = self.store.lock_text("x", random_pick=True)
            when = self.store._unlock_at(self.store._row(i))
            self.assertGreaterEqual(when, T0 + 60)
            self.assertLessEqual(when, T0 + 10 * 86400)
            picks.add(when)
        self.assertGreater(len(picks), 30)                       # genuinely varied
        self.assertTrue(all(it["b_concealed"] and "unlock_at" not in it
                            for it in self.store.list_items()["items"]))

    def test_random_needs_confirmation_when_long(self):
        self.store.settings.update({"max_random_days": 31})
        with self.assertRaises(NeedsConfirmation):
            self.store.lock_text("x", random_pick=True)
        self.store.lock_text("x", random_pick=True, confirm_long=True)
        self.store.settings.update({"max_random_days": 30})
        self.store.lock_text("x", random_pick=True)               # 30 days needs no warning

    def test_random_ignores_given_time_and_never_logged(self):
        import logging
        original_log_dir = cfg.LOG_DIR
        cfg.DEBUG_MODE = True
        cfg.LOG_DIR = Path(self.tmp.name) / "logs"
        from functions import functions_log as log
        log._logger = None
        try:
            i = self.store.lock_text("x", T0 + 5, random_pick=True)      # the 5 seconds is ignored
            self.store.extend(i, 3600)
            logging.shutdown()
            text = (cfg.LOG_DIR / "time_lock.log").read_text()
            when = self.store._unlock_at(self.store._row(i))
            self.assertNotIn(str(when), text)
            self.assertNotIn(str(when - 3600), text)
        finally:
            cfg.DEBUG_MODE = False
            cfg.LOG_DIR = original_log_dir
            log._logger = None

    def test_random_days_coercion(self):
        from functions.functions_settings import coerce_random_days as c
        self.assertEqual(c("abc"), 30)
        self.assertEqual(c(""), 30)
        self.assertEqual(c(None), 30)
        self.assertEqual(c(True), 30)
        self.assertEqual(c(0), 30)
        self.assertEqual(c(-4), 30)
        self.assertEqual(c("12"), 12)
        self.assertEqual(c(" 45 "), 45)
        self.assertEqual(c(99999), 9999)
        self.assertEqual(c("1e3"), 30)
        self.assertEqual(self.store.settings.update({"max_random_days": "banana"})["max_random_days"], 30)
        self.assertEqual(self.store.settings.update({"max_random_days": 20000})["max_random_days"], 9999)


class ExtrasTests(unittest.TestCase):
    """Blind mode, decoy locks, panic seal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.store = open_store(self.tmp.name, self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def test_blind_mode_hides_names_until_unlock(self):
        self.store.settings.update({"blind_mode": True})
        a = self.store.lock_text("alpha", T0 + 100, "Wifi password")
        self.store.lock_text("beta", T0 + 5000, "Bank notes")
        raw = (Path(self.tmp.name) / "t.sqlite").read_bytes()
        self.assertNotIn(b"Wifi password", raw)
        self.assertNotIn(b"Bank notes", raw)
        items = self.store.list_items()["items"]
        self.assertEqual([i["label"] for i in items], ["Hidden item 1", "Hidden item 2"])
        self.fake.t += 100
        items = self.store.list_items()["items"]
        self.assertEqual(items[0]["label"], "Wifi password")      # revealed on unlock
        self.assertEqual(items[0]["text"], "alpha")
        self.assertEqual(items[1]["label"], "Hidden item 1")      # still hidden, renumbered

    def test_blind_mode_name_never_logged(self):
        original = cfg.LOG_DIR
        from functions import functions_log as log
        import logging
        cfg.DEBUG_MODE = True
        cfg.LOG_DIR = Path(self.tmp.name) / "logs"
        log._logger = None
        try:
            self.store.settings.update({"blind_mode": True})
            self.store.lock_text("x", T0 + 50, "SuperPrivateName")
            logging.shutdown()
            self.assertNotIn("SuperPrivateName", (cfg.LOG_DIR / "time_lock.log").read_text())
        finally:
            cfg.DEBUG_MODE = False
            cfg.LOG_DIR = original
            log._logger = None

    def test_old_database_without_label_column_is_upgraded(self):
        import sqlite3
        old = Path(self.tmp.name) / "old.sqlite"
        raw = sqlite3.connect(old)
        raw.execute("CREATE TABLE items (id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, "
                    "created_at INTEGER NOT NULL, unlock_hidden BLOB NOT NULL, wrapped_key BLOB NOT NULL, "
                    "payload BLOB NOT NULL, b_concealed INTEGER NOT NULL DEFAULT 1, "
                    "reveal_count INTEGER NOT NULL DEFAULT 0, exported_path TEXT)")
        raw.commit()
        raw.close()
        conn = db.connect(old)
        self.assertIn("label_enc", {r["name"] for r in conn.execute("PRAGMA table_info(items)")})

    def test_decoys_look_real_and_say_so_only_when_opened(self):
        self.store.lock_text("real one", T0 + 10 ** 6, "Mine", confirm_long=True)
        self.assertEqual(self.store.add_decoys(4), 4)
        items = self.store.list_items()["items"]
        self.assertEqual(len(items), 5)
        raw = (Path(self.tmp.name) / "t.sqlite").read_bytes()
        self.assertNotIn(b"decoy", raw)                                  # the marker is encrypted
        self.assertTrue(all(i["state"] == "locked" and i["b_concealed"] and "unlock_at" not in i for i in items))
        self.fake.t += 100 * 86400                                       # everything is due
        items = self.store.list_items()["items"]
        decoys = [i for i in items if i.get("decoy")]
        self.assertEqual(len(decoys), 4)
        self.assertTrue(all(i["text"] == "" for i in decoys))
        self.assertEqual([i for i in items if not i.get("decoy")][0]["text"], "real one")

    def test_decoy_limits_and_marker_rejected(self):
        for bad in (0, 11, -1, True, "3"):
            with self.assertRaises(LockError):
                self.store.add_decoys(bad)
        with self.assertRaises(LockError):
            self.store.lock_text(cfg.DECOY_MARKER.decode() + "pretend", T0 + 100)

    def test_panic_seal_relocks_everything_unlocked(self):
        from PIL import Image
        import io
        a = self.store.lock_text("alpha", T0 + 10, "A")
        buf = io.BytesIO()
        Image.new("RGB", (6, 6)).save(buf, "PNG")
        b, _ = self.store.lock_image(buf.getvalue(), T0 + 10, "B")
        c = self.store.lock_text("later", T0 + 10 ** 6, "C", confirm_long=True)
        self.fake.t += 10
        items = self.store.list_items()["items"]
        image_item = next(i for i in items if i["kind"] == "image")
        exported = Path(self.tmp.name) / "out" / image_item["exported_path"]
        self.assertTrue(exported.is_file())
        self.fake.t += 1
        out = self.store.panic_seal(3600)
        self.assertEqual(out["sealed"], 2)                               # C was already locked
        self.assertFalse(exported.exists())                              # unlocked jpg removed
        items = self.store.list_items()["items"]
        self.assertTrue(all(i["state"] == "locked" and i["b_concealed"] for i in items))
        self.fake.t += 3601
        again = self.store.list_items()["items"]
        by_label = {i["label"]: i for i in again}
        self.assertEqual(by_label["A"]["text"], "alpha")                 # still opens afterwards
        self.assertTrue(by_label["B"]["exported_path"].endswith(".jpg"))      # image intact in the database

    def test_panic_seal_rules(self):
        with self.assertRaises(LockError):                               # nothing unlocked
            self.store.panic_seal(3600)
        self.store.lock_text("a", T0 + 10)
        self.fake.t += 10
        for bad in (0, 59, -5, 10 ** 10, True):
            with self.assertRaises(LockError):
                self.store.panic_seal(bad)
        with self.assertRaises(NeedsConfirmation):
            self.store.panic_seal(31 * 86400)
        self.assertEqual(self.store.panic_seal(31 * 86400, confirm_long=True)["sealed"], 1)

    def test_panic_seal_paused_after_rollback(self):
        i = self.store.lock_text("s", T0 + 10)
        snap = (Path(self.tmp.name) / "t.sqlite").read_bytes()
        self.fake.t += 10
        self.store.lock_text("other", T0 + 99999)
        self.store.conn.close()
        (Path(self.tmp.name) / "t.sqlite").write_bytes(snap)
        store = open_store(self.tmp.name, self.fake)
        with self.assertRaises(LockError) as c:
            store.panic_seal(3600)
        self.assertEqual(c.exception.status, 423)


class ReviewFixTests(unittest.TestCase):
    """Problems found in the code review, kept fixed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.store = open_store(self.tmp.name, self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def _png(self):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
        return buf.getvalue()

    def test_unlocked_picture_is_not_decrypted_again_on_every_check(self):
        from functions import functions_crypto as crypto
        self.store.lock_image(self._png(), T0 + 5, "Pic")
        self.fake.t += 5
        self.store.list_items()                                  # first sight: decrypts and writes the jpg
        calls = []
        original = crypto.decrypt
        crypto.decrypt = lambda *a, **k: (calls.append(1), original(*a, **k))[1]
        try:
            for _ in range(3):
                self.store.list_items()                          # the page asks every few seconds
        finally:
            crypto.decrypt = original
        self.assertEqual(calls, [])

    def test_text_not_decrypted_again_either_but_changes_are_noticed(self):
        a = self.store.lock_text("hello", T0 + 5, "T")
        self.fake.t += 5
        self.assertEqual(self.store.list_items()["items"][0]["text"], "hello")
        self.store.panic_seal(60)
        self.assertEqual(self.store.list_items()["items"][0]["state"], "locked")
        self.fake.t += 61
        self.assertEqual(self.store.list_items()["items"][0]["text"], "hello")

    def test_blind_picture_file_uses_the_real_name(self):
        self.store.settings.update({"blind_mode": True})
        self.store.lock_image(self._png(), T0 + 5, "Holiday")
        self.fake.t += 5
        item = self.store.list_items()["items"][0]
        self.assertEqual(item["label"], "Holiday")
        self.assertTrue(item["exported_path"].startswith("Holiday_"))

    def test_exported_path_in_database_cannot_escape_the_folder(self):
        i, _ = self.store.lock_image(self._png(), T0 + 5, "Pic")
        self.fake.t += 5
        self.store.conn.execute("UPDATE items SET exported_path='../../etc/passwd' WHERE id=?", (i,))
        self.store.conn.commit()
        self.store._open_cache.clear()
        path = self.store.image_path(i)
        self.assertEqual(path.parent, self.store.unlocked_dir)

    def test_vault_key_file_is_private_and_not_overwritten(self):
        import stat
        key = Path(self.tmp.name) / "vault.key"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        before = key.read_bytes()
        db.load_vault(self.store.conn, key)
        self.assertEqual(key.read_bytes(), before)

    def test_witness_files_are_written_whole(self):
        self.store.lock_text("x", T0 + 100)
        names = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.assertFalse([n for n in names if n.endswith(".tmp")])

    def test_strict_mode_first_action_works_when_internet_is_available(self):
        store = open_store(self.tmp.name, None, offline=False, fetch_fn=lambda: float(T0),
                           uptime_fn=lambda: 1000.0, wall_fn=lambda: float(T0), boot_fn=lambda: "b")
        store.settings.update({"time_mode": "network_only"})
        store.lock_text("works on the very first try", T0 + 600)   # no earlier clock reading
        self.assertEqual(len(store.list_items()["items"]), 1)


class HighWaterWriteTests(unittest.TestCase):
    def test_not_saved_on_every_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake()
            store = open_store(tmp, fake)
            store.clock.now()                              # first reading is saved
            original = store.clock.conn
            counting = []
            store.clock.conn = type("C", (), {
                "execute": lambda self, sql, *a: (counting.append(sql) if "INSERT INTO meta" in sql else None, original.execute(sql, *a))[1],
                "commit": lambda self: original.commit()})()
            for _ in range(20):
                fake.t += 1                                # twenty checks, twenty seconds
                store.clock.now()
            self.assertEqual(counting, [])                 # nothing saved yet
            fake.t += 15
            store.clock.now()                              # now past 30 seconds
            self.assertEqual(len(counting), 1)
            store.clock.conn = original


class ClockGuardTests(unittest.TestCase):
    """Drive the guarded system clock with fake wall/uptime/boot values."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wall, self.up, self.boot = float(T0), 1000.0, "boot-A"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, **kw):
        return open_store(self.tmp.name, None, uptime_fn=lambda: self.up,
                          wall_fn=lambda: self.wall, boot_fn=lambda: self.boot, **kw)

    def test_wall_jump_inside_one_run_is_ignored(self):
        s = self.store()
        self.assertEqual(s.clock.now(), T0)
        self.up += 5
        self.wall += 100000                      # user winds the clock forward a day
        self.assertEqual(s.clock.now(), T0 + 5)
        self.assertIn("jumped", s.clock.suspect_reason)

    def test_restart_in_same_boot_ignores_forward_change(self):
        s = self.store()
        s.clock.now()
        s.conn.close()
        self.up += 100
        self.wall += 50000                       # clock wound forward while the app was closed
        s2 = self.store()
        self.assertEqual(s2.clock.now(), T0 + 100)
        self.assertIsNotNone(s2.clock.suspect_reason)

    def test_new_boot_clock_behind_is_ignored(self):
        s = self.store()
        s.clock.now()
        s.conn.close()
        self.boot, self.up, self.wall = "boot-B", 50.0, T0 - 5000.0   # rebooted, clock set back
        s2 = self.store()
        self.assertGreaterEqual(s2.clock.now(), T0 + 50)
        self.assertIsNotNone(s2.clock.suspect_reason)

    def test_new_boot_normal_follows_wall(self):
        s = self.store()
        s.clock.now()
        s.conn.close()
        self.boot, self.up, self.wall = "boot-B", 50.0, T0 + 3600.0
        s2 = self.store()
        self.assertEqual(s2.clock.now(), T0 + 3600)
        self.assertIsNone(s2.clock.suspect_reason)

    def test_unlock_cannot_be_forced_by_clock_change(self):
        s = self.store()
        i = s.lock_text("secret", T0 + 600)
        self.wall += 10 ** 6                     # wind forward a long way
        self.up += 3
        with self.assertRaises(LockError):
            s.decrypt_item(i)
        self.up += 600
        self.assertEqual(s.decrypt_item(i), b"secret")

    def test_network_time_wins_when_available(self):
        s = self.store(offline=False, fetch_fn=lambda: T0 + 7.0)
        self.wall = T0 + 90000.0                 # wall clock is a day fast
        self.assertEqual(s.clock.now(), T0 + 7)
        self.assertEqual(s.clock.source, "network")

    def test_auto_mode_falls_back_when_network_fails(self):
        s = self.store(offline=False, fetch_fn=lambda: None)
        self.assertEqual(s.clock.now(), T0)
        self.assertEqual(s.clock.source, "system")

    def test_network_only_refuses_without_internet(self):
        s = self.store(offline=False, fetch_fn=lambda: None)
        s.settings.update({"time_mode": "network_only"})
        with self.assertRaises(LockError) as c:
            s.lock_text("x", T0 + 100)
        self.assertEqual(c.exception.status, 503)

    def test_system_only_never_calls_network(self):
        calls = []
        s = self.store(offline=False, fetch_fn=lambda: calls.append(1))
        s.settings.update({"time_mode": "system_only"})
        s.clock.now()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
