# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

"""将 ``EffectiveEnterpriseConfig`` 中的 react 模板写入 config 快照（合并 config.yaml::react）。

react 配置存 react_template.data；下发后与 ``config["react"]`` 按键深度合并：
data 里配置了的键以企业值为准，data 未配置的键保留本地 config.yaml 原值。
模型三键（model_name / model_client_config / model_config_obj）由模型槽位统一掌管，
保留 apply_models 已写入的值，data 里误带的键剥掉不覆盖。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_SLOT_KEYS = ("model_name", "model_client_config", "model_config_obj")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """按键深度合并：override 优先，base 中 override 未涉及的键（含嵌套键）保留。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_enterprise_react_to_config(
    config_base: dict[str, Any],
    enterprise: Any | None,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并把 react.data 按键合并进 ``config["react"]``。

    Returns:
        ``(merged, applied)``：``applied=True`` 表示已用企业 react.data 合并。
    """
    entities = getattr(enterprise, "react", None) if enterprise is not None else None
    if not entities:
        return config_base, False

    # react 槽位与 agent 是 1:1 契约：一个 agent 只绑定一个 react 模板。
    # 多实体属配置异常，仅首个生效，warning 提示配置侧排查。
    if isinstance(entities, (list, tuple)) and len(entities) > 1:
        logger.warning(
            "[enterprise_react] multiple react templates configured, "
            "only the first one takes effect: ids=%s",
            [
                e.get("template_id")
                for e in entities
                if isinstance(e, dict)
            ],
        )
    entity = entities[0] if isinstance(entities, (list, tuple)) else entities
    if not isinstance(entity, dict):
        return config_base, False
    data = entity.get("data")
    if not isinstance(data, dict) or not data:
        return config_base, False

    merged = copy.deepcopy(config_base)
    existing_react = merged.get("react")
    base_react = existing_react if isinstance(existing_react, dict) else {}
    # 模型三键由模型槽位管理：保留 apply_models 已写入的值，剥掉 data 里可能误带的；
    # 管理员在 data 里配置模型键是自然预期，静默丢弃不可诊断，warning 点名被剥离键
    dropped = [key for key in _MODEL_SLOT_KEYS if key in data]
    if dropped:
        logger.warning(
            "[enterprise_react] dropped model-slot keys from react_template.data: "
            "%s (model keys are owned by the model slot)",
            dropped,
        )
    data = copy.deepcopy(data)
    for key in _MODEL_SLOT_KEYS:
        data.pop(key, None)

    merged["react"] = _deep_merge(base_react, data)
    logger.info("[enterprise_react] merged react body: keys=%s", sorted(data))
    return merged, True


__all__ = ("apply_enterprise_react_to_config",)
