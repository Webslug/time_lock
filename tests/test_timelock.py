import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from functions import functions_config as cfg
from functions import functions_crypto as crypto
from functions import functions_db as db
from functions.functions_lock import LockError, NeedsConfirmation, TimeLockStore
from functions.functions_routes import create_app

cfg.DEBUG_MODE = False
ORIGINAL_DB, ORIGINAL_KEY = cfg.DB_PATH, cfg.VAULT_KEY_PATH


class Fake:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


def make_store(tmp, fake=None):
    tmp = Path(tmp)
    conn = db.connect(tmp / "t.sqlite")
    vault = db.load_vault(conn, tmp / "vault.key")
    return TimeLockStore(conn, vault, offline=True, fixed_now=fake, unlocked_dir=tmp / "out",
                         witness_paths=[tmp / "w1.seal", tmp / "w2.seal"])


class CryptoTests(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        k = crypto.new_key()
        blob = crypto.encrypt(k, b"Mypassword", b"id")
        self.assertEqual(crypto.decrypt(k, blob, b"id"), b"Mypassword")
        with self.assertRaises(crypto.TamperError):
            crypto.decrypt(k, blob[:-1] + bytes([blob[-1] ^ 1]), b"id")
        with self.assertRaises(crypto.TamperError):
            crypto.decrypt(k, blob, b"other")

    def test_date_hidden_and_bound(self):
        v = b"v" * 32
        hidden = crypto.hide_date(v, "a", 1234567890)
        self.assertEqual(crypto.show_date(v, "a", hidden), 1234567890)
        self.assertNotEqual(hidden, (1234567890).to_bytes(8, "big"))
        k = crypto.new_key()
        w = crypto.wrap_key(v, "a", 100, k)
        self.assertEqual(crypto.unwrap_key(v, "a", 100, w), k)
        self.assertNotEqual(crypto.unwrap_key(v, "a", 101, w), k)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.store = make_store(self.tmp.name, self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_locked_then_unlocked(self):
        i = self.store.lock_text("Mypassword", self.fake.t + 10)
        with self.assertRaises(LockError) as c:
            self.store.decrypt_item(i)
        self.assertEqual(c.exception.status, 423)
        item = self.store.list_items()["items"][0]
        self.assertNotIn("text", item)
        self.assertNotIn("unlock_at", item)       # concealed
        self.fake.t += 10
        item = self.store.list_items()["items"][0]
        self.assertEqual(item["text"], "Mypassword")

    def test_default_one_week_and_long_confirm(self):
        i = self.store.lock_text("x")
        row = self.store._row(i)
        self.assertEqual(self.store._unlock_at(row), self.fake.t + 7 * 86400)
        with self.assertRaises(NeedsConfirmation):
            self.store.lock_text("x", self.fake.t + 31 * 86400)
        self.store.lock_text("x", self.fake.t + 31 * 86400, confirm_long=True)

    def test_past_and_empty_rejected(self):
        with self.assertRaises(LockError):
            self.store.lock_text("x", self.fake.t - 1)
        with self.assertRaises(LockError):
            self.store.lock_text("", self.fake.t + 5)

    def test_reveal_adds_48h_once(self):
        i = self.store.lock_text("secret", self.fake.t + 100)
        new = self.store.reveal(i)
        self.assertEqual(new, self.fake.t + 100 + 48 * 3600)
        item = self.store.list_items()["items"][0]
        self.assertEqual(item["unlock_at"], new)
        with self.assertRaises(LockError):
            self.store.reveal(i)
        self.fake.t += 101
        with self.assertRaises(LockError):          # not unlocked yet: date moved
            self.store.decrypt_item(i)
        self.fake.t = new
        self.assertEqual(self.store.decrypt_item(i), b"secret")

    def test_tampering_with_date_does_not_unlock(self):
        i = self.store.lock_text("secret", self.fake.t + 100000)
        row = self.store._row(i)
        forged = crypto.hide_date(self.store.vault, i, self.fake.t - 5)   # attacker knows vault
        self.store.conn.execute("UPDATE items SET unlock_hidden=? WHERE id=?", (forged, i))
        self.store.conn.commit()
        with self.assertRaises(LockError) as c:
            self.store.decrypt_item(i)
        self.assertEqual(c.exception.status, 422)
        self.assertEqual(self.store.list_items()["items"][0]["state"], "tampered")

    def test_payload_flip_detected(self):
        i = self.store.lock_text("secret", self.fake.t + 5)
        blob = bytearray(self.store._row(i)["payload"])
        blob[20] ^= 1
        self.store.conn.execute("UPDATE items SET payload=? WHERE id=?", (bytes(blob), i))
        self.store.conn.commit()
        self.fake.t += 5
        with self.assertRaises(LockError):
            self.store.decrypt_item(i)

    def test_clock_rewind_ignored(self):
        i = self.store.lock_text("secret", self.fake.t + 100)
        self.fake.t += 50
        self.store.list_items()                   # records high-water
        self.fake.t -= 3600                       # wind clock back
        self.assertEqual(self.store.clock.now(), 1_800_000_050)

    def test_cannot_delete_locked(self):
        i = self.store.lock_text("secret", self.fake.t + 100)
        with self.assertRaises(LockError):
            self.store.delete(i)
        self.fake.t += 100
        self.store.delete(i)
        self.assertEqual(self.store.list_items()["items"], [])

    def test_image_roundtrip_and_auto_export(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "PNG")
        i, _ = self.store.lock_image(buf.getvalue(), self.fake.t + 3, "Red")
        self.fake.t += 3
        item = self.store.list_items()["items"][0]
        out = Path(self.tmp.name) / "out" / item["exported_path"]
        self.assertTrue(out.is_file())
        self.assertEqual(Image.open(out).format, "JPEG")

    def test_source_delete(self):
        from PIL import Image
        src = Path(self.tmp.name) / "orig.png"
        Image.new("RGB", (4, 4)).save(src)
        i, deleted = self.store.lock_image(src.read_bytes(), self.fake.t + 5, source_path=src,
                                           delete_source=True)
        self.assertTrue(deleted)
        self.assertFalse(src.exists())

    def test_source_kept_when_not_asked(self):
        from PIL import Image
        src = Path(self.tmp.name) / "keep.png"
        Image.new("RGB", (4, 4)).save(src)
        _, deleted = self.store.lock_image(src.read_bytes(), self.fake.t + 5, source_path=src)
        self.assertIsNone(deleted)
        self.assertTrue(src.exists())

    def test_vault_missing_refused(self):
        self.store.lock_text("x", self.fake.t + 5)
        (Path(self.tmp.name) / "vault.key").unlink()
        with self.assertRaises(db.VaultProblem) as c:
            db.load_vault(self.store.conn, Path(self.tmp.name) / "vault.key")
        self.assertEqual(c.exception.kind, "missing_key")

    def test_sqlite_file_has_no_plaintext(self):
        self.store.lock_text("Mypassword-needle", self.fake.t + 5, "label")
        raw = (Path(self.tmp.name) / "t.sqlite").read_bytes()
        self.assertNotIn(b"Mypassword-needle", raw)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        self.client = create_app(make_store(self.tmp.name, self.fake), token="tok").test_client()
        self.client.environ_base["HTTP_X_TIMELOCK_TOKEN"] = "tok"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flow(self):
        r = self.client.post("/api/items/text", json={"text": "hi", "unlock_at": self.fake.t + 5})
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/items/text", json={"text": "hi", "unlock_at": self.fake.t + 40 * 86400})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_confirm"])
        items = self.client.get("/api/items").get_json()["items"]
        self.assertNotIn("text", items[0])
        self.assertEqual(self.client.delete(f"/api/items/{items[0]['id']}").status_code, 423)
        self.assertEqual(self.client.post(f"/api/items/{items[0]['id']}/reveal").status_code, 200)

    def test_token_required(self):
        bare = create_app(make_store(self.tmp.name, self.fake), token="tok").test_client()
        r = bare.get("/api/items")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["code"], "bad_token")
        self.assertEqual(bare.get("/api/items", headers={"X-TimeLock-Token": "wrong"}).status_code, 403)
        self.assertEqual(bare.get("/api/items", headers={"X-TimeLock-Token": "tok"}).status_code, 200)
        self.assertEqual(bare.get("/").status_code, 200)       # the page itself carries the token
        self.assertIn(b'content="tok"', bare.get("/").data)

    def test_odd_requests_get_clean_answers_not_crashes(self):
        bare = create_app(make_store(self.tmp.name, self.fake), token="tok").test_client()
        bad = bare.get("/api/items", headers={"X-TimeLock-Token": "caf\u00e9"})
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(self.client.put("/api/settings", json=[1, 2]).status_code, 400)
        self.assertEqual(self.client.post("/api/items/text", json="just a string").status_code, 400)
        self.assertEqual(self.client.post("/api/decoys", json=[3]).status_code, 400)

    def test_giant_upload_refused_early(self):
        import io
        app = create_app(make_store(self.tmp.name, self.fake), token="tok")
        app.config["MAX_CONTENT_LENGTH"] = 1000
        client = app.test_client()
        client.environ_base["HTTP_X_TIMELOCK_TOKEN"] = "tok"
        r = client.post("/api/items/image", data={"file": (io.BytesIO(b"x" * 5000), "big.png"),
                                                  "label": "x"}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.get_json()["error"])

    def test_safety_headers(self):
        h = self.client.get("/api/items").headers
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")

    def test_extend_and_settings_and_browse(self):
        i = self.client.post("/api/items/text", json={"text": "a", "unlock_at": self.fake.t + 3600}).get_json()["id"]
        self.assertEqual(self.client.post(f"/api/items/{i}/extend", json={"seconds": 3600}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/items/{i}/extend", json={"seconds": 5}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", json={"network_url": "http://x.example"}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", json={"sound_volume": 10}).get_json()["sound_volume"], 10)
        self.assertEqual(self.client.get("/api/browse").status_code, 200)
        self.assertEqual(self.client.get("/api/browse?path=/definitely/not/here").status_code, 404)

    def test_decoys_and_panic_seal_routes(self):
        self.assertEqual(self.client.post("/api/decoys", json={"count": 3}).get_json()["added"], 3)
        self.assertEqual(self.client.post("/api/decoys", json={"count": 99}).status_code, 400)
        self.assertEqual(self.client.post("/api/panic-seal", json={"seconds": 3600}).status_code, 400)
        self.client.post("/api/items/text", json={"text": "q", "unlock_at": self.fake.t + 5})
        self.fake.t += 5
        self.assertEqual(self.client.post("/api/panic-seal", json={"seconds": 3600}).get_json()["sealed"], 1)

    def test_problem_page_when_key_missing(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            conn = db.connect(t / "x.sqlite")
            conn.execute("INSERT INTO items(id,kind,label,created_at,unlock_hidden,wrapped_key,payload) VALUES('a','text','l',1,x'00',x'00',x'00')")
            conn.commit()
            cfg.DB_PATH, cfg.VAULT_KEY_PATH = t / "x.sqlite", t / "none.key"
            try:
                client = create_app().test_client()
                page = client.get("/")
                self.assertEqual(page.status_code, 503)
                self.assertIn(b"Don't worry", page.data)
                self.assertEqual(client.get("/api/items").get_json()["code"], "missing_key")
                self.assertFalse((t / "none.key").exists())      # never invents a new key
            finally:
                cfg.DB_PATH = ORIGINAL_DB
                cfg.VAULT_KEY_PATH = ORIGINAL_KEY

    def test_foreign_origin_and_host_blocked(self):
        self.assertEqual(self.client.get("/api/items", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/items", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/items", headers={"Host": "127.0.0.1:5000"}).status_code, 200)


class RealClockTests(unittest.TestCase):
    def test_few_seconds_real_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)               # real clock
            now = store.clock.now()
            i = store.lock_text("real wait", now + 2)
            with self.assertRaises(LockError):
                store.decrypt_item(i)
            time.sleep(2.2)
            self.assertEqual(store.decrypt_item(i), b"real wait")


if __name__ == "__main__":
    unittest.main()
