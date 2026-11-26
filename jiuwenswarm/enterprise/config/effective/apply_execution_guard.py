# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig`` 中的执行护栏模板写入 config 快照。

有启用模板且 ``body`` 通过校验时，用 body **整段替换** ``config.execution_guard``。
未绑定、行未启用、body 不是对象或校验失败时保留从 ``config.yaml`` 读入的值。
不写回 ``config.yaml``。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .execution_guard_body import validate_execution_guard_body

logger = logging.getLogger(__name__)


def apply_enterprise_execution_guard_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并在 body 合法时整段替换 ``execution_guard``。

    Returns:
        ``(merged, applied)``：``applied=True`` 表示已用企业 body 覆盖。
    """
    entities = (
        getattr(enterprise, "execution_guard", None) if enterprise is not None else None
    )
    if not entities:
        return config_base, False

    if isinstance(entities, (list, tuple)) and len(entities) > 1:
        logger.warning(
            "[enterprise_execution_guard] multiple execution guard templates configured, "
            "only the first one takes effect: ids=%s",
            [
                entity.get("template_id")
                for entity in entities
                if isinstance(entity, dict)
            ],
        )
    entity = entities[0] if isinstance(entities, (list, tuple)) else entities
    if not isinstance(entity, dict) or entity.get("enabled") is False:
        return config_base, False

    try:
        body = validate_execution_guard_body(entity.get("body"))
    except ValueError:
        logger.error(
            "[enterprise_execution_guard] invalid body for template_id=%s; "
            "keeping config.yaml execution_guard",
            entity.get("template_id"),
            exc_info=True,
        )
        return config_base, False

    merged = copy.deepcopy(config_base)
    merged["execution_guard"] = body
    logger.info(
        "[enterprise_execution_guard] applied execution guard body: "
        "anomaly_enabled=%s transient_max_retries=%s breaker_enabled=%s",
        body["model_anomaly_detection_rail"].get("enabled"),
        body["model_anomaly_detection_rail"].get("transient_max_retries"),
        body["circuit_breaker"].get("enabled"),
    )
    return merged, True


__all__ = ("apply_enterprise_execution_guard_to_config",)
