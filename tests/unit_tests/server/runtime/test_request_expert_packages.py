# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""本次请求的专家包：个人版不读策略，企业版只放行用户包和请求里下发的受控包。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.expert_packages import METADATA_KEY
from jiuwenswarm.enterprise.capabilities.experts.request_packages import (
    allows,
    bind_request_subject,
    hidden_from_enterprise,
)


def _bind_workspace(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def _agent_workspace(tenant_key: str | None = None) -> Path:
        del tenant_key
        return root

    def _user_workspace() -> Path:
        return root

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_workspace_dir",
        _agent_workspace,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        _user_workspace,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager.get_agent_workspace_dir",
        _agent_workspace,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager.get_user_workspace_dir",
        _user_workspace,
    )


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _bind_packages(packages: list[dict], **identity):
    metadata = dict(identity)
    metadata[METADATA_KEY] = packages
    return bind_request_subject(metadata)


def test_personal_bind_does_not_read_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    def _deny(*_args, **_kwargs):
        raise AssertionError("personal bind must not read metadata")

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.capabilities.experts.request_packages._packages_from_metadata",
        _deny,
    )
    with _bind_packages(
        [{"kind": "agent_template", "id": "kept"}],
        user_id="alice",
    ):
        assert allows("agent_template", "kept") is True


def test_personal_allows_without_reading_request_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    def _deny_read() -> list[dict]:
        raise AssertionError("personal edition must not read the request packages")

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.capabilities.experts.request_packages._request_packages",
        _deny_read,
    )
    assert allows("agent_template", "hub-expert") is True
    assert allows("agent_group", "finance") is True
    assert hidden_from_enterprise("794db1bfe8884d6897d62d3584894a59") is False


def test_enterprise_empty_packages_denies_controlled_and_allows_user_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    assert hidden_from_enterprise("794db1bfe8884d6897d62d3584894a59") is True
    _bind_workspace(monkeypatch, tmp_path)
    _mkdir(tmp_path / "plugins" / "agent_templates" / "local" / "mine")
    _mkdir(tmp_path / "plugins" / "agent_templates" / "built_in" / "builtin-expert")
    market = tmp_path / "plugins" / "agent_templates" / "marketplace.json"
    _mkdir(market.parent)
    market.write_text(
        json.dumps({"plugins": [{"id": "from-hub", "source": "hub", "installed": True}]}),
        encoding="utf-8",
    )
    _mkdir(tmp_path / "plugins" / "agent_templates" / "local" / "from-hub")

    with _bind_packages([]):
        assert allows("agent_template", "mine") is True
        assert allows("agent_template", "builtin-expert") is False
        assert allows("agent_template", "from-hub") is False


def test_enterprise_packages_then_revoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    _mkdir(tmp_path / "plugins" / "agent_templates" / "built_in" / "builtin-expert")
    granted = [{"kind": "agent_template", "id": "builtin-expert"}]
    with _bind_packages(granted):
        assert allows("agent_template", "builtin-expert") is True
    with _bind_packages([]):
        assert allows("agent_template", "builtin-expert") is False


def test_allowed_install_still_checks_connectors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    granted = [{"kind": "agent_template", "id": "hub-expert"}]
    from jiuwenswarm.server.runtime import extension_package_manager as equipment

    monkeypatch.setattr(equipment, "_equipment_definition_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(equipment, "manifest_connector_names", lambda *_args, **_kwargs: ["mail"])
    monkeypatch.setattr(equipment, "unready_connectors", lambda names: list(names))
    with _bind_packages(granted):
        ok, payload = asyncio.run(
            equipment.install_equipment_from_hub_gated("agent_templates", {"id": "hub-expert"})
        )
    assert ok is False
    assert payload["pending_connectors"] == ["mail"]


def test_empty_packages_rejects_hub_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    from jiuwenswarm.server.runtime import extension_package_manager as equipment

    prepared: list[str] = []

    async def _prepare(*_args, **_kwargs) -> str:
        prepared.append("downloaded")
        return "hub-expert"

    monkeypatch.setattr(equipment, "prepare_hub_equipment", _prepare)
    monkeypatch.setattr(equipment, "_equipment_definition_exists", lambda *_args, **_kwargs: False)
    with _bind_packages([]):
        ok, payload = asyncio.run(
            equipment.install_equipment_from_hub_gated("agent_templates", {"id": "hub-expert"})
        )
    assert ok is False
    assert prepared == []
    assert "not allowed" in payload["error"]


def test_revoked_template_fails_next_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    granted = [{"kind": "agent_template", "id": "hub-expert"}]
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        interface_deep.equipment,
        "is_agent_template_installed",
        lambda _name: True,
    )
    params = {"agent_template_name": "hub-expert", "plugin_names": []}
    with _bind_packages(granted):
        assert interface_deep.JiuWenSwarmDeepAdapter._marketplace_equipment_gate(params) is None
    with _bind_packages([]):
        message = interface_deep.JiuWenSwarmDeepAdapter._marketplace_equipment_gate(params)
    assert message == "agent_template not allowed: hub-expert"


def test_expert_group_catalog_and_launch_follow_request_packages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    _mkdir(tmp_path / ".agent_teams" / "agent_groups" / "local" / "mine")
    granted = [{"kind": "agent_group", "id": "keep"}]

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jiuwenswarm.agents.harness.team.experts import catalog as catalog_mod
    from jiuwenswarm.agents.harness.team.experts import JiuwenExpertCatalog
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    monkeypatch.setattr(
        catalog_mod, "_list_installed",
        AsyncMock(return_value=[SimpleNamespace(name=name) for name in ("keep", "drop", "mine")]),
    )
    launcher = JiuwenExpertTeamLauncher()

    def _passed(self) -> None:
        raise RuntimeError("packages passed")

    monkeypatch.setattr(JiuwenExpertTeamLauncher, "ensure_turn_runner_installed", _passed)
    with _bind_packages(granted):
        names = [item.name for item in asyncio.run(JiuwenExpertCatalog().list_group_packages())]
        assert names == ["keep"]
        with pytest.raises(ValueError, match="not allowed"):
            asyncio.run(
                launcher.launch(
                    organization_id="org",
                    agent_group_name="drop",
                    session_id="web_session",
                )
            )
        with pytest.raises(RuntimeError, match="packages passed"):
            asyncio.run(
                launcher.launch(
                    organization_id="org",
                    agent_group_name="keep",
                    session_id="web_session",
                )
            )
