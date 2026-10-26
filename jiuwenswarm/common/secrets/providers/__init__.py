"""Built-in algorithm providers."""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from pathlib import Path


class BuiltinAlgorithm(ABC):
    name: str

    @abstractmethod
    def encrypt(self, plaintext: str) -> tuple[str, str]:
        """Return (wrap_b64, payload_b64) for envelope."""

    @abstractmethod
    def decrypt(self, wrap_b64: str, payload_b64: str) -> str: ...


class Aes256GcmAlgorithm(BuiltinAlgorithm):
    name = "aes256gcm"

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("aes256gcm master key must be 32 bytes")
        self._master_key = master_key

    @classmethod
    def from_sources(
        cls,
        *,
        master_key_env: str = "JIUWEN_SECRET_MASTER_KEY",
        master_key_file: str = "~/.jiuwenswarm/config/.master_key",
    ) -> Aes256GcmAlgorithm:
        raw = os.environ.get(master_key_env, "").strip()
        if not raw:
            key_path = Path(master_key_file).expanduser()
            if key_path.is_file():
                raw = key_path.read_text(encoding="utf-8").strip()
        if not raw:
            raise ValueError(
                f"aes256gcm master key not found in env {master_key_env!r} or file {master_key_file!r}"
            )
        key_bytes = (
            base64.b64decode(raw) if _looks_like_b64(raw) else raw.encode("utf-8")
        )
        if len(key_bytes) != 32:
            key_bytes = _derive_32(key_bytes)
        return cls(key_bytes)

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ct = AESGCM(self._master_key).encrypt(nonce, plaintext.encode("utf-8"), None)
        payload = base64.b64encode(nonce + ct).decode("ascii")
        return "-", payload

    def decrypt(self, wrap_b64: str, payload_b64: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.b64decode(payload_b64)
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(self._master_key).decrypt(nonce, ct, None).decode("utf-8")


class DekAlgorithm(BuiltinAlgorithm):
    name = "dek"

    def __init__(self, private_key_raw: bytes) -> None:
        self._private_key_raw = private_key_raw

    @classmethod
    def from_private_key_b64(cls, private_key_b64: str) -> DekAlgorithm:
        raw = base64.b64decode(private_key_b64.strip())
        return cls(raw)

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives import serialization

        _raw = serialization.Encoding.Raw
        _pub = serialization.PublicFormat.Raw

        dek = os.urandom(32)
        esk = X25519PrivateKey.generate()
        epk = esk.public_key().public_bytes(_raw, _pub)
        peer = X25519PrivateKey.from_private_bytes(self._private_key_raw).public_key()
        shared = esk.exchange(peer)
        kek = _derive_32(shared)
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(kek).encrypt(nonce, dek, None)
        wrap_b64 = base64.b64encode(epk + wrapped).decode("ascii")

        data_nonce = os.urandom(12)
        payload_ct = AESGCM(dek).encrypt(data_nonce, plaintext.encode("utf-8"), None)
        payload_b64 = base64.b64encode(data_nonce + payload_ct).decode("ascii")
        return wrap_b64, payload_b64

    def decrypt(self, wrap_b64: str, payload_b64: str) -> str:
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        wrap_raw = base64.b64decode(wrap_b64)
        epk, wrapped = wrap_raw[:32], wrap_raw[32:]
        sk = X25519PrivateKey.from_private_bytes(self._private_key_raw)
        shared = sk.exchange(X25519PublicKey.from_public_bytes(epk))
        kek = _derive_32(shared)
        w_nonce, w_ct = wrapped[:12], wrapped[12:]
        dek = AESGCM(kek).decrypt(w_nonce, w_ct, None)

        payload_raw = base64.b64decode(payload_b64)
        p_nonce, p_ct = payload_raw[:12], payload_raw[12:]
        return AESGCM(dek).decrypt(p_nonce, p_ct, None).decode("utf-8")


def _looks_like_b64(text: str) -> bool:
    try:
        decoded = base64.b64decode(text, validate=True)
        return len(decoded) == 32
    except Exception:
        return False


def _derive_32(raw: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(raw).digest()
