# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 把库里的专家包允许名单盖到发往 AgentServer 的请求上。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from openjiuwen_protocol.package_allowlist import (
    PACKAGE_ALLOWLIST_HEADER_NAME as HEADER_NAME,
    apply_allowlist_to_envelope,  # noqa: F401 — re-export，供 agent_client 使用
    encode_package_allowlist_header,
)

# 组 HTTP 头期间持有刚从库里读出的名单。None 表示这次发送没有绑定。
_OUTBOUND: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "package_allowlist_outbound", default=None
)


@contextmanager
def bind_outbound_allowlist(packages: list[dict[str, Any]]) -> Iterator[None]:
    """Gateway 组 HTTP 头期间持有刚从库里读出的名单。"""
    token = _OUTBOUND.set(list(packages))
    try:
        yield
    finally:
        _OUTBOUND.reset(token)


def outbound_allowlist_packages() -> list[dict[str, Any]] | None:
    """未绑定时为 None。调用方不得改用客户端自带的名单。"""
    current = _OUTBOUND.get()
    if current is None:
        return None
    return list(current)


def attach_package_allowlist_header(headers: dict[str, str]) -> None:
    """写入刚绑定的名单。未绑定时按空名单发送。"""
    packages = outbound_allowlist_packages()
    if packages is None:
        packages = []
    headers[HEADER_NAME] = encode_package_allowlist_header(packages)
