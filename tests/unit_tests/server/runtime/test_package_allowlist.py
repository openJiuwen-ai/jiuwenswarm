# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包允许名单：个人版不读策略，企业版只放行用户包和请求名单命中的受控包。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.package_allowlist import (
    allows,
    bind_request_subject,
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


def _grant(packages: list[dict], **identity):
    metadata = dict(identity)
    metadata["package_allowlist"] = packages
    return bind_request_subject(metadata)


def test_personal_allows_without_reading_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    def _deny_read() -> list[dict]:
        raise AssertionError("personal edition must not read the grant")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.package_allowlist._request_packages",
        _deny_read,
    )
    assert allows("agent_template", "hub-expert") is True
    assert allows("agent_group", "finance") is True


def test_enterprise_empty_grant_denies_controlled_and_allows_user_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
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

    with _grant([]):
        assert allows("agent_template", "mine") is True
        assert allows("agent_template", "builtin-expert") is False
        assert allows("agent_template", "from-hub") is False


def test_enterprise_grant_then_revoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    _mkdir(tmp_path / "plugins" / "agent_templates" / "built_in" / "builtin-expert")
    granted = [{"kind": "agent_template", "id": "builtin-expert"}]
    with _grant(granted):
        assert allows("agent_template", "builtin-expert") is True
    with _grant([]):
        assert allows("agent_template", "builtin-expert") is False


def test_scope_limits_controlled_package_to_matching_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    scoped = [
        {
            "kind": "agent_template",
            "id": "builtin-expert",
            "match_expr": "user_id == 'alice'",
        }
    ]
    with _grant(scoped, user_id="alice"):
        assert allows("agent_template", "builtin-expert") is True
    with _grant(scoped, user_id="bob"):
        assert allows("agent_template", "builtin-expert") is False
    assert allows("agent_template", "builtin-expert") is False

    open_grant = [{"kind": "agent_template", "id": "builtin-expert"}]
    with _grant(open_grant, user_id="bob"):
        assert allows("agent_template", "builtin-expert") is True
    with _grant(open_grant, user_id="alice", routing={"group_id": "finance"}):
        assert allows("agent_template", "builtin-expert") is True


def test_granted_install_still_checks_connectors(
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
    with _grant(granted):
        ok, payload = asyncio.run(
            equipment.install_equipment_from_hub_gated("agent_templates", {"id": "hub-expert"})
        )
    assert ok is False
    assert payload["pending_connectors"] == ["mail"]


def test_empty_grant_rejects_hub_download(
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

    monkeypatch.setattr(equipment, "_prepare_hub_equipment", _prepare)
    monkeypatch.setattr(equipment, "_equipment_definition_exists", lambda *_args, **_kwargs: False)
    with _grant([]):
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
    with _grant(granted):
        assert interface_deep.JiuWenSwarmDeepAdapter._marketplace_equipment_gate(params) is None
    with _grant([]):
        message = interface_deep.JiuWenSwarmDeepAdapter._marketplace_equipment_gate(params)
    assert message == "agent_template not allowed: hub-expert"


def test_expert_group_catalog_and_launch_follow_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _bind_workspace(monkeypatch, tmp_path)
    _mkdir(tmp_path / ".agent_teams" / "agent_groups" / "local" / "mine")
    granted = [{"kind": "agent_group", "id": "keep"}]

    from jiuwenswarm.agents.harness.team.expert_org import catalog as catalog_mod
    from jiuwenswarm.agents.harness.team.expert_org.catalog import (
        ExpertGroupDescriptor,
        JiuwenExpertGroupCatalog,
    )
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    monkeypatch.setattr(
        catalog_mod,
        "_iter_agent_group_dirs",
        lambda: [("keep", tmp_path), ("drop", tmp_path), ("mine", tmp_path)],
    )
    monkeypatch.setattr(
        catalog_mod,
        "descriptor_from_agent_group_dir",
        lambda name, _path: ExpertGroupDescriptor(agent_group_name=name),
    )
    launcher = JiuwenExpertTeamLauncher()

    def _passed(self) -> None:
        raise RuntimeError("grant passed")

    monkeypatch.setattr(JiuwenExpertTeamLauncher, "ensure_turn_runner_installed", _passed)
    with _grant(granted):
        names = [item.agent_group_name for item in JiuwenExpertGroupCatalog.list()]
        assert names == ["keep", "mine"]
        with pytest.raises(ValueError, match="not allowed"):
            asyncio.run(
                launcher.launch(
                    organization_id="org",
                    agent_group_name="drop",
                    session_id="web_session",
                )
            )
        with pytest.raises(RuntimeError, match="grant passed"):
            asyncio.run(
                launcher.launch(
                    organization_id="org",
                    agent_group_name="keep",
                    session_id="web_session",
                )
            )
