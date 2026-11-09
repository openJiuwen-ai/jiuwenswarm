# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

"""流级回归：嵌套流镜像帧不得改写父轮 bookkeeping（PR #9194 检视 CR-008/CR-010）。

helper 级用例（``_a2ui_probe_for_frame`` / ``_a2ui_suppress_should_engage``）无法
覆盖真实帧顺序；本文件用 fake adapter 把「父轮 delta → 子流 delta（含
``<a2ui-json>``）→ 父轮 tool_call → 父轮 final → 晚到子流 final」灌进
``JiuWenSwarm.process_message_stream``，断言收尾 ``assistant_message`` 与落库历史
都不含子流文本。用例覆盖三条守卫：

- CR#2：子流 marker 不得进入父轮 probe（否则下一条主链 delta 被误 suppress）。
- CR#3：子流 delta 不得进入 ``durable_pending_final_chunks``（否则 tool_call
  会把子流文本写成父历史）。
- CR#1：晚到的子流 final 不得顶掉父轮 ``final_answer_content``。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

# 与被测模块 _A2UI_PENDING_RENDER_DELTA 保持一致：suppress 触发时发出的占位帧内容
_PENDING_RENDER_DELTA = "<a2ui-json>\n"

_CHILD_SOURCE_ID = "member_1"


class _ScriptedStreamAdapter:
    """按脚本逐帧 yield 的 fake adapter（payload 可带 stream_source_id 模拟子流镜像）。"""

    def __init__(self, frames: list[dict[str, str]]) -> None:
        self._frames = frames

    async def process_message_stream_impl(self, request: AgentRequest, _inputs: dict) -> object:
        for payload in self._frames:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=dict(payload),
                is_complete=False,
            )


def _build_swarm(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, str]]):
    swarm = interface_module.JiuWenSwarm()
    swarm._adapter = _ScriptedStreamAdapter(frames)
    swarm._sdk_name = "harness"
    monkeypatch.setattr(swarm, "_build_inputs", lambda request: ({}, "local", "生成表单"))
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "is_auto_memory_enabled", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(interface_module, "is_memory_enabled", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    return swarm


def _request() -> AgentRequest:
    return AgentRequest(
        request_id="req-foreign-stream",
        req_method=ReqMethod.CHAT_SEND,
        channel_id="web",
        session_id="session-foreign-stream",
        params={"query": "生成表单", "mode": "agent"},
        is_stream=True,
    )


async def _collect(swarm, request: AgentRequest) -> list[AgentResponseChunk]:
    return [chunk async for chunk in swarm.process_message_stream(request)]


@pytest.mark.asyncio
async def test_foreign_mirror_frames_do_not_pollute_parent_round(monkeypatch):
    """CR#1/#2/#3 流级复现：子流帧全程不触碰父轮 probe、durable 与终稿。"""
    frames = [
        # 父轮第一段正文
        {"event_type": "chat.delta", "content": "父轮开头。"},
        # 子流镜像 delta：带 stream_source_id + a2ui marker（旧实现会污染父轮 probe）
        {
            "event_type": "chat.delta",
            "content": "子流 <a2ui-json>{\"beginRendering\": {}}</a2ui-json>",
            "stream_source_id": _CHILD_SOURCE_ID,
        },
        # 父轮第二段正文：若 probe 被污染，这里会被 split 误判 suppress
        {"event_type": "chat.delta", "content": "父轮继续。"},
        # 父轮工具调用：触发 _persist_pending_final_text（落库父轮已流出的正文）
        {"event_type": "chat.tool_call", "content": ""},
        # 父轮终稿
        {"event_type": "chat.final", "content": "父轮终稿。"},
        # 晚到的子流终稿：不得顶掉父轮答案
        {"event_type": "chat.final", "content": "子流终稿。", "stream_source_id": _CHILD_SOURCE_ID},
    ]
    swarm = _build_swarm(monkeypatch, frames)

    history: list[dict] = []
    monkeypatch.setattr(swarm, "_append_history_record", lambda **kwargs: history.append(kwargs))

    finalized_inputs: list[str] = []

    async def fake_finalize(content, **_kwargs):
        finalized_inputs.append(content)
        return content

    monkeypatch.setattr(
        interface_module, "finalize_assistant_response_if_a2ui", fake_finalize
    )

    try:
        chunks = await _collect(swarm, _request())
    finally:
        await swarm._session_manager.close_all_sessions()

    # 1. 收尾 assistant_message 是父轮终稿（子流文本不参与组装）
    assert finalized_inputs == ["父轮终稿。"]

    # 2. 父轮 tool_call 落库的是主链 delta；父轮 final 单独落库
    persisted_finals = [
        record["content"]
        for record in history
        if record.get("role") == "assistant" and record.get("event_type") == "chat.final"
    ]
    assert persisted_finals == ["父轮开头。父轮继续。", "父轮终稿。"]

    # 3. 任何历史记录都不含子流文本
    assert all("子流" not in str(record.get("content") or "") for record in history)

    # 4. 子流 marker 未触发 suppress：没有 probe 占位帧
    assert all(
        (chunk.payload or {}).get("content") != _PENDING_RENDER_DELTA for chunk in chunks
    )


@pytest.mark.asyncio
async def test_late_foreign_final_keeps_parent_answer_under_suppression(monkeypatch):
    """CR#1 补充：suppress 已激活时，晚到的子流 final 仍维持 withhold，但不改写父轮终稿。"""
    frames = [
        # 主链 marker 触发 suppress
        {"event_type": "chat.delta", "content": "表单如下：<a2ui-json>{\"beginRendering\": {}}</a2ui-json>"},
        # 父轮终稿先到（被扣住，不上 wire）
        {"event_type": "chat.final", "content": "父轮终稿。"},
        # 子流终稿晚到：旧实现会覆盖 final_answer_content
        {"event_type": "chat.final", "content": "子流终稿。", "stream_source_id": _CHILD_SOURCE_ID},
    ]
    swarm = _build_swarm(monkeypatch, frames)

    history: list[dict] = []
    monkeypatch.setattr(swarm, "_append_history_record", lambda **kwargs: history.append(kwargs))

    finalized_inputs: list[str] = []

    async def fake_finalize(content, **_kwargs):
        finalized_inputs.append(content)
        return content

    monkeypatch.setattr(
        interface_module, "finalize_assistant_response_if_a2ui", fake_finalize
    )

    try:
        chunks = await _collect(swarm, _request())
    finally:
        await swarm._session_manager.close_all_sessions()

    # 父轮终稿被扣住后由 finalize 收尾；晚到子流 final 不得顶上
    assert finalized_inputs == ["父轮终稿。"]

    # suppress 激活时仍会发占位帧（行为不变），但历史里不得出现子流文本
    assert any(
        (chunk.payload or {}).get("content") == _PENDING_RENDER_DELTA for chunk in chunks
    )
    assert all("子流" not in str(record.get("content") or "") for record in history)
