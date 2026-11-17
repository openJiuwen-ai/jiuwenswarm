# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public Connector Source SPI used by extensions.

Enterprise 真源在本模块（对齐 ``skill_source`` 本地落点；个人版 develop 仍可由 gateway 转发）。
对齐 ``skill_source`` 的职责边界：Provider 只做远端目录与可信制品，
不做落盘、connect、凭证或会话变更。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

try:
    from jiuwenswarm.extensions.sdk.base import BaseExtension
except ModuleNotFoundError:  # gateway_protocol is provided by the runtime image
    class BaseExtension:  # type: ignore[no-redef]
        """Stand-in so AgentServer unit tests can import the SPI without the protocol package."""


CONNECTOR_CAPABILITIES = frozenset(
    {"search", "get_artifact", "check_updates", "get_detail"}
)


def _host_matches_allow_rule(host: str, rule: str) -> bool:
    """Match a download hostname against one allow-list rule.

    Rules (case-insensitive):
    - Exact DNS label match with optional ``*`` wildcards per label
      (same shape as skill Hub host rules), e.g. ``cdn.*.example.com``
    - Leading-dot suffix, e.g. ``.example.com`` matches ``a.example.com``
      and ``example.com``
    """
    host_value = str(host or "").strip().lower()
    rule_value = str(rule or "").strip().lower()
    if not host_value or not rule_value:
        return False
    if rule_value.startswith("."):
        suffix = rule_value[1:]
        return bool(suffix) and (
            host_value == suffix or host_value.endswith(rule_value)
        )
    host_parts = host_value.split(".")
    rule_parts = rule_value.split(".")
    if len(host_parts) != len(rule_parts):
        return False
    for host_part, rule_part in zip(host_parts, rule_parts):
        if rule_part == "*":
            continue
        if host_part != rule_part:
            return False
    return True


@dataclass(frozen=True)
class DownloadPolicy:
    """Platform download limits plus optional source-private knobs.

    Default ``download_artifact`` enforces ``max_bytes``, ``timeout_seconds``,
    and ``allowed_hosts`` when the allow-list is non-empty (empty = no host
    filter). ``metadata`` is ignored by the default implementation; Providers
    that override download may read source-private keys from it.
    """

    allowed_hosts: tuple[str, ...] = ()
    max_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 60.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderInvocationContext:
    request_id: str
    trace_id: str | None = None
    authorization_scope: str | None = None
    credential_ref: str | None = None
    user_id: str | None = None
    group_id: str | None = None
    bot_id: str | None = None


