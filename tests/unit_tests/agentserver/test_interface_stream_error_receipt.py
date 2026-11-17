# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the stream failure terminal receipt.

toc-stable wrapper 终帧曾无条件 {"is_complete": True}——中途 chat.error 之后流仍以
成功终态收尾（gateway_normalize:326/:368 → e2a.complete 假成功）。本文件覆盖修复
四件：空流显式失败（EmptyAgentStreamError）、孤完成帧过滤、中途错误收集 + 上游
complete 帧压制 + 条件终态回执、正常成功路径不受影响。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def _make_request(rid: str = "req-term") -> AgentRequest:
    return AgentRequest(
        request_id=rid, channel_id="desktop", session_id=f"session-{rid}",
        params={"query": "你好", "mode": "agent"}, is_stream=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_empty_stream_fails_with_error_terminal(monkeypatch, as_dict):
    """producer 零事件：空流必须以携带 AGENT_STREAM_EMPTY 的 chat.error 终帧收尾，
    不得再以裸 {"is_complete": True} 假成功。"""

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            if False:  # noqa: SIM108 - 空 async generator 的惯用写法
                yield None

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: {},
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(_make_request())]

    terminals = [chunk for chunk in chunks if chunk.is_complete]
    assert len(terminals) == 1
    payload = terminals[0].payload
    assert isinstance(payload, dict)
    assert payload.get("event_type") == "chat.error"
    assert payload.get("code") == "AGENT_STREAM_EMPTY"
    # 终帧之前应有一条中途 chat.error（is_complete=False）——客户端琥珀横幅/失败收口依赖它
    assert any(
        isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.error"
        for chunk in chunks if not chunk.is_complete
    )


@pytest.mark.asyncio
async def test_orphan_completion_frame_filtered(monkeypatch):
    """producer 只吐一个裸完成帧（无 content/error）：孤帧被过滤 → 空流 → 显式失败，
    不得把孤帧当成功终态透传。"""

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-orphan", channel_id="desktop",
                payload={"is_complete": True}, is_complete=True,
            )

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: {},
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(_make_request("req-orphan"))]

    terminals = [chunk for chunk in chunks if chunk.is_complete]
    assert len(terminals) == 1
    payload = terminals[0].payload
    assert isinstance(payload, dict)
    assert payload.get("event_type") == "chat.error"
    assert payload.get("code") == "AGENT_STREAM_EMPTY"
    # 孤帧本体不得出现在输出里
    assert all(
        not (isinstance(chunk.payload, dict) and chunk.payload == {"is_complete": True})
        for chunk in chunks[:-1]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_midstream_error_terminal_carries_error(monkeypatch, as_dict):
    """中途 chat.error 后：错误被收集、上游 complete 帧被压制，
    唯一终帧携带错误（不再假成功）。"""

    def _events():
        yield {"event_type": "chat.delta", "content": "部分内容"}
        yield {"event_type": "chat.error", "error": "模型调用失败", "code": "MODEL_CALL_FAILED"}
        if not as_dict:
            yield AgentResponseChunk(
                request_id="req-mid", channel_id="desktop",
                payload={"is_complete": True}, is_complete=True,
            )

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            for payload in _events():
                if as_dict:
                    yield payload
                else:
                    yield AgentResponseChunk(
                        request_id="req-mid", channel_id="desktop",
                        payload=payload, is_complete=False,
                    )

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: {},
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(_make_request("req-mid"))]

    terminals = [chunk for chunk in chunks if chunk.is_complete]
    assert len(terminals) == 1
    payload = terminals[0].payload
    assert isinstance(payload, dict)
    assert payload.get("event_type") == "chat.error"
    assert payload.get("error") == "模型调用失败"
    if not as_dict:
        # chunk 形态：上游 complete 帧必须被压制（不得出现裸 {"is_complete": True} 透传）
        assert all(
            not (isinstance(chunk.payload, dict) and chunk.payload == {"is_complete": True})
            for chunk in chunks[:-1]
        )


@pytest.mark.asyncio
async def test_execution_error_synthesizes_error_terminal(monkeypatch):
    """round 级 execution.error：合成 chat.error 且终帧携带错误（原本只剩琥珀横幅 +
    假成功终态）。"""

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-round", channel_id="desktop",
                payload={"event_type": "execution.error", "message": "本轮执行失败"},
                is_complete=False,
            )

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: {},
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(_make_request("req-round"))]

    terminals = [chunk for chunk in chunks if chunk.is_complete]
    assert len(terminals) == 1
    payload = terminals[0].payload
    assert isinstance(payload, dict)
    assert payload.get("event_type") == "chat.error"
    assert payload.get("error") == "本轮执行失败"


@pytest.mark.asyncio
async def test_success_path_terminal_unchanged(monkeypatch):
    """正常成功路径：终帧仍为裸 {"is_complete": True}，行为零变化。"""

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-ok", channel_id="desktop",
                payload={"event_type": "chat.delta", "content": "你好呀"},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-ok", channel_id="desktop",
                payload={"event_type": "chat.final", "content": "你好呀"},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-ok", channel_id="desktop",
                payload={"is_complete": True}, is_complete=True,
            )

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: None)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: {},
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(_make_request("req-ok"))]

    terminals = [chunk for chunk in chunks if chunk.is_complete]
    assert len(terminals) == 2  # 上游 complete 帧 + wrapper 终帧（既有行为）
    assert terminals[-1].payload == {"is_complete": True}
    assert all(
        not (isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.error")
        for chunk in chunks
    )
