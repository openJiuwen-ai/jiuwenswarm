# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer 企业配置门面：请求时读库合并.

管理面 HTTP 挂载属 Gateway 进程，见 ``gateway.enterprise``，本模块不依赖 gateway。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentRequest


class ConfigFacade:
    """AgentServer 侧企业配置：load_and_merge（GDB → 本次运行配置）."""

    async def load_and_merge(
        self,
        base: dict,
        request: AgentRequest | None = None,
        **kwargs: Any,
    ) -> dict:
        """请求时加载有效企业配置并合并进 base."""
        from jiuwenswarm.enterprise.config.config_provider import resolve_agent_config

        result = await resolve_agent_config(base, request=request, **kwargs)
        return result.config
