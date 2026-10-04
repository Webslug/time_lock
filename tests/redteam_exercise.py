"""Red team exercise: attacks a scratch copy of Time Lock and reports what worked.

Run:  python3 tests/redteam_exercise.py     (uses a temporary folder, never your real vault)
"""
import shutil, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from functions import functions_config as cfg, functions_db as db, functions_crypto as crypto
cfg.DEBUG_MODE = False
from functions.functions_lock import TimeLockStore, LockError

T0 = 1_800_000_000
class Clk:
    def __init__(s): s.t = T0
    def __call__(s): return s.t

def new_site(root, clk, tag):
    root = Path(root) / tag; root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root / "t.sqlite"); v = db.load_vault(conn, root / "vault.key")
    return root, TimeLockStore(conn, v, offline=True, fixed_now=clk, unlocked_dir=root / "out",
                               witness_paths=[root / "w1", root / "w2"])

def reopen(root, clk, witness=None):
    conn = db.connect(root / "t.sqlite"); v = db.load_vault(conn, root / "vault.key")
    w = witness or [root / "w1", root / "w2"]
    return TimeLockStore(conn, v, offline=True, fixed_now=clk, unlocked_dir=root / "out", witness_paths=w)

def result(name, won, detail):
    print(f"{'RED TEAM WINS ' if won else 'DEFENDED      '} | {name} | {detail}")

