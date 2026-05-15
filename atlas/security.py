"""ATLAS credential encryption.

Design (per Round 3, Q18):

- On first launch, the operator sets a master password.
- A 256-bit key is derived from the password via Argon2id (memory-hard KDF).
- Sensitive values (Anthropic API key, AAVSO/MPC/TNS tokens, ntfy.sh topic)
  are encrypted with AES-256-GCM using that key and stored in the
  ``credentials`` table.
- On subsequent launches, the operator enters the master password once;
  the key is held in memory for the lifetime of the process.

The master password itself is never stored. We store only an Argon2 hash
(``master_password.lock``) so we can verify the password before deriving the
encryption key.

This file is deliberately small and dependency-light: cryptography +
argon2-cffi. Nothing in atlas imports from here at module level except the
security manager singleton.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ---- Argon2 parameters (interactive, ~250ms on modern hardware) -------------

ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 64 * 1024  # 64 MiB
ARGON2_PARALLELISM = 2
ARGON2_HASH_LEN = 32  # bytes (AES-256 key)
ARGON2_SALT_LEN = 16

# Marker file storing the master password verifier (Argon2 hash + salt)
LOCK_FILENAME = "master_password.lock"


@dataclass
class LockFile:
    salt: bytes          # for KDF (key derivation)
    verifier: str        # Argon2 hash (string form) for password verification

    @classmethod
    def load(cls, path: Path) -> "LockFile":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            salt=base64.b64decode(data["salt"]),
            verifier=data["verifier"],
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "verifier": self.verifier,
        }), encoding="utf-8")


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from password + salt using Argon2id."""
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID,
    )


# ---- Public API -------------------------------------------------------------

class CredentialVault:
    """Encrypted credential storage.

    Encrypts values with AES-256-GCM. Decrypted values are returned as
    strings. Nonces are random (96-bit) and prepended to the ciphertext.
    """

    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._key: bytes | None = None

    # --- master password lifecycle -----------------------------------------

    @property
    def is_initialised(self) -> bool:
        """True if a master password has been set."""
        return self._lock_path.exists()

    @property
    def is_unlocked(self) -> bool:
        """True if the vault has a key in memory."""
        return self._key is not None

    def initialise(self, password: str) -> None:
        """Create a new master password. First-launch only."""
        if self.is_initialised:
            raise RuntimeError("Vault is already initialised. Use unlock() instead.")
        if len(password) < 8:
            raise ValueError("Master password must be at least 8 characters.")
        salt = secrets.token_bytes(ARGON2_SALT_LEN)
        verifier = PasswordHasher(
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
        ).hash(password)
        LockFile(salt=salt, verifier=verifier).save(self._lock_path)
        self._key = _derive_key(password, salt)

    def unlock(self, password: str) -> bool:
        """Unlock an existing vault. Returns False on wrong password."""
        if not self.is_initialised:
            raise RuntimeError("Vault not initialised. Use initialise() first.")
        lock = LockFile.load(self._lock_path)
        try:
            PasswordHasher().verify(lock.verifier, password)
        except VerifyMismatchError:
            return False
        self._key = _derive_key(password, lock.salt)
        return True

    def lock(self) -> None:
        """Drop the key from memory."""
        self._key = None

    # --- encrypt / decrypt --------------------------------------------------

    def encrypt(self, plaintext: str) -> bytes:
        if not self._key:
            raise RuntimeError("Vault is locked.")
        aesgcm = AESGCM(self._key)
        nonce = secrets.token_bytes(12)
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return nonce + ct

    def decrypt(self, blob: bytes) -> str:
        if not self._key:
            raise RuntimeError("Vault is locked.")
        aesgcm = AESGCM(self._key)
        nonce, ct = blob[:12], blob[12:]
        return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


# Singleton accessor ----------------------------------------------------------

_vault: CredentialVault | None = None


def get_vault() -> CredentialVault:
    """Return the global vault. Creates it on first call."""
    global _vault
    if _vault is None:
        from atlas.config import get_settings
        s = get_settings()
        _vault = CredentialVault(s.install_root / LOCK_FILENAME)
    return _vault
