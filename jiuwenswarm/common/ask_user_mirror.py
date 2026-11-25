# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""桌面发起、手机作答的 AskUser 跨进程桥接（共享文件）。

背景：``app.py`` 用两个子进程分别跑 ``app_agentserver`` 与 ``app_gateway``。
桌面端在「手机发起过」的话题里续发消息时，agent 产出的 ``chat.ask_user_question``
由 ``agent_ws_server`` 直发桌面连接（不经网关）；用户在手机点选后的结构化应答
回包却进网关 xiaoyi 通道。若 xiaoyi 侧没有对应待答复 pending，回包被判「应答无
待答复审批」丢弃 —— 表现为电脑和手机两端卡片都不收口、任务不续跑。

因此桥接必须跨进程。两进程共享同一用户工作目录，这里用一个小 JSON 文件做传递：

- ``agent_ws_server``（agent_server 进程）在出站 ``chat.ask_user_question`` 且为
  非 xiaoyi 请求、请求 metadata 又带小艺会话时，调用 :func:`write_ask_user_mirror`
  把该事件载荷按 ``conversation_id`` 写入共享文件；
- 网关 xiaoyi 通道（gateway 进程）收到手机作答、本通道无 pending 时，调用
  :func:`pop_ask_user_mirror` 取回并据以登记 pending。

只登记、不发卡；文件带 TTL，best-effort，异常一律吞掉，绝不影响主链路。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# 镜像记录有效期：超过即视为过期（避免陈旧记录误命中后续回包）
_MIRROR_TTL_S = 10 * 60
_RELATIVE_PATH = Path(".runtime") / "ask_user_mirror.json"


def _mirror_path() -> Path:
    """共享镜像文件路径（两进程同一用户工作目录下的隐藏运行时文件）。"""
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return Path(get_user_workspace_dir()) / _RELATIVE_PATH


def _load() -> dict[str, Any]:
    """读取镜像文件；缺失/损坏一律返回空字典。"""
    try:
        path = _mirror_path()
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    """原子写入镜像文件（临时文件 + replace），失败静默。"""
    try:
        path = _mirror_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".aum_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def write_ask_user_mirror(conv: str, payload: dict[str, Any]) -> None:
    """登记一条「桌面发起」的 AskUser 载荷（按 conversation_id 覆盖写）。

    Args:
        conv: 小艺逻辑会话 id（``xiaoyi_conversation_id``）。
        payload: ``chat.ask_user_question`` 事件载荷（含 request_id/questions）。

    Returns:
        None
    """
    if not conv or not isinstance(payload, dict):
        return
    now = time.time()
    data = _load()
    for key in list(data.keys()):
        try:
            if now - float(data[key].get("updated_at", 0)) > _MIRROR_TTL_S:
                data.pop(key, None)
        except Exception:
            data.pop(key, None)
    data[conv] = {"payload": payload, "updated_at": now}
    _save(data)


def pop_ask_user_mirror(conv: str) -> dict[str, Any] | None:
    """取走并移除某会话的镜像载荷（过期或不存在返回 None）。

    Args:
        conv: 小艺逻辑会话 id（``xiaoyi_conversation_id``）。

    Returns:
        未过期的 ``chat.ask_user_question`` 载荷；否则 None。
    """
    if not conv:
        return None
    data = _load()
    record = data.pop(conv, None)
    if record is None:
        return None
    # 命中即移除，避免同一回包重复 resume（并发写冲突概率极低，TTL 兜底）
    _save(data)
    try:
        if time.time() - float(record.get("updated_at", 0)) > _MIRROR_TTL_S:
            return None
    except Exception:
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None
