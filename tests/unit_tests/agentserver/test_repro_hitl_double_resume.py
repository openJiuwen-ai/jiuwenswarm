# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ask_user HITL 挂起期间的 suppress 清除行为：ask_user 之后首个非噪声 chunk
只清 suppress（恢复转发），hitl_pending_stream 保持 True，使收尾帧仍为
chat.invocation_paused（气泡保持开启）。直接测 adapter 静态方法，防护回退。
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _apply(chunk, suppress=True):
    return JiuWenSwarmDeepAdapter._apply_hitl_suppress_clear(
        chunk,
        suppress_stream_after_hitl=suppress,
        request_id="req-1",
    )


def test_noise_chunks_keep_suppression() -> None:
    """噪声 chunk（llm_usage / context.usage）：维持抑制并跳过。"""
    for chunk_type in ("llm_usage", "context.usage"):
        suppress, skip = _apply(SimpleNamespace(type=chunk_type, payload={}))
        assert suppress is True
        assert skip is True


def test_first_non_noise_chunk_clears_suppress_keeps_hitl_pending() -> None:
    """首个非噪声 chunk：清 suppress（恢复转发）。

    hitl_pending_stream 由调用方持有、本方法不触碰——它驱动
    chat.invocation_paused 收尾帧；曾在此分支被一并清成 False 导致气泡
    提前关闭、前端二次 resume。
    """
    for chunk_type in ("llm_output", "chat.ask_user_question", "__interaction__"):
        suppress, skip = _apply(SimpleNamespace(type=chunk_type, payload={}))
        assert suppress is False, f"{chunk_type} 应清除 suppress"
        assert skip is False, f"{chunk_type} 清除后应继续处理（不跳过）"


def test_suppress_off_passes_through_untouched() -> None:
    """suppress 已清除：chunk 原样放行（不跳过、状态不变）。"""
    chunk = SimpleNamespace(type="llm_usage", payload={})
    suppress, skip = _apply(chunk, suppress=False)
    assert suppress is False
    assert skip is False
