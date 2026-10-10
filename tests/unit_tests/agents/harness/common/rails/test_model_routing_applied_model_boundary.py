# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""回归：路由后实际生效的模型名必须跨过「外层 ctx → 内层 ctx」这道边界。

`ModelRoutingRail.before_invoke` 跑在外层 DeepAgent 的回调上下文上（`BEFORE_INVOKE`
属 openjiuwen 的 outer-only 事件），`RuntimePromptRail.before_model_call` 被桥接到内层
ReActAgent（`BEFORE_MODEL_CALL` 属 bridge 事件），内层每次 invoke 都新建
`AgentCallbackContext`、`extra` 是全新 dict，两边不共享。唯一的通道是按引用透传的
`RunContext`：外层写 ``ctx.inputs.run_context.extra``，内层经
``ctx.extra["run_context"].extra`` 读回。

本文件走**真实 DeepAgent 事件链**。既有 UT 手工造单一 ctx 直接调 rail —— 那条路径恰好
绕开这道边界，会让跨 ctx 缺陷在 CI 全绿下合入。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from openjiuwen.core.foundation.llm import AssistantMessage, UsageMetadata
from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import DeepAgent, DeepAgentConfig
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails import runtime_prompt_rail as _runtime_prompt_mod
from jiuwenswarm.agents.harness.common.rails.model_routing import (
    ModelCapability,
    ModelRoutingRail,
)
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail


class _MockLLM:
    """最小 LLM 替身：真实事件链需要一个能返回文本的模型。"""

    def __init__(self) -> None:
        self.calls = 0

    def _response(self) -> AssistantMessage:
        self.calls += 1
        return AssistantMessage(
            content=f"mock reply {self.calls}",
            usage_metadata=UsageMetadata(model_name="mock-model", finish_reason="stop"),
        )

    async def invoke(self, messages, **kwargs):
        del messages, kwargs
        return self._response()

    async def stream(self, messages, **kwargs):
        del messages, kwargs
        response = self._response()
        yield AssistantMessageChunk(
            content=response.content,
            tool_calls=response.tool_calls,
            usage_metadata=response.usage_metadata,
        )


class _FakeSession:
    def get_session_id(self) -> str:
        return "boundary-session"


class _FakePromptAgent:
    def __init__(self, builder: SystemPromptBuilder) -> None:
        self.system_prompt_builder = builder
        self.prompt_attachment_manager = None


def _build_model(name: str) -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_key="test-key",
            api_base="https://example.invalid/v1",
            verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model=name),
    )


def _write_runtime_state(tmp_path: Path, *, model: str, available_models: list[str]) -> Path:
    """落一份 runtime_state.yaml，复现「档位关键字被当成模型名写进去」的现场。"""
    state_dir = tmp_path / "runtime_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "boundary-session.yaml"
    state_file.write_text(
        yaml.safe_dump({"model": model, "available_models": available_models}, allow_unicode=True),
        encoding="utf-8",
    )
    return state_file


def _capture_runtime_setting(rail: RuntimePromptRail) -> list[str]:
    """抓下 runtime.setting 的内容（不依赖 attachment manager 是否可用）。"""
    captured: list[str] = []
    original = rail._upsert_prompt_attachment

    async def _recording(ctx, *, section, content, kind, priority):
        if section == "runtime.setting":
            captured.append(content)
        await original(ctx, section=section, content=content, kind=kind, priority=priority)

    rail._upsert_prompt_attachment = _recording  # type: ignore[method-assign]
    return captured


