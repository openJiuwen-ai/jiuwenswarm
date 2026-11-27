# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Rail for disabling tools based on configuration.

Unregisters tools listed in `react.disabled_tools` configuration
during init to prevent them from being available to the Agent.
Supports hot-reload via update_config for dynamic enable/disable.

Key insight: ability_manager uses tool_name as key, resource_mgr uses tool_id (card.id) as key.
So we need to: 1) get ToolCard from ability_manager by name, 2) use card.id to remove Tool from resource_mgr.
"""
from __future__ import annotations

import logging
import traceback
from typing import Set, List, Dict, Optional

from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.core.runner import Runner
logger = logging.getLogger(__name__)


class DisabledToolsRail(DeepAgentRail):
    """Rail that disables tools based on configuration.

    Unregisters tools from ability_manager and resource_mgr during init.
    Supports hot-reload via update_config to dynamically enable/disable tools.

    Example config in config.yaml:
        react:
          disabled_tools: ["bash", "write_file", "mcp_exec_command"]
    """

    priority: int = 1  # 低优先级，确保最后执行

    def __init__(self, disabled_tools: List[str] | None = None) -> None:
        """Initialize DisabledToolsRail.

        Args:
            disabled_tools: List of tool names to disable. Can be None or empty.
        """
        super().__init__()
        self._disabled_tools: Set[str] = set(disabled_tools or [])
        self._agent: Optional["DeepAgent"] = None  # 保存 agent 引用用于热更新
        # tool_name -> (tool_id, Tool, ToolCard) - 保存被移除的完整信息用于重新注册
        self._removed_data: Dict[str, tuple[str, Tool, ToolCard]] = {}

    def init(self, agent: "DeepAgent") -> None:
        """Initialize rail and unregister disabled tools from ability_manager and resource_mgr."""
        super().init(agent)
        self._agent = agent  # 保存 agent 引用

        # 打印当前配置的 disabled_tools
        logger.info("[DisabledToolsRail] initialized with disabled_tools: %s", list(
            self._disabled_tools))

        # 注销禁用的工具
        self._unregister_tools(self._disabled_tools)

    def uninit(self, agent) -> None:
        """Cleanup when rail is removed - re-register all disabled tools."""
        stack_info = ''.join(traceback.format_stack())
        if self._agent:
            self._register_tools(self._disabled_tools)
        self._agent = None
        self._removed_data.clear()

    def _unregister_tools(self, tool_names: Set[str]) -> None:
        """Unregister tools from ability_manager and resource_mgr.

        Correct approach:
        1. Get ToolCard from ability_manager by name
        2. Use card.id to get Tool from resource_mgr (先获取 Tool 对象)
        3. Use card.id to remove Tool from resource_mgr (再删除)
        4. Remove ToolCard from ability_manager
        5. Save (tool_id, Tool, ToolCard) for re-registration

        Args:
            tool_names: Set of tool names to unregister.
        """
        if not self._agent:
            logger.warning(
                "[DisabledToolsRail] _unregister_tools: no agent reference")
            return

        for tool_name in tool_names:
            # Step 1: 从 ability_manager 获取 ToolCard（先获取，再删除）
            tool_card = self._agent.ability_manager.get(tool_name)
            if not tool_card:
                logger.warning(
                    "[DisabledToolsRail] tool '%s' not found in ability_manager", tool_name)
                continue

            tool_id = tool_card.id or tool_card.name

            # Step 2: 用 card.id 先获取 Tool 对象
            tool = Runner.resource_mgr.get_tool(tool_id)

            # Step 3: 用 card.id 删除 resource_mgr 中的 Tool
            remove_result = Runner.resource_mgr.remove_tool(tool_id)
            # remove_tool 返回 Result 类型，检查是否成功
            if hasattr(remove_result, 'is_ok') and not remove_result.is_ok():
                logger.warning("[DisabledToolsRail] remove_tool failed for '%s' (id=%s): %s",
                               tool_name, tool_id, remove_result)

            if not tool:
                logger.warning(
                    "[DisabledToolsRail] tool '%s' (id=%s) not found in resource_mgr", tool_name, tool_id)
                # 仍然继续删除 ability_manager 中的 card

            # Step 4: 从 ability_manager 删除 ToolCard
            self._agent.ability_manager.remove(tool_name)

            # Step 5: 保存数据用于重新注册
            if tool:
                self._removed_data[tool_name] = (tool_id, tool, tool_card)
                logger.info(
                    "[DisabledToolsRail] unregistered tool: name=%s, id=%s",
                    tool_name,
                    tool_id,
                )
            else:
                # 即使 resource_mgr 中没有找到，也保存 card 用于重新注册
                self._removed_data[tool_name] = (tool_id, None, tool_card)
                logger.warning(
                    "[DisabledToolsRail] unregistered ToolCard only (no Tool instance): name=%s, id=%s",
                    tool_name,
                    tool_id,
                )

    def _register_tools(self, tool_names: Set[str]) -> None:
        """Re-register previously removed tools.

        Args:
            tool_names: Set of tool names to re-register.
        """
        if not self._agent:
            logger.warning(
                "[DisabledToolsRail] _register_tools: no agent reference")
            return

        for tool_name in tool_names:
            saved_data = self._removed_data.pop(tool_name, None)
            if not saved_data:
                logger.warning(
                    "[DisabledToolsRail] no saved data for tool '%s'", tool_name)
                continue

            tool_id, tool, tool_card = saved_data

            # Step 1: 重新添加 ToolCard 到 ability_manager
            self._agent.ability_manager.add(tool_card)
            logger.info(
                "[DisabledToolsRail] re-registered ToolCard: name=%s", tool_name)

            # Step 2: 重新添加 Tool 到 resource_mgr（如果有 Tool 实例）
            if tool:
                Runner.resource_mgr.add_tool(tool)
                logger.info(
                    "[DisabledToolsRail] re-registered Tool: name=%s, id=%s",
                    tool_name,
                    tool_id,
                )

    def update_config(self, disabled_tools: List[str] | None) -> None:
        """Update disabled tools configuration with hot-reload support.

        Computes diff between old and new disabled_tools, then:
        - Unregisters newly disabled tools
        - Re-registers newly enabled tools

        Args:
            disabled_tools: New list of tool names to disable.
        """
        new_set = set(disabled_tools or [])
        old_set = self._disabled_tools

        # 计算差异
        to_disable = new_set - old_set  # 新增禁用的
        to_enable = old_set - new_set   # 解禁的

        logger.info(
            "[DisabledToolsRail] update_config: old=%s, new=%s, to_disable=%s, to_enable=%s",
            list(old_set),
            list(new_set),
            list(to_disable),
            list(to_enable),
        )

        # 注销新增禁用的工具
        if to_disable:
            self._unregister_tools(to_disable)

        # 重新注册解禁的工具
        if to_enable:
            self._register_tools(to_enable)

        # 更新当前状态
        self._disabled_tools = new_set
