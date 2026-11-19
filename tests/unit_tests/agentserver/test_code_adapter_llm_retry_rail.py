# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code 模式的模型重试装配和流式提示回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.rails import LLMRetryRail

from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import NotifyingLLMRetryRail
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter


@pytest.mark.asyncio
async def test_code_mode_mounts_one_retry_rail_and_streams_both_notifications(monkeypatch, tmp_path):
    adapter = JiuwenSwarmCodeAdapter(workspace_dir=str(tmp_path))
    instantiate = adapter._instantiate_rails

    def instantiate_retry_rails(rail_infos, config_base):
        # 保留真实的 adapter 装配，只隔离与重试无关的 rail 构建。
        return instantiate([info for info in rail_infos if info.attr_name == "_llm_retry_rail"], config_base)

    monkeypatch.setattr(adapter, "_instantiate_rails", instantiate_retry_rails)
    config_base = {"execution_guard": {"llm_retry_rail": {"enabled": True, "max_retries": 1}}}
    rails = adapter._build_agent_rails({}, config_base, mode="code", sub_mode="normal")
    model = Model(
        ModelClientConfig(client_provider="OpenAI", api_key="test", api_base="https://example.invalid"),
        ModelRequestConfig(model_name="test-model"),
    )
    agent = create_deep_agent(model=model, rails=rails, workspace=str(tmp_path), enable_sys_operation=False)

    retry_rails = agent.find_rails_by_type((LLMRetryRail,))
    assert len(retry_rails) == 1
    rail = retry_rails[0]
    assert isinstance(rail, NotifyingLLMRetryRail)
    write_stream = AsyncMock()
    ctx = AgentCallbackContext(
        agent=agent,
        session=SimpleNamespace(write_stream=write_stream),
        exception=RuntimeError("openAI API async stream error: APIConnectionError: Connection error."),
    )

    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None

    retry_chunk, exhausted_chunk = [call.args[0] for call in write_stream.await_args_list]
    retry_event = adapter._parse_stream_chunk(retry_chunk)
    assert retry_event["event_type"] == "chat.delta"
    assert "第 1/1 次重试" in retry_event["content"]
    exhausted_event = adapter._parse_stream_chunk(exhausted_chunk)
    assert exhausted_event["event_type"] == "chat.error"
    assert "已用尽自动重试" in exhausted_event["error"]
