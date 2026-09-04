"""ws 保活循环回归测试：任务完结后不得再发空 working 保活帧。

背景：_ws_keepalive_loop 周期向 _active_push_sessions 中的 agent_id 发送
kind=status-update（text=""，state=working）空帧保活。修复前存在两个缺陷：
1. _finalize_session 不清理 _active_push_sessions，任务完结后条目残留；
2. 保活循环不检查会话是否活跃，且发送成功即刷新 last_seen 自续期——
   死任务的空 working 帧无限期发送，把手机端任务状态反复拉回 working、
   又阻止其空闲超时收口，导致最终响应一直"收不掉"。
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


@pytest.mark.asyncio
async def test_finalize_session_removes_ws_keepalive_entries() -> None:
    """任务收尾必须移除保活映射中该 (sid, tid) 的登记。"""
    channel, _ = _build_channel()
    channel._mark_session_active("sid-1", "tid-1")
    channel._active_push_sessions["aid-1"] = ("sid-1", "tid-1", "push-1", time.time())
    # 其他会话的条目不能被误删
    channel._active_push_sessions["aid-2"] = ("sid-2", "tid-2", "push-2", time.time())

    await channel._finalize_session("sid-1", "tid-1")

    assert "aid-1" not in channel._active_push_sessions
    assert "aid-2" in channel._active_push_sessions


@pytest.mark.asyncio
async def test_ws_keepalive_loop_skips_finished_tasks() -> None:
    """完结任务的条目：不发保活帧、直接移除；活跃任务：正常保活并刷新 last_seen。"""
    channel, sent = _build_channel()
    channel._running = True
    channel._team_ws_keepalive_interval = 0.01
    channel._team_ws_alive_window = 60.0

    old_ts = time.time() - 1
    # 死任务：从未标记活跃（模拟任务已 finalize 后的残留条目）
    channel._active_push_sessions["aid-dead"] = ("sid-dead", "tid-dead", "", old_ts)
    # 活跃任务
    channel._mark_session_active("sid-live", "tid-live")
    channel._active_push_sessions["aid-live"] = ("sid-live", "tid-live", "", old_ts)

    loop = asyncio.get_event_loop().create_task(channel._ws_keepalive_loop())
    await asyncio.sleep(0.08)
    loop.cancel()
    try:
        await loop
    except asyncio.CancelledError:
        pass

    # 死任务条目被移除，活跃任务条目保留
    assert "aid-dead" not in channel._active_push_sessions
    assert "aid-live" in channel._active_push_sessions
    # 活跃条目 last_seen 被刷新（不再是最初的 old_ts）
    assert channel._active_push_sessions["aid-live"][3] > old_ts

    # 所有发出的帧都只属于活跃任务：无 tid-dead 的空 working 帧
    task_ids = {_result_of(w)["taskId"] for w in sent}
    assert "tid-dead" not in task_ids
    live_frames = [w for w in sent if _result_of(w)["taskId"] == "tid-live"]
    assert live_frames, "活跃任务应收到保活帧"
    for wrapper in live_frames:
        result = _result_of(wrapper)
        assert result["kind"] == "status-update"
        assert result["status"]["state"] == "working"
        assert result["final"] is False


@pytest.mark.asyncio
async def test_ws_keepalive_loop_removal_prevents_endless_frames() -> None:
    """死任务条目出窗后不再产生任何帧：多轮保活周期内零发送。"""
    channel, sent = _build_channel()
    channel._running = True
    channel._team_ws_keepalive_interval = 0.01
    channel._team_ws_alive_window = 60.0

    channel._active_push_sessions["aid-dead"] = ("sid-dead", "tid-dead", "", time.time())
    # 不标记活跃 → 视为已完结

    loop = asyncio.get_event_loop().create_task(channel._ws_keepalive_loop())
    await asyncio.sleep(0.1)
    loop.cancel()
    try:
        await loop
    except asyncio.CancelledError:
        pass

    assert "aid-dead" not in channel._active_push_sessions
    assert sent == [], "完结任务在多个保活周期内不应发出任何帧"
