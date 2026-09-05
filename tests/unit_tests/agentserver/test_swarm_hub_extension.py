from __future__ import annotations

import io
import zipfile

import pytest

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    ArtifactRef,
    DownloadPolicy,
    SkillRef,
    SkillSearchRequest,
    SkillSearchResult,
    SkillSourceProvider,
    SourceConfig,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.source_registry import SourceRegistry


class _CustomVerifyProvider(SkillSourceProvider):
    """覆盖 download_artifact 与 verify_artifact 的客户 Provider。"""

    source_id = "hub-c"
    provider_type = "customhub"
    display_name = "CustomHub"
    capabilities = frozenset({"search", "check_updates", "get_artifact"})

    def __init__(self) -> None:
        self.download_calls: list[tuple[ArtifactDescriptor, DownloadPolicy | None]] = []
        self.verify_calls: list[tuple[ArtifactDescriptor, bytes]] = []

    async def search(self, request: SkillSearchRequest, context) -> SkillSearchResult:
        return SkillSearchResult(items=())

    async def get_artifact(self, skill_ref: SkillRef, version_id: str, context) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            artifact_ref=ArtifactRef(skill_ref=skill_ref, version_id=version_id),
            download_url="https://example.com/demo.zip",
        )

    async def download_artifact(self, descriptor, download_policy=None) -> bytes:
        self.download_calls.append((descriptor, download_policy))
        return b"zip-bytes"

    async def verify_artifact(self, descriptor, content):
        self.verify_calls.append((descriptor, content))
        return {"verified": True, "artifact_sha256": "custom-sha"}


class _MarketplaceMetadataProvider(SkillSourceProvider):
    source_id = "hub-market"
    provider_type = "customhub"
    display_name = "Marketplace"
    capabilities = frozenset({"get_artifact"})

    async def search(self, request: SkillSearchRequest, context) -> SkillSearchResult:
        return SkillSearchResult(items=())

    async def get_artifact(self, skill_ref: SkillRef, version_id: str, context) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            artifact_ref=ArtifactRef(skill_ref=skill_ref, version_id=version_id),
            download_url="https://example.com/enterprise-architecture.zip",
            metadata={"version": "1.0.0"} if version_id == "version-456" else {},
        )

    async def download_artifact(self, descriptor, download_policy=None) -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "enterprise-architecture/SKILL.md",
                "---\nname: enterprise-architecture\nversion: 0.1\n---\n# Enterprise Architecture\n",
            )
        return stream.getvalue()

    async def verify_artifact(self, descriptor, content):
        return {"verified": True}


@pytest.mark.asyncio


async def test_install_path_uses_provider_download_and_verify_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    manager = SkillManager(workspace_dir=str(tmp_path))

    provider = _CustomVerifyProvider()
    registry = SourceRegistry()
    registry.register(
        SourceConfig(
            source_id="hub-c",
            provider_type="customhub",
            capabilities=frozenset({"get_artifact"}),
            download_policy=DownloadPolicy(allowed_hosts=("example.com",)),
        ),
        provider,
    )
    manager._source_registry = registry

    descriptor, body, verification = await manager._fetch_verified_source_artifact(
        source_id="hub-c",
        skill_id="demo",
        version_id="1.0.0",
        params={},
    )

    assert body == b"zip-bytes"
    assert descriptor.download_url == "https://example.com/demo.zip"
    # 下载经 Provider 覆写实现（平台默认下载被绕过），download_policy 透传
    assert len(provider.download_calls) == 1
    _, download_policy = provider.download_calls[0]
    assert download_policy is not None
    assert download_policy.allowed_hosts == ("example.com",)
    # 验签经 Provider 覆写实现：返回的审计 Mapping 直接入账（不再 to_audit_dict）
    assert verification == {"verified": True, "artifact_sha256": "custom-sha"}
    assert len(provider.verify_calls) == 1
    _, content = provider.verify_calls[0]
    assert content == b"zip-bytes"


@pytest.mark.asyncio
async def test_source_install_keeps_marketplace_version_and_author_in_list_and_detail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    manager = SkillManager(workspace_dir=str(tmp_path))
    registry = SourceRegistry()
    registry.register(
        SourceConfig(
            source_id="hub-market",
            provider_type="customhub",
            capabilities=frozenset({"get_artifact"}),
            download_policy=DownloadPolicy(allowed_hosts=("example.com",)),
        ),
        _MarketplaceMetadataProvider(),
    )
    manager._source_registry = registry

    result = await manager.handle_skills_source_install(
        {
            "source_id": "hub-market",
            "skill_id": "skill-123",
            "version_id": "version-456",
            "version": "1.0.0",
            "author": "Architecture Team",
            "display_name": "Enterprise Architecture",
        }
    )

    assert result["success"] is True
    listed = await manager.handle_skills_list({"with_installed": True})
    skill = next(item for item in listed["skills"] if item["name"] == "enterprise-architecture")
    detail = await manager.handle_skills_get(
        {"name": "enterprise-architecture", "origin": "hub-market:skill-123"}
    )
    assert skill["version"] == "1.0.0"
    assert skill["author"] == "Architecture Team"
    assert detail["version"] == "1.0.0"
    assert detail["author"] == "Architecture Team"


@pytest.mark.asyncio
async def test_source_update_keeps_target_marketplace_version_and_author_in_list_and_detail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    manager = SkillManager(workspace_dir=str(tmp_path))
    registry = SourceRegistry()
    registry.register(
        SourceConfig(
            source_id="hub-market",
            provider_type="customhub",
            capabilities=frozenset({"get_artifact"}),
            download_policy=DownloadPolicy(allowed_hosts=("example.com",)),
        ),
        _MarketplaceMetadataProvider(),
    )
    manager._source_registry = registry
    installed = await manager.handle_skills_source_install(
        {
            "source_id": "hub-market",
            "skill_id": "skill-123",
            "version_id": "version-456",
            "version": "1.0.0",
            "author": "Architecture Team",
        }
    )
    assert installed["success"] is True

    updated = await manager.handle_skills_update(
        {
            "source_id": "hub-market",
            "skill_id": "skill-123",
            "target_version_id": "version-789",
            "expected_current_version_id": "version-456",
            "version": "1.1.0",
            "author": "Architecture Team v2",
        }
    )

    assert updated["success"] is True
    listed = await manager.handle_skills_list({"with_installed": True})
    skill = next(item for item in listed["skills"] if item["name"] == "enterprise-architecture")
    detail = await manager.handle_skills_get(
        {"name": "enterprise-architecture", "origin": "hub-market:skill-123"}
    )
    assert skill["version"] == "1.1.0"
    assert skill["author"] == "Architecture Team v2"
    assert detail["version"] == "1.1.0"
    assert detail["author"] == "Architecture Team v2"
