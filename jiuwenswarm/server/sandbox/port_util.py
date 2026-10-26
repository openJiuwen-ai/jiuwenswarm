# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""internal 模式下 jiuwenbox 监听端口的解析与分配.

照搬自 jiuwenswarm PR #4088 的 ``agentserver/sandbox/port_util.py``, 让端口工具
从 ``AgentWebSocketServer`` 内联方法独立出来, Linux/Windows 共用. 行为与原内联
实现完全一致 (``_parse_sandbox_host_port`` / ``_is_tcp_port_bindable`` /
``_pick_free_tcp_port`` / ``_allocate_internal_jiuwenbox_port``).
"""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

logger = logging.getLogger(__name__)


def parse_sandbox_host_port(url: str) -> tuple[str, int]:
    """从 sandbox url 解析 host:port; 默认 127.0.0.1:8321."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8321
    except Exception:  # noqa: BLE001
        host, port = "127.0.0.1", 8321
    return host, int(port)


def is_tcp_port_bindable(host: str, port: int) -> bool:
    """``True`` 表示当前能在 ``host:port`` 上 ``bind`` 成功 (即没有被占用)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True
    finally:
        sock.close()


def pick_free_tcp_port(host: str) -> int:
    """让内核挑一个空闲端口 (``bind`` 到 0); 仅用于绑定测试, 不会真正监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def allocate_internal_jiuwenbox_port(
    host: str,
    preferred_port: int,
    *,
    runner: JiuwenBoxRunner | None = None,
) -> int:
    """internal 模式下确定 jiuwenbox 实际监听端口。

    - 若本 runner 已经在 ``host:preferred_port`` 上拥有一个仍在跑的 jiuwenbox,
      直接复用 (避免重复 spawn);
    - 否则若 ``preferred_port`` 当前无人占用, 用之;
    - 再否则让内核挑一个空闲端口返回。
    """
    box = runner if runner is not None else JiuwenBoxRunner.instance()
    if box.is_owned_listener(host, preferred_port):
        return preferred_port
    if is_tcp_port_bindable(host, preferred_port):
        return preferred_port
    new_port = pick_free_tcp_port(host)
    logger.warning(
        "[sandbox.port] preferred port %s:%d is busy; "
        "allocating fresh port %d for new jiuwenbox instance",
        host,
        preferred_port,
        new_port,
    )
    return new_port
