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
import re
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_SLOT_KEYS = ("model_name", "model_client_config", "model_config_obj")

# 叶子键名命中的敏感凭据，展平时只打掩码不打明文（api_key、custom_headers 等）；
# 掩码产物与 common.utils 日志脱敏同形态（纯 ****** 不会被二次脱敏）
_MASKED_VALUE = "******"
_SENSITIVE_KEY_PATTERN = re.compile(
    r"api_key|apikey|token|secret|password|headers", re.IGNORECASE
)


def _flatten_react_kvs(data: dict[str, Any], prefix: str = "") -> list[str]:
    """把 react 配置递归展平成 ``a.b=value`` 片段，一行看全所有 key value。

    键名含 api_key / token / secret / password / headers 的键整棵掩码为
    ``******``——模型 api_key、custom_headers 凭据明文不落日志。
    """
    pairs: list[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if _SENSITIVE_KEY_PATTERN.search(str(key)):
            # 整个子树掩码：headers 内部键名不一定敏感，但值可能带凭据
            pairs.append(f"{path}={_MASKED_VALUE}")
        elif isinstance(value, dict) and value:
            pairs.extend(_flatten_react_kvs(value, path))
        else:
            pairs.append(f"{path}={value!r}")
    return pairs


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
    # 排查企业 react 下发问题：把生效后的 react 配置全部 key value 展平打印。
    logger.debug(
        "[enterprise_react] applied react kvs: %s",
        " ".join(_flatten_react_kvs(merged["react"])),
    )
    return merged, True


__all__ = ("apply_enterprise_react_to_config",)
