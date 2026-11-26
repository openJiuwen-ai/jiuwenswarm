# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig`` 中的执行护栏模板写入 config 快照。

企业版不依赖 ``config.yaml`` 的 ``execution_guard``：先套用代码写死的
``DEFAULT_EXECUTION_GUARD_BODY``（数值对齐包内 yaml，但不读取 yaml），再在有
启用模板且 body 合法时用 body **整段替换**。个人版不进入本函数。
不写回 ``config.yaml``。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .execution_guard_body import (
    DEFAULT_EXECUTION_GUARD_BODY,
    validate_execution_guard_body,
)

logger = logging.getLogger(__name__)


def _code_default_body() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_EXECUTION_GUARD_BODY)


def apply_enterprise_execution_guard_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base``，用代码默认铺底，再用合法模板 body 整段替换。

    Returns:
        ``(merged, applied)``：企业路径总会写入 ``execution_guard``，``applied``
        恒为 ``True``。
    """
    merged = copy.deepcopy(config_base)
    entities = (
        getattr(enterprise, "execution_guard", None) if enterprise is not None else None
    )

    if not entities:
        merged["execution_guard"] = _code_default_body()
        logger.info(
            "[enterprise_execution_guard] applied code defaults "
            "(no execution_guard template)"
        )
        return merged, True

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
        merged["execution_guard"] = _code_default_body()
        logger.info(
            "[enterprise_execution_guard] applied code defaults "
            "(template missing or disabled)"
        )
        return merged, True

    try:
        body = validate_execution_guard_body(entity.get("body"))
    except ValueError:
        logger.error(
            "[enterprise_execution_guard] invalid body for template_id=%s; "
            "falling back to code defaults",
            entity.get("template_id"),
            exc_info=True,
        )
        merged["execution_guard"] = _code_default_body()
        return merged, True

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
