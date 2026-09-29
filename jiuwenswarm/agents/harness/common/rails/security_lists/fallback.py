# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""兜底总开关（spec 7.3 / 设计 5.2）：``security.fallback.p2_fail_closed``。

- 默认 ``true``：CSPL 不可用且命中 P2（疑似凭证外传）→ fail-closed 拒绝；
- ``false`` → P2 也降级 fail-open + 审计 + 提示（与 P3/P4 一致），放行事件打
  ``p2_fail_open_disabled`` 标记（可独立筛查）；
- **strict 档（profile == default）忽略此开关**（恒 fail-closed，不可被可用性
  开关稀释）；开关变更落 ``security.fallback.switch`` 审计。
"""
from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rails.security_lists import audit

logger = logging.getLogger(__name__)

_SWITCH_DEFAULT = True


def is_strict_profile() -> bool:
    """当前是否 strict 档（profile == "default"）；异常回退 strict（fail-safe）。"""
    try:
        from jiuwenswarm.common.permission_profile import current_permission_profile

        return current_permission_profile() == "default"
    except Exception:  # noqa: BLE001
        return True


def p2_fail_closed_switch() -> bool:
    """读取开关原始值（缺省 true；不考虑 strict 档覆盖）。"""
    try:
        from jiuwenswarm.common.config import get_config

        cfg: Any = get_config() or {}
        raw = ((cfg.get("security") or {}).get("fallback") or {}).get("p2_fail_closed")
        return raw is not False
    except Exception:  # noqa: BLE001
        return _SWITCH_DEFAULT


def p2_fail_closed_active() -> bool:
    """P2 兜底实际生效：strict 档恒 true（忽略开关），否则看开关。"""
    return is_strict_profile() or p2_fail_closed_switch()


def set_p2_fail_closed(enabled: bool) -> bool:
    """写入总开关并落审计（变更落审计；写失败返回 False）。"""
    from jiuwenswarm.common.config import update_config

    before = p2_fail_closed_switch()
    after = bool(enabled)

    def _mut(data: dict[str, Any]) -> dict[str, Any]:
        security = data.setdefault("security", {})
        if not isinstance(security, dict):
            data["security"] = security = {}
        fb = security.setdefault("fallback", {})
        if not isinstance(fb, dict):
            security["fallback"] = fb = {}
        fb["p2_fail_closed"] = after
        return data

    try:
        update_config(_mut)
    except Exception:  # noqa: BLE001
        logger.warning("[security_lists] 兜底总开关写入失败", exc_info=True)
        return False
    audit.log_event(audit.AUDIT_FALLBACK_SWITCH, before=before, after=after)
    logger.info("[security_lists] 兜底总开关变更 p2_fail_closed %s -> %s", before, after)
    return True


__all__ = [
    "is_strict_profile",
    "p2_fail_closed_active",
    "p2_fail_closed_switch",
    "set_p2_fail_closed",
]