def _current_model(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("- 当前模型："):
            return line.split("：", 1)[1].strip()
    raise AssertionError(f"runtime.setting 里没有「当前模型」行:\n{content}")


async def _run_real_chain(selection: str) -> list[str]:
    """真实 DeepAgent 事件链跑一轮，返回 runtime.setting 的内容。"""
    agent = DeepAgent(AgentCard(id="boundary_agent", name="Boundary"))
    agent.configure(
        DeepAgentConfig(
            model=_build_model("mock-model"),
            enable_task_loop=False,
            max_iterations=2,
        )
    )

    routing_rail = ModelRoutingRail(
        capability_table=[
            ModelCapability(
                model_name="deepseek-v4.1-flash",
                model_id="flash-0731",
                model=_build_model("deepseek-v4.1-flash"),
            ),
            ModelCapability(
                model_name="glm-5.2",
                model_id="glm-52",
                model=_build_model("glm-5.2"),
            ),
        ]
    )
    runtime_rail = RuntimePromptRail(language="cn", channel="web")
    runtime_rail.set_session_id("boundary-session")
    captured = _capture_runtime_setting(runtime_rail)

    agent.add_rail(routing_rail)
    agent.add_rail(runtime_rail)

    mock_llm = _MockLLM()
    await Runner.start()
    try:
        with (
            patch("openjiuwen.core.foundation.llm.model.Model.invoke", side_effect=mock_llm.invoke),
            patch("openjiuwen.core.foundation.llm.model.Model.stream", side_effect=mock_llm.stream),
        ):
            await Runner.run_agent(
                agent=agent,
                inputs={
                    "query": "你现在用的是哪个模型",
                    "conversation_id": "boundary-session",
                    # DeepAgent._normalize_inputs 只认嵌套的 run.context；
                    # adapter 正是把 request.params["model_name"] 写在这里。
                    "run": {"kind": "normal", "context": {"extra": {"model_selection": selection}}},
                },
            )
    finally:
        await Runner.stop()
    return captured


@pytest.mark.asyncio
async def test_applied_model_crosses_outer_to_inner_ctx_boundary(tmp_path, monkeypatch):
    """选 fast 档：内层 RuntimePromptRail 必须报真实模型名，而不是档位关键字。

    修复前 `model_routing_applied_model` 写在外层 ctx.extra 上，内层读不到 →
    `routed_model` 恒为空 → 回退到 runtime_state 里的 `fast`，本用例失败。
    """
    state_file = _write_runtime_state(tmp_path, model="fast", available_models=["fast"])
    monkeypatch.setattr(
        _runtime_prompt_mod, "get_runtime_state_path", lambda session_id=None: state_file
    )

    contents = await _run_real_chain("fast")

    assert contents, "runtime.setting 没有被注入，真实事件链没跑到内层 before_model_call"
    assert _current_model(contents[-1]) == "deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_concrete_model_selection_keeps_runtime_state_model(tmp_path, monkeypatch):
    """具体模型名走 skip 分支：不写路由结果，回退链行为与合入前一致。"""
    state_file = _write_runtime_state(tmp_path, model="glm-5.2", available_models=["glm-5.2"])
    monkeypatch.setattr(
        _runtime_prompt_mod, "get_runtime_state_path", lambda session_id=None: state_file
    )

    contents = await _run_real_chain("glm-5.2")

    assert contents
    assert _current_model(contents[-1]) == "glm-5.2"


@pytest.mark.asyncio
async def test_inner_ctx_without_run_context_falls_back(tmp_path, monkeypatch):
    """内层 ctx.extra 里没有 run_context（或它不是 RunContext）时不炸，回退原链。"""
    state_file = _write_runtime_state(tmp_path, model="glm-5.2", available_models=["glm-5.2"])
    monkeypatch.setattr(
        _runtime_prompt_mod, "get_runtime_state_path", lambda session_id=None: state_file
    )

    builder = SystemPromptBuilder(language="cn")
    rail = RuntimePromptRail(language="cn", channel="web")
    rail.init(_FakePromptAgent(builder))
    captured = _capture_runtime_setting(rail)

    for extra in (
        {},
        {"run_context": None},
        {"run_context": SimpleNamespace(extra=None)},
        {"run_context": SimpleNamespace(extra={})},
    ):
        ctx = AgentCallbackContext(
            agent=SimpleNamespace(),
            inputs=None,
            session=_FakeSession(),
            extra=dict(extra),
        )
        await rail.before_model_call(ctx)
        assert _current_model(captured[-1]) == "glm-5.2"
