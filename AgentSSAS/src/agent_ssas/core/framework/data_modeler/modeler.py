# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 数据建模模块驱动器。

DataModeler 持有各检测模块的数据建模插件,按检测模块名分发事件描述,
调用对应插件的 build_model 构建建模数据,供配对的威胁分析插件消费。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_ssas.core.framework.config import AgentSSASConfig
from agent_ssas.core.framework.data_modeler.interfaces import DataModelerPlugin

logger = logging.getLogger(__name__)


class DataModeler:
    """数据建模模块驱动器。

    持有各检测模块的数据建模插件,按订阅关系分发事件。
    一个检测模块 = 1 个数据建模插件 + 1 个威胁分析插件。
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化数据建模驱动器。

        Args:
            config: AgentSSAS 子系统配置,注入到驱动器供插件读取专有配置。
        """
        self._config = config
        # module_name → DataModelerPlugin
        self._modelers: dict[str, DataModelerPlugin] = {}

    def register_modeler(self, module_name: str, plugin: DataModelerPlugin) -> None:
        """注册数据建模插件。

        将检测模块名与数据建模插件绑定,后续 build_model 按模块名分发。

        Args:
            module_name: 检测模块名,作为分发键。
            plugin: 实现 DataModelerPlugin 协议的插件实例。

        Raises:
            TypeError: module_name 不是 str,或 plugin 未实现 DataModelerPlugin 协议时。
            ValueError: module_name 为空字符串时。
        """
        if not isinstance(module_name, str):
            raise TypeError(
                f"module_name 必须为 str,实际类型: {type(module_name).__name__}"
            )
        if not module_name:
            raise ValueError("module_name 不能为空字符串")
        if not isinstance(plugin, DataModelerPlugin):
            raise TypeError(
                f"plugin 必须实现 DataModelerPlugin 协议,实际类型: {type(plugin).__name__}"
            )

        self._modelers[module_name] = plugin
        logger.debug(
            "注册数据建模插件: module_name=%s, plugin=%s",
            module_name,
            getattr(plugin, "name", type(plugin).__name__),
        )

    async def build_model(self, module_name: str, event_desc: dict[str, Any]) -> Any:
        """为指定检测模块构建建模数据。

        根据模块名查找已注册的数据建模插件,调用其 build_model 方法。
        插件未注册时返回 None,插件抛出异常时记录日志并返回 None,
        避免单个模块的建模失败影响整条流水线。

        Args:
            module_name: 检测模块名。
            event_desc: 事件描述 json,包含 event_node、aux_ids、trace 等字段。

        Returns:
            建模数据,格式由插件自行定义;模块未注册或建模失败时返回 None。
        """
        if not isinstance(module_name, str):
            raise TypeError(
                f"module_name 必须为 str,实际类型: {type(module_name).__name__}"
            )
        if not isinstance(event_desc, dict):
            raise TypeError(
                f"event_desc 必须为 dict,实际类型: {type(event_desc).__name__}"
            )

        plugin = self._modelers.get(module_name)
        if plugin is None:
            logger.warning("未找到数据建模插件: module_name=%s", module_name)
            return None

        try:
            return await plugin.build_model(event_desc)
        except Exception:
            logger.exception(
                "数据建模插件执行失败: module_name=%s, plugin=%s",
                module_name,
                getattr(plugin, "name", type(plugin).__name__),
            )
            return None
