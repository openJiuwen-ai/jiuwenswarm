# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""兼容 openjiuwen<0.1.9 的 AsyncCallbackFramework API 缺口（如 unregister_sync）。"""

from __future__ import annotations

from typing import Any, Callable


def unregister_callback_sync(framework: Any, event: str, callback: Callable | None) -> None:
    """与 openjiuwen 0.1.9+ 的 ``unregister_sync`` 行为一致；在旧版上回退为等价的同步实现。"""
    if callback is None:
        return
    unreg = getattr(framework, "unregister_sync", None)
    if callable(unreg):
        unreg(event, callback)
        return

    callbacks_map = getattr(framework, "_callbacks", None)
    if callbacks_map is None or event not in callbacks_map:
        return

    callback_to_remove = None
    for callback_info in callbacks_map[event]:
        if callback_info.callback == callback:
            callback_to_remove = callback_info.callback
            break
        if callback_info.wrapper == callback:
            callback_to_remove = callback_info.callback
            break
        if hasattr(callback, "__wrapped__"):
            wrapped_func = getattr(callback, "__wrapped__", None)
            if wrapped_func is not None and callback_info.callback == wrapped_func:
                callback_to_remove = callback_info.callback
                break

    if callback_to_remove is None:
        return

    callbacks_map[event] = [
        ci for ci in callbacks_map[event] if ci.callback != callback_to_remove
    ]
    cb_filters = getattr(framework, "_callback_filters", None)
    if isinstance(cb_filters, dict):
        cb_filters.pop(callback_to_remove, None)
    chains = getattr(framework, "_chains", None)
    if isinstance(chains, dict) and event in chains:
        chains[event].remove(callback_to_remove)
    if getattr(framework, "enable_logging", False):
        logger = getattr(framework, "logger", None)
        if logger:
            name = getattr(callback_to_remove, "__name__", "unknown")
            logger.info("Unregistered callback: %s -> %s", event, name)
