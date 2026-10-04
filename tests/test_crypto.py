import os

import pytest

import crypto


def _needs_crypto():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        pytest.skip("cryptography package not installed")


def test_round_trip():
    _needs_crypto()
    data = b"hello, encrypted mirror\x00\xff binary \xf0\x9f\x94\x91"
    blob = crypto.encrypt_bytes(data, "correct horse battery staple")
    assert crypto.is_encrypted_blob(blob)
    assert blob[:4] == crypto.MAGIC
    assert crypto.decrypt_bytes(blob, "correct horse battery staple") == data


def test_wrong_passphrase_fails():
    _needs_crypto()
    blob = crypto.encrypt_bytes(b"secret data", "right-passphrase")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(blob, "wrong-passphrase")


def test_tampered_blob_fails():
    _needs_crypto()
    blob = bytearray(crypto.encrypt_bytes(b"secret data", "pass"))
    blob[-1] ^= 0x01  # flip one bit of the GCM tag/ciphertext
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(blob), "pass")


def test_truncated_and_foreign_blobs_rejected():
    _needs_crypto()
    blob = crypto.encrypt_bytes(b"secret data", "pass")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(blob[:10], "pass")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(b"definitely not an encrypted blob", "pass")
    assert not crypto.is_encrypted_blob(b"short")


def test_randomized_output():
    _needs_crypto()
    # Same input -> different blobs (random salt + nonce), both decrypt fine.
    b1 = crypto.encrypt_bytes(b"same", "pass")
    b2 = crypto.encrypt_bytes(b"same", "pass")
    assert b1 != b2
    assert crypto.decrypt_bytes(b1, "pass") == b"same"
    assert crypto.decrypt_bytes(b2, "pass") == b"same"


def test_empty_passphrase_rejected():
    _needs_crypto()
    with pytest.raises(crypto.EncryptionError):
        crypto.encrypt_bytes(b"data", "")
    blob = crypto.encrypt_bytes(b"data", "pass")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(blob, "")


def test_fingerprint_is_sha256_hex():
    _needs_crypto()
    blob = crypto.encrypt_bytes(b"data", "pass")
    fp = crypto.blob_fingerprint(blob)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
    # Fingerprint reveals nothing: differs per encryption, stable per blob.
    assert crypto.blob_fingerprint(blob) == fp
    assert crypto.blob_fingerprint(crypto.encrypt_bytes(b"data", "pass")) != fp


def test_large_payload_round_trip():
    _needs_crypto()
    data = os.urandom(2 * 1024 * 1024)  # 2 MiB, like a small bundle
    blob = crypto.encrypt_bytes(data, "pass")
    assert crypto.decrypt_bytes(blob, "pass") == data
