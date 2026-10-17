"""L3: plaintext <-> stored string transforms."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jiuwenswarm.common.security.base_crypto import CryptoProvider
from jiuwenswarm.common.secrets.envelope import build_envelope, parse_envelope
from jiuwenswarm.common.secrets.legacy import is_legacy_sensitive_key
from jiuwenswarm.common.secrets.providers import Aes256GcmAlgorithm, BuiltinAlgorithm, DekAlgorithm

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SecretTransform:
    def __init__(self) -> None:
        self._custom_crypto: CryptoProvider | None = None
        self._builtin: dict[str, BuiltinAlgorithm] = {}

    def register_custom_crypto(self, provider: CryptoProvider | None) -> None:
        self._custom_crypto = provider

    def configure_aes256gcm(
        self,
        *,
        master_key_env: str = "JIUWEN_SECRET_MASTER_KEY",
        master_key_file: str = "~/.jiuwenswarm/config/.master_key",
    ) -> None:
        algo = Aes256GcmAlgorithm.from_sources(
            master_key_env=master_key_env,
            master_key_file=master_key_file,
        )
        self._builtin[algo.name] = algo

    def configure_dek(self, *, private_key_b64: str) -> None:
        algo = DekAlgorithm.from_private_key_b64(private_key_b64)
        self._builtin[algo.name] = algo

    def encode_for_store(
        self,
        logical_key: str,
        plaintext: str,
        *,
        algorithm: str | None,
        legacy_name: str,
    ) -> str:
        if plaintext == "":
            return ""
        if algorithm:
            algo = self._builtin.get(algorithm)
            if algo is None:
                raise ValueError(f"algorithm {algorithm!r} is not configured")
            wrap_b64, payload_b64 = algo.encrypt(plaintext)
            return build_envelope(algorithm, wrap_b64, payload_b64)
        if self._custom_crypto and is_legacy_sensitive_key(legacy_name):
            try:
                return self._custom_crypto.encrypt(plaintext)
            except Exception as exc:
                logger.warning("Custom encrypt failed for %s: %s", legacy_name, exc)
                return plaintext
        return plaintext

    def decode_from_store(
        self,
        logical_key: str,
        stored: str,
        *,
        legacy_name: str,
    ) -> str:
        if not stored:
            return ""
        parsed = parse_envelope(stored)
        if parsed is not None:
            algorithm, wrap_b64, payload_b64 = parsed
            algo = self._builtin.get(algorithm)
            if algo is None:
                logger.warning("Envelope algorithm %s not configured; returning raw", algorithm)
                return stored
            try:
                return algo.decrypt(wrap_b64, payload_b64)
            except Exception as exc:
                logger.warning("Builtin decrypt failed for %s: %s", algorithm, exc)
                return stored
        if self._custom_crypto and is_legacy_sensitive_key(legacy_name):
            try:
                return self._custom_crypto.decrypt(stored)
            except Exception as exc:
                logger.warning("Custom decrypt failed for %s: %s", legacy_name, exc)
                return stored
        return stored
