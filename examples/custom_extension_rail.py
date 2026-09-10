#!/usr/bin/env python
"""自定义 ExtensionConfig Rail 示例：通过环境变量非侵入式注册。

使用方式：
    export AGENT_EXTRA_RAILS=examples.custom_extension_rail
    # 然后启动 AgentServer

    # 可选：开启内置 ExtensionConfigDebugRail（默认关闭）
    # export AGENT_EXTENSION_CONFIG_DEBUG_RAIL=1

本模块演示如何从 run_context.extra 读取 extension_config 并打日志。
示例只验证「能读到配置」，不真正拦截工具。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.extension_config_util import (
    get_extension_config_from_ctx,
)

logger = logging.getLogger(__name__)


class CustomExtensionConfigRail(DeepAgentRail):
    """示例：自定义 Rail，消费 extension_config（只读日志，不拦截）。

    假设 extension_config 包含如下结构::

        [
            {
                "template_id": "tpl-001",
                "template_name": "限制工具调用",
                "component": "agent_server",
                "hook_type": "pre_request",
                "hook_config": {
                    "handler": "hooks.limit_tools",
                    "params": {"allowed_tools": ["bash", "read_file"]}
                }
            }
        ]
    """

    priority = 50  # 在 ExtensionConfigDebugRail (45) 之后，TaskExecutionRail (85) 之前

    @staticmethod
    def _get_extension_config(ctx: AgentCallbackContext) -> list[dict[str, Any]] | None:
        """从 AgentCallbackContext 提取 extension_config。"""
        return get_extension_config_from_ctx(ctx)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """invoke 开始时打印已加载的扩展配置。"""
        ext_config = self._get_extension_config(ctx)
        if ext_config:
            logger.info(
                "[CustomExtensionConfigRail] before_invoke: loaded %d config(s)",
                len(ext_config),
            )
            for cfg in ext_config:
                hook_config = cfg.get("hook_config", {}) if isinstance(cfg, dict) else {}
                params = hook_config.get("params", {}) if isinstance(hook_config, dict) else {}
                logger.info(
                    "[CustomExtensionConfigRail]   - handler=%s params=%s",
                    hook_config.get("handler") if isinstance(hook_config, dict) else None,
                    params,
                )
        else:
            logger.debug("[CustomExtensionConfigRail] before_invoke: no extension_config")

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """工具调用前打印配置摘要（演示读取，不拦截）。"""
        ext_config = self._get_extension_config(ctx)
        if not ext_config:
            return

        for cfg in ext_config:
            if not isinstance(cfg, dict):
                continue
            hook_config = cfg.get("hook_config", {})
            if not isinstance(hook_config, dict):
                continue
            params = hook_config.get("params", {})
            if not isinstance(params, dict):
                continue
            allowed_tools = params.get("allowed_tools")
            if allowed_tools:
                logger.info(
                    "[CustomExtensionConfigRail] before_tool_call: "
                    "allowed_tools=%s (demo log only)",
                    allowed_tools,
                )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """工具调用后执行清理逻辑。"""
        pass


def register_rails() -> list[DeepAgentRail]:
    """注册入口：返回要挂载到 DeepAgent 的 Rail 实例列表。

    框架会通过 importlib 动态导入本模块并调用此函数。
    """
    return [CustomExtensionConfigRail()]
