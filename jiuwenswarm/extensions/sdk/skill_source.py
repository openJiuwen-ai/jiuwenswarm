# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Public Skill Source SPI used by AgentServer extensions.

Providers translate a remote catalogue protocol into the common models below.
They deliberately do not install, update, or remove workspace files.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from jiuwenswarm.extensions.sdk.base import BaseExtension


@dataclass(frozen=True)
class SkillRef:
    source_id: str
    skill_id: str


@dataclass(frozen=True)
class ArtifactRef:
    skill_ref: SkillRef
    version_id: str


@dataclass(frozen=True)
class TrustPolicy:
    verification: str = "if-present"
    allowed_algorithms: frozenset[str] = frozenset({"HmacSHA256"})
    hmac_key_refs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadPolicy:
    allowed_hosts: tuple[str, ...] = ()
    max_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 60.0


class SecretResolver(ABC):
    """Resolve an approved secret reference without exposing it to Providers."""

    @abstractmethod
    def resolve(self, reference: str) -> str | None:
        raise NotImplementedError


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    provider_type: str
    enabled: bool = True
    priority: int = 0
    endpoint_ref: str | None = None
    auth_ref: str | None = None
    capabilities: frozenset[str] = frozenset()
    download_policy: DownloadPolicy | None = None
    trust_policy: TrustPolicy | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str
    provider_type: str
    display_name: str
    enabled: bool
    priority: int
    capabilities: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_id,
            "source_id": self.source_id,
            "provider_type": self.provider_type,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "priority": self.priority,
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True)
class SkillSearchRequest:
    q: str = ""
    page: int = 1
    page_size: int = 20
    filters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillCandidate:
    source_id: str
    skill_id: str
    version_id: str
    name: str
    display_name: str | None = None
    summary: str | None = None
    version: str | None = None
    fingerprint: str | None = None
    namespace: str | None = None
    slug: str | None = None
    canonical_slug: str | None = None
    owner_display_name: str | None = None
    labels: tuple[Mapping[str, Any], ...] = ()
    homepage: str | None = None
    rating_avg: float | None = None
    rating_count: int | None = None
    download_count: int | None = None
    updated_at: datetime | str | int | None = None
    versions: tuple[Mapping[str, Any], ...] = ()
    previous_version: str | None = None
    accessible: bool | None = None
    downloadable: bool | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        updated_at: str | int | None = self.updated_at
        if isinstance(updated_at, datetime):
            updated_at = updated_at.isoformat()
        raw: dict[str, Any] = {
            "source_id": self.source_id,
            "skill_id": self.skill_id,
            "version_id": self.version_id,
            "name": self.name,
            "display_name": self.display_name,
            "summary": self.summary,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "namespace": self.namespace,
            "slug": self.slug,
            "canonical_slug": self.canonical_slug,
            "owner_display_name": self.owner_display_name,
            "labels": list(self.labels),
            "homepage": self.homepage,
            "rating_avg": self.rating_avg,
            "rating_count": self.rating_count,
            "download_count": self.download_count,
            "updated_at": updated_at,
            "versions": list(self.versions),
            "previous_version": self.previous_version,
            "accessible": self.accessible,
            "downloadable": self.downloadable,
            "metadata": dict(self.metadata),
        }
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value in ((), [], {}):
                continue
            result[key] = value
        return result


@dataclass(frozen=True)
class SkillSearchResult:
    items: tuple[SkillCandidate, ...]
    total: int | None = None
    next_page: int | None = None


@dataclass(frozen=True)
class InstalledArtifact:
    source_id: str
    skill_id: str
    version_id: str
    version: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True)
class SkillUpdateStatus:
    skill_ref: SkillRef
    current_version_id: str
    latest_version_id: str | None
    has_update: bool
    latest_version: str | None = None
    fingerprint_matched: bool | None = None
    accessible: bool | None = None
    downloadable: bool | None = None
    remote_status: str = "unknown"
    error_code: str | None = None


@dataclass(frozen=True)
class ArtifactDescriptor:
    artifact_ref: ArtifactRef
    download_url: str
    expires_at: datetime | None = None
    checksum_sha256: str | None = None
    artifact_sha256: str | None = None
    signature: str | None = None
    signature_algorithm: str | None = None
    signature_encoding: str | None = None
    signature_scope: str | None = None
    key_id: str | None = None
    fingerprint: str | None = None
    content_length: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderInvocationContext:
    request_id: str
    trace_id: str | None = None
    authorization_scope: str | None = None
    credential_ref: str | None = None


