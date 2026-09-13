# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ExtensionConfigDebugRail - 用于验证扩展配置端到端联调的调试 Rail。

在企业级配置（Enterprise Config）下发链路中，Manager 将 ``extension_config``
下发到 Gateway，AgentServer 从企业策略加载并注入 ``run_context.extra``。
本 Rail 在 invoke / 工具调用前打印**脱敏**摘要，验证扩展配置是否到达 AgentServer。

默认不挂载；企业版需显式开启::

    export AGENT_EXTENSION_CONFIG_DEBUG_RAIL=1
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.extension_config_util import (
    get_extension_config_from_ctx,
    summarize_extension_config_for_log,
)

logger = logging.getLogger(__name__)


def _template_label(cfg: dict[str, Any]) -> Any:
    return (
        cfg.get("template_name")
        or cfg.get("name")
        or cfg.get("template_id")
        or cfg.get("id")
    )


class ExtensionConfigDebugRail(DeepAgentRail):
    """在工具调用前打印扩展配置脱敏摘要的调试 Rail（需环境变量开启）。"""

    priority = 45  # 比 TaskExecutionRail (85) 更优先执行

    @staticmethod
    def _get_extension_config(ctx: AgentCallbackContext) -> list[dict[str, Any]] | None:
        """从 AgentCallbackContext 提取 extension_config。"""
        return get_extension_config_from_ctx(ctx)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """每次 invoke 开始时打印扩展配置摘要。"""
        ext_config = self._get_extension_config(ctx)
        if ext_config:
            logger.info(
                "[ExtensionConfigDebugRail] before_invoke: extension_config present, "
                "count=%d, names=%s",
                len(ext_config),
                [_template_label(c) for c in ext_config if isinstance(c, dict)],
            )
        else:
            logger.debug(
                "[ExtensionConfigDebugRail] before_invoke: no extension_config found"
            )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """工具调用前打印脱敏摘要（不含 hook_config / params）。"""
        ext_config = self._get_extension_config(ctx)
        if ext_config:
            logger.info(
                "[ExtensionConfigDebugRail] before_tool_call: extension_config present, "
                "count=%d, summary=%s",
                len(ext_config),
                summarize_extension_config_for_log(ext_config),
            )
        else:
            logger.debug(
                "[ExtensionConfigDebugRail] before_tool_call: no extension_config found"
            )
