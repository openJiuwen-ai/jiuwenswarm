# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Sync callers 中运行 async 协程的桥接 helper。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def run_awaitable(awaitable: Any) -> Any:
    """在无运行中事件循环时用 asyncio.run；已有 loop 时放到临时线程执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


__all__ = ["run_awaitable"]
