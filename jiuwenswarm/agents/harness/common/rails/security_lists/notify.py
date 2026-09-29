# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""桌面健康告警（behavior bridge 通道，best-effort，不阻断业务判定）。

名单 rail 的 fail-closed 拒绝、CSPL 降级放行等共用本通道把兜底事件上报桌面
（前端安全中心 banner / 降级提示）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def report_security_event(ctx: Any, *, stage: str, detail: str) -> None:
    """经 BehaviorSecurityBridge 上报一条兜底/降级事件（失败仅告警）。"""
    try:
        from jiuwenswarm.common.behavior_security import (
            ACTIVE_BEHAVIOR,
            desktop_security_active,
            raw_event,
        )

        if not desktop_security_active():
            return
        active = ACTIVE_BEHAVIOR.get()
        if active is None:
            return
        event = raw_event(ctx, stage)
        event["detail"] = str(detail)[:500]
        active[0].report(event)
    except Exception:  # noqa: BLE001
        logger.warning("[security_lists] 桌面健康告警发送失败 stage=%s", stage, exc_info=True)


def report_security_event_detached(*, stage: str, detail: str) -> None:
    """无工具上下文的健康告警（审计落盘异常等后台事件，best-effort）。

    与 :func:`report_security_event` 同通道，但事件不依附具体工具调用：
    手工构造最小事件结构（sessionId/tool 留空）。
    """
    try:
        import hashlib
        import time

        from jiuwenswarm.common.behavior_security import (
            ACTIVE_BEHAVIOR,
            desktop_security_active,
        )

        if not desktop_security_active():
            return
        active = ACTIVE_BEHAVIOR.get()
        if active is None:
            return
        event = {
            "version": 1,
            "eventId": hashlib.sha256(
                f"{stage}:{detail}:{time.time()}".encode()
            ).hexdigest(),
            "stage": stage,
            "sessionId": "",
            "tool": {"id": "", "name": "", "arguments": {}},
            "detail": str(detail)[:500],
        }
        active[0].report(event)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[security_lists] 桌面健康告警发送失败（detached） stage=%s", stage, exc_info=True
        )


__all__ = ["report_security_event", "report_security_event_detached"]
