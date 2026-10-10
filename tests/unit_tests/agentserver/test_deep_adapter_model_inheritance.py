# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Request model selection must reach both the main executor and new children."""

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.common.invocation_context.model_trace import TraceAwareModel
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _model(name):
    return TraceAwareModel(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_key="test-key",
            api_base="http://test.invalid/v1",
        ),
        model_config=ModelRequestConfig(model=name, temperature=0.2),
    )


def _adapter(tmp_path):
    initial, flash, pinned = _model("think"), _model("flash"), _model("specialist")
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model = initial
    adapter._model_cache = {"think": initial, "flash": flash}
    adapter._model_name_to_keys = {}
    adapter._instance = create_deep_agent(
        model=initial,
        workspace=str(tmp_path),
        auto_create_workspace=False,
        add_general_purpose_agent=True,
        subagents=[
            SubAgentConfig(
                agent_card=AgentCard(name="specialist", description="pinned"),
                system_prompt="specialist",
                model=pinned,
            )
        ],
    )
    return adapter, initial, flash, pinned


@pytest.mark.parametrize("requested", ["flash", "dynamic-flash"])
def test_request_model_reaches_main_and_general_purpose(tmp_path, requested):
    adapter, initial, flash, pinned = _adapter(tmp_path)
    selected = adapter._resolve_model_for_request(
        SimpleNamespace(params={"model_name": requested})
    )
    adapter._apply_model_to_react_agent(selected, model_name_override=requested)

    parent = adapter._instance
    effective = parent._deep_config.model
    assert effective.model_config.model_name == requested
    assert parent._react_agent._config.model_name == requested
    assert parent._react_agent._get_llm() is effective
    assert parent._react_agent._config.model_config_obj is effective.model_config
    assert adapter._model_request_config is effective.model_config
    assert isinstance(effective, TraceAwareModel)
    assert effective.model_config.temperature == 0.2
    assert effective.model_client_config is selected.model_client_config

    child = parent.create_subagent("general-purpose", "general-session")
    assert child._deep_config.model is effective
    assert child._react_agent._config.model_name == requested
    assert child._react_agent._get_llm() is effective
    if requested == "flash":
        assert effective is flash
    specialist = parent.create_subagent("specialist", "specialist-session")
    assert specialist._deep_config.model is pinned
    assert initial.model_config.model_name == "think"
    assert flash.model_config.model_name == "flash"


def test_switching_model_does_not_change_existing_children_or_shared_cache(tmp_path):
    adapter, initial, flash, _ = _adapter(tmp_path)
    adapter._apply_model_to_react_agent(initial, model_name_override="dynamic-flash")
    first = adapter._instance.create_subagent("general-purpose", "first")
    adapter._apply_model_to_react_agent(flash)
    second = adapter._instance.create_subagent("general-purpose", "second")
    assert first._react_agent._config.model_name == "dynamic-flash"
    assert second._react_agent._config.model_name == "flash"
    adapter._apply_model_to_react_agent(initial)
    third = adapter._instance.create_subagent("general-purpose", "third")
    assert third._react_agent._config.model_name == "think"
    assert initial.model_config.model_name == "think"
