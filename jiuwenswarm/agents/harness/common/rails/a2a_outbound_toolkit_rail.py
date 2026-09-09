"""Lifecycle rail for the three built-in A2A outbound Agent tools."""

from __future__ import annotations

from typing import Any, Callable

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.tools.a2a_outbound_tools import (
    A2AOutboundToolBackend,
    A2AOutboundToolkit,
    GatewayA2AOutboundToolBackend,
)

_SECTION_NAME = "a2a_outbound_usage"
_TOOL_NAMES = ("a2a_find_agents", "a2a_dispatch_task", "a2a_get_dispatch")


class A2AOutboundToolkitRail(DeepAgentRail):
    """Expose A2A tools only while a Gateway reverse-RPC owner is available."""

    priority = 45

    def __init__(
        self,
        *,
        backend_provider: Callable[[], A2AOutboundToolBackend | None] | None = None,
        runtime_route: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._backend_provider = backend_provider or GatewayA2AOutboundToolBackend
        self._runtime_route = runtime_route
        self._registered: list[str] = []
        self._prompt_builder: Any = None
        self._agent: Any = None

    def init(self, agent: Any) -> None:
        self._agent = agent
        self._prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._try_register(agent)

    def _try_register(self, agent: Any) -> None:
        backend = self._backend_provider()
        if backend is None or not backend.ready:
            self._unregister_tools(agent)
            return
        if self._registered:
            return
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return
        toolkit = (
            A2AOutboundToolkit(backend, runtime_route=self._runtime_route)
            if self._runtime_route is not None
            else A2AOutboundToolkit(backend)
        )
        tools = toolkit.get_tools()
        for tool in tools:
            ability_manager.add_ability(tool.card, tool)
            self._registered.append(tool.card.name)
        self._sync_prompt()

    def uninit(self, agent: Any) -> None:
        self._unregister_tools(agent)
        self._prompt_builder = None
        self._agent = None

    def _unregister_tools(self, agent: Any) -> None:
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is not None:
            for name in reversed(self._registered):
                ability_manager.remove_ability(name)
        self._registered.clear()
        builder = getattr(agent, "system_prompt_builder", None) or self._prompt_builder
        if builder is not None:
            builder.remove_section(_SECTION_NAME)

    async def before_model_call(self, ctx: Any) -> None:
        live_agent = getattr(ctx, "agent", None) or self._agent
        if live_agent is not None:
            self._try_register(live_agent)
        if not self._registered:
            return
        live_builder = getattr(
            getattr(ctx, "agent", None), "system_prompt_builder", None
        )
        if live_builder is not None:
            self._prompt_builder = live_builder
        self._sync_prompt()

    def _sync_prompt(self) -> None:
        if self._prompt_builder is None or not self._registered:
            return
        language = getattr(self._prompt_builder, "language", "cn") or "cn"
        content = (
            """## A2A 出站工具

需要调用外部 A2A Agent 时：
1. 首次用 a2a_find_agents(query="", required_skills=[]) 列出已注册且可用的目录；不要用“可用的智能体”等泛化描述做首查；
2. query 只用于相关性排序，不会隐藏可调用候选；仅在明确需要某项能力时使用精确 required_skills 过滤；
3. 从返回目录中选择与任务技能匹配的 agent_id；
4. 需要立即取得答案时用 sync，长任务或不阻塞当前轮次时用 async；
5. async 返回后仅用 a2a_get_dispatch(dispatch_id) 查询，不自行构造远端请求。"""
            if language == "cn"
            else """## A2A outbound tools

When calling an external A2A Agent:
1. First use a2a_find_agents(query="", required_skills=[]) to list the callable catalog; do not start with a generic phrase such as "available agents".
2. query ranks but never hides callable candidates; use exact required_skills only for explicit capability constraints.
3. Select a skill-matching agent_id from the returned catalog.
4. Use sync for an immediate answer and async for long-running work.
5. After async dispatch, query only with a2a_get_dispatch(dispatch_id); never construct a remote request."""
        )
        self._prompt_builder.add_section(
            PromptSection(
                name=_SECTION_NAME,
                content={language: content},
                priority=self.priority,
            )
        )


__all__ = ["A2AOutboundToolkitRail"]