class SkillSourceProvider(ABC):
    """Remote catalogue adapter. Workspace mutation is intentionally absent."""

    source_id: str
    provider_type: str
    display_name: str
    capabilities: frozenset[str]

    # 信任策略内聚在 Provider：由扩展工厂/宿主在构造后经 set_trust_policy()
    # 注入，验签默认实现据此执行；类级默认值保证未调用 super().__init__()
    # 的子类也可安全读取。
    _trust_policy: TrustPolicy | None = None
    _secret_resolver: SecretResolver | None = None
    # Provider 显式声明的默认下载主机白名单：注册来源时并入
    # SourceConfig.download_policy（管理面显式配置的 allowed_hosts 优先）；
    # 两者皆空时安装链路 fail-closed 拒绝下载。
    download_allowed_hosts: tuple[str, ...] = ()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def set_trust_policy(
        self,
        trust_policy: TrustPolicy | None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        """Inject the source trust policy after construction.

        Called by the extension factory or the host runtime; the default
        ``verify_artifact`` implementation verifies against it.
        """
        self._trust_policy = trust_policy
        if secret_resolver is not None:
            self._secret_resolver = secret_resolver

    @abstractmethod
    async def search(
        self,
        request: SkillSearchRequest,
        context: ProviderInvocationContext,
    ) -> SkillSearchResult:
        raise NotImplementedError

    async def check_updates(
        self,
        installed: Sequence[InstalledArtifact],
        context: ProviderInvocationContext,
    ) -> Sequence[SkillUpdateStatus]:
        raise NotImplementedError

    @abstractmethod
    async def get_artifact(
        self,
        skill_ref: SkillRef,
        version_id: str,
        context: ProviderInvocationContext,
    ) -> ArtifactDescriptor:
        raise NotImplementedError

    async def download_artifact(
        self,
        descriptor: ArtifactDescriptor,
        download_policy: DownloadPolicy | None = None,
    ) -> bytes:
        """Download artifact bytes under the platform's secure download policy.

        The default enforces HTTPS-only URLs, forbids redirects, applies the
        DownloadPolicy ``max_bytes``/``timeout_seconds`` limits (timeout is
        clamped to at least 30s, matching the platform's historical behavior),
        requires a ZIP payload (PK magic, intact archive) and checks the
        descriptor's ``checksum_sha256`` when present. Providers may override
        to customize the download.
        """
        download_url = str(descriptor.download_url or "").strip()
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("skill 下载 URL 必须是 HTTPS 地址")
        policy = download_policy or DownloadPolicy()
        timeout = max(30.0, policy.timeout_seconds or DownloadPolicy.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(download_url)
            if resp.is_redirect:
                raise RuntimeError("skill 下载不支持自动跳转，请提供最终制品地址")
            resp.raise_for_status()
            body = resp.content or b""

        if not body:
            raise RuntimeError("下载内容为空")
        if len(body) > policy.max_bytes:
            raise RuntimeError("下载内容超过来源允许的最大大小")
        if len(body) < 4 or not body.startswith(b"PK"):
            raise RuntimeError("下载内容不是 ZIP 文件")

        expected_sha256 = str(descriptor.checksum_sha256 or "").strip().lower()
        if expected_sha256 and hashlib.sha256(body).hexdigest().lower() != expected_sha256:
            raise RuntimeError("下载文件校验失败（SHA256 不匹配）")

        try:
            with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
                if zf.testzip() is not None:
                    raise RuntimeError("下载 ZIP 文件已损坏")
        except zipfile.BadZipFile as exc:
            raise RuntimeError("下载内容不是有效 ZIP 文件") from exc
        return body

    async def verify_artifact(
        self,
        descriptor: ArtifactDescriptor,
        content: bytes,
    ) -> Mapping[str, Any] | None:
        """Verify downloaded artifact bytes before installation.

        Returns the verification audit mapping recorded on the installation,
        or None when the artifact carries no signature and the trust policy
        is ``if-present``; raises ArtifactVerificationError on verification
        failure. The default delegates to the platform's common SkillHub
        verification with the trust policy injected via ``set_trust_policy()``;
        Providers may override to enforce a custom verification contract.
        """
        from jiuwenswarm.server.runtime.skill.artifact_security import (
            EnvironmentSecretResolver,
            verify_skillhub_artifact,
        )

        trust_policy = self._trust_policy or TrustPolicy(verification="if-present")
        resolver = self._secret_resolver or EnvironmentSecretResolver()
        result = verify_skillhub_artifact(
            descriptor,
            content,
            trust_policy=trust_policy,
            secret_resolver=resolver,
        )
        return result.to_audit_dict() if result.verified else None


class SkillSourceExtension(BaseExtension, ABC):
    """Factory registered by a trusted extension package."""

    provider_type: str
    api_version: str = "2"

    @abstractmethod
    def create_provider(self, config: SourceConfig) -> SkillSourceProvider:
        raise NotImplementedError

    def default_source_config(self) -> SourceConfig | None:
        """Return the default source offered by this extension, or None.

        是否提供默认源由扩展包自行决定（例如内置参考源在企业版默认关闭）。
        返回 None 表示该扩展不提供默认源；提供时平台在启动和管理面配置
        重载时经 bind_extension 注册。配置来源（skill_sources）的 enabled
        不受此钩子影响，仍由配置驱动。
        """
        return None
