# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code TTSE is plugin-only; legacy switches must not mount a builtin rail."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep


def test_code_ttse_bank_path_uses_agent_workspace_not_project(tmp_path):
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(tmp_path / "project")
    adapter._workspace_dir = str(tmp_path / "project")
    adapter._agent_workspace_dir = str(tmp_path / "agent")

    path = Path(adapter._ttse_bank_path())

    assert path == tmp_path / "agent" / ".ttse" / "code" / "bank.json"


def test_code_and_work_ttse_banks_are_isolated(tmp_path):
    agent_ws = tmp_path / "agent"
    code_adapter = interface_code.JiuwenSwarmCodeAdapter()
    code_adapter._workspace_dir = str(tmp_path / "project")
    code_adapter._agent_workspace_dir = str(agent_ws)
    work_adapter = interface_deep.JiuWenSwarmDeepAdapter()
    work_adapter._workspace_dir = str(agent_ws)

    code_path = Path(code_adapter._ttse_bank_path())
    work_path = Path(work_adapter._ttse_bank_path())

    assert work_path == agent_ws / ".ttse" / "work" / "bank.json"
    assert code_path != work_path
    assert code_path.parent != work_path.parent


def test_code_never_delegates_to_builtin_ttse_builder(monkeypatch, tmp_path):
    built: list[object] = []

    def fake_build(self, config):
        rail = object()
        built.append(rail)
        return rail

    monkeypatch.setattr(
        interface_deep.JiuWenSwarmDeepAdapter,
        "_build_ttse_rail",
        fake_build,
    )

    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(tmp_path)
    adapter._workspace_dir = str(tmp_path)
    adapter._agent_workspace_dir = str(tmp_path / "agent")

    disabled = {
        "react": {"ttse": {"enabled": False}},
        "modes": {"code": {}},
    }
    enabled = {
        "react": {"ttse": {"enabled": True}},
        "modes": {"code": {}},
    }

    assert adapter._build_ttse_rail(disabled) is None
    assert adapter._build_ttse_rail(enabled) is None
    assert built == []


def test_update_rails_for_mode_does_not_mount_ttse_for_legacy_switch(monkeypatch, tmp_path):
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(tmp_path)
    adapter._workspace_dir = str(tmp_path)
    adapter._agent_workspace_dir = str(tmp_path / "agent")
    adapter._instance = SimpleNamespace(
        register_rail=AsyncMock(),
        unregister_rail=AsyncMock(),
        ability_manager=SimpleNamespace(list=lambda: []),
    )

    # Avoid unrelated rail side effects in _update_rails_for_mode.
    monkeypatch.setattr(
        adapter,
        "_build_subagent_rail",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(adapter, "_build_project_memory_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_coding_memory_rail", lambda: None)
    monkeypatch.setattr(
        adapter,
        "_sync_personal_context_rail",
        AsyncMock(),
    )
    monkeypatch.setattr(adapter, "_mark_ttse_consult_direct_exposure", lambda: None)
    monkeypatch.setattr(adapter, "_sync_ttse_rail_config", lambda *_a, **_k: None)

    enabled_cfg = {
        "react": {"ttse": {"enabled": True}},
        "modes": {"code": {"memory": {"enabled": False}}},
    }
    disabled_cfg = {
        "react": {"ttse": {"enabled": False}},
        "modes": {"code": {"memory": {"enabled": False}}},
    }

    with adapter._code_spec_config_scope(enabled_cfg):
        asyncio.run(adapter._update_rails_for_mode("agent.code.normal"))

    assert adapter._ttse_rail is None
    adapter._instance.register_rail.assert_not_awaited()

    with adapter._code_spec_config_scope(disabled_cfg):
        asyncio.run(adapter._update_rails_for_mode("agent.code.normal"))

    assert adapter._ttse_rail is None
    adapter._instance.register_rail.assert_not_awaited()
    adapter._instance.unregister_rail.assert_not_awaited()
