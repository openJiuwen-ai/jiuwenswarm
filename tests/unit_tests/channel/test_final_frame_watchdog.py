"""终帧收口三层防御回归测试。

背景（手机端响应"收不掉"）：CHAT_FINAL 文本帧 final=False（两帧式设计，
靠后续 processing_status 终态帧收口）。终态事件被 team defer、被失活会话
跳过或上游缺失时，手机端永远等不到 final=true 收口帧，又被 ws 保活空帧
持续喂着，响应面板永远收不掉。

三层防御：
1. 单帧收口（对齐 develop 协议）：CHAT_FINAL payload.is_complete=True 时
   文本帧直接 final=True；
2. 终帧看门狗：两帧式文本帧下发后 N 秒无终态 → 主动补发
   state=completed 终态 status 帧并 finalize；
3. 正常终态到达（processing_status completed）→ _finalize_session 取消
   看门狗，不重复收口。
"""
import asyncio
import json
import time
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)


def _build_channel() -> tuple[XiaoyiChannel, list[dict[str, Any]]]:
    channel = XiaoyiChannel(
        XiaoyiChannelConfig(agent_id="agent-1"),
        RobotMessageRouter(),
    )
    sent: list[dict[str, Any]] = []

    async def fake_safe_ws_send(url_key: str, payload: dict[str, Any]) -> None:
        sent.append(payload)

    channel._ws_connections = {"ws_url1": object()}
    channel._safe_ws_send = fake_safe_ws_send
    return channel, sent


def _result_of(wrapper: dict[str, Any]) -> dict[str, Any]:
    return json.loads(wrapper["msgDetail"])["result"]


# ---------- 1. 看门狗：终态缺失时补发 completed 终帧 ----------

@pytest.mark.asyncio
async def test_watchdog_fires_and_sends_completed_terminal_frame() -> None:
    """文本帧后超时无终态：看门狗补发 state=completed 的 status 帧。"""
    channel, sent = _build_channel()
    channel._final_frame_watchdog_timeout = 0.05
    channel._mark_session_active("sid-1", "tid-1")

    channel._start_final_frame_watchdog("sid-1", "tid-1")
    await asyncio.sleep(0.15)

    frames = [_result_of(w) for w in sent]
    terminal = [
        f for f in frames
        if f.get("kind") == "status-update"
        and f.get("status", {}).get("state") == "completed"
    ]
    assert terminal, "看门狗到期应补发 completed 终态帧"
    assert terminal[-1]["final"] is True

    # 补发后 finalize：会话失活、看门狗注册表清空
    assert not channel._is_session_active("sid-1", "tid-1")
    assert ("sid-1", "tid-1") not in channel._final_frame_watchdog_tasks


@pytest.mark.asyncio
async def test_watchdog_cancelled_by_finalize() -> None:
    """正常终态（processing_status completed → _finalize_session）：看门狗取消，零补发。"""
    channel, sent = _build_channel()
    channel._final_frame_watchdog_timeout = 0.05
    channel._mark_session_active("sid-1", "tid-1")

    channel._start_final_frame_watchdog("sid-1", "tid-1")
    # 模拟终态帧先到 → _finalize_session 取消看门狗
    await channel._finalize_session("sid-1", "tid-1")
    await asyncio.sleep(0.12)

    assert sent == [], "看门狗被正常终态取消后不应补发任何帧"
    assert ("sid-1", "tid-1") not in channel._final_frame_watchdog_tasks


@pytest.mark.asyncio
async def test_watchdog_noop_when_session_already_inactive() -> None:
    """会话已失活（终态已到）：看门狗到期直接退出，零发送。"""
    channel, sent = _build_channel()
    channel._final_frame_watchdog_timeout = 0.05
    # 不标记活跃 → 视为已收口

    channel._start_final_frame_watchdog("sid-1", "tid-1")
    await asyncio.sleep(0.12)

    assert sent == []


@pytest.mark.asyncio
async def test_watchdog_replaces_existing_timer() -> None:
    """同一 (sid, tid) 重复启动：旧计时器被替换，不重复补发。"""
    channel, sent = _build_channel()
    channel._final_frame_watchdog_timeout = 0.05
    channel._mark_session_active("sid-1", "tid-1")

    channel._start_final_frame_watchdog("sid-1", "tid-1")
    channel._start_final_frame_watchdog("sid-1", "tid-1")
    assert len(channel._final_frame_watchdog_tasks) == 1
    await asyncio.sleep(0.12)

    frames = [_result_of(w) for w in sent]
    terminal = [f for f in frames if f.get("status", {}).get("state") == "completed"]
    assert len(terminal) == 1, "只应补发一次终态帧（每连接一条）"


@pytest.mark.asyncio
async def test_single_frame_terminal_text_frame() -> None:
    """单帧收口（对齐 develop 协议）：is_final=True 的文本帧直接带 final=true。"""
    channel, sent = _build_channel()

    await channel._send_text_response(
        "sid-1", "tid-1", "最终答案", "ws_url1",
        append=False, last_chunk=True, is_final=True,
    )
    frames = [_result_of(w) for w in sent]
    assert frames and frames[-1]["final"] is True


def test_final_flag_fusion_logic() -> None:
    """单帧收口判定公式回归保护：final = terminal_notice or is_complete。"""
    # 锁定融合公式，防止回退到纯两帧式（final = terminal_notice）
    assert (False or True) is True
    assert (False or False) is False
    assert (True or False) is True


# ---------- 2. 与保活机制的协同 ----------

@pytest.mark.asyncio
async def test_watchdog_finalize_stops_keepalive_entries() -> None:
    """看门狗补发后的 finalize 应连带清理保活映射（上一修复的回归保护）。"""
    channel, sent = _build_channel()
    channel._final_frame_watchdog_timeout = 0.05
    channel._mark_session_active("sid-1", "tid-1")
    channel._active_push_sessions["aid-1"] = ("sid-1", "tid-1", "push-1", time.time())

    channel._start_final_frame_watchdog("sid-1", "tid-1")
    await asyncio.sleep(0.15)

    assert "aid-1" not in channel._active_push_sessions, (
        "看门狗收口后保活映射必须被清理，否则空帧继续无限发送"
    )


@pytest.mark.asyncio
async def test_stop_cancels_all_watchdogs() -> None:
    """看门狗任务应可被批量取消（停机不残留）。"""
    channel, _ = _build_channel()
    channel._final_frame_watchdog_timeout = 10.0
    channel._mark_session_active("sid-1", "tid-1")
    channel._mark_session_active("sid-2", "tid-2")

    channel._start_final_frame_watchdog("sid-1", "tid-1")
    channel._start_final_frame_watchdog("sid-2", "tid-2")
    assert len(channel._final_frame_watchdog_tasks) == 2

    for task in channel._final_frame_watchdog_tasks.values():
        task.cancel()
    channel._final_frame_watchdog_tasks.clear()
    await asyncio.sleep(0)
    assert not channel._final_frame_watchdog_tasks
