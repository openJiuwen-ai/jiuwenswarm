# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""兼容封装：优先转调 ``telemetry.audit``；无则直调 foundation ``log_audit``。

新代码请使用 ``jiuwenswarm.telemetry.audit.audit_claw_log``。
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# caller 要落到业务打点，不能停在本封装或 telemetry / foundation 入口上。
_CALLER_SKIP_MODULES = (
    "openjiuwen_runtime.foundation.audit",
    "jiuwenswarm.telemetry.audit",
    "jiuwenswarm.common.audit_emit",
)


def capture_audit_caller() -> str:
    """栈回溯：``模块.函数:行号``，跳过审计封装帧。"""
    frame = inspect.currentframe()
    try:
        while frame is not None:
            module = str(frame.f_globals.get("__name__", "") or "")
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _CALLER_SKIP_MODULES
            ):
                frame = frame.f_back
                continue
            func = frame.f_code.co_name
            short = module.rsplit(".", 1)[-1] if module else "<unknown>"
            return f"{short}.{func}:{frame.f_lineno}"
        return "-"
    finally:
        del frame


def emit_audit(
    event_type: str,
    *,
    level: str,
    SUBMDL: str,
    PROC: str,
    RSPCD: str = "0000",
    **fields: Any,
) -> None:
    """打点；个人版 no-op；异常只记 warning。"""
    caller = capture_audit_caller()
    try:
        from jiuwenswarm.edition import is_enterprise

        if not is_enterprise():
            return
    except Exception:  # noqa: BLE001
        pass

    try:
        from jiuwenswarm.telemetry.audit import audit_claw_log

        keyword = str(event_type or "").strip().upper()
        success = keyword != "EVT"
        desc = fields.get("UA") or fields.get("EVT") or fields.get("MSG")
        audit_claw_log(
            submdl=SUBMDL,
            proc=PROC,
            success=success,
            rspcd=RSPCD,
            desc=str(desc) if desc is not None else None,
            message=str(fields.get("MSG") or ""),
            level=level,
            caller=caller,
            extra={k: v for k, v in fields.items() if k not in ("UA", "EVT", "MSG")},
        )
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        from openjiuwen_runtime.foundation.audit import log_audit

        payload = dict(fields)
        payload.setdefault("SUBMDL", SUBMDL)
        payload.setdefault("PROC", PROC)
        payload.setdefault("RSPCD", RSPCD)
        payload["caller"] = caller
        keyword = str(event_type or "").strip().upper()
        if keyword == "UA":
            payload.setdefault("UA", PROC)
        elif keyword == "EVT":
            payload.setdefault("EVT", PROC)
        log_audit(keyword, level=level, **payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "emit_audit failed event_type=%s SUBMDL=%s PROC=%s: %s",
            event_type,
            SUBMDL,
            PROC,
            exc,
        )


def emit_audit_ua(
    *,
    SUBMDL: str,
    PROC: str,
    RSPCD: str = "0000",
    level: str = "INFO",
    **fields: Any,
) -> None:
    emit_audit("UA", level=level, SUBMDL=SUBMDL, PROC=PROC, RSPCD=RSPCD, **fields)


def emit_audit_evt(
    *,
    SUBMDL: str,
    PROC: str,
    RSPCD: str = "E999",
    level: str = "WARN",
    **fields: Any,
) -> None:
    emit_audit("EVT", level=level, SUBMDL=SUBMDL, PROC=PROC, RSPCD=RSPCD, **fields)


class AuditTimer:
    """上下文：记录耗时毫秒。"""

    def __init__(self) -> None:
        self._start = 0.0
        self.cost_ms = 0

    def __enter__(self) -> "AuditTimer":
        self._start = time.perf_counter()
        self.cost_ms = 0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cost_ms = int((time.perf_counter() - self._start) * 1000)
