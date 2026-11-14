"""自定义 subagent 定义加载与 SubAgentConfig 转换。

职责：
- 把 AgentDefinition 转成 SubAgentConfig（含 progressive tool rail）
- 从 AgentConfigService 按 enabled 配置加载非 builtin agent

供 Deep / Code 装配路径共用；无 adapter 实例状态。
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.agents.harness.common.rails.disabled_tools_rail import DisabledToolsRail
from jiuwenswarm.common.config import get_config
from jiuwenswarm.server.runtime.agent_adapter.deep.progressive_tools import (
    build_progressive_tool_rail_from_config,
    is_subagent_tool_lazy_load_enabled,
)

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition


def _agent_def_to_subagent_config(
    agent_def: AgentDefinition,
    model: Any,
    workspace: str,
    model_cache: dict[str, Any] | None = None,
    sys_operation: SysOperation | None = None,
) -> SubAgentConfig:
    """将 AgentDefinition 转换为 SubAgentConfig，用于 SubagentRail 注册。

    Args:
        agent_def: 自定义 agent 定义（来自 .jiuwenswarm/agents/*.md）
        model: 父 agent 的 Model 实例（作为默认模型）
        workspace: 工作空间路径
        model_cache: 模型缓存字典（用于按名称查找指定模型）
        sys_operation: 父 agent 的 SysOperation。子 agent 必须复用同一文件系统
            边界。``DeepAgent.create_subagent`` 只在 ``spec.workspace`` 同时非空时
            才采用 ``spec.sys_operation``，因此 workspace 也要一并写入 spec，否则
            会落到一套带 ``restrict_to_sandbox`` 约束的新 LOCAL SysOperation。
    """
    # Resolve model: if agent_def specifies a model name, look it up in cache
    resolved_model = model
    if agent_def.model and isinstance(model_cache, dict):
        resolved_model = model_cache.get(agent_def.model, model)

    # Build tool list: merge allowed tools and disallowed_tools
    tools: list[str] = list(agent_def.tools) if agent_def.tools else ["*"]
    if agent_def.disallowed_tools and tools != ["*"]:
        tools = [t for t in tools if t not in agent_def.disallowed_tools]

    # A stable id keeps ``create_deep_agent`` on its get-or-create path for this
    # sub-agent's SysOperation: the id is derived from the card, and the default
    # ``AgentCard.id`` is a fresh uuid, so leaving it unset made every Agent-tool
    # invocation register another SysOperation plus its ~16 derived tools in the
    # process-global resource manager, never to be reclaimed. Same-named
    # definitions carry the same permissions, so sharing one instance is safe.
    card = AgentCard(
        name=agent_def.name,
        id=f"subagent_{agent_def.name}",
        description=agent_def.description,
    )

    # Build progressive tool rail for custom subagent if enabled.
    custom_rails: list[Any] | None = None
    config_base = get_config()
    react_config = config_base.get("react", {}) or {}
    if is_subagent_tool_lazy_load_enabled(react_config):
        progressive_rail = build_progressive_tool_rail_from_config(
            react_config,
            language=resolve_language(
                str(config_base.get("preferred_language", "zh"))
            ),
            profile="subagent",
            agent_id=card.id,
            agent_card_id=card.id,
        )
        if progressive_rail is not None:
            custom_rails = [progressive_rail]

    if "ttse_consult" in (agent_def.disallowed_tools or []):
        custom_rails = [*(custom_rails or []), DisabledToolsRail(["ttse_consult"])]

    return SubAgentConfig(
        agent_card=card,
        system_prompt=agent_def.prompt,
        tools=tools,
        model=resolved_model,
        workspace=workspace,
        sys_operation=sys_operation,
        skills=agent_def.skills,
        max_iterations=agent_def.max_iterations,
        enable_task_loop=True,
        rails=custom_rails,
    )


def _load_custom_subagents(
    workspace_dir: str,
    subagents_cfg: dict | None,
    model: Any,
    workspace: str,
    logger_name: str,
    **kwargs: Any,
) -> list[Any]:
    """从 AgentConfigService 加载自定义 agent 并转换为 SubAgentConfig 列表。

    通用逻辑，同时被 JiuWenSwarmDeepAdapter 和 JiuWenSwarmCodeAdapter 使用。

    Args:
        workspace_dir: 工作空间目录路径
        subagents_cfg: 子 agent 配置字典
        model: 模型配置
        workspace: 工作空间路径
        logger_name: 日志记录器名称
        **kwargs: 额外参数，支持 model_cache、sys_operation 等
    """
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    _logger = logging.getLogger(logger_name)
    agent_service = AgentConfigService(workspace_dir)
    model_cache: dict | None = kwargs.get("model_cache")
    sys_operation = kwargs.get("sys_operation")
    result: list[Any] = []
    for agent_def in agent_service.list_agents():
        if agent_def.source == "builtin":
            continue
        subagent_cfg = subagents_cfg.get(agent_def.name) if isinstance(subagents_cfg, dict) else None
        # 只有显式 enabled: true 才加载
        if not (isinstance(subagent_cfg, dict) and bool(subagent_cfg.get("enabled", False))):
            continue
        custom_spec = _agent_def_to_subagent_config(
            agent_def, model, workspace, model_cache, sys_operation,
        )
        custom_spec.factory_kwargs = {"auto_create_workspace": False}
        result.append(custom_spec)
        _logger.info("loaded custom agent '%s' from %s", agent_def.name, agent_def.source)
    return result
