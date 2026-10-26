# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevContext — 每个阶段的执行上下文.

Context 不是 Agent，它是每阶段 StageHandler 的运行环境：
- 持有 deps（外部依赖）和 state（运行时状态）的引用
- 提供 emit() 向前端推送事件
- 提供 create_stage_agent() 为当前阶段创建隔离的 ReActAgent

每阶段独立 Agent 的核心价值：
    - 工具隔离：PLAN 只有搜索，GENERATE 才有文件写入
    - Prompt 隔离：每阶段有焦点明确的专属 system prompt
    - 内存隔离：阶段结束 Agent 即释放，无残留上下文
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, AsyncIterator

from jiuwenswarm.server.runtime.skill.skilldev.deps import SkillDevDeps
from jiuwenswarm.server.runtime.skill.skilldev.schema import (
    SkillDevEvent,
    SkillDevEventType,
    SkillDevState,
)

logger = logging.getLogger(__name__)


class SkillDevContext:
    """阶段执行上下文.

    由 Pipeline 在每阶段入口创建，传递给 StageHandler.execute()。
    """

    def __init__(
        self,
        task_id: str,
        deps: SkillDevDeps,
        state: SkillDevState,
        workspace: Path,
        event_queue: asyncio.Queue,
    ) -> None:
        self.task_id = task_id
        self.deps = deps
        self.state = state
        self.workspace = workspace
        self._event_queue = event_queue

    async def emit(self, event_type: SkillDevEventType, payload: dict) -> None:
        """向前端推送一个事件（放入 Pipeline 的事件队列）."""
        event = SkillDevEvent(
            event_type=event_type,
            payload={"task_id": self.task_id, **payload},
            task_id=self.task_id,
        )
        await self._event_queue.put(event)

    @staticmethod 
    def create_stage_agent(
        stage_name: str,
        system_prompt: str,
        tools: list[str] | None = None,
        max_iterations: int = 20,
    ):
        """为当前阶段创建隔离的 ReActAgent.

        Args:
            stage_name:     阶段标识，用于 agent 命名（调试/日志用）
            system_prompt:  该阶段专属的 system prompt
            tools:          工具名白名单，如 ["file_read", "file_write", "web_search"]
            max_iterations: ReAct 最大循环次数

        Returns:
            配置完毕的 ReActAgent 实例（尚未执行）

        待实现: 接入 openjiuwen ReActAgent 的实际构造逻辑，参考 JiuWenSwarm.create_instance()
        """
        # 待实现: 实际实现
        # from openjiuwen.core.single_agent import AgentCard, ReActAgentConfig
        # from openjiuwen.core.runner import Runner
        # from jiuwenswarm.agentserver.react_agent import JiuSwarmReActAgent
        #
        # agent_card = AgentCard(name=f"skilldev_{self.task_id}_{stage_name}")
        # agent = JiuSwarmReActAgent(agent_card)
        # config = ReActAgentConfig(
        #     model_name=self.deps.model_name,
        #     model_client_config=self.deps.model_client_config,
        #     max_iterations=max_iterations,
        #     prompt_template=[{"role": "system", "content": system_prompt}],
        # )
        # agent.configure(config)
        # if tools:
        #     self._register_tools(agent, tools)
        # return agent
        logger.info(
            "[SkillDevContext] create_stage_agent: stage=%s tools=%s max_iterations=%d",
            stage_name,
            tools,
            max_iterations,
        )
        raise NotImplementedError("create_stage_agent 尚未接入 openjiuwen，待实现")

    def _register_tools(self, agent, tool_names: list[str]) -> None:
        """根据工具名白名单将工具注册到 Agent.

        待实现: 接入实际工具注册逻辑
        """
        # file_read / file_write / shell → 由 SysOperationCard 提供
        # web_search 等 → 从 MCP 工具中筛选
        raise NotImplementedError("_register_tools 尚未实现")
