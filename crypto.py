#!/usr/bin/env python3
"""
Passphrase-based file encryption for the encrypted-mirror feature.

Every Gitea repository is serialized with `git bundle` (full history, all
branches and tags) and then encrypted as one opaque blob before it is pushed
to GitHub. Nothing readable — no file names, contents, commit messages or
branch names — ever leaves the machine unencrypted.

Binary format (all fields big, concatenated):
    MAGIC  4 bytes  b"GGM1"  (format marker)
    SALT  16 bytes  random per file
    NONCE 12 bytes  random per file
    DATA   N bytes  AES-256-GCM ciphertext (16-byte auth tag appended)

Key derivation: PBKDF2-HMAC-SHA256(passphrase, salt, 600_000 iterations).

Requires the `cryptography` package (only needed for encrypted mirroring):
    pip install cryptography
    # or
    pip install -r requirements-encrypted.txt
"""

import hashlib
import hmac
import os

MAGIC = b"GGM1"
FORMAT_NAME = "ggm-bundle-enc-v1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
PBKDF2_ITERATIONS = 600_000
MIN_BLOB_LEN = len(MAGIC) + SALT_LEN + NONCE_LEN + 16  # header + GCM tag


class EncryptionError(Exception):
    """Raised when encryption cannot be performed."""


class DecryptionError(Exception):
    """Raised when decryption fails (wrong passphrase or corrupt/tampered data)."""


def _require_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        return AESGCM, PBKDF2HMAC
    except ImportError as e:
        raise EncryptionError(
            "The 'cryptography' package is required for encrypted mirroring. "
            "Install it with: pip install cryptography  "
            "(or: pip install -r requirements-encrypted.txt)"
        ) from e


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    AESGCM, PBKDF2HMAC = _require_crypto()
    from cryptography.hazmat.primitives import hashes

    if not passphrase:
        raise EncryptionError("Passphrase must not be empty.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    """Encrypt bytes with the passphrase. Returns the opaque blob."""
    AESGCM, _ = _require_crypto()
    if not passphrase:
        raise EncryptionError("Passphrase must not be empty.")
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, data, None)
    blob = MAGIC + salt + nonce + ciphertext
    # Best effort: don't leave key material around longer than needed.
    _wipe(bytearray(key))
    return blob


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    """Decrypt a blob produced by encrypt_bytes. Raises DecryptionError on failure."""
    AESGCM, _ = _require_crypto()
    if not passphrase:
        raise DecryptionError("Passphrase must not be empty.")
    if not is_encrypted_blob(blob):
        raise DecryptionError(
            "Not an encrypted-mirror blob (bad magic). "
            "The file may be corrupt, truncated, or not produced by this tool."
        )
    salt = blob[4 : 4 + SALT_LEN]
    nonce = blob[4 + SALT_LEN : 4 + SALT_LEN + NONCE_LEN]
    ciphertext = blob[4 + SALT_LEN + NONCE_LEN :]
    try:
        key = _derive_key(passphrase, salt)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as e:
        # AESGCM raises InvalidTag on wrong passphrase or tampered data.
        raise DecryptionError(
            "Decryption failed: wrong passphrase or the data was modified/corrupted."
        ) from e
    finally:
        try:
            _wipe(bytearray(key))
        except Exception:
            pass
    return plaintext


def is_encrypted_blob(blob: bytes) -> bool:
    """Quick check: does this look like one of our encrypted blobs?"""
    return len(blob) >= MIN_BLOB_LEN and blob[: len(MAGIC)] == MAGIC


def blob_fingerprint(blob: bytes) -> str:
    """SHA-256 hex of a blob (safe to store/log; reveals nothing about content)."""
    return hashlib.sha256(blob).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _wipe(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0
