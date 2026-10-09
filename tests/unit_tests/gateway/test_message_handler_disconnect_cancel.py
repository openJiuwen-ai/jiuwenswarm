# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""断连取消的 stale 键形状兼容回归测试。

GatewayServer 的 ``_client_route_key``（app_gateway.py）V2 带 ``agent_ref``
时返回三元组 ``(channel_id, scoped_id, agent_ref_str)``，不带时返回二元组。
断连清扫把 ``_request_to_client`` / ``_session_to_client`` 的键原样装进
``stale_request_keys`` / ``session_keys`` 传给 MessageHandler——历史上
``_merge_disconnect_session_keys`` 按二元组解包，agent_ref 路由场景 100%
抛 ``ValueError: too many values to unpack``，断连取消（TUI 掉线清理 /
前端停止任务）全部失效，会话成孤儿。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


def _bare_handler(stream_sessions: dict[str, str] | None = None) -> MessageHandler:
    """Bare MessageHandler carrying only the merge path dependencies."""
    handler = object.__new__(MessageHandler)
    handler._stream_sessions = dict(stream_sessions or {})
    return handler


class TestMergeDisconnectSessionKeysShapes:
    def test_three_tuple_stale_request_keys_do_not_crash(self) -> None:
        """回归：V2 三元组 stale_request_keys 必须正常合并，不得抛 ValueError。"""
        handler = _bare_handler(
            stream_sessions={"req_v2": "session_v2", "req_v1": "session_v1"}
        )
        # 混合形状：V2 三元组（带 agent_ref）+ 旧二元组
        stale = [
            ("tui", "req_v2", "agent_ref_x"),
            ("tui", "req_v1"),
        ]

        merged, recovered = handler._merge_disconnect_session_keys(
            [], stale_request_keys=stale
        )

        # 两个 request 都通过 _stream_sessions 反查出 session 并入 merged
        assert ("tui", "session_v2") in merged
        assert ("tui", "session_v1") in merged
        assert ("tui", "session_v2") in recovered
        assert ("tui", "session_v1") in recovered

    def test_three_tuple_session_keys_do_not_crash(self) -> None:
        """回归：``_session_to_client`` 的 ACP 路径同样可能产三元组键。"""
        handler = _bare_handler()
        session_keys = [
            ("acp", "session_a", "agent_ref_y"),  # ACP + agent_ref（app_gateway V2）
            ("tui", "session_b"),                 # 旧二元组
        ]

        merged, _ = handler._merge_disconnect_session_keys(session_keys)

        assert ("acp", "session_a") in merged
        assert ("tui", "session_b") in merged

    def test_agent_ref_element_not_part_of_dedup(self) -> None:
        """第三元（agent_ref）不参与合并语义：同 (channel, session) 不同
        agent_ref 的键只保留一条——合并目标是会话级取消，不是路由级。"""
        handler = _bare_handler()
        session_keys = [
            ("tui", "session_dup", "agent_a"),
            ("tui", "session_dup", "agent_b"),
        ]

        merged, _ = handler._merge_disconnect_session_keys(session_keys)
        assert merged == [("tui", "session_dup")]

    def test_missing_stream_session_entry_is_skipped(self) -> None:
        """request_id 不在 _stream_sessions（流已结束/不存在）→ 跳过，不炸。"""
        handler = _bare_handler(stream_sessions={"req_known": "session_known"})
        stale = [("tui", "req_unknown", "agent_ref_z")]

        merged, recovered = handler._merge_disconnect_session_keys(
            [], stale_request_keys=stale
        )
        assert merged == []
        assert recovered == []

    @pytest.mark.asyncio
    async def test_cancel_entry_point_accepts_three_tuple_keys(self) -> None:
        """入口方法（cancel_agent_sessions_on_disconnect）端到端兼容三元组。

        修复前该方法在 V2 三元组上直接 ValueError；修复后应走到「无可取消
        会话 → True」的正常空返回（_stream_sessions 无该 request 的映射，
        且我们不真的触发 AgentServer cancel——merged 为空即短路）。
        """
        handler = _bare_handler(stream_sessions={"req_v2": "session_v2"})
        # 桩掉真实取消依赖：本测试只验证解包/合并不炸
        handler._disconnect_cancel_tasks = {}
        async def _fake_cancel(channel_id, sid, *, user_id=None):
            return True
        handler._cancel_disconnect_session = _fake_cancel

        result = await handler.cancel_agent_sessions_on_disconnect(
            [("tui", "session_x", "agent_ref_x")],
            stale_request_keys=[("tui", "req_v2", "agent_ref_x")],
        )
        assert result is True
