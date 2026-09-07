"""回归测试：xiaoyi channel 思考与正文分流。

修复前 xiaoyi 出站分类两层错位，端侧思考区显示正文 delta 拼接、与正文区重复，
且模型真实思考过程（CHAT_REASONING）被 SKIPPED 兜底丢弃：

  - 识别层（formatter.py）：``CHAT_DELTA``（正文增量）误入
    ``reasoning_text_events``，``CHAT_REASONING`` 不在任何分类集合。
  - 投递层（_send_text_response）：part kind 由 ``last_chunk`` 推导——正文
    delta 非末块（``last_chunk=False``）→ ``kind="reasoningText"``，正文被误投
    思考区；``CHAT_REASONING`` 无帧。

修复后 ``CHAT_DELTA`` 归入 ``text_events``、``CHAT_REASONING`` 归入
``reasoning_text_events``；``_send_text_response`` 增加显式 ``kind`` 参数解开与
``last_chunk`` 的耦合，``send()`` 对 ``CHAT_REASONING`` 单独分流逐块流式投
``reasoningText``，正文路径显式 ``kind="text"``。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    should_send_as_reasoning_text,
    should_send_as_status_update,
    should_send_as_text,
)


def _config() -> XiaoyiChannelConfig:
    """最小可用配置：enable_streaming=True（bug 触发条件）。"""
    return XiaoyiChannelConfig(
        enabled=True,
        mode="xiaoyi_claw",
        ak="dummy-ak",
        sk="dummy-sk",
        agent_id="dummy-agent",
        ws_url1="wss://dummy/xiaoyi1",
        ws_url2="wss://dummy/xiaoyi2",
        enable_streaming=True,
        uid="dummy-uid",
        api_key="dummy-api-key",
        api_id="dummy-api-id",
        push_id="dummy-push-id",
        push_url="https://dummy/push",
        file_upload_url="https://dummy/upload",
        task_timeout_ms=3600000,
    )


def _make_channel():
    """实例化 XiaoyiChannel，塞 fake ws + patch 底层封包点捕获实际帧。

    返回 (channel, sent_frames)。sent_frames 是 _send_agent_response 收到的
    每个响应字典，复用 demo 的捕获策略。
    """
    channel = XiaoyiChannel(config=_config(), router=None)
    # send() 开头 `if not self._ws_connections: return`，塞一个 truthy fake ws
    channel._ws_connections["ws1"] = object()
    channel._running = True

    sent_frames: list[dict] = []

    async def _capture(session_id, task_id, response, url_key):
        sent_frames.append({
            "session_id": session_id,
            "task_id": task_id,
            "url_key": url_key,
            "response": response,
        })

    # _send_text_response / _send_status_update_with_state 最终都调
    # _send_agent_response；patch 它即截获所有出站帧。
    channel._send_agent_response = _capture  # type: ignore[assignment]
    # _finalize_session 会清理 session 状态，patch 掉避免副作用。
    channel._finalize_session = AsyncMock()  # type: ignore[assignment]
    return channel, sent_frames


def _msg(event_type: EventType, content: str, *, is_complete: bool = False) -> Message:
    return Message(
        id="msg-test-001",
        type="event",
        channel_id="xiaoyi",
        session_id="sess-test-001",
        params={},
        timestamp=0.0,
        ok=True,
        event_type=event_type,
        payload={"content": content, "is_complete": is_complete},
        metadata={
            "xiaoyi_session_id": "sess-test-001",
            "xiaoyi_task_id": "task-test-001",
        },
    )


def _part_kind(frames: list[dict]) -> str | None:
    """取第一帧 artifact parts[0].kind（一个 event 可能多发，取首帧）。"""
    for fr in frames:
        parts = fr["response"].get("result", {}).get("artifact", {}).get("parts", [])
        if parts:
            return parts[0].get("kind")
    return None


class TestClassification:

    @pytest.mark.unit
    def test_chat_delta_classified_as_text(self):
        # 修复前：CHAT_DELTA 被 should_send_as_reasoning_text 判为 True → 误入 reasoning
        assert should_send_as_reasoning_text(EventType.CHAT_DELTA) is False
        assert should_send_as_text(EventType.CHAT_DELTA) is True
        assert should_send_as_status_update(EventType.CHAT_DELTA) is False

    @pytest.mark.unit
    def test_chat_reasoning_classified_as_reasoning(self):
        # 修复前：CHAT_REASONING 三个分类函数全 False → send() SKIPPED 丢弃
        assert should_send_as_reasoning_text(EventType.CHAT_REASONING) is True
        assert should_send_as_text(EventType.CHAT_REASONING) is False
        assert should_send_as_status_update(EventType.CHAT_REASONING) is False

    @pytest.mark.unit
    def test_chat_final_classified_as_text(self):
        assert should_send_as_reasoning_text(EventType.CHAT_FINAL) is False
        assert should_send_as_text(EventType.CHAT_FINAL) is True
        assert should_send_as_status_update(EventType.CHAT_FINAL) is False


class TestDelivery:

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_chat_reasoning_emits_reasoning_text_part(self):
        # 修复前：CHAT_REASONING 无帧（SKIPPED 丢弃，真思考丢失）
        channel, frames = _make_channel()
        await channel.send(_msg(EventType.CHAT_REASONING, "让我分析一下这个问题"))
        assert len(frames) == 1
        assert _part_kind(frames) == "reasoningText"
        result = frames[0]["response"]["result"]
        assert result["append"] is True
        assert result["lastChunk"] is False
        assert result["final"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_chat_delta_emits_text_part(self):
        # 修复前：CHAT_DELTA → kind="reasoningText"（正文被误当思考，与正文区重复）
        channel, frames = _make_channel()
        await channel.send(_msg(EventType.CHAT_DELTA, "第一步正文"))
        assert len(frames) == 1
        assert _part_kind(frames) == "text"
        result = frames[0]["response"]["result"]
        # 正文增量：append=True、非末块
        assert result["append"] is True
        assert result["lastChunk"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_chat_final_emits_text_part_last_chunk(self):
        channel, frames = _make_channel()
        await channel.send(
            _msg(EventType.CHAT_FINAL, "完整正文", is_complete=True)
        )
        assert len(frames) == 1
        assert _part_kind(frames) == "text"
        result = frames[0]["response"]["result"]
        assert result["lastChunk"] is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_chat_subtask_update_emits_reasoning_text_part(self):
        """回归论证：CHAT_SUBTASK_UPDATE 必须投递为 reasoningText，不能是 text。

        SUBTASK_UPDATE 在 formatter 层归 reasoning_text_events
       （``should_send_as_reasoning_text`` 返回 True，``should_send_as_status_update``
        返回 False），既非 status_update 分流（xiaoyi_connect.py:724）也非
        non_user_visible SKIPPED（:822 ``not (True or False)`` = False），故落进
        正文投递路径（:1006 的 ``for url_key, ws`` 循环、:1009 ``_send_text_response``）。

        修复前正文路径 ``_send_text_response`` 不传 kind，由 last_chunk 推导：
        SUBTASK_UPDATE 非末块（``is_complete`` 通常 False → ``last_chunk=False``）
        → 推导出 ``kind="reasoningText"``，作为思考文本投递（**正确**）。
        """
        channel, frames = _make_channel()
        await channel.send(
            _msg(EventType.CHAT_SUBTASK_UPDATE, "子任务1：检索资料")
        )
        assert len(frames) == 1, "SUBTASK_UPDATE 应投递一帧，不应被 SKIPPED 或被 status 分流"
        # 分类前提：formatter 层确把它归 reasoning、且非 status
        assert should_send_as_reasoning_text(EventType.CHAT_SUBTASK_UPDATE) is True
        assert should_send_as_status_update(EventType.CHAT_SUBTASK_UPDATE) is False
        # 投递期望：reasoningText，不是 text
        assert _part_kind(frames) == "reasoningText", (
            "SUBTASK_UPDATE 被正文路径硬编码 kind=\"text\" 强制降为正文——"
            "子任务更新会从思考区消失、错显示在正文区"
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_reasoning_and_text_do_not_duplicate(self):
        """端到端回归：真思考 + 正文 delta + 正文末块依次投递，kind 各归其位。"""
        channel, frames = _make_channel()
        await channel.send(_msg(EventType.CHAT_REASONING, "思考A"))
        reasoning_frames = list(frames)
        frames.clear()
        await channel.send(_msg(EventType.CHAT_DELTA, "正文增量"))
        delta_frames = list(frames)
        frames.clear()
        await channel.send(
            _msg(EventType.CHAT_FINAL, "完整正文", is_complete=True)
        )
        final_frames = list(frames)

        assert _part_kind(reasoning_frames) == "reasoningText"
        assert _part_kind(delta_frames) == "text"
        assert _part_kind(final_frames) == "text"


class TestRobustness:
    """reasoning 分流分支的边界行为与容错。"""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_empty_reasoning_content_dropped(self):
        # 空 content 直接 return，不产生帧
        channel, frames = _make_channel()
        await channel.send(_msg(EventType.CHAT_REASONING, ""))
        assert frames == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_reasoning_single_connection_failure_isolated(self):
        """单连接 _send_text_response 抛异常不应阻塞其余连接、不应逃逸出 send()。

        回归：reasoning 分流分支与正文路径（xiaoyi_connect.py:1002）同级的
        if ws + try/except 容错。修复前该分支裸遍历，一个连接失败会抛出发送循环。
        """
        channel, frames = _make_channel()
        # 两个连接：ws1 正常捕获，ws2 的 _send_text_response 抛异常
        channel._ws_connections.clear()
        channel._ws_connections["ws1"] = object()
        channel._ws_connections["ws2"] = object()

        call_log: list[str] = []

        async def _flaky_send_text_response(*args, **kwargs):
            url_key = args[3] if len(args) > 3 else kwargs.get("url_key", "")
            call_log.append(url_key)
            if url_key == "ws2":
                raise RuntimeError("ws2 broken")

        channel._send_text_response = _flaky_send_text_response  # type: ignore[assignment]

        # 不应抛异常
        await channel.send(_msg(EventType.CHAT_REASONING, "思考"))

        # 两个连接都被尝试（ws2 抛异常后 ws1 仍应执行过）
        # 注：dict 迭代顺序在 3.7+ 保序，ws1 先 ws2 后；flaky 在 ws2，
        # 所以 ws1 必须已成功调用。这里断言两个都尝试了。
        assert set(call_log) == {"ws1", "ws2"}


class TestSendTextResponseKind:
    """kind 参数解开 last_chunk 耦合，旧调用点向后兼容。"""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_explicit_kind_reasoning_text_overrides_last_chunk(self):
        # 显式 kind="reasoningText" + last_chunk=True → 仍 reasoningText
        # （修复前 last_chunk=True 强制 kind="text"，reasoning 末块无法投递）
        channel, frames = _make_channel()
        await channel._send_text_response(
            "s", "t", "思考", "ws1",
            append=True, last_chunk=True, is_final=False, kind="reasoningText",
        )
        assert _part_kind(frames) == "reasoningText"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_explicit_kind_text_with_non_final_delta(self):
        # 显式 kind="text" + last_chunk=False → 仍 text
        # （修复前 last_chunk=False 强制 kind="reasoningText"，正文 delta 误投思考区）
        channel, frames = _make_channel()
        await channel._send_text_response(
            "s", "t", "正文增量", "ws1",
            append=True, last_chunk=False, is_final=False, kind="text",
        )
        assert _part_kind(frames) == "text"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_default_kind_derives_from_last_chunk_backward_compat(self):
        # kind=None（旧调用点）：last_chunk=True → text，向后兼容
        channel, frames = _make_channel()
        await channel._send_text_response(
            "s", "t", "正文", "ws1", last_chunk=True,
        )
        assert _part_kind(frames) == "text"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_default_kind_derives_reasoning_when_not_last_chunk(self):
        # kind=None + last_chunk=False → reasoningText（旧推导规则保持，向后兼容）
        channel, frames = _make_channel()
        await channel._send_text_response(
            "s", "t", "思考", "ws1", last_chunk=False,
        )
        assert _part_kind(frames) == "reasoningText"
