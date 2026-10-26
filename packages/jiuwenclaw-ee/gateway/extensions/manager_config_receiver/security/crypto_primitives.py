# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""配置下发安全的非对称密码学原语（与 Manager 端逐字节一致）。

- 签名：Ed25519（Gateway 用握手分发的 Manager 公钥验签）。
- 加密：信封加密——AES-256-GCM 加密数据，DEK 用 X25519(ECDH)+HKDF 包裹；
  Gateway 用自身 X25519 私钥解包 DEK 再解字段。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_RAW = serialization.Encoding.Raw
_PRIV_RAW = serialization.PrivateFormat.Raw
_PUB_RAW = serialization.PublicFormat.Raw
_NOENC = serialization.NoEncryption()
_HKDF_INFO = b"jiuwenclaw-config-dek-v1"


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text)


def fingerprint(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()


# ---------- Ed25519 验签 ----------

def ed25519_generate() -> tuple[bytes, bytes]:
    sk = Ed25519PrivateKey.generate()
    priv = sk.private_bytes(_RAW, _PRIV_RAW, _NOENC)
    pub = sk.public_key().public_bytes(_RAW, _PUB_RAW)
    return priv, pub


def ed25519_sign(private_raw: bytes, data: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(private_raw).sign(data)


def ed25519_verify(public_raw: bytes, data: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        # 签名不符或公钥/签名格式非法 → 统一返回 False（fail-closed）。
        return False


# ---------- X25519 信封 ----------

def x25519_generate() -> tuple[bytes, bytes]:
    sk = X25519PrivateKey.generate()
    priv = sk.private_bytes(_RAW, _PRIV_RAW, _NOENC)
    pub = sk.public_key().public_bytes(_RAW, _PUB_RAW)
    return priv, pub


def _derive_kek(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared)


def wrap_dek(peer_public_raw: bytes, dek: bytes) -> tuple[bytes, bytes]:
    esk = X25519PrivateKey.generate()
    epk = esk.public_key().public_bytes(_RAW, _PUB_RAW)
    shared = esk.exchange(X25519PublicKey.from_public_bytes(peer_public_raw))
    kek = _derive_kek(shared)
    nonce = os.urandom(12)
    ct = AESGCM(kek).encrypt(nonce, dek, None)
    return epk, nonce + ct


def unwrap_dek(private_raw: bytes, epk_raw: bytes, wrapped: bytes) -> bytes:
    sk = X25519PrivateKey.from_private_bytes(private_raw)
    shared = sk.exchange(X25519PublicKey.from_public_bytes(epk_raw))
    kek = _derive_kek(shared)
    nonce, ct = wrapped[:12], wrapped[12:]
    return AESGCM(kek).decrypt(nonce, ct, None)


# ---------- AES-256-GCM 字段数据 ----------

def aes_gcm_encrypt(dek: bytes, plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)
    return b64e(nonce + ct)


def aes_gcm_decrypt(dek: bytes, ciphertext_b64: str) -> str:
    raw = b64d(ciphertext_b64)
    return AESGCM(dek).decrypt(raw[:12], raw[12:], None).decode("utf-8")
