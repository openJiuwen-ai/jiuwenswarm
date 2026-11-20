# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the A2UI feature.

Dict/env parsing lives in Control so Front config queries never import Runtime.
``get_current_a2ui_config`` stays here because it reads the Runtime config cache
and the request-scoped A2UI config source.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from jiuwenswarm.server.control.a2ui_config import (
    A2UIConfig,
    SUPPORTED_A2UI_PROTOCOL_VERSIONS,
    get_a2ui_config,
    is_a2ui_enabled as _is_a2ui_enabled_from_dict,
)

__all__ = [
    "A2UIConfig",
    "SUPPORTED_A2UI_PROTOCOL_VERSIONS",
    "get_a2ui_config",
    "get_current_a2ui_config",
    "is_a2ui_enabled",
    "refresh_a2ui_config_source",
    "reset_a2ui_config_source",
    "a2ui_config_source",
    "set_a2ui_config_source",
]


# 仅服务 A2UI：请求级读源（通常是 adapter 已 resolve 快照的复印件，含 a2ui）。
# 权威源仍是 adapter._config_base_cache；这里不是公共企业配置总线。
# 无绑定时 get_current_a2ui_config 回退进程 get_config()（个人版 yaml）。
_A2UI_CONFIG_SOURCE: ContextVar[dict[str, Any] | None] = ContextVar(
    "a2ui_config_source",
    default=None,
)


def set_a2ui_config_source(config: dict[str, Any] | None) -> Token:
    """Bind a config dict that ``get_a2ui_config`` can read for this task."""
    return _A2UI_CONFIG_SOURCE.set(config if isinstance(config, dict) else None)


def reset_a2ui_config_source(token: Token) -> None:
    """Undo :func:`set_a2ui_config_source` / ``a2ui_config_source``."""
    _A2UI_CONFIG_SOURCE.reset(token)


def refresh_a2ui_config_source(config: dict[str, Any] | None) -> None:
    """Replace the A2UI read source mid-request (e.g. Team 重新 resolve 后).

    未绑定时也会写入，便于 Team 路径在外层尚未 bind 时仍能生效。
    """
    _A2UI_CONFIG_SOURCE.set(config if isinstance(config, dict) else None)


@contextmanager
def a2ui_config_source(config: dict[str, Any] | None) -> Iterator[None]:
    """Request-scope：让 ``get_current_a2ui_config`` 优先读这份快照里的 a2ui。"""
    token = set_a2ui_config_source(config)
    try:
        yield
    finally:
        reset_a2ui_config_source(token)


def get_current_a2ui_config() -> A2UIConfig:
    """Read A2UI config from the request source, else process config.

    企业路径：会话 / 请求上 ``resolve_agent_config`` 的结果已写入
    ``config.a2ui``（含无行 fail closed）。绑了读源就读读源，避免再悄悄
    回落到进程 yaml 把开关盖回去。
    """
    source = _A2UI_CONFIG_SOURCE.get()
    if isinstance(source, dict):
        return get_a2ui_config(source)

    from jiuwenswarm.common.config import get_config

    return get_a2ui_config(get_config() or {})


def is_a2ui_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether A2UI is enabled for ``config`` or the current request."""
    if config is None:
        return get_current_a2ui_config().enabled
    return _is_a2ui_enabled_from_dict(config)
