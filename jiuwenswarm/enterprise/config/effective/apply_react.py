# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig`` 中的 react 模板写入 config 快照（覆盖 config.yaml::react）。

react 配置整段存 react_template.data；下发后整段覆盖 ``config["react"]``，
未配置 react 槽位 / 无 data / data 非法时保留本地 config.yaml（兜底）。
模型三键（model_name / model_client_config / model_config_obj）由模型槽位统一掌管，
保留 apply_models 已写入的值，不受 react.data 覆盖。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_SLOT_KEYS = ("model_name", "model_client_config", "model_config_obj")


def apply_enterprise_react_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并把 react.data 写入 ``config["react"]``。

    Returns:
        ``(merged, applied)``：``applied=True`` 表示已用企业 react.data 覆盖。
    """
    entities = getattr(enterprise, "react", None) if enterprise is not None else None
    if not entities:
        return config_base, False

    entity = entities[0] if isinstance(entities, (list, tuple)) else entities
    if not isinstance(entity, dict):
        return config_base, False
    data = entity.get("data")
    if not isinstance(data, dict) or not data:
        return config_base, False

    merged = copy.deepcopy(config_base)
    existing_react = merged.get("react")
    base_react = existing_react if isinstance(existing_react, dict) else {}
    # 模型三键由模型槽位管理：保留 apply_models 已写入的值，剥掉 data 里可能误带的
    model_keys = {key: base_react[key] for key in _MODEL_SLOT_KEYS if key in base_react}
    data = copy.deepcopy(data)
    for key in _MODEL_SLOT_KEYS:
        data.pop(key, None)

    merged["react"] = {**data, **model_keys}
    logger.info("[enterprise_react] applied react body: keys=%s", sorted(data))
    return merged, True


__all__ = ("apply_enterprise_react_to_config",)
