# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise agent groups live under the bound tenant, not the shared data root."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import jiuwenswarm.common.utils as utils
from jiuwenswarm.enterprise.capabilities.experts.allowlist import allows
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.server.runtime import extension_package_manager as catalog
from jiuwenswarm.server.runtime.extension_package_manager import AgentGroupPackageError
from tests.unit_tests.server.extensions.test_extension_package_manager import (
    _seed_valid_agent_group,
)


def _ids(cards: list[dict]) -> set[str]:
    return {str(card.get("id") or "") for card in cards}


def test_personal_group_root_stays_on_data_root(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del extension_workspace
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    assert catalog._kind_root("agent_groups") == (
        utils.get_domain_data_dir() / ".agent_teams" / "agent_groups"
    )


def test_unbound_enterprise_does_not_write_the_data_root(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del extension_workspace
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    data_root = utils.get_domain_data_dir()
    source = _seed_valid_agent_group(
        tmp_path / "source" / "agent" / "workspace",
        "finance-team",
        under="local",
    )

    assert catalog.ensure_builtin_agent_groups_installed() == []
    with pytest.raises(AgentGroupPackageError, match="bound tenant"):
        catalog.import_agent_group({"path": str(source)})

    assert not (data_root / ".agent_teams").exists()


def test_bound_tenants_do_not_see_each_others_groups(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del extension_workspace
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    data_root = utils.get_domain_data_dir()
    source = _seed_valid_agent_group(
        tmp_path / "source" / "agent" / "workspace",
        "finance-team",
        under="local",
    )

    with TenantContext.scoped("user-1"):
        result = catalog.import_agent_group({"path": str(source)})
        assert result == {"id": "finance-team"}
        tenant_one = TenantContext.current_tenant_root()
        assert tenant_one is not None
        assert (
            tenant_one / ".agent_teams" / "agent_groups" / "local" / "finance-team"
        ).is_dir()
        listed = asyncio.run(catalog.list_agent_groups_with_hub({"filter": "local"}))
        assert "finance-team" in _ids(listed)
        assert allows("agent_group", "finance-team") is True

    with TenantContext.scoped("user-2"):
        listed = asyncio.run(catalog.list_agent_groups_with_hub({"filter": "local"}))
        assert "finance-team" not in _ids(listed)
        assert allows("agent_group", "finance-team") is False

    assert not (data_root / ".agent_teams" / "agent_groups" / "local" / "finance-team").exists()


def test_bound_enterprise_does_not_seed_builtin_groups(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del extension_workspace
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    resource_package = _seed_valid_agent_group(
        tmp_path / "resource-home" / "agent" / "workspace",
        "designer",
        under="resources",
    )
    monkeypatch.setattr(
        catalog,
        "get_equipment_resources_agent_groups_dir",
        lambda: resource_package.parent,
    )

    with TenantContext.scoped("user-1"):
        assert catalog.ensure_builtin_agent_groups_installed() == []
        tenant_root = TenantContext.current_tenant_root()
        assert tenant_root is not None
        assert not (
            tenant_root / ".agent_teams" / "agent_groups" / ".builtin_seeded"
        ).exists()
        assert not (
            tenant_root / ".agent_teams" / "agent_groups" / "built_in" / "designer"
        ).exists()


def test_default_tenant_key_rejects_group_import(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del extension_workspace
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key

    source = _seed_valid_agent_group(
        tmp_path / "source" / "agent" / "workspace",
        "finance-team",
        under="local",
    )
    with TenantContext.scoped(build_tenant_key()):
        tenant_root = TenantContext.current_tenant_root()
        assert tenant_root is not None
        with pytest.raises(AgentGroupPackageError, match="bound tenant"):
            catalog.import_agent_group({"path": str(source)})
        assert not (tenant_root / ".agent_teams").exists()

    assert not (utils.get_domain_data_dir() / ".agent_teams").exists()


async def _directory_prompt() -> str:
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
    from openjiuwen.harness.prompts import SystemPromptBuilder
    from openjiuwen.harness.prompts.prompt_attachment_manager import (
        PromptAttachmentManager,
    )
    from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import (
        RuntimePromptRail,
    )

    class _Agent:
        def __init__(self, builder: SystemPromptBuilder) -> None:
            self.system_prompt_builder = builder
            self.prompt_attachment_manager = PromptAttachmentManager()

    class _Session:
        def get_session_id(self) -> str:
            return "sess1"

    builder = SystemPromptBuilder(language="cn")
    agent = _Agent(builder)
    rail = RuntimePromptRail(language="cn", channel="web")
    rail.init(agent)
    await rail.before_model_call(
        AgentCallbackContext(agent=agent, inputs=None, session=_Session(), extra={})
    )
    return builder.build()


@pytest.mark.asyncio
async def test_prompt_agent_group_dir_follows_real_tenant(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del extension_workspace
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    personal = await _directory_prompt()
    personal_dir = utils.get_domain_data_dir() / ".agent_teams" / "agent_groups" / "local"
    assert f"已安装的专家团存放在：`{personal_dir}`" in personal

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    unbound = await _directory_prompt()
    assert "已安装的专家团" not in unbound

    from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key

    with TenantContext.scoped(build_tenant_key()):
        default_key = await _directory_prompt()
    assert "已安装的专家团" not in default_key

    with TenantContext.scoped("user-1"):
        tenant_root = TenantContext.current_tenant_root()
        assert tenant_root is not None
        bound = await _directory_prompt()
    expected = tenant_root / ".agent_teams" / "agent_groups" / "local"
    assert f"已安装的专家团存放在：`{expected}`" in bound
