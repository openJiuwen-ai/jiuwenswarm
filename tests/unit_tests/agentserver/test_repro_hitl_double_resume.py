# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ask_user HITL 挂起期间的 suppress 清除行为：ask_user 之后第一个非噪声 chunk
只清 suppress（恢复转发），hitl_pending_stream 保持 True，使收尾帧仍为
chat.invocation_paused（气泡保持开启）。
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _end_frame_kind(hitl_pending_stream: bool) -> str:
    """镜像 process_message_stream_impl 循环后的收尾帧决策（interface_deep.py:17338）。

    ``hitl_pending_stream`` 仍为 True → ``chat.invocation_paused``（气泡保持开启）；
    否则 → 普通完成帧（气泡关闭，前端据此对父级中断再发一次 resume）。
    """
    if hitl_pending_stream:
        return "chat.invocation_paused"
    return "plain_complete"


def _apply_suppress_clear(
    chunk: SimpleNamespace,
    *,
    suppress_stream_after_hitl: bool,
    hitl_pending_stream: bool,
) -> tuple[bool, bool]:
    """镜像 interface_deep.py 的 suppress 清除分支。

    ask_user 之后的 chunk：噪声（llm_usage / context.usage）继续抑制；其余任意
    chunk 只清 ``suppress_stream_after_hitl``，保留 ``hitl_pending_stream``。
    返回更新后的 (suppress, hitl_pending)。
    """
    if suppress_stream_after_hitl:
        if JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(chunk):
            return suppress_stream_after_hitl, hitl_pending_stream
        return False, hitl_pending_stream
    return suppress_stream_after_hitl, hitl_pending_stream


def test_ask_user_hitl_kept_paused_when_only_noise_follows() -> None:
    """ask_user 后只跟噪声 chunk：气泡应保持 awaiting_user_input（基线正确行为）。"""
    suppress = True
    hitl_pending = True  # ask_user payload 已置位
    for chunk in (
        SimpleNamespace(type="llm_usage", payload={}),
        SimpleNamespace(type="context.usage", payload={}),
    ):
        suppress, hitl_pending = _apply_suppress_clear(
            chunk, suppress_stream_after_hitl=suppress, hitl_pending_stream=hitl_pending
        )
    assert _end_frame_kind(hitl_pending) == "chat.invocation_paused"


def test_second_ask_user_does_not_close_bubble() -> None:
    """ask_user 后第二张 ask_user 卡片：suppress 清除但 hitl_pending 保持 True，
    收尾帧仍为 chat.invocation_paused。
    """
    suppress = True
    hitl_pending = True
    chunks = [
        SimpleNamespace(type="llm_usage", payload={}),
        SimpleNamespace(
            type="chat.ask_user_question",
            payload={"event_type": "chat.ask_user_question", "request_id": "req2"},
        ),
    ]
    for chunk in chunks:
        suppress, hitl_pending = _apply_suppress_clear(
            chunk, suppress_stream_after_hitl=suppress, hitl_pending_stream=hitl_pending
        )
    assert hitl_pending is True
    assert suppress is False
    assert _end_frame_kind(hitl_pending) == "chat.invocation_paused"


def test_replayed_stage_delta_does_not_close_bubble() -> None:
    """ask_user 后重放 stage delta：只清 suppress，hitl_pending 保持 True。"""
    suppress = True
    hitl_pending = True
    chunk = SimpleNamespace(
        type="llm_output",
        payload={"content": "完成执行 Stage 1: 流水线初始化（1/14）"},
    )
    suppress, hitl_pending = _apply_suppress_clear(
        chunk, suppress_stream_after_hitl=suppress, hitl_pending_stream=hitl_pending
    )
    assert hitl_pending is True
    assert suppress is False
    assert _end_frame_kind(hitl_pending) == "chat.invocation_paused"


def test_noise_chunk_contract_unchanged() -> None:
    """噪声判定契约：llm_usage / context.usage 为噪声，其余非噪声。"""
    assert JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
        SimpleNamespace(type="llm_usage", payload={})
    )
    assert JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
        SimpleNamespace(type="context.usage", payload={})
    )
    # 非噪声不应被判为噪声
    assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
        SimpleNamespace(type="chat.ask_user_question", payload={})
    )
    assert not JiuWenSwarmDeepAdapter._is_hitl_suppress_noise_chunk(
        SimpleNamespace(type="llm_output", payload={})
    )
