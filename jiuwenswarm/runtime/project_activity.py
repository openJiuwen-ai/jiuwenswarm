# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project conversation counts and execution blockers shared by Runtime users.

Metadata access is deferred until a scan; importing these rules loads no
transport adapter, UI service or agent SDK.
"""

from __future__ import annotations

from typing import Any


def project_conversation_state(
    project_id: str,
    runtime: Any = None,
    *,
    ignore_heartbeats: bool = False,
) -> tuple[int, list[str]]:
    """Count Web conversations and check all channels for running sessions.

    与会话删除/归档共用 Runtime 的执行判定：空闲团队和常驻响应流不算 busy。

    ``ignore_heartbeats`` 把后台 Heartbeat 执行排除在读之外,回答"停掉心跳后
    本会话是否仍在跑"。预检用它,避免把即将被停掉的心跳读成阻塞项。
    """
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    count = 0
    busy: list[str] = []
    for session in collect_all_sessions_metadata():
        session_id = str(session.get("session_id") or "")
        if session.get("cron_id") or session_id.startswith("cron_"):
            continue
        if str(session.get("project_id") or "") != project_id:
            continue
        if session.get("channel_id") == "web":
            count += 1
        if runtime is None:
            continue
        if not session_id or not runtime.is_session_running(
            session_id, ignore_heartbeats=ignore_heartbeats
        ):
            continue
        busy.append(session_id)
    return count, busy


def project_busy_sessions(project_id: str, runtime: Any) -> list[str]:
    """project.remove 预检专用的阻塞会话列表。

    预检只回答"移除会不会被挡",不执行移除,因此不负责停心跳——它按"心跳已被
    停掉"来读,与真正的移除(停心跳后再严格扫描)保持同向:预检放行、移除停掉
    心跳后成功;心跳真的停不掉时,由移除那一次扫描报 SESSION_BUSY。
    """
    return project_conversation_state(project_id, runtime, ignore_heartbeats=True)[1]
