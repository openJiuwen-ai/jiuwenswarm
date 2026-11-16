# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""手机连发新消息时上一条平台任务的收口（取消即消费语义）单测。

覆盖：
  - 同会话快速连发：后一条消息才是真实意图，上一条仍在跑的平台任务按
    「已消费」收口（登记 _canceled_platform_tasks、撤销活跃/保活、补发
    final completed 终态帧 + artifact 收口正文），新消息照常路由
  - 上一条已正常完成：不误发收口
  - team 会话：维持既有 retire（completed）语义，不走取消
  - 收口后旧任务的迟到出站帧按 _should_drop_canceled_output 丢弃
"""

from __future__ import annotations

import json
import time

import pytest

import jiuwenswarm.common.config as cfgmod
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)


# ------------------------------------------------------------------ fixtures


class _FakeWs:
    def __init__(self, sink: list):
        self.sink = sink

    async def send(self, data):
        self.sink.append(json.loads(data))


def _make_channel(captured: list, sent: list) -> XiaoyiChannel:
    """绕过 __init__ 的最小化渠道实例（仅填充被测路径用到的字段）。"""
    ch = XiaoyiChannel.__new__(XiaoyiChannel)
    ch.config = XiaoyiChannelConfig(
        enabled=True, agent_id="ag1", ak="ak", sk="sk", channel_id="xiaoyi"
    )
    ch._on_message_cb = lambda m: (captured.append(m) or True)
    ch.bus = None
    ch._session_task_map = {}
    ch._session_active = set()
    ch._active_push_sessions = {}
    ch._task_timeout_tasks = {}
    ch._session_timeout_tasks = {}
    ch._session_heartbeat_tasks = {}
    ch._sessions_waiting_for_push = {}
    ch._pending_approvals = {}
    ch._ws_connections = {"k": _FakeWs(sent)}
    ch._accumulated_texts = {}
    ch._send_locks = {}
    ch._reload_permissions_cb = None
    ch._active_tasks = set()
    ch._latest_platform_tasks = {}
    ch._team_sessions = set()
    ch._team_tasks = set()
    ch._team_last_leader_finals = {}
    ch._sessions_marked_for_cleanup = {}
    ch._stream_text_buffers = {}
    ch._task_last_activity = {}
    ch._ws_flush_buffers = {}
    ch._ws_flush_tasks = {}
    ch._push_merge_buffers = {}
    ch._push_flush_tasks = {}
    ch._data_event_handlers = {}
    ch._gui_agent_handlers = []
    ch._device_command_locks = {}
    ch._canceled_platform_tasks = {}
    ch._round_error_texts = {}
    ch._session_app_vers = {}
    ch._answer_receipts = {}
    ch._pending_resume_seeds = {}
    ch._streamed_chain_texts = {}
    ch._text_stream_prefix = {}
    ch._text_stream_pending = {}
    ch._status_update_interval = 3600
    ch.push_id = ""
    return ch


def _build_stream_msg(text, conv="conv-1", top="top-1", task="task-1"):
    return {
        "conversationId": conv,
        "deviceId": "dev",
        "id": task,
        "jsonrpc": "2.0",
        "method": "message/stream",
        "params": {
            "id": task,
            "message": {
                "kind": "message",
                "messageId": task,
                "parts": [{"kind": "text", "text": text}],
                "role": "user",
            },
            "sessionId": conv,
        },
        "sessionId": top,
        "agentId": "agent0c18",
        "agentMode": "OpenClawToC",
        "userId": "u1",
    }


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "permissions:\n"
        "  enabled: false\n"
        "  permission_mode: normal\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "CONFIG_YAML_PATH", path)
    return path


def _cleanup_tasks(ch: XiaoyiChannel) -> None:
    for t in list(ch._task_timeout_tasks.values()) + list(ch._session_timeout_tasks.values()):
        t.cancel()


def _terminal_status_frames(sent: list) -> list[dict]:
    """sink 收到的是 agent_response 包装帧：msgDetail 为 JSON 字符串。"""
    frames = []
    for wrapper in sent:
        if not isinstance(wrapper, dict) or wrapper.get("msgType") != "agent_response":
            continue
        try:
            detail = json.loads(wrapper.get("msgDetail") or "")
        except (TypeError, json.JSONDecodeError):
            continue
        result = detail.get("result") if isinstance(detail, dict) else None
        if isinstance(result, dict) and result.get("kind") == "status-update":
            frames.append(result)
    return frames


# ------------------------------------------------------------------ 用例


@pytest.mark.asyncio
async def test_rapid_second_message_cancels_active_previous_task(cfg_file):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        await ch._handle_message_stream(_build_stream_msg("帮我写", task="task-1"))
        assert ("conv-1", "task-1") in ch._active_tasks

        await ch._handle_message_stream(
            _build_stream_msg("帮我写一份周报，主题是A", task="task-2")
        )

        # 上一条按取消收口，新一条照常登记
        assert ("conv-1", "task-1") in ch._canceled_platform_tasks
        assert ("conv-1", "task-1") not in ch._active_tasks
        assert ("conv-1", "task-2") in ch._active_tasks
        assert ch._latest_platform_tasks["conv-1"] == "task-2"

        # 两条 chat.send 都已路由（AgentServer 侧由流式顶替机制取消旧运行）
        assert [m.req_method for m in captured] == [
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_SEND,
        ]
        # query 会被渠道注入 <claw_time> 时间指令尾段（现状行为），只断言用户原文前缀
        assert captured[0].params["query"].startswith("帮我写")
        assert captured[1].params["query"].startswith("帮我写一份周报，主题是A")

        # 旧任务收到「已消费」收口：completed 终态帧（手机气泡/桌面镜像收口）。
        # 不再发 canceled——那会被手机/云端视为「未完成」自动重发旧消息，与新
        # 轮次互相残杀（2026-11-16 活锁案例）；收口正文「已切换到新任务」随
        # artifact-update 下发（status text 不进镜像正文）。
        consumed = [
            r
            for r in _terminal_status_frames(sent)
            if r.get("taskId") == "task-1"
            and r.get("final") is True
            and r.get("status", {}).get("state") == "completed"
        ]
        assert len(consumed) == 1

        # 旧任务的保活/超时定时器已撤销，只剩新任务自己的
        assert set(ch._task_timeout_tasks) == {("conv-1", "task-2")}
        assert set(ch._session_timeout_tasks) == {("conv-1", "task-2")}
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_finished_previous_task_is_not_canceled(cfg_file):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        await ch._handle_message_stream(_build_stream_msg("第一句", task="task-1"))
        # 上一条已正常完成（终帧路径会移除活跃标记）
        ch._mark_session_completed("conv-1", "task-1")

        await ch._handle_message_stream(_build_stream_msg("第二句", task="task-2"))

        assert ("conv-1", "task-1") not in ch._canceled_platform_tasks
        assert not [
            r
            for r in _terminal_status_frames(sent)
            if r.get("taskId") == "task-1"
        ]
        assert [m.req_method for m in captured] == [
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_SEND,
        ]
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_team_session_keeps_retire_semantics(cfg_file):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        await ch._handle_message_stream(_build_stream_msg("团队指令一", task="task-1"))
        ch._team_sessions.add("conv-1")
        ch._team_tasks.add(("conv-1", "task-1"))

        await ch._handle_message_stream(_build_stream_msg("团队指令二", task="task-2"))

        # team 会话维持既有 retire：completed 终态、不登记取消
        assert ("conv-1", "task-1") not in ch._canceled_platform_tasks
        retired = [
            r
            for r in _terminal_status_frames(sent)
            if r.get("taskId") == "task-1"
            and r.get("status", {}).get("state") == "completed"
        ]
        assert len(retired) == 1
    finally:
        _cleanup_tasks(ch)


@pytest.mark.asyncio
async def test_superseded_task_late_frames_are_dropped(cfg_file):
    captured, sent = [], []
    ch = _make_channel(captured, sent)
    try:
        await ch._handle_message_stream(_build_stream_msg("旧指令", task="task-1"))
        await ch._handle_message_stream(_build_stream_msg("新指令", task="task-2"))

        # 旧任务取消后才产生的出站帧（迟到正文/终帧）必须丢弃
        late = Message(
            id="m1",
            type="resp",
            channel_id="xiaoyi",
            session_id="conv-1",
            params={},
            timestamp=time.time(),
            ok=True,
            metadata={"xiaoyi_session_id": "conv-1", "xiaoyi_task_id": "task-1"},
        )
        assert ch._should_drop_canceled_output(late, "conv-1", "task-1", "task-1") is True
        # 新任务不受影响
        fresh = Message(
            id="m2",
            type="resp",
            channel_id="xiaoyi",
            session_id="conv-1",
            params={},
            timestamp=time.time(),
            ok=True,
            metadata={"xiaoyi_session_id": "conv-1", "xiaoyi_task_id": "task-2"},
        )
        assert ch._should_drop_canceled_output(fresh, "conv-1", "task-2", "task-2") is False
    finally:
        _cleanup_tasks(ch)
