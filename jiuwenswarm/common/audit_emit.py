# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FR2 业务侧安全审计打点封装：永不打断主路径。"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def emit_audit(
    event_type: str,
    *,
    level: str,
    SUBMDL: str,
    PROC: str,
    RSPCD: str = "0000",
    **fields: Any,
) -> None:
    """调用 foundation ``log_audit``；异常只记 warning。"""
    try:
        from openjiuwen_runtime.foundation.audit import log_audit

        payload = dict(fields)
        payload.setdefault("SUBMDL", SUBMDL)
        payload.setdefault("PROC", PROC)
        payload.setdefault("RSPCD", RSPCD)
        keyword = str(event_type or "").strip().upper()
        # UA/EVT 正文缺省用 PROC，避免 required 全 placeholder 时 body 过空
        if keyword == "UA":
            payload.setdefault("UA", PROC)
        elif keyword == "EVT":
            payload.setdefault("EVT", PROC)
        log_audit(keyword, level=level, **payload)
        # TODO(temp): 联调可见性，确认后删除（含当前热更配置身份，便于核对 PUT 是否生效）
        try:
            from openjiuwen_runtime.foundation.audit import get_audit_manager

            cfg = get_audit_manager().config
            identity = cfg.identity
            system_code = identity.system_code
            data_center = identity.data_center
            node = identity.node
            service = cfg.service
            otel_endpoint = cfg.otel.endpoint
        except Exception:  # noqa: BLE001
            system_code = data_center = node = service = otel_endpoint = "?"
        logger.info(
            "[audit][temp] emit ok event_type=%s level=%s SUBMDL=%s PROC=%s "
            "RSPCD=%s system_code=%s data_center=%s node=%s service=%s "
            "otel_endpoint=%s",
            keyword,
            level,
            SUBMDL,
            PROC,
            RSPCD,
            system_code,
            data_center,
            node,
            service,
            otel_endpoint,
        )
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
    RSPCD: str = "E001",
    level: str = "WARN",
    **fields: Any,
) -> None:
    emit_audit("EVT", level=level, SUBMDL=SUBMDL, PROC=PROC, RSPCD=RSPCD, **fields)


class audit_timer:
    """``with audit_timer() as t: ...`` → ``t.cost_ms``。"""

    __slots__ = ("_start", "cost_ms")

    def __init__(self) -> None:
        self._start = 0.0
        self.cost_ms = 0

    def __enter__(self) -> audit_timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.cost_ms = int((time.perf_counter() - self._start) * 1000)


__all__ = [
    "audit_timer",
    "emit_audit",
    "emit_audit_evt",
    "emit_audit_ua",
]