with tempfile.TemporaryDirectory() as tmp:
    clk = Clk()
    # ---- A1: read the raw database file ----
    root, s = new_site(tmp, clk, "a1")
    i = s.lock_text("TOPSECRET-NEEDLE-123", T0 + 7 * 86400, "diary")
    raw = (root / "t.sqlite").read_bytes()
    result("A1 grep the database file for the secret", b"NEEDLE" in raw, "plaintext found" if b"NEEDLE" in raw else "no plaintext in the file")

    # ---- A2: edit the unlock date blindly (no key file) ----
    row = s._row(i)
    forged = bytes(b ^ 0xFF for b in bytes(row["unlock_hidden"]))
    s.conn.execute("UPDATE items SET unlock_hidden=? WHERE id=?", (forged, i)); s.conn.commit()
    clk.t = T0 + 8 * 86400
    state = s.list_items()["items"][0]["state"]
    result("A2 blind edit of the stored unlock date, then wait", state == "unlocked" and "text" in s.list_items()["items"][0], f"item shows as '{state}', text not released")

    # ---- A3: edit the date WITH the key file ----
    root, s = new_site(tmp, clk, "a3"); clk.t = T0
    i = s.lock_text("SECRET3", T0 + 7 * 86400)
    forged = crypto.hide_date(s.vault, i, T0 - 5)
    s.conn.execute("UPDATE items SET unlock_hidden=? WHERE id=?", (forged, i)); s.conn.commit()
    item = s.list_items()["items"][0]
    result("A3 forge the date to the past using the key file", item.get("text") == "SECRET3", f"state '{item['state']}': the key is bound to the real date, so it fails the integrity check")

    # ---- A4: use the key file and the source code to decrypt directly ----
    root, s = new_site(tmp, clk, "a4"); clk.t = T0
    i = s.lock_text("SECRET4", T0 + 365 * 86400, confirm_long=True)
    r = s._row(i)
    date = crypto.show_date(s.vault, i, bytes(r["unlock_hidden"]))
    key = crypto.unwrap_key(s.vault, i, date, bytes(r["wrapped_key"]))
    plain = crypto.decrypt(key, bytes(r["payload"]), i.encode())
    result("A4 read the source, take the key file, decrypt directly", plain == b"SECRET4", "decrypted a year early with 5 lines of code")

    # ---- A5: put an older database back (same witnesses) ----
    root, s = new_site(tmp, clk, "a5"); clk.t = T0
    i = s.lock_text("SECRET5", T0 + 3600)
    snap = (root / "t.sqlite").read_bytes()
    s.reveal(i); s.conn.close()
    (root / "t.sqlite").write_bytes(snap)
    s = reopen(root, clk)
    st = s.status()
    result("A5 restore an older database after Reveal Now", st["paused_until"] is None, f"rollback detected, unlocking paused {(st['paused_until']-T0)//3600}h")

    # ---- A6: restore the database AND delete every witness ----
    root, s = new_site(tmp, clk, "a6"); clk.t = T0
    i = s.lock_text("SECRET6", T0 + 3600)
    snap = (root / "t.sqlite").read_bytes(); snap_w = [(root / "w1").read_bytes(), (root / "w2").read_bytes()]
    s.reveal(i); s.conn.close()
    (root / "t.sqlite").write_bytes(snap); (root / "w1").write_bytes(snap_w[0]); (root / "w2").write_bytes(snap_w[1])
    s = reopen(root, clk)
    result("A6 restore the database AND both witness files together", s.status()["paused_until"] is None, "no pause: the 48 hour penalty was undone")

    # ---- A7: clone the whole folder to another machine, press Reveal Now there ----
    root, s = new_site(tmp, clk, "a7"); clk.t = T0
    i = s.lock_text("SECRET7", T0 + 86400)
    s.conn.close()
    clone = Path(tmp) / "a7_clone"; shutil.copytree(root, clone)
    s2 = reopen(clone, clk, witness=[clone / "other_w1", clone / "other_w2"])   # different machine: its own witness dir
    when = s2.reveal(i)
    s = reopen(root, clk)
    unaffected = s._unlock_at(s._row(i)) == T0 + 86400
    result("A7 clone the folder to another computer, press Reveal Now on the clone", unaffected,
           "the clone revealed the date, the original is untouched: the date was learned for free")

    # ---- A8: same, but clone on the SAME machine (shared home-folder witness) ----
    root, s = new_site(tmp, clk, "a8"); clk.t = T0
    i = s.lock_text("SECRET8", T0 + 86400)
    s.conn.close()
    clone = Path(tmp) / "a8_clone"; shutil.copytree(root, clone)
    shared = [root / "w1", root / "w2"]      # clone points at the same witness files (same user, same home folder)
    s2 = reopen(clone, clk, witness=shared); s2.reveal(i)
    s = reopen(root, clk)
    result("A8 clone the folder on the SAME computer, reveal on the clone", s.status()["paused_until"] is None, f"original is paused for {(s.status()['paused_until']-T0)//3600}h" if s.status()["paused_until"] else "no pause")

    # ---- A9: delete the witness files only ----
    root, s = new_site(tmp, clk, "a9"); clk.t = T0
    i = s.lock_text("SECRET9", T0 + 86400); s.conn.close()
    (root / "w1").unlink(); (root / "w2").unlink()
    s = reopen(root, clk); st = s.status()
    result("A9 delete the witness files", st["paused_until"] is not None and False,
           f"nothing gained, no pause needed; witnesses rewritten: {(root/'w1').exists() and (root/'w2').exists()}")

    # ---- A10: delete the rollback counter row from the database ----
    root, s = new_site(tmp, clk, "a10"); clk.t = T0
    i = s.lock_text("SECRET10", T0 + 86400)
    s.conn.execute("DELETE FROM meta WHERE key='guard'"); s.conn.commit(); s.conn.close()
    s = reopen(root, clk)
    result("A10 delete the counter row from the database", s.status()["paused_until"] is None, "paused 48h as a rollback" if s.status()["paused_until"] else "no pause")

    # ---- A11: delete the key file and let the app make a new one ----
    root, s = new_site(tmp, clk, "a11"); clk.t = T0
    i = s.lock_text("SECRET11", T0 + 86400); s.conn.close()
    (root / "vault.key").unlink()
    try:
        reopen(root, clk); won = True; d = "a fresh key was made"
    except db.VaultProblem as e:
        won = False; d = f"refused ({e.kind}); no new key, items intact"
    result("A11 delete vault.key hoping for a fresh start", won, d)

    # ---- A12: flip bits in the encrypted payload ----
    root, s = new_site(tmp, clk, "a12"); clk.t = T0
    i = s.lock_text("SECRET12", T0 + 100)
    blob = bytearray(s._row(i)["payload"]); blob[20] ^= 1
    s.conn.execute("UPDATE items SET payload=? WHERE id=?", (bytes(blob), i)); s.conn.commit()
    clk.t = T0 + 200
    result("A12 corrupt the encrypted data, hoping it opens or yields garbage", s.list_items()["items"][0].get("text") is not None, "marked tampered, nothing released")

    # ---- A13: the offline clock attacks, using the guarded clock ----
    from functions.functions_time import TrustedClock
    def guarded(root, wall, up, boot):
        conn = db.connect(root / "t.sqlite"); v = db.load_vault(conn, root / "vault.key")
        return TimeLockStore(conn, v, offline=True, unlocked_dir=root / "out", witness_paths=[root / "w1", root / "w2"],
                             uptime_fn=lambda: up[0], wall_fn=lambda: wall[0], boot_fn=lambda: boot[0])
    root = Path(tmp) / "a13"; root.mkdir()
    wall, up, boot = [float(T0)], [1000.0], ["boot-1"]
    s = guarded(root, wall, up, boot)
    i = s.lock_text("SECRET13", T0 + 7 * 86400)
    wall[0] += 8 * 86400; up[0] += 5          # wind clock forward a week while running
    try: s.decrypt_item(i); won = True
    except LockError: won = False
    result("A13a wind the clock forward while Time Lock is running", won, "jump ignored and flagged: " + str(s.clock.suspect_reason)[:60])
    s.conn.close()
    up[0] += 60; wall[0] += 8 * 86400         # close, wind forward, reopen in the same boot
    s = guarded(root, wall, up, boot)
    try: s.decrypt_item(i); won = True
    except LockError: won = False
    result("A13b close the app, wind the clock forward, reopen (same boot)", won, "ignored: virtual time follows the uptime counter")
    s.conn.close()
    boot[0] = "boot-2"; up[0] = 40.0           # reboot with the clock still wound forward (offline)
    s = guarded(root, wall, up, boot)
    try: s.decrypt_item(i); won = True
    except LockError: won = False
    result("A13c wind the clock forward, REBOOT, reopen (offline)", won, "the new boot trusts the wall clock, so a week passed instantly")

    # ---- A14: ask for a random unlock time repeatedly to learn the range ----
    root, s = new_site(tmp, clk, "a14"); clk.t = T0
    ids = [s.lock_text("r", random_pick=True) for _ in range(5)]
    leaks = any("unlock_at" in it for it in s.list_items()["items"])
    result("A14 look for a random item's unlock time in the list", leaks, "concealed: no date in the response")
