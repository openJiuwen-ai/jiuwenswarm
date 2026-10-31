# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 威胁分析模块驱动器。

ThreatAnalyzer 持有各检测模块的威胁分析插件,按检测模块名分发建模数据,
调用对应插件的 analyze 方法产出威胁分析报告(简化格式)。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_ssas.core.framework.config import AgentSSASConfig
from agent_ssas.core.framework.threat_analyzer.interfaces import ThreatAnalyzerPlugin

logger = logging.getLogger(__name__)


class ThreatAnalyzer:
    """威胁分析模块驱动器。

    持有各检测模块的威胁分析插件,按 model_type 匹配执行分析。
    一个检测模块 = 1 个数据建模插件 + 1 个威胁分析插件,
    配对插件的 model_type 与 expected_model_type 应保持一致。
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化威胁分析驱动器。

        Args:
            config: AgentSSAS 子系统配置,注入到驱动器供插件读取专有配置。
        """
        self._config = config
        # module_name → ThreatAnalyzerPlugin
        self._analyzers: dict[str, ThreatAnalyzerPlugin] = {}

    def register_analyzer(self, module_name: str, plugin: ThreatAnalyzerPlugin) -> None:
        """注册威胁分析插件。

        将检测模块名与威胁分析插件绑定,后续 analyze 按模块名分发。

        Args:
            module_name: 检测模块名,作为分发键。
            plugin: 实现 ThreatAnalyzerPlugin 协议的插件实例。

        Raises:
            TypeError: module_name 不是 str,或 plugin 未实现 ThreatAnalyzerPlugin 协议时。
            ValueError: module_name 为空字符串时。
        """
        if not isinstance(module_name, str):
            raise TypeError(
                f"module_name 必须为 str,实际类型: {type(module_name).__name__}"
            )
        if not module_name:
            raise ValueError("module_name 不能为空字符串")
        if not isinstance(plugin, ThreatAnalyzerPlugin):
            raise TypeError(
                f"plugin 必须实现 ThreatAnalyzerPlugin 协议,实际类型: {type(plugin).__name__}"
            )

        self._analyzers[module_name] = plugin
        logger.debug(
            "注册威胁分析插件: module_name=%s, plugin=%s",
            module_name,
            getattr(plugin, "name", type(plugin).__name__),
        )

    async def analyze(self, module_name: str, model_data: Any) -> dict[str, Any]:
        """执行指定检测模块的威胁分析。

        根据模块名查找已注册的威胁分析插件,调用其 analyze 方法。
        插件未注册或执行异常时返回无风险报告,避免单个模块的分析失败
        影响整条流水线,保证调用方总能拿到结构一致的报告。

        Args:
            module_name: 检测模块名。
            model_data: 配对的数据建模插件输出的建模数据。

        Returns:
            威胁分析报告(简化格式)。插件未注册或执行异常时返回无风险报告,
            至少包含 has_risk、risk_level、module_name 字段。
        """
        if not isinstance(module_name, str):
            raise TypeError(
                f"module_name 必须为 str,实际类型: {type(module_name).__name__}"
            )

        plugin = self._analyzers.get(module_name)
        if plugin is None:
            logger.warning("未找到威胁分析插件: module_name=%s", module_name)
            return self._empty_report(module_name)

        try:
            return await plugin.analyze(model_data)
        except Exception:
            logger.exception(
                "威胁分析插件执行失败: module_name=%s, plugin=%s",
                module_name,
                getattr(plugin, "name", type(plugin).__name__),
            )
            return self._empty_report(module_name)

    @staticmethod
    def _empty_report(module_name: str) -> dict[str, Any]:
        """构造无风险兜底报告。

        用于插件未注册或执行异常时的兜底返回,保证调用方拿到结构一致的报告。

        Args:
            module_name: 检测模块名,用于报告字段 module_name。

        Returns:
            无风险的威胁分析报告(简化格式)。
        """
        return {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "",
            "risk_score": 0.0,
            "confidence": 1.0,
            "detected_threats": [],
            "evidence": {},
            "module_name": module_name,
        }
