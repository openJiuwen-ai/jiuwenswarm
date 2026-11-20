# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host runtime for Connector Source SPI: defaults + ``connector_sources`` apply."""

from __future__ import annotations

import json
import logging
from typing import Any

from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorSourceConfig,
    ConnectorSourceExtension,
    ConnectorSourceProvider,
    DownloadPolicy,
)
from jiuwenswarm.extensions.swarm_connector_hub.constants import OPENJIUWEN_HUB_SOURCE_ID
from jiuwenswarm.server.runtime.connector.source_registry import (
    SourceRegistry,
    SourceRegistryError,
)

logger = logging.getLogger(__name__)


def _is_enterprise() -> bool:
    try:
        from jiuwenswarm.enterprise.edition import is_enterprise

        return bool(is_enterprise())
    except Exception:  # noqa: BLE001
        return False


class SourceRuntime:
    """Owns the process-local connector SourceRegistry and config application.

    Skill-side equivalent responsibilities live on ``SkillManager``; this type
    keeps connector multi-source binding out of ``mcp.registry`` (connect state).
    """

    def __init__(self) -> None:
        self._registry = SourceRegistry()

    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    def list_providers(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._registry.list()]

    def register_provider(
        self,
        config: ConnectorSourceConfig,
        provider: ConnectorSourceProvider,
        *,
        display_name: str | None = None,
    ) -> None:
        self._registry.register(config, provider, display_name=display_name)

    def bind_extension(
        self,
        config: ConnectorSourceConfig,
        extension: ConnectorSourceExtension,
        *,
        display_name: str | None = None,
    ) -> ConnectorSourceProvider:
        return self._registry.bind_extension(
            config, extension, display_name=display_name
        )

    def register_default_sources(self) -> None:
        """Bind extension-offered defaults into the current registry."""
        self._register_default_sources(self._registry)

    @staticmethod
    def _list_extensions() -> list[ConnectorSourceExtension]:
        try:
            from jiuwenswarm.extensions import ExtensionRegistry

            return ExtensionRegistry.get_instance().list_connector_source_extensions()
        except RuntimeError:
            return []

    @staticmethod
    def _get_extension(provider_type: str) -> ConnectorSourceExtension | None:
        try:
            from jiuwenswarm.extensions import ExtensionRegistry

            return ExtensionRegistry.get_instance().get_connector_source_extension(
                provider_type
            )
        except RuntimeError:
            return None

    @classmethod
    def _register_default_sources(
        cls,
        registry: SourceRegistry,
        *,
        skip: set[str] | None = None,
    ) -> None:
        skipped = skip or set()
        for factory in cls._list_extensions():
            try:
                config = factory.default_source_config()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "connector source extension %s failed to provide default source config",
                    getattr(factory, "provider_type", "?"),
                    exc_info=True,
                )
                continue
            if config is None or config.source_id in skipped:
                continue
            try:
                registry.bind_extension(config, factory)
            except (SourceRegistryError, ValueError) as exc:
                logger.warning(
                    "default connector source %s rejected: %s", config.source_id, exc
                )

    @staticmethod
    def _source_config_custom_payload(record: dict[str, Any]) -> dict[str, Any] | None:
        raw = record.get("custom_config")
        if raw is None:
            raw = record.get("data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SourceRegistryError(
                    "source_misconfigured", "extension custom_config is not valid JSON"
                ) from exc
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _parse_source_config(raw: dict[str, Any]) -> ConnectorSourceConfig:
        capabilities = raw.get("capabilities") or []
        if not isinstance(capabilities, list):
            raise SourceRegistryError(
                "source_misconfigured", "capabilities must be an array"
            )
        options = raw.get("options") or {}
        if not isinstance(options, dict):
            raise SourceRegistryError(
                "source_misconfigured", "options must be an object"
            )
        verify_ssl = raw.get("verify_ssl", True)
        if not isinstance(verify_ssl, bool):
            raise SourceRegistryError(
                "source_misconfigured", "verify_ssl must be a boolean"
            )
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise SourceRegistryError(
                "source_misconfigured", "priority must be an integer"
            ) from exc
        download_raw = raw.get("download_policy")
        download_policy = DownloadPolicy()
        if isinstance(download_raw, dict):
            allowed_hosts = download_raw.get("allowed_hosts") or []
            if not isinstance(allowed_hosts, list):
                raise SourceRegistryError(
                    "source_misconfigured",
                    "download_policy.allowed_hosts must be an array",
                )
            try:
                max_bytes = int(download_raw.get("max_bytes", 64 * 1024 * 1024))
                timeout_seconds = float(download_raw.get("timeout_seconds", 60.0))
            except (TypeError, ValueError) as exc:
                raise SourceRegistryError(
                    "source_misconfigured", "download policy limits must be numeric"
                ) from exc
            if max_bytes <= 0 or timeout_seconds <= 0:
                raise SourceRegistryError(
                    "source_misconfigured", "download policy limits must be positive"
                )
            known_keys = {"allowed_hosts", "max_bytes", "timeout_seconds", "metadata"}
            metadata_raw = download_raw.get("metadata")
            if metadata_raw is None:
                metadata: dict[str, Any] = {
                    key: value
                    for key, value in download_raw.items()
                    if key not in known_keys
                }
            elif not isinstance(metadata_raw, dict):
                raise SourceRegistryError(
                    "source_misconfigured",
                    "download_policy.metadata must be an object",
                )
            else:
                metadata = dict(metadata_raw)
                for key, value in download_raw.items():
                    if key not in known_keys:
                        metadata.setdefault(key, value)
            download_policy = DownloadPolicy(
                allowed_hosts=tuple(
                    str(item).strip().lower()
                    for item in allowed_hosts
                    if str(item).strip()
                ),
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
                metadata=metadata,
            )
        return ConnectorSourceConfig(
            source_id=str(raw.get("source_id") or "").strip(),
            provider_type=str(raw.get("provider_type") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            priority=priority,
            endpoint_ref=str(raw.get("endpoint_ref") or "").strip() or None,
            auth_ref=str(raw.get("auth_ref") or "").strip() or None,
            capabilities=frozenset(str(item) for item in capabilities),
            download_policy=download_policy,
            options=dict(options),
            verify_ssl=verify_ssl,
        )

    async def apply_connector_source_configs(
        self,
        extension_config: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Atomically apply ``custom_config.connector_sources`` from effective policy.

        Distinguishes ``connector_sources`` absent (leave registry unchanged) from
        an explicit empty array (clear configured sources, keep defaults only).
        Non-enterprise editions ignore configured non-default sources (D-07).
        """
        raw_sources: list[dict[str, Any]] = []
        connector_sources_present = False
        for record in extension_config or []:
            if not isinstance(record, dict):
                continue
            custom = self._source_config_custom_payload(record)
            if custom is None or "connector_sources" not in custom:
                continue
            connector_sources_present = True
            configured = custom.get("connector_sources")
            if not isinstance(configured, list):
                raise SourceRegistryError(
                    "source_misconfigured",
                    "custom_config.connector_sources must be an array",
                )
            for item in configured:
                if not isinstance(item, dict):
                    raise SourceRegistryError(
                        "source_misconfigured",
                        "connector_sources entries must be objects",
                    )
                raw_sources.append(item)
        if not connector_sources_present:
            return {
                "applied": False,
                "source_count": len(self._registry.list()),
            }

        configs = [self._parse_source_config(item) for item in raw_sources]
        if not _is_enterprise():
            configs = [
                item
                for item in configs
                if item.source_id == OPENJIUWEN_HUB_SOURCE_ID
            ]

        candidate = SourceRegistry()
        configured_ids = {config.source_id for config in configs}
        self._register_default_sources(candidate, skip=configured_ids)

        for config in configs:
            extension = self._get_extension(config.provider_type)
            if extension is None:
                raise SourceRegistryError(
                    "source_misconfigured",
                    f"provider_type is not registered: {config.provider_type}",
                )
            try:
                candidate.bind_extension(config, extension)
            except ValueError as exc:
                raise SourceRegistryError("source_misconfigured", str(exc)) from exc

        previous = self._registry
        self._registry = candidate
        await previous.close()
        return {"applied": True, "source_count": len(candidate.list())}


_RUNTIME: SourceRuntime | None = None


def get_source_runtime() -> SourceRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = SourceRuntime()
    return _RUNTIME


def reset_source_runtime() -> None:
    """Test helper: drop the process-local runtime singleton."""
    global _RUNTIME
    _RUNTIME = None
