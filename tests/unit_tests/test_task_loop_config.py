from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.task_loop_config import (
    resolve_task_loop_completion_timeout,
)


def test_missing_completion_timeout_defaults_to_six_hours() -> None:
    assert resolve_task_loop_completion_timeout({}) == 21600.0


@pytest.mark.parametrize("value", [None, ""])
def test_explicit_empty_completion_timeout_disables_deadline(value: object) -> None:
    assert resolve_task_loop_completion_timeout({"completion_timeout": value}) is None


@pytest.mark.parametrize("value", [3600, 3600.0, "3600"])
def test_completion_timeout_preserves_explicit_positive_value(value: object) -> None:
    assert resolve_task_loop_completion_timeout({"completion_timeout": value}) == 3600.0


@pytest.mark.parametrize("value", [0, -1, True, "invalid", "nan", "inf", 10**400])
def test_completion_timeout_rejects_invalid_value(value: object) -> None:
    with pytest.raises(ValueError, match="completion_timeout"):
        resolve_task_loop_completion_timeout({"completion_timeout": value})


@pytest.mark.asyncio
@pytest.mark.parametrize(("override", "expected"), [
    ({}, 21600.0),
    ({"completion_timeout": None}, None),
    ({"completion_timeout": 7200}, 7200.0),
])
async def test_code_session_passes_merged_timeout_to_sdk(monkeypatch, override, expected):
    from jiuwenswarm.common.utils import (
        load_yaml_dict,
        merge_template_with_override,
        resolve_shipped_template_config_path,
    )
    from jiuwenswarm.server.runtime.agent_adapter import interface_code

    template = load_yaml_dict(resolve_shipped_template_config_path())
    config = merge_template_with_override(template, {"react": override})
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    adapter.mark_as_session_scoped("timeout-test")
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", Mock())
    monkeypatch.setattr(adapter, "_create_model", Mock(return_value=object()))
    monkeypatch.setattr(adapter, "_get_tool_cards", AsyncMock(return_value=[]))
    monkeypatch.setattr(adapter, "_build_agent_rails", Mock(return_value=[]))
    monkeypatch.setattr(adapter, "_create_sys_operation", Mock(return_value=object()))
    monkeypatch.setattr(adapter, "_build_configured_subagents", Mock(return_value=([], False)))
    monkeypatch.setattr(adapter, "_build_agent_workspace", Mock(return_value=object()))
    factory = Mock(side_effect=RuntimeError("reached SDK factory"))
    monkeypatch.setattr(interface_code, "create_deep_agent", factory)

    with pytest.raises(RuntimeError, match="reached SDK factory"):
        await adapter.create_instance(config_base=config, sub_mode="normal")
    assert factory.call_args.kwargs["completion_timeout"] == expected


@pytest.mark.asyncio
async def test_invalid_reload_keeps_last_timeout_for_future_sessions():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    root = JiuWenSwarmDeepAdapter()
    await root.reload_agent_config({"react": {"completion_timeout": 7200}})
    previous_version = root._session_adapter_config_version
    with pytest.raises(ValueError, match="react.completion_timeout"):
        await root.reload_agent_config({"react": {"completion_timeout": "bad"}})

    assert root._config_cache["completion_timeout"] == 7200
    assert root._startup_config_base["react"]["completion_timeout"] == 7200
    assert root._session_adapter_config_version == previous_version

    await root.reload_agent_config({"react": {"completion_timeout": None}})
    assert root._startup_config_base["react"]["completion_timeout"] is None


@pytest.mark.asyncio
async def test_invalid_reload_preserves_existing_instance_and_cached_timeout(mocker):
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = JiuWenSwarmDeepAdapter()
    await adapter.reload_agent_config({"react": {"completion_timeout": 7200}})
    card = AgentCard(name="timeout-test")
    instance = DeepAgent(card).configure(DeepAgentConfig(card=card, completion_timeout=7200))
    adapter._instance = instance
    configure = mocker.spy(instance, "configure")

    with pytest.raises(ValueError, match=r"react\.completion_timeout"):
        await adapter.reload_agent_config({"react": {"completion_timeout": -1}})

    configure.assert_not_called()
    assert adapter._instance is instance
    assert instance.deep_config.completion_timeout == 7200
    assert adapter._config_base_cache["react"]["completion_timeout"] == 7200
    assert adapter._startup_config_base["react"]["completion_timeout"] == 7200
    assert adapter._config_cache["completion_timeout"] == 7200
