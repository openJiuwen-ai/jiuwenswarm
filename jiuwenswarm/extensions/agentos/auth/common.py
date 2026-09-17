# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""WebSocket 握手鉴权公共工具。"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult

_UNAUTHORIZED_BODY = b"Unauthorized\n"
_HANDSHAKE_AUTH_TTL_S = 60.0
# path|token -> (monotonic_ts, AuthResult)；供 process_request → connection_handler 传递身份
_handshake_auth_cache: dict[str, tuple[float, AuthResult]] = {}


def headers_to_dict(headers: Any) -> dict[str, str]:
    """将 websockets legacy/new headers 转为普通 dict。"""
    if headers is None:
        return {}
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}
    try:
        return {str(k): str(v) for k, v in headers.items()}  # type: ignore[union-attr]
    except Exception:
        return {}


# 从 URL query / Authorization header / X-Token header 提取 token
def extract_token_from_path_and_headers(
        path: str,
        headers: Any,
) -> str | None:
    """从握手 path/headers 提取 JWT token。
    优先级：
    1. URL 查询参数 ?token=xxx
    2. Authorization: Bearer xxx
    3. X-Token: xxx
    """
    parsed = urlparse(path or "")
    params = parse_qs(parsed.query)
    token = params.get("token", [None])[0]
    if token:
        return token

    header_map = headers_to_dict(headers)
    # header 名大小写不敏感查找
    lowered = {k.lower(): v for k, v in header_map.items()}
    auth = lowered.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    x_token = lowered.get("x-token", "")

    if x_token:
        return x_token

    return None


def extract_token(ws: Any) -> str | None:
    """从已建立的 WebSocket 连接中提取 JWT token。"""
    path = getattr(ws, "path", "") or ""
    return extract_token_from_path_and_headers(path, extract_headers(ws))


def extract_headers(ws: Any) -> dict:
    """提取 WebSocket 连接的 HTTP 请求头。"""
    headers = (
            getattr(getattr(ws, "request", None), "headers", None)
            or getattr(ws, "request_headers", None)
    )
    return headers_to_dict(headers)


def get_remote_addr(ws: Any) -> str:
    """获取客户端 IP 地址。"""
    remote = getattr(ws, "remote_address", None)
    if remote:
        if isinstance(remote, (list, tuple)):
            return f"{remote[0]}:{remote[1]}"
        return str(remote)
    return ""


def _handshake_cache_key(path: str, headers: Any) -> str:
    token = extract_token_from_path_and_headers(path, headers) or ""
    return f"{path}|{token}"


def take_handshake_auth(path: str, headers: Any) -> AuthResult | None:
    key = _handshake_cache_key(path, headers)
    item = _handshake_auth_cache.pop(key, None)
    if item is None:
        return None
    ts, result = item
    if time.monotonic() - ts > _HANDSHAKE_AUTH_TTL_S:
        return None
    return result


def apply_auth_result_to_ws(ws: Any, result: AuthResult) -> None:
    """将鉴权身份挂到 ws，供后续路由等逻辑读取。

    当前仅落盘返回值约定字段；完整身份贯通（注册中心 / 实例创建）后续再接。
    """
    ws.user_id = result.user_id
    setattr(ws, "_gateway_user_id", result.user_id)
    setattr(ws, "_web_connection_user_id", result.user_id or None)
    for key, value in (result.extensions or {}).items():
        setattr(ws, f"auth_{key}", value)