"""Ed25519 signing — CLAUDE.md §7."""

from __future__ import annotations

import os
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture
def signing(capsule_dirs):
    """Reset the signing cache between tests so each starts with no keypair."""
    from app import signing as s

    s._reset_cache_for_tests()
    yield s
    s._reset_cache_for_tests()


def test_ensure_keypair_creates_files(signing, capsule_dirs):
    kp = signing.ensure_keypair()
    assert isinstance(kp.private, Ed25519PrivateKey)
    assert signing.private_key_path().exists()
    assert signing.public_key_path().exists()


def test_private_key_mode_is_0600(signing):
    signing.ensure_keypair()
    if os.name != "nt":  # POSIX-only assertion
        mode = stat.S_IMODE(signing.private_key_path().stat().st_mode)
        assert mode == 0o600


def test_public_key_mode_is_0644(signing):
    signing.ensure_keypair()
    if os.name != "nt":
        mode = stat.S_IMODE(signing.public_key_path().stat().st_mode)
        assert mode == 0o644


def test_ensure_keypair_caches(signing):
    a = signing.ensure_keypair()
    b = signing.ensure_keypair()
    assert a is b


def test_ensure_keypair_loads_existing(signing):
    first = signing.ensure_keypair()
    fp1 = signing.fingerprint(first.public)
    signing._reset_cache_for_tests()
    second = signing.ensure_keypair()
    fp2 = signing.fingerprint(second.public)
    assert fp1 == fp2


def test_sign_and_verify_round_trip(signing):
    signing.ensure_keypair()
    payload = b"hello world"
    sig = signing.sign(payload)
    assert signing.verify(payload, sig) is True


def test_verify_rejects_tampered_payload(signing):
    signing.ensure_keypair()
    payload = b"hello world"
    sig = signing.sign(payload)
    assert signing.verify(b"hello WORLD", sig) is False


def test_verify_rejects_tampered_signature(signing):
    signing.ensure_keypair()
    payload = b"hello world"
    sig = bytearray(signing.sign(payload))
    sig[0] ^= 0xFF
    assert signing.verify(payload, bytes(sig)) is False


def test_fingerprint_is_stable(signing):
    kp = signing.ensure_keypair()
    fp1 = signing.fingerprint(kp.public)
    fp2 = signing.fingerprint(kp.public)
    assert fp1 == fp2
    assert len(fp1) == 32
    assert all(c in "0123456789abcdef" for c in fp1)


def test_sign_file_writes_sig_next_to_file(signing, tmp_path):
    signing.ensure_keypair()
    target = tmp_path / "meta.json"
    target.write_bytes(b'{"hello":"world"}')
    sig_path = signing.sign_file(target)
    assert sig_path == tmp_path / "meta.json.sig"
    assert sig_path.exists()
    assert signing.verify(target.read_bytes(), sig_path.read_bytes()) is True


def test_import_keypair_replaces_active_key(signing):
    first = signing.ensure_keypair()
    fp_old = signing.fingerprint(first.public)

    new_priv = Ed25519PrivateKey.generate()
    new_pem = new_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    new_kp = signing.import_keypair(new_pem)
    fp_new = signing.fingerprint(new_kp.public)
    assert fp_new != fp_old

    # Subsequent ensure_keypair returns the imported one (cache invalidated).
    assert signing.ensure_keypair().public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == new_kp.public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def test_import_rejects_non_ed25519(signing):
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(ValueError):
        signing.import_keypair(rsa_pem)
