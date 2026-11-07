# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSASSecurityRail 决策策略加载器。

从独立 YAML 策略文件加载策略模式定义和 AlertLevel 映射。
策略文件在运行期不变,加载后缓存避免重复 I/O。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 策略文件路径(与本文件同目录)
_POLICY_FILE = Path(__file__).parent / "decision_policies.yaml"

# 模块级缓存:策略文件在运行期不变,加载后缓存
_policies_cache: dict[str, Any] | None = None


def load_policies() -> dict[str, Any]:
    """从 YAML 文件加载所有策略模式定义。

    首次调用读取文件并缓存,后续调用直接返回缓存结果。

    Returns:
        policies: 策略模式字典,key 为策略名,value 为风险等级映射配置
        default_policy: 默认策略名
        alert_levels: AlertLevel 映射(risk_level → alert_level 名称)
    """
    global _policies_cache
    if _policies_cache is not None:
        return _policies_cache

    try:
        with open(_POLICY_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _policies_cache = {
            "policies": data.get("policies", {}),
            "default_policy": data.get("default_policy", "observe_only"),
            "alert_levels": data.get("alert_levels", {}),
        }
    except Exception as e:
        logger.warning("加载策略文件失败,使用默认策略: %s", e)
        _policies_cache = {
            "policies": {},
            "default_policy": "observe_only",
            "alert_levels": {},
        }
    return _policies_cache


def get_policy(policy_name: str) -> dict[str, str]:
    """获取指定策略模式的配置(risk_level → action 映射)。

    Args:
        policy_name: 策略模式名(如 observe_only、active_protection)

    Returns:
        该策略模式的风险等级映射配置,未知策略名回退到默认策略
    """
    data = load_policies()
    policies = data["policies"]
    default = data["default_policy"]
    if policy_name not in policies:
        logger.warning(
            "未知的决策策略模式名: %s,回退到默认策略: %s",
            policy_name,
            default,
        )
    return policies.get(policy_name, policies.get(default, {}))


def get_alert_level(risk_level: str) -> str:
    """获取风险等级对应的 AlertLevel 名称。

    从策略文件的 alert_levels 映射加载(risk_level → alert_level 名称)。
    未知风险等级回退为 "warning"。

    Args:
        risk_level: 风险等级字符串,如 "high"、"medium"。

    Returns:
        AlertLevel 名称:error / warning / info。未知回退为 warning。
    """
    data = load_policies()
    alert_levels = data.get("alert_levels", {})
    return alert_levels.get(risk_level, "warning")
