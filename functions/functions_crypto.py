"""Standard-library-only crypto: HMAC-SHA256 counter-mode stream cipher with an
encrypt-then-MAC tag, plus key wrapping bound to the unlock timestamp.

Plaintext and item keys are never stored. The wrapping key is derived from the
per-install vault secret AND the unlock time, so editing the stored unlock date
makes the key come out wrong and the integrity check fails."""

from __future__ import annotations

import hashlib
import hmac
import os

NONCE_LEN = 16
TAG_LEN = 32
KEY_LEN = 32


class TamperError(Exception):
    """Integrity check failed: data or its unlock date was modified."""


def _h(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"|".join(parts), hashlib.sha256).digest()


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = []
    for counter in range((length + 31) // 32):
        blocks.append(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
    return b"".join(blocks)[:length]


def new_key() -> bytes:
    return os.urandom(KEY_LEN)


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    enc_key, mac_key = _h(key, b"enc"), _h(key, b"mac")
    nonce = os.urandom(NONCE_LEN)
    ciphertext = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext))) if plaintext else b""
    tag = _h(mac_key, aad, nonce, ciphertext)
    return nonce + ciphertext + tag


def decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise TamperError("payload too short")
    enc_key, mac_key = _h(key, b"enc"), _h(key, b"mac")
    nonce, ciphertext, tag = blob[:NONCE_LEN], blob[NONCE_LEN:-TAG_LEN], blob[-TAG_LEN:]
    if not hmac.compare_digest(tag, _h(mac_key, aad, nonce, ciphertext)):
        raise TamperError("integrity check failed")
    return _xor(ciphertext, _keystream(enc_key, nonce, len(ciphertext))) if ciphertext else b""


def _kek(vault: bytes, item_id: str, unlock_at: int) -> bytes:
    return _h(vault, b"kek", item_id.encode(), str(int(unlock_at)).encode())


def wrap_key(vault: bytes, item_id: str, unlock_at: int, item_key: bytes) -> bytes:
    return _xor(item_key, _kek(vault, item_id, unlock_at))


def unwrap_key(vault: bytes, item_id: str, unlock_at: int, wrapped: bytes) -> bytes:
    return _xor(wrapped, _kek(vault, item_id, unlock_at))


def _date_pad(vault: bytes, item_id: str) -> bytes:
    return _h(vault, b"date", item_id.encode())[:8]


def hide_date(vault: bytes, item_id: str, unlock_at: int) -> bytes:
    """Obscure the unlock date so a database browser does not show it."""
    return _xor(int(unlock_at).to_bytes(8, "big"), _date_pad(vault, item_id))


def show_date(vault: bytes, item_id: str, hidden: bytes) -> int:
    return int.from_bytes(_xor(hidden, _date_pad(vault, item_id)), "big")


def seal_number(vault: bytes, label: bytes, value: int) -> bytes:
    body = int(value).to_bytes(8, "big")
    return body + _h(vault, b"num", label, body)


def open_number(vault: bytes, label: bytes, blob: bytes) -> int | None:
    if len(blob) != 8 + TAG_LEN:
        return None
    body, tag = blob[:8], blob[8:]
    if not hmac.compare_digest(tag, _h(vault, b"num", label, body)):
        return None
    return int.from_bytes(body, "big")


def seal_bytes(vault: bytes, label: bytes, data: bytes) -> bytes:
    """Tamper-evident (not secret) blob: data followed by an HMAC tag."""
    return data + _h(vault, b"seal", label, data)


def open_bytes(vault: bytes, label: bytes, blob: bytes) -> bytes | None:
    if len(blob) < TAG_LEN:
        return None
    data, tag = blob[:-TAG_LEN], blob[-TAG_LEN:]
    if not hmac.compare_digest(tag, _h(vault, b"seal", label, data)):
        return None
    return data


def subkey(vault: bytes, purpose: bytes) -> bytes:
    return _h(vault, b"subkey", purpose)
