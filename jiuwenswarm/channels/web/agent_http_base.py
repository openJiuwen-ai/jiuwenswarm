# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Web 进程侧的 AgentServer HTTP bridge 基址解析（gateway 拆分本地副本）。

原实现位于 ``jiuwenswarm.gateway.routing.agent_http_bridge``，为 Web 静态服务
（``app_web``）提供统一的下载/上传基址推导。gateway 拆分后 Web 进程不再
import gateway 模块，此处保留纯静态解析逻辑的本地副本（允许临时重复）：
- 环境变量优先级、端口推导规则与 gateway 侧保持一致；
- ``set_agent_http_base_resolver`` 扩展点按进程独立注册（Web 进程经
  ``app_web`` re-export，gateway 进程走 gateway 仓自己的副本）。
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def resolve_agent_host_port() -> tuple[str, str, str]:
    """返回 (http_scheme, host, ws_port)，与 AgentServer WS 地址解析一致。

    AgentServer 地址优先级为 ``AGENT_SERVER_URL`` 优先于
    ``AGENT_SERVER_HOST`` + ``AGENT_SERVER_PORT``。下载端点由 AgentServer 在
    WS 端口上拦截（``agent_ws_server._process_request``），因此下载 HTTP 基址
    应与 WS 地址同 host/port，仅把 ``ws``/``wss`` 换成 ``http``/``https``。
    """
    url = os.getenv("AGENT_SERVER_URL")
    if url:
        try:
            parsed = urlparse(url)
            if parsed.hostname and parsed.port:
                scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
                return scheme, parsed.hostname, str(parsed.port)
        except ValueError:
            pass

    host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1")
    port = os.getenv("AGENT_SERVER_PORT") or os.getenv("AGENT_PORT", "18092")
    return "http", host, str(port)


def resolve_agent_http_base() -> str:
    """返回目标 AgentServer 的下载 HTTP 端点基址（HTTP bridge 代理目标）。

    优先级：
    1. ``JIUWENSWARM_AGENT_HTTP_BASE`` 环境变量（AgentOS 部署注入沙箱网络基址）；
    2. 单机模式：由 ``AGENT_SERVER_URL`` / ``AGENT_SERVER_HOST`` + ``AGENT_SERVER_PORT``
       （默认 18092）推导 ``http(s)://<host>:<port>``（AgentServer 在 WS 端口拦截
       ``/file-api/download``）。
    """
    env_base = os.getenv("JIUWENSWARM_AGENT_HTTP_BASE")
    if env_base:
        return env_base.rstrip("/")
    scheme, host, port = resolve_agent_host_port()
    return f"{scheme}://{host}:{port}"


def resolve_agent_upload_base() -> str:
    """返回目标 AgentServer 的上传 HTTP 端点基址。

    AgentOS 部署时由路由/扩展层注入 ``JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE``
    或 ``JIUWENSWARM_AGENT_HTTP_PORT`` 环境变量；未注入时按 WS 端口 + 1 推导
    （该推导值仅对已启动独立上传监听器的部署有效，单机模式不会监听该端口，
    调用方应改走本地共享目录写入而非 HTTP 上传）。下载/WS 的
    ``JIUWENSWARM_AGENT_HTTP_BASE`` 不能复用为上传基址；上传监听器恒绑定
    localhost，故端口推导沿用 WS 端口但 host 固定 ``127.0.0.1``。
    """
    env_base = os.getenv("JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE")
    if env_base:
        return env_base.rstrip("/")
    env_port = os.getenv("JIUWENSWARM_AGENT_HTTP_PORT")
    if env_port:
        return f"http://127.0.0.1:{env_port}"
    _, _, port = resolve_agent_host_port()
    try:
        return f"http://127.0.0.1:{int(port) + 1}"
    except (TypeError, ValueError):
        return f"http://127.0.0.1:{port}"


#: 按请求（token payload）解析目标 AgentServer HTTP 基址的可注入扩展点。
#: AgentOS 多用户部署由路由/扩展层注册：``fn(payload: dict) -> str | None``，
#: 返回 ``None`` 时回落环境变量/单机默认推导。单机模式不注册也保持正确。
_agent_http_base_resolver: Any = None


def set_agent_http_base_resolver(resolver: Any) -> None:
    """注册按请求解析 AgentServer HTTP 基址的函数（AgentOS 部署侧扩展点）。"""
    global _agent_http_base_resolver
    _agent_http_base_resolver = resolver


def _decode_token_payload(token: str) -> dict[str, Any] | None:
    """无密钥解码下载/上传 token 的明文 payload（签名校验仍由 AgentServer 负责）。

    仅用于路由决策（解析 ``sid``/``path``），不用于鉴权。
    """
    import base64 as _base64
    import json as _json

    try:
        payload_b64 = token.split(".", 1)[0]
        padding = "=" * (-len(payload_b64) % 4)
        decoded = _base64.urlsafe_b64decode(payload_b64 + padding)
        payload = _json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        return None


def resolve_agent_http_base_for_token(token: str, *, endpoint: str) -> str:
    """解析当前请求应代理到的 AgentServer HTTP 基址。

    Token payload 只用于提取 resolver 所需的路由标识，不能提供代理目标：
    payload 在此处尚未完成签名校验，直接使用其中的 URL 会让攻击者控制
    Web 进程的出站请求目标。AgentOS 多用户部署应注册 resolver；旧部署继续
    使用环境变量或单机默认推导。
    """
    if endpoint not in {"download", "upload"}:
        raise ValueError(f"unsupported HTTP bridge endpoint: {endpoint}")
    payload = _decode_token_payload(token)
    resolver = _agent_http_base_resolver
    if resolver is not None and payload is not None:
        try:
            # 新 resolver 可按端点返回不同基址；保留单参数回调兼容已有部署扩展。
            try:
                base = resolver(payload, endpoint)
            except TypeError:
                base = resolver(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent_http_base] agent http base resolver failed: %s", exc)
            base = None
        if base:
            return str(base).rstrip("/")
    return ""
