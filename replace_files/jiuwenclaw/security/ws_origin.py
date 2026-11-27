# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Shared WebSocket Origin validation helpers."""

from __future__ import annotations

import logging
import os
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_ALLOWED_WS_ORIGIN_HOSTS = {"127.0.0.1", "localhost"}
_FORBIDDEN_BODY = b"Forbidden: Origin not allowed\n"

# 配置缓存，避免每次调用都读取配置文件
_ws_origin_check_enabled_cache: bool | None = None


def _parse_bool(value: str | None) -> bool | None:
    """解析布尔值字符串。

    Args:
        value: 字符串值，支持 "true", "false", "1", "0"（大小写不敏感）

    Returns:
        解析后的布尔值，无法解析时返回 None
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    return None


def _get_ws_origin_check_enabled() -> bool:
    """读取 WebSocket Origin 校验开关配置。

    优先级：
    1. 环境变量 WS_ORIGIN_CHECK_ENABLED
    2. 配置文件 ws_origin_check_enabled（已解析环境变量语法）
    3. 默认值 True（默认要校验）

    Returns:
        True 表示需要校验，False 表示跳过校验
    """
    global _ws_origin_check_enabled_cache
    if _ws_origin_check_enabled_cache is not None:
        return _ws_origin_check_enabled_cache

    # 1. 优先从环境变量读取
    env_value = os.getenv("WS_ORIGIN_CHECK_ENABLED")
    parsed_env = _parse_bool(env_value)
    if parsed_env is not None:
        _ws_origin_check_enabled_cache = parsed_env
        return parsed_env

    # 2. 从配置文件读取（config.yaml 已通过 resolve_env_vars 解析环境变量语法）
    try:
        from jiuwenclaw.config import get_config

        config = get_config() or {}
        config_value = config.get("ws_origin_check_enabled")
        parsed_config = _parse_bool(str(config_value) if config_value is not None else None)
        if parsed_config is not None:
            _ws_origin_check_enabled_cache = parsed_config
            return parsed_config
    except Exception as e:
        logger.debug("[ws_origin] Failed to read config: %s", e)

    # 3. 默认值：True（默认要校验）
    _ws_origin_check_enabled_cache = True
    return True


def clear_ws_origin_check_cache() -> None:
    """清除 WebSocket Origin 校验开关配置缓存。"""
    global _ws_origin_check_enabled_cache
    _ws_origin_check_enabled_cache = None


def is_allowed_browser_origin(origin: str | None) -> bool:
    """校验浏览器 Origin 是否来自允许的本机地址。

    当配置项 ws_origin_check_enabled 为 False 时，
    跳过校验直接返回 True。

    Args:
        origin: 浏览器请求的 Origin header 值

    Returns:
        True 表示允许该 Origin，False 表示禁止
    """
    # 配置开关关闭时，跳过校验
    if not _get_ws_origin_check_enabled():
        return True

    if origin is None:
        return False

    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False

    return parsed.hostname in _ALLOWED_WS_ORIGIN_HOSTS


def extract_handshake_request(args: tuple[Any, ...]) -> tuple[str, Any]:
    """Extract path and headers from legacy/new websockets process_request args."""
    path = ""
    headers = None

    if len(args) >= 2:
        first, second = args[0], args[1]
        if isinstance(first, str):
            path = first
            headers = second
        else:
            path = getattr(second, "path", "") or ""
            headers = getattr(second, "headers", second)

    return path, headers


def get_header_value(headers: Any, key: str) -> str | None:
    """Read a header from either legacy or modern websockets header containers."""
    if headers is None:
        return None
    get = getattr(headers, "get", None)
    if callable(get):
        value = get(key)
        if value is None:
            value = get(key.lower())
        return str(value) if value is not None else None
    return None


def forbidden_origin_response(process_request_args: tuple[Any, ...]) -> Any:
    """Build a 403 response for legacy/new websockets process_request APIs."""
    status = HTTPStatus.FORBIDDEN
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(_FORBIDDEN_BODY))),
    ]

    if process_request_args and not isinstance(process_request_args[0], str):
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        return Response(status.value, status.phrase, Headers(headers), _FORBIDDEN_BODY)

    return status, headers, _FORBIDDEN_BODY
