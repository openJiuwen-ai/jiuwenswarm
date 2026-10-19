# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression: hot-reload inject must carry the parent agent's workspace and
``sys_operation`` into the general-purpose subagent spec.

The hot-reload path ``JiuWenSwarmDeepAdapter._make_deep_agent_config`` builds a
``DeepAgentConfig`` directly (it does not re-enter ``create_deep_agent``), so the
factory's ``_inject_general_purpose_subagent`` is invoked explicitly at the call
site. The cold-start path (``resolve_deep_agent_parts``) passes the parent's
``workspace`` and ``sys_operation`` into that call so the injected spec stays
inside the parent's filesystem boundary — otherwise
``DeepAgent.create_subagent`` mints a fresh LOCAL ``SysOperation`` for the
general-purpose subagent and it escapes the parent's sandbox / remote
filesystem boundary.

This locks down that the hot-reload call site passes both fields and that they
land on the injected spec as the *same instance* the parent ``DeepAgentConfig``
carries — removing the two kwargs at the call site makes both assertions fail.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.config import SubAgentConfig


# general_agent on (so should_add_general_agent=True), browser/research off so
# _build_configured_subagents does not try to build a browser subagent.
_CFG = {
    "max_iterations": 15,
    "subagents": {
        "general_agent": {"enabled": True},
        "research_agent": {"enabled": False},
        "browser_agent": {"enabled": False},
    },
    "models": {"default": {"model_client_config": {"model_name": "stub"}}},
}


def _general_purpose_spec(cfg):
    specs = [
        s
        for s in (cfg.subagents or [])
        if getattr(getattr(s, "agent_card", None), "name", None) == "general-purpose"
    ]
    assert len(specs) == 1, f"expected one injected general-purpose spec, got {len(specs)}"
    spec = specs[0]
    assert isinstance(spec, SubAgentConfig)
    return spec


def test_hot_reload_injected_spec_carries_parent_workspace_and_sys_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The hot-reload injected general-purpose spec must carry the parent
    agent's resolved ``workspace`` and ``sys_operation`` — same instances the
    parent ``DeepAgentConfig`` carries, not fresh LOCAL substitutes.
    """
    adapter = JiuWenSwarmDeepAdapter()

    # Sentinel sys_operation: identity probe. If the call site drops the
    # ``sys_operation=`` kwarg, the spec falls back to None (a fresh LOCAL
    # SysOperation at create_subagent time) and this identity check fails.
    sentinel_sysop = object()
    adapter._sys_operation = sentinel_sysop
    adapter._workspace_dir = str(tmp_path)

    # Keep _build_configured_subagents from reaching for a browser runtime or
    # the on-disk config (we feed our own CFG as config_base).
    monkeypatch.setattr(interface_module, "get_config", lambda *a, **k: _CFG)
    adapter._sync_browser_runtime_environment = lambda *a, **k: None  # type: ignore[assignment]
    adapter._browser_runtime_enabled = lambda *a, **k: False  # type: ignore[assignment]

    cfg = adapter._make_deep_agent_config(
        model=object(),
        config=_CFG,
        config_base=_CFG,
        agent_card=AgentCard(name="main_agent", description="verify"),
        tool_cards=[],
        rails=None,
    )

    spec = _general_purpose_spec(cfg)

    # Parent DeepAgentConfig must itself carry both resolved values.
    assert cfg.workspace is not None
    assert cfg.sys_operation is sentinel_sysop

    # [P1] The injected spec inherits the SAME workspace instance (not a new
    # one) and the SAME sys_operation instance (not a fresh LOCAL one).
    assert spec.workspace is cfg.workspace, (
        "hot-reload inject dropped parent workspace; general-purpose subagent "
        "would escape the parent filesystem boundary"
    )
    assert spec.sys_operation is sentinel_sysop, (
        "hot-reload inject dropped parent sys_operation; create_subagent would "
        "mint a fresh LOCAL SysOperation for the general-purpose subagent"
    )
    assert getattr(spec.workspace, "root_path", None) == str(tmp_path)


def test_deep_adapter_live_hot_switch_rebuilds_child_spec_on_same_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Same Deep adapter, no rebuild: switch workspace then remake config.

    The new general-purpose spec must use the new root and the same
    sys_operation instance the parent still carries.
    """
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()

    adapter = JiuWenSwarmDeepAdapter()
    sentinel_sysop = object()
    adapter._sys_operation = sentinel_sysop
    adapter._workspace_dir = str(old_root)

    monkeypatch.setattr(interface_module, "get_config", lambda *a, **k: _CFG)
    adapter._sync_browser_runtime_environment = lambda *a, **k: None  # type: ignore[assignment]
    adapter._browser_runtime_enabled = lambda *a, **k: False  # type: ignore[assignment]

    first = adapter._make_deep_agent_config(
        model=object(),
        config=_CFG,
        config_base=_CFG,
        agent_card=AgentCard(name="main_agent", description="verify"),
        tool_cards=[],
        rails=None,
    )
    first_spec = _general_purpose_spec(first)
    assert getattr(first_spec.workspace, "root_path", None) == str(old_root)
    assert first.sys_operation is sentinel_sysop

    adapter._workspace_dir = str(new_root)
    second = adapter._make_deep_agent_config(
        model=object(),
        config=_CFG,
        config_base=_CFG,
        agent_card=AgentCard(name="main_agent", description="verify"),
        tool_cards=[],
        rails=None,
    )
    second_spec = _general_purpose_spec(second)
    assert second is not first
    assert getattr(second.workspace, "root_path", None) == str(new_root)
    assert getattr(second_spec.workspace, "root_path", None) == str(new_root)
    assert second.sys_operation is sentinel_sysop
    assert second_spec.sys_operation is sentinel_sysop
    assert second_spec.workspace is second.workspace
