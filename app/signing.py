"""Ed25519 signing — meta.json files and the evidence-export manifest
(CLAUDE.md §7).

The keypair lives at ``$CAPSULE_CONFIG_DIR/keys/{private,public}_key.pem``
with file modes 0600/0644. ``ensure_keypair()`` creates the keypair on
first call and loads it thereafter; subsequent calls are pure cache lookups.

``import_keypair()`` lets investigators bring their own keypair (e.g. one
that survives reinstalling the container). Replacing the key does **not**
re-sign existing items; that policy is enforced by the caller in
``cases.py`` / ``main.py``, which writes a ``key.imported`` audit-log entry.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import config

__all__ = [
    "Keypair",
    "ensure_keypair",
    "fingerprint",
    "sign",
    "verify",
    "sign_file",
    "import_keypair",
    "private_key_path",
    "public_key_path",
    "keys_dir",
]

def keys_dir() -> Path:
    """Live keys-dir lookup — reflects ``config.CONFIG_DIR`` at call time
    so tests that swap the config dir don't see stale paths.
    """
    return config.CONFIG_DIR / "keys"


def private_key_path() -> Path:
    return keys_dir() / "private_key.pem"


def public_key_path() -> Path:
    return keys_dir() / "public_key.pem"


@dataclass(frozen=True)
class Keypair:
    private: Ed25519PrivateKey
    public: Ed25519PublicKey


_lock = threading.Lock()
_cache: Keypair | None = None


def _write_pem(priv: Ed25519PrivateKey) -> None:
    keys_dir().mkdir(parents=True, exist_ok=True)
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = private_key_path()
    pub_path = public_key_path()
    priv_path.write_bytes(priv_bytes)
    pub_path.write_bytes(pub_bytes)
    os.chmod(priv_path, 0o600)
    os.chmod(pub_path, 0o644)


def _load_pem() -> Keypair:
    priv = serialization.load_pem_private_key(
        private_key_path().read_bytes(), password=None
    )
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("private_key.pem is not an Ed25519 key")
    return Keypair(private=priv, public=priv.public_key())


def ensure_keypair() -> Keypair:
    """Generate (first call) or load (later calls) the active keypair.

    Thread-safe; the result is cached until ``import_keypair`` invalidates it.
    """
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if not private_key_path().exists():
            priv = Ed25519PrivateKey.generate()
            _write_pem(priv)
        _cache = _load_pem()
        return _cache


FINGERPRINT_HEX_LEN = 32


def fingerprint(public: Ed25519PublicKey | None = None) -> str:
    """SHA-256 fingerprint of the DER-encoded public key (32 hex chars).

    128 bits of output — long enough that grinding a colliding keypair is
    not feasible on commodity hardware. Stable across processes for the
    same key. Shown in Settings, About, and every evidence export so an
    investigator can confirm a recipient is looking at the right key.
    """
    pk = public or ensure_keypair().public
    der = pk.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:FINGERPRINT_HEX_LEN]


def sign(data: bytes, private: Ed25519PrivateKey | None = None) -> bytes:
    """Detached Ed25519 signature over ``data``."""
    priv = private or ensure_keypair().private
    return priv.sign(data)


def verify(
    data: bytes, signature: bytes, public: Ed25519PublicKey | None = None
) -> bool:
    """Return True iff ``signature`` is valid for ``data`` under the active
    (or supplied) public key.

    Never raises — caller can branch on the bool. Use only for *expected*
    success/fail flows; cryptography's exceptions still propagate when keys
    or signatures are structurally malformed.
    """
    pk = public or ensure_keypair().public
    try:
        pk.verify(signature, data)
        return True
    except Exception:  # pragma: no cover — InvalidSignature is the normal path
        return False


def sign_file(path: Path) -> Path:
    """Sign ``path`` and write the signature next to it as ``{path}.sig``."""
    data = path.read_bytes()
    sig = sign(data)
    sig_path = path.with_name(path.name + ".sig")
    sig_path.write_bytes(sig)
    return sig_path


def import_keypair(private_pem: bytes) -> Keypair:
    """Replace the active keypair with one provided by the investigator.

    The new keypair is persisted to ``keys/`` and the in-memory cache is
    invalidated so subsequent ``ensure_keypair()`` calls see the new key.
    Existing meta.json signatures are **not** re-signed; that policy is
    enforced by the caller, which is also responsible for writing a
    ``key.imported`` audit-log entry.
    """
    priv = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("imported key is not an Ed25519 private key")
    global _cache
    with _lock:
        _write_pem(priv)
        _cache = Keypair(private=priv, public=priv.public_key())
        return _cache


def _reset_cache_for_tests() -> None:
    """Test helper. Not part of the public API."""
    global _cache
    with _lock:
        _cache = None