class ConnectorCapabilityError(RuntimeError):
    """Raised when a Provider does not support the requested capability."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConnectorArtifactVerificationError(RuntimeError):
    """Raised when default or provider verification rejects an artifact."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConnectorDownloadError(RuntimeError):
    """Raised when default or provider download rejects an artifact URL/body."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ConnectorVerificationResult:
    """Outcome of a successful or skipped connector artifact verification.

    ``verified=True`` means a signature was checked and accepted.
    Optional algorithm/scope/key_id record what the artifact declared and the
    verifier accepted (not the platform policy itself).
    Source-private audit knobs belong in ``metadata``—do not grow first-class
    fields per signing protocol.
    """

    verified: bool
    content_sha256: str
    algorithm: str | None = None
    scope: str | None = None
    key_id: str | None = None
    verified_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "verified": self.verified,
            "content_sha256": self.content_sha256,
            "algorithm": self.algorithm,
            "scope": self.scope,
            "key_id": self.key_id,
            "verified_at": self.verified_at,
            "metadata": dict(self.metadata),
        }
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value == {}:
                continue
            result[key] = value
        return result


@dataclass(frozen=True)
class ConnectorRef:
    source_id: str
    asset_id: str


@dataclass(frozen=True)
class ConnectorArtifactRef:
    connector_ref: ConnectorRef
    version_id: str


@dataclass(frozen=True)
class ConnectorSourceConfig:
    source_id: str
    provider_type: str
    enabled: bool = True
    priority: int = 0
    endpoint_ref: str | None = None
    auth_ref: str | None = None
    capabilities: frozenset[str] = frozenset()
    download_policy: DownloadPolicy | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    verify_ssl: bool = True


@dataclass(frozen=True)
class ConnectorSourceDescriptor:
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
class ConnectorSearchRequest:
    q: str = ""
    page: int = 1
    page_size: int = 20
    filters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorCandidate:
    """Catalogue card for search results.

    Identity aligns with ``SkillCandidate`` shape where practical:
    ``source_id`` / ``asset_id`` / ``version_id`` / ``package_name`` /
    ``display_name``. ``versions`` is an optional market-supplied list of
    available / historical version objects (opaque dicts; empty when the
    catalogue does not embed them). Further knobs belong in ``metadata``.
    """

    source_id: str
    asset_id: str
    package_name: str
    display_name: str
    version_id: str
    versions: tuple[Mapping[str, Any], ...] = ()
    short_description: str = ""
    icon_uri: str = ""
    tags: tuple[str, ...] = ()
    category_name: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def package_id(self) -> str:
        """Alias of ``package_name`` for install-ledger naming."""
        return self.package_name

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "source": self.source_id,
            "source_id": self.source_id,
            "asset_id": self.asset_id,
            "package_name": self.package_name,
            "display_name": self.display_name,
            "version_id": self.version_id,
            "versions": list(self.versions),
            "short_description": self.short_description,
            "icon_uri": self.icon_uri,
            "tags": list(self.tags),
            "category_name": self.category_name,
            "metadata": dict(self.metadata),
        }
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value in ((), [], {}):
                continue
            result[key] = value
        return result


@dataclass(frozen=True)
class ConnectorSearchResult:
    items: tuple[ConnectorCandidate, ...]
    total: int | None = None
    next_page: int | None = None
    page: int | None = None
    page_size: int | None = None


@dataclass(frozen=True)
class ConnectorDetail:
    """Catalogue detail for one connector.

    Same identity / display rules as ``ConnectorCandidate`` (``version_id`` +
    optional ``versions`` history bag). Package inventory summaries (tools,
    transport hints, …) go in ``metadata`` under a provider-chosen key such as
    ``package_profile``—not as new first-class fields.
    """

    source_id: str
    asset_id: str
    package_name: str
    display_name: str
    version_id: str = ""
    versions: tuple[Mapping[str, Any], ...] = ()
    short_description: str = ""
    detail_description: str = ""
    icon_uri: str = ""
    tags: tuple[str, ...] = ()
    category_name: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def package_id(self) -> str:
        """Alias of ``package_name`` for install-ledger naming."""
        return self.package_name

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "source": self.source_id,
            "source_id": self.source_id,
            "asset_id": self.asset_id,
            "package_name": self.package_name,
            "display_name": self.display_name,
            "version_id": self.version_id,
            "versions": list(self.versions),
            "short_description": self.short_description,
            "detail_description": self.detail_description,
            "icon_uri": self.icon_uri,
            "tags": list(self.tags),
            "category_name": self.category_name,
            "metadata": dict(self.metadata),
        }
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value in ((), [], {}):
                continue
            result[key] = value
        return result


@dataclass(frozen=True)
class InstalledConnectorArtifact:
    source_id: str
    asset_id: str
    version_id: str
    package_name: str = ""
    version: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True)
class ConnectorUpdateStatus:
    connector_ref: ConnectorRef
    current_version_id: str
    latest_version_id: str | None
    has_update: bool
    latest_version: str | None = None
    fingerprint_matched: bool | None = None
    remote_status: str = "unknown"
    error_code: str | None = None


@dataclass(frozen=True)
class ConnectorArtifactDescriptor:
    artifact_ref: ConnectorArtifactRef
    download_url: str
    content_sha256: str | None = None
    signature: str | None = None
    signature_algorithm: str | None = None
    signature_encoding: str | None = None
    signature_scope: str | None = None
    key_id: str | None = None
    fingerprint: str | None = None
    content_length: int | None = None
    package_name: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ConnectorSourceProvider(ABC):
    """Remote connector catalogue adapter. Connect / credential mutation is absent."""

    source_id: str
    provider_type: str
    display_name: str
    capabilities: frozenset[str]

    download_allowed_hosts: tuple[str, ...] = ()
    verify_ssl: bool = True

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @abstractmethod
    async def search(
        self,
        request: ConnectorSearchRequest,
        context: ProviderInvocationContext,
    ) -> ConnectorSearchResult:
        raise NotImplementedError

    async def get_detail(
        self,
        connector_ref: ConnectorRef,
        context: ProviderInvocationContext,
    ) -> ConnectorDetail:
        """Return connector detail when the source supports ``get_detail``."""
        raise ConnectorCapabilityError(
            "capability_unsupported",
            f"connector source {self.source_id} does not support get_detail",
        )

    @abstractmethod
    async def get_artifact(
        self,
        connector_ref: ConnectorRef,
        version_id: str,
        context: ProviderInvocationContext,
    ) -> ConnectorArtifactDescriptor:
        raise NotImplementedError

    async def check_updates(
        self,
        installed: Sequence[InstalledConnectorArtifact],
        context: ProviderInvocationContext,
    ) -> Sequence[ConnectorUpdateStatus]:
        return ()

    async def download_artifact(
        self,
        descriptor: ConnectorArtifactDescriptor,
        download_policy: DownloadPolicy | None = None,
    ) -> bytes:
        """Download connector package bytes under the platform secure policy.

        Default accepts HTTP(S), forbids redirects and URL userinfo, enforces
        size/timeout, applies ``allowed_hosts`` when non-empty (else falls back
        to ``download_allowed_hosts``), requires a ZIP payload, and checks
        ``content_sha256`` when present. ``content_length`` is advisory only.
        """
        download_url = str(descriptor.download_url or "").strip()
        parsed = urlparse(download_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConnectorDownloadError(
                "invalid_download_url",
                "连接器下载 URL 必须是 HTTP 或 HTTPS 地址",
            )
        if parsed.username or parsed.password:
            raise ConnectorDownloadError(
                "invalid_download_url",
                "连接器下载 URL 不能包含用户信息",
            )
        host = str(parsed.hostname or "").strip().lower()
        policy = download_policy or DownloadPolicy()
        allowed_hosts = tuple(
            str(item).strip().lower()
            for item in (policy.allowed_hosts or ())
            if str(item).strip()
        )
        if not allowed_hosts:
            allowed_hosts = tuple(
                str(item).strip().lower()
                for item in (self.download_allowed_hosts or ())
                if str(item).strip()
            )
        if allowed_hosts and not any(
            _host_matches_allow_rule(host, rule) for rule in allowed_hosts
        ):
            raise ConnectorDownloadError(
                "host_not_allowed",
                f"连接器下载 URL 主机不在来源白名单: {host}",
            )
        timeout = max(30.0, policy.timeout_seconds or DownloadPolicy.timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, verify=self.verify_ssl
        ) as client:
            resp = await client.get(download_url)
            if resp.is_redirect:
                raise ConnectorDownloadError(
                    "redirect_forbidden",
                    "连接器下载不支持自动跳转，请提供最终制品地址",
                )
            resp.raise_for_status()
            body = resp.content or b""

        if not body:
            raise ConnectorDownloadError("empty_content", "下载内容为空")
        if len(body) > policy.max_bytes:
            raise ConnectorDownloadError(
                "content_too_large",
                "下载内容超过来源允许的最大大小",
            )
        if len(body) < 4 or not body.startswith(b"PK"):
            raise ConnectorDownloadError("not_zip", "下载内容不是 ZIP 文件")

        expected_sha256 = str(descriptor.content_sha256 or "").strip().lower()
        if expected_sha256 and hashlib.sha256(body).hexdigest().lower() != expected_sha256:
            raise ConnectorDownloadError(
                "checksum_mismatch",
                "下载文件校验失败（SHA256 不匹配）",
            )

        try:
            with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
                if zf.testzip() is not None:
                    raise ConnectorDownloadError("zip_corrupt", "下载 ZIP 文件已损坏")
        except zipfile.BadZipFile as exc:
            raise ConnectorDownloadError(
                "invalid_zip",
                "下载内容不是有效 ZIP 文件",
            ) from exc
        return body

    async def verify_artifact(
        self,
        descriptor: ConnectorArtifactDescriptor,
        content: bytes,
    ) -> ConnectorVerificationResult | None:
        """Verify downloaded connector package bytes before installation.

        Generic SPI default (no source-specific signing contract):
        - Does not re-check ``content_sha256`` (that belongs to download).
        - When no ``signature`` is present, return ``None`` (skip).
        - When a ``signature`` is present, raise and require the Provider to
          override (e.g. OpenJiuwen Hub HMAC in ``swarm_connector_hub``).

        Returns ``None`` when verification is skipped (unsigned).
        Raises ``ConnectorArtifactVerificationError`` on failure.
        """
        _ = content  # Provider overrides may inspect bytes; default does not.
        signature = str(descriptor.signature or "").strip()
        if not signature:
            return None

        raise ConnectorArtifactVerificationError(
            "artifact_signature_unsupported",
            (
                f"connector source {self.source_id} received a signed artifact but "
                "the generic verify_artifact default has no signing contract; "
                "override verify_artifact on the Provider"
            ),
        )


class ConnectorSourceExtension(BaseExtension, ABC):
    """Factory registered by a trusted extension package."""

    provider_type: str
    api_version: str = "1"

    @abstractmethod
    def create_provider(self, config: ConnectorSourceConfig) -> ConnectorSourceProvider:
        raise NotImplementedError

    def default_source_config(self) -> ConnectorSourceConfig | None:
        """Return the default source offered by this extension, or None."""
        return None
