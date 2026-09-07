# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""McpProjectIdRail -- 在 MCP 工具调用前自动注入项目隔离绑定。

MCP server 通常是长驻进程，无法通过进程环境变量动态切换会话与
项目作用域。此 Rail 从当前 session 的元数据中获取 session_id /
project_id / project_dir，并在 MCP 工具调用前以宿主信任值写入 tool_args。

优先级 55：在 StreamEventRail(80) 与 UserHookRail(60) 之后执行。
StreamEventRail 先清理 call_goal 并发送工具调用事件，用户 hook 随后处理，
本 Rail 最后注入项目绑定。
"""
from __future__ import annotations

import json
import os

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

class McpProjectIdRail(DeepAgentRail):
    """自动为 MCP 工具调用注入项目隔离绑定。

    优先调用 AbilityManager 的 MCP scope 解析器识别工具，兼容模型可见的
    ``mcp_<server>_<tool>`` 名称和资源 ID，不依赖具体 MCP server 或工具名。
    """

    priority = 55

    # 会话与项目绑定的参数键名（注入到 MCP 工具参数中）
    SESSION_ID_KEY = "session_id"
    PROJECT_ID_KEY = "project_id"
    PROJECT_DIR_KEY = "project_dir"

    def __init__(self, session_id: str | None = None) -> None:
        super().__init__()
        # 每个 DeepAgent 由 session-scoped adapter 独占。将其会话 id 固定在
        # Rail 上，避免工具调用运行于创建请求之前的常驻任务时丢失 ContextVar。
        self._bound_session_id = (
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """在 MCP 工具调用前注入 session_id / project_id / project_dir。"""
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "").strip()

        if not self._is_mcp_tool(ctx, tool_name):
            return

        scope_keys = self._declared_scope_keys(ctx, tool_name)
        if not scope_keys:
            logger.debug("[MCP Scope] skipped tool=%s reason=no_scope_schema", tool_name)
            return

        tool_args = getattr(ctx.inputs, "tool_args", None)
        # BEFORE_TOOL_CALL 早于 AbilityManager 的统一参数解析。模型返回的
        # function arguments 在这里通常还是 JSON 字符串，而 Stdio 调用日志
        # 显示的是后续解析后的 dict。解析成功后回写上下文，供执行器复用。
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except json.JSONDecodeError:
                return
        if not isinstance(tool_args, dict):
            return
        ctx.inputs.tool_args = tool_args

        # scope 是宿主隔离边界，不能信任模型生成的同名入参。先清掉
        # 请求中的 scope，再以 session metadata / 宿主环境的值覆盖写入。
        binding = self._resolve_project_binding(ctx)
        session_id = self._resolve_session_id(ctx)
        if session_id:
            binding[self.SESSION_ID_KEY] = session_id
        replaced = [
            key for key in scope_keys
            if key in tool_args and tool_args.get(key) != binding.get(key)
        ]
        removed = [key for key in scope_keys if key in tool_args and key not in binding]
        for key in scope_keys:
            tool_args.pop(key, None)
        applied = {
            key: value
            for key, value in binding.items()
            if key in scope_keys and value
        }
        tool_args.update(applied)
        logger.info(
            "[MCP Scope] tool=%s session=%s project_id=%s "
            "project_dir=%s applied=%s replaced=%s removed=%s",
            tool_name,
            binding.get(self.SESSION_ID_KEY, "<missing>"),
            binding.get(self.PROJECT_ID_KEY, "<missing>"),
            binding.get(self.PROJECT_DIR_KEY, "<missing>"),
            ",".join(sorted(applied)) or "<none>",
            ",".join(sorted(replaced)) or "<none>",
            ",".join(sorted(removed)) or "<none>",
        )
        ctx.inputs.tool_args = tool_args

        # 同步修改 ToolCall.arguments（与 StreamEventRail 同模式，
        # 确保 AbilityManager._execute_single_tool_call 从 ToolCall
        # 重新解析参数时也能拿到 project_id）
        tc = getattr(ctx.inputs, "tool_call", None)
        if tc is not None:
            try:
                existing_args = getattr(tc, "arguments", None)
                if isinstance(existing_args, dict):
                    for key in scope_keys:
                        existing_args.pop(key, None)
                    existing_args.update(applied)
                else:
                    tc.arguments = tool_args
            except (AttributeError, TypeError) as exc:
                logger.warning(
                    "[McpProjectIdRail] rewrite ToolCall.arguments failed: %s", exc
                )

    @staticmethod
    def _is_mcp_tool(ctx: AgentCallbackContext, tool_name: str) -> bool:
        """使用 AbilityManager 的注册信息识别 MCP 工具。

        旧 core / 聚焦单测中可能没有 scope 解析器，此时仅对标准模型可见
        ``mcp_`` 前缀做兼容。不再用点号猜测，避免改变名称含点的内置工具。
        """
        agent = getattr(ctx, "agent", None)
        ability_manager = getattr(agent, "ability_manager", None)
        resolver = getattr(ability_manager, "_resolve_mcp_tool_scope", None)
        if callable(resolver):
            try:
                return resolver(tool_name) is not None
            except (AttributeError, TypeError, ValueError):
                return False
        return tool_name.startswith("mcp_")

    @classmethod
    def _declared_scope_keys(cls, ctx: AgentCallbackContext, tool_name: str) -> tuple[str, ...]:
        """仅向 schema 显式声明的字段注入，保持其他 MCP 工具的协议不变。"""
        manager = getattr(getattr(ctx, "agent", None), "ability_manager", None)
        cards = getattr(manager, "_tools", {})
        if not isinstance(cards, dict):
            return ()
        card = cards.get(tool_name)
        if card is None:
            card = next(
                (item for item in cards.values() if getattr(item, "id", None) == tool_name),
                None,
            )
        schema = getattr(card, "input_params", None)
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not isinstance(properties, dict):
            return ()
        return tuple(
            key for key in (cls.SESSION_ID_KEY, cls.PROJECT_ID_KEY, cls.PROJECT_DIR_KEY)
            if isinstance(properties.get(key), dict) and properties[key].get("type") == "string"
        )

    # ------------------------------------------------------------------
    # Internal: session_id 解析
    # ------------------------------------------------------------------

    def _resolve_session_id(self, ctx: AgentCallbackContext) -> str | None:
        """从 AgentCallbackContext 提取 session_id。

        参考 CsplSentinelRail._resolve_session_id() 和
        CodeTaskPlanningRail._session_id() 的模式：
        - ctx.session.get_session_id()（DeepAgent 注入）
        - ctx.session_id / ctx.conversation_id（部分上下文）
        - ctx.inputs.session_id（工具调用上下文）
        """
        # session-scoped adapter 的业务会话与 MemoryHook 使用同一个 ID。
        # core 的内部 session 可能是 task-loop / 子会话 ID，不能优先用于查项目元数据。
        bound_session_id = getattr(self, "_bound_session_id", None)
        if isinstance(bound_session_id, str) and bound_session_id.strip():
            return bound_session_id.strip()

        # 路径 1: ctx.session 对象
        session = getattr(ctx, "session", None)
        if session is not None and hasattr(session, "get_session_id"):
            sid = session.get_session_id()
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
        # 路径 2: ctx 上的 session_id / conversation_id 属性
        for attr in ("session_id", "conversation_id"):
            value = getattr(ctx, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # 路径 3: ctx.inputs 上的 session_id
        inputs = getattr(ctx, "inputs", None)
        for attr in ("session_id", "conversation_id"):
            value = getattr(inputs, attr, None) if inputs else None
            if isinstance(value, str) and value.strip():
                return value.strip()

        # 路径 5: Jiuwen 的请求执行上下文。常规 chat 调用会在进入
        # process_message_impl 前绑定 CURRENT_SESSION_ID，但 agent-core 的
        # before_tool_call 回调不会把该值挂到 AgentCallbackContext 上。
        # 这是 MCP 工具调用实际可用的稳定兜底，且 ContextVar 会随当前
        # asyncio 任务传播，不会串到其他会话。
        try:
            from jiuwenswarm.agents.harness.common.channel_runtime_context import (
                CURRENT_SESSION_ID,
            )

            sid = CURRENT_SESSION_ID.get()
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
        except (ImportError, LookupError):
            pass
        return None

    # ------------------------------------------------------------------
    # Internal: 项目绑定解析
    # ------------------------------------------------------------------

    def _resolve_project_binding(self, ctx: AgentCallbackContext) -> dict[str, str]:
        """从 session 元数据获取 project_id / project_dir。

        优先级：
        1. ctx 关联的 session_metadata["project_id" / "project_dir"]
        2. 运行时 contextvar 中的 cron metadata
        3. 环境变量 GSPD_CELIAWORK_PROJECT_ID（兜底）
        """
        binding: dict[str, str] = {}

        # 路径 1: session 元数据
        session_id = self._resolve_session_id(ctx)
        if session_id:
            try:
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata,
                )
                metadata = get_session_metadata(session_id)
                if isinstance(metadata, dict):
                    for key in (self.PROJECT_ID_KEY, self.PROJECT_DIR_KEY):
                        value = str(metadata.get(key, "") or "").strip()
                        if value:
                            binding[key] = value
            except Exception as exc:
                logger.debug(
                    "[McpProjectIdRail] session metadata lookup failed: %s", exc
                )

        # 路径 2: cron 工具的 contextvar metadata
        try:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
                _CRON_TOOL_METADATA,
            )
            cron_meta = _CRON_TOOL_METADATA.get()
            if isinstance(cron_meta, dict):
                for key in (self.PROJECT_ID_KEY, self.PROJECT_DIR_KEY):
                    if key in binding:
                        continue
                    value = str(cron_meta.get(key, "") or "").strip()
                    if value:
                        binding[key] = value
        except (ImportError, AttributeError):
            pass

        # 路径 3: 环境变量兜底（仅旧单项目 project_id，无目录可用）。
        if self.PROJECT_ID_KEY not in binding:
            value = os.getenv("GSPD_CELIAWORK_PROJECT_ID", "").strip()
            if value:
                binding[self.PROJECT_ID_KEY] = value
        return binding

    def _resolve_project_id(self, ctx: AgentCallbackContext) -> str:
        """兼容旧调用方：只读取 project_id。"""
        return self._resolve_project_binding(ctx).get(self.PROJECT_ID_KEY, "")


__all__ = ["McpProjectIdRail"]
