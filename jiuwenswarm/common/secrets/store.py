"""L1: SecretStore facade."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from jiuwenswarm.common.security.base_crypto import CryptoProvider
from jiuwenswarm.common.secrets.persistence import (
    DefaultFileStorageBackend,
    EnvMediumAdapter,
    FileMediumAdapter,
    PersistenceGateway,
)
from jiuwenswarm.common.secrets.registry import SecretRegistry, derive_legacy_name
from jiuwenswarm.common.secrets.transform import SecretTransform
from jiuwenswarm.common.utils import get_config_dir, get_env_file, get_workspace_dir

logger = logging.getLogger(__name__)

_instance: SecretStore | None = None


class SecretStore:
    def __init__(
        self,
        *,
        registry: SecretRegistry,
        transform: SecretTransform,
        gateway: PersistenceGateway,
    ) -> None:
        self._registry = registry
        self._transform = transform
        self._gateway = gateway

    @classmethod
    def get_instance(cls) -> SecretStore:
        global _instance
        if _instance is None:
            _instance = cls.build_default()
        return _instance

    @classmethod
    def build_default(
        cls,
        *,
        config_dir: Path | None = None,
        workspace_dir: Path | None = None,
    ) -> SecretStore:
        cfg = (config_dir or get_config_dir()).resolve()
        ws = (workspace_dir or get_workspace_dir()).resolve()
        registry = SecretRegistry(config_dir=cfg, workspace_dir=ws)
        transform = SecretTransform()
        gateway = PersistenceGateway(
            env_adapter=EnvMediumAdapter(get_env_file() if config_dir is None else cfg / ".env"),
            file_adapter=FileMediumAdapter(config_dir=cfg, workspace_dir=ws),
            default_backend=DefaultFileStorageBackend(cfg / "secrets_store.json"),
        )
        return cls(registry=registry, transform=transform, gateway=gateway)

    @classmethod
    def reset_for_tests(cls, store: SecretStore | None = None) -> None:
        global _instance
        _instance = store

    def get(self, key: str) -> str:
        target = self._registry.resolve(key)
        raw = self._gateway.read(target)
        legacy_name = derive_legacy_name(key, target)
        return self._transform.decode_from_store(key, raw, legacy_name=legacy_name)

    def set(self, key: str, value: str, *, algorithm: str | None = None) -> None:
        target = self._registry.resolve(key)
        legacy_name = derive_legacy_name(key, target)
        raw = self._transform.encode_for_store(
            key, value, algorithm=algorithm, legacy_name=legacy_name
        )
        self._gateway.write(target, raw)

    def delete(self, key: str) -> None:
        target = self._registry.resolve(key)
        self._gateway.delete(target)

    def configure_aes256gcm(
        self,
        *,
        master_key_env: str = "JIUWEN_SECRET_MASTER_KEY",
        master_key_file: str | None = None,
    ) -> None:
        if not master_key_file:
            master_key_file = str(get_config_dir() / ".master_key")
        self._transform.configure_aes256gcm(
            master_key_env=master_key_env,
            master_key_file=master_key_file,
        )

    def configure_dek(self, *, private_key_b64: str) -> None:
        self._transform.configure_dek(private_key_b64=private_key_b64)

    def register_custom_crypto(self, provider: CryptoProvider) -> None:
        self._transform.register_custom_crypto(provider)

    def bridge_legacy_extension_crypto(self) -> None:
        reg_mod = sys.modules.get("jiuwenswarm.extensions.registry")
        if reg_mod is None or not hasattr(reg_mod, "ExtensionRegistry"):
            logger.debug("ExtensionRegistry unavailable; skip legacy crypto bridge")
            return
        try:
            provider = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
        except Exception as exc:
            logger.debug("ExtensionRegistry crypto unavailable: %s", exc)
            return
        if provider is None:
            return
        self.register_custom_crypto(provider)
