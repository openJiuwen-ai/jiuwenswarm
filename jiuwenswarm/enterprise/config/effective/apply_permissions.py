# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig`` 中的 permissions 模板写入 config 快照。

有启用护栏模板且 ``body`` 为 dict 时，用 body **整段替换** ``config.permissions``，
避免 yaml 默认 ``tools: allow`` 冲掉 Manager 下发的 ``defaults["*"]=ask``。
无槽位 / 无启用模板 / body 无效时保留 yaml（未绑护栏则保持默认关闭）。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .permissions import resolve_permissions_body_from_enterprise

logger = logging.getLogger(__name__)


def apply_enterprise_permissions_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并在有护栏 body 时写入 ``permissions``。

    Returns:
        ``(merged, applied)``：``applied=True`` 表示已用企业 body 覆盖。
    """
    body = resolve_permissions_body_from_enterprise(enterprise)
    if not isinstance(body, dict):
        return config_base, False

    merged = copy.deepcopy(config_base)
    merged["permissions"] = body
    logger.info(
        "[enterprise_permissions] applied permissions body: enabled=%s "
        "permission_mode=%s defaults=%s",
        body.get("enabled"),
        body.get("permission_mode"),
        body.get("defaults"),
    )
    return merged, True


__all__ = ("apply_enterprise_permissions_to_config",)
