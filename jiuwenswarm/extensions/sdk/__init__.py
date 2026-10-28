"""兼容入口：扩展 SDK 契约的唯一实现位于 ``gateway.extensions.sdk``。

第三方扩展仍可按 ``from jiuwenswarm.extensions.sdk import BaseExtension`` 导入，
本模块只做惰性转发，不再保留第二份类定义（避免 BaseExtension / CryptoUtility
等基类出现两套类身份）。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_GATEWAY_SDK = import_module("gateway.extensions.sdk")

__all__ = list(_GATEWAY_SDK.__all__)


def __getattr__(name: str) -> Any:
    try:
        return getattr(_GATEWAY_SDK, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_GATEWAY_SDK.__all__))
