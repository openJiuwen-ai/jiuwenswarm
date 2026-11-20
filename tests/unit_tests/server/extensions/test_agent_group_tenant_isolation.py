# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise agent groups live under the bound tenant, not the shared data root."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import jiuwenswarm.common.utils as utils
from jiuwenswarm.enterprise.capabilities.experts.request_packages import allows
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
        utils.get_user_workspace_dir() / ".agent_teams" / "agent_groups"
    )


def test_unbound_enterprise_does_not_write_the_data_root(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del extension_workspace
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    data_root = utils.get_user_workspace_dir()
    source = _seed_valid_agent_group(
        tmp_path / "source" / "agent" / "workspace",
        "finance-team",
        under="local",
    )

    with pytest.raises(AgentGroupPackageError, match="bound tenant"):
        catalog.import_agent_group({"path": str(source)})

    assert catalog.list_agent_groups() == []
    assert catalog.read_agent_group_marketplace_entries() == []
    assert catalog._resolve_agent_group_definition_dir("finance-team") is None
    listed = asyncio.run(catalog.list_agent_groups_with_hub({"filter": "local"}))
    assert listed == []
    assert not (data_root / ".agent_teams").exists()


def test_bound_tenants_do_not_see_each_others_groups(
    extension_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del extension_workspace
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    data_root = utils.get_user_workspace_dir()
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
