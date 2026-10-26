# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""In-process registry for configured Skill Source providers."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace

from jiuwenswarm.extensions.sdk.skill_source import (
    DownloadPolicy,
    SourceConfig,
    SourceDescriptor,
    SkillSourceExtension,
    SkillSourceProvider,
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SourceRegistryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _ProviderEntry:
    config: SourceConfig
    provider: SkillSourceProvider
    display_name: str
    started: bool = False
    start_lock: asyncio.Lock | None = None


class SourceRegistry:
    """Keeps runtime provider instances only; it never stores workspace state."""

    def __init__(self) -> None:
        self._entries: dict[str, _ProviderEntry] = {}

    def register(
        self,
        config: SourceConfig,
        provider: SkillSourceProvider,
        *,
        display_name: str | None = None,
    ) -> None:
        source_id = str(config.source_id or "").strip()
        provider_type = str(config.provider_type or "").strip()
        if not _ID_RE.fullmatch(source_id):
            raise SourceRegistryError("source_misconfigured", f"invalid source_id: {source_id}")
        if not _ID_RE.fullmatch(provider_type):
            raise SourceRegistryError("source_misconfigured", f"invalid provider_type: {provider_type}")
        if source_id in self._entries:
            raise SourceRegistryError("source_misconfigured", f"duplicate source_id: {source_id}")
        if str(provider.source_id or "").strip() != source_id:
            raise SourceRegistryError("source_misconfigured", "provider source_id does not match config")
        declared = frozenset(provider.capabilities)
        configured = frozenset(config.capabilities)
        if configured and not configured.issubset(declared):
            raise SourceRegistryError(
                "source_misconfigured",
                f"provider {provider_type} does not implement {sorted(configured - declared)}",
            )
        config = self._merge_provider_download_hosts(config, provider)
        self._entries[source_id] = _ProviderEntry(
            config=config,
            provider=provider,
            display_name=(display_name or provider.display_name or source_id).strip(),
        )

    @staticmethod
    def _merge_provider_download_hosts(
        config: SourceConfig,
        provider: SkillSourceProvider,
    ) -> SourceConfig:
        """Merge the Provider-declared download allowlist into the source config.

        SPI 来源下载 fail-closed：管理面显式配置的 allowed_hosts 优先；未配置时
        采用扩展 Provider 显式声明的默认白名单（download_allowed_hosts）；两者
        皆空则保持为空，由安装链路拒绝下载。
        """
        declared = tuple(getattr(provider, "download_allowed_hosts", ()) or ())
        if not declared:
            return config
        policy = config.download_policy
        if policy is not None and policy.allowed_hosts:
            return config
        return replace(
            config,
            download_policy=DownloadPolicy(
                allowed_hosts=declared,
                max_bytes=(policy.max_bytes if policy else DownloadPolicy.max_bytes),
                timeout_seconds=(
                    policy.timeout_seconds if policy else DownloadPolicy.timeout_seconds
                ),
            ),
        )

    def bind_extension(
        self,
        config: SourceConfig,
        extension: SkillSourceExtension,
        *,
        display_name: str | None = None,
    ) -> SkillSourceProvider:
        """Create and bind a configured Provider through an extension factory."""
        if str(extension.provider_type or "").strip() != config.provider_type:
            raise SourceRegistryError(
                "source_misconfigured", "extension provider_type does not match config"
            )
        provider = extension.create_provider(config)
        self.register(config, provider, display_name=display_name)
        return provider

    def list(self) -> list[SourceDescriptor]:
        descriptors = [
            SourceDescriptor(
                source_id=entry.config.source_id,
                provider_type=entry.config.provider_type,
                display_name=entry.display_name,
                enabled=entry.config.enabled,
                priority=entry.config.priority,
                capabilities=frozenset(entry.config.capabilities or entry.provider.capabilities),
            )
            for entry in self._entries.values()
        ]
        return sorted(descriptors, key=lambda item: (-item.priority, item.source_id))

    def get_config(self, source_id: str) -> SourceConfig:
        """Return the trusted runtime configuration for one source."""
        entry = self._entries.get(str(source_id or "").strip())
        if entry is None:
            raise SourceRegistryError("source_not_found", f"skill source not found: {source_id}")
        return entry.config

    async def get(self, source_id: str, capability: str) -> SkillSourceProvider:
        entry = self._entries.get(str(source_id or "").strip())
        if entry is None:
            raise SourceRegistryError("source_not_found", f"skill source not found: {source_id}")
        if not entry.config.enabled:
            raise SourceRegistryError("source_disabled", f"skill source is disabled: {source_id}")
        capabilities = frozenset(entry.config.capabilities or entry.provider.capabilities)
        if capability not in capabilities:
            raise SourceRegistryError(
                "source_capability_unsupported",
                f"skill source {source_id} does not support {capability}",
            )
        if not entry.started:
            if entry.start_lock is None:
                entry.start_lock = asyncio.Lock()
            async with entry.start_lock:
                if not entry.started:
                    try:
                        await entry.provider.start()
                    except Exception as exc:
                        raise SourceRegistryError(
                            "source_unavailable", f"skill source {source_id} failed to start: {exc}"
                        ) from exc
                    entry.started = True
        return entry.provider

    async def close(self) -> None:
        for entry in self._entries.values():
            if entry.started:
                await entry.provider.close()
                entry.started = False
