# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo thinking=off: Mixin, resolver, and bare-retry helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.thinking_seam import (
    is_skill_turbo_thinking_param_error,
    resolve_skill_turbo_thinking_kwargs,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import DisableThinkingMixin, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.deep_research import (
    PageWorkerNode as P6PageWorkerNode,
    PrepareNode as P6PrepareNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PPTPageGenNode,
    PageWorkerNode as P8PageWorkerNode,
)


class _ThinkingProbeNode(DisableThinkingMixin, PlanNode):
    def __init__(self) -> None:
        super().__init__(plan_name="probe", instruction="probe")

    async def _execute(self, inputs: dict) -> dict:
        return inputs


class TestResolveSkillTurboThinkingKwargs:
    def test_none_is_zero_inject(self):
        assert resolve_skill_turbo_thinking_kwargs(None, MagicMock()) == {}

    def test_off_glm_uses_allowlist(self):
        client = MagicMock()
        client.model_config = MagicMock(model_name="glm-5.2")
        kwargs = resolve_skill_turbo_thinking_kwargs("off", client)
        assert kwargs["extra_body"]["thinking"]["type"] == "disabled"

    def test_off_unsupported_uses_shotgun_fallback(self):
        client = MagicMock()
        client.model_config = MagicMock(model_name="qwen3-plus")
        kwargs = resolve_skill_turbo_thinking_kwargs("off", client)
        assert kwargs == {}

    def test_on_unsupported_empty(self):
        client = MagicMock()
        client.model_config = MagicMock(model_name="qwen3-plus")
        assert resolve_skill_turbo_thinking_kwargs("on", client) == {}

    def test_fail_open_when_adapter_errors(self, monkeypatch: pytest.MonkeyPatch):
        client = MagicMock()
        client.model_config = MagicMock(model_name="glm-5.2")

        def _boom(*args, **kwargs):
            raise RuntimeError("adapter boom")

        monkeypatch.setattr(
            "jiuwenswarm.common.thinking.adapter.adapt_thinking",
            _boom,
        )

        assert resolve_skill_turbo_thinking_kwargs("off", client) == {}


class TestThinkingParamErrorHeuristic:
    def test_type_error(self):
        assert is_skill_turbo_thinking_param_error(TypeError("unexpected keyword")) is True

    def test_message_needles(self):
        assert is_skill_turbo_thinking_param_error(
            RuntimeError("invalid_request: extra_body.thinking not supported")
        ) is True

    def test_unrelated_error(self):
        assert is_skill_turbo_thinking_param_error(RuntimeError("connection reset")) is False


class TestDisableThinkingMixin:
    @pytest.mark.asyncio
    async def test_forces_off_on_call_llm(self):
        node = _ThinkingProbeNode()
        captured: dict = {}

        async def _capture(*args, **kwargs):
            captured.update(kwargs)
            return "ok"

        node._call_llm_callback = _capture
        await node.call_llm("hi", thinking="on")
        assert captured["thinking"] == "off"

    @pytest.mark.asyncio
    async def test_forces_off_on_stream_llm(self):
        node = _ThinkingProbeNode()
        captured: dict = {}

        async def _stream(*args, **kwargs):
            captured.update(kwargs)
            yield "chunk"

        node._stream_llm_callback = _stream
        async for _ in node.stream_llm("hi", thinking="default"):
            pass
        assert captured["thinking"] == "off"


class TestPptNodeMixinMount:
    def test_p6_nodes_inherit_mixin(self):
        assert issubclass(P6PrepareNode, DisableThinkingMixin)
        assert issubclass(P6PageWorkerNode, DisableThinkingMixin)

    def test_p8_worker_and_root_inherit_mixin(self):
        assert issubclass(P8PageWorkerNode, DisableThinkingMixin)
        assert issubclass(PPTPageGenNode, DisableThinkingMixin)
