# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression: skills.updates.check must not read SkillUpdateStatus.downloadable."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.sdk.skill_source import (
    InstalledArtifact,
    ProviderInvocationContext,
    SkillRef,
    SkillUpdateStatus,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


class _UpdateProvider:
    source_id = "swarmskillhub"
    capabilities = frozenset({"check_updates"})

    async def check_updates(
        self,
        installed: list[InstalledArtifact] | tuple[InstalledArtifact, ...],
        context: ProviderInvocationContext,
    ) -> tuple[SkillUpdateStatus, ...]:
        del context
        return tuple(
            SkillUpdateStatus(
                skill_ref=SkillRef(item.source_id, item.skill_id),
                current_version_id=item.version_id,
                latest_version_id="ver-2",
                has_update=True,
                latest_version="2.0.0",
                accessible=True,
                remote_status="available",
            )
            for item in installed
        )


@pytest.mark.asyncio
async def test_skills_updates_check_omits_downloadable(tmp_path, monkeypatch) -> None:
    manager = SkillManager(workspace_dir=str(tmp_path))
    provider = _UpdateProvider()

    monkeypatch.setattr(
        manager,
        "_get_installed_plugins",
        lambda: [
            {
                "installation_id": "inst-1",
                "name": "demo-skill",
                "source_id": "swarmskillhub",
                "skill_id": "asset-1",
                "version_id": "ver-1",
                "version": "1.0.0",
            }
        ],
    )

    async def _get(source_id: str, capability: str | None = None):
        assert source_id == "swarmskillhub"
        assert capability == "check_updates"
        return provider

    monkeypatch.setattr(manager._source_registry, "get", _get)

    result = await manager.handle_skills_updates_check({"source_id": "swarmskillhub"})

    assert result["success"] is True
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["skill_id"] == "asset-1"
    assert item["has_update"] is True
    assert item["latest_version_id"] == "ver-2"
    assert "downloadable" not in item
