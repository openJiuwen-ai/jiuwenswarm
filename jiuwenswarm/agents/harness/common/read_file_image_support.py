# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-core ``ReadFileTool`` 图片内联能力的跨版本接线。

agent-core 的 ``ReadFileTool(image_multimodal_resolver=...)``（调用时求值）与
``_image_multimodal_enabled()`` 尚未进入 ``uv.lock`` 锁定的 dev-stable（8215f2c）。
本模块按构造函数签名探测一次，避免把该参数无条件传给旧版本 core：
``TypeError: __init__() got an unexpected keyword argument`` 会让整条 rail 装配失败。

- core 支持：resolver 透传，read_file 每次调用时求值，模型热切换立即跟随；
- core 不支持：只在构造时求值（装配时的能力值），退回既有的静态语义。

core 侧 resolver 合入并刷新 ``uv.lock`` 后，本模块自动走支持分支，无需再改调用点。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_MULTIMODAL_RESOLVER_PARAM = "image_multimodal_resolver"

_supports_resolver: bool | None = None


def supports_read_file_image_resolver() -> bool:
    """Whether the installed openjiuwen ``ReadFileTool`` accepts a resolver kwarg."""

    global _supports_resolver
    if _supports_resolver is None:
        try:
            from openjiuwen.harness.tools.filesystem import ReadFileTool
        except ImportError:
            # 不缓存：openjiuwen 可能稍后才可用（局部导入 / 测试替身）。
            return False
        try:
            parameters = inspect.signature(ReadFileTool.__init__).parameters
        except (TypeError, ValueError):
            _supports_resolver = False
        else:
            _supports_resolver = IMAGE_MULTIMODAL_RESOLVER_PARAM in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
    return bool(_supports_resolver)


def build_read_file_image_kwargs(
    resolver: Callable[[], bool] | None = None,
    *,
    enable_image_multimodal: bool,
) -> dict[str, Any]:
    """ReadFileTool 的图片能力关键字参数（无 resolver 的 core 只收静态 bool）。"""

    kwargs: dict[str, Any] = {
        "enable_image_multimodal": bool(enable_image_multimodal),
    }
    if resolver is not None and supports_read_file_image_resolver():
        kwargs[IMAGE_MULTIMODAL_RESOLVER_PARAM] = resolver
    return kwargs


def read_file_image_multimodal_enabled(tool: Any) -> bool:
    """工具当前的图片内联开关：新 core 有调用时方法，旧 core 退回实例属性。"""

    checker = getattr(tool, "_image_multimodal_enabled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # 诊断辅助：探测失败退回静态属性，不影响工具构造
            logger.debug("read_file image resolver probe failed", exc_info=True)
    return bool(getattr(tool, "enable_image_multimodal", False))


__all__ = [
    "IMAGE_MULTIMODAL_RESOLVER_PARAM",
    "build_read_file_image_kwargs",
    "read_file_image_multimodal_enabled",
    "supports_read_file_image_resolver",
]
